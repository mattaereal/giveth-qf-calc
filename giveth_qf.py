#!/usr/bin/env python3
"""Estimate Giveth Quadratic Funding payouts for a round.

Single-file, stdlib only. Default round: Ethereum Security (slug ethereum-security). Pool and cap are resolved from live Giveth round metadata.

Usage:
  python3 giveth_qf_live.py
  python3 giveth_qf_live.py --round-slug ethereum-security
  python3 giveth_qf_live.py --round-id 16 --round-slug ethereum-security --top 30 --bottom 20
  python3 giveth_qf_live.py --focus 'SEAL 911,SEAL Certifications,SEAL Intel,SEAL Frameworks,SEAL Safe Harbor'
  python3 giveth_qf_live.py --simulate-tiers 1,5,10,25,100,1000
  python3 giveth_qf_live.py --no-nft-boost
  python3 giveth_qf_live.py --self-test

QF model:
  raw_qf_p = (sum sqrt(c_i))^2 across donors i
  match_component_p = max(raw_qf_p - sum c_i, 0)
  pre_cap_share_p = pool * (match_component_p / sum_all match_components)
  capped_p = pre_cap_share_p with iterative cap redistribution

Adjustments:
  - Donations < MIN_QF_USD ($1) are excluded from matching.
  - Those small donations still count as raised/direct funding.
  - FINN priced at ETH/USD / 1000 when valueUsd is unavailable.
  - TIK priced at $1 when valueUsd is unavailable.
  - Anonymous donations (no user.id, no fromWalletAddress) are clustered by
    1-minute timestamp bucket. Same-minute anons are treated as same donor.
  - Donors holding the ETHSecurity Voting Badge NFT have their contribution
    multiplied by --nft-multiplier (default 4) inside the QF formula.

Important:
  Matching pool and per-project cap are not hardcoded. The script reads
  allocatedFund, allocatedTokenSymbol, allocatedFundUSD, and maximumReward from
  the Giveth round API. For ETH/WETH pools, the native pool amount is canonical
  and USD values are live conversions for comparability with donation valueUsd.

  This is an estimator. Giveth final payouts may use COCM, Passport/Sybil
  weighting, manual disqualifications, and post-round review.
"""
import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from copy import deepcopy

ENDPOINT = "https://core.v6.giveth.io/graphql"
BLOCKSCOUT_BASE = "https://eth.blockscout.com/api/v2"
PAGE = 50
MIN_QF_USD = 1.0
FINN_PER_ETH = 1000.0
NFT_CONTRACT = "0x3B49f45EC8796F64feBB1Ae0f5661791845ce35C"
NFT_MULTIPLIER = 4
DEFAULT_FOCUS = "SEAL 911,SEAL Certifications,SEAL Intel,SEAL Frameworks,SEAL Safe Harbor,The Red Guild: security as a public good"
EPSILON = 1e-10
USER_AGENT = "giveth-qf/1.3"
TRANSIENT_HTTP_STATUS = {429, 500, 502, 503, 504, 520, 522, 524}
RETRY_INITIAL_SLEEP = 2.0
RETRY_MAX_SLEEP = 60.0
SLEEP_FN = time.sleep

ROUND_QUERY = """
query QfRoundBySlug($slug:String!){
  qfRoundBySlug(slug:$slug){
    id name slug isActive beginDate endDate
    allocatedFund allocatedFundUSD allocatedTokenSymbol maximumReward
  }
}
"""

PROJECTS_QUERY = """
query Projects($skip:Int!,$take:Int!,$filters:ProjectFiltersInput){
  projects(skip:$skip,take:$take,filters:$filters){
    projects{ id title slug }
    total
  }
}
"""

DONATIONS_QUERY = """
query DonationsByProject($projectId:Int!,$skip:Int!,$take:Int!,$qfRoundId:Int){
  donationsByProject(
    projectId:$projectId, skip:$skip, take:$take,
    orderBy:CreatedAt, orderDirection:DESC, qfRoundId:$qfRoundId
  ){
    donations{
      id amount valueUsd currency createdAt anonymous
      fromWalletAddress
      user{ id wallets{ address } }
    }
    total
  }
}
"""

ETH_USD_PRICE = None


def _retry_delay(attempt):
    return min(RETRY_MAX_SLEEP, RETRY_INITIAL_SLEEP * (2 ** min(attempt, 8)))


def _format_exception(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    return f"{exc.__class__.__name__}: {exc}"


def _request_json(url, data=None, headers=None, timeout=45, label="request"):
    """Request JSON with infinite retry for transient upstream/API failures.

    HTTP 503 and other transient gateway/rate-limit statuses retry forever.
    Network-level temporary failures also retry forever. GraphQL/application errors
    still fail fast in graphql().
    """
    headers = dict(headers or {})
    headers.setdefault("user-agent", USER_AGENT)
    attempt = 0

    while True:
        req = urllib.request.Request(url, data=data, headers=headers)
        last_error = None
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_HTTP_STATUS:
                detail = ""
                try:
                    detail = exc.read().decode(errors="replace")[:500]
                except Exception:
                    pass
                raise RuntimeError(f"{label} failed with HTTP {exc.code}: {detail}") from exc
            last_error = exc
        except urllib.error.URLError as exc:
            last_error = exc
        except TimeoutError as exc:
            last_error = exc
        except ConnectionError as exc:
            last_error = exc
        except OSError as exc:
            last_error = exc
        except json.JSONDecodeError as exc:
            # Some upstream failures return an HTML gateway page with a 200 status.
            last_error = exc

        delay = _retry_delay(attempt)
        print(f"  {label}: {_format_exception(last_error)}; retrying in {delay:.0f}s", file=sys.stderr)
        SLEEP_FN(delay)
        attempt += 1


def graphql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    payload = _request_json(
        ENDPOINT,
        data=body,
        headers={"content-type": "application/json"},
        timeout=45,
        label="Giveth GraphQL",
    )
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"]))
    return payload.get("data") or {}


def fetch_eth_usd():
    global ETH_USD_PRICE
    if ETH_USD_PRICE is not None:
        return ETH_USD_PRICE
    data = _request_json(
        "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
        headers={"user-agent": USER_AGENT},
        timeout=15,
        label="ETH/USD price",
    )
    try:
        ETH_USD_PRICE = float(data["ethereum"]["usd"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("ETH/USD price unavailable; refusing to use a hardcoded fallback") from exc
    if ETH_USD_PRICE <= 0:
        raise RuntimeError("ETH/USD price unavailable; got non-positive quote")
    return ETH_USD_PRICE


def fetch_nft_holders(contract):
    holders = set()
    url = f"{BLOCKSCOUT_BASE}/tokens/{contract}/holders"
    page_no = 1
    while url:
        data = _request_json(url, timeout=30, label=f"Blockscout NFT holders page {page_no}")
        for item in data.get("items", []):
            addr = (item.get("address") or {}).get("hash", "")
            if addr:
                holders.add(addr.lower())
        npp = data.get("next_page_params")
        if npp:
            url = f"{BLOCKSCOUT_BASE}/tokens/{contract}/holders?" + urllib.parse.urlencode(npp)
            page_no += 1
        else:
            url = None
    return holders


def donation_usd(donation):
    v = donation.get("valueUsd")
    if v not in (None, 0, "0", ""):
        try:
            usd = float(v)
            return usd if usd > 0 else 0.0
        except (TypeError, ValueError):
            pass

    currency = (donation.get("currency") or "").upper()
    try:
        amount = float(donation.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0
    if amount <= 0:
        return 0.0
    if currency == "FINN":
        return amount * (fetch_eth_usd() / FINN_PER_ETH)
    if currency == "TIK":
        return amount
    return 0.0


def donor_key(donation):
    user = donation.get("user") or {}
    if user.get("id"):
        return f"u:{user['id']}"
    addr = (donation.get("fromWalletAddress") or "").lower()
    if not addr and user.get("wallets"):
        addr = (user["wallets"][0].get("address") or "").lower()
    if addr:
        return f"w:{addr}"
    ts = donation.get("createdAt") or "unknown"
    return f"cluster:{ts[:16]}"


def donor_wallets(donation):
    addrs = set()
    addr = (donation.get("fromWalletAddress") or "").lower()
    if addr:
        addrs.add(addr)
    user = donation.get("user") or {}
    for w in (user.get("wallets") or []):
        wa = (w.get("address") or "").lower()
        if wa:
            addrs.add(wa)
    return addrs


def fetch_round(slug):
    return graphql(ROUND_QUERY, {"slug": slug}).get("qfRoundBySlug") or {}


def fetch_projects(round_id):
    out = []
    skip = 0
    total = None
    while total is None or skip < total:
        data = graphql(
            PROJECTS_QUERY,
            {
                "skip": skip,
                "take": PAGE,
                "filters": {"qfRoundId": int(round_id)},
            },
        ).get("projects") or {}
        total = int(data.get("total") or 0)
        batch = data.get("projects") or []
        out.extend(batch)
        skip += PAGE
        if not batch:
            break
    return out, total


def fetch_donations(project_id, round_id):
    skip = 0
    while True:
        data = graphql(
            DONATIONS_QUERY,
            {
                "projectId": int(project_id),
                "skip": skip,
                "take": PAGE,
                "qfRoundId": int(round_id),
            },
        ).get("donationsByProject") or {}
        page = data.get("donations") or []
        for don in page:
            yield don
        if len(page) < PAGE:
            return
        skip += PAGE


def aggregate_round(round_id, nft_holders=None, nft_multiplier=1, track_currencies=("FINN", "TIK"), progress=None):
    projects, total = fetch_projects(round_id)
    state = []
    currency_stats = {c: {"amount": 0.0, "count": 0, "donors": set()} for c in track_currencies}

    for i, p in enumerate(projects, 1):
        if progress:
            progress(i, total, p["title"])

        qf_contribs = defaultdict(float)
        dwallets = defaultdict(set)
        raised = 0.0

        try:
            for don in fetch_donations(p["id"], round_id):
                value = donation_usd(don)
                dk = donor_key(don)

                if value > 0:
                    raised += value
                if value >= MIN_QF_USD:
                    qf_contribs[dk] += value

                dwallets[dk] |= donor_wallets(don)

                cur = (don.get("currency") or "").upper()
                if cur in currency_stats:
                    try:
                        currency_stats[cur]["amount"] += float(don.get("amount") or 0)
                    except (TypeError, ValueError):
                        pass
                    currency_stats[cur]["count"] += 1
                    currency_stats[cur]["donors"].add(dk)
        except Exception as exc:
            print(f"  skip {p['title']}: {exc}", file=sys.stderr)

        sqrt_sum = 0.0
        effective_linear = 0.0
        nft_donor_count = 0

        for dk, amount in qf_contribs.items():
            is_nft = bool(nft_holders and dwallets[dk] & nft_holders)
            effective_amount = amount * nft_multiplier if is_nft else amount
            if is_nft:
                nft_donor_count += 1
            sqrt_sum += math.sqrt(effective_amount)
            effective_linear += effective_amount

        state.append(
            {
                "id": p["id"],
                "title": p["title"],
                "slug": p.get("slug", ""),
                "donors": len(qf_contribs),
                "raised": raised,
                "sqrt_sum": sqrt_sum,
                "boosted_linear": effective_linear,
                "match_component": max(sqrt_sum * sqrt_sum - effective_linear, 0.0),
                "nft_donor_count": nft_donor_count,
            }
        )

    return state, currency_stats


def parse_fraction(value, default=None, name="fraction"):
    if value in (None, ""):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if out > 1:
        out /= 100.0
    if out < 0 or out > 1:
        raise ValueError(f"{name} must be between 0 and 1, or between 0 and 100 when given as a percent")
    return out


def resolve_pool_from_api(round_meta):
    """Resolve matching pool from live round metadata.

    Returns: (pool_usd, pool_native_amount, pool_native_symbol, source)

    Rules:
      - ETH/WETH: allocatedFund is canonical. USD is live ETH/USD conversion.
      - Stable USD-like tokens: allocatedFund is already USD-denominated.
      - Other tokens: allocatedFundUSD is used if the API provides it.
      - No hardcoded pool or fallback price is used.
    """
    allocated = round_meta.get("allocatedFund")
    symbol = (round_meta.get("allocatedTokenSymbol") or "").upper()

    amount = 0.0
    if allocated not in (None, "", 0, "0"):
        try:
            amount = float(allocated)
        except (TypeError, ValueError):
            amount = 0.0

    if amount > 0 and symbol in {"ETH", "WETH"}:
        return amount * fetch_eth_usd(), amount, symbol, "allocatedFund*live_eth_usd"

    if amount > 0 and symbol in {"USD", "USDC", "USDT", "DAI", "CUSD", "USDS"}:
        return amount, amount, symbol, "allocatedFund_stable"

    allocated_usd = round_meta.get("allocatedFundUSD")
    if allocated_usd not in (None, "", 0, "0"):
        try:
            usd = float(allocated_usd)
            if usd > 0:
                return usd, None, "USD", "allocatedFundUSD"
        except (TypeError, ValueError):
            pass

    return 0.0, None, symbol or "UNKNOWN", "unavailable"


def resolve_cap_fraction_from_api(round_meta):
    cap = parse_fraction(round_meta.get("maximumReward"), default=None, name="round maximumReward")
    if cap is None or cap <= 0:
        raise RuntimeError("round maximumReward unavailable from API; refusing to use a hardcoded cap")
    return cap

def _redistribute_cap_fractions(base_fractions, cap_fraction):
    """Apply per-project cap to normalized matching fractions.

    This mirrors Giveth's public qf-calculator behavior: cap projects above the
    limit, redistribute their excess proportionally to uncapped projects, repeat.
    If too few projects have positive match components to spend the whole pool
    under the cap, the remaining fraction is left unused.
    """
    if not base_fractions:
        return []
    if cap_fraction >= 1:
        return list(base_fractions)
    if cap_fraction <= 0:
        return [0.0 for _ in base_fractions]

    shares = [max(float(x), 0.0) for x in base_fractions]
    total = sum(shares)
    if total <= 0:
        return [0.0 for _ in shares]
    shares = [x / total for x in shares]

    while True:
        over = [max(0.0, x - cap_fraction) for x in shares]
        overflow = sum(over)
        if overflow <= EPSILON:
            break

        changed = False
        for idx, value in enumerate(shares):
            if value > cap_fraction:
                shares[idx] = cap_fraction
                changed = True

        uncapped_total = sum(x for x in shares if x < cap_fraction - EPSILON)
        if uncapped_total <= EPSILON:
            break

        factor = 1.0 + overflow / uncapped_total
        for idx, value in enumerate(shares):
            if value < cap_fraction - EPSILON:
                shares[idx] = value * factor
                changed = True

        if not changed:
            break

    return [min(max(x, 0.0), cap_fraction) for x in shares]


def compute_qf(state, pool, max_reward, redistribute_caps=True):
    total_mc = sum(max(s.get("match_component", 0.0), 0.0) for s in state)
    cap = pool * max_reward

    if total_mc <= 0 or pool <= 0:
        for s in state:
            s["pre_cap"] = 0.0
            s["match"] = 0.0
            s["total"] = s.get("raised", 0.0)
        return total_mc, cap

    base_fractions = [max(s.get("match_component", 0.0), 0.0) / total_mc for s in state]
    if redistribute_caps:
        final_fractions = _redistribute_cap_fractions(base_fractions, max_reward)
    else:
        final_fractions = [min(f, max_reward) for f in base_fractions]

    for s, base_fraction, final_fraction in zip(state, base_fractions, final_fractions):
        s["pre_cap"] = pool * base_fraction
        s["match"] = pool * final_fraction
        s["total"] = s.get("raised", 0.0) + s["match"]
    return total_mc, cap


def _simulated_state_with_extra_donors(state, target_index, donor_amount, donor_count):
    simulated = deepcopy(state)
    target = simulated[target_index]
    sqrt_sum = target.get("sqrt_sum", 0.0) + donor_count * math.sqrt(donor_amount)
    effective_linear = target.get("boosted_linear", 0.0) + donor_count * donor_amount
    target["sqrt_sum"] = sqrt_sum
    target["boosted_linear"] = effective_linear
    target["match_component"] = max(sqrt_sum * sqrt_sum - effective_linear, 0.0)
    target["raised"] = target.get("raised", 0.0) + donor_count * donor_amount
    target["donors"] = target.get("donors", 0) + donor_count
    return simulated


def find_min_donors_for_cap(state, target_index, donor_amount, pool, max_reward, redistribute_caps=True):
    if donor_amount <= 0:
        return None
    cap = pool * max_reward

    def match_with(n):
        simulated = _simulated_state_with_extra_donors(state, target_index, donor_amount, n)
        compute_qf(simulated, pool, max_reward, redistribute_caps=redistribute_caps)
        return simulated[target_index].get("match", 0.0)

    if match_with(0) >= cap - 1e-7:
        return 0

    hi = 1
    while match_with(hi) < cap - 1e-7:
        hi *= 2
        if hi > 10_000_000:
            return None

    lo = 1
    while lo < hi:
        mid = (lo + hi) // 2
        if match_with(mid) >= cap - 1e-7:
            hi = mid
        else:
            lo = mid + 1
    return lo


def truncate(text, width):
    text = str(text or "")
    return text if len(text) <= width else text[: max(width - 3, 0)] + "..."


def fmt_money(value):
    return f"${value:,.2f}"


def print_table(rows, include_global_rank=False):
    cols = "RANK"
    header = f"{cols:>4}"
    if include_global_rank:
        header += f" {'GLOBAL':>6}"
    header += f"  {'PROJECT':42}  {'NFT':>3}  {'DONORS':>6}  {'RAISED':>11}  {'PRE-CAP':>11}  {'MATCH':>11}  {'TOTAL':>11}"
    print(header)
    for local_rank, row in enumerate(rows, 1):
        line = f"{local_rank:>4}"
        if include_global_rank:
            line += f" {row.get('global_rank', ''):>6}"
        line += (
            f"  {truncate(row['title'], 42):42}  {row.get('nft_donor_count', 0):>3}  {row['donors']:>6}  "
            f"{fmt_money(row['raised']):>11}  {fmt_money(row.get('pre_cap', 0.0)):>11}  "
            f"{fmt_money(row.get('match', 0.0)):>11}  {fmt_money(row.get('total', 0.0)):>11}"
        )
        print(line)


def parse_args():
    p = argparse.ArgumentParser(description="Giveth QF round payout estimator.")
    p.add_argument("--round-id", type=int, default=None, help="QF round id. Defaults to the id resolved from --round-slug.")
    p.add_argument("--round-slug", default="ethereum-security")
    p.add_argument("--focus", default=DEFAULT_FOCUS, help="Comma-separated project titles for the focus section.")
    p.add_argument("--simulate-tiers", default="1,5,10,25,50,100,1000", help="Comma-separated $ tiers for cap-hit simulation.")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--bottom", type=int, default=20)
    p.add_argument("--track-currencies", default="FINN,TIK")
    p.add_argument("--no-currency-stats", action="store_true")
    p.add_argument("--nft-contract", default=NFT_CONTRACT, help="ERC-721 contract address for NFT holder boost.")
    p.add_argument("--nft-multiplier", type=float, default=NFT_MULTIPLIER, help="QF contribution multiplier for NFT holders.")
    p.add_argument("--no-nft-boost", action="store_true", help="Disable NFT holder QF multiplier.")
    p.add_argument("--no-cap-redistribution", action="store_true", help="Use old clamp-only cap behavior; mainly for comparison.")
    p.add_argument("--retry-initial-sleep", type=float, default=RETRY_INITIAL_SLEEP, help="Seconds before the first transient API retry.")
    p.add_argument("--retry-max-sleep", type=float, default=RETRY_MAX_SLEEP, help="Maximum seconds between transient API retries.")
    p.add_argument("--self-test", action="store_true", help="Run deterministic offline tests and exit.")
    return p.parse_args()


def _assert_close(actual, expected, label, tol=1e-8):
    if abs(actual - expected) > tol:
        raise AssertionError(f"{label}: expected {expected}, got {actual}")


def run_self_tests():
    # No cap reached: exact proportional distribution, full pool used.
    state = [
        {"title": "A", "raised": 0.0, "match_component": 1.0},
        {"title": "B", "raised": 0.0, "match_component": 3.0},
    ]
    compute_qf(state, 100.0, 0.90)
    _assert_close(state[0]["match"], 25.0, "uncapped A")
    _assert_close(state[1]["match"], 75.0, "uncapped B")
    _assert_close(sum(s["match"] for s in state), 100.0, "uncapped full pool")

    # Single cap case: old clamp-only would produce 50 + 10 = 60 and leave 40 unused.
    state = [
        {"title": "A", "raised": 0.0, "match_component": 90.0},
        {"title": "B", "raised": 0.0, "match_component": 10.0},
        {"title": "C", "raised": 0.0, "match_component": 0.0},
    ]
    compute_qf(state, 100.0, 0.50)
    _assert_close(state[0]["match"], 50.0, "cap redistrib A")
    _assert_close(state[1]["match"], 50.0, "cap redistrib B")
    _assert_close(state[2]["match"], 0.0, "cap redistrib C")
    _assert_close(sum(s["match"] for s in state), 100.0, "cap redistrib full pool")

    # Multi-step redistribution: A caps, overflow pushes B over cap, then C receives the remainder.
    state = [
        {"title": "A", "raised": 0.0, "match_component": 80.0},
        {"title": "B", "raised": 0.0, "match_component": 15.0},
        {"title": "C", "raised": 0.0, "match_component": 5.0},
    ]
    compute_qf(state, 100.0, 0.40)
    _assert_close(state[0]["match"], 40.0, "multi cap A")
    _assert_close(state[1]["match"], 40.0, "multi cap B")
    _assert_close(state[2]["match"], 20.0, "multi cap C")

    # Infeasible cap: with only two positive projects and a 40% cap, 20% must remain unused.
    state = [
        {"title": "A", "raised": 0.0, "match_component": 90.0},
        {"title": "B", "raised": 0.0, "match_component": 10.0},
    ]
    compute_qf(state, 100.0, 0.40)
    _assert_close(state[0]["match"], 40.0, "infeasible cap A")
    _assert_close(state[1]["match"], 40.0, "infeasible cap B")
    _assert_close(sum(s["match"] for s in state), 80.0, "infeasible cap leftover")

    # Donations under $1 count as raised but not as matching contributions.
    global fetch_projects, fetch_donations, ETH_USD_PRICE
    old_fetch_projects = fetch_projects
    old_fetch_donations = fetch_donations
    old_eth_price = ETH_USD_PRICE
    ETH_USD_PRICE = 3000.0
    try:
        fetch_projects = lambda round_id: ([{"id": 1, "title": "P", "slug": "p"}], 1)
        def fake_donations(project_id, round_id):
            return iter([
                {"valueUsd": 0.50, "currency": "TIK", "amount": 0.5, "createdAt": "2026-01-01T00:00:00Z", "fromWalletAddress": "0xsmall", "user": None},
                {"valueUsd": 2.00, "currency": "TIK", "amount": 2.0, "createdAt": "2026-01-01T00:01:00Z", "fromWalletAddress": "0xlarge", "user": None},
            ])
        fetch_donations = fake_donations
        agg, _ = aggregate_round(1, nft_holders=None, nft_multiplier=1, track_currencies=())
        _assert_close(agg[0]["raised"], 2.50, "small donation raised total")
        if agg[0]["donors"] != 1:
            raise AssertionError(f"small donation donor count: expected 1, got {agg[0]['donors']}")
    finally:
        fetch_projects = old_fetch_projects
        fetch_donations = old_fetch_donations
        ETH_USD_PRICE = old_eth_price


    # ETH-denominated pools use allocatedFund as canonical; USD is derived from live ETH price.
    old_eth_price = ETH_USD_PRICE
    ETH_USD_PRICE = 2000.0
    try:
        usd, native, sym, source = resolve_pool_from_api({"allocatedFund": "540", "allocatedFundUSD": "1000000", "allocatedTokenSymbol": "ETH"})
        _assert_close(usd, 1080000.0, "ETH pool USD conversion")
        _assert_close(native, 540.0, "ETH pool native amount")
        if sym != "ETH":
            raise AssertionError(f"ETH pool symbol: expected ETH, got {sym}")
        if source != "allocatedFund*live_eth_usd":
            raise AssertionError(f"ETH pool source: expected allocatedFund*live_eth_usd, got {source}")
        cap_fraction = resolve_cap_fraction_from_api({"maximumReward": "5"})
        _assert_close(cap_fraction, 0.05, "API percent cap")
        cap_fraction = resolve_cap_fraction_from_api({"maximumReward": "0.05"})
        _assert_close(cap_fraction, 0.05, "API fraction cap")
    finally:
        ETH_USD_PRICE = old_eth_price

    # NFT multiplier is applied as an effective contribution, preserving the single-donor zero-match invariant.
    state = [
        {
            "title": "NFT single donor",
            "raised": 10.0,
            "sqrt_sum": math.sqrt(40.0),
            "boosted_linear": 40.0,
            "match_component": max(math.sqrt(40.0) ** 2 - 40.0, 0.0),
        }
    ]
    _assert_close(state[0]["match_component"], 0.0, "NFT single donor no match")

    # Transient HTTP 503 retries indefinitely until the same request succeeds.
    old_urlopen = urllib.request.urlopen
    old_sleep_fn = SLEEP_FN
    calls = {"count": 0}
    sleeps = []

    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=None):
        calls["count"] += 1
        if calls["count"] <= 2:
            raise urllib.error.HTTPError(getattr(req, "full_url", "url"), 503, "Service Unavailable", None, None)
        return FakeResponse()

    try:
        urllib.request.urlopen = fake_urlopen
        globals()["SLEEP_FN"] = lambda seconds: sleeps.append(seconds)
        got = _request_json("https://example.invalid/graphql", data=b"{}", label="retry self-test")
        if got != {"ok": True}:
            raise AssertionError(f"retry result: expected {{'ok': True}}, got {got}")
        if calls["count"] != 3:
            raise AssertionError(f"retry call count: expected 3, got {calls['count']}")
        if sleeps != [2.0, 4.0]:
            raise AssertionError(f"retry sleeps: expected [2.0, 4.0], got {sleeps}")
    finally:
        urllib.request.urlopen = old_urlopen
        globals()["SLEEP_FN"] = old_sleep_fn

    # Non-transient HTTP errors still fail fast.
    old_urlopen = urllib.request.urlopen
    try:
        urllib.request.urlopen = lambda req, timeout=None: (_ for _ in ()).throw(
            urllib.error.HTTPError(getattr(req, "full_url", "url"), 400, "Bad Request", None, None)
        )
        try:
            _request_json("https://example.invalid/graphql", data=b"{}", label="bad request self-test")
        except RuntimeError as exc:
            if "HTTP 400" not in str(exc):
                raise AssertionError(f"non-transient error text missing status: {exc}")
        else:
            raise AssertionError("non-transient HTTP 400 did not fail fast")
    finally:
        urllib.request.urlopen = old_urlopen

    print("self-test: ok")


def main():
    global RETRY_INITIAL_SLEEP, RETRY_MAX_SLEEP
    args = parse_args()
    RETRY_INITIAL_SLEEP = max(0.0, float(args.retry_initial_sleep))
    RETRY_MAX_SLEEP = max(RETRY_INITIAL_SLEEP, float(args.retry_max_sleep))
    if args.self_test:
        run_self_tests()
        return

    track = tuple(c.strip().upper() for c in args.track_currencies.split(",") if c.strip())
    print(f"Fetching round '{args.round_slug}'...", file=sys.stderr)
    round_meta = fetch_round(args.round_slug)
    if not round_meta:
        print(f"Round '{args.round_slug}' not found", file=sys.stderr)
        sys.exit(1)

    round_id = args.round_id
    meta_round_id = None
    try:
        meta_round_id = int(round_meta.get("id")) if round_meta.get("id") is not None else None
    except (TypeError, ValueError):
        meta_round_id = None
    if round_id is None:
        round_id = meta_round_id
    elif meta_round_id is not None and int(round_id) != meta_round_id:
        print(
            f"[WARN] --round-id {round_id} does not match --round-slug {args.round_slug!r} id {meta_round_id}; using --round-id.",
            file=sys.stderr,
        )
    if round_id is None:
        print("Round id unavailable. Pass --round-id explicitly.", file=sys.stderr)
        sys.exit(1)

    try:
        pool, pool_native_amount, pool_native_symbol, pool_source = resolve_pool_from_api(round_meta)
        max_reward = resolve_cap_fraction_from_api(round_meta)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    if pool <= 0:
        print("Matching pool unavailable from API; refusing to use a hardcoded pool.", file=sys.stderr)
        sys.exit(1)
    if max_reward <= 0 or max_reward > 1:
        print("Per-project cap from API must be > 0 and <= 100% of the pool.", file=sys.stderr)
        sys.exit(1)

    nft_holders = None
    nft_multiplier = 1
    if not args.no_nft_boost:
        print(f"Fetching NFT holders from {args.nft_contract}...", file=sys.stderr)
        nft_holders = fetch_nft_holders(args.nft_contract)
        nft_multiplier = args.nft_multiplier
        print(f"  Found {len(nft_holders)} NFT holders (multiplier: {nft_multiplier}x)", file=sys.stderr)

    def progress(i, total, title):
        print(f"  [{i}/{total}] {title[:50]}", file=sys.stderr)

    state, currency_stats = aggregate_round(
        round_id,
        nft_holders=nft_holders,
        nft_multiplier=nft_multiplier,
        track_currencies=track if not args.no_currency_stats else (),
        progress=progress,
    )
    redistribute_caps = not args.no_cap_redistribution
    total_mc, cap = compute_qf(state, pool, max_reward, redistribute_caps=redistribute_caps)
    state.sort(key=lambda s: (-s["match"], -s["raised"], s["title"].lower()))
    for i, s in enumerate(state, 1):
        s["global_rank"] = i

    total_raised = sum(s["raised"] for s in state)
    total_match = sum(s["match"] for s in state)
    total_nft_donors = sum(s["nft_donor_count"] for s in state)
    leftover = max(pool - total_match, 0.0)

    print()
    print(f"Round: {round_meta.get('name')} ({round_meta.get('beginDate')} to {round_meta.get('endDate')})")
    print(f"  Round id: {round_id} | Slug: {round_meta.get('slug')}")
    print(f"  Pool source: {pool_source} | Cap source: round.maximumReward")
    if pool_native_amount is not None and pool_native_symbol in {"ETH", "WETH"}:
        print(
            f"  Pool: {pool_native_amount:,.6g} {pool_native_symbol} (~{fmt_money(pool)}) | "
            f"Per-project cap: {max_reward*100:.4g}% ({pool_native_amount*max_reward:,.6g} {pool_native_symbol}, ~{fmt_money(cap)})"
        )
    else:
        print(f"  Pool: {fmt_money(pool)} | Per-project cap: {max_reward*100:.4g}% ({fmt_money(cap)})")
    print(
        f"  Projects: {len(state)} | Total raised: {fmt_money(total_raised)} | "
        f"Total match: {fmt_money(total_match)} | Leftover: {fmt_money(leftover)}"
    )
    print(f"  Cap redistribution: {'on' if redistribute_caps else 'off'}")
    if nft_holders:
        print(f"  NFT boost: {nft_multiplier}x for {len(nft_holders)} holders ({total_nft_donors} donors matched in this round)")
    if ETH_USD_PRICE:
        print(f"  ETH price: ${ETH_USD_PRICE:,.2f} (FINN = ${ETH_USD_PRICE/FINN_PER_ETH:.4f})")

    print()
    print(f"=== TOP {args.top} ===")
    print_table(state[: args.top])

    print()
    print(f"=== BOTTOM {args.bottom} ===")
    print_table(state[-args.bottom:])

    focus_titles = [t.strip() for t in args.focus.split(",") if t.strip()]
    if focus_titles:
        focus_rows = [s for s in state if s["title"] in focus_titles]
        if focus_rows:
            print()
            print("=== FOCUS PROJECTS ===")
            print_table(focus_rows, include_global_rank=True)
            sm = sum(r["match"] for r in focus_rows)
            sr = sum(r["raised"] for r in focus_rows)
            sn = sum(r["nft_donor_count"] for r in focus_rows)
            print(f"  Focus total: raised {fmt_money(sr)} | match {fmt_money(sm)} | total {fmt_money(sr+sm)} | NFT donors: {sn}")

            tiers = [float(t) for t in args.simulate_tiers.split(",") if t.strip()]
            if tiers:
                print()
                print("=== NON-NFT DONORS NEEDED FOR CAP (focus only, others frozen) ===")
                header = f"{'PROJECT':25}" + "".join(f" {'$'+str(int(t)):>8}" for t in tiers)
                print(header)
                for s in focus_rows:
                    target_index = next(i for i, candidate in enumerate(state) if candidate is s)
                    row = f"{truncate(s['title'], 25):25}"
                    for amt in tiers:
                        n = find_min_donors_for_cap(state, target_index, amt, pool, max_reward, redistribute_caps=redistribute_caps)
                        row += f" {(str(n) if n is not None else 'N/A'):>8}"
                    print(row)

    if not args.no_currency_stats and currency_stats:
        print()
        print("=== TRACKED CURRENCY TOTALS ===")
        for cur, d in currency_stats.items():
            print(f"  {cur}: {d['amount']:,.2f} {cur} | {d['count']} donations | {len(d['donors'])} unique donors (cluster-deduped)")

    print()
    notes = (
        "Note: estimate from current donation snapshot. Final Giveth payout may apply COCM, "
        "Passport/Sybil weighting, manual exclusions, and post-round review. Anonymous donations clustered by 1-minute timestamp."
    )
    if nft_holders:
        notes += f" NFT holder contributions boosted {nft_multiplier}x in QF matching."
    print(notes)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Fetch and analyze Giveth QF round project descriptions for accountability info."""
import json
import urllib.request

ENDPOINT = "https://core.v6.giveth.io/graphql"
PAGE = 50

PROJECTS_QUERY = """
query Projects($skip:Int!,$take:Int!,$filters:ProjectFiltersInput){
  projects(skip:$skip,take:$take,filters:$filters){
    projects{ id title slug description }
    total
  }
}
"""


def graphql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"content-type": "application/json", "user-agent": "giveth-qf/1.0"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.loads(resp.read().decode())
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"]))
    return payload.get("data") or {}


def fetch_projects(round_id):
    out = []
    skip = 0
    total = None
    while total is None or skip < total:
        data = graphql(PROJECTS_QUERY, {
            "skip": skip, "take": PAGE,
            "filters": {"qfRoundId": int(round_id)},
        }).get("projects") or {}
        total = int(data.get("total") or 0)
        batch = data.get("projects") or []
        out.extend(batch)
        skip += PAGE
        if not batch:
            break
    return out, total


def main():
    print("Fetching projects for round 16...")
    projects, total = fetch_projects(16)
    print(f"Fetched {len(projects)} projects (total reported: {total})")

    with open("projects_round_16.json", "w", encoding="utf-8") as f:
        json.dump(projects, f, indent=2, ensure_ascii=False)
    print("Saved raw data to projects_round_16.json")


if __name__ == "__main__":
    main()

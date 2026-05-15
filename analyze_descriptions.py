#!/usr/bin/env python3
"""Analyze project descriptions for accountability and funding tier mentions."""
import json
import re

# Keywords suggesting accountability, reporting, or use of funds
ACCOUNTABILITY_PATTERNS = [
    r"\bwill be used\b",
    r"\bwe will\b",
    r"\bfunds? will\b",
    r"\buse of funds\b",
    r"\bmoney will\b",
    r"\bdonations? will\b",
    r"\bgrant will\b",
    r"\bgenerous\b",
    r"\bwith the(?:se)? funds\b",
    r"\bwith your\b",
    r"\bfund(?:ing)? goal\b",
    r"\bstretch goal\b",
    r"\btier\b",
    r"\bphase\b",
    r"\bmilestone\b",
    r"\btransparency\b",
    r"\baccountability\b",
    r"\btrack\b",
    r"\breport(?:ing)?\b",
    r"\bif we raise\b",
    r"\bif we reach\b",
    r"\bat \$?[\d,]+\b",
    r"\bfor \$?[\d,]+\b",
]

# Compiled regex
ACCOUNTABILITY_RE = re.compile("|".join(ACCOUNTABILITY_PATTERNS), re.IGNORECASE)


def has_accountability(text):
    return bool(ACCOUNTABILITY_RE.search(text or ""))


def funding_snippets(text):
    """Extract sentences/fragments mentioning money/funds."""
    snippets = []
    if not text:
        return snippets
    sentences = re.split(r'(?<=[.!?])\s+', text)
    for s in sentences:
        if re.search(r'\$\d|funds?|money|grant|donations?|budget|goal|tier|phase|milestone', s, re.IGNORECASE):
            snippets.append(s.strip())
    return snippets


def main():
    with open("projects_round_16.json", "r", encoding="utf-8") as f:
        projects = json.load(f)

    results = []
    for p in projects:
        title = p.get("title", "")
        desc = p.get("description", "") or ""
        slug = p.get("slug", "")
        is_accountable = has_accountability(desc)
        snippets = funding_snippets(desc) if is_accountable else []
        results.append({
            "id": p.get("id"),
            "title": title,
            "slug": slug,
            "has_accountability": is_accountable,
            "desc_length": len(desc),
            "snippets": snippets,
        })

    accountable = [r for r in results if r["has_accountability"]]
    non_accountable = [r for r in results if not r["has_accountability"]]

    print(f"Total projects: {len(results)}")
    print(f"Projects with accountability/tier/money mention: {len(accountable)} ({len(accountable)/len(results)*100:.1f}%)")
    print(f"Projects without: {len(non_accountable)} ({len(non_accountable)/len(results)*100:.1f}%)")
    print()

    print("=" * 60)
    print("PROJECTS THAT MENTION SOMETHING LIKE ACCOUNTABILITY OR FUNDING TIERS")
    print("=" * 60)
    for r in accountable:
        print(f"\n--- {r['title']} (slug: {r['slug']}) ---")
        for s in r["snippets"]:
            print(f"  > {s}")

    print()
    print("=" * 60)
    print("PROJECTS THAT DID NOT (SHORT DESCRIPTIONS < 150 CHARS)")
    print("=" * 60)
    for r in non_accountable:
        if r["desc_length"] < 150:
            snippet = (p.get("description") or "")[:120].replace("\n", " ")
            print(f"  {r['title']}: {snippet}...")

    print()
    print("=" * 60)
    print("PROJECTS THAT DID NOT (LONGER DESCRIPTIONS)")
    print("=" * 60)
    for r in non_accountable:
        if r["desc_length"] >= 150:
            snippet = (p.get("description") or "")[:120].replace("\n", " ")
            print(f"  {r['title']}: {snippet}...")

    with open("analysis_results.json", "w", encoding="utf-8") as f:
        json.dump({
            "total_projects": len(results),
            "accountable_count": len(accountable),
            "non_accountable_count": len(non_accountable),
            "accountable": accountable,
            "non_accountable": [{"id": r["id"], "title": r["title"], "slug": r["slug"], "desc_length": r["desc_length"]} for r in non_accountable],
        }, f, indent=2, ensure_ascii=False)
    print("\nSaved detailed results to analysis_results.json")


if __name__ == "__main__":
    main()

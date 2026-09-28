"""GrantAnchor CLI demo: audits three expenses against the seed grant, with and without memory."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import compliance_engine  # noqa: E402
from app.config import settings  # noqa: E402

SEED = json.loads(Path(settings.seed_path).read_text(encoding="utf-8"))
GRANT_ID = SEED["active_grant_id"]
GRANT = {"id": GRANT_ID, **SEED["grants"][GRANT_ID]}
LEDGER = [m for m in SEED["memories"] if m["grant_id"] == GRANT_ID]
MEMBERS = {m["id"]: m for m in SEED["team_members"]}

CASES = [
    dict(spender_id="tm_2", vendor="Nordic Tech Solutions", location="Oslo, Norway", amount=8500,
         purpose="Contract UI/UX work for Milestone 2", category="Contractor"),
    dict(spender_id="tm_3", vendor="ANA / Tokyo Hilton", location="Tokyo, Japan", amount=3100,
         purpose="Flights and hotel for robotics summit", category="Travel"),
    dict(spender_id="tm_1", vendor="Amazon Web Services", location="Seattle, USA", amount=600,
         purpose="GPU compute for benchmark runs", category="Compute"),
]


async def main() -> None:
    print(f"Grant {GRANT_ID}: {GRANT['name']}  ({len(LEDGER)} prior ledger entries)\n")
    for case in CASES:
        spender = MEMBERS[case["spender_id"]]
        expense = compliance_engine.build_expense(grant_id=GRANT_ID, spender_name=spender["name"], **case)
        _, with_mem = await compliance_engine.assess(grant=GRANT, expense=expense, ledger=LEDGER, mode="with_memory")
        _, without = await compliance_engine.assess(
            grant=GRANT, expense=expense, ledger=LEDGER, mode="without_memory", use_advisory=False
        )
        d = compliance_engine.diff(with_mem, without)
        print(f"- {spender['name']} -> {case['vendor']} ${case['amount']:,.2f} [{expense.category}]")
        print(f"    without memory: {without['status']:<24} risk {without['risk_score']}")
        print(f"    with memory:    {with_mem['status']:<24} risk {with_mem['risk_score']}")
        for f in with_mem["findings"]:
            print(f"      [{f['severity']}] {f['message']}")
        for a in with_mem["advisory"]["findings"]:
            print(f"      [advisory] {a['message']}")
        print(f"    {d['summary']}\n")


if __name__ == "__main__":
    asyncio.run(main())

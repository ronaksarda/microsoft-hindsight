"""GrantAnchor CLI demo: audits three expenses against the seed grant, with and without memory."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import compliance_engine
from app.config import settings

SEED_FILE = Path(settings.seed_path or Path(__file__).resolve().parent / "tests" / "fixtures" / "seed_state.json")
if SEED_FILE.is_file():
    SEED = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    GRANT_ID = SEED["active_grant_id"]
    GRANT = {"id": GRANT_ID, **SEED["grants"][GRANT_ID]}
    LEDGER = [m for m in SEED["memories"] if m["grant_id"] == GRANT_ID]
    MEMBERS = {m["id"]: m for m in SEED["team_members"]}
else:
    GRANT_ID = "NSF-2026-881"
    GRANT = {
        "id": "NSF-2026-881",
        "name": "NSF DeepTech Phase I",
        "total_funding": 250000,
        "currency": "USD",
        "home_country": "US",
        "start_date": "2026-01-01",
        "end_date": "2027-12-31",
        "rules": [
            "Clause 2.1: Costs are allowable only within the period of performance (2026-01-01 to 2027-12-31).",
            "Clause 3.1: Total award spend may not exceed $250,000.",
            "Clause 4.2: Cumulative travel expenses capped at $8,000 total across all team members.",
            "Clause 4.3: Compute spend capped at $15,000 per fiscal quarter.",
            "Clause 5.1: Contractor and subcontractor spend capped at $40,000 across the award.",
            "Clause 5.2: No more than $20,000 to any single vendor per rolling 365 days.",
            "Clause 6.1: No team member may commit more than $30,000 per rolling 90 days.",
            "Clause 7.1: Equipment purchases above $5,000 require written prior approval.",
            "Clause 8.1: Alcohol and entertainment are unallowable costs.",
            "Clause 9.1: Zero foreign contractor spend without 30-day prior written agency approval.",
            "Clause 10.2: Participant support costs may not be rebudgeted without agency approval.",
        ],
        "policies": [
            {"id": "nsf-period", "type": "grant_period", "clause": "Clause 2.1", "start": "2026-01-01", "end": "2027-12-31", "description": "Clause 2.1: Costs are allowable only within the period of performance (2026-01-01 to 2027-12-31)."},
            {"id": "nsf-total", "type": "category_cap", "clause": "Clause 3.1", "category": None, "cap": 250000, "warn_at_pct": 90, "description": "Clause 3.1: Total award spend may not exceed $250,000."},
            {"id": "nsf-travel", "type": "category_cap", "clause": "Clause 4.2", "category": "Travel", "cap": 8000, "warn_at_pct": 80, "description": "Clause 4.2: Cumulative travel expenses capped at $8,000 total across all team members."},
            {"id": "nsf-compute-q", "type": "category_cap", "clause": "Clause 4.3", "category": "Compute", "cap": 15000, "window": {"kind": "fiscal", "period": "quarter", "start_month": 10}, "warn_at_pct": 80, "description": "Clause 4.3: Compute spend capped at $15,000 per fiscal quarter."},
            {"id": "nsf-contractor", "type": "category_cap", "clause": "Clause 5.1", "category": "Contractor", "cap": 40000, "warn_at_pct": 80, "description": "Clause 5.1: Contractor and subcontractor spend capped at $40,000 across the award."},
            {"id": "nsf-vendor", "type": "vendor_cap", "clause": "Clause 5.2", "vendor": None, "cap": 20000, "window": {"kind": "rolling", "days": 365}, "warn_at_pct": 80, "description": "Clause 5.2: No more than $20,000 to any single vendor per rolling 365 days."},
            {"id": "nsf-spender", "type": "spender_cap", "clause": "Clause 6.1", "spender": None, "cap": 30000, "window": {"kind": "rolling", "days": 90}, "warn_at_pct": 80, "description": "Clause 6.1: No team member may commit more than $30,000 per rolling 90 days."},
            {"id": "nsf-equipment-approval", "type": "prior_approval", "clause": "Clause 7.1", "threshold": 5000, "categories": ["Equipment"], "description": "Clause 7.1: Equipment purchases above $5,000 require written prior approval."},
            {"id": "nsf-unallowable", "type": "blocked_list", "clause": "Clause 8.1", "categories": ["Alcohol", "Entertainment"], "description": "Clause 8.1: Alcohol and entertainment are unallowable costs."},
            {"id": "nsf-foreign", "type": "jurisdiction", "clause": "Clause 9.1", "allowed_countries": ["US"], "categories": ["Contractor"], "waivable_with_prior_approval": True, "description": "Clause 9.1: Zero foreign contractor spend without 30-day prior written agency approval."},
        ],
    }
    LEDGER = [
        {"id": "mem_02", "grant_id": "NSF-2026-881", "timestamp": "2026-01-22T10:00:00Z", "spender": "Ronak Sarda", "spender_id": "tm_1", "category": "Equipment", "amount": 4800, "vendor": "Dell Technologies", "location": "Austin, TX, USA", "content": "Ronak Sarda paid Dell Technologies $4,800 for two gpu workstations for model development (Equipment)."},
        {"id": "mem_03", "grant_id": "NSF-2026-881", "timestamp": "2026-02-10T10:00:00Z", "spender": "David Park", "spender_id": "tm_3", "category": "Compute", "amount": 3200, "vendor": "Amazon Web Services", "location": "Seattle, USA", "content": "David Park paid Amazon Web Services $3,200 for cloud training runs for the baseline model (Compute)."},
        {"id": "mem_04", "grant_id": "NSF-2026-881", "timestamp": "2026-03-05T10:00:00Z", "spender": "Sarah Miller", "spender_id": "tm_2", "category": "Contractor", "amount": 12000, "vendor": "Brightline Labs", "location": "Boston, USA", "content": "Sarah Miller paid Brightline Labs $12,000 for ux research study with pilot customers (Contractor)."},
        {"id": "mem_05", "grant_id": "NSF-2026-881", "timestamp": "2026-03-28T10:00:00Z", "spender": "David Park", "spender_id": "tm_3", "category": "Compute", "amount": 4100, "vendor": "Lambda Labs", "location": "San Francisco, USA", "content": "David Park paid Lambda Labs $4,100 for gpu rental for hyperparameter sweep (Compute)."},
        {"id": "mem_06", "grant_id": "NSF-2026-881", "timestamp": "2026-04-15T10:00:00Z", "spender": "Ronak Sarda", "spender_id": "tm_1", "category": "Software", "amount": 1200, "vendor": "GitHub", "location": "San Francisco, USA", "content": "Ronak Sarda paid GitHub $1,200 for team plan and ci minutes for the year (Software)."},
        {"id": "mem_07", "grant_id": "NSF-2026-881", "timestamp": "2026-05-12T10:00:00Z", "spender": "Sarah Miller", "spender_id": "tm_2", "category": "Personnel", "amount": 18500, "vendor": "Gusto", "location": "New York, USA", "content": "Sarah Miller paid Gusto $18,500 for research assistant stipend, april to may (Personnel)."},
        {"id": "mem_01", "grant_id": "NSF-2026-881", "timestamp": "2026-06-15T10:00:00Z", "spender": "Sarah Miller", "spender_id": "tm_2", "category": "Travel", "amount": 5400, "vendor": "Lufthansa / Marriott Munich", "location": "Munich, Germany", "content": "Sarah Miller booked transatlantic flights and lodging for 2 devs attending NeurIPS Munich ($5,400 of $8,000 travel cap)."},
        {"id": "mem_08", "grant_id": "NSF-2026-881", "timestamp": "2026-07-08T10:00:00Z", "spender": "David Park", "spender_id": "tm_3", "category": "Compute", "amount": 6300, "vendor": "Amazon Web Services", "location": "Seattle, USA", "content": "David Park paid Amazon Web Services $6,300 for inference cluster for the pilot (Compute)."},
        {"id": "mem_09", "grant_id": "NSF-2026-881", "timestamp": "2026-08-19T10:00:00Z", "spender": "Ronak Sarda", "spender_id": "tm_1", "category": "Contractor", "amount": 6500, "vendor": "Brightline Labs", "location": "Boston, USA", "content": "Ronak Sarda paid Brightline Labs $6,500 for follow-up usability testing round (Contractor)."},
        {"id": "mem_10", "grant_id": "NSF-2026-881", "timestamp": "2026-09-02T10:00:00Z", "spender": "David Park", "spender_id": "tm_3", "category": "Compute", "amount": 4900, "vendor": "Lambda Labs", "location": "San Francisco, USA", "content": "David Park paid Lambda Labs $4,900 for fine-tuning runs for milestone 2 (Compute)."},
        {"id": "mem_11", "grant_id": "NSF-2026-881", "timestamp": "2026-09-10T10:00:00Z", "spender": "Sarah Miller", "spender_id": "tm_2", "category": "Supplies", "amount": 850, "vendor": "Digi-Key", "location": "Thief River Falls, MN, USA", "content": "Sarah Miller paid Digi-Key $850 for sensors and cables for the hardware demo (Supplies)."},
    ]
    MEMBERS = {
        "tm_1": {"id": "tm_1", "name": "Ronak Sarda", "role": "Lead Architect"},
        "tm_2": {"id": "tm_2", "name": "Sarah Miller", "role": "Head of Operations"},
        "tm_3": {"id": "tm_3", "name": "David Park", "role": "Senior ML Engineer"},
    }

CASES = [
    dict(
        spender_id="tm_2",
        vendor="Nordic Tech Solutions",
        location="Oslo, Norway",
        amount=8500,
        purpose="Contract UI/UX work for Milestone 2",
        category="Contractor",
    ),
    dict(
        spender_id="tm_3",
        vendor="ANA / Tokyo Hilton",
        location="Tokyo, Japan",
        amount=3100,
        purpose="Flights and hotel for robotics summit",
        category="Travel",
    ),
    dict(
        spender_id="tm_1",
        vendor="Amazon Web Services",
        location="Seattle, USA",
        amount=600,
        purpose="GPU compute for benchmark runs",
        category="Compute",
    ),
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

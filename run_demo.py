"""GrantAnchor CLI demo — seeds data, audits the Norway invoice, asserts Clause 9.1 flagged."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings
from app import hindsight, compliance_engine


SEED_PATH = Path(__file__).resolve().parent / "data" / "seed_grant_lifecycle.json"


async def main() -> None:
    print("=" * 65)
    print("  GrantAnchor — CLI Compliance Demo")
    print("=" * 65)

    # Step 1: Seed grant terms + Month 3 expense
    print("\n[1/3] Seeding grant lifecycle into Hindsight memory...")
    hindsight.clear_bank(settings.hindsight_bank_id)

    with open(SEED_PATH) as f:
        lifecycle = json.load(f)

    retained = 0
    for phase in lifecycle:
        award = phase["award_name"]
        for event in phase["events"]:
            if event["type"] == "grant_rule":
                await compliance_engine.record_grant_rule(
                    award_name=award,
                    rule_type=event["rule_type"],
                    condition=event["condition"],
                    penalty=event["penalty"],
                )
                retained += 1
            elif event["type"] == "expense_record":
                content = (
                    f"[EXPENSE] {award} | {event['description']} | "
                    f"${event['amount']:,.2f} | Vendor: {event['vendor']}"
                )
                await hindsight.retain(
                    bank_id=settings.hindsight_bank_id,
                    content=content,
                    context=f"expense:{award}",
                    timestamp=phase["date"],
                )
                retained += 1

    print(f"      Retained {retained} records into bank '{settings.hindsight_bank_id}'")

    # Step 2: Recall and audit the Month 6 foreign contractor invoice
    print("\n[2/3] Auditing Month 6 expense: Nordic Tech Solutions ($8,500)...")

    recalled = await hindsight.recall(
        bank_id=settings.hindsight_bank_id,
        query="foreign contractor international Norway Clause 9.1 budget contractor cap",
        top_k=10,
    )
    print(f"      Recalled {len(recalled)} memories from Hindsight")

    result = await compliance_engine.audit_expenditure(
        award_name="National Science Tech Grant #NSF-2026-881",
        expense_description="Contract UI/UX optimization for Milestone 2 deliverables",
        amount=8500.00,
        vendor_info="Nordic Tech Solutions (Oslo, Norway)",
        recalled_history=recalled,
    )

    print("\n" + "-" * 65)
    print("  AUDIT RESULT")
    print("-" * 65)
    print(f"  Verdict:            {result.get('verdict', 'UNKNOWN')}")
    print(f"  Risk Level:         {result.get('risk_level', 'UNKNOWN')}")
    print(f"  Breached Clauses:   {json.dumps(result.get('breached_clauses', []), indent=2)}")
    print(f"  Spend Impact:       {result.get('cumulative_spend_impact', 'N/A')}")
    print(f"  Remediation:        {result.get('recommended_remediation', 'N/A')}")
    print("-" * 65)

    # Step 3: Assert Clause 9.1 foreign contractor restriction is flagged
    print("\n[3/3] Asserting Clause 9.1 violation detected...")

    assert result["verdict"] == "VIOLATION_DETECTED", (
        f"Expected VIOLATION_DETECTED, got {result['verdict']}"
    )

    clauses_text = " ".join(result.get("breached_clauses", [])).lower()
    assert any(kw in clauses_text for kw in ["9.1", "foreign", "international"]), (
        f"Expected Clause 9.1 / foreign contractor reference in breached_clauses, got: {result['breached_clauses']}"
    )

    print("      PASS: Clause 9.1 foreign contractor restriction correctly flagged")
    print("      PASS: Verdict = VIOLATION_DETECTED")
    print(f"      PASS: Risk Level = {result['risk_level']}")

    print("\n" + "=" * 65)
    print("  All assertions passed. GrantAnchor compliance engine verified.")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())

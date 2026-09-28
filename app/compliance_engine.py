"""Compliance orchestration: deterministic decision first, advisory LLM second.

The deterministic engine (``app.engine``) owns the verdict. The LLM layer
(``app.advisory``) only adds labelled advisory notes and cannot change status,
findings or risk score.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from app import advisory, engine
from app.engine import Decision, Expense
from app.rules import (
    Rule,
    compile_text_rules,
    infer_category,
    normalize_category,
    parse_rules,
)

logger = logging.getLogger("grantanchor.compliance")

Mode = Literal["with_memory", "without_memory"]

WITHOUT_MEMORY_NOTE = (
    "CAUTION: Evaluated without persistent grant memory. Past team expenditure, cumulative caps and "
    "rolling windows were computed from this single transaction only."
)


def grant_rules(grant: dict[str, Any]) -> list[Rule]:
    """Structured policies if present, otherwise a conservative compile of the text clauses."""
    if grant.get("policies"):
        return parse_rules(grant["policies"])
    return compile_text_rules(grant.get("rules", []), home_country=grant.get("home_country", "US"))


def build_expense(
    *,
    grant_id: str,
    spender_id: str,
    spender_name: str,
    vendor: str,
    location: str,
    amount: float,
    purpose: str,
    category: str | None = None,
    expense_date: datetime | str | None = None,
    prior_approval_ref: str | None = None,
) -> Expense:
    cat = normalize_category(category) if category else infer_category(f"{purpose} {vendor}")
    return Expense(
        grant_id=grant_id,
        spender_id=spender_id,
        spender_name=spender_name,
        vendor=vendor,
        location=location,
        amount=float(amount),
        category=cat,
        expense_date=engine.parse_ts(expense_date),
        purpose=purpose,
        prior_approval_ref=prior_approval_ref or None,
    )


def decide(grant: dict[str, Any], expense: Expense, ledger: list[dict[str, Any]], mode: Mode) -> Decision:
    history = ledger if mode == "with_memory" else []
    return engine.evaluate(expense, grant_rules(grant), history)


def render(decision: Decision, expense: Expense, mode: Mode, advice: dict[str, Any] | None) -> dict[str, Any]:
    remediation = engine.remediation_text(decision)
    if mode == "without_memory":
        remediation = f"{WITHOUT_MEMORY_NOTE} {remediation}"
    advice = advice or {"status": "skipped", "findings": [], "summary": "", "model": None}
    return {
        # Legacy fields (kept stable for existing clients)
        "status": decision.status,
        "violations": [f.message for f in decision.blocking],
        "cumulative_spend": engine.cumulative_text(decision, expense.amount),
        "remediation": remediation,
        "mode": mode,
        # Engine v2 fields
        "warnings": [f.message for f in decision.warnings],
        "findings": [f.model_dump() for f in decision.findings],
        "risk_score": decision.risk_score,
        "severity_counts": {
            "blocking": len(decision.blocking),
            "warning": len(decision.warnings),
            "advisory": len(advice.get("findings", [])),
        },
        "requires_review": bool(decision.findings),
        "category": decision.category,
        "expense_date": expense.expense_date.isoformat(),
        "jurisdiction": decision.jurisdiction,
        "utilization": [u.model_dump() for u in decision.utilization],
        "advisory": advice,
        "engine_version": decision.engine_version,
        "ledger_entries_considered": decision.ledger_entries_considered,
    }


async def assess(
    *,
    grant: dict[str, Any],
    expense: Expense,
    ledger: list[dict[str, Any]],
    mode: Mode,
    recalled: list[dict[str, Any]] | None = None,
    use_advisory: bool = True,
) -> tuple[Decision, dict[str, Any]]:
    decision = decide(grant, expense, ledger, mode)
    advice = None
    if use_advisory:
        advice = await advisory.review(grant=grant, expense=expense, decision=decision, recalled=recalled or [])
    return decision, render(decision, expense, mode, advice)


def diff(with_memory: dict[str, Any], without_memory: dict[str, Any]) -> dict[str, Any]:
    """What persistent memory changed between the two evaluations."""

    def keyed(ev: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        return {(f["rule_id"], f["code"]): f for f in ev["findings"]}

    w, wo = keyed(with_memory), keyed(without_memory)
    caught = [w[k] for k in w if k not in wo]
    only_without = [wo[k] for k in wo if k not in w]
    escalated = [
        {"rule_id": k[0], "code": k[1], "without": wo[k]["severity"], "with": w[k]["severity"]}
        for k in w
        if k in wo and w[k]["severity"] != wo[k]["severity"]
    ]
    util_w = {u["rule_id"]: u for u in with_memory["utilization"]}
    util_delta = [
        {
            "rule_id": rid,
            "clause": u["clause"],
            "label": u["label"],
            "cap": u["cap"],
            "without_memory_projected": wou["projected"],
            "with_memory_projected": u["projected"],
            "prior_spend_seen_only_with_memory": round(u["projected"] - wou["projected"], 2),
        }
        for wou in without_memory["utilization"]
        for rid, u in util_w.items()
        if rid == wou["rule_id"] and u["projected"] != wou["projected"]
    ]
    return {
        "status_changed": with_memory["status"] != without_memory["status"],
        "status": {"without_memory": without_memory["status"], "with_memory": with_memory["status"]},
        "risk_delta": with_memory["risk_score"] - without_memory["risk_score"],
        "caught_by_memory": caught,
        "only_without_memory": only_without,
        "severity_changes": escalated,
        "utilization_delta": util_delta,
        "summary": (
            f"Memory surfaced {len(caught)} additional finding(s)"
            + (
                f" and changed the verdict to {with_memory['status']}"
                if with_memory["status"] != without_memory["status"]
                else ""
            )
            + "."
        ),
    }


# --------------------------------------------------------------------------- #
# Legacy entry point (signature kept for existing callers)
# --------------------------------------------------------------------------- #


async def evaluate_compliance(
    grant_id: str,
    grant_name: str,
    grant_rules: list[str],
    spender_name: str,
    spender_role: str,
    vendor: str,
    location: str,
    amount: float,
    purpose: str,
    recalled_memories: list[dict[str, Any]],
    mode: str = "with_memory",
    *,
    policies: list[dict[str, Any]] | None = None,
    category: str | None = None,
    expense_date: datetime | str | None = None,
    prior_approval_ref: str | None = None,
) -> dict[str, Any]:
    grant = {"id": grant_id, "name": grant_name, "rules": grant_rules, "policies": policies or []}
    expense = build_expense(
        grant_id=grant_id,
        spender_id=spender_name,
        spender_name=spender_name,
        vendor=vendor,
        location=location,
        amount=amount,
        purpose=purpose,
        category=category,
        expense_date=expense_date,
        prior_approval_ref=prior_approval_ref,
    )
    ledger = [m for m in recalled_memories if not m.get("grant_id") or m.get("grant_id") == grant_id]
    m: Mode = "without_memory" if mode == "without_memory" else "with_memory"
    _, result = await assess(grant=grant, expense=expense, ledger=ledger, mode=m, recalled=recalled_memories)
    return result

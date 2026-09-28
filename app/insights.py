"""Read-side analytics for the dashboard: burn, pace, guardrail utilisation, timelines."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from app import clock, engine
from app.compliance_engine import grant_rules
from app.rules import CategoryCapRule, SpenderCapRule, VendorCapRule, normalize_vendor


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _months(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _guardrail(rule: Any, entries: list[engine.LedgerEntry]) -> dict[str, Any]:
    now = clock.now()
    pool = [e for e in entries if not rule.category or e.category == rule.category]
    groups: dict[str, list[engine.LedgerEntry]] = {"": pool}
    if isinstance(rule, SpenderCapRule):
        groups = defaultdict(list)
        for e in pool:
            groups[e.spender or e.spender_id or "?"].append(e)
        if rule.spender:
            groups = {k: v for k, v in groups.items() if k.casefold() == rule.spender.casefold()}
    elif isinstance(rule, VendorCapRule):
        groups = defaultdict(list)
        for e in pool:
            groups[e.vendor].append(e)
        if rule.vendor:
            groups = {k: v for k, v in groups.items() if normalize_vendor(k) == normalize_vendor(rule.vendor)}
    worst_subject, worst = "", 0.0
    window = "lifetime"
    for subject, es in (groups or {"": []}).items():
        prior, _, window, _, _ = engine.window_peak(es, now, 0.0, rule.window)
        if prior >= worst:
            worst, worst_subject = prior, subject
    pct = round(worst / rule.cap * 100, 1)
    if isinstance(rule, CategoryCapRule):
        label = f"{rule.category} spend" if rule.category else "Total award"
    elif isinstance(rule, SpenderCapRule):
        label = f"Per member{': ' + worst_subject if worst_subject else ''}"
    else:
        label = f"Per vendor{': ' + worst_subject if worst_subject else ''}"
    state = "over" if worst > rule.cap else "warn" if pct >= rule.warn_at_pct else "ok"
    return {
        "rule_id": rule.id,
        "clause": rule.label,
        "type": rule.type,
        "label": label,
        "cap": rule.cap,
        "used": round(worst, 2),
        "pct": pct,
        "warn_at_pct": rule.warn_at_pct,
        "window": window,
        "state": state,
        "headroom": round(rule.cap - worst, 2),
        "description": rule.description,
    }


def dashboard(grant_id: str, grant: dict[str, Any], memories: list[dict[str, Any]]) -> dict[str, Any]:
    entries = [engine.LedgerEntry.from_record(m) for m in memories]
    rules = grant_rules({"id": grant_id, **grant})
    today = clock.now().date()
    start = date.fromisoformat(
        grant.get("start_date") or (min((e.timestamp.date() for e in entries), default=today)).isoformat()
    )
    end = date.fromisoformat(grant.get("end_date") or today.isoformat())
    total = float(grant.get("total_funding") or 0)
    spent = round(sum(e.amount for e in entries), 2)

    by_cat: dict[str, float] = defaultdict(float)
    by_spender: dict[str, float] = defaultdict(float)
    monthly: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in entries:
        by_cat[e.category] += e.amount
        by_spender[e.spender or "?"] += e.amount
        monthly[_month_key(e.timestamp.date())][e.category] += e.amount

    months = _months(start, min(max(today, start), end))
    period_days = max((end - start).days, 1)
    elapsed_days = min(max((today - start).days, 0), period_days)
    months_elapsed = max(elapsed_days / 30.44, 1.0)
    burn = spent / months_elapsed
    remaining = round(total - spent, 2)
    guardrails = [
        _guardrail(r, entries) for r in rules if isinstance(r, (CategoryCapRule, SpenderCapRule, VendorCapRule))
    ]
    over = sum(g["state"] == "over" for g in guardrails)
    warn = sum(g["state"] == "warn" for g in guardrails)
    pct_time = round(elapsed_days / period_days * 100, 1)
    pct_budget = round(spent / total * 100, 1) if total else 0.0
    health = max(0, 100 - 30 * over - 10 * warn - (10 if pct_budget > pct_time + 15 else 0))
    sync: dict[str, int] = defaultdict(int)
    for m in memories:
        sync[m.get("sync_status") or "pending"] += 1
    return {
        "grant_id": grant_id,
        "currency": grant.get("currency", "USD"),
        "total_funding": total,
        "spent": spent,
        "remaining": remaining,
        "burn_per_month": round(burn, 2),
        "runway_months": round(remaining / burn, 1) if burn > 0 else None,
        "projected_at_end": round(spent + burn * max((end - today).days, 0) / 30.44, 2),
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "pct_elapsed": pct_time,
            "days_left": max((end - today).days, 0),
        },
        "pct_budget_used": pct_budget,
        "pace": "ahead" if pct_budget > pct_time + 5 else "behind" if pct_budget < pct_time - 15 else "on_track",
        "health_score": health,
        "by_category": dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
        "by_spender": dict(sorted(by_spender.items(), key=lambda kv: -kv[1])),
        "timeline": [{"month": mo, "by_category": dict(monthly.get(mo, {}))} for mo in months],
        "guardrails": sorted(guardrails, key=lambda g: -g["pct"]),
        "enforced_rules": [
            {"id": r.id, "type": r.type, "clause": r.label, "description": r.description} for r in rules
        ],
        "sync": dict(sync),
        "entries": len(entries),
    }

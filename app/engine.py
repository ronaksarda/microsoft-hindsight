"""Deterministic compliance evaluation. The LLM never changes anything decided here."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from app import clock
from app.jurisdiction import LocationResult, expand_codes, resolve_location
from app.rules import (
    AllowedCategoriesRule,
    BlockedListRule,
    CapRule,
    CategoryCapRule,
    FiscalWindow,
    GrantPeriodRule,
    JurisdictionRule,
    PriorApprovalRule,
    RollingWindow,
    Rule,
    SpenderCapRule,
    VendorCapRule,
    normalize_category,
    normalize_vendor,
)

ENGINE_VERSION = "2.0.0"
BACKDATE_WARNING_DAYS = 30
FUTURE_TOLERANCE = timedelta(days=1)

Severity = Literal["blocking", "warning"]
Status = Literal["APPROVED", "APPROVED_WITH_WARNINGS", "CLAWBACK_RISK_DETECTED"]


# --------------------------------------------------------------------------- #
# Inputs / outputs
# --------------------------------------------------------------------------- #


@dataclass
class Expense:
    grant_id: str
    spender_id: str
    spender_name: str
    vendor: str
    location: str
    amount: float
    category: str
    expense_date: datetime
    purpose: str = ""
    prior_approval_ref: str | None = None


@dataclass
class LedgerEntry:
    id: str
    timestamp: datetime
    amount: float
    category: str
    spender: str
    spender_id: str | None
    vendor: str

    @classmethod
    def from_record(cls, m: dict[str, Any]) -> LedgerEntry:
        return cls(
            id=str(m.get("id", "")),
            timestamp=parse_ts(m.get("timestamp")),
            amount=float(m.get("amount") or 0.0),
            category=normalize_category(m.get("category")),
            spender=str(m.get("spender") or ""),
            spender_id=m.get("spender_id"),
            vendor=str(m.get("vendor") or ""),
        )


class Finding(BaseModel):
    rule_id: str
    rule_type: str
    clause: str
    code: str
    severity: Severity
    message: str
    source: Literal["deterministic"] = "deterministic"
    score: int = Field(ge=0, le=100)
    details: dict[str, Any] = Field(default_factory=dict)


class Utilization(BaseModel):
    rule_id: str
    clause: str
    label: str
    cap: float
    prior: float
    projected: float
    pct_before: float
    pct_after: float
    warn_at_pct: float
    window: str
    window_start: str | None = None
    window_end: str | None = None


class Decision(BaseModel):
    status: Status
    risk_score: int = Field(ge=0, le=100)
    findings: list[Finding]
    utilization: list[Utilization]
    category: str
    jurisdiction: dict[str, Any]
    engine_version: str = ENGINE_VERSION
    ledger_entries_considered: int = 0

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #


def parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time(12, 0))
    elif isinstance(value, str) and value.strip():
        v = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(v)
        except ValueError:
            dt = datetime.combine(date.fromisoformat(v[:10]), time(12, 0))
    else:
        return clock.now()
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fiscal_bounds(ts: datetime, w: FiscalWindow) -> tuple[datetime, datetime]:
    """[start, end) of the fiscal month/quarter/year containing ``ts``."""
    months = {"month": 1, "quarter": 3, "year": 12}[w.period]
    offset = (ts.month - w.start_month) % 12
    start_index = ts.year * 12 + (ts.month - 1) - (offset % months)
    sy, sm = divmod(start_index, 12)
    ey, em = divmod(start_index + months, 12)
    return (
        datetime(sy, sm + 1, 1, tzinfo=timezone.utc),
        datetime(ey, em + 1, 1, tzinfo=timezone.utc),
    )


def window_peak(
    entries: list[LedgerEntry], ts: datetime, amount: float, window: RollingWindow | FiscalWindow | None
) -> tuple[float, float, str, datetime | None, datetime | None]:
    """Worst-case window total that includes the new expense.

    Returns (prior_in_window, projected_total, label, window_start, window_end).

    Rolling windows are half-open ``(end - days, end]``. A backdated expense can
    push a *later* window over the cap, so every window end in
    ``[ts, ts + days)`` is checked, not only the window ending at ``ts``.
    """
    if window is None:
        prior = sum(e.amount for e in entries)
        return prior, prior + amount, "lifetime", None, None
    if isinstance(window, FiscalWindow):
        start, end = fiscal_bounds(ts, window)
        prior = sum(e.amount for e in entries if start <= e.timestamp < end)
        return prior, prior + amount, f"fiscal {window.period}", start, end
    span = timedelta(days=window.days)
    ends = [ts] + [e.timestamp for e in entries if ts <= e.timestamp < ts + span]
    best_prior, best_end = -1.0, ts
    for end in ends:
        prior = sum(e.amount for e in entries if end - span < e.timestamp <= end)
        if prior > best_prior:
            best_prior, best_end = prior, end
    return best_prior, best_prior + amount, f"rolling {window.days} days", best_end - span, best_end


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

_BASE_SCORES = {
    "VENDOR_BLOCKED": 100,
    "JURISDICTION_BLOCKED": 95,
    "CATEGORY_BLOCKED": 90,
    "OUTSIDE_GRANT_PERIOD": 90,
    "CATEGORY_NOT_ALLOWED": 85,
    "PRIOR_APPROVAL_REQUIRED": 80,
    "AMBIGUOUS_LOCATION": 60,
    "FUTURE_DATED": 40,
    "JURISDICTION_WAIVED": 35,
    "BACKDATED_ENTRY": 25,
}


def _cap_score(projected: float, cap: float, warn_at: float) -> tuple[str, int] | None:
    pct = projected / cap * 100
    if projected > cap:
        over = (projected - cap) / cap
        return "CAP_EXCEEDED", min(100, int(round(70 + 60 * over)))
    if pct >= warn_at:
        span = max(100 - warn_at, 1e-9)
        return "CAP_NEAR_LIMIT", int(round(30 + 35 * (pct - warn_at) / span))
    return None


def aggregate_risk(findings: list[Finding], utilization: list[Utilization]) -> int:
    base = int(round(max((u.pct_after for u in utilization), default=0.0) * 0.25))
    base = min(base, 25)
    if not findings:
        return base
    top = max(f.score for f in findings)
    return max(base, min(100, top + 3 * (len(findings) - 1)))


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


@dataclass
class _Ctx:
    expense: Expense
    ledger: list[LedgerEntry]
    location: LocationResult
    findings: list[Finding] = field(default_factory=list)
    utilization: list[Utilization] = field(default_factory=list)

    def add(self, rule: Rule, code: str, severity: Severity, message: str, score: int, **details: Any) -> None:
        if rule.severity == "warning" and severity == "blocking":
            severity = "warning"
            score = min(score, 69)
        self.findings.append(
            Finding(
                rule_id=rule.id,
                rule_type=rule.type,
                clause=rule.label,
                code=code,
                severity=severity,
                message=message,
                score=max(0, min(100, score)),
                details=details,
            )
        )


def _money(v: float) -> str:
    return f"${v:,.2f}"


def _in_scope(categories: Iterable[str] | None, category: str) -> bool:
    return not categories or category in set(categories)


def _spender_matches(e: LedgerEntry, spender_id: str, spender_name: str) -> bool:
    if e.spender_id:
        return e.spender_id == spender_id
    return e.spender.casefold() == spender_name.casefold()


def _eval_cap(ctx: _Ctx, rule: CapRule) -> None:
    x = ctx.expense
    if rule.category and rule.category != x.category:
        return
    pool = ctx.ledger
    if rule.category:
        pool = [e for e in pool if e.category == rule.category]
    subject = "total spend"
    if isinstance(rule, CategoryCapRule):
        subject = f"{rule.category} spend" if rule.category else "total award spend"
    elif isinstance(rule, SpenderCapRule):
        target = rule.spender
        if target and target not in (x.spender_id, x.spender_name) and target.casefold() != x.spender_name.casefold():
            return
        pool = [e for e in pool if _spender_matches(e, x.spender_id, x.spender_name)]
        subject = f"spend by {x.spender_name}" + (f" on {rule.category}" if rule.category else "")
    elif isinstance(rule, VendorCapRule):
        key = normalize_vendor(x.vendor)
        if rule.vendor and normalize_vendor(rule.vendor) != key:
            return
        pool = [e for e in pool if normalize_vendor(e.vendor) == key]
        subject = f"spend with {x.vendor}" + (f" on {rule.category}" if rule.category else "")

    prior, projected, wlabel, wstart, wend = window_peak(pool, x.expense_date, x.amount, rule.window)
    util = Utilization(
        rule_id=rule.id,
        clause=rule.label,
        label=subject,
        cap=rule.cap,
        prior=round(prior, 2),
        projected=round(projected, 2),
        pct_before=round(prior / rule.cap * 100, 1),
        pct_after=round(projected / rule.cap * 100, 1),
        warn_at_pct=rule.warn_at_pct,
        window=wlabel,
        window_start=wstart.isoformat() if wstart else None,
        window_end=wend.isoformat() if wend else None,
    )
    ctx.utilization.append(util)
    scored = _cap_score(projected, rule.cap, rule.warn_at_pct)
    if not scored:
        return
    code, score = scored
    details = util.model_dump()
    if code == "CAP_EXCEEDED":
        ctx.add(
            rule,
            code,
            "blocking",
            f"{rule.label}: {subject} would reach {_money(projected)} against a {_money(rule.cap)} cap "
            f"({wlabel}); prior {_money(prior)} + this {_money(x.amount)} exceeds it by {_money(projected - rule.cap)}.",
            score,
            **details,
        )
    else:
        ctx.add(
            rule,
            code,
            "warning",
            f"{rule.label}: {subject} would reach {util.pct_after:.1f}% of the {_money(rule.cap)} cap ({wlabel}); "
            f"{_money(rule.cap - projected)} headroom remains.",
            score,
            **details,
        )


def _eval_jurisdiction(ctx: _Ctx, rule: JurisdictionRule) -> None:
    x = ctx.expense
    if not _in_scope(rule.categories, x.category):
        return
    loc = ctx.location
    if loc.status != "resolved":
        ctx.add(
            rule,
            "AMBIGUOUS_LOCATION",
            "blocking",
            f"{rule.label}: cannot verify jurisdiction for '{loc.raw or '(blank)'}'. {loc.reason}. "
            "Provide 'City, Country' before committing funds.",
            _BASE_SCORES["AMBIGUOUS_LOCATION"],
            location=loc.to_dict(),
        )
        return
    assert loc.country is not None
    if rule.allowed_countries:
        allowed = expand_codes(rule.allowed_countries)
        violated = loc.country not in allowed
    else:
        violated = loc.country in expand_codes(rule.blocked_countries or [])
    if not violated:
        return
    if rule.waivable_with_prior_approval and x.prior_approval_ref:
        ctx.add(
            rule,
            "JURISDICTION_WAIVED",
            "warning",
            f"{rule.label}: vendor in {loc.country} is outside the permitted jurisdiction; proceeding under prior "
            f"approval '{x.prior_approval_ref}'. Keep the approval on file.",
            _BASE_SCORES["JURISDICTION_WAIVED"],
            country=loc.country,
        )
        return
    how = " without documented prior approval" if rule.waivable_with_prior_approval else ""
    ctx.add(
        rule,
        "JURISDICTION_BLOCKED",
        "blocking",
        f"{rule.label}: {x.category} spend with '{x.vendor}' in {loc.country} ({loc.raw}) is not permitted{how}.",
        _BASE_SCORES["JURISDICTION_BLOCKED"],
        country=loc.country,
    )


def _eval_prior_approval(ctx: _Ctx, rule: PriorApprovalRule) -> None:
    x = ctx.expense
    if not _in_scope(rule.categories, x.category) or x.amount <= rule.threshold or x.prior_approval_ref:
        return
    ctx.add(
        rule,
        "PRIOR_APPROVAL_REQUIRED",
        "blocking",
        f"{rule.label}: {x.category} spend of {_money(x.amount)} is above the {_money(rule.threshold)} "
        "prior-approval threshold and no approval reference was supplied.",
        _BASE_SCORES["PRIOR_APPROVAL_REQUIRED"],
        threshold=rule.threshold,
    )


def _eval_blocked(ctx: _Ctx, rule: BlockedListRule) -> None:
    x = ctx.expense
    v = normalize_vendor(x.vendor)
    for blocked in rule.vendors:
        b = normalize_vendor(blocked)
        if b and (v == b or f" {b} " in f" {v} "):
            ctx.add(
                rule,
                "VENDOR_BLOCKED",
                "blocking",
                f"{rule.label}: vendor '{x.vendor}' is on the blocked vendor list.",
                _BASE_SCORES["VENDOR_BLOCKED"],
                matched=blocked,
            )
            break
    if x.category in rule.categories:
        ctx.add(
            rule,
            "CATEGORY_BLOCKED",
            "blocking",
            f"{rule.label}: category '{x.category}' is not an allowable cost.",
            _BASE_SCORES["CATEGORY_BLOCKED"],
        )


def _eval_allowed(ctx: _Ctx, rule: AllowedCategoriesRule) -> None:
    if ctx.expense.category not in rule.categories:
        ctx.add(
            rule,
            "CATEGORY_NOT_ALLOWED",
            "blocking",
            f"{rule.label}: category '{ctx.expense.category}' is not in the approved budget categories "
            f"({', '.join(rule.categories)}).",
            _BASE_SCORES["CATEGORY_NOT_ALLOWED"],
        )


def _eval_period(ctx: _Ctx, rule: GrantPeriodRule) -> None:
    d = ctx.expense.expense_date.date()
    if rule.start <= d <= rule.end:
        return
    ctx.add(
        rule,
        "OUTSIDE_GRANT_PERIOD",
        "blocking",
        f"{rule.label}: expense dated {d.isoformat()} falls outside the period of performance "
        f"{rule.start.isoformat()} to {rule.end.isoformat()}.",
        _BASE_SCORES["OUTSIDE_GRANT_PERIOD"],
    )


def _date_checks(ctx: _Ctx) -> None:
    now = clock.now()
    ts = ctx.expense.expense_date
    pseudo = GrantPeriodRule(id="date_integrity", clause="Date integrity", start=date(1970, 1, 1), end=date(2100, 1, 1))
    if ts > now + FUTURE_TOLERANCE:
        ctx.add(
            pseudo,
            "FUTURE_DATED",
            "warning",
            f"Expense is dated {ts.date().isoformat()}, in the future. Confirm the date before committing.",
            _BASE_SCORES["FUTURE_DATED"],
        )
    elif ts < now - timedelta(days=BACKDATE_WARNING_DAYS):
        ctx.add(
            pseudo,
            "BACKDATED_ENTRY",
            "warning",
            f"Expense is backdated {(now - ts).days} days. Rolling-window totals were recomputed for every "
            "window that includes this date.",
            _BASE_SCORES["BACKDATED_ENTRY"],
        )


_DISPATCH = {
    "category_cap": _eval_cap,
    "spender_cap": _eval_cap,
    "vendor_cap": _eval_cap,
    "jurisdiction": _eval_jurisdiction,
    "prior_approval": _eval_prior_approval,
    "blocked_list": _eval_blocked,
    "allowed_categories": _eval_allowed,
    "grant_period": _eval_period,
}


def evaluate(expense: Expense, rules: list[Rule], ledger: list[dict[str, Any]] | list[LedgerEntry]) -> Decision:
    entries = [e if isinstance(e, LedgerEntry) else LedgerEntry.from_record(e) for e in ledger]
    ctx = _Ctx(expense=expense, ledger=entries, location=resolve_location(expense.location))
    _date_checks(ctx)
    for rule in rules:
        _DISPATCH[rule.type](ctx, rule)  # type: ignore[operator]

    blocking = any(f.severity == "blocking" for f in ctx.findings)
    status: Status = "CLAWBACK_RISK_DETECTED" if blocking else "APPROVED_WITH_WARNINGS" if ctx.findings else "APPROVED"
    ctx.findings.sort(key=lambda f: (f.severity != "blocking", -f.score))
    return Decision(
        status=status,
        risk_score=aggregate_risk(ctx.findings, ctx.utilization),
        findings=ctx.findings,
        utilization=ctx.utilization,
        category=expense.category,
        jurisdiction=ctx.location.to_dict(),
        ledger_entries_considered=len(entries),
    )


# --------------------------------------------------------------------------- #
# Presentation helpers (legacy response fields)
# --------------------------------------------------------------------------- #

_REMEDIATION = {
    "VENDOR_BLOCKED": "Do not pay this vendor from grant funds. Select an eligible vendor.",
    "JURISDICTION_BLOCKED": "File the agency prior-approval request and supply its reference before contracting. "
    "Do not disburse grant funds until written consent is received.",
    "AMBIGUOUS_LOCATION": "Re-submit with an unambiguous 'City, Country' location.",
    "CATEGORY_BLOCKED": "Charge this cost to non-grant (institutional) funds.",
    "CATEGORY_NOT_ALLOWED": "Re-classify to an approved budget category or request a budget modification.",
    "OUTSIDE_GRANT_PERIOD": "Costs outside the period of performance are unallowable. Request a no-cost extension "
    "or use other funds.",
    "PRIOR_APPROVAL_REQUIRED": "Obtain written prior approval and resubmit with its reference.",
    "CAP_EXCEEDED": "Reduce the amount, or submit a budget re-allocation request before committing.",
}


def remediation_text(decision: Decision) -> str:
    codes = [f.code for f in decision.blocking]
    if codes:
        seen: list[str] = []
        for c in codes:
            if c in _REMEDIATION and _REMEDIATION[c] not in seen:
                seen.append(_REMEDIATION[c])
        return " ".join(seen)
    if decision.warnings:
        return "Approved with warnings. Review the amber items before the next commitment in these categories."
    return "Compliant with all machine-enforced grant terms. Proceed with standard procurement."


def cumulative_text(decision: Decision, amount: float) -> str:
    if not decision.utilization:
        return f"{_money(amount)} ({decision.category}; no cumulative caps apply)"
    u = max(decision.utilization, key=lambda u: u.pct_after)
    return (
        f"{_money(u.projected)} of {_money(u.cap)} {u.label} cap ({u.pct_after:.1f}%, {u.window}; "
        f"prior {_money(u.prior)})"
    )

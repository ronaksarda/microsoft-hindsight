"""Deterministic engine: rule types and edge cases."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app import advisory, clock, compliance_engine, engine, llm
from app.jurisdiction import resolve_location
from app.main import app
from app.rules import parse_rules

NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: NOW)


def exp(amount, category="Travel", when=NOW, location="Austin, TX", vendor="Delta", spender="tm_1", ref=None):
    return engine.Expense(
        grant_id="G", spender_id=spender, spender_name=spender, vendor=vendor, location=location,
        amount=amount, category=category, expense_date=when, prior_approval_ref=ref,
    )


def entry(amount, when, category="Travel", vendor="Delta", spender="tm_1"):
    return {"id": f"m{amount}{when}", "amount": amount, "timestamp": when, "category": category,
            "vendor": vendor, "spender": spender, "spender_id": spender}


def codes(d):
    return {f.code for f in d.findings}


def test_category_cap_exactly_at_cap_is_not_blocking():
    rules = parse_rules([{"id": "t", "type": "category_cap", "category": "Travel", "cap": 8000}])
    d = engine.evaluate(exp(2600), rules, [entry(5400, "2026-06-01")])
    assert d.status == "APPROVED_WITH_WARNINGS" and codes(d) == {"CAP_NEAR_LIMIT"}
    d = engine.evaluate(exp(2600.01), rules, [entry(5400, "2026-06-01")])
    assert d.status == "CLAWBACK_RISK_DETECTED" and "CAP_EXCEEDED" in codes(d)


def test_early_warning_tier():
    rules = parse_rules([{"id": "t", "type": "category_cap", "category": "Travel", "cap": 1000, "warn_at_pct": 80}])
    assert engine.evaluate(exp(790), rules, []).status == "APPROVED"
    d = engine.evaluate(exp(800), rules, [])
    assert d.status == "APPROVED_WITH_WARNINGS" and 30 <= d.risk_score < 70


def test_rolling_window_boundary_excludes_entry_exactly_window_days_old():
    rules = parse_rules([{"id": "r", "type": "category_cap", "category": "Travel", "cap": 1000,
                          "window": {"kind": "rolling", "days": 90}}])
    edge = datetime(2026, 6, 30, 12, tzinfo=timezone.utc)  # exactly 90 days before NOW
    assert engine.evaluate(exp(600), rules, [entry(600, edge.isoformat())]).status == "APPROVED"
    inside = datetime(2026, 6, 30, 12, 1, tzinfo=timezone.utc)
    assert engine.evaluate(exp(600), rules, [entry(600, inside.isoformat())]).status == "CLAWBACK_RISK_DETECTED"


def test_backdated_entry_checks_later_windows():
    rules = parse_rules([{"id": "r", "type": "category_cap", "category": "Travel", "cap": 1000,
                          "window": {"kind": "rolling", "days": 30}}])
    ledger = [entry(500, "2026-05-10T00:00:00Z"), entry(400, "2026-05-25T00:00:00Z")]
    # Backdated to May 1: window ending May 1 holds nothing, but the window ending May 25 holds 900 + 200.
    d = engine.evaluate(exp(200, when=datetime(2026, 5, 1, tzinfo=timezone.utc)), rules, ledger)
    assert "CAP_EXCEEDED" in codes(d) and "BACKDATED_ENTRY" in codes(d)


def test_fiscal_quarter_window():
    rules = parse_rules([{"id": "q", "type": "category_cap", "category": "Compute", "cap": 1000,
                          "window": {"kind": "fiscal", "period": "quarter", "start_month": 10}}])
    ledger = [entry(900, "2026-09-15T00:00:00Z", category="Compute")]  # fiscal Q4 (Jul-Sep)
    assert engine.evaluate(exp(200, "Compute"), rules, ledger).status == "CLAWBACK_RISK_DETECTED"
    oct1 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert codes(engine.evaluate(exp(200, "Compute", when=oct1), rules, ledger)) == {"FUTURE_DATED"}


def test_spender_and_vendor_caps():
    rules = parse_rules([
        {"id": "s", "type": "spender_cap", "cap": 1000},
        {"id": "v", "type": "vendor_cap", "cap": 1000},
    ])
    ledger = [entry(900, "2026-09-01", spender="tm_2", vendor="Acme Inc.")]
    assert engine.evaluate(exp(200, vendor="Other"), rules, ledger).status == "APPROVED"
    d = engine.evaluate(exp(200, vendor="ACME inc"), rules, ledger)
    assert [f.rule_id for f in d.blocking] == ["v"]


def test_jurisdiction_allow_list_ambiguous_and_waiver():
    rules = parse_rules([{"id": "j", "type": "jurisdiction", "allowed_countries": ["US"],
                          "categories": ["Contractor"]}])
    assert "JURISDICTION_BLOCKED" in codes(engine.evaluate(exp(10, "Contractor", location="Oslo, Norway"), rules, []))
    assert "AMBIGUOUS_LOCATION" in codes(engine.evaluate(exp(10, "Contractor", location="Tbilisi, Georgia"), rules, []))
    assert "AMBIGUOUS_LOCATION" in codes(engine.evaluate(exp(10, "Contractor", location="Paris"), rules, []))
    w = engine.evaluate(exp(10, "Contractor", location="Oslo, Norway", ref="PA-1"), rules, [])
    assert w.status == "APPROVED_WITH_WARNINGS"
    assert engine.evaluate(exp(10, "Travel", location="Oslo, Norway"), rules, []).status == "APPROVED"


@pytest.mark.parametrize("text,country", [
    ("Oslo, Norway", "NO"), ("Austin, TX", "US"), ("London, UK", "GB"), ("Boston, USA", "US"),
    ("US-East (Virginia)", "US"), ("Munich, Deutschland", "DE"),
])
def test_resolve_location(text, country):
    assert resolve_location(text).country == country


@pytest.mark.parametrize("text", ["Toronto, CA", "Tbilisi, Georgia", "Berlin, Germany / Oslo, Norway", "Paris", ""])
def test_resolve_location_never_guesses(text):
    assert resolve_location(text).status != "resolved"


def test_prior_approval_blocked_list_allowed_categories_period():
    rules = parse_rules([
        {"id": "p", "type": "prior_approval", "threshold": 5000, "categories": ["Equipment"]},
        {"id": "b", "type": "blocked_list", "vendors": ["Bad Corp"], "categories": ["Alcohol"]},
        {"id": "a", "type": "allowed_categories", "categories": ["Equipment", "Travel", "Alcohol"]},
        {"id": "g", "type": "grant_period", "start": "2026-01-01", "end": "2026-12-31"},
    ])
    assert engine.evaluate(exp(5000, "Equipment"), rules, []).status == "APPROVED"
    assert "PRIOR_APPROVAL_REQUIRED" in codes(engine.evaluate(exp(5000.01, "Equipment"), rules, []))
    assert engine.evaluate(exp(6000, "Equipment", ref="A1"), rules, []).status == "APPROVED"
    assert "VENDOR_BLOCKED" in codes(engine.evaluate(exp(1, vendor="Bad Corp LLC"), rules, []))
    assert "CATEGORY_BLOCKED" in codes(engine.evaluate(exp(1, "Alcohol"), rules, []))
    assert "CATEGORY_NOT_ALLOWED" in codes(engine.evaluate(exp(1, "Software"), rules, []))
    late = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert "OUTSIDE_GRANT_PERIOD" in codes(engine.evaluate(exp(1, when=late), rules, []))


def test_invalid_rules_rejected():
    for bad in [
        {"id": "x", "type": "category_cap", "cap": -1},
        {"id": "x", "type": "jurisdiction"},
        {"id": "x", "type": "grant_period", "start": "2027-01-01", "end": "2026-01-01"},
        {"id": "x", "type": "nope"},
    ]:
        with pytest.raises(ValueError):
            parse_rules([bad])


@pytest.mark.anyio
async def test_llm_cannot_override_deterministic_decision(monkeypatch):
    class FakeLLM:
        enabled = True
        model = "fake"

        async def complete_json(self, *a, **k):
            return llm.LLMResult(data={"status": "APPROVED", "findings": [{"message": "Looks fine", "confidence": "high"}],
                                       "summary": "approve"}, model="fake", attempts=1)

    monkeypatch.setattr(llm, "get_client", lambda: FakeLLM())
    grant = {"id": "G", "policies": [{"id": "t", "type": "category_cap", "category": "Travel", "cap": 100}]}
    decision, out = await compliance_engine.assess(grant=grant, expense=exp(500), ledger=[], mode="with_memory")
    assert out["status"] == "CLAWBACK_RISK_DETECTED"
    assert out["advisory"]["status"] == "ok"
    assert all(f["label"] == "advisory" for f in out["advisory"]["findings"])
    assert out["severity_counts"]["advisory"] == 1


@pytest.mark.anyio
async def test_malformed_llm_output_is_dropped(monkeypatch):
    class FakeLLM:
        enabled = True
        model = "fake"

        async def complete_json(self, *a, **k):
            return llm.LLMResult(data={"findings": "nope"}, model="fake", attempts=1)

    monkeypatch.setattr(llm, "get_client", lambda: FakeLLM())
    out = await advisory.review(grant={"id": "G"}, expense=exp(1), decision=engine.evaluate(exp(1), [], []), recalled=[])
    assert out["status"] == "invalid" and out["findings"] == []


@pytest.mark.anyio
async def test_compare_endpoint_shows_what_memory_caught():
    payload = {"grant_id": "NSF-2026-881", "spender_id": "tm_3", "vendor": "ANA", "location": "Tokyo, Japan",
               "amount": 3100, "purpose": "Flight to Tokyo summit", "category": "Travel"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/expenses/audit/compare", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["without_memory"]["status"] != "CLAWBACK_RISK_DETECTED"
        assert body["with_memory"]["status"] == "CLAWBACK_RISK_DETECTED"
        assert body["diff"]["status_changed"]
        assert any(f["rule_id"] == "nsf-travel" for f in body["diff"]["caught_by_memory"])
        state = (await c.get("/api/state")).json()
        assert len(state["memories"]) == 1  # compare never commits


@pytest.mark.anyio
async def test_audit_commits_only_non_blocking():
    base = {"grant_id": "NSF-2026-881", "spender_id": "tm_1", "vendor": "AWS", "location": "Seattle, USA",
            "amount": 600, "purpose": "GPU compute", "category": "Compute"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        ok = (await c.post("/api/audit-expense", json=base)).json()["evaluation"]
        assert ok["status"] == "APPROVED" and ok["committed"]
        bad = (await c.post("/api/audit-expense", json={**base, "category": "Alcohol"})).json()["evaluation"]
        assert bad["status"] == "CLAWBACK_RISK_DETECTED" and not bad["committed"]
        assert len((await c.get("/api/state")).json()["memories"]) == 2

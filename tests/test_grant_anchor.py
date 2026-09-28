import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app import local_store, compliance_engine, hindsight


@pytest.fixture(autouse=True)
def reset_store_before_each():
    local_store.reset_seed()
    yield
    local_store.reset_seed()


@pytest.mark.anyio
async def test_local_store_crud():
    store = local_store.load_store()
    assert store["active_grant_id"] == "NSF-2026-881"
    assert len(store["team_members"]) == 3
    assert len(store["memories"]) == 1

    # Add memory
    rec = local_store.add_memory(
        grant_id="NSF-2026-881",
        spender="Ronak Sarda",
        category="Compute",
        amount=600.0,
        vendor="Amazon Web Services",
        content="Ronak Sarda provisioned GPU cluster compute ($600).",
    )
    assert rec["amount"] == 600.0
    updated = local_store.load_store()
    assert len(updated["memories"]) == 2

    # Switch grant
    local_store.switch_grant("EU-HORIZON-409")
    switched = local_store.load_store()
    assert switched["active_grant_id"] == "EU-HORIZON-409"


@pytest.mark.anyio
async def test_hindsight_local_sync():
    rec = await hindsight.retain(
        bank_id="test-bank",
        content="Test sync content",
        grant_id="NSF-2026-881",
        spender="David Park",
        category="Hardware",
        amount=1200.0,
        vendor="NVIDIA",
    )
    assert rec["record"]["amount"] == 1200.0

    memories = await hindsight.recall(
        bank_id="test-bank",
        query="NVIDIA hardware",
        grant_id="NSF-2026-881",
    )
    assert len(memories) >= 1
    assert any(m["vendor"] == "NVIDIA" for m in memories)


@pytest.mark.anyio
async def test_quick_fill_1_foreign_contractor_breach():
    store = local_store.load_store()
    grant = store["grants"]["NSF-2026-881"]

    res = await compliance_engine.evaluate_compliance(
        grant_id="NSF-2026-881",
        grant_name=grant["name"],
        grant_rules=grant["rules"],
        spender_name="Sarah Miller",
        spender_role="Head of Operations",
        vendor="Oslo Dev Lab",
        location="Oslo, Norway",
        amount=8500.0,
        purpose="Foreign contractor invoice for Milestone 2 development",
        recalled_memories=store["memories"],
        mode="with_memory",
    )
    assert res["status"] == "CLAWBACK_RISK_DETECTED"
    assert any("Clause 9.1" in v or "foreign" in v.lower() for v in res["violations"])


@pytest.mark.anyio
async def test_quick_fill_2_cumulative_travel_breach():
    store = local_store.load_store()
    grant = store["grants"]["NSF-2026-881"]

    # Sarah already spent $5,400 on travel. David's $3,100 makes it $8,500 > $8,000
    res = await compliance_engine.evaluate_compliance(
        grant_id="NSF-2026-881",
        grant_name=grant["name"],
        grant_rules=grant["rules"],
        spender_name="David Park",
        spender_role="Senior ML Engineer",
        vendor="ANA / Tokyo Hilton",
        location="Tokyo, Japan",
        amount=3100.0,
        purpose="Flight and hotel for Tokyo Robotics Summit presentation",
        recalled_memories=store["memories"],
        mode="with_memory",
    )
    assert res["status"] == "CLAWBACK_RISK_DETECTED"
    assert any("Clause 4.2" in v or "travel" in v.lower() for v in res["violations"])


@pytest.mark.anyio
async def test_quick_fill_3_domestic_compute_approved():
    store = local_store.load_store()
    grant = store["grants"]["NSF-2026-881"]

    res = await compliance_engine.evaluate_compliance(
        grant_id="NSF-2026-881",
        grant_name=grant["name"],
        grant_rules=grant["rules"],
        spender_name="Ronak Sarda",
        spender_role="Lead Architect",
        vendor="Amazon Web Services",
        location="US-East (Virginia)",
        amount=600.0,
        purpose="Production model training cluster compute",
        recalled_memories=store["memories"],
        mode="with_memory",
    )
    assert res["status"] == "APPROVED"
    assert len(res["violations"]) == 0


@pytest.mark.anyio
async def test_without_memory_mode():
    store = local_store.load_store()
    grant = store["grants"]["NSF-2026-881"]

    # Without memory, even $3,100 travel is treated blindly as approved
    res = await compliance_engine.evaluate_compliance(
        grant_id="NSF-2026-881",
        grant_name=grant["name"],
        grant_rules=grant["rules"],
        spender_name="David Park",
        spender_role="Senior ML Engineer",
        vendor="ANA / Tokyo Hilton",
        location="Tokyo, Japan",
        amount=3100.0,
        purpose="Flight and hotel for Tokyo Robotics Summit presentation",
        recalled_memories=store["memories"],
        mode="without_memory",
    )
    assert res["status"] == "APPROVED"
    assert "without persistent grant memory" in res["remediation"].lower() or "blind" in res["remediation"].lower()


@pytest.mark.anyio
async def test_fastapi_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET /api/state
        resp = await client.get("/api/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active_grant_id"] == "NSF-2026-881"

        # POST /api/switch-grant
        resp = await client.post("/api/switch-grant", json={"grant_id": "EU-HORIZON-409"})
        assert resp.status_code == 200
        assert resp.json()["active_grant_id"] == "EU-HORIZON-409"

        # POST /api/audit-expense
        audit_payload = {
            "grant_id": "NSF-2026-881",
            "spender_id": "tm_2",
            "vendor": "Oslo Dev Lab",
            "location": "Oslo, Norway",
            "amount": 8500.0,
            "purpose": "Milestone foreign contract",
            "mode": "with_memory",
        }
        resp = await client.post("/api/audit-expense", json=audit_payload)
        assert resp.status_code == 200
        audit_res = resp.json()["evaluation"]
        assert audit_res["status"] == "CLAWBACK_RISK_DETECTED"

        # POST /api/reset-seed
        resp = await client.post("/api/reset-seed")
        assert resp.status_code == 200
        assert resp.json()["state"]["active_grant_id"] == "NSF-2026-881"

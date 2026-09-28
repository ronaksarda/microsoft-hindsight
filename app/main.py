"""GrantAnchor FastAPI application serving API routes and dark terminal command center."""

from __future__ import annotations

import logging
from pathlib import Path
from datetime import date, datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app import compliance_engine, engine, hindsight, local_store, memory_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("grantanchor.main")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="GrantAnchor",
    description="Autonomous grant compliance and budget milestone memory engine",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- Request Models ---

class SwitchGrantRequest(BaseModel):
    grant_id: str


class AuditExpenseRequest(BaseModel):
    grant_id: str = Field(..., description="Target grant identifier")
    spender_id: str = Field(..., description="ID of the team member committing funds")
    vendor: str = Field(..., min_length=1, max_length=200, description="Vendor or counterparty name")
    location: str = Field(default="", max_length=200, description="Vendor location, 'City, Country'")
    amount: float = Field(..., gt=0, le=1e9, description="Amount in the grant currency")
    purpose: str = Field(..., min_length=1, max_length=2000, description="Operational or technical justification")
    mode: Literal["with_memory", "without_memory"] = Field(default="with_memory")
    category: str | None = Field(default=None, max_length=60, description="Budget category; inferred if omitted")
    expense_date: datetime | date | None = Field(default=None, description="When the cost was incurred (default now)")
    prior_approval_ref: str | None = Field(default=None, max_length=120, description="Agency approval reference")
    commit: bool = Field(default=True, description="Record approved expenses in the ledger")


# --- Endpoints ---

@app.get("/")
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="static/index.html not found")
    return FileResponse(index_path)


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    """Return the entire synchronized local store state."""
    return local_store.load_store()


@app.post("/api/switch-grant")
async def switch_active_grant(req: SwitchGrantRequest) -> dict[str, Any]:
    """Switch active grant context."""
    try:
        updated = local_store.switch_grant(req.grant_id)
        return {"status": "success", "active_grant_id": req.grant_id, "state": updated}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _resolve(req: AuditExpenseRequest) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    store = local_store.load_store()
    grant_info = store.get("grants", {}).get(req.grant_id)
    if not grant_info:
        raise HTTPException(status_code=404, detail=f"Grant '{req.grant_id}' not found in registry")
    grant = {"id": req.grant_id, **grant_info}
    team = {m["id"]: m for m in store.get("team_members", [])}
    spender = team.get(req.spender_id, {"id": req.spender_id, "name": req.spender_id, "role": "Team Contributor"})
    ledger = [m for m in store.get("memories", []) if m.get("grant_id") == req.grant_id]
    return grant, spender, ledger


def _expense(req: AuditExpenseRequest, spender: dict[str, Any]) -> engine.Expense:
    return compliance_engine.build_expense(
        grant_id=req.grant_id,
        spender_id=req.spender_id,
        spender_name=spender.get("name", req.spender_id),
        vendor=req.vendor,
        location=req.location,
        amount=req.amount,
        purpose=req.purpose,
        category=req.category,
        expense_date=req.expense_date,
        prior_approval_ref=req.prior_approval_ref,
    )


async def _recall(req: AuditExpenseRequest) -> list[dict[str, Any]]:
    return await hindsight.recall(
        bank_id=settings.hindsight_bank_id,
        query=f"{req.vendor} {req.location} {req.purpose}",
        top_k=10,
        grant_id=req.grant_id,
    )


@app.post("/api/audit-expense")
async def audit_expense(req: AuditExpenseRequest) -> dict[str, Any]:
    """Audit a proposed expenditure. Approved spend is committed to the ledger (with_memory, commit=true)."""
    grant, spender, ledger = _resolve(req)
    expense = _expense(req, spender)
    recalled = await _recall(req) if req.mode == "with_memory" else []
    decision, result = await compliance_engine.assess(
        grant=grant, expense=expense, ledger=ledger, mode=req.mode, recalled=recalled
    )

    committed: dict[str, Any] | None = None
    if req.mode == "with_memory" and req.commit and not decision.blocking:
        content = (
            f"{spender.get('name')} disbursed ${req.amount:,.2f} to {req.vendor} ({req.location}) "
            f"for: {req.purpose}"
        )
        out = await memory_sync.record_memory(
            grant_id=req.grant_id,
            spender=spender.get("name", "Team Member"),
            spender_id=req.spender_id,
            category=expense.category,
            amount=req.amount,
            vendor=req.vendor,
            location=req.location,
            content=content,
            timestamp=expense.expense_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        committed = out["record"]
    result["committed"] = committed is not None
    result["memory_id"] = committed["id"] if committed else None

    return {
        "evaluation": result,
        "expense": {
            "grant_id": req.grant_id,
            "spender": spender,
            "vendor": req.vendor,
            "location": req.location,
            "amount": req.amount,
            "purpose": req.purpose,
            "mode": req.mode,
            "category": expense.category,
            "expense_date": expense.expense_date.isoformat(),
        },
        "recalled_count": len(recalled),
    }


@app.post("/api/expenses/audit/compare")
async def compare_audit(req: AuditExpenseRequest) -> dict[str, Any]:
    """Run the same expense with and without memory. Never commits."""
    grant, spender, ledger = _resolve(req)
    expense = _expense(req, spender)
    recalled = await _recall(req)
    _, with_mem = await compliance_engine.assess(
        grant=grant, expense=expense, ledger=ledger, mode="with_memory", recalled=recalled
    )
    _, without_mem = await compliance_engine.assess(
        grant=grant, expense=expense, ledger=ledger, mode="without_memory", use_advisory=False
    )
    return {
        "with_memory": with_mem,
        "without_memory": without_mem,
        "diff": compliance_engine.diff(with_mem, without_mem),
        "recalled_count": len(recalled),
    }


@app.post("/api/reset-seed")
async def reset_seed() -> dict[str, Any]:
    """Reset store to default initial baseline."""
    fresh = local_store.reset_seed()
    return {"status": "reset_successful", "state": fresh}

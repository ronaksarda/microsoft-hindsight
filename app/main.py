"""GrantAnchor FastAPI application serving API routes and dark terminal command center."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app import hindsight, local_store, compliance_engine

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
    vendor: str = Field(..., description="Vendor or counterparty name")
    location: str = Field(default="", description="Vendor geographical location")
    amount: float = Field(..., gt=0, description="Amount to be audited")
    purpose: str = Field(..., description="Operational or technical justification")
    mode: str = Field(default="with_memory", description="'with_memory' or 'without_memory'")


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


@app.post("/api/audit-expense")
async def audit_expense(req: AuditExpenseRequest) -> dict[str, Any]:
    """Audit proposed expenditure against grant rules and persistent team memory."""
    store = local_store.load_store()

    # Resolve grant
    grant_info = store.get("grants", {}).get(req.grant_id)
    if not grant_info:
        raise HTTPException(status_code=404, detail=f"Grant '{req.grant_id}' not found in registry")

    # Resolve spender
    team = {m["id"]: m for m in store.get("team_members", [])}
    spender = team.get(req.spender_id, {"name": req.spender_id, "role": "Team Contributor"})

    # Recall past memories if with_memory mode is active
    recalled: list[dict[str, Any]] = []
    if req.mode == "with_memory":
        query = f"{req.vendor} {req.location} {req.purpose} travel contractor"
        recalled = await hindsight.recall(
            bank_id=settings.hindsight_bank_id,
            query=query,
            top_k=10,
            grant_id=req.grant_id,
        )

    # Evaluate compliance via Groq / Deterministic engine
    result = await compliance_engine.evaluate_compliance(
        grant_id=req.grant_id,
        grant_name=grant_info.get("name", req.grant_id),
        grant_rules=grant_info.get("rules", []),
        spender_name=spender.get("name", "Team Member"),
        spender_role=spender.get("role", "Staff"),
        vendor=req.vendor,
        location=req.location,
        amount=req.amount,
        purpose=req.purpose,
        recalled_memories=recalled,
        mode=req.mode,
    )

    # If approved and using memory, record to persistent memory
    if req.mode == "with_memory" and result.get("status") == "APPROVED":
        content = (
            f"{spender.get('name')} disbursed ${req.amount:,.2f} to {req.vendor} ({req.location}) "
            f"for '{req.purpose}'."
        )
        category = "Travel" if any(k in req.purpose.lower() for k in ["flight", "travel", "hotel"]) else "General"
        await hindsight.retain(
            bank_id=settings.hindsight_bank_id,
            content=content,
            grant_id=req.grant_id,
            spender=spender.get("name", "Team Member"),
            category=category,
            amount=req.amount,
            vendor=req.vendor,
        )

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
        },
        "recalled_count": len(recalled),
    }


@app.post("/api/reset-seed")
async def reset_seed() -> dict[str, Any]:
    """Reset store to default initial baseline."""
    fresh = local_store.reset_seed()
    return {"status": "reset_successful", "state": fresh}

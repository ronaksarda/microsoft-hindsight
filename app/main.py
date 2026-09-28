"""GrantAnchor FastAPI application: API, auth, and the web app."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import (
    auth,
    clock,
    compliance_engine,
    documents,
    engine,
    fx,
    grant_import,
    hindsight,
    insights,
    learning,
    llm,
    local_store,
    memory_sync,
)
from app.config import settings
from app.rules import parse_rules

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("grantanchor.main")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
PUBLIC_API = ("/api/auth/", "/api/health")


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = None
    if hindsight.get_client().enabled:
        # Push any ledger entries that never reached Hindsight (seed data, earlier outages).
        task = asyncio.create_task(_background_sync())
    yield
    if task:
        task.cancel()


async def _background_sync() -> None:
    try:
        await memory_sync.sync_pending(limit=200)
    except Exception as exc:  # never crash the app over a sync
        logger.warning("background sync failed: %s", exc)


app = FastAPI(
    title="GrantAnchor",
    description="Grant compliance checks backed by a persistent spend ledger and Hindsight memory.",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _user(request: Request) -> dict[str, Any] | None:
    if not settings.require_login:
        return {"id": "local", "email": "", "name": "Local user", "role": "admin"}
    return auth.user_for_token(request.cookies.get(auth.COOKIE))


@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith(PUBLIC_API):
        user = _user(request)
        if user is None:
            return JSONResponse({"detail": "Please sign in."}, status_code=401)
        request.state.user = user
    return await call_next(request)


def who(request: Request) -> dict[str, Any]:
    return getattr(request.state, "user", None) or _user(request) or {"name": "unknown", "id": ""}


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


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
    currency: str | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="Currency of `amount` (ISO code). Omit to use the grant's currency.",
    )
    fx_rate: float | None = Field(
        default=None, gt=0, le=1e6, description="Your own rate: grant-currency units per 1 unit of `currency`."
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "grant_id": "NSF-2026-881",
                "spender_id": "tm_3",
                "vendor": "ANA",
                "location": "Tokyo, Japan",
                "amount": 3100,
                "purpose": "Flights to a robotics summit",
                "category": "Travel",
            }
        }
    }


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class MemoryPatch(BaseModel):
    amount: float | None = Field(
        default=None, gt=0, le=1e9, description="In `currency` (or the entry's original currency)"
    )
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    fx_rate: float | None = Field(default=None, gt=0, le=1e6)
    vendor: str | None = Field(default=None, min_length=1, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, min_length=1, max_length=60)
    expense_date: date | None = None
    spender_id: str | None = None
    content: str | None = Field(default=None, max_length=2000)
    reason: str = Field(min_length=1, max_length=300, description="Why the entry is being corrected")
    confirm: bool = Field(default=False, description="Save even if the edited entry breaks a blocking rule")


class MemberPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    role: str | None = Field(default=None, max_length=100)


class MemberRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: str = Field(default="Team member", max_length=100)


class GrantBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    total_funding: float = Field(gt=0, le=1e12)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    start_date: date
    end_date: date
    home_country: str = Field(default="US", min_length=2, max_length=2)
    rules: list[str] = Field(default_factory=list, description="Free-text clauses shown to people and the AI reviewer")
    policies: list[dict[str, Any]] = Field(default_factory=list, description="Machine-checked rules (see app/rules.py)")


class GrantCreate(GrantBody):
    add_total_cap: bool = Field(default=True, description="Add a rule capping total spend at the award amount")
    add_period_rule: bool = Field(default=True, description="Add a rule blocking costs outside the grant dates")
    id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")


class GrantPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    total_funding: float | None = Field(default=None, gt=0, le=1e12)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    start_date: date | None = None
    end_date: date | None = None
    home_country: str | None = Field(default=None, min_length=2, max_length=2)
    rules: list[str] | None = None
    policies: list[dict[str, Any]] | None = None


class ExtractRequest(BaseModel):
    text: str = Field(min_length=20, max_length=grant_import.MAX_CHARS)


class DeleteRequest(BaseModel):
    reason: str = Field(default="Correction", min_length=1, max_length=300)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


@app.get("/", include_in_schema=False)
async def serve_index(request: Request):
    if _user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login", include_in_schema=False)
async def serve_login(request: Request):
    if settings.require_login and _user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def _set_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        auth.COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=auth.SESSION_DAYS * 86400,
        path="/",
    )


@app.post("/api/auth/signup", tags=["auth"])
async def signup(req: SignupRequest, response: Response) -> dict[str, Any]:
    try:
        user = auth.signup(req.email, req.name, req.password)
    except auth.AuthError as exc:
        raise HTTPException(409, str(exc)) from exc
    _set_cookie(response, auth.create_session(user["id"]))
    return {"user": user}


@app.post("/api/auth/login", tags=["auth"])
async def login(req: LoginRequest, response: Response) -> dict[str, Any]:
    try:
        user = auth.login(req.email, req.password)
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    _set_cookie(response, auth.create_session(user["id"]))
    return {"user": user}


@app.post("/api/auth/logout", tags=["auth"])
async def logout(request: Request, response: Response) -> dict[str, str]:
    auth.end_session(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"status": "signed_out"}


@app.get("/api/auth/me", tags=["auth"])
async def me(request: Request) -> dict[str, Any]:
    user = _user(request)
    if user is None:
        raise HTTPException(401, "Please sign in.")
    return {"user": user, "login_required": settings.require_login}


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #


@app.get("/api/health", tags=["system"])
async def health() -> dict[str, Any]:
    try:
        local_store.conn().execute("SELECT 1")
        db = {"status": "ok", "path": str(local_store.db_path().name)}
    except Exception as exc:
        db = {"status": "error", "error": str(exc)}
    hs, gq = await asyncio.gather(hindsight.get_client().health(), llm.get_client().ping())
    hs["bank"] = settings.hindsight_bank_id if hindsight.get_client().enabled else None
    gq["model"] = settings.groq_model if llm.get_client().enabled else None
    return {"database": db, "hindsight": hs, "llm": gq, "engine_version": engine.ENGINE_VERSION}


# --------------------------------------------------------------------------- #
# State & grants
# --------------------------------------------------------------------------- #


@app.get("/api/categories", tags=["grants"])
async def categories() -> dict[str, list[str]]:
    """Standard budget categories. Grants and expenses may also use their own."""
    from app.rules import CANONICAL_CATEGORIES

    return {"categories": [c for c in CANONICAL_CATEGORIES if c != "General"]}


@app.get("/api/fx/currencies", tags=["currency"])
async def fx_currencies() -> dict[str, Any]:
    """Currencies you can pay in (ECB reference currencies)."""
    try:
        return {"currencies": await fx.currencies(), "source": "European Central Bank via frankfurter.dev"}
    except fx.FxError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/fx/rate", tags=["currency"])
async def fx_rate(
    base: str = Query(..., min_length=3, max_length=3),
    quote: str = Query(..., min_length=3, max_length=3),
    day: date | None = None,
) -> dict[str, Any]:
    """Rate for converting 1 unit of `base` into `quote` on `day` (default today)."""
    try:
        return (await fx.get_rate(base, quote, day)).to_dict()
    except fx.FxError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/state", tags=["grants"])
async def get_state() -> dict[str, Any]:
    """Grants, team and ledger in one payload (legacy shape)."""
    return local_store.load_store()


@app.post("/api/switch-grant", tags=["grants"])
async def switch_active_grant(req: SwitchGrantRequest) -> dict[str, Any]:
    try:
        updated = local_store.switch_grant(req.grant_id)
        return {"status": "success", "active_grant_id": req.grant_id, "state": updated}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _grant(grant_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    store = local_store.load_store()
    g = store["grants"].get(grant_id)
    if not g:
        raise HTTPException(404, f"Grant '{grant_id}' not found")
    return g, store


@app.get("/api/grants/{grant_id}/dashboard", tags=["grants"])
async def grant_dashboard(grant_id: str) -> dict[str, Any]:
    """Burn rate, runway, pace, guardrail utilisation and monthly timeline."""
    g, store = _grant(grant_id)
    mems = [m for m in store["memories"] if m.get("grant_id") == grant_id]
    return insights.dashboard(grant_id, g, mems)


def _clean_grant(data: dict[str, Any]) -> dict[str, Any]:
    """Validate policies and dates; raise 422 with a readable message."""
    if data["start_date"] > data["end_date"]:
        raise HTTPException(422, "The start date must be on or before the end date.")
    try:
        policies = parse_rules(data.get("policies") or [])
    except ValueError as exc:
        raise HTTPException(422, f"Invalid rule: {exc}") from exc
    data["policies"] = [p.model_dump(mode="json", exclude_none=True) for p in policies]
    data["currency"] = data["currency"].upper()
    data["home_country"] = data["home_country"].upper()
    data["start_date"] = data["start_date"].isoformat()
    data["end_date"] = data["end_date"].isoformat()
    return data


def _slug(name: str) -> str:
    import re
    import uuid

    base = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").upper()[:40] or "GRANT"
    return base if not local_store.grant_exists(base) else f"{base}-{uuid.uuid4().hex[:4].upper()}"


@app.post("/api/grants", tags=["grants"], status_code=201)
async def create_grant(req: GrantCreate, request: Request) -> dict[str, Any]:
    """Create a grant. A grant-period rule and a total-award cap are added if missing."""
    gid = req.id or _slug(req.name)
    if local_store.get_grant(gid):
        raise HTTPException(409, f"A grant with id '{gid}' already exists.")
    data = req.model_dump(exclude={"id", "add_total_cap", "add_period_rule"})
    kinds = {p.get("type") for p in data["policies"]}
    if req.add_period_rule and "grant_period" not in kinds:
        data["policies"].insert(
            0,
            {
                "id": "period",
                "type": "grant_period",
                "clause": "Period of performance",
                "start": data["start_date"].isoformat(),
                "end": data["end_date"].isoformat(),
            },
        )
    if req.add_total_cap and not any(
        p.get("type") == "category_cap" and not p.get("category") for p in data["policies"]
    ):
        data["policies"].insert(
            1,
            {
                "id": "total",
                "type": "category_cap",
                "clause": "Total award",
                "category": None,
                "cap": data["total_funding"],
                "warn_at_pct": 90,
            },
        )
    grant = local_store.save_grant(gid, _clean_grant(data))
    user = who(request)
    if not local_store.load_store()["team_members"] and user.get("name"):
        local_store.add_member(user["name"], "Owner")
    local_store.switch_grant(gid)
    local_store.add_audit(
        gid, {"kind": "grant", "by": who(request).get("name"), "vendor": grant["name"], "reason": "Grant created"}
    )
    return {"id": gid, "grant": grant}


@app.patch("/api/grants/{grant_id}", tags=["grants"])
async def update_grant(grant_id: str, req: GrantPatch, request: Request) -> dict[str, Any]:
    current = local_store.get_grant(grant_id)
    if not current:
        raise HTTPException(404, f"Grant '{grant_id}' not found")
    if (
        req.currency
        and req.currency.upper() != current.get("currency", "").upper()
        and any(m.get("grant_id") == grant_id for m in local_store.load_store()["memories"])
    ):
        raise HTTPException(
            409, "This grant already has expenses in its currency. Currency can only change while the ledger is empty."
        )
    merged = {**current, **req.model_dump(exclude_none=True)}
    if req.total_funding is not None and req.policies is None:
        # Keep the "total award" cap in step when the award amount changes.
        for pol in merged.get("policies", []):
            if (
                pol.get("type") == "category_cap"
                and not pol.get("category")
                and pol.get("cap") == current.get("total_funding")
            ):
                pol["cap"] = req.total_funding
    for k in ("start_date", "end_date"):
        if isinstance(merged.get(k), str):
            merged[k] = date.fromisoformat(merged[k])
    merged.setdefault("home_country", "US")
    merged.setdefault("currency", "USD")
    grant = local_store.save_grant(grant_id, _clean_grant(merged))
    changed = ", ".join(req.model_dump(exclude_none=True)) or "nothing"
    local_store.add_audit(
        grant_id,
        {
            "kind": "grant",
            "by": who(request).get("name"),
            "vendor": grant["name"],
            "reason": f"Grant updated ({changed})",
        },
    )
    return {"id": grant_id, "grant": grant}


@app.delete("/api/grants/{grant_id}", tags=["grants"])
async def delete_grant(grant_id: str) -> dict[str, str]:
    """Archive a grant. Its ledger and history stay in the database."""
    if len(local_store.load_store()["grants"]) <= 1:
        raise HTTPException(409, "You need at least one grant. Create another one first.")
    if not local_store.delete_grant(grant_id):
        raise HTTPException(404, f"Grant '{grant_id}' not found")
    return {"status": "archived"}


@app.post("/api/grants/extract-file", tags=["grants"])
async def extract_grant_file(request: Request, filename: str = Query("", max_length=255)) -> dict[str, Any]:
    """Upload a PDF, Word (.docx) or text file as the raw request body. Returns the same draft as /extract."""
    body = await request.body()
    try:
        doc = documents.extract_text(filename, body)
    except documents.DocumentError as exc:
        raise HTTPException(422, str(exc)) from exc
    out = await grant_import.extract(doc.text)
    out["file"] = {"name": filename, "kind": doc.kind, "pages": doc.pages, "chars": len(doc.text)}
    out["text"] = doc.text[: grant_import.MAX_CHARS]
    return out


@app.post("/api/grants/extract", tags=["grants"])
async def extract_grant(req: ExtractRequest) -> dict[str, Any]:
    """Draft grant details and rules from pasted agreement text. Nothing is saved."""
    return await grant_import.extract(req.text)


@app.post("/api/reset-seed", tags=["grants"])
async def reset_seed() -> dict[str, Any]:
    fresh = local_store.reset_seed()
    if hindsight.get_client().enabled:
        asyncio.create_task(_background_sync())
    return {"status": "reset_successful", "state": fresh}


# --------------------------------------------------------------------------- #
# Audits
# --------------------------------------------------------------------------- #


def _resolve(req: AuditExpenseRequest) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    g, store = _grant(req.grant_id)
    grant = {"id": req.grant_id, **g}
    team = {m["id"]: m for m in store.get("team_members", [])}
    spender = team.get(req.spender_id, {"id": req.spender_id, "name": req.spender_id, "role": "Team Contributor"})
    ledger = [m for m in store.get("memories", []) if m.get("grant_id") == req.grant_id]
    return grant, spender, ledger


async def _to_grant_currency(
    amount: float, currency: str | None, grant: dict[str, Any], day: Any, manual_rate: float | None
) -> tuple[float, dict[str, Any] | None]:
    """Convert an amount into the grant's currency. Returns (converted, fx details or None)."""
    gcur = (grant.get("currency") or "USD").upper()
    cur = (currency or gcur).upper()
    if cur == gcur:
        return round(amount, 2), None
    d = engine.parse_ts(day).date() if day else None
    try:
        converted, q = await fx.convert(amount, cur, gcur, d, manual_rate)
    except fx.FxError as exc:
        raise HTTPException(503, str(exc)) from exc
    return converted, {
        "original_amount": round(amount, 2),
        "original_currency": cur,
        "fx_rate": q.rate,
        "fx_rate_date": q.rate_date,
        "fx_source": q.source,
        "converted_amount": converted,
        "grant_currency": gcur,
    }


async def _expense(
    req: AuditExpenseRequest, spender: dict[str, Any], grant: dict[str, Any]
) -> tuple[engine.Expense, dict[str, Any] | None]:
    amount, fxinfo = await _to_grant_currency(req.amount, req.currency, grant, req.expense_date, req.fx_rate)
    return compliance_engine.build_expense(
        grant_id=req.grant_id,
        spender_id=req.spender_id,
        spender_name=spender.get("name", req.spender_id),
        vendor=req.vendor,
        location=req.location,
        amount=amount,
        purpose=req.purpose,
        category=req.category,
        expense_date=req.expense_date,
        prior_approval_ref=req.prior_approval_ref,
    ), fxinfo


async def _recall(req: AuditExpenseRequest) -> list[dict[str, Any]]:
    return await memory_sync.recall_memories(
        query=f"{req.vendor} {req.location} {req.purpose} {req.category or ''}", top_k=8, grant_id=req.grant_id
    )


def _history_entry(
    kind: str, req: AuditExpenseRequest, spender: dict[str, Any], ev: dict[str, Any], user: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    return {
        "kind": kind,
        "by": user.get("name"),
        "spender": spender.get("name"),
        "vendor": req.vendor,
        "location": req.location,
        "amount": ev.get("amount", req.amount),
        "fx": ev.get("fx"),
        "category": ev["category"],
        "expense_date": ev["expense_date"],
        "status": ev["status"],
        "risk_score": ev["risk_score"],
        "findings": [
            {"clause": f["clause"], "severity": f["severity"], "message": f["message"]} for f in ev["findings"]
        ],
        "engine_version": ev["engine_version"],
        **extra,
    }


@app.post("/api/audit-expense", tags=["audits"])
async def audit_expense(req: AuditExpenseRequest, request: Request) -> dict[str, Any]:
    """Check an expense. If nothing blocks it (and commit=true, with_memory) it is recorded in the ledger."""
    grant, spender, ledger = _resolve(req)
    expense, fxinfo = await _expense(req, spender, grant)
    recalled = await _recall(req) if req.mode == "with_memory" else []
    decision, result = await compliance_engine.assess(
        grant=grant, expense=expense, ledger=ledger, mode=req.mode, recalled=recalled
    )

    result["amount"], result["fx"] = expense.amount, fxinfo
    committed: dict[str, Any] | None = None
    if req.mode == "with_memory" and req.commit and not decision.blocking:
        paid = (
            f"{fxinfo['original_currency']} {fxinfo['original_amount']:,.2f} (= {fxinfo['grant_currency']} "
            f"{expense.amount:,.2f})"
            if fxinfo
            else f"{(grant.get('currency') or 'USD')} {expense.amount:,.2f}"
        )
        out = await memory_sync.record_memory(
            grant_id=req.grant_id,
            spender=spender.get("name", "Team Member"),
            spender_id=req.spender_id,
            category=expense.category,
            amount=expense.amount,
            vendor=req.vendor,
            location=req.location,
            content=f"{spender.get('name')} paid {req.vendor} {paid} for {req.purpose} "
            f"({expense.category}, {req.location or 'location not given'}).",
            timestamp=expense.expense_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            fx=fxinfo,
        )
        committed = out["record"]
    result["committed"] = committed is not None
    result["memory_id"] = committed["id"] if committed else None
    result["sync_status"] = committed.get("sync_status") if committed else None
    result["recalled"] = recalled[:5]
    await _learn_from_check(grant, spender, req, expense, decision, committed)
    local_store.add_audit(
        req.grant_id,
        _history_entry(
            "check", req, spender, result, who(request), committed=result["committed"], memory_id=result["memory_id"]
        ),
    )
    return {
        "evaluation": result,
        "expense": {
            "grant_id": req.grant_id,
            "spender": spender,
            "vendor": req.vendor,
            "location": req.location,
            "amount": expense.amount,
            "fx": fxinfo,
            "purpose": req.purpose,
            "mode": req.mode,
            "category": expense.category,
            "expense_date": expense.expense_date.isoformat(),
        },
        "recalled_count": len(recalled),
    }


async def _learn_from_check(grant: dict[str, Any], spender: dict[str, Any], req: AuditExpenseRequest,
                            expense: engine.Expense, decision: engine.Decision, committed: dict[str, Any] | None) -> None:
    """Turn what just happened into experience Hindsight can use next time."""
    cur = (grant.get("currency") or "USD").upper()
    who_ = spender.get("name", "someone")
    base = dict(vendor=req.vendor, spender_id=req.spender_id, spender=who_, category=expense.category)
    if decision.blocking:
        f = decision.blocking[0]
        await learning.remember(
            "decision", req.grant_id,
            f"GrantAnchor stopped a {cur} {expense.amount:,.2f} {expense.category} payment to {req.vendor} for "
            f"{who_} dated {expense.expense_date.date().isoformat()}. {f.message}",
            clause=f.clause, code=f.code, **base,
        )
    elif committed and req.prior_approval_ref:
        waived = next((f for f in decision.findings if f.code == "JURISDICTION_WAIVED"), None)
        await learning.remember(
            "approval", req.grant_id,
            f"{req.vendor} ({req.location or 'location not given'}): a {cur} {expense.amount:,.2f} "
            f"{expense.category} payment was allowed under funder approval {req.prior_approval_ref}"
            + (f" ({waived.clause})." if waived else "."),
            approval_ref=req.prior_approval_ref, clause=waived.clause if waived else "", **base,
        )


async def _compare(req: AuditExpenseRequest, *, advisory: bool) -> dict[str, Any]:
    grant, spender, ledger = _resolve(req)
    expense, fxinfo = await _expense(req, spender, grant)
    recalled = await _recall(req) if advisory else []
    _, with_mem = await compliance_engine.assess(
        grant=grant, expense=expense, ledger=ledger, mode="with_memory", recalled=recalled, use_advisory=advisory
    )
    _, without_mem = await compliance_engine.assess(
        grant=grant, expense=expense, ledger=ledger, mode="without_memory", use_advisory=False
    )
    for ev in (with_mem, without_mem):
        ev["amount"], ev["fx"] = expense.amount, fxinfo
    d = compliance_engine.diff(with_mem, without_mem)
    # Which past ledger entries drove the extra findings: the evidence memory supplied.
    rules_hit = {f["rule_id"] for f in d["caught_by_memory"]} or {
        u["rule_id"] for u in sorted(d["utilization_delta"], key=lambda u: -u["with_memory_projected"] / u["cap"])[:1]
    }
    evidence = _evidence(grant, expense, ledger, rules_hit)
    return {
        "with_memory": with_mem,
        "without_memory": without_mem,
        "diff": d,
        "evidence": evidence,
        "recalled": recalled[:5],
        "recalled_count": len(recalled),
        "spender": spender,
    }


def _evidence(
    grant: dict[str, Any], expense: engine.Expense, ledger: list[dict[str, Any]], rule_ids: set[str]
) -> list[dict[str, Any]]:
    rules = {r.id: r for r in compliance_engine.grant_rules(grant)}
    out: list[dict[str, Any]] = []
    for rid in rule_ids:
        r = rules.get(rid)
        cat = getattr(r, "category", None)
        for m in ledger:
            if cat and engine.normalize_category(m.get("category")) != cat:
                continue
            if r is not None and r.type == "vendor_cap" and m.get("vendor", "").casefold() != expense.vendor.casefold():
                continue
            if r is not None and r.type == "spender_cap" and m.get("spender_id") not in (None, expense.spender_id):
                continue
            if m not in out:
                out.append(m)
    return sorted(out, key=lambda m: m.get("timestamp", ""), reverse=True)[:8]


@app.post("/api/expenses/audit/compare", tags=["audits"])
async def compare_audit(req: AuditExpenseRequest, request: Request) -> dict[str, Any]:
    """Run the same expense with and without memory, side by side. Never commits."""
    out = await _compare(req, advisory=True)
    local_store.add_audit(
        req.grant_id,
        _history_entry(
            "compare",
            req,
            out["spender"],
            out["with_memory"],
            who(request),
            without_memory_status=out["without_memory"]["status"],
        ),
    )
    return out


@app.post("/api/expenses/preview", tags=["audits"])
async def preview(req: AuditExpenseRequest) -> dict[str, Any]:
    """Fast dry run for live feedback while typing: deterministic only, no LLM, no Hindsight call, no commit."""
    return await _compare(req, advisory=False)


@app.get("/api/audits", tags=["audits"])
async def audit_history(grant_id: str, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return {"items": local_store.list_audits(grant_id, limit)}


# --------------------------------------------------------------------------- #
# Ledger / memory
# --------------------------------------------------------------------------- #


@app.get("/api/memories", tags=["ledger"])
async def list_memories(
    grant_id: str,
    q: str = "",
    category: str = "",
    sync_status: str = "",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return local_store.list_memories(
        grant_id, q=q, category=category, sync_status=sync_status, limit=limit, offset=offset
    )


@app.patch("/api/memories/{memory_id}", tags=["ledger"])
async def edit_memory(memory_id: str, req: MemoryPatch, request: Request) -> Any:
    """Correct a ledger entry. The edited entry is re-checked against the grant rules (excluding itself).

    If it would break a blocking rule, the API answers 409 with the evaluation unless ``confirm`` is true.
    Every edit is kept in the entry's ``edits`` list and in the check history, and re-synced to Hindsight.
    """
    m = local_store.get_memory(memory_id)
    if m is None:
        raise HTTPException(404, "Entry not found")
    g, store = _grant(m["grant_id"])
    team = {t["id"]: t for t in store["team_members"]}
    changes: dict[str, Any] = {}
    gcur = (g.get("currency") or "USD").upper()
    prev_fx = m.get("fx") or {}
    cur = (req.currency or prev_fx.get("original_currency") or gcur).upper()
    if (
        req.amount is not None
        or req.currency is not None
        or req.fx_rate is not None
        or (req.expense_date is not None and cur != gcur)
    ):
        orig = req.amount if req.amount is not None else prev_fx.get("original_amount", m.get("amount"))
        day = req.expense_date or engine.parse_ts(m.get("timestamp")).date()
        if (
            req.fx_rate is None
            and cur == prev_fx.get("original_currency")
            and prev_fx.get("fx_source") == "manual"
            and req.currency is None
        ):
            manual = prev_fx.get("fx_rate")
        else:
            manual = req.fx_rate
        amount, fxinfo = await _to_grant_currency(float(orig), cur, g, day, manual)
        changes["amount"] = amount
        changes["fx"] = fxinfo
    if req.vendor is not None:
        changes["vendor"] = req.vendor.strip()
    if req.location is not None:
        changes["location"] = req.location.strip()
    if req.category is not None:
        changes["category"] = engine.normalize_category(req.category)
    if req.expense_date is not None:
        changes["timestamp"] = f"{req.expense_date.isoformat()}T12:00:00Z"
    if req.content is not None:
        changes["content"] = req.content.strip()
    if req.spender_id is not None:
        if req.spender_id not in team:
            raise HTTPException(422, "Unknown team member")
        changes["spender_id"] = req.spender_id
        changes["spender"] = team[req.spender_id]["name"]
    diff = {k: [m.get(k), v] for k, v in changes.items() if m.get(k) != v}
    if not diff:
        return {"memory": m, "evaluation": None, "changed": []}

    new = {**m, **changes}
    expense = compliance_engine.build_expense(
        grant_id=m["grant_id"],
        spender_id=new.get("spender_id") or new.get("spender", ""),
        spender_name=new.get("spender", ""),
        vendor=new.get("vendor", ""),
        location=new.get("location", ""),
        amount=float(new["amount"]),
        purpose=new.get("content", ""),
        category=new.get("category"),
        expense_date=new.get("timestamp"),
    )
    ledger = [x for x in store["memories"] if x.get("grant_id") == m["grant_id"] and x["id"] != memory_id]
    decision = compliance_engine.decide({"id": m["grant_id"], **g}, expense, ledger, "with_memory")
    evaluation = compliance_engine.render(decision, expense, "with_memory", None)
    if decision.blocking and not req.confirm:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "This change would break a grant rule. Confirm to save it anyway.",
                "evaluation": evaluation,
            },
        )

    user = who(request).get("name", "")
    edits = list(m.get("edits", [])) + [{"at": clock.now_iso(), "by": user, "reason": req.reason, "changes": diff}]
    updated = local_store.update_memory(memory_id, {**changes, "edits": edits, "sync_status": "pending"})
    assert updated is not None
    await memory_sync.sync_record(updated)
    old_amt, new_amt = float(m.get("amount") or 0), float(updated.get("amount") or 0)
    if "amount" in diff and old_amt > 0 and new_amt > old_amt:
        pct = (new_amt - old_amt) / old_amt * 100
        cur = (g.get("currency") or "USD").upper()
        await learning.remember(
            "overrun", m["grant_id"],
            f"{updated.get('spender')}'s {updated.get('category')} expense with {updated.get('vendor')} was checked "
            f"at {cur} {old_amt:,.2f} but the final amount was {cur} {new_amt:,.2f}, {pct:.0f}% more. "
            f"Reason given: {req.reason}.",
            vendor=updated.get("vendor", ""), spender_id=updated.get("spender_id") or "",
            spender=updated.get("spender", ""), overrun_pct=round(pct, 1), category=updated.get("category", ""),
        )
    local_store.add_audit(
        m["grant_id"],
        {
            "kind": "edit",
            "by": user,
            "vendor": updated.get("vendor"),
            "amount": updated.get("amount"),
            "memory_id": memory_id,
            "reason": req.reason,
            "changes": diff,
            "status": evaluation["status"],
            "risk_score": evaluation["risk_score"],
            "findings": [
                {"clause": f["clause"], "severity": f["severity"], "message": f["message"]}
                for f in evaluation["findings"]
            ],
        },
    )
    return {"memory": local_store.get_memory(memory_id), "evaluation": evaluation, "changed": list(diff)}


@app.delete("/api/memories/{memory_id}", tags=["ledger"])
async def delete_memory(memory_id: str, request: Request, req: DeleteRequest | None = None) -> dict[str, Any]:
    """Soft delete a ledger entry (kept for audit, removed from cap math) and forget it in Hindsight."""
    reason = req.reason if req else "Correction"
    m = local_store.delete_memory(memory_id, by=who(request).get("name", ""), reason=reason)
    if m is None:
        raise HTTPException(404, "Entry not found")
    await memory_sync.forget(memory_id)
    local_store.add_audit(
        m["grant_id"],
        {
            "kind": "delete",
            "by": who(request).get("name"),
            "vendor": m.get("vendor"),
            "amount": m.get("amount"),
            "memory_id": memory_id,
            "reason": reason,
        },
    )
    return {"status": "deleted", "id": memory_id}


@app.post("/api/memories/{memory_id}/resync", tags=["ledger"])
async def resync_memory(memory_id: str) -> dict[str, Any]:
    m = local_store.get_memory(memory_id)
    if m is None:
        raise HTTPException(404, "Entry not found")
    hindsight.get_client().reset_circuit()
    await memory_sync.sync_record(m)
    return {"memory": local_store.get_memory(memory_id)}


@app.post("/api/memories/sync", tags=["ledger"])
async def sync_all(grant_id: str | None = None) -> dict[str, Any]:
    """Push every unsynced ledger entry to Hindsight."""
    hindsight.get_client().reset_circuit()
    return {"result": await memory_sync.sync_pending(grant_id, limit=200)}


class BriefingRequest(BaseModel):
    grant_id: str
    vendor: str = Field(default="", max_length=200)
    spender_id: str = Field(default="", max_length=64)
    category: str = Field(default="", max_length=60)


@app.post("/api/memory/briefing", tags=["memory"])
async def memory_briefing(req: BriefingRequest) -> dict[str, Any]:
    """What GrantAnchor remembers about this vendor, person and category: past stops, a funder approval
    reference to reuse, and the person's typical overrun. Advisory only."""
    _, store = _grant(req.grant_id)
    person = next((t for t in store["team_members"] if t["id"] == req.spender_id), {})
    return await learning.briefing(req.grant_id, vendor=req.vendor.strip(), spender_id=req.spender_id,
                                   spender=person.get("name", ""), category=req.category)


@app.get("/api/memory/lessons", tags=["memory"])
async def memory_lessons(grant_id: str, refresh: bool = False) -> dict[str, Any]:
    """Lessons learned on this grant, written by Hindsight reflect from everything it remembers."""
    g, _ = _grant(grant_id)
    return await learning.lessons(grant_id, g.get("name", grant_id), refresh=refresh)


@app.get("/api/memory/stats", tags=["memory"])
async def memory_stats(grant_id: str) -> dict[str, Any]:
    _grant(grant_id)
    return learning.stats(grant_id)


@app.get("/api/memory/recall", tags=["ledger"])
async def recall(grant_id: str, q: str = Query(..., min_length=1, max_length=300)) -> dict[str, Any]:
    """Ask memory a question. Uses Hindsight semantic recall; falls back to local keyword search."""
    items = await memory_sync.recall_memories(query=q, top_k=10, grant_id=grant_id)
    source = "hindsight" if any(i.get("source") == "hindsight" for i in items) else "local"
    return {"items": items, "source": source}


@app.get("/api/ledger.csv", tags=["ledger"])
async def ledger_csv(grant_id: str) -> Response:
    rows = local_store.list_memories(grant_id, limit=100000)["items"]
    buf = io.StringIO()
    cols = ["id", "timestamp", "spender", "category", "amount", "vendor", "location", "content", "sync_status"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="ledger_{grant_id}.csv"'},
    )


# --------------------------------------------------------------------------- #
# Team
# --------------------------------------------------------------------------- #


@app.post("/api/team-members", tags=["team"])
async def add_member(req: MemberRequest) -> dict[str, Any]:
    return {"member": local_store.add_member(req.name.strip(), req.role.strip() or "Team member")}


@app.patch("/api/team-members/{member_id}", tags=["team"])
async def edit_member(member_id: str, req: MemberPatch) -> dict[str, Any]:
    """Rename or change role. A rename also updates the name on that person's ledger entries."""
    fields = {k: v.strip() for k, v in req.model_dump(exclude_none=True).items()}
    m = local_store.update_member(member_id, fields)
    if m is None:
        raise HTTPException(404, "Member not found")
    return {"member": m}


@app.delete("/api/team-members/{member_id}", tags=["team"])
async def remove_member(member_id: str) -> dict[str, str]:
    if not local_store.delete_member(member_id):
        raise HTTPException(404, "Member not found")
    return {"status": "removed"}

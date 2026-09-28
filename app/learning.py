"""What GrantAnchor learns over time, stored in Hindsight.

Every recorded expense is already retained (see memory_sync). This module adds
*experience*: things that happened and should change future advice.

* ``decision``  a check that was stopped, and why
* ``approval``  a payment allowed under a funder approval reference (reusable next time)
* ``overrun``   an expense corrected upward after the fact (quote vs. final invoice)

Each event is written to the local ``memory_events`` table and retained to
Hindsight with tags ``grant:``, ``kind:``, ``vendor:`` and ``spender:``.

``briefing()`` recalls what Hindsight remembers about the vendor, person and
category being checked, and turns it into concrete hints: an approval
reference to reuse, and the person's typical overrun. ``lessons()`` asks
Hindsight's ``reflect`` to summarise what the team has learned.

None of this changes a verdict. The engine decides; memory advises.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from statistics import mean
from typing import Any, Literal

from app import clock, hindsight, local_store
from app.rules import normalize_vendor

logger = logging.getLogger("grantanchor.learning")

Kind = Literal["decision", "approval", "overrun"]
_LESSONS_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}
LESSONS_TTL = 600.0

LESSONS_SCHEMA = {
    "type": "object",
    "properties": {
        "lessons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"lesson": {"type": "string"}, "evidence": {"type": "string"}},
                "required": ["lesson"],
            },
        }
    },
    "required": ["lessons"],
}


# --------------------------------------------------------------------------- #
# Recording events
# --------------------------------------------------------------------------- #


def _tags(grant_id: str, kind: str, vendor: str, spender_id: str) -> list[str]:
    tags = ["grantanchor", f"grant:{grant_id}", f"kind:{kind}"]
    if vendor:
        tags.append(f"vendor:{normalize_vendor(vendor)}")
    if spender_id:
        tags.append(f"spender:{spender_id}")
    return tags


async def remember(kind: Kind, grant_id: str, text: str, *, vendor: str = "", spender_id: str = "",
                   spender: str = "", **meta: Any) -> dict[str, Any]:
    """Save an experience locally and retain it in Hindsight (best effort, never blocks the caller)."""
    local_store.load_store()  # make sure first-run seeding has happened before we write
    event = {
        "id": f"evt_{uuid.uuid4().hex[:10]}", "grant_id": grant_id, "kind": kind, "text": text,
        "vendor": vendor, "vendor_key": normalize_vendor(vendor), "spender_id": spender_id, "spender": spender,
        "created_at": clock.now_iso(), "sync_status": "pending", **meta,
    }
    client = hindsight.get_client()
    if client.enabled:
        item = hindsight.MemoryItem(
            content=text,
            timestamp=event["created_at"],
            context=f"grant-experience:{grant_id}",
            metadata={k: str(v) for k, v in {**meta, "kind": kind, "event_id": event["id"], "vendor": vendor,
                                              "spender": spender, "spender_id": spender_id}.items() if v is not None},
            document_id=event["id"],
            tags=_tags(grant_id, kind, vendor, spender_id),
        )
        try:
            await client.retain([item], async_=True)
            event["sync_status"] = "synced"
        except hindsight.HindsightError as exc:
            event["sync_status"] = "failed"
            logger.warning("could not retain %s event: %s", kind, exc)
    else:
        event["sync_status"] = "local_only"
    local_store.conn().execute(
        "INSERT INTO memory_events(id, grant_id, kind, created_at, data) VALUES(?, ?, ?, ?, ?)",
        (event["id"], grant_id, kind, event["created_at"], json.dumps(event)),
    )
    _LESSONS_CACHE.pop(grant_id, None)
    return event


def events(grant_id: str) -> list[dict[str, Any]]:
    return [json.loads(r["data"]) for r in local_store.conn().execute(
        "SELECT data FROM memory_events WHERE grant_id=? ORDER BY created_at DESC", (grant_id,))]


def stats(grant_id: str) -> dict[str, Any]:
    evs = events(grant_id)
    ledger = [m for m in local_store.load_store()["memories"] if m.get("grant_id") == grant_id]
    n = len(evs) + len(ledger)
    level = ("Just started" if n < 3 else "Getting to know this grant" if n < 10
             else "Knows your patterns" if n < 25 else "Knows this grant well")
    return {"events": len(evs), "expenses": len(ledger), "total": n, "level": level,
            "by_kind": {k: sum(e["kind"] == k for e in evs) for k in ("decision", "approval", "overrun")},
            "in_hindsight": sum(e.get("sync_status") == "synced" for e in evs)
            + sum(m.get("sync_status") == "synced" for m in ledger)}


# --------------------------------------------------------------------------- #
# Using what was learned
# --------------------------------------------------------------------------- #


def _hints(evs: list[dict[str, Any]], vendor: str, spender_id: str) -> dict[str, Any]:
    vkey = normalize_vendor(vendor) if vendor else ""
    approvals = [e for e in evs if e["kind"] == "approval" and vkey and e.get("vendor_key") == vkey
                 and e.get("approval_ref")]
    overruns = [e for e in evs if e["kind"] == "overrun" and spender_id and e.get("spender_id") == spender_id
                and float(e.get("overrun_pct", 0)) > 0]
    stops = [e for e in evs if e["kind"] == "decision" and (
        (vkey and e.get("vendor_key") == vkey) or (spender_id and e.get("spender_id") == spender_id))]
    out: dict[str, Any] = {"suggested_approval": None, "overrun": None, "past_stops": len(stops)}
    if approvals:
        a = max(approvals, key=lambda e: e["created_at"])
        out["suggested_approval"] = {"ref": a["approval_ref"], "vendor": a["vendor"], "when": a["created_at"],
                                     "clause": a.get("clause", "")}
    if overruns:
        pcts = [float(e["overrun_pct"]) for e in overruns]
        out["overrun"] = {"spender": overruns[0].get("spender", ""), "avg_pct": round(mean(pcts), 1),
                          "times": len(pcts), "max_pct": round(max(pcts), 1)}
    return out


async def briefing(grant_id: str, *, vendor: str = "", spender_id: str = "", spender: str = "",
                   category: str = "") -> dict[str, Any]:
    """What memory says about this vendor/person/category. Hindsight first, local events as fallback."""
    local = events(grant_id)
    known = {e["id"]: e for e in local}
    ledger = {m["id"]: m for m in local_store.load_store()["memories"] if m.get("grant_id") == grant_id}
    client = hindsight.get_client()
    source = "local"
    recalled: list[dict[str, Any]] = []
    matched: list[dict[str, Any]] = []
    if client.enabled and not client.circuit_open and (vendor or spender or category):
        query = " ".join(x for x in [vendor, spender, category] if x) + " approvals overruns stopped payments"
        try:
            results = await client.recall(query, tags=[f"grant:{grant_id}"])
            source = "hindsight"
        except hindsight.HindsightError as exc:
            logger.info("briefing recall unavailable: %s", exc)
            results = []
        seen: set[str] = set()
        for r in results:
            meta = r.metadata or {}
            eid = meta.get("event_id") or r.document_id or ""
            if eid in known and eid not in seen:  # an experience that still exists
                seen.add(eid)
                matched.append(known[eid])
                recalled.append({"kind": known[eid]["kind"], "text": r.text, "when": known[eid]["created_at"]})
            elif (meta.get("memory_id") or r.document_id) in ledger and eid not in seen:  # a past expense
                seen.add(eid)
                m = ledger[meta.get("memory_id") or r.document_id]
                recalled.append({"kind": "expense", "text": r.text, "when": m.get("timestamp")})
    vkey = normalize_vendor(vendor) if vendor else ""
    local_hits = [e for e in local if (vkey and e.get("vendor_key") == vkey) or
                  (spender_id and e.get("spender_id") == spender_id)]
    if not matched and local_hits:
        # Hindsight off, down, or still indexing a just-recorded event: show the local copy.
        source = "local"
        recalled = [{"kind": e["kind"], "text": e["text"], "when": e["created_at"]} for e in local_hits] + recalled
    # Hindsight chooses what to show; the structured hints use every experience with this exact vendor/person.
    ids = {e["id"] for e in matched}
    hints = _hints(matched + [e for e in local_hits if e["id"] not in ids], vendor, spender_id)
    return {"source": source, "memories": recalled[:5], **hints, "stats": stats(grant_id)}


async def lessons(grant_id: str, grant_name: str, refresh: bool = False) -> dict[str, Any]:
    """Hindsight reflect: short lessons the team has learned on this grant. Cached until new events arrive."""
    st = stats(grant_id)
    cached = _LESSONS_CACHE.get(grant_id)
    if cached and not refresh and time.time() - cached[0] < LESSONS_TTL and cached[1] == st["total"]:
        return {**cached[2], "cached": True}
    client = hindsight.get_client()
    if not client.enabled:
        return {"status": "disabled", "lessons": [], "stats": st}
    if st["total"] == 0:
        return {"status": "empty", "lessons": [], "stats": st}
    try:
        r = await client.reflect(
            f"What has this team learned so far about spending on the grant '{grant_name}'? Give at most 4 short, "
            "practical lessons for the next person about to spend money: limits that keep getting close, people "
            "whose final invoices run over, vendors that needed funder approval and the reference used. "
            "Only use what is in memory.",
            tags=[f"grant:{grant_id}"], response_schema=LESSONS_SCHEMA,
        )
    except hindsight.HindsightError as exc:
        return {"status": "unavailable", "error": str(exc)[:200], "lessons": [], "stats": st}
    items = []
    for x in (r.structured_output or {}).get("lessons", [])[:4]:
        if isinstance(x, dict) and str(x.get("lesson", "")).strip():
            items.append({"lesson": str(x["lesson"])[:400], "evidence": str(x.get("evidence", ""))[:400]})
    out = {"status": "ok" if items else "invalid", "lessons": items, "summary": r.text[:1500] if not items else "",
           "stats": st, "generated_at": clock.now_iso()}
    _LESSONS_CACHE[grant_id] = (time.time(), st["total"], out)
    return out

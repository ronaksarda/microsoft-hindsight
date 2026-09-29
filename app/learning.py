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
    await _push(event)
    local_store.conn().execute(
        "INSERT INTO memory_events(id, grant_id, kind, created_at, data) VALUES(?, ?, ?, ?, ?)",
        (event["id"], grant_id, kind, event["created_at"], json.dumps(event)),
    )
    _LESSONS_CACHE.pop(grant_id, None)
    return event


def _item(event: dict[str, Any]) -> hindsight.MemoryItem:
    meta = {k: v for k, v in event.items() if k not in ("text", "created_at", "sync_status", "vendor_key", "grant_id",
                                                         "id", "sync_attempts", "next_attempt_at", "sync_gave_up")}
    return hindsight.MemoryItem(
        content=event["text"],
        timestamp=event["created_at"],
        context=f"grant-experience:{event['grant_id']}",
        metadata={k: str(v) for k, v in {**meta, "event_id": event["id"]}.items() if v is not None},
        document_id=event["id"],
        tags=_tags(event["grant_id"], event["kind"], event.get("vendor", ""), event.get("spender_id", "")),
    )


async def _push(event: dict[str, Any]) -> None:
    from app.memory_sync import next_attempt

    client = hindsight.get_client()
    if not client.enabled:
        event["sync_status"] = "local_only"
        return
    try:
        await client.retain([_item(event)], async_=True)
        event.update(sync_status="synced", sync_attempts=0, next_attempt_at=None)
    except hindsight.HindsightError as exc:
        event["sync_status"] = "failed"
        event.update(next_attempt(int(event.get("sync_attempts", 0)), exc.retryable))
        logger.warning("could not retain %s event: %s", event["kind"], exc)


async def sync_pending_events(limit: int = 100) -> dict[str, int]:
    """Outbox pass for experiences that haven't reached Hindsight yet."""
    from app.memory_sync import due

    rows = [json.loads(r["data"]) for r in local_store.conn().execute("SELECT data FROM memory_events")]
    counts = {"synced": 0, "failed": 0}
    if not hindsight.get_client().enabled:
        return counts
    from app.memory_sync import BATCH, next_attempt

    todo = [e for e in rows if due(e)][:limit]
    for i in range(0, len(todo), BATCH):
        chunk = todo[i : i + BATCH]
        try:
            await hindsight.get_client().retain([_item(e) for e in chunk], async_=True)
            for e in chunk:
                e.update(sync_status="synced", sync_attempts=0, next_attempt_at=None)
            counts["synced"] += len(chunk)
        except hindsight.HindsightError as exc:
            for e in todo[i:]:
                e["sync_status"] = "failed"
                e.update(next_attempt(int(e.get("sync_attempts", 0)), exc.retryable))
            counts["failed"] += len(todo) - i
            chunk = todo[i:]
        for e in chunk:
            local_store.conn().execute("UPDATE memory_events SET data=? WHERE id=?", (json.dumps(e), e["id"]))
        if counts["failed"]:
            break
    return counts


def events(grant_id: str) -> list[dict[str, Any]]:
    return [json.loads(r["data"]) for r in local_store.conn().execute(
        "SELECT data FROM memory_events WHERE grant_id=? ORDER BY created_at DESC", (grant_id,))]


def impact_of(evaluation: dict[str, Any], approval_ref: str | None) -> dict[str, Any]:
    """What memory contributed to one check, stored in history so the impact can be counted."""
    mem = [f for f in evaluation.get("findings", []) if f.get("source") == "memory"]
    hints = (evaluation.get("memory") or {}).get("hints") or {}
    sug = (hints.get("suggested_approval") or {}).get("ref") or ""
    extra = sum(f["details"].get("estimated_payment", 0) - f["details"].get("checked_payment", 0)
                for f in mem if f["code"] == "LEARNED_OVERRUN")
    return {
        "codes": [f["code"] for f in mem],
        "changed_answer": bool(evaluation.get("status_changed_by_memory")),
        "approval_reused": bool(sug and approval_ref and approval_ref.strip().lower() == sug.lower()),
        "overrun_flagged": round(extra, 2),
    }


def impact(grant_id: str) -> dict[str, Any]:
    """Totals across the check history: how often memory changed or improved an answer."""
    rows = [a.get("memory_impact") or {} for a in local_store.list_audits(grant_id, 5000)]
    return {
        "checks_helped": sum(bool(r.get("codes") or r.get("approval_reused")) for r in rows),
        "answers_changed": sum(bool(r.get("changed_answer")) for r in rows),
        "approvals_reused": sum(bool(r.get("approval_reused")) for r in rows),
        "overrun_flagged": round(sum(float(r.get("overrun_flagged") or 0) for r in rows), 2),
        "checks": len(rows),
    }


def stats(grant_id: str) -> dict[str, Any]:
    evs = events(grant_id)
    ledger = [m for m in local_store.load_store()["memories"] if m.get("grant_id") == grant_id]
    n = len(evs) + len(ledger)
    level = ("Just started" if n < 3 else "Getting to know this grant" if n < 10
             else "Knows your patterns" if n < 25 else "Knows this grant well")
    months: dict[str, list[int]] = {}
    for e in evs:
        months.setdefault(e["created_at"][:7], [0, 0])[0] += 1
    for m in ledger:
        months.setdefault(str(m.get("timestamp", ""))[:7], [0, 0])[1] += 1
    run = 0
    timeline = []
    for mo in sorted(k for k in months if k):
        run += sum(months[mo])
        timeline.append({"month": mo, "experiences": months[mo][0], "expenses": months[mo][1], "total": run})
    return {"events": len(evs), "expenses": len(ledger), "total": n, "level": level, "timeline": timeline,
            "impact": impact(grant_id),
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
            "Name the people, vendors and approval references involved. Only use what is in memory.",
            # Only this grant's experiences (stops, approvals, overruns), not every expense.
            tag_groups=[{"tags": [f"grant:{grant_id}"], "match": "all_strict"},
                        {"tags": ["kind:decision", "kind:approval", "kind:overrun"], "match": "any_strict"}],
            response_schema=LESSONS_SCHEMA,
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


# --------------------------------------------------------------------------- #
# Memory in the verdict
# --------------------------------------------------------------------------- #


def apply_memory(evaluation: dict[str, Any], grant_id: str, *, vendor: str, spender_id: str,
                 has_approval_ref: bool, currency: str = "USD") -> dict[str, Any]:
    """Add memory findings to a with-memory evaluation.

    Memory can raise caution (APPROVED -> APPROVED_WITH_WARNINGS) but never blocks or approves: the rules
    still own the hard decision. Findings are tagged ``source: memory`` so the UI and the with/without-memory
    diff can show exactly what experience changed.
    """
    evs = events(grant_id)
    hints = _hints(evs, vendor, spender_id)
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}.get(currency.upper(), currency.upper() + " ")
    found: list[dict[str, Any]] = []
    o = hints["overrun"]
    if o:
        factor = 1 + o["avg_pct"] / 100
        for u in evaluation.get("utilization", []):
            this = u["projected"] - u["prior"]
            est = u["prior"] + this * factor
            pct = est / u["cap"] * 100
            if u["projected"] > u["cap"]:
                continue  # the rules already stop it
            if est > u["cap"] or (pct >= u["warn_at_pct"] and u["pct_after"] < u["warn_at_pct"]):
                breaks = est > u["cap"]
                found.append({
                    "rule_id": u["rule_id"], "rule_type": "memory", "clause": "Memory", "code": "LEARNED_OVERRUN",
                    "severity": "warning", "source": "memory", "score": 62 if breaks else 45,
                    "message": (f"Memory: {o['spender']}'s final invoices have come in {o['avg_pct']:g}% above the "
                                f"checked amount ({o['times']} time{'s' if o['times'] != 1 else ''}). At that rate this "
                                f"is really about {sym}{est - u['prior']:,.0f}, taking {u['label']} to {pct:.0f}% of the "
                                f"{sym}{u['cap']:,.0f} limit" + (", over it." if breaks else ".")),
                    "details": {"estimated_total": round(est, 2), "estimated_payment": round(this * factor, 2),
                                "checked_payment": round(this, 2), "cap": u["cap"], "avg_overrun_pct": o["avg_pct"],
                                "currency": currency},
                })
                break
    a = hints["suggested_approval"]
    blocked_by_approval = any(f["code"] in ("JURISDICTION_BLOCKED", "PRIOR_APPROVAL_REQUIRED")
                              for f in evaluation.get("findings", []))
    if a and blocked_by_approval and not has_approval_ref:
        found.append({
            "rule_id": "memory-approval", "rule_type": "memory", "clause": "Memory", "code": "APPROVAL_ON_FILE",
            "severity": "warning", "source": "memory", "score": 20,
            "message": f"Memory: {a['vendor']} was paid under funder approval {a['ref']} before. If it still "
                       "covers this work, add it and this payment can go ahead.",
            "details": {"approval_ref": a["ref"]},
        })
    if not found:
        evaluation["memory"] = {"hints": hints, "findings": 0}
        return evaluation
    evaluation["findings"] = evaluation.get("findings", []) + found
    evaluation["warnings"] = evaluation.get("warnings", []) + [f["message"] for f in found]
    if evaluation["status"] == "APPROVED" and any(f["code"] == "LEARNED_OVERRUN" for f in found):
        evaluation["status"] = "APPROVED_WITH_WARNINGS"
        evaluation["status_changed_by_memory"] = True
    evaluation["risk_score"] = max(evaluation["risk_score"], max(f["score"] for f in found))
    evaluation["severity_counts"]["warning"] = evaluation["severity_counts"].get("warning", 0) + len(found)
    evaluation["memory"] = {"hints": hints, "findings": len(found)}
    return evaluation


ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "highlights": {"type": "array", "items": {"type": "string"}}},
    "required": ["answer"],
}


async def answer(grant_id: str, question: str) -> dict[str, Any]:
    """A written answer to a question about this grant, from Hindsight reflect over everything it remembers."""
    client = hindsight.get_client()
    if not client.enabled:
        return {"status": "disabled", "answer": "", "highlights": []}
    try:
        r = await client.reflect(
            f"{question}\n\nAnswer in two to four plain sentences for a busy founder. Use exact amounts, dates, "
            "people and vendors from memory. If memory doesn't say, answer that you don't know.",
            tags=[f"grant:{grant_id}"], response_schema=ANSWER_SCHEMA,
        )
    except hindsight.HindsightError as exc:
        return {"status": "unavailable", "error": str(exc)[:200], "answer": "", "highlights": []}
    out = r.structured_output or {}
    text = str(out.get("answer") or "").strip() or r.text.strip()
    return {"status": "ok" if text else "invalid", "answer": text[:1500],
            "highlights": [str(h)[:200] for h in (out.get("highlights") or [])[:4] if str(h).strip()]}

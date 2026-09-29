"""Bridges the local ledger (system of record) and Hindsight (semantic memory)."""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

from app import hindsight, local_store
from app.config import settings

logger = logging.getLogger("grantanchor.memory_sync")


async def record_memory(
    *,
    grant_id: str,
    spender: str,
    category: str,
    amount: float,
    vendor: str,
    content: str,
    timestamp: str | None = None,
    context: str = "",
    spender_id: str | None = None,
    location: str = "",
    fx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the ledger entry locally first, then try to retain it in Hindsight."""
    grant_id = grant_id or local_store.load_store().get("active_grant_id", "")
    record = local_store.add_memory(
        grant_id=grant_id,
        spender=spender,
        category=category,
        amount=amount,
        vendor=vendor,
        content=content,
        location=location,
        spender_id=spender_id,
        timestamp=timestamp,
        fx=fx,
    )
    status = await sync_record(record)
    return {"status": status, "record": local_store.get_memory(record["id"]) or record}


async def sync_record(record: dict[str, Any]) -> str:
    client = hindsight.get_client()
    if not client.enabled:
        local_store.update_memory(record["id"], {"sync_status": "local_only"})
        return "retained_local"
    try:
        await client.retain([hindsight.build_memory_item(record)], async_=True)
    except hindsight.HindsightError as exc:
        logger.warning("hindsight retain failed", extra={"memory_id": record["id"], "error": str(exc)})
        local_store.update_memory(record["id"], {"sync_status": "failed", "sync_error": str(exc)[:300],
                                                 **next_attempt(record.get("sync_attempts", 0), exc.retryable)})
        return "retained_local"
    local_store.update_memory(record["id"], {"sync_status": "synced", "sync_error": None, "sync_attempts": 0,
                                             "next_attempt_at": None})
    return "retained_remote"


def next_attempt(attempts: int, retryable: bool = True) -> dict[str, object]:
    """Outbox bookkeeping: exponential backoff with jitter; stop after ``outbox_max_attempts``."""
    attempts += 1
    if not retryable or attempts >= settings.outbox_max_attempts:
        return {"sync_attempts": attempts, "next_attempt_at": None, "sync_gave_up": True}
    delay = min(settings.outbox_backoff_max_seconds, settings.outbox_backoff_base_seconds * 2 ** (attempts - 1))
    return {"sync_attempts": attempts, "next_attempt_at": time.time() + random.uniform(delay / 2, delay),
            "sync_gave_up": False}


def due(record: dict[str, object]) -> bool:
    if record.get("sync_status") == "synced" or record.get("sync_gave_up"):
        return False
    nxt = record.get("next_attempt_at")
    return not nxt or float(nxt) <= time.time()  # type: ignore[arg-type]


async def sync_pending(grant_id: str | None = None, limit: int = 50) -> dict[str, int]:
    """Push every ledger entry that is not yet in Hindsight. Safe to repeat (document_id upserts)."""
    store = local_store.load_store()
    todo = [m for m in store["memories"] if due(m) and (not grant_id or m.get("grant_id") == grant_id)][:limit]
    counts = {"synced": 0, "failed": 0, "local_only": 0}
    client = hindsight.get_client()
    if not client.enabled:
        for m in todo:
            await sync_record(m)
            counts["local_only"] += 1
        return counts
    for i in range(0, len(todo), BATCH):
        chunk = todo[i : i + BATCH]
        try:
            await client.retain([hindsight.build_memory_item(m) for m in chunk], async_=True)
        except hindsight.HindsightError as exc:
            for m in todo[i:]:
                local_store.update_memory(m["id"], {"sync_status": "failed", "sync_error": str(exc)[:300],
                                                    **next_attempt(m.get("sync_attempts", 0), exc.retryable)})
            counts["failed"] += len(todo) - i
            break  # remote is down; the outbox will come back later
        for m in chunk:
            local_store.update_memory(m["id"], {"sync_status": "synced", "sync_error": None, "sync_attempts": 0,
                                                "next_attempt_at": None})
        counts["synced"] += len(chunk)
    return counts


BATCH = 25  # items per Hindsight retain call


async def forget(memory_id: str) -> None:
    client = hindsight.get_client()
    if client.enabled:
        try:
            await client.delete_document(memory_id)
        except hindsight.HindsightError as exc:
            logger.warning("hindsight delete failed", extra={"memory_id": memory_id, "error": str(exc)})


async def recall_memories(query: str, top_k: int = 10, grant_id: str = "") -> list[dict[str, Any]]:
    """Semantic recall from Hindsight, restricted to entries that exist in the ledger right now.

    Hindsight can hold facts about entries that were later removed, plus consolidated
    observations that belong to no single entry. Only results that map back to a live
    ledger row (via metadata.memory_id or document_id) are returned, with the ledger's
    current values. Falls back to local keyword search.
    """
    store = local_store.load_store()
    active = grant_id or store.get("active_grant_id", "")
    ledger = {m["id"]: m for m in store.get("memories", []) if not active or m.get("grant_id") == active}
    client = hindsight.get_client()
    if client.enabled and not client.circuit_open and query.strip() and ledger:
        tags = [f"grant:{active}"] if active else None
        try:
            results = await client.recall(query, tags=tags)
        except hindsight.HindsightError as exc:
            logger.info("hindsight recall unavailable; using local store", extra={"error": str(exc)})
            results = []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r in results:
            mid = (r.metadata or {}).get("memory_id") or r.document_id
            if not mid or mid not in ledger or mid in seen:
                continue
            seen.add(mid)
            out.append({**ledger[mid], "snippet": r.text, "source": "hindsight"})
        if out:
            return out[:top_k]
    return local_recall(query=query, top_k=top_k, grant_id=active)


_STOP = {
    "what",
    "did",
    "does",
    "have",
    "has",
    "the",
    "and",
    "for",
    "with",
    "our",
    "was",
    "were",
    "how",
    "much",
    "many",
    "spend",
    "spent",
    "spending",
    "cost",
    "costs",
    "paid",
    "pay",
    "payments",
    "payment",
    "any",
    "all",
    "from",
    "this",
    "that",
    "who",
    "when",
    "where",
    "recent",
    "show",
    "list",
    "give",
    "about",
    "money",
}


def local_recall(query: str, top_k: int = 10, grant_id: str = "") -> list[dict[str, Any]]:
    """Keyword search over the live ledger. Returns only entries that match at least one term."""
    store = local_store.load_store()
    memories = store.get("memories", [])
    active = grant_id or store.get("active_grant_id", "")
    pool = [m for m in memories if m.get("grant_id") == active] if active else list(memories)
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 3 and w not in _STOP]
    words = [w[:-1] if w.endswith("s") and len(w) > 4 else w for w in words]
    if not words:
        return [{**m, "source": "ledger"} for m in reversed(pool[-top_k:])]
    scored = []
    for m in pool:
        text = (
            f"{m.get('spender', '')} {m.get('category', '')} {m.get('vendor', '')} {m.get('location', '')} "
            f"{m.get('content', '')}".lower()
        )
        score = sum(1 for w in words if w in text)
        if score:
            scored.append((score, m.get("timestamp", ""), m))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [{**m, "source": "ledger"} for _, _, m in scored[:top_k]]

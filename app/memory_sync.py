"""Bridges the local ledger (system of record) and Hindsight (semantic memory)."""

from __future__ import annotations

import logging
from typing import Any

from app import hindsight, local_store

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
) -> dict[str, Any]:
    """Write the ledger entry locally first, then try to retain it in Hindsight."""
    grant_id = grant_id or local_store.load_store().get("active_grant_id", "")
    record = local_store.add_memory(
        grant_id=grant_id, spender=spender, category=category, amount=amount, vendor=vendor, content=content
    )
    if timestamp:
        record = local_store.update_memory(record["id"], {"timestamp": timestamp}) or record
    status = await sync_record(record)
    return {"status": status, "record": local_store.get_memory(record["id"]) or record}


async def sync_record(record: dict[str, Any]) -> str:
    client = hindsight.get_client()
    if not client.enabled:
        local_store.update_memory(record["id"], {"sync_status": "local_only"})
        return "retained_local"
    try:
        await client.retain([hindsight.build_memory_item(record)])
    except hindsight.HindsightError as exc:
        logger.warning("hindsight retain failed", extra={"memory_id": record["id"], "error": str(exc)})
        local_store.update_memory(record["id"], {"sync_status": "failed", "sync_error": str(exc)[:300]})
        return "retained_local"
    local_store.update_memory(record["id"], {"sync_status": "synced", "sync_error": None})
    return "retained_remote"


async def recall_memories(query: str, top_k: int = 10, grant_id: str = "") -> list[dict[str, Any]]:
    """Semantic recall from Hindsight, falling back to local keyword ranking."""
    client = hindsight.get_client()
    if client.enabled and not client.circuit_open and query.strip():
        tags = [f"grant:{grant_id}"] if grant_id else None
        try:
            results = await client.recall(query, tags=tags)
            if results:
                return _dedupe([_recall_to_dict(r) for r in results])[:top_k]
        except hindsight.HindsightError as exc:
            logger.info("hindsight recall unavailable; using local store", extra={"error": str(exc)})
    return local_recall(query=query, top_k=top_k, grant_id=grant_id)


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hindsight returns several extracted facts per retained item; keep one per ledger id."""
    seen: set[str] = set()
    out = []
    for it in items:
        key = str(it["id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _recall_to_dict(r: hindsight.RecallResult) -> dict[str, Any]:
    meta = r.metadata or {}
    try:
        amount = float(meta.get("amount", 0.0))
    except ValueError:
        amount = 0.0
    return {
        "id": meta.get("memory_id") or r.id,
        "hindsight_id": r.id,
        "grant_id": meta.get("grant_id", ""),
        "timestamp": r.occurred_start or r.mentioned_at,
        "spender": meta.get("spender", ""),
        "category": meta.get("category", ""),
        "amount": amount,
        "vendor": meta.get("vendor", ""),
        "content": r.text,
        "source": "hindsight",
    }


def local_recall(query: str, top_k: int = 10, grant_id: str = "") -> list[dict[str, Any]]:
    store = local_store.load_store()
    memories = store.get("memories", [])
    active = grant_id or store.get("active_grant_id", "")
    pool = [m for m in memories if m.get("grant_id") == active] if active else list(memories)
    if not query.strip():
        return pool[-top_k:]
    tokens = set(query.lower().split())
    scored = []
    for m in pool:
        text = f"{m.get('spender', '')} {m.get('category', '')} {m.get('vendor', '')} {m.get('content', '')}".lower()
        scored.append((sum(1 for t in tokens if t in text), m))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:top_k]]

"""Async Hindsight REST client with seamless local JSON store synchronization."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import settings
from app import local_store

logger = logging.getLogger("grantanchor.hindsight")

# Circuit breaker cache to prevent stalling on dead/unprovisioned remote banks
_remote_cooldowns: dict[str, float] = {}


def _is_remote_circuit_open(bank_id: str) -> bool:
    cooldown = _remote_cooldowns.get(bank_id, 0.0)
    return time.time() < cooldown


def _trip_circuit_breaker(bank_id: str, duration_sec: float = 60.0) -> None:
    _remote_cooldowns[bank_id] = time.time() + duration_sec


async def retain(
    bank_id: str,
    content: str,
    context: str = "",
    timestamp: str = "",
    metadata: dict[str, Any] | None = None,
    grant_id: str = "",
    spender: str = "",
    category: str = "",
    amount: float = 0.0,
    vendor: str = "",
) -> dict[str, Any]:
    """Push memory to Vectorize Hindsight API AND append to local_store.py."""
    ts = timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta = metadata or {}

    effective_grant_id = grant_id or meta.get("grant_id") or local_store.load_store().get("active_grant_id", "")
    effective_spender = spender or meta.get("spender", "System")
    effective_category = category or meta.get("category", "General")
    effective_amount = amount or float(meta.get("amount", 0.0))
    effective_vendor = vendor or meta.get("vendor", "")

    # Always persist locally first for instant zero-latency safety
    local_rec = local_store.add_memory(
        grant_id=effective_grant_id,
        spender=effective_spender,
        category=effective_category,
        amount=effective_amount,
        vendor=effective_vendor,
        content=content,
    )

    if not settings.hindsight_api_key or _is_remote_circuit_open(bank_id):
        return {"status": "retained_local", "record": local_rec}

    url = f"{settings.hindsight_base_url}/banks/{bank_id}/retain"
    headers = {
        "Authorization": f"Bearer {settings.hindsight_api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "content": content,
        "metadata": {
            **meta,
            "grant_id": effective_grant_id,
            "spender": effective_spender,
            "category": effective_category,
            "amount": effective_amount,
            "vendor": effective_vendor,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 404:
                _trip_circuit_breaker(bank_id, 120.0)
                logger.info("Remote bank %s returned 404; falling back to local persistence", bank_id)
                return {"status": "retained_local_synced", "record": local_rec}
            resp.raise_for_status()
            return {"status": "retained_remote", "response": resp.json(), "record": local_rec}
    except Exception as exc:
        _trip_circuit_breaker(bank_id, 30.0)
        logger.info("Hindsight retain unavailable (%s); using local store", exc)
        return {"status": "retained_local_synced", "record": local_rec}


async def recall(
    bank_id: str,
    query: str,
    top_k: int = 10,
    grant_id: str = "",
) -> list[dict[str, Any]]:
    """Recall memories from Hindsight API with seamless local_store fallback."""
    if not settings.hindsight_api_key or _is_remote_circuit_open(bank_id):
        return _local_recall(query=query, top_k=top_k, grant_id=grant_id)

    url = f"{settings.hindsight_base_url}/banks/{bank_id}/recall"
    headers = {
        "Authorization": f"Bearer {settings.hindsight_api_key}",
        "Content-Type": "application/json",
    }
    payload = {"query": query, "top_k": top_k}

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 404:
                _trip_circuit_breaker(bank_id, 120.0)
                return _local_recall(query=query, top_k=top_k, grant_id=grant_id)
            resp.raise_for_status()
            data = resp.json()
            memories = data.get("memories", data.get("results", []))
            if isinstance(memories, list) and memories:
                return memories
            return _local_recall(query=query, top_k=top_k, grant_id=grant_id)
    except Exception as exc:
        _trip_circuit_breaker(bank_id, 30.0)
        logger.info("Hindsight recall unavailable (%s); falling back to local store", exc)
        return _local_recall(query=query, top_k=top_k, grant_id=grant_id)


def _local_recall(query: str, top_k: int = 10, grant_id: str = "") -> list[dict[str, Any]]:
    """Filter and rank memories from local_store.py."""
    store = local_store.load_store()
    memories = store.get("memories", [])
    active_grant = grant_id or store.get("active_grant_id", "")

    # Filter to current grant if provided
    if active_grant:
        grant_memories = [m for m in memories if m.get("grant_id") == active_grant]
    else:
        grant_memories = list(memories)

    if not query.strip():
        return grant_memories[-top_k:]

    query_tokens = set(query.lower().split())
    scored: list[tuple[int, dict[str, Any]]] = []

    for m in grant_memories:
        text = f"{m.get('spender', '')} {m.get('category', '')} {m.get('vendor', '')} {m.get('content', '')}".lower()
        score = sum(1 for t in query_tokens if t in text)
        scored.append((score, m))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:top_k]]

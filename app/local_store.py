"""Local JSON persistence engine for GrantAnchor."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger("grantanchor.local_store")

_LOCK = threading.RLock()


def _store_path() -> Path:
    return Path(settings.data_dir) / settings.json_store_filename

def _load_seed() -> dict[str, Any]:
    with open(settings.seed_path, "r", encoding="utf-8") as f:
        return json.load(f)


DEFAULT_STORE: dict[str, Any] = _load_seed()


def load_store() -> dict[str, Any]:
    """Load store from JSON file; auto-initialize if not present."""
    path = _store_path()
    if not path.exists():
        save_store(DEFAULT_STORE)
        return copy.deepcopy(DEFAULT_STORE)

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.error("Failed to load store from %s: %s; recreating defaults", path, exc)
        save_store(DEFAULT_STORE)
        return copy.deepcopy(DEFAULT_STORE)


def save_store(data: dict[str, Any]) -> None:
    """Save store dictionary to disk atomically."""
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    with _LOCK:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, path)


def add_memory(
    grant_id: str,
    spender: str,
    category: str,
    amount: float,
    vendor: str,
    content: str,
    **extra: Any,
) -> dict[str, Any]:
    """Append a new spending or milestone memory to local store."""
    mem_id = f"mem_{uuid.uuid4().hex[:6]}"
    record = {
        "id": mem_id,
        "grant_id": grant_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "spender": spender,
        "category": category,
        "amount": amount,
        "vendor": vendor,
        "content": content,
    }
    record.update({k: v for k, v in extra.items() if v is not None})
    with _LOCK:
        store = load_store()
        store.setdefault("memories", []).append(record)
        save_store(store)
    return record


def get_memory(memory_id: str) -> dict[str, Any] | None:
    return next((m for m in load_store().get("memories", []) if m.get("id") == memory_id), None)


def update_memory(memory_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    with _LOCK:
        store = load_store()
        for m in store.get("memories", []):
            if m.get("id") == memory_id:
                m.update(fields)
                save_store(store)
                return m
    return None


def switch_grant(grant_id: str) -> dict[str, Any]:
    """Change the active grant identifier."""
    store = load_store()
    if grant_id not in store.get("grants", {}):
        raise ValueError(f"Unknown grant ID: {grant_id}")
    store["active_grant_id"] = grant_id
    save_store(store)
    return store


def reset_seed() -> dict[str, Any]:
    """Reset store to default seed baseline."""
    fresh = copy.deepcopy(DEFAULT_STORE)
    save_store(fresh)
    return fresh

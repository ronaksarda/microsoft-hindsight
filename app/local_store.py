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

DEFAULT_STORE: dict[str, Any] = {
    "active_grant_id": "NSF-2026-881",
    "grants": {
        "NSF-2026-881": {
            "name": "NSF DeepTech Phase I",
            "total_funding": 250000,
            "currency": "USD",
            "rules": [
                "Clause 9.1: Zero foreign contractor spend without 30-day prior written agency approval.",
                "Clause 4.2: Cumulative travel expenses capped at $8,000 total across all team members."
            ]
        },
        "EU-HORIZON-409": {
            "name": "Horizon Europe EIC Transition",
            "total_funding": 1200000,
            "currency": "EUR",
            "rules": [
                "Article 12: Subcontracting capped at 15% of total budget.",
                "Article 6: Equipment deprecation must span 36 months minimum."
            ]
        }
    },
    "team_members": [
        {"id": "tm_1", "name": "Ronak Sarda", "role": "Lead Architect"},
        {"id": "tm_2", "name": "Sarah Miller", "role": "Head of Operations"},
        {"id": "tm_3", "name": "David Park", "role": "Senior ML Engineer"}
    ],
    "memories": [
        {
            "id": "mem_01",
            "grant_id": "NSF-2026-881",
            "timestamp": "2026-06-15T10:00:00Z",
            "spender": "Sarah Miller",
            "category": "Travel",
            "amount": 5400,
            "vendor": "Lufthansa / Marriott Munich",
            "content": "Sarah Miller booked transatlantic flights and lodging for 2 devs attending NeurIPS Munich ($5,400 of $8,000 travel cap)."
        }
    ]
}


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

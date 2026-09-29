"""SQLite persistence for GrantAnchor (system of record).

Tables: grants, members, memories (the spend ledger), audits (every check that
was run), users, sessions, meta. Hindsight holds a semantic copy of each ledger
entry; this database is the source of truth for all money math.

The public functions keep the shape of the earlier JSON store so existing
callers (``load_store()`` returning a dict) keep working.
"""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from app import clock
from app.config import settings

logger = logging.getLogger("grantanchor.store")

_LOCK = threading.RLock()
_CONNS: dict[str, sqlite3.Connection] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, data TEXT NOT NULL, deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS members (id TEXT PRIMARY KEY, data TEXT NOT NULL, deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, ts TEXT NOT NULL, data TEXT NOT NULL, deleted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_mem_grant ON memories(grant_id, ts);
CREATE TABLE IF NOT EXISTS audits (id TEXT PRIMARY KEY, grant_id TEXT, created_at TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_audit_grant ON audits(grant_id, created_at);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pw_hash TEXT NOT NULL,
    role TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_events (
    id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_grant ON memory_events(grant_id, created_at);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL);
"""


def db_path() -> Path:
    return Path(settings.data_dir) / settings.sqlite_filename


def conn() -> sqlite3.Connection:
    path = str(db_path())
    with _LOCK:
        c = _CONNS.get(path)
        if c is None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            c.executescript(SCHEMA)
            _CONNS[path] = c
        return c


EMPTY_STORE: dict[str, Any] = {"active_grant_id": "", "grants": {}, "team_members": [], "memories": []}

# Records created by the old built-in sample data. Removed once from existing databases.
_DEMO_GRANTS = {"NSF-2026-881": "NSF DeepTech Phase I", "EU-HORIZON-409": "Horizon Europe EIC Transition"}
_DEMO_MEMBERS = {"tm_1": "Ronak Sarda", "tm_2": "Sarah Miller", "tm_3": "David Park"}


def default_store() -> dict[str, Any]:
    if settings.seed_path:
        with open(settings.seed_path, encoding="utf-8") as f:
            return json.load(f)
    return copy.deepcopy(EMPTY_STORE)


def _purge_demo_data() -> None:
    """One-time cleanup of the sample grants/team/ledger the app used to ship with."""
    c = conn()
    removed = []
    for gid, name in _DEMO_GRANTS.items():
        row = c.execute("SELECT data FROM grants WHERE id=?", (gid,)).fetchone()
        if row and json.loads(row["data"]).get("name") == name:
            c.execute("DELETE FROM grants WHERE id=?", (gid,))
            c.execute("DELETE FROM memories WHERE grant_id=?", (gid,))
            c.execute("DELETE FROM audits WHERE grant_id=?", (gid,))
            removed.append(gid)
    for mid, name in _DEMO_MEMBERS.items():
        row = c.execute("SELECT data FROM members WHERE id=?", (mid,)).fetchone()
        if row and json.loads(row["data"]).get("name") == name:
            c.execute("DELETE FROM members WHERE id=?", (mid,))
    if _meta("active_grant_id") in removed:
        _set_meta("active_grant_id", "")
    _set_meta("demo_purged", "1")
    if removed:
        logger.info("removed built-in sample data: %s", removed)


def _meta(key: str, default: str | None = None) -> str | None:
    row = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_meta(key: str, value: str) -> None:
    conn().execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def _ensure_seeded() -> None:
    with _LOCK:
        if _meta("seeded") is None:
            _write_all(default_store())
            _set_meta("demo_purged", "1")
        elif _meta("demo_purged") is None and not settings.seed_path:
            _purge_demo_data()


def _write_all(data: dict[str, Any]) -> None:
    c = conn()
    with _LOCK:
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("DELETE FROM grants")
            c.execute("DELETE FROM members")
            c.execute("DELETE FROM memories")
            c.execute("DELETE FROM memory_events")
            for gid, g in data.get("grants", {}).items():
                c.execute("INSERT INTO grants(id, data) VALUES(?, ?)", (gid, json.dumps(g)))
            for m in data.get("team_members", []):
                c.execute("INSERT INTO members(id, data) VALUES(?, ?)", (m["id"], json.dumps(m)))
            for m in data.get("memories", []):
                m.setdefault("sync_status", "pending")
                c.execute(
                    "INSERT INTO memories(id, grant_id, ts, data) VALUES(?, ?, ?, ?)",
                    (m["id"], m.get("grant_id", ""), m.get("timestamp", ""), json.dumps(m)),
                )
            if "audits" in data:
                c.execute("DELETE FROM audits")
            for a in data.get("audits", []):
                c.execute(
                    "INSERT INTO audits(id, grant_id, created_at, data) VALUES(?, ?, ?, ?)",
                    (a["id"], a["grant_id"], a["created_at"], json.dumps(a)),
                )
            for e in data.get("events", []):
                e.setdefault("sync_status", "pending")
                c.execute(
                    "INSERT INTO memory_events(id, grant_id, kind, created_at, data) VALUES(?, ?, ?, ?, ?)",
                    (e["id"], e["grant_id"], e["kind"], e["created_at"], json.dumps(e)),
                )
            c.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('active_grant_id', ?)",
                (data.get("active_grant_id", ""),),
            )
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('seeded', '1')")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


# --------------------------------------------------------------------------- #
# Whole-state API (legacy shape)
# --------------------------------------------------------------------------- #


def load_store() -> dict[str, Any]:
    _ensure_seeded()
    c = conn()
    grants = {r["id"]: json.loads(r["data"]) for r in c.execute("SELECT id, data FROM grants WHERE deleted=0")}
    members = [json.loads(r["data"]) for r in c.execute("SELECT data FROM members WHERE deleted=0 ORDER BY rowid")]
    memories = [json.loads(r["data"]) for r in c.execute("SELECT data FROM memories WHERE deleted=0 ORDER BY ts")]
    active = _meta("active_grant_id", "") or ""
    if active not in grants and grants:
        active = next(iter(grants))
    return {"active_grant_id": active, "grants": grants, "team_members": members, "memories": memories}


def save_store(data: dict[str, Any]) -> None:
    _write_all(copy.deepcopy(data))


def reset_seed() -> dict[str, Any]:
    """Reset the workspace to the configured seed (empty unless SEED_PATH is set). Users are kept."""
    with _LOCK:
        conn().execute("DELETE FROM audits")
        _write_all(default_store())
    return load_store()


def switch_grant(grant_id: str) -> dict[str, Any]:
    store = load_store()
    if grant_id not in store.get("grants", {}):
        active_id = store.get("active_grant_id")
        if active_id and active_id in store.get("grants", {}):
            return store
        if store.get("grants"):
            grant_id = next(iter(store["grants"]))
        else:
            raise ValueError(f"Unknown grant ID: {grant_id}")
    _set_meta("active_grant_id", grant_id)
    store["active_grant_id"] = grant_id
    return store


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


def add_memory(
    grant_id: str,
    spender: str,
    category: str,
    amount: float,
    vendor: str,
    content: str,
    **extra: Any,
) -> dict[str, Any]:
    """Append a spend record to the ledger."""
    _ensure_seeded()
    record: dict[str, Any] = {
        "id": f"mem_{uuid.uuid4().hex[:10]}",
        "grant_id": grant_id,
        "timestamp": clock.now_iso(),
        "spender": spender,
        "category": category,
        "amount": amount,
        "vendor": vendor,
        "content": content,
        "recorded_at": clock.now_iso(),
        "sync_status": "pending",
    }
    record.update({k: v for k, v in extra.items() if v is not None})
    conn().execute(
        "INSERT INTO memories(id, grant_id, ts, data) VALUES(?, ?, ?, ?)",
        (record["id"], grant_id, record["timestamp"], json.dumps(record)),
    )
    return record


def get_memory(memory_id: str, include_deleted: bool = False) -> dict[str, Any] | None:
    _ensure_seeded()
    q = "SELECT data FROM memories WHERE id=?" + ("" if include_deleted else " AND deleted=0")
    row = conn().execute(q, (memory_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def update_memory(memory_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    with _LOCK:
        m = get_memory(memory_id, include_deleted=True)
        if m is None:
            return None
        m.update(fields)
        conn().execute(
            "UPDATE memories SET data=?, ts=? WHERE id=?", (json.dumps(m), m.get("timestamp", ""), memory_id)
        )
        return m


def delete_memory(memory_id: str, *, by: str, reason: str) -> dict[str, Any] | None:
    """Soft delete: the row stays for audit, but no longer counts toward any cap."""
    m = update_memory(memory_id, {"deleted_at": clock.now_iso(), "deleted_by": by, "delete_reason": reason})
    if m is not None:
        conn().execute("UPDATE memories SET deleted=1 WHERE id=?", (memory_id,))
    return m


def list_memories(
    grant_id: str,
    *,
    q: str = "",
    category: str = "",
    sync_status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    _ensure_seeded()
    rows = [
        json.loads(r["data"])
        for r in conn().execute(
            "SELECT data FROM memories WHERE deleted=0 AND grant_id=? ORDER BY ts DESC", (grant_id,)
        )
    ]
    if category:
        rows = [m for m in rows if str(m.get("category", "")).lower() == category.lower()]
    if sync_status:
        rows = [m for m in rows if m.get("sync_status") == sync_status]
    if q:
        ql = q.lower()
        rows = [
            m
            for m in rows
            if ql
            in f"{m.get('vendor', '')} {m.get('spender', '')} {m.get('content', '')} {m.get('location', '')}".lower()
        ]
    return {"items": rows[offset : offset + limit], "total": len(rows), "limit": limit, "offset": offset}


# --------------------------------------------------------------------------- #
# Team
# --------------------------------------------------------------------------- #


def add_member(name: str, role: str) -> dict[str, Any]:
    _ensure_seeded()
    m = {"id": f"tm_{uuid.uuid4().hex[:6]}", "name": name, "role": role}
    conn().execute("INSERT INTO members(id, data) VALUES(?, ?)", (m["id"], json.dumps(m)))
    return m


def get_member(member_id: str) -> dict[str, Any] | None:
    _ensure_seeded()
    row = conn().execute("SELECT data FROM members WHERE id=? AND deleted=0", (member_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def update_member(member_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Update a member; a rename also updates the spender name on their ledger entries."""
    with _LOCK:
        m = get_member(member_id)
        if m is None:
            return None
        old_name = m.get("name")
        m.update(fields)
        conn().execute("UPDATE members SET data=? WHERE id=?", (json.dumps(m), member_id))
        if fields.get("name") and fields["name"] != old_name:
            for r in conn().execute("SELECT id, data FROM memories").fetchall():
                d = json.loads(r["data"])
                if d.get("spender_id") == member_id or (not d.get("spender_id") and d.get("spender") == old_name):
                    d["spender"] = fields["name"]
                    conn().execute("UPDATE memories SET data=? WHERE id=?", (json.dumps(d), r["id"]))
        return m


def delete_member(member_id: str) -> bool:
    cur = conn().execute("UPDATE members SET deleted=1 WHERE id=? AND deleted=0", (member_id,))
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Audit history
# --------------------------------------------------------------------------- #


def add_audit(grant_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    entry = {"id": f"aud_{uuid.uuid4().hex[:10]}", "grant_id": grant_id, "created_at": clock.now_iso(), **entry}
    conn().execute(
        "INSERT INTO audits(id, grant_id, created_at, data) VALUES(?, ?, ?, ?)",
        (entry["id"], grant_id, entry["created_at"], json.dumps(entry, default=str)),
    )
    return entry


def list_audits(grant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return [
        json.loads(r["data"])
        for r in conn().execute(
            "SELECT data FROM audits WHERE grant_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?", (grant_id, limit)
        )
    ]


# --------------------------------------------------------------------------- #
# Grants
# --------------------------------------------------------------------------- #


def get_grant(grant_id: str) -> dict[str, Any] | None:
    _ensure_seeded()
    row = conn().execute("SELECT data FROM grants WHERE id=? AND deleted=0", (grant_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def grant_exists(grant_id: str) -> bool:
    return conn().execute("SELECT 1 FROM grants WHERE id=?", (grant_id,)).fetchone() is not None


def save_grant(grant_id: str, data: dict[str, Any]) -> dict[str, Any]:
    _ensure_seeded()
    conn().execute(
        "INSERT INTO grants(id, data, deleted) VALUES(?, ?, 0) ON CONFLICT(id) DO UPDATE SET data=excluded.data, deleted=0",
        (grant_id, json.dumps(data)),
    )
    return data


def delete_grant(grant_id: str) -> bool:
    cur = conn().execute("UPDATE grants SET deleted=1 WHERE id=? AND deleted=0", (grant_id,))
    if cur.rowcount and _meta("active_grant_id") == grant_id:
        row = conn().execute("SELECT id FROM grants WHERE deleted=0 LIMIT 1").fetchone()
        _set_meta("active_grant_id", row["id"] if row else "")
    return cur.rowcount > 0

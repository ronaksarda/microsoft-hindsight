"""Email + password accounts with server-side sessions (httpOnly cookie).

Passwords: PBKDF2-HMAC-SHA256, 200k iterations, per-user salt.
Sessions: random 256-bit token in the cookie; only its SHA-256 is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta
from typing import Any

from app import clock, local_store

COOKIE = "ga_session"
SESSION_DAYS = 14
_ITER = 200_000


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    return f"pbkdf2${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc.hex(), dk)
    except (ValueError, TypeError):
        return False


def _public(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"]}


class AuthError(Exception):
    pass


def signup(email: str, name: str, password: str) -> dict[str, Any]:
    email = email.strip().lower()
    c = local_store.conn()
    first = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
    try:
        uid = f"usr_{uuid.uuid4().hex[:10]}"
        c.execute(
            "INSERT INTO users(id, email, name, pw_hash, role, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (uid, email, name.strip(), hash_password(password), "admin" if first else "member", clock.now_iso()),
        )
    except Exception as exc:  # sqlite3.IntegrityError
        raise AuthError("An account with this email already exists.") from exc
    return _public(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def login(email: str, password: str) -> dict[str, Any]:
    row = local_store.conn().execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not row or not verify_password(password, row["pw_hash"]):
        raise AuthError("Email or password is incorrect.")
    return _public(row)


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    exp = (clock.now() + timedelta(days=SESSION_DAYS)).isoformat()
    local_store.conn().execute(
        "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?, ?, ?)",
        (hashlib.sha256(token.encode()).hexdigest(), user_id, exp),
    )
    return token


def user_for_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    row = (
        local_store.conn()
        .execute(
            "SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=?", (th,)
        )
        .fetchone()
    )
    if not row or row["expires_at"] < clock.now().isoformat():
        return None
    return _public(row)


def end_session(token: str | None) -> None:
    if token:
        local_store.conn().execute(
            "DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
        )

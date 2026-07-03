"""
Accounts & sessions for EnPassant.

Email + password auth. Passwords are hashed with PBKDF2-HMAC-SHA256 from the
standard library (no compiled dependency — clean install on any platform).
Sessions are opaque random tokens stored in the `sessions` table and carried
in an httponly `ep_session` cookie.
"""

import uuid
import hmac
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..database import db

# --- Password hashing -------------------------------------------------------

_PBKDF2_ITERATIONS = 200_000
_SALT_BYTES = 16
SESSION_TTL_DAYS = 30


def hash_password(password: str) -> str:
    """Return a self-describing hash: pbkdf2_sha256$iterations$salt_hex$hash_hex."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a stored pbkdf2_sha256$... string."""
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


# --- Validation -------------------------------------------------------------

def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    """Deliberately lenient — one @, a dot in the domain, no spaces. We're not
    the arbiter of RFC 5322; we just reject the obviously-broken."""
    email = email.strip()
    if " " in email or email.count("@") != 1:
        return False
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")


# --- Users ------------------------------------------------------------------

def _uuid() -> str:
    return uuid.uuid4().hex


def create_user(email: str, password: str, display_name: str) -> dict:
    """Create a user. Returns {"user": {...}} or {"error": "..."}."""
    email = normalize_email(email)
    display_name = display_name.strip()[:40]
    if not valid_email(email):
        return {"error": "Enter a valid email address."}
    if len(password) < 8:
        return {"error": "Password must be at least 8 characters."}
    if not display_name:
        return {"error": "Display name is required."}
    uid = _uuid()
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, display_name) VALUES (?, ?, ?, ?)",
                (uid, email, hash_password(password), display_name),
            )
    except sqlite3.IntegrityError:
        return {"error": "An account with that email already exists."}
    return {"user": {"id": uid, "email": email, "display_name": display_name}}


def get_user(uid: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT id, email, display_name, created_at FROM users WHERE id = ?", (uid,)
        ).fetchone()
        return dict(row) if row else None


def authenticate(email: str, password: str) -> Optional[dict]:
    """Return the public user dict on success, None on any failure."""
    email = normalize_email(email)
    with db() as conn:
        row = conn.execute(
            "SELECT id, email, display_name, password_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    return {"id": row["id"], "email": row["email"], "display_name": row["display_name"]}


# --- Sessions ---------------------------------------------------------------

def create_session(user_id: str) -> str:
    """Mint a session token for the user and persist it. Returns the token."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires),
        )
    return token


def user_for_session(token: Optional[str]) -> Optional[dict]:
    """Resolve a session token to a public user dict, or None if missing/expired.
    Expired sessions are pruned lazily on lookup."""
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT s.user_id, s.expires_at, u.email, u.display_name "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        try:
            expired = datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc)
        except ValueError:
            expired = True
        if expired:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
    return {"id": row["user_id"], "email": row["email"], "display_name": row["display_name"]}


def delete_session(token: Optional[str]) -> None:
    if not token:
        return
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

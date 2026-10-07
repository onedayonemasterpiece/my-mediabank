"""Small owner invite/session store. It never stores image or audio bytes."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

COOKIE = "my_mediabank_session"


def digest(value: str):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Actor:
    subject: str
    session: str

    @property
    def resource_id(self):
        return "review:" + self.session[:24]

    @property
    def wire(self):
        return {"subject": self.subject, "tenant_id": "my-mediabank"}


class AccessStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS invites (
                    hash TEXT PRIMARY KEY, subject TEXT NOT NULL,
                    expires INTEGER NOT NULL, consumed INTEGER
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    hash TEXT PRIMARY KEY, subject TEXT NOT NULL, expires INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS session_expiry ON sessions(expires);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    bucket INTEGER PRIMARY KEY, attempts INTEGER NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=5)
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def invite(self, *, ttl_seconds=86400, subject="owner"):
        if not 60 <= ttl_seconds <= 7 * 86400:
            raise ValueError("Invite lifetime must be between one minute and seven days")
        code = secrets.token_urlsafe(18)
        now = int(time.time())
        with self.connection() as con:
            con.execute("DELETE FROM invites WHERE expires < ?", (now,))
            con.execute("INSERT INTO invites VALUES(?,?,?,NULL)", (digest(code), subject, now + ttl_seconds))
        return code

    def login(self, code: str, *, ttl_seconds: int):
        if not isinstance(code, str) or not 16 <= len(code.strip()) <= 128:
            return None
        now = int(time.time())
        with self.connection() as con:
            # One atomic consume: two concurrent logins cannot reuse an invite.
            con.execute("BEGIN IMMEDIATE")
            bucket = now // 60
            con.execute("DELETE FROM login_attempts WHERE bucket < ?", (bucket - 2,))
            con.execute("INSERT INTO login_attempts VALUES(?,1) ON CONFLICT(bucket) DO UPDATE SET attempts=attempts+1", (bucket,))
            if con.execute("SELECT attempts FROM login_attempts WHERE bucket=?", (bucket,)).fetchone()[0] > 30:
                return None
            row = con.execute("SELECT subject FROM invites WHERE hash=? AND expires>? AND consumed IS NULL",
                              (digest(code.strip()), now)).fetchone()
            if not row:
                return None
            con.execute("UPDATE invites SET consumed=? WHERE hash=?", (now, digest(code.strip())))
            token = secrets.token_urlsafe(32)
            con.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            con.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token), row[0], now + ttl_seconds))
            return token

    def authenticate(self, token: str | None):
        if not isinstance(token, str) or len(token) > 128:
            return None
        hashed = digest(token)
        with self.connection() as con:
            row = con.execute("SELECT subject FROM sessions WHERE hash=? AND expires>?", (hashed, int(time.time()))).fetchone()
        return Actor(row[0], hashed) if row else None

    def logout(self, token: str):
        with self.connection() as con:
            con.execute("DELETE FROM sessions WHERE hash=?", (digest(token),))

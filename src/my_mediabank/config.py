from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    origin: str
    state_dir: Path
    static_dir: Path
    model: str = "gemini-3.8-live"
    release: str = "development"
    session_days: int = 30

    def __post_init__(self):
        parsed = urlsplit(self.origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("MY_MEDIABANK_ORIGIN must be one HTTPS origin")
        if self.origin.endswith("/"):
            object.__setattr__(self, "origin", self.origin.rstrip("/"))
        if self.model not in {"gemini-3.8-live", "gemini-3.8-live-extended-thinking"}:
            raise ValueError("Unsupported Live model for the pinned framework")

    @property
    def db_path(self):
        return self.state_dir / "access.sqlite3"

    @classmethod
    def from_env(cls):
        return cls(
            origin=os.environ.get("MY_MEDIABANK_ORIGIN", "https://my-mediabank.kenigevents.ru"),
            state_dir=Path(os.environ.get("MY_MEDIABANK_STATE_DIR", "/home/dev/.local/share/my-mediabank/state")),
            static_dir=Path(os.environ.get("MY_MEDIABANK_STATIC_DIR", "dist")).resolve(),
            model=os.environ.get("MY_MEDIABANK_LIVE_MODEL", "gemini-3.8-live"),
            release=os.environ.get("MY_MEDIABANK_RELEASE", "development"),
        )


def resource_environment():
    """Existing private SDK configuration; there is no per-product raw-key fallback."""
    return {name: os.environ[name] for name in (
        "AI_RESOURCE_CONTROL_URL", "AI_RESOURCE_CONTROL_SERVICE_KEY", "AI_RESOURCE_LEDGER_ID"
    ) if os.environ.get(name)}

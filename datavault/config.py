"""Filesystem layout. Everything lives under one data directory so a backup is
just the database file plus the media folder."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOME = Path(__file__).resolve().parent.parent / "data"


@dataclass(frozen=True)
class Config:
    home: Path

    @classmethod
    def from_env(cls, home: str | Path | None = None) -> "Config":
        return cls(Path(home or os.environ.get("DATAVAULT_HOME") or DEFAULT_HOME).expanduser().resolve())

    @property
    def db_path(self) -> Path:
        return self.home / "datavault.db"

    @property
    def media_dir(self) -> Path:
        return self.home / "media"

    @property
    def thumbs_dir(self) -> Path:
        return self.home / "thumbs"

    @property
    def backups_dir(self) -> Path:
        return self.home / "backups"

    def ensure_dirs(self) -> None:
        for d in (self.home, self.media_dir, self.thumbs_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)

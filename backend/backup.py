"""Consistent online backups for the SQLite application database."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from backend.config import settings


def backup_database(destination_dir: Path | None = None) -> Path:
    source_path = settings.data_dir / "feedback.db"
    if not source_path.exists():
        raise FileNotFoundError(f"database not found: {source_path}")
    destination_dir = destination_dir or (settings.data_dir / "backups")
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = destination_dir / f"feedback-{stamp}.db"
    with closing(sqlite3.connect(source_path)) as source, closing(
        sqlite3.connect(destination)
    ) as target:
        source.backup(target)
        check = target.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError("backup integrity check failed")
        target.commit()
    return destination

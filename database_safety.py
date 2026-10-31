"""Non-destructive SQLite migration and rotating snapshot helpers."""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def migrate_legacy_database(target_path: str | Path, legacy_path: str | Path,
                            logger: logging.Logger | None = None) -> bool:
    """Copy a legacy SQLite DB to a mounted persistent path using SQLite backup API.

    The source is never deleted or modified. Existing target databases are never
    overwritten. The caller must invoke this before opening the target database.
    """
    log = logger or logging.getLogger(__name__)
    target = Path(target_path).expanduser().resolve()
    legacy = Path(legacy_path).expanduser().resolve()
    if target == legacy or target.exists() or not legacy.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".migration.tmp")
    if temp.exists():
        temp.unlink()
    try:
        with sqlite3.connect(str(legacy), timeout=30) as source:
            with sqlite3.connect(str(temp), timeout=30) as destination:
                source.backup(destination, pages=256, sleep=0.05)
                check = destination.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise sqlite3.DatabaseError(f"Legacy database integrity check failed: {check!r}")
        # Atomic rename: do not expose a half-copied database on a crash.
        os.replace(temp, target)
        log.info("Migrated legacy SQLite database to persistent storage: %s", target)
        return True
    except Exception:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            log.exception("Could not remove incomplete SQLite migration file")
        log.exception("Legacy database migration failed; source database was preserved")
        raise


def backup_sqlite_database(db_path: str | Path, backup_dir: str | Path,
                           keep: int = 7, logger: logging.Logger | None = None) -> Path | None:
    """Create a consistent SQLite snapshot and rotate old snapshots.

    This is a local safety snapshot, not an off-site disaster-recovery backup.
    """
    log = logger or logging.getLogger(__name__)
    source_path = Path(db_path).expanduser().resolve()
    if not source_path.is_file():
        return None
    destination_dir = Path(backup_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = destination_dir / f"shop-{stamp}.sqlite3"
    temp = target.with_suffix(".sqlite3.tmp")
    try:
        with sqlite3.connect(str(source_path), timeout=30) as source:
            with sqlite3.connect(str(temp), timeout=30) as destination:
                source.backup(destination, pages=256, sleep=0.05)
                check = destination.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise sqlite3.DatabaseError(f"Backup integrity check failed: {check!r}")
        os.replace(temp, target)
        backups = sorted(destination_dir.glob("shop-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        keep_count = max(1, int(keep))
        for old in backups[keep_count:]:
            try:
                old.unlink()
            except OSError:
                log.warning("Could not rotate old DB backup %s", old, exc_info=True)
        log.info("SQLite snapshot created: %s", target)
        return target
    except Exception:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            log.exception("Could not remove incomplete DB backup")
        log.exception("SQLite backup failed")
        raise

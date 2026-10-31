"""Scheduled, integrity-checked SQLite backups for SEYED PREMIUM.

This module wraps the project's non-destructive SQLite backup primitive.
It does not delete the live database and does not claim that a backup kept
on the same disk is an off-site disaster-recovery copy.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from database_safety import backup_sqlite_database


DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_RETRY_SECONDS = 15 * 60
DEFAULT_RETENTION_COUNT = 7


def create_backup(db_path: str | Path, backup_dir: str | Path, *,
                  keep: int = DEFAULT_RETENTION_COUNT,
                  logger: logging.Logger | None = None) -> Optional[Path]:
    """Create and integrity-check one SQLite snapshot.

    Returns None when the source database does not exist yet.
    Raises on actual backup errors so callers can alert/log them.
    """
    source = Path(db_path).expanduser().resolve()
    target_dir = Path(backup_dir).expanduser().resolve()

    if not source.exists():
        return None
    if source.is_dir():
        raise ValueError("Database path points to a directory.")
    if target_dir == source or source in target_dir.parents:
        raise ValueError("Backup directory must not be the database file or a child of it.")

    return backup_sqlite_database(
        source, target_dir, keep=max(1, int(keep)), logger=logger
    )


async def run_backup_loop(db_path: str | Path, backup_dir: str | Path, *,
                          logger: logging.Logger | None = None,
                          interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
                          retry_seconds: int = DEFAULT_RETRY_SECONDS,
                          keep: int = DEFAULT_RETENTION_COUNT) -> None:
    """Back up immediately, then daily; retry failures sooner.

    Cancellation is propagated so the application can shut down cleanly.
    """
    log = logger or logging.getLogger(__name__)
    interval = max(60, int(interval_seconds))
    retry = max(30, min(int(retry_seconds), interval))
    while True:
        try:
            path = await asyncio.to_thread(
                create_backup, db_path, backup_dir, keep=keep, logger=log
            )
            if path:
                log.info("Database backup completed: %s", path)
            else:
                log.warning("Database backup skipped because database file does not exist yet: %s", db_path)
            delay = interval
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Scheduled database backup failed")
            delay = retry
        await asyncio.sleep(delay)


def latest_backup(backup_dir: str | Path) -> Optional[dict]:
    """Return metadata for the newest snapshot without opening its contents."""
    directory = Path(backup_dir).expanduser()
    if not directory.is_dir():
        return None
    candidates = list(directory.glob("shop-*.sqlite3"))
    if not candidates:
        return None
    newest = max(candidates, key=lambda item: item.stat().st_mtime)
    stat_result = newest.stat()
    return {
        "path": str(newest),
        "size_bytes": int(stat_result.st_size),
        "modified_at": stat_result.st_mtime,
    }

"""SQLite-backed run history.

The state DB tracks one row per exporter invocation (started_at,
finished_at, status, paths, error). It is intentionally tiny: enough for
``harvester status`` and post-mortem inspection, nothing more.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Status values stored in the ``runs`` table. Kept as plain strings rather
# than an enum for trivial SQL compatibility.
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exporter TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    output_path TEXT,
    log_path TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_exporter ON runs(exporter, started_at DESC);
"""


def _now_iso() -> str:
    """Current UTC timestamp in ISO 8601, second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class State:
    """Thin wrapper around a SQLite file storing run history."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # ``isolation_level=None`` keeps each statement auto-committed; we
        # don't need transactions for these single-row writes.
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def start_run(self, exporter: str) -> int:
        """Insert a ``running`` row and return its id."""
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (exporter, started_at, status) VALUES (?, ?, ?)",
                (exporter, _now_iso(), STATUS_RUNNING),
            )
            run_id = cursor.lastrowid
            assert run_id is not None
            return run_id

    def finish_run(
        self,
        run_id: int,
        status: str,
        output_path: Optional[Path],
        log_path: Path,
        error: Optional[str] = None,
    ) -> None:
        """Update an in-progress run with its final status."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                   SET finished_at = ?,
                       status = ?,
                       output_path = ?,
                       log_path = ?,
                       error_message = ?
                 WHERE id = ?
                """,
                (
                    _now_iso(),
                    status,
                    str(output_path) if output_path is not None else None,
                    str(log_path),
                    error,
                    run_id,
                ),
            )

    def last_run(self, exporter: str) -> Optional[dict]:
        """Return the most recent run for ``exporter`` as a dict, or None."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, exporter, started_at, finished_at, status,
                       output_path, log_path, error_message
                  FROM runs
                 WHERE exporter = ?
                 ORDER BY started_at DESC, id DESC
                 LIMIT 1
                """,
                (exporter,),
            ).fetchone()
            return dict(row) if row is not None else None

    def find_success_run(
        self, exporter: str, output_path: Path
    ) -> Optional[tuple[str, str]]:
        """Return ``(started_at, finished_at)`` for the successful run that
        produced ``output_path``, or ``None`` if no such row exists.

        Used by the manifest writer to attach per-snapshot run metadata.
        Matches by exact string equality of the stored ``output_path``: the
        caller must pass the same absolute path that the runner recorded.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT started_at, finished_at
                  FROM runs
                 WHERE exporter = ?
                   AND status = ?
                   AND output_path = ?
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (exporter, STATUS_SUCCESS, str(output_path)),
            ).fetchone()
            if row is None:
                return None
            started = row["started_at"]
            finished = row["finished_at"]
            if started is None or finished is None:
                return None
            return (started, finished)

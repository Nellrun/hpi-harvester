"""Filesystem layout helpers: snapshot timestamps, paths and atomic moves.

The harvester writes one snapshot per exporter run. Snapshots are placed
under ``<output_root>/<exporter>/`` and named after a UTC timestamp. To make
partial writes invisible to consumers, we always materialise output under a
hidden ``.<name>.tmp`` path first and rename it on success.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from harvester.config import ExporterConfig

# Format chosen so it is safe to use as a filename on every common
# filesystem (no colons, no slashes), while still being lexicographically
# sortable.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H-%M-%S"


def utc_timestamp(now: Optional[datetime] = None) -> str:
    """Return a filesystem-safe UTC timestamp string for naming snapshots."""
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def service_dir(output_root: Path, exporter: ExporterConfig) -> Path:
    """Return the directory that holds all snapshots for one exporter."""
    return output_root / exporter.name


def log_file_path(log_dir: Path, exporter: ExporterConfig, timestamp: str) -> Path:
    """Return the per-run log file path."""
    return log_dir / exporter.name / f"{timestamp}.log"


def snapshot_paths(
    output_root: Path,
    exporter: ExporterConfig,
    timestamp: str,
) -> tuple[Path, Path]:
    """Return ``(tmp_path, final_path)`` for a snapshot of ``exporter``.

    The temporary path is hidden (leading dot) so that consumers scanning
    the directory ignore it. The final path is what callers should consume
    after the run completes successfully.
    """
    sdir = service_dir(output_root, exporter)
    mode = exporter.output.mode

    if mode == "stdout":
        ext = exporter.output.extension
        return (
            sdir / f".{timestamp}.{ext}.tmp",
            sdir / f"{timestamp}.{ext}",
        )

    # argument / cwd: the snapshot is either a single file (no extension —
    # the exporter decides) or a directory. The naming is identical in both
    # cases since the kind is recorded by the exporter's output config.
    return (
        sdir / f".{timestamp}.tmp",
        sdir / timestamp,
    )


def ensure_dir(path: Path) -> None:
    """Create ``path`` (and parents) if it does not exist."""
    path.mkdir(parents=True, exist_ok=True)


def cleanup_path(path: Path) -> None:
    """Remove a file or directory at ``path``, ignoring missing entries."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def atomic_promote(tmp_path: Path, final_path: Path) -> None:
    """Move ``tmp_path`` to ``final_path`` atomically.

    Both files and directories are supported. The destination's parent
    directory is created if missing. ``Path.rename`` is atomic on POSIX as
    long as source and destination live on the same filesystem, which is
    always true here because both paths are siblings.
    """
    ensure_dir(final_path.parent)
    tmp_path.rename(final_path)

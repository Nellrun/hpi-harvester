"""Public manifest (`_index.json`) writer.

Describes the content of a single exporter's service directory as a stable,
machine-readable contract for downstream consumers (HPI modules, analytical
scripts, MCP-backed assistants).

The directory itself is the source of truth; the manifest is an acceleration
layer. Harvester rebuilds it from scratch on every successful run — any
manual files with valid timestamp names are picked up automatically, and
spurious files (README.md, .DS_Store, ...) are ignored.

Atomicity: we write ``<service_dir>/._index.json.tmp`` then rename it over
``<service_dir>/_index.json``. Both paths are siblings of each other, so the
rename is atomic on POSIX. A kill between ``fsync`` and ``rename`` leaves the
old manifest untouched; the next successful run will overwrite the stale tmp.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from harvester.state import State
from harvester.storage import TIMESTAMP_FORMAT

logger = logging.getLogger(__name__)

# Public contract — bump on any breaking change to the manifest schema.
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "_index.json"
_MANIFEST_TMP_NAME = "._index.json.tmp"


def _iso_z(moment: datetime) -> str:
    """Format a UTC datetime as ISO 8601 with a ``Z`` suffix."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc).replace(microsecond=0)
    return moment.isoformat().replace("+00:00", "Z")


def _normalise_iso(stored: str) -> str:
    """Normalise an ISO timestamp string from the state DB to ``...Z`` form.

    ``state.py`` stores ``datetime.isoformat()`` output (``+00:00`` suffix).
    The manifest contract uses ``Z`` instead.
    """
    if stored.endswith("+00:00"):
        return stored[:-6] + "Z"
    return stored


def _parse_timestamp(candidate: str) -> bool:
    """Return True if ``candidate`` matches :data:`TIMESTAMP_FORMAT`."""
    try:
        datetime.strptime(candidate, TIMESTAMP_FORMAT)
    except ValueError:
        return False
    return True


def _directory_size(path: Path) -> int:
    """Sum of regular-file sizes in ``path`` (recursive, symlinks skipped)."""
    total = 0
    for entry in path.rglob("*"):
        # Skip symlinks outright — contract says we do not follow them, and
        # including their target's size would be misleading.
        if entry.is_symlink():
            continue
        if entry.is_file():
            total += entry.stat().st_size
    return total


def _build_snapshot_entry(
    path: Path,
    exporter_name: str,
    state: State,
) -> Optional[dict[str, Any]]:
    """Build one ``snapshots[]`` entry for ``path``, or return None to skip it.

    Skips anything whose filename cannot be parsed as a canonical timestamp —
    that's how we filter out README.md, .DS_Store and anything else a user
    might drop into the directory.
    """
    if path.is_file():
        # ``stem`` strips exactly one extension suffix, which is what we want
        # both for ``<timestamp>.json`` (stdout mode) and ``<timestamp>``
        # without extension (argument+file mode: stem == name).
        candidate = path.stem
        entry_type = "file"
        size_bytes = path.stat().st_size
    elif path.is_dir():
        candidate = path.name
        entry_type = "directory"
        size_bytes = _directory_size(path)
    else:
        # Broken symlink / socket / FIFO — not a snapshot we know how to
        # describe, skip silently.
        return None

    if not _parse_timestamp(candidate):
        return None

    entry: dict[str, Any] = {
        "timestamp": candidate,
        "path": path.name,
        "type": entry_type,
        "size_bytes": size_bytes,
    }

    run_meta = state.find_success_run(exporter_name, path)
    if run_meta is not None:
        started, finished = run_meta
        entry["run_started_at"] = _normalise_iso(started)
        entry["run_ended_at"] = _normalise_iso(finished)

    return entry


def _atomic_write(service_dir: Path, payload: dict[str, Any]) -> None:
    """Serialise ``payload`` to JSON and atomically replace the manifest."""
    tmp_path = service_dir / _MANIFEST_TMP_NAME
    final_path = service_dir / MANIFEST_FILENAME

    # ``sort_keys=True`` keeps byte-level reproducibility (useful for diffs in
    # review tooling) since the snapshot list is already ordered.
    data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    with tmp_path.open("w", encoding="utf-8") as fp:
        fp.write(data)
        fp.flush()
        os.fsync(fp.fileno())

    tmp_path.rename(final_path)


def update_manifest(
    service_dir: Path,
    exporter_name: str,
    state: State,
    *,
    now: Optional[datetime] = None,
) -> Path:
    """Rebuild ``<service_dir>/_index.json`` from directory contents + state DB.

    Always writes a manifest, even if ``service_dir`` is empty or contains no
    valid snapshots (``snapshots: []``). The caller is responsible for
    deciding whether to call this at all (see
    :func:`config.effective_write_manifest`).

    Returns the path of the freshly-written manifest.
    """
    if not service_dir.exists():
        # Nothing to describe. This can only happen if a consumer calls
        # update_manifest before the first snapshot was ever promoted; the
        # runner creates the directory before calling us so in practice we
        # never hit this branch.
        raise FileNotFoundError(f"service_dir does not exist: {service_dir}")

    entries: list[dict[str, Any]] = []
    for child in service_dir.iterdir():
        # Hidden paths (tmp manifests, stray ``.DS_Store``) and the manifest
        # itself are never candidates.
        if child.name.startswith(".") or child.name == MANIFEST_FILENAME:
            continue
        entry = _build_snapshot_entry(child, exporter_name, state)
        if entry is not None:
            entries.append(entry)

    entries.sort(key=lambda e: e["timestamp"])

    payload: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "exporter": exporter_name,
        "updated_at": _iso_z(now if now is not None else datetime.now(timezone.utc)),
        "snapshots": entries,
    }

    _atomic_write(service_dir, payload)
    return service_dir / MANIFEST_FILENAME

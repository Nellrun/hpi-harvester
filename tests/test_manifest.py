"""Tests for harvester.manifest."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from harvester.manifest import (
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    update_manifest,
)
from harvester.state import State


def _make_state(tmp_path: Path) -> State:
    return State(tmp_path / "state.db")


def _read_manifest(service_dir: Path) -> dict[str, Any]:
    return json.loads((service_dir / MANIFEST_FILENAME).read_text())


def _touch_snapshot_file(service_dir: Path, name: str, contents: str = "x") -> Path:
    service_dir.mkdir(parents=True, exist_ok=True)
    path = service_dir / name
    path.write_text(contents)
    return path


def _touch_snapshot_dir(service_dir: Path, name: str, files: dict[str, str]) -> Path:
    service_dir.mkdir(parents=True, exist_ok=True)
    path = service_dir / name
    path.mkdir()
    for fname, content in files.items():
        sub = path / fname
        sub.parent.mkdir(parents=True, exist_ok=True)
        sub.write_text(content)
    return path


# ---------------------------------------------------------------------------
# Minimal shapes
# ---------------------------------------------------------------------------


def test_empty_directory_yields_empty_snapshots(tmp_path: Path) -> None:
    service = tmp_path / "dummy"
    service.mkdir()
    state = _make_state(tmp_path)

    update_manifest(service, "dummy", state)
    data = _read_manifest(service)

    assert data["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert data["exporter"] == "dummy"
    assert data["snapshots"] == []
    assert data["updated_at"].endswith("Z")


def test_single_file_snapshot_entry_shape(tmp_path: Path) -> None:
    service = tmp_path / "lastfm"
    snap = _touch_snapshot_file(service, "2026-04-20T03-00-00.json", '{"ok": true}')
    state = _make_state(tmp_path)

    update_manifest(service, "lastfm", state)
    data = _read_manifest(service)

    assert len(data["snapshots"]) == 1
    entry = data["snapshots"][0]
    assert entry["timestamp"] == "2026-04-20T03-00-00"
    assert entry["path"] == "2026-04-20T03-00-00.json"
    assert entry["type"] == "file"
    assert entry["size_bytes"] == snap.stat().st_size
    # Manual file has no state.db row → run_*_at absent.
    assert "run_started_at" not in entry
    assert "run_ended_at" not in entry


def test_schema_version_is_one(tmp_path: Path) -> None:
    service = tmp_path / "dummy"
    service.mkdir()
    update_manifest(service, "dummy", _make_state(tmp_path))
    assert _read_manifest(service)["schema_version"] == 1


# ---------------------------------------------------------------------------
# Filtering: valid vs spurious entries
# ---------------------------------------------------------------------------


def test_mixed_content_filters_spurious_entries(tmp_path: Path) -> None:
    service = tmp_path / "lastfm"
    valid_a = _touch_snapshot_file(service, "2026-04-18T03-00-00.json", "a")
    valid_b = _touch_snapshot_file(service, "2026-04-20T03-00-00.json", "bb")
    # Spurious files: neither should appear in the manifest.
    _touch_snapshot_file(service, "README.md", "docs")
    _touch_snapshot_file(service, "notes.txt", "random")
    # Hidden leftover from an aborted write — ignored.
    _touch_snapshot_file(service, ".2026-04-22T03-00-00.json.tmp", "half")
    # Pre-existing manifest — ignored on rebuild.
    (service / MANIFEST_FILENAME).write_text('{"stale": true}')

    update_manifest(service, "lastfm", _make_state(tmp_path))
    data = _read_manifest(service)

    paths = [e["path"] for e in data["snapshots"]]
    assert paths == [valid_a.name, valid_b.name]


def test_snapshots_sorted_ascending(tmp_path: Path) -> None:
    service = tmp_path / "dummy"
    _touch_snapshot_file(service, "2026-04-20T03-00-00.json")
    _touch_snapshot_file(service, "2026-04-15T03-00-00.json")
    _touch_snapshot_file(service, "2026-04-18T03-00-00.json")

    update_manifest(service, "dummy", _make_state(tmp_path))
    entries = _read_manifest(service)["snapshots"]

    stamps = [e["timestamp"] for e in entries]
    assert stamps == sorted(stamps)
    # And the freshest comes last, as promised by the contract.
    assert stamps[-1] == "2026-04-20T03-00-00"


# ---------------------------------------------------------------------------
# Directory-mode snapshots
# ---------------------------------------------------------------------------


def test_directory_snapshot_size_sums_file_tree(tmp_path: Path) -> None:
    service = tmp_path / "dummy"
    # 3-byte + 5-byte files in a nested tree.
    _touch_snapshot_dir(
        service,
        "2026-04-20T03-00-00",
        {"a.txt": "abc", "sub/b.txt": "hello"},
    )

    update_manifest(service, "dummy", _make_state(tmp_path))
    entry = _read_manifest(service)["snapshots"][0]

    assert entry["type"] == "directory"
    assert entry["path"] == "2026-04-20T03-00-00"
    assert entry["size_bytes"] == 3 + 5


def test_directory_and_file_coexist(tmp_path: Path) -> None:
    service = tmp_path / "dummy"
    _touch_snapshot_file(service, "2026-04-18T03-00-00.json", "a")
    _touch_snapshot_dir(service, "2026-04-20T03-00-00", {"x.txt": "xyz"})

    update_manifest(service, "dummy", _make_state(tmp_path))
    entries = _read_manifest(service)["snapshots"]

    types = {e["path"]: e["type"] for e in entries}
    assert types == {
        "2026-04-18T03-00-00.json": "file",
        "2026-04-20T03-00-00": "directory",
    }


# ---------------------------------------------------------------------------
# Run metadata from state.db
# ---------------------------------------------------------------------------


def test_state_metadata_attached_when_run_recorded(tmp_path: Path) -> None:
    service = tmp_path / "dummy"
    snap = _touch_snapshot_file(service, "2026-04-20T03-00-00.json", "{}")
    state = _make_state(tmp_path)

    # Simulate a recorded successful run by driving the public API directly.
    run_id = state.start_run("dummy")
    state.finish_run(
        run_id=run_id,
        status="success",
        output_path=snap,
        log_path=tmp_path / "logs" / "dummy" / "2026-04-20T03-00-00.log",
    )

    update_manifest(service, "dummy", state)
    entry = _read_manifest(service)["snapshots"][0]

    assert "run_started_at" in entry
    assert "run_ended_at" in entry
    # Contract: ISO 8601 UTC with Z suffix, no ``+00:00``.
    assert entry["run_started_at"].endswith("Z")
    assert entry["run_ended_at"].endswith("Z")
    assert "+00:00" not in entry["run_started_at"]


def test_manual_snapshot_has_no_run_metadata(tmp_path: Path) -> None:
    """Files placed manually (no state.db row) must still appear — without run_*_at."""
    service = tmp_path / "dummy"
    _touch_snapshot_file(service, "2020-01-01T00-00-00.json", "historical")
    state = _make_state(tmp_path)

    update_manifest(service, "dummy", state)
    entry = _read_manifest(service)["snapshots"][0]

    assert entry["timestamp"] == "2020-01-01T00-00-00"
    assert entry["size_bytes"] > 0
    assert "run_started_at" not in entry
    assert "run_ended_at" not in entry


def test_failed_run_not_attached(tmp_path: Path) -> None:
    """Only ``success`` rows feed ``run_*_at``. A failed row for the same path is ignored."""
    service = tmp_path / "dummy"
    snap = _touch_snapshot_file(service, "2026-04-20T03-00-00.json", "{}")
    state = _make_state(tmp_path)

    run_id = state.start_run("dummy")
    state.finish_run(
        run_id=run_id,
        status="failed",
        output_path=snap,
        log_path=tmp_path / "logs" / "dummy" / "fail.log",
        error="boom",
    )

    update_manifest(service, "dummy", state)
    entry = _read_manifest(service)["snapshots"][0]

    assert "run_started_at" not in entry


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_manifest_write_is_atomic_via_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch ``Path.rename`` to simulate a kill between ``fsync`` and rename.

    Expected invariants:
    * the old ``_index.json`` remains unchanged on disk;
    * no partially-written ``_index.json`` exists;
    * the stale ``._index.json.tmp`` is left behind (will be overwritten on
      the next successful call).
    """
    service = tmp_path / "dummy"
    _touch_snapshot_file(service, "2026-04-20T03-00-00.json", "new")
    state = _make_state(tmp_path)

    # Seed a valid old manifest to prove it isn't truncated by the failed write.
    old_manifest = {"schema_version": 1, "exporter": "dummy", "sentinel": "old"}
    (service / MANIFEST_FILENAME).write_text(json.dumps(old_manifest))

    original_rename = Path.rename

    def fail_on_manifest_rename(self: Path, target: Any) -> Path:
        target_path = Path(target)
        if target_path.name == MANIFEST_FILENAME:
            raise OSError("simulated kill between fsync and rename")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_on_manifest_rename)

    with pytest.raises(OSError, match="simulated kill"):
        update_manifest(service, "dummy", state)

    # Old manifest still intact.
    assert json.loads((service / MANIFEST_FILENAME).read_text()) == old_manifest
    # Tmp left behind — benign, next successful call overwrites it.
    assert (service / "._index.json.tmp").exists()


def test_manifest_write_recovers_after_simulated_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a failed rename, the next successful call produces a clean manifest."""
    service = tmp_path / "dummy"
    _touch_snapshot_file(service, "2026-04-20T03-00-00.json", "new")
    state = _make_state(tmp_path)
    (service / MANIFEST_FILENAME).write_text(
        json.dumps({"schema_version": 1, "exporter": "dummy", "sentinel": "old"})
    )

    original_rename = Path.rename

    def fail_once(self: Path, target: Any) -> Path:
        target_path = Path(target)
        if target_path.name == MANIFEST_FILENAME:
            raise OSError("simulated kill")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fail_once)
    with pytest.raises(OSError):
        update_manifest(service, "dummy", state)

    # Undo the patch — next call should complete cleanly.
    monkeypatch.setattr(Path, "rename", original_rename)
    update_manifest(service, "dummy", state)

    data = _read_manifest(service)
    assert data["schema_version"] == 1
    assert data["exporter"] == "dummy"
    assert [e["timestamp"] for e in data["snapshots"]] == ["2026-04-20T03-00-00"]
    # Tmp was consumed by the successful rename.
    assert not (service / "._index.json.tmp").exists()


# ---------------------------------------------------------------------------
# Miscellaneous
# ---------------------------------------------------------------------------


def test_updated_at_reflects_passed_now(tmp_path: Path) -> None:
    service = tmp_path / "dummy"
    service.mkdir()
    moment = datetime(2026, 4, 21, 3, 2, 41, tzinfo=timezone.utc)

    update_manifest(service, "dummy", _make_state(tmp_path), now=moment)
    assert _read_manifest(service)["updated_at"] == "2026-04-21T03:02:41Z"


def test_stale_tmp_is_overwritten(tmp_path: Path) -> None:
    """Leftover ``._index.json.tmp`` from a prior aborted run must not block us."""
    service = tmp_path / "dummy"
    service.mkdir()
    (service / "._index.json.tmp").write_text("stale")

    update_manifest(service, "dummy", _make_state(tmp_path))

    assert not (service / "._index.json.tmp").exists()
    data = _read_manifest(service)
    assert data["exporter"] == "dummy"


def test_update_manifest_missing_service_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        update_manifest(tmp_path / "nope", "dummy", _make_state(tmp_path))

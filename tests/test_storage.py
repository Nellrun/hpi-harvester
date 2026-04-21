"""Tests for harvester.storage."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from harvester.config import ExporterConfig, OutputConfig
from harvester.storage import (
    TIMESTAMP_FORMAT,
    atomic_promote,
    cleanup_path,
    ensure_dir,
    log_file_path,
    service_dir,
    snapshot_paths,
    utc_timestamp,
)


def _exporter(mode: str, **output_kwargs) -> ExporterConfig:
    if mode == "stdout":
        output = OutputConfig(mode="stdout", extension=output_kwargs.get("extension", "json"))
        command = "echo hi"
    elif mode == "argument":
        output = OutputConfig(mode="argument", format=output_kwargs.get("format", "file"))
        command = "exporter --out {OUTPUT}"
    else:
        output = OutputConfig(mode="cwd", format=output_kwargs.get("format", "directory"))
        command = "exporter"
    return ExporterConfig(name="lastfm", command=command, schedule="0 3 * * *", output=output)


def test_utc_timestamp_format() -> None:
    moment = datetime(2026, 4, 20, 18, 35, 7, tzinfo=timezone.utc)
    assert utc_timestamp(moment) == "2026-04-20T18-35-07"


def test_utc_timestamp_now_is_parseable() -> None:
    ts = utc_timestamp()
    parsed = datetime.strptime(ts, TIMESTAMP_FORMAT)
    assert parsed.year >= 2024


def test_utc_timestamp_naive_input_assumed_utc() -> None:
    naive = datetime(2026, 4, 20, 18, 35, 7)
    assert utc_timestamp(naive) == "2026-04-20T18-35-07"


def test_utc_timestamp_converts_other_timezones() -> None:
    from datetime import timedelta

    tz = timezone(timedelta(hours=2))
    moment = datetime(2026, 4, 20, 20, 35, 7, tzinfo=tz)
    # 20:35 +02:00 == 18:35 UTC
    assert utc_timestamp(moment) == "2026-04-20T18-35-07"


def test_service_and_log_paths(tmp_path: Path) -> None:
    exporter = _exporter("stdout")
    assert service_dir(tmp_path, exporter) == tmp_path / "lastfm"
    assert log_file_path(tmp_path / "logs", exporter, "2026-04-20T18-35-07") == (
        tmp_path / "logs" / "lastfm" / "2026-04-20T18-35-07.log"
    )


def test_snapshot_paths_stdout(tmp_path: Path) -> None:
    exporter = _exporter("stdout", extension="json")
    tmp, final = snapshot_paths(tmp_path, exporter, "2026-04-20T18-35-07")
    assert tmp == tmp_path / "lastfm" / ".2026-04-20T18-35-07.json.tmp"
    assert final == tmp_path / "lastfm" / "2026-04-20T18-35-07.json"


def test_snapshot_paths_argument_file(tmp_path: Path) -> None:
    exporter = _exporter("argument", format="file")
    tmp, final = snapshot_paths(tmp_path, exporter, "2026-04-20T18-35-07")
    assert tmp == tmp_path / "lastfm" / ".2026-04-20T18-35-07.tmp"
    assert final == tmp_path / "lastfm" / "2026-04-20T18-35-07"


def test_snapshot_paths_cwd_directory(tmp_path: Path) -> None:
    exporter = _exporter("cwd", format="directory")
    tmp, final = snapshot_paths(tmp_path, exporter, "2026-04-20T18-35-07")
    assert tmp.name.startswith(".") and tmp.name.endswith(".tmp")
    assert final == tmp_path / "lastfm" / "2026-04-20T18-35-07"


def test_ensure_dir_creates_nested(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c"
    ensure_dir(target)
    assert target.is_dir()
    # Idempotent.
    ensure_dir(target)
    assert target.is_dir()


def test_cleanup_path_file(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("hi")
    cleanup_path(f)
    assert not f.exists()


def test_cleanup_path_dir(tmp_path: Path) -> None:
    d = tmp_path / "snapshot"
    d.mkdir()
    (d / "inner.txt").write_text("data")
    cleanup_path(d)
    assert not d.exists()


def test_cleanup_path_missing_is_noop(tmp_path: Path) -> None:
    cleanup_path(tmp_path / "does-not-exist")  # must not raise


def test_atomic_promote_file(tmp_path: Path) -> None:
    src = tmp_path / ".tmp-file.tmp"
    src.write_text("payload")
    dst = tmp_path / "snap" / "final.json"
    atomic_promote(src, dst)
    assert not src.exists()
    assert dst.read_text() == "payload"


def test_atomic_promote_directory(tmp_path: Path) -> None:
    src = tmp_path / ".tmp-dir.tmp"
    src.mkdir()
    (src / "f.txt").write_text("inner")
    dst = tmp_path / "snap" / "2026"
    atomic_promote(src, dst)
    assert not src.exists()
    assert (dst / "f.txt").read_text() == "inner"


def test_atomic_promote_missing_source_raises(tmp_path: Path) -> None:
    src = tmp_path / "missing.tmp"
    dst = tmp_path / "out"
    with pytest.raises(FileNotFoundError):
        atomic_promote(src, dst)

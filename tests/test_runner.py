"""Tests for harvester.runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harvester.config import ExporterConfig, HarvesterConfig, OutputConfig
from harvester.runner import (
    ExporterFailed,
    ExporterTimeout,
    run_exporter,
)
from harvester.state import State


def _make_config(tmp_path: Path, exporters: list[ExporterConfig]) -> HarvesterConfig:
    config = HarvesterConfig(
        output_root=tmp_path / "data",
        exporters=exporters,
    )
    return config.resolve_paths()


def _make_state(config: HarvesterConfig) -> State:
    assert config.state_db is not None
    return State(config.state_db)


def _list_dir(d: Path) -> list[str]:
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir())


# -----------------------------------------------------------------------------
# stdout mode
# -----------------------------------------------------------------------------


def test_stdout_mode_success_writes_snapshot(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        command="echo '{\"hello\": \"world\"}'",
        schedule="0 3 * * *",
        output=OutputConfig(mode="stdout", extension="json"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    final_path = run_exporter(exporter, config, state)
    assert final_path is not None
    assert final_path.exists()
    assert final_path.suffix == ".json"
    assert json.loads(final_path.read_text()) == {"hello": "world"}

    service = config.output_root / "dummy"
    files = _list_dir(service)
    assert len(files) == 1
    assert not any(name.endswith(".tmp") for name in files)
    assert not any(name.startswith(".") for name in files)


def test_stdout_mode_failure_leaves_no_tmp(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        # Print to stdout, then exit 1: tmp file is created and must be cleaned up.
        command="echo partial; exit 1",
        schedule="0 3 * * *",
        output=OutputConfig(mode="stdout", extension="json"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    with pytest.raises(ExporterFailed) as exc_info:
        run_exporter(exporter, config, state)
    assert exc_info.value.returncode == 1

    service = config.output_root / "dummy"
    assert _list_dir(service) == []  # no tmp, no final


def test_stdout_mode_failure_records_state(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        command="echo 'oh no, broken' >&2; exit 2",
        schedule="0 3 * * *",
        output=OutputConfig(mode="stdout", extension="json"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    with pytest.raises(ExporterFailed):
        run_exporter(exporter, config, state)

    last = state.last_run("dummy")
    assert last is not None
    assert last["status"] == "failed"
    assert last["output_path"] is None
    assert last["log_path"]
    assert "code 2" in (last["error_message"] or "")


def test_stdout_mode_records_success_in_state(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        command="echo '{\"x\": 1}'",
        schedule="0 3 * * *",
        output=OutputConfig(mode="stdout", extension="json"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    run_exporter(exporter, config, state)
    last = state.last_run("dummy")
    assert last is not None
    assert last["status"] == "success"
    assert last["output_path"]
    assert Path(last["output_path"]).exists()


def test_stdout_mode_passes_env_to_subprocess(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        command='printf "%s" "$MY_TEST_VALUE"',
        schedule="0 3 * * *",
        output=OutputConfig(mode="stdout", extension="txt"),
        env={"MY_TEST_VALUE": "hello-from-env"},
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    final_path = run_exporter(exporter, config, state)
    assert final_path.read_text() == "hello-from-env"


# -----------------------------------------------------------------------------
# argument mode
# -----------------------------------------------------------------------------


def test_argument_mode_file_success(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        command="sh -c 'printf payload > {OUTPUT}'",
        schedule="0 3 * * *",
        output=OutputConfig(mode="argument", format="file"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    final_path = run_exporter(exporter, config, state)
    assert final_path.is_file()
    assert final_path.read_text() == "payload"
    assert _list_dir(config.output_root / "dummy") == [final_path.name]


def test_argument_mode_directory_success(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        command="sh -c 'echo a > {OUTPUT}/a.txt && echo b > {OUTPUT}/b.txt'",
        schedule="0 3 * * *",
        output=OutputConfig(mode="argument", format="directory"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    final_path = run_exporter(exporter, config, state)
    assert final_path.is_dir()
    assert sorted(p.name for p in final_path.iterdir()) == ["a.txt", "b.txt"]


def test_argument_mode_failure_cleans_up(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        command="sh -c 'touch {OUTPUT} && exit 7'",
        schedule="0 3 * * *",
        output=OutputConfig(mode="argument", format="file"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    with pytest.raises(ExporterFailed):
        run_exporter(exporter, config, state)
    assert _list_dir(config.output_root / "dummy") == []


def test_argument_mode_directory_failure_cleans_up(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        command="sh -c 'echo partial > {OUTPUT}/half.txt && exit 7'",
        schedule="0 3 * * *",
        output=OutputConfig(mode="argument", format="directory"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    with pytest.raises(ExporterFailed):
        run_exporter(exporter, config, state)
    assert _list_dir(config.output_root / "dummy") == []


def test_argument_mode_success_but_no_file_is_failure(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        # Command references {OUTPUT} but never actually creates it.
        command="sh -c 'echo would-write-to {OUTPUT} >/dev/null'",
        schedule="0 3 * * *",
        output=OutputConfig(mode="argument", format="file"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    with pytest.raises(ExporterFailed):
        run_exporter(exporter, config, state)


# -----------------------------------------------------------------------------
# cwd mode
# -----------------------------------------------------------------------------


def test_cwd_mode_success(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        command="sh -c 'echo hi > out.txt'",
        schedule="0 3 * * *",
        output=OutputConfig(mode="cwd", format="directory"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    final_path = run_exporter(exporter, config, state)
    assert final_path.is_dir()
    assert (final_path / "out.txt").read_text().strip() == "hi"


def test_cwd_mode_failure_cleans_up_tempdir(tmp_path: Path) -> None:
    # Sanity: count harvester_dummy_* tempdirs before and after; failure
    # path must clean up after itself.
    import tempfile as _tempfile

    sysmp = Path(_tempfile.gettempdir())
    pre = list(sysmp.glob("harvester_dummy_*"))

    exporter = ExporterConfig(
        name="dummy",
        command="sh -c 'echo half > out.txt && exit 3'",
        schedule="0 3 * * *",
        output=OutputConfig(mode="cwd", format="directory"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    with pytest.raises(ExporterFailed):
        run_exporter(exporter, config, state)

    post = list(sysmp.glob("harvester_dummy_*"))
    # No new leaked tempdirs.
    assert len(post) <= len(pre)
    assert _list_dir(config.output_root / "dummy") == []


# -----------------------------------------------------------------------------
# timeout
# -----------------------------------------------------------------------------


def test_timeout_raises_and_cleans_up(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        command="sleep 5",
        schedule="0 3 * * *",
        output=OutputConfig(mode="stdout", extension="json"),
        timeout_seconds=1,
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    with pytest.raises(ExporterTimeout):
        run_exporter(exporter, config, state)

    assert _list_dir(config.output_root / "dummy") == []
    last = state.last_run("dummy")
    assert last is not None
    assert last["status"] == "timeout"


# -----------------------------------------------------------------------------
# log file
# -----------------------------------------------------------------------------


def test_log_file_is_written_on_success(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        command="echo hi >&2; echo '{}'",
        schedule="0 3 * * *",
        output=OutputConfig(mode="stdout", extension="json"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    run_exporter(exporter, config, state)

    assert config.log_dir is not None
    log_files = list((config.log_dir / "dummy").glob("*.log"))
    assert len(log_files) == 1
    text = log_files[0].read_text()
    assert "exporter   : dummy" in text
    assert "status     : success" in text
    assert "hi" in text  # stderr captured


def test_log_file_is_written_on_failure(tmp_path: Path) -> None:
    exporter = ExporterConfig(
        name="dummy",
        command="echo something-broke >&2; exit 5",
        schedule="0 3 * * *",
        output=OutputConfig(mode="stdout", extension="json"),
    )
    config = _make_config(tmp_path, [exporter])
    state = _make_state(config)

    with pytest.raises(ExporterFailed):
        run_exporter(exporter, config, state)

    assert config.log_dir is not None
    log_files = list((config.log_dir / "dummy").glob("*.log"))
    assert len(log_files) == 1
    text = log_files[0].read_text()
    assert "status     : failed" in text
    assert "returncode : 5" in text
    assert "something-broke" in text

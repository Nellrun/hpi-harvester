"""Tests for harvester.config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from harvester.config import (
    ExporterConfig,
    HarvesterConfig,
    OutputConfig,
    effective_write_manifest,
    load_config,
)


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "harvester.yaml"
    path.write_text(textwrap.dedent(body))
    return path


def test_load_valid_config(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        output_root: /data
        timezone: Europe/Madrid
        exporters:
          - name: lastfm
            command: lastfm-backup --user daniil
            schedule: "0 3 * * *"
            output:
              mode: stdout
              extension: json
        """,
    )
    config = load_config(path)
    assert config.output_root == Path("/data")
    assert config.timezone == "Europe/Madrid"
    assert config.log_dir == Path("/data/.harvester/logs")
    assert config.state_db == Path("/data/.harvester/state.db")
    assert len(config.exporters) == 1
    exporter = config.exporters[0]
    assert exporter.name == "lastfm"
    assert exporter.timeout_seconds == 1800
    assert exporter.output.mode == "stdout"
    assert exporter.output.extension == "json"


def test_explicit_log_dir_and_state_db_preserved(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        output_root: /data
        log_dir: /var/log/harvester
        state_db: /var/lib/harvester/state.db
        exporters:
          - name: lastfm
            command: lastfm-backup --user x
            schedule: "0 3 * * *"
            output: {mode: stdout, extension: json}
        """,
    )
    config = load_config(path)
    assert config.log_dir == Path("/var/log/harvester")
    assert config.state_db == Path("/var/lib/harvester/state.db")


def test_duplicate_exporter_names_rejected(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        output_root: /data
        exporters:
          - name: lastfm
            command: x
            schedule: "0 3 * * *"
            output: {mode: stdout, extension: json}
          - name: lastfm
            command: y
            schedule: "0 4 * * *"
            output: {mode: stdout, extension: json}
        """,
    )
    with pytest.raises(ValidationError, match="unique"):
        load_config(path)


def test_invalid_exporter_name_rejected() -> None:
    with pytest.raises(ValidationError):
        ExporterConfig(
            name="Bad Name!",
            command="x",
            schedule="0 3 * * *",
            output=OutputConfig(mode="stdout", extension="json"),
        )


def test_stdout_requires_extension() -> None:
    with pytest.raises(ValidationError, match="output.extension is required"):
        OutputConfig(mode="stdout")


def test_argument_requires_format() -> None:
    with pytest.raises(ValidationError, match="output.format is required"):
        OutputConfig(mode="argument")


def test_cwd_requires_format() -> None:
    with pytest.raises(ValidationError, match="output.format is required"):
        OutputConfig(mode="cwd")


def test_argument_mode_requires_output_placeholder() -> None:
    with pytest.raises(ValidationError, match=r"\{OUTPUT\}"):
        ExporterConfig(
            name="bad",
            command="exporter --no-placeholder",
            schedule="0 3 * * *",
            output=OutputConfig(mode="argument", format="file"),
        )


def test_argument_mode_accepts_command_with_placeholder() -> None:
    exporter = ExporterConfig(
        name="ok",
        command="exporter --out {OUTPUT}",
        schedule="0 3 * * *",
        output=OutputConfig(mode="argument", format="file"),
    )
    assert "{OUTPUT}" in exporter.command


def test_invalid_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        OutputConfig(mode="bogus")  # type: ignore[arg-type]


def test_env_var_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LASTFM_API_KEY", "secret-value")
    path = _write_yaml(
        tmp_path,
        """
        output_root: /data
        exporters:
          - name: lastfm
            command: 'lastfm-backup --api-key "$LASTFM_API_KEY"'
            schedule: "0 3 * * *"
            env:
              LASTFM_API_KEY: "${LASTFM_API_KEY}"
            output: {mode: stdout, extension: json}
        """,
    )
    config = load_config(path)
    assert config.exporters[0].env == {"LASTFM_API_KEY": "secret-value"}


def test_env_var_unset_leaves_placeholder_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("MISSING_KEY_FOR_HARVESTER_TEST", raising=False)
    path = _write_yaml(
        tmp_path,
        """
        output_root: /data
        exporters:
          - name: lastfm
            command: lastfm-backup --user x
            schedule: "0 3 * * *"
            env:
              MY_KEY: "${MISSING_KEY_FOR_HARVESTER_TEST}"
            output: {mode: stdout, extension: json}
        """,
    )
    with caplog.at_level("WARNING", logger="harvester.config"):
        config = load_config(path)
    assert config.exporters[0].env == {"MY_KEY": "${MISSING_KEY_FOR_HARVESTER_TEST}"}
    assert any("undefined shell variable" in r.message for r in caplog.records)


def test_write_manifest_defaults_to_false(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        output_root: /data
        exporters:
          - name: lastfm
            command: x
            schedule: "0 3 * * *"
            output: {mode: stdout, extension: json}
        """,
    )
    config = load_config(path)
    assert config.write_manifest is False
    assert config.exporters[0].write_manifest is None


def test_write_manifest_parsed_globally(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        output_root: /data
        write_manifest: true
        exporters:
          - name: lastfm
            command: x
            schedule: "0 3 * * *"
            output: {mode: stdout, extension: json}
        """,
    )
    config = load_config(path)
    assert config.write_manifest is True


def test_write_manifest_per_exporter_override(tmp_path: Path) -> None:
    path = _write_yaml(
        tmp_path,
        """
        output_root: /data
        write_manifest: true
        exporters:
          - name: with-manifest
            command: x
            schedule: "0 3 * * *"
            output: {mode: stdout, extension: json}
          - name: without-manifest
            command: y
            schedule: "0 4 * * *"
            output: {mode: stdout, extension: json}
            write_manifest: false
        """,
    )
    config = load_config(path)
    assert config.exporters[0].write_manifest is None
    assert config.exporters[1].write_manifest is False


def test_effective_write_manifest_inherits_when_none() -> None:
    exporter = ExporterConfig(
        name="e",
        command="x",
        schedule="0 3 * * *",
        output=OutputConfig(mode="stdout", extension="json"),
    )
    config_off = HarvesterConfig(
        output_root=Path("/data"), exporters=[exporter]
    )
    assert effective_write_manifest(config_off, exporter) is False

    config_on = HarvesterConfig(
        output_root=Path("/data"), write_manifest=True, exporters=[exporter]
    )
    assert effective_write_manifest(config_on, exporter) is True


def test_effective_write_manifest_per_exporter_wins() -> None:
    forced_on = ExporterConfig(
        name="on",
        command="x",
        schedule="0 3 * * *",
        output=OutputConfig(mode="stdout", extension="json"),
        write_manifest=True,
    )
    forced_off = ExporterConfig(
        name="off",
        command="x",
        schedule="0 3 * * *",
        output=OutputConfig(mode="stdout", extension="json"),
        write_manifest=False,
    )
    global_off = HarvesterConfig(
        output_root=Path("/data"), exporters=[forced_on, forced_off]
    )
    global_on = HarvesterConfig(
        output_root=Path("/data"), write_manifest=True, exporters=[forced_on, forced_off]
    )
    # Per-exporter override wins in both directions, regardless of the global.
    assert effective_write_manifest(global_off, forced_on) is True
    assert effective_write_manifest(global_off, forced_off) is False
    assert effective_write_manifest(global_on, forced_on) is True
    assert effective_write_manifest(global_on, forced_off) is False


def test_resolve_paths_idempotent() -> None:
    config = HarvesterConfig(
        output_root=Path("/data"),
        exporters=[
            ExporterConfig(
                name="x",
                command="echo hi",
                schedule="0 3 * * *",
                output=OutputConfig(mode="stdout", extension="json"),
            )
        ],
    )
    once = config.resolve_paths()
    twice = once.resolve_paths()
    assert once.log_dir == twice.log_dir
    assert once.state_db == twice.state_db

"""Pydantic models for the harvester YAML configuration."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# Matches ${VAR} or ${VAR_WITH_UNDERSCORES}, used to detect unexpanded
# placeholders left over after os.path.expandvars().
_ENV_PLACEHOLDER_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


class OutputConfig(BaseModel):
    """How an exporter writes its output.

    - ``stdout``: harvester captures stdout and writes it to a file with
      the configured ``extension``.
    - ``argument``: harvester substitutes ``{OUTPUT}`` in the command with a
      target path (file or directory, depending on ``format``).
    - ``cwd``: harvester runs the command in a temporary working directory
      and moves it to the final location on success.
    """

    mode: Literal["stdout", "argument", "cwd"]
    extension: Optional[str] = None
    format: Optional[Literal["file", "directory"]] = None

    @model_validator(mode="after")
    def _check_mode_specific_fields(self) -> OutputConfig:
        if self.mode == "stdout" and not self.extension:
            raise ValueError("output.extension is required when mode=stdout")
        if self.mode in ("argument", "cwd") and not self.format:
            raise ValueError("output.format is required when mode=argument/cwd")
        return self


class ExporterConfig(BaseModel):
    """Configuration for a single exporter to be run on a cron schedule."""

    name: str = Field(..., pattern=r"^[a-z0-9_-]+$")
    command: str
    schedule: str  # crontab syntax, e.g. "0 3 * * *"
    output: OutputConfig
    timeout_seconds: int = 1800
    env: dict[str, str] = Field(default_factory=dict)
    # Per-exporter override for HarvesterConfig.write_manifest. ``None`` means
    # "inherit the global value"; True/False force it regardless of the global.
    write_manifest: Optional[bool] = None

    @model_validator(mode="after")
    def _check_argument_placeholder(self) -> ExporterConfig:
        if self.output.mode == "argument" and "{OUTPUT}" not in self.command:
            raise ValueError(
                f"exporter '{self.name}': command must contain '{{OUTPUT}}' "
                f"placeholder when output.mode=argument"
            )
        return self


class HarvesterConfig(BaseModel):
    """Top-level harvester configuration loaded from YAML."""

    output_root: Path
    timezone: str = "UTC"
    log_dir: Optional[Path] = None
    state_db: Optional[Path] = None
    # Global default for the public ``_index.json`` manifest. Off by default so
    # existing deployments keep their behaviour; set to ``true`` once at least
    # one consumer depends on the manifest format.
    write_manifest: bool = False
    exporters: list[ExporterConfig]

    @field_validator("exporters")
    @classmethod
    def _unique_names(cls, v: list[ExporterConfig]) -> list[ExporterConfig]:
        names = [e.name for e in v]
        if len(names) != len(set(names)):
            raise ValueError("Exporter names must be unique")
        return v

    def resolve_paths(self) -> HarvesterConfig:
        """Fill in default values for ``log_dir`` and ``state_db``.

        Defaults live under ``<output_root>/.harvester/`` so that everything
        related to a harvester instance can be mounted as a single volume.
        """
        if self.log_dir is None:
            self.log_dir = self.output_root / ".harvester" / "logs"
        if self.state_db is None:
            self.state_db = self.output_root / ".harvester" / "state.db"
        return self


def _expand_env_in_exporters(exporters: list[dict]) -> None:
    """Expand ``${VAR}`` placeholders in every exporter's ``env`` mapping.

    Mutates ``exporters`` in place. Unset variables are left untouched (per
    ``os.path.expandvars`` semantics) but a warning is logged so that
    misconfiguration surfaces during ``validate-config``.
    """
    for exporter in exporters:
        env = exporter.get("env") or {}
        if not isinstance(env, dict):
            continue
        for key, value in list(env.items()):
            if not isinstance(value, str):
                continue
            expanded = os.path.expandvars(value)
            if _ENV_PLACEHOLDER_RE.search(expanded):
                logger.warning(
                    "Exporter %r: env var %r references undefined shell variable(s); "
                    "leaving value as-is: %r",
                    exporter.get("name", "<unnamed>"),
                    key,
                    expanded,
                )
            env[key] = expanded
        exporter["env"] = env


def effective_write_manifest(
    config: HarvesterConfig, exporter: ExporterConfig
) -> bool:
    """Resolve the manifest flag for ``exporter``.

    Per-exporter ``write_manifest`` takes precedence; when it is ``None``, the
    global :attr:`HarvesterConfig.write_manifest` applies.
    """
    if exporter.write_manifest is None:
        return config.write_manifest
    return exporter.write_manifest


def load_config(path: Path) -> HarvesterConfig:
    """Load and validate a harvester configuration from a YAML file."""
    with Path(path).open() as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(f"Top-level YAML in {path} must be a mapping")

    exporters = data.get("exporters")
    if isinstance(exporters, list):
        _expand_env_in_exporters(exporters)

    config = HarvesterConfig.model_validate(data)
    return config.resolve_paths()

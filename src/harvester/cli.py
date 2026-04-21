"""Typer-based CLI for hpi-harvester."""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from harvester.config import load_config
from harvester.logging_setup import setup_logging
from harvester.runner import HarvesterError, run_exporter
from harvester.scheduler import run_daemon
from harvester.state import State

# Default location of the YAML config inside the Docker image. The
# ``/config`` directory is declared as a volume in the Dockerfile so users
# can mount their own config there.
DEFAULT_CONFIG_PATH = Path("/config/harvester.yaml")

app = typer.Typer(
    add_completion=False,
    help="Personal data exports orchestrator. See README for details.",
)


@app.command()
def run(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", "-c", help="Path to harvester.yaml"
    ),
) -> None:
    """Start the harvester as a foreground daemon."""
    config = load_config(config_path)
    setup_logging(config)
    run_daemon(config)


@app.command("run-once")
def run_once(
    exporter_name: str = typer.Argument(..., help="Name of the exporter to run"),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", "-c", help="Path to harvester.yaml"
    ),
) -> None:
    """Run a single exporter once and exit (handy for debugging)."""
    config = load_config(config_path)
    setup_logging(config)
    exporter = next((e for e in config.exporters if e.name == exporter_name), None)
    if exporter is None:
        typer.echo(f"Exporter '{exporter_name}' not found", err=True)
        raise typer.Exit(1)
    assert config.state_db is not None
    state = State(config.state_db)
    try:
        path = run_exporter(exporter, config, state)
    except HarvesterError as e:
        typer.echo(f"Exporter '{exporter_name}' failed: {e}", err=True)
        raise typer.Exit(2)
    typer.echo(str(path))


@app.command()
def status(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", "-c", help="Path to harvester.yaml"
    ),
) -> None:
    """Show the most recent run for each configured exporter."""
    config = load_config(config_path)
    assert config.state_db is not None
    state = State(config.state_db)
    for exporter in config.exporters:
        last = state.last_run(exporter.name)
        if last is None:
            typer.echo(f"{exporter.name}: never ran")
            continue
        when = last.get("finished_at") or last.get("started_at")
        typer.echo(f"{exporter.name}: {last['status']} at {when}")


@app.command("validate-config")
def validate_config(
    config_path: Path = typer.Argument(..., help="Path to harvester.yaml"),
) -> None:
    """Validate a YAML config file without starting anything."""
    # Capture warnings (e.g. about unset env vars) so they actually surface
    # to the user during validation.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        load_config(config_path)
    except Exception as e:  # noqa: BLE001 — we want to convert any parse/validation error
        typer.echo(f"Invalid: {e}", err=True)
        raise typer.Exit(1)
    typer.echo("OK")


if __name__ == "__main__":
    app()

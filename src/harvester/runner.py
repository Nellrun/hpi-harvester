"""Subprocess driver: executes one exporter and writes a snapshot atomically.

The runner is the only place that touches the filesystem with side effects
during a scheduled run. It owns the ordering between
``state.start_run`` / ``state.finish_run``, snapshot promotion, and
per-run log emission, so failures in any of those stages still leave the
system in a sane state (no orphan ``.tmp`` files, no dangling ``running``
rows).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from harvester.config import ExporterConfig, HarvesterConfig
from harvester.state import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    State,
)
from harvester.storage import (
    atomic_promote,
    cleanup_path,
    ensure_dir,
    log_file_path,
    service_dir,
    snapshot_paths,
    utc_timestamp,
)

logger = logging.getLogger(__name__)

# Limit the size of stderr we attach to exception messages and state rows
# so they remain readable in CLI output. The full stderr is always written
# to the per-run log file unredacted.
_STDERR_PREVIEW_LIMIT = 500


class HarvesterError(Exception):
    """Base class for all errors raised by the runner."""


class ExporterFailed(HarvesterError):
    """Raised when the exporter exits with a non-zero return code."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"Exporter failed with code {returncode}: {stderr[:_STDERR_PREVIEW_LIMIT]}"
        )


class ExporterTimeout(HarvesterError):
    """Raised when the exporter does not finish within ``timeout_seconds``."""

    def __init__(self, timeout_seconds: int, stderr: str = "") -> None:
        self.timeout_seconds = timeout_seconds
        self.stderr = stderr
        super().__init__(f"Exporter timed out after {timeout_seconds}s")


def _build_env(exporter: ExporterConfig) -> dict[str, str]:
    """Compose the subprocess environment.

    The harvester's own env is inherited so that PATH and friends are
    available, then exporter-specific values are layered on top.
    """
    env = dict(os.environ)
    env.update(exporter.env)
    return env


def _format_log_header(
    exporter: ExporterConfig,
    command: str,
    env: dict[str, str],
    started_at: datetime,
) -> str:
    # Mask values of any env keys the exporter explicitly set: we treat them
    # as potentially secret (API keys etc.). Only the keys, not the values,
    # are recorded in the log.
    explicit_keys = sorted(exporter.env.keys())
    return (
        f"=== harvester run ===\n"
        f"exporter   : {exporter.name}\n"
        f"started_at : {started_at.isoformat()}\n"
        f"timeout    : {exporter.timeout_seconds}s\n"
        f"output.mode: {exporter.output.mode}\n"
        f"command    : {command}\n"
        f"env keys   : {', '.join(explicit_keys) if explicit_keys else '(inherit only)'}\n"
        f"--- stderr ---\n"
    )


def _format_log_footer(
    status: str,
    returncode: Optional[int],
    duration_s: float,
    finished_at: datetime,
    extra_stdout: Optional[bytes] = None,
) -> str:
    lines = ["--- end stderr ---\n"]
    if extra_stdout:
        lines.append("--- stdout ---\n")
        try:
            lines.append(extra_stdout.decode("utf-8", errors="replace"))
        except Exception:  # pragma: no cover - defensive only
            lines.append(repr(extra_stdout))
        if not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("--- end stdout ---\n")
    lines.append(
        f"finished_at: {finished_at.isoformat()}\n"
        f"duration   : {duration_s:.3f}s\n"
        f"returncode : {returncode if returncode is not None else 'n/a'}\n"
        f"status     : {status}\n"
    )
    return "".join(lines)


def _run_stdout_mode(
    exporter: ExporterConfig,
    tmp_path: Path,
    final_path: Path,
    env: dict[str, str],
) -> tuple[Path, bytes]:
    """Run exporter in stdout mode. Returns (final_path, stderr_bytes)."""
    ensure_dir(tmp_path.parent)
    with tmp_path.open("wb") as f:
        result = subprocess.run(
            exporter.command,
            stdout=f,
            stderr=subprocess.PIPE,
            shell=True,
            env=env,
            timeout=exporter.timeout_seconds,
        )
    if result.returncode != 0:
        cleanup_path(tmp_path)
        raise ExporterFailed(result.returncode, result.stderr.decode("utf-8", errors="replace"))
    atomic_promote(tmp_path, final_path)
    return final_path, result.stderr


def _run_argument_mode(
    exporter: ExporterConfig,
    tmp_path: Path,
    final_path: Path,
    env: dict[str, str],
) -> tuple[Path, bytes, bytes]:
    """Run exporter in argument mode. Returns (final_path, stdout, stderr)."""
    ensure_dir(tmp_path.parent)
    if exporter.output.format == "directory":
        tmp_path.mkdir(exist_ok=False)
    # For format='file' we do not pre-create tmp_path: the exporter is
    # expected to create the file itself (otherwise it might refuse to
    # overwrite it).

    command = exporter.command.replace("{OUTPUT}", str(tmp_path))
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            env=env,
            timeout=exporter.timeout_seconds,
        )
    except BaseException:
        cleanup_path(tmp_path)
        raise

    if result.returncode != 0:
        cleanup_path(tmp_path)
        raise ExporterFailed(result.returncode, result.stderr.decode("utf-8", errors="replace"))

    if not tmp_path.exists():
        # Exporter exited 0 but produced nothing — surface as failure rather
        # than silently promoting a missing snapshot.
        raise ExporterFailed(
            0,
            f"exporter exited successfully but did not write to {tmp_path}",
        )

    atomic_promote(tmp_path, final_path)
    return final_path, result.stdout, result.stderr


def _run_cwd_mode(
    exporter: ExporterConfig,
    final_path: Path,
    env: dict[str, str],
) -> tuple[Path, bytes, bytes]:
    """Run exporter in cwd mode. Returns (final_path, stdout, stderr)."""
    ensure_dir(final_path.parent)
    tmp_parent = tempfile.mkdtemp(prefix=f"harvester_{exporter.name}_")
    try:
        result = subprocess.run(
            exporter.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
            cwd=tmp_parent,
            env=env,
            timeout=exporter.timeout_seconds,
        )
        if result.returncode != 0:
            raise ExporterFailed(
                result.returncode, result.stderr.decode("utf-8", errors="replace")
            )
        # shutil.move handles cross-filesystem moves transparently; on the
        # common case (same fs) it degrades to an atomic rename.
        shutil.move(tmp_parent, final_path)
        tmp_parent = None  # mark as consumed so the finally block leaves it
        return final_path, result.stdout, result.stderr
    finally:
        if tmp_parent is not None and Path(tmp_parent).exists():
            shutil.rmtree(tmp_parent, ignore_errors=True)


def run_exporter(
    exporter: ExporterConfig,
    config: HarvesterConfig,
    state: State,
) -> Path:
    """Execute a single exporter, write a snapshot, log and update state.

    Returns the path of the produced snapshot on success, and re-raises any
    :class:`HarvesterError` (or :class:`subprocess.TimeoutExpired`) on
    failure after recording it in the per-run log and the state DB.
    """
    assert config.log_dir is not None and config.state_db is not None, (
        "config.resolve_paths() must be called before run_exporter"
    )

    timestamp = utc_timestamp()
    sdir = service_dir(config.output_root, exporter)
    ensure_dir(sdir)

    log_path = log_file_path(config.log_dir, exporter, timestamp)
    ensure_dir(log_path.parent)

    env = _build_env(exporter)
    started_at = datetime.now(timezone.utc)
    start_monotonic = time.monotonic()
    run_id = state.start_run(exporter.name)

    log_handle = log_path.open("w", encoding="utf-8")
    log_handle.write(_format_log_header(exporter, exporter.command, env, started_at))
    log_handle.flush()

    status = STATUS_FAILED
    final_path: Optional[Path] = None
    returncode: Optional[int] = None
    error_message: Optional[str] = None
    stderr_bytes: bytes = b""
    extra_stdout: Optional[bytes] = None

    try:
        if exporter.output.mode == "stdout":
            tmp_path, target_path = snapshot_paths(config.output_root, exporter, timestamp)
            final_path, stderr_bytes = _run_stdout_mode(exporter, tmp_path, target_path, env)
            returncode = 0
        elif exporter.output.mode == "argument":
            tmp_path, target_path = snapshot_paths(config.output_root, exporter, timestamp)
            final_path, extra_stdout, stderr_bytes = _run_argument_mode(
                exporter, tmp_path, target_path, env
            )
            returncode = 0
        elif exporter.output.mode == "cwd":
            _, target_path = snapshot_paths(config.output_root, exporter, timestamp)
            final_path, extra_stdout, stderr_bytes = _run_cwd_mode(
                exporter, target_path, env
            )
            returncode = 0
        else:  # pragma: no cover - validated by pydantic
            raise HarvesterError(f"unknown output mode: {exporter.output.mode}")

        status = STATUS_SUCCESS
        return final_path
    except subprocess.TimeoutExpired as e:
        # subprocess attaches whatever it had captured up to the timeout.
        stderr_bytes = e.stderr or b""
        # Best-effort cleanup for argument/stdout modes that leave a tmp
        # path behind on timeout. cwd mode handles this in its own finally.
        try:
            tmp_path  # type: ignore[has-type]
        except NameError:
            pass
        else:
            cleanup_path(tmp_path)
        status = STATUS_TIMEOUT
        error_message = f"timeout after {exporter.timeout_seconds}s"
        raise ExporterTimeout(exporter.timeout_seconds, stderr_bytes.decode("utf-8", errors="replace"))
    except ExporterFailed as e:
        stderr_bytes = e.stderr.encode("utf-8") if isinstance(e.stderr, str) else (e.stderr or b"")
        returncode = e.returncode
        status = STATUS_FAILED
        error_message = str(e)
        raise
    except Exception as e:
        # Unexpected error (e.g. shutil failure). Treat as failed.
        status = STATUS_FAILED
        error_message = f"{type(e).__name__}: {e}"
        raise
    finally:
        finished_at = datetime.now(timezone.utc)
        duration_s = time.monotonic() - start_monotonic
        try:
            if stderr_bytes:
                log_handle.write(stderr_bytes.decode("utf-8", errors="replace"))
                if not stderr_bytes.endswith(b"\n"):
                    log_handle.write("\n")
            log_handle.write(
                _format_log_footer(
                    status=status,
                    returncode=returncode,
                    duration_s=duration_s,
                    finished_at=finished_at,
                    extra_stdout=extra_stdout,
                )
            )
        finally:
            log_handle.close()
        try:
            state.finish_run(
                run_id=run_id,
                status=status,
                output_path=final_path if status == STATUS_SUCCESS else None,
                log_path=log_path,
                error=error_message,
            )
        except Exception:  # pragma: no cover - state DB issues should not mask the original error
            logger.exception("failed to update state for run_id=%s", run_id)

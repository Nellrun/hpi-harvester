"""Global logging configuration for the harvester process.

Two destinations are wired up here:

- ``stdout`` so that ``docker logs`` shows what the daemon is doing.
- A rotating file (``<log_dir>/harvester.log``) for longer-lived inspection.

Per-run exporter logs are produced by :mod:`harvester.runner` and live in
``<log_dir>/<exporter>/<timestamp>.log``; they are intentionally separate
from this global stream.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from harvester.config import HarvesterConfig

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def setup_logging(config: HarvesterConfig, level: int = logging.INFO) -> None:
    """Configure the root logger for the harvester process.

    Idempotent: removes any handlers that a previous call (e.g. another
    test) installed before adding fresh ones.
    """
    assert config.log_dir is not None, "config.resolve_paths() must be called first"
    config.log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    file_handler = RotatingFileHandler(
        filename=config.log_dir / "harvester.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # APScheduler is chatty at INFO. Bump it down a notch unless the user
    # has explicitly asked for DEBUG logs.
    if level > logging.DEBUG:
        logging.getLogger("apscheduler").setLevel(logging.WARNING)

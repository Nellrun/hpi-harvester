"""APScheduler-based daemon entrypoint.

One ``BlockingScheduler`` is created per process; one cron job is added per
exporter. ``max_instances=1`` makes sure a slow exporter does not get
re-launched on top of itself, while ``coalesce=True`` collapses any missed
firings (e.g. across container restarts) into a single run.
"""

from __future__ import annotations

import logging

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from harvester.config import ExporterConfig, HarvesterConfig
from harvester.runner import run_exporter
from harvester.state import State

logger = logging.getLogger(__name__)

# Grace window for missed firings. One hour is enough to absorb a container
# restart or a brief host outage without skipping a daily snapshot.
_MISFIRE_GRACE_SECONDS = 3600


def _run_with_error_handling(
    exporter: ExporterConfig,
    config: HarvesterConfig,
    state: State,
) -> None:
    """Wrap :func:`run_exporter` so a single failure cannot kill the daemon.

    ``run_exporter`` already records failures into the per-run log and the
    state DB; here we only need to catch the propagated exception so the
    scheduler keeps spinning.
    """
    try:
        run_exporter(exporter, config, state)
    except Exception:
        logger.exception("Exporter %s crashed", exporter.name)


def run_daemon(config: HarvesterConfig) -> None:
    """Start the blocking scheduler and run until interrupted."""
    assert config.state_db is not None, "config.resolve_paths() must be called first"

    tz = pytz.timezone(config.timezone)
    scheduler = BlockingScheduler(timezone=tz)
    state = State(config.state_db)

    for exporter in config.exporters:
        scheduler.add_job(
            func=_run_with_error_handling,
            trigger=CronTrigger.from_crontab(exporter.schedule, timezone=tz),
            args=[exporter, config, state],
            id=exporter.name,
            name=exporter.name,
            misfire_grace_time=_MISFIRE_GRACE_SECONDS,
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        logger.info("Scheduled %s: %s", exporter.name, exporter.schedule)

    logger.info("Harvester started with %d exporters", len(config.exporters))
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Harvester shutting down")

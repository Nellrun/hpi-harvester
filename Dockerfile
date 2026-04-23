FROM python:3.12-slim

# System dependencies. ``git`` is required so we can `pip install`
# exporters straight from GitHub, ``ca-certificates`` for HTTPS to APIs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the harvester itself first so dependency layers stay cached
# across changes to exporter installs below.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Exporters live in their own layer so adding/removing one does not
# invalidate the harvester install above.
#
# karlicoss/lastfm-backup ships a setup.py that imports the module to read
# its __version__, but the module does ``import backoff`` at the top.
# Because pip evaluates setup.py *before* installing any declared deps —
# and PEP 517 build isolation hides the outer environment from setup.py —
# the build aborts with ModuleNotFoundError unless we both pre-install
# backoff *and* disable build isolation so setup.py can see it. setuptools
# must be present too because we are turning off the isolated build env
# that would normally provide it.
RUN pip install --no-cache-dir backoff setuptools wheel \
 && pip install --no-cache-dir --no-build-isolation \
        git+https://github.com/karlicoss/lastfm-backup

# The upstream lastfm-backup tool reads USERNAME/API_KEY from a config.py
# in cwd and ships its script as ``lastfm_backup.py``. Install a thin
# wrapper that exposes the conventional ``lastfm-backup --user --api-key[-file]``
# CLI that our example config and README assume.
COPY docker/lastfm-backup /usr/local/bin/lastfm-backup
RUN chmod +x /usr/local/bin/lastfm-backup

# purarue/traktexport — OAuth-based Trakt.tv exporter. Prints the full
# account dump (history, ratings, watchlist, lists) to stdout. Unlike
# lastfm-backup it needs a writable creds file that it creates on first
# `traktexport auth` and refreshes on every run, so we pin the path to
# /state (a read-write mount declared below) and out of the read-only
# /secrets mount used for static keys.
RUN pip install --no-cache-dir traktexport
# Upstream traktexport 0.1.10 aborts the entire export if Trakt's
# ``users/<name>/stats`` endpoint returns 5xx — which it does, persistently,
# for accounts where the service has trouble computing aggregates. Patch
# ``stats`` to become an optional field so the rest of the export (history,
# watchlist, ratings, ...) still lands on disk.
RUN apt-get update && apt-get install -y --no-install-recommends patch \
 && rm -rf /var/lib/apt/lists/*
COPY docker/traktexport-stats-optional.patch /tmp/traktexport-stats-optional.patch
RUN cd "$(python -c 'import os, traktexport; print(os.path.dirname(traktexport.__file__))')" \
 && patch -p1 < /tmp/traktexport-stats-optional.patch \
 && rm /tmp/traktexport-stats-optional.patch
ENV TRAKTEXPORT_CFG=/state/traktexport.json

# ps-timetracker.com scraper — ships inside this repo under tools/.
# Requires the ``_my_app_session`` cookie value at /secrets/ps_timetracker.cookie
# and keeps its incremental cursor at /state/ps_timetracker.json.
COPY tools/ps_timetracker_export /tmp/ps_timetracker_export
RUN pip install --no-cache-dir /tmp/ps_timetracker_export && rm -rf /tmp/ps_timetracker_export

# Default volume mounts: configuration, read-only secrets, writable
# exporter state (OAuth refresh tokens et al.), and the snapshot tree.
VOLUME ["/config", "/secrets", "/state", "/data"]

# tini reaps zombie children spawned by the various exporters and forwards
# signals so ``docker stop`` shuts the daemon down cleanly.
ENTRYPOINT ["tini", "--", "harvester"]
CMD ["run", "--config", "/config/harvester.yaml"]

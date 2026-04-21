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

# Default volume mounts: configuration, secrets and the snapshot tree.
VOLUME ["/config", "/secrets", "/data"]

# tini reaps zombie children spawned by the various exporters and forwards
# signals so ``docker stop`` shuts the daemon down cleanly.
ENTRYPOINT ["tini", "--", "harvester"]
CMD ["run", "--config", "/config/harvester.yaml"]

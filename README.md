# hpi-harvester

A thin Python orchestrator that runs CLI exporters on a schedule and drops their output into a standardised filesystem layout. Designed to feed [HPI](https://github.com/karlicoss/HPI) (and any other tool that consumes raw exports) without coupling the orchestrator to any particular exporter — adding a new one is a YAML edit, not a code change.

The MVP ships with a single exporter wired up: Last.fm, via [`lastfm-backup`](https://github.com/karlicoss/lastfm-backup).

## Quickstart

```bash
git clone https://github.com/<you>/hpi-harvester.git
cd hpi-harvester
```

1. Get a Last.fm API key from <https://www.last.fm/api/account/create>.
2. Create the runtime directories (these are git-ignored):

   ```bash
   mkdir -p config secrets data
   ```

3. Choose one of two ways to feed the API key into the container.

   **a. File-based (recommended for shared machines):**

   ```bash
   echo "YOUR_LASTFM_API_KEY" > secrets/lastfm.key
   chmod 600 secrets/lastfm.key
   ```

4. Copy the example config and set your Last.fm username:

   ```bash
   cp examples/harvester.yaml config/harvester.yaml
   $EDITOR config/harvester.yaml   # replace YOUR_LASTFM_USERNAME
   ```

   **b. Env-based** alternative: uncomment the second exporter block in
   `config/harvester.yaml`, then create a `.env` file next to
   `docker-compose.yml`:

   ```bash
   echo "LASTFM_API_KEY=YOUR_LASTFM_API_KEY" > .env
   ```

5. Build and start the container:

   ```bash
   docker compose up -d
   docker compose logs -f harvester
   ```

   You should see `Scheduled lastfm: 0 3 * * *` and `Harvester started with 1 exporters`.

6. Trigger a one-off run to verify everything works without waiting for the next scheduled tick:

   ```bash
   docker compose exec harvester harvester run-once lastfm
   ```

7. The result lands in `./data/lastfm/<timestamp>.json`.

## Output structure

Everything lives under `output_root` (defaults to `/data` inside the container, `./data` on the host):

```
data/
├── lastfm/
│   ├── 2026-04-19T03-00-00.json
│   ├── 2026-04-20T03-00-00.json
│   └── ...
└── .harvester/
    ├── state.db                # SQLite, one row per run
    └── logs/
        ├── harvester.log       # global daemon log (rotating, 10MB × 5)
        └── lastfm/
            ├── 2026-04-19T03-00-00.log   # full stderr + metadata for that run
            └── ...
```

- Snapshot filenames use UTC timestamps in `YYYY-MM-DDTHH-MM-SS` form (no colons, lexicographically sortable).
- While a run is in progress, the output is written to a hidden `.<timestamp>.<ext>.tmp` path next to the destination. On success it is `rename(2)`-promoted; on failure it is unlinked. Consumers that ignore dotfiles (which is the standard convention) will therefore never see partial snapshots.

## Adding a new exporter

Drop a new block into the `exporters:` list. Three output modes are supported, depending on how the exporter wants to emit its data:

### `mode: stdout` — the exporter prints to stdout

The harvester captures stdout into `<output_root>/<name>/<timestamp>.<extension>`.

```yaml
- name: hackernews
  command: hn-export --user myhandle
  schedule: "30 4 * * *"
  output:
    mode: stdout
    extension: json
```

### `mode: argument` — the exporter takes an output path

The harvester substitutes `{OUTPUT}` in the command with a target path. Set `format: file` for a single-file export, or `format: directory` for a multi-file export.

```yaml
- name: spotify
  command: spotify-backup --out {OUTPUT}
  schedule: "0 5 * * *"
  output:
    mode: argument
    format: file

- name: instagram
  command: instaloader --dirname-pattern {OUTPUT} --no-metadata-json myhandle
  schedule: "0 6 * * 0"
  output:
    mode: argument
    format: directory
```

### `mode: cwd` — the exporter writes into the current working directory

The harvester runs the command in a fresh tempdir and moves it to the final location on success. Useful for tools that don't accept an output path argument.

```yaml
- name: gh-stars
  command: gh-stars-backup --user myhandle
  schedule: "0 7 * * 1"
  output:
    mode: cwd
    format: directory
```

### Secrets

Two patterns work out of the box:

- Mount a file into `/secrets/` and reference it from the command (`--api-key-file /secrets/foo.key`). Files in `./secrets/` are mounted read-only.
- Pass an environment variable through `docker-compose.yml` and reference it from the exporter's `env:` block. Values in `env:` may use `${VAR}` placeholders that are expanded against the harvester process's environment at config-load time. Unset variables are left as-is and a warning is logged so misconfiguration is loud.

## A note on the Last.fm exporter

The bundled `lastfm-backup` is a thin wrapper (`docker/lastfm-backup`) around [`karlicoss/lastfm-backup`](https://github.com/karlicoss/lastfm-backup). The upstream tool reads `USERNAME` / `API_KEY` from a `config.py` in the working directory and exposes no command-line flags; the wrapper translates `--user` and `--api-key`/`--api-key-file` into that contract by writing a temporary `config.py` and exec'ing the upstream script.

The upstream tool also has no incremental mode — every run re-downloads every page of scrobbles from the Last.fm API. For accounts with a long history (10k+ plays) a single run can take 30+ minutes; set `timeout_seconds:` accordingly.

## Troubleshooting

- **Validate the config without starting the daemon:**
  ```bash
  docker compose run --rm harvester harvester validate-config /config/harvester.yaml
  ```
- **Inspect the global daemon log:**
  ```bash
  docker compose logs harvester                   # stdout, last hour
  cat data/.harvester/logs/harvester.log          # full file, rotated
  ```
- **Inspect a single failed run:**
  ```bash
  ls data/.harvester/logs/lastfm/
  cat data/.harvester/logs/lastfm/<timestamp>.log # full stderr from the exporter
  ```
- **Check status of all exporters:**
  ```bash
  docker compose exec harvester harvester status
  ```
- **Run an exporter once for debugging (bypasses cron):**
  ```bash
  docker compose exec harvester harvester run-once <exporter-name>
  ```

## Integration with HPI

Point your HPI config at the snapshot directory. For Last.fm:

```python
# ~/.config/my/config/__init__.py
from pathlib import Path

class lastfm:
    export_path = Path('/path/to/hpi-harvester/data/lastfm')  # all *.json files
```

`my.lastfm.gdpr` (and similar modules) will pick up every snapshot under that directory.

## Development

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
pytest
```

## What is intentionally not in MVP

The following items are deliberately out of scope; they will land in a v2 if and when they prove necessary:

- Notifications (Telegram / email / Slack)
- Snapshot rotation (`keep_last`)
- Snapshot deduplication by content hash
- Web UI / dashboard
- Automatic retries on failure (one attempt per scheduled tick — the next tick is the retry)
- Incremental exports (passing the previous snapshot path into the command)
- Health checks for Docker
- Prometheus metrics

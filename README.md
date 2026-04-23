# hpi-harvester

A thin Python orchestrator that runs CLI exporters on a schedule and drops their output into a standardised filesystem layout. Designed to feed [HPI](https://github.com/karlicoss/HPI) (and any other tool that consumes raw exports) without coupling the orchestrator to any particular exporter — adding a new one is a YAML edit, not a code change.

Bundled exporters:

- **Last.fm** via [`lastfm-backup`](https://github.com/karlicoss/lastfm-backup) (API-key based).
- **Trakt.tv** via [`traktexport`](https://github.com/purarue/traktexport) (OAuth; requires a one-off auth bootstrap — see [Trakt setup](#trakt-setup)).
- **PlayStation playtime** via [`ps-timetracker-export`](tools/ps_timetracker_export) — scrapes [ps-timetracker.com](https://ps-timetracker.com/), which monitors PSN presence through a friend-bot (no PSN password or NPSSO required, only a browser session cookie). Tracks sessions, aggregate library, trophies-adjacent metadata. See [PS-Timetracker setup](#ps-timetracker-setup).

## Quickstart

```bash
git clone https://github.com/<you>/hpi-harvester.git
cd hpi-harvester
```

1. Get a Last.fm API key from <https://www.last.fm/api/account/create>.
2. Create the runtime directories (these are git-ignored):

   ```bash
   mkdir -p config secrets state data
   ```

   `state/` is the writable home for exporter runtime state (OAuth refresh tokens, cursors). It's separate from `secrets/` (read-only static keys) because tools like `traktexport` rewrite their creds on every run.

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

## Trakt setup

[`traktexport`](https://github.com/purarue/traktexport) authenticates over OAuth, not a static API key, which means the very first export needs a human-in-the-loop step: create a Trakt app, authorise it against your account, and hand the PIN back. After that, runs are fully unattended — the stored refresh token is used to mint a new access token on every export, and the creds file rotates itself in place.

Rather than wrestle with an interactive TTY inside Docker, do the auth once **on your host** and drop the resulting JSON into `./state/`. It's the same file `traktexport` would have produced inside the container, and we've pointed the container at that exact path via `TRAKTEXPORT_CFG=/state/traktexport.json` in the `Dockerfile`.

1. Create a Trakt OAuth app at <https://trakt.tv/oauth/applications>.
   - **Name:** anything (e.g. `hpi-harvester`).
   - **Redirect URI:** `urn:ietf:wg:oauth:2.0:oob` (literal — this tells Trakt to hand you a PIN instead of redirecting to a URL).
   - Save; note the **Client ID** and **Client Secret** on the next screen.

2. Install `traktexport` on your host and run `auth` once. It'll prompt for the Client ID / Client Secret from step 1, then open a browser tab where you authorise the app and copy a PIN back into the terminal:

   ```bash
   pip install --user traktexport
   traktexport auth YOUR_TRAKT_USERNAME
   ```

   On success the tokens land at `~/.local/share/traktexport.json` (on Linux / macOS; on other platforms `traktexport` prints the path).

3. Copy the creds into the repo's `./state/` directory so the container can read them:

   ```bash
   mkdir -p state
   cp ~/.local/share/traktexport.json state/traktexport.json
   ```

4. Edit `config/harvester.yaml` and replace `YOUR_TRAKT_USERNAME` in the `trakt` block with your actual Trakt username. Rebuild the image (the `traktexport` CLI is baked into it) and verify with a one-off run:

   ```bash
   docker compose build
   docker compose up -d --force-recreate       # only needed if the daemon is already running
   docker compose exec harvester harvester run-once trakt
   ls data/trakt/
   ```

   The scheduled daemon picks up the new exporter at startup, so if you were already running it before this change, `up -d --force-recreate` is what restarts it against the new image + new config.

**Note on the export payload.** `traktexport export` returns the entire account state — history (watched episodes/movies), ratings, watchlist, custom lists, profile metadata — as a single JSON blob on every run. There's no incremental mode; expect each snapshot to be 1–50 MB depending on how much you've watched.

**Note on token rotation.** After the initial bootstrap you never touch `state/traktexport.json` again — `traktexport` rewrites it on every run so the refresh token in it stays valid indefinitely. If that file ever gets deleted or corrupted, repeat steps 2–3 to regenerate it; you don't need to create a new OAuth app.

## PS-Timetracker setup

[ps-timetracker.com](https://ps-timetracker.com/) is a third-party site that tracks PSN playtime by running a bot (`TRACK_horse`) that adds you as a friend and logs the games shown on your online-presence feed. No PSN password, NPSSO token, or official PSN API is involved — the service only sees what any PSN friend would see. Register on the site, add the bot as a friend on PSN, and play a session so the first data points land.

The bundled `ps-timetracker-export` tool authenticates with a single browser cookie (`_my_app_session`), paginates `/profile/<name>/playtimes`, parses the HTML into JSON, and archives the raw HTML alongside the parsed output so you can re-parse older snapshots if the site's layout changes. It keeps its own incremental cursor in `state/ps_timetracker.json` — on each run it stops paginating as soon as it encounters rows already seen previously.

1. Log in to ps-timetracker.com in your browser.
2. Open DevTools → Application → Cookies → `https://ps-timetracker.com` and copy the value of the `_my_app_session` cookie.
3. Choose one of two ways to feed the cookie into the container.

   **a. File-based:**

   ```bash
   printf '%s' 'PASTE_THE_COOKIE_VALUE_HERE' > secrets/ps_timetracker.cookie
   chmod 600 secrets/ps_timetracker.cookie
   ```

   The tool also accepts the form `_my_app_session=<value>` so you can paste a raw `Cookie:` header fragment without editing it.

   **b. Env-based:** add the cookie to your `.env` next to `docker-compose.yml`:

   ```bash
   echo "PS_TIMETRACKER_COOKIE=PASTE_THE_COOKIE_VALUE_HERE" >> .env
   ```

   Then uncomment the "Variant 2" block for `ps_timetracker` in `config/harvester.yaml` and comment out Variant 1. Make sure `PS_TIMETRACKER_COOKIE` is forwarded to the harvester container in `docker-compose.yml` (same pattern as `LASTFM_API_KEY`).

4. In `config/harvester.yaml`, replace `YOUR_PSN_NAME` in the `ps_timetracker` block with your PSN profile name as it appears on ps-timetracker.com.

5. Rebuild and run once:

   ```bash
   docker compose build
   docker compose up -d --force-recreate
   docker compose exec harvester harvester run-once ps_timetracker
   ls data/ps_timetracker/
   ```

   The first run is a full scan (follows the `rel="next"` link through every page of session history); subsequent runs are short because the incremental cursor stops pagination after the first page that's fully known.

**Snapshot layout.** Unlike Last.fm and Trakt, each ps-timetracker snapshot is a directory, not a single file:

```
data/ps_timetracker/<timestamp>/
├── raw/
│   ├── profile.html
│   └── playtimes_p1.html, playtimes_p2.html, ...
├── library.json       # per-game aggregates from the profile landing page
├── sessions.jsonl     # new sessions for this run (one JSON object per line)
└── meta.json          # run metadata (pages fetched, stop reason, cursor values)
```

Sessions are append-only across snapshots: each run's `sessions.jsonl` holds *only* what was new since the previous run. Deduplicate by `playtime_id` when you consume them. The raw HTML is kept so you can re-parse historical snapshots with a newer parser if the site changes its markup.

**Cookie rotation.** The `_my_app_session` cookie is long-lived (months) but will eventually expire. When it does, the exporter exits with code 2 and writes `auth error: ... returned the login form` to its per-run log. Re-extract the cookie from the browser and overwrite the secret (or `.env` value). Delete `state/ps_timetracker.json` first if you want to force a full re-scan rather than just pick up where you left off.

**Timezone note.** The `start_local` / `end_local` / `last_played_local` fields are recorded verbatim from what ps-timetracker renders, which is the account's local time (not UTC). If your HPI consumer needs UTC, convert using the PSN profile's configured timezone — it is not included in the HTML.

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

Point your HPI config at the snapshot directory. For Last.fm and Trakt:

```python
# ~/.config/my/config/__init__.py
from pathlib import Path

HARVEST = Path('/path/to/hpi-harvester/data')

class lastfm:
    export_path = HARVEST / 'lastfm'  # all *.json files

class trakt:
    export_path = HARVEST / 'trakt'   # all *.json files (consumed by purarue/HPI's my.trakt.export)
```

`my.lastfm.gdpr` and [`my.trakt.export`](https://github.com/purarue/HPI) (and similar modules) will pick up every snapshot under the respective directory.

For `ps_timetracker`, snapshots are directories rather than single JSON files, so an HPI module has to glob for inner files:

```python
class ps_timetracker:
    export_path = HARVEST / 'ps_timetracker'      # <timestamp>/sessions.jsonl, <timestamp>/library.json
```

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

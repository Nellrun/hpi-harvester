"""CLI: scrape ps-timetracker.com for one profile into an hpi-harvester snapshot dir.

Output layout (inside the directory passed as ``--output``):
    raw/playtimes_p{N}.html    raw pages as fetched (archived for re-parsing)
    raw/profile.html           raw profile landing page
    sessions.jsonl             parsed new sessions (one JSON object per line)
    library.json               parsed library snapshot from profile page
    meta.json                  run metadata

State file (``--state-file``) carries the highest ``playtime_id`` seen so far
between runs. The scraper paginates newest-first and stops as soon as it
encounters rows that are already covered by the state, bounded by ``--max-pages``
as a safety net. Removing the state file forces a full re-scan.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from ps_timetracker_export.parser import (
    AuthRequired,
    has_next_page,
    looks_like_login,
    parse_library,
    parse_sessions,
)

DEFAULT_BASE_URL = "https://ps-timetracker.com"
DEFAULT_USER_AGENT = "hpi-harvester/ps-timetracker-export (+https://github.com/)"
SESSION_COOKIE_NAME = "_my_app_session"


def _log(msg: str) -> None:
    # stderr so the harvester captures it into the per-run log; stdout is reserved
    # for exporters that write data to it (this tool uses --output instead).
    print(msg, file=sys.stderr, flush=True)


def _normalize_cookie(raw: str) -> str:
    raw = raw.strip()
    # Accept either the bare value or "_my_app_session=..." for convenience.
    if raw.startswith(f"{SESSION_COOKIE_NAME}="):
        raw = raw.split("=", 1)[1].strip()
    return raw


def _resolve_cookie(cookie_value: Optional[str], cookie_file: Optional[Path]) -> str:
    # Exactly one source must be supplied. Reject both-or-neither in the CLI
    # layer rather than down the stack so the error message points at the user.
    sources = [s for s in (cookie_value, cookie_file) if s]
    if len(sources) != 1:
        raise SystemExit(
            "exactly one of --cookie or --cookie-file must be provided"
        )
    if cookie_value is not None:
        normalized = _normalize_cookie(cookie_value)
    else:
        assert cookie_file is not None
        normalized = _normalize_cookie(cookie_file.read_text(encoding="utf-8"))
    if not normalized:
        raise SystemExit("cookie value is empty")
    return normalized


def _load_state(path: Optional[Path]) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"state file {path} is not valid JSON: {e}")


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _build_session(cookie_value: str, user_agent: str) -> requests.Session:
    s = requests.Session()
    s.cookies.set(SESSION_COOKIE_NAME, cookie_value, domain="ps-timetracker.com", path="/")
    s.headers.update({"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"})
    return s


def _fetch(session: requests.Session, url: str, timeout: float) -> str:
    resp = session.get(url, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    if looks_like_login(html):
        raise AuthRequired(
            f"GET {url} returned the login form — cookie is missing or expired"
        )
    return html


def run(
    output: Path,
    profile: str,
    cookie: str,
    state_file: Optional[Path],
    base_url: str,
    user_agent: str,
    max_pages: int,
    delay_seconds: float,
    request_timeout: float,
    full: bool,
) -> int:
    if not output.exists():
        # argument-mode directory format: harvester pre-creates this, but allow
        # standalone invocation for testing.
        output.mkdir(parents=True, exist_ok=True)
    raw_dir = output / "raw"
    raw_dir.mkdir(exist_ok=True)

    state = _load_state(state_file) if not full else {}
    last_seen_before: Optional[int] = state.get("last_seen_playtime_id") if not full else None

    session = _build_session(cookie, user_agent)

    # 1) Profile landing page — full library snapshot.
    profile_url = f"{base_url}/profile/{profile}"
    _log(f"GET {profile_url}")
    profile_html = _fetch(session, profile_url, request_timeout)
    (raw_dir / "profile.html").write_text(profile_html, encoding="utf-8")
    library = parse_library(profile_html)
    (output / "library.json").write_text(
        json.dumps(library, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _log(f"library: {len(library)} games")

    # 2) Paginate /playtimes newest-first, collecting new sessions.
    new_sessions: list[dict] = []
    max_id_seen: Optional[int] = last_seen_before
    stopped_reason = "no_more_pages"
    pages_fetched = 0

    for page in range(1, max_pages + 1):
        url = f"{base_url}/profile/{profile}/playtimes?page={page}"
        _log(f"GET {url}")
        html = _fetch(session, url, request_timeout)
        (raw_dir / f"playtimes_p{page}.html").write_text(html, encoding="utf-8")
        pages_fetched += 1

        rows = parse_sessions(html)
        if not rows:
            stopped_reason = "empty_page"
            break

        page_has_known = False
        for row in rows:
            pid = row["playtime_id"]
            if max_id_seen is None or pid > max_id_seen:
                max_id_seen = pid
            if last_seen_before is not None and pid <= last_seen_before:
                page_has_known = True
                continue
            new_sessions.append(row)

        if page_has_known:
            # Reached rows already covered by a previous run.
            stopped_reason = "hit_known_playtime_id"
            break

        if not has_next_page(html):
            stopped_reason = "no_next_link"
            break

        if delay_seconds > 0:
            time.sleep(delay_seconds)
    else:
        stopped_reason = "max_pages_reached"

    # 3) Persist outputs.
    with (output / "sessions.jsonl").open("w", encoding="utf-8") as f:
        for row in new_sessions:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")

    now = datetime.now(timezone.utc)
    meta = {
        "profile": profile,
        "base_url": base_url,
        "fetched_at_utc": now.isoformat(),
        "incremental": last_seen_before is not None,
        "full": full,
        "pages_fetched": pages_fetched,
        "stopped_reason": stopped_reason,
        "new_sessions_count": len(new_sessions),
        "library_games_count": len(library),
        "last_seen_playtime_id_before": last_seen_before,
        "last_seen_playtime_id_after": max_id_seen,
    }
    (output / "meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
    )

    # 4) Update state last so a crash mid-run does not hide unfetched data on
    # the next invocation.
    if state_file is not None and max_id_seen is not None:
        new_state = {
            "profile": profile,
            "last_seen_playtime_id": max_id_seen,
            "last_run_at_utc": now.isoformat(),
        }
        _save_state(state_file, new_state)

    _log(
        f"done: +{len(new_sessions)} new sessions, {pages_fetched} pages, "
        f"stopped_reason={stopped_reason}, max_id={max_id_seen}"
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ps-timetracker-export")
    parser.add_argument("--output", required=True, type=Path, help="Output directory")
    parser.add_argument("--profile", required=True, help="PSN profile name on ps-timetracker")
    cookie_group = parser.add_mutually_exclusive_group(required=True)
    cookie_group.add_argument(
        "--cookie",
        default=None,
        help=(
            f"{SESSION_COOKIE_NAME} cookie value passed directly (suitable for "
            f"shell-expanded env vars like \"$PS_TIMETRACKER_COOKIE\")"
        ),
    )
    cookie_group.add_argument(
        "--cookie-file",
        type=Path,
        default=None,
        help=f"File containing the {SESSION_COOKIE_NAME} cookie value",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="JSON state file (tracks highest seen playtime_id across runs)",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=200,
        help="Hard ceiling on paginated requests per run",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.75,
        help="Seconds to sleep between page requests",
    )
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore state file and crawl every page up to --max-pages",
    )
    args = parser.parse_args(argv)

    cookie = _resolve_cookie(args.cookie, args.cookie_file)

    try:
        return run(
            output=args.output,
            profile=args.profile,
            cookie=cookie,
            state_file=args.state_file,
            base_url=args.base_url.rstrip("/"),
            user_agent=args.user_agent,
            max_pages=args.max_pages,
            delay_seconds=args.delay,
            request_timeout=args.request_timeout,
            full=args.full,
        )
    except AuthRequired as e:
        _log(f"auth error: {e}")
        return 2
    except requests.RequestException as e:
        _log(f"http error: {e}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI: fetch one /api/player snapshot from the gwm-stats aggregator.

The service exposes a single endpoint that returns a JSON blob with totals,
top games, sessions log, trophies and platform breakdown for one player
(identified by ``name`` plus a list of cross-platform ``aliases``).

The exporter writes that JSON to stdout — harvester's ``output.mode=stdout``
persists it as ``<service_dir>/<timestamp>.json`` atomically.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

import requests

DEFAULT_BASE_URL = "https://gowithme.club/"
DEFAULT_ENDPOINT = "/api/player"
DEFAULT_USER_AGENT = "hpi-harvester/gwm-stats-export (+https://github.com/)"
DEFAULT_TIMEOUT = 60.0


def _log(msg: str) -> None:
    # stderr so the harvester per-run log captures it; stdout carries the JSON.
    print(msg, file=sys.stderr, flush=True)


def _split_aliases(raw: str) -> list[str]:
    # Accept comma-separated values and trim whitespace. Empty entries are
    # dropped so trailing commas don't poison the query.
    return [a.strip() for a in raw.split(",") if a.strip()]


def fetch(
    name: str,
    aliases: list[str],
    base_url: str,
    endpoint: str,
    user_agent: str,
    timeout: float,
    verify_tls: bool,
) -> dict:
    url = f"{base_url.rstrip('/')}{endpoint}"
    params = [("name", name)]
    # The API accepts repeated `aliases=` params *or* a single comma-joined
    # value. The latter matches the curl example the user shared, and keeps
    # us off the "what if requests reorders params" path.
    params.append(("aliases", ",".join(aliases) if aliases else name))
    _log(f"GET {url} name={name!r} aliases={aliases!r}")
    resp = requests.get(
        url,
        params=params,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
        timeout=timeout,
        verify=verify_tls,
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError as e:
        raise SystemExit(f"response was not valid JSON: {e}\nbody[:500]={resp.text[:500]!r}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="gwm-stats-export")
    parser.add_argument("--name", required=True, help="Primary player name")
    parser.add_argument(
        "--aliases",
        default="",
        help=(
            "Comma-separated list of aliases (cross-platform handles). "
            "Defaults to just --name when empty."
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (the public service runs over http, "
        "but operators may proxy it via self-signed TLS).",
    )
    args = parser.parse_args(argv)

    aliases = _split_aliases(args.aliases)
    try:
        payload = fetch(
            name=args.name,
            aliases=aliases,
            base_url=args.base_url,
            endpoint=args.endpoint,
            user_agent=args.user_agent,
            timeout=args.timeout,
            verify_tls=not args.insecure,
        )
    except requests.RequestException as e:
        _log(f"http error: {e}")
        return 3

    # Pretty-print so the snapshot file is diffable in git/CI logs.
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

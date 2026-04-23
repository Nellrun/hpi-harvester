"""HTML parsing for ps-timetracker.com pages.

Pure functions on HTML strings so they are trivially unit-testable with
saved fixtures. Keep all BeautifulSoup usage here — the scraper module
only feeds raw HTML in and consumes structured dicts out.
"""

from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

_PLAYTIME_ID_RE = re.compile(r"/playtimes/(\d+)/")
_GAME_ID_RE = re.compile(r"/game/([^/?#]+)")


class AuthRequired(Exception):
    """Raised when a page looks like the login form instead of real content."""


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def looks_like_login(html: str) -> bool:
    # The login form has an input named "code" with placeholder "Your Code".
    # Cheap string check avoids a second parse when we just want to fail fast.
    return 'placeholder="Your Code"' in html or 'name="code"' in html.lower()


def parse_sessions(html: str) -> list[dict]:
    """Parse /profile/<user>/playtimes page into a list of session rows.

    Row schema (7 <td> cells):
        0: visibility toggle link — carries playtime_id in href
        1: game <a> — href has PSN content id, text is title
        2: platform (PS5/PS4/PS3/PSVITA)
        3: duration — text "1:48 hours" plus data-sort="<seconds>"
        4: start datetime "YYYY-MM-DD HH:MM" (account-local tz)
        5: end datetime   "YYYY-MM-DD HH:MM"
        6: edit link
    """
    soup = _soup(html)
    table = soup.select_one("table tbody")
    if table is None:
        if looks_like_login(html):
            raise AuthRequired("playtimes page returned a login form")
        return []

    out: list[dict] = []
    for tr in table.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 6:
            continue

        first_link = tds[0].find("a", href=True)
        pid_match = _PLAYTIME_ID_RE.search(first_link["href"]) if first_link else None
        if not pid_match:
            continue
        playtime_id = int(pid_match.group(1))

        game_link = tds[1].find("a", href=True)
        game_title = (game_link.get_text(strip=True) if game_link else tds[1].get_text(strip=True)) or None
        game_id_match = _GAME_ID_RE.search(game_link["href"]) if game_link else None
        game_id = game_id_match.group(1) if game_id_match else None

        platform = tds[2].get_text(strip=True) or None

        dur_td = tds[3]
        dur_text = dur_td.get_text(strip=True) or None
        dur_sort = dur_td.get("data-sort")
        duration_seconds = int(dur_sort) if dur_sort and dur_sort.isdigit() else None

        start = tds[4].get_text(strip=True) or None
        end = tds[5].get_text(strip=True) or None

        out.append(
            {
                "playtime_id": playtime_id,
                "game_id": game_id,
                "game_title": game_title,
                "platform": platform,
                "duration_seconds": duration_seconds,
                "duration_text": dur_text,
                "start_local": start,
                "end_local": end,
            }
        )
    return out


def has_next_page(html: str) -> bool:
    soup = _soup(html)
    return soup.select_one('a.page-link[rel="next"]') is not None


def parse_library(html: str) -> list[dict]:
    """Parse /profile/<user> landing page into aggregate per-game rows.

    Columns: rank, title, platform, hours, sessions, avg_session, last_played.
    Schema is simpler than sessions and may change with layout tweaks; keep
    this tolerant and skip malformed rows rather than failing the whole run.
    """
    soup = _soup(html)
    table = soup.select_one("table tbody")
    if table is None:
        if looks_like_login(html):
            raise AuthRequired("profile page returned a login form")
        return []

    out: list[dict] = []
    for tr in table.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 7:
            continue
        game_link = tds[1].find("a", href=True)
        game_id_match = _GAME_ID_RE.search(game_link["href"]) if game_link else None
        out.append(
            {
                "rank": _maybe_int(tds[0].get_text(strip=True)),
                "game_id": game_id_match.group(1) if game_id_match else None,
                "game_title": (game_link.get_text(strip=True) if game_link else None),
                "platform": tds[2].get_text(strip=True) or None,
                "hours_text": tds[3].get_text(strip=True) or None,
                "hours_sort": _maybe_int(tds[3].get("data-sort")),
                "sessions_count": _maybe_int(tds[4].get_text(strip=True)),
                "avg_session_text": tds[5].get_text(strip=True) or None,
                "avg_session_sort": _maybe_int(tds[5].get("data-sort")),
                "last_played_local": tds[6].get_text(strip=True) or None,
            }
        )
    return out


def _maybe_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    return None

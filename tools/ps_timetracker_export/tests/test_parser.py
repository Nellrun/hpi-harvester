from pathlib import Path

import pytest

from ps_timetracker_export.main import _resolve_cookie
from ps_timetracker_export.parser import (
    AuthRequired,
    has_next_page,
    looks_like_login,
    parse_sessions,
    parse_library,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_sessions_extracts_structured_rows():
    html = _load("playtimes_with_next.html")
    rows = parse_sessions(html)
    assert len(rows) == 3
    assert rows[0] == {
        "playtime_id": 54647302,
        "game_id": "PPSA02530_00",
        "game_title": "PRAGMATA",
        "platform": "PS5",
        "duration_seconds": 6477,
        "duration_text": "1:48 hours",
        "start_local": "2026-04-18 17:46",
        "end_local": "2026-04-18 19:34",
    }
    # Newest-first ordering is what the pagination relies on.
    ids = [r["playtime_id"] for r in rows]
    assert ids == sorted(ids, reverse=True)


def test_has_next_page_detects_rel_next():
    assert has_next_page(_load("playtimes_with_next.html")) is True
    assert has_next_page(_load("playtimes_last.html")) is False


def test_login_form_raises_on_session_parse():
    html = _load("login.html")
    assert looks_like_login(html) is True
    with pytest.raises(AuthRequired):
        parse_sessions(html)


def test_parse_library_handles_empty_without_login():
    # Page with no table and no login form should return an empty list rather
    # than exploding — tolerates layout drift.
    assert parse_library("<html><body>no data</body></html>") == []


def test_resolve_cookie_accepts_bare_value(tmp_path):
    assert _resolve_cookie("abc123", None) == "abc123"


def test_resolve_cookie_strips_name_prefix(tmp_path):
    assert _resolve_cookie("_my_app_session=abc123", None) == "abc123"


def test_resolve_cookie_reads_file(tmp_path):
    f = tmp_path / "c.cookie"
    f.write_text("  xyz789  \n", encoding="utf-8")
    assert _resolve_cookie(None, f) == "xyz789"


def test_resolve_cookie_rejects_neither():
    with pytest.raises(SystemExit):
        _resolve_cookie(None, None)


def test_resolve_cookie_rejects_empty():
    with pytest.raises(SystemExit):
        _resolve_cookie("   ", None)


def test_parse_sessions_with_duration_missing_data_sort():
    html = """<html><body><table><tbody>
      <tr>
        <td class="mini first"><a href="/profile/X/playtimes/42/update_visibility"></a></td>
        <td><a href="/profile/X/game/PPSA00001_00">Foo</a></td>
        <td>PS5</td>
        <td>1 minute</td>
        <td>2026-01-01 00:00</td>
        <td>2026-01-01 00:01</td>
        <td></td>
      </tr>
    </tbody></table></body></html>"""
    rows = parse_sessions(html)
    assert len(rows) == 1
    assert rows[0]["playtime_id"] == 42
    assert rows[0]["duration_seconds"] is None
    assert rows[0]["duration_text"] == "1 minute"

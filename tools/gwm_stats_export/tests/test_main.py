"""Tests for the gwm-stats-export CLI."""

from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import patch

import pytest

from gwm_stats_export import main as cli


class _FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")

    def json(self) -> Any:
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


def test_split_aliases_drops_empty_and_trims() -> None:
    assert cli._split_aliases("a, b ,,c,") == ["a", "b", "c"]


def test_fetch_builds_query(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get(url: str, params=None, headers=None, timeout=None, verify=None):  # type: ignore[no-untyped-def]
        captured.update(
            url=url, params=params, headers=headers, timeout=timeout, verify=verify
        )
        return _FakeResponse({"ok": True})

    monkeypatch.setattr(cli.requests, "get", fake_get)

    payload = cli.fetch(
        name="Nellrun",
        aliases=["Nellrun", "TrueNellrun"],
        base_url="http://example.test:3300/",
        endpoint="/api/player",
        user_agent="ua",
        timeout=12.0,
        verify_tls=False,
    )

    assert payload == {"ok": True}
    assert captured["url"] == "http://example.test:3300/api/player"
    assert captured["params"] == [("name", "Nellrun"), ("aliases", "Nellrun,TrueNellrun")]
    assert captured["verify"] is False
    assert captured["timeout"] == 12.0


def test_fetch_defaults_aliases_to_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get(url: str, params=None, **_: Any):  # type: ignore[no-untyped-def]
        captured["params"] = params
        return _FakeResponse({"ok": True})

    monkeypatch.setattr(cli.requests, "get", fake_get)
    cli.fetch(
        name="solo",
        aliases=[],
        base_url="http://x",
        endpoint="/api/player",
        user_agent="ua",
        timeout=1.0,
        verify_tls=True,
    )
    assert captured["params"] == [("name", "solo"), ("aliases", "solo")]


def test_main_writes_json_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sample = {"name": "Nellrun", "totals": {"sessions": 16}}
    monkeypatch.setattr(cli, "fetch", lambda **_: sample)

    rc = cli.main(["--name", "Nellrun", "--aliases", "Nellrun,TrueNellrun"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == sample


def test_main_http_error_returns_3(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    def boom(**_: Any) -> Any:
        raise requests.ConnectionError("nope")

    monkeypatch.setattr(cli, "fetch", boom)
    rc = cli.main(["--name", "x"])
    assert rc == 3

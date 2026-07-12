from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import steam_proactive


def _configure(monkeypatch, tmp_path, now: datetime) -> None:
    monkeypatch.setattr(steam_proactive, "_DB_PATH", tmp_path / "steam.sqlite3")
    monkeypatch.setattr(steam_proactive, "_CONFIG_PATH", tmp_path / "steam.json")
    monkeypatch.setattr(steam_proactive, "_now", lambda: now)
    (tmp_path / "steam.json").write_text(
        json.dumps(
            {
                "steam_api_key": "test-key",
                "steam_id": "test-user",
                "snapshot_interval_seconds": 3600,
            }
        ),
        encoding="utf-8",
    )


def test_context_refreshes_expired_snapshot_once(monkeypatch, tmp_path) -> None:
    now = datetime(2026, 7, 13, tzinfo=UTC)
    _configure(monkeypatch, tmp_path, now)
    recent_calls = 0

    def recently_played(_: str) -> list[dict]:
        nonlocal recent_calls
        recent_calls += 1
        return [
            {
                "appid": 1,
                "name": "Game",
                "playtime_2weeks": 120,
                "playtime_forever": 600,
            }
        ]

    monkeypatch.setattr(steam_proactive, "_fetch_recently_played", recently_played)
    monkeypatch.setattr(
        steam_proactive,
        "_fetch_player_summary",
        lambda _: {"personastate": 1},
    )

    first = steam_proactive.get_context()
    second = steam_proactive.get_context()

    assert recent_calls == 1
    assert first["recent_snapshot_at"] == now.isoformat()
    assert first["games"][0]["recent_2w_hours"] == 2.0
    assert second["snapshot_refresh_error"] is None


def test_empty_snapshot_records_fresh_run(monkeypatch, tmp_path) -> None:
    now = datetime(2026, 7, 13, tzinfo=UTC)
    _configure(monkeypatch, tmp_path, now)
    calls = 0

    def recently_played(_: str) -> list[dict]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(steam_proactive, "_fetch_recently_played", recently_played)
    monkeypatch.setattr(steam_proactive, "_fetch_player_summary", lambda _: {})

    context = steam_proactive.get_context()
    _ = steam_proactive.get_context()

    assert calls == 1
    assert context["available"] is True
    assert context["games"] == []


def test_snapshot_refresh_failure_is_visible(monkeypatch, tmp_path) -> None:
    now = datetime(2026, 7, 13, tzinfo=UTC)
    _configure(monkeypatch, tmp_path, now)
    monkeypatch.setattr(
        steam_proactive,
        "_fetch_recently_played",
        lambda _: (_ for _ in ()).throw(OSError("Steam unavailable")),
    )
    monkeypatch.setattr(steam_proactive, "_fetch_player_summary", lambda _: {})

    context = steam_proactive.get_context()

    assert context["available"] is False
    assert context["snapshot_refresh_error"] == "Steam unavailable"


def test_recent_snapshot_skips_refresh(monkeypatch, tmp_path) -> None:
    now = datetime(2026, 7, 13, tzinfo=UTC)
    _configure(monkeypatch, tmp_path, now)
    conn = steam_proactive._get_conn()
    conn.execute(
        "INSERT INTO snapshot_runs(snapshotted_at) VALUES (?)",
        ((now - timedelta(minutes=30)).isoformat(),),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        steam_proactive,
        "_fetch_recently_played",
        lambda _: (_ for _ in ()).throw(AssertionError("不应刷新")),
    )
    monkeypatch.setattr(steam_proactive, "_fetch_player_summary", lambda _: {})

    context = steam_proactive.get_context()

    assert context["snapshot_refresh_error"] is None

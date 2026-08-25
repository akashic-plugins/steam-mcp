from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from steam_runtime import backend


def _config(tmp_path) -> None:
    (tmp_path / "steam_mcp_config.json").write_text(
        json.dumps(
            {
                "steam_api_key": "test-key",
                "steam_id": "test-user",
                "snapshot_interval_seconds": 300,
            }
        ),
        encoding="utf-8",
    )


def test_fresh_context_uses_persisted_transition_and_stale_is_absent(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 23, 8, tzinfo=UTC)
    _config(tmp_path)
    monkeypatch.setattr(
        backend,
        "_fetch_player_summary",
        lambda _: {
            "personastate": 1,
            "gameid": "1",
            "gameextrainfo": "Game",
        },
    )
    monkeypatch.setattr(backend, "_fetch_recently_played", lambda _: [])
    _ = backend.refresh(tmp_path, now)

    fresh = backend.wake_context(tmp_path, now + timedelta(minutes=1))
    assert fresh is not None
    assert fresh["presence"] == "in_game"
    assert fresh["currently_playing"] == "Game"
    assert fresh["transition"] == ""

    monkeypatch.setattr(
        backend,
        "_fetch_player_summary",
        lambda _: {"personastate": 0},
    )
    _ = backend.refresh(tmp_path, now + timedelta(minutes=5))
    changed = backend.wake_context(tmp_path, now + timedelta(minutes=6))
    assert changed is not None
    assert changed["presence"] == "offline"
    assert changed["transition"] == "in_game->offline"
    assert backend.wake_context(tmp_path, now + timedelta(minutes=11)) is None


def test_unknown_presence_never_becomes_wake_hint(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 8, 23, 8, tzinfo=UTC)
    _config(tmp_path)
    monkeypatch.setattr(backend, "_fetch_player_summary", lambda _: {})
    monkeypatch.setattr(backend, "_fetch_recently_played", lambda _: [])

    _ = backend.refresh(tmp_path, now)

    assert backend.wake_context(tmp_path, now) is None


def test_fresh_context_keeps_prior_history_visible_when_current_games_are_empty(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 23, 8, tzinfo=UTC)
    _config(tmp_path)
    games = [
        {
            "appid": 1,
            "name": "Old Game",
            "playtime_2weeks": 120,
            "playtime_forever": 600,
        }
    ]
    monkeypatch.setattr(
        backend,
        "_fetch_player_summary",
        lambda _: {"personastate": 1},
    )
    monkeypatch.setattr(backend, "_fetch_recently_played", lambda _: games)
    _ = backend.refresh(tmp_path, now - timedelta(days=15))
    monkeypatch.setattr(backend, "_fetch_recently_played", lambda _: [])
    _ = backend.refresh(tmp_path, now)

    current = backend.wake_context(tmp_path, now)

    assert current is not None
    assert current["previous_snapshot_at"] == (now - timedelta(days=15)).isoformat()
    assert current["games"] == [
        {
            "name": "Old Game",
            "recent_2w_hours": 0.0,
            "all_time_hours": 10.0,
            "previous_snapshot_2w_hours": 2.0,
        }
    ]

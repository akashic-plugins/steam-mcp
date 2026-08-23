from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

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


def test_changed_snapshot_appends_but_same_and_empty_only_advance_current_state(
    tmp_path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 23, 8, tzinfo=UTC)
    _config(tmp_path)
    games = [
        {
            "appid": 1,
            "name": "Game",
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

    first = backend.refresh(tmp_path, now)
    repeated = backend.refresh(tmp_path, now + timedelta(minutes=5))
    monkeypatch.setattr(backend, "_fetch_recently_played", lambda _: [])
    empty = backend.refresh(tmp_path, now + timedelta(minutes=10))
    current = backend.state(tmp_path, now + timedelta(minutes=10))

    assert first.history_appended is True
    assert repeated.history_appended is False
    assert empty.history_appended is False
    assert current["snapshot_runs"] == 1
    assert current["snapshots"] == 1
    assert json.loads(str(current["current_games_json"])) == []


def test_existing_history_is_never_trimmed(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 8, 23, 8, tzinfo=UTC)
    _config(tmp_path)
    monkeypatch.setattr(
        backend,
        "_fetch_player_summary",
        lambda _: {"personastate": 1},
    )
    games = [
        {
            "appid": 1,
            "name": "Game",
            "playtime_2weeks": 60,
            "playtime_forever": 60,
        }
    ]
    monkeypatch.setattr(backend, "_fetch_recently_played", lambda _: games)
    for index in range(4):
        games[0] = {**games[0], "playtime_forever": 60 + index}
        _ = backend.refresh(tmp_path, now + timedelta(minutes=5 * index))

    current = backend.state(tmp_path, now + timedelta(minutes=20))
    assert current["snapshot_runs"] == 4
    assert current["snapshots"] == 4


def test_initialize_adopts_existing_history_without_rewriting_it(tmp_path) -> None:
    database = tmp_path / "steam_proactive.sqlite3"
    existing = [
        ("2026-07-01T08:00:00+00:00", 1, "Old Game", 120, 600),
        ("2026-08-01T08:00:00+00:00", 1, "Old Game", 60, 660),
    ]
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshotted_at TEXT NOT NULL,
                game_appid INTEGER NOT NULL,
                game_name TEXT NOT NULL,
                playtime_2w_mins INTEGER NOT NULL,
                playtime_forever_mins INTEGER NOT NULL
            );
            CREATE TABLE snapshot_runs (snapshotted_at TEXT PRIMARY KEY);
            """
        )
        connection.executemany(
            """
            INSERT INTO snapshots(
                snapshotted_at, game_appid, game_name,
                playtime_2w_mins, playtime_forever_mins
            ) VALUES (?, ?, ?, ?, ?)
            """,
            existing,
        )
        connection.executemany(
            "INSERT INTO snapshot_runs(snapshotted_at) VALUES (?)",
            [(row[0],) for row in existing],
        )
        connection.commit()

    backend.initialize(tmp_path, datetime(2026, 8, 23, tzinfo=UTC))

    with sqlite3.connect(database) as connection:
        adopted = connection.execute(
            """
            SELECT snapshotted_at, game_appid, game_name,
                   playtime_2w_mins, playtime_forever_mins
            FROM snapshots ORDER BY id
            """
        ).fetchall()
        runs = connection.execute(
            "SELECT snapshotted_at FROM snapshot_runs ORDER BY snapshotted_at"
        ).fetchall()
    assert adopted == existing
    assert runs == [(row[0],) for row in existing]


def test_incompatible_history_schema_fails_loud(tmp_path) -> None:
    database = tmp_path / "steam_proactive.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE snapshots(value TEXT NOT NULL)")
        connection.commit()

    with pytest.raises(RuntimeError, match="schema 不兼容"):
        backend.initialize(tmp_path, datetime(2026, 8, 23, tzinfo=UTC))

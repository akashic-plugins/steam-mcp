from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from steam_runtime.config import SteamRuntimeConfig, load_runtime_config


_DB_NAME = "steam_proactive.sqlite3"
_PRESENCE_REFRESH_SECONDS = 300
_TRANSIENT_RETRY_SECONDS = 60
_TABLE_SCHEMAS = {
    "snapshots": (
        ("id", "INTEGER", 0, None, 1),
        ("snapshotted_at", "TEXT", 1, None, 0),
        ("game_appid", "INTEGER", 1, None, 0),
        ("game_name", "TEXT", 1, None, 0),
        ("playtime_2w_mins", "INTEGER", 1, None, 0),
        ("playtime_forever_mins", "INTEGER", 1, None, 0),
    ),
    "snapshot_runs": (("snapshotted_at", "TEXT", 0, None, 1),),
    "current_state": (
        ("singleton", "INTEGER", 0, None, 1),
        ("presence_json", "TEXT", 0, None, 0),
        ("presence_observed_at", "TEXT", 0, None, 0),
        ("presence_expires_at", "TEXT", 0, None, 0),
        ("current_games_json", "TEXT", 1, None, 0),
        ("last_refresh_attempt_at", "TEXT", 0, None, 0),
        ("last_refresh_error", "TEXT", 0, None, 0),
        ("last_snapshot_checked_at", "TEXT", 0, None, 0),
        ("last_history_fingerprint", "TEXT", 0, None, 0),
        ("next_refresh_at", "TEXT", 1, None, 0),
    ),
}


class SteamNetworkError(RuntimeError):
    """表示可重试的 Steam 网络失败。"""


@dataclass(frozen=True, slots=True)
class RefreshResult:
    next_due: datetime
    history_appended: bool
    presence: str


def initialize(data_root: Path, now: datetime) -> None:
    """建立并验证 Steam state schema，同时保留全部既有快照。"""

    connection = _connect(data_root)
    try:
        _ensure_current_row(connection, _aware(now))
        connection.commit()
    finally:
        connection.close()


def next_deadline(data_root: Path, now: datetime) -> datetime:
    """返回 source 持久化的下一次刷新时间。"""

    aware_now = _aware(now)
    connection = _connect(data_root)
    try:
        _ensure_current_row(connection, aware_now)
        row = connection.execute(
            "SELECT next_refresh_at FROM current_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("Steam current_state singleton 缺失")
        return _parse_aware(str(row["next_refresh_at"]), "next_refresh_at")
    finally:
        connection.close()


def refresh(data_root: Path, now: datetime) -> RefreshResult:
    """刷新 current presence，并仅在游戏快照有意义变化时追加历史。"""

    aware_now = _aware(now)
    config = load_runtime_config(data_root / "steam_mcp_config.json")
    state = _read_current(data_root, aware_now)

    # 1. 网络边界先冻结完整结果；部分响应不能写入本地状态。
    summary = _fetch_player_summary(config)
    snapshot_due = _snapshot_due(state, config, aware_now)
    games = _fetch_recently_played(config) if snapshot_due else None
    previous_presence = "unknown"
    if state.get("presence_json") is not None:
        previous = _json_object(state["presence_json"], "presence_json")
        previous_presence = str(previous.get("presence") or "unknown")
    presence = _presence(summary, aware_now, previous_presence)

    # 2. 一个事务覆盖 current state，并按 fingerprint 决定是否追加历史。
    connection = _connect(data_root)
    try:
        _ensure_current_row(connection, aware_now)
        history_appended = False
        if games is not None:
            fingerprint = _games_fingerprint(games)
            current = connection.execute(
                "SELECT last_history_fingerprint FROM current_state WHERE singleton = 1"
            ).fetchone()
            if current is None:
                raise RuntimeError("Steam current_state singleton 缺失")
            previous_fingerprint = current["last_history_fingerprint"]
            if games and fingerprint != previous_fingerprint:
                _append_snapshot(connection, games, aware_now)
                previous_fingerprint = fingerprint
                history_appended = True
            connection.execute(
                """
                UPDATE current_state
                SET current_games_json = ?,
                    last_snapshot_checked_at = ?,
                    last_history_fingerprint = ?
                WHERE singleton = 1
                """,
                (
                    _json(games),
                    aware_now.isoformat(),
                    previous_fingerprint,
                ),
            )
        next_due = aware_now + timedelta(seconds=_PRESENCE_REFRESH_SECONDS)
        connection.execute(
            """
            UPDATE current_state
            SET presence_json = ?,
                presence_observed_at = ?,
                presence_expires_at = ?,
                last_refresh_attempt_at = ?,
                last_refresh_error = NULL,
                next_refresh_at = ?
            WHERE singleton = 1
            """,
            (
                _json(presence),
                aware_now.isoformat(),
                (aware_now + timedelta(seconds=_PRESENCE_REFRESH_SECONDS)).isoformat(),
                aware_now.isoformat(),
                next_due.isoformat(),
            ),
        )
        connection.commit()
        return RefreshResult(next_due, history_appended, str(presence["presence"]))
    finally:
        connection.close()


def record_transient_failure(
    data_root: Path,
    now: datetime,
    error: SteamNetworkError,
) -> datetime:
    """记录可观察的当前失败，并返回有界重试 deadline。"""

    aware_now = _aware(now)
    retry_due = aware_now + timedelta(seconds=_TRANSIENT_RETRY_SECONDS)
    connection = _connect(data_root)
    try:
        _ensure_current_row(connection, aware_now)
        connection.execute(
            """
            UPDATE current_state
            SET last_refresh_attempt_at = ?,
                last_refresh_error = ?,
                next_refresh_at = ?
            WHERE singleton = 1
            """,
            (aware_now.isoformat(), str(error), retry_due.isoformat()),
        )
        connection.commit()
        return retry_due
    finally:
        connection.close()


def wake_context(data_root: Path, now: datetime) -> dict[str, object] | None:
    """读取 fresh current state；stale 或 unknown 时不返回 hint。"""

    aware_now = _aware(now)
    database = data_root / _DB_NAME
    if not database.is_file():
        return None
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    try:
        _validate_schema(connection)
        row = connection.execute(
            "SELECT * FROM current_state WHERE singleton = 1"
        ).fetchone()
        if row is None or row["presence_expires_at"] is None:
            return None
        expires_at = _parse_aware(str(row["presence_expires_at"]), "presence_expires_at")
        presence = _json_object(row["presence_json"], "presence_json")
        if expires_at < aware_now or presence.get("presence") == "unknown":
            return None
        games = _json_list(row["current_games_json"], "current_games_json")
        previous_at, previous_games = _previous_snapshot(connection, aware_now)
        return {
            "observed_at": row["presence_observed_at"],
            "expires_at": row["presence_expires_at"],
            "presence": presence["presence"],
            "online_status": presence["online_status"],
            "currently_playing": presence["currently_playing"],
            "interruptibility": presence["interruptibility"],
            "confidence": presence["confidence"],
            "transition": presence["transition"],
            "games": _context_games(games, previous_games),
            "last_snapshot_checked_at": row["last_snapshot_checked_at"],
            "previous_snapshot_at": previous_at,
        }
    finally:
        connection.close()


def state(data_root: Path, now: datetime) -> dict[str, object]:
    """返回 fixture 和诊断使用的 current state 与历史计数。"""

    aware_now = _aware(now)
    connection = _connect(data_root)
    try:
        _ensure_current_row(connection, aware_now)
        row = connection.execute(
            "SELECT * FROM current_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("Steam current_state singleton 缺失")
        snapshot_runs = connection.execute(
            "SELECT COUNT(*) FROM snapshot_runs"
        ).fetchone()[0]
        snapshots = connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        return {**dict(row), "snapshot_runs": snapshot_runs, "snapshots": snapshots}
    finally:
        connection.close()


def _connect(data_root: Path) -> sqlite3.Connection:
    data_root.mkdir(parents=True, exist_ok=True)
    database = data_root / _DB_NAME
    if database.exists():
        _validate_existing_database(database)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        # 1. 只在只读盘点通过后，用一个事务安装或补齐兼容 schema。
        connection.execute("BEGIN IMMEDIATE")
        _install_schema(connection)
        _validate_schema(connection)
        connection.execute(
            "INSERT OR IGNORE INTO snapshot_runs(snapshotted_at) "
            "SELECT DISTINCT snapshotted_at FROM snapshots"
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        connection.close()
        raise
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _validate_schema(connection: sqlite3.Connection) -> None:
    _validate_table_inventory(connection, require_all=True)


def _validate_existing_database(database: Path) -> None:
    """只读盘点既有数据库，保证失败不会留下迁移痕迹。"""

    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    try:
        _validate_table_inventory(connection, require_all=False)
    finally:
        connection.close()


def _validate_table_inventory(
    connection: sqlite3.Connection,
    *,
    require_all: bool,
) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
    }
    unknown = existing - _TABLE_SCHEMAS.keys()
    if unknown:
        raise RuntimeError(
            "Steam SQLite schema 不兼容: 未知表 " + ", ".join(sorted(unknown))
        )
    if require_all and existing != _TABLE_SCHEMAS.keys():
        missing = _TABLE_SCHEMAS.keys() - existing
        raise RuntimeError(
            "Steam SQLite schema 不兼容: 缺少表 " + ", ".join(sorted(missing))
        )
    for table in sorted(existing):
        actual = tuple(
            (
                str(row["name"]),
                str(row["type"]).upper(),
                int(row["notnull"]),
                row["dflt_value"],
                int(row["pk"]),
            )
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        )
        if actual != _TABLE_SCHEMAS[table]:
            raise RuntimeError(f"Steam SQLite schema 不兼容: {table}")
        _validate_table_constraints(connection, table)
    _validate_snapshot_index(connection, required=require_all)


def _validate_table_constraints(
    connection: sqlite3.Connection,
    table: str,
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None or row["sql"] is None:
        raise RuntimeError(f"Steam SQLite schema 不兼容: {table}")
    compact = "".join(str(row["sql"]).lower().split())
    if table == "snapshots" and "integerprimarykeyautoincrement" not in compact:
        raise RuntimeError("Steam SQLite schema 不兼容: snapshots 缺少 AUTOINCREMENT")
    if table == "current_state" and "check(singleton=1)" not in compact:
        raise RuntimeError("Steam SQLite schema 不兼容: current_state 缺少 singleton CHECK")


def _validate_snapshot_index(
    connection: sqlite3.Connection,
    *,
    required: bool,
) -> None:
    row = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = 'index' AND name = 'idx_snap_time'"
    ).fetchone()
    if row is None:
        if required:
            raise RuntimeError("Steam SQLite schema 不兼容: 缺少 idx_snap_time")
        return
    columns = tuple(
        str(item["name"])
        for item in connection.execute("PRAGMA index_info(idx_snap_time)").fetchall()
    )
    if columns != ("snapshotted_at",):
        raise RuntimeError("Steam SQLite schema 不兼容: idx_snap_time")


def _install_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshotted_at        TEXT NOT NULL,
            game_appid            INTEGER NOT NULL,
            game_name             TEXT NOT NULL,
            playtime_2w_mins      INTEGER NOT NULL,
            playtime_forever_mins INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshot_runs (
            snapshotted_at TEXT PRIMARY KEY
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS current_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            presence_json TEXT,
            presence_observed_at TEXT,
            presence_expires_at TEXT,
            current_games_json TEXT NOT NULL,
            last_refresh_attempt_at TEXT,
            last_refresh_error TEXT,
            last_snapshot_checked_at TEXT,
            last_history_fingerprint TEXT,
            next_refresh_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_snap_time ON snapshots(snapshotted_at)"
    )


def _ensure_current_row(connection: sqlite3.Connection, now: datetime) -> None:
    latest = connection.execute(
        "SELECT snapshotted_at FROM snapshot_runs ORDER BY snapshotted_at DESC LIMIT 1"
    ).fetchone()
    latest_at = None if latest is None else str(latest["snapshotted_at"])
    fingerprint = _latest_history_fingerprint(connection, latest_at)
    connection.execute(
        """
        INSERT OR IGNORE INTO current_state(
            singleton, current_games_json, last_snapshot_checked_at,
            last_history_fingerprint, next_refresh_at
        ) VALUES(1, '[]', ?, ?, ?)
        """,
        (latest_at, fingerprint, now.isoformat()),
    )


def _read_current(data_root: Path, now: datetime) -> dict[str, object]:
    connection = _connect(data_root)
    try:
        _ensure_current_row(connection, now)
        row = connection.execute(
            "SELECT * FROM current_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("Steam current_state singleton 缺失")
        connection.commit()
        return dict(row)
    finally:
        connection.close()


def _snapshot_due(
    state: Mapping[str, object],
    config: SteamRuntimeConfig,
    now: datetime,
) -> bool:
    checked = state.get("last_snapshot_checked_at")
    if checked is None:
        return True
    elapsed = (now - _parse_aware(str(checked), "last_snapshot_checked_at")).total_seconds()
    return elapsed >= config.snapshot_interval_seconds


def _fetch_player_summary(config: SteamRuntimeConfig) -> dict[str, object]:
    data = _steam_get(
        config,
        "ISteamUser/GetPlayerSummaries/v0002",
        {"steamids": config.steam_id},
    )
    response = _mapping(data.get("response"), "Steam player response")
    players = _list(response.get("players"), "Steam players")
    if not players:
        return {}
    return dict(_mapping(players[0], "Steam player"))


def _fetch_recently_played(config: SteamRuntimeConfig) -> list[dict[str, object]]:
    data = _steam_get(
        config,
        "IPlayerService/GetRecentlyPlayedGames/v0001",
        {"steamid": config.steam_id, "count": 0},
    )
    response = _mapping(data.get("response"), "Steam games response")
    raw_games = _list(response.get("games", []), "Steam games")
    games: list[dict[str, object]] = []
    for raw in raw_games:
        game = _mapping(raw, "Steam game")
        appid = game.get("appid")
        if not isinstance(appid, int) or isinstance(appid, bool) or appid <= 0:
            raise RuntimeError("Steam game appid 无效")
        games.append(
            {
                "appid": appid,
                "name": str(game.get("name") or f"App {appid}"),
                "playtime_2weeks": _nonnegative_int(
                    game.get("playtime_2weeks", 0), "playtime_2weeks"
                ),
                "playtime_forever": _nonnegative_int(
                    game.get("playtime_forever", 0), "playtime_forever"
                ),
            }
        )
    return sorted(games, key=lambda item: cast(int, item["appid"]))


def _steam_get(
    config: SteamRuntimeConfig,
    path: str,
    params: Mapping[str, object],
) -> dict[str, object]:
    query = urlencode({**params, "key": config.steam_api_key, "format": "json"})
    url = f"https://api.steampowered.com/{path}?{query}"
    try:
        with urlopen(url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in {408, 429} or error.code >= 500:
            raise SteamNetworkError(f"Steam API HTTP {error.code}: {path}") from error
        raise RuntimeError(f"Steam API 拒绝请求 HTTP {error.code}: {path}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise SteamNetworkError(f"Steam API 网络失败: {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Steam API 返回非法 JSON: {path}") from error
    return dict(_mapping(payload, "Steam API payload"))


def _presence(
    summary: Mapping[str, object],
    now: datetime,
    previous_presence: str,
) -> dict[str, object]:
    status_by_code = {0: "offline", 1: "online", 2: "busy", 3: "away", 4: "snooze"}
    if not summary:
        status = "unknown"
    else:
        state = summary.get("personastate", 0)
        if not isinstance(state, int) or isinstance(state, bool):
            raise RuntimeError("Steam personastate 无效")
        status = "in-game" if summary.get("gameid") else status_by_code.get(state, "offline")
    presence = {
        "in-game": "in_game",
        "online": "active",
        "busy": "active",
        "away": "idle",
        "snooze": "idle",
        "offline": "offline",
    }.get(status, "unknown")
    interruptibility = {
        "in-game": 0.1,
        "online": 0.8,
        "busy": 0.1,
        "away": 0.4,
        "snooze": 0.3,
        "offline": 0.0,
    }.get(status, 0.4)
    transition = ""
    if (
        previous_presence != "unknown"
        and presence != "unknown"
        and presence != previous_presence
    ):
        transition = f"{previous_presence}->{presence}"
    return {
        "observed_at": now.isoformat(),
        "online_status": status,
        "presence": presence,
        "currently_playing": summary.get("gameextrainfo"),
        "interruptibility": interruptibility,
        "confidence": 0.9 if presence != "unknown" else 0.1,
        "transition": transition,
    }


def _append_snapshot(
    connection: sqlite3.Connection,
    games: list[dict[str, object]],
    now: datetime,
) -> None:
    timestamp = now.isoformat()
    connection.execute(
        "INSERT INTO snapshot_runs(snapshotted_at) VALUES (?)", (timestamp,)
    )
    connection.executemany(
        """
        INSERT INTO snapshots(
            snapshotted_at, game_appid, game_name,
            playtime_2w_mins, playtime_forever_mins
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                timestamp,
                game["appid"],
                game["name"],
                game["playtime_2weeks"],
                game["playtime_forever"],
            )
            for game in games
        ],
    )


def _latest_history_fingerprint(
    connection: sqlite3.Connection,
    snapshot_at: str | None,
) -> str | None:
    if snapshot_at is None:
        return None
    rows = connection.execute(
        """
        SELECT game_appid, game_name, playtime_2w_mins, playtime_forever_mins
        FROM snapshots WHERE snapshotted_at = ? ORDER BY game_appid
        """,
        (snapshot_at,),
    ).fetchall()
    games = [
        {
            "appid": row["game_appid"],
            "name": row["game_name"],
            "playtime_2weeks": row["playtime_2w_mins"],
            "playtime_forever": row["playtime_forever_mins"],
        }
        for row in rows
    ]
    return _games_fingerprint(games) if games else None


def _games_fingerprint(games: list[dict[str, object]]) -> str:
    return hashlib.sha256(_json(games).encode("utf-8")).hexdigest()


def _previous_snapshot(
    connection: sqlite3.Connection,
    now: datetime,
) -> tuple[str | None, dict[int, sqlite3.Row]]:
    cutoff = (now - timedelta(days=14)).isoformat()
    meta = connection.execute(
        """
        SELECT snapshotted_at FROM snapshot_runs
        WHERE snapshotted_at <= ? ORDER BY snapshotted_at DESC LIMIT 1
        """,
        (cutoff,),
    ).fetchone()
    if meta is None:
        return None, {}
    snapshot_at = str(meta["snapshotted_at"])
    rows = connection.execute(
        "SELECT * FROM snapshots WHERE snapshotted_at = ?",
        (snapshot_at,),
    ).fetchall()
    return snapshot_at, {int(row["game_appid"]): row for row in rows}


def _context_games(
    games: list[object],
    previous: Mapping[int, sqlite3.Row],
) -> list[dict[str, object]]:
    context: list[dict[str, object]] = []
    current_appids: set[int] = set()
    for raw in games:
        game = _mapping(raw, "Steam current game")
        appid = _nonnegative_int(game["appid"], "appid")
        current_appids.add(appid)
        previous_row = previous.get(appid)
        context.append(
            {
                "name": game["name"],
                "recent_2w_hours": round(
                    _nonnegative_int(game["playtime_2weeks"], "playtime_2weeks") / 60,
                    1,
                ),
                "all_time_hours": round(
                    _nonnegative_int(game["playtime_forever"], "playtime_forever") / 60,
                    1,
                ),
                "previous_snapshot_2w_hours": (
                    0.0
                    if previous_row is None
                    else round(int(previous_row["playtime_2w_mins"]) / 60, 1)
                ),
            }
        )
    for appid, previous_row in previous.items():
        if appid in current_appids or int(previous_row["playtime_2w_mins"]) == 0:
            continue
        context.append(
            {
                "name": str(previous_row["game_name"]),
                "recent_2w_hours": 0.0,
                "all_time_hours": round(
                    int(previous_row["playtime_forever_mins"]) / 60,
                    1,
                ),
                "previous_snapshot_2w_hours": round(
                    int(previous_row["playtime_2w_mins"]) / 60,
                    1,
                ),
            }
        )
    return sorted(context, key=lambda item: cast(float, item["recent_2w_hours"]), reverse=True)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_object(value: object, label: str) -> dict[str, object]:
    parsed = json.loads(str(value))
    return dict(_mapping(parsed, label))


def _json_list(value: object, label: str) -> list[object]:
    parsed = json.loads(str(value))
    return list(_list(parsed, label))


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} 必须是 object")
    return cast(Mapping[str, Any], value)


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise RuntimeError(f"{label} 必须是 list")
    return cast(list[Any], value)


def _nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"Steam {label} 无效")
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Steam clock 必须包含时区")
    return value.astimezone(UTC)


def _parse_aware(value: str, label: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise RuntimeError(f"Steam {label} 必须包含时区")
    return parsed.astimezone(UTC)

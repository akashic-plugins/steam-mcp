"""steam_proactive.py — Steam proactive context backend.

维护本地 SQLite 快照，追踪用户近期游戏活动。
通过 get_context() 向 proactive engine 的 background_context channel 提供持久感知数据。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

logger = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).parent
_RUNTIME_DIR = Path(os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip() or _SCRIPT_DIR)
_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = _RUNTIME_DIR / "steam_proactive.sqlite3"
_CONFIG_PATH = _RUNTIME_DIR / "steam_mcp_config.json"
_last_wake_presence = "unknown"
_DEFAULT_SNAPSHOT_INTERVAL_SECONDS = 6 * 3600


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        loaded = json.loads(_CONFIG_PATH.read_text())
        if not isinstance(loaded, dict):
            raise ValueError("steam_mcp_config.json 根节点必须是 object")
    else:
        loaded = {}
    steam_api_key = os.environ.get("STEAM_API_KEY", "").strip()
    steam_id = os.environ.get("STEAM_ID", "").strip()
    if steam_api_key:
        loaded["steam_api_key"] = steam_api_key
    if steam_id:
        loaded["steam_id"] = steam_id
    return loaded


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshotted_at        TEXT NOT NULL,
        game_appid            INTEGER NOT NULL,
        game_name             TEXT NOT NULL,
        playtime_2w_mins      INTEGER NOT NULL,
        playtime_forever_mins INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_snap_time ON snapshots(snapshotted_at);
    CREATE TABLE IF NOT EXISTS snapshot_runs (
        snapshotted_at TEXT PRIMARY KEY
    );
    """)
    conn.execute(
        "INSERT OR IGNORE INTO snapshot_runs(snapshotted_at) "
        "SELECT DISTINCT snapshotted_at FROM snapshots"
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Steam API
# ---------------------------------------------------------------------------

def _steam_get(path: str, params: dict) -> dict:
    cfg = _load_config()
    p = dict(params, key=cfg.get("steam_api_key", ""), format="json")
    url = f"https://api.steampowered.com/{path}?{urlencode(p)}"
    try:
        with urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        raise RuntimeError(f"Steam API {e.code}: {path}") from e


def _fetch_recently_played(steamid: str) -> list[dict]:
    data = _steam_get("IPlayerService/GetRecentlyPlayedGames/v0001", {"steamid": steamid, "count": 0})
    return data.get("response", {}).get("games", [])


def _fetch_player_summary(steamid: str) -> dict:
    data = _steam_get("ISteamUser/GetPlayerSummaries/v0002", {"steamids": steamid})
    players = data.get("response", {}).get("players", [])
    return players[0] if players else {}


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def take_snapshot() -> dict:
    """拉取 Steam API 并存储快照，通常由定时任务调用。"""
    cfg = _load_config()
    steamid = cfg.get("steam_id", "")
    if not steamid:
        return {"ok": False, "error": "steam_id 未在 steam_mcp_config.json 中配置"}

    try:
        games = _fetch_recently_played(steamid)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    conn = _get_conn()
    now = _now().isoformat()
    conn.execute(
        "INSERT INTO snapshot_runs(snapshotted_at) VALUES (?)",
        (now,),
    )
    for g in games:
        conn.execute(
            "INSERT INTO snapshots (snapshotted_at, game_appid, game_name, "
            "playtime_2w_mins, playtime_forever_mins) VALUES (?, ?, ?, ?, ?)",
            (
                now,
                g["appid"],
                g.get("name", f"App {g['appid']}"),
                g.get("playtime_2weeks", 0),
                g.get("playtime_forever", 0),
            ),
        )
    conn.commit()
    conn.close()
    return {"ok": True, "snapshotted_at": now, "game_count": len(games)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _snapshot_interval_seconds(cfg: dict) -> int:
    raw = cfg.get("snapshot_interval_seconds", _DEFAULT_SNAPSHOT_INTERVAL_SECONDS)
    return max(300, int(raw))


def _refresh_snapshot_if_due(cfg: dict) -> str | None:
    """快照过期时刷新；失败原因交给主动上下文显式展示。"""

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT snapshotted_at FROM snapshot_runs "
            "ORDER BY snapshotted_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is not None:
        latest = datetime.fromisoformat(str(row["snapshotted_at"]))
        if (_now() - latest).total_seconds() < _snapshot_interval_seconds(cfg):
            return None
    result = take_snapshot()
    return None if result["ok"] else str(result["error"])


# ---------------------------------------------------------------------------
# Context computation
# ---------------------------------------------------------------------------

def get_context() -> dict[str, Any]:
    """返回结构化游戏上下文，供 proactive engine 注入 background_context。"""
    cfg = _load_config()
    steamid = cfg.get("steam_id", "")
    snapshot_refresh_error = _refresh_snapshot_if_due(cfg)

    # 1. 实时状态（每次调用直接打 API，轻量）
    realtime: dict[str, Any] = {}
    if steamid:
        try:
            summary = _fetch_player_summary(steamid)
            _PERSONA = {0: "offline", 1: "online", 2: "busy", 3: "away", 4: "snooze"}
            realtime = {
                "fetched_at": _now().isoformat(),
                "online_status": "in-game" if summary.get("gameid") else _PERSONA.get(summary.get("personastate", 0), "offline"),
                "currently_playing": summary.get("gameextrainfo"),
            }
        except Exception as e:
            realtime = {"fetched_at": _now().isoformat(), "error": str(e)}

    # 2. 读取快照
    conn = _get_conn()

    latest = conn.execute(
        "SELECT snapshotted_at FROM snapshot_runs ORDER BY snapshotted_at DESC LIMIT 1"
    ).fetchone()
    if not latest:
        conn.close()
        return _with_wake_contract(
            {
                "available": False,
                "realtime": realtime,
                "snapshot_refresh_error": snapshot_refresh_error,
            }
        )

    recent_snap_at: str = latest["snapshotted_at"]
    recent_rows = conn.execute(
        "SELECT * FROM snapshots WHERE snapshotted_at = ?", (recent_snap_at,)
    ).fetchall()

    # 上次快照：距今 ≥14 天中最新的一条
    cutoff = (_now() - timedelta(days=14)).isoformat()
    prev_meta = conn.execute(
        "SELECT snapshotted_at FROM snapshot_runs WHERE snapshotted_at <= ? "
        "ORDER BY snapshotted_at DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    prev_snap_at: str | None = prev_meta["snapshotted_at"] if prev_meta else None
    prev_rows = (
        conn.execute("SELECT * FROM snapshots WHERE snapshotted_at = ?", (prev_snap_at,)).fetchall()
        if prev_snap_at
        else []
    )
    conn.close()

    # 3. 两个快照取并集，任意一侧时长不为 0 的游戏都收录
    recent_by_appid = {r["game_appid"]: r for r in recent_rows}
    prev_by_appid = {r["game_appid"]: r for r in prev_rows}
    all_appids = set(recent_by_appid) | set(prev_by_appid)

    games: list[dict] = []
    for appid in all_appids:
        recent_r = recent_by_appid.get(appid)
        prev_r = prev_by_appid.get(appid)
        recent_2w_h = round(recent_r["playtime_2w_mins"] / 60, 1) if recent_r else 0
        prev_2w_h = round(prev_r["playtime_2w_mins"] / 60, 1) if prev_r else 0
        if recent_2w_h == 0 and prev_2w_h == 0:
            continue
        if recent_r is not None:
            r = recent_r
        elif prev_r is not None:
            r = prev_r
        else:
            raise RuntimeError(f"快照索引缺少 appid={appid}")
        all_time_h = round(max(r["playtime_forever_mins"], r["playtime_2w_mins"]) / 60, 1)
        games.append({
            "name": r["game_name"],
            "recent_2w_hours": recent_2w_h,
            "prev_snapshot_2w_hours": prev_2w_h,
            "all_time_hours": all_time_h,
        })
    games.sort(key=lambda g: g["recent_2w_hours"], reverse=True)

    # 4. 时间元数据
    now_dt = _now()
    recent_dt = datetime.fromisoformat(recent_snap_at)
    data_freshness_h = round((now_dt - recent_dt).total_seconds() / 3600, 1)
    prev_age_days: float | None = None
    if prev_snap_at:
        prev_age_days = round((now_dt - datetime.fromisoformat(prev_snap_at)).total_seconds() / 86400, 1)

    payload = {
        "_hint": {
            "recent_snapshot_at": "本次快照时间戳",
            "prev_snapshot_at": "对比快照时间戳，null 表示尚无历史数据",
            "prev_snapshot_age_days": "对比快照距今天数",
            "data_freshness_hours": "本次快照距现在的小时数，越小越新鲜",
            "games[].recent_2w_hours": "本次快照记录的近两周时长，0 表示这两周没玩",
            "games[].prev_snapshot_2w_hours": "对比快照记录的近两周时长，0 表示当时没玩或无数据",
            "games[].all_time_hours": "历史累计时长，反映用户对该游戏的熟悉程度",
            "realtime.currently_playing": "非 null 时表示用户此刻正在游戏中",
            "realtime.online_status": "in-game/online/away/offline",
        },
        "available": True,
        "data_freshness_hours": data_freshness_h,
        "recent_snapshot_at": recent_snap_at,
        "prev_snapshot_at": prev_snap_at,
        "prev_snapshot_age_days": prev_age_days,
        "games": games,
        "realtime": realtime,
        "snapshot_refresh_error": snapshot_refresh_error,
    }
    return _with_wake_contract(payload)


def _with_wake_contract(
    payload: dict[str, Any],
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    global _last_wake_presence
    realtime = payload.get("realtime")
    realtime = realtime if isinstance(realtime, dict) else {}
    status = str(realtime.get("online_status") or "unknown").strip().lower()
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
    confidence = 0.9 if presence != "unknown" and not realtime.get("error") else 0.1
    transition = ""
    if _last_wake_presence != "unknown" and presence != _last_wake_presence:
        transition = f"{_last_wake_presence}->{presence}"
    if presence != "unknown":
        _last_wake_presence = presence
    observed = observed_at or _parse_observed_at(realtime.get("fetched_at"))
    original = dict(payload)
    return {
        **original,
        "presence": presence,
        "interruptibility": interruptibility,
        "confidence": confidence,
        "transition": transition,
        "observed_at": observed.isoformat(),
        "expires_at": (observed + timedelta(minutes=5)).isoformat(),
        "payload": original,
    }


def _parse_observed_at(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

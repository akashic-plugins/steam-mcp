from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.etree import ElementTree

from mcp.server import FastMCP

from http_client import HttpClient

mcp = FastMCP("steam-web-api")
RUNTIME_DIR = Path(os.environ.get("AKA_PLUGIN_DATA_DIR", "").strip() or Path.cwd())
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
http_client = HttpClient(config_path=str(RUNTIME_DIR / "steam_mcp_config.json"))
SUPPORTED_FORMATS = {"json", "xml", "vdf"}
MAX_STEAM_IDS_PER_REQUEST = 100
MAX_PUBLISHED_FILE_IDS_PER_REQUEST = 100
VALID_RELATIONSHIPS = {"all", "friend"}
COMMUNITY_PROFILE_TIMEOUT_SECONDS = 1.5
COMMUNITY_PROFILE_MAX_WORKERS = 4
STEAM_USER_CACHE_PATH = RUNTIME_DIR / "steam_user_cache.json"
STEAM_APP_CACHE_PATH = RUNTIME_DIR / "steam_app_cache.json"
STORE_APPDETAILS_TIMEOUT_SECONDS = 3.0
APP_NAME_MAX_WORKERS = 4


# 1. 获取指定游戏的新闻列表。
# 2. 支持限制新闻条数、内容长度和输出格式。
@mcp.tool()
def get_news_for_app(
    app_id: int,
    count: int = 3,
    max_length: int = 300,
    format: str = "json",
):
    """获取指定 Steam 游戏的最新新闻。"""
    # 1. 校验基础参数，避免无效请求直接打到远端接口。
    # 2. 调用 Steam 新闻接口获取指定 app 的新闻数据。
    # 3. 按统一格式返回 JSON 或原始文本内容。
    _validate_positive_int(app_id, "app_id")
    _validate_positive_int(count, "count")
    _validate_positive_int(max_length, "max_length")
    _validate_format(format)

    response = http_client.get(
        path="ISteamNews/GetNewsForApp/v0002",
        params={
            "appid": app_id,
            "count": count,
            "maxlength": max_length,
        },
        response_format=format,
    )
    return _serialize_response(response.data, response.status_code, response.format)


# 1. 批量获取一个或多个 Steam 用户的公开资料。
# 2. 该接口依赖项目配置文件中的 Steam API Key。
@mcp.tool()
def get_player_summaries(steamids: list[str] | str, format: str = "json"):
    """获取一个或多个 Steam 玩家摘要信息，需要在 `steam_mcp_config.json` 中配置 `steam_api_key`。"""
    # 1. 校验输出格式并标准化 SteamID 输入。
    # 2. 调用官方资料接口批量获取玩家摘要。
    # 3. 返回统一封装后的接口结果。
    _validate_format(format)
    steam_ids_text = _normalize_steamids(steamids)

    response = http_client.get(
        path="ISteamUser/GetPlayerSummaries/v0002",
        params={"steamids": steam_ids_text},
        response_format=format,
        require_key=True,
    )
    return _serialize_response(response.data, response.status_code, response.format)


# 1. 获取玩家拥有的游戏列表。
# 2. 自动缓存游戏名并把所有 playtime 字段转换为小时。
@mcp.tool()
def get_owned_games(
    steamid: str,
    include_appinfo: bool = True,
    include_played_free_games: bool = True,
    appids_filter: list[int] | None = None,
    format: str = "json",
):
    """获取玩家拥有的游戏列表，需要配置 `steam_api_key`。"""
    # 1. 校验格式和 SteamID，并构造 input_json 请求体。
    # 2. 调用拥有游戏接口获取完整游戏库。
    # 3. 将返回中的游戏名写入本地缓存。
    # 4. 将所有 playtime_* 字段转换为小时浮点数。
    _validate_format(format)
    steam_id_text = _normalize_single_steamid(steamid)
    payload = {
        "steamid": steam_id_text,
        "include_appinfo": include_appinfo,
        "include_played_free_games": include_played_free_games,
    }
    if appids_filter:
        payload["appids_filter"] = _normalize_int_list(appids_filter, "appids_filter")

    response = http_client.get(
        path="IPlayerService/GetOwnedGames/v0001",
        params={"input_json": _json_compact(payload)},
        response_format=format,
        require_key=True,
    )
    data = response.data
    if format == "json":
        games = data.get("response", {}).get("games", [])
        _cache_app_names_from_games(games)
        _enrich_games_with_app_names(games)
    return _serialize_response(data, response.status_code, response.format)


# 1. 获取玩家最近游玩的游戏列表。
# 2. 自动补齐游戏名并将游玩时长转换为小时。
@mcp.tool()
def get_recently_played_games(steamid: str, count: int | None = None, format: str = "json"):
    """获取玩家最近玩过的游戏，需要配置 `steam_api_key`。"""
    # 1. 校验格式、SteamID 和可选的返回条数。
    # 2. 调用最近游玩接口获取游戏记录。
    # 3. 将结果中的游戏名写入缓存并统一时长单位。
    _validate_format(format)
    steam_id_text = _normalize_single_steamid(steamid)
    payload: dict[str, object] = {"steamid": steam_id_text}
    if count is not None:
        _validate_positive_int(count, "count")
        payload["count"] = count

    response = http_client.get(
        path="IPlayerService/GetRecentlyPlayedGames/v0001",
        params={"input_json": _json_compact(payload)},
        response_format=format,
        require_key=True,
    )
    data = response.data
    if format == "json":
        games = data.get("response", {}).get("games", [])
        _cache_app_names_from_games(games)
        _enrich_games_with_app_names(games)
    return _serialize_response(data, response.status_code, response.format)


# 1. 获取玩家好友列表。
# 2. 自动补齐好友昵称，并返回好友 ID 到昵称的映射。
@mcp.tool()
def get_friend_list(steamid: str, relationship: str = "friend", format: str = "json"):
    """获取玩家好友列表，需要配置 `steam_api_key`。"""
    # 1. 校验格式、SteamID 和 relationship 参数。
    # 2. 调用好友列表接口获取原始好友数据。
    # 3. 通过缓存、社区页面和官方 API 解析好友昵称。
    # 4. 返回增强后的好友列表和 friend_name_map。
    _validate_format(format)
    steam_id_text = _normalize_single_steamid(steamid)
    if relationship not in VALID_RELATIONSHIPS:
        raise ValueError("relationship must be one of: all, friend")

    response = http_client.get(
        path="ISteamUser/GetFriendList/v0001",
        params={
            "steamid": steam_id_text,
            "relationship": relationship,
        },
        response_format=format,
        require_key=True,
    )
    data = response.data
    if format != "json":
        return _serialize_response(data, response.status_code, response.format)

    friends = data.get("friendslist", {}).get("friends", [])
    friend_name_map = _build_friend_name_map(friends)
    enriched_friends = [
        {
            **friend,
            "personaname": friend_name_map.get(friend.get("steamid")),
        }
        for friend in friends
    ]
    return {
        "friendslist": {
            **data.get("friendslist", {}),
            "friends": enriched_friends,
        },
        "friend_name_map": friend_name_map,
    }


# 1. 获取玩家在指定游戏中的成就列表。
# 2. 顺手把 gameName 写入本地 app 名缓存。
@mcp.tool()
def get_player_achievements(steamid: str, app_id: int, language: str | None = None, format: str = "json"):
    """获取玩家在指定游戏中的成就，需要配置 `steam_api_key`。"""
    # 1. 校验格式、SteamID 和 AppID。
    # 2. 按需附带语言参数查询玩家成就。
    # 3. 若返回 gameName，则同步写入 app 名缓存。
    _validate_format(format)
    steam_id_text = _normalize_single_steamid(steamid)
    _validate_positive_int(app_id, "app_id")
    params: dict[str, object] = {
        "steamid": steam_id_text,
        "appid": app_id,
    }
    if language:
        params["l"] = language

    response = http_client.get(
        path="ISteamUserStats/GetPlayerAchievements/v0001",
        params=params,
        response_format=format,
        require_key=True,
    )
    data = response.data
    if format == "json":
        _cache_single_app_name(str(app_id), data.get("playerstats", {}).get("gameName"))
    return _serialize_response(data, response.status_code, response.format)


# 1. 获取玩家在指定游戏中的统计数据。
# 2. 顺手把 gameName 写入本地 app 名缓存。
@mcp.tool()
def get_user_stats_for_game(steamid: str, app_id: int, format: str = "json"):
    """获取玩家在指定游戏中的统计数据，需要配置 `steam_api_key`。"""
    # 1. 校验格式、SteamID 和 AppID。
    # 2. 调用用户游戏统计接口获取数据。
    # 3. 若返回 gameName，则同步更新本地 app 名缓存。
    _validate_format(format)
    steam_id_text = _normalize_single_steamid(steamid)
    _validate_positive_int(app_id, "app_id")

    response = http_client.get(
        path="ISteamUserStats/GetUserStatsForGame/v0002",
        params={
            "steamid": steam_id_text,
            "appid": app_id,
        },
        response_format=format,
        require_key=True,
    )
    data = response.data
    if format == "json":
        _cache_single_app_name(str(app_id), data.get("playerstats", {}).get("gameName"))
    return _serialize_response(data, response.status_code, response.format)


# 1. 获取指定游戏当前在线人数。
# 2. 自动补齐并返回 app_name。
@mcp.tool()
def get_number_of_current_players(app_id: int, format: str = "json"):
    """获取指定游戏当前在线人数。"""
    # 1. 校验 AppID 和格式。
    # 2. 调用在线人数接口获取 player_count。
    # 3. 通过缓存或商店接口补齐 app_name。
    _validate_positive_int(app_id, "app_id")
    _validate_format(format)

    response = http_client.get(
        path="ISteamUserStats/GetNumberOfCurrentPlayers/v1",
        params={"appid": app_id},
        response_format=format,
    )
    data = response.data
    if format == "json":
        app_name = _get_app_names([str(app_id)]).get(str(app_id))
        if app_name:
            _cache_single_app_name(str(app_id), app_name)
        data.setdefault("response", {})["app_name"] = app_name
    return _serialize_response(data, response.status_code, response.format)


# 1. 批量解析 appid 对应的游戏名。
# 2. 优先读取缓存，缺失时并发查询商店接口。
@mcp.tool()
def resolve_app_ids(appids: list[int] | list[str] | str):
    """批量解析 appid 对应的游戏名。"""
    # 1. 标准化传入的 appid 列表。
    # 2. 通过缓存和商店接口批量解析游戏名。
    # 3. 返回 app_name_map 供上层直接消费。
    app_id_values = _normalize_appids(appids)
    return {
        "app_name_map": _get_app_names(app_id_values)
    }


# 1. 校验输出格式是否在支持列表中。
def _validate_format(format: str) -> None:
    if format not in SUPPORTED_FORMATS:
        raise ValueError("format must be one of: json, xml, vdf")


# 1. 校验正整数参数，避免 0 或负数进入业务流程。
def _validate_positive_int(value: int, field_name: str) -> None:
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


# 1. 标准化单个 SteamID。
# 2. 保证其为纯数字字符串。
def _normalize_single_steamid(steamid: str | int) -> str:
    value = str(steamid).strip()
    if not value or not value.isdigit():
        raise ValueError("steamid must be a 64-bit numeric string")
    return value


# 1. 标准化多个 SteamID。
# 2. 限制最大数量为 100。
def _normalize_steamids(steamids: list[str] | str) -> str:
    if isinstance(steamids, str):
        items = [item.strip() for item in steamids.split(",") if item.strip()]
    else:
        items = [str(item).strip() for item in steamids if str(item).strip()]

    if not items:
        raise ValueError("steamids must contain at least one Steam ID")
    if len(items) > MAX_STEAM_IDS_PER_REQUEST:
        raise ValueError("steamids cannot contain more than 100 Steam IDs")
    if any(not item.isdigit() for item in items):
        raise ValueError("each Steam ID must be a 64-bit numeric string")

    return ",".join(items)


# 1. 标准化创意工坊文件 ID 列表。
# 2. 保证每个 ID 都是数字字符串。
def _normalize_published_file_ids(publishedfileids: list[str | int]) -> list[str]:
    items = [str(item).strip() for item in publishedfileids if str(item).strip()]
    if not items:
        raise ValueError("publishedfileids must contain at least one ID")
    if len(items) > MAX_PUBLISHED_FILE_IDS_PER_REQUEST:
        raise ValueError("publishedfileids cannot contain more than 100 IDs")
    if any(not item.isdigit() for item in items):
        raise ValueError("each published file ID must be numeric")
    return items


# 1. 标准化 appid 列表。
# 2. 保证每个 appid 都是数字字符串。
def _normalize_appids(appids: list[int] | list[str] | str) -> list[str]:
    if isinstance(appids, str):
        items = [item.strip() for item in appids.split(",") if item.strip()]
    else:
        items = [str(item).strip() for item in appids if str(item).strip()]

    if not items:
        raise ValueError("appids must contain at least one appid")
    if any(not item.isdigit() for item in items):
        raise ValueError("each appid must be numeric")
    return items


# 1. 标准化整型列表。
# 2. 确保每个值都是正整数。
def _normalize_int_list(values: list[int], field_name: str) -> list[int]:
    normalized = []
    for value in values:
        integer_value = int(value)
        _validate_positive_int(integer_value, field_name)
        normalized.append(integer_value)
    return normalized


# 1. 将对象序列化为紧凑 JSON。
# 2. 用于 Steam input_json 请求参数。
def _json_compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


# 1. 统一封装所有工具的返回格式。
# 2. JSON 直接返回对象，非 JSON 返回 format/status_code/content。
def _serialize_response(data, status_code: int, format: str):
    if format == "json":
        return data

    return {
        "format": format,
        "status_code": status_code,
        "content": data,
    }


# 1. 为好友列表构建 steamid -> personaname 映射。
# 2. 优先缓存，其次社区页面，最后官方 API 回退。
def _build_friend_name_map(friends: list[dict]) -> dict[str, str]:
    if not friends:
        return {}

    steam_ids = [friend["steamid"] for friend in friends if friend.get("steamid")]
    cached_names = _load_cached_user_names()
    resolved_names = {steam_id: cached_names[steam_id] for steam_id in steam_ids if steam_id in cached_names}
    unresolved_ids = [steam_id for steam_id in steam_ids if steam_id not in resolved_names]
    community_names = _fetch_names_from_community(unresolved_ids)
    unresolved_ids = [steam_id for steam_id in unresolved_ids if steam_id not in community_names]
    api_names = _fetch_names_from_api(unresolved_ids) if unresolved_ids else {}
    resolved_names.update(api_names)
    resolved_names.update(community_names)
    _save_cached_user_names(resolved_names)
    return resolved_names


# 1. 并发从 Steam Community 页面拉取昵称。
# 2. 只返回成功解析到的 steamid -> personaname。
def _fetch_names_from_community(steam_ids: list[str]) -> dict[str, str]:
    if not steam_ids:
        return {}

    names: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(COMMUNITY_PROFILE_MAX_WORKERS, len(steam_ids))) as executor:
        futures = {executor.submit(_fetch_single_name_from_community, steam_id): steam_id for steam_id in steam_ids}
        for future in as_completed(futures):
            result = future.result()
            if result:
                names[result["steamid"]] = result["personaname"]
    return names


# 1. 解析单个 Steam Community XML 页面。
# 2. 提取 steamid 和 personaname。
def _fetch_single_name_from_community(steam_id: str) -> dict | None:
    try:
        xml_text = http_client.get_text(
            f"https://steamcommunity.com/profiles/{steam_id}?xml=1",
            timeout=COMMUNITY_PROFILE_TIMEOUT_SECONDS,
        )
        root = ElementTree.fromstring(xml_text)
    except Exception:
        return None

    steam_id_node = root.findtext("steamID64")
    persona_name = root.findtext("steamID")
    if not steam_id_node or not persona_name:
        return None

    return {
        "steamid": steam_id_node,
        "personaname": persona_name,
    }


# 1. 批量调用 GetPlayerSummaries 作为好友昵称回退源。
# 2. 返回 steamid -> personaname。
def _fetch_names_from_api(steam_ids: list[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for start in range(0, len(steam_ids), MAX_STEAM_IDS_PER_REQUEST):
        chunk = steam_ids[start : start + MAX_STEAM_IDS_PER_REQUEST]
        response = http_client.get(
            path="ISteamUser/GetPlayerSummaries/v0002",
            params={"steamids": ",".join(chunk)},
            response_format="json",
            require_key=True,
        )
        for player in response.data.get("response", {}).get("players", []):
            personaname = player.get("personaname")
            if personaname:
                names[player["steamid"]] = personaname
    return names


# 1. 读取本地用户昵称缓存。
# 2. 只保留合法的 steamid -> personaname 数据。
def _load_cached_user_names() -> dict[str, str]:
    if not STEAM_USER_CACHE_PATH.exists():
        return {}

    try:
        data = json.loads(STEAM_USER_CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(data, dict):
        return {}

    cached_names: dict[str, str] = {}
    for steam_id, personaname in data.items():
        steam_id_text = str(steam_id).strip()
        personaname_text = str(personaname).strip()
        if steam_id_text.isdigit() and personaname_text:
            cached_names[steam_id_text] = personaname_text
    return cached_names


# 1. 写入本地用户昵称缓存。
# 2. 新结果会覆盖同 steamid 的旧昵称。
def _save_cached_user_names(names: dict[str, str]) -> None:
    cached_names = _load_cached_user_names()
    cached_names.update({steam_id: personaname for steam_id, personaname in names.items() if personaname})
    STEAM_USER_CACHE_PATH.write_text(
        json.dumps(dict(sorted(cached_names.items())), ensure_ascii=False, indent=2) + "\n"
    )


# 1. 为游戏列表补齐名字并统一 playtime 单位。
# 2. 缺名字时会并发走 appid -> name 解析。
def _enrich_games_with_app_names(games: list[dict]) -> None:
    missing_app_ids = [str(game["appid"]) for game in games if game.get("appid") and not game.get("name")]
    resolved_names = _get_app_names(missing_app_ids)
    for game in games:
        _convert_game_playtimes_to_hours(game)
        app_id = game.get("appid")
        if app_id and not game.get("name"):
            game["name"] = resolved_names.get(str(app_id))


# 1. 批量解析 appid -> name。
# 2. 优先缓存，缺失时并发请求商店接口，再回填缓存。
def _get_app_names(app_ids: list[str]) -> dict[str, str | None]:
    unique_app_ids = []
    seen = set()
    for app_id in app_ids:
        app_id_text = str(app_id).strip()
        if app_id_text and app_id_text not in seen:
            seen.add(app_id_text)
            unique_app_ids.append(app_id_text)

    if not unique_app_ids:
        return {}

    cached_app_names = _load_cached_app_names()
    resolved_names: dict[str, str | None] = {
        app_id: cached_app_names.get(app_id)
        for app_id in unique_app_ids
        if cached_app_names.get(app_id)
    }
    missing_app_ids = [app_id for app_id in unique_app_ids if app_id not in resolved_names]

    if missing_app_ids:
        with ThreadPoolExecutor(max_workers=min(APP_NAME_MAX_WORKERS, len(missing_app_ids))) as executor:
            futures = {executor.submit(_fetch_app_name_from_store, app_id): app_id for app_id in missing_app_ids}
            for future in as_completed(futures):
                app_id = futures[future]
                resolved_names[app_id] = future.result()

        cached_app_names.update({app_id: name for app_id, name in resolved_names.items() if name})
        _save_cached_app_names(cached_app_names)

    return {app_id: resolved_names.get(app_id) for app_id in unique_app_ids}


# 1. 读取本地游戏名缓存。
# 2. 只保留合法的 appid -> name 数据。
def _load_cached_app_names() -> dict[str, str]:
    if not STEAM_APP_CACHE_PATH.exists():
        return {}

    try:
        data = json.loads(STEAM_APP_CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(data, dict):
        return {}

    app_names: dict[str, str] = {}
    for app_id, name in data.items():
        app_id_text = str(app_id).strip()
        name_text = str(name).strip()
        if app_id_text.isdigit() and name_text:
            app_names[app_id_text] = name_text
    return app_names


# 1. 写入本地游戏名缓存。
# 2. 按 appid 排序落盘，方便后续查看。
def _save_cached_app_names(app_names: dict[str, str]) -> None:
    STEAM_APP_CACHE_PATH.write_text(
        json.dumps(dict(sorted(app_names.items())), ensure_ascii=False, indent=2) + "\n"
    )


# 1. 从游戏列表响应中提取 appid 和 name。
# 2. 顺手将已有游戏名写入本地缓存。
def _cache_app_names_from_games(games: list[dict]) -> None:
    app_names: dict[str, str] = {}
    for game in games:
        app_id = str(game.get("appid", "")).strip()
        name = str(game.get("name", "")).strip()
        if app_id.isdigit() and name:
            app_names[app_id] = name

    if not app_names:
        return

    cached_app_names = _load_cached_app_names()
    cached_app_names.update(app_names)
    _save_cached_app_names(cached_app_names)


# 1. 将单个 appid 和名称写入缓存。
# 2. 常用于成就和统计接口的 gameName 回填。
def _cache_single_app_name(app_id: str, app_name: str | None) -> None:
    app_id_text = str(app_id).strip()
    app_name_text = str(app_name or "").strip()
    if not app_id_text.isdigit() or not app_name_text:
        return

    cached_app_names = _load_cached_app_names()
    cached_app_names[app_id_text] = app_name_text
    _save_cached_app_names(cached_app_names)


# 1. 从 Steam 商店 appdetails 接口获取单个游戏名。
# 2. 该方法是 app 名缓存未命中时的兜底来源。
def _fetch_app_name_from_store(app_id: str) -> str | None:
    data = http_client.get_json_url(
        f"https://store.steampowered.com/api/appdetails?appids={app_id}&filters=basic",
        timeout=STORE_APPDETAILS_TIMEOUT_SECONDS,
    )
    app_details = data.get(app_id, {})
    if not app_details.get("success"):
        return None

    name = str(app_details.get("data", {}).get("name", "")).strip()
    return name or None


# 1. 将 playtime_* 字段从分钟转换为小时。
# 2. 保留 1 位小数，直接覆盖原字段值。
def _convert_game_playtimes_to_hours(game: dict) -> None:
    for key, value in list(game.items()):
        if key.startswith("playtime_") and isinstance(value, int | float):
            game[key] = round(value / 60, 1)


if __name__ == "__main__":
    mcp.run()

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SteamRuntimeConfig:
    steam_api_key: str
    steam_id: str
    snapshot_interval_seconds: int


def load_runtime_config(path: Path) -> SteamRuntimeConfig:
    """读取并校验 formal Steam runtime 配置。"""

    # 1. 配置只来自 formal plugin-data，不接受 ambient secret
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError("Steam formal runtime 缺少 steam_mcp_config.json") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("steam_mcp_config.json 不是合法 JSON") from error
    if not isinstance(raw, dict):
        raise RuntimeError("steam_mcp_config.json 根节点必须是 object")

    # 2. 启动前建立完整 credential 与用户身份不变量
    api_key = raw.get("steam_api_key")
    steam_id = raw.get("steam_id")
    if not isinstance(api_key, str) or not api_key.strip():
        raise RuntimeError("steam_mcp_config.json 缺少 steam_api_key")
    if not isinstance(steam_id, str) or not steam_id.strip():
        raise RuntimeError("steam_mcp_config.json 缺少 steam_id")
    interval = raw.get("snapshot_interval_seconds", 6 * 3600)
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 300:
        raise RuntimeError("snapshot_interval_seconds 必须是大于等于 300 的整数")
    return SteamRuntimeConfig(api_key.strip(), steam_id.strip(), interval)

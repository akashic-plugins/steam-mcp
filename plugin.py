from __future__ import annotations

import shutil
from pathlib import Path

from agent.plugins import Plugin


class SteamPlugin(Plugin):
    name = "steam"
    version = "0.1.0"
    desc = "Steam MCP plugin"

    async def initialize(self) -> None:
        data_dir = self.context.data_dir
        workspace = self.context.workspace
        if data_dir is None or workspace is None:
            return
        data_dir.mkdir(parents=True, exist_ok=True)
        if _has_state(data_dir):
            return
        _copy_legacy_state(workspace / "mcp" / "steam-mcp", data_dir)


def _has_state(data_dir: Path) -> bool:
    for name in (
        "steam_mcp_config.json",
        "steam_user_cache.json",
        "steam_app_cache.json",
        "steam_proactive.sqlite3",
    ):
        if (data_dir / name).exists():
            return True
    return False


def _copy_legacy_state(source_dir: Path, data_dir: Path) -> None:
    if not source_dir.exists():
        return
    for name in (
        "steam_mcp_config.json",
        "steam_user_cache.json",
        "steam_app_cache.json",
        "steam_proactive.sqlite3",
    ):
        source = source_dir / name
        target = data_dir / name
        if not source.exists() or target.exists():
            continue
        shutil.copy2(source, target)

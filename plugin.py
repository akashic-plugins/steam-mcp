from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field

from agent.plugins import McpServerSpec, Plugin, ProactiveSourceSpec


class SteamProactiveConfig(BaseModel):
    enabled: bool = True


class SteamConfig(BaseModel):
    proactive: SteamProactiveConfig = Field(default_factory=SteamProactiveConfig)


class SteamPlugin(Plugin):
    name = "steam"
    version = "1.0.0"
    desc = "Steam MCP plugin"
    ConfigModel = SteamConfig

    @classmethod
    def skill_roots(cls) -> tuple[str, ...]:
        return ("skills",)

    @classmethod
    def mcp_servers(cls) -> list[McpServerSpec]:
        return [
            McpServerSpec(
                name="steam",
                command=("python", "mcp/run_mcp.py"),
            )
        ]

    def proactive_sources(self) -> list[ProactiveSourceSpec]:
        config = cast(SteamConfig, self.context.config)
        if not config.proactive.enabled:
            return []
        return [
            ProactiveSourceSpec(
                id="presence",
                channels=("context",),
                server="steam",
                fetch_tool="get_steam_context",
            )
        ]

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

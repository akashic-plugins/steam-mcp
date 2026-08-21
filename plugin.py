from __future__ import annotations

from pydantic import BaseModel, Field

from agent.plugin_composition import (
    MCP_SERVERS,
    PROACTIVE_COMPONENTS,
    Context,
    McpServerDefinition,
    ProactiveSourceDefinition,
)


class SteamProactiveConfig(BaseModel):
    enabled: bool = True


class SteamConfig(BaseModel):
    proactive: SteamProactiveConfig = Field(default_factory=SteamProactiveConfig)


api_version = 3
name = "steam"
version = "3.0.0"
desc = "Steam MCP plugin"
Config = SteamConfig
inject = (MCP_SERVERS, PROACTIVE_COMPONENTS)
skill_roots = ("skills",)


async def apply(ctx: Context, config: object) -> None:
    """声明 Steam MCP 与可选的主动上下文源。"""

    if not isinstance(config, SteamConfig):
        raise TypeError("steam config 必须是 SteamConfig")

    # 1. MCP 由 Core staged Python runtime 启动，candidate 仅开放 recording 上下文
    await ctx.require(MCP_SERVERS).register(
        ctx,
        McpServerDefinition(
            name="steam",
            command=("python", "mcp/run_mcp.py"),
            required_tools=("get_steam_context",),
            candidate_read_only_tools=("get_steam_context",),
            candidate_env={"STEAM_BACKEND": "recording"},
        ),
    )

    # 2. 主动源只消费明确的 FetchItems/FetchEmpty 结果
    if config.proactive.enabled:
        await ctx.require(PROACTIVE_COMPONENTS).register(
            ctx,
            ProactiveSourceDefinition(
                name="presence",
                channels=("context",),
                mcp_server="steam",
                fetch_tool="get_steam_context",
            ),
        )

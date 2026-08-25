from __future__ import annotations

from pydantic import BaseModel

from agent.lifecycle.composition import CONTEXT_PREPARED_EVENT
from agent.plugin_composition import (
    MCP_SERVERS,
    RUNTIME_STARTED,
    RUNTIME_STOPPING,
    TIMERS,
    Context,
    McpServerDefinition,
)

from .context_source import SteamContextRuntime


class SteamConfig(BaseModel):
    pass


api_version = 3
name = "steam"
version = "3.1.0"
desc = "Timer 刷新的 Steam current context 与用户 MCP"
Config = SteamConfig
inject = (MCP_SERVERS, TIMERS)
skill_roots = ("skills",)


async def apply(ctx: Context, config: object) -> None:
    """组合用户 MCP、Timer current state 和 Wake context listener。"""

    if not isinstance(config, SteamConfig):
        raise TypeError("steam config 必须是 SteamConfig")

    # 1. MCP 只保留用户主动查询；candidate 只完成隔离握手。
    await ctx.require(MCP_SERVERS).register(
        ctx,
        McpServerDefinition(
            name="steam",
            command=("python", "mcp/run_mcp.py"),
            required_tools=("get_player_summaries",),
            candidate_read_only_tools=(),
            candidate_env={"STEAM_BACKEND": "recording"},
        ),
    )

    # 2. 正式 Root 独占 Timer 刷新；listener 只读 current state。
    health = await ctx.health("context-refresh", required=True)
    runtime = SteamContextRuntime(
        ctx.data_root,
        ctx.require(TIMERS),
        health,
        ctx.report_incident,
    )

    def setup() -> object:
        return runtime.close

    _ = await ctx.effect(setup, label="steam-context-runtime")
    _ = await ctx.on(CONTEXT_PREPARED_EVENT, runtime.prepare)
    _ = await ctx.on(RUNTIME_STARTED, lambda _: runtime.start())
    _ = await ctx.on(RUNTIME_STOPPING, lambda _: runtime.close())

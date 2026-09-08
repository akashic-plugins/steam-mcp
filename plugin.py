from __future__ import annotations

from plugins.tools.plugin import TOOLS
from .tools import register_tools

from pydantic import BaseModel

from agent.plugin_composition import (
    MCP_SERVERS,
    RUNTIME_STARTED,
    RUNTIME_STOPPING,
    TIMERS,
    Context,
    McpServerDefinition,
)
from .context_source import SteamContextRuntime
from .eventmail import EVENTMAIL_CONTEXT_SOURCE


class SteamConfig(BaseModel):
    pass


api_version = 3
name = "steam"
version = "3.2.2"
desc = "Timer 上报的 Steam current Context 与用户 MCP"
Config = SteamConfig
inject = (TOOLS, MCP_SERVERS, TIMERS)
skill_roots = ("skills",)


async def apply(ctx: Context, config: object) -> None:
    """组合用户 MCP、Timer current state 和 Wake Context 上报。"""

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

    await register_tools(ctx)

    # 2. EventMail 存在时，独立子 Fiber 才刷新 current state。
    async def apply_eventmail(source_ctx: Context) -> None:
        health = await source_ctx.health("context-refresh", required=True)
        runtime = SteamContextRuntime(
            source_ctx.data_root,
            source_ctx.require(TIMERS),
            health,
            source_ctx.report_incident,
            source_ctx.require(EVENTMAIL_CONTEXT_SOURCE).bind("steam-presence"),
        )

        def setup() -> object:
            return runtime.close

        _ = await source_ctx.effect(setup, label="steam-context-runtime")
        _ = await source_ctx.on(RUNTIME_STARTED, lambda _: runtime.start())
        _ = await source_ctx.on(RUNTIME_STOPPING, lambda _: runtime.close())

    _ = await ctx.inject(
        (TIMERS, EVENTMAIL_CONTEXT_SOURCE),
        apply_eventmail,
        name="steam-eventmail-source",
    )

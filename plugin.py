from __future__ import annotations

from plugins.tools.contract import TOOLS
from .tools import register_tools

from pydantic import BaseModel

from agent.plugin_composition import RUNTIME_STARTED, RUNTIME_STOPPING, Context
from plugins.mcp.contract import MCP_SERVERS, McpServerDefinition
from plugins.timer.contract import TIMERS
from .context_source import SteamContextRuntime
from plugins.watch.contract import WATCH_EVENTS


class SteamConfig(BaseModel):
    pass


api_version = 3
name = "steam"
version = "3.3.0"
desc = "Steam 当前状态交给值班与用户 MCP"
inject = (TOOLS, MCP_SERVERS, TIMERS)
skill_roots = ("skills",)


async def apply(ctx: Context) -> None:
    """组合用户 MCP、Timer current state 和值班状态事件。"""
    _ = SteamConfig.model_validate(ctx.config)

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

    await register_tools(ctx, description=desc)

    # 2. 值班存在时，独立子 Fiber 才刷新 current state 并交给它。
    async def apply_watch(source_ctx: Context) -> None:
        health = await source_ctx.health("context-refresh", required=True)
        runtime = SteamContextRuntime(
            source_ctx.data_root,
            source_ctx.require(TIMERS),
            health,
            source_ctx.report_incident,
            source_ctx.require(WATCH_EVENTS),
        )

        def setup() -> object:
            return runtime.close

        _ = await source_ctx.effect(setup, label="steam-context-runtime")
        _ = await source_ctx.on(RUNTIME_STARTED, lambda _: runtime.start())
        _ = await source_ctx.on(RUNTIME_STOPPING, lambda _: runtime.close())

    _ = await ctx.inject(
        (TIMERS, WATCH_EVENTS),
        apply_watch,
        name="steam-watch-source",
    )

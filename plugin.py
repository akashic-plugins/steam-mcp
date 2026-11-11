from __future__ import annotations

from ._tool_contract import TOOLS
from .tools import register_tools

from collections.abc import Mapping

from pydantic import BaseModel

from agent.plugin_composition import RUNTIME_STARTED, RUNTIME_STOPPING, Context
from plugins.mcp.contract import MCP_SERVERS, McpServerDefinition
from plugins.timer.contract import TIMERS
from .context_source import SteamContextRuntime
from .eventmail import EVENTMAIL_CONTEXT_SOURCE
from .watch import WATCH_EVENTS


class SteamConfig(BaseModel):
    pass


api_version = 3
name = "steam"
version = "3.2.4"
desc = "Timer 上报的 Steam current Context 与用户 MCP"
inject = (TOOLS, MCP_SERVERS, TIMERS)
skill_roots = ("skills",)


async def apply(ctx: Context) -> None:
    """组合用户 MCP、Timer current state 和 Wake Context 上报。"""
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

    # 2. EventMail 存在时，独立子 Fiber 才刷新 current state。
    async def apply_eventmail(source_ctx: Context) -> None:
        health = await source_ctx.health("context-refresh", required=True)
        source = source_ctx.require(EVENTMAIL_CONTEXT_SOURCE).bind("steam-presence")
        _ = await source_ctx.effect(lambda: source.close, label="steam-eventmail-source")
        async def current_state(current: Mapping[str, object]) -> None:
            # 只说对值班有意义的那一层：在玩什么、在线、离线；状态没变时值班不会记成新事件。
            playing = current.get("currently_playing")
            text = f"正在玩 {playing}" if playing else "Steam 离线" if current.get("presence") == "offline" else "Steam 在线，没在玩游戏"
            try:
                async with source_ctx.runtime_scope():
                    with source_ctx.borrow(WATCH_EVENTS) as events:
                        if events is not None:
                            _ = await events.emit("steam-presence", text=text, state=True)
            except Exception as error:
                # 值班不可用不影响 Steam 状态本身的刷新
                _ = source_ctx.report_incident("steam_watch_emit_failed", f"{type(error).__name__}: {error}"[:300])

        runtime = SteamContextRuntime(
            source_ctx.data_root,
            source_ctx.require(TIMERS),
            health,
            source_ctx.report_incident,
            source,
            current_state=current_state,
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

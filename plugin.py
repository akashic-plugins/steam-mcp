from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from plugins.tools.contract import TOOLS
from .tools import register_tools

from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import CREDENTIALS, RUNTIME_STARTED, RUNTIME_STOPPING, Context, CredentialRef
from agent.plugin_composition.plugin_config import PLUGIN_CONFIG
from plugins.mcp.contract import MCP_SERVERS, McpServerDefinition
from plugins.onboarding.contract import ONBOARDING, Ability, PreviewLine
from plugins.plugin_settings.contract import SETTINGS_FORMS, SettingsField, SettingsForm, Setup, declare_settings, declare_setup
from plugins.timer.contract import TIMERS
from .context_source import SteamContextRuntime
from plugins.watch.contract import WATCH_EVENTS


class SteamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steam_api_key: CredentialRef | None = None
    # 64 位 SteamID：个人资料页网址 /profiles/ 后面那串 17 位数字。
    steam_id: str | None = Field(default=None, pattern=r"^7656\d{13}$")
    snapshot_interval_seconds: int = Field(default=6 * 3600, ge=300)


SETTINGS_FORM = SettingsForm(
    title="Steam", description="读取你的 Steam 在线状态和最近玩的游戏，交给值班，也供聊天时查询。",
    fields=(
        SettingsField("steam_api_key", "Steam Web API Key", kind="secret",
                      hint="在 steamcommunity.com/dev/apikey 申请，域名随便填"),
        SettingsField("steam_id", "Steam ID", hint="17 位数字，个人资料页网址 /profiles/ 后面那串"),
        SettingsField("snapshot_interval_seconds", "游戏记录刷新间隔（秒）", kind="number",
                      hint="最近玩过的游戏多久记一次，最少 300"),
    ),
    setup=Setup("steam", "Steam 游戏状态", Ability(
        pitch="知道你在玩什么游戏",
        benefit="接上 Steam 后，值班能看到你在线、正在玩哪款游戏，聊天时也能查最近玩过的游戏和成就。",
        preview=(PreviewLine("你", "我这周打了多久艾尔登法环？"),
                 PreviewLine("Akashic", "这两周玩了 14 小时，昨晚在线到一点多。"))),
    ),
)
RUNTIME_CONFIG = "steam_mcp_config.json"


api_version = 3
name = "steam"
version = "3.4.0"
desc = "Steam 当前状态交给值班与用户 MCP"
inject = (TOOLS, MCP_SERVERS, TIMERS, CREDENTIALS)
skill_roots = ("skills",)


# 设置与引导只说缺什么；凭据和 Steam ID 都填了才算就绪。
async def _status(ctx: Context) -> Mapping[str, object]:
    config = SteamConfig.model_validate(ctx.config)
    missing = [label for value, label in ((config.steam_api_key, "Steam Web API Key"), (config.steam_id, "Steam ID"))
               if value is None]
    return {"ready": not missing, "reason": f"还需要填写{'和'.join(missing)}" if missing else ""}


# MCP 子进程和 Timer 刷新读同一份运行文件；它由插件配置派生，内容不变时不重写。
def _write_runtime_config(data_root: Path, api_key: str, steam_id: str, interval: int) -> None:
    path = data_root / RUNTIME_CONFIG
    body = json.dumps({"steam_api_key": api_key, "steam_id": steam_id, "snapshot_interval_seconds": interval},
                      ensure_ascii=False, indent=2) + "\n"
    try:
        if path.read_text(encoding="utf-8") == body:
            return
    except FileNotFoundError:
        pass
    temporary = path.with_name(f"{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        _ = handle.write(body)
    os.replace(temporary, path)


async def apply(ctx: Context) -> None:
    """组合用户 MCP、Timer current state 和值班状态事件；未配置时只提供设置与引导。"""
    config = SteamConfig.model_validate(ctx.config)

    # 1. 设置表单与引导是可选依赖，缺凭据时也要在，用户才能填。
    async def settings(child: Context) -> None:
        _ = await declare_settings(child, SteamConfig, SETTINGS_FORM, status=lambda: _status(child))

    async def setup(child: Context) -> None:
        await declare_setup(child, SteamConfig, SETTINGS_FORM, status=lambda: _status(child))

    _ = await ctx.inject((SETTINGS_FORMS, PLUGIN_CONFIG), settings, name="settings")
    _ = await ctx.inject((SETTINGS_FORMS, PLUGIN_CONFIG, ONBOARDING), setup, name="onboarding")
    if config.steam_api_key is None or config.steam_id is None:
        return
    api_key = config.steam_api_key
    async with ctx.require(CREDENTIALS).open(ctx, {"steam_api_key": api_key}) as client:
        _write_runtime_config(ctx.data_root, client.credential(api_key), config.steam_id,
                              config.snapshot_interval_seconds)

    # 2. MCP 只保留用户主动查询；candidate 只完成隔离握手。
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

    # 3. 值班存在时，独立子 Fiber 才刷新 current state 并交给它。
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

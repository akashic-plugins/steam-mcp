from __future__ import annotations

import inspect
from pathlib import Path
from typing import cast

import pytest
from plugins.tools.plugin import TOOLS, ToolCatalog
from agent.plugin_composition.tasks import TaskAdmission
from agent.control.timer import AsyncioOneShotTimer
from steam_test_plugin.tools import STEAM_TOOLS  # pyright: ignore[reportMissingImports]  # conftest 注册测试包。
from steam_test_plugin import plugin  # pyright: ignore[reportMissingImports]
from agent.plugin_composition import (
    MCP_SERVERS,
    TIMERS,
    CompositionRoot,
    PluginRuntime,
    PluginTimers,
)
from agent.plugin_composition.assets import INSTALLED_ASSETS
from agent.plugins.composable import ComposablePlugin
from agent.plugins.static_manifest import load_static_plugin_manifest
from steam_test_plugin.eventmail import EVENTMAIL_CONTEXT_SOURCE  # pyright: ignore[reportMissingImports]


ROOT = Path(__file__).resolve().parents[1]


class RecordingServers:
    def __init__(self) -> None:
        self.definitions: dict[str, object] = {}

    async def register(self, ctx: object, definition: object) -> None:
        self.definitions[definition.name] = definition


class RecordingAssets:
    def __init__(self) -> None:
        self.registrations: list[tuple[str, str]] = []

    async def register(self, ctx: object, category: str, relative_path: str) -> None:
        self.registrations.append((category, relative_path))


def test_pure_v3_exports_and_exact_apply() -> None:
    assert plugin.api_version == 3
    assert plugin.name == "steam"
    assert plugin.version == "3.2.2"
    assert tuple(inspect.signature(plugin.apply).parameters) == ("ctx",)
    composable = ComposablePlugin.from_module(
        plugin, load_static_plugin_manifest(ROOT)
    )
    assert composable.name == "steam"


@pytest.mark.asyncio
async def test_apply_registers_user_mcp_and_context_runtime(
    tmp_path: Path,
) -> None:
    root = CompositionRoot("steam:test")
    servers = RecordingServers()
    assets = RecordingAssets()
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(INSTALLED_ASSETS, assets)
    await root.context.provide(TOOLS, ToolCatalog(root.context, cast(TaskAdmission, None)))
    await root.context.provide(TIMERS, PluginTimers(AsyncioOneShotTimer()))
    class Sources:
        def bind(self, source_id: str) -> object:
            assert source_id == "steam-presence"
            return object()

    _ = await root.context.provide(EVENTMAIL_CONTEXT_SOURCE, Sources())
    data_root = tmp_path / "plugin-data"
    composable = ComposablePlugin.from_module(
        plugin, load_static_plugin_manifest(ROOT)
    )
    await root.mount(
        composable.apply,
        name="steam",
        inject=composable.inject,
        runtime=PluginRuntime(
            plugin_id="steam",
            generation_id="steam:test",
            plugin_dir=ROOT,
            data_dir=data_root,
            workspace=tmp_path / "workspace",
            config=dict(plugin.SteamConfig().model_dump()),
        ),
    )

    server = servers.definitions["steam"]
    assert server.required_tools == ("get_player_summaries",)
    assert server.candidate_read_only_tools == ()
    assert server.candidate_env == {"STEAM_BACKEND": "recording"}
    assert ("skills", "skills") in assets.registrations
    assert data_root.is_dir()
    listeners = root.topology_view().listeners
    assert listeners == (
        "serial:runtime.started:steam-eventmail-source",
        "serial:runtime.stopping:steam-eventmail-source",
    )
    assert any(item["name"].startswith("mcp_steam__") for item in (ref.description for ref in root.context.require(STEAM_TOOLS).refs))
    await root.dispose()


@pytest.mark.asyncio
async def test_apply_keeps_user_mcp_without_eventmail(tmp_path: Path) -> None:
    root = CompositionRoot("steam:without-eventmail")
    servers = RecordingServers()
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(INSTALLED_ASSETS, RecordingAssets())
    await root.context.provide(TOOLS, ToolCatalog(root.context, cast(TaskAdmission, None)))
    await root.context.provide(TIMERS, PluginTimers.candidate_validation())
    composable = ComposablePlugin.from_module(
        plugin, load_static_plugin_manifest(ROOT)
    )
    await root.mount(
        composable.apply,
        name="steam",
        inject=composable.inject,
        runtime=PluginRuntime(
            plugin_id="steam",
            generation_id="steam:without-eventmail",
            plugin_dir=ROOT,
            data_dir=tmp_path / "plugin-data",
            workspace=tmp_path / "workspace",
            config=dict(plugin.SteamConfig().model_dump()),
        ),
    )

    assert "steam" in servers.definitions
    assert any(item["name"].startswith("mcp_steam__") for item in (ref.description for ref in root.context.require(STEAM_TOOLS).refs))
    await root.dispose()


def test_static_manifest_matches_module_identity() -> None:
    manifest = load_static_plugin_manifest(ROOT)

    assert manifest.name == plugin.name == "steam"
    assert manifest.version == plugin.version == "3.2.2"
    assert manifest.api_version == plugin.api_version == 3
    assert manifest.requirements == ("mcp/requirements.txt",)

from __future__ import annotations

import inspect
from pathlib import Path
from typing import cast

from plugins.tools.plugin import TOOLS, ToolCatalog
from steam_test_plugin.tools import STEAM_TOOLS  # pyright: ignore[reportMissingImports]  # conftest 注册测试包。

import pytest
from steam_test_plugin import plugin  # pyright: ignore[reportMissingImports]
from agent.control.timer import OneShotTimer
from agent.plugin_composition import (
    MCP_SERVERS,
    TIMERS,
    CompositionRoot,
    PluginRuntime,
    PluginTimers,
)
from agent.plugin_composition.mcp_slots import (
    PluginMcpServers,
    _freeze_plugin_mcp_servers,
)
from agent.plugins.composable import ComposablePlugin
from agent.plugins.manager import _copy_validation_tree
from agent.plugins.static_manifest import load_static_plugin_manifest
from steam_test_plugin.eventmail import EVENTMAIL_CONTEXT_SOURCE  # pyright: ignore[reportMissingImports]


ROOT = Path(__file__).resolve().parents[1]


def test_pure_v3_exports_and_exact_apply() -> None:
    assert plugin.api_version == 3
    assert plugin.name == "steam"
    assert plugin.version == "3.2.2"
    assert plugin.skill_roots == ("skills",)
    assert tuple(inspect.signature(plugin.apply).parameters) == ("ctx", "config")
    assert ComposablePlugin.from_module(plugin).skill_roots == ("skills",)


@pytest.mark.asyncio
async def test_apply_registers_user_mcp_and_dormant_context_runtime(
    tmp_path: Path,
) -> None:
    root = CompositionRoot("steam:test")
    servers = PluginMcpServers(root.instance_token)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(TOOLS, ToolCatalog(root.context))
    await root.context.provide(
        TIMERS,
        PluginTimers(cast(OneShotTimer, object())),
    )
    class Sources:
        def bind(self, source_id: str) -> object:
            assert source_id == "steam-presence"
            return object()

    _ = await root.context.provide(EVENTMAIL_CONTEXT_SOURCE, Sources())
    data_root = tmp_path / "plugin-data"
    composable = ComposablePlugin.from_module(plugin)
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
            config=plugin.SteamConfig(),
        ),
    )

    server = _freeze_plugin_mcp_servers(
        servers,
        root.instance_token,
    )["steam"].definition
    assert server.required_tools == ("get_player_summaries",)
    assert server.candidate_read_only_tools == ()
    assert server.candidate_env == {"STEAM_BACKEND": "recording"}
    assert not data_root.exists()
    assert root.topology_view().listeners == (
        "serial:runtime.started:steam-eventmail-source",
        "serial:runtime.stopping:steam-eventmail-source",
    )
    assert any(item["name"].startswith("mcp_steam__") for item in (ref.description for ref in root.context.require(STEAM_TOOLS).refs))
    await root.dispose()


@pytest.mark.asyncio
async def test_apply_keeps_user_mcp_without_eventmail(tmp_path: Path) -> None:
    root = CompositionRoot("steam:without-eventmail")
    servers = PluginMcpServers(root.instance_token)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(TOOLS, ToolCatalog(root.context))
    await root.context.provide(TIMERS, PluginTimers.candidate_validation())
    composable = ComposablePlugin.from_module(plugin)
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
            config=plugin.SteamConfig(),
        ),
    )

    assert "steam" in _freeze_plugin_mcp_servers(servers, root.instance_token)
    assert any(item["name"].startswith("mcp_steam__") for item in (ref.description for ref in root.context.require(STEAM_TOOLS).refs))
    await root.dispose()


def test_static_manifest_excludes_state_and_bounded_logs() -> None:
    manifest = load_static_plugin_manifest(ROOT)

    assert manifest.name == plugin.name == "steam"
    assert manifest.version == plugin.version == "3.2.2"
    assert manifest.api_version == plugin.api_version == 3
    assert manifest.requirements == ("mcp/requirements.txt",)
    assert "steam_proactive.sqlite3" in manifest.exclude_data_paths
    assert "steam_proactive.sqlite3-wal" in manifest.exclude_data_paths
    assert "steam_context.runtime.log.3" in manifest.exclude_data_paths
    assert "steam_mcp.runtime.log.3" in manifest.exclude_data_paths
    server = manifest.mcp_servers[0]
    assert server.required_tools == ("get_player_summaries",)
    assert server.candidate_read_only_tools == ()
    assert server.candidate_env == (("STEAM_BACKEND", "recording"),)


def test_candidate_copy_excludes_formal_state_and_logs(tmp_path: Path) -> None:
    manifest = load_static_plugin_manifest(ROOT)
    source = tmp_path / "workspace" / "plugin-data" / "steam-builtin"
    source.mkdir(parents=True)
    for name in manifest.exclude_data_paths:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"formal:{name}", encoding="utf-8")
    (source / "candidate-visible.txt").write_text("visible", encoding="utf-8")
    target = tmp_path / "validation" / "steam"

    inventory = _copy_validation_tree(
        source,
        target,
        manifest.exclude_data_paths,
    )

    assert inventory == ("candidate-visible.txt",)
    assert (target / "candidate-visible.txt").read_text() == "visible"
    assert not any((target / name).exists() for name in manifest.exclude_data_paths)

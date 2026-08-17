from __future__ import annotations

import inspect
from pathlib import Path

import plugin
import pytest
from agent.plugin_composition import (
    MCP_SERVERS,
    PROACTIVE_COMPONENTS,
    CompositionRoot,
    PluginProactiveComponents,
    PluginRuntime,
)
from agent.plugin_composition.mcp_slots import (
    PluginMcpServers,
    _freeze_plugin_mcp_servers,
)
from agent.plugin_composition.proactive import _freeze_plugin_proactive_components
from agent.plugins.composable import ComposablePlugin
from agent.plugins.static_manifest import load_static_plugin_manifest


ROOT = Path(__file__).resolve().parents[1]


def test_pure_v3_exports_and_exact_apply() -> None:
    assert plugin.api_version == 3
    assert plugin.name == "steam"
    assert plugin.version == "3.0.0"
    assert plugin.skill_roots == ("skills",)
    assert tuple(inspect.signature(plugin.apply).parameters) == ("ctx", "config")
    assert ComposablePlugin.from_module(plugin).skill_roots == ("skills",)
    assert not hasattr(plugin, "SteamPlugin")


@pytest.mark.asyncio
async def test_apply_registers_mcp_and_proactive_without_data_writes(
    tmp_path: Path,
) -> None:
    root = CompositionRoot("steam:test")
    servers = PluginMcpServers(root.instance_token)
    components = PluginProactiveComponents(root.instance_token)
    await root.context.provide(MCP_SERVERS, servers)
    await root.context.provide(PROACTIVE_COMPONENTS, components)
    data_root = tmp_path / "plugin-data"
    await root.mount(
        ComposablePlugin.from_module(plugin),
        name="steam",
        runtime=PluginRuntime(
            plugin_id="steam",
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
    source = _freeze_plugin_proactive_components(
        components,
        root.instance_token,
        {"steam": "steam:test"},
    ).source("presence")
    assert server.command == ("python", "mcp/run_mcp.py")
    assert server.required_tools == ("get_steam_context",)
    assert server.candidate_read_only_tools == ("get_steam_context",)
    assert server.candidate_env == {"STEAM_BACKEND": "recording"}
    assert source is not None
    assert source.definition.mcp_server == "steam"
    assert source.definition.fetch_tool == "get_steam_context"
    assert not data_root.exists()
    await root.dispose()


def test_static_manifest_matches_module_and_recording_contract() -> None:
    manifest = load_static_plugin_manifest(ROOT)

    assert manifest.name == plugin.name == "steam"
    assert manifest.version == plugin.version == "3.0.0"
    assert manifest.api_version == plugin.api_version == 3
    assert manifest.requirements == ("mcp/requirements.txt",)
    assert manifest.exclude_data_paths == (
        "steam_mcp_config.json",
        "steam_user_cache.json",
        "steam_app_cache.json",
        "steam_proactive.sqlite3",
        ".steam-v2-migration.json",
    )
    server = manifest.mcp_servers[0]
    assert server.required_tools == ("get_steam_context",)
    assert server.candidate_read_only_tools == ("get_steam_context",)
    assert server.candidate_env == (("STEAM_BACKEND", "recording"),)

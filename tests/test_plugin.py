from __future__ import annotations

import inspect
from pathlib import Path
from typing import cast

from plugins.tools.plugin import TOOLS, ToolCatalog
from steam_test_plugin.tools import STEAM_TOOLS  # pyright: ignore[reportMissingImports]  # conftest 注册测试包。

import pytest
from steam_test_plugin import plugin  # pyright: ignore[reportMissingImports]
from agent.plugin_composition import (
    MCP_SERVERS,
    TIMERS,
    CompositionRoot,
    PluginRuntime,
    PluginTimers,
)
from agent.plugin_composition.mcp_slots import McpServerDefinition, McpServers
from agent.plugins.composable import ComposablePlugin
from agent.plugin_composition.tasks import Tasks
from agent.plugins.static_manifest import load_static_plugin_manifest


ROOT = Path(__file__).resolve().parents[1]


class _RecordingMcpServers:
    """Capture declarations; manager integration tests exercise the real provider."""

    def __init__(self) -> None:
        self.definitions: dict[str, McpServerDefinition] = {}

    async def register(self, _ctx: object, definition: McpServerDefinition) -> None:
        self.definitions[definition.name] = definition


def test_pure_v3_exports_and_exact_apply() -> None:
    assert plugin.api_version == 3
    assert plugin.name == "steam"
    assert plugin.version == "3.2.2"
    assert plugin.skill_roots == ("skills",)
    assert tuple(inspect.signature(plugin.apply).parameters) == ("ctx",)


@pytest.mark.asyncio
async def test_apply_keeps_user_mcp_without_eventmail(tmp_path: Path) -> None:
    root = CompositionRoot("steam:without-eventmail")
    servers = _RecordingMcpServers()
    await root.context.provide(MCP_SERVERS, cast(McpServers, servers))
    await root.context.provide(TOOLS, ToolCatalog(root.context, Tasks()))
    await root.context.provide(TIMERS, PluginTimers.candidate_validation())
    composable = ComposablePlugin.from_module(plugin, load_static_plugin_manifest(ROOT))
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
            config={},
        ),
    )

    server = servers.definitions["steam"]
    assert server.required_tools == ("get_player_summaries",)
    assert server.candidate_read_only_tools == ()
    assert server.candidate_env == {"STEAM_BACKEND": "recording"}
    assert not (tmp_path / "plugin-data").exists()
    assert any(item["name"].startswith("mcp_steam__") for item in (ref.description for ref in root.context.require(STEAM_TOOLS).refs))
    await root.dispose()


def test_static_manifest_declares_the_python_install_input() -> None:
    manifest = load_static_plugin_manifest(ROOT)

    assert manifest.name == plugin.name == "steam"
    assert manifest.version == plugin.version == "3.2.2"
    assert manifest.api_version == plugin.api_version == 3
    assert manifest.requirements == ("mcp/requirements.txt",)
    assert manifest.python[0].runtime_root == "mcp"

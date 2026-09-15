from __future__ import annotations

from pathlib import Path

import pytest

from agent.plugin_composition import MCP_SERVERS, TIMERS, CompositionRoot, PluginRuntime, PluginTimers
from agent.plugin_composition.assets import INSTALLED_ASSETS
from agent.plugins.composable import ComposablePlugin
from agent.plugins.static_manifest import load_static_plugin_manifest
from plugins.assets.plugin import Assets
from plugins.tools.plugin import TOOLS, ToolCatalog
from steam_test_plugin import plugin
from steam_test_plugin.eventmail import EVENTMAIL_CONTEXT_SOURCE
from steam_test_plugin.tools import STEAM_TOOLS


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("eventmail", [False, True])
async def test_apply_registers_lazy_tools_and_scoped_skills(tmp_path: Path, eventmail: bool) -> None:
    """装配只登记资源，关闭组合会注销贡献，不启动业务刷新。"""
    root = CompositionRoot("steam:test")
    definitions = []

    class Servers:
        async def register(self, ctx, definition):
            definitions.append(definition)

    class Sources:
        def bind(self, source_id):
            assert source_id == "steam-presence"
            return object()

    assets = Assets(root.context)
    await root.context.provide(INSTALLED_ASSETS, assets)
    await root.context.provide(MCP_SERVERS, Servers())
    await root.context.provide(TOOLS, ToolCatalog(root.context))
    await root.context.provide(TIMERS, PluginTimers.candidate_validation())
    if eventmail:
        await root.context.provide(EVENTMAIL_CONTEXT_SOURCE, Sources())
    composable = ComposablePlugin.from_module(plugin, load_static_plugin_manifest(ROOT))
    data_root = tmp_path / "plugin-data" / "steam-test"
    await root.mount(
        composable.apply, name="steam", inject=composable.inject,
        runtime=PluginRuntime(
            plugin_id="steam", generation_id="steam:test", plugin_dir=ROOT,
            data_dir=data_root, workspace=tmp_path, config={},
        ),
    )
    assert len(definitions) == 1
    assert definitions[0].command == ("python", "mcp/run_mcp.py")
    assert definitions[0].candidate_env == {"STEAM_BACKEND": "recording"}
    assert not data_root.exists()
    assert any(ref.description["name"].startswith("mcp_steam__")
               for ref in root.context.require(STEAM_TOOLS).refs)
    assert [(item.category, item.root_dir) for item in assets()] == [("skills", ROOT / "skills")]
    await root.dispose()
    assert not assets._entries

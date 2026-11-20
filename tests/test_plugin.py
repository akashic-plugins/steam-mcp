from __future__ import annotations

from plugins.timer.timer import AsyncioOneShotTimer

import inspect
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from plugins.tools.plugin import TOOLS, ToolCatalog
from steam_test_plugin.tools import STEAM_TOOLS  # pyright: ignore[reportMissingImports]  # conftest 注册测试包。

import pytest
from steam_test_plugin import plugin  # pyright: ignore[reportMissingImports]
from agent.plugin_composition import CREDENTIALS, CompositionRoot, CredentialRef, PluginRuntime
from plugins.mcp.contract import MCP_SERVERS
from plugins.timer.contract import TIMERS
from plugins.mcp.contract import McpServerDefinition, McpServers
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


_KEY = CredentialRef(("a" * 32, "b" * 64))


class _Client:
    def credential(self, ref: CredentialRef) -> str:
        assert ref == _KEY
        return "test-only"


class _Credentials:
    """只解析测试用的一条凭据引用；真实的私有凭据目录由 manager 集成测试覆盖。"""

    @asynccontextmanager
    async def open(self, _ctx: object, refs: Mapping[str, CredentialRef]) -> AsyncIterator[_Client]:
        assert set(refs.values()) == {_KEY}
        yield _Client()


async def _mount(tmp_path: Path, config: dict[str, object]) -> tuple[CompositionRoot, _RecordingMcpServers]:
    root = CompositionRoot("steam:without-watch")
    servers = _RecordingMcpServers()
    await root.context.provide(MCP_SERVERS, cast(McpServers, servers))
    await root.context.provide(TOOLS, ToolCatalog(root.context, Tasks()))
    await root.context.provide(TIMERS, AsyncioOneShotTimer())
    await root.context.provide(CREDENTIALS, cast(object, _Credentials()))  # pyright: ignore[reportArgumentType]
    composable = ComposablePlugin.from_module(plugin, load_static_plugin_manifest(ROOT))
    data_dir = tmp_path / "plugin-data"
    data_dir.mkdir()
    await root.mount(
        composable.apply,
        name="steam",
        inject=composable.inject,
        runtime=PluginRuntime(
            plugin_id="steam",
            generation_id="steam:without-watch",
            plugin_dir=ROOT,
            data_dir=data_dir,
            workspace=tmp_path / "workspace",
            config=config,
        ),
    )
    return root, servers


def test_pure_v3_exports_and_exact_apply() -> None:
    assert plugin.api_version == 3
    assert plugin.name == "steam"
    assert plugin.version == "3.4.0"
    assert plugin.skill_roots == ("skills",)
    assert tuple(inspect.signature(plugin.apply).parameters) == ("ctx",)


@pytest.mark.asyncio
async def test_apply_keeps_user_mcp_without_watch(tmp_path: Path) -> None:
    root, servers = await _mount(tmp_path, {"steam_api_key": _KEY, "steam_id": "76561198000000000",
                                            "snapshot_interval_seconds": 3600})

    server = servers.definitions["steam"]
    assert server.required_tools == ("get_player_summaries",)
    assert server.candidate_read_only_tools == ()
    assert server.candidate_env == {"STEAM_BACKEND": "recording"}
    # 运行文件由插件配置派生，只有本人可读。
    runtime_file = tmp_path / "plugin-data" / "steam_mcp_config.json"
    assert json.loads(runtime_file.read_text()) == {
        "steam_api_key": "test-only", "steam_id": "76561198000000000", "snapshot_interval_seconds": 3600}
    assert runtime_file.stat().st_mode & 0o777 == 0o600
    assert any(item["name"].startswith("mcp_steam__") for item in (ref.description for ref in root.context.require(STEAM_TOOLS).refs))
    await root.dispose()


@pytest.mark.asyncio
async def test_unconfigured_apply_only_waits_for_settings(tmp_path: Path) -> None:
    root, servers = await _mount(tmp_path, {})

    assert servers.definitions == {}
    assert list((tmp_path / "plugin-data").iterdir()) == []
    await root.dispose()


def test_static_manifest_declares_the_python_install_input() -> None:
    manifest = load_static_plugin_manifest(ROOT)

    assert manifest.name == plugin.name == "steam"
    assert manifest.version == plugin.version == "3.4.0"
    assert manifest.api_version == plugin.api_version == 3
    assert manifest.requirements == ("mcp/requirements.txt",)
    assert manifest.python[0].runtime_root == "mcp"

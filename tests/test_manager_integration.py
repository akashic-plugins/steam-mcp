from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from agent.plugins.artifacts import ArtifactPointer, write_pointers
from agent.plugins.generation_activity_host import ActivityHost
from agent.plugins.generation_proactive_host import ProactiveActivityAdapter
from agent.plugins.manifest import write_plugin_manifest
from agent.plugins.manager import PluginManager
from bus.event_bus import EventBus


ROOT = Path(__file__).resolve().parents[1]


def _stage_installed_plugin(tmp_path: Path) -> Path:
    """为 installed candidate 测试准备 stable/latest 两个 immutable artifact。"""

    # 1. 复制完整插件 artifact，复用当前测试解释器作为隔离 MCP runtime。
    plugin_base = tmp_path / "home" / "cache" / "github" / "steam"
    artifacts = plugin_base / ".artifacts"
    runtime = Path(sys.executable).parent.parent
    for label in ("stable", "candidate"):
        artifact = artifacts / label
        shutil.copytree(
            ROOT,
            artifact,
            ignore=shutil.ignore_patterns(
                ".git",
                ".akashic-core",
                ".pytest_cache",
                "__pycache__",
                ".venv",
            ),
        )
        (artifact / "mcp" / ".venv").symlink_to(runtime, target_is_directory=True)
        if label == "candidate":
            (artifact / ".candidate-marker").write_text("candidate\n", encoding="utf-8")

    # 2. stable 与 latest 指向不同 artifact，显式打开 installed 插件。
    write_pointers(
        plugin_base,
        stable=ArtifactPointer(".artifacts/stable"),
        latest=ArtifactPointer(".artifacts/stable"),
    )
    write_plugin_manifest(
        {"steam@github": True},
        plugins_home=tmp_path / "home",
    )
    return plugin_base


def _stage_plugin(tmp_path: Path) -> Path:
    """复制可执行插件，并复用当前测试解释器的依赖环境。"""

    source = tmp_path / "plugins" / "steam"
    source.mkdir(parents=True)
    for relative in (
        "plugin.py",
        "akashic.plugin.toml",
        "mcp/requirements.txt",
        "mcp/run_mcp.py",
        "mcp/runtime_config.py",
        "mcp/steam_mcp.py",
        "mcp/steam_proactive.py",
        "mcp/http_client.py",
    ):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    shutil.copytree(ROOT / "skills", source / "skills")
    runtime = Path(sys.executable).parent.parent
    (source / "mcp" / ".venv").symlink_to(runtime, target_is_directory=True)
    return source


@pytest.mark.asyncio
async def test_manager_boots_formal_steam_without_network_calls_and_drains(
    tmp_path: Path,
) -> None:
    """启动真实 stdio handshake，并证明 formal data 与 runtime 精确回收。"""

    # 1. 只提供测试专用 formal 配置；测试不调用任何 Steam tool
    plugin_root = _stage_plugin(tmp_path)
    workspace = tmp_path / "workspace"
    data_root = workspace / "plugin-data" / "steam-builtin"
    data_root.mkdir(parents=True)
    config = data_root / "steam_mcp_config.json"
    config.write_text(
        json.dumps(
            {
                "steam_api_key": "test-only",
                "steam_id": "76561198000000000",
                "snapshot_interval_seconds": 3600,
            }
        ),
        encoding="utf-8",
    )
    config_digest = hashlib.sha256(config.read_bytes()).hexdigest()

    # 2. 走真实 Manager/Host formal publication，只观察 tools/list
    manager = PluginManager(
        plugin_dirs=[plugin_root.parent],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=workspace,
        installed_cache_root=tmp_path / "cache",
    )
    adapter = ProactiveActivityAdapter(manager.composition_generation_host)
    activity = ActivityHost((adapter,))
    manager.bind_activity_host(activity)
    snapshot = None
    generation_id = None
    route = None
    try:
        await manager.load_all()
        snapshot = manager.current_snapshot
        assert snapshot is not None and snapshot.mcp_server_registry is not None
        assert tuple(snapshot.mcp_server_registry) == ("steam",)
        generation = next(iter(snapshot.generations.values()))
        generation_id = generation.generation_id
        runtime = manager.composition_generation_host.get(generation_id)
        assert runtime is not None and runtime.mode == "formal"
        assert runtime.mcp is not None and runtime.mcp.state == "ready"
        server = runtime.mcp.server("steam")
        route = server.route()
        assert route.mode == "formal"
        assert "get_steam_context" in route.tool_names
        assert "take_steam_snapshot" in route.tool_names
        logs = list(server.logs().stdout) + list(server.logs().stderr)
        assert not any("CallToolRequest" in line or '"tools/call"' in line for line in logs)
        assert adapter.source_fetch_invocations == 0
        assert hashlib.sha256(config.read_bytes()).hexdigest() == config_digest
    finally:
        if route is not None:
            await route.aclose()
        await manager.terminate_all()

    # 3. terminate 后 exact generation、Activity、Root effects 全部释放
    assert activity.active is None
    assert manager.composition_generation_host.get(generation_id) is None
    assert snapshot is not None and snapshot.composition_root is not None
    assert snapshot.composition_root.receipt().effects == ()
    assert snapshot.composition_root.topology_view().listeners == ()


@pytest.mark.asyncio
async def test_manager_candidate_publish_switches_formal_steam_and_cleans_validation(
    tmp_path: Path,
) -> None:
    """验证 installed Steam candidate 能 formalize、promote 并清理隔离资源。"""

    # 1. 准备 stable formal 配置与 latest candidate artifact。
    plugin_base = _stage_installed_plugin(tmp_path)
    workspace = tmp_path / "workspace"
    data_root = workspace / "plugin-data" / "steam-github"
    data_root.mkdir(parents=True)
    (data_root / "steam_mcp_config.json").write_text(
        json.dumps(
            {
                "steam_api_key": "test-only",
                "steam_id": "76561198000000000",
                "snapshot_interval_seconds": 3600,
            }
        ),
        encoding="utf-8",
    )
    manager = PluginManager(
        plugin_dirs=[tmp_path / "builtin-plugins"],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=workspace,
        installed_cache_root=plugin_base.parent.parent,
    )
    adapter = ProactiveActivityAdapter(manager.composition_generation_host)
    activity = ActivityHost((adapter,))
    manager.bind_activity_host(activity)
    stable_snapshot = None
    candidate = None
    try:
        # 2. 先启动 stable formal runtime，再走 candidate -> latest_ready。
        await manager.load_all()
        stable_snapshot = manager.current_snapshot
        assert stable_snapshot is not None
        stable_root = stable_snapshot.composition_root
        assert stable_root is not None
        assert stable_snapshot.proactive_component_catalog is not None
        assert (
            stable_snapshot.proactive_component_catalog.root_instance_token
            is stable_root.instance_token
        )
        write_pointers(
            plugin_base,
            stable=ArtifactPointer(".artifacts/stable"),
            latest=ArtifactPointer(".artifacts/candidate"),
        )
        candidate = await manager.prepare_candidate("steam@github")
        assert candidate is not None and candidate.runtime_snapshot is not None
        assert candidate.validation_workspace is not None
        candidate_root = candidate.runtime_snapshot.composition_root
        assert candidate_root is not None
        assert candidate.runtime_snapshot.proactive_component_catalog is not None
        assert (
            candidate.runtime_snapshot.proactive_component_catalog.root_instance_token
            is candidate_root.instance_token
        )

        # 3. publish 重新 formalize 到最终 Root，再 promote 并观察验证目录清理。
        ready_result = await manager.publish_prepared("steam@github")
        assert ready_result["publication_state"] == "latest_ready"
        ready = manager.ready_candidate
        assert ready is not None
        assert ready is candidate
        ready_snapshot = ready.runtime_snapshot
        assert ready_snapshot is not None and ready_snapshot.composition_root is not None
        assert ready_snapshot.proactive_component_catalog is not None
        assert (
            ready_snapshot.proactive_component_catalog.root_instance_token
            is ready_snapshot.composition_root.instance_token
        )
        validation_root = candidate.validation_workspace.parent
        assert validation_root.exists()

        promoted = await manager.switch_ready("steam@github")
        assert promoted["publication_state"] == "promoted"
        final_snapshot = manager.current_snapshot
        assert final_snapshot is not None and final_snapshot.composition_root is not None
        assert final_snapshot.proactive_component_catalog is not None
        assert (
            final_snapshot.proactive_component_catalog.root_instance_token
            is final_snapshot.composition_root.instance_token
        )
        assert not validation_root.exists()
        assert manager.ready_candidate is None
        assert json.loads((plugin_base / ".pointers.json").read_text()) == {
            "latest": ".artifacts/candidate",
            "stable": ".artifacts/candidate",
        }
    finally:
        await manager.terminate_all()

    assert candidate is not None
    assert activity.active is None
    assert manager.composition_generation_host.get(candidate.generation_id) is None

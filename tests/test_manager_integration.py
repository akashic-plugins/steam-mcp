from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import agent.plugins.host as plugin_host_module
from agent.plugin_composition.bindings import BINDINGS
from agent.plugin_contracts.tools import ALL_TOOLS, TOOLS
from agent.plugins.selection import PluginSelection
from plugins.tools.plugin import open_tool
from agent.control.timer import TimerReceipt, TimerStatus
from session.log import MessageLog
from agent.plugins.manager import PluginManager
from agent.plugins.python_environment import ENVIRONMENT_FILE, PythonEnvironments
from agent.plugins.static_manifest import load_static_plugin_manifest
from bus.event_bus import EventBus

from steam_runtime import backend


ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = Path(os.environ["AKASHIC_AGENT_ROOT"])


class _TimerHandle:
    def __init__(self, timer_id: str, deadline: datetime, now: datetime) -> None:
        self._id = timer_id
        self.deadline = deadline
        self.now = now
        self.future: asyncio.Future[TimerReceipt] = (
            asyncio.get_running_loop().create_future()
        )

    @property
    def id(self) -> str:
        return self._id

    async def result(self) -> TimerReceipt:
        return await asyncio.shield(self.future)

    async def cancel(self) -> TimerReceipt:
        if not self.future.done():
            self.future.set_result(self._receipt(TimerStatus.CANCELLED))
        return await self.future

    async def cleanup(self) -> None:
        _ = await self.cancel()

    def _receipt(self, status: TimerStatus) -> TimerReceipt:
        return TimerReceipt(self.id, self.deadline, self.now, status)


class _Timer:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.handles: list[_TimerHandle] = []

    def schedule(self, deadline: datetime) -> _TimerHandle:
        handle = _TimerHandle(f"timer:{len(self.handles)}", deadline, self.now)
        self.handles.append(handle)
        return handle


async def _eventually(predicate) -> None:
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not settle")


def _stage_plugin(tmp_path: Path) -> Path:
    """复制真实 Steam 插件并链接调用方声明的 artifact 运行时。"""

    runtime = Path(os.environ["AKASHIC_PLUGIN_FIXTURE_PYTHON"]).parent.parent
    shutil.copytree(
        CORE_ROOT / "plugins" / "eventmail",
        tmp_path / "plugins" / "eventmail",
    )
    source = tmp_path / "plugins" / "steam"
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            ".cache",
            ".akashic-core",
            ".plugin-contracts",
            ".pytest_cache",
            ".venv",
            "__pycache__",
            "tests",
        ),
    )
    (source / "mcp" / ".venv").symlink_to(runtime, target_is_directory=True)
    return source


def _prepare_python_environment(source: Path, workspace: Path) -> None:
    """通过安装 owner 为测试 artifact 固定独立 Python 环境。"""

    manifest = load_static_plugin_manifest(source)
    environments = PythonEnvironments(workspace)
    refs = {
        item.runtime_root: environments.prepare(source, item)
        for item in manifest.python
    }
    (source / ENVIRONMENT_FILE).write_text(json.dumps(refs), encoding="utf-8")


def test_stage_plugin_links_declared_fixture_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "artifact" / ".venv"
    fixture_python = runtime / "bin" / "python"
    fixture_python.parent.mkdir(parents=True)
    fixture_python.touch()
    monkeypatch.setenv("AKASHIC_PLUGIN_FIXTURE_PYTHON", str(fixture_python))

    source = _stage_plugin(tmp_path)

    assert (source / "mcp" / ".venv").readlink() == runtime


def test_stage_plugin_requires_fixture_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AKASHIC_PLUGIN_FIXTURE_PYTHON", raising=False)

    with pytest.raises(KeyError) as error:
        _stage_plugin(tmp_path)

    assert error.value.args == ("AKASHIC_PLUGIN_FIXTURE_PYTHON",)
    assert not (tmp_path / "plugins").exists()


def _config(data_root: Path) -> Path:
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / "steam_mcp_config.json"
    path.write_text(
        json.dumps(
            {
                "steam_api_key": "test-only",
                "steam_id": "76561198000000000",
                "snapshot_interval_seconds": 3600,
            }
        ),
        encoding="utf-8",
    )
    return path


def _seed_fresh_state(data_root: Path, now: datetime) -> None:
    backend.initialize(data_root, now)
    database = data_root / "steam_proactive.sqlite3"
    presence = {
        "observed_at": now.isoformat(),
        "online_status": "online",
        "presence": "active",
        "currently_playing": None,
        "interruptibility": 0.8,
        "confidence": 0.9,
        "transition": "",
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE current_state
            SET presence_json = ?, presence_observed_at = ?,
                presence_expires_at = ?, current_games_json = '[]'
            WHERE singleton = 1
            """,
            (
                json.dumps(presence, sort_keys=True, separators=(",", ":")),
                now.isoformat(),
                datetime(2026, 8, 23, 8, 5, tzinfo=UTC).isoformat(),
            ),
        )
        connection.commit()


@pytest.mark.asyncio
async def test_manager_real_mcp_and_timer_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load the real provider, call recording MCP, and drain its timer on update."""
    now = datetime(2026, 8, 23, 8, tzinfo=UTC)
    timers: list[_Timer] = []

    def timer_factory() -> _Timer:
        timer = _Timer(now)
        timers.append(timer)
        return timer

    monkeypatch.setattr(plugin_host_module, "AsyncioOneShotTimer", timer_factory)
    monkeypatch.setenv("STEAM_BACKEND", "recording")
    plugin_root = _stage_plugin(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    PluginSelection(workspace).initialize()
    _prepare_python_environment(plugin_root, workspace)
    data_root = workspace / "plugin-data" / "steam-builtin"
    config = _config(data_root)
    _seed_fresh_state(data_root, now)
    config_digest = hashlib.sha256(config.read_bytes()).hexdigest()
    log = MessageLog(tmp_path / "sessions.db")
    events = EventBus()
    manager = PluginManager(
        message_log=log,
        plugin_dirs=[plugin_root.parent, *(CORE_ROOT / "plugins" / name for name in
                      ("content", "tools", "mcp", "managed_processes"))],
        event_bus=events,
        workspace=workspace,
        installed_cache_root=tmp_path / "cache",
    )
    try:
        # 1. The live Fiber starts its context owner when it becomes ready.
        await manager.load_all()
        root = manager.live_root
        old = manager.generation("steam")
        assert root is not None and old is not None and old.fiber is not None
        refs = root.context.require(ALL_TOOLS)().refs
        target = next(ref for ref in refs if ref.name == "mcp_steam__get_player_summaries")
        assert "risk" not in target.description
        await manager.start_runtime()
        await _eventually(lambda: sum(len(timer.handles) for timer in timers) == 1)
        formal_timer = next(timer for timer in timers if timer.handles)

        # 2. Open the actual granted tool and recording MCP process.
        bindings = root.context.require(BINDINGS)
        identity = await root.context.require(TOOLS).bind(target, bindings)
        async with open_tool(bindings, identity) as tool:
            call = await tool.invoke("invalid-steam-id", {"steamids": []})
            assert call.outcome == "error"
            assert "at least one Steam ID" in str(call.parts)

        # 3. A local replacement drains the old timer while retaining business data.
        with (plugin_root / "plugin.py").open("a", encoding="utf-8") as handle:
            handle.write("\n# replacement fixture revision\n")
        result = await manager.reconcile_changed()
        assert any(row["publication_state"] == "active" for row in result)
        assert manager.generation("steam") is not old
        assert old.scope.closed
        assert manager.live_root is root
        await _eventually(lambda: sum(len(timer.handles) for timer in timers) == 2)
        assert (await formal_timer.handles[0].result()).status is TimerStatus.CANCELLED
        assert len([handle for timer in timers for handle in timer.handles
                    if not handle.future.done()]) == 1
        assert hashlib.sha256(config.read_bytes()).hexdigest() == config_digest
        assert (data_root / "steam_proactive.sqlite3").exists()
    finally:
        await manager.terminate_all()
        log.close()
        await events.aclose()
    assert all(handle.future.done() for timer in timers for handle in timer.handles)

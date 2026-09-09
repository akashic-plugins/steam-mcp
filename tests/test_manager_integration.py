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
import agent.plugins.manager as plugin_manager_module
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
async def test_manager_candidate_context_and_timer_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证真实 MCP、Wake hint、静默 candidate 与 Timer 换班。"""

    now = datetime(2026, 8, 23, 8, tzinfo=UTC)
    timers: list[_Timer] = []

    def timer_factory() -> _Timer:
        timer = _Timer(now)
        timers.append(timer)
        return timer

    monkeypatch.setattr(plugin_manager_module, "AsyncioOneShotTimer", timer_factory)
    monkeypatch.setenv("STEAM_BACKEND", "recording")
    plugin_root = _stage_plugin(tmp_path)
    workspace = tmp_path / "workspace"
    _prepare_python_environment(plugin_root, workspace)
    data_root = workspace / "plugin-data" / "steam-builtin"
    config = _config(data_root)
    _seed_fresh_state(data_root, now)
    log = MessageLog(tmp_path / "sessions.db")
    manager = PluginManager(
        message_log=log,
        plugin_dirs=[plugin_root.parent, CORE_ROOT / "plugins" / "tools"],
        event_bus=EventBus(),
        tool_registry=None,
        workspace=workspace,
        installed_cache_root=tmp_path / "cache",
    )
    await manager.load_all()
    snapshot = manager.current_snapshot
    assert snapshot is not None and snapshot.composition_root is not None
    runtime = manager.composition_generation_host.get(
        snapshot.generations["steam"].generation_id
    )
    assert runtime is not None and runtime.mcp is not None
    assert "get_player_summaries" in runtime.mcp.server("steam").tool_names
    async with runtime.mcp.server("steam").route() as route:
        call = await route.call("get_player_summaries", {"steamids": []})
        assert not call.success
        assert "at least one Steam ID" in call.output
    lifecycle = asyncio.create_task(manager.run_runtime_services())
    try:
        # 1. 稳定 Root 只注册一个 Timer；Context 只写 EventMail。
        await _eventually(lambda: sum(len(timer.handles) for timer in timers) == 1)
        formal_timer = next(timer for timer in timers if timer.handles)
        assert not any(
            listener.startswith("serial:turn.context_prepared")
            for listener in snapshot.composition_root.topology_view().listeners
        )

        # 2. candidate 可握手，但没有 Timer、外网或正式 write set。
        database = data_root / "steam_proactive.sqlite3"
        formal_hashes = {
            "config": hashlib.sha256(config.read_bytes()).hexdigest(),
            "database": hashlib.sha256(database.read_bytes()).hexdigest(),
        }
        with (plugin_root / "plugin.py").open("a", encoding="utf-8") as handle:
            handle.write("\n# candidate fixture revision\n")
        _prepare_python_environment(plugin_root, workspace)
        candidate = await manager.prepare_candidate("steam")
        assert candidate is not None and candidate.runtime_snapshot is not None
        assert sum(len(timer.handles) for timer in timers) == 1
        candidate_root = candidate.runtime_snapshot.composition_root
        assert candidate_root is not None
        assert candidate_root.receipt().optional_pending == ()
        assert hashlib.sha256(config.read_bytes()).hexdigest() == formal_hashes["config"]
        assert hashlib.sha256(database.read_bytes()).hexdigest() == formal_hashes["database"]

        # 3. 发布先取消旧 Timer，再由新稳定 Root 注册一个 Timer。
        result = await manager.publish_prepared("steam")
        assert result["publication_state"] == "committed"
        await _eventually(lambda: sum(len(timer.handles) for timer in timers) == 2)
        assert (await formal_timer.handles[0].result()).status is TimerStatus.CANCELLED
        active = [
            handle
            for timer in timers
            for handle in timer.handles
            if not handle.future.done()
        ]
        assert len(active) == 1
    finally:
        lifecycle.cancel()
        _ = await asyncio.gather(lifecycle, return_exceptions=True)
        await manager.terminate_all()
        log.close()

    assert all(handle.future.done() for timer in timers for handle in timer.handles)

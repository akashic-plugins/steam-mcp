from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.control.timer import TimerReceipt, TimerStatus
from agent.plugin_composition import PluginTimers
from steam_test_plugin.context_source import SteamContextRuntime  # pyright: ignore[reportMissingImports]
from steam_test_plugin.steam_runtime import backend  # pyright: ignore[reportMissingImports]


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

    def fire(self) -> None:
        self.future.set_result(self._receipt(TimerStatus.FIRED))

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


class _Health:
    def __init__(self) -> None:
        self.reason: str | None = None

    @property
    def healthy(self) -> bool:
        return self.reason is None

    def degrade(self, reason: str) -> None:
        self.reason = reason

    def recover(self) -> None:
        self.reason = None


def _config(data_root: Path) -> None:
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / "steam_mcp_config.json").write_text(
        json.dumps(
            {
                "steam_api_key": "test-key",
                "steam_id": "test-user",
                "snapshot_interval_seconds": 300,
            }
        ),
        encoding="utf-8",
    )


class _Context:
    def __init__(self) -> None:
        self.reports: list[dict[str, object]] = []

    def report(self, **kwargs: object) -> Mapping[str, object]:
        self.reports.append(dict(kwargs))
        return {"changed": True}


async def _eventually(predicate) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not settle")


@pytest.mark.asyncio
async def test_network_incident_retries_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 23, 8, tzinfo=UTC)
    _config(tmp_path)
    timer = _Timer(now)
    health = _Health()
    incidents: list[tuple[str, str]] = []
    calls = 0

    def refresh(_data_root: Path, attempt: datetime) -> backend.RefreshResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise backend.SteamNetworkError("temporary outage")
        return backend.RefreshResult(attempt + timedelta(minutes=5), False, "active")

    monkeypatch.setattr(backend, "refresh", refresh)
    runtime = SteamContextRuntime(
        tmp_path,
        PluginTimers(timer),
        health,  # type: ignore[arg-type]
        lambda kind, message: incidents.append((kind, message)),
        _Context(),  # type: ignore[arg-type]
        now=lambda: now,
    )
    await runtime.start()
    handler = runtime._log_handler  # pyright: ignore[reportPrivateUsage]
    assert handler is not None
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 3

    timer.handles[0].fire()
    await _eventually(lambda: len(timer.handles) == 2)
    assert incidents == [("steam_refresh_transient", "temporary outage")]
    assert "temporary outage" in str(backend.state(tmp_path, now)["last_refresh_error"])

    timer.handles[1].fire()
    await _eventually(lambda: len(timer.handles) == 3)
    assert calls == 2
    assert health.reason is None
    await runtime.close()


def test_current_presence_is_reported_as_expiring_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 23, 8, tzinfo=UTC)
    _config(tmp_path)
    monkeypatch.setattr(
        backend,
        "_fetch_player_summary",
        lambda _: {"personastate": 1},
    )
    monkeypatch.setattr(backend, "_fetch_recently_played", lambda _: [])
    _ = backend.refresh(tmp_path, now)
    context = _Context()
    runtime = SteamContextRuntime(
        tmp_path,
        PluginTimers(None),
        _Health(),  # type: ignore[arg-type]
        lambda _kind, _message: None,
        context,  # type: ignore[arg-type]
        now=lambda: now,
    )

    runtime._report_current(now)  # pyright: ignore[reportPrivateUsage]

    assert len(context.reports) == 1
    assert context.reports[0]["source_id"] == "steam-presence"
    assert context.reports[0]["event_id"] == "current"
    assert context.reports[0]["expires_at"] == now + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_contract_failure_degrades_and_does_not_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 23, 8, tzinfo=UTC)
    _config(tmp_path)
    timer = _Timer(now)
    health = _Health()
    incidents: list[tuple[str, str]] = []
    runtime = SteamContextRuntime(
        tmp_path,
        PluginTimers(timer),
        health,  # type: ignore[arg-type]
        lambda kind, message: incidents.append((kind, message)),
        _Context(),  # type: ignore[arg-type]
        now=lambda: now,
    )
    monkeypatch.setattr(
        backend,
        "refresh",
        lambda *_: (_ for _ in ()).throw(RuntimeError("schema mismatch")),
    )

    await runtime.start()
    task = runtime._task  # pyright: ignore[reportPrivateUsage]
    assert task is not None
    timer.handles[0].fire()
    with pytest.raises(RuntimeError, match="schema mismatch"):
        await task

    assert len(timer.handles) == 1
    assert health.reason == "RuntimeError: schema mismatch"
    assert incidents == [("steam_refresh_contract", "RuntimeError: schema mismatch")]
    await runtime.close()

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agent.control.timer import TimerHandle, TimerStatus
from agent.plugin_composition import HealthHandle, PluginTimers
from plugins.wake.contracts import WakeContextSource

from .steam_runtime import backend


class SteamContextRuntime:
    """用 Timer 刷新 Steam current state，并上报可过期 Context。"""

    def __init__(
        self,
        data_root: Path,
        timers: PluginTimers,
        health: HealthHandle,
        report_incident: Callable[[str, str], object],
        context: WakeContextSource,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._data_root = data_root
        self._timers = timers
        self._health = health
        self._report_incident = report_incident
        self._context = context
        self._now = now or (lambda: datetime.now(UTC))
        self._handle: TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._log = logging.Logger("steam-context-source", level=logging.INFO)
        self._log.propagate = False
        self._log_handler: RotatingFileHandler | None = None

    async def start(self) -> None:
        """初始化正式 state，并只注册一个 source Timer。"""

        if self._closed:
            raise RuntimeError("Steam Context runtime 已关闭")
        if self._handle is not None:
            return
        self._start_diagnostics()
        now = self._aware_now()
        await asyncio.to_thread(backend.initialize, self._data_root, now)
        deadline = await asyncio.to_thread(backend.next_deadline, self._data_root, now)
        await asyncio.to_thread(self._report_current, now)
        self._arm(deadline)

    async def close(self) -> None:
        """取消并收束当前等待，不改变 Steam domain state。"""

        if self._closed:
            return
        self._closed = True
        handle = self._handle
        task = self._task
        self._handle = None
        self._task = None
        if handle is not None:
            _ = await handle.cancel()
        if task is not None and task is not asyncio.current_task():
            _ = await asyncio.gather(task, return_exceptions=True)
        if handle is not None:
            await handle.cleanup()
        self._stop_diagnostics()

    def _arm(self, deadline: datetime) -> None:
        if self._closed or self._handle is not None:
            return
        handle = self._timers.schedule(deadline)
        self._handle = handle
        self._task = asyncio.create_task(
            self._wait_refresh_rearm(handle),
            name="steam-context-source:refresh",
        )
        self._task.add_done_callback(self._observe_task)

    async def _wait_refresh_rearm(self, handle: TimerHandle) -> None:
        """消费一次 Timer，明确处理网络重试，再注册下一次。"""

        next_due: datetime | None = None
        try:
            receipt = await handle.result()
            if receipt.status is TimerStatus.CANCELLED or self._closed:
                return
            now = self._aware_now()
            try:
                result = await asyncio.to_thread(
                    backend.refresh,
                    self._data_root,
                    now,
                )
            except backend.SteamNetworkError as error:
                next_due = await asyncio.to_thread(
                    backend.record_transient_failure,
                    self._data_root,
                    now,
                    error,
                )
                _ = self._report_incident("steam_refresh_transient", str(error))
                self._log.warning(
                    "refresh transient retry_at=%s error=%s",
                    next_due.isoformat(),
                    error,
                )
            except Exception as error:
                reason = f"{type(error).__name__}: {error}"
                self._health.degrade(reason)
                _ = self._report_incident("steam_refresh_contract", reason)
                self._log.exception("refresh stopped by contract failure")
                raise
            else:
                self._health.recover()
                next_due = result.next_due
                await asyncio.to_thread(self._report_current, now)
                self._log.info(
                    "refresh committed presence=%s history_appended=%s next_due=%s",
                    result.presence,
                    result.history_appended,
                    result.next_due.isoformat(),
                )
        finally:
            self._handle = None
            self._task = None
            await handle.cleanup()
        if not self._closed and next_due is not None:
            self._arm(next_due)

    def _report_current(self, now: datetime) -> None:
        current = backend.wake_context(self._data_root, now)
        if current is None:
            return
        observed_at = datetime.fromisoformat(str(current["observed_at"]))
        expires_at = datetime.fromisoformat(str(current["expires_at"]))
        _ = self._context.report(
            source_id="steam-presence",
            event_id="current",
            payload=current,
            observed_at=observed_at,
            expires_at=expires_at,
        )

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("Steam Context clock 必须包含时区")
        return value.astimezone(UTC)

    def _observe_task(self, task: asyncio.Task[None]) -> None:
        """把未被等待的 runtime failure 转为 required health 与 Incident。"""

        if task.cancelled():
            return
        error = task.exception()
        if error is None or not self._health.healthy:
            return
        reason = f"{type(error).__name__}: {error}"
        self._health.degrade(reason)
        _ = self._report_incident("steam_runtime_failure", reason)

    def _start_diagnostics(self) -> None:
        if self._log_handler is not None:
            return
        self._data_root.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            self._data_root / "steam_context.runtime.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
            )
        )
        self._log.addHandler(handler)
        self._log_handler = handler

    def _stop_diagnostics(self) -> None:
        handler = self._log_handler
        if handler is None:
            return
        self._log.removeHandler(handler)
        handler.close()
        self._log_handler = None

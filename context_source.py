from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agent.plugin_composition import HealthHandle
from plugins.timer.contract import OneShotTimer, TimerHandle, TimerStatus

from plugins.watch.contract import WatchEvents
from .steam_runtime import backend


# 只说对值班有意义的那一层：在玩什么、在线、离线；状态没变时值班不会记成新事件。
def _presence_text(current: Mapping[str, object]) -> str:
    playing = current.get("currently_playing")
    if playing:
        return f"正在玩 {playing}"
    if current.get("presence") == "offline":
        return "Steam 离线"
    return "Steam 在线，没在玩游戏"


class SteamContextRuntime:
    """用 Timer 刷新 Steam current state，并把当前状态交给值班。"""

    def __init__(
        self,
        data_root: Path,
        timers: OneShotTimer,
        health: HealthHandle,
        report_incident: Callable[[str, str], object],
        events: WatchEvents,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._data_root = data_root
        self._timers = timers
        self._health = health
        self._report_incident = report_incident
        self._events = events
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
        await self._report_current(now)
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
                await self._report_current(now)
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

    # 把当前状态交给值班；值班不可用不影响 Steam 状态本身的刷新。
    async def _report_current(self, now: datetime) -> None:
        current = await asyncio.to_thread(backend.wake_context, self._data_root, now)
        if current is None:
            return
        observed_at = datetime.fromisoformat(str(current["observed_at"]))
        try:
            _ = await self._events.emit(
                "steam-presence",
                text=_presence_text(current),
                at=observed_at,
                state=True,
            )
        except Exception as error:
            _ = self._report_incident(
                "steam_watch_emit_failed",
                f"{type(error).__name__}: {error}"[:300],
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

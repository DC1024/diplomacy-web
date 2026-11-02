"""阶段时钟：有人挂机时把对局往前推，而不是让六个人等一个人。

设计取舍：
- 计时状态**只放内存**，不写进 `state.json`。存档结构是 vendored 内核的，能不碰就不碰；
  代价是进程重启后本阶段重新计时（TODO 的「已知缺口」里已记这条）。
- 到点后不是「踢人」，而是替他交一份合法驻守——对局继续，人回来还能接着打。
- 时钟默认关闭（`PHASE_SECONDS=0`）。热座一个人操作七国时，倒计时只会添乱。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from time import monotonic

log = logging.getLogger("diplomacy.clock")

RETRY_AFTER = 5.0  # 到点后托管没推动局面，隔 5 秒再试，避免空转打日志


class PhaseClock:
    def __init__(self, get_arena: Callable, on_timeout: Callable[[object, dict], bool],
                 seconds: float):
        self._get_arena = get_arena
        self._on_timeout = on_timeout
        self.seconds = seconds
        self._key: tuple[str, str] | None = None
        self._started = 0.0
        self._retry_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- 计时 ----------
    @property
    def enabled(self) -> bool:
        return self.seconds > 0

    def note(self, match_id: str, phase: str, *, running: bool = True) -> float | None:
        """按当前阶段推进计时，返回剩余秒数；未启用或已暂停时返回 None。

        对同一个 (match_id, phase) 反复调用是幂等的——页面每次轮询都会走这里。
        """
        if not self.enabled:
            return None
        if not running:
            self._key = None  # 暂停/终局：下次继续时整段重新计时
            return None
        key = (match_id, phase)
        if key != self._key:
            self._key = key
            self._started = monotonic()
            self._retry_at = 0.0
        return max(0.0, self.seconds - (monotonic() - self._started))

    def tick(self) -> bool:
        """到点就托管并结算。返回是否真的推动了局面。"""
        if not self.enabled:
            return False
        arena = self._get_arena()
        if arena is None:
            self._key = None
            return False

        state = arena.admin_state()
        running = not (state["game_over"] or state["paused"])
        left = self.note(state["match_id"], state["phase"], running=running)
        if left is None or left > 0:
            return False
        if monotonic() < self._retry_at:
            return False

        moved = self._on_timeout(arena, state)
        if not moved:
            self._retry_at = monotonic() + RETRY_AFTER
        return moved

    # ---------- 线程 ----------
    def start(self) -> None:
        if not self.enabled:
            log.info("阶段时钟未启用（PHASE_SECONDS=0）")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="phase-clock", daemon=True)
        self._thread.start()
        log.info("阶段时钟已启动：每阶段 %ss", self.seconds)

    def stop(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        self._key = None
        self._retry_at = 0.0

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("阶段时钟异常")
            self._stop.wait(1.0)

"""让 LLM 座位自己动起来的后台循环。

设计上刻意保持「可手动驱动」：`tick()` 是一个纯同步函数，测试可以直接调用它，
不必和线程抢时序；真正的服务在 `main()` 里才启动线程。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from .seats import LLMSeat

log = logging.getLogger("diplomacy.llm")


class SeatRunner:
    def __init__(self, get_arena: Callable, seats: dict[str, LLMSeat],
                 interval: float = 2.0):
        self._get_arena = get_arena
        self.seats = seats
        self.interval = interval
        self._acted: set[tuple[int, str]] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- 同步驱动 ----------
    def tick(self) -> list[dict]:
        """跑一轮：谁需要行动且还没交令，就让谁行动一次。

        同一 (revision, power) 只尝试一次，防止模型持续失败时被反复计费。
        """
        arena = self._get_arena()
        if arena is None or not self.seats:
            return []

        state = arena.admin_state()
        if state["game_over"] or state["paused"]:
            return []

        results = []
        revision = arena.data["revision"]
        for power, seat in self.seats.items():
            if not seat.enabled:
                continue
            info = state["players"].get(power, {})
            if not info.get("required") or info.get("ready"):
                continue
            key = (revision, power)
            if key in self._acted:
                continue
            self._acted.add(key)
            try:
                results.append(seat.act(arena))
            except Exception:  # 单个座位炸掉不能影响其他人
                log.exception("%s 行动异常", power)
        return results

    # ---------- 线程 ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="llm-seats", daemon=True)
        self._thread.start()
        log.info("LLM 座位线程已启动（%d 个座位，%ss 轮询）", len(self.seats), self.interval)

    def stop(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        """新开一局时调用：清掉「本回合已尝试」记录，座位本身保留。"""
        self._acted.clear()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("LLM 轮询异常")
            self._stop.wait(self.interval)

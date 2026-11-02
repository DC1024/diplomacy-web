"""让 LLM 座位自己动起来的后台循环。

设计上刻意保持「可手动驱动」：`tick()` 是一个纯同步函数，测试可以直接调用它，
不必和线程抢时序；真正的服务在 `main()` 里才启动线程。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from .orders import hold_orders
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
                self._force_holds(arena, power, "座位抛出异常")
            else:
                # 座位没报错但也没交令（比如内部提前 return）——同样要兜住
                if seat.enabled and self._still_pending(arena, power):
                    self._force_holds(arena, power, "座位返回却没有交令")
        return results

    def attempted(self, revision: int, power: str) -> bool:
        """后台是否已经为这个 (回合, 国家) 试过一手。

        阶段时钟靠它区分「还没轮到它」和「试过却没交令」——前者要等，
        后者必须接管，否则那一家永远不交令、结算永远不成立。
        """
        return (revision, power) in self._acted

    @staticmethod
    def _still_pending(arena, power: str) -> bool:
        info = arena.admin_state()["players"].get(power, {})
        return bool(info.get("required")) and not info.get("ready")

    def _force_holds(self, arena, power: str, why: str) -> None:
        """替没能交令的座位驻守，然后尝试结算。

        没有这一层，一个抛异常的模型座位会同时占住两个坑：`_acted` 让后台不再重试，
        `timeout_hold` 又因为「它已经交给模型了」而跳过它。结果是没人交令 →
        结算不成立 → 全桌卡死，连管理员的「强制结算」都推不动。
        """
        try:
            if not self._still_pending(arena, power):
                return
            orders = hold_orders(arena.game(), power)
            arena.act(power, dict(action="orders", orders=orders,
                                  request_id=f"runner-hold-{arena.data['revision']}-{power}"))
            log.warning("%s 未交令，已由后台按驻守处理（%s）", power, why)
        except Exception:
            log.exception("%s 后台驻守也失败，只能等阶段时钟兜底", power)
            return
        try:
            if arena.settle(quiet=True):
                arena.save()
        except ValueError:
            pass

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

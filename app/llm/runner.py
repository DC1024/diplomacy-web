"""让 LLM 座位自己动起来的后台循环。

设计上刻意保持「可手动驱动」：`tick()` 是一个纯同步函数，测试可以直接调用它，
不必和线程抢时序；真正的服务在 `main()` 里才启动线程。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from ..persist import save
from .orders import hold_orders
from .seats import LLMSeat

log = logging.getLogger("diplomacy.llm")


class SeatRunner:
    def __init__(self, get_arena: Callable, seats: dict[str, LLMSeat],
                 interval: float = 2.0,
                 total_budget: Callable[[], float] | None = None,
                 persist: Callable[[], None] | None = None):
        self._get_arena = get_arena
        self.seats = seats
        self.interval = interval
        # 全场总闸：单座位预算管不住「七个座位各烧 ¥20」，这里补一道总预算。
        # 传的是取值函数而不是数字，改了配置下一轮就生效，不用重建 runner。
        self._total_budget = total_budget
        # 座位表落盘（哪些国家交给了模型 + 各自的账），重启不丢
        self._persist = persist
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

        halted = self._enforce_total_budget()
        results = []
        revision = arena.data["revision"]
        # 先拍快照再遍历：座位表是活的，网页上勾一个座位、或脚本收回座位，
        # 都会在别的线程里往里插删。直接 `.items()` 遍历会撞
        # `RuntimeError: dictionary changed size during iteration`——P1 实测时真的撞上了，
        # 表现是「这一轮什么都没发生」，日志里只有一条 ERROR，很难往座位表上想。
        for power, seat in list(self.seats.items()):
            info = state["players"].get(power, {})
            if not info.get("required") or info.get("ready"):
                continue
            if not seat.enabled:
                # 钱烧完了的那一国必须有人交令，否则「结算」永远不成立，全桌陪它等。
                # 用户自己按的暂停不在此列：那种情况一般有人接手，代下驻守反而会
                # 把接手者的命令盖掉（后台 2 秒一轮，人还没点就被占了）。
                if seat.autohold:
                    self._force_holds(arena, power, seat.stop_reason or "座位已停摆")
                    results.append(dict(power=power, skipped=True, why=seat.stop_reason,
                                        forced_hold=True))
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
        if results or halted:
            self._save()
        return results

    def _enforce_total_budget(self) -> bool:
        """全场总闸：花超了就把所有还在叫模型的座位转成「停摆 + 代驻守」。

        单座位预算只能保证「每家不超过 ¥20」，七家一起撞线就是 ¥140。这一道闸是给
        「一局 < ¥20」这个验收口径用的——撞线后**不冻桌**：座位改成 autohold，
        由后台替它们交驻守，局面继续往前推，只是不再花钱。
        """
        if self._total_budget is None:
            return False
        try:
            limit = float(self._total_budget() or 0)
        except (TypeError, ValueError):
            return False
        if limit <= 0:      # 0 = 不限
            return False
        spent = sum(s.usage.cost_cny for s in self.seats.values())
        if spent < limit:
            return False
        hit = [s for s in self.seats.values() if s.enabled]
        for seat in hit:
            seat.halt(f"全场预算已达 ¥{limit:.2f}")
        if hit:
            log.warning("全场花费 ¥%.4f 已达上限 ¥%.2f，%s 转为驻守", spent, limit,
                        "、".join(s.power for s in hit))
        return bool(hit)

    def _save(self) -> None:
        if self._persist is None:
            return
        try:
            self._persist()
        except Exception:  # 落盘失败不该让本轮白跑
            log.exception("座位表落盘失败")

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
                save(arena)
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
        """停线程并等它退出。

        只 `set()` 不 join 的话，线程可能正卡在 `seat.act()` 里（最长 60s 的模型调用），
        关停之后还会再写一次存档；测试里手动驱动 `tick()` 时也会和它抢。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)

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

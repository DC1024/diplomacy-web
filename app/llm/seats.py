"""LLM 座位：一个国家的操作者。

一次 act 做四件事：组装上下文 → 问模型 → 校验并落子 → 记账。
任何一步失败都降级为「合法驻守」，绝不让一个掉线的模型卡住整局。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass

from ..engine.arena import OrderError
from ..persist import save
from . import context as ctxmod
from . import prompt as promptmod
from .orders import hold_orders
from .provider import Provider, ProviderError, Reply

log = logging.getLogger("diplomacy.llm")

_JSON = re.compile(r"\{.*\}", re.S)


def parse_action(text: str) -> dict | None:
    """从模型输出里抠出那个 JSON 对象。允许前后有废话或 ```json 包裹。"""
    if not text:
        return None
    candidates = []
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates += fenced
    m = _JSON.search(text)
    if m:
        candidates.append(m.group(0))
    for raw in candidates:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    retries: int = 0
    passes: int = 0     # 模型主动选择驻守，属于正常决策
    fallbacks: int = 0  # 输出不可用/报错后的兜底，属于故障
    errors: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMSeat:
    def __init__(self, power: str, provider: Provider, *, persona: str = "",
                 budget_cny: float = 20.0, price_in: float = 2.0, price_out: float = 8.0,
                 keep_hist: int = ctxmod.KEEP_HIST, keep_msgs: int = ctxmod.KEEP_MSGS):
        self.power = power
        self.provider = provider
        self.persona = persona
        self.budget_cny = budget_cny
        self.price_in = price_in     # ¥ / 百万 input token
        self.price_out = price_out   # ¥ / 百万 output token
        self.keep_hist = keep_hist
        self.keep_msgs = keep_msgs
        self.usage = Usage()
        self.enabled = True
        self.busy = False  # 正在跟模型说话：阶段时钟看到它会先让一轮，别抢
        self.stop_reason = ""
        self.last_error = ""  # 最近一次故障原因，成功一次就清掉；页面上挂个 ⚠ 提示

    # ---------- 对外 ----------
    def status(self) -> dict:
        return dict(power=self.power, provider=getattr(self.provider, "name", "?"),
                    enabled=self.enabled, busy=self.busy, stop_reason=self.stop_reason,
                    last_error=self.last_error, budget_cny=self.budget_cny,
                    persona=self.persona,
                    usage=asdict(self.usage), tokens=self.usage.tokens)

    def act(self, arena) -> dict:
        """让这个座位走一步。返回一份可直接塞进 API 响应的诊断信息。

        `busy` 是给外部看的「它正在调模型」信号：一次调用最长 60s（provider 超时），
        这期间阶段时钟不该替它驻守——否则模型白花一次 token，交的令还被覆盖。
        """
        self.busy = True
        try:
            return self._act(arena)
        finally:
            self.busy = False

    def _act(self, arena) -> dict:
        if not self.enabled:
            return dict(power=self.power, skipped=True, why=self.stop_reason or "已停用")

        state = arena.admin_state()
        if state["game_over"]:
            return dict(power=self.power, skipped=True, why="整局已结束")
        if state["paused"]:
            return dict(power=self.power, skipped=True, why="对局已冻结")
        me = state["players"].get(self.power, {})
        if not me.get("required"):
            return dict(power=self.power, skipped=True, why="本阶段无需行动")
        if me.get("ready"):
            return dict(power=self.power, skipped=True, why="已提交")

        ctx = ctxmod.build(arena, self.power, self.keep_hist, self.keep_msgs)
        system, user = promptmod.build(self.power, ctx.as_prompt(), self.persona)

        try:
            action, reply = self._ask(system, user)
        except ProviderError as exc:
            self.usage.errors += 1
            log.warning("%s 模型调用失败：%s", self.power, exc)
            return self._submit_holds(arena, f"模型调用失败：{exc}")

        said = self._apply_says(arena, action.get("say") or [])
        outcome = self._apply_orders(arena, action, system, user)

        return dict(power=self.power, skipped=False, said=said, orders=outcome["orders"],
                    ok=outcome["ok"], note=outcome["note"], reason=str(action.get("reason") or "")[:200],
                    context=ctx.stats, tokens=reply.tokens, cost_cny=round(self.usage.cost_cny, 4),
                    usage=asdict(self.usage))

    # ---------- 内部 ----------
    def _ask(self, system: str, user: str) -> tuple[dict, Reply]:
        reply = self.provider.chat(system, user)
        self._bill(reply)
        action = parse_action(reply.text)
        if action is None:
            # 给一次纠正机会：把输出格式再强调一遍，比直接放弃更有价值
            self.usage.retries += 1
            retry_user = user + "\n\n上次输出无法解析为 JSON。请只输出一个 JSON 对象。"
            reply2 = self.provider.chat(system, retry_user)
            self._bill(reply2)
            action = parse_action(reply2.text)
            if action is None:
                raise ProviderError("模型连续两次未给出可解析的 JSON")
            return action, reply2
        return action, reply

    def _bill(self, reply: Reply) -> None:
        self.usage.calls += 1
        self.usage.prompt_tokens += reply.prompt_tokens
        self.usage.completion_tokens += reply.completion_tokens
        self.usage.cost_cny += (reply.prompt_tokens * self.price_in
                                + reply.completion_tokens * self.price_out) / 1_000_000
        if self.usage.cost_cny > self.budget_cny:
            self.enabled = False
            self.stop_reason = f"超出预算 ¥{self.budget_cny:.2f}"
            log.warning("%s 超出预算，转由保底命令接管", self.power)

    def _apply_says(self, arena, says: list) -> list[dict]:
        sent = []
        for item in says[:3]:  # 一回合最多三条，防刷屏
            if not isinstance(item, dict):
                continue
            to = str(item.get("to", "")).upper()
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            try:
                arena.act(self.power, dict(action="send", to=to, text=text,
                                           request_id=str(uuid.uuid4())))
                sent.append(dict(to=to, ok=True))
            except (OrderError, ValueError) as exc:
                sent.append(dict(to=to, ok=False, error=str(exc)[:120]))
        return sent

    def _apply_orders(self, arena, action: dict, system: str, user: str) -> dict:
        if action.get("pass"):
            return self._submit_holds(arena, "模型表示驻守", degraded=False)

        orders = action.get("orders")
        if not isinstance(orders, list):
            orders = []
        orders = [str(o).strip() for o in orders if str(o).strip()]

        first_error = ""
        for attempt in range(2):
            if attempt == 1:
                # 把裁判的报错原样回灌，让模型自己修
                self.usage.retries += 1
                fix = (f"\n\n上次提交被裁判拒绝：{first_error}\n"
                       f"请给出合法的完整命令集（每支需要行动的部队都要有命令）。")
                try:
                    reply = self.provider.chat(system, user + fix)
                    self._bill(reply)
                except ProviderError:
                    break
                fixed = parse_action(reply.text)
                if not fixed:
                    break
                orders = [str(o).strip() for o in (fixed.get("orders") or []) if str(o).strip()]
                if fixed.get("pass"):
                    return self._submit_holds(arena, "模型改为驻守", degraded=False)

            try:
                result = arena.act(self.power, dict(action="orders", orders=orders,
                                                    request_id=str(uuid.uuid4())))
            except (OrderError, ValueError) as exc:
                first_error = str(exc)
                continue
            self._settle(arena)
            self.last_error = ""
            return dict(ok=True, orders=orders, note=result.get("text", "")[:160])

        return self._submit_holds(arena, first_error or "模型未给出可用命令")

    def _submit_holds(self, arena, why: str, *, degraded: bool = True) -> dict:
        """提交一份合法的驻守命令。

        `degraded=True` 表示这是故障兜底（模型输出不可用、报错、超额），
        与「模型主动选择驻守」区分开计数——否则正常对局会被统计成满屏故障。
        """
        if degraded:
            self.usage.fallbacks += 1
            self.last_error = why[:200]
        else:
            self.usage.passes += 1
        try:
            orders = hold_orders(arena.game(), self.power)
            arena.act(self.power, dict(action="orders", orders=orders, request_id=str(uuid.uuid4())))
            self._settle(arena)
            note = f"保底驻守（{why[:80]}）" if degraded else f"驻守（{why}）"
            return dict(ok=True, orders=orders, note=note, fallback=degraded)
        except (OrderError, ValueError) as exc:
            return dict(ok=False, orders=[], note=f"驻守也没提交成功：{exc}", fallback=degraded)

    @staticmethod
    def _settle(arena) -> bool:
        try:
            if arena.settle(quiet=True):
                save(arena)
                return True
        except ValueError:
            pass
        return False

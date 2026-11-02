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
from .provider import BadOutput, Provider, ProviderError, Reply

log = logging.getLogger("diplomacy.llm")

_JSON = re.compile(r"\{.*\}", re.S)

# 思维链跑飞的唯一廉价解药：明说「别想」。实测时它把 193 token 的答案救回来过一次，
# 但同一措辞随后连试 4 次全部照样跑飞——只能当兜底，不能当依赖。
_NUDGE = "\n\n（不要再推理，直接给出 JSON 答案。）"


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
    truncated: int = 0  # 撞 max_tokens 的次数：这些调用白花钱，值得单独看

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
        # 「暂停」与「停摆」是两件事，页面上的勾选框只表达前者：
        #   paused=True    用户主动收手，可能有人接手，所以后台**不**替它驻守
        #   autohold=True  钱烧完了/全场预算到顶，没人接手，必须由后台替它驻守，
        #                  否则这一国永远不交令，全桌冻死
        self.paused = False
        self.autohold = False
        self.stop_reason = ""
        self.last_error = ""  # 最近一次故障原因，成功一次就清掉；页面上挂个 ⚠ 提示

    # ---------- 对外 ----------
    def status(self) -> dict:
        return dict(power=self.power, provider=getattr(self.provider, "name", "?"),
                    enabled=self.enabled, paused=self.paused, autohold=self.autohold,
                    busy=self.busy, stop_reason=self.stop_reason,
                    last_error=self.last_error, budget_cny=self.budget_cny,
                    persona=self.persona,
                    usage=asdict(self.usage), tokens=self.usage.tokens)

    def pause(self, why: str = "已暂停") -> None:
        """用户收手：不再叫模型，但**座位与账都留着**。

        原先「收回」是 `_seats.pop()`，对象连同 `usage` 一起消失——想先别花钱又想知道
        已经花了多少，做不到。现在停的是开关，不是账本。
        """
        self.enabled = False
        self.paused = True
        self.autohold = False
        self.stop_reason = why

    def resume(self) -> None:
        self.enabled = True
        self.paused = False
        self.autohold = False
        self.stop_reason = ""

    def halt(self, why: str) -> None:
        """钱烧完了：停掉模型，但让后台替它驻守，别把全桌拖住。"""
        self.enabled = False
        self.paused = False
        self.autohold = True
        self.stop_reason = why

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
        reply = self._chat(system, user)
        action = parse_action(reply.text)
        if action is None:
            # 给一次纠正机会：把输出格式再强调一遍，比直接放弃更有价值
            self.usage.retries += 1
            retry_user = user + "\n\n上次输出无法解析为 JSON。请只输出一个 JSON 对象。"
            reply2 = self._chat(system, retry_user)
            action = parse_action(reply2.text)
            if action is None:
                raise ProviderError("模型连续两次未给出可解析的 JSON")
            return action, reply2
        return action, reply

    def _chat(self, system: str, user: str) -> Reply:
        """叫一次模型，并且**无论成败都记账**。

        被截断的那次调用同样是真金白银。以前失败路径直接往上抛，账面只算成功的两次，
        成本实测会低报一半——而那正是衡量「一局多少钱」的唯一数字，低报等于白测。

        撞墙分**两种**，处置完全不同，所以这里是一条梯子而不是一次重试：

        - **一个字都没写**（`reply.text` 为空）= 它还在想，思维链把配额吃干了。这时候
          翻倍等于给「想不完」加钱：实测有座位 4096 → 8192 连着两次空正文，第三次
          劝它「不要再推理」才用 193 token 交出完整答案。所以这一支**先劝、不翻倍**，
          配额顺势收到 2000。
        - **写了一半被截断**（有正文、不是完整 JSON）= 真的差地方，放宽一倍就够。

        为什么劝的时候要把配额**收到 2000**（看似反直觉，但量过两遍）：这 2000 不是给
        「能写完的那些」用的——它们拿到提示后只用 200 上下；它是给「劝了也白搭的那些」
        设的**天花板**。v0.7.8 试过「只劝、不缩配额」，同条件 8 阶段的结果是：放宽到
        8192 的次数 18 → 15（确实救回 3 条链），但截断总数 43 → 47、每调用 token
        4818 → 5733、总账 ¥2.16 → ¥2.78。**救回的 3 次抵不过 15 次多烧 2000**。
        推论：撞墙的钱主要花在「注定撞的那一次烧多高」，不在「撞几次」。

        劝也不是万灵药：同一措辞探针里 1 次管用、随后 4 次照样跑飞，所以它只是
        「最便宜的那一手」，后面还留着放宽到 16384 这一级。三级都走完才认栽降级驻守，
        每一次尝试都进账，页面上看得见。
        """
        cap = None            # None = 用 provider 的默认配额
        nudge = ""
        widened = False
        for _ in range(3):
            try:
                reply = self.provider.chat(system, user + nudge, max_tokens=cap)
            except BadOutput as exc:
                self._bill(exc.reply)
                if exc.kind != "truncated":
                    raise
                self.usage.truncated += 1
                if not exc.reply.text.strip() and not nudge:
                    # 收配额不是「觉得它不需要那么多地方」，是「这次注定写不完，少烧点」
                    cap = min(max(exc.cap, 1000), 2000)
                    nudge = _NUDGE
                    log.info("%s 一个字都没写（%s token 全在思维链上），改为劝它直接作答",
                             self.power, exc.cap)
                    continue
                if not widened and exc.cap:
                    widened = True
                    target = exc.cap * 2
                    if nudge:
                        # 劝过还撞墙，别从 2000 翻到 4000 再撞一次，直接给足一档
                        target = max(target, 8192)
                    cap = min(target, 16384)
                    log.info("%s 输出被截断（max_tokens=%s），放宽到 %s 重试",
                             self.power, exc.cap, cap)
                    continue
                raise
            self._bill(reply)
            return reply
        raise ProviderError("输出被截断，放宽配额与要求直接作答都试过")  # pragma: no cover - 循环内必返回或抛出

    def _bill(self, reply: Reply) -> None:
        self.usage.calls += 1
        self.usage.prompt_tokens += reply.prompt_tokens
        self.usage.completion_tokens += reply.completion_tokens
        self.usage.cost_cny += (reply.prompt_tokens * self.price_in
                                + reply.completion_tokens * self.price_out) / 1_000_000
        if self.usage.cost_cny > self.budget_cny:
            self.halt(f"超出预算 ¥{self.budget_cny:.2f}")
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
                    reply = self._chat(system, user + fix)
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

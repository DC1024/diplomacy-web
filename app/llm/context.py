"""上下文治理：决定一个 LLM 座位在每一回合到底看到什么。

成本大头是「全量历史」——`brief()` 默认把 history 里每个阶段的结算都拼进去，
一局打满约 28 KB，且随阶段数线性增长。上游项目第 3 局烧掉 153 亿输入 token 就是
每轮重放全部历史导致的。

这里不做文本切割，而是直接使用引擎自带的游标：`brief(power, msg_after, hist_after)`
本来就支持「只要最近 N 段历史 / 最近 N 条消息」，比事后正则裁剪稳得多。
丢掉的旧历史用一行结构化摘要补位，避免模型对局势完全失忆。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 默认窗口：最近 3 段结算 + 最近 30 条消息。实测中局约 5 KB，终局也不会膨胀。
KEEP_HIST = 3
KEEP_MSGS = 30

# 自己发出的信：最近 6 封，每封最多留 120 字。谈判记忆不能丢（见 `_own` 的说明），
# 但也不能让一个话痨模型把窗口撑回去——6×120 字封顶约 400 token。
SELF_KEEP = 6
SELF_CHARS = 120


def estimate_tokens(text: str) -> int:
    """粗估 token 数。中文约 1.5 字符/token，英文约 4 字符，这里取保守的 1.5。

    只用于预算拦截和展示，不参与计费——真实用量以 API 返回为准。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4) + 1


@dataclass
class SeatContext:
    text: str
    digest: str
    own: str = ""
    note: str = ""
    dropped_history: int = 0
    dropped_messages: int = 0
    stats: dict = field(default_factory=dict)

    def as_prompt(self) -> str:
        parts = []
        if self.digest:
            parts.append(self.digest)
        if self.own:
            parts.append(self.own)
        if self.note:
            parts.append(self.note)
        parts.append(self.text)
        return "\n".join(parts)


def _msg_note(dropped: int) -> str:
    """消息是按条数裁的，滑出窗口的那些对模型就是「消失了」。

    没有任何提示时，模型会把「这一回合没看到某国来信」当成「它没找过我」，
    而真相可能是那封信在三个阶段前就滑出去了。一句话说明，比让模型自己猜便宜。
    """
    if dropped <= 0:
        return ""
    return f"【消息窗口】更早的 {dropped} 条消息已滑出本次上下文，下面只剩最近的这些。"


def _own(a, power: str, keep: int = SELF_KEEP, chars: int = SELF_CHARS) -> tuple[str, int]:
    """把「你自己发出的信」补回上下文。

    引擎的 `brief()` 用 `incoming()` 组装消息块，而 `incoming()` 明确过滤掉
    `sender == power`。于是模型永远看不到自己说过什么：它对 AUSTRIA 提了
    「我不打比利时，你把北海让给我」，下一回合只收到一句「同意。」，
    既不知道同意的是哪一条，也无从判断该不该守约——承诺变成随机遗忘，
    结盟与背叛就不再是有意的策略。

    人类玩家的收件箱（`inbox()`）本来就把自己发出的信算在内，这里只是把模型
    补齐到同一水平。条数与字数都封顶，不让它把成本护栏顶回去。
    """
    mine = [m for m in a.data["messages"] if m.get("sender") == power][-max(0, keep):]
    if not mine:
        return "", 0
    lines = ["【你此前发出的信（只你自己可见）】照对方的回信，确认你答应过什么。"]
    for m in mine:
        to = "全体" if "GLOBAL" in m.get("to", []) else (m.get("to") or ["?"])[0]
        text = m.get("text", "")
        if len(text) > chars:
            text = text[:chars] + "…"
        lines.append(f'[{m.get("phase", "?")}] 你 → {to}：{text}')
    return "\n".join(lines), len(mine)


def _digest(a, power: str, upto: int) -> str:
    """把窗口之前的旧历史压成一两行，避免模型对早期局势一无所知。"""
    history = a.data["history"][:upto]
    if not history:
        return ""

    first, last = history[0], history[-1]
    start = first.get("before", {}).get("centers", {})
    end = last.get("centers") or {}
    mine_start = len(start.get(power, []))
    mine_end = len(end.get(power, []))

    flips = 0
    for rec in history:
        before = (rec.get("before") or {}).get("centers", {})
        after = rec.get("centers") or {}
        for p in {*before, *after}:
            flips += len(set(after.get(p, [])) ^ set(before.get(p, [])))

    # 谁在扩张：按末态中心数排前三
    ranked = sorted(((p, len(c)) for p, c in end.items() if c), key=lambda kv: -kv[1])[:3]
    leaders = "、".join(f"{p} {n}" for p, n in ranked if p != power)

    head = (f"【早期局势摘要】已略去前 {len(history)} 个阶段（{first['phase']} 起）的逐条结算。"
            f"你的中心数 {mine_start}→{mine_end}；期间共 {flips} 次中心易手。")
    tail = f"当前领先：{leaders}。" if leaders else ""
    return head + tail


def build(a, power: str, keep_hist: int = KEEP_HIST, keep_msgs: int = KEEP_MSGS) -> SeatContext:
    """组装该座位本回合的上下文。a 是 Arena 实例。"""
    history = a.data["history"]
    incoming = a.incoming(power)

    hist_after = max(0, len(history) - max(0, keep_hist))
    msg_after = max(0, len(incoming) - max(0, keep_msgs))

    text, _, _ = a.brief(power, msg_after=msg_after, hist_after=hist_after)
    digest = _digest(a, power, hist_after)
    own, kept_own = _own(a, power)
    note = _msg_note(msg_after)
    whole = "\n".join(p for p in (digest, own, note, text) if p)

    return SeatContext(
        text=text,
        digest=digest,
        own=own,
        note=note,
        dropped_history=hist_after,
        dropped_messages=msg_after,
        stats=dict(
            chars=len(whole),
            tokens=estimate_tokens(whole),
            kept_history=len(history) - hist_after,
            kept_messages=len(incoming) - msg_after,
            kept_own=kept_own,
        ),
    )

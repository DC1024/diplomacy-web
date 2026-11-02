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
    dropped_history: int = 0
    dropped_messages: int = 0
    stats: dict = field(default_factory=dict)

    def as_prompt(self) -> str:
        parts = []
        if self.digest:
            parts.append(self.digest)
        parts.append(self.text)
        return "\n".join(parts)


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

    return SeatContext(
        text=text,
        digest=digest,
        dropped_history=hist_after,
        dropped_messages=msg_after,
        stats=dict(
            chars=len(text) + len(digest),
            tokens=estimate_tokens(text) + estimate_tokens(digest),
            kept_history=len(history) - hist_after,
            kept_messages=len(incoming) - msg_after,
        ),
    )

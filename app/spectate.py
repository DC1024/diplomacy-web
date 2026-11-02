"""观战频道：旁观者之间的公开聊天。

**为什么不写进 `arena.data`**：那是 vendored 内核的存档结构（`mode='async-v3'`），
我们往里塞自己的字段，升级内核时 `Game.from_dict()` 会先炸；而且一份存档同时回答
「这局棋走到哪了」和「观众聊了什么」两件不相干的事，备份、迁移、回放都会变难。
所以单独放 `data/spectate.json`，与 `state.json`、`seats.json` 并列。

**为什么发言要一道码**：这是一个**公网可写的入口**，而它旁边就是正在进行的对局。
没有码的话，任何路过的人都能刷屏；有了码，房主至少知道「我把链接给了谁」，
也能一键轮换把它全部作废（`rotate()`）。只读不需要码——观众看的信息本来就是公开的
（部队位置、补给中心、结算原文），私信与合法命令他们一概看不到。

三道防线缺一不可，都是这类入口的标配：

- **观战码**：管「谁能进来」，由房主掌握、可轮换。
- **限长限频**：管「能塞多少」，一条 200 字、同一来源 1 秒一条。
- **环形上限**：管「能攒多久」，最多 400 条，超了丢最旧的——一局打三小时也不会
  把内存和这个 JSON 文件撑大。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

log = logging.getLogger("diplomacy.spectate")

MAX_TEXT = 200        # 一条发言的字数上限
MAX_NAME = 12         # 昵称上限：够辨认，又不会长到把列表挤变形
MIN_GAP = 1.0         # 同一来源两条之间至少隔这么久（秒）
MAX_MESSAGES = 400    # 环形上限
KEEP_ON_SILENT = 80   # 首次拉取（after=0）时给最近多少条


def _clean(value: object, limit: int | None = None) -> str:
    """去掉控制字符、合并空白。

    聊天是一行文本，所以换行也压成空格——不然一条消息能在列表里撑出十几行，
    把别人的发言全顶走。

    `limit` 只给昵称用（截短了没人损失）。正文不截：**超长要报错而不是悄悄砍掉**，
    砍掉的话用户以为发出去了，其实后半句没了——那比直接拒绝难查得多。
    """
    text = "".join(ch for ch in str(value or "") if ch >= " " or ch == "\n")
    text = " ".join(text.split()).strip()
    return text[:limit].strip() if limit else text


class SpectateChannel:
    def __init__(self, path: Path, min_gap: float | None = None):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = self._load()
        # 限频间隔可按部署调：本机朋友局嫌 1 秒太紧就设 0.2；公网暴露就该往上调。
        self.min_gap = float(MIN_GAP if min_gap is None else min_gap)
        # 限频表不落盘：重启后没人会接着上一轮的节奏刷屏，落盘只会让调试变难
        self._last: dict[str, float] = {}

    # ---------- 存盘 ----------
    def _blank(self) -> dict:
        return dict(key="", match_id=None, seq=0, messages=[])

    def _load(self) -> dict:
        """读坏了就当空的。

        这里刻意不抛：观战频道是**次要**功能，一个坏掉的 JSON 不该让整局打不开。
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._blank()
        if not isinstance(raw, dict):
            return self._blank()
        out = self._blank()
        out["key"] = str(raw.get("key") or "")
        out["match_id"] = raw.get("match_id")
        try:
            out["seq"] = int(raw.get("seq") or 0)
        except (TypeError, ValueError):
            out["seq"] = 0
        messages = [m for m in (raw.get("messages") or []) if isinstance(m, dict)]
        out["messages"] = messages[-MAX_MESSAGES:]
        if out["messages"]:
            # seq 是自增游标，必须不小于现存最大 id，否则新消息会撞号
            out["seq"] = max(out["seq"], max(int(m.get("id") or 0) for m in out["messages"]))
        return out

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            # 内存里仍然是对的，这一条消息不会丢；落盘失败只影响重启后还在不在
            log.warning("观战频道落盘失败：%s", exc)

    # ---------- 观战码 ----------
    def code(self) -> str:
        """惰性生成：没人要就不生成，免得每台机器都有一个没人用的码。"""
        with self.lock:
            if not self.data["key"]:
                self.data["key"] = secrets.token_urlsafe(12)
                self._save()
            return self.data["key"]

    def matches(self, code: str | None) -> bool:
        if not code:
            return False
        return secrets.compare_digest(code, self.code())

    def rotate(self) -> str:
        """换一把码：旧链接立刻失效。房主发现码被转发到公开渠道时用这个。"""
        with self.lock:
            self.data["key"] = secrets.token_urlsafe(12)
            self._save()
            return self.data["key"]

    def reset(self, match_id: str | None) -> None:
        """新局清空留言，但**保留观战码**。

        码是房主的邀请函，不该因为「开了新一局」就作废——那会让所有观众在
        第二局开始前重新要一次链接。
        """
        with self.lock:
            self.data["match_id"] = match_id
            self.data["seq"] = 0
            self.data["messages"] = []
            self._last.clear()
            self._save()

    # ---------- 消息 ----------
    def since(self, after: int = 0) -> list[dict]:
        with self.lock:
            if after <= 0:
                return list(self.data["messages"][-KEEP_ON_SILENT:])
            return [m for m in self.data["messages"] if int(m.get("id") or 0) > after]

    def say(self, name: str, text: str, phase: str = "", who: str = "") -> dict:
        name = _clean(name, MAX_NAME) or "旁观者"
        text = _clean(text)
        if not text:
            raise ValueError("说点什么吧。")
        if len(text) > MAX_TEXT:
            raise ValueError(f"太长了：一条最多 {MAX_TEXT} 字（你写了 {len(text)} 字）。")
        with self.lock:
            now = time.monotonic()
            key = who or name
            gap = now - self._last.get(key, -self.min_gap - 1)
            if gap < self.min_gap:
                raise ValueError(f"说得有点快，{self.min_gap - gap:.1f} 秒后再发。")
            self._last[key] = now
            self.data["seq"] += 1
            message = dict(id=self.data["seq"], at=time.strftime("%H:%M:%S"),
                           name=name, text=text, phase=phase)
            self.data["messages"].append(message)
            if len(self.data["messages"]) > MAX_MESSAGES:
                del self.data["messages"][: len(self.data["messages"]) - MAX_MESSAGES]
            self._save()
            return message

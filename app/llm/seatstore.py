"""哪些国家交给了模型——落盘，重启不丢。

原先座位只在内存里（`main._seats`），后果有两个，都不是小事：

- 重启或容器重建之后七个座位的勾选全丢，得一个个重新勾。P1 实测时每次改配置
  重启服务都要重勾一遍，很容易漏掉一个，然后那一国安静地不交令。
- 更要紧的是 `usage` 跟着没了——「这一局花了多少」在重启后归零，成本护栏
  也就重新开始，等于「重启一次就把预算重置了」。

所以这份文件只做一件事：记住「谁在座位上、各自花了多少、是用户按了暂停还是钱烧完了」。
**不存密钥**（那在 `llm.json`），也不存对局本身（那在 `state.json`）。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import fields
from pathlib import Path

from .seats import LLMSeat, Usage

log = logging.getLogger("diplomacy.llm")

FILENAME = "seats.json"


def path() -> Path:
    """跟存档、跟 `llm.json` 一起走（同一个 `DATA_DIR`，容器里一个卷全带走）。"""
    return Path(os.environ.get("DATA_DIR", "./data")) / FILENAME


def save(seats: dict[str, LLMSeat]) -> None:
    """原子写。座位表很小，写整份比想增量简单，也不会写坏半份。"""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {
        power: dict(paused=seat.paused, persona=seat.persona,
                    budget_cny=seat.budget_cny,
                    usage={f.name: getattr(seat.usage, f.name) for f in fields(Usage)})
        for power, seat in seats.items()
    }
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".seats-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load() -> dict[str, dict]:
    """读回来。读坏了当作「没存过」——不能让半份 JSON 把服务打挂。"""
    target = path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("座位表 %s 读取失败（%s），本次按「没有座位」处理", target, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _usage(raw) -> Usage:
    """容忍脏数据：不认识的键丢掉，类型不对的退回默认值。"""
    usage = Usage()
    if not isinstance(raw, dict):
        return usage
    for f in fields(Usage):
        if f.name not in raw:
            continue
        try:
            setattr(usage, f.name, type(getattr(usage, f.name))(raw[f.name]))
        except (TypeError, ValueError):
            pass
    return usage


def restore(into: dict[str, LLMSeat], cfg) -> list[str]:
    """把上次的座位装回去，返回装回来的国家列表。

    `provider` 拿不到（还没配模型）时**什么都不做**：装作有座位却不叫模型，
    比没有座位更难查。用户重启后看到「模型没勾」会去勾，看到「勾着但不动」只会困惑。
    """
    rows = load()
    provider = cfg.build_provider()
    if not rows or provider is None:
        return []
    restored = []
    for power, row in rows.items():
        if not isinstance(row, dict):
            continue
        seat = LLMSeat(power, provider, persona=str(row.get("persona") or ""),
                       budget_cny=float(row.get("budget_cny") or cfg.budget_cny),
                       price_in=cfg.price_in, price_out=cfg.price_out,
                       keep_hist=cfg.keep_hist, keep_msgs=cfg.keep_msgs)
        seat.usage = _usage(row.get("usage"))
        if row.get("paused"):
            seat.pause("已暂停")
        elif seat.usage.cost_cny > seat.budget_cny:
            # 重启不该把「预算已烧穿」这件事一起忘掉，否则就是重启一次多发一次钱
            seat.halt(f"超出预算 ¥{seat.budget_cny:.2f}")
        into[power] = seat
        restored.append(power)
    if restored:
        log.info("已恢复 %d 个模型座位：%s", len(restored), "、".join(restored))
    return restored

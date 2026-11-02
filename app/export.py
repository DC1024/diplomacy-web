"""对局导出：一份给人看的文本战报，一份给机器留的 JSON。

单独成模块的理由：这是**唯一**会把整局数据一次性搬出去的地方。搬错一个字段，
泄露的不是一条消息，而是七国的全部私信。所以这里不碰 HTTP、不碰鉴权，
私信的边界只由**一个入参**表达——`power`：

- 传座位名 → 只包含那个座位收发的信；
- 传空串 → 一封都不给（观众、匿名下载、管理员都是这一条）。

管理员也走 `power=""`：v0.7.5 定下的「管理是运维不是偷看」，导出不该是它的例外。
真正要整档备份就拷 `data/state.json`，那是另一件事。
"""
from __future__ import annotations

from datetime import datetime

from .engine.arena import POWERS, RESULT_LABELS, phase_name

SCHEMA = "diplomacy-web/export/1"

# 只为可读性：命令里仍然是英文代号（A BUD - SER），表头里给个中文名省得对不上号。
# 与前端 index.html 里那张 POWERS 表是同源的两份，改一处记得改另一处。
NAMES = {"AUSTRIA": "奥地利", "ENGLAND": "英国", "FRANCE": "法国", "GERMANY": "德国",
         "ITALY": "意大利", "RUSSIA": "俄国", "TURKEY": "土耳其"}


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def board(game) -> dict:
    """当前局面：每国的补给中心与部队。"""
    centers, units = game.get_centers(), game.get_units()
    return {p: dict(centers=sorted(centers.get(p, [])), units=sorted(units.get(p, [])))
            for p in POWERS}


def seat_messages(arena, power: str) -> list[dict]:
    """本座位收发的信件；`power` 为空时一封都不给。

    `GLOBAL` 是公开信，理论上谁都能读，但这里**故意不特判**：复盘页给观众的
    `messages` 就是空字符串，导出跟着同一条规矩走，免得「导出能看、复盘看不到」
    变成第二个需要解释的口径。
    """
    if not power:
        return []
    return [dict(phase=m["phase"], sender=m["sender"], to=list(m["to"]), text=m["text"])
            for m in arena.data.get("messages", [])
            if m["sender"] == power or power in m["to"]]


def _order_marks(order: str, status: dict) -> str:
    """一条命令 + 它被裁判判掉的原因（受阻 / 被击退 / 无效…）。"""
    unit = " ".join(order.split()[:2])
    failed = [RESULT_LABELS.get(r, r) for r in status.get(unit, []) if r]
    return order + (f"（{'、'.join(failed)}）" if failed else "")


def _phase_range(history: list[dict]) -> str:
    if not history:
        return "尚无结算"
    first, last = history[0]["phase"], history[-1]["phase"]
    if first == last:
        return f"{phase_name(first)}（1 个阶段）"
    return f"{phase_name(first)} — {phase_name(last)}（{len(history)} 个阶段）"


def markdown(arena, power: str = "") -> str:
    """文本战报（Markdown）。整局按时间顺序读下来，附本座位信件。"""
    game = arena.game()
    history = list(arena.data.get("history", []))
    done = arena.data.get("stage") == "done"
    lines = [
        f"# 外交战报 · {arena.data['match_id']}",
        "",
        f"- 进程：{_phase_range(history)}",
        f"- 当前：{phase_name(game.get_current_phase()) if not done else '对局已结束'}",
        f"- 结果：{arena.result_text() if done else '进行中'}",
        f"- 导出：{_stamp()} · 视角 "
        + (f"{power}（含本座位收发的信件）" if power else "公开（不含任何私信）"),
        "",
        "## 截至导出的局面",
        "",
        "| 国家 | 中心 | 部队 | 补给中心 |",
        "|---|---:|---:|---|",
    ]
    now_board = board(game)
    for p in POWERS:
        row = now_board[p]
        if not row["centers"] and not row["units"]:
            lines.append(f"| {p} {NAMES[p]} | 0 | 0 | 已出局 |")
            continue
        lines.append(f"| {p} {NAMES[p]} | {len(row['centers'])} | {len(row['units'])} "
                     f"| {' '.join(row['centers']) or '无'} |")

    lines += ["", "## 逐阶段", ""]
    if not history:
        lines.append("还没有结算记录。")
    for i, rec in enumerate(history, 1):
        lines += [f"### {i}. {phase_name(rec['phase'])}", ""]
        if rec.get("event") == "agreed_draw":
            lines += ["全体同意和局，本阶段命令未执行。", ""]
            continue
        results = rec.get("results") or {}
        orders = rec.get("orders") or {}
        anyone = False
        for p in POWERS:
            given = orders.get(p)
            if not given:
                continue
            anyone = True
            marks = "、".join(f"`{_order_marks(o, results.get(p, {}))}`" for o in given)
            lines.append(f"- **{p}** {NAMES[p]}：{marks}")
        if not anyone:
            lines.append("- （本阶段无人下令）")

        # 中心易主单独列一行：整局走向只看这一段就够，不必回翻上面的表格
        before = (rec.get("before") or {}).get("centers", {})
        after = rec.get("centers") or {}
        shifts = []
        for p in POWERS:
            gained = sorted(set(after.get(p, [])) - set(before.get(p, [])))
            lost = sorted(set(before.get(p, [])) - set(after.get(p, [])))
            if gained or lost:
                bit = f"{p}"
                if gained:
                    bit += f" +{' +'.join(gained)}"
                if lost:
                    bit += f" -{' -'.join(lost)}"
                shifts.append(bit)
        if shifts:
            lines.append(f"- 中心变化：{'；'.join(shifts)}")
        lines.append("")

    if power:
        letters = seat_messages(arena, power)
        lines += ["## 本座位往来信件", ""]
        if not letters:
            lines.append("这一局你没有收到也没有发出任何信件。")
        else:
            phase = None
            for m in letters:
                if m["phase"] != phase:
                    phase = m["phase"]
                    lines += ["", f"### {phase_name(phase)}"]
                dst = "全体" if "GLOBAL" in m["to"] else "、".join(m["to"])
                who = "你" if m["sender"] == power else m["sender"]
                lines.append(f"- **{who} → {dst}**：{m['text']}")
        lines.append("")

    lines += ["---",
              "由 diplomacy-web 导出。"
              + ("信件只含本座位收发的那些——换个座位导出就是另一份。" if power
                 else "公开视角：不含任何私信。")]
    return "\n".join(lines).rstrip() + "\n"


def payload(arena, power: str = "") -> dict:
    """JSON 版：同一份事实，按字段给机器用。

    刻意**不放**整份 `arena.data`：那里有 `tokens`。哪怕今天多一层过滤，明天内核
    加个新字段也会被顺手带出去——白名单比黑名单省心。
    """
    game = arena.game()
    history = list(arena.data.get("history", []))
    return dict(
        schema=SCHEMA,
        match_id=arena.data["match_id"],
        exported_at=datetime.now().isoformat(timespec="seconds"),
        viewer=power or "",
        phase=game.get_current_phase(),
        stage=arena.data.get("stage", ""),
        paused=bool(arena.data.get("paused")),
        result=arena.data.get("result"),
        result_text=arena.result_text(),
        powers=POWERS,
        board=board(game),
        settled=len(history),
        history=[dict(
            index=i,
            phase=rec["phase"],
            time=rec.get("time"),
            event=rec.get("event") or "",
            orders=rec.get("orders") or {},
            results=rec.get("results") or {},
            centers=rec.get("centers") or {},
            units=rec.get("units") or {},
        ) for i, rec in enumerate(history, 1)],
        messages=seat_messages(arena, power),
    )

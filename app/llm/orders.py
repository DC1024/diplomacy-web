"""保底命令：模型没给出可用命令时，也要让对局能往前走。

裁判要求「每支需要行动的部队都要下令」，空列表不算合法，所以保底不能提交 `[]`，
必须真的算出一份合法的驻守/裁军命令。
"""
from __future__ import annotations


def _dedupe_by_loc(orders: list[str]) -> list[str]:
    """圣彼得堡这类双海岸地点会给出 STP/NC、STP/SC 两条，同一地点只能留一条。"""
    seen, out = set(), []
    for order in orders:
        parts = order.split()
        if len(parts) < 2:
            continue
        base = parts[1].split("/")[0]
        if base not in seen:
            seen.add(base)
            out.append(order)
    return out


def hold_orders(game, power: str) -> list[str]:
    """按当前阶段类型生成一份合法且保守的命令。"""
    phase = game.get_current_phase()
    possible = game.get_all_possible_orders()
    locs = game.get_orderable_locations(power)

    if phase.endswith("A"):
        count = game.get_state().get("builds", {}).get(power, {}).get("count", 0)
        if count < 0:
            # 必须裁军：优先裁掉内陆陆军，减少后续选择面损失
            cand = _dedupe_by_loc([o for loc in locs for o in sorted(possible.get(loc, []))
                                   if o.endswith(" D")])
            cand.sort(key=lambda o: (0 if o.startswith("A ") else 1, o))
            return cand[:abs(count)]
        # 可建可不建，保底一律不建
        return []

    if phase.endswith("R"):
        # 撤退阶段：能撤就撤到第一个合法地点，实在没地方就解散
        out = []
        for loc in locs:
            opts = sorted(possible.get(loc, []))
            if not opts:
                continue
            retreat = [o for o in opts if " R " in o]
            out.append(retreat[0] if retreat else opts[0])
        return _dedupe_by_loc(out)

    out = []
    for loc in locs:
        opts = sorted(possible.get(loc, []))
        if not opts:
            continue
        holds = [o for o in opts if o.endswith(" H")]
        out.append(holds[0] if holds else opts[0])
    return _dedupe_by_loc(out)

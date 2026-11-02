"""玩家可见文字的中英双语词条。

只做一件事：把「玩家会读到的那句话」按语言取出来。字串集中在这里，而不是散在
`arena.py` 的 f-string 里——加第三种语言时不用满仓库找引号，改一句话也不用担心
改到裁判逻辑。

两条约定：

- **默认语言是中文**。`brief()`、`phase_name()`、`result_text()` 不传 `lang` 时行为与
  以前完全一致，所以模型座位的上下文、导出、旧测试都不受影响；双语是叠加层，不是替换。
- **占位符用 `{}`**，一律带名字（`{power}` 而不是 `{}`）。位置参数在双语之间顺序容易错位，
  而这类错误在中文里根本看不出来。

国家代号仍用引擎的 `AUSTRIA` 这套（命令、存档、令牌全靠它），翻译只发生在展示层。
"""
from __future__ import annotations

LANGS = ("zh", "en")
DEFAULT = "zh"


def pick(value: str | None) -> str:
    """把外部传来的语言标记规整成受支持的两种之一。

    接受 `en`、`en-US`、`EN_us` 之类的写法——浏览器的 `navigator.language` 就是这种形状，
    前端直接把原样带上来了。认不出来的（含空）落到中文。
    """
    tag = (value or "").strip().lower().replace("_", "-")
    return "en" if tag.split("-")[0] == "en" else DEFAULT


# 国家名：简报、结算、座位表都用它。英文用通行国名，中文用外交棋圈里的惯称。
POWERS = {
    "AUSTRIA": ("奥地利", "Austria"),
    "ENGLAND": ("英国", "England"),
    "FRANCE": ("法国", "France"),
    "GERMANY": ("德国", "Germany"),
    "ITALY": ("意大利", "Italy"),
    "RUSSIA": ("俄国", "Russia"),
    "TURKEY": ("土耳其", "Turkey"),
}

SEASONS = {"S": ("春", "Spring"), "F": ("秋", "Fall"), "W": ("冬", "Winter")}
KINDS = {"M": ("外交与移动", "Diplomacy & Movement"),
         "R": ("撤退", "Retreats"),
         "A": ("增兵与裁军", "Builds & Disbands")}

# 引擎给的结算标记（`results[unit]`）。只翻展示，判断逻辑仍然比原字符串。
RESULT_LABELS = {
    "bounce": ("受阻", "bounced"),
    "dislodged": ("被击退", "dislodged"),
    "cut": ("支援被切断", "support cut"),
    "void": ("无效", "void"),
    "disrupted": ("海运中断", "convoy disrupted"),
    "no convoy": ("无有效海运", "no convoy"),
    "disband": ("解散", "disbanded"),
}

# key -> (中文, English)
_M = {
    # ---- 简报外壳 ----
    "brief.title": ("==== 你是 {power} · 当前 {phase} ====",
                    "==== You are {power} · {phase} ===="),
    "brief.title_done": ("==== 你是 {power} · 已结束 ====",
                         "==== You are {power} · game over ===="),
    "brief.new_msgs": ("【新消息 {n} 条】", "[{n} new message(s)]"),
    "brief.msg_line": ("[{phase}] {sender} → {target}：{text}",
                       "[{phase}] {sender} → {target}: {text}"),
    "brief.no_msgs": ("【暂无消息】", "[no messages yet]"),
    "brief.to_all": ("全体", "everyone"),
    "brief.to_you": ("你", "you"),
    "brief.game_over": ("【整局结束】{result}", "[Game over] {result}"),
    "brief.eliminated": ("【你已被淘汰】无需再操作。",
                         "[You are eliminated] Nothing left to do."),
    "brief.no_action": ("【本阶段你无需行动】可以继续外交。",
                        "[No orders needed this phase] You may keep negotiating."),
    "brief.no_action_wait": ("【本阶段你无需行动】等待其他国家处理。",
                             "[No orders needed this phase] Waiting for the others."),

    # ---- 本阶段任务 ----
    "task.need_orders": ("【本阶段你需要】给每支部队下令（未下令的部队不会自动驻守）。",
                         "[Your move] Order every unit (unordered units do not auto-hold)."),
    "task.need_retreats": ("【本阶段你需要】为被击退的部队选择撤退或解散：",
                           "[Your move] Choose retreat or disband for each dislodged unit:"),
    "task.need_builds": ("【本阶段你需要】最多增兵 {delta} 支（也可少建或不建，提交空命令即放弃）。可选：",
                         "[Your move] Up to {delta} build(s) (you may build fewer or none — "
                         "submit no orders to waive). Options:"),
    "task.need_disbands": ("【本阶段你需要】必须裁军 {n} 支。可选：",
                           "[Your move] You must disband {n} unit(s). Options:"),
    "task.loc_list": ("  {loc}：{orders}", "  {loc}: {orders}"),
    "task.submitted": ("你已提交：{orders}。需要修改就重新提交。",
                       "You submitted: {orders}. Submit again to change."),
    "task.submitted_empty": ("（空命令）", "(no orders)"),
    "task.not_submitted": ("你尚未提交。", "You have not submitted yet."),
    "task.others": ("其他需行动国家已交 {done}/{total}。",
                    "Other powers that must act: {done}/{total} submitted."),
    "task.draw_votes": ("和局赞成：{list}", "Draw votes: {list}"),

    # ---- 局面 ----
    "board.centers": ("【补给中心】（18 个单独获胜）",
                      "[Supply centers] (18 to win alone)"),
    "board.units": ("【部队】（* 表示被击退、等待撤退）",
                    "[Units] (* = dislodged, awaiting retreat)"),
    "board.you": ("（你）", " (you)"),
    "board.none": ("无", "none"),
    "board.line": ("{power}{tag} {n}：{items}", "{power}{tag} {n}: {items}"),
    "board.neutral": ("中立 {n}：{list}", "Neutral {n}: {list}"),

    # ---- 结算 ----
    "settle.header": ("—— {phase} 结算 ——", "—— {phase} · resolution ——"),
    "settle.draw": ("—— {phase}：全体同意和局，本阶段命令未执行 ——",
                    "—— {phase}: everyone agreed to a draw; orders were not executed ——"),
    "settle.mark": ("{order}（{reasons}）", "{order} ({reasons})"),
    "settle.power_line": ("{power}{tag}：{orders}", "{power}{tag}: {orders}"),
    "settle.order_sep": ("；", "; "),
    "settle.center_delta": ("中心变化 {power}：{delta}", "Center changes {power}: {delta}"),
    "settle.list_sep": ("、", ", "),

    # ---- 合法命令 / 收件箱 / 历史 ----
    "legal.none": ("本阶段你没有可下令的部队。", "You have no units to order this phase."),
    "legal.line": ("{loc}：{orders}", "{loc}: {orders}"),
    "inbox.none": ("没有符合条件的消息。", "No messages match."),
    "history.none": ("尚无结算记录。", "No resolution recorded yet."),

    # ---- 结果 ----
    "result.victory": ("18 中心单独胜利", "Solo victory with 18 centers"),
    "result.agreed_draw": ("全体同意和局", "Draw by agreement"),
    "result.turn_limit": ("{year} 年秋季到期和局", "Draw at the end of Fall {year}"),
    "result.line": ("{reason}：{who}", "{reason}: {who}"),
    "result.points": ("。得分：{points}", ". Scores: {points}"),

    # ---- 提交校验（会原样弹给玩家） ----
    "orders.illegal": ("不合法：{order}", "Illegal: {order}"),
    "orders.illegal_hint": ("。{loc} 可选：{options}", ". {loc} allows: {options}"),
    "orders.illegal_noloc": ("。该地点没有你的可下令部队。",
                             ". You have no orderable unit there."),
    "orders.duplicate": ("{loc} 重复下令，只能保留一条。",
                         "Duplicate orders for {loc}; keep only one."),
    "orders.missing": ("缺少命令：{locs}（每支需要行动的部队都要下令）",
                       "Missing orders: {locs} (every unit that can act must be ordered)"),
    "orders.disband_count": ("需要裁军 {n} 支，你提交了 {m} 条。",
                             "You must disband {n} unit(s); you submitted {m}."),
    "orders.build_count": ("最多增兵 {n} 支，你提交了 {m} 条。",
                           "At most {n} build(s); you submitted {m}."),

    # ---- 操作回执 ----
    "act.game_over": ("整局已结束。", "The game is over. "),
    "act.frozen": ("对局已冻结，本次操作未执行。结束回复，等待恢复。",
                   "The game is frozen; nothing was executed. Stop replying and wait."),
    "act.eliminated": ("你已被淘汰。", "You have been eliminated."),
    "act.only_movement": ("只有春、秋移动阶段可以发消息。",
                          "Messages can only be sent in Spring and Fall movement phases."),
    "act.bad_recipient": ("收件人必须是其他在场国家，或 GLOBAL（全体公开）。",
                          "The recipient must be another power still in the game, or GLOBAL."),
    "act.empty_message": ("消息不能为空。", "The message cannot be empty."),
    "act.message_too_long": ("消息 {n} 字，超过上限 {limit} 字（含标点）。请缩短后重发。",
                             "Message is {n} characters, over the {limit}-character limit "
                             "(punctuation included). Shorten it and resend."),
    "act.sent": ("已发送给 {to}。", "Sent to {to}."),
    "act.no_orders_needed": ("本阶段你无需下令。", "You have nothing to order this phase."),
    "act.bad_order_format": ("命令格式错误。", "Malformed order."),
    "act.orders_in": ("军令已提交：{orders}。收到新消息时你会被唤醒，可以再改。",
                      "Orders submitted: {orders}. You will be woken on new messages and "
                      "may change them."),
    "act.draw_movement_only": ("只有春、秋移动阶段可以表决和局。",
                               "Draws can only be voted in Spring and Fall movement phases."),
    "act.draw_too_early": ("本局 {year} 年春季起才能表决协议和局。",
                           "Agreed draws may be voted from Spring {year}."),
    "act.draw_passed": ("全体同意，和局成立。", "Everyone agreed — the game is a draw. "),
    "act.draw_voted": ("已赞成和局", "Voted for a draw"),
    "act.draw_unvoted": ("已撤回和局赞成", "Withdrew the draw vote"),
    "act.draw_tally": ("。当前赞成：{votes}；需要全部在场国家同意。",
                       ". Currently in favour: {votes}; every power still in the game must agree."),
    "act.unknown": ("未知操作", "Unknown action"),
}


def t(lang: str, key: str, **kw) -> str:
    """取一条词条并填空。缺 key 时直接把 key 抛到脸上——静默返回空串更难查。"""
    pair = _M.get(key)
    if pair is None:
        raise KeyError(f"没有这条词条：{key}")
    template = pair[1] if pick(lang) == "en" else pair[0]
    return template.format(**kw) if kw else template


def power_name(code: str, lang: str = DEFAULT) -> str:
    """国家代号 → 展示名。认不出来的原样返回（别把没见过的代号吞成空串）。"""
    pair = POWERS.get(code.upper())
    if not pair:
        return code
    return pair[1] if pick(lang) == "en" else pair[0]


def power_names(lang: str = DEFAULT) -> dict[str, str] :
    return {code: power_name(code, lang) for code in POWERS}


def phase_name(phase: str | None, lang: str = DEFAULT) -> str:
    """`S1901M` → `1901年春季·外交与移动（S1901M）` / `Spring 1901 · Diplomacy & Movement (S1901M)`.

    没见过的阶段记号原样返回：那是引擎的状态（`COMPLETED` / `FORMING`），翻译它只会撒谎。
    """
    if not phase or phase in ("COMPLETED", "FORMING"):
        return phase or ""
    i = 1 if pick(lang) == "en" else 0
    season = SEASONS.get(phase[0], (phase[0], phase[0]))[i]
    kind = KINDS.get(phase[-1], (phase[-1], phase[-1]))[i]
    if i:
        return f"{season} {phase[1:5] } · {kind} ({phase})"
    return f"{phase[1:5]}年{season}·{kind}（{phase}）"


def result_label(code: str, lang: str = DEFAULT) -> str:
    pair = RESULT_LABELS.get(code)
    if not pair:
        return code
    return pair[1] if pick(lang) == "en" else pair[0]

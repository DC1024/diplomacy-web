"""B1：LLM 座位的上下文治理、输出解析与降级路径。

全部用 ScriptedProvider 驱动——不联网、不花钱，验证的是「组装→询问→校验→落子」
这条链路本身，而不是某个模型的智力。
"""
import importlib

import pytest
from fastapi.testclient import TestClient

from app import i18n
from app.engine.arena import POWERS
from app.llm import LLMSeat, ScriptedProvider, SeatRunner
from app.llm.context import KEEP_MSGS, SELF_CHARS, SELF_KEEP, estimate_tokens
from app.llm.context import build as build_context
from app.llm.orders import hold_orders
from app.llm.seats import parse_action


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        # 这个文件里的用例自己驱动 tick() 或直接调 seat.act()；把 lifespan 起的后台线程停掉，
        # 否则两个驱动者会抢同一批 (revision, power)，断言时而"已提交"时而"没动"。
        # 线程到底会不会起，由 test_seats_and_clock.py 里那条专门的用例盯着。
        main._runner.stop()
        yield client, main


def seat(main, power, script=None, **kw):
    s = LLMSeat(power, ScriptedProvider(script or [{"pass": True}]), **kw)
    main._seats[power] = s
    return s


def test_estimate_tokens_prefers_cjk():
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文" * 100) < estimate_tokens("x" * 600)


def test_parse_action_tolerates_chatter_and_fences():
    assert parse_action('```json\n{"pass": true}\n```') == {"pass": True}
    assert parse_action('好的，我的决定是：{"orders": ["A PAR H"]} 完毕')["orders"] == ["A PAR H"]
    assert parse_action("完全不是 JSON") is None
    assert parse_action("") is None


def test_hold_orders_are_legal_and_cover_every_unit(srv):
    client, main = srv
    client.post("/api/match")
    a = main.require_arena()
    game = a.game()
    orders = hold_orders(game, "FRANCE")
    assert len(orders) == 3
    # 真提交一次，确认裁判认可
    assert a.act("FRANCE", dict(action="orders", orders=orders, request_id="t1"))["ok"] is True


def test_context_window_stays_bounded(srv):
    """打若干阶段后，上下文不应随历史线性膨胀。"""
    client, main = srv
    client.post("/api/match")
    seats = {p: seat(main, p) for p in POWERS}
    runner = SeatRunner(main.arena, seats, interval=0)

    sizes = []
    for _ in range(12):
        runner.tick()
        state = client.get("/api/state").json()
        if state["game_over"]:
            break
        sizes.append(build_context(main.require_arena(), "ITALY").stats["chars"])

    assert sizes, "没有跑到任何阶段"
    assert max(sizes) < 6000, f"上下文失控：{sizes}"
    ctx = build_context(main.require_arena(), "ITALY")
    assert ctx.dropped_history > 0
    assert ctx.digest.startswith("【早期局势摘要】")


def test_pass_is_turned_into_legal_holds(srv):
    client, main = srv
    client.post("/api/match")
    s = seat(main, "AUSTRIA", [{"pass": True}])
    out = s.act(main.require_arena())
    assert out["ok"] is True
    assert out["orders"], "pass 必须落成真正的驻守命令"
    assert main.require_arena().admin_state()["players"]["AUSTRIA"]["ready"] is True


def test_illegal_orders_fall_back_without_stalling(srv):
    client, main = srv
    client.post("/api/match")
    s = seat(main, "ITALY", [{"orders": ["A NAP - ATLANTIS"]}])
    out = s.act(main.require_arena())
    assert out["ok"] is True
    assert out.get("fallback") is True or "保底" in out["note"]
    assert s.usage.fallbacks == 1
    assert s.usage.retries >= 1
    assert main.require_arena().admin_state()["players"]["ITALY"]["ready"] is True


def test_garbage_output_retries_then_falls_back(srv):
    client, main = srv
    client.post("/api/match")
    s = seat(main, "RUSSIA", ["我认为应该进攻。"])
    out = s.act(main.require_arena())
    assert out["ok"] is True
    assert s.usage.retries == 1
    assert s.usage.fallbacks == 1


def test_budget_trips_and_seat_stops(srv):
    client, main = srv
    client.post("/api/match")
    s = seat(main, "TURKEY", [{"pass": True}], budget_cny=0.000001)
    s.act(main.require_arena())
    assert s.enabled is False
    assert "预算" in s.stop_reason
    assert s.act(main.require_arena())["skipped"] is True


def test_messages_from_model_respect_limits(srv):
    client, main = srv
    client.post("/api/match")
    long_text = "很长的信" * 100  # 400 字，超过私信 300 上限
    s = seat(main, "GERMANY", [{"say": [{"to": "FRANCE", "text": long_text}]}])
    out = s.act(main.require_arena())
    assert out["said"][0]["ok"] is False
    assert "超过上限" in out["said"][0]["error"]


def test_seat_sees_its_own_offer_before_the_reply(srv):
    """谈判记忆：模型必须看得到自己发出的信。

    引擎的 `incoming()` 过滤掉 `sender == power`，所以 FRANCE 提了条款、AUSTRIA 回了
    「同意。」之后，FRANCE 的上下文里原先只剩那句「同意。」——同意的是什么无从得知。
    承诺变成随机遗忘，结盟与背叛就不再是有意的策略。人类收件箱（`inbox()`）本来就含
    自己发出的信，`context._own()` 只是把模型补齐到同一水平。
    """
    client, main = srv
    client.post("/api/match")
    a = main.require_arena()
    a.act("FRANCE", dict(action="send", to="AUSTRIA", text="我不打比利时，你把北海让给我。",
                         request_id="m1"))
    a.act("AUSTRIA", dict(action="send", to="FRANCE", text="同意。", request_id="m2"))

    mine = build_context(a, "FRANCE").as_prompt()
    # 断言跟着展示层走：座位上下文里出现的是国名（也是模型谈判时的称呼），
    # 不是引擎代号。代号只活在命令、存档和 API 参数里。
    at, fr = i18n.power_name("AUSTRIA"), i18n.power_name("FRANCE")
    assert f"你 → {at}：我不打比利时，你把北海让给我。" in mine, mine
    assert f"[S1901M] {at} → 你：同意。" in mine, mine

    theirs = build_context(a, "AUSTRIA").as_prompt()
    assert f"[S1901M] {fr} → 你：我不打比利时" in theirs  # 收件人照旧看得到原文
    assert f"你 → {at}" not in theirs  # 别把「自己发出」的块抄进别人上下文

    outsider = build_context(a, "GERMANY").as_prompt()
    assert "比利时" not in outsider and "→ 你：同意。" not in outsider


def test_own_messages_are_capped_and_truncated(srv):
    """自己发出的信也要封顶：只留最近 6 封，单封截到 120 字。

    否则一个每回合写满 300 字的座位，光「自己说过什么」就能把上下文顶回全量水位。
    """
    client, main = srv
    client.post("/api/match")
    a = main.require_arena()
    for i in range(10):
        a.act("ITALY", dict(action="send", to="FRANCE", text=f"第{i}号提案：" + "细节" * 100,
                            request_id=f"c{i}"))

    ctx = build_context(a, "ITALY")
    assert ctx.stats["kept_own"] == SELF_KEEP
    assert "第3号提案" not in ctx.own, "最早的几封应当已经滑出窗口"
    assert "第4号提案" in ctx.own and "第9号提案" in ctx.own
    assert "…" in ctx.own, "超出 120 字的应该被截断"
    assert max(len(line) for line in ctx.own.splitlines()) < SELF_CHARS + 40


def test_opponent_message_appears_exactly_once(srv):
    """补自己的信，不能把对方的信重抄一遍——否则模型会以为收到两封。"""
    client, main = srv
    client.post("/api/match")
    a = main.require_arena()
    a.act("FRANCE", dict(action="send", to="AUSTRIA", text="北海归我。", request_id="n1"))
    a.act("AUSTRIA", dict(action="send", to="FRANCE", text="同意。", request_id="n2"))
    assert build_context(a, "FRANCE").as_prompt().count("同意。") == 1


def test_dropped_messages_are_announced(srv):
    """消息按条数裁，滑出窗口的要说明一句。

    没有这句话，模型会把「这一回合没看到某国来信」当成「它没找过我」，
    而真相可能是那封信在三个阶段前就滑出窗口了。
    """
    client, main = srv
    client.post("/api/match")
    a = main.require_arena()
    for i in range(40):
        a.act("FRANCE", dict(action="send", to="ITALY", text=f"第{i}封：条件照旧。",
                             request_id=f"w{i}"))

    ctx = build_context(a, "ITALY")
    assert ctx.dropped_messages == 40 - KEEP_MSGS
    assert f"更早的 {40 - KEEP_MSGS} 条消息已滑出" in ctx.note
    prompt = ctx.as_prompt()
    assert "第0封" not in prompt and "第39封" in prompt


def test_debug_context_matches_the_real_prompt(srv):
    """`/api/llm/context` 必须原样拼出模型真收到的东西，否则调试面板只是自我安慰。"""
    client, main = srv
    client.post("/api/match")
    a = main.require_arena()
    a.act("TURKEY", dict(action="send", to="RUSSIA", text="黑海我不动。", request_id="d1"))

    d = client.get("/api/llm/context?power=TURKEY").json()
    joined = "\n".join(p for p in (d["digest"], d["own"], d["note"], d["text"]) if p)
    assert joined == build_context(a, "TURKEY").as_prompt()
    assert f"你 → {i18n.power_name('RUSSIA')}：黑海我不动。" in d["own"]
    assert d["stats"]["kept_own"] == 1


def test_full_game_driven_entirely_by_seats(srv):
    """七个座位全部交给模型：整局能自己打完，不会卡死在某个阶段。

    用应用自己的 `_runner`（而不是另造一个）——那才是生产里真正驱动座位的那个对象。
    但要把它的后台线程停掉再手动 tick：两个驱动者同时在跑，会互相把 `_acted` 里的
    (revision, power) 抢先消化掉，断言"这一轮没人动"就失去意义了。
    """
    client, main = srv
    client.post("/api/match")
    seats = {p: seat(main, p) for p in POWERS}
    main._runner.stop()
    runner = main._runner

    rounds = 0
    while rounds < 240:
        state = client.get("/api/state").json()
        if state["game_over"]:
            break
        results = runner.tick()
        rounds += 1
        if not results:
            after = client.get("/api/state").json()
            assert after["game_over"] or after["phase"] != state["phase"], \
                f"卡在 {state['phase']}：无人行动也未推进"
    else:
        pytest.fail("阶段数超过 240，疑似死循环")

    final = client.get("/api/state").json()
    assert final["game_over"] is True
    assert final["result"]["type"] in ("solo", "draw")
    total_fallbacks = sum(s.usage.fallbacks for s in seats.values())
    assert total_fallbacks == 0, "脚本模型只发 pass，不该有任何保底降级"
    cli = client.get("/api/llm").json()
    assert cli["seats"], "座位状态应可查询"
    assert cli["total"]["tokens"] > 0


def test_admin_key_protects_seat_config(srv, monkeypatch):
    import app.main as main

    monkeypatch.setenv("ADMIN_KEY", "k")
    importlib.reload(main)
    with TestClient(main.app) as client:
        client.post("/api/match", headers={"X-Admin-Key": "k"})
        assert client.post("/api/llm/seat", json={"power": "FRANCE"}).status_code == 403
        monkeypatch.setenv("LLM_PROVIDER", "scripted")
        r = client.post("/api/llm/seat", json={"power": "FRANCE"}, headers={"X-Admin-Key": "k"})
        assert r.status_code == 200 and r.json()["enabled"] is True
        assert client.post("/api/llm/tick", headers={"X-Admin-Key": "k"}).status_code == 200

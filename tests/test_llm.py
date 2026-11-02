"""B1：LLM 座位的上下文治理、输出解析与降级路径。

全部用 ScriptedProvider 驱动——不联网、不花钱，验证的是「组装→询问→校验→落子」
这条链路本身，而不是某个模型的智力。
"""
import importlib

import pytest
from fastapi.testclient import TestClient

from app.engine.arena import POWERS
from app.llm import LLMSeat, ScriptedProvider, SeatRunner
from app.llm.context import build as build_context
from app.llm.context import estimate_tokens
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


def test_full_game_driven_entirely_by_seats(srv):
    """七个座位全部交给模型：整局能自己打完，不会卡死在某个阶段。"""
    client, main = srv
    client.post("/api/match")
    seats = {p: seat(main, p) for p in POWERS}
    runner = SeatRunner(main.arena, seats, interval=0)

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

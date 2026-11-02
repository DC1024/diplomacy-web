"""座位账本：暂停不等于收回、重启不丢、全场有一道总闸（A 组三件事）。

三件都是「花了钱却看不见」的形态：

- **收回座位**把对象连同 `usage` 一起删掉 → 想先别花钱又想知道花了多少，做不到。
  所以拆成「暂停」与「移除」两个动作。
- **座位只在内存里** → 重启后勾选全丢，更糟的是花费归零、成本护栏跟着重置，
  等于「重启一次就多发一次钱」。
- **只有单座位预算** → 七家各 ¥20 就是 ¥140，而验收口径是「一局 < ¥20」。
  总闸撞线后不能冻桌：座位转成「停摆 + 代驻守」，局面继续走，只是不再花钱。
"""
import importlib
import sys

from fastapi.testclient import TestClient

from app.engine.arena import POWERS
from app.llm import LLMSeat, ScriptedProvider, seatstore


def make_server(tmp_path, monkeypatch, admin="k"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PHASE_SECONDS", "0")
    monkeypatch.setenv("LLM_PROVIDER", "scripted")   # 离线 provider：这些用例不该联网
    monkeypatch.setenv("ADMIN_KEY", admin)
    import app.main as main

    importlib.reload(main)
    return TestClient(main.app)


def started(tmp_path, monkeypatch, admin="k"):
    client = make_server(tmp_path, monkeypatch, admin)
    assert client.post("/api/match", headers={"X-Admin-Key": admin}).status_code == 200
    return client, sys.modules["app.main"], {"X-Admin-Key": admin}


def seat(main, power, *, cost=0.0, calls=1) -> LLMSeat:
    s = LLMSeat(power, ScriptedProvider([{"pass": True}]))
    s.usage.calls = calls
    s.usage.cost_cny = cost
    main._seats[power] = s
    return s


def test_pausing_keeps_the_ledger(tmp_path, monkeypatch):
    """暂停只停开关，不停账本——「先别花钱但我要看账」必须做得到。"""
    client, main, ok = started(tmp_path, monkeypatch)
    with client:
        main._runner.stop()
        d = client.post("/api/llm/seat", json={"power": "ITALY"}, headers=ok).json()
        assert d["enabled"] is True

        seat(main, "ITALY", cost=1.25, calls=7)

        d = client.post("/api/llm/seat", json={"power": "ITALY", "enabled": False},
                        headers=ok).json()
        assert d["paused"] is True
        assert "ITALY" in main._seats                     # 座位还在
        assert main._seats["ITALY"].usage.cost_cny == 1.25  # 账也还在
        assert main._seats["ITALY"].enabled is False
        # 前端读到的也是这条（勾选框据此取消勾选，而不是整行变「人类操作」）
        assert d["seats"]["ITALY"]["paused"] is True
        assert d["seats"]["ITALY"]["usage"]["cost_cny"] == 1.25

        # 再勾回来：接着上一份账继续，不清零
        d = client.post("/api/llm/seat", json={"power": "ITALY"}, headers=ok).json()
        assert d["seat"]["enabled"] is True
        assert d["seat"]["usage"]["cost_cny"] == 1.25
        assert d["seat"]["paused"] is False


def test_removing_a_seat_really_drops_it(tmp_path, monkeypatch):
    """真要清掉（连同账）得显式说 remove——否则「取消勾选」会让人以为账也没了。"""
    client, main, ok = started(tmp_path, monkeypatch)
    with client:
        main._runner.stop()
        client.post("/api/llm/seat", json={"power": "ITALY"}, headers=ok)
        d = client.post("/api/llm/seat",
                        json={"power": "ITALY", "enabled": False, "remove": True},
                        headers=ok).json()
        assert d["removed"] is True
        assert "ITALY" not in main._seats


def test_seats_survive_a_restart(tmp_path, monkeypatch):
    """重启后勾选与花费都要回来：花费归零等于预算护栏重置，重启一次就多发一次钱。"""
    client, main, ok = started(tmp_path, monkeypatch)
    with client:
        main._runner.stop()
        client.post("/api/llm/seat", json={"power": "FRANCE"}, headers=ok)
        seat(main, "FRANCE", cost=3.5, calls=9)
        seatstore.save(main._seats)

    # 换一个进程（重新 reload = 内存里的 _seats 清空），同一个 DATA_DIR
    client2 = make_server(tmp_path, monkeypatch)
    main2 = sys.modules["app.main"]
    with client2:
        main2._runner.stop()
        assert "FRANCE" in main2._seats, "座位没恢复"
        assert main2._seats["FRANCE"].usage.cost_cny == 3.5
        assert main2._seats["FRANCE"].usage.calls == 9


def test_a_seat_that_already_burned_its_budget_stays_stopped_after_restart(tmp_path, monkeypatch):
    """重启不能把「预算已烧穿」这件事忘掉。"""
    client, main, ok = started(tmp_path, monkeypatch)
    with client:
        main._runner.stop()
        client.post("/api/llm/seat", json={"power": "RUSSIA"}, headers=ok)
        main._seats["RUSSIA"].budget_cny = 5.0
        main._seats["RUSSIA"].usage.cost_cny = 6.0     # 已经烧穿 5 元
        seatstore.save(main._seats)

    client2 = make_server(tmp_path, monkeypatch)
    main2 = sys.modules["app.main"]
    with client2:
        main2._runner.stop()
        s = main2._seats["RUSSIA"]
        assert s.enabled is False and s.autohold is True
        assert "超出预算" in s.stop_reason


def test_total_budget_stops_everyone_without_freezing_the_table(tmp_path, monkeypatch):
    """总闸撞线：所有座位转驻守，但局面必须继续往前推。"""
    client, main, ok = started(tmp_path, monkeypatch)
    with client:
        main._runner.stop()
        assert client.put("/api/llm/settings", json={"total_budget_cny": 2.0},
                          headers=ok).status_code == 200
        a = main.arena()
        for p in ("ITALY", "GERMANY"):
            seat(main, p, cost=1.2)      # 合计 2.4 > 2.0

        main._runner.tick()

        for p in ("ITALY", "GERMANY"):
            s = main._seats[p]
            assert s.enabled is False, p
            assert s.autohold is True, p
            assert "全场预算" in s.stop_reason, s.stop_reason
            # 代驻守已经交上去了：没人交令就会冻桌
            assert a.admin_state()["players"][p]["ready"] is True, p

        # 撞线之后不再叫模型：这一轮没有新的调用
        before = sum(s.usage.calls for s in main._seats.values())
        main._runner.tick()
        assert sum(s.usage.calls for s in main._seats.values()) == before


def test_a_paused_seat_is_not_auto_held(tmp_path, monkeypatch):
    """用户按的暂停不能变成「后台替它驻守」——那会把接手者的命令盖掉。"""
    client, main, _ = started(tmp_path, monkeypatch)
    with client:
        main._runner.stop()
        a = main.arena()
        s = seat(main, "ITALY")
        s.pause("已暂停（用量保留）")
        main._runner.tick()
        assert a.admin_state()["players"]["ITALY"]["ready"] is False


def test_budget_stop_marks_the_seat_for_auto_hold():
    """单座位撞线走的是 halt（代驻守），不是 pause——两者都停模型，但后者不交令。"""
    from app.llm.provider import Reply

    s = LLMSeat("AUSTRIA", ScriptedProvider([{"pass": True}]), budget_cny=1.0)
    s._bill(Reply(text="x", prompt_tokens=1_000_000, completion_tokens=0))
    assert s.enabled is False
    assert s.autohold is True
    assert s.paused is False
    assert "超出预算" in s.stop_reason


def test_every_power_can_be_restored(tmp_path, monkeypatch):
    """七个国家都能装回来，顺序无所谓（JSON 对象不保证顺序，别依赖它）。"""
    client, main, ok = started(tmp_path, monkeypatch)
    with client:
        main._runner.stop()
        for p in POWERS:
            client.post("/api/llm/seat", json={"power": p}, headers=ok)
        assert set(main._seats) == set(POWERS)
        seatstore.save(main._seats)

    client2 = make_server(tmp_path, monkeypatch)
    main2 = sys.modules["app.main"]
    with client2:
        main2._runner.stop()
        assert set(main2._seats) == set(POWERS)

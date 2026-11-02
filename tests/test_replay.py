"""P2：复盘页。

数据本身都在 `state.json` 里（history + messages），这里只测两件事：
逐阶段能取回来，以及**私信视角不能越界**——复盘是最容易把别国信件顺手漏出去的地方。
"""
import importlib

import pytest
from fastapi.testclient import TestClient

from app.engine.arena import POWERS
from app.llm.orders import hold_orders


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        main._runner.stop()
        yield client, main


def start(client) -> dict:
    return client.post("/api/match").json()["tokens"]


def settle_one(main, extra=None) -> str:
    """把当前阶段按「全员驻守」结掉，返回结算前的阶段名。

    `Arena.act()` 自己**不结算**——`auto_settle` 由 HTTP 层在 `try_settle()` 里触发，
    直接调内核时得自己补这一下。顺带注意 `settle()` 可能一口气推进多个「没人需要下令」
    的阶段（撤退、调整），所以history 一次可能长出好几条。
    """
    a = main.require_arena()
    phase = a.game().get_current_phase()
    if extra:
        extra(a, phase)
    for p in POWERS:
        a.act(p, dict(action="orders", orders=hold_orders(a.game(), p),
                      request_id=f"hold-{phase}-{p}"))
    a.settle(quiet=True)
    return phase


def test_replay_is_empty_before_the_first_settlement(srv):
    client, _main = srv
    tokens = start(client)
    d = client.get("/api/replay", headers={"X-Seat-Token": tokens["FRANCE"]}).json()
    assert d["power"] == "FRANCE"
    assert d["items"] == [] and d["settled"] == 0
    assert d["current_phase"] == "S1901M"


def test_replay_returns_one_entry_per_settled_phase(srv):
    client, main = srv
    tokens = start(client)
    first = settle_one(main)
    second = settle_one(main)

    d = client.get("/api/replay", headers={"X-Seat-Token": tokens["ITALY"]}).json()
    assert d["settled"] == 2
    assert [it["phase"] for it in d["items"]] == [first, second]
    assert [it["index"] for it in d["items"]] == [1, 2]
    # 结算完要看下一阶段，不能还停在刚结算的那个阶段上
    assert d["current_phase"] != second

    head = d["items"][0]
    assert "结算" in head["text"] and "ITALY" in head["text"]
    # 1901 春开局七国各 3 中心（俄国 4），中心数要能算出来
    assert head["centers"]["RUSSIA"] == 4 and head["centers"]["ITALY"] == 3


def test_replay_messages_stay_inside_the_seat(srv):
    """FRANCE→AUSTRIA 的私信，只有这两个座位能在复盘里看到。"""
    client, main = srv
    tokens = start(client)

    def send(a, _phase):
        a.act("FRANCE", dict(action="send", to="AUSTRIA", text="春季我不进皮埃蒙特。",
                             request_id="rp-1"))

    settle_one(main, extra=send)

    def item(power):
        d = client.get("/api/replay", headers={"X-Seat-Token": tokens[power]}).json()
        return d["items"][0]["messages"]

    assert "春季我不进皮埃蒙特" in item("FRANCE")   # 发件人看得到自己发的
    assert "春季我不进皮埃蒙特" in item("AUSTRIA")  # 收件人看得到
    assert "春季我不进皮埃蒙特" not in item("GERMANY")


def test_replay_survives_a_finished_game(srv):
    """整局打完后复盘仍要能取，且条数与结算次数一致。"""
    client, main = srv
    tokens = start(client)
    for _ in range(200):
        a = main.require_arena()
        if a.data["stage"] == "done":
            break
        settle_one(main)

    d = client.get("/api/replay", headers={"X-Seat-Token": tokens["TURKEY"]}).json()
    a = main.require_arena()
    assert a.data["stage"] == "done", "全员驻守应当打到 1920 年终局"
    assert d["settled"] == len(a.data["history"]) > 0
    assert d["result_text"], "终局要有结果文案"
    assert d["items"][-1]["index"] == d["settled"]
    assert all(it["text"] for it in d["items"]), "每个已结算阶段都要有文字"

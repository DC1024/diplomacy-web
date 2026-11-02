"""B0 冒烟测试：建局、私信隔离、命令校验、全员交令后推进阶段。

这些用例覆盖了网页版最容易出错的三件事：令牌鉴权、私信保密、结算门控。
"""
import importlib
import json

import pytest
from diplomacy import Game
from fastapi.testclient import TestClient


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, tmp_path


def start(client):
    r = client.post("/api/match")
    assert r.status_code == 200, r.text
    return r.json()


def test_healthz(srv):
    client, _ = srv
    body = client.get("/healthz").json()
    assert body["ok"] is True


def test_new_match_has_seven_tokens(srv):
    client, _ = srv
    d = start(client)
    assert len(d["tokens"]) == 7
    assert d["phase"] == "S1901M"
    assert d["summary"]["centers"]["RUSSIA"] == 4


def test_rejects_missing_and_bad_token(srv):
    client, _ = srv
    start(client)
    assert client.get("/api/brief").status_code == 401
    assert client.get("/api/brief", headers={"X-Seat-Token": "nope"}).status_code == 403


def test_private_message_isolation(srv):
    client, _ = srv
    tokens = start(client)["tokens"]
    r = client.post("/api/send", json={"to": "FRANCE", "text": "只给法国看的一句话"},
                    headers={"X-Seat-Token": tokens["ITALY"]})
    assert r.status_code == 200, r.text

    seen = client.get("/api/inbox", headers={"X-Seat-Token": tokens["FRANCE"]}).json()["text"]
    assert "只给法国看的一句话" in seen

    blind = client.get("/api/inbox", headers={"X-Seat-Token": tokens["GERMANY"]}).json()["text"]
    assert "只给法国看的一句话" not in blind

    brief = client.get("/api/brief", headers={"X-Seat-Token": tokens["GERMANY"]}).json()["text"]
    assert "只给法国看的一句话" not in brief


def test_public_message_visible_to_all(srv):
    client, _ = srv
    tokens = start(client)["tokens"]
    client.post("/api/send", json={"to": "GLOBAL", "text": "公开声明"},
                headers={"X-Seat-Token": tokens["RUSSIA"]})
    for power in ("FRANCE", "TURKEY"):
        text = client.get("/api/inbox", headers={"X-Seat-Token": tokens[power]}).json()["text"]
        assert "公开声明" in text


def test_message_length_limit(srv):
    client, _ = srv
    tokens = start(client)["tokens"]
    r = client.post("/api/send", json={"to": "FRANCE", "text": "字" * 301},
                    headers={"X-Seat-Token": tokens["ITALY"]})
    assert r.status_code == 400
    assert "300" in r.json()["detail"]


def test_illegal_order_returns_hint(srv):
    client, _ = srv
    tokens = start(client)["tokens"]
    r = client.post("/api/orders", json={"orders": ["A PAR - BUR"]},
                    headers={"X-Seat-Token": tokens["ITALY"]})
    assert r.status_code == 400
    assert "不合法" in r.json()["detail"]


def test_full_phase_advances_after_all_orders(srv):
    client, tmp = srv
    tokens = start(client)["tokens"]
    state = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
    game = Game.from_dict(state["game"])
    possible = game.get_all_possible_orders()

    for power, token in tokens.items():
        orders = []
        for loc in game.get_orderable_locations(power):
            holds = sorted(o for o in possible[loc] if o.endswith(" H"))
            if holds:
                orders.append(holds[0])
        r = client.post("/api/orders", json={"orders": orders},
                        headers={"X-Seat-Token": token})
        assert r.status_code == 200, f"{power}: {r.text}"

    st = client.get("/api/state").json()
    assert st["phase"] == "F1901M", st
    assert all(v["ready"] is False for v in st["players"].values())


def test_reset_requires_confirm(srv):
    client, _ = srv
    start(client)
    assert client.post("/api/reset", json={}).status_code == 400
    d = client.post("/api/reset", json={"confirm": True}).json()
    assert len(d["tokens"]) == 7
    assert client.get("/api/state").json()["phase"] == "S1901M"

"""管理面鉴权：设置了 ADMIN_KEY 时，建局与清档必须先过密钥。

背景：`/api/match` 会一次性返回全部 7 个座位令牌，等于交出全场的私信。
未设 ADMIN_KEY 时它对本机单人模式是便利，对公网就是信息泄露，所以这两条路径必须有测试守着。
"""
import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def make_server(tmp_path, monkeypatch, admin_key, host="0.0.0.0"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOST", host)
    if admin_key is None:
        monkeypatch.delenv("ADMIN_KEY", raising=False)
    else:
        monkeypatch.setenv("ADMIN_KEY", admin_key)
    import app.main as main

    importlib.reload(main)
    return TestClient(main.app)


def test_loopback_without_key_stays_open(tmp_path, monkeypatch):
    """本机单人：没密钥也能建局，这是开发体验的底线。"""
    with make_server(tmp_path, monkeypatch, None, host="127.0.0.1") as client:
        assert client.get("/healthz").json()["admin_required"] is False
        r = client.post("/api/match")
        assert r.status_code == 200
        assert len(r.json()["tokens"]) == 7


def test_exposed_without_key_is_locked(tmp_path, monkeypatch):
    """对外监听又没密钥：管理面全锁。

    以前这里是「留空一律放行」，等于把本机的方便原样搬到公网——任何能打开页面的人
    都能一次拿到 7 个座位令牌（读完七国私信）并清空存档。现在宁可 403。
    """
    with make_server(tmp_path, monkeypatch, None) as client:      # host 默认 0.0.0.0
        h = client.get("/healthz").json()
        assert h["exposed"] is True
        assert h["admin_locked"] is True
        r = client.post("/api/match")
        assert r.status_code == 403
        assert "ADMIN_KEY" in r.json()["detail"]


def test_locking_admin_still_lets_people_watch(tmp_path, monkeypatch):
    """锁的是管理面，不是整个服务：局面是公开信息，旁观者该能看。"""
    with make_server(tmp_path, monkeypatch, "s3cret") as client:
        ok = {"X-Admin-Key": "s3cret"}
        assert client.post("/api/match", headers=ok).status_code == 200
    # 换成一个「没密钥 + 对外」的实例，读同一个 DATA_DIR
    with make_server(tmp_path, monkeypatch, None) as client:
        assert client.get("/api/state").status_code == 200
        board = client.get("/api/board")
        assert board.status_code == 200
        assert board.json()["mine"] == {}          # 观众拿不到任何合法命令
        assert client.get("/api/brief").status_code == 401


def test_healthz_reports_admin_required(tmp_path, monkeypatch):
    with make_server(tmp_path, monkeypatch, "s3cret") as client:
        assert client.get("/healthz").json()["admin_required"] is True


@pytest.mark.parametrize("header", [{}, {"X-Admin-Key": "wrong"}])
def test_new_match_rejected_without_valid_key(tmp_path, monkeypatch, header):
    with make_server(tmp_path, monkeypatch, "s3cret") as client:
        r = client.post("/api/match", headers=header)
        assert r.status_code == 403
        assert "管理员密钥" in r.json()["detail"]


def test_new_match_accepts_key_and_yields_tokens(tmp_path, monkeypatch):
    with make_server(tmp_path, monkeypatch, "s3cret") as client:
        r = client.post("/api/match", headers={"X-Admin-Key": "s3cret"})
        assert r.status_code == 200
        assert len(r.json()["tokens"]) == 7


def test_reset_requires_key_even_when_match_exists(tmp_path, monkeypatch):
    with make_server(tmp_path, monkeypatch, "s3cret") as client:
        ok = {"X-Admin-Key": "s3cret"}
        assert client.post("/api/match", headers=ok).status_code == 200
        first = client.post("/api/match", headers=ok).json()["match_id"]

        assert client.post("/api/reset", json={"confirm": True}).status_code == 403
        assert client.post("/api/reset", json={"confirm": True},
                           headers={"X-Admin-Key": "nope"}).status_code == 403

        r = client.post("/api/reset", json={"confirm": True}, headers=ok)
        assert r.status_code == 200
        assert r.json()["match_id"] != first


def test_reset_still_needs_confirm(tmp_path, monkeypatch):
    """密钥不能替代 confirm，两个条件都要满足。"""
    with make_server(tmp_path, monkeypatch, "s3cret") as client:
        r = client.post("/api/reset", json={"confirm": False},
                        headers={"X-Admin-Key": "s3cret"})
        assert r.status_code == 400


def test_seat_endpoints_stay_open_with_admin_key(tmp_path, monkeypatch):
    """座位接口不应被管理密钥牵连：拿到座位令牌的人不需要密钥。"""
    with make_server(tmp_path, monkeypatch, "s3cret") as client:
        tokens = client.post("/api/match", headers={"X-Admin-Key": "s3cret"}).json()["tokens"]
        r = client.get("/api/me", headers={"X-Seat-Token": tokens["FRANCE"]})
        assert r.status_code == 200
        assert r.json()["power"] == "FRANCE"


def risky(h: dict) -> bool:
    """前端红条出现的条件，与 index.html 的 renderWarn 保持一致。"""
    return h["admin_required"] is False and h["exposed"] is True


@pytest.mark.parametrize("host,exposed", [
    ("0.0.0.0", True),      # 容器默认值：整个局域网可达
    ("::", True),
    ("192.168.43.18", True),
    ("127.0.0.1", False),   # 只本机能连
    ("localhost", False),
])
def test_healthz_reports_listen_scope(tmp_path, monkeypatch, host, exposed):
    with make_server(tmp_path, monkeypatch, None, host=host) as client:
        assert client.get("/healthz").json()["exposed"] is exposed


@pytest.mark.parametrize("admin_key,host,expect", [
    (None, "0.0.0.0", True),     # 无密钥 + 对外监听：真正危险
    ("s3cret", "0.0.0.0", False),  # 有人在门口查密钥
    (None, "127.0.0.1", False),    # 没有门，但只有本机能走到
])
def test_warning_matrix(tmp_path, monkeypatch, admin_key, host, expect):
    with make_server(tmp_path, monkeypatch, admin_key, host=host) as client:
        h = client.get("/healthz").json()
    assert risky(h) is expect


def test_page_wires_the_warning():
    """页面上的红条必须真的接在 /healthz 上。

    这条断言盯着"接线"而不是视觉效果：告警文案怎么改都行，
    但元素被删、或 boot() 不再调用 renderWarn，就等于这个 P0 又回来了。
    """
    html = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text("utf-8")
    assert 'id="warn"' in html
    assert 'id="warnMini"' in html
    assert "function renderWarn(" in html
    assert "renderWarn(h)" in html

"""管理面鉴权：设置了 ADMIN_KEY 时，建局与清档必须先过密钥。

背景：`/api/match` 会一次性返回全部 7 个座位令牌，等于交出全场的私信。
未设 ADMIN_KEY 时它对本机单人模式是便利，对公网就是信息泄露，所以这两条路径必须有测试守着。
"""
import importlib

import pytest
from fastapi.testclient import TestClient


def make_server(tmp_path, monkeypatch, admin_key):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    if admin_key is None:
        monkeypatch.delenv("ADMIN_KEY", raising=False)
    else:
        monkeypatch.setenv("ADMIN_KEY", admin_key)
    import app.main as main

    importlib.reload(main)
    return TestClient(main.app)


def test_open_mode_without_admin_key(tmp_path, monkeypatch):
    with make_server(tmp_path, monkeypatch, None) as client:
        assert client.get("/healthz").json()["admin_required"] is False
        r = client.post("/api/match")
        assert r.status_code == 200
        assert len(r.json()["tokens"]) == 7


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

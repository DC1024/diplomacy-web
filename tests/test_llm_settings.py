"""模型配置的运行时化：网页里存一次就生效，密钥不出后端。

这一层坏掉的代价很不对称：配置读不出来 = 七个模型座位**静默**失效（对局照跑，
只是全在驻守，看上去像"模型不聪明"）；密钥漏出去 = 比失效严重得多。
所以除了功能，重点盯四件事：密钥不回显、非管理员改不了、留空不抹密钥、坏文件不炸服务。
"""
import importlib
import json

import pytest
from fastapi.testclient import TestClient

SECRET = "sk-unit-test-9876543210"


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    for name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        main._runner.stop()  # 用例自己驱动，别让后台线程插一脚
        yield client, main, tmp_path


def put(client, headers=None, **body):
    return client.put("/api/llm/settings", json=body, headers=headers or {})


def test_nothing_saved_falls_back_to_env(tmp_path, monkeypatch):
    """一个变量都没设时，来源必须是 none——显示成 env 会让人去翻 compose 文件。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        main._runner.stop()
        s = client.get("/api/llm/settings").json()["settings"]
        assert s["source"] == "none"
        assert s["configured"] is False
        assert s["base_url"] == ""

        # 设了环境变量就该认它，但仍然没有落盘文件
        monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com/v1")
        monkeypatch.setenv("LLM_MODEL", "env-model")
        s = client.get("/api/llm/settings").json()["settings"]
        assert s["source"] == "env"
        assert s["configured"] is True
        assert s["model"] == "env-model"
        assert not (tmp_path / "llm.json").exists()


def test_saved_settings_win_over_env(srv, monkeypatch):
    """网页存过就以网页为准。两边都能改却不说哪边算数，比只给一边更糟。"""
    client, main, tmp_path = srv
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "env-model")

    r = put(client, mode="openai", base_url="https://api.web.example/v1", model="web-model")
    assert r.status_code == 200
    s = r.json()["settings"]
    assert (s["source"], s["base_url"], s["model"]) == ("file", "https://api.web.example/v1",
                                                        "web-model")
    # 落盘了，且再读一次还是文件里的值
    assert (tmp_path / "llm.json").exists()
    s2 = client.get("/api/llm/settings").json()["settings"]
    assert s2["model"] == "web-model" and s2["source"] == "file"


def test_base_url_is_normalized(srv):
    """手填地址容忍多余后缀：多一个尾斜杠就拼出 `//chat/completions` 的错法很常见。"""
    client, _, _ = srv
    s = put(client, base_url=" https://api.deepseek.com/v1/ ").json()["settings"]
    assert s["base_url"] == "https://api.deepseek.com/v1"
    s = put(client, base_url="https://api.deepseek.com/v1/chat/completions").json()["settings"]
    assert s["base_url"] == "https://api.deepseek.com/v1"


def test_api_key_never_comes_back(srv):
    """密钥只存不取：接口给尾四位，原始值不出现在任何响应体里。"""
    client, _, tmp_path = srv
    put(client, base_url="https://api.x.com/v1", model="m", api_key=SECRET)

    body = client.get("/api/llm/settings").text
    assert SECRET not in body
    assert "api_key" not in json.loads(body)["settings"]
    s = json.loads(body)["settings"]
    assert s["has_key"] is True
    assert s["key_hint"].endswith("3210")
    assert SECRET not in s["key_hint"]

    # 状态接口同样不该带出来
    assert SECRET not in client.get("/api/llm").text
    # 但文件里有——它就是那个「保险箱」，所以存在盘上而不是内存里
    assert SECRET in (tmp_path / "llm.json").read_text(encoding="utf-8")


def test_blank_key_keeps_the_saved_one(srv):
    """留空 = 沿用。若把空串当清空，那么每改一次模型名都会把密钥抹掉。"""
    client, _, tmp_path = srv
    put(client, base_url="https://api.x.com/v1", model="m", api_key=SECRET)
    put(client, model="m2")            # 只改模型名，不带密钥
    assert SECRET in (tmp_path / "llm.json").read_text(encoding="utf-8")
    assert client.get("/api/llm/settings").json()["settings"]["has_key"] is True

    # 真要清空得显式说
    put(client, clear_key=True)
    assert client.get("/api/llm/settings").json()["settings"]["has_key"] is False


def test_missing_fields_are_not_overwritten(srv):
    """没传的字段不能当成空值覆盖——否则改一个预算会把地址和模型名一起清掉。"""
    client, _, _ = srv
    put(client, base_url="https://api.x.com/v1", model="keep-me", budget_cny=7)
    s = put(client, budget_cny=9).json()["settings"]
    assert (s["base_url"], s["model"], s["budget_cny"]) == ("https://api.x.com/v1", "keep-me", 9.0)


def test_only_admin_can_read_or_write(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_KEY", "k")
    for name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        main._runner.stop()
        assert client.get("/api/llm/settings").status_code == 403
        assert put(client, model="x").status_code == 403
        at = {"X-Admin-Key": "k"}
        assert client.get("/api/llm/settings", headers=at).status_code == 200
        assert put(client, headers=at, base_url="https://api.x.com/v1").status_code == 200


def test_bad_values_are_rejected(srv):
    """预算为负、超时为 0 这类输入要在门口挡住，别留到算账时才炸。"""
    client, _, _ = srv
    assert put(client, budget_cny=-1).status_code == 422
    assert put(client, timeout=1).status_code == 422
    assert put(client, keep_hist=-1).status_code == 422


def test_broken_config_file_falls_back_to_env(srv, monkeypatch):
    """手改坏的 JSON 不能让服务起不来，也不能装作「没配置」而不留痕迹。"""
    client, _, tmp_path = srv
    (tmp_path / "llm.json").write_text("{ 这不是 JSON", encoding="utf-8")
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    s = client.get("/api/llm/settings").json()["settings"]
    assert s["source"] == "env" and s["model"] == "env-model"


def test_saved_seats_pick_up_the_new_provider(srv):
    """保存后要有反应：已有座位换 provider，预算调高了还得把停掉的座位放回来。"""
    client, main, _ = srv
    put(client, mode="scripted", budget_cny=1)
    assert client.post("/api/llm/seat", json={"power": "FRANCE"}).status_code == 200
    seat = main._seats["FRANCE"]
    assert seat.budget_cny == 1.0

    # 模拟撞线停机，再把预算调大：不带上回收的话，页面会一直显示「超出预算 ¥1」
    seat.usage.cost_cny = 1.5
    seat._bill(type("R", (), {"prompt_tokens": 0, "completion_tokens": 0})())
    assert seat.enabled is False
    put(client, budget_cny=50)
    assert seat.budget_cny == 50.0
    assert seat.enabled is True and seat.stop_reason == ""

    # 换到真实网关：provider 当场换掉，不必重开座位
    put(client, mode="openai", base_url="https://api.x.com/v1", model="m3")
    assert main._seats["FRANCE"].provider.name == "openai:m3"


def test_probe_reports_instead_of_raising(srv):
    """「测试连接」失败要回一份可读的报告，不能 500——否则页面只能显示「失败了」。"""
    client, _, _ = srv
    r = client.post("/api/llm/settings/test",
                    json={"base_url": "http://127.0.0.1:1/v1", "model": "nope"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert d["error"]                      # 有具体原因，不是空串
    assert d["ms"] >= 0

    # 离线脚本模式下链路必须自证可用
    ok = client.post("/api/llm/settings/test", json={"mode": "scripted"}).json()
    assert ok["ok"] is True and ok["tokens"] > 0


def test_probe_does_not_save(srv):
    """先测后存：测的可以是还没存的那份，测砸了也不能动正在用的那份。"""
    client, _, tmp_path = srv
    put(client, mode="openai", base_url="http://127.0.0.1:1/v1", model="saved-model")
    before = (tmp_path / "llm.json").read_text(encoding="utf-8")

    r = client.post("/api/llm/settings/test", json={"model": "other-model"})
    assert r.json()["ok"] is False
    assert (tmp_path / "llm.json").read_text(encoding="utf-8") == before
    assert client.get("/api/llm/settings").json()["settings"]["model"] == "saved-model"

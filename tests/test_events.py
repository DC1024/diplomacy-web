"""P2：SSE 实时推送。

三个约定必须立住，否则「实时」就是假的：
- 连上先给一帧完整快照，不然新开的页面要等下一次局面变化才有内容；
- 任一 bump（交令、暂停、结算）都要在**不等轮询**的情况下推下来；
- 推送里不能有座位令牌——这条通道不鉴权，漏一次等于公开全部私信。
"""
import importlib
import json
import queue
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_KEY", "k")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        # 这个文件只关心推送本身，把后台座位线程停掉，免得它自己交令把局面搅动
        main._runner.stop()
        yield client, main


def _frame(lines) -> tuple[str, dict]:
    """从 SSE 行流里取下一帧。跳过 `: ping` 这类注释与空行。"""
    name, data = "message", []
    for line in lines:
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif line == "":
            if data:
                return name, json.loads("\n".join(data))
    raise AssertionError("流提前结束了")


def _next(lines, timeout: float = 10.0) -> tuple[str, dict]:
    """带超时地取一帧。

    没有看门狗的话，推送真断了只会把测试挂在那里等 CI 超时，
    报错信息还看不出是哪一步——宁可自己判超时。
    """
    box: queue.Queue = queue.Queue()

    def run() -> None:
        try:
            box.put(_frame(lines))
        except Exception as exc:  # noqa: BLE001
            box.put(exc)

    threading.Thread(target=run, daemon=True).start()
    try:
        got = box.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError(f"等了 {timeout:.0f} 秒没收到下一帧") from None
    if isinstance(got, Exception):
        raise got
    return got


def test_events_sends_snapshot_and_wakes_on_change(srv):
    client, _main = srv
    admin = {"X-Admin-Key": "k"}
    assert client.post("/api/match", headers=admin).status_code == 200

    with client.stream("GET", "/api/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = r.iter_lines()

        name, snap = _next(lines)
        assert name == "state", "连上第一帧就该是完整快照，不能先让客户端干等"
        assert snap["phase"] == "S1901M" and snap["paused"] is False
        assert snap["revision"] >= 1

        # 暂停会 bump：不靠轮询也要立刻收到
        assert client.post("/api/admin/control", json={"action": "pause"},
                           headers=admin).status_code == 200
        name, pushed = _next(lines)
        assert name == "state"
        assert pushed["paused"] is True, pushed
        assert pushed["revision"] > snap["revision"]

        # 继续同理：revision 再动一次
        client.post("/api/admin/control", json={"action": "resume"}, headers=admin)
        name, pushed2 = _next(lines)
        assert pushed2["paused"] is False
        assert pushed2["revision"] > pushed["revision"]


def test_events_never_carry_a_seat_token(srv):
    """推送通道不鉴权，所以它吐出去的每个字节都必须是公开信息。"""
    client, main = srv
    admin = {"X-Admin-Key": "k"}
    client.post("/api/match", headers=admin)
    tokens = list(main.require_arena().data["tokens"].values())
    assert len(tokens) == 7

    with client.stream("GET", "/api/events") as r:
        lines = r.iter_lines()
        _, snap = _next(lines)
        raw = json.dumps(snap, ensure_ascii=False)
        assert "token" not in raw.lower()
        for t in tokens:
            assert t not in raw, "快照里混进了座位令牌"


def test_events_idle_before_any_match(srv):
    """还没开局时不能 404、也不能把连接关掉——前端要靠它等到房主开局。"""
    client, _main = srv
    with client.stream("GET", "/api/events") as r:
        lines = r.iter_lines()
        name, payload = _next(lines)
        assert name == "idle"
        assert payload["match_id"] is None

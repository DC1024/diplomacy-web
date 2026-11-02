"""角色体系：管理员 / 玩家 / 观众 / 旁观。

四档权限的差别不是「能不能进」，而是**看得到什么、动得了什么**：

| | 围观局面 | 合法命令 | 私信 | 观战频道发言 | 管理面 |
|---|---|---|---|---|---|
| 旁观（无凭据） | ✅ | ❌ | ❌ | ❌ | ❌ |
| 观众（观战码） | ✅ | ❌ | ❌ | ✅ | ❌ |
| 玩家（座位令牌） | ✅ | 本国 | 本国 | ❌ | ❌ |
| 管理员 | ✅ | ❌ | ❌ | ✅ | ✅ |

两个刻意的取舍写在用例里：

- **管理员也看不到私信**。管理是运维，不是偷看；真要看「模型为什么乱来」应该看
  模型上下文面板（`/api/llm/context`），那里是模型**自己收到**的内容。
- **合法命令对观众是黑的**。Diplomacy 里命令等于意图宣言，围观不该顺手把它交出去。
  这是观众与玩家真正的分界线，比「能不能点按钮」硬得多。
"""
import importlib

from fastapi.testclient import TestClient

from app.engine.arena import POWERS
from app.llm.orders import hold_orders


def make_server(tmp_path, monkeypatch, admin="k", host="127.0.0.1", gap="0"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOST", host)
    monkeypatch.setenv("PHASE_SECONDS", "0")
    monkeypatch.setenv("SPECTATE_MIN_GAP", gap)        # 用例不等那 1 秒
    if admin is None:
        monkeypatch.delenv("ADMIN_KEY", raising=False)
    else:
        monkeypatch.setenv("ADMIN_KEY", admin)
    import app.main as main

    importlib.reload(main)
    return TestClient(main.app)


def started(tmp_path, monkeypatch, **kw):
    client = make_server(tmp_path, monkeypatch, **kw)
    admin = kw.get("admin", "k")
    r = client.post("/api/match", headers={"X-Admin-Key": admin})
    assert r.status_code == 200, r.text
    return client, r.json()["tokens"], {"X-Admin-Key": admin}


def settle_one(main, before=None):
    """按全员驻守结掉当前阶段。"""
    a = main.require_arena()
    phase = a.game().get_current_phase()
    if before:
        before(a, phase)
    for p in POWERS:
        a.act(p, dict(action="orders", orders=hold_orders(a.game(), p),
                      request_id=f"hold-{phase}-{p}"))
    a.settle(quiet=True)
    return phase


def code_of(client, ok) -> str:
    return client.get("/api/spectate/key", headers=ok).json()["key"]


# ---------------- 玩家 vs 旁观 ----------------

def test_spectator_board_has_no_legal_orders(tmp_path, monkeypatch):
    """观众能看全盘，但拿不到任何一国的合法命令。"""
    client, tokens, _ = started(tmp_path, monkeypatch)
    with client:
        anon = client.get("/api/board").json()
        assert len(anon["units"]) == 22 and len(anon["centers"]) == 22
        assert anon["mine"] == {} and anon["editable"] is False

        mine = client.get("/api/board", headers={"X-Seat-Token": tokens["ITALY"]}).json()
        assert mine["power"] == "ITALY"
        assert mine["mine"] and mine["editable"] is True
        # 只给本国的：别国的部队位置不该出现在 mine 里
        assert all(k not in mine["mine"] for k in ("PAR", "BRE", "MUN"))


def test_brief_and_inbox_still_need_a_seat(tmp_path, monkeypatch):
    client, _, _ = started(tmp_path, monkeypatch)
    with client:
        assert client.get("/api/brief").status_code == 401
        assert client.get("/api/inbox").status_code == 401
        assert client.get("/api/legal").status_code == 401
        assert client.post("/api/orders", json={"orders": ["A PAR H"]}).status_code == 401


def test_bad_seat_token_does_not_silently_downgrade(tmp_path, monkeypatch):
    """令牌给了但不对，必须报错。

    若悄悄降级成观众，玩家会看到一个「能看不能交令」的页面，还以为是前端坏了——
    座位链接轮换之后这会是常态，报错才查得出来。
    """
    client, _, _ = started(tmp_path, monkeypatch)
    with client:
        r = client.get("/api/board", headers={"X-Seat-Token": "nope"})
        assert r.status_code == 403


def test_history_is_public_but_loses_the_you_tag(tmp_path, monkeypatch):
    client, tokens, _ = started(tmp_path, monkeypatch)
    with client:
        main = __import__("app.main", fromlist=["x"])
        settle_one(main)
        anon = client.get("/api/history").json()["text"]
        mine = client.get("/api/history", headers={"X-Seat-Token": tokens["ITALY"]}).json()["text"]
        assert "结算" in anon
        assert "（你）" not in anon          # 观众没有「你」
        assert "（你）" in mine


def test_replay_hides_messages_from_spectators(tmp_path, monkeypatch):
    """复盘是最容易漏私信的地方：观众拿到的 messages 必须是空字符串。"""
    client, tokens, _ = started(tmp_path, monkeypatch)
    with client:
        main = __import__("app.main", fromlist=["x"])

        def send_letter(a, phase):
            a.act("FRANCE", dict(action="send", to="ITALY", text="我春季不进勃艮第",
                                 request_id="letter-1"))

        settle_one(main, before=send_letter)

        anon = client.get("/api/replay").json()
        assert anon["items"], "应该有结算记录"
        assert anon["items"][0]["messages"] == ""
        assert "勃艮第" not in str(anon)

        mine = client.get("/api/replay", headers={"X-Seat-Token": tokens["ITALY"]}).json()
        assert "勃艮第" in mine["items"][0]["messages"]


# ---------------- 观战频道 ----------------

def test_anyone_can_read_the_channel(tmp_path, monkeypatch):
    client, _, ok = started(tmp_path, monkeypatch)
    with client:
        code = code_of(client, ok)
        client.post("/api/spectate", json=dict(name="阿观", text="德国这波可以"),
                    headers={"X-Spectate-Key": code})
        d = client.get("/api/spectate").json()
        assert [m["text"] for m in d["messages"]] == ["德国这波可以"]
        assert d["can_say"] is False           # 没带码，只能看


def test_speaking_needs_the_code(tmp_path, monkeypatch):
    client, _, ok = started(tmp_path, monkeypatch)
    with client:
        body = dict(name="路人", text="我插一句")
        assert client.post("/api/spectate", json=body).status_code == 403

        code = code_of(client, ok)
        assert client.post("/api/spectate", json=body,
                           headers={"X-Spectate-Key": "wrong"}).status_code == 403
        assert client.post("/api/spectate", json=body,
                           headers={"X-Spectate-Key": code}).status_code == 200


def test_a_seat_token_cannot_speak_in_the_channel(tmp_path, monkeypatch):
    """玩家有自己的 GLOBAL 频道；观战频道属于观众席，别把两种身份混在一起。"""
    client, tokens, _ = started(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/spectate", json=dict(name="", text="我是法国"),
                        headers={"X-Seat-Token": tokens["FRANCE"]})
        assert r.status_code == 403


def test_the_code_is_admin_only_and_rotatable(tmp_path, monkeypatch):
    client, _, ok = started(tmp_path, monkeypatch)
    with client:
        assert client.get("/api/spectate/key").status_code == 403
        old = code_of(client, ok)
        new = client.post("/api/spectate/rotate", headers=ok).json()["key"]
        assert new and new != old
        # 旧链接立刻失效——码被转发到公开渠道时这是唯一的止血手段
        assert client.post("/api/spectate", json=dict(text="旧码"),
                           headers={"X-Spectate-Key": old}).status_code == 403
        assert client.post("/api/spectate", json=dict(text="新码"),
                           headers={"X-Spectate-Key": new}).status_code == 200


def test_empty_oversize_and_rapid_messages_are_rejected(tmp_path, monkeypatch):
    client, _, ok = started(tmp_path, monkeypatch, gap="1")
    with client:
        code = code_of(client, ok)
        h = {"X-Spectate-Key": code}
        assert client.post("/api/spectate", json=dict(text="   "), headers=h).status_code == 400
        long = client.post("/api/spectate", json=dict(text="啊" * 300), headers=h)
        assert long.status_code == 422 or long.status_code == 400  # 模型层或频道层拦住
        assert client.post("/api/spectate", json=dict(text="第一句"), headers=h).status_code == 200
        assert client.post("/api/spectate", json=dict(text="第二句"), headers=h).status_code == 400


def test_messages_survive_a_restart(tmp_path, monkeypatch):
    client, _, ok = started(tmp_path, monkeypatch)
    with client:
        code = code_of(client, ok)
        client.post("/api/spectate", json=dict(name="阿观", text="留个脚印"),
                    headers={"X-Spectate-Key": code})
    client2 = make_server(tmp_path, monkeypatch)
    with client2:
        assert [m["text"] for m in client2.get("/api/spectate").json()["messages"]] == ["留个脚印"]


def test_new_match_clears_the_channel_but_keeps_the_code(tmp_path, monkeypatch):
    """新局清空留言，但观战码不该作废——否则第二局开始时所有观众要重新要链接。"""
    client, _, ok = started(tmp_path, monkeypatch)
    with client:
        code = code_of(client, ok)
        client.post("/api/spectate", json=dict(text="上一局的痕迹"),
                    headers={"X-Spectate-Key": code})
        client.post("/api/reset", json={"confirm": True}, headers=ok)
        assert client.get("/api/spectate").json()["messages"] == []
        assert code_of(client, ok) == code
        assert client.post("/api/spectate", json=dict(text="新一局"),
                           headers={"X-Spectate-Key": code}).status_code == 200


def test_the_channel_does_not_leak_into_the_game(tmp_path, monkeypatch):
    """观众的话不进模型上下文、不进玩家收件箱——这是「独立频道」的全部意义。"""
    client, tokens, ok = started(tmp_path, monkeypatch)
    with client:
        code = code_of(client, ok)
        client.post("/api/spectate", json=dict(text="德国肯定要打勃艮第"),
                    headers={"X-Spectate-Key": code})
        inbox = client.get("/api/inbox", headers={"X-Seat-Token": tokens["GERMANY"]}).json()["text"]
        assert "勃艮第" not in inbox
        state = main_state(client)
        assert "勃艮第" not in str(state)


def main_state(client):
    return client.get("/api/state").json()

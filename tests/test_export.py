"""对局导出：文本战报 + JSON。

这里要钉住的不是排版好不好看，而是**导出是不是第二个漏私信的口子**：

- 别国的信不能出现在我的导出里（和复盘同一条线）；
- 匿名下载必须是「一封都没有」，而不是「有但是空的」；
- 座位令牌给了但不对，要报错而不是悄悄降级成公开版——降级会让玩家以为
  「我的信丢了」，而这其实是他手里那把钥匙作废了。

另外两条小的：文件名不能被 match_id 注入（那玩意儿迟早会被人从别处改），
以及没结算过的新局也能导出（导出按钮不该挑时机）。
"""
import importlib
import json
import re

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
    """全员驻守结掉当前阶段；`extra` 可以在结算前塞点私信进去。"""
    a = main.require_arena()
    phase = a.game().get_current_phase()
    if extra:
        extra(a, phase)
    for p in POWERS:
        a.act(p, dict(action="orders", orders=hold_orders(a.game(), p),
                      request_id=f"hold-{phase}-{p}"))
    a.settle(quiet=True)
    return phase


def test_markdown_covers_every_phase_and_the_board(srv):
    client, main = srv
    tokens = start(client)
    first = settle_one(main)
    second = settle_one(main)

    r = client.get("/api/export", headers={"X-Seat-Token": tokens["FRANCE"]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    md = r.text

    assert f"{first}" in md and f"{second}" in md
    assert "## 逐阶段" in md and "## 截至导出的局面" in md
    # 表格里七国都在，中心数要能对得上（开局各国 3，俄国 4）
    for p in POWERS:
        assert f"| {p} " in md
    assert "2 个阶段" in md
    # 驻守命令本身也得在：战报不看命令，等于只看了个比分
    assert "**RUSSIA**" in md


def test_json_never_carries_the_seat_tokens(srv):
    client, main = srv
    tokens = start(client)
    settle_one(main)

    r = client.get("/api/export?format=json", headers={"X-Seat-Token": tokens["FRANCE"]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    d = json.loads(r.text)

    assert d["schema"].startswith("diplomacy-web/export/")
    assert d["viewer"] == "FRANCE"
    assert d["settled"] == 1 and len(d["history"]) == 1
    assert set(d["board"]) == set(POWERS)

    # 存档里最要命的字段：任何一把令牌出现在导出里，这条链接就等于把七国私信一起送人
    assert "tokens" not in d
    for tok in tokens.values():
        assert tok not in r.text


def test_letters_stay_inside_the_seat(srv):
    client, main = srv
    tokens = start(client)

    def send(a, _phase):
        a.act("FRANCE", dict(action="send", to="AUSTRIA", text="春季我不进皮埃蒙特。",
                             request_id="ex-1"))

    settle_one(main, extra=send)

    mine = client.get("/api/export", headers={"X-Seat-Token": tokens["FRANCE"]}).text
    theirs = client.get("/api/export", headers={"X-Seat-Token": tokens["AUSTRIA"]}).text
    other = client.get("/api/export", headers={"X-Seat-Token": tokens["GERMANY"]}).text

    assert "春季我不进皮埃蒙特" in mine          # 发件人
    assert "春季我不进皮埃蒙特" in theirs        # 收件人
    assert "春季我不进皮埃蒙特" not in other     # 第三国：一个字都不该有
    assert "## 本座位往来信件" in mine
    # 德国那份里连「谁跟谁通过信」这种结构都不该出现
    assert "FRANCE → AUSTRIA" not in other


def test_anonymous_export_has_no_letters_at_all(srv):
    """没有座位令牌 = 公开战报：连「有信」这件事都不该露出来。"""
    client, main = srv
    start(client)

    def send(a, _phase):
        a.act("FRANCE", dict(action="send", to="GERMANY", text="别动比利时。", request_id="ex-2"))

    settle_one(main, extra=send)

    md = client.get("/api/export").text
    assert "别动比利时" not in md
    assert "本座位往来信件" not in md

    d = client.get("/api/export?format=json").json()
    assert d["viewer"] == "" and d["messages"] == []


def test_a_bad_token_is_an_error_not_a_downgrade(srv):
    """令牌给了但不对 → 403。悄悄换成公开版会让玩家以为自己的信丢了。"""
    client, _main = srv
    start(client)
    r = client.get("/api/export", headers={"X-Seat-Token": "not-a-real-token"})
    assert r.status_code == 403


def test_export_before_the_first_settlement(srv):
    client, _main = srv
    start(client)
    md = client.get("/api/export").text
    assert "还没有结算记录" in md
    d = client.get("/api/export?format=json").json()
    assert d["settled"] == 0 and d["history"] == []
    assert d["phase"] == "S1901M"


def test_filename_cannot_be_injected_through_the_match_id(srv):
    """match_id 换个来路（导入存档、手改 json）就可能带引号与换行，不能原样进响应头。"""
    client, main = srv
    start(client)
    main.require_arena().data["match_id"] = 'a"b\r\nX-Evil: 1'

    r = client.get("/api/export")
    assert r.status_code == 200
    disposition = r.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert disposition.count('"') == 2          # 只有包住文件名的那一对
    # 真正要钉的是「换行与引号被换掉」：`X-Evil: 1` 变成普通文本留在文件名里无害，
    # 留着冒号换行才会被中间层当成另一个响应头。
    name = disposition.split('filename="', 1)[1].rstrip('"')
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name), name


def test_format_aliases(srv):
    client, _main = srv
    start(client)
    assert client.get("/api/export?format=text").headers["content-type"].startswith("text/markdown")
    assert client.get("/api/export?format=markdown").status_code == 200
    assert client.get("/api/export?format=docx").status_code == 422


def test_export_after_the_final_result(srv):
    """整局打完后导出：表头要写结果而不是「进行中」，55 个阶段一条不少。"""
    client, main = srv
    start(client)
    for _ in range(200):
        if main.require_arena().data["stage"] == "done":
            break
        settle_one(main)

    d = client.get("/api/export?format=json").json()
    assert d["stage"] == "done" and d["result_text"]
    assert d["settled"] == len(main.require_arena().data["history"]) > 0

    md = client.get("/api/export").text
    head = md.split("## 截至导出的局面")[0]
    assert "进行中" not in head and d["result_text"] in head

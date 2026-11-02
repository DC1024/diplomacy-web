"""P2：地图可视化（服务端那一半）。

几何是解析引擎自带 SVG 得来的，所以这里最该盯的是**代码对不齐**：图上少一块省份、或者
五片海用了 jDip 的缩写（GOL/MID/NAT/NRG/TYN）而没映射到引擎代码（LYO/MAO/NAO/NWG/TYS），
前端就只会画出一张有洞的图，而且没有任何报错。第二条盯的是反向风险：地图页拼出来的命令
必须是引擎认可的合法串——点一下生成的 `F STP/SC - BOT` 提交不进去，比不能点更糟。
"""
import importlib

import pytest
from diplomacy import Map
from fastapi.testclient import TestClient

from app.engine.arena import POWERS
from app.map_data import geometry


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


def seats(client) -> dict:
    """建局拿七条座位令牌。"""
    return client.post("/api/match").json()["tokens"]


def test_geometry_covers_every_area_the_engine_knows():
    """图上 76 块必须与引擎的 76 个基础地点一一对应，且每块都有轮廓和锚点。"""
    g = geometry()
    map_ = Map("standard")
    engine = {loc.split("/")[0].upper() for loc in map_.locs}

    assert g["missing"] == [], f"引擎有、图上没有：{g['missing']}"
    assert set(g["areas"]) == engine, f"多出来：{sorted(set(g['areas']) - engine)}"
    assert len(g["areas"]) == 76
    # viewBox 是 [x, y, w, h]：按内容包围盒取景，不是文件里声明的 0 0 1835 1360
    vx, vy, vw, vh = g["viewBox"]
    assert vw > 1700 and vh > 1200, f"取景框太小：{g['viewBox']}"
    for code, area in g["areas"].items():
        assert area["d"].startswith("M "), f"{code} 的轮廓不像路径"
        assert area["at"] and len(area["at"]) == 2, f"{code} 没有棋子锚点"
        # 锚点必须落在取景框内：有八个标签用 translate() 定位而把 x/y 写成 0，
        # 只读 x/y 会让 POR、SKA、ROM 一起堆到原点去——这条断言就是防这个。
        x, y = area["at"]
        assert vx <= x <= vx + vw and vy <= y <= vy + vh, f"{code} 的锚点跑到取景框外：{area['at']}"
        assert area["kind"] in ("land", "coast", "water")
    # 34 个补给中心是标准图的硬事实，画错一个，胜负线就会显示歪。
    assert sum(1 for a in g["areas"].values() if a["sc"]) == 34


def test_geometry_aliases_the_seas_named_differently_upstream():
    """五片海在图上叫 GOL/MID/NAT/NRG/TYN，引擎叫 LYO/MAO/NAO/NWG/TYS。"""
    areas = geometry()["areas"]
    for engine_code in ("LYO", "MAO", "NAO", "NWG", "TYS"):
        assert engine_code in areas, f"{engine_code} 没有映射过来"
        assert areas[engine_code]["kind"] == "water"
    for upstream in ("GOL", "MID", "NAT", "NRG", "TYN"):
        assert upstream not in areas, f"{upstream} 是图上旧名，不该留在结果里"


def test_map_endpoint_is_anonymous_and_cacheable(srv):
    """底图不需要令牌（它不含对局信息），但要能被浏览器缓存一天。"""
    client, _ = srv
    r = client.get("/api/map")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=86400"
    assert len(r.json()["areas"]) == 76


def test_board_shows_every_unit_but_only_your_own_orders(srv):
    """部队全公开，命令只给自己的——`mine` 里出现别国地盘就是越界。"""
    client, _ = srv
    tokens = seats(client)
    mine = client.get("/api/board", headers={"X-Seat-Token": tokens["FRANCE"]}).json()
    other = client.get("/api/board", headers={"X-Seat-Token": tokens["ITALY"]}).json()

    assert mine["power"] == "FRANCE" and mine["editable"] is True
    assert len(mine["units"]) == 22, "开局全图 22 支部队"
    assert mine["units"] == other["units"], "部队是公开信息，两个座位看到的必须一样"
    assert {u["power"] for u in mine["units"]} == set(POWERS)
    assert mine["centers"]["PAR"] == "FRANCE" and mine["centers"]["ROM"] == "ITALY"

    assert set(mine["mine"]) == {"BRE", "MAR", "PAR"}
    assert set(other["mine"]) == {"ROM", "VEN", "NAP"}
    assert not set(mine["mine"]) & set(other["mine"]), "两个人的可下令地点不许重叠"


def test_orders_picked_off_the_board_are_accepted_by_the_engine(srv):
    """地图页给的串必须能直接提交：拿每个地点的第一条合法令，七国交齐后应当结算。"""
    client, main = srv
    tokens = seats(client)
    a = main.require_arena()
    phase = a.game().get_current_phase()

    for power, token in tokens.items():
        board = client.get("/api/board", headers={"X-Seat-Token": token}).json()
        # 注意两个口径：`mine` 的键是**基础地名**（`STP`），命令串里才带海岸（`F STP/SC - BOT`）。
        # 引擎的 `get_orderable_locations()` 就是这么给的，而地图上的形状也只到基础省份这一层。
        own = {u["loc"].split("/")[0] for u in board["units"] if u["power"] == power}
        assert set(board["mine"]) == own, f"{power} 的可下令地点与部队所在地不一致"
        orders = [options[0] for options in board["mine"].values()]
        r = client.post("/api/orders", headers={"X-Seat-Token": token},
                        json=dict(orders=orders, request_id=f"board-{power}-{phase}"))
        assert r.status_code == 200, f"{power} 的合法命令被拒了：{r.text}"

    assert a.game().get_current_phase() != phase, "七国都交齐了，应当已经结算"

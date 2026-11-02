"""整局回归：从 1901 春一路打到终局。

这是 B0 唯一没法靠单阶段用例覆盖的地方——长局里存档体积会增长、阶段类型会在
移动/撤退/调整之间切换、终局判定（18 中心胜利或 1920 年到期）只在最后一刻触发。
命令由 `get_orderable_locations()` + `get_all_possible_orders()` 生成，随机种子固定，
所以每局走势可复现。
"""
import importlib
import random

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main, tmp_path


def pick_orders(main, power: str) -> list[str]:
    """给该国挑一份合法命令。

    移动/撤退阶段：每支需要行动的部队一条；
    调整阶段：可建造数 > 0 时只建这么多，< 0 时只裁这么多（超额提交会被裁判拒绝）。
    """
    game = main.require_arena().game()
    phase = game.get_current_phase()
    possible = game.get_all_possible_orders()
    locs = game.get_orderable_locations(power)
    rnd = random.Random(f"{phase}:{power}")

    if phase.endswith("A"):
        count = game.get_state().get("builds", {}).get(power, {}).get("count", 0)

        def take(suffix: str, n: int) -> list[str]:
            # 圣彼得堡这类双海岸地点会有 STP/NC、STP/SC 两条，按地点去重
            seen, cand = set(), []
            for loc in locs:
                for order in sorted(possible.get(loc, [])):
                    parts = order.split()
                    if len(parts) < 2 or not order.endswith(suffix):
                        continue
                    base = parts[1].split("/")[0]
                    if base not in seen:
                        seen.add(base)
                        cand.append(order)
            return sorted(rnd.sample(cand, min(n, len(cand)))) if cand else []

        if count > 0:
            return take(" B", count)
        if count < 0:
            return take(" D", abs(count))
        return []

    orders = []
    for loc in locs:
        opts = sorted(possible.get(loc, []))
        if opts:
            orders.append(rnd.choice(opts))
    return orders


def test_full_game_reaches_terminal_state(srv):
    client, main, data_dir = srv
    tokens = client.post("/api/match").json()["tokens"]
    headers = {p: {"X-Seat-Token": t} for p, t in tokens.items()}

    phases = 0
    while True:
        state = client.get("/api/state").json()
        if state["game_over"]:
            break

        need = [p for p, s in state["players"].items() if s["required"] and not s["ready"]]
        assert need, f"{state['phase']} 没有任何国家需要行动，却也没结束"

        for power in need:
            r = client.post("/api/orders", json={"orders": pick_orders(main, power)},
                            headers=headers[power])
            assert r.status_code == 200, f"{state['phase']} {power}: {r.text}"

        phases += 1
        assert phases < 120, "阶段数异常膨胀，可能存在死循环"

        after = client.get("/api/state").json()
        assert after["phase"] != state["phase"] or after["game_over"], \
            f"{state['phase']} 全员交令后没有推进"

    final = client.get("/api/state").json()
    result = final["result"]

    # 一局完整对局在 1901–1920 之间，至少 40 个阶段
    assert phases >= 40, f"只跑了 {phases} 个阶段"
    assert final["phase"] == "COMPLETED"
    assert result["type"] in ("solo", "draw")
    assert result["reason"] in ("victory", "turn_limit", "agreed_draw")

    # SoS 计分：无人单独胜利时，全场得分归一化到 100
    if result["type"] == "draw":
        assert abs(sum(result["scores"].values()) - 100) < 0.5
    # 部队数与中心数应当对得上（裁军/增兵逻辑没漏）
    for power in tokens:
        assert final["centers"][power] == len(result["centers"][power])

    # 存档确实落盘，且大小在合理区间（早期 4 KB，整局几百 KB）
    raw = (data_dir / "state.json")
    assert raw.exists() and raw.stat().st_size > 100_000


def test_state_survives_restart(srv):
    """打完一局后重启进程：局面、令牌、结果都要还在（刷新不掉局）。"""
    client, main, _ = srv
    tokens = client.post("/api/match").json()["tokens"]
    headers = {p: {"X-Seat-Token": t} for p, t in tokens.items()}

    # 只推进几个阶段，够验证"中途重启"即可
    for _ in range(3):
        state = client.get("/api/state").json()
        for power in [p for p, s in state["players"].items() if s["required"] and not s["ready"]]:
            client.post("/api/orders", json={"orders": pick_orders(main, power)},
                        headers=headers[power])
    before = client.get("/api/state").json()

    importlib.reload(main)  # 模拟进程重启：内存单例清空，只能从磁盘恢复
    with TestClient(main.app) as client2:
        after = client2.get("/api/state").json()
        assert after["match_id"] == before["match_id"]
        assert after["phase"] == before["phase"]
        assert after["centers"] == before["centers"]

        # 令牌仍然有效，旧浏览器不用重新开局
        me = client2.get("/api/me", headers={"X-Seat-Token": tokens["TURKEY"]})
        assert me.json()["power"] == "TURKEY"
        brief = client2.get("/api/brief", headers={"X-Seat-Token": tokens["TURKEY"]})
        assert brief.status_code == 200 and len(brief.json()["text"]) > 500

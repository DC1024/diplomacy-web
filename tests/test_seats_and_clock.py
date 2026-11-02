"""B2：按座位分发链接 + 阶段时钟 + 管理面（暂停/强制结算）。

三条都是「多人真打」的前提：
- 没有座位链接，热座模式下每个人都握着全场令牌，私信隔离等于没有；
- 没有时钟，一个人挂机就能冻住全桌；
- 没有强制结算，房主只能干等。
"""
import importlib
import sys
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import persist
from app.clock import PhaseClock
from app.engine.arena import POWERS
from app.llm import LLMSeat, ScriptedProvider


def make_server(tmp_path, monkeypatch, admin_key=None, phase_seconds="0"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PHASE_SECONDS", phase_seconds)
    if admin_key is None:
        monkeypatch.delenv("ADMIN_KEY", raising=False)
    else:
        monkeypatch.setenv("ADMIN_KEY", admin_key)
    import app.main as main

    importlib.reload(main)
    return TestClient(main.app)


def started(tmp_path, monkeypatch, admin_key="k", phase_seconds="0"):
    """建好局的服务器；返回 (client, main, admin headers)。

    这里**不** `__enter__`：TestClient 二次 enter 会把第一次的 lifespan 关掉，后台线程
    随之停摆。要跑 lifespan 的用例自己 `with client:`，只进一次。
    """
    client = make_server(tmp_path, monkeypatch, admin_key, phase_seconds)
    headers = {"X-Admin-Key": admin_key} if admin_key else {}
    assert client.post("/api/match", headers=headers).status_code == 200
    return client, sys.modules["app.main"], headers


# ---------- 座位链接 ----------
def test_links_need_admin_key(tmp_path, monkeypatch):
    client, _, _ = started(tmp_path, monkeypatch, "k")
    with client:
        assert client.post("/api/links").status_code == 403


def test_links_bind_exactly_one_seat_each(tmp_path, monkeypatch):
    client, _, ok = started(tmp_path, monkeypatch, "k")
    with client:
        links = client.post("/api/links", headers=ok).json()["links"]
        assert set(links) == set(POWERS)
        for power, url in links.items():
            q = parse_qs(urlparse(url).query)
            assert q["seat"] == [power], url
            # 链接里的令牌只能认出这一国——这就是"一人一条"的全部意义
            who = client.get("/api/me", headers={"X-Seat-Token": q["t"][0]}).json()["power"]
            assert who == power


def test_links_warn_when_deployment_is_open(tmp_path, monkeypatch):
    """没设密钥时链接没有保密意义，要明说，别让人误以为安全。"""
    client, _, _ = started(tmp_path, monkeypatch, admin_key=None)
    with client:
        d = client.post("/api/links").json()
        assert d["notice"] and "ADMIN_KEY" in d["notice"]


def test_links_need_an_existing_match(tmp_path, monkeypatch):
    client = make_server(tmp_path, monkeypatch, "k")
    with client:
        r = client.post("/api/links", headers={"X-Admin-Key": "k"})
        assert r.status_code == 404


# ---------- 阶段时钟 ----------
class StubArena:
    def __init__(self, phase="S1901M"):
        self.phase, self.paused, self.over = phase, False, False

    def admin_state(self):
        return dict(match_id="m1", revision=1, phase=self.phase, paused=self.paused,
                    game_over=self.over, players={})


def test_background_threads_start_with_the_asgi_app(tmp_path, monkeypatch):
    """后台线程必须挂在 lifespan 上，不能只写在 `main()` 里。

    踩过的坑：`_runner.start()` / `_clock.start()` 原本只在 `python -m app.main` 的
    `main()` 里调用，而 Dockerfile 用 `uvicorn app.main:app` —— 镜像里的模型座位与阶段时钟
    从来没启动过，测试却全绿（测试自己手动调 `/api/llm/tick`，绕开了后台线程）。
    这条用例盯的就是「谁来启动线程」这件事。
    """
    client, main, _ = started(tmp_path, monkeypatch, "k", phase_seconds="120")
    with client:
        assert main._clock._thread is not None, "阶段时钟线程没起来"
        assert main._clock._thread.is_alive(), "阶段时钟线程起来了又退了"
        assert main._runner._thread is not None, "模型座位线程没起来"
        assert main._runner._thread.is_alive(), "模型座位线程起来了又退了"


def test_clock_disabled_returns_none():
    clock = PhaseClock(lambda: StubArena(), lambda a, s: True, seconds=0)
    assert clock.note("m1", "S1901M") is None
    assert clock.tick() is False


def test_clock_fires_once_per_phase():
    arena = StubArena()
    fired = []
    clock = PhaseClock(lambda: arena, lambda a, s: fired.append(s["phase"]) or True, seconds=60)

    # 计时从"第一次看到这个阶段"起算，不是对象创建时刻
    assert clock.tick() is False
    clock._started -= 61  # 等价于"已过 61 秒"，比 sleep 稳
    assert clock.tick() is True
    assert fired == ["S1901M"]

    arena.phase = "F1901M"  # 换阶段就重新计时
    assert clock.tick() is False
    clock._started -= 61
    assert clock.tick() is True
    assert fired == ["S1901M", "F1901M"]


def test_clock_frozen_while_paused():
    arena = StubArena()
    fired = []
    clock = PhaseClock(lambda: arena, lambda a, s: fired.append(1) or True, seconds=60)
    clock.note("m1", "S1901M")
    arena.paused = True
    clock._started -= 61
    assert clock.tick() is False
    assert not fired
    assert clock.note("m1", "S1901M", running=False) is None


def test_clock_retries_when_timeout_changes_nothing():
    """回调没推动局面时，冷却期内不能反复调用——否则每秒刷一条日志。

    `tick()` 的返回值是「局面是否真的动了」，不是「有没有调用回调」，
    所以这里数的是调用次数，而不是看返回值的真假。
    """
    arena = StubArena()
    calls = []
    clock = PhaseClock(lambda: arena, lambda a, s: calls.append(s["phase"]) or False, seconds=60)
    clock.note("m1", "S1901M")
    clock._started -= 61
    assert clock.tick() is False   # 到点了，但回调没能推动局面
    assert calls == ["S1901M"]     # 回调确实被调了一次
    assert clock.tick() is False
    assert calls == ["S1901M"]     # 冷却期内没有第二次调用

    clock._retry_at = 0.0          # 冷却结束后允许再试一次
    clock.tick()
    assert calls == ["S1901M", "S1901M"]


def test_state_exposes_countdown(tmp_path, monkeypatch):
    client, _, _ = started(tmp_path, monkeypatch, "k", phase_seconds="120")
    with client:
        st = client.get("/api/state").json()
        assert st["phase_seconds"] == 120
        assert 0 < st["deadline_left"] <= 120


def test_countdown_hidden_when_clock_off(tmp_path, monkeypatch):
    client, _, _ = started(tmp_path, monkeypatch, "k", phase_seconds="0")
    with client:
        assert client.get("/api/state").json()["deadline_left"] is None


# ---------- 超时托管 / 强制结算 ----------
def test_timeout_hold_submits_holds_and_settles(tmp_path, monkeypatch):
    client, main, _ = started(tmp_path, monkeypatch, "k")
    with client:
        stop_background(main)
        arena = main.arena()
        assert arena.game().get_current_phase() == "S1901M"
        assert main.timeout_hold(arena, arena.admin_state()) is True
        assert arena.game().get_current_phase() == "F1901M"


def test_timeout_hold_is_idempotent_within_the_same_revision(tmp_path, monkeypatch):
    """时钟重复触发不能把局面推两格。

    同一份 admin_state（同 revision）再推一次时，`clock-{revision}-{power}` 这些 request_id
    会被内核当成重复请求直接返回旧结果，没人被标成"已交令"，于是结算不成立。
    """
    client, main, _ = started(tmp_path, monkeypatch, "k")
    with client:
        stop_background(main)
        arena = main.arena()
        st = arena.admin_state()
        assert main.timeout_hold(arena, st) is True
        phase = arena.game().get_current_phase()
        assert main.timeout_hold(arena, st) is False
        assert arena.game().get_current_phase() == phase


def test_forced_settle_endpoint(tmp_path, monkeypatch):
    client, main, ok = started(tmp_path, monkeypatch, "k")
    with client:
        d = client.post("/api/admin/settle", headers=ok).json()
        assert d["settled"] is True
        assert d["state"]["phase"] == "F1901M"


def test_forced_settle_needs_key(tmp_path, monkeypatch):
    client, _, _ = started(tmp_path, monkeypatch, "k")
    with client:
        assert client.post("/api/admin/settle").status_code == 403


@pytest.mark.parametrize("action,expected", [("pause", True), ("resume", False)])
def test_pause_and_resume(tmp_path, monkeypatch, action, expected):
    client, _, ok = started(tmp_path, monkeypatch, "k")
    with client:
        st = client.post("/api/admin/control", json={"action": action}, headers=ok).json()
        assert st["paused"] is expected


def test_control_rejects_unknown_action(tmp_path, monkeypatch):
    client, _, ok = started(tmp_path, monkeypatch, "k")
    with client:
        r = client.post("/api/admin/control", json={"action": "nuke"}, headers=ok)
        assert r.status_code == 400


# ---------- 令牌隔离（"一人一国"能不能真成立） ----------
def seat_tokens(client, ok) -> dict[str, str]:
    links = client.post("/api/links", headers=ok).json()["links"]
    return {p: parse_qs(urlparse(u).query)["t"][0] for p, u in links.items()}


# ---------- 掉线托管：模型座位不能把全桌冻住 ----------
def stop_background(main):
    """停掉 lifespan 起的座位线程：这些用例自己驱动 `tick()` 或直接托管。

    两个驱动者在同一批 (revision, power) 上会互相抢——后台先交令，用例再 tick 就"没人动"，
    断言看起来像功能坏了。线程本身是否启动由 test_background_threads_start_with_the_asgi_app 盯着。
    """
    main._runner.stop()


def model_seat(main, power, script=None) -> LLMSeat:
    s = LLMSeat(power, ScriptedProvider(script or [{"pass": True}]))
    main._seats[power] = s
    return s


def test_runner_force_holds_when_a_seat_raises(tmp_path, monkeypatch):
    """座位抛异常也必须有人交令。

    曾经的写法是「抛异常就记日志、这个回合不再重试」，而 `timeout_hold` 又因为
    「这一国已交给模型」而跳过它——两边都不管，于是没人交令、结算永不成立。
    """
    client, main, _ = started(tmp_path, monkeypatch, "k")
    with client:
        stop_background(main)
        arena = main.arena()

        def boom(_arena):
            raise RuntimeError("provider 半路炸了")

        main._seats["ITALY"] = LLMSeat("ITALY", ScriptedProvider([{"pass": True}]))
        main._seats["ITALY"].act = boom
        main._runner.tick()
        info = arena.admin_state()["players"]["ITALY"]
        assert info["ready"] is True
        # 其余六国还没交令，所以这一回合不该被推进
        assert arena.game().get_current_phase() == "S1901M"


def test_runner_force_holds_when_seat_returns_without_orders(tmp_path, monkeypatch):
    """座位没报错、也没交令（内部提前 return）→ 后台同样接管。"""
    client, main, _ = started(tmp_path, monkeypatch, "k")
    with client:
        stop_background(main)
        arena = main.arena()
        main._seats["ITALY"] = LLMSeat("ITALY", ScriptedProvider([{"pass": True}]))
        main._seats["ITALY"].act = lambda _arena: dict(skipped=True, why="模拟静默返回")
        main._runner.tick()
        assert arena.admin_state()["players"]["ITALY"]["ready"] is True


def test_tick_survives_the_seat_table_changing_mid_flight(tmp_path, monkeypatch):
    """座位表在遍历途中被改，不能让整轮白跑。

    P1 实测时撞到的是 `RuntimeError: dictionary changed size during iteration`：
    网页上勾/取消座位、脚本收回座位，都发生在别的线程里。异常被 `_loop` 吞成一条
    ERROR 日志，表现只是「这一轮什么都没发生」——最难查的那种。
    """
    client, main, _ = started(tmp_path, monkeypatch, "k")
    with client:
        stop_background(main)
        arena = main.arena()
        seat = model_seat(main, "ITALY")

        def act_and_mutate(_arena):
            main._seats["FRANCE"] = LLMSeat("FRANCE", ScriptedProvider([{"pass": True}]))
            main._seats.pop("ITALY", None)
            return dict(skipped=True, why="模拟座位表变动")

        seat.act = act_and_mutate
        model_seat(main, "GERMANY")
        main._runner.tick()   # 不该抛 RuntimeError
        assert arena.admin_state()["players"]["GERMANY"]["ready"] is True


def test_timeout_hold_leaves_a_busy_seat_alone(tmp_path, monkeypatch):
    """模型正在说话时别抢：抢了就是白花一次 token，还把它交的令盖掉。"""
    client, main, _ = started(tmp_path, monkeypatch, "k")
    with client:
        stop_background(main)
        arena = main.arena()
        seat = model_seat(main, "ITALY")
        seat.busy = True
        assert main.timeout_hold(arena, arena.admin_state()) is False
        assert arena.game().get_current_phase() == "S1901M"
        assert arena.admin_state()["players"]["ITALY"]["ready"] is False


def test_timeout_hold_waits_for_a_seat_not_yet_attempted(tmp_path, monkeypatch):
    """后台 2 秒轮询还没轮到它时，不要把它这一回合废掉。"""
    client, main, _ = started(tmp_path, monkeypatch, "k")
    with client:
        stop_background(main)
        arena = main.arena()
        model_seat(main, "ITALY")
        assert main.timeout_hold(arena, arena.admin_state()) is False
        assert arena.game().get_current_phase() == "S1901M"


def test_timeout_hold_takes_over_a_seat_that_gave_up(tmp_path, monkeypatch):
    """试过一手、没在忙、还是没交令 → 时钟必须接管，否则全桌等一个死人。"""
    client, main, _ = started(tmp_path, monkeypatch, "k")
    with client:
        stop_background(main)
        arena = main.arena()
        st = arena.admin_state()
        model_seat(main, "ITALY")
        main._runner._acted.add((st["revision"], "ITALY"))  # 等价于「后台试过了，没成」
        assert main.timeout_hold(arena, st) is True
        assert arena.game().get_current_phase() == "F1901M"


# ---------- 存档串行化 ----------
def test_concurrent_saves_do_not_collide(tmp_path, monkeypatch):
    """两个线程同时保存不能炸。

    内核的 `Arena.save()` 不持锁：写 `state.tmp` 再 `os.replace`。后台座位线程交令后保存、
    阶段时钟托管后保存，两者撞在一起时先完成的那个已经把 tmp 改名走，后一个就
    `FileNotFoundError`。CI 上整局回归正是这样挂的（卡在 F1918M，日志里一串
    `FileNotFoundError: state.tmp -> state.json`）。
    """
    client, main, _ = started(tmp_path, monkeypatch, "k")
    with client:
        arena = main.arena()
        errors: list[Exception] = []
        start = threading.Barrier(4)

        def worker() -> None:
            try:
                start.wait()
                for _ in range(20):
                    persist.save(arena)
            except Exception as exc:  # noqa: BLE001 - 这里就是要抓住任何异常
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        # 存完还能读回来，说明没有把存档写坏
        assert arena.path.exists()


def test_seat_token_cannot_read_another_power(tmp_path, monkeypatch):
    """令牌只能证明「你是谁」，不能靠加个查询参数改看别国。

    这条是座位隔离的地基：/api/brief 的视角只来自 X-Seat-Token。
    哪怕有人在 URL 后头拼 ?power=FRANCE，看到的仍是自己那国。
    """
    client, _, ok = started(tmp_path, monkeypatch, "k")
    with client:
        tok = seat_tokens(client, ok)
        at = {"X-Seat-Token": tok["AUSTRIA"]}
        for url in ("/api/brief?power=FRANCE", "/api/brief?power=FRANCE&as=FRANCE"):
            d = client.get(url, headers=at).json()
            assert d["power"] == "AUSTRIA", url
            assert "AUSTRIA" in d["text"].split("\n")[0], url
        # 收件箱也不能借参数换人
        assert client.get("/api/inbox?power=FRANCE", headers=at).status_code == 200
        assert client.get("/api/me", headers=at).json()["power"] == "AUSTRIA"


def test_token_cannot_touch_admin_surface(tmp_path, monkeypatch):
    """玩家令牌不是管理密钥：不能建局拿别人的令牌、不能发链接、不能改局面。"""
    client, main, ok = started(tmp_path, monkeypatch, "k")
    with client:
        at = {"X-Seat-Token": seat_tokens(client, ok)["AUSTRIA"]}
        phase = main.arena().game().get_current_phase()
        assert client.post("/api/match", headers=at).status_code == 403
        assert client.post("/api/links", headers=at).status_code == 403
        assert client.post("/api/admin/settle", headers=at).status_code == 403
        assert client.post("/api/admin/control", json={"action": "pause"},
                           headers=at).status_code == 403
        assert client.get("/api/llm/context?power=FRANCE", headers=at).status_code == 403
        # 三条都被拒之后，局面必须一点没动
        assert main.arena().game().get_current_phase() == phase
        assert main.arena().admin_state()["paused"] is False

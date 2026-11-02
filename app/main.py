"""B0 版：单局热座网页服务。

设计要点：
- 裁判逻辑完全复用 app/engine/arena.py（来自 meow-ai-arena，AGPL），本文件只做 HTTP 壳。
- 单进程、单局、单 worker：Arena 是模块级单例，uvicorn 不能用多 worker。
- 座位鉴权沿用原设计的 Bearer 令牌，热座模式下前端持有全部 7 个令牌。
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .clock import PhaseClock
from .engine.arena import POWERS, Arena, OrderError
from .llm import LLMSeat, SeatRunner, Usage, from_env
from .llm import build as build_context
from .llm.orders import hold_orders
from .persist import save

# 版本由构建期注入（Dockerfile 的 APP_VERSION 来自 git tag），避免镜像标签与 /healthz 漂移。
# 本地直接跑时没有注入，别硬编码一个具体版本——那会让 /healthz 说谎，用 dev 占位最诚实。
VERSION = os.environ.get("APP_VERSION", "0.0.0-dev")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
STATE_PATH = DATA_DIR / "state.json"
STATIC_DIR = Path(__file__).parent / "static"

# 管理密钥：留空表示"本机单人"模式，建局/清档不鉴权；一旦对外开放必须设置。
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()

# 监听地址：决定"没设密钥"是安全的（只有本机能连）还是危险的（整个局域网/公网都能连）。
# Docker 里恒为 0.0.0.0，所以容器部署不做额外声明时一定按"对外可达"处理。
HOST = os.environ.get("HOST", "0.0.0.0")
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def bound_off_loopback() -> bool:
    """监听地址是否可能被本机之外的机器连上。"""
    return HOST.strip().lower() not in _LOOPBACK

log = logging.getLogger("diplomacy")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "info").upper(),
                    format="%(asctime)s %(levelname)s %(message)s")

_lock = threading.RLock()
_arena: Arena | None = None

# ---- LLM 座位（B1）----
_seats: dict[str, LLMSeat] = {}
_runner = SeatRunner(lambda: _arena, _seats, interval=float(os.environ.get("LLM_INTERVAL", "2")))

DEFAULT_BUDGET = float(os.environ.get("LLM_BUDGET_CNY", "20"))
PRICE_IN = float(os.environ.get("LLM_PRICE_IN", "2"))    # ¥ / 百万 input token
PRICE_OUT = float(os.environ.get("LLM_PRICE_OUT", "8"))  # ¥ / 百万 output token
KEEP_HIST = int(os.environ.get("LLM_KEEP_HIST", "3"))
KEEP_MSGS = int(os.environ.get("LLM_KEEP_MSGS", "30"))

# 阶段时钟：0 = 关闭。多人对局建议 300～900 秒；热座一个人操作七国时开着只会添乱。
PHASE_SECONDS = float(os.environ.get("PHASE_SECONDS", "0"))


def arena() -> Arena | None:
    global _arena
    with _lock:
        if _arena is None and STATE_PATH.exists():
            _arena = Arena(STATE_PATH)
        return _arena


def require_arena() -> Arena:
    a = arena()
    if a is None:
        raise HTTPException(404, "还没有开局，请先新建对局")
    return a


def seat(a: Arena, token: str | None) -> str:
    if not token:
        raise HTTPException(401, "缺少座位令牌")
    try:
        return a.identity(token)
    except PermissionError:
        raise HTTPException(403, "座位令牌无效") from None


def require_admin(key: str | None) -> None:
    """设置了 ADMIN_KEY 才校验；留空时放行（本机单人模式）。"""
    if not ADMIN_KEY:
        return
    if not key or not hmac.compare_digest(key, ADMIN_KEY):
        raise HTTPException(403, "需要管理员密钥：请在页面右上角填写，或传 X-Admin-Key 请求头")


def summary(a: Arena, st: dict | None = None) -> dict:
    st = st if st is not None else a.admin_state()
    game = a.game()
    running = not (st["game_over"] or st["paused"])
    return dict(
        version=VERSION,
        match_id=st["match_id"],
        revision=st["revision"],
        phase=st["phase"],
        paused=st["paused"],
        game_over=st["game_over"],
        result=st["result"],
        result_text=a.result_text(),
        centers={p: len(game.get_centers(p)) for p in POWERS},
        units={p: len(game.get_units(p)) for p in POWERS},
        players={p: dict(active=s["active"], required=s["required"], ready=s["ready"])
                 for p, s in st["players"].items()},
        phase_seconds=PHASE_SECONDS,
        deadline_left=_clock.note(st["match_id"], st["phase"], running=running),
    )


def try_settle(a: Arena) -> bool:
    """所有人都交令了就结算；否则静默返回 False。"""
    try:
        if a.settle(quiet=True):
            save(a)
            return True
    except ValueError:
        pass
    except Exception:
        log.exception("settle 失败")
    return False


def timeout_hold(a: Arena, st: dict) -> bool:
    """阶段到点：把还没交令的活跃座位按驻守处理，再尝试结算。

    三种人不碰：
    - 座位正跟模型说话（`busy`）：它自己最长 60s 就有结论，抢在前面交只会白花一次 token；
    - 座位还没轮到（后台 2s 轮询还没尝试过）：再等一轮，别把这一回合替它废掉；
    - 座位已经试过却没交令、也没在忙：那是后台兜底也失败了，这里必须接管——
      否则这一家永远不交令，结算永远不成立，管理员点「强制结算」也推不动。
    """
    held = []
    with a.lock:
        for power, info in st["players"].items():
            if not info["required"] or info["ready"]:
                continue
            seat = _seats.get(power)
            if seat is not None and seat.enabled:
                if seat.busy or not _runner.attempted(st["revision"], power):
                    continue
            orders = hold_orders(a.game(), power)
            try:
                a.act(power, {"action": "orders", "orders": orders,
                              "request_id": f"clock-{st['revision']}-{power}"})
                held.append(power)
            except Exception as exc:  # 一个国家托管失败不该拖死其余六国
                log.warning("超时托管 %s 失败：%s", power, exc)
        settled = try_settle(a)
    if held or settled:
        log.info("阶段 %s 超时：托管 %s，结算=%s",
                 st["phase"], ",".join(held) or "无", settled)
    return settled


_clock = PhaseClock(lambda: _arena, timeout_hold, seconds=PHASE_SECONDS)


def warn_if_unprotected() -> None:
    """没设管理密钥时按「监听范围」分级告警：对外是事故，本机只是提醒。"""
    if ADMIN_KEY:
        return
    msg = ("未设置 ADMIN_KEY：/api/match 会把 7 个座位令牌发给任何访问者"
           "（等于公开全部私信），任何人也能清档。")
    if bound_off_loopback():
        log.critical("%s 当前监听 %s，本机之外也能连上——页面上会挂红色告警。"
                     "对外部署请设 ADMIN_KEY=<长随机串>；只在本机玩请设 HOST=127.0.0.1。",
                     msg, HOST)
    else:
        log.warning("%s 仅监听 %s，只有本机能连，暂不构成风险。", msg, HOST)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """后台线程挂在 ASGI 生命周期上，而不是 `main()` 里。

    踩过的坑：线程原本只在 `main()`（`python -m app.main`）里启动，而 Dockerfile 用
    `uvicorn app.main:app` —— `main()` 压根不执行，于是**镜像里的模型座位与阶段时钟
    从来没跑起来过**。测试全绿是因为它们自己手动调 `/api/llm/tick`，绕开了后台线程。
    """
    warn_if_unprotected()
    _runner.start()
    _clock.start()
    try:
        yield
    finally:
        _clock.stop()
        _runner.stop()


app = FastAPI(title="七国纵横 · Diplomacy Web", version=VERSION, lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")


class SendReq(BaseModel):
    to: str = Field(..., description="国家代码或 GLOBAL")
    text: str


class OrdersReq(BaseModel):
    orders: list[str] = Field(default_factory=list)


class DrawReq(BaseModel):
    vote: bool = True


class ControlReq(BaseModel):
    action: str = Field(..., description="pause 或 resume")


class ResetReq(BaseModel):
    confirm: bool = False


@app.get("/healthz")
def healthz() -> dict:
    """探活。exposed 让前端自己判断"没密钥"要不要报警。"""
    return dict(ok=True, version=VERSION, has_match=arena() is not None,
                admin_required=bool(ADMIN_KEY), exposed=bound_off_loopback())


@app.post("/api/match")
def new_match(x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """建局或返回现有对局。热座模式：一次返回全部 7 个座位令牌。

    这个接口会吐出全部座位令牌（等于交出所有私信），设置 ADMIN_KEY 后必须带密钥，
    否则任何拿到地址的人都能读全场的私信。
    """
    global _arena
    require_admin(x_admin_key)
    with _lock:
        a = arena()
        if a is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            a = Arena.create(STATE_PATH)
            _arena = a
            log.info("新建对局 %s", a.data["match_id"])
        return dict(match_id=a.data["match_id"], phase=a.game().get_current_phase(),
                    tokens=a.data["tokens"], summary=summary(a))


@app.post("/api/links")
def make_links(request: Request, x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """生成 7 条「只绑定一个座位」的链接，一人一条发下去。

    链接里带着该国令牌，谁拿到就等于拿到该国的私信和交令权——按座位单独发，别群发。
    拿到链接的页面只看得到自己那一国的令牌，切不了国家，也看不到别国的信。
    """
    require_admin(x_admin_key)
    a = require_arena()
    base = str(request.base_url).rstrip("/")
    return dict(
        links={p: f"{base}/?seat={p}&t={a.data['tokens'][p]}" for p in POWERS},
        notice=None if ADMIN_KEY else
        "当前没设 ADMIN_KEY：任何人都能调 /api/match 拿到全部令牌，发链接没有保密意义",
    )


@app.post("/api/reset")
def reset(req: ResetReq, x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """开新局。必须显式确认，避免误清存档。"""
    global _arena
    if not req.confirm:
        raise HTTPException(400, "需要 confirm=true 才会清空当前对局")
    require_admin(x_admin_key)
    with _lock:
        _arena = None
        if STATE_PATH.exists():
            backup = STATE_PATH.with_name(f"state.{a_ts()}.json.bak")
            STATE_PATH.rename(backup)
            log.info("旧存档已备份到 %s", backup.name)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _arena = Arena.create(STATE_PATH)
        _runner.reset()
        _clock.reset()
        # 新局重置预算与用量，但保留哪些座位由模型操作
        for llm_seat in _seats.values():
            llm_seat.usage = Usage()
            llm_seat.enabled = True
            llm_seat.stop_reason = ""
        return dict(match_id=_arena.data["match_id"], tokens=_arena.data["tokens"])


def a_ts() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@app.get("/api/state")
def get_state() -> dict:
    return summary(require_arena())


# 等待切片：`admin_wait()` 被 `bump()` 唤醒是**立刻**的，切片只决定两件闲事——
# ① 客户端断开后这条线程多久能退出（线程要等当前这次 wait 返回才会看到连接没了）；
# ② 管理员换局这类「换了 Arena 对象」的事件最迟多久被发现。取 0.5 秒：
#    代价只是每条连接每秒醒两次，换来关标签页后不留半分钟的僵尸线程。
SSE_SLICE = 0.5
# 空转多少个切片补一行注释心跳（约 16 秒），免得中间设备把安静当断线。
SSE_PING_EVERY = 32


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _event_frames():
    """SSE 帧流：局面一动就推一份公开快照。

    刻意只推 `summary()`——里面没有任何座位令牌。客户端拿它当「该刷新了」的信号，
    简报与私信仍旧走带令牌的 `/api/brief`、`/api/inbox`：推送通道本身不必鉴权，
    也就不会因为一条连接被别人看到而漏掉别国私信。

    等待用内核自带的 `admin_wait()`（Condition + 超时），不是忙轮询：结算完
    `bump()` 会 `notify_all()`，下一次 wait 立刻带着新 revision 返回，所以切片长短
    不影响推送延迟。代价是每条连接占一个线程池线程——单局七人够用，真要上百人
    围观就得换成单一广播器，这条记在 TODO 的已知缺口里。
    """
    last_rev, last_match, idle = None, None, 0
    while True:
        a = arena()
        if a is None:
            # 还没开局：隔一会儿再看，别空转刷日志
            yield _sse("idle", dict(match_id=None))
            time.sleep(1.0)
            continue
        try:
            st = a.admin_wait(-1 if last_rev is None else last_rev, SSE_SLICE)
        except Exception:  # 读档 / 换局瞬间可能暂时不可用，下一轮重试
            yield _sse("idle", dict(match_id=None))
            time.sleep(1.0)
            continue
        if st["revision"] == last_rev and st["match_id"] == last_match:
            idle += 1
            if idle % SSE_PING_EVERY == 0:
                yield ": ping\n\n"  # 局面没动，但别让连接显得死了
            else:
                # 空块：客户端一个字节都收不到，但生成器会从 next() 返回，
                # 于是「连接已断、这条线程被放弃」能立刻被发现。少了这一步，
                # 关标签页 / TestClient 关闭时要等到下一次心跳（16 秒）才回收线程。
                yield ""
            continue
        idle = 0
        last_rev, last_match = st["revision"], st["match_id"]
        try:
            payload = summary(a, st)
        except Exception:  # 一场正在收敛的结算不该把连接打断
            log.exception("推送组装失败，跳过这一帧")
            continue
        yield _sse("state", payload)


@app.get("/api/events")
def events() -> StreamingResponse:
    """实时推送（SSE）：结算、暂停、有人交令都会立刻下发，不用等下一轮轮询。"""
    return StreamingResponse(
        _event_frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 反代（nginx/宝塔）默认会缓冲响应体：不声明这一行，推送会变成攒批下发
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/me")
def me(x_seat_token: Annotated[str | None, Header()] = None) -> dict:
    a = require_arena()
    power = seat(a, x_seat_token)
    return dict(power=power)


@app.get("/api/brief")
def brief(x_seat_token: Annotated[str | None, Header()] = None) -> dict:
    a = require_arena()
    power = seat(a, x_seat_token)
    with a.lock:
        text = a.brief(power)[0]
    return dict(power=power, text=text, phase=a.game().get_current_phase(),
                game_over=a.data["stage"] == "done", result_text=a.result_text())


@app.get("/api/legal")
def legal(x_seat_token: Annotated[str | None, Header()] = None,
          locs: str = Query("", description="逗号分隔的地点，留空表示全部")) -> dict:
    a = require_arena()
    power = seat(a, x_seat_token)
    with a.lock:
        text = a.legal(power, [x for x in locs.split(",") if x])
    return dict(text=text)


@app.get("/api/inbox")
def inbox(x_seat_token: Annotated[str | None, Header()] = None,
          with_: str | None = Query(None, alias="with"),
          phase: str | None = None) -> dict:
    a = require_arena()
    power = seat(a, x_seat_token)
    with a.lock:
        text = a.inbox(power, with_, phase)
    return dict(text=text)


@app.get("/api/history")
def history(x_seat_token: Annotated[str | None, Header()] = None,
            phase: str | None = None) -> dict:
    a = require_arena()
    power = seat(a, x_seat_token)
    with a.lock:
        text = a.history_text(power, phase)
    return dict(text=text)


@app.get("/api/replay")
def replay(x_seat_token: Annotated[str | None, Header()] = None) -> dict:
    """复盘：逐阶段回放，视角永远锁在调用者的座位上。

    私信是最容易在这里漏的东西——`inbox(power, phase=...)` 本身按座位过滤，
    所以一局打完后，任何人拿自己的令牌只能看到「自己收到/发出的那些信」。
    不要为了「看起来完整」去返回全量 messages，那等于把复盘页做成偷看面板。
    """
    a = require_arena()
    power = seat(a, x_seat_token)
    with a.lock:
        history = list(a.data["history"])
        items = []
        for i, rec in enumerate(history, 1):
            phase = rec["phase"]
            centers = rec.get("centers") or {}
            items.append(dict(
                index=i,
                phase=phase,
                time=rec.get("time"),
                agreed_draw=rec.get("event") == "agreed_draw",
                text=a.history_text(power, phase),
                centers={p: len(c) for p, c in centers.items() if c},
                messages=a.inbox(power, phase=phase),
            ))
        current = a.game().get_current_phase()
        stage = a.data["stage"]
        return dict(
            power=power,
            items=items,
            current_phase=current,
            settled=len(items),
            stage=stage,
            result_text=a.result_text() if stage == "done" else "",
        )


@app.post("/api/send")
def send(req: SendReq, x_seat_token: Annotated[str | None, Header()] = None) -> dict:
    return _act(x_seat_token, dict(action="send", to=req.to, text=req.text))


@app.post("/api/orders")
def orders(req: OrdersReq, x_seat_token: Annotated[str | None, Header()] = None) -> dict:
    return _act(x_seat_token, dict(action="orders", orders=req.orders))


@app.post("/api/draw")
def draw(req: DrawReq, x_seat_token: Annotated[str | None, Header()] = None) -> dict:
    return _act(x_seat_token, dict(action="draw" if req.vote else "undraw"))


# ---------------- LLM 座位（B1）----------------

class SeatReq(BaseModel):
    power: str
    enabled: bool = True
    persona: str = Field("", description="给这个座位的人设，影响说话风格")
    budget_cny: float | None = Field(None, description="本局预算，超出后自动转保底驻守")


def provider_or_400():
    provider = from_env()
    if provider is None:
        raise HTTPException(400, "未配置模型：请设置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL"
                                 "（或 LLM_PROVIDER=scripted 做离线演练）")
    return provider


@app.post("/api/admin/control")
def admin_control(req: ControlReq, x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """暂停/继续。暂停期间阶段时钟不走。"""
    require_admin(x_admin_key)
    a = require_arena()
    if req.action not in ("pause", "resume"):
        raise HTTPException(400, "action 只能是 pause 或 resume")
    a.control(req.action)  # 内核里已经 bump + save（持锁），这里别再存一遍
    _clock.reset()
    log.info("管理员 %s 对局 %s", req.action, a.data["match_id"])
    return summary(a)


@app.post("/api/admin/settle")
def admin_settle(x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """强制结算：未交令的国家按驻守处理，然后立即推进阶段。"""
    require_admin(x_admin_key)
    a = require_arena()
    moved = timeout_hold(a, a.admin_state())
    return dict(settled=moved, state=summary(a))


@app.get("/api/llm")
def llm_status() -> dict:
    """当前有哪些 LLM 座位、花了多少、上下文窗口多大。"""
    provider = from_env()
    return dict(
        configured=provider is not None,
        provider=getattr(provider, "name", None),
        budget_cny=DEFAULT_BUDGET,
        price=dict(input=PRICE_IN, output=PRICE_OUT),
        context=dict(keep_history=KEEP_HIST, keep_messages=KEEP_MSGS),
        seats={p: s.status() for p, s in _seats.items()},
        total=dict(tokens=sum(s.usage.tokens for s in _seats.values()),
                   cost_cny=round(sum(s.usage.cost_cny for s in _seats.values()), 4)),
    )


@app.post("/api/llm/seat")
def llm_seat(req: SeatReq, x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """把某个国家交给模型，或收回人类操作。"""
    require_admin(x_admin_key)
    power = req.power.upper()
    if power not in POWERS:
        raise HTTPException(400, f"未知国家：{req.power}")

    if not req.enabled:
        _seats.pop(power, None)
        return dict(power=power, enabled=False, seats={p: s.status() for p, s in _seats.items()})

    seat = LLMSeat(power, provider_or_400(), persona=req.persona,
                   budget_cny=req.budget_cny if req.budget_cny is not None else DEFAULT_BUDGET,
                   price_in=PRICE_IN, price_out=PRICE_OUT,
                   keep_hist=KEEP_HIST, keep_msgs=KEEP_MSGS)
    _seats[power] = seat
    return dict(power=power, enabled=True, seat=seat.status())


@app.post("/api/llm/tick")
def llm_tick(x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """手动推一轮（调试用；服务运行时后台线程也会自己推）。"""
    require_admin(x_admin_key)
    return dict(results=_runner.tick())


@app.get("/api/llm/context")
def llm_context(power: str, x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """看一眼某个座位本回合会收到什么上下文——用来核对 token 预算是否真的收住了。"""
    require_admin(x_admin_key)
    a = require_arena()
    ctx = build_context(a, power.upper(), KEEP_HIST, KEEP_MSGS)
    # 四个字段原样拼起来就是 `as_prompt()`：调试面板看到的必须等于模型真收到的
    return dict(power=power.upper(), digest=ctx.digest, own=ctx.own, note=ctx.note,
                text=ctx.text,
                dropped_history=ctx.dropped_history, dropped_messages=ctx.dropped_messages,
                stats=ctx.stats)


def _act(token: str | None, payload: dict) -> dict:
    import uuid

    a = require_arena()
    power = seat(a, token)
    payload["request_id"] = str(uuid.uuid4())
    with a.lock:
        try:
            result = a.act(power, payload)
        except OrderError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        settled = try_settle(a)
        return dict(result=result, settled=settled, state=summary(a))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    import uvicorn

    # 线程与告警都在 lifespan 里，这里只管起服务——两条启动路径行为一致
    uvicorn.run(app, host=HOST,
                port=int(os.environ.get("PORT", "8080")), workers=1, log_level="info")


if __name__ == "__main__":
    main()

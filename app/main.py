"""B0 版：单局热座网页服务。

设计要点：
- 裁判逻辑完全复用 app/engine/arena.py（来自 meow-ai-arena，AGPL），本文件只做 HTTP 壳。
- 单进程、单局、单 worker：Arena 是模块级单例，uvicorn 不能用多 worker。
- 座位鉴权沿用原设计的 Bearer 令牌，热座模式下前端持有全部 7 个令牌。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import export as export_mod
from .clock import PhaseClock
from .engine.arena import POWERS, Arena, OrderError
from .llm import LLMSeat, SeatRunner, Usage, probe, seatstore
from .llm import build as build_context
from .llm import settings as llm_settings
from .llm.orders import hold_orders
from .map_data import geometry
from .persist import save
from .spectate import SpectateChannel

# 版本由构建期注入（Dockerfile 的 APP_VERSION 来自 git tag），避免镜像标签与 /healthz 漂移。
# 本地直接跑时没有注入，别硬编码一个具体版本——那会让 /healthz 说谎，用 dev 占位最诚实。
VERSION = os.environ.get("APP_VERSION", "0.0.0-dev")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
STATE_PATH = DATA_DIR / "state.json"
STATIC_DIR = Path(__file__).parent / "static"

# 观战频道：观众之间的公开聊天。与 state.json（对局）、seats.json（模型座位）并列，
# 三份文件各管一件事——存档不该同时承担「棋走到哪」和「观众聊了什么」。
SPECTATE = SpectateChannel(DATA_DIR / "spectate.json",
                           min_gap=float(os.environ.get("SPECTATE_MIN_GAP", "1.0")))

# 管理密钥：留空表示"本机单人"模式——但**只对本机**放行，见 admin_locked()。
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
_runner = SeatRunner(
    lambda: _arena, _seats,
    interval=float(os.environ.get("LLM_INTERVAL", "2")),
    total_budget=lambda: llm_settings.current().total_budget_cny,
    persist=lambda: seatstore.save(_seats),
)

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
        # 轮换之后会走到这里。不能只说「无效」——玩家会以为是页面坏了，
        # 反复刷新；要指明「钥匙换过了，去要新的」。
        raise HTTPException(403, "座位令牌无效或已被轮换：请向房主索取新链接") from None


def require_admin(key: str | None) -> None:
    """管理面：设了 ADMIN_KEY 就校验；留空时**只有本机**放行。

    这里以前是「留空一律放行」，等于把「本机单人」的方便原样搬到公网：任何能打开
    页面的人都能调 `/api/match` 拿到全部 7 个座位令牌（一次读完七国私信）、清空存档、
    改模型密钥。现在留空 + 对外监听 = **管理面全锁**，页面顶部会写明怎么解开。

    取舍：宁可让部署者撞一个 403 去配密钥，也不要在无人看守时把令牌发给第一个访问者。
    """
    if is_admin(key):
        return
    if ADMIN_KEY:
        raise HTTPException(403, "需要管理员密钥：请在页面右上角填写，或传 X-Admin-Key 请求头")
    raise HTTPException(
        403,
        "未设置 ADMIN_KEY 且监听在非回环地址：管理面已锁定。"
        "请加 -e ADMIN_KEY=<长随机串> 后重启；只在本机玩就把监听改回 127.0.0.1。",
    )


def is_admin(key: str | None) -> bool:
    """不抛异常的版本：观战频道要判断「这条发言是不是房主发的」。"""
    if ADMIN_KEY:
        return bool(key) and hmac.compare_digest(key, ADMIN_KEY)
    return not bound_off_loopback()


def admin_locked() -> bool:
    """管理面是否因为「没设密钥 + 对外可达」被锁死。

    与 `exposed` 的区别：`exposed` 说「外面连得上」，这个说「连得上但没人管得了」。
    前端要按后者决定提示文案——两者的处置完全不同。
    """
    return not ADMIN_KEY and bound_off_loopback()


def optional_seat(a: Arena, token: str | None) -> str | None:
    """有令牌就认，没有就返回 None（旁观视角）。

    令牌**给了但不对**必须报错，不能悄悄降级成旁观：否则座位链接轮换后，玩家会看到
    「能看不能交令」的页面，还以为是前端坏了。只有「压根没带令牌」才是旁观。
    """
    if not token:
        return None
    return seat(a, token)


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
    # 座位表落过盘就装回来：重启后勾选与花费都还在（原先 `_seats` 只在内存里，
    # 重启等于把「谁在打」和「这局花了多少」一起清空，成本护栏也跟着重置）。
    if not _seats:
        seatstore.restore(_seats, llm_settings.current())
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


class RotateReq(BaseModel):
    powers: list[str] = Field(
        default_factory=list,
        description="要换令牌的国家（如 [\"FRANCE\"]）；留空 = 七个全换",
    )


@app.get("/healthz")
def healthz() -> dict:
    """探活。exposed 说「外面连得上」，admin_locked 说「连得上但没人管得了」。"""
    return dict(ok=True, version=VERSION, has_match=arena() is not None,
                admin_required=bool(ADMIN_KEY), admin_locked=admin_locked(),
                exposed=bound_off_loopback(),
                spectate=dict(has_key=bool(SPECTATE.data["key"])))


@app.post("/api/match")
def new_match(x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """建局或返回现有对局。**只有管理员**能调——一次返回全部 7 个座位令牌。

    这个接口吐出全部座位令牌（等于交出所有私信），所以它必须站在管理面后面。
    无密钥又对外监听时 `require_admin` 会直接 403，页面表现为「等待管理密钥」。
    真玩家不走这里：他们拿的是 `/api/links` 发出去的**单人单链接**。
    """
    global _arena
    require_admin(x_admin_key)
    with _lock:
        a = arena()
        if a is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            a = Arena.create(STATE_PATH)
            _arena = a
            SPECTATE.reset(a.data["match_id"])
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
        spectate=f"{base}/?watch={SPECTATE.code()}",
        notice=None,
    )


@app.post("/api/links/rotate")
def rotate_links(request: Request,
                 req: RotateReq | None = None,
                 x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """换座位令牌，旧链接立刻失效，返回新链接。

    补的是「链接被转发出去」这个洞：以前只能开新局（连棋盘一起丢）才换得掉令牌，
    现在只换令牌，存档里其它字段一个不动——棋照走，观众照看，只有那几把旧钥匙作废。

    刻意做成**替换**而不是「再加一把」：`tokens` 里每个国家只留一把，换就是让旧的死。
    否则「转发过的链接还能用」这条根本没堵上，只是多了一把新钥匙。
    """
    require_admin(x_admin_key)
    a = require_arena()
    wanted = [p.strip().upper() for p in (req.powers if req else []) if p.strip()]
    unknown = [p for p in wanted if p not in POWERS]
    if unknown:
        raise HTTPException(400, f"不认识的国家：{'、'.join(unknown)}；可选 {'、'.join(POWERS)}")
    rotated = list(dict.fromkeys(wanted)) or list(POWERS)
    with a.lock:
        for p in rotated:
            a.data["tokens"][p] = secrets.token_urlsafe(32)
        a.save()
        a.bump()            # 让轮询中的页面发现「有变化」，而不是继续拿死令牌去撞
        fresh = {p: a.data["tokens"][p] for p in rotated}
    base = str(request.base_url).rstrip("/")
    return dict(
        rotated=rotated,
        links={p: f"{base}/?seat={p}&t={t}" for p, t in fresh.items()},
        spectate=f"{base}/?watch={SPECTATE.code()}",
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
        SPECTATE.reset(_arena.data["match_id"])
        # 新局重置花费与停摆，但**保留用户按下的暂停**：那是「这一国暂时不给模型」的
        # 意图，不该被新开一局悄悄改掉
        for llm_seat in _seats.values():
            llm_seat.usage = Usage()
            if llm_seat.paused:
                llm_seat.pause("已暂停")
            else:
                llm_seat.resume()
        seatstore.save(_seats)
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


@app.get("/api/map")
def map_shape(request: Request) -> Response:
    """标准地图的轮廓与棋子锚点（`app/map_data.py` 从引擎自带 SVG 里抽出来的）。

    不需要鉴权：这只是底图，没有对局信息。解析一次约 0.3 秒，之后走进程内缓存；
    响应带一天缓存 + ETag——**几何真的变了**（换了地图文件）要能立刻生效，靠校验器而不是
    靠用户清缓存：前端按 `no-cache` 取，命中就 304，只花一个来回。
    """
    data = geometry()
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    # 版本就是内容哈希：改了 SVG 或解析逻辑，标签自然就变
    etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=86400"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@app.get("/api/board")
def board(x_seat_token: Annotated[str | None, Header()] = None) -> dict:
    """地图页要的局面：谁在哪、哪个补给中心归谁、**你**能下哪些令。

    部队与中心归属在 Diplomacy 里是公开信息（`brief()` 本来就把整张局面写给每个人），
    所以这里不按座位过滤；唯一私密的是 `mine`——只含调用者座位的合法命令。

    **不带令牌也能看**（观战视角）：`mine` 为空、`editable` 为假。合法命令在 Diplomacy
    里等于意图宣言，一场围观不该顺手把它交出去——这是观众与玩家的分界线。

    `mine` 的键是**基础地名**（`STP`），命令串里才带海岸（`F STP/SC - BOT`）。引擎的
    `get_orderable_locations()` 就是这么给的，而地图上的形状也只到基础省份这一层——
    两边正好对齐，前端不必自己拆海岸。
    """
    a = require_arena()
    power = optional_seat(a, x_seat_token)
    with a.lock:
        game = a.game()
        units, centers = [], {}
        for who in POWERS:
            for line in game.get_units(who):
                kind, loc = line.split(" ", 1)
                units.append(dict(power=who, kind=kind, loc=loc, at=loc.split("/")[0]))
            for center in game.get_centers(who):
                centers[center] = who
        mine = {}
        if power:
            possible = game.get_all_possible_orders()
            mine = {loc: sorted(possible[loc]) for loc in game.get_orderable_locations(power)}
        return dict(
            power=power or "", phase=game.get_current_phase(), stage=a.data["stage"],
            paused=a.data["paused"], units=units, centers=centers,
            mine=mine, editable=bool(mine),
        )


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
    """结算原文。**不带令牌也能看**，只是没有「（你）」标记。

    结算记录本来就是公开的（谁动了哪支、成没成），`_settlement(record, power)` 里
    唯一按座位变化的只有那个「（你）」后缀。所以这里给 `power=""` 就是干净的公开版，
    不需要另造一套「观众版结算」逻辑。
    """
    a = require_arena()
    power = optional_seat(a, x_seat_token) or ""
    with a.lock:
        text = a.history_text(power, phase)
    return dict(text=text, power=power)


@app.get("/api/replay")
def replay(x_seat_token: Annotated[str | None, Header()] = None) -> dict:
    """复盘：逐阶段回放，视角永远锁在调用者的座位上。

    私信是最容易在这里漏的东西——`inbox(power, phase=...)` 本身按座位过滤，
    所以一局打完后，任何人拿自己的令牌只能看到「自己收到/发出的那些信」。
    不要为了「看起来完整」去返回全量 messages，那等于把复盘页做成偷看面板。

    观战者是 `power=None`：结算全给、**messages 一律为空字符串**。空缺不是遗漏——
    「这一阶段有没有信」本身就是信息，连条数都不该给观众。
    """
    a = require_arena()
    power = optional_seat(a, x_seat_token) or ""
    with a.lock:
        history_log = list(a.data["history"])
        items = []
        for i, rec in enumerate(history_log, 1):
            phase = rec["phase"]
            centers = rec.get("centers") or {}
            items.append(dict(
                index=i,
                phase=phase,
                time=rec.get("time"),
                agreed_draw=rec.get("event") == "agreed_draw",
                text=a.history_text(power, phase),
                centers={p: len(c) for p, c in centers.items() if c},
                messages=a.inbox(power, phase=phase) if power else "",
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


@app.get("/api/export")
def export_game(x_seat_token: Annotated[str | None, Header()] = None,
                fmt: str = Query("md", alias="format", pattern="^(md|markdown|text|json)$"),
                ) -> Response:
    """把整局导出：文本战报（`format=md`）或 JSON（`format=json`）。

    私信的边界只看**有没有座位令牌**：坐着的人拿到自己收发的那一份，其余人拿到一份
    不含任何私信的公开战报——管理员也一样。v0.7.5 定的「管理是运维不是偷看」，
    导出不该是它的例外；真要整档备份就拷 `data/state.json`。

    令牌走请求头而不是查询串：下载链接会被贴进聊天、留在浏览器历史里，
    令牌一旦进了 URL 就等于公开。所以前端是先带头发请求、再拿 Blob 落盘。
    """
    a = require_arena()
    who = optional_seat(a, x_seat_token) or ""
    with a.lock:
        if fmt.lower() == "json":
            body = json.dumps(export_mod.payload(a, who), ensure_ascii=False, indent=2)
            media, ext = "application/json; charset=utf-8", "json"
        else:
            body = export_mod.markdown(a, who)
            media, ext = "text/markdown; charset=utf-8", "md"
        phase = a.game().get_current_phase()
        match_id = a.data["match_id"]
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", f"{match_id}-{phase}")
    stem = re.sub(r"-{2,}", "-", stem).strip("-")   # match_id 本身可能以 `-` 结尾，别落成 `--`
    return Response(body, media_type=media, headers={
        "content-disposition": f'attachment; filename="diplomacy-{stem}.{ext}"',
    })


@app.post("/api/send")
def send(req: SendReq, x_seat_token: Annotated[str | None, Header()] = None) -> dict:
    return _act(x_seat_token, dict(action="send", to=req.to, text=req.text))


@app.post("/api/orders")
def orders(req: OrdersReq, x_seat_token: Annotated[str | None, Header()] = None) -> dict:
    return _act(x_seat_token, dict(action="orders", orders=req.orders))


@app.post("/api/draw")
def draw(req: DrawReq, x_seat_token: Annotated[str | None, Header()] = None) -> dict:
    return _act(x_seat_token, dict(action="draw" if req.vote else "undraw"))


# ---------------- 观战频道 ----------------

class SayReq(BaseModel):
    name: str = Field("", max_length=40, description="昵称，留空叫「旁观者」")
    text: str = Field(..., max_length=400)


def _client(request: Request) -> str:
    """限频用的来源标识。

    放在 nginx 后面时这里恒为代理地址，全馆共用一个桶——观战频道本来就只打算给
    一小圈人用，共用 1 秒一条的桶仍然比「不限」好。真要做按人限频得先信任
    X-Forwarded-For，而信任它需要先确定代理层是干净的，这一步留到有需要时再做。
    """
    return getattr(request.client, "host", "") or "?"


@app.get("/api/spectate")
def spectate_read(after: int = Query(0, ge=0),
                  x_spectate_key: Annotated[str | None, Header()] = None,
                  x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """观战频道：**谁都能读**。

    频道里只有观众看得见的东西（观众本来就只能看公开信息），所以玩家读到它不会
    多知道一个字；反过来，玩家看到他国被怎么评价也挺有意思。真正的门在写那一侧。
    """
    a = arena()
    return dict(
        messages=SPECTATE.since(after),
        phase=a.game().get_current_phase() if a else "",
        can_say=is_admin(x_admin_key) or SPECTATE.matches(x_spectate_key),
        has_key=bool(SPECTATE.data["key"]),
    )


@app.post("/api/spectate")
def spectate_say(req: SayReq, request: Request,
                 x_spectate_key: Annotated[str | None, Header()] = None,
                 x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """发言要观战码。这是一条公网可写入口，旁边就是正在进行的对局，不能裸开。"""
    if not (is_admin(x_admin_key) or SPECTATE.matches(x_spectate_key)):
        raise HTTPException(403, "发言需要观战码：向房主要一条观战链接（/?watch=<码>）。")
    a = arena()
    try:
        message = SPECTATE.say(req.name, req.text,
                              phase=a.game().get_current_phase() if a else "",
                              who=_client(request))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return dict(ok=True, message=message)


@app.get("/api/spectate/key")
def spectate_key(request: Request,
                 x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """观战码只给管理员看。

    码等于「发言权」，拿到就能往频道里写字。管理员需要把它变成一条链接发给朋友，
    所以这里回完整明文——它不像 API Key 那样是长期凭证，轮换一次就作废。
    """
    require_admin(x_admin_key)
    code = SPECTATE.code()
    base = str(request.base_url).rstrip("/")
    return dict(key=code, link=f"{base}/?watch={code}")


@app.post("/api/spectate/rotate")
def spectate_rotate(x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """换一把码：已经发出去的观战链接全部失效。"""
    require_admin(x_admin_key)
    return dict(key=SPECTATE.rotate())


# ---------------- LLM 座位（B1）----------------

class SeatReq(BaseModel):
    power: str
    enabled: bool = True
    remove: bool = Field(False, description="真的要删掉这个座位（连账一起），而不是暂停")
    persona: str = Field("", description="给这个座位的人设，影响说话风格")
    budget_cny: float | None = Field(None, description="本局预算，超出后自动转保底驻守")


class SettingsReq(BaseModel):
    """网页上的模型配置。所有字段都可缺省，缺省即「不改这一项」。"""
    mode: str | None = Field(None, description="openai = 兼容 /v1/chat/completions；scripted = 离线演练")
    base_url: str | None = None
    api_key: str | None = Field(None, description="留空表示沿用已保存的密钥，不会被清空")
    clear_key: bool = Field(False, description="要真的清掉密钥才传 true")
    model: str | None = None
    timeout: float | None = Field(None, ge=5, le=600)
    max_tokens: int | None = Field(None, ge=256, le=32000,
                                   description="单次输出上限，含推理型模型的思维链")
    price_in: float | None = Field(None, ge=0, description="¥ / 百万 input token")
    price_out: float | None = Field(None, ge=0, description="¥ / 百万 output token")
    budget_cny: float | None = Field(None, gt=0, description="单座位预算，超出自动转保底驻守")
    total_budget_cny: float | None = Field(None, ge=0,
                                           description="全场总预算，0 = 不限；撞线后全员转驻守")
    keep_hist: int | None = Field(None, ge=0, le=50, description="上下文：最近几段结算")
    keep_msgs: int | None = Field(None, ge=0, le=500, description="上下文：最近几条消息")

    def patch(self) -> dict:
        """只把**用户真的传了**的字段挑出来，未传的不能当成空值覆盖掉旧配置。"""
        return {k: v for k, v in self.model_dump(exclude_unset=True).items() if v is not None}


def provider_or_400():
    cfg = llm_settings.current()
    provider = cfg.build_provider()
    if provider is None:
        raise HTTPException(400, "未配置模型：在「模型」页签填 Base URL 与模型名（存一次即生效），"
                                 "或用 LLM_PROVIDER=scripted 做离线演练")
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
    cfg = llm_settings.current()
    return dict(
        configured=cfg.configured,
        provider=getattr(cfg.build_provider(), "name", None),
        source=cfg.source,
        model=cfg.model,
        budget_cny=cfg.budget_cny,
        total_budget_cny=cfg.total_budget_cny,
        price=dict(input=cfg.price_in, output=cfg.price_out),
        context=dict(keep_history=cfg.keep_hist, keep_messages=cfg.keep_msgs),
        seats={p: s.status() for p, s in _seats.items()},
        total=dict(tokens=sum(s.usage.tokens for s in _seats.values()),
                   cost_cny=round(sum(s.usage.cost_cny for s in _seats.values()), 4)),
    )


# ---- 模型配置：网页里填，存一次即生效（原先是环境变量 + 重启）----

@app.get("/api/llm/settings")
def llm_get_settings(x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """当前生效的配置（**不含原始密钥**，只给尾四位）。

    要管理员：`base_url` 可能是一个内网地址，密钥虽被掩码，凑齐「网关地址 + 模型名」
    也足够画出部署拓扑。页面上这个页签本来也只对管理员显示。
    """
    require_admin(x_admin_key)
    cfg = llm_settings.current()
    return dict(settings=cfg.public(), path=str(llm_settings.config_path()),
                editable=True)


@app.put("/api/llm/settings")
def llm_put_settings(req: SettingsReq, x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """保存配置，并立刻推到已经在跑的座位上。

    「存完还要重启」是最容易让人以为没保存成功的行为，所以这里顺手做两件事：
    已有座位换 provider、回填价格/预算/上下文窗口；预算调高了还会把因超额停掉的座位放回来。
    """
    require_admin(x_admin_key)
    cfg = llm_settings.update(req.patch())
    for llm_seat in _seats.values():
        cfg.apply_to(llm_seat)
    seatstore.save(_seats)
    log.info("模型配置已由管理员更新（来源 %s，模式 %s）", cfg.source, cfg.mode)
    return dict(settings=cfg.public(), seats={p: s.status() for p, s in _seats.items()})


@app.post("/api/llm/settings/test")
def llm_test_settings(req: SettingsReq | None = None,
                      x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """拿**表单里刚填的**配置真发一次请求，不落盘。

    先测后存：填错 base_url 是 404、模型名错是 400、密钥错是 401，三种在页面上都只表现为
    「没反应」，真发一次才能把这三种分开。不落盘是有意的——测不通的配置不该覆盖能用的那份。
    """
    require_admin(x_admin_key)
    cfg = llm_settings.current()
    if req is not None:
        cfg = cfg.merged(req.patch())
    provider = cfg.build_provider()
    if provider is None:
        return dict(ok=False, ms=0, error="配置不完整：至少要有 Base URL 与模型名")
    result = probe(provider)
    result["settings"] = cfg.public()
    if result.get("error"):
        # 网关的报错经常原样回显请求头，抹一遍再回给页面
        result["error"] = llm_settings.redact(str(result["error"]), cfg.api_key)
    return result


@app.post("/api/llm/seat")
def llm_seat(req: SeatReq, x_admin_key: Annotated[str | None, Header()] = None) -> dict:
    """把某个国家交给模型 / 让模型暂停 / 彻底移除这个座位。

    这里特意把「暂停」和「收回」分成两个动作：

    - 暂停（`enabled:false`）：座位和账都留着，人可能在旁边接手。原先的做法是
      `_seats.pop()`，对象连同 `usage` 一起消失——「先别花钱，但我要看账」做不到，
      P1 实测时为了叫停一次失控的花费只能连账一起丢。
    - 移除（`remove:true`）：真的要清掉这个座位（连同账），下一局重新开始。
    """
    require_admin(x_admin_key)
    power = req.power.upper()
    if power not in POWERS:
        raise HTTPException(400, f"未知国家：{req.power}")

    def snapshot(**extra) -> dict:
        return dict(power=power, seats={p: s.status() for p, s in _seats.items()}, **extra)

    existing = _seats.get(power)
    if req.remove:
        _seats.pop(power, None)
        seatstore.save(_seats)
        return snapshot(enabled=False, removed=True)

    if not req.enabled:
        if existing is None:
            return snapshot(enabled=False)
        existing.pause("已暂停（用量保留）")
        seatstore.save(_seats)
        return snapshot(enabled=False, paused=True, seat=existing.status())

    if existing is not None:
        existing.resume()
        llm_settings.current().apply_to(existing)   # 顺手把最新配置推上去
        seatstore.save(_seats)
        return snapshot(enabled=True, resumed=True, seat=existing.status())

    cfg = llm_settings.current()
    seat = LLMSeat(power, provider_or_400(), persona=req.persona,
                   budget_cny=req.budget_cny if req.budget_cny is not None else cfg.budget_cny,
                   price_in=cfg.price_in, price_out=cfg.price_out,
                   keep_hist=cfg.keep_hist, keep_msgs=cfg.keep_msgs)
    _seats[power] = seat
    seatstore.save(_seats)
    return snapshot(enabled=True, seat=seat.status())


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
    cfg = llm_settings.current()
    ctx = build_context(a, power.upper(), cfg.keep_hist, cfg.keep_msgs)
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

"""B0 版：单局热座网页服务。

设计要点：
- 裁判逻辑完全复用 app/engine/arena.py（来自 meow-ai-arena，AGPL），本文件只做 HTTP 壳。
- 单进程、单局、单 worker：Arena 是模块级单例，uvicorn 不能用多 worker。
- 座位鉴权沿用原设计的 Bearer 令牌，热座模式下前端持有全部 7 个令牌。
"""
from __future__ import annotations

import hmac
import logging
import os
import threading
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .engine.arena import POWERS, Arena, OrderError
from .llm import LLMSeat, SeatRunner, Usage, from_env
from .llm import build as build_context

# 版本由构建期注入（Dockerfile 的 APP_VERSION 来自 git tag），避免镜像标签与 /healthz 漂移
VERSION = os.environ.get("APP_VERSION", "0.1.0")
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


def summary(a: Arena) -> dict:
    st = a.admin_state()
    game = a.game()
    return dict(
        version=VERSION,
        match_id=st["match_id"],
        phase=st["phase"],
        paused=st["paused"],
        game_over=st["game_over"],
        result=st["result"],
        result_text=a.result_text(),
        centers={p: len(game.get_centers(p)) for p in POWERS},
        units={p: len(game.get_units(p)) for p in POWERS},
        players={p: dict(active=s["active"], required=s["required"], ready=s["ready"])
                 for p, s in st["players"].items()},
    )


def try_settle(a: Arena) -> bool:
    """所有人都交令了就结算；否则静默返回 False。"""
    try:
        if a.settle(quiet=True):
            a.save()
            return True
    except ValueError:
        pass
    except Exception:
        log.exception("settle 失败")
    return False


app = FastAPI(title="七国纵横 · Diplomacy Web", version=VERSION, docs_url="/api/docs", openapi_url="/api/openapi.json")


class SendReq(BaseModel):
    to: str = Field(..., description="国家代码或 GLOBAL")
    text: str


class OrdersReq(BaseModel):
    orders: list[str] = Field(default_factory=list)


class DrawReq(BaseModel):
    vote: bool = True


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
    return dict(power=power.upper(), digest=ctx.digest, text=ctx.text,
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

    if not ADMIN_KEY:
        msg = ("未设置 ADMIN_KEY：/api/match 会把 7 个座位令牌发给任何访问者"
               "（等于公开全部私信），任何人也能清档。")
        if bound_off_loopback():
            log.critical("%s 当前监听 %s，本机之外也能连上——页面上会挂红色告警。"
                         "对外部署请设 ADMIN_KEY=<长随机串>；只在本机玩请设 HOST=127.0.0.1。",
                         msg, HOST)
        else:
            log.warning("%s 仅监听 %s，只有本机能连，暂不构成风险。", msg, HOST)
    _runner.start()
    uvicorn.run(app, host=HOST,
                port=int(os.environ.get("PORT", "8080")), workers=1, log_level="info")


if __name__ == "__main__":
    main()

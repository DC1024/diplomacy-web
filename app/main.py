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

# 版本由构建期注入（Dockerfile 的 APP_VERSION 来自 git tag），避免镜像标签与 /healthz 漂移
VERSION = os.environ.get("APP_VERSION", "0.1.0")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
STATE_PATH = DATA_DIR / "state.json"
STATIC_DIR = Path(__file__).parent / "static"

# 管理密钥：留空表示"本机单人"模式，建局/清档不鉴权；一旦对外开放必须设置。
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()

log = logging.getLogger("diplomacy")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "info").upper(),
                    format="%(asctime)s %(levelname)s %(message)s")

_lock = threading.RLock()
_arena: Arena | None = None


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


app = FastAPI(title="Diplomacy Web (B0)", version=VERSION, docs_url="/api/docs", openapi_url="/api/openapi.json")


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
    return dict(ok=True, version=VERSION, has_match=arena() is not None, admin_required=bool(ADMIN_KEY))


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
        log.warning("未设置 ADMIN_KEY：/api/match 会把 7 个座位令牌发给任何访问者，"
                    "任何人也能清档。仅供本机单人使用；对外部署请设置 ADMIN_KEY=<长随机串>。")
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8080")), workers=1, log_level="info")


if __name__ == "__main__":
    main()

"""模型接入层：只要实现 `chat(system, user) -> Reply` 就能当一个座位的大脑。

刻意不绑定具体厂商——本机已经有 OpenAI 兼容网关（new-api），
把它当默认形态即可，换模型只改环境变量。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse


class ProviderError(RuntimeError):
    """网络/鉴权/配额等失败，调用方应当降级而不是崩掉整局。"""


@dataclass
class Reply:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BadOutput(ProviderError):
    """网关通了、钱花了，但拿回来的东西不能当命令用。

    单独成一类是因为它和「连不上」的处置完全不同：
    - 连不上：重试没意义，立刻兜底。
    - 被截断：把输出上限调大就能救，而且**那次调用已经计费了**，得照样记账，
      否则成本报表会把失败的尝试当成免费的——实测里这正好是一半的钱。
    """

    def __init__(self, message: str, reply: Reply, *, kind: str = "", cap: int = 0):
        super().__init__(message)
        self.reply = reply
        self.kind = kind    # "truncated"（被 max_tokens 截断）| "empty"（正文为空）
        self.cap = cap      # 本次用的 max_tokens，重试时据此翻倍


class Provider(Protocol):
    name: str

    def chat(self, system: str, user: str, *, max_tokens: int | None = None) -> Reply: ...


class ScriptedProvider:
    """测试用的假模型：不联网、不花钱、行为可预期。

    把它接到某个座位后，该座位每回合都会按脚本行动，用来验证
    「上下文组装 → 解析 → 校验 → 提交」这条链路本身。
    """

    name = "scripted"

    def __init__(self, script: list[dict] | None = None, *, repeat_last: bool = True):
        self.script = script or [{"pass": True}]
        self.repeat_last = repeat_last
        self.calls = 0

    def chat(self, system: str, user: str, *, max_tokens: int | None = None) -> Reply:
        idx = min(self.calls, len(self.script) - 1) if self.repeat_last else self.calls
        item = self.script[idx] if idx < len(self.script) else {"pass": True}
        self.calls += 1
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        # 粗估，够测试断言用
        return Reply(text=text, prompt_tokens=len(system + user) // 2, completion_tokens=len(text) // 2)


def normalize_base(url: str) -> str:
    """把用户手填的各种写法收成一个根地址。

    页面上让人只填一次，就必须容忍他填 `https://api.x.com/v1/`、
    `https://api.x.com/v1/chat/completions`、甚至带空格的复制粘贴。
    只做「去掉多余后缀」这一件事，不猜该补 `/v1`——补错了会得到更难懂的报错。
    """
    u = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if u.endswith(suffix):
            u = u[: -len(suffix)].rstrip("/")
    return u


def _is_private_host(url: str) -> bool:
    """这个地址该不该绕过代理。

    httpx 默认 `trust_env=True`，会照 `HTTP_PROXY` / `HTTPS_PROXY` 走。本机或局域网里
    的网关（ollama `127.0.0.1:11434`、new-api `192.168.1.10:3000`）一旦被塞进外网代理，
    得到的是 **502 upstream connect failed**——报错看起来像「网关挂了」，实际只是绕错了路。
    反过来的错误更要命：为了绕开代理而全局 `trust_env=False`，会让需要代理才能连上的
    `api.openai.com` 直接不通。所以按目标地址判断，而不是一刀切。
    """
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "::1") or host.endswith(".local"):
        return True
    if host.startswith("127.") or host.startswith("10.") or host.startswith("192.168."):
        return True
    # 172.16.0.0/12
    parts = host.split(".")
    if len(parts) == 4 and parts[0] == "172" and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
        return True
    return False


class OpenAICompatProvider:
    """OpenAI 兼容的 /v1/chat/completions（new-api、vLLM、官方 API 都适用）。"""

    # 为什么不是 900：推理型模型（deepseek-r1 这类、以及很多网关的 smart-split 路由）
    # 会先在 `reasoning_content` 里烧掉一两千 token 的思维链，`max_tokens` 是**含思维链**
    # 的总配额。900 的配额在思维链阶段就见底了，`content` 回来是空串——表现成
    # 「模型连续两次未给出可解析的 JSON」，看着像模型不会输出 JSON，实际是没给它地方写。
    #
    # 为什么是 4096 而不是更大：直觉上「配额给宽一点就少一次重发」，实测**反了**。
    # 同一模型（deepseek-v4.1-flash）各跑 8 阶段对比：
    #
    #   4096 → ¥2.8935、458 388 token、77 次调用、46 分钟
    #   8192 → ¥3.7449、551 629 token、62 次调用、53 分钟
    #
    # 调用次数是降了（22%），但单次更贵：这条路由的思维链是**长尾**，约三成调用
    # 连 8192 都填不满。配额抬高并不能让它们一次说完，只是把「白烧的那一次」
    # 从 4096 变成 8192（撞墙后还要再给 16384）——总 token 反而涨了 20%。
    # 配额只影响「撞墙时白烧多少」，不影响「撞不撞墙」，所以给到中位数附近最省。
    # 要真正省钱得改阶梯（空正文不该翻倍，该直接劝它别想），见 TODO 缺口 16。
    DEFAULT_MAX_TOKENS = 4096

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0,
                 max_tokens: int = DEFAULT_MAX_TOKENS):
        self.base_url = normalize_base(base_url)
        self.api_key = (api_key or "").strip()
        self.model = model
        self.timeout = timeout
        self.max_tokens = int(max_tokens or self.DEFAULT_MAX_TOKENS)
        self.name = f"openai:{model}"

    def chat(self, system: str, user: str, *, max_tokens: int | None = None) -> Reply:
        import httpx

        cap = int(max_tokens or self.max_tokens)
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.7,
            "max_tokens": cap,
        }
        # 本机 ollama / vLLM / 内网网关常常不校验密钥，空 key 时不发这个头，
        # 免得 `Authorization: Bearer ` 被某些网关判成「给了个错的凭证」而 401
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        url = f"{self.base_url}/chat/completions"
        try:
            with httpx.Client(timeout=self.timeout,
                              trust_env=not _is_private_host(self.base_url)) as client:
                r = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"请求失败：{exc}") from exc
        if r.status_code != 200:
            raise ProviderError(f"HTTP {r.status_code}：{r.text[:200]}")
        try:
            data = r.json()
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"响应格式异常：{r.text[:200]}") from exc

        text = message.get("content") or ""
        finish = choice.get("finish_reason") or ""
        usage = data.get("usage") or {}
        reply = Reply(text=text,
                      prompt_tokens=int(usage.get("prompt_tokens") or 0),
                      completion_tokens=int(usage.get("completion_tokens") or 0))

        if not text.strip():
            # finish=length 的空正文 = 配额在思维链阶段就见底了：这不是「模型不会说话」，
            # 是「没地方写」，按截断处理，让上层有机会放宽配额再来一次。
            kind = "truncated" if finish == "length" else "empty"
            raise BadOutput(_empty_message(message, usage, finish, cap), reply,
                            kind=kind, cap=cap)
        if finish == "length":
            raise BadOutput(f"输出被 max_tokens={cap} 截断（completion {reply.completion_tokens} token），"
                            f"拿到的不是完整 JSON", reply, kind="truncated", cap=cap)
        return reply


def _empty_message(message: dict, usage: dict, finish: str, cap: int) -> str:
    """正文为空时说清楚「为什么空」。

    只说「未给出可解析的 JSON」会把排查引向提示词——而实测的原因是**配额被思维链吃光了**，
    改提示词一个字都不会变。这里把思维链长度和配额一起报出来，看一眼就知道往哪调。
    """
    detail = usage.get("completion_tokens_details") or {}
    reasoning_tokens = int(detail.get("reasoning_tokens") or 0)
    has_cot = bool(message.get("reasoning_content") or message.get("reasoning"))
    bits = [f"finish_reason={finish or '?'}"]
    if reasoning_tokens or has_cot:
        bits.append(f"思维链占了 {reasoning_tokens or '?'} token")
        bits.append(f"max_tokens={cap} 不足以覆盖思维链+正文")
    else:
        bits.append(f"completion={usage.get('completion_tokens') or 0} token")
    return "模型返回了空正文（" + "，".join(bits) + "）"


def from_env() -> Provider | None:
    """按环境变量造一个 provider。没有配置就返回 None（= LLM 座位不可用）。

    保留这个入口是为了兼容 `docker run -e LLM_*` 的老用法；正常路径走
    `settings.current()`——它优先读网页保存的配置。
    """
    kind = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if kind == "scripted":
        return ScriptedProvider()
    base = os.environ.get("LLM_BASE_URL", "").strip()
    key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not (base and model):
        return None
    return OpenAICompatProvider(base, key, model,
                                timeout=float(os.environ.get("LLM_TIMEOUT", "120") or 120),
                                max_tokens=_env_cap())


def _env_cap() -> int:
    try:
        return int(os.environ.get("LLM_MAX_TOKENS", "") or OpenAICompatProvider.DEFAULT_MAX_TOKENS)
    except ValueError:
        return OpenAICompatProvider.DEFAULT_MAX_TOKENS


def probe(provider: Provider, *, timeout: float = 20.0) -> dict:
    """真发一次最小请求，验证 base_url / 密钥 / 模型名三样都对得上。

    为什么必须真发：填错 base_url 是 404，填错模型名是 400 且报错文本因供应商而异，
    密钥错是 401——这三种在页面上长得一模一样（都只是「没反应」）。花几厘钱
    换一次确定的答复，比让用户回到对局里等一回合失败划算。

    超时按 `min(timeout, 配置的 timeout)` 收短：一个连两句话都答不完的网关，
    放进对局只会白占着阶段时钟。
    """
    limit = min(timeout, float(getattr(provider, "timeout", timeout) or timeout))
    started = time.monotonic()
    try:
        if isinstance(provider, OpenAICompatProvider):
            provider = OpenAICompatProvider(provider.base_url, provider.api_key,
                                            provider.model, timeout=limit,
                                            max_tokens=provider.max_tokens)
        reply = provider.chat("你在做连通性自检。", "只回复两个字：通了")
    except ProviderError as exc:
        return dict(ok=False, ms=int((time.monotonic() - started) * 1000), error=str(exc))
    except Exception as exc:  # noqa: BLE001 - 自检不该把任何异常抛给页面
        return dict(ok=False, ms=int((time.monotonic() - started) * 1000),
                    error=f"{type(exc).__name__}: {exc}")
    return dict(ok=True, ms=int((time.monotonic() - started) * 1000),
                model=getattr(provider, "model", ""), text=(reply.text or "")[:120],
                tokens=reply.tokens)

"""模型网关的运行时配置：网页里填、落盘保存，环境变量只当初始默认值。

为什么非要有这一层：P1 的两件事——**成本实测**（1 人 + 6 模型 < ¥20）与
**谈判质量评估**——都必须接真实模型跑，而原先配置只认环境变量，换个模型名要重建
容器。配置搬进网页之后，「换个模型再跑一局」才是三秒钟的事，比价与调参才可能真的发生。

落盘的 `data/llm.json` 里**有 API Key**，所以这一层的规矩比别处严：

- 接口只回显尾四位（`public()`），原始 key 永不离开后端，连 `500` 的错误文本也要抹一遍
- 写接口只认管理员（`X-Admin-Key`）；`api_key` 留空表示「沿用旧值」，不必每次重填
- 文件写 0600（Windows 上基本无效，容器里有效），原子替换，不写坏半份 JSON

优先级固定为 **网页保存的配置 > 环境变量 > 未配置**。这样 `docker run -e LLM_*` 的老
用法仍然开箱可用，而一旦在网页里存过一次，就以网页为准——否则用户改完发现没生效，
却不知道该去改哪一边，比不给改更糟。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .provider import OpenAICompatProvider, Provider, ScriptedProvider, normalize_base

log = logging.getLogger("diplomacy.llm")

FILENAME = "llm.json"
_MODES = ("openai", "scripted")
_lock = threading.RLock()


def config_path() -> Path:
    """配置跟着存档目录走（`DATA_DIR`），容器里挂一个卷就把对局和密钥一起带走了。"""
    return Path(os.environ.get("DATA_DIR", "./data")) / FILENAME


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    mode: str = "openai"          # openai = 兼容 /v1/chat/completions；scripted = 离线演练
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 120.0
    max_tokens: int = OpenAICompatProvider.DEFAULT_MAX_TOKENS  # 含思维链的总配额
    price_in: float = 2.0         # ¥ / 百万 input token
    price_out: float = 8.0        # ¥ / 百万 output token
    budget_cny: float = 20.0      # 单座位预算，超出后自动转保底驻守
    keep_hist: int = 3            # 上下文：最近几段结算
    keep_msgs: int = 30           # 上下文：最近几条消息
    source: str = "none"          # none | env | file —— 只用于页面提示，不落盘

    # ---------- 派生 ----------
    @property
    def configured(self) -> bool:
        """能不能真的叫到模型。

        `api_key` 不是必要条件：本机的 ollama / vLLM / 内网网关常常不校验密钥，
        强行要求填一个反而挡住最省钱的用法。`base_url` 与 `model` 才是硬条件。
        """
        if self.mode == "scripted":
            return True
        return bool(self.base_url and self.model)

    def build_provider(self, *, timeout: float | None = None) -> Provider | None:
        if self.mode == "scripted":
            return ScriptedProvider()
        if not self.configured:
            return None
        return OpenAICompatProvider(self.base_url, self.api_key, self.model,
                                    timeout if timeout is not None else self.timeout,
                                    max_tokens=self.max_tokens)

    def public(self) -> dict:
        """给页面的形态：**不含原始 key**，只给「有没有」和尾四位。"""
        data = asdict(self)
        data.pop("api_key")
        data["has_key"] = bool(self.api_key)
        data["key_hint"] = mask_key(self.api_key)
        data["configured"] = self.configured
        data["provider"] = getattr(self.build_provider(), "name", None)
        return data

    # ---------- 构造 ----------
    @classmethod
    def from_env(cls) -> Settings:
        mode = (os.environ.get("LLM_PROVIDER", "") or "").strip().lower() or "openai"
        mode = mode if mode in _MODES else "openai"
        base = (os.environ.get("LLM_BASE_URL", "") or "").strip()
        key = (os.environ.get("LLM_API_KEY", "") or "").strip()
        model = (os.environ.get("LLM_MODEL", "") or "").strip()
        # 一个变量都没设时叫 "env" 会让页面显示「来源 环境变量」而实际什么都没配，
        # 用户会去翻 compose 文件找一个不存在的变量
        return cls(
            mode=mode,
            base_url=base,
            api_key=key,
            model=model,
            timeout=_env_float("LLM_TIMEOUT", 120.0),
            max_tokens=_env_int("LLM_MAX_TOKENS", OpenAICompatProvider.DEFAULT_MAX_TOKENS),
            price_in=_env_float("LLM_PRICE_IN", 2.0),
            price_out=_env_float("LLM_PRICE_OUT", 8.0),
            budget_cny=_env_float("LLM_BUDGET_CNY", 20.0),
            keep_hist=_env_int("LLM_KEEP_HIST", 3),
            keep_msgs=_env_int("LLM_KEEP_MSGS", 30),
            source="env" if (base or model or key or mode == "scripted") else "none",
        )

    @classmethod
    def from_dict(cls, raw: dict, *, source: str = "file") -> Settings:
        """容忍脏数据：类型不对的字段退回默认值，别让一个手改坏的 JSON 打不开服务。"""
        base = cls()
        out = {}
        for f in fields(cls):
            if f.name == "source" or f.name not in raw:
                continue
            value = raw[f.name]
            try:
                if f.type in ("float", float):
                    value = float(value)
                elif f.type in ("int", int):
                    value = int(value)
                else:
                    value = str(value).strip()
            except (TypeError, ValueError):
                log.warning("配置项 %s 的值 %r 无法解析，按默认值处理", f.name, raw[f.name])
                value = getattr(base, f.name)
            out[f.name] = value
        if out.get("mode") not in _MODES:
            out["mode"] = "openai"
        if out.get("mode") == "openai" and out.get("base_url"):
            out["base_url"] = normalize_base(out["base_url"])
        return cls(**out, source=source)

    def merged(self, patch: dict) -> Settings:
        """把一份（可能只有几个字段的）改动合到当前配置上。

        `api_key` 是特例：**不传或传空串都表示沿用旧值**。密钥框永远是空的（页面拿不到
        原始 key），如果空串被当成「清空」，那么每次只改模型名都会把密钥抹掉。
        要清空密钥得显式传 `clear_key: true`。
        """
        data = asdict(self)
        patch = dict(patch)          # 别改调用方传进来的那份
        clear_key = bool(patch.pop("clear_key", False))
        for key, value in patch.items():
            if key not in data or value is None:
                continue
            if key == "api_key":
                if value:
                    data["api_key"] = str(value).strip()
                continue
            data[key] = value
        if clear_key:
            data["api_key"] = ""
        if data.get("base_url"):
            data["base_url"] = normalize_base(str(data["base_url"]))
        return Settings.from_dict(data)

    # ---------- 作用到已有座位 ----------
    def apply_to(self, seat) -> None:
        """把新配置推到已经在跑的座位上。

        不这么做的话，「改完预算」这个动作要等下一局才生效——用户会以为没保存。
        预算上调后要**把停掉的座位放回来**：`_bill()` 超额就 `enabled=False`，
        此时若只改数字不带回收，页面会一直显示「超出预算 ¥20」而预算已经改成 ¥50。
        """
        provider = self.build_provider()
        if provider is not None:
            seat.provider = provider
        seat.price_in, seat.price_out = self.price_in, self.price_out
        seat.budget_cny = self.budget_cny
        seat.keep_hist, seat.keep_msgs = self.keep_hist, self.keep_msgs
        if not seat.enabled and seat.usage.cost_cny <= self.budget_cny:
            seat.enabled = True
            seat.stop_reason = ""


def mask_key(key: str) -> str:
    """只露尾四位。短到没法露就一律四个点——别用「长度」当密钥强度的旁证。"""
    key = (key or "").strip()
    if len(key) <= 8:
        return "••••" if key else ""
    return "••••" + key[-4:]


def redact(text: str, key: str) -> str:
    """把错误文本里可能出现的密钥抹掉。

    网关报错经常回显整个请求（含 Authorization），这些字串会一路走到页面的
    「测试连接」结果里。宁可多抹一层。
    """
    key = (key or "").strip()
    if key and len(key) >= 6 and key in text:
        text = text.replace(key, "••••")
    return text


# ---------------- 读写 ----------------

def load() -> Settings | None:
    """读网页保存的配置；没有或读坏了都返回 None（= 退回环境变量）。"""
    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        # 读坏了不能装作没配置就完事：用户会以为「我明明存过」，得留一条日志
        log.warning("模型配置 %s 读取失败（%s），本次退回环境变量", path, exc)
        return None
    if not isinstance(raw, dict):
        return None
    return Settings.from_dict(raw)


def save(settings: Settings) -> None:
    """原子写：先写同目录临时文件再 replace，中途崩掉也不会留下半份 JSON。"""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in asdict(settings).items() if k != "source"}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".llm-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # Windows 上不支持，忽略
            pass
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def current() -> Settings:
    """当前生效的配置。每次现读，不做进程内缓存。

    缓存能省的那点开销（一次 stat + 一个几百字节的 JSON）远不如「改完立刻生效」
    重要：测试里 `DATA_DIR` 是按用例 monkeypatch 的，任何跨用例的缓存都会让
    「默认来自环境变量」这条断言变得时对时错。
    """
    with _lock:
        saved = load()
        if saved is not None:
            # 文件存在就以文件为准，不因为「填得不全」偷偷退回环境变量：
            # 那会让人在页面上看到「已配置」而实际跑的是另一套，比报错难查得多。
            return saved
        return Settings.from_env()


def update(patch: dict) -> Settings:
    """合并保存，返回新配置。"""
    with _lock:
        merged = replace(current().merged(patch), source="file")
        save(merged)
        log.info("模型配置已更新：mode=%s base=%s model=%s", merged.mode,
                 merged.base_url or "(空)", merged.model or "(空)")
        return merged

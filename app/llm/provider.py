"""模型接入层：只要实现 `chat(system, user) -> Reply` 就能当一个座位的大脑。

刻意不绑定具体厂商——本机已经有 OpenAI 兼容网关（new-api），
把它当默认形态即可，换模型只改环境变量。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol


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


class Provider(Protocol):
    name: str

    def chat(self, system: str, user: str) -> Reply: ...


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

    def chat(self, system: str, user: str) -> Reply:
        idx = min(self.calls, len(self.script) - 1) if self.repeat_last else self.calls
        item = self.script[idx] if idx < len(self.script) else {"pass": True}
        self.calls += 1
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        # 粗估，够测试断言用
        return Reply(text=text, prompt_tokens=len(system + user) // 2, completion_tokens=len(text) // 2)


class OpenAICompatProvider:
    """OpenAI 兼容的 /v1/chat/completions（new-api、vLLM、官方 API 都适用）。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.name = f"openai:{model}"

    def chat(self, system: str, user: str) -> Reply:
        import httpx

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.7,
            "max_tokens": 900,
        }
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=payload,
                           headers={"Authorization": f"Bearer {self.api_key}"},
                           timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(f"请求失败：{exc}") from exc
        if r.status_code != 200:
            raise ProviderError(f"HTTP {r.status_code}：{r.text[:200]}")
        try:
            data = r.json()
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise ProviderError(f"响应格式异常：{r.text[:200]}") from exc
        usage = data.get("usage") or {}
        return Reply(text=text or "",
                     prompt_tokens=int(usage.get("prompt_tokens") or 0),
                     completion_tokens=int(usage.get("completion_tokens") or 0))


def from_env() -> Provider | None:
    """按环境变量造一个 provider。没有配置就返回 None（= LLM 座位不可用）。"""
    kind = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if kind == "scripted":
        return ScriptedProvider()
    base = os.environ.get("LLM_BASE_URL", "").strip()
    key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not (base and key and model):
        return None
    return OpenAICompatProvider(base, key, model, float(os.environ.get("LLM_TIMEOUT", "60")))

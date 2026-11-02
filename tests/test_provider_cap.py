"""输出配额：推理型模型的思维链会先把 `max_tokens` 吃光。

这条用例守的是一个真实踩过的坑——P1 实测时七国全部卡在
「模型连续两次未给出可解析的 JSON」，看着像模型不会输出 JSON，
实际是应用只给了 900 token，而该路由光思维链就烧了 1346，
`content` 回来是空串。三件事必须钉死：

1. 空正文要说清「思维链占了多少」，别把人引去改提示词；
2. 被截断要能自救（放宽配额重发一次），而不是两次都撞同一堵墙；
3. **失败的调用也要计费**——否则成本实测会低报，而那正是要测的数字。
"""
import json

import pytest

from app.llm import LLMSeat
from app.llm.provider import BadOutput, OpenAICompatProvider, ProviderError


class _Resp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


def reply_body(text: str, *, finish: str = "stop", reasoning: int = 0,
               completion: int = 1500) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text,
                                 "reasoning_content": "（思维链）"},
                     "finish_reason": finish}],
        "usage": {"prompt_tokens": 700, "completion_tokens": completion,
                  "completion_tokens_details": {"reasoning_tokens": reasoning}},
    }


def fake_gateway(monkeypatch, bodies: list[dict]) -> list[int]:
    """把 httpx.Client 换成按顺序吐预置响应的假货，返回每次请求用的 max_tokens。"""
    import httpx

    seen: list[int] = []
    queue = [_Resp(b) for b in bodies]

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):  # noqa: A002 - 对齐 httpx 签名
            seen.append(json)
            return queue.pop(0)

    monkeypatch.setattr(httpx, "Client", _Client)
    return seen


def caps(seen: list[dict]) -> list[int]:
    return [p["max_tokens"] for p in seen]


def last_user(seen: list[dict]) -> str:
    return seen[-1]["messages"][-1]["content"]


def provider(**kw) -> OpenAICompatProvider:
    return OpenAICompatProvider("http://127.0.0.1:3425/v1", "k", "group/smart-split", **kw)


def test_default_cap_leaves_room_for_a_chain_of_thought():
    # 900 是踩坑时的值：思维链 1346 就把配额吃穿了。给个下限，别再悄悄调回去。
    # 后来又按一局实测把下限从 4096 提到 8192：单次 completion_tokens 中位数就在
    # 4300 上下，4096 恰好压在中位数上，等于一半的调用都要重发一次（慢一倍、两倍钱）。
    assert OpenAICompatProvider.DEFAULT_MAX_TOKENS >= 8192
    assert provider().max_tokens == OpenAICompatProvider.DEFAULT_MAX_TOKENS


def test_cap_is_actually_sent(monkeypatch):
    seen = fake_gateway(monkeypatch, [reply_body('{"pass": true}')])
    provider(max_tokens=1234).chat("s", "u")
    assert caps(seen) == [1234]


def test_empty_body_blames_the_chain_of_thought(monkeypatch):
    fake_gateway(monkeypatch, [reply_body("", finish="length", reasoning=1346)])
    with pytest.raises(BadOutput) as box:
        provider(max_tokens=900).chat("s", "u")
    exc = box.value
    assert exc.kind == "truncated"
    assert "1346" in str(exc) and "max_tokens=900" in str(exc)
    # 失败的这次照样带回 token 数，否则上层没法记账
    assert exc.reply.completion_tokens == 1500


def test_plain_empty_body_is_not_treated_as_truncation(monkeypatch):
    """finish=stop 还回空串 = 模型就是没说话，翻倍配额救不了，别浪费第二次钱。"""
    fake_gateway(monkeypatch, [reply_body("", finish="stop")])
    with pytest.raises(BadOutput) as box:
        provider().chat("s", "u")
    assert box.value.kind == "empty"


def test_seat_widens_the_cap_once_on_truncation(monkeypatch):
    seen = fake_gateway(monkeypatch, [
        reply_body("", finish="length", reasoning=1900),
        reply_body('{"pass": true}', reasoning=900),
    ])
    seat = LLMSeat("GERMANY", provider(max_tokens=3000), budget_cny=20.0)
    reply = seat._chat("s", "u")
    assert caps(seen) == [3000, 6000]    # 第一次撞墙，第二次翻倍
    assert json.loads(reply.text) == {"pass": True}
    assert seat.usage.calls == 2         # 两次都记了账
    assert seat.usage.cost_cny > 0


def test_runaway_chain_of_thought_gets_told_to_stop_thinking(monkeypatch):
    """配额翻倍救不了「一直想」：实测有座位把 6000 token 全用来想、正文一个字没有。

    这时第三次该带着「别再推理」把人拽回来——实测同一提示词下思维链归零、
    193 token 交出完整答案，比继续加额度便宜二十倍。
    """
    seen = fake_gateway(monkeypatch, [
        reply_body("", finish="length", reasoning=8192),
        reply_body("", finish="length", reasoning=16384),
        reply_body('{"pass": true}', reasoning=0, completion=193),
    ])
    seat = LLMSeat("AUSTRIA", provider(), budget_cny=20.0)
    reply = seat._chat("s", "u")
    # 放宽（8192→16384）→ 放宽到头（16384 是天花板）→ 反而缩回 2000 劝它别想
    assert caps(seen) == [8192, 16384, 2000]
    assert "不要再推理" in last_user(seen)
    assert all("不要再推理" not in p["messages"][-1]["content"] for p in seen[:2])  # 前两次不加
    assert json.loads(reply.text) == {"pass": True}
    assert seat.usage.calls == 3


def test_truncation_costs_money_even_when_it_gives_up(monkeypatch):
    fake_gateway(monkeypatch, [
        reply_body("", finish="length", reasoning=2900),
        reply_body("", finish="length", reasoning=2900),
        reply_body("", finish="length", reasoning=2900),
    ])
    seat = LLMSeat("ITALY", provider(max_tokens=3000), budget_cny=20.0)
    with pytest.raises(ProviderError):
        seat._chat("s", "u")
    assert seat.usage.calls == 3
    assert seat.usage.tokens == 3 * (700 + 1500)
    # 一次没成功，但钱是真花了：不能是 0，否则成本实测就是假的
    assert seat.usage.cost_cny > 0


def test_settings_carry_the_cap_into_the_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.llm import settings as llm_settings

    cfg = llm_settings.Settings(base_url="http://127.0.0.1:3425/v1", model="m",
                               max_tokens=4096)
    assert cfg.build_provider().max_tokens == 4096
    # 手改坏的 JSON 不能把服务打挂，退回默认值即可
    bad = llm_settings.Settings.from_dict({"max_tokens": "很多"})
    assert bad.max_tokens == OpenAICompatProvider.DEFAULT_MAX_TOKENS

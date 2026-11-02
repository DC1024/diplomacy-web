"""P3：中英双语。

双语是**叠加层**，不是替换：不传 `lang` 时输出必须和以前一字不差，否则座位上下文、
导出、以及几十条旧断言会一起变味。这里钉四件事：

1. 默认仍是中文（`brief()` / `result_text()` 的旧调用点不受影响）；
2. 英语视图里国家名是英语国名，且**不漏出引擎代号**——`AUSTRIA` 只活在命令与存档里；
3. HTTP 层认 `X-Lang`，SSE 认 `?lang=`（EventSource 设不了请求头）；
4. 每条词条的两种语言占位符集合一致（少一个 `{}` 只有真跑起来才炸）。
"""
import importlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import i18n
from app.engine.arena import POWERS
from app.llm.orders import hold_orders


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        main._runner.stop()
        yield client, main


def start(client) -> dict:
    return client.post("/api/match").json()["tokens"]


# ---------- 词条本身 ----------

def test_every_entry_has_matching_placeholders():
    """`{power}` 写成 `{pw}` 之类，只会在真跑到那句时 KeyError。这里一次抓干净。"""
    broken = []
    for key, (zh, en) in i18n._M.items():
        a = set(re.findall(r"{(\w+)}", zh))
        b = set(re.findall(r"{(\w+)}", en))
        if a != b:
            broken.append(f"{key}: zh={sorted(a)} en={sorted(b)}")
    assert not broken, broken


def test_every_power_has_both_names():
    for code in POWERS:
        zh, en = i18n.POWERS[code]
        assert zh and en, code
        # 中文名必须真的是中文：`奥地利` ≠ `AUSTRIA`。
        # 英文名不做这条检查——引擎代号本来就是英文国名的大写形式（AUSTRIA / Austria）。
        assert zh != code, code
        assert any("\u4e00" <= ch <= "\u9fff" for ch in zh), code


def test_unknown_names_are_passed_through():
    """认不出来就原样返回。把陌生代号吞成空串会让界面静默丢信息。"""
    assert i18n.power_name("ATLANTIS") == "ATLANTIS"
    assert i18n.phase_name("COMPLETED") == "COMPLETED"
    assert i18n.phase_name("FORMING", "en") == "FORMING"


def test_lang_tag_is_normalized():
    """浏览器的 `navigator.language` 是 `en-US` / `zh-CN` 这种形状，别只认 `en`。"""
    assert i18n.pick("en-US") == "en"
    assert i18n.pick("EN_us") == "en"
    assert i18n.pick("zh-CN") == "zh"
    assert i18n.pick(None) == "zh"
    assert i18n.pick("fr") == "zh"  # 没有法语就走默认，不报错


def test_missing_key_raises_instead_of_returning_empty():
    """静默返回空串会变成「界面上一块空白」，比崩还难查。"""
    with pytest.raises(KeyError):
        i18n.t("zh", "no.such.key")


# ---------- 简报 ----------

def test_brief_defaults_to_chinese(srv):
    """不传 lang 必须与传 lang='zh' 完全一致——旧调用点一个字都不能变。"""
    client, main = srv
    start(client)
    a = main.require_arena()
    assert a.brief("ITALY") == a.brief("ITALY", lang="zh")
    text, _, _ = a.brief("ITALY")
    assert "你是 意大利" in text
    assert "结算" not in text  # 还没结算过


def test_english_brief_translates_country_names(srv):
    """这是本次改动的核心诉求：简报里认国家靠国名，不是靠 `ITALY` 这种代号。"""
    client, main = srv
    start(client)
    a = main.require_arena()
    text, _, _ = a.brief("ITALY", lang="en")

    assert "You are Italy" in text
    for code in POWERS:
        assert i18n.POWERS[code][1] in text, code
        assert code not in text, f"英文简报里漏出了引擎代号 {code}"
    # 中文国名不该混进英文视图
    for code in POWERS:
        assert i18n.POWERS[code][0] not in text, code


def test_english_settlement_uses_names_not_codes(srv):
    """结算行是最容易漏的地方：它是拼出来的，不是一整条模板。"""
    client, main = srv
    start(client)
    a = main.require_arena()
    phase = a.game().get_current_phase()
    for p in POWERS:
        a.act(p, dict(action="orders", orders=hold_orders(a.game(), p),
                      request_id=f"hold-{phase}-{p}"))
    a.settle(quiet=True)

    text, _, _ = a.brief("ITALY", lang="en")
    assert "· resolution ——" in text
    for code in POWERS:
        assert code not in text, f"结算文本里漏出了 {code}"
    assert "Italy (you)" in text


def test_api_brief_honours_x_lang_header(srv):
    """前端切语言只改一个头，服务端一处读取，两边都不猜。"""
    client, main = srv
    tokens = start(client)
    at = {"X-Seat-Token": tokens["FRANCE"]}

    zh = client.get("/api/brief", headers=at).json()
    en = client.get("/api/brief", headers={**at, "X-Lang": "en"}).json()

    assert "你是 法国" in zh["text"]
    assert "You are France" in en["text"]
    # `power` 字段是给程序用的，不能跟着语言变
    assert zh["power"] == en["power"] == "FRANCE"


# ---------- SSE ----------

def test_event_stream_reads_lang_from_query():
    """`EventSource` 不能自定义请求头，所以流式接口额外认一个查询参数。

    只钉「这个参数被接住了」：真正的推送要等一局结束才有 `result_text`，
    在单测里等它等于等整局跑完。
    """
    import inspect

    import app.main as main

    params = inspect.signature(main.events).parameters
    assert "lang" in params, "SSE 没有查询参数入口，前端切语言时推送到的那句话会一直是中文"


# ---------- 前端：漏翻的守卫 ----------
# 这三条都是「浏览器里才看得见、单测里看不见」的失败模式：英文字符串少一条，
# 界面就那一处停在中文字，而所有接口都返回 200。

HTML = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"


def _html() -> str:
    return HTML.read_text("utf-8")


def _unescape(literal: str) -> str:
    """把源码里的 `\\n` / `\\"` 还原成真字符，否则 `T("…\\n")` 会和词典键对不上。"""
    try:
        return json.loads(f'"{literal}"')
    except ValueError:
        return literal


def _dict_keys(src: str, name: str) -> set[str]:
    """从 `const NAME={…}` 里取键。

    只在两个 `const` 之间找 `"…":`，比真的把 JS 跑一遍省事，也够用：值后面跟的是 `,` 不是 `:`
    （唯一会误收的是值里自带 `"…":` 的句子，多收几条不影响「有没有漏」这个判断）。
    """
    start = src.index(f"const {name}={{")
    end = src.index("const EN_HTML={") if name == "EN" else src.index("}", start)
    if name == "EN_HTML":
        # EN_HTML 是最后一个数据块，截到 ban 后第一个 `};`
        end = src.index("\n};", start)
    chunk = src[start:end]
    return {_unescape(m) for m in re.findall(r'"((?:[^"\\]|\\.)*)"\s*:', chunk)}


def test_every_t_call_has_a_translation():
    """`T("...")` 里的字面量必须在词典里有条目，否则英文界面就那一处是中文。

    这一条抓到过两条真漏：地图图例的「属于 {p}」和退出绑定的确认框——
    它们都带占位符，`T()` 不报错、页面也不报错，只是字没变。
    """
    src = _html()
    keys = _dict_keys(src, "EN")
    calls = {_unescape(m) for m in re.findall(r'\bT\(\s*"((?:[^"\\]|\\.)*)"', src)}
    missing = sorted(c for c in calls if c not in keys)
    assert not missing, f"这些 T() 没有英文词条：{missing}"


def test_every_html_segment_has_a_translation():
    """`data-i18n-html="key"` 与 `EN_HTML` 必须一一对应。

    缺一边的表现都是「那块提示整段不翻」：属性写错 → 找不到 key，原文照旧；
    词条多写 → 从来没人用。两边都算错。
    """
    src = _html()
    used = set(re.findall(r'data-i18n-html="([^"]+)"', src))
    defined = _dict_keys(src, "EN_HTML")
    assert used == defined, {
        "属性里用了但没定义": sorted(used - defined),
        "定义了但没人用": sorted(defined - used),
    }


def test_inline_script_parses_as_javascript():
    """整页脚本必须能过 `node --check`。

    这条是拿真金白银换的：往词典里加中文长句时，我照着 Python 的习惯写了两个相邻字符串
    （`"a" "b"`）当键——JS 不是这么拼的，整个 `<script>` 直接 `SyntaxError`。
    页面照样 200、HTML 照样完整，浏览器里只表现为「所有按钮都不动了」。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过语法检查（这只在开发机上装了）")
    src = _html()
    for i, block in enumerate(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", src)):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(block)
            path = fh.name
        try:
            r = subprocess.run([node, "--check", path], capture_output=True, text=True)
            assert r.returncode == 0, f"第 {i} 段脚本语法错误：{r.stderr[:800]}"
        finally:
            Path(path).unlink(missing_ok=True)

"""前端主题的守卫测试。

整套配色只有一个来源：`app/static/index.html` 里的两组 CSS 变量（深色 `:root` +
日间 `html[data-theme="light"]`）。这类「改一个数字、另一处忘了改」的问题在浏览器里
只表现为「某个字看不清」，测试看不见——所以把三条硬约束钉在测试里：

1. 用到的每个 `var(--x)` 都必须在 `:root` 里定义（打错一个字，颜色会静默失效）；
2. 日间必须覆盖掉底盘相关的几项（漏一项就是白底白字）；
3. 地图的取色必须来自 CSS 变量，不能又写回十六进制字面量（那样切主题地图不会变）。
"""

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "app" / "static" / "index.html").read_text("utf-8")

ROOT = re.search(r":root\{(.*?)\n\}", HTML, re.S)
LIGHT = re.search(r'html\[data-theme="light"\]\{(.*?)\n\}', HTML, re.S)


def vars_in(css: str) -> set[str]:
    """块里**定义**了哪些变量（`--x:` 开头才算，`var(--x)` 不算）。"""
    return set(re.findall(r"(--[\w-]+)\s*:", css))


def test_there_is_a_light_theme_block():
    assert ROOT, "找不到 :root 变量块"
    assert LIGHT, "找不到 html[data-theme=light] 变量块"


def test_every_used_variable_is_defined():
    defined = vars_in(ROOT.group(1))
    used = set(re.findall(r"var\((--[\w-]+)\)", HTML))
    missing = sorted(used - defined)
    assert not missing, f"这些变量被用了但没定义：{missing}"


def test_light_theme_covers_every_surface_colour():
    """底盘/文字/边框/地图底必须成组覆盖。

    只覆盖一半是最糟的形态：页面看起来「变得有点怪」，但不至于立刻发现是主题的问题。
    """
    dark, light = vars_in(ROOT.group(1)), vars_in(LIGHT.group(1))
    must = {"--bg", "--panel", "--panel2", "--line", "--text", "--muted",
            "--accent", "--map-bg", "--map-water", "--map-neutral"}
    assert must <= dark, f":root 少了：{sorted(must - dark)}"
    assert must <= light, f"日间主题少了：{sorted(must - light)}"
    extra = sorted(light - dark)
    assert not extra, f"日间定义了几个 :root 里没有的变量：{extra}"


def test_map_palette_comes_from_css():
    """地图填色必须读变量。

    `fillOf()` 把颜色写进 SVG 的 `fill` 属性，改主题不会自己更新——所以这里只要出现
    十六进制字面量，就等于「切到日间后地图还是夜间配色」。
    """
    body = re.search(r"function fillOf\(code\)\{(.*?)\n\}", HTML, re.S)
    assert body, "找不到 fillOf()"
    assert not re.search(r"#[0-9a-fA-F]{6}", body.group(1)), body.group(1)
    assert "--map-water" in body.group(1) or "palette()" in body.group(1)


def test_toggle_exists_and_theme_is_set_before_first_paint():
    assert 'id="btnTheme"' in HTML, "页头没有日夜模式开关"
    assert "dw_theme" in HTML, "没有记住用户的选择"
    assert "prefers-color-scheme" in HTML, "没有跟随系统"
    # 首帧前定主题：内联脚本必须排在 <style> 之前，否则夜间用户会先看到一片白
    head = HTML[: HTML.index("<style>")]
    assert "dataset.theme" in head, "主题脚本不在 <style> 之前，会出现白闪"

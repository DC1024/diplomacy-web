"""标准地图几何：省份轮廓 + 部队棋子的落点。

几何不自己画。引擎自带的 `diplomacy/maps/svg/standard.svg`（jDip Detailed Standard Map，
GPL）里每个省份都有一条 `<path id="_par">`，它的 `id` 就是引擎的地点代码；同一份文件里还有
`<text x y>PAR</text>` 标签，位置正是各家的视觉中心，可以顺手当部队棋子与补给中心圆点的锚点。

自己描图有两个坑：七十六个省份的边界要逐条对齐（偏一点就点不中），以及海岸分裂
（`SPA/NC`、`STP/SC`、`BUL/EC`）在图上只有**一个**形状——从现成文件里取，这两件事都不用操心。

解析一次就缓存；`/api/map` 只是把它吐出去，浏览器那边可以缓存一整天。
"""
from __future__ import annotations

import re
from pathlib import Path

import diplomacy.maps
from diplomacy import Map

# 引擎把标准图放在包内；wheel 里确实带了这个文件（见 tests/test_map.py 的自检）。
SVG_PATH = Path(diplomacy.maps.__file__).parent / "svg" / "standard.svg"

# `<path class="nopower" d="M ... z" id="_par"/>`：属性顺序不固定，先切出整个标签再各取所需。
_TAG = re.compile(r"<path\b[^>]*>", re.S)
_D = re.compile(r'\bd="([^"]+)"')
_ID = re.compile(r'\bid="_([a-z0-9]+)"')
# `<text x="649.4" y="924.2">SWI</text>`，也可能带 class 之类的前置属性。
# 一次捕获「属性串 + 三个字母」，别用 index() 去找标签位置——同一个标签可能出现多次。
_LABEL = re.compile(r"<text\b([^>]*)>\s*([A-Za-z]{3})\s*</text>")
_X = re.compile(r'\bx="([-\d.]+)"')
_Y = re.compile(r'\by="([-\d.]+)"')
# 有八个标签不给 x/y，改用 `transform="translate(175.1 1102.1) rotate(-60)"` 定位，
# 而 `x="0" y="0"` 是占位。只读 x/y 的话，POR、SKA、ROM 这些会全部堆到左上角原点去。
_TRANS = re.compile(r'translate\(\s*([-\d.]+)[\s,]+([-\d.]+)')
# 文件只用到 M / C / z 三种命令，坐标全是成对的数字，所以直接扫数字求包围盒是准的。
_NUM = re.compile(r"-?\d+\.?\d*")

# 描边宽度外扩一点，别让边缘省份的线条被裁掉
_MARGIN = 8

# 引擎的 `loc_type` 用 WATER 表示海域，前端只需区分三档。
_KIND = {"WATER": "water", "COAST": "coast", "LAND": "land"}

# 图上这五片海用的是 jDip 自己的缩写，和引擎代码不是一回事：
#   图上 GOL/MID/NAT/NRG/TYN  ↔  引擎 LYO/MAO/NAO/NWG/TYS
# 不做这层映射，这五块海就没有可点的形状（而且图上还没给它们文字标签）。
# 剩下七十一个省份两边代码一致。`tests/test_map.py` 会断言映射后既不缺也不多。
_ALIAS = {"GOL": "LYO", "MID": "MAO", "NAT": "NAO", "NRG": "NWG", "TYN": "TYS"}

_CACHE: dict | None = None


def _base(loc: str) -> str:
    """`SPA/NC` → `SPA`。图上只有基础省份一个形状，海岸只在命令字符串里出现。

    小写不能漏：引擎的 `loc_type` 里 `bul`/`spa`/`stp` 各有一份小写键（和 `BUL/EC` 并存），
    不统一大小写，这三个省份就会被判成「图上没有」。
    """
    return loc.split("/")[0].upper()


def _kinds(map_: Map) -> dict[str, str]:
    """基础省份 → land / coast / water。海岸分裂的省份以第一个海裤子地点为准。"""
    kinds: dict[str, str] = {}
    for loc, kind in map_.loc_type.items():
        kinds.setdefault(_base(loc), _KIND.get(kind, "land"))
    return kinds


def _outline(svg: str) -> dict[str, str]:
    shapes: dict[str, str] = {}
    for tag in _TAG.findall(svg):
        d, ident = _D.search(tag), _ID.search(tag)
        if d and ident:
            code = ident.group(1).upper()
            shapes[_ALIAS.get(code, code)] = d.group(1)
    return shapes


def _anchors(svg: str) -> dict[str, list[float]]:
    """标签位置，取**文字起点（基线）**——jDip 的标签是左对齐的，中心还要往右挪半个字宽。

    这个偏移留在前端算：字号是显示层的事，后端不该猜。旋转（`rotate(-60)`）不影响定位，
    只有一个标签专门转了角度，那个角度是为了让字顺着窄省份走，棋子照旧放在中点。
    """
    points: dict[str, list[float]] = {}
    for attrs, code in _LABEL.findall(svg):
        moved = _TRANS.search(attrs)
        if moved:
            points[code.upper()] = [float(moved.group(1)), float(moved.group(2))]
            continue
        x, y = _X.search(attrs), _Y.search(attrs)
        if x and y:
            points[code.upper()] = [float(x.group(1)), float(y.group(1))]
    return {_ALIAS.get(k, k): v for k, v in points.items()}


def _bounds(shapes: dict[str, str], anchors: dict[str, list[float]]) -> list[float]:
    """所有轮廓 + 所有标签位置的真实包围盒 → `[x, y, w, h]`。

    文件头写的是 `viewBox="0 0 1835 1360"`，但图形实际画在 x 202–2023、y 174–1527 之间：
    照文件头那个框取景，右边和下面各要切掉一百多像素（突尼斯、那不勒斯那一圈是活生生的
    可下命令省份），左边反而空着一片。所以按内容取景，别信声明。

    标签也要算进去——标签可以落在轮廓外面（葡萄牙那个标签就偏在海岸线左侧）。
    """
    xs: list[float] = []
    ys: list[float] = []
    for d in shapes.values():
        nums = [float(n) for n in _NUM.findall(d)]
        xs += nums[0::2]
        ys += nums[1::2]
    for point in anchors.values():
        # 标签画在基线**上方**、棋子是半径 22 的圆：只取基线点会让最上面那行的字被切掉，
        # 所以按「字宽约 48、字高约 26」把它占的地方一并算进画面。
        xs += [point[0], point[0] + 48]
        ys += [point[1] - 33, point[1] + 15]
    x0, y0 = min(xs) - _MARGIN, min(ys) - _MARGIN
    return [x0, y0, max(xs) + _MARGIN - x0, max(ys) + _MARGIN - y0]


def geometry() -> dict:
    """`{"viewBox": [x, y, w, h], "areas": {code: {d, at, kind, sc}}, "missing": [...]}`。

    `at` 是标签的**文字起点（基线）**，不是中心：jDip 的标签左对齐，画棋子时前端要往右
    补半个字宽、往上补半个字高。`missing` 是引擎认识、图上却没有形状的地点——留着给它自己
    看：哪天换了地图文件，前端会少画一块，而这里能立刻指出来是哪些。
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    svg = SVG_PATH.read_text(encoding="utf-8")
    shapes, anchors = _outline(svg), _anchors(svg)
    map_ = Map("standard")
    kinds = _kinds(map_)

    areas = {}
    for code, d in shapes.items():
        areas[code] = dict(
            d=d,
            at=anchors.get(code),
            kind=kinds.get(code, "land"),
            sc=code in map_.scs,
        )
    _CACHE = dict(
        viewBox=_bounds(shapes, anchors),
        source="diplomacy/maps/svg/standard.svg",
        areas=areas,
        missing=sorted(set(kinds) - set(shapes)),
    )
    return _CACHE

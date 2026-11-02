"""依赖声明守卫。

来由：`httpx` 一度只写在 `requirements-dev.txt` 里——本地测试全绿，镜像也能正常启动，
但第一次让模型说话就 `ModuleNotFoundError`。这类漂移在 CI 里唯一能提前发现的位置就是这里。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def imported_packages() -> set[str]:
    """app/ 下直接 import 的第三方顶层包名（不含标准库与自身）。"""
    found: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top and top not in sys.stdlib_module_names and top != "app":
                    found.add(top)
    return found


def declared_packages() -> set[str]:
    """requirements.txt 里声明的包名，去掉版本约束与 extras。"""
    names = set()
    for line in (ROOT / "requirements.txt").read_text("utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        name = line.split("[")[0]
        for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", ";"):
            name = name.split(sep)[0]
        names.add(name.strip().lower().replace("-", "_"))
    return names


def test_every_runtime_import_is_declared():
    missing = {p for p in imported_packages() if p.lower().replace("-", "_") not in declared_packages()}
    assert not missing, f"requirements.txt 缺少运行期依赖：{sorted(missing)}"


def test_no_unused_declaration():
    """反向：声明了却没 import，多半是删代码时忘了收尾。"""
    used = {p.lower().replace("-", "_") for p in imported_packages()}
    unused = declared_packages() - used
    assert not unused, f"requirements.txt 里有没被用到的包：{sorted(unused)}"

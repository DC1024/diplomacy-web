"""串行化存档写入。

内核的 `Arena.save()` **不持锁**：它先写 `state.tmp`，再 `os.replace(tmp, path)`。
两个线程同时走到这里（后台座位线程交令后保存 + 阶段时钟托管后保存）时，
先完成的那个已经把 `state.tmp` 改名走了，后一个就会 `FileNotFoundError`。

`Arena.lock` 是 `threading.RLock`，所以在锁里调 `save()` 既安全，也不会和
`act()` / `settle()` 里已经持锁的路径打架（可重入）。内核是 vendored 的，能不碰就不碰，
因此这一层放在外面而不是改 `arena.py`。
"""
from __future__ import annotations

from .engine.arena import Arena


def save(a: Arena) -> None:
    """持锁保存，避免两个线程抢同一个 `state.tmp`。"""
    with a.lock:
        a.save()

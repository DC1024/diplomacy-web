"""所有用例的公共前提：**本机单人部署**。

`app.main` 在 import 时读 `HOST` / `ADMIN_KEY` 决定管理面是否锁定（见 `admin_locked()`）：
监听在回环地址时，「没设密钥」是安全的，管理面照常可用。测试要的正是这个基线——
否则每个用例都得先 `pip install` 一个管理密钥才能建局，测试噪音会盖住真正要验的东西。

要验「对外可达又没密钥会怎样」的用例，自己在 fixture 里 `setenv("HOST", "0.0.0.0")`
再 reload 即可（见 `tests/test_roles.py`）——那才是这条规则的考场，不该让全部用例陪跑。
"""
import pytest


@pytest.fixture(autouse=True)
def local_single_player(monkeypatch):
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.delenv("ADMIN_KEY", raising=False)

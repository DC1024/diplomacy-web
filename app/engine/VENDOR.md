# Vendored 代码说明

`arena.py` 与 `simulate.py` 来自另一个项目，不是本仓库原创。原样引入是为了避免重写已经被实战验证过的
《外交》结算与鉴权逻辑。

| 项目 | 内容 |
|---|---|
| 来源仓库 | https://github.com/chen-006/meow-ai-arena |
| 来源路径 | `diplomacy/referee/arena.py`、`diplomacy/referee/simulate.py` |
| 基线提交 | `edf644a68d1b450708dac9554960af715541fbe6`（main） |
| 上游许可 | 仓库整体 MIT，但依赖 `diplomacy` 包（AGPL-3.0），回放地图来自 jDip（GPL） |

## 本仓库做的改动

1. `arena.py` 第 19 行：`from simulate import ...` → `from .simulate import ...`（改成包内相对导入）。
2. 无其他改动。业务逻辑、鉴权方式、错误文案全部保持原样，便于和上游对照。

## 为什么本仓库是 AGPL-3.0

本服务 `import diplomacy` 并对外提供网络服务。`diplomacy` 包是 AGPL-3.0-or-later，AGPL 第 13 条要求
通过网络提供服务时向用户提供对应源码。因此本仓库整体按 AGPL-3.0 发布——这是许可兼容性要求，不是选择。

## 升级方式

上游若更新 `arena.py`，直接覆盖 `app/engine/arena.py` 并重做上面第 1 条改动即可，勿改其他内容。
`tests/test_smoke.py` 会在覆盖后立刻暴露不兼容。
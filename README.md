# 七国纵横 · Diplomacy Web

标准七国《外交》（Diplomacy）的网页版。一条命令起服务，浏览器里打完整局。

> **B0 阶段**：单局热座模式。7 个座位全部由你（或围坐的几个人）轮流操作，规则、结算、私信保密、
> 命令校验全部走真裁判。LLM 玩家与多房间在后续版本。

**English TL;DR** — A self-hosted web version of standard 7-player Diplomacy. Docker one-liner,
single match, hot-seat mode, real referee (private messaging, simultaneous adjudication, order
validation). LLM seats and multi-room are on the roadmap.

---

## 快速开始

### Docker

```bash
docker run -d --name diplomacy -p 8080:8080 -v "$PWD/data:/data" \
  ghcr.io/dc1024/diplomacy-web:latest
```

打开 <http://localhost:8080>。

### Docker Compose

```bash
docker compose up -d
```

### 本地运行（Python 3.12）

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
.venv/bin/python -m app.main                  # Windows: .venv\Scripts\python
```

打开 <http://localhost:8080>。数据写在 `./data/state.json`，可以用 `DATA_DIR` 改位置。

---

## 怎么玩

1. 页面打开即自动开一局，`1901 年春` 开始。
2. 左侧点一个国家切换视角——**热座模式下你持有全部 7 个座位令牌**，能看到该国全部的私信。
3. 「简报」页是这个国家当前能看到的一切：局面、别人发来的信、本阶段要做什么、其他几家交了几份令。
4. 「发信」页发私信（300 字）或公开发言（400 字）。承诺没有强制力，允许欺骗。
5. 「军令」页每行一条命令，提交整份。格式不对会返回该部队的合法选项。
6. 所有需要行动的国家都交令后自动结算，进入下一阶段。

命令写法：

```text
A PAR - BUR            陆军移动
A MAR S A PAR - BUR    支援移动
F ENG C A LON - BRE    海运（陆军写 A LON - BRE VIA）
A PAR H                驻守
A BUR R PAR            撤退
A PAR B                增兵
```

完整规则见页面简报，或上游的 `workspace/RULES.md`。

> **未下令的部队不会自动驻守**——每支需要行动的部队都必须下令。这是原版规则，不是 bug。

### 想几个人一起玩

当前是热座：一台电脑轮流操作。要不泄漏私信，由一个人专门操作一个国家的窗口，或等 B1 的多座位令牌分发。

---

## 已实现 / 未实现

| 功能 | 状态 |
|---|---|
| 标准七国地图、1901–1920 完整规则、18 中心胜利、SoS 和局计分 | ✅ |
| 私信保密（服务端按座位过滤） | ✅ |
| 秘密交令、同时结算、结算门控 | ✅ |
| 命令合法性校验 + 合法选项提示 | ✅ |
| 幂等提交（`request_id`，前端重试不会双发） | ✅ |
| 收件箱 / 历史结算查询 | ✅ |
| 一键新局（旧存档自动备份） | ✅ |
| Docker / compose / 健康检查 | ✅ |
| 地图可视化 | ❌ 文字界面 |
| LLM 玩家 | ❌ 下一版 |
| 多房间 / 账号 / 观战 | ❌ 下一版 |
| 阶段时钟 / 超时托管 | ❌ 下一版 |

---

## 架构

```text
浏览器（单页，无构建步骤）
   │  X-Seat-Token
   ▼
FastAPI（app/main.py，薄壳：路由 + 鉴权 + 结算触发）
   │
   ▼
Arena（app/engine/arena.py，vendored）
   ├─ 鉴权、私信过滤、命令校验、幂等、回滚
   └─ diplomacy 1.1.2（结算引擎）
        └─ data/state.json（全量存档）
```

裁判内核来自 [meow-ai-arena](https://github.com/chen-006/meow-ai-arena) 的第 4 集，
是经过三局 105 阶段实战验证的实现。本仓库只加了 HTTP 壳和前端，没有改业务逻辑——
改动清单与升级方式见 [`app/engine/VENDOR.md`](app/engine/VENDOR.md)。

**必须单 worker**：`Arena` 是进程内单例，多 worker 会各自持有一份状态。`Dockerfile` 已固定。

---

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DATA_DIR` | `./data` | 存档目录（Docker 里是 `/data`） |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | 端口 |
| `LOG_LEVEL` | `info` | 日志级别 |

---

## 开发

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q
```

测试覆盖三件最容易坏的事：令牌鉴权、私信隔离、结算门控。改完裁判逻辑务必跑一遍。

CI 在每次 push 时跑 lint + 测试 + 构建镜像 + 容器冒烟。推 `v*` 标签会构建
`linux/amd64` 与 `linux/arm64` 双架构镜像并推到 GHCR。

---

## 路线

- **B0（当前）** 单局热座，文字界面
- **B1** LLM 座位：`function calling` 直连 + 滚动摘要控成本，一个人打 6 个模型
- **B2** 多座位令牌分发、阶段时钟、掉线自动驻守
- **C** 地图可视化、多房间、复盘页

设计取舍与成本测算见上游仓库的可行性方案。

---

## 许可

**AGPL-3.0**。不是选择，是兼容性要求：本服务 `import diplomacy` 并对外提供网络服务，
而 `diplomacy` 包是 AGPL-3.0-or-later，AGPL 第 13 条要求向网络用户提供对应源码。

《Diplomacy》是 Avalon Hill / Hasbro 的商标，本项目与其无关。规则本身不受版权保护，
但官方地图美术有版权——本项目不附带官方地图素材。

## 致谢

- 裁判内核、规则文本与实战数据：[chen-006/meow-ai-arena](https://github.com/chen-006/meow-ai-arena)
- 结算引擎：[diplomacy](https://github.com/diplomacy/diplomacy)（AGPL-3.0）
- 原始棋盘设计：Allan B. Calhamer, 1959
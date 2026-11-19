# 每日 AI 新闻助手 Agent

一个「按个人口味筛选 AI 新闻」的 Agent 项目。当前完成**第一阶段（用户订阅管理）**、
**第二阶段（Agent 核心与 Function Calling 工具调用）**、
**第三阶段（新闻采集、个性化筛选与简报生成）**与
**第四阶段（定时任务、邮件推送与历史简报管理）**——整条链路已经可以长期跑：
定时触发 → Agent 自主搜索 → 个性化筛选 → 生成简报 → 入库 → 邮件推送 → 前端查看历史。

> 技术栈：Python 3.11+ · FastAPI · SQLAlchemy 2.0 · SQLite · APScheduler · SMTP · 原生 HTML/CSS/JS（无框架、无构建）· OpenAI 兼容 Function Calling · RSS/Atom（零第三方解析库）

---

## 一、当前阶段能力

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 项目分层框架 | ✅ | 接口 / 模型 / 数据访问 / 业务服务 / 前端静态资源 / 配置 分离 |
| 用户信息管理 | ✅ | 创建、查询、修改（含列表辅助接口）、推送开关 |
| 订阅偏好管理 | ✅ | 关注话题、关键词、排除关键词，支持多个词条，整体覆盖保存 |
| 前端配置页 | ✅ | 标签式输入、预设领域、表单校验、刷新后回填 |
| **五个基础工具** | ✅ | list_dir / read_file / search_content / write_file / bash，统一返回格式 + 参数校验 + 异常兜底 |
| **工具注册中心** | ✅ | 装饰器注册、JSON Schema 自动生成、按名动态执行，主循环零 if-else |
| **手写 Agent Loop** | ✅ | 多轮 Function Calling：模型决策 → 执行工具 → 结果回填 → 继续决策 → 自主停止 |
| **安全与护栏** | ✅ | 工作区路径约束、危险命令黑名单、执行超时、输出截断、轮数/次数上限、重复调用提醒 |
| **新闻业务工具** | ✅ | search_news / fetch_article / get_user_preferences / save_briefing，与文件工具同一注册表 |
| **RSS 采集层** | ✅ | 10 个中英文源、并发抓取、单源失败不影响整体、RSS 2.0 / Atom / RDF 全格式 |
| **个性化筛选** | ✅ | 偏好读库 → 语义判断交给模型；时间窗口、排除词、跨调用去重、标题近似去重由工具兜底 |
| **简报生成与入库** | ✅ | 结构化 Markdown 校验（章节/条目/链接白名单）+ `news_briefings` 表按「用户+日期」去重 |
| **定时任务** | ✅ | APScheduler 每日固定时间/时区触发，遍历全部用户分别派发，单用户失败隔离，重启后重新注册 + 启动补跑 |
| **自愈能力** | ✅ | 僵尸任务回收作业：心跳过期 → 标记失败并当天重派；推送失败按指数退避自动重试 |
| **任务状态机与幂等** | ✅ | `generation_tasks`（唯一键 user+date）、条件 UPDATE 领取、心跳防僵尸、失败可重跑、成功不重复 |
| **邮件推送** | ✅ | SMTP（SSL/STARTTLS/明文），标题 + 摘要 + 简报入口 + 原始链接；成功/失败/跳过三类状态落库 |
| **推送重试** | ✅ | 指数退避 + 定时扫描 + 手动重试；重发不重新跑 Agent，`UNIQUE(task_id, channel)` 防重复推送 |
| **历史简报管理** | ✅ | 首页概览、历史列表（日期/标题/条数/生成状态/推送状态）、详情（Markdown 安全渲染）、手动生成 |
| 自动化测试 | ✅ | 186 个用例（19 接口 + 54 工具/Loop + 26 新闻能力层 + 21 新闻工具 + 8 简报链路 + 58 第四阶段） |

---

## 二、整体架构与目录结构

```
                  ┌───────────────────────── 触发层 ─────────────────────────┐
                  │  APScheduler 每日定时      前端「立即生成」    启动补跑    │
                  │  （scheduler/jobs.py）   （api/v1/tasks.py） （catch-up）  │
                  └───────────────┬─────────────────────────────────────────┘
                                  │ ① 幂等登记：UNIQUE(user_id, briefing_date)
                                  ▼
                  ┌──────── 任务状态机（services/task_service.py）────────────┐
                  │ pending ─claim()→ running ─→ success / failed / skipped  │
                  │ claim 用「条件 UPDATE」实现乐观锁；心跳区分在跑 / 僵尸      │
                  └───────────────┬─────────────────────────────────────────┘
                                  │ ② 入队（services/generation_worker.py，并发上限）
                                  ▼
                  ┌──────── 生成编排（services/generation_service.py）───────┐
                  │ 读用户画像 → run_briefing() → 记录状态 → 触发推送         │
                  └───────────────┬─────────────────────────────────────────┘
                                  │ ③ 完全复用第三阶段
                                  ▼
   ┌──────────── Agent 内核（app/agent）────────────┐   ┌───── 新闻能力层（app/news）─────┐
   │ AgentRunner：发请求 → 解析 → 执行 → 回填       │──▶│ RSS 抓取 / 正文抽取 / 去重 /     │
   │ 工具注册中心（9 个工具）/ 护栏 / 轨迹           │   │ 时间过滤 / 关键词打分 / 简报校验  │
   └───────────────┬───────────────────────────────┘   └─────────────────────────────────┘
                   │ ④ save_briefing 写库 + 落盘（一次调用完成）
                   ▼
   ┌──────── 存储（SQLite）────────┐         ┌──── 推送（app/push）────┐
   │ users / subscriptions /       │────────▶│ 邮件模板 → SMTP → 状态   │
   │ news_briefings / tasks / push │         │ 失败退避 → 定时重试      │
   └───────────────────────────────┘         └─────────────────────────┘
                   │
                   ▼
   ┌──── 前端（app/static，hash 路由）────┐
   │ 概览 / 历史列表 / 简报详情 / 订阅配置 │
   └──────────────────────────────────────┘
```

**为什么要分这么多层**：调度器只回答「什么时候做」，任务状态机只回答「谁可以做、做完怎么记」，
生成编排只回答「用什么流程做」，推送只回答「发不发、发没发成功」。
这样任何一环出问题都只影响自己——邮件发不出去不会把已入库的简报回滚，
某个用户的画像读失败也不会让整批定时任务中断。


### 目录结构

```
NewsAgent/
├── app/
│   ├── main.py                 # 应用入口：lifespan（建表/补列/补建任务/启动 worker 与调度器）
│   ├── core/                   # 基础设施
│   │   ├── config.py           # 配置加载（.env + pydantic-settings），含 Agent / 调度 / 推送参数
│   │   ├── database.py         # 引擎 / 会话 / Base / get_db / init_db
│   │   ├── migrations.py       # ★ 极简补列迁移（create_all 只建表不改表）
│   │   ├── response.py         # 统一响应信封 {code, message, data}
│   │   └── exceptions.py       # 业务异常 + 全局异常处理器
│   ├── models/                 # ORM 模型（数据库表的 Python 映射）
│   │   ├── base.py             # 主键与时间戳 Mixin
│   │   ├── user.py             # 用户表（含 push_enabled）
│   │   ├── subscription.py     # 订阅头表 + 词条明细表
│   │   ├── briefing.py         # 新闻简报表（user_id + briefing_date 唯一）
│   │   ├── task.py             # ★ 任务执行记录（user_id + briefing_date 唯一）
│   │   └── push.py             # ★ 推送记录（task_id + channel 唯一）
│   ├── schemas/                # Pydantic 请求/响应模型（接口契约 + 参数校验）
│   │   ├── common.py           # 健康检查等通用结构
│   │   ├── user.py             # 用户相关
│   │   ├── subscription.py     # 订阅偏好相关（含词条清洗逻辑）
│   │   ├── briefing.py         # 简报生成/查询契约
│   │   ├── task.py             # ★ 任务 / 概览 / 触发 / 调度状态契约
│   │   └── push.py             # ★ 推送记录与推送设置契约
│   ├── crud/                   # 数据访问层：只做数据库读写，不提交事务
│   │   ├── user.py
│   │   ├── subscription.py
│   │   ├── briefing.py
│   │   ├── task.py             # ★ 含 claim()：条件 UPDATE 抢执行权
│   │   └── push.py             # ★ 含 list_retryable()：扫描到期重试
│   ├── services/               # 业务服务层：事务边界 + 业务规则
│   │   ├── user_service.py
│   │   ├── subscription_service.py
│   │   ├── briefing_service.py # 用户画像 / 任务书 / 简报 upsert
│   │   ├── task_service.py     # ★ 任务状态机（幂等登记 / 领取 / 成功 / 失败 / 跳过 / 补建）
│   │   ├── generation_service.py # ★ 一次任务的完整执行（复用 run_briefing + 触发推送）
│   │   ├── generation_worker.py  # ★ 后台 worker 池（并发上限 + 队列去重 + 线程兜底）
│   │   ├── push_service.py     # ★ 推送编排（跳过判断 / 发送 / 退避重试 / 批量扫描）
│   │   └── dashboard_service.py  # ★ 首页概览聚合（一次请求拿全）
│   ├── news/                   # 第三阶段：新闻能力层（与 Agent 解耦）
│   │   ├── feeds.py            # 源目录（ID / 名称 / 语种 / 分类）
│   │   ├── http.py             # 抓取：UA 伪装、字节上限、错误归一
│   │   ├── rss.py              # RSS 2.0 / Atom / RDF 解析 + 时间归一 + 文本清洗
│   │   ├── article.py          # 正文抽取：去噪 → 段落 → 密度兜底（full/excerpt/summary）
│   │   ├── dedupe.py           # URL 规范化 + 标题归一 + 二元组预筛 + 相似度
│   │   ├── filter.py           # 时间窗口 / 关键词匹配 / 相关度打分 / 排除词
│   │   ├── providers.py        # 搜索提供方协议 + RSS 实现 + 扩展注册点
│   │   ├── state.py            # 一次运行内的候选池（跨调用去重 + 链接白名单）
│   │   └── briefing.py         # 简报结构校验（章节 / 条目 / 链接 / 截断检测）
│   ├── agent/                  # 第二阶段：Agent 核心
│   │   ├── registry.py         # 工具注册中心：@tool 装饰器 / Schema 生成 / 动态执行
│   │   ├── result.py           # ToolResult 统一返回格式（ok/summary/data/error/meta）
│   │   ├── context.py          # ToolContext：工作区 + 限额 + 用户/新闻状态/会话工厂
│   │   ├── safety.py           # 路径约束 / 危险命令拦截 / 环境变量清洗
│   │   ├── prompts.py          # System Prompt（通用 + 简报专用，只写原则不写步骤）
│   │   ├── loop.py             # AgentRunner：手写 Function Calling 循环
│   │   ├── briefing_task.py    # 简报编排：任务书 → 上下文 → 跑循环 → 回读结果
│   │   ├── tracer.py           # 运行记录与内存环形缓冲
│   │   ├── schemas.py          # Agent 接口的请求/响应模型
│   │   ├── tools/
│   │   │   ├── filesystem.py   # list_dir / read_file / search_content / write_file
│   │   │   ├── shell.py        # bash
│   │   │   └── news.py         # search_news / fetch_article / get_user_preferences / save_briefing
│   │   └── llm/
│   │       ├── base.py         # LLMClient 协议 + ToolCall / LLMResponse / LLMError
│   │       ├── openai_client.py# 任意 OpenAI 兼容网关（httpx 直连 + 重试）
│   │       ├── mock_client.py  # 仅用于离线验证链路的规则化假模型
│   │       ├── mock_news.py    # 简报场景的离线策略（状态机 + 模板，非模型能力）
│   │       └── __init__.py     # build_llm_client：auto / openai / mock
│   ├── scheduler/              # ★ 第四阶段：定时任务
│   │   ├── manager.py          # APScheduler 封装（注册 / 启动 / 停止 / 状态）
│   │   └── jobs.py             # 作业本体：批量派发 / 启动补跑 / 推送重试
│   ├── push/                   # ★ 第四阶段：邮件推送
│   │   ├── email.py            # SMTP 发信（SSL / STARTTLS / 明文 + 错误翻译）
│   │   ├── template.py         # 邮件模板（标题 / 摘要 / 入口 / 原始链接）
│   │   ├── markdown.py         # Markdown → HTML / 纯文本（先转义再插标签）
│   │   └── local_sink.py       # 本地假 SMTP（仅用于测试与本地验证）
│   ├── api/                    # 接口层
│   │   ├── agent.py            # /api/agent/run、/briefings、/tools、/runs
│   │   ├── deps.py             # 公共依赖（会话、user_id → User）
│   │   └── v1/
│   │       ├── router.py       # v1 路由聚合
│   │       ├── users.py        # /api/v1/users
│   │       ├── subscriptions.py# /api/v1/users/{id}/preferences
│   │       ├── briefings.py    # /api/v1/briefings（列表 / 详情）
│   │       ├── tasks.py        # ★ 概览 / 触发 / 任务列表 / 任务详情 / 重试
│   │       ├── push.py         # ★ 推送设置 / 推送历史 / 重试推送
│   │       └── scheduler.py    # ★ 调度状态 / 立即派发
│   └── static/                 # 前端静态资源（由 FastAPI 直接托管）
│       ├── index.html          # 四个视图（概览 / 历史 / 详情 / 配置）
│       ├── favicon.svg
│       ├── css/style.css
│       └── js/app.js           # hash 路由 + 轮询状态机 + Markdown 渲染
├── workspace/                  # Agent 沙箱：所有文件工具与命令的边界
│   ├── README.md
│   ├── briefings/              # 生成的简报 Markdown（按「日期-userID.md」命名）
│   ├── notes/{agent_design.md, todo.md}
│   ├── src/{agent_runner.py, news_fetcher.py, utils.py}
│   └── data/sample_news.json
├── tests/                      # pytest 测试（182 个用例，全部离线）
│   ├── conftest.py             # 临时库夹具 + 会话工厂替换 + 离线新闻网络夹具
│   ├── test_api.py             # 第一阶段接口
│   ├── test_tools.py           # 五个工具 + 注册中心 + 安全用例
│   ├── test_agent_loop.py      # 多轮循环、顺序自由、异常与护栏
│   ├── test_agent_api.py       # /api/agent/* 接口
│   ├── test_news_core.py       # RSS/正文/去重/时间/简报校验 单元测试
│   ├── test_news_tools.py      # 四个新闻工具 + 异常场景
│   ├── test_briefing_flow.py   # 双用户端到端 + 同日报简报去重
│   ├── test_stage4_task_state.py # ★ 幂等 / 并发领取 / 僵尸恢复 / 失败重跑 / 补建
│   ├── test_stage4_push.py     # ★ 邮件模板 / 本地 SMTP 实投 / 跳过分支 / 重试
│   ├── test_stage4_scheduler.py# ★ 遍历用户 / 单用户隔离 / 补跑 / 作业注册
│   └── test_stage4_api.py      # ★ 概览 / 触发 / 列表 / 详情 / 推送开关 / 全链路
├── scripts/
│   ├── e2e_check.py            # 可选：Playwright 驱动的前端自动化冒烟测试
│   ├── ui_check_stage4.py      # ★ 第四阶段前端验收（概览→生成→详情→历史→开关）
│   ├── send_test_mail.py       # ★ 真实 SMTP 发信测试（连通性 / 完整推送链路 / 内容预览）
│   ├── agent_demo.py           # 三组多步任务的命令行演示
│   └── briefing_demo.py        # 两组用户生成简报的命令行演示
├── data/                       # SQLite 数据库文件目录（自动创建，已 gitignore）
│   └── screenshots/stage4/     # 前端验收截图
├── requirements.txt
├── .env.example                # 环境变量模板
├── .gitignore
├── pytest.ini
└── README.md
```

### 分层职责与调用方向

```
HTTP 请求 / 定时触发
   │
   ▼
api/v1/*.py        接口层：定义路由、注入依赖、组织响应（不写业务逻辑、不碰 SQL）
scheduler/jobs.py  调度层：只负责「什么时候触发」，触发后调用业务层
   │
   ▼
services/*.py      业务层：业务校验、事务提交/回滚、编排多个数据操作
   │
   ▼
crud/*.py          数据访问层：纯数据库读写（select/add/flush），不 commit
   │
   ▼
models/*.py        模型层：表结构、关系、约束
```

**为什么这样分层**：接口层薄、业务层厚，是为了让「改业务规则」只需动一处；
crud 不提交事务，是为了让「一次请求一个事务」的边界只存在于 service 层，避免半途失败留下脏数据。

---

## 三、快速开始

### 1. 准备环境

> ⚠️ 下面的命令请**一行一行执行**。整段复制粘贴进终端时，PowerShell 会进入 `>>`
> 续行模式，把多行拼成一条命令，从而报出 `ments.txt#`、`source` 之类的“幽灵命令”错误。

**第一步：确认解释器**（Windows 上很容易选错，务必先做）

```powershell
python -c "import sysconfig; print(sysconfig.get_platform())"
```

| 输出 | 含义 |
| --- | --- |
| `win-amd64` | 官方 CPython，✅ 可以继续 |
| `mingw_x86_64_ucrt` | MSYS2 自带的 Python，❌ **不要用来跑本项目** |

MSYS2 的 Python 只认 `mingw` 平台的 wheel，装 `numpy` / `pandas` / `uvicorn[standard]`
这类带 C 扩展的包时，pip 会退回源码编译并报 `FileNotFoundError [WinError 2]`。
请改用 python.org 安装的 CPython（3.11 / 3.12 均可）。

**第二步：创建并激活虚拟环境**

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

> 若提示「无法加载文件 …Activate.ps1，因为在此系统上禁止运行脚本」，先执行一次：
> `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`

Windows Git Bash：

```bash
python -m venv .venv
source .venv/Scripts/activate
```

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
```

激活成功的标志：命令行提示符最前面出现 `(.venv)`。

**第三步：安装依赖**

```bash
python -m pip install -r requirements.txt
```

> 用 `python -m pip` 而不是直接 `pip`，能确保装进**当前激活的这个**环境，
> 避免 PATH 里其他 Python 的 pip 抢先命中。

### 2. 配置环境变量

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

Windows Git Bash / macOS / Linux：

```bash
cp .env.example .env
```

`.env` 中需要关注四组配置：

1. `DATABASE_URL`（默认 `sqlite:///./data/news_agent.db`，相对路径会以**项目根目录**为基准解析）；
2. Agent 相关——**没有 API Key 也能跑**：`AGENT_MODE=auto` 时若无 `LLM_API_KEY` 会自动降级到
   Mock 模式（仅验证工具注册与调用链路，不代表模型决策能力）。想跑真实模型就填
   `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`（任意 OpenAI 兼容网关均可）；
3. 定时任务——`SCHEDULER_ENABLED` / `SCHEDULER_TIME` / `SCHEDULER_TIMEZONE`，详见 §十二；
4. 邮件推送——`SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_SENDER`，
   **没配 SMTP 也不影响生成与页面查看**，推送会记为 `skipped` 并说明原因。

代码中没有任何硬编码密钥，密钥只从环境变量读取。

### 3. 启动服务

以下命令需在**已激活虚拟环境**的终端中执行（提示符带 `(.venv)`）：

```bash
python -m uvicorn app.main:app --reload --port 8000
```

启动日志会明确告诉你三件事是否就绪：

```text
生成任务 worker 已启动：并发 2
定时任务已启动：每天 08:00（Asia/Shanghai），推送重试每 10 分钟，并发上限 2
定时任务 | 启用=True 时间=08:00 时区=Asia/Shanghai 并发=2 | 邮件推送=启用（SMTP 已配置）
```

启动后：

| 地址 | 用途 |
| --- | --- |
| http://127.0.0.1:8000/ | 前端首页（概览 / 历史简报 / 订阅配置） |
| http://127.0.0.1:8000/docs | Swagger 交互式接口文档 |
| http://127.0.0.1:8000/redoc | ReDoc 文档 |
| http://127.0.0.1:8000/api/health | 健康检查 |
| http://127.0.0.1:8000/api/v1/scheduler | 定时任务状态（下次执行时间） |

> 首次启动会自动创建 `data/news_agent.db` 及全部数据表；**已有的老库会被自动补列**
> （`users.push_enabled`），并给第三阶段的历史简报补建任务记录，无需手动迁移。
> 若 8000 端口被占用，换一个端口即可（如 `--port 8010`），前端用的是相对路径，无需改代码。

> ⚠️ **多进程部署注意**：`--workers N` 会起 N 个调度器，同一时刻触发 N 次派发。
> 数据库唯一键能挡住重复简报，但会浪费 N 倍模型调用。正确做法是让**只有一个进程**开
> `SCHEDULER_ENABLED=true`，其余设 `false`；或者另写一个只启动
> `scheduler_manager.start()` + `generation_worker.start()` 的单进程入口（作业函数本身不依赖 Web 层）。

### 4. 运行测试

```bash
python -m pytest -v            # 186 个用例，全部离线（不打网络、不连真实 SMTP）
python -m pytest tests/test_stage4_api.py -v   # 只跑第四阶段
```

测试使用临时目录中的独立 SQLite 文件，并把全局 `SessionLocal` 指向该临时库，
因此后台任务 / 推送 / 调度作业也不会写到开发库。
邮件测试使用 `app/push/local_sink.py` 起的**本地真 SMTP 服务器**（随机端口），
smtplib 真的走一遍 EHLO / MAIL FROM / RCPT TO / DATA，只把最后一跳换成本地进程。

---

## 四、数据库设计

### 4.1 users（用户表）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | INTEGER | PK, 自增 | 用户 ID |
| nickname | VARCHAR(50) | NOT NULL, 索引 | 昵称 |
| identity | VARCHAR(200) | NOT NULL | 身份 / 职业描述 |
| email | VARCHAR(255) | NULL, 索引 | 新闻接收邮箱（选填） |
| **push_enabled** | BOOLEAN | NOT NULL, 默认 1 | 是否接收邮件推送（用户级开关，第四阶段新增） |
| created_at | DATETIME | NOT NULL | 创建时间 |
| updated_at | DATETIME | NOT NULL | 更新时间 |

### 4.2 subscriptions（订阅头表，与用户 1:1）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | INTEGER | PK | 订阅 ID |
| user_id | INTEGER | FK → users.id, UNIQUE, 级联删除 | 所属用户 |
| created_at | DATETIME | NOT NULL | 订阅创建时间 |
| updated_at | DATETIME | NOT NULL | 订阅最后更新时间 |

### 4.3 subscription_terms（订阅词条，1:N）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | INTEGER | PK | 词条 ID |
| subscription_id | INTEGER | FK → subscriptions.id, 级联删除 | 所属订阅 |
| term_type | VARCHAR(20) | CHECK ∈ (topic / keyword / exclude_keyword) | 词条类型 |
| term_value | VARCHAR(64) | NOT NULL | 词条内容 |
| created_at / updated_at | DATETIME | NOT NULL | 时间戳 |

唯一约束：`(subscription_id, term_type, term_value)`；联合索引：`(subscription_id, term_type)`。

### 4.4 为什么「一个用户多个话题/关键词」要拆成两张表

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| 用户表里放 `topics`、`keywords` 三个逗号拼接字符串 | 表少、实现快 | 无法按词条精确检索/统计；长度受限；增删要整串重写；后续按话题匹配新闻只能全表取出在内存里 split |
| **词条明细表（本方案）** | 任意多个词条；可索引、可去重、可聚合；与用户/订阅关系清晰 | 多一次 join（本地 SQLite 几乎无感） |
| SQLite JSON 字段 | 结构灵活、单表搞定 | 跨库迁移差、无法建索引、聚合查询要写 JSON 函数 |

结论：用「订阅头表 + 词条明细表」，后续「按话题匹配新闻」「统计话题热度」都能直接用 SQL 完成。

### 4.5 news_briefings（简报，`user_id + briefing_date` 唯一）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | 简报 ID |
| user_id | FK → users.id，CASCADE | 用户 ID |
| briefing_date | DATE | 简报日期（自然日） |
| title | VARCHAR(200) | 简报标题 |
| content | TEXT | Markdown 全文（以库为准，文件只是导出物） |
| news_count | INTEGER | 条目数 |
| generated_at | DATETIME | 生成时间（重新生成会刷新） |
| focus_topics | JSON | 生成时的关注方向快照 |
| file_path | VARCHAR(255) | 导出 Markdown 在工作区内的相对路径 |
| run_id | VARCHAR(32) | 对应 Agent 运行 ID，可回溯轨迹 |

### 4.6 generation_tasks（任务执行记录，`user_id + briefing_date` 唯一）

这是第四阶段的**幂等落点**：一个用户一天最多一条任务，于是「先查后写」的并发穿透问题消失了。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | 任务 ID |
| user_id | FK → users.id，CASCADE | 所属用户 |
| briefing_date | DATE | 任务归属日期 |
| trigger_type | VARCHAR(20) | manual / scheduled / catch_up / retry |
| status | VARCHAR(20) | pending / running / success / failed / skipped |
| attempts | INTEGER | 被领取执行过几次 |
| started_at / finished_at | DATETIME | 开始 / 结束时间 |
| heartbeat_at | DATETIME | 心跳：区分「真的在跑」与「进程崩溃遗留」 |
| error_message | TEXT | 失败 / 跳过原因 |
| warnings | JSON | 质量提示（未产出简报、条数为 0 等） |
| briefing_id | FK → news_briefings.id，SET NULL | 生成成功时对应的简报 |
| run_id | VARCHAR(32) | 对应 Agent 运行 ID |

唯一约束 `uq_task_user_date(user_id, briefing_date)`；索引 `(status, heartbeat_at)` 供僵尸任务扫描。

### 4.7 push_records（推送记录，`task_id + channel` 唯一）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | 推送 ID |
| task_id | FK → generation_tasks.id，CASCADE | 关联任务 |
| user_id | FK → users.id | 收件用户 |
| briefing_id | FK → news_briefings.id，SET NULL | 推送的简报 |
| channel | VARCHAR(20) | 当前只有 email |
| target | VARCHAR(255) | 收件地址 |
| subject | VARCHAR(255) | 邮件主题 |
| status | VARCHAR(20) | pending / success / failed / skipped / disabled |
| attempts | INTEGER | 已尝试次数（重试累加，不新增行） |
| last_error | TEXT | 最后一次失败原因（人话，如「认证失败，请检查 SMTP_USER」） |
| sent_at | DATETIME | 投递成功时间 |
| next_retry_at | DATETIME | 下次允许重试时间；NULL 表示不再自动重试 |
| message_id | VARCHAR(255) | SMTP 返回的 Message-ID |

唯一约束 `uq_push_task_channel(task_id, channel)` —— 这就是「不会重复推送」的物理保证；
索引 `(status, next_retry_at)` 供重试扫描使用。

### 4.8 三张表为什么不能合并

| 方案 | 问题 |
| --- | --- |
| 只留 `news_briefings`，用字段表示状态 | 「今天失败了」没有简报行，历史列表就少了这一天；用户看不到「生成失败」这件事 |
| 把推送状态塞进任务表 | 重试次数、下次重试时间、message_id 与生成无关；一条任务将来可能多渠道（邮件 / 企微），塞进去会变成一个宽表 |
| 用内存字典记「今天做过」 | 多进程 / 重启即失效，并发下必穿透 |

三张表分别对应三个生命周期：**任务**（谁在什么时候做、做到哪一步）、**简报**（产物）、
**推送**（是否送达）。状态各自独立，页面上也就能分别展示「生成成功 + 推送失败」。

---

## 五、接口说明

统一响应格式（所有接口，含错误）：

```json
{ "code": 0, "message": "success", "data": { } }
```

`code = 0` 表示成功；失败时 `code` 与 HTTP 状态码一致（400 / 404 / 409 / 422 / 500）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/users` | 创建用户 |
| GET | `/api/v1/users` | 用户列表（辅助，便于演示时切换用户） |
| GET | `/api/v1/users/{user_id}` | 获取用户信息（含 `push_enabled`） |
| PUT | `/api/v1/users/{user_id}` | 修改用户信息（局部更新） |
| PUT | `/api/v1/users/{user_id}/preferences` | 创建或更新订阅偏好（整体覆盖） |
| GET | `/api/v1/users/{user_id}/preferences` | 获取订阅偏好（未设置时 `data = null`） |
| GET | `/api/v1/users/{user_id}/dashboard` | ★ 首页概览（一次拿全） |
| POST | `/api/v1/users/{user_id}/briefings/trigger` | ★ 立即生成简报（异步，返回任务） |
| GET | `/api/v1/users/{user_id}/briefings/{briefing_id}` | ★ 按用户取简报详情（带归属校验） |
| GET | `/api/v1/tasks?user_id=` | ★ 历史任务列表（含简报 + 推送状态） |
| GET | `/api/v1/tasks/{task_id}` | ★ 任务详情（前端轮询用） |
| POST | `/api/v1/tasks/{task_id}/retry` | ★ 重新执行失败/跳过的任务 |
| GET | `/api/v1/users/{user_id}/push-settings` | ★ 推送开关与「能不能真发出去」 |
| PUT | `/api/v1/users/{user_id}/push-settings` | ★ 打开 / 关闭邮件推送 |
| GET | `/api/v1/users/{user_id}/push-records` | ★ 推送历史 |
| POST | `/api/v1/push-records/{push_id}/retry` | ★ 重试推送（不重新生成） |
| GET | `/api/v1/scheduler` | ★ 定时任务状态与下次执行时间 |
| POST | `/api/v1/scheduler/run-now` | ★ 立即执行一次批量派发 |
| POST | `/api/v1/scheduler/recover` | ★ 立即回收僵尸任务并重派 |
| GET | `/api/v1/briefings?user_id=` | 简报列表（第三阶段） |
| GET | `/api/v1/briefings/{briefing_id}` | 简报详情（第三阶段） |
| POST | `/api/agent/briefings` | 同步生成简报（第三阶段，调试用） |
| POST | `/api/agent/run` | 通用 Agent 任务 |
| GET | `/api/agent/tools` | 已注册工具与 Schema |
| GET | `/api/health` | 健康检查 |

### 关键校验规则

| 字段 | 规则 |
| --- | --- |
| nickname | 必填，1 ~ 50 字符，自动去首尾空格 |
| identity | 必填，1 ~ 200 字符 |
| email | 选填；格式校验；传空字符串视为未填写；`PUT` 传 `null` 表示清空 |
| topics | 最多 20 个；单个词条 ≤ 30 字符 |
| keywords / exclude_keywords | 各最多 50 个；单个词条 ≤ 30 字符 |
| 词条清洗 | 去首尾空格 → 丢弃空值 → 忽略大小写的顺序去重 → 超长截断 |

> 说明：`PUT .../preferences` 是**全量覆盖**语义，未传的数组按空处理。
> 若整个字段传字符串（如 `"topics": "大模型,Agent"`），服务端会按中英文逗号、分号、顿号拆分，方便 Postman 调试。

### curl 示例

```bash
BASE=http://127.0.0.1:8000/api/v1

# 1. 创建用户
curl -X POST $BASE/users \
  -H "Content-Type: application/json" \
  -d '{"nickname":"小林","identity":"后端开发实习生","email":"xiaolin@example.com"}'

# 2. 修改用户（只改昵称，email 传 null 清空）
curl -X PUT $BASE/users/1 \
  -H "Content-Type: application/json" \
  -d '{"nickname":"林同学","email":null}'

# 3. 保存订阅偏好
curl -X PUT $BASE/users/1/preferences \
  -H "Content-Type: application/json" \
  -d '{"topics":["大模型 / LLM","AI Agent"],"keywords":["RAG","微调"],"exclude_keywords":["招聘"]}'

# 4. 查询订阅偏好
curl $BASE/users/1/preferences
```

---

## 六、前端使用说明

页面地址：`http://127.0.0.1:8000/`，四个视图用 hash 路由切换（无框架、无构建、可离线打开）：

| 视图 | 路由 | 内容 |
| --- | --- | --- |
| 概览 | `#/` | 用户信息、当前订阅偏好、最近一次简报、最近一次推送、定时任务状态、立即生成入口 |
| 历史简报 | `#/briefings` | 日期 / 标题 / 新闻数 / 生成状态 / 推送状态 / 操作（查看、失败重试） |
| 简报详情 | `#/briefing/{id}` | 完整 Markdown 渲染、元信息、推送状态与「重新推送」、重新生成 |
| 订阅配置 | `#/settings` | 第一阶段的表单（昵称 / 身份 / 邮箱 / 三组标签） |

### 6.1 手动生成的状态机

点「立即生成简报」后不会原地等 3 分钟，而是：

```
点击 → POST /users/{id}/briefings/trigger 立即返回 task（pending）
     → 每 2 秒 GET /tasks/{id}
     → 等待执行（pending）→ 正在生成（running）→ 生成成功 / 生成失败 / 已跳过
     → 自动刷新概览的最近简报与推送状态
```

| 细节 | 做法 | 为什么 |
| --- | --- | --- |
| 防重复点击 | 请求期间 + 轮询期间按钮禁用，文案变「生成中…」 | 用户不会看到「点了没反应」而反复点 |
| 服务端兜底 | `UNIQUE(user_id, briefing_date)` + 条件 UPDATE | 前端禁用只是体验，重复的最终防线在数据库 |
| 刷新不丢进度 | 任务 ID 存 `localStorage`（`news_agent_active_task`），页面加载时恢复轮询 | 真实模型要跑几分钟，用户很可能刷新或关标签页 |
| 换设备可见 | 概览接口返回 `running_task`，有值就自动进入轮询 | 手机上看一眼也能继续 |
| 超时保护 | 轮询超过 15 分钟自动停止并提示「任务可能仍在后台执行」 | 避免一个僵死任务让页面永远转圈 |

### 6.2 Markdown 安全渲染

前端没有引入任何第三方 Markdown 库（页面要求零依赖、内网可离线打开），只实现简报用到的语法：
`#~###` 标题、有序/无序列表、引用、分隔线、`**粗体**`、`` `行内代码` ``、`[文字](链接)` 与裸链接。

**安全**：一律先 `escapeHtml` 再插标签。简报正文来自大模型，直接塞进 `innerHTML` 会有 XSS 风险。
服务端发送 HTML 邮件时用的是同一套思路（`app/push/markdown.py`），两边支持范围保持一致。

新闻链接统一加 `target="_blank" rel="noopener noreferrer"`，避免 `window.opener` 被反向控制。

### 6.3 订阅配置页

1. 填写昵称、身份描述、邮箱；
2. 关注领域可直接点「快速添加」预设，或在输入框内输入后按 <kbd>Enter</kbd> / <kbd>,</kbd> 添加；
3. 关键词、排除关键词同理，点击标签上的 `×` 可删除，输入框为空时按退格删除最后一个；
4. 点击「保存订阅」：首次保存会自动创建用户，之后保存则更新；
5. 用户 ID 记录在 `localStorage`（key: `news_agent_user_id`），**刷新页面会自动回填**；
6. 「新建一份配置」会解除本地关联并清空表单，可创建另一个用户。

支持 <kbd>Ctrl</kbd>/<kbd>Cmd</kbd> + <kbd>S</kbd> 快捷保存；离开页面前若有未保存修改会提示。

验收时可以直接用 URL 参数指定用户，省去手动改 `localStorage`：

```text
http://127.0.0.1:8000/?user_id=3    # 用户 A：AI Agent / MCP
http://127.0.0.1:8000/?user_id=4    # 用户 B：大模型 / AI 编程
```

### 6.4 实现上的两个坑

- **日期用本地时区拼**：`new Date().toISOString()` 是 UTC，晚上 8 点后会把「今天」算成第二天，
  导致读不到当天简报。改用 `getFullYear/getMonth/getDate` 拼接。
- **`hidden` 属性会被 `display:flex` 覆盖**：阶段指示、进度条都用 `hidden` 控制显隐，
  而 `.phase-steps { display: flex }` 的优先级高于 UA 的 `[hidden] { display: none }`，
  所以 CSS 里显式写了 `[hidden] { display: none !important; }`。这是很常见的踩坑点。

---

## 七、常见问题与坑位

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 启动报 `WinError 10048` 端口被占用 | 8000 端口已有服务 | 换端口 `--port 8010`，或结束占用进程 |
| 页面能打开但保存报「无法连接后端服务」 | 前端未通过 FastAPI 托管，或用 `file://` 直接打开了 HTML | 必须访问 `http://127.0.0.1:8000/`，静态资源由后端托管 |
| 刷新后数据没回来 | 浏览器换了无痕窗口 / 清了 localStorage | 页面右上角会显示关联的用户 ID；也可用 `GET /api/v1/users` 找回 |
| 数据库文件出现在奇怪的位置 | 用了相对 `DATABASE_URL` 且启动目录不同 | 已在 `config.py` 统一以项目根目录解析，绝对路径也可直接填 |
| 修改了模型字段但表没变 | `create_all` 只建表、不改表 | 第四阶段新增的列已由 `core/migrations.py` 自动补；更复杂的变更请引入 Alembic |
| 中文写库报编码错误 | 客户端未声明 UTF-8 | 请求头带 `Content-Type: application/json`（默认 UTF-8） |
| 定时任务时间没生效 | 改的是 `.env` 但服务没重启 | 调度器只在启动时注册，改完 `SCHEDULER_TIME` 需重启服务 |
| 到点没有自动生成 | `SCHEDULER_ENABLED=false`，或该用户没有订阅偏好 | 看启动日志与 `GET /api/v1/scheduler`；偏好多为空的用户会被记为 `skipped` |
| 任务一直停在「正在生成」 | 进程在执行中被杀，任务成了僵尸 | 心跳超过 `SCHEDULER_STALE_MINUTES`（默认 30 分钟）后会被重新领取；也可调小该值 |
| 邮件没收到 | 没配 SMTP / 没填邮箱 / 用户关了推送 / 进了垃圾箱 | 看 `GET /api/v1/users/{id}/push-settings` 与推送记录的 `last_error` |
| QQ 邮箱报认证失败 | 用了登录密码而不是授权码 | 在邮箱设置里开启 SMTP 服务并生成「授权码」，填到 `SMTP_PASSWORD` |

---

## 八、已知限制与后续演进

| 限制 | 现状 | 上线前建议 |
| --- | --- | --- |
| 无鉴权 | `user_id` 由调用方传入（路径参数 / 查询参数），前端存在 localStorage | 加 JWT：查询类接口「本人可读」，触发类接口「本人可触发」 |
| 单机调度 | APScheduler 跑在 Web 进程内，多 worker 会重复触发 | 拆独立调度进程，或换 Celery Beat / K8s CronJob |
| 迁移靠补列 | `core/migrations.py` 只支持 SQLite 加列 | 表结构复杂后引入 Alembic |
| 推送只有邮件 | `push_records.channel` 已预留字段 | 加企业微信 / Webhook 只需新增 sender 实现 |
| 运行轨迹在内存 | `run_store` 保留最近 50 次，重启即清空 | 落 `agent_runs` / `agent_run_steps` 两张表 |
| 跨语言同一事件不去重 | 中英文媒体标题差异大，机械相似度会误杀 | 交给模型在写作时合并（prompt 已要求） |
| 正文抽取是启发式的 | 纯 JS 渲染站点只能拿到站点摘要 | 需要完整正文时接 Readability 类库或无头浏览器 |

---

## 九、开发约定

- 接口返回一律用 `app/core/response.py` 的 `success()`，不要手写 dict 结构；
- 业务错误抛 `AppError` 子类，不要在接口里 `try/except` 包一层；
- 事务只在 service 层 `commit()`，crud 层只用 `flush()`；
- 幂等只认数据库约束，不要用内存标记（多进程 / 重启即失效）；
- 后台任务自己开会话（`SessionLocal`），不复用请求级会话（线程/协程边界不同）；
- 新增字段先改 `models`，同步改 `schemas`，再改 `crud/service`，最后补 `tests`；
  若表已存在，还要在 `core/migrations.py` 的 `COLUMN_PATCHES` 里加一条补列规则。

---

## 十、第二阶段：Agent 核心与 Function Calling

### 10.1 一句话说清「这是 Agent 而不是脚本」

同一份循环代码（`app/agent/loop.py`），喂三个不同任务，走出三条完全不同的路径：

| 任务 | 实际调用序列 | 轮数 | 是否产出文件 |
| --- | --- | --- | --- |
| 查看目录 → 找含 `Agent` 的 Python 文件 → 整理成 `summary.md` | `list_dir → search_content → read_file ×2 → write_file` | 6 | ✅ `summary.md` |
| 搜索含 `TODO` 的文件 → 读第一个 → 追加到 `todo_report.md` | `search_content → read_file ×2 → write_file` | 5 | ✅ `todo_report.md` |
| `notes` 目录有哪些文件？读 `agent_design.md` 讲讲内容 | `list_dir → read_file`（**无写入，直接回答**） | 3 | ❌ 不需要 |

三个任务在代码里**没有对应的分支**。差异只来自两处：任务语义（进了 prompt）+ 模型每一轮的决策。
这就是「调用顺序、次数、停止时机由 LLM 决定」的实际含义。

### 10.2 Agent Loop 执行过程

每一轮 = 一次 LLM 请求。`messages` 数组在整个过程中只增不减：

```
[0] system  : 你是工作区里的 AI 助手，工具清单…（只写原则，不写步骤）
[1] user    : 用户任务
── 第 1 轮 ──────────────────────────────────────────────
    请求 LLM(tools=[5 个工具的 JSON Schema])
    ← assistant: content="先看看目录" + tool_calls=[list_dir({path: "."})]
    执行 list_dir → ToolResult(ok=true, summary="共 10 项…")
[2] assistant: 带 tool_calls 的原样回填
[3] tool     : {"ok":true,"tool":"list_dir","summary":"…","data":{…}}  ← tool_call_id 必须对得上
── 第 2 轮 ──────────────────────────────────────────────
    请求 LLM(整个 messages 一起发)
    ← assistant: tool_calls=[search_content({keyword:"Agent", glob:"*.py"})]
    …（读文件、写文件同理）
── 第 N 轮 ──────────────────────────────────────────────
    ← assistant: 只有 content，没有 tool_calls   ← 停止条件
    循环结束，content 就是最终回答
```

循环里只做四件事：**发请求 → 解析 → 执行 → 回填**。没有 `if tool == "read_file"` 这类分支，
路由靠注册中心的字典完成，所以加工具不需要动主循环。

为什么工具串行执行：后一个调用常依赖前一个的结果（先搜索才知道读哪个文件）。
并行能省时间，但前提是模型明确声明这些调用互不依赖；本阶段正确性优先。

### 10.3 五个基础工具

| 工具 | 主要参数 | 关键校验 | 主要失败码 |
| --- | --- | --- | --- |
| `list_dir` | `path` / `recursive` / `max_depth(1-5)` / `show_hidden` | 目录必须存在且在沙箱内；跳过大目录（.git/.venv/node_modules） | `not_found` / `safety_violation` |
| `read_file` | `path` / `start_line` / `max_lines(1-2000)` | 编码探测（utf-8→gbk→utf-16）；二进制拒读；单次 200KB 上限 | `not_found` / `limit_exceeded` |
| `search_content` | `keyword` / `dir` / `glob` / `case_sensitive` | 纯 Python 遍历（不拼命令行，天然防注入）；跳过二进制与超大文件 | `not_found` / `safety_violation` |
| `write_file` | `path` / `content` / `mode(overwrite/append)` | 父目录自动创建；200KB 上限；不允许写到目录 | `limit_exceeded` / `safety_violation` |
| `bash` | `command` / `timeout(1-60)` | 危险命令黑名单、单行限制、cwd 锁定、超时、输出截断、环境变量清洗 | `safety_violation` / `command_failed` / `timeout` |

所有工具返回同一结构，LLM 与日志都不需要为不同工具写解析分支：

```json
{
  "ok": true,
  "tool": "read_file",
  "summary": "读取 src/agent_runner.py 第 1-37 行（共 37 行）",
  "data": { "path": "src/agent_runner.py", "content": "...", "total_lines": 37 },
  "error": null,
  "meta": { "duration_ms": 4 }
}
```

失败时 `ok=false`、`error={code, message, detail}`，`summary` 直接说人话。
超长结果的截断也返回**合法 JSON**（附加 `truncated/note/preview`），不会把半个 JSON 丢给模型。

### 10.4 九个工具一览（Agent 的完整能力面）

所有工具在同一个注册表里，模型看到的是同一份 Schema 列表。前五个是通用文件/命令能力，
后四个是第三阶段加进来的新闻业务能力——**加工具不需要改主循环**，这是注册中心的价值。

| 工具 | 参数要点 | 作用 | 什么时候会被调用 |
| --- | --- | --- | --- |
| `list_dir` | `path` / `recursive` / `max_depth` / `show_hidden` | 列出沙箱内目录结构 | 探查工作区里有什么 |
| `read_file` | `path` / `start_line` / `max_lines` | 读文本文件（编码探测、二进制拒读） | 看已有笔记 / 简报模板 |
| `search_content` | `keyword` / `dir` / `glob` / `case_sensitive` | 在沙箱内全文检索 | 找含关键字的文件 |
| `write_file` | `path` / `content` / `mode(overwrite/append)` | 写文件（父目录自动创建） | 通用任务产出文件（简报不走这条，见下） |
| `bash` | `command` / `timeout` | 执行白名单内只读命令 | 需要 `ls` / `grep` 等命令行时 |
| `get_user_preferences` | `user_id` | 从库里读身份、话题、关键词、排除词 | 生成简报前**先拿真实偏好**，而不是靠任务描述里的转述 |
| `search_news` | `keyword` / `date_range` / `exclude_keywords` / `sources` / `max_items` / `only_new` | 多源并发搜索 + 时间过滤 + 排除词 + 去重 + 打分 | 每个关注方向搜一次；信息不足时换角度重搜 |
| `fetch_article` | `url` / `max_chars` | 抓详情页正文（去噪 → 段落 → 密度兜底） | 标题不够判断重要性时读原文 |
| `save_briefing` | `user_id` / `content` / `title` / `briefing_date` / `news_count` / `file_path` | 结构校验 + 链接溯源 + 按「用户+日期」upsert 入库 + 导出 Markdown 到 `briefings/日期-userID.md` | 一次交付：写完直接入库并落盘，不需要再用 `write_file` 写一遍 |

### 10.5 工具注册中心

```python
@tool_registry.tool(description="列出指定目录下的文件与子目录…")
def list_dir(args: ListDirArgs, ctx: ToolContext) -> ToolResult:
    ...
```

- **参数模型 = 唯一事实来源**：`ListDirArgs`（Pydantic）既用来生成 JSON Schema 发给 LLM，
  又用来做运行时校验与类型转换（模型常把 `200` 写成 `"200"`，Pydantic 会自动转换）。
- **Schema 自动生成**：`model_json_schema()` → 去掉 `title` → 塞进 `{"type":"function","function":{...}}`。
  打开 `GET /api/agent/tools` 能看到 LLM 实际收到的定义。
- **按名动态执行**：`registry.execute(name, arguments, ctx)`，字典查找，零 if-else。
  `arguments` 是模型给的 JSON 字符串，先 `json.loads` 再 `model_validate`，任何一步失败都转成
  ToolResult 而不是抛异常。
- **新增工具只需三步**：写函数（带 Pydantic 参数模型 + docstring）→ 加 `@tool_registry.tool`
  → 在 `app/agent/tools/__init__.py` 里 import 一次。主循环、Schema 生成、路由都不用改。

### 10.6 异常与安全控制

| 场景 | 处理方式 | 是否中断任务 |
| --- | --- | --- |
| LLM 返回不存在的工具名 | 记 `unknown_tool`，错误消息里附**可用工具清单**回填给模型 | 否，让模型自纠 |
| 参数不是合法 JSON / 校验失败 | 记 `invalid_arguments` + 具体字段原因回填 | 否 |
| 文件不存在 / 越界 / 二进制 | 记 `not_found` / `safety_violation` / `limit_exceeded` | 否 |
| 工具内部抛异常 | 统一捕获为 `tool_error`，堆栈只进服务端日志 | 否 |
| LLM 网络/鉴权/超时失败 | 重试 2 次（指数退避），仍失败则 `status=llm_error` | 是 |
| Agent 无限调用工具 | 三重护栏：轮数上限（默认 8，硬顶 20）、工具次数上限（默认 20，硬顶 50）、整体超时（120s） | 是，返回已获得的信息 |
| 同一调用反复出现 | 第 3 次完全相同调用时插入提醒，促使模型换策略 | 否 |

`bash` 的安全是五层叠加的，**且明确不是完备沙箱**：

1. **命令黑名单**：`rm -rf`、`del /s`、`format`、注册表、系统服务、关机、杀进程、
   Shell 解释器（PowerShell / WMIC）、`curl … | sh`、`python -c`、`cd ..`、`pip install` 等一律拒绝；
2. **工作目录锁定**：`cwd` 强制为 `workspace/`，拒绝 `../` 与绝对路径逃逸；
3. **超时**：默认 10s、最大 60s，超时杀子进程并回报 `timed_out`；
4. **输出上限**：stdout/stderr 各截断 8000 字符（保留头 70% + 尾 30%，头尾信息最有价值）；
5. **环境变量清洗**：`*_API_KEY`、`*_TOKEN`、`*_SECRET`、`*_PASSWORD` 不传给子进程，
   模型即使 `echo $LLM_API_KEY` 也拿不到东西。

> 局限要写明白：黑名单挡不住所有变形命令。真要执行不可信代码，需要容器 / 低权限用户 /
> 系统级隔离，不能靠黑名单。本项目定位是「单人本地调试」，风险模型不一样。

路径校验用 `Path.resolve()` 之后再判断包含关系，而不是字符串前缀比较——
后者挡不住符号链接与 `..` 变形，这是常见踩坑点。

### 10.7 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_MODE` | `auto` | `auto`（有 key 用真模型，否则 mock）/ `openai` / `mock` |
| `AGENT_WORKSPACE` | 空 = `<项目根>/workspace` | Agent 沙箱目录 |
| `AGENT_MAX_STEPS` | 8 | LLM 轮数上限 |
| `AGENT_MAX_TOOL_CALLS` | 20 | 工具调用次数上限 |
| `AGENT_RUN_TIMEOUT` | 120 | 单任务整体超时（秒） |
| `AGENT_LLM_TIMEOUT` / `AGENT_LLM_RETRIES` | 60 / 2 | 单次请求超时与重试次数 |
| `AGENT_BASH_TIMEOUT` / `AGENT_BASH_TIMEOUT_MAX` | 10 / 60 | 命令超时 |
| `AGENT_TOOL_RESULT_LIMIT` | 12000 | 单条工具结果回填上限（字符） |
| `AGENT_CONTEXT_BUDGET_CHARS` | 40000 | 一次运行的工具结果总字符预算；超了就把最早的压成精简版（0 = 关闭压缩） |
| `AGENT_TOOL_RESULT_KEEP_FULL` | 3 | 压缩时始终保留全文的最近 N 条工具结果 |

### 10.8 接口与调试

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/agent/run` | 执行任务，同步返回回答 + 完整工具调用轨迹 |
| GET | `/api/agent/tools` | 查看已注册工具及其 JSON Schema |
| GET | `/api/agent/runs` | 最近运行摘要（内存保留最近 50 次） |
| GET | `/api/agent/runs/{run_id}` | 单次运行的完整轨迹 |

```bash
curl -X POST http://127.0.0.1:8000/api/agent/run \
  -H "Content-Type: application/json" \
  -d '{"task":"帮我查看 workspace 目录中的文件，找到包含 Agent 关键字的 Python 文件，读取相关内容，最后整理成 summary.md"}'
```

响应（节选）：

```json
{
  "code": 0,
  "message": "任务完成",
  "data": {
    "run_id": "110210347caf",
    "status": "completed",
    "steps": 6,
    "tool_call_count": 5,
    "tool_calls": [
      {"index":1,"tool":"list_dir","arguments":{"path":"."},"ok":true,"summary":"目录 . 下共 5 项…","duration_ms":4},
      {"index":2,"tool":"search_content","arguments":{"keyword":"Agent","glob":"*.py"},"ok":true,"summary":"命中 5 处，涉及 2 个文件","duration_ms":9},
      {"index":3,"tool":"read_file","arguments":{"path":"src/agent_runner.py"},"ok":true,"summary":"读取 src/agent_runner.py 第 1-37 行","duration_ms":4},
      {"index":5,"tool":"write_file","arguments":{"path":"summary.md"},"ok":true,"summary":"写入 summary.md 成功（3071 字节，新建文件）","duration_ms":6}
    ],
    "answer": "…"
  }
}
```

命令行演示（三组任务一次跑完，逐条打印轨迹）：

```bash
python scripts/agent_demo.py --mode mock     # 离线：验证链路
python scripts/agent_demo.py                 # 有 key 时自动用真实模型
```

### 10.9 Mock 模式的边界（重要）

`AGENT_MODE=mock` 时使用的 `MockLLMClient` 用**正则抽取任务意图 + 固定优先级**来模拟决策。
它证明的是「Schema 生成 → tool_calls 解析 → 执行 → 结果回填 → 多轮循环」这条链路是通的，
**不能**用来证明「模型能自主规划」。

要验证真正的自主性，配置 `LLM_API_KEY` 后重跑同样三个任务——主循环、工具、测试一行都不用改，
这正是 LLM 客户端抽象层的目的。另外 `tests/test_agent_loop.py` 用 `ScriptedLLM` 故意喂三种
人为规定的调用顺序驱动同一份循环，验证「循环不预设流程」这个性质本身。

### 10.10 常见问题（第二阶段新增）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 启动日志提示「Agent 运行在 Mock 模式」 | 未配置 `LLM_API_KEY`，`auto` 降级 | 填 `.env` 里的 key，或显式 `AGENT_MODE=mock` 做离线验证 |
| 接口返回 400「未配置 LLM_API_KEY」 | 请求里 `llm_mode` 指定了 `openai` | 去掉该字段或补 key |
| `bash` 输出中文乱码 | Windows 控制台编码 | 工具已按 utf-8→gbk 顺序解码；仍有问题时给命令加 `chcp 65001` 之外的写法或改用 Python 脚本 |
| 命令被拦「被安全策略拦截」 | 命中危险命令黑名单 | 换只读命令；黑名单规则见 `app/agent/safety.py` |
| 提示「路径越界」 | 访问了 `workspace/` 之外 | 这是设计行为，把文件放进工作区再操作 |
| `status=max_steps_reached` | 模型反复调工具停不下来 | 调大 `AGENT_MAX_STEPS`，或把任务描述得更具体 |
| 运行记录查不到 | 内存环形缓冲只留最近 50 次，重启即清空 | 调整 `AGENT_KEEP_RUNS`；需要持久化则落库 |

---

## 十一、第三阶段：AI 新闻采集、个性化筛选与简报生成

### 11.1 为什么「这仍然是同一个 Agent」

第三阶段**没有**另写一条固定流程，而是给同一个 Agent 加了四个工具、一份专用 prompt：

| 复用点 | 第二阶段 | 第三阶段 | 改动量 |
| --- | --- | --- | --- |
| 工具注册中心 | 5 个工具 | 9 个工具（+4 新闻工具） | 新增模块，注册表代码未改 |
| Agent 主循环 `loop.py` | 文件任务 | 新闻任务 | **只有一处**：允许注入 `system_prompt` |
| 执行上下文 `ToolContext` | 工作区 + 限额 | +用户 ID / 新闻候选池 / 会话工厂 | 加三个字段 |
| 轨迹与护栏 | 轮数 / 次数 / 超时 / 重复提醒 | 完全一致（简报场景另配 16 轮 / 40 次） | 0 |
| Mock 客户端 | 文件意图 | 增加新闻策略分支 | 仅离线验证用 |

判断「是不是固定流程」的标准很直接：**代码里有没有"先搜 A 再搜 B"的编排**。
本阶段所有搜索角度、搜索次数、是否读原文、何时收笔，都是模型在循环里基于上一轮结果决定的；
服务端只提供三样东西——工具、护栏、交付物清单。

### 11.2 新闻能力层 `app/news/`

刻意与 Agent 解耦：这一层不 import 任何 `app.agent.*`，因此可以单独测试、单独替换。

| 模块 | 职责 | 关键决策 |
| --- | --- | --- |
| `feeds.py` | 10 个源（量子位 / 雷锋网 / 极客公园 / IT之家 / OpenAI / Google AI / HuggingFace / TechCrunch AI / Simon Willison / MarkTechPost） | 源清单属部署配置，放代码 + `NEWS_FEEDS` 覆盖 |
| `http.py` | 抓取并归一化错误 | 伪装浏览器 UA（多个源默认拒绝 httpx UA）、按字节截断、**不解析域名做 SSRF 判定**（TUN 代理会把域名解析成 198.18.x.x 假 IP，误杀正常请求） |
| `rss.py` | RSS 2.0 / Atom / RDF 解析 | 标准库 `ElementTree`，零依赖；把 UTC 统一转本地时间（否则「今天」判定错一天）；行内标签删空而不是换空格（中文会变成「这是 摘要 内容」） |
| `article.py` | 正文抽取 | 去噪 → 取 `<p>/<h2>` → 密度兜底；产出 `full / excerpt / summary / none` 四态，**抓不到就是 none**，不拼接不脑补 |
| `dedupe.py` | 去重 | URL 规范化（去 utm/fragment/尾斜杠）+ 标题归一 + 二元组预筛 + `SequenceMatcher` |
| `filter.py` | 时间窗口 / 关键词 / 打分 | 支持 `today / yesterday / last_24h / 3d / 7d / 2026-11-18 / 区间`；英文词边界匹配（`ai` 不命中 `said`） |
| `providers.py` | 搜索提供方协议 | `RSSNewsProvider` + `register_provider()`，接 NewsAPI/博查/Tavily/模型内置搜索只需新增实现 |
| `state.py` | 一次运行的候选池 | 跨调用去重 + **链接白名单**（简报链接溯源的技术兜底） |
| `briefing.py` | 简报结构校验 | 章节 / 头部字段 / 条目链接 / 字数截断检测 |

### 11.3 四个新闻工具

| 工具 | 主要参数 | 做什么 | 护栏 |
| --- | --- | --- | --- |
| `get_user_preferences` | `user_id` | 从 SQLite 读身份、话题、关键词、排除词 | 用户不存在 → 404 错误码而非异常 |
| `search_news` | `keyword`、`date_range`、`exclude_keywords`、`sources`、`max_items`、`only_new` | 多源并发搜索 + 时间过滤 + 排除词 + 去重 + 打分排序 | 全部源失败 → 明确报错并提示「不要编造」；单源失败只作降级记录；结果带 `source_reports` 与 `stats` |
| `fetch_article` | `url`、`max_chars` | 抓详情页，抽标题/来源/时间/正文 | HTTP 错误、非 HTML、页面无正文、内网地址一律失败并说明；返回 `content_kind` 与 `warnings`；**同一轮重复读同一篇命中内存缓存，不重复下载** |
| `save_briefing` | `user_id`、`content`、`briefing_date`、`file_path`… | 校验后按「用户+日期」upsert，并自己把 Markdown 导出到工作区 | 结构不全 → 拒收并列出缺失项；链接不在本轮检索结果内 → 拒收并给出可用链接；路径越界/超限 → 只降级为 warning，不影响入库 |

工具描述（LLM 靠它选工具）里写明了幂等语义、能返回什么、什么时候该放弃，例如
`search_news` 会明确说「一次搜索只覆盖一个关键词，多个主题应分多次搜索」。

#### `search_news` 的两种模式

| 模式 | 怎么触发 | 返回什么 | 什么时候用 |
| --- | --- | --- | --- |
| 浏览 | **不传 `keyword`** | 时间窗内全部候选，按发布时间倒序，只给标题/来源/时间/链接（不带摘要） | 每次生成**先来一次**，一眼看清「今天有什么」，再挑 3~6 条读正文 |
| 关键词 | 传 `keyword` | 按相关度打分后的候选，带摘要与命中词 | 清单里明显缺某个方向时定向补搜，通常 0~2 次 |

为什么要有浏览模式：关键词模式一次只覆盖一个概念，早期实现里模型只能不断换词试探——
实测一次生成搜 19~29 次、撞上 `max_steps` 才收尾，而候选中真正相关的只有几条。
浏览模式把「目录」一次性摆出来，模型的工作从「猜关键词」变成「在清单里挑」：

| 指标（demo 用户 B，DeepSeek） | 只有关键词模式 | 加入浏览模式 |
| --- | --- | --- |
| 搜索调用 | 19~29 次 | **4~5 次** |
| Agent 轮数 | 15 | **8** |
| 工具调用总数 | 37 | **13~15** |
| 单次生成耗时 | 62~74s | **51s** |

清单条数由 `NEWS_BROWSE_MAX_ITEMS`（默认 40）封顶，模型要求更多也不放大：
清单里的每一条都会被后续每一轮重新发送，60 条就能把上下文推到 4 万字符。
清单里没给全时，摘要会提示「用 sources 分源浏览或按关键词补搜」，避免它重复浏览同一个窗口。


### 11.4 个性化筛选：机械判断与语义判断的分工

| 判断类型 | 谁来做 | 例子 |
| --- | --- | --- |
| 时间是否在今天 | 工具（`filter.py`） | 昨天 23:59 的新闻不进今日简报 |
| 是否命中排除词 | 工具 | 用户排除「融资」→ 「完成 3 亿元 B 轮融资」直接过滤 |
| 是否同一条新闻 | 工具（`state.py` + `dedupe.py`） | 同一篇稿子的 `utm_source` 变体、同标题转载只留一条 |
| **这条新闻对这个人重不重要** | **模型** | 「Daydream 推出 AI 造型师」字面命中 AI+Agent，但消费级穿搭应用对后端 Agent 开发者价值有限 → 模型应排除 |

为什么语义判断不交给代码：用户关心「AI Agent 落地」，而相关新闻的标题里可能一个「Agent」都没有
（「某公司发布新工具，可自动完成多步任务」）。工具层越"聪明"，越容易把该给模型的信息提前筛掉。
搜索工具的作用只是把几百条裁到十几条候选，把判断权留给模型。

`search_news` 的 `stats` 会如实回报每类过滤挡掉了多少条：

```json
"stats": {"feeds_ok": 10, "feeds_failed": 0, "scanned": 230, "matched": 6,
          "duplicates_removed": 5, "excluded_by_keyword": 1,
          "out_of_date_range": 148, "undated": 0, "unmatched": 39}
```

这样模型知道「是没搜到，还是被过滤掉了」，才能决定「换个词再搜」还是「今天确实没新闻」。

### 11.5 简报生成的三道闸

1. **结构闸**（`parse_briefing`）：必须有 `# 每日 AI 新闻简报`、日期/关注方向/数量三行、
   `## 今日重点新闻`、`## 行业动态总结`、`## 今日值得关注`，且每条重点新闻都要有链接；
   字数 < 200 视为被截断。
2. **溯源闸**（`save_briefing` + `news_state`）：正文里所有 http(s) 链接必须出现在本轮
   `search_news` / `fetch_article` 的结果里，否则拒收并回传允许的链接清单。
3. **唯一闸**（数据库）：`UNIQUE(user_id, briefing_date)`。定时任务与手动触发并发写同一天时，
   应用层的「先查后写」会穿透，唯一键是最后一道闸。

被拒收时工具返回的是**可修复的说明**（缺哪章、哪条没链接、哪几个链接不可信），
模型据此改写后重提——而不是整个任务失败。

### 11.6 表设计 `news_briefings`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PK | 简报 ID |
| `user_id` | FK → users.id，CASCADE | 用户 ID |
| `briefing_date` | DATE | 简报日期（自然日） |
| `title` | VARCHAR(200) | 简报标题 |
| `content` | TEXT | Markdown 全文（以库为准，文件只是导出物） |
| `news_count` | INTEGER | 条目数 |
| `generated_at` | DATETIME | 生成时间（重新生成会刷新） |
| `focus_topics` | JSON | 生成时的关注方向快照（用户偏好之后可能变化） |
| `file_path` | VARCHAR(255) | 导出的 Markdown 在工作区内的相对路径 |
| `run_id` | VARCHAR(32) | 对应 Agent 运行 ID，可回溯轨迹 |

去重语义：同一用户同一天只有一行；再次生成执行 UPDATE（覆盖内容、刷新 `generated_at`、`updated_at`）。

### 11.7 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/agent/briefings` | 生成简报（Agent 自主执行），返回 `run`（轨迹）+ `briefing`（入库结果） |
| GET | `/api/v1/briefings?user_id=1` | 按用户分页列出（不含正文） |
| GET | `/api/v1/briefings/{id}` | 简报详情（含 Markdown 全文） |
| GET | `/api/agent/runs/{run_id}` | 回看该次运行的完整工具调用轨迹 |

```bash
# 生成（llm_mode 省略则按 AGENT_MODE；mock 为离线策略）
curl -X POST http://127.0.0.1:8000/api/agent/briefings \
  -H "Content-Type: application/json" \
  -d '{"user_id":1,"focus":"MCP 生态"}'

# 查询
curl "http://127.0.0.1:8000/api/v1/briefings?user_id=1"
curl http://127.0.0.1:8000/api/v1/briefings/1
```

响应里的 `run.tool_calls` 就是「Agent 到底干了什么」的证据：

```json
"tool_calls": [
  {"index":1,"tool":"get_user_preferences","arguments":{"user_id":1},"ok":true},
  {"index":2,"tool":"search_news","arguments":{"keyword":"AI Agent","date_range":"today"},"ok":true},
  {"index":3,"tool":"search_news","arguments":{"keyword":"MCP","date_range":"today"},"ok":true},
  {"index":4,"tool":"fetch_article","arguments":{"url":"https://..."},"ok":true},
  {"index":5,"tool":"save_briefing","arguments":{"user_id":1,"file_path":"briefings/2026-11-18-user1.md"},"ok":true,"summary":"新建简报 #1（2026-11-18，4 条，用户 1），已导出 briefings/2026-11-18-user1.md"}
]
```

> 早期版本这里是 `fetch_article → write_file → save_briefing` 三条：
> 模型要把同一份正文生成两遍。现在落盘由 `save_briefing` 自己做，
> 轨迹里不会再出现 `write_file`。若在旧轨迹里看到它，属于历史记录。

### 11.8 配置项（第三阶段新增）

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `NEWS_PROVIDER` | `rss` | 搜索提供方；注册新服务后改这里切全局，未知名字回退 `rss` |
| `NEWS_FEEDS` | 空（用内置 10 个源） | 逗号分隔的源 ID，如 `qbitai,openai` |
| `NEWS_MAX_ITEMS_PER_FEED` | 30 | 单源最多读取条数 |
| `NEWS_FEED_CACHE_TTL` | 300 | RSS 抓取的进程内缓存时长（秒）；一次运行多次搜索只真抓一次，0 = 关闭 |
| `NEWS_FETCH_TIMEOUT` | 12.0 | 抓取超时（秒） |
| `NEWS_HTTP_MAX_BYTES` | 3000000 | 单个响应体上限（OpenAI 的 Feed 有 800KB+） |
| `NEWS_SEARCH_MAX_ITEMS` | 12 | `search_news` 关键词模式默认返回上限 |
| `NEWS_BROWSE_MAX_ITEMS` | 40 | `search_news` 浏览模式（不传 keyword）单次返回上限 |
| `NEWS_ARTICLE_MAX_CHARS` | 6000 | `fetch_article` 正文上限 |
| `NEWS_TITLE_SIMILARITY` | 0.9 | 判定「同一条新闻」的标题相似度阈值 |
| `BRIEFING_OUTPUT_DIR` | `briefings` | 简报在 workspace 内的子目录 |
| `BRIEFING_MAX_STEPS` | 16 | 简报任务的轮数上限（搜索多，比通用任务宽松） |
| `BRIEFING_MAX_TOOL_CALLS` | 40 | 简报任务的工具调用上限 |
| `BRIEFING_RUN_TIMEOUT` | 600 | 简报任务整体超时（秒） |

### 11.9 实测记录

两组用户（`python scripts/briefing_demo.py`，真实 RSS，离线策略）：

| | 用户 A | 用户 B |
| --- | --- | --- |
| 身份 | AI Agent 应用开发工程师 | 后端开发工程师 |
| 关注 | AI Agent、MCP、Agent 框架 / 多智能体、工具调用 | 大模型、AI 编程 / 代码生成、Copilot |
| Agent 实际搜的词 | `AI Agent` → `MCP` → `Agent 框架` | `大模型` → `AI 编程` → `代码生成` |
| 轨迹 | 7 次工具调用、8 轮、约 21s | 7 次工具调用、8 轮、约 16s |
| 结果 | 5 条，覆盖 Agent 编排 / Agentic Search | 5 条，覆盖多模态文档 / 代码生成 |

**真实模型实测（DeepSeek，`--mode openai`）**：同一份代码、同一个 prompt，只换模型，行为立刻分叉——

| | 用户 A（AI Agent / MCP） | 用户 B（大模型 / AI 编程） |
| --- | --- | --- |
| 轮数 / 工具调用 | 13 轮 / 22 次 | 10 轮 / 27 次 |
| 耗时 | 113.6s | 135.6s |
| 搜索 | 10 次，自己拆出 `MCP Model Context Protocol`、`多智能体 编排`、`agent 工具调用`、`agent framework`、`coding agent sandbox` 等角度 | 16 次，除偏好词外自己补了 `Codex`、`Copilot`、`Claude`、`Gemini`、`SWE-bench`、`Qwen`、`open weights model release` |
| 读原文 | 7 篇（MarkTechPost ×4、Simon Willison、IT之家、雷锋网） | 6 篇（含 openai.com 一次 HTTP 403） |
| 候选 / 产出 | 23 条 → 6 条，17.7 KB | 44 条 → 6 条，13.2 KB |

自主决策在两个用户身上的具体表现：

- **A 发现信息缺口后主动换角度**：搜 3 次 MCP 相关词都为空，它没有硬凑，而是改从「Code Mode 的 MCP 谱系」切入，并在简报开头写明「今日没有检索到 MCP 协议本身的直接更新……已在条目中说明」。
- **B 遇到抓取失败后自行降级**：`fetch_article` 对 `openai.com` 返回 403，它没有编内容，而是改读 MarkTechPost 的同题报道，并在条目里留了一行「备注：OpenAI 官方页面抓取返回 HTTP 403，正文未获取，官方侧信息仅依据其 Feed 摘要」。
- **两人的重合与分叉都合理**：Code Mode、外部沙箱、Perplexity Multiagent、DeepSeek 计费这 4 条两人都收了，但 A 的落点是「工具编排范式与执行侧隔离」，B 的落点是「编码落地与每任务成本」；第 6 条完全不同的两条（A 收 SID-2 agentic search，B 收 Gemini Neon 爆料）。
- **检索广度不同**：B 搜了 16 次、攒到 44 条候选才收笔，A 搜 10 次就判定够了——同一个 prompt 下两种不同的收敛策略，说明步数是模型自己定的，不是脚本写死的。

异常场景的覆盖情况（均在 `tests/` 中可复现）：

| 场景 | 触发方式 | 预期与实际行为 |
| --- | --- | --- |
| RSS 源访问失败 | `test_search_news_all_sources_down` | `ok=false`，错误码 `news_source_unavailable`，提示「不要编造新闻」 |
| 部分源失败 | `test_search_news_applies_all_filters` | 不影响结果，`stats.feeds_failed` 如实计数 |
| 新闻重复 | 同标题转载 + `?utm_source=` 变体 | `duplicates_removed` 计数，只保留一条 |
| 正文无法获取 | 403 / PDF / 空壳页 / 内网地址 | `article_fetch_failed`，`content_available=false`，提示「请勿编造」 |
| 当天没有相关新闻 | 冷门主题（量子退火） | 不写文件、不入库，如实说明「没有检索到」 |
| LLM 内容不完整 | 缺章节 / 缺链接 / 字数过短 | 拒收并列出缺失项，模型可补齐后重提 |

### 11.10 离线策略的边界（重要）

仓库默认可以**不配 Key 就跑通整条链路**：没有 `LLM_API_KEY` 时 `AGENT_MODE=auto`
会降级到 `mock_news.py` 里的「状态机 + 模板」策略。它和真实模型走的是**同一个
`AgentRunner`、同一套工具、同一套校验**，区别只在「下一步做什么」由谁决定。

离线策略能证明的是：

- ✅ 工具注册、参数校验、多轮循环、结果回填这条链路对新闻工具同样成立；
- ✅ 时间过滤、排除词、跨调用去重、正文抓取失败处理都按设计工作；
- ✅ 简报结构校验、链接溯源、同日去重都生效；
- ✅ 不同偏好确实产出不同简报。

它**不能**证明的是「模型能自主规划」，具体差异：

| 能力 | 真实模型 | 离线策略 |
| --- | --- | --- |
| 搜索角度 | 自行拆同义词、上位概念、中英文表述 | 按偏好词条顺序各搜一次 |
| 相关性判断 | 语义级（能排除「AI 造型师」这类字面命中） | 关键词字面匹配，会误收 |
| 摘要与重要性分析 | 基于正文改写 | 从正文截取前两句 + 模板句 |
| 信息不足时的反应 | 换角度重搜 | 只在候选 < 3 时放宽到 3 天 |

补上 Key 后复跑，代码一行不用改：

```bash
# .env 里填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（任意 OpenAI 兼容网关）
python scripts/briefing_demo.py --mode openai
# 或指定单个用户
python scripts/briefing_demo.py --user A --mode openai
```

本次用 DeepSeek 实测（配置见 `.env.example`）：`LLM_BASE_URL=https://api.deepseek.com/v1`、
`LLM_MODEL=deepseek-chat`、`AGENT_MODE=openai`。上表「语义级相关度」这一行在真实模型下得到验证——
离线版会把《Daydream's new AI agent acts as your personal stylist》（AI 造型师）当成 AI Agent 新闻收进简报，
真实模型直接把它排除了。

### 11.11 常见问题（第三阶段新增）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `search_news` 返回 0 条但明明有新闻 | 时间窗过窄（`today`），或命中排除词 | 「今天」只覆盖 0 点之后，早上生成时几乎没内容；用默认的 `last_24h`，并看 `stats.out_of_date_range` / `excluded_by_keyword` |
| 模型反复换关键词搜十几次 | 它不知道「池子里一共只有几条」 | 看 `stats.pool_window`：这是时间窗内全站候选总数，接近 `returned_so_far` 就该收手 |
| 启动日志显示 `实际模式=mock` | `LLM_API_KEY` 没读到 | 密钥放 `.env.local`（覆盖 `.env`）；指纹 `sk-xxx***#hash` 变了说明文件被改过 |
| 所有源都 403 | UA 被拦，或本机代理拦截 | 已伪装浏览器 UA；检查网络/代理设置 |
| `fetch_article` 总是 `content_kind=summary` | 站点正文由 JS 渲染（无 `<p>`） | 属预期行为，简报中会标注「正文未获取」 |
| 简报被拒「链接不在检索结果内」 | 模型自己拼了 URL | 用返回的 `allowed_urls` 改写；这是设计行为 |
| 同一用户同一天有两条简报 | 唯一键未生效（旧库未迁移） | 删掉旧库或用 `init_db()` 重建；唯一约束在表定义里 |
| 中文源覆盖偏少 | 机器之心等源已关闭 RSS | 用 `NEWS_FEEDS` 增删；或按 `providers.py` 接入搜索 API |
| `status=max_steps_reached` | 简报任务搜索过多 | 调大 `BRIEFING_MAX_STEPS`，或在偏好里减少关注词条 |

### 11.12 已知限制

- **跨语言同一事件不去重**：中文和英文媒体对同一事件的报道标题差异大，机械相似度会误杀，
  交给模型在写作时合并（`prompt` 里已明确要求）。
- **正文抽取是启发式的**：对纯 JS 渲染站点只能拿到站点摘要，这是「宁可标不确定，也不拼内容」的取舍。
- **源可用性依赖网络环境**：源的可用状态会变化，`source_reports` 每次都如实回报，便于发现失效源。
- **无鉴权**：`user_id` 由调用方传入，第四阶段接入 JWT 后应改为「从令牌取用户」。

---

## 十二、第四阶段：定时任务、消息推送与历史简报管理

### 12.1 一次完整的日常运行

```
08:00  APScheduler 触发 dispatch_daily_generation()
        │
        ├─ 遍历 users 表拿到全部 user_id（每用户独立会话）
        │
        ├─ user 1: get_or_create(user, today)         → created  → 入队
        ├─ user 2: get_or_create(...)                 → created  → 入队
        ├─ user 3: 当天已 success                     → already_generated（跳过）
        └─ user 4: 画像读取失败 → 记 failed，继续下一个用户（不中断）
        │
        ▼  worker（并发上限 2）
   claim(task_id) 条件 UPDATE 抢执行权
        │
        ├─ 读用户画像（第三阶段 briefing_service.get_user_profile）
        ├─ run_briefing(...)  ← 完全复用第三阶段：同一个 AgentRunner、同一套工具
        │     Agent 自主：get_user_preferences → search_news ×N → fetch_article ×M → save_briefing
        ├─ 简报入库 + Markdown 落盘（同一次 save_briefing 内完成）
        └─ 标记 task = success
        │
        ▼
   push_for_task(task_id)（独立会话，失败不影响生成结论）
        │
        ├─ 用户关了推送 → disabled
        ├─ 没填邮箱 / 没配 SMTP → skipped（说明原因）
        └─ 否则发信 → success（记录 message_id）/ failed（记录原因 + next_retry_at）
        │
        ▼
   10 分钟后 retry_failed_pushes() 扫描 failed 且到期的记录 → 重发（不重新生成）
        │
        ▼
   前端：概览看最近简报 + 推送状态；历史列表看每天的生成/推送；详情看 Markdown 全文
```

### 12.2 为什么「定时任务」和「生成逻辑」必须分开

| 混在一起的写法 | 本项目的写法 |
| --- | --- |
| 调度函数里写 `for user in users: 读偏好 → 拼任务 → 跑 Agent → 写库 → 发邮件` | `jobs.py` 只做「遍历 + 登记 + 入队」；`generation_service` 做生成；`push_service` 做推送 |
| 想手动触发一次？只能抄一遍流程，或者等到明天 | 手动触发调用同一个 `get_or_create` + 同一个 `execute_task` |
| 想换 Celery / crontab？要重写业务逻辑 | 删掉 `manager.py` 即可，作业函数一行不改 |
| 单用户异常把整批带崩 | 每个用户 `try/except`，失败只写自己那一行 |

作业层可以直接调用（测试就是这么做的），不需要真的等到明天早上八点：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/scheduler/run-now       # 立即派发一次
curl http://127.0.0.1:8000/api/v1/scheduler                        # 下次几点跑
```

### 12.3 幂等：三道闸而不是一个布尔变量

| 闸 | 位置 | 挡住什么 |
| --- | --- | --- |
| `UNIQUE(user_id, briefing_date)` | `generation_tasks` | 同一用户同一天被登记两条任务（并发、多进程、重复触发） |
| 条件 UPDATE `claim()` | `crud/task.py` | 同一条任务被两个执行者同时跑（受影响行数 = 1 才算抢到） |
| `UNIQUE(task_id, channel)` | `push_records` | 同一份简报被推送两次 |

`claim()` 是整段逻辑的核心：

```sql
UPDATE generation_tasks
   SET status='running', attempts=attempts+1, started_at=:now, heartbeat_at=:now
 WHERE id=:id
   AND (status IN ('pending','failed','skipped')
        OR (status='running' AND heartbeat_at < :stale_before))
```

- 行数为 1 → 抢到执行权；
- 行数为 0 → 别人在跑，或已经成功，直接放弃（记录日志，不报错）。

**为什么不用进程内锁**：锁不跨进程、不跨重启。多开一个 uvicorn、或者服务重启，
内存锁就形同不存在；而这条 UPDATE 在 SQLite 里是原子的，在 MySQL / PG 里同样是行锁。

状态语义（前端据此显示「等待执行 / 正在生成 / 成功 / 失败 / 跳过」）：

| 状态 | 含义 | 能否被再次领取 |
| --- | --- | --- |
| `pending` | 已登记，等 worker | ✅ |
| `running` | 正在跑（心跳新鲜） | ❌（`already_running`） |
| `running` 且心跳过期 | 进程崩溃遗留的僵尸 | ✅（当作失败重跑） |
| `success` | 简报已入库 | ❌（除非手动 `force=true`） |
| `failed` | 执行失败，可重跑 | ✅ |
| `skipped` | 主动跳过（如未配置偏好） | ✅ |

`heartbeat_at` 由 `generation_service._heartbeat_loop()` 每 60 秒刷新一次。
没有心跳的话，一次 `Ctrl+C` 就会让某个用户当天永远「正在生成」；
有了心跳，超过 `SCHEDULER_STALE_MINUTES`（默认 30 分钟）就会被下一次触发重新领取。

### 12.4 并发与隔离

| 隔离维度 | 做法 |
| --- | --- |
| 用户之间 | 每个用户独立任务行、独立数据库会话；派发时逐用户 `try/except` |
| 任务之间 | worker 池（`asyncio.Queue` + `SCHEDULER_MAX_CONCURRENCY` 个消费协程），避免 20 个用户同时打满模型配额 |
| 生成与推送 | 两个数据库会话；推送异常只记 `push_status=error`，已入库的简报不回滚 |
| 重复提交 | 队列层 `_submitted` 集合去重 + 数据库层 `claim()` 兜底（两层是故意的：前者省数据库往返，后者才是保证） |
| 线程安全 | 工具在 `anyio.to_thread` 里执行，`Session` 不跨线程复用，每个工具调用新开会话 |

### 12.5 定时任务配置

```bash
# .env
SCHEDULER_ENABLED=true            # 总开关
SCHEDULER_TIME=08:00              # 每天几点（HH:MM，写错自动回退 08:00 并告警）
SCHEDULER_TIMEZONE=Asia/Shanghai  # 时区（决定「几点」和「任务归属哪一天」）
SCHEDULER_CATCH_UP=true           # 启动补跑
SCHEDULER_MAX_CONCURRENCY=2       # 同时跑几个用户的生成
SCHEDULER_MISFIRE_GRACE=3600      # 错过触发点后的补执行宽限（秒）
SCHEDULER_STALE_MINUTES=30        # 心跳超过该值视为僵尸任务
```

两个作业：

| 作业 ID | 触发器 | 说明 |
| --- | --- | --- |
| `daily-briefing-generation` | `CronTrigger(hour, minute, timezone)` | 每日批量派发；`coalesce=True`（错过多次只补一次）、`max_instances=1`（不叠加） |
| `push-retry` | `IntervalTrigger(minutes=PUSH_RETRY_INTERVAL_MINUTES)` | 扫描失败的推送并重试 |
| `stale-task-recovery` | `IntervalTrigger(minutes=SCHEDULER_RECOVERY_INTERVAL_MINUTES)` | 回收心跳过期的僵尸任务，并把当天的任务重新派发（自愈） |

**时区为什么要单独配**：`SCHEDULER_TIME=08:00` 是业务时区的时间。服务器可能跑在 UTC，
「早上八点」和「任务归属哪一天」都必须按 `SCHEDULER_TIMEZONE` 算，否则会出现
「早上八点生成的是昨天的简报」。`jobs._today_for_scheduler()` 就是干这个的。

**重启后能恢复**：作业定义在代码里，每次进程启动都在 lifespan 里重新 `add_job`（不依赖持久化 jobstore）；
真正的幂等由数据库保证，所以「重启后重复触发」也不会产生重复简报。

**启动补跑（catch-up）** 覆盖「停机期间错过时间点」这个 APScheduler 管不到的场景：
进程重启后是全新调度器，不会知道自己错过了一次。补跑条件三个都满足才执行：
`SCHEDULER_CATCH_UP=true` + 当前时间已过今天的执行点 + 该用户当天没有 `success` 任务。

### 12.6 邮件推送配置

```bash
# .env（QQ 邮箱示例：465 端口 + 授权码）
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=你的账号@qq.com
SMTP_PASSWORD=邮箱授权码        # 不是登录密码
SMTP_SENDER=你的账号@qq.com     # 留空则回退 SMTP_USER
SMTP_FROM_NAME=AI 新闻助手
SMTP_TIMEOUT=15

PUSH_ENABLED=true               # 服务端总开关
PUSH_BASE_URL=http://127.0.0.1:8000   # 邮件里「查看简报」入口的前缀
PUSH_MAX_ATTEMPTS=3
PUSH_RETRY_BACKOFF_SECONDS=60
PUSH_RETRY_INTERVAL_MINUTES=10
```

加密方式按端口自动判断，可用 `SMTP_USE_SSL` 显式覆盖：

| 端口 | 方式 | 适用 |
| --- | --- | --- |
| 465 | SSL 直连 | QQ / 163 / Gmail 常见配置 |
| 587 | 明文连接后 STARTTLS | 企业邮箱、Outlook |
| 其它 | 明文 | 本地假 SMTP（`app/push/local_sink.py`）、内网中继（Postfix / MailHog） |

邮件内容包含验收要求的四项：

| 项 | 来源 | 说明 |
| --- | --- | --- |
| 简报标题 | `news_briefings.title` + 日期 | 主题：`【AI 新闻简报】2026-11-18 · 每日 AI 新闻简报` |
| 新闻摘要 | 从 Markdown 的 `- 新闻摘要：` 行解析 | 摘录而非重写，保证与详情页一致；超长截断 220 字 |
| 简报查看入口 | `PUSH_BASE_URL/#/briefing/{briefing_id}` | HTML 里是按钮，纯文本里是裸链接 |
| 原始新闻链接 | 从 Markdown 提取 http(s) 链接并去重 | 每条带「阅读原文 ↗」，文末再汇总一份清单 |

同时发 `text/plain` 与 `text/html` 两个分部：很多客户端默认不渲染 HTML，只发 HTML 会看到空白。

**不重复推送**：`push_records` 上 `UNIQUE(task_id, channel)` + 「已 success 默认不再发」
（除非显式 `force=true`）。重新生成简报覆盖了内容，也不会自动再发一封。

**失败可单独重试**：`failed` 记录带 `next_retry_at = now + base * 2^(attempts-1)`，
到点后由 `push-retry` 作业扫描重发；也可以手动触发：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/push-records/3/retry
```

`skipped` / `disabled` 是终态，不参与自动重试——它们不是「发失败」，重试没有意义，
反复重试只会刷错误日志。

### 12.7 本地验证邮件（没有 SMTP 账号也能验证）

`app/push/local_sink.py` 是一个只实现 SMTP 最小交互的本地服务器：它会真的收下整封信，
smtplib 走的 EHLO / MAIL FROM / RCPT TO / DATA / MIME 编码全是真实路径，只有最后一跳换成本地进程。

```bash
# 终端 A：起一个监听 2525 的假 SMTP
python -m app.push.local_sink     # 或 python -m app.push.local_sink --port 2525

# 终端 B：把服务指向它（环境变量优先级高于 .env）
SMTP_HOST=127.0.0.1 SMTP_PORT=2525 SMTP_SENDER=noreply@example.com SMTP_USE_SSL=false \
  python -m uvicorn app.main:app --port 8000
```

触发一次生成后，假 SMTP 的 `mails` 里就有完整邮件（`tests/test_stage4_push.py` 就是这么验的，
其中 `test_push_success_delivers_real_email` 会用标准库 `email` 解析出发件人、主题与两个正文分部）。

### 12.7.1 真实邮箱验证（填了自己的 SMTP 之后）

```bash
# 只测 SMTP 通不通：发一封 5 行的短邮件，最快定位「配置/网络」还是「业务数据」的问题
python scripts/send_test_mail.py --to you@example.com

# 只预览内容不发信：打印主题、条目数、纯文本正文前 800 字
python scripts/send_test_mail.py --user-id 2 --dry-run

# 走完整推送链路：取该用户最近一份真实简报，force 重发并写 push_records
python scripts/send_test_mail.py --user-id 2 --full
```

脚本只打印打码后的账号（`24***@qq.com`），密码只从 `.env` 读取、不接受命令行参数
（避免留在 shell 历史里）。三类错误各有对应提示：缺配置、认证失败（535）、端口不通。

### 12.8 稳定性：六类失败的落点

| 失败场景 | 处理 | 落库表现 |
| --- | --- | --- |
| 新闻搜索全部失败 | `search_news` 返回 `ok=false` + 「不要编造」，模型据此说明没搜到 | 任务 `failed`，`error_message` 含原因 |
| 部分源失败 | 只记降级，`stats.feeds_failed` 如实计数 | 任务照常 `success` |
| LLM 请求失败 | 客户端重试 2 次（指数退避），仍失败则任务失败；`AGENT_MODE=openai` 缺 key 直接 400 | 任务 `failed` + 可读原因 |
| Agent 工具执行失败 | 工具返回结构化错误回填给模型自纠，不中断循环 | 轨迹里 `ok=false`，任务可能仍成功 |
| 邮件发送失败 | 状态 `failed` + `last_error` + `next_retry_at`，不重跑 Agent | 任务仍 `success`，推送 `failed` |
| 定时任务重复触发 | 三层幂等（唯一键 / 条件 UPDATE / 队列去重） | 只有一条任务、一封邮件 |
| 服务意外重启 | 启动补跑处理「错过时间点」；僵尸任务由回收作业标记失败并当天重派（最多 3 次） | 无重复简报，无丢天数 |

### 12.9 配置项汇总（第四阶段新增）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SCHEDULER_ENABLED` | `true` | 定时任务总开关 |
| `SCHEDULER_TIME` | `08:00` | 每日执行时间 |
| `SCHEDULER_TIMEZONE` | `Asia/Shanghai` | 时区 |
| `SCHEDULER_CATCH_UP` | `true` | 启动补跑 |
| `SCHEDULER_MAX_CONCURRENCY` | `2` | 生成并发上限 |
| `SCHEDULER_MISFIRE_GRACE` | `3600` | 错过触发的补执行宽限（秒） |
| `SCHEDULER_STALE_MINUTES` | `30` | 僵尸任务判定阈值 |
| `SCHEDULER_RECOVERY_INTERVAL_MINUTES` | `10` | 僵尸任务回收作业的扫描间隔 |
| `PUSH_ENABLED` | `true` | 服务端推送总开关 |
| `PUSH_BASE_URL` | `http://127.0.0.1:8000` | 邮件入口前缀 |
| `PUSH_MAX_ATTEMPTS` | `3` | 最多尝试次数（含首次） |
| `PUSH_RETRY_BACKOFF_SECONDS` | `60` | 退避基数 |
| `PUSH_RETRY_INTERVAL_MINUTES` | `10` | 重试扫描间隔 |
| `SMTP_HOST` / `SMTP_PORT` | 空 / `465` | SMTP 服务器 |
| `SMTP_USER` / `SMTP_PASSWORD` | 空 | 账号与授权码（只从环境变量读） |
| `SMTP_SENDER` | 空（回退 `SMTP_USER`） | 发件人地址 |
| `SMTP_FROM_NAME` | `AI 新闻助手` | 发件人显示名 |
| `SMTP_USE_SSL` | 空（按端口判断） | 是否 SSL 直连 |
| `SMTP_TIMEOUT` | `15` | 连接超时（秒） |

### 12.10 完整功能测试方法

**① 自动化测试（离线、可重复）**

```bash
python -m pytest -v                            # 186 个用例
python -m pytest tests/test_stage4_task_state.py -v   # 幂等 / 并发 / 僵尸恢复 / 补建
python -m pytest tests/test_stage4_push.py -v         # 本地 SMTP 实投 / 跳过分支 / 重试
python -m pytest tests/test_stage4_scheduler.py -v    # 遍历用户 / 单用户隔离 / 补跑
python -m pytest tests/test_stage4_api.py -v          # 概览 / 触发 / 列表 / 全链路
```

第四阶段用例覆盖的验收点：

| 验收要求 | 对应用例 |
| --- | --- |
| 用户可设置和修改订阅偏好 | `test_api.py`、`test_stage4_api.py::test_dashboard_shows_preferences` |
| Agent 自主调用工具搜索整理新闻 | `test_briefing_flow.py::test_agent_trace_shows_autonomous_multi_step` |
| 不同用户获得不同简报 | `test_briefing_flow.py::test_two_users_get_different_briefings` |
| 简报保存到数据库和 Markdown 文件 | `test_manual_trigger_full_chain`（断言文件落盘 + 入库） |
| 定时任务能触发简报生成 | `test_stage4_scheduler.py::test_dispatch_creates_task_for_every_user` |
| 可以邮件推送简报 | `test_stage4_push.py::test_push_success_delivers_real_email` |
| 前端可以查看历史简报 | `scripts/ui_check_stage4.py`（真实浏览器） |
| 用户可手动触发 | `test_manual_trigger_full_chain` / `test_double_click_does_not_create_two_tasks` |
| 不重复生成 / 不重复推送 | `test_same_user_same_day_only_one_task` / `test_push_is_not_repeated` |
| 失败有合理异常处理 | `test_one_user_failure_does_not_block_others` / `test_failed_task_can_be_retried_via_api` |

**② 命令行手工验收（对着 curl 跑一遍）**

```bash
BASE=http://127.0.0.1:8000/api/v1

# 1) 建用户 + 偏好
curl -X POST $BASE/users -H "Content-Type: application/json" \
  -d '{"nickname":"小林","identity":"后端开发实习生","email":"you@example.com"}'
curl -X PUT $BASE/users/1/preferences -H "Content-Type: application/json" \
  -d '{"topics":["AI Agent","MCP"],"keywords":["工具调用"],"exclude_keywords":["融资"]}'

# 2) 手动触发（立刻返回，action=created）
curl -X POST $BASE/users/1/briefings/trigger -H "Content-Type: application/json" -d '{}'
# 3) 轮询状态：pending → running → success
curl $BASE/tasks/1
# 4) 概览 / 历史列表
curl $BASE/users/1/dashboard
curl "$BASE/tasks?user_id=1&page=1&page_size=10"
# 5) 推送状态与重试
curl $BASE/users/1/push-records
curl -X POST $BASE/push-records/1/retry
# 6) 定时任务状态 + 立即派发一次
curl $BASE/scheduler
curl -X POST $BASE/scheduler/run-now
```

**③ 浏览器端到端（真实点击，含截图）**

```bash
python -m pip install playwright    # 复用本机 Edge/Chrome，无需下载浏览器
python scripts/ui_check_stage4.py --base http://127.0.0.1:8000 --user-id 1
```

脚本会走完「概览 → 点击生成 → 等待/生成中 → 成功 → 详情 → 历史列表 → 推送开关 → 配置页回填」，
并断言生成期间按钮被禁用、详情页有可点击链接、历史行同时显示生成与推送状态；
截图输出在 `data/screenshots/stage4/`。

**④ 定时任务不等时钟的验法**

```bash
# 僵尸任务回收（把阈值调到 5 分钟，重启后手动触发一次）
SCHEDULER_STALE_MINUTES=5 python -m uvicorn app.main:app --port 8000
curl -X POST http://127.0.0.1:8000/api/v1/scheduler/recover

# 把时间设成一分钟前，重启服务，观察启动补跑日志
SCHEDULER_TIME=08:00 python -m uvicorn app.main:app --port 8000
# 日志应出现：启动补跑完成：提交 N，跳过 M，失败 0
# 或直接手动派发：
curl -X POST http://127.0.0.1:8000/api/v1/scheduler/run-now
```

### 12.11 实测记录（本机真实跑通）

同一天内跑了两轮完整链路（离线策略 + 本地 SMTP sink + 真实浏览器驱动）：

| 环节 | 结果 |
| --- | --- |
| 手动触发 → 入队 | `action=created`，任务 `pending`，按钮立即禁用 |
| Agent 执行 | 6 轮 / 6 次工具调用：`get_user_preferences → search_news ×2 → fetch_article → write_file → save_briefing`，约 5 秒（**旧流程**：入库与落盘分两次调用，见 12.11.1） |
| 简报入库 | `news_count=4`，文件落盘 `workspace/briefings/2026-11-18-userN.md` |
| 邮件推送 | 本地 SMTP 收下完整 MIME 邮件，主题 `【AI 新闻简报】2026-11-18 · 每日 AI 新闻简报`，含 4 条摘要与 4 个原始链接 |
| 前端列表 | `2026-11-18 \| 每日 AI 新闻简报 \| 4 条 \| 生成成功 \| 已发送 \| 查看` |
| 重复点击 | 第二次触发返回 `already_generated`，只入队一次 |
| 重启补跑 | 启动日志：`启动补跑完成：提交 0，跳过 5，失败 0`（5 个用户当天均已有结论） |
| 僵尸任务自愈 | 两个任务因进程重启停在 `running`，心跳过期后 `POST /scheduler/recover` 返回`回收 2 个僵尸任务，重新派发 2 个`，两个任务最终 `success`（attempts=2、各 4 条新闻、推送 success） |

### 12.11.1 交付合并后的复测（真实 DeepSeek，浏览模式）

改动点：`save_briefing` 自己把 Markdown 写入工作区，模型不再需要用 `write_file`
把同一份几千字正文**再生成一遍**（这一步曾占单次生成约 70% 的耗时）。
下面是一次真实运行（`python -m scripts.briefing_demo --mode openai`）的数据：

| 环节 | 用户 A（AI Agent / MCP） | 用户 B（大模型 / AI 编程） |
| --- | --- | --- |
| 轮数 / 工具调用 | 9 轮 / 18 次 | 8 轮 / 16 次 |
| 搜索次数 | 7 次（1 次浏览 + 6 次补搜） | 5 次（1 次浏览 + 4 次补搜） |
| 抓正文 | 10 次（其中 3 次命中同轮缓存） | 9 次（其中 3 次命中同轮缓存） |
| 端到端耗时 | 46.7 s | 33.6 s |
| 交付 | 1 次 `save_briefing`：入库 + 导出 12.5 KB Markdown，轨迹里没有 `write_file` | 1 次 `save_briefing`：入库 + 导出 8.6 KB Markdown |

省下来的是**一整轮大文本输出**（约 3000~4000 个 out-token），而不是磁盘 IO——
写本地文件本身只有几毫秒。端到端耗时仍主要取决于模型自己决定搜几次、读几篇：
A 这次多搜了 3 次、多读了 3 篇，所以绝对耗时反而高于更早那轮。

### 12.12 常见问题（第四阶段新增）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 点「立即生成」提示未配置订阅偏好 | 关注话题与关键词都为空，无法个性化 | 去「订阅配置」加至少一个领域 |
| 任务一直 `pending` | worker 没启动（跑了非 lifespan 方式），或并发被占满 | 看启动日志有没有「生成任务 worker 已启动」；调大 `SCHEDULER_MAX_CONCURRENCY` |
| 任务一直 `running` | 进程执行中被杀 | 等心跳过期（默认 30 分钟）自动重新领取，或调小 `SCHEDULER_STALE_MINUTES` |
| 到点没生成 | `SCHEDULER_ENABLED=false` / 用户无偏好 / 服务当时没运行 | 查 `/api/v1/scheduler`；服务停机错过则依赖补跑 |
| 邮件 `skipped: 未配置 SMTP_HOST` | 没填 SMTP 配置 | 补齐五项 SMTP 配置后重启；不影响生成与页面查看 |
| 邮件 `failed: 认证失败` | QQ/163 用了登录密码 | 换成邮箱「授权码」 |
| 邮件 `failed: 连接超时` | 端口被封 / 网络不通 | 465 走 SSL、587 走 STARTTLS，确认端口与 `SMTP_USE_SSL` 匹配 |
| 明细页「重新推送」不出现 | 推送状态不是 `failed` | 只有失败记录才给重试按钮，避免误发 |
| 历史列表里老简报消失了 | 老库没有任务记录 | 启动时会自动补建（`backfill_from_briefings`），重启一次即可 |

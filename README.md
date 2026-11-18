# 每日 AI 新闻助手 Agent

一个「按个人口味筛选 AI 新闻」的 Agent 项目。当前完成**第一阶段（用户订阅管理）**与
**第二阶段（Agent 核心与 Function Calling 工具调用）**——Agent 已经能自己决定调用哪些工具、
调用几次、什么时候停下来；真实新闻搜索、定时任务与推送仍留待后续阶段。

> 技术栈：Python 3.11+ · FastAPI · SQLAlchemy 2.0 · SQLite · 原生 HTML/CSS/JS（无框架、无构建）· OpenAI 兼容 Function Calling

---

## 一、当前阶段能力

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 项目分层框架 | ✅ | 接口 / 模型 / 数据访问 / 业务服务 / 前端静态资源 / 配置 分离 |
| 用户信息管理 | ✅ | 创建、查询、修改（含列表辅助接口） |
| 订阅偏好管理 | ✅ | 关注话题、关键词、排除关键词，支持多个词条，整体覆盖保存 |
| 前端配置页 | ✅ | 标签式输入、预设领域、表单校验、刷新后回填 |
| **五个基础工具** | ✅ | list_dir / read_file / search_content / write_file / bash，统一返回格式 + 参数校验 + 异常兜底 |
| **工具注册中心** | ✅ | 装饰器注册、JSON Schema 自动生成、按名动态执行，主循环零 if-else |
| **手写 Agent Loop** | ✅ | 多轮 Function Calling：模型决策 → 执行工具 → 结果回填 → 继续决策 → 自主停止 |
| **安全与护栏** | ✅ | 工作区路径约束、危险命令黑名单、执行超时、输出截断、轮数/次数上限、重复调用提醒 |
| **调试入口与轨迹** | ✅ | `POST /api/agent/run` 返回完整调用记录；`GET /api/agent/tools` 可查 Schema |
| 自动化测试 | ✅ | 73 个用例（19 个第一阶段接口 + 54 个工具/Loop/Agent 接口） |
| 新闻搜索 / 简报生成 / 自动推送 | ⬜ | 后续阶段实现 |

---

## 二、目录结构

```
NewsAgent/
├── app/
│   ├── main.py                 # 应用入口：CORS、异常、路由、静态资源、启动建表
│   ├── core/                   # 基础设施
│   │   ├── config.py           # 配置加载（.env + pydantic-settings），含 Agent 参数
│   │   ├── database.py         # 引擎 / 会话 / Base / get_db / init_db
│   │   ├── response.py         # 统一响应信封 {code, message, data}
│   │   └── exceptions.py       # 业务异常 + 全局异常处理器
│   ├── models/                 # ORM 模型（数据库表的 Python 映射）
│   │   ├── base.py             # 主键与时间戳 Mixin
│   │   ├── user.py             # 用户表
│   │   └── subscription.py     # 订阅头表 + 词条明细表
│   ├── schemas/                # Pydantic 请求/响应模型（接口契约 + 参数校验）
│   │   ├── common.py           # 健康检查等通用结构
│   │   ├── user.py             # 用户相关
│   │   └── subscription.py     # 订阅偏好相关（含词条清洗逻辑）
│   ├── crud/                   # 数据访问层：只做数据库读写，不提交事务
│   │   ├── user.py
│   │   └── subscription.py
│   ├── services/               # 业务服务层：事务边界 + 业务规则
│   │   ├── user_service.py
│   │   └── subscription_service.py
│   ├── agent/                  # ★ 第二阶段：Agent 核心
│   │   ├── registry.py         # 工具注册中心：@tool 装饰器 / Schema 生成 / 动态执行
│   │   ├── result.py           # ToolResult 统一返回格式（ok/summary/data/error/meta）
│   │   ├── context.py          # ToolContext：工作区 + 各工具限额
│   │   ├── safety.py           # 路径约束 / 危险命令拦截 / 环境变量清洗
│   │   ├── prompts.py          # System Prompt（只写原则，不写步骤）
│   │   ├── loop.py             # AgentRunner：手写 Function Calling 循环
│   │   ├── tracer.py           # 运行记录与内存环形缓冲
│   │   ├── schemas.py          # Agent 接口的请求/响应模型
│   │   ├── tools/
│   │   │   ├── filesystem.py   # list_dir / read_file / search_content / write_file
│   │   │   └── shell.py        # bash
│   │   └── llm/
│   │       ├── base.py         # LLMClient 协议 + ToolCall / LLMResponse / LLMError
│   │       ├── openai_client.py# 任意 OpenAI 兼容网关（httpx 直连 + 重试）
│   │       ├── mock_client.py  # 仅用于离线验证链路的规则化假模型
│   │       └── __init__.py     # build_llm_client：auto / openai / mock
│   ├── api/                    # 接口层
│   │   ├── agent.py            # ★ /api/agent/run、/tools、/runs
│   │   ├── deps.py             # 公共依赖（会话、user_id → User）
│   │   └── v1/
│   │       ├── router.py       # v1 路由聚合
│   │       ├── users.py        # /api/v1/users
│   │       └── subscriptions.py# /api/v1/users/{id}/preferences
│   └── static/                 # 前端静态资源（由 FastAPI 直接托管）
│       ├── index.html
│       ├── favicon.svg
│       ├── css/style.css
│       └── js/app.js
├── workspace/                  # ★ Agent 沙箱：所有文件工具与命令的边界
│   ├── README.md
│   ├── notes/{agent_design.md, todo.md}
│   ├── src/{agent_runner.py, news_fetcher.py, utils.py}
│   └── data/sample_news.json
├── tests/                      # pytest 测试
│   ├── conftest.py             # 临时库夹具（不污染开发库）
│   ├── test_api.py             # 第一阶段接口
│   ├── test_tools.py           # 五个工具 + 注册中心 + 安全用例
│   ├── test_agent_loop.py      # 多轮循环、顺序自由、异常与护栏
│   └── test_agent_api.py       # /api/agent/* 接口
├── scripts/
│   ├── e2e_check.py            # 可选：Playwright 驱动的前端自动化冒烟测试
│   └── agent_demo.py           # ★ 三组多步任务的命令行演示
├── data/                       # SQLite 数据库文件目录（自动创建，已 gitignore）
├── requirements.txt
├── .env.example                # 环境变量模板
├── .gitignore
├── pytest.ini
└── README.md
```

### 分层职责与调用方向

```
HTTP 请求
   │
   ▼
api/v1/*.py        接口层：定义路由、注入依赖、组织响应（不写业务逻辑、不碰 SQL）
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

`.env` 中本阶段需要关注两项：

1. `DATABASE_URL`（默认 `sqlite:///./data/news_agent.db`，相对路径会以**项目根目录**为基准解析）；
2. Agent 相关配置——**没有 API Key 也能跑**：`AGENT_MODE=auto` 时若无 `LLM_API_KEY` 会自动降级到
   Mock 模式（仅验证工具注册与调用链路，不代表模型决策能力）。想跑真实模型就填
   `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`（任意 OpenAI 兼容网关均可）。

代码中没有任何硬编码密钥，密钥只从环境变量读取。

### 3. 启动服务

以下命令需在**已激活虚拟环境**的终端中执行（提示符带 `(.venv)`）：

```bash
python -m uvicorn app.main:app --reload --port 8000
```

启动后：

| 地址 | 用途 |
| --- | --- |
| http://127.0.0.1:8000/ | 前端订阅配置页 |
| http://127.0.0.1:8000/docs | Swagger 交互式接口文档 |
| http://127.0.0.1:8000/redoc | ReDoc 文档 |
| http://127.0.0.1:8000/api/health | 健康检查 |

> 首次启动会自动创建 `data/news_agent.db` 及全部数据表，无需手动建库。
> 若 8000 端口被占用，换一个端口即可（如 `--port 8010`），前端用的是相对路径，无需改代码。

### 4. 运行测试

```bash
python -m pytest -v
```

测试使用临时目录中的独立 SQLite 文件，不会动开发库。

---

## 四、数据库设计

### 4.1 users（用户表）

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| id | INTEGER | PK, 自增 | 用户 ID |
| nickname | VARCHAR(50) | NOT NULL, 索引 | 昵称 |
| identity | VARCHAR(200) | NOT NULL | 身份 / 职业描述 |
| email | VARCHAR(255) | NULL, 索引 | 新闻接收邮箱（选填） |
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
| GET | `/api/v1/users/{user_id}` | 获取用户信息 |
| PUT | `/api/v1/users/{user_id}` | 修改用户信息（局部更新） |
| PUT | `/api/v1/users/{user_id}/preferences` | 创建或更新订阅偏好（整体覆盖） |
| GET | `/api/v1/users/{user_id}/preferences` | 获取订阅偏好（未设置时 `data = null`） |
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

页面地址：`http://127.0.0.1:8000/`

1. 填写昵称、身份描述、邮箱；
2. 关注领域可直接点「快速添加」预设，或在输入框内输入后按 <kbd>Enter</kbd> / <kbd>,</kbd> 添加；
3. 关键词、排除关键词同理，点击标签上的 `×` 可删除，输入框为空时按退格删除最后一个；
4. 点击「保存订阅」：首次保存会自动创建用户，之后保存则更新；
5. 用户 ID 记录在 `localStorage`（key: `news_agent_user_id`），**刷新页面会自动回填**；
6. 「新建一份配置」会解除本地关联并清空表单，可创建另一个用户。

支持 <kbd>Ctrl</kbd>/<kbd>Cmd</kbd> + <kbd>S</kbd> 快捷保存；离开页面前若有未保存修改会提示。

---

## 七、常见问题与坑位

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 启动报 `WinError 10048` 端口被占用 | 8000 端口已有服务 | 换端口 `--port 8010`，或结束占用进程 |
| 页面能打开但保存报「无法连接后端服务」 | 前端未通过 FastAPI 托管，或用 `file://` 直接打开了 HTML | 必须访问 `http://127.0.0.1:8000/`，静态资源由后端托管 |
| 刷新后数据没回来 | 浏览器换了无痕窗口 / 清了 localStorage | 页面右上角会显示关联的用户 ID；也可用 `GET /api/v1/users` 找回 |
| 数据库文件出现在奇怪的位置 | 用了相对 `DATABASE_URL` 且启动目录不同 | 已在 `config.py` 统一以项目根目录解析，绝对路径也可直接填 |
| 修改了模型字段但表没变 | `create_all` 只建表、不改表 | 本阶段可删除 `data/news_agent.db` 重建；正式演进请引入 Alembic |
| 中文写库报编码错误 | 客户端未声明 UTF-8 | 请求头带 `Content-Type: application/json`（默认 UTF-8） |

---

## 八、下一阶段衔接点

本阶段已经把后续需要的「数据底座」和「扩展位」留好，第二阶段可以直接接：

1. **偏好读取**：`subscription_service.get_subscription(db, user_id)` 已能把话题/关键词/排除词按类型取回，
   新闻搜索时直接用它构造查询条件与过滤规则。
2. **简报历史表**：新增 `news_briefings`（user_id、简报内容、生成时间、推送状态）即可支撑「历史简报」页面，
   与现有 `users` 通过外键关联。
3. **推送目标**：`users.email` 已可空可存，邮件推送直接读该字段；SMTP 配置已在 `.env.example` 预留。
4. **密钥管理**：`LLM_API_KEY` 已在第二阶段启用（Agent 的 Function Calling 走它），
   `NEWS_API_KEY` 仍留待第三阶段；一律只读 `settings`，禁止写进代码。
5. **定时任务**：建议用 APScheduler（轻量）或系统 crontab 调用一个「生成并推送」的入口，
   任务内复用 `SessionLocal` 独立开会话。
6. **鉴权**：当前无登录，用户身份靠前端 `localStorage` 中的 ID 传递；
   若要上线，需增加 JWT 鉴权并给 `users` 增加唯一标识（邮箱/手机号）与密码字段。

---

## 九、开发约定

- 接口返回一律用 `app/core/response.py` 的 `success()`，不要手写 dict 结构；
- 业务错误抛 `AppError` 子类，不要在接口里 `try/except` 包一层；
- 事务只在 service 层 `commit()`，crud 层只用 `flush()`；
- 新增字段先改 `models`，同步改 `schemas`，再改 `crud/service`，最后补 `tests`。

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

### 10.4 工具注册中心

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

### 10.5 异常与安全控制

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

### 10.6 配置项

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

### 10.7 接口与调试

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

### 10.8 Mock 模式的边界（重要）

`AGENT_MODE=mock` 时使用的 `MockLLMClient` 用**正则抽取任务意图 + 固定优先级**来模拟决策。
它证明的是「Schema 生成 → tool_calls 解析 → 执行 → 结果回填 → 多轮循环」这条链路是通的，
**不能**用来证明「模型能自主规划」。

要验证真正的自主性，配置 `LLM_API_KEY` 后重跑同样三个任务——主循环、工具、测试一行都不用改，
这正是 LLM 客户端抽象层的目的。另外 `tests/test_agent_loop.py` 用 `ScriptedLLM` 故意喂三种
人为规定的调用顺序驱动同一份循环，验证「循环不预设流程」这个性质本身。

### 10.9 常见问题（第二阶段新增）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 启动日志提示「Agent 运行在 Mock 模式」 | 未配置 `LLM_API_KEY`，`auto` 降级 | 填 `.env` 里的 key，或显式 `AGENT_MODE=mock` 做离线验证 |
| 接口返回 400「未配置 LLM_API_KEY」 | 请求里 `llm_mode` 指定了 `openai` | 去掉该字段或补 key |
| `bash` 输出中文乱码 | Windows 控制台编码 | 工具已按 utf-8→gbk 顺序解码；仍有问题时给命令加 `chcp 65001` 之外的写法或改用 Python 脚本 |
| 命令被拦「被安全策略拦截」 | 命中危险命令黑名单 | 换只读命令；黑名单规则见 `app/agent/safety.py` |
| 提示「路径越界」 | 访问了 `workspace/` 之外 | 这是设计行为，把文件放进工作区再操作 |
| `status=max_steps_reached` | 模型反复调工具停不下来 | 调大 `AGENT_MAX_STEPS`，或把任务描述得更具体 |
| 运行记录查不到 | 内存环形缓冲只留最近 50 次，重启即清空 | 调整 `AGENT_KEEP_RUNS`；需要持久化则落库 |

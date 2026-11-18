# 每日 AI 新闻助手 Agent

一个「按个人口味筛选 AI 新闻」的 Agent 项目。当前完成**第一阶段（用户订阅管理）**、
**第二阶段（Agent 核心与 Function Calling 工具调用）**与
**第三阶段（新闻采集、个性化筛选与简报生成）**——Agent 已经能自己决定搜什么词、搜几次、
读不读原文、什么时候收笔，并把简报写成文件、登记入库；定时任务与消息推送留待后续阶段。

> 技术栈：Python 3.11+ · FastAPI · SQLAlchemy 2.0 · SQLite · 原生 HTML/CSS/JS（无框架、无构建）· OpenAI 兼容 Function Calling · RSS/Atom（零第三方解析库）

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
| **新闻业务工具** | ✅ | search_news / fetch_article / get_user_preferences / save_briefing，与文件工具同一注册表 |
| **RSS 采集层** | ✅ | 10 个中英文源、并发抓取、单源失败不影响整体、RSS 2.0 / Atom / RDF 全格式 |
| **个性化筛选** | ✅ | 偏好读库 → 语义判断交给模型；时间窗口、排除词、跨调用去重、标题近似去重由工具兜底 |
| **简报生成与入库** | ✅ | 结构化 Markdown 校验（章节/条目/链接白名单）+ `news_briefings` 表按「用户+日期」去重 |
| 自动化测试 | ✅ | 125 个用例（19 接口 + 54 工具/Loop + 26 新闻能力层 + 18 新闻工具 + 8 简报链路） |
| 定时任务 / 消息推送 | ⬜ | 后续阶段实现 |

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
│   │   ├── subscription.py     # 订阅头表 + 词条明细表
│   │   └── briefing.py         # ★ 新闻简报表（user_id + briefing_date 唯一）
│   ├── schemas/                # Pydantic 请求/响应模型（接口契约 + 参数校验）
│   │   ├── common.py           # 健康检查等通用结构
│   │   ├── user.py             # 用户相关
│   │   ├── subscription.py     # 订阅偏好相关（含词条清洗逻辑）
│   │   └── briefing.py         # ★ 简报生成/查询契约
│   ├── crud/                   # 数据访问层：只做数据库读写，不提交事务
│   │   ├── user.py
│   │   ├── subscription.py
│   │   └── briefing.py         # ★
│   ├── services/               # 业务服务层：事务边界 + 业务规则
│   │   ├── user_service.py
│   │   ├── subscription_service.py
│   │   └── briefing_service.py # ★ 用户画像 / 任务书 / 简报 upsert
│   ├── news/                   # ★ 第三阶段：新闻能力层（与 Agent 解耦）
│   │   ├── feeds.py            # 源目录（ID / 名称 / 语种 / 分类）
│   │   ├── http.py             # 抓取：UA 伪装、字节上限、SSRF 防护、错误归一
│   │   ├── rss.py              # RSS 2.0 / Atom / RDF 解析 + 时间归一 + 文本清洗
│   │   ├── article.py          # 正文抽取：去噪 → 段落 → 密度兜底（full/excerpt/summary）
│   │   ├── dedupe.py           # URL 规范化 + 标题归一 + 二元组预筛 + 相似度
│   │   ├── filter.py           # 时间窗口 / 关键词匹配 / 相关度打分 / 排除词
│   │   ├── providers.py        # 搜索提供方协议 + RSS 实现 + 扩展注册点
│   │   ├── state.py            # 一次运行内的候选池（跨调用去重 + 链接白名单）
│   │   └── briefing.py         # 简报结构校验（章节 / 条目 / 链接 / 截断检测）
│   ├── agent/                  # ★ 第二阶段：Agent 核心
│   │   ├── registry.py         # 工具注册中心：@tool 装饰器 / Schema 生成 / 动态执行
│   │   ├── result.py           # ToolResult 统一返回格式（ok/summary/data/error/meta）
│   │   ├── context.py          # ToolContext：工作区 + 限额 + 用户/新闻状态/会话工厂
│   │   ├── safety.py           # 路径约束 / 危险命令拦截 / 环境变量清洗
│   │   ├── prompts.py          # System Prompt（通用 + 简报专用，只写原则不写步骤）
│   │   ├── loop.py             # AgentRunner：手写 Function Calling 循环
│   │   ├── briefing_task.py    # ★ 简报编排：任务书 → 上下文 → 跑循环 → 回读结果
│   │   ├── tracer.py           # 运行记录与内存环形缓冲
│   │   ├── schemas.py          # Agent 接口的请求/响应模型
│   │   ├── tools/
│   │   │   ├── filesystem.py   # list_dir / read_file / search_content / write_file
│   │   │   ├── shell.py        # bash
│   │   │   └── news.py         # ★ search_news / fetch_article / get_user_preferences / save_briefing
│   │   └── llm/
│   │       ├── base.py         # LLMClient 协议 + ToolCall / LLMResponse / LLMError
│   │       ├── openai_client.py# 任意 OpenAI 兼容网关（httpx 直连 + 重试）
│   │       ├── mock_client.py  # 仅用于离线验证链路的规则化假模型
│   │       ├── mock_news.py    # ★ 简报场景的离线策略（状态机 + 模板，非模型能力）
│   │       └── __init__.py     # build_llm_client：auto / openai / mock
│   ├── api/                    # 接口层
│   │   ├── agent.py            # ★ /api/agent/run、/briefings、/tools、/runs
│   │   ├── deps.py             # 公共依赖（会话、user_id → User）
│   │   └── v1/
│   │       ├── router.py       # v1 路由聚合
│   │       ├── users.py        # /api/v1/users
│   │       ├── subscriptions.py# /api/v1/users/{id}/preferences
│   │       └── briefings.py    # ★ /api/v1/briefings（列表 / 详情）
│   └── static/                 # 前端静态资源（由 FastAPI 直接托管）
│       ├── index.html
│       ├── favicon.svg
│       ├── css/style.css
│       └── js/app.js
├── workspace/                  # ★ Agent 沙箱：所有文件工具与命令的边界
│   ├── README.md
│   ├── briefings/              # ★ 生成的简报 Markdown（按「日期-userID.md」命名）
│   ├── notes/{agent_design.md, todo.md}
│   ├── src/{agent_runner.py, news_fetcher.py, utils.py}
│   └── data/sample_news.json
├── tests/                      # pytest 测试
│   ├── conftest.py             # 临时库夹具 + 离线新闻网络夹具
│   ├── test_api.py             # 第一阶段接口
│   ├── test_tools.py           # 五个工具 + 注册中心 + 安全用例
│   ├── test_agent_loop.py      # 多轮循环、顺序自由、异常与护栏
│   ├── test_agent_api.py       # /api/agent/* 接口
│   ├── test_news_core.py       # ★ RSS/正文/去重/时间/简报校验 单元测试
│   ├── test_news_tools.py      # ★ 四个新闻工具 + 异常场景
│   └── test_briefing_flow.py   # ★ 双用户端到端 + 同日报简报去重
├── scripts/
│   ├── e2e_check.py            # 可选：Playwright 驱动的前端自动化冒烟测试
│   ├── agent_demo.py           # ★ 三组多步任务的命令行演示
│   └── briefing_demo.py        # ★ 两组用户生成简报的命令行演示
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

### 今日简报区块（第三阶段新增）

页面下方新增「今日简报」卡片，不用再靠 curl 看结果：

| 元素 | 说明 |
| --- | --- |
| 状态行 | 显示「今日尚未生成 / 已生成 · 日期」、条数与生成时间 |
| 「生成今日简报」按钮 | 未保存用户时禁用；已生成时文案变为「重新生成」 |
| 进度提示 | 真实模型要跑 1~3 分钟，显示已用秒数；期间按钮转圈且不可重复点击 |
| 运行摘要 | 生成完成后展示 搜索次数 / 候选条数 / 读原文篇数 / 轮数 / 工具调用 / 耗时 / 模型 |
| 简报正文 | 前端用 40 行左右的原生 JS 渲染 Markdown（标题、列表、引用、粗体、行内代码、链接） |

两个实现上的注意点：

- **不引第三方 Markdown 库**：页面要求零依赖、内网可离线打开，因此只实现简报用到的语法；
  渲染前先 `escapeHtml` 再插标签——正文来自大模型，直接塞 `innerHTML` 会有 XSS 风险。
- **日期用本地时区拼**：`new Date().toISOString()` 是 UTC，晚上 8 点后会把「今天」算成第二天，
  导致读不到当天简报。改用 `getFullYear/getMonth/getDate` 拼接。

生成接口在 `/api/agent/briefings`（不在 `/api/v1` 下），前端为此给请求封装加了 `base` 参数。

验收时可以直接用 URL 参数指定用户，省去手动改 `localStorage`：

```text
http://127.0.0.1:8000/?user_id=3    # 用户 A：AI Agent / MCP
http://127.0.0.1:8000/?user_id=4    # 用户 B：大模型 / AI 编程
```

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
| `fetch_article` | `url`、`max_chars` | 抓详情页，抽标题/来源/时间/正文 | HTTP 错误、非 HTML、页面无正文、内网地址一律失败并说明；返回 `content_kind` 与 `warnings` |
| `save_briefing` | `user_id`、`content`、`briefing_date`… | 校验后按「用户+日期」upsert | 结构不全 → 拒收并列出缺失项；链接不在本轮检索结果内 → 拒收并给出可用链接 |

工具描述（LLM 靠它选工具）里写明了幂等语义、能返回什么、什么时候该放弃，例如
`search_news` 会明确说「一次搜索只覆盖一个关键词，多个主题应分多次搜索」。

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
  {"index":5,"tool":"write_file","arguments":{"path":"briefings/2026-11-18-user1.md"},"ok":true},
  {"index":6,"tool":"save_briefing","arguments":{"user_id":1},"ok":true}
]
```

### 11.8 配置项（第三阶段新增）

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `NEWS_FEEDS` | 空（用内置 10 个源） | 逗号分隔的源 ID，如 `qbitai,openai` |
| `NEWS_MAX_ITEMS_PER_FEED` | 30 | 单源最多读取条数 |
| `NEWS_FETCH_TIMEOUT` | 12.0 | 抓取超时（秒） |
| `NEWS_HTTP_MAX_BYTES` | 3000000 | 单个响应体上限（OpenAI 的 Feed 有 800KB+） |
| `NEWS_SEARCH_MAX_ITEMS` | 12 | `search_news` 默认返回上限 |
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
| `search_news` 返回 0 条但明明有新闻 | `date_range=today` 太严，或命中排除词 | 看 `stats` 里的 `out_of_date_range` / `excluded_by_keyword`，换 `3d` 或换词 |
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

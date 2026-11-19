# NewsAgent 新闻订阅推送链路优化 · 前后对比技术文档

| 项 | 内容 |
| --- | --- |
| 文档日期 | 2026-11-19 |
| 项目 | `D:\PythonProject\NewsAgent`（FastAPI + APScheduler + SQLite + RSS + DeepSeek Agent） |
| 对比范围 | 定时派发 → RSS 采集 → Agent 生成 → 简报交付（入库/落盘）→ 邮件推送 |
| 真实模型 | `deepseek-chat`（OpenAI 兼容接口），`AGENT_MODE=openai` |
| 测量入口 | `python -m scripts.briefing_demo --mode openai --verbose`；耗时取 `RunRecord.duration_ms`，token 取运行结束日志 `tokens=in=… out=… total=…`，上下文字符数取循环压缩日志 |
| 当前测试状态 | `199 passed` |

> **数据口径声明（先读这一段）**
> 本项目的耗时由模型自主决定「搜几次、读几篇」主导，**不同轮次不是严格 A/B 基线**。
> 因此文档只做两类对比：① 同一环节改造前后的结构性对比（如交付从 2 次调用变 1 次）；
> ② 同一口径下的实测数字并列展示。**不把跨轮次的端到端秒数差当作某个改动的净收益。**
> 所有秒数均来自本机真实运行日志，未做推算或补齐；估算值一律标注「预计 / 结构性」。

---

## 0. 一页速览

| 维度 | 优化前 | 优化后 | 性质 |
| --- | --- | --- | --- |
| 交付方式 | `write_file` → `save_briefing` 两次调用，**同一份正文生成两遍** | `save_briefing` 一次完成校验 + 入库 + 落盘 | 结构性，已实测确认轨迹中无 `write_file` |
| 单次生成的大文本输出 | 2 份约 2.5 千字正文 | 1 份 | 结构性 |
| 交付两步模型耗时 | 23s + 13s = **36s（约占单次生成 70%）** | 只保留一次生成 | 实测（剖析轮逐轮时间戳） |
| 搜索次数 | 19~29 次（关键词模式反复试词） | 4~7 次（先浏览总览再补搜） | 实测（不同轮次并列） |
| 轮数 / 工具调用 | 15 轮 / 37 次 | 8~9 轮 / 13~18 次 | 实测（不同轮次并列） |
| 上下文峰值 | 42379 字符（超 40000 预算） | 36563 / 38564 字符（守住预算） | 实测 |
| 运行结局 | 频繁 `max_steps` / `max_tool_calls` 失败 | `completed` | 实测 |
| RSS 重复抓取 | 每次搜索全量重抓 230 条，1.3~3.2s × 4~8 次 | TTL 缓存命中 0.000s + 共享连接池 | 实测 |
| 慢源 | 一个源 13.04s 拖住整批 | 8s 批次截止预算，超时按失败计入报告 | 实测 |
| 推送状态自愈 | `skipped` 是终态，配置补齐后永不补发 | `requeue_config_skipped()` 自愈 + 精确诊断 | 代码行为 |
| 密钥配置 | 写在 `.env`，被编辑器旧缓冲静默覆盖 → 退回 Mock | `.env.local` 覆盖 + 启动校验 + 指纹日志 | 代码行为 |

---

## 1. 链路与测量口径

### 1.1 链路分段

```
APScheduler 定时/手动触发
   │ ① 派发：条件 UPDATE 抢任务，写 task 行（毫秒级）
   ▼
RSS 采集层（app/news）
   │ ② 10 源并发 → 解析 230 条 → 时间过滤 / 去重 / 打分
   ▼
Agent 生成层（app/agent）
   │ ③ 读偏好 → search_news ×N → fetch_article ×M → 写正文 → 交付
   │    每轮都要把完整历史上下文重发给模型
   ▼
交付层（app/agent/tools/news.py + app/services/briefing_service.py）
   │ ④ 结构校验 → 链接白名单 → upsert 入库 → 导出 Markdown
   ▼
推送层（app/push）
   │ ⑤ 渲染 HTML → SMTP → 记录 push 状态（失败退避重试）
   ▼
前端列表 / 详情
```

### 1.2 分段实测（改动前的全链路剖析，单用户 51.1s / 160,115 tokens）

| 阶段 | 实测 | 占比 | 说明 |
| --- | --- | --- | --- |
| 调度派发 → 建任务 | 毫秒级 | ~0% | 只做 DB 写入 |
| 采集（首次搜索） | **8.0s 冷 / 0ms 热** | 13% | 10 源并发，被极客公园 7.28s 拖到批次预算上限 |
| Agent 生成 | **≈46s / 9 轮** | 90% | tokens：in 151,042 / out 9,073 |
| 简报落库 | 13ms | ~0% | 5 条，upsert |
| 邮件渲染 | <1ms | — | HTML 6315 字符 |
| SMTP 推送 | **~0.3–0.5s** | ~1% | QQ 465 握手 266 / 281 / 286ms；本地完整会话 40ms，邮件 16KB |

工具总耗时只有 **4.4s**（采集 2.4s、抓正文 1.3s、写文件 4ms、落库 13ms），
**其余 46s 全在等模型**。推送这一段从来不是瓶颈。

### 1.3 批量行为

- worker 并发 `SCHEDULER_MAX_CONCURRENCY=2`，N 个用户 ≈ `⌈N/2⌉ × 单次耗时`。
- 实测一次派发 6 个用户：08:51:05 → 08:54:08 全部完成（3 分钟），单任务 41~66s，与公式吻合。
- 注意：进程反复重启会触发僵尸任务回收重派（曾出现 `attempts=5` / `attempts=3`），
  每重跑一次 ≈ 51s + 16 万 tokens，排查时别把 start/finish 时间差误读为链路性能。

---

## 2. 优化前：问题清单

| # | 层 | 现象 | 实测证据 | 后果 |
| --- | --- | --- | --- | --- |
| P1 | 交付 | 同一份正文写两遍 | 逐轮时间戳：`write_file` 23s → `save_briefing` 13s，合计 36s / 70% | 用户等待翻倍、成本翻倍、两份产物可能不一致 |
| P2 | 检索 | 默认 `today` 窗口与 RSS 滚动节奏错位 | 230 条候选中「今天 00:00 后」仅 54 条，08:00 定时更少 | 每个关键词只命中 0~3 条 → 模型反复换词 19~29 次 |
| P3 | Agent | 上下文只增不减 | 峰值 42379 字符 > 40000 预算，40 次工具调用 | 逼近上限、触发 `max_steps` / `max_tool_calls` |
| P4 | 采集 | 每次搜索全量重抓 | 连续 3 次 `provider.search()` = 1.98 / 3.20 / 1.32s，每次重解析 230 条 | 一次生成 4~8 次重复网络工作，且易被源站 403 |
| P5 | 采集 | 慢源拖住整批 | `pool.map` 等最慢源；极客公园单源 13.04s，`httpx.Timeout` 是每阶段而非总时长 | 一批 10 源被拖到 10s+ |
| P6 | 推送/配置 | `skipped` 是终态、密钥被静默覆盖 | 邮件没发出；抬头写「离线策略（Mock 模式）」 | 用户看不到简报，且不知道差哪一项配置 |

---

## 3. 优化后：改动清单

| 编号 | 环节 | 改动 | 主要文件 | 可验证效果 |
| --- | --- | --- | --- | --- |
| A1 | 采集 | 进程内 TTL 缓存（key=源集合+单源上限，每键一把锁 + 双检），并发用户共享一次抓取；`clear_cache()` | `app/news/providers.py` | 第 2 次起 0.000s（原 1.3~3.2s） |
| A2 | 采集 | 进程级共享 `httpx.Client`（20 连接 / 10 保活），lifespan 关闭 | `app/news/http.py`、`app/main.py` | A/B：2.28/2.35s → 1.92/1.15s |
| A3 | 采集 | `submit + wait(timeout)` 批次截止预算，超时源计入 `SourceReport`，`shutdown(wait=False)` | `app/news/providers.py` | 整批不被单个 13s 慢源拖住 |
| B1 | Agent | 上下文三档压缩：精简版 → 压最近 → 存根；**只替换 `content`，绝不删消息**（否则 `tool_call_id` 配对会 400） | `app/agent/loop.py` | 42379 → 36563，守住 40000 |
| B2 | 检索 | `date_range` 默认 `today` → `last_24h`；任务书写明覆盖时段；简报结构新增「覆盖时段」 | `tools/news.py`、`briefing_service.py`、`news/briefing.py`、`mock_news.py` | 窗内候选 54 → 85/86 条，状态从 `max_steps` 变 `completed` |
| B3 | 检索 | `stats.pool_window` / `returned_so_far` 给出「该收手」的判据 | `app/agent/tools/news.py` | 模型可判断池子已看全 |
| B4 | 检索 | 浏览模式：`keyword` 可选，倒序返回**仅标题/链接/来源/时间**，`NEWS_BROWSE_MAX_ITEMS=40` | `app/agent/tools/news.py` | 搜索 19~29 → 4 次，轮数 15 → 8 |
| B5 | 抓取 | 同轮 `fetch_article` 缓存，重复读同一篇不再下载 | `app/news/state.py`、`tools/news.py` | 命中时 0ms，且不重复计流量 |
| C1 | **交付** | **`save_briefing` 自己落盘**，任务书与工具描述同步删掉 `write_file` 步骤 | `tools/news.py`、`briefing_service.py`、`briefing_task.py`、`mock_news.py` | 轨迹中 `save_briefing` 恰好 1 次、无 `write_file`；文件由工具导出 |
| C2 | 交付 | 导出失败降级为 warning（越界 / 超限 / OSError），不回滚入库 | `tools/news.py` | 数据不丢，调用方通过 `file_exported` 知情 |
| D1 | 配置 | `env_file=(".env", ".env.local")`；启动校验 openai 模式缺 key 直接 `RuntimeError`；打印密钥指纹 | `app/core/config.py`、`app/main.py` | 不再静默退回 Mock |
| D2 | 推送 | 精确 `smtp_missing_items` 文案、`requeue_config_skipped(days=3)`、重试前先自愈 | `push_service.py`、`crud/push.py`、`scheduler/jobs.py` | 配置补齐后自动补发被跳过的简报 |
| D3 | 可观测 | 运行结束日志加 token 统计；`briefing_demo.py --verbose` | `loop.py`、`scripts/briefing_demo.py` | 每轮成本直接可见，不用猜 |

---

## 4. 前后对比（核心）

### 4.1 调用序列

```
【优化前】
get_user_preferences
  → search_news ×19~29（关键词试词，today 窗口）
  → fetch_article ×N（同 URL 可能重复下载）
  → write_file(content = 整份 2.5k 字 Markdown)      ← 第 1 次生成正文，23s
  → save_briefing(content = 同一份 Markdown 再发一遍) ← 第 2 次生成正文，13s
  → final

【优化后】
get_user_preferences
  → search_news()               ← 不传 keyword，一次浏览拿全窗内清单（仅标题）
  → search_news(keyword=…) ×0~2 ← 只有清单明显缺方向时才补搜
  → fetch_article ×M（同轮缓存）
  → save_briefing(content = 整份 Markdown)            ← 唯一一次生成，工具内部入库 + 落盘
  → final
```

### 4.2 关键指标

| 指标 | 优化前 | 优化后 | 说明 |
| --- | --- | --- | --- |
| 交付调用次数 | 2（`write_file` + `save_briefing`） | 1（`save_briefing`） | 结构性，轨迹可验证 |
| 整份正文生成次数 | 2 | 1 | 结构性 |
| 交付两步模型耗时 | 23s + 13s = 36s | 13s 量级（仅一次生成） | 剖析轮逐轮时间戳；改动后未做同基线隔离计时 |
| 搜索次数 | 19~29 次 | 4~7 次 | 不同轮次并列，模型自主决策 |
| 轮数 / 工具调用 | 15 轮 / 37 次 | 8~9 轮 / 13~18 次 | 同上 |
| 上下文峰值 | 42379（超预算） | 36563 / 38564（守住） | 三档压缩生效 |
| 窗内候选 | 54 条（today） | 85~86 条（last_24h） | 窗口与内容节奏对齐 |
| 成稿条数 | 3~4 条 | 5~6 条 | 质量提升，非耗时指标 |
| 运行结局 | `max_steps` / `max_tool_calls` | `completed` | 完成率提升 |
| 单用户端到端 | 62~74s（关键词模式）/ 51.1s（剖析轮） | 33.6s ~ 46.7s | **不是严格 A/B，见 6.3** |

### 4.3 交付环节专项（本次改动重点）

| 对比项 | 优化前 | 优化后 |
| --- | --- | --- |
| 数据流向 | 模型 → `write_file` 参数 → 磁盘；模型 → `save_briefing` 参数 → DB | 模型 → `save_briefing` 参数 → DB + 磁盘 |
| 一致性 | 两份独立生成，理论上可能不一致（模型第二次复述可能改写） | 单一来源，DB 与文件**逐字节相同** |
| 磁盘成本 | 由模型「输出」一份 2.5 千字（≈23s） | 工具本地写 12.5KB（**4ms**） |
| 失败语义 | `write_file` 失败 → 模型可能重试或放弃；落库失败 → 文件已存在但库里没有 | 入库成功即 `ok: true`；导出失败只加 warning，`file_exported: false` |
| 路径来源 | 模型自己拼 `path` | 工具按 `briefing_relative_path()` 生成，或接受可选 `file_path` |
| 离线策略（Mock） | 也要先 write_file 再 save_briefing | 与真实模型同路径，只调 `save_briefing` |
| 轨迹可验证性 | 需检查两次调用参数是否一致 | 断言 `save_briefing == 1` 且 `write_file == 0` 即可 |

### 4.4 可靠性对比

| 场景 | 优化前 | 优化后 |
| --- | --- | --- |
| 导出路径越界 | 工具报错，模型可能反复重试 | 入库照常成功，warning 说明原因，`file_exported=false` |
| 内容超写入上限 | `write_file` 直接拒收，简报交付失败 | 入库照常成功，仅不导出副本 |
| SMTP 配置缺失 | 记录 `skipped` 终态，补齐后永不补发 | 精确诊断缺哪一项 + 自动自愈补发 |
| `.env` 被编辑器覆盖 | 静默退回 Mock，抬头写「离线策略」 | `.env.local` 覆盖，启动校验 + 指纹日志立刻暴露 |
| 慢源 13s | 整批被拖住 | 8s 截止，按失败计入报告，其余源照常返回 |

---

## 5. 本次改动的设计细节

### 5.1 前后代码对照

**优化前 —— 任务书要求两步（`app/services/briefing_service.py`）**

```python
"1. 用 write_file 把 Markdown 简报保存到工作区 `briefings/2026-11-19-user3.md`。",
"2. 调用 save_briefing 把同一份内容登记进数据库（同一用户同一天只保留一份）。",
```

**优化后 —— 合并为一步**

```python
"调用一次 save_briefing，把完整 Markdown 正文作为 content 传进去："
"它会校验结构、登记入库，并把同一份内容导出到工作区 `briefings/2026-11-19-user3.md`。",
"**不要再用 write_file 写一遍同样的内容**——那样等于把几千字正文重复生成两次，",
"既慢又可能两份不一致。save_briefing 的返回里会告诉你是否导出成功。",
```

**`save_briefing` 内部（入库后追加）**

```python
exported, export_error = _export_briefing_markdown(args.content, file_path, ctx)
if export_error:
    warnings.append(f"Markdown 导出失败：{export_error}")
...
data={
    ...,
    "file_path": file_path,
    "file_exported": exported,   # 调用方可据此判断副本是否真的存在
    "warnings": warnings,
}
```

`_export_briefing_markdown()` 走 `ctx.resolve()` 做越界校验，按 `write_max_bytes` 限流，
父目录自动创建，写 UTF-8 / LF。

### 5.2 三个设计取舍

1. **导出失败不回滚入库。** `upsert_briefing()` 已经 commit，文件只是副本。
   为一次磁盘 IO 失败把整次生成判成失败，会让用户连页面上都看不到简报。
   降级成 warning + `file_exported=false` 是更合理的语义。
2. **保留 `write_file` 工具。** 它是第二阶段的通用文件工具，其他任务（整理笔记、
   写 summary）还在用；只是新闻简报这条链路不再走它。
   `briefing_task._last_written_file()` 仍兼容旧轨迹里的 `write_file.path`。
3. **提示词 + 工具描述双保险。** 只改任务书，真实模型仍可能按习惯调 `write_file`；
   所以 `save_briefing` 的 `@tool_registry.tool(description=…)` 里也写了
   「**只需调用本工具一次，不要再用 write_file 写同一份内容**」，
   离线策略 `mock_news` 同步删掉该步骤，两条路径一致。

### 5.3 顺带的一处提示强化

`fetch_article` 命中同轮缓存时，返回文案从「直接复用缓存正文」改成
「正文就是上一次返回的那份，请直接使用，不要为同一个 URL 再调用 fetch_article」。
实测模型会为「确认一下」重复读同一篇，每轮白等 2~4s；缓存消除了下载，但消不掉这一轮 LLM 往返。

---

## 6. 实测记录

### 6.1 分阶段（每行标注各自的基线，不做跨行相减）

| 阶段 | 搜索 | 轮数 | 工具调用 | 耗时 | 状态 | 上下文峰值 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 关键词模式（优化前） | 19~29 | 15 | 37 | 62~74s | 常 `max_steps` | 42379（超预算） | 两档压缩，29 次搜索 |
| 三档压缩后 | 19 | — | 37 | 90s | — | 36563 | 守住预算，但耗时上升 |
| `last_24h` 窗口后 | 21 | — | — | 61s | `completed` | 38564 | 候选 54 → 86，成稿 6 条 |
| 浏览模式（带摘要 60 条） | 5 | 11 | 15 | 74s | — | — | 摘要太重，反而更慢 |
| 浏览模式（去摘要 40 条） | 4 | 8 | 13~15 | **51s** | `completed` | 37662 | 触发 4 次压缩 |
| 全链路剖析轮 | 3 | 9 | — | 51.1s | `completed` | 37662 | in 151,042 / out 9,073 / total 160,115 |
| **交付合并后 · 用户 A** | 7 | 9 | 18 | **46.7s** | `completed` | — | 10 次抓取，3 次命中缓存；导出 12.5KB |
| **交付合并后 · 用户 B** | 5 | 8 | 16 | **33.6s** | `completed` | — | 9 次抓取，3 次命中缓存；导出 8.6KB |

> 结论：真正省时间的是「**少轮次 + 每轮上下文小**」，不是单纯少搜索。
> 带摘要的浏览模式虽然把搜索降到 5 次，却因为每轮上下文变重而回到 74s。

### 6.2 交付合并后的产物校验

- 轨迹里 `save_briefing` 恰好 1 次、`write_file` 0 次，summary 显示「已导出 briefings/…」。
- 导出文件抬头：`> 生成方式：Agent 模式（deepseek-chat）…`，覆盖时段、新闻条数、链接均正确。
- 文件大小 12.5KB / 8.6KB，与入库 `content` 同源。

### 6.3 为什么用户 A 的 46.7s 没有比剖析轮的 51.1s 低很多

剖析轮里模型做了 3 次搜索、约 10s 花在选材；本次用户 A 自主决定搜 7 次、抓 10 篇，
多出来的 4 次搜索 + 若干轮 LLM 往返，把交付省下的那一轮大文本生成抵消了大半。
**「少生成一份正文」是固定的结构性收益，端到端秒数仍由模型自主决策主导**——
这也是本文档不宣称「51s → 28s 已实测达成」的原因（-45% 是改动前按耗时结构做的预计值）。

---

## 7. 验证与回归

| 项 | 内容 |
| --- | --- |
| 全量测试 | `199 passed`（改动前 197） |
| 新增用例 | `test_save_briefing_exports_markdown_itself`（导出内容与入库一致）、`test_save_briefing_export_outside_workspace_degrades`（越界只降级、入库不受影响） |
| 改写用例 | `test_agent_trace_shows_autonomous_multi_step`：断言 `save_briefing == 1`、`write_file not in tools`、文件真实落盘 |
| 兼容用例 | 上下文三档压缩、超预算源、浏览模式、抓取缓存、配置自愈等既有测试保持通过 |
| 复现命令 | `python -m pytest`；`python -m scripts.briefing_demo --mode openai --verbose --date 2026-11-19` |

---

## 8. 遗留与下一步

| 项 | 状态 | 说明 |
| --- | --- | --- |
| `SMTP_PASSWORD` | **待用户处理** | `.env.local` 里仍是注释（QQ 邮箱授权码，不是登录密码）。填完重启进程，`requeue_config_skipped()` 会自动补发今天被跳过的简报 |
| 严格 A/B 计时 | 未做 | 需要固定同一用户、同一批新闻、冻结模型决策（或直接对「交付两步」单独计时）才能给出净收益秒数 |
| 同轮多工具串行 | 未改 | `loop.py` 顺序 `await`，一次返回 3 个 `fetch_article` 会白等约 1s；缓存后影响变小 |
| 重复 `fetch_article` 调用 | 已缓解未根治 | 服务端缓存消掉下载，但模型仍可能多跑一轮；已用返回文案劝阻 |
| 冷采集预热 | 未改 | 首次搜索 8.0s，可在服务启动时预热一次 TTL 缓存，或把批次预算 8s → 5s |
| 批量关键词 | 未改 | 浏览模式已覆盖主要场景，暂不需要 |

---

## 9. 附录：新增配置项速查

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| `NEWS_FEED_CACHE_TTL` | 300 | RSS 结果 TTL（秒），0 = 关闭 |
| `NEWS_SEARCH_DEADLINE` | 8 | 单次搜索的批次截止预算（秒） |
| `AGENT_CONTEXT_BUDGET_CHARS` | 40000 | 上下文字符预算，超出触发三档压缩 |
| `AGENT_TOOL_RESULT_KEEP_FULL` | 3 | 保留完整结果的最近 N 条 |
| `NEWS_BROWSE_MAX_ITEMS` | 40 | 浏览模式单次返回的清单上限 |
| `NEWS_SEARCH_MAX_ITEMS` | — | 关键词模式返回上限 |
| `LLM_API_KEY` / `SMTP_USER` / `SMTP_PASSWORD` | — | 放在 `.env.local`（`.gitignore` 已包含），覆盖 `.env` |

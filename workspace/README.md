# 工作区说明（Agent Sandbox）

这个目录是「每日 AI 新闻助手」Agent 的**唯一可操作沙箱**：

- 五个工具（list_dir / read_file / search_content / write_file / bash）的路径都被限制在这里；
- `bash` 的工作目录固定为本目录，且禁止 `cd ..`、禁止删除/格式化/改注册表等危险命令；
- 写入产物（例如 `summary.md`、`todo_report.md`）也会落在这里，方便查看 Agent 到底做了什么。

目录内容是为第二阶段验收准备的示例素材：

```
workspace/
├── README.md               # 本文件
├── notes/
│   ├── agent_design.md     # 含 "Agent" 关键字，用于搜索命中
│   └── todo.md             # 含 "TODO" 关键字
├── src/
│   ├── agent_runner.py     # 含 "Agent" 关键字（Python 文件，任务一的目标）
│   ├── news_fetcher.py     # 含 "TODO" 关键字
│   └── utils.py            # 普通工具函数，不含关键字
└── data/
    └── sample_news.json    # 供 json 解析类命令使用
```

"""应用配置模块。

职责：
1. 从环境变量 / .env 文件加载配置，做到「配置与业务代码分离」；
2. 对路径、连接串做统一规整（例如把相对 SQLite 路径解析为项目根目录下的绝对路径）；
3. 以单例形式对外暴露 settings 对象，任何模块都只从这里取配置。

注意：所有密钥类配置（LLM_API_KEY、NEWS_API_KEY、SMTP_PASSWORD 等）
必须来自环境变量，禁止在代码中出现明文。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：.../NewsAgent
# __file__ = .../NewsAgent/app/core/config.py -> parent.parent.parent = .../NewsAgent
BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
DATA_DIR: Path = BASE_DIR / "data"
STATIC_DIR: Path = BASE_DIR / "app" / "static"


class Settings(BaseSettings):
    """全局配置对象，字段名与环境变量名一一对应。"""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # 忽略 .env 中未声明的变量，方便后续阶段扩展
    )

    # ---------- 应用基础 ----------
    APP_NAME: str = "每日 AI 新闻助手"
    APP_VERSION: str = "0.1.0"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = True

    # ---------- 数据库 ----------
    DATABASE_URL: str = "sqlite:///./data/news_agent.db"
    SQL_ECHO: bool = False

    # ---------- 服务监听 ----------
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # ---------- 跨域 ----------
    CORS_ORIGINS: str = "*"

    # ---------- 大模型（第二阶段：Agent 核心） ----------
    LLM_API_KEY: str | None = None
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = "gpt-4o-mini"
    NEWS_API_KEY: str | None = None

    # ---------- 新闻采集（第三阶段） ----------
    # 启用的 RSS 源 ID 列表（逗号分隔），留空表示使用 app/news/feeds.py 里的默认集合
    NEWS_FEEDS: str = ""
    # 单次请求单个 RSS 源最多读取多少条（防止 800KB 的大源撑爆上下文）
    NEWS_MAX_ITEMS_PER_FEED: int = 30
    # RSS / 文章页 HTTP 超时（秒）
    NEWS_FETCH_TIMEOUT: float = 12.0
    # 单次抓取的最大响应体（字节），超出部分丢弃
    NEWS_HTTP_MAX_BYTES: int = 3_000_000
    # search_news 默认返回条数上限
    NEWS_SEARCH_MAX_ITEMS: int = 12
    # fetch_article 返回正文的最大字符数
    NEWS_ARTICLE_MAX_CHARS: int = 6000
    # 两段标题相似度超过该阈值即判定为同一条新闻
    NEWS_TITLE_SIMILARITY: float = 0.9
    # 简报在 workspace 内的输出子目录
    BRIEFING_OUTPUT_DIR: str = "briefings"
    # 生成简报的专用护栏：搜索 + 读原文 + 写作 + 入库，轮数比通用任务更多，
    # 但仍受 Agent 主循环的硬上限（20 轮 / 50 次调用）约束。
    BRIEFING_MAX_STEPS: int = 16
    BRIEFING_MAX_TOOL_CALLS: int = 40
    BRIEFING_RUN_TIMEOUT: float = 600.0

    # ---------- Agent 行为参数 ----------
    # 模式：auto（有 key 用真实 LLM，否则降级 mock）/ openai / mock
    AGENT_MODE: str = "auto"
    # 工作区：Agent 只能在此目录内读写与执行命令；留空则使用 <项目根>/workspace
    AGENT_WORKSPACE: str = ""
    # 单次任务最大「LLM 轮次」，硬上限 20，防止死循环
    AGENT_MAX_STEPS: int = 8
    # 单次任务最大工具调用次数，硬上限 50
    AGENT_MAX_TOOL_CALLS: int = 20
    # 单次任务整体超时（秒）
    AGENT_RUN_TIMEOUT: float = 120.0
    # 单次 LLM 请求超时（秒）
    AGENT_LLM_TIMEOUT: float = 60.0
    # LLM 失败重试次数（429 / 5xx / 网络错误）
    AGENT_LLM_RETRIES: int = 2
    # 采样温度
    AGENT_TEMPERATURE: float = 0.2
    # bash 工具默认 / 最大超时（秒）
    AGENT_BASH_TIMEOUT: int = 10
    AGENT_BASH_TIMEOUT_MAX: int = 60
    # bash 输出截断长度（字符）
    AGENT_BASH_OUTPUT_LIMIT: int = 8000
    # read_file 单次读取上限（字节）
    AGENT_READ_MAX_BYTES: int = 200_000
    # list_dir 单次返回条目上限
    AGENT_LIST_MAX_ENTRIES: int = 200
    # search_content 单次命中上限
    AGENT_SEARCH_MAX_MATCHES: int = 50
    # write_file 单次写入上限（字节）
    AGENT_WRITE_MAX_BYTES: int = 200_000
    # 反馈给 LLM 的单条工具结果最大长度（字符）
    AGENT_TOOL_RESULT_LIMIT: int = 12_000
    # 内存中保留的运行记录条数
    AGENT_KEEP_RUNS: int = 50
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 465
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM_NAME: str = "AI 新闻助手"

    # ---------------- 派生属性 ----------------

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 CORS 配置解析为列表。"""
        if self.CORS_ORIGINS.strip() in ("*", ""):
            return ["*"]
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def agent_workspace_path(self) -> Path:
        """Agent 沙箱工作区绝对路径。

        留空时使用 <项目根>/workspace；填相对路径时同样以项目根为基准，
        避免因启动目录不同导致 Agent 跑到别处读写文件。
        """
        raw = (self.AGENT_WORKSPACE or "").strip()
        if not raw:
            return (BASE_DIR / "workspace").resolve()
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate.resolve()
        return (BASE_DIR / candidate).resolve()

    # ---------------- 校验与规整 ----------------

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        """把 SQLite 相对路径统一转换为绝对路径。

        原因：uvicorn 的启动工作目录可能不同，相对路径会导致数据库文件
        被创建在意外位置（例如在 app/ 目录下再生成一个 data/）。
        这里统一以「项目根目录」为基准，避免踩坑。
        """
        value = value.strip()
        if not value.startswith("sqlite"):
            return value

        prefix = "sqlite:///"
        if not value.startswith(prefix):
            # 兼容 sqlite:/// 以外的写法（如 sqlite+pysqlite:///）
            return value

        raw_path = value[len(prefix) :]
        if raw_path in ("", ":memory:"):
            return value

        db_path = Path(raw_path)
        if db_path.is_absolute():
            return value

        absolute_path = (BASE_DIR / db_path).resolve()
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        return f"{prefix}{absolute_path.as_posix()}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例（进程内只解析一次）。"""
    return Settings()


settings: Settings = get_settings()

"""LLM 客户端工厂。

模式解析规则（`AGENT_MODE`）：
- `auto`：有 LLM_API_KEY 就走真实模型，没有就降级到 mock，并打印明确告警；
- `openai`：强制真实模型，缺 key 直接报错（避免「以为在跑真模型，其实在跑假模型」）；
- `mock`：强制 Mock，用于离线验证工具链路。
"""

from __future__ import annotations

import logging

from app.agent.llm.base import LLMClient, LLMError, LLMResponse, ToolCall
from app.agent.llm.mock_client import MockLLMClient
from app.agent.llm.openai_client import OpenAICompatibleClient
from app.core.config import settings

logger = logging.getLogger(__name__)

_VALID_MODES = frozenset({"auto", "openai", "mock"})


def resolve_mode(mode: str | None = None) -> str:
    """把配置/入参归一化为 `openai` 或 `mock`。

    Args:
        mode: 显式指定的模式，优先级高于配置。

    Returns:
        "openai" 或 "mock"。

    Raises:
        LLMError: 模式取值非法，或强制 openai 但缺少 API Key。
    """
    requested = (mode or settings.AGENT_MODE or "auto").strip().lower()
    if requested not in _VALID_MODES:
        raise LLMError(f"未知的 Agent 模式：{requested}，可选 auto / openai / mock", code="invalid_mode")

    has_key = bool((settings.LLM_API_KEY or "").strip())
    if requested == "auto":
        return "openai" if has_key else "mock"
    if requested == "openai" and not has_key:
        raise LLMError(
            "未配置 LLM_API_KEY，无法使用真实模型。请在 .env 中配置，或改用 AGENT_MODE=mock 验证工具链路。",
            code="llm_not_configured",
        )
    return requested


def build_llm_client(mode: str | None = None) -> LLMClient:
    """按模式创建客户端。

    Args:
        mode: auto / openai / mock；缺省读配置。

    Returns:
        满足 LLMClient 协议的实例。

    Raises:
        LLMError: 模式非法或缺少必要配置。
    """
    resolved = resolve_mode(mode)
    if resolved == "mock":
        logger.warning("Agent 运行在 Mock 模式：仅验证工具注册与调用链路，不代表真实模型能力")
        return MockLLMClient(model=settings.LLM_MODEL if settings.LLM_MODEL else "mock-agent")

    logger.info("Agent 使用真实模型：%s @ %s", settings.LLM_MODEL, settings.LLM_BASE_URL)
    return OpenAICompatibleClient(
        api_key=(settings.LLM_API_KEY or "").strip(),
        base_url=settings.LLM_BASE_URL,
        model=settings.LLM_MODEL,
        timeout=settings.AGENT_LLM_TIMEOUT,
        max_retries=settings.AGENT_LLM_RETRIES,
        temperature=settings.AGENT_TEMPERATURE,
    )


__all__ = [
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "MockLLMClient",
    "OpenAICompatibleClient",
    "ToolCall",
    "build_llm_client",
    "resolve_mode",
]

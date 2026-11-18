"""OpenAI 兼容的 Chat Completions 客户端。

刻意**不**引入 openai SDK，而是用 httpx 直接 POST /chat/completions，原因有三：
1. 依赖更少，装得上就能跑；
2. 任何「OpenAI 兼容」的服务（DeepSeek、通义、Kimi、vLLM、Ollama 的 /v1）都能直接用；
3. 能看到请求体的真实形状——这是理解 Function Calling 协议最快的方式。

密钥只从配置读取，绝不落代码；报错信息里也不会打印 Authorization 头。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from app.agent.llm.base import LLMError, LLMResponse, ToolCall

logger = logging.getLogger(__name__)

# 这些状态码通常是瞬时故障，值得重试
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class OpenAICompatibleClient:
    """适配任意 OpenAI 兼容网关的客户端。"""

    mode = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 2,
        temperature: float = 0.2,
    ) -> None:
        self.api_key = api_key
        self.base_url = self._normalize_base_url(base_url)
        self.model = model
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.temperature = temperature

    # ------------------------------------------------------------ 内部工具

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        """兼容三种写法：`https://x/v1`、`https://x/v1/`、`https://x/v1/chat/completions`。"""
        url = (base_url or "").strip().rstrip("/")
        if not url:
            raise LLMError("未配置 LLM_BASE_URL", code="llm_not_configured")
        if url.endswith("/chat/completions"):
            return url
        return f"{url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        # 只在真的需要工具时才带 tools 字段：部分服务商对空数组会报错
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        return payload

    # ------------------------------------------------------------ 主流程

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
    ) -> LLMResponse:
        """调用模型，返回归一化结果。

        Args:
            messages: OpenAI 格式消息列表。
            tools: 工具 Schema 列表。
            tool_choice: 工具选择策略。

        Returns:
            LLMResponse。

        Raises:
            LLMError: 网络错误、鉴权失败、上游返回非 2xx 或响应结构不符合预期。
        """
        payload = self._build_payload(messages, tools, tool_choice)
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(self.base_url, headers=self._headers(), json=payload)
            except httpx.TimeoutException as exc:
                last_error = LLMError(f"LLM 请求超时（{self.timeout}s）", code="llm_timeout")
                logger.warning("LLM 超时（第 %s 次）：%s", attempt + 1, exc)
            except httpx.RequestError as exc:
                last_error = LLMError(f"LLM 网络错误：{exc}", code="llm_network_error")
                logger.warning("LLM 网络错误（第 %s 次）：%s", attempt + 1, exc)
            else:
                if response.status_code in _RETRYABLE_STATUS and attempt < self.max_retries:
                    last_error = LLMError(
                        f"LLM 上游返回 {response.status_code}：{self._error_text(response)}",
                        code="llm_upstream_error",
                        status_code=response.status_code,
                    )
                    logger.warning("LLM 上游 %s，准备重试（第 %s 次）", response.status_code, attempt + 1)
                elif response.status_code >= 400:
                    raise LLMError(
                        f"LLM 调用失败（HTTP {response.status_code}）：{self._error_text(response)}",
                        code="llm_auth_error" if response.status_code in (401, 403) else "llm_upstream_error",
                        status_code=response.status_code,
                    )
                else:
                    return self._parse_response(response)

            # 指数退避：0.5s / 1s / 2s ...，避免打爆上游
            if attempt < self.max_retries:
                await asyncio.sleep(0.5 * (2**attempt))

        raise last_error or LLMError("LLM 调用失败", code="llm_error")

    # ------------------------------------------------------------ 响应解析

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        """从错误响应里尽力捞出可读信息（有的网关返回 HTML）。"""
        try:
            body = response.json()
        except ValueError:
            return response.text[:300]
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error)[:300]
            if error:
                return str(error)[:300]
        return str(body)[:300]

    def _parse_response(self, response: httpx.Response) -> LLMResponse:
        """把上游 JSON 解析成 LLMResponse。

        Raises:
            LLMError: 响应缺少 choices 或结构异常。
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError(f"LLM 响应不是合法 JSON：{response.text[:200]}", code="llm_bad_response") from exc

        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"LLM 响应缺少 choices：{str(body)[:300]}", code="llm_bad_response")

        message = choices[0].get("message") or {}
        raw_calls = message.get("tool_calls") or []

        calls: list[ToolCall] = []
        for index, item in enumerate(raw_calls):
            function = item.get("function") or {}
            name = function.get("name")
            if not name:
                continue  # 没有函数名的调用无法路由，直接丢弃（主循环会看到「本轮无有效调用」）
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                # 少数服务商直接把 arguments 返回成对象，这里统一转成字符串，
                # 让下游「解析 JSON」的路径只有一条。
                arguments = json.dumps(arguments or {}, ensure_ascii=False)
            calls.append(
                ToolCall(
                    id=item.get("id") or f"call_{index}",
                    name=name,
                    arguments=arguments,
                )
            )

        return LLMResponse(
            content=message.get("content"),
            tool_calls=calls,
            finish_reason=choices[0].get("finish_reason"),
            usage=body.get("usage"),
            model=body.get("model") or self.model,
            raw=body,
        )

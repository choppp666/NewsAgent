"""新闻抓取用的 HTTP 客户端。

为什么自己包一层 httpx 而不是各工具里直接调：
1. **统一 UA**：大量 RSS 源（量子位、IT之家等）默认拒绝 python-httpx 的 UA，
   必须伪装成浏览器，否则拿到 403 甚至一段「反爬」HTML；
2. **统一限额**：响应体先按字节截断再解码，避免某些源一次返回 800KB 把上下文撑爆；
3. **统一错误语义**：超时 / DNS 失败 / 证书错误 / 非 2xx 都收敛成 `FetchOutcome.error`，
   上层工具只需要判断 `ok`，不会因为一个源挂掉而整个任务异常；
4. **可测试**：测试里 monkeypatch 掉 `fetch_text` 就能完全离线，不需要起本地 HTTP 服务。

安全边界：`fetch_article` 的 URL 来自大模型，属于「不可信输入」，
因此这里做了 scheme 白名单 + 内网地址拦截（基础 SSRF 防护）。
要真正做到生产级隔离仍需出口代理/网络策略，本模块只降低风险。
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# 伪装成常见浏览器：不少资讯站点对默认 UA 直接 403
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
RSS_ACCEPT = "application/rss+xml,application/atom+xml,application/xml;q=0.9,text/xml;q=0.9,*/*;q=0.8"

# 解码候选：先按响应头 charset，失败再依次尝试
_FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "gb18030", "big5", "latin-1")


class FetchError(Exception):
    """抓取失败（URL 非法 / 网络错误 / 非 2xx）。"""

    def __init__(self, message: str, *, code: str = "fetch_failed") -> None:
        self.code = code
        super().__init__(message)


@dataclass(slots=True)
class FetchOutcome:
    """一次抓取的结果。

    Attributes:
        ok: 是否拿到可用内容（HTTP 2xx 且有正文）。
        url: 请求的原始 URL。
        final_url: 跟随重定向后的最终 URL（展示给模型时以它为准）。
        status_code: HTTP 状态码，网络层失败时为 0。
        text: 解码后的文本（已按 max_bytes 截断）。
        content_type: 响应头里的 Content-Type。
        size: 实际读取的字节数。
        truncated: 是否因为超过 max_bytes 被截断。
        error: 失败原因；成功时为 None。
    """

    ok: bool
    url: str
    final_url: str
    status_code: int
    text: str
    content_type: str
    size: int
    truncated: bool = False
    error: str | None = None


def _is_public_host(host: str) -> bool:
    """判断主机名是否允许访问（基础 SSRF 防护）。

    这里刻意**不对域名做解析后判定**，原因来自实际踩坑：
    本机常开 TUN 模式的代理/分流软件（Clash 等）会把所有域名解析成
    198.18.0.0/15 这类假 IP，一旦「解析结果落在私有网段就拒绝」，
    所有正常外网请求都会被误杀。域名解析层面的防护也挡不住 DNS rebinding，
    真正需要的是网络出口策略，不是应用层猜。

    因此只做两件确定有效的事：
    1. 直接以 IP 形式出现的目标（`http://127.0.0.1/admin`）：私有/回环/链路本地一律拒绝；
    2. 明显指向本机的名字（localhost / *.local / *.internal）：拒绝。
    """
    lowered = host.strip().lower().rstrip(".")
    if not lowered:
        return False
    if lowered in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return False
    if lowered.endswith(".local") or lowered.endswith(".internal") or lowered.endswith(".localhost"):
        return False

    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        # 不是 IP 字面量 → 视为普通域名放行
        return True

    return not (
        address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
    )


def ensure_url_allowed(url: str) -> str:
    """校验 URL 是否允许抓取，返回规范化后的 URL。

    Raises:
        FetchError: scheme 非 http/https，或指向内网/回环地址。
    """
    text = (url or "").strip()
    if not text:
        raise FetchError("URL 不能为空", code="invalid_arguments")

    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"只支持 http/https 链接，收到的协议是 {parsed.scheme or '空'}", code="invalid_arguments")
    if not parsed.hostname:
        raise FetchError(f"URL 缺少主机名：{text}", code="invalid_arguments")
    if not _is_public_host(parsed.hostname):
        raise FetchError(f"出于安全考虑，不允许抓取内网或本机地址：{parsed.hostname}", code="safety_violation")
    return text


def _decode(content: bytes, content_type: str) -> str:
    """按响应头 charset 解码，失败则依次尝试常见编码。"""
    charset = ""
    for part in (content_type or "").split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip('"\'')
            break

    encodings = ([charset] if charset else []) + list(_FALLBACK_ENCODINGS)
    for encoding in encodings:
        try:
            return content.decode(encoding, errors="replace")
        except LookupError:
            continue
    return content.decode("utf-8", errors="replace")


def fetch_text(
    url: str,
    *,
    timeout: float = 12.0,
    max_bytes: int = 3_000_000,
    accept: str = DEFAULT_ACCEPT,
) -> FetchOutcome:
    """抓取网页/Feed 并按文本返回（同步调用，需在线程池中执行）。

    Args:
        url: 目标地址。
        timeout: 超时秒数。
        max_bytes: 最大读取字节数，超出截断。
        accept: Accept 头。

    Returns:
        FetchOutcome。**本函数不抛异常**：所有失败都通过 ok/error 表达，
        这样批量抓取多个 RSS 源时单个失败不会中断整体流程。
    """
    try:
        safe_url = ensure_url_allowed(url)
    except FetchError as exc:
        return FetchOutcome(
            ok=False, url=url, final_url=url, status_code=0, text="", content_type="", size=0, error=str(exc)
        )

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers=headers,
            # 部分资讯站的证书链在 Windows 上缺失中间证书，这里不放开校验，
            # 失败时如实报错，避免「看起来抓到了」的假象。
        ) as client:
            with client.stream("GET", safe_url) as response:
                status_code = response.status_code
                content_type = response.headers.get("content-type", "")
                final_url = str(response.url)

                if status_code >= 400:
                    return FetchOutcome(
                        ok=False,
                        url=url,
                        final_url=final_url,
                        status_code=status_code,
                        text="",
                        content_type=content_type,
                        size=0,
                        error=f"HTTP {status_code}",
                    )

                chunks: list[bytes] = []
                size = 0
                truncated = False
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= max_bytes:
                        truncated = True
                        break
                content = b"".join(chunks)[:max_bytes]

    except httpx.TimeoutException:
        return FetchOutcome(
            ok=False, url=url, final_url=url, status_code=0, text="", content_type="", size=0,
            error=f"请求超时（>{timeout}s）",
        )
    except httpx.HTTPError as exc:
        return FetchOutcome(
            ok=False, url=url, final_url=url, status_code=0, text="", content_type="", size=0,
            error=f"网络错误：{type(exc).__name__}: {exc}",
        )

    text = _decode(content, content_type)
    return FetchOutcome(
        ok=bool(text.strip()),
        url=url,
        final_url=final_url,
        status_code=status_code,
        text=text,
        content_type=content_type,
        size=len(content),
        truncated=truncated,
        error=None if text.strip() else "响应体为空",
    )

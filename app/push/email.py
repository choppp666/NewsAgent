"""SMTP 邮件发送。

设计原则：
1. **不在这里读 .env**：`SmtpSettings.from_app_settings()` 是唯一入口，
   其它模块只拿到一个配置对象，测试可以塞一个指向本地假 SMTP 的配置进来。
2. **只返回结构化结果，不抛异常**：调用方（推送服务）需要把「成功/失败原因」
   落库，抛异常会让每条失败路径都要写一遍 try/except。
3. **错误翻译成人话**：`SMTPAuthenticationError` 对使用者毫无意义，
   「认证失败，请检查 SMTP_USER / SMTP_PASSWORD」才是能照着修的提示。
4. **密码只用于登录，绝不进日志**：`_mask()` 会在日志里把账号打码。

加密方式按端口自动判断（可用 SMTP_USE_SSL 显式覆盖）：
- 465 → SSL 直连（QQ / 163 / Gmail 的常见配置）
- 587 → 明文连上后 STARTTLS 升级
- 其它端口 → 明文（仅用于本地假 SMTP 或内网中继，如 MailHog、Postfix）
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.core.config import settings

logger = logging.getLogger(__name__)

__all__ = ["EmailNotConfigured", "EmailSender", "SendOutcome", "SmtpSettings"]


class EmailNotConfigured(RuntimeError):
    """SMTP 必要配置缺失（例如没有 SMTP_HOST）。"""


@dataclass(slots=True)
class SmtpSettings:
    """一次发信所需的全部参数（不含任何业务语义）。"""

    host: str
    port: int = 465
    user: str | None = None
    password: str | None = None
    sender: str = ""
    from_name: str = "AI 新闻助手"
    use_ssl: bool = True
    use_starttls: bool = False
    timeout: float = 15.0

    @classmethod
    def from_app_settings(cls) -> SmtpSettings:
        """从全局配置构造。

        Raises:
            EmailNotConfigured: 未配置 SMTP_HOST，或缺少发件人地址。
        """
        host = (settings.SMTP_HOST or "").strip()
        if not host:
            raise EmailNotConfigured("未配置 SMTP_HOST，无法发送邮件")

        sender = settings.smtp_sender_address
        if not sender:
            raise EmailNotConfigured("未配置发件人地址（SMTP_SENDER 或 SMTP_USER）")

        return cls(
            host=host,
            port=int(settings.SMTP_PORT),
            user=(settings.SMTP_USER or "").strip() or None,
            password=settings.SMTP_PASSWORD or None,
            sender=sender,
            from_name=settings.SMTP_FROM_NAME or "AI 新闻助手",
            use_ssl=settings.smtp_effective_ssl,
            use_starttls=settings.smtp_needs_starttls,
            timeout=float(settings.SMTP_TIMEOUT),
        )

    @property
    def transport(self) -> str:
        """人类可读的传输方式，写进日志便于排查。"""
        if self.use_ssl:
            return "SSL"
        if self.use_starttls:
            return "STARTTLS"
        return "PLAIN"


@dataclass(slots=True)
class SendOutcome:
    """发送结果。"""

    ok: bool
    message_id: str | None = None
    error: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "message_id": self.message_id,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


def _mask(address: str | None) -> str:
    """把账号打码成 `ab***@example.com`，避免日志泄露完整邮箱。"""
    if not address or "@" not in address:
        return "***"
    name, domain = address.split("@", 1)
    visible = name[:2] if len(name) > 2 else name[:1]
    return f"{visible}***@{domain}"


class EmailSender:
    """基于 smtplib 的同步发送器（每次发送新建连接）。"""

    def __init__(self, config: SmtpSettings | None = None) -> None:
        self.config = config or SmtpSettings.from_app_settings()

    # ------------------------------------------------------------ 对外接口

    def send(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        reply_to: str | None = None,
    ) -> SendOutcome:
        """发送一封邮件。

        Args:
            to: 收件地址。
            subject: 主题（CJK 会自动按 RFC 2047 编码）。
            text: 纯文本正文（必须有，作为不支持 HTML 时的兜底）。
            html: HTML 正文（可选）。
            reply_to: 回复地址（可选）。

        Returns:
            SendOutcome —— 成功含 message_id，失败含可读错误原因。
            任何异常都被收敛为 `ok=False`，不向调用方抛出。
        """
        started = time.monotonic()
        if not (to or "").strip():
            return SendOutcome(ok=False, error="收件地址为空")

        try:
            message = self._build_message(to=to, subject=subject, text=text, html=html, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001 - 构造阶段很少失败，兜底即可
            return SendOutcome(ok=False, error=f"构造邮件失败：{exc}")

        cfg = self.config
        try:
            with self._connect() as client:
                if cfg.user and cfg.password:
                    client.login(cfg.user, cfg.password)
                client.send_message(message)
            duration = int((time.monotonic() - started) * 1000)
            logger.info(
                "邮件已发送 → %s（%s:%s %s，%d ms）",
                _mask(to),
                cfg.host,
                cfg.port,
                cfg.transport,
                duration,
            )
            return SendOutcome(ok=True, message_id=message.get("Message-ID"), duration_ms=duration)
        except smtplib.SMTPAuthenticationError as exc:
            return SendOutcome(
                ok=False,
                error=(
                    f"SMTP 认证失败（{exc.smtp_code}）：请检查 SMTP_USER / SMTP_PASSWORD。"
                    "QQ 邮箱等需要使用「授权码」而不是登录密码"
                ),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except smtplib.SMTPRecipientsRefused as exc:
            return SendOutcome(
                ok=False,
                error=f"收件地址被服务器拒绝：{exc.recipients}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except smtplib.SMTPSenderRefused as exc:
            return SendOutcome(
                ok=False,
                error=f"发件地址被服务器拒绝（{exc.smtp_code}）：{exc.smtp_error!r}，请确认 SMTP_SENDER 与账号一致",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except smtplib.SMTPException as exc:
            return SendOutcome(
                ok=False,
                error=f"SMTP 协议错误：{exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except (socket.timeout, TimeoutError):
            return SendOutcome(
                ok=False,
                error=f"连接 {cfg.host}:{cfg.port} 超时（{cfg.timeout}s），请检查网络或端口是否被封",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except (OSError, ssl.SSLError) as exc:
            return SendOutcome(
                ok=False,
                error=f"无法连接 SMTP 服务器 {cfg.host}:{cfg.port}（{cfg.transport}）：{exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - 未知异常也要落到记录里
            logger.exception("发送邮件时出现未预期异常")
            return SendOutcome(
                ok=False,
                error=f"发送失败：{exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

    # ------------------------------------------------------------ 内部实现

    def _build_message(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        html: str | None,
        reply_to: str | None,
    ) -> EmailMessage:
        cfg = self.config
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((cfg.from_name, cfg.sender))
        message["To"] = to
        message["Date"] = formatdate(localtime=True)
        domain = cfg.sender.split("@", 1)[-1] if "@" in cfg.sender else None
        message["Message-ID"] = make_msgid(domain=domain)
        message["Auto-Submitted"] = "auto-generated"
        if reply_to:
            message["Reply-To"] = reply_to

        message.set_content(text)
        if html:
            message.add_alternative(html, subtype="html")
        return message

    def _connect(self) -> smtplib.SMTP:
        """建立连接（返回未登录的客户端，由调用方负责 login 与关闭）。"""
        cfg = self.config
        if cfg.use_ssl:
            context = ssl.create_default_context()
            return smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout, context=context)

        client = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
        if cfg.use_starttls:
            client.ehlo()
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        return client

"""本地假 SMTP 服务器（仅用于开发与自动化验证）。

现实问题：验证「邮件推送」这条路，手上不一定有可用的 SMTP 账号；
即使有，也不该在自动化测试里往真实邮箱发信（不可重复、可能进垃圾箱、
还有泄露测试数据的风险）。

解决办法：起一个只实现 SMTP 最小交互的本地服务器，把 smtplib 发来的
整封信原样收下来。它证明的是**真实的 SMTP 客户端代码**（EHLO / MAIL FROM /
RCPT TO / DATA / Message-ID / MIME 编码）没有问题——只有最后一跳换成了本地进程。

用法：

```python
with LocalSmtpSink() as sink:
    # 把 SMTP_HOST/PORT 指向 sink.host / sink.port，SMTP_USER/PASSWORD 留空
    ...  # 触发推送
    print(sink.messages[0])       # 原始邮件文本
    print(sink.recipients)        # 收件人列表
```

注意：**它不是邮件服务器**，不做认证（AUTH 一律返回成功）、不投递、不持久化，
只把收到的内容留在内存里。生产环境不要启动它。
"""

from __future__ import annotations

import logging
import socketserver
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

__all__ = ["LocalSmtpSink", "CapturedMail"]


@dataclass(slots=True)
class CapturedMail:
    """一封被收下的邮件。"""

    raw: str
    sender: str = ""
    recipients: list[str] = field(default_factory=list)


class _SmtpHandler(socketserver.StreamRequestHandler):
    """最小 SMTP 对话实现。"""

    server: _SmtpServer  # 类型标注，便于 IDE 提示

    def handle(self) -> None:  # noqa: C901 - 协议状态机，拆开反而更难读
        self.wfile.write(b"220 local-smtp-sink ESMTP ready\r\n")
        in_data = False
        buffer: list[bytes] = []
        sender = ""
        recipients: list[str] = []

        while True:
            line = self.rfile.readline()
            if not line:
                break

            if in_data:
                if line.strip() == b".":
                    in_data = False
                    raw = b"".join(buffer).decode("utf-8", "replace")
                    buffer = []
                    self.server.mails.append(
                        CapturedMail(raw=raw, sender=sender, recipients=list(recipients))
                    )
                    self.wfile.write(b"250 2.0.0 OK queued\r\n")
                else:
                    buffer.append(line)
                continue

            command = line.decode("utf-8", "replace").strip()
            verb = command.split(" ", 1)[0].upper()

            if verb in ("EHLO", "HELO"):
                self.wfile.write(
                    b"250-local-smtp-sink\r\n"
                    b"250-8BITMIME\r\n"
                    b"250-SMTPUTF8\r\n"
                    b"250 OK\r\n"
                )
            elif verb == "AUTH":
                # 本地 sink 不校验凭据，直接放行（真实服务器会校验）
                self.wfile.write(b"235 2.7.0 Authentication successful\r\n")
            elif verb == "MAIL":
                sender = command.split(":", 1)[-1].strip().strip("<>")
                self.wfile.write(b"250 2.1.0 OK\r\n")
            elif verb == "RCPT":
                address = command.split(":", 1)[-1].strip().strip("<>")
                recipients.append(address)
                self.wfile.write(b"250 2.1.5 OK\r\n")
            elif verb == "DATA":
                in_data = True
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
            elif verb == "RSET":
                sender, recipients = "", []
                self.wfile.write(b"250 2.0.0 OK\r\n")
            elif verb == "QUIT":
                self.wfile.write(b"221 2.0.0 Bye\r\n")
                break
            else:
                self.wfile.write(b"250 OK\r\n")


class _SmtpServer(socketserver.ThreadingTCPServer):
    """线程化的 TCP 服务器。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _SmtpHandler)
        self.mails: list[CapturedMail] = []


class LocalSmtpSink:
    """上下文管理器形式的本地 SMTP sink。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port
        self._server: _SmtpServer | None = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------- 生命周期

    def start(self) -> LocalSmtpSink:
        """启动监听（port=0 时由系统分配空闲端口）。"""
        self._server = _SmtpServer((self.host, self.port))
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="smtp-sink", daemon=True)
        self._thread.start()
        logger.info("本地 SMTP sink 已启动：%s:%s（仅用于验证，不投递真实邮件）", self.host, self.port)
        return self

    def stop(self) -> None:
        """停止监听并释放端口。"""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def __enter__(self) -> LocalSmtpSink:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ---------------------------------------------------------- 访问收件

    @property
    def mails(self) -> list[CapturedMail]:
        return list(self._server.mails) if self._server is not None else []

    @property
    def messages(self) -> list[str]:
        """收到的原始邮件文本。"""
        return [mail.raw for mail in self.mails]

    @property
    def recipients(self) -> list[str]:
        return [address for mail in self.mails for address in mail.recipients]

    def clear(self) -> None:
        if self._server is not None:
            self._server.mails.clear()


if __name__ == "__main__":  # pragma: no cover - 手动调试入口
    import argparse
    import time

    parser = argparse.ArgumentParser(description="本地假 SMTP 服务器（仅用于验证邮件推送）")
    parser.add_argument("--port", type=int, default=2525, help="监听端口，默认 2525")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    args = parser.parse_args()

    with LocalSmtpSink(host=args.host, port=args.port) as sink:
        print(f"SMTP sink 监听 {args.host}:{sink.port}，Ctrl+C 退出")
        print("把服务的 SMTP_HOST/SMTP_PORT 指向该地址即可收信（SMTP_USE_SSL=false）")
        try:
            while True:
                time.sleep(1)
                if sink.mails:
                    latest = sink.mails[-1]
                    print(f"收到 {len(sink.mails)} 封邮件，最新收件人：{latest.recipients}")
        except KeyboardInterrupt:
            print("\n已退出")

"""推送能力包。

- `email`     : SMTP 发送（配置、加密方式、错误翻译）
- `template`  : 邮件内容组装（标题 / 摘要 / 入口 / 原始链接）
- `markdown`  : Markdown → HTML / 纯文本（转义优先，防注入）
- `local_sink`: 本地假 SMTP，仅用于开发与测试验证（生产不启动）
"""

from app.push.email import EmailNotConfigured, EmailSender, SendOutcome, SmtpSettings
from app.push.template import EmailContent, NewsItem, build_email_content, parse_news_items

__all__ = [
    "EmailSender",
    "EmailNotConfigured",
    "SendOutcome",
    "SmtpSettings",
    "EmailContent",
    "NewsItem",
    "build_email_content",
    "parse_news_items",
]

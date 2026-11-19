"""真实 SMTP 连通性 / 简报邮件发送测试。

用法（在已激活虚拟环境、且 `.env` 里填好 SMTP_ 五项之后）：

```bash
# 1) 只测 SMTP 连通与投递能力：发一封极简测试邮件
python scripts/send_test_mail.py --to 2424886952@qq.com

# 2) 走完整推送链路：取该用户最近一份真实简报，强制重发（会写 push_records）
python scripts/send_test_mail.py --user-id 2 --full

# 3) 只预览邮件内容，不真的发信
python scripts/send_test_mail.py --user-id 2 --dry-run
```

为什么单独做一个脚本而不是直接调接口：
- 排查「邮件发不出去」时，先要区分是 **SMTP 配置/网络问题** 还是 **业务数据问题**。
  `--to` 模式只发一封 5 行的短邮件，能最快把 SMTP 这一段单独验证掉；
- `--full` 模式才走 `push_service`，会读取真实简报、生成 HTML/纯文本双分部、写推送记录，
  用来验证「推送内容」是否符合预期。

安全说明：脚本只打印**打码后的**账号（`24***@qq.com`），不会输出密码；
密码只从环境变量 / `.env` 读取，不接受命令行参数（避免留在 shell 历史里）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.push.email import EmailNotConfigured, EmailSender, SmtpSettings  # noqa: E402


def _mask(address: str | None) -> str:
    if not address or "@" not in address:
        return "(未配置)"
    name, domain = address.split("@", 1)
    return f"{name[:2]}***@{domain}"


def _print_config() -> bool:
    """打印当前 SMTP 配置（脱敏），返回是否具备发信条件。"""
    sender = settings.smtp_sender_address
    print("当前 SMTP 配置：")
    print(f"  主机      : {settings.SMTP_HOST or '(空)'}")
    print(f"  端口      : {settings.SMTP_PORT}（{'SSL 直连' if settings.smtp_effective_ssl else ('STARTTLS' if settings.smtp_needs_starttls else '明文')}）")
    print(f"  账号      : {_mask(settings.SMTP_USER)}")
    print(f"  密码/授权码: {'已填写' if settings.SMTP_PASSWORD else '(空)'}")
    print(f"  发件人    : {_mask(sender)}")
    print(f"  超时      : {settings.SMTP_TIMEOUT}s")

    missing: list[str] = []
    if not settings.SMTP_HOST:
        missing.append("SMTP_HOST")
    if not settings.SMTP_USER:
        missing.append("SMTP_USER")
    if not settings.SMTP_PASSWORD:
        missing.append("SMTP_PASSWORD（QQ 邮箱要填「授权码」，不是登录密码）")
    if not sender:
        missing.append("SMTP_SENDER")

    if missing:
        print("\n❌ 还缺这些配置：")
        for item in missing:
            print(f"   - {item}")
        print("\n请编辑项目根目录的 .env 补齐后重试。QQ 邮箱获取授权码：")
        print("  邮箱设置 → 账号 → 开启「POP3/SMTP 服务」→ 生成授权码（16 位）")
        return False

    print("\n✅ 配置齐全，可以发信")
    return True


def _smtp_test(to: str) -> int:
    """只测 SMTP：发一封极简测试邮件。"""
    try:
        sender = EmailSender()
    except EmailNotConfigured as exc:
        print(f"\n❌ {exc}")
        return 2

    body_text = (
        "这是一封来自「每日 AI 新闻助手」的 SMTP 连通性测试邮件。\n\n"
        "如果你收到了它，说明 SMTP 配置正确，后续每日简报可以正常发送。\n"
        "这封邮件不含新闻内容，可以忽略。\n"
    )
    body_html = (
        '<div style="font-family:-apple-system,\'PingFang SC\',sans-serif;font-size:14px;'
        'line-height:1.8;color:#1f2937;">'
        "<p>这是一封来自「每日 AI 新闻助手」的 <strong>SMTP 连通性测试邮件</strong>。</p>"
        "<p>如果你收到了它，说明 SMTP 配置正确，后续每日简报可以正常发送。</p>"
        '<p style="color:#9ca3af;font-size:12px;">这封邮件不含新闻内容，可以忽略。</p>'
        "</div>"
    )

    print(f"\n开始发送测试邮件 → {to} ...")
    outcome = sender.send(
        to=to,
        subject="【测试】每日 AI 新闻助手 · SMTP 连通性验证",
        text=body_text,
        html=body_html,
    )

    if outcome.ok:
        print(f"✅ 发送成功（{outcome.duration_ms} ms）")
        print(f"   Message-ID: {outcome.message_id}")
        print("\n请检查收件箱（以及垃圾邮件箱）。若几分钟内没收到，多半被反垃圾拦了：")
        print("  1) 发件人用同一账号，且主题不含营销词；")
        print("  2) QQ 邮箱首次给陌生地址发信可能需要收件方确认。")
        return 0

    print(f"❌ 发送失败：{outcome.error}")
    print("\n排查顺序：")
    print("  1) 端口与加密方式：465 用 SSL、587 用 STARTTLS（本项目按端口自动判断）；")
    print("  2) 密码必须用邮箱「授权码」，不是登录密码；")
    print("  3) 公司/校园网可能封 465：换 587 试试；")
    print("  4) 发件人地址需与账号一致（QQ 强制要求）。")
    return 1


def _full_chain(user_id: int, *, force: bool, dry_run: bool) -> int:
    """走完整推送链路：取最近一份真实简报，发一次（或只预览内容）。"""
    from app.core.database import SessionLocal
    from app.crud import push as push_crud
    from app.crud import task as task_crud
    from app.models.briefing import NewsBriefing
    from app.models.user import User
    from app.push.template import build_email_content
    from app.services import push_service

    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None:
            print(f"❌ 用户不存在（id={user_id}）")
            return 2

        task = task_crud.latest_for_user(db, user_id)
        if task is None or not task.briefing_id:
            print(f"❌ 用户 {user.nickname}（id={user_id}）还没有生成成功的简报，先触发一次生成吧")
            return 2

        briefing = db.get(NewsBriefing, task.briefing_id)
        if briefing is None:
            print("❌ 简报记录已不存在")
            return 2

        print(f"\n用户：{user.nickname}（id={user.id}）  收件：{_mask(user.email)}")
        print(f"简报：{briefing.briefing_date} · {briefing.title}（{briefing.news_count} 条）")
        print(f"推送开关：{'开' if user.push_enabled else '关'}")

        content = build_email_content(briefing, user, base_url=settings.PUSH_BASE_URL)
        print("\n主题：", content.subject)
        print(f"解析条目：{len(content.items)} 条，原始链接：{len(content.links)} 个")
        print(f"纯文本正文：{len(content.text)} 字；HTML 正文：{len(content.html)} 字")

        if dry_run:
            print("\n---- 纯文本正文预览（前 800 字）----")
            print(content.text[:800])
            print("\n（--dry-run 模式，未真的发信）")
            return 0

        task_id = task.id
        push_id = None
        existing = push_crud.get_by_task(db, task_id)
        if existing is not None:
            push_id = existing.id

    print("\n通过推送服务发送（force=True，会写推送记录）...")
    result = push_service.push_for_task(task_id, force=force)
    print(f"结果：status={result.status}，{result.message}")
    if result.record is not None:
        print(
            f"记录：push_id={result.record.id} 尝试={result.record.attempts} "
            f"target={_mask(result.record.target)} message_id={result.record.message_id}"
        )
        if result.record.last_error:
            print(f"原因：{result.record.last_error}")
    _ = push_id
    return 0 if result.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="SMTP / 简报邮件发送测试")
    parser.add_argument("--to", help="只测 SMTP：给这个地址发一封极简测试邮件")
    parser.add_argument("--user-id", type=int, help="走完整推送链路：用该用户最近一份简报")
    parser.add_argument("--full", action="store_true", help="配合 --user-id：真的通过 push_service 发送")
    parser.add_argument("--dry-run", action="store_true", help="只预览内容，不发送")
    args = parser.parse_args()

    configured = _print_config()
    if not configured and not (args.dry_run and args.user_id):
        return 2
    if not configured:
        # 预览邮件内容不需要真的能发信：很多人是先看看「发出去长什么样」，
        # 再去申请授权码。这里不给配置缺失挡路。
        print("\n（--dry-run 只预览内容，不要求 SMTP 配置齐全）")

    if args.to:
        return _smtp_test(args.to)

    if args.user_id:
        return _full_chain(args.user_id, force=True, dry_run=args.dry_run)

    print("\n请指定测试方式：")
    print("  只测 SMTP      : python scripts/send_test_mail.py --to you@example.com")
    print("  走完整推送链路 : python scripts/send_test_mail.py --user-id 2 --full")
    return 0


if __name__ == "__main__":
    sys.exit(main())

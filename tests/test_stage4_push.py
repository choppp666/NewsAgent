"""第四阶段：邮件推送与重试测试。

推送这块最容易「看起来能用、其实没验证」——本地没有 SMTP 账号时，
一个 try/except 吞掉异常就能让日志显示「推送完成」。

这里的做法是起一个**真实的本地 SMTP 服务器**（`app.push.local_sink`），
让 smtplib 真的走一遍 EHLO / MAIL FROM / RCPT TO / DATA，
只把最后一跳換成本地进程。于是可以断言：

- 邮件主题里带简报标题与日期；
- 正文里同时有「新闻摘要」「查看简报入口」「原始新闻链接」；
- 收件人就是第一阶段填的邮箱；
- 同一任务重复推送只发一封（数据库唯一键 + 状态判断）；
- 发不出去时状态是 failed，且带下次重试时间；修好后重试能成功。
"""

from __future__ import annotations

import email
import sys
from datetime import date, timedelta
from email import policy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.crud import push as push_crud  # noqa: E402
from app.models.base import local_now  # noqa: E402
from app.models.push import PushStatus  # noqa: E402
from app.push.local_sink import LocalSmtpSink  # noqa: E402
from app.push.template import build_email_content, parse_news_items  # noqa: E402
from app.services import briefing_service, push_service, task_service  # noqa: E402

SAMPLE_MARKDOWN = """# 每日 AI 新闻简报

- 简报日期：2026-11-18
- 用户关注方向：AI Agent、MCP
- 今日新闻数量：2

## 今日重点新闻

### 1. OpenAI 开放 MCP 协议支持

- 新闻摘要：OpenAI 在 API 层支持 Model Context Protocol，第三方工具可用统一描述接入。
- 核心事件：官方文档给出 Python 与 TypeScript 示例。
- 重要性分析：工具接入方式从私有插件变成统一协议。
- 新闻来源：量子位
- 原文链接：https://example.com/mcp-openai
- 发布时间：2026-11-18 09:30

### 2. LangGraph 1.0 发布

- 新闻摘要：新版重构状态机与检查点，支持更长的 Agent 任务链。
- 核心事件：官方给出多智能体协作示例。
- 重要性分析：长链路任务可以中断后续跑。
- 新闻来源：雷锋网
- 原文链接：https://example.com/langgraph
- 发布时间：2026-11-18 10:00

## 行业动态总结

工具调用与编排正在工程化。
"""


# ---------------------------------------------------------------- 夹具


def _create_user(client: TestClient, *, email: str | None = "reader@example.com") -> int:
    response = client.post(
        "/api/v1/users",
        json={"nickname": "小林", "identity": "后端开发实习生", "email": email},
    )
    assert response.status_code == 201
    return response.json()["data"]["id"]


def _make_completed_task(db, session_factory, user_id: int, *, day: date | None = None):
    """造一条「已生成成功」的任务 + 简报，返回 (task, briefing, user)。"""
    from app.crud import task as task_crud
    from app.crud import user as user_crud

    day = day or date.today()
    trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=day)
    task_service.claim(db, trigger.task.id)

    db.expire_all()
    task = task_crud.get(db, trigger.task.id)
    briefing, _ = briefing_service.upsert_briefing(
        db,
        user_id=user_id,
        briefing_date=day,
        title="每日 AI 新闻简报（2026-11-18）",
        content=SAMPLE_MARKDOWN,
        news_count=2,
    )
    task_service.succeed(db, task, briefing=briefing, run_id="run-push")
    db.expire_all()

    task = task_crud.get(db, trigger.task.id)
    user = user_crud.get(db, user_id)
    return task, briefing, user


@pytest.fixture()
def smtp_sink(monkeypatch: pytest.MonkeyPatch):
    """启动本地 SMTP 假服务器，并把配置指向它。"""
    with LocalSmtpSink() as sink:
        monkeypatch.setattr(settings, "SMTP_HOST", "127.0.0.1")
        monkeypatch.setattr(settings, "SMTP_PORT", sink.port)
        monkeypatch.setattr(settings, "SMTP_USER", "")
        monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
        monkeypatch.setattr(settings, "SMTP_SENDER", "noreply@example.com")
        monkeypatch.setattr(settings, "SMTP_USE_SSL", False)
        monkeypatch.setattr(settings, "SMTP_TIMEOUT", 5.0)
        monkeypatch.setattr(settings, "PUSH_ENABLED", True)
        monkeypatch.setattr(settings, "PUSH_BASE_URL", "http://127.0.0.1:8000")
        yield sink


@pytest.fixture()
def no_smtp(monkeypatch: pytest.MonkeyPatch):
    """完全不配置 SMTP 的环境。"""
    monkeypatch.setattr(settings, "SMTP_HOST", None)
    monkeypatch.setattr(settings, "SMTP_USER", None)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", None)
    monkeypatch.setattr(settings, "SMTP_SENDER", None)


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------- 模板单元测试


def test_parse_news_items_extracts_all_fields() -> None:
    items = parse_news_items(SAMPLE_MARKDOWN)

    assert len(items) == 2
    assert items[0].title.startswith("OpenAI 开放 MCP")
    assert "Model Context Protocol" in items[0].summary
    assert items[0].source == "量子位"
    assert items[0].url == "https://example.com/mcp-openai"
    assert items[1].url == "https://example.com/langgraph"


def test_email_content_contains_required_parts() -> None:
    """验收要求：标题、摘要、查看入口、原始链接四项都要在邮件里。"""

    class _Briefing:
        id = 42
        title = "每日 AI 新闻简报"
        briefing_date = date(2026, 11, 18)
        news_count = 2
        content = SAMPLE_MARKDOWN

    class _User:
        nickname = "小林"

    content = build_email_content(_Briefing(), _User(), base_url="http://127.0.0.1:8000")

    assert "2026-11-18" in content.subject
    assert "每日 AI 新闻简报" in content.subject
    # 入口
    assert "http://127.0.0.1:8000/#/briefing/42" in content.html
    assert "http://127.0.0.1:8000/#/briefing/42" in content.text
    # 摘要
    assert "Model Context Protocol" in content.text
    # 原始链接
    assert "https://example.com/mcp-openai" in content.text
    assert "https://example.com/mcp-openai" in content.html
    # 纯文本兜底必须有内容（不能只发 HTML）
    assert len(content.text) > 200


# ---------------------------------------------------------------- 跳过分支


def test_push_skipped_when_smtp_not_configured(client: TestClient, db, session_factory, no_smtp) -> None:
    user_id = _create_user(client)
    task, _, _ = _make_completed_task(db, session_factory, user_id)

    result = push_service.push_for_task(task.id)

    assert result.status == PushStatus.SKIPPED.value
    assert result.record is not None
    assert "SMTP" in (result.record.last_error or "")
    # 没有尝试发送，所以 attempts 保持 0
    assert result.record.attempts == 0


def test_push_disabled_by_user(client: TestClient, db, session_factory, smtp_sink) -> None:
    user_id = _create_user(client)
    task, _, _ = _make_completed_task(db, session_factory, user_id)

    from app.services import user_service

    user_service.update_push_settings(db, user_id, False)

    result = push_service.push_for_task(task.id)

    assert result.status == PushStatus.DISABLED.value
    assert "关闭" in (result.record.last_error or "")
    assert smtp_sink.mails == []  # 确认一封都没发


def test_push_skipped_without_email(client: TestClient, db, session_factory, smtp_sink) -> None:
    user_id = _create_user(client, email=None)
    task, _, _ = _make_completed_task(db, session_factory, user_id)

    result = push_service.push_for_task(task.id)

    assert result.status == PushStatus.SKIPPED.value
    assert "邮箱" in (result.record.last_error or "")
    assert smtp_sink.mails == []


def test_push_requires_successful_task(client: TestClient, db, session_factory, smtp_sink) -> None:
    """任务没成功生成简报时，推送应直接拒绝（而不是发一封空邮件）。"""
    user_id = _create_user(client)
    trigger = task_service.get_or_create(db, user_id=user_id, briefing_date=date.today())

    result = push_service.push_for_task(trigger.task.id)

    assert result.ok is False
    assert result.status == PushStatus.SKIPPED.value
    assert smtp_sink.mails == []


# ---------------------------------------------------------------- 成功分支


def _decode_mail(raw: str) -> tuple[str, str, str]:
    """把 sink 收到的原始邮件解码成 (主题, 纯文本正文, HTML 正文)。

    为什么要解码：邮件正文按标准会用 base64 / quoted-printable 传输编码，
    直接对 raw 做字符串包含判断会失败（中文全是乱码）。这里用标准库
    `email` 解析，顺便验证「我们发出去的是一封合规的 MIME 邮件」。
    """
    message = email.message_from_string(raw, policy=policy.default)
    subject = str(message["Subject"])
    text_part = ""
    html_part = ""
    for part in message.walk():
        content_type = part.get_content_type()
        if content_type == "text/plain" and not text_part:
            text_part = part.get_content()
        elif content_type == "text/html" and not html_part:
            html_part = part.get_content()
    return subject, text_part, html_part


def test_push_success_delivers_real_email(client: TestClient, db, session_factory, smtp_sink) -> None:
    user_id = _create_user(client)
    task, briefing, _ = _make_completed_task(db, session_factory, user_id)

    result = push_service.push_for_task(task.id)

    assert result.ok is True
    assert result.status == PushStatus.SUCCESS.value
    assert result.record.attempts == 1
    assert result.record.sent_at is not None
    assert result.record.message_id

    assert len(smtp_sink.mails) == 1
    mail = smtp_sink.mails[0]
    assert mail.recipients == ["reader@example.com"]

    subject, text, html = _decode_mail(mail.raw)
    assert "每日 AI 新闻简报" in subject  # 简报标题进了主题
    assert "2026-11-18" in subject  # 日期进了主题
    assert "Model Context Protocol" in text  # 新闻摘要进了正文
    assert f"/#/briefing/{briefing.id}" in text  # 查看入口
    assert "https://example.com/mcp-openai" in text  # 原始链接
    assert "https://example.com/langgraph" in text
    # HTML 分部同样要齐（很多客户端默认渲染 HTML）
    assert "Model Context Protocol" in html
    assert f"/#/briefing/{briefing.id}" in html
    assert "<html" not in html.lower() or "<div" in html


def test_push_is_not_repeated(client: TestClient, db, session_factory, smtp_sink) -> None:
    """同一任务重复推送只发一封（数据库状态判断，不靠内存）。"""
    user_id = _create_user(client)
    task, _, _ = _make_completed_task(db, session_factory, user_id)

    first = push_service.push_for_task(task.id)
    second = push_service.push_for_task(task.id)

    assert first.ok and second.ok
    assert second.record.attempts == 1  # 没有重新发送
    assert len(smtp_sink.mails) == 1


def test_force_push_sends_again(client: TestClient, db, session_factory, smtp_sink) -> None:
    """显式 force 时才允许重发（用于「再发一次」的人工操作）。"""
    user_id = _create_user(client)
    task, _, _ = _make_completed_task(db, session_factory, user_id)

    push_service.push_for_task(task.id)
    result = push_service.push_for_task(task.id, force=True)

    assert result.ok
    assert result.record.attempts == 2
    assert len(smtp_sink.mails) == 2


def test_global_switch_disables_push(client: TestClient, db, session_factory, smtp_sink, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PUSH_ENABLED", False)
    user_id = _create_user(client)
    task, _, _ = _make_completed_task(db, session_factory, user_id)

    result = push_service.push_for_task(task.id)

    assert result.status == PushStatus.SKIPPED.value
    assert smtp_sink.mails == []


# ---------------------------------------------------------------- 失败与重试


def test_push_failure_records_error_and_retry_time(
    client: TestClient, db, session_factory, monkeypatch
) -> None:
    """SMTP 连不上：状态 failed、有可读错误、算出下次重试时间。"""
    monkeypatch.setattr(settings, "SMTP_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "SMTP_PORT", 1)  # 必然连不上的端口
    monkeypatch.setattr(settings, "SMTP_USER", "")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    monkeypatch.setattr(settings, "SMTP_SENDER", "noreply@example.com")
    monkeypatch.setattr(settings, "SMTP_USE_SSL", False)
    monkeypatch.setattr(settings, "SMTP_TIMEOUT", 2.0)
    monkeypatch.setattr(settings, "PUSH_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "PUSH_RETRY_BACKOFF_SECONDS", 30)

    user_id = _create_user(client)
    task, _, _ = _make_completed_task(db, session_factory, user_id)

    result = push_service.push_for_task(task.id)

    assert result.ok is False
    assert result.status == PushStatus.FAILED.value
    assert result.record.attempts == 1
    assert result.record.last_error
    assert result.record.next_retry_at is not None


def test_retry_after_failure_succeeds(client: TestClient, db, session_factory, monkeypatch) -> None:
    """失败记录在 SMTP 修好后重试成功，且 attempts 累加、不新增行。"""
    # 第一次：指向不存在的端口
    monkeypatch.setattr(settings, "SMTP_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "SMTP_PORT", 1)
    monkeypatch.setattr(settings, "SMTP_USER", "")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    monkeypatch.setattr(settings, "SMTP_SENDER", "noreply@example.com")
    monkeypatch.setattr(settings, "SMTP_USE_SSL", False)
    monkeypatch.setattr(settings, "SMTP_TIMEOUT", 2.0)

    user_id = _create_user(client)
    task, _, _ = _make_completed_task(db, session_factory, user_id)
    failed = push_service.push_for_task(task.id)
    assert failed.status == PushStatus.FAILED.value

    # 第二次：换成可用的本地 sink
    with LocalSmtpSink() as sink:
        monkeypatch.setattr(settings, "SMTP_PORT", sink.port)
        retried = push_service.retry_push_record(failed.record.id)

        assert retried.ok is True
        assert retried.status == PushStatus.SUCCESS.value
        assert retried.record.attempts == 2
        assert len(sink.mails) == 1

    # 整个过程只有一条推送记录
    records, total = push_crud.list_by_user(db, user_id)
    assert total == 1


def test_retry_due_pushes_scans_failed_records(
    client: TestClient, db, session_factory, monkeypatch
) -> None:
    """定时重试扫描：只捡到期的 failed 记录，成功后不再重复。"""
    monkeypatch.setattr(settings, "SMTP_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "SMTP_PORT", 1)
    monkeypatch.setattr(settings, "SMTP_USER", "")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    monkeypatch.setattr(settings, "SMTP_SENDER", "noreply@example.com")
    monkeypatch.setattr(settings, "SMTP_USE_SSL", False)
    monkeypatch.setattr(settings, "SMTP_TIMEOUT", 2.0)

    user_id = _create_user(client)
    task, _, _ = _make_completed_task(db, session_factory, user_id)
    failed = push_service.push_for_task(task.id)
    assert failed.status == PushStatus.FAILED.value

    # 还没到重试时间 → 扫描不到
    assert push_service.retry_due_pushes()["scanned"] == 0

    # 把重试时间改成过去，再换成可用的 sink
    db.expire_all()
    record = push_crud.get(db, failed.record.id)
    record.next_retry_at = local_now() - timedelta(minutes=1)
    db.commit()

    with LocalSmtpSink() as sink:
        monkeypatch.setattr(settings, "SMTP_PORT", sink.port)
        stats = push_service.retry_due_pushes()

        assert stats["scanned"] == 1
        assert stats["succeeded"] == 1
        assert len(sink.mails) == 1

    db.expire_all()
    assert push_crud.get(db, failed.record.id).status == PushStatus.SUCCESS.value


def test_retry_stops_at_max_attempts(client: TestClient, db, session_factory, monkeypatch) -> None:
    """超过最大尝试次数后不再安排重试（避免无限重试）。"""
    monkeypatch.setattr(settings, "SMTP_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "SMTP_PORT", 1)
    monkeypatch.setattr(settings, "SMTP_USER", "")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    monkeypatch.setattr(settings, "SMTP_SENDER", "noreply@example.com")
    monkeypatch.setattr(settings, "SMTP_USE_SSL", False)
    monkeypatch.setattr(settings, "SMTP_TIMEOUT", 2.0)
    monkeypatch.setattr(settings, "PUSH_MAX_ATTEMPTS", 1)

    user_id = _create_user(client)
    task, _, _ = _make_completed_task(db, session_factory, user_id)

    result = push_service.push_for_task(task.id)

    assert result.status == PushStatus.FAILED.value
    assert result.record.next_retry_at is None


def test_retry_skips_terminal_records(client: TestClient, db, session_factory, smtp_sink) -> None:
    """已成功 / 已关闭的记录不会被「重试」打扰。"""
    user_id = _create_user(client)
    task, _, _ = _make_completed_task(db, session_factory, user_id)
    done = push_service.push_for_task(task.id)

    again = push_service.retry_push_record(done.record.id)
    assert again.ok is True
    assert len(smtp_sink.mails) == 1

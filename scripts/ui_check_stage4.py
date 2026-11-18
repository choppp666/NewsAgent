"""第四阶段前端验收（Playwright + 本机 Edge）。

驱动真实浏览器走一遍用户路径：

    概览（含用户/偏好/定时任务） → 点「立即生成简报」 → 等待执行/正在生成/成功
    → 最近简报/推送状态更新 → 点进详情看 Markdown → 历史列表 → 推送开关

截图输出到 data/screenshots/stage4/。

用法（先启动服务，并准备一个已有订阅偏好的用户）：
    python scripts/ui_check_stage4.py --base http://127.0.0.1:8010 --user-id 6
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "data" / "screenshots" / "stage4"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--channel", default="msedge", help="浏览器通道，默认本机 Edge")
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")
    url = f"{base}/?user_id={args.user_id}"

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.channel, headless=args.headless)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(f"pageerror: {exc}"))

        # ---------------- 概览 ----------------
        page.goto(url, wait_until="networkidle")
        expect(page.locator("#dashNickname")).to_have_text("演示用户")
        expect(page.locator("#prefSummary")).to_contain_text("AI Agent")
        expect(page.locator("#schedStats")).to_contain_text("每天执行")
        print("[1] 概览渲染 OK：用户 / 偏好 / 定时任务状态")

        # 推送开关的视觉状态必须与后端值一致（checked 时轨道变主色）
        assert page.locator("#pushToggle").is_checked(), "push_enabled=true 时开关应为选中态"
        track_bg = page.locator(".switch-track").evaluate(
            "el => getComputedStyle(el).backgroundColor"
        )
        assert track_bg not in ("rgba(0, 0, 0, 0)", ""), track_bg

        # 从未生成过时，阶段指示与进度条应当是收起的
        if "尚未生成" in page.locator("#genState").inner_text():
            assert page.locator("#phaseSteps").is_hidden(), "未生成时不应显示阶段指示"
            assert page.locator("#genProgress").is_hidden(), "未生成时不应显示进度条"
            print("[1.1] 未生成状态下阶段指示/进度条已收起 OK")

        page.screenshot(path=str(SHOTS / "01-dashboard.png"), full_page=True)

        # ---------------- 手动生成 ----------------
        page.click("#genBtn")
        # 提交后应立刻进入「等待执行 / 正在生成」并出现阶段指示
        expect(page.locator("#phaseSteps")).to_be_visible(timeout=10_000)
        state_text = page.locator("#genState").inner_text()
        print(f"[2] 点击后状态：{state_text}")
        page.screenshot(path=str(SHOTS / "02-generating.png"), full_page=True)

        # 按钮在轮询期间禁用（防重复点击）
        assert page.locator("#genBtn").is_disabled(), "生成期间按钮必须禁用"
        print("[3] 生成期间按钮已禁用，防重复点击 OK")

        # 等待终态（真实模型 1~3 分钟，mock 约 10 秒）
        expect(page.locator("#genResult")).to_be_visible(timeout=180_000)
        result_text = page.locator("#genResult").inner_text()
        print(f"[4] 生成结果：{result_text[:80]}")
        assert "生成成功" in result_text, f"期望生成成功，实际：{result_text}"
        expect(page.locator("#genBtn")).to_be_enabled(timeout=10_000)
        page.screenshot(path=str(SHOTS / "03-generated.png"), full_page=True)

        # ---------------- 最近简报 / 推送 ----------------
        expect(page.locator("#latestBriefing")).to_contain_text("条")
        push_text = page.locator("#latestPush").inner_text()
        print(f"[5] 最近推送卡片：{push_text.replace(chr(10), ' / ')[:90]}")
        assert "已发送" in push_text or "推送失败" in push_text, push_text

        # ---------------- 简报详情 ----------------
        page.click("#latestBriefing a:has-text('查看完整简报')")
        page.wait_for_selector("#detailBody h1", timeout=15_000)
        page.wait_for_timeout(300)
        expect(page.locator("#detailBody h1")).to_contain_text("每日 AI 新闻简报")
        link_count = page.locator("#detailBody a").count()
        assert link_count > 0, "详情页应包含可点击的新闻链接"
        print(f"[6] 详情页 Markdown 渲染 OK，含 {link_count} 个可点击链接")
        page.screenshot(path=str(SHOTS / "04-detail.png"), full_page=True)

        # ---------------- 历史列表 ----------------
        page.goto(f"{base}/#/briefings", wait_until="networkidle")
        page.wait_for_selector(".table-row", timeout=10_000)
        rows = page.locator(".table-row").count()
        first_row = page.locator(".table-row").first.inner_text().replace("\n", " | ")
        print(f"[7] 历史列表 OK：{rows} 行，首行：{first_row}")
        assert "生成成功" in first_row, first_row
        page.screenshot(path=str(SHOTS / "05-history.png"), full_page=True)

        # ---------------- 推送开关 ----------------
        page.goto(f"{base}/#/", wait_until="networkidle")
        # 复选框本身是视觉隐藏的（样式用自定义轨道），点轨道等价于点开关
        page.locator(".switch-track").click()
        page.wait_for_timeout(700)
        label = page.locator("#pushToggleLabel").inner_text()
        print(f"[8] 推送开关切换后：{label}")
        assert "已关闭" in label, label
        page.locator(".switch-track").click()
        page.wait_for_timeout(700)
        print(f"[9] 再切回：{page.locator('#pushToggleLabel').inner_text()}")
        assert "接收" in page.locator("#pushToggleLabel").inner_text()

        # ---------------- 订阅配置页 ----------------
        page.goto(f"{base}/#/settings", wait_until="networkidle")
        expect(page.locator("#nickname")).to_have_value("演示用户")
        expect(page.locator("#topicsList")).to_contain_text("AI Agent")
        print("[10] 订阅配置页回填 OK")
        page.screenshot(path=str(SHOTS / "06-settings.png"), full_page=True)

        browser.close()

        if console_errors:
            print("!! 前端控制台错误：")
            for item in console_errors[:10]:
                print("   -", item)
            return 1

    print("\n全部前端检查通过，截图位于", SHOTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())

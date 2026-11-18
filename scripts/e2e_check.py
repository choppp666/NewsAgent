"""前端 E2E 冒烟测试（可选开发工具，不属于运行依赖）。

前置：pip install playwright（复用本机已安装的 Edge，无需下载浏览器内核）
运行：python scripts/e2e_check.py   （需先启动服务，默认校验 8010 端口）

用 Playwright 驱动本机 Edge，模拟用户真实操作：
1. 打开配置页，填写昵称、身份、邮箱；
2. 添加关注领域（含预设按钮）、关键词、排除关键词；
3. 点击保存，等待成功提示；
4. 刷新页面，确认数据被回填。
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8010"
SHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "screenshots"
SHOT_DIR.mkdir(parents=True, exist_ok=True)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 1000}, locale="zh-CN")
        page = context.new_page()

        errors: list[str] = []
        page.on("console", lambda msg: errors.append(f"{msg.type}: {msg.text}") if msg.type == "error" else None)
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

        page.goto(BASE_URL, wait_until="networkidle")
        page.screenshot(path=str(SHOT_DIR / "01-empty.png"), full_page=True)

        # ---- 1. 填写基本信息 ----
        page.fill("#nickname", "小林")
        page.fill(
            "#identity",
            "后端开发实习生，主攻 Java / Spring Boot，正在学习大模型应用开发，关注 Agent 与 RAG 落地实践",
        )
        page.fill("#email", "xiaolin@example.com")

        # ---- 2. 通过预设按钮 + 键盘输入添加话题 ----
        page.click(".preset >> text=大模型 / LLM")
        page.click(".preset >> text=AI Agent")
        page.click("#topicsInput")
        page.type("#topicsInput", "多模态")
        page.keyboard.press("Enter")
        page.type("#topicsInput", "具身智能，AI 芯片与算力")
        page.keyboard.press("Enter")

        # ---- 3. 关键词与排除关键词 ----
        for word in ["RAG", "微调", "推理成本"]:
            page.type("#keywordsInput", word)
            page.keyboard.press("Enter")
        for word in ["招聘", "课程广告"]:
            page.type("#excludeKeywordsInput", word)
            page.keyboard.press("Enter")

        topic_count = page.locator("#topicsList .tag").count()
        print(f"[页面] 关注领域标签数：{topic_count}")
        page.screenshot(path=str(SHOT_DIR / "02-filled.png"), full_page=True)

        # ---- 4. 保存 ----
        page.click("#saveBtn")
        page.wait_for_selector(".toast.is-success", timeout=10000)
        toast_text = page.inner_text("#toast")
        status_text = page.inner_text("#saveStatus")
        chip_text = page.inner_text("#userChipText")
        print(f"[保存] 提示：{toast_text} | 状态：{status_text} | 用户：{chip_text}")
        page.wait_for_timeout(600)
        page.screenshot(path=str(SHOT_DIR / "03-saved.png"), full_page=True)

        user_id = page.evaluate("localStorage.getItem('news_agent_user_id')")
        print(f"[保存] localStorage user_id = {user_id}")

        # ---- 5. 刷新后回填 ----
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(800)
        values = {
            "昵称": page.input_value("#nickname"),
            "身份": page.input_value("#identity")[:20] + "…",
            "邮箱": page.input_value("#email"),
            "话题数": page.locator("#topicsList .tag").count(),
            "关键词数": page.locator("#keywordsList .tag").count(),
            "排除词数": page.locator("#excludeKeywordsList .tag").count(),
            "状态": page.inner_text("#saveStatus"),
        }
        print(f"[刷新] 回填结果：{values}")
        page.screenshot(path=str(SHOT_DIR / "04-reloaded.png"), full_page=True)

        # ---- 6. 手机端视口 ----
        mobile = context.new_page()
        mobile.set_viewport_size({"width": 390, "height": 844})
        mobile.goto(BASE_URL, wait_until="networkidle")
        mobile.wait_for_timeout(600)
        mobile.screenshot(path=str(SHOT_DIR / "05-mobile.png"), full_page=True)

        if errors:
            print("[控制台错误]")
            for e in errors:
                print("  ", e)

        browser.close()

        ok = (
            topic_count == 5  # 2 个预设 + 多模态 + 逗号拆出的 2 个
            and toast_text == "订阅保存成功"
            and values["昵称"] == "小林"
            and values["话题数"] == 5
            and values["关键词数"] == 3
            and values["排除词数"] == 2
            and not errors
        )
        print("[结果]", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

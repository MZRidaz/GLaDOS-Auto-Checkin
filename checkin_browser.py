#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GLaDOS 浏览器签到（GitHub Actions 版）

背景：2026-11 起 glados.cloud 在签到提交上加了 Cloudflare 验证
（纯 HTTP 提交返回 "Cloudflare verification failed. Nothing was recorded"），
requests 脚本无法通过。本脚本改用真实 Chromium（Patchright，有头模式 +
Xvfb）在 Actions 里打开签到页、注入 COOKIES 登录态、点击签到按钮，
让页面自身的 JS 完成 challenge + Cloudflare 验证，再读取页面结果。

环境变量：
  COOKIES  与 checkin.py 相同格式（gld:sess / gld:sess.sig），
           多账号用 ||| 、& 或换行分隔。

退出码：全部账号失败返回 1，否则 0（与 checkin.py 行为一致）。
"""

import os
import re
import sys
import time
import random
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("glados-browser-checkin")

CHECKIN_URL = "https://glados.cloud/console/checkin"
DOMAIN = "glados.cloud"

# 与 MZ 本机一致的桌面 Chrome UA，降低设备/环境不匹配的概率
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

RESULT_TIMEOUT = 60  # 点击签到后等待结果的最长秒数


def normalize_cookie(cookie: str) -> str:
    """与 checkin.py 相同的归一化：去空白/引号/误带的 Cookie: 前缀。"""
    c = (cookie or "").strip()
    while len(c) >= 2 and c[0] == c[-1] and c[0] in ('"', "'"):
        c = c[1:-1].strip()
    if c[:7].lower() == "cookie:":
        c = c[7:].strip()
    return c


def split_cookies(raw: str):
    return [c for c in (normalize_cookie(x) for x in re.split(r"\|\|\||[&\n]", raw or "")) if c]


def parse_cookie_pairs(cookie: str):
    """把 'a=1; b=2' 形式的 Cookie 串拆成 (name, value) 列表。"""
    pairs = []
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        pairs.append((name.strip(), value.strip()))
    return pairs


def has_session_pair(pairs) -> bool:
    """校验存在成对的 <prefix>:sess 与 <prefix>:sess.sig（前缀无关）。"""
    names = {n for n, _ in pairs}
    for n in names:
        if n.endswith(":sess") and (n + ".sig") in names:
            return True
    return False


def mask_email(text: str) -> str:
    return re.sub(r"([\w.+-])[^\s@]*(@[\w.-]+)", r"\1***\2", text or "")


def try_click_turnstile(page) -> bool:
    """若页面出现 Cloudflare 验证 iframe，尝试点击其复选框区域。"""
    for frame in page.frames:
        if "challenges.cloudflare.com" in (frame.url or ""):
            try:
                frame.locator("body").click(position={"x": 28, "y": 28}, timeout=3000)
                logger.info("已点击 Cloudflare 验证框")
                return True
            except Exception:
                try:
                    # 退而求其次：点击 iframe 元素本身靠左位置
                    el = page.locator("iframe[src*='challenges.cloudflare.com']").first
                    el.click(position={"x": 28, "y": 28}, timeout=3000)
                    logger.info("已点击 Cloudflare 验证 iframe")
                    return True
                except Exception:
                    return False
    return False


def page_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def checkin_one(browser, idx: int, cookie: str) -> str:
    """
    返回结果分类字符串：
      success / repeat / cf_failed / login_invalid / error:<detail>
    """
    pairs = parse_cookie_pairs(cookie)
    if not has_session_pair(pairs):
        logger.warning("账号 %d Cookie 缺少成对的 sess/sess.sig，跳过", idx)
        return "error:cookie 格式异常（无成对 sess/sess.sig）"

    context = browser.new_context(
        user_agent=USER_AGENT,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1280, "height": 800},
    )
    context.add_cookies(
        [
            {
                "name": name,
                "value": value,
                "domain": "." + DOMAIN,
                "path": "/",
                "secure": True,
            }
            for name, value in pairs
        ]
    )
    page = context.new_page()
    try:
        page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        # 未登录会被重定向到首页/登录页
        if "/console" not in page.url:
            logger.warning("账号 %d 被重定向到 %s，Cookie 可能已失效", idx, page.url)
            return "login_invalid"

        text = page_text(page)
        if "log in" in text.lower() and "checkin" not in text.lower():
            return "login_invalid"

        # 定位签到按钮
        button = page.get_by_role("button", name=re.compile(r"checkin", re.I)).first
        try:
            btn_text = (button.inner_text(timeout=8000) or "").strip()
        except Exception:
            return "error:未找到签到按钮（页面结构可能已变化）"
        logger.info("账号 %d 签到按钮当前文案: %s", idx, btn_text)

        if re.search(r"got\s+\d+\s+points?", btn_text, re.I):
            logger.info("账号 %d 今日已签到: %s", idx, btn_text)
            return "repeat"

        button.click(timeout=10000)
        logger.info("账号 %d 已点击签到，等待验证与结果...", idx)

        clicked_cf = False
        deadline = time.time() + RESULT_TIMEOUT
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            try:
                btn_text = (button.inner_text(timeout=3000) or "").strip()
            except Exception:
                btn_text = ""
            if re.search(r"got\s+\d+\s+points?", btn_text, re.I):
                logger.info("账号 %d 签到成功: %s", idx, btn_text)
                return "success"
            text = page_text(page)
            if "cloudflare verification failed" in text.lower() or "nothing was recorded" in text.lower():
                logger.warning("账号 %d 仍被 Cloudflare 验证拒绝", idx)
                return "cf_failed"
            # 验证框迟迟不自动过时，尝试点一次
            if not clicked_cf and "checking in" not in btn_text.lower():
                clicked_cf = try_click_turnstile(page)
        logger.warning("账号 %d 等待结果超时，最后按钮文案: %s", idx, btn_text)
        return "error:等待签到结果超时"
    except Exception as exc:  # noqa: BLE001
        logger.exception("账号 %d 浏览器签到异常", idx)
        return f"error:{exc.__class__.__name__}: {exc}"
    finally:
        try:
            context.close()
        except Exception:
            pass


def main() -> int:
    raw = os.getenv("COOKIES", "")
    cookies = split_cookies(raw)
    if not cookies:
        logger.error("未检测到 COOKIES，请配置 GitHub Secrets")
        return 1
    logger.info("检测到 %d 个账号", len(cookies))

    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright  # 兜底

    ok = fail = repeat = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--lang=zh-CN",
            ],
        )
        for idx, cookie in enumerate(cookies, 1):
            logger.info("正在处理账号 %d/%d...", idx, len(cookies))
            result = checkin_one(browser, idx, cookie)
            if result == "success":
                ok += 1
            elif result == "repeat":
                repeat += 1
            else:
                fail += 1
                logger.error("账号 %d 结果: %s", idx, result)
            if idx < len(cookies):
                time.sleep(random.uniform(3, 6))
        browser.close()

    logger.info("汇总: 成功 %d，已签到 %d，失败 %d", ok, repeat, fail)
    if fail and not (ok or repeat):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

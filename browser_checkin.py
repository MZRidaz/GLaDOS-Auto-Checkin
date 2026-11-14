"""让官网页面完成签到验证；不复制验证算法、不复用 Turnstile 令牌。"""
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from glados_auth import is_auth_failure

ORIGIN = "https://glados.cloud"
logger = logging.getLogger("GLaDOS")
TOKEN_READY = """() => {
    try {
        if (window.turnstile && window.turnstile.getResponse()) return true;
    } catch (_) {}
    return [...document.querySelectorAll('input[name="cf-turnstile-response"]')]
        .some(node => node.value.length > 0);
}"""


class BrowserCheckinError(RuntimeError):
    """可直接显示的脱敏错误；不包含浏览器原始异常或请求内容。"""


def _cookies_for_browser(session, headers):
    host = urlsplit(ORIGIN).hostname
    raw = headers.get("cookie")
    if raw is not None:
        pairs = [part.strip().split("=", 1) for part in raw.split(";") if "=" in part]
    else:
        pairs = [(c.name, c.value) for c in session.cookies
                 if not c.domain or c.domain.lstrip(".") == host]
    return [{"name": name.strip(), "value": value, "url": ORIGIN + "/"}
            for name, value in pairs if name.strip() and value]


def _sync_cookies(context, session, headers):
    host = urlsplit(ORIGIN).hostname
    cookies = context.cookies(ORIGIN)
    for cookie in list(session.cookies):
        if not cookie.domain or cookie.domain.lstrip(".") == host:
            session.cookies.clear(cookie.domain, cookie.path, cookie.name)
    for cookie in cookies:
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"],
                            path=cookie["path"], secure=cookie["secure"])
    if "cookie" in headers:
        headers["cookie"] = "; ".join(c["name"] + "=" + c["value"] for c in cookies)


class BrowserCheckin:
    """共享 Chromium 进程，每次签到创建独立、用后即关的账号 context。"""

    def __init__(self, *, headless=None, timeout_ms=None):
        self.launch_mode = os.getenv("GLADOS_BROWSER_LAUNCH", "playwright").strip().lower()
        if self.launch_mode not in ("playwright", "native"):
            raise BrowserCheckinError("GLADOS_BROWSER_LAUNCH 必须为 playwright 或 native")
        self.channel = os.getenv("GLADOS_BROWSER_CHANNEL", "chromium").strip().lower()
        if self.channel not in ("chromium", "chrome", "msedge"):
            raise BrowserCheckinError("GLADOS_BROWSER_CHANNEL 必须为 chromium、chrome 或 msedge")
        raw = os.getenv("GLADOS_BROWSER_HEADLESS", "true").strip().lower()
        if headless is None and raw not in ("true", "false", "1", "0"):
            raise BrowserCheckinError("GLADOS_BROWSER_HEADLESS 必须为 true 或 false")
        self.headless = raw in ("true", "1") if headless is None else headless
        if timeout_ms is None:
            try:
                seconds = int(os.getenv("GLADOS_BROWSER_TIMEOUT", "120"))
                if not 30 <= seconds <= 300:
                    raise ValueError
            except ValueError:
                raise BrowserCheckinError("GLADOS_BROWSER_TIMEOUT 必须为 30 到 300 秒") from None
            timeout_ms = seconds * 1000
        self.timeout_ms = timeout_ms
        self.runtime = None
        self.browser = None
        self.process = None
        self.profile = None

    def _launch_native(self):
        if self.channel == "chromium":
            executable = self.runtime.chromium.executable_path
        else:
            names = ("google-chrome", "google-chrome-stable", "chrome", "chrome.exe") if self.channel == "chrome" else ("microsoft-edge", "msedge", "msedge.exe")
            executable = next((path for name in names if (path := shutil.which(name))), None)
            if not executable and os.name == "nt":
                relative = ("Google/Chrome/Application/chrome.exe" if self.channel == "chrome" else
                            "Microsoft/Edge/Application/msedge.exe")
                for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                    root = os.getenv(variable)
                    candidate = Path(root) / relative if root else None
                    if candidate and candidate.is_file():
                        executable = str(candidate)
                        break
            if not executable:
                raise BrowserCheckinError("所选原生浏览器不在 PATH 中")
        self.profile = tempfile.TemporaryDirectory(prefix="glados-chrome-")
        # 使用已分配的非零 loopback 端口；不开放远程连接，也不接管用户浏览器。
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        args = [executable, f"--user-data-dir={self.profile.name}",
                f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                "--no-first-run", "--no-default-browser-check", "about:blank"]
        if self.headless:
            args.insert(1, "--headless=new")
        if os.getenv("CI") == "true":
            args.insert(1, "--no-sandbox")  # 与 Playwright 在隔离 CI 中的默认启动方式一致。
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        endpoint = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                # readiness 只连接自己创建的本机浏览器，不记录调试地址或 profile 内容。
                with urllib.request.urlopen(endpoint + "/json/version", timeout=1) as response:
                    if response.status == 200:
                        return self.runtime.chromium.connect_over_cdp(endpoint, timeout=self.timeout_ms)
            except OSError:
                time.sleep(0.1)
        raise BrowserCheckinError("原生 Chrome 启动超时或提前退出")

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise BrowserCheckinError("请安装 requirements.txt 依赖及 Chromium：python -m playwright install chromium") from None
        try:
            self.runtime = sync_playwright().start()
            self.browser = (self._launch_native() if self.launch_mode == "native" else
                            self.runtime.chromium.launch(headless=self.headless, channel=self.channel))
            page = self.browser.new_page()
            self.user_agent = page.evaluate("navigator.userAgent")
            page.close()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise BrowserCheckinError("浏览器启动失败：chrome/msedge 需要已安装对应浏览器；chromium 请运行 python -m playwright install --with-deps chromium。有界面模式需要显示器或 xvfb") from None

    def __exit__(self, *_):
        try:
            if self.browser:
                self.browser.close()
        finally:
            try:
                if self.runtime:
                    self.runtime.stop()
            finally:
                if self.process and self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
                if self.profile:
                    profile_path = Path(self.profile.name).resolve()
                    if (profile_path.parent != Path(tempfile.gettempdir()).resolve()
                            or not profile_path.name.startswith("glados-chrome-")):
                        raise BrowserCheckinError("浏览器临时目录边界检查失败，已跳过清理")
                    self.profile.cleanup()

    def _run(self, session, headers, expected_email, readonly):
        context = self.browser.new_context(user_agent=self.user_agent, locale="en-US",
                                           service_workers="block")
        context.set_default_timeout(self.timeout_ms)
        page = context.new_page()
        sent = False
        armed = False
        base = urlsplit(ORIGIN)
        turnstile_codes = set()
        turnstile_http = set()
        blocked_verification = 0

        def console_message(message):
            # 只收集固定格式的错误码，不记录控制台原文（可能包含账号或令牌）。
            for code in re.findall(r"Turnstile\][^\d]{0,40}(\d{6})", message.text):
                turnstile_codes.add(code)

        def resource_response(response):
            if urlsplit(response.url).hostname == "challenges.cloudflare.com":
                turnstile_http.add(response.status)

        def guard(route):
            nonlocal sent, blocked_verification
            request = route.request
            url = urlsplit(request.url)
            if (url.scheme, url.netloc) == (base.scheme, base.netloc) and request.method not in ("GET", "HEAD", "OPTIONS"):
                # 官网页面只能提交一次签到，不允许兑换、游戏、退出或验证码写操作。
                if readonly or not armed or sent or request.method != "POST" or url.path != "/api/user/checkin":
                    if url.path.startswith("/cdn-cgi/challenge-platform/"):
                        blocked_verification += 1
                    route.abort()
                    return
                sent = True
            route.continue_()

        context.route("**/*", guard)
        page.on("console", console_message)
        page.on("response", resource_response)
        try:
            context.add_cookies(_cookies_for_browser(session, headers))
            response = page.goto(ORIGIN + "/api/user/status", wait_until="domcontentloaded")
            if response is None:
                raise BrowserCheckinError("浏览器状态查询没有响应")
            if response.status == 401:
                return {"code": -2, "message": "unauthorized"}
            try:
                status = response.json()
            except Exception:
                raise BrowserCheckinError("浏览器状态查询被拦截或未返回 JSON；未触发邮箱重登") from None
            if not isinstance(status, dict):
                raise BrowserCheckinError("浏览器状态查询格式异常")
            if is_auth_failure(status):
                return status
            actual_email = (status.get("data") or {}).get("email")
            if response.status != 200 or status.get("code") != 0 or not isinstance(actual_email, str):
                raise BrowserCheckinError("浏览器状态查询失败；未触发邮箱重登")
            if expected_email and actual_email.strip().lower() != expected_email.lower():
                raise BrowserCheckinError("浏览器返回的账号身份不匹配，已停止签到")

            def is_api(request, path, method):
                url = urlsplit(request.url)
                return (url.scheme, url.netloc, url.path) == (base.scheme, base.netloc, path) and request.method == method

            # 先等待正文接收完毕，再读 JSON；仅等 response 头会漏掉正文悬挂超时。
            with page.expect_event("requestfinished", predicate=lambda request: is_api(
                    request, "/api/user/checkin/challenge", "GET"), timeout=self.timeout_ms) as loaded:
                page.goto(ORIGIN + "/console/checkin", wait_until="domcontentloaded")
                button = page.locator("button.ckb")
                button.wait_for(state="visible")
            challenge_response = loaded.value.response()
            try:
                challenge = challenge_response.json() if challenge_response else None
            except Exception:
                challenge = None
            if not isinstance(challenge, dict):
                raise BrowserCheckinError("官网签到 challenge 未正常加载，未提交签到")
            if is_auth_failure(challenge):
                return challenge
            if challenge.get("code") != 0 or not challenge.get("nonce"):
                raise BrowserCheckinError("官网签到 challenge 被拒绝，未提交签到")
            if readonly:
                # 读取页面实际生成的令牌状态，不输出 nonce/proof/令牌本身。
                if challenge.get("turnstile"):
                    try:
                        page.wait_for_function(TOKEN_READY, timeout=min(self.timeout_ms, 30000))
                    except Exception:
                        pass
                ready = page.evaluate(TOKEN_READY)
                visibility = page.evaluate("({focused:document.hasFocus(),visible:document.visibilityState})")
                if not ready and os.getenv("GLADOS_DIAGNOSTIC_SCREENSHOTS") == "true":
                    frames = page.locator('iframe[src^="https://challenges.cloudflare.com/"]')
                    candidates = frames.all()
                    fields = page.locator('input[name="cf-turnstile-response"]')
                    if fields.count():
                        candidates.append(fields.first.locator(".."))
                    for frame in candidates:
                        box = frame.bounding_box()
                        if box and 20 <= box["width"] <= 400 and 20 <= box["height"] <= 150:
                            # 只裁剪公开验证框，绝不截图包含邮箱/积分的完整账号页面。
                            folder = Path(".glados/diagnostics")
                            folder.mkdir(parents=True, exist_ok=True)
                            self._capture_index = getattr(self, "_capture_index", 0) + 1
                            try:
                                frame.screenshot(path=str(folder / f"turnstile-{self._capture_index}.png"), timeout=5000)
                            except Exception:
                                pass
                            break
                return {"code": 0, "challenge_loaded": challenge.get("code") == 0 and bool(challenge.get("nonce")),
                        "turnstile_required": bool(challenge.get("turnstile")), "turnstile_ready": ready,
                        "turnstile_fields": page.locator('input[name="cf-turnstile-response"]').count(),
                        "turnstile_http": sorted(turnstile_http), "turnstile_codes": sorted(turnstile_codes),
                        "blocked_verification": blocked_verification, "visibility": visibility}

            logger.info("通过官网浏览器页面执行签到验证（当前浏览器会话最多提交一次）")
            armed = True
            with page.expect_event("requestfinished", predicate=lambda request: is_api(
                    request, "/api/user/checkin", "POST"), timeout=self.timeout_ms) as pending:
                button.click(timeout=self.timeout_ms)
            response = pending.value.response()
            if response is None:
                raise BrowserCheckinError("签到已提交但没有响应，不自动重复提交")
            if response.status == 401:
                return {"code": -2, "message": "unauthorized"}
            try:
                payload = response.json()
            except Exception:
                raise BrowserCheckinError("签到响应不是 JSON，可能被 Cloudflare 拦截；不自动重复提交") from None
            if not isinstance(payload, dict) or "code" not in payload:
                raise BrowserCheckinError("签到响应格式异常，不自动重复提交")
            if response.status >= 400 and not is_auth_failure(payload):
                raise BrowserCheckinError("浏览器签到 HTTP 请求失败，不自动重复提交")
            _sync_cookies(context, session, headers)
            return payload
        except BrowserCheckinError:
            raise
        except Exception:
            if sent:
                raise BrowserCheckinError("签到已提交但未能确认结果；为避免重复操作，本轮不再提交") from None
            raise BrowserCheckinError("官网签到页加载或验证超时；如 Cloudflare 要求人工验证，请在浏览器完成后再试") from None
        finally:
            context.close()

    def submit(self, session, headers, expected_email=None):
        return self._run(session, headers, expected_email, readonly=False)

    def diagnose(self, session, headers, expected_email=None):
        return self._run(session, headers, expected_email, readonly=True)

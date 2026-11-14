"""离线测试：本机 fixture 模拟官网，不连接 GLaDOS 或 Cloudflare。"""
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

try:
    import browser_checkin as browser
except ImportError:
    browser = None


class BrowserContractTests(unittest.TestCase):
    def test_browser_transport_is_available(self):
        self.assertIsNotNone(browser, "尚未实现官网浏览器签到适配")

    def test_only_target_host_cookies_are_imported_and_legacy_header_takes_precedence(self):
        session = requests.Session()
        session.cookies.set("gld:sess", "own", domain="glados.cloud")
        session.cookies.set("foreign", "do-not-copy", domain="other.example")
        cookies = browser._cookies_for_browser(session, {})
        self.assertEqual([c["name"] for c in cookies], ["gld:sess"])
        cookies = browser._cookies_for_browser(session, {"cookie": "gld:sess=legacy==; gld:sess.sig=sig"})
        self.assertEqual([c["value"] for c in cookies], ["legacy==", "sig"])

    def test_invalid_browser_settings_fail_before_opening_any_mailbox(self):
        for env in ({"GLADOS_BROWSER_HEADLESS": "invalid"}, {"GLADOS_BROWSER_TIMEOUT": "-1"},
                    {"GLADOS_BROWSER_CHANNEL": "invalid"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(browser.BrowserCheckinError):
                    browser.BrowserCheckin()


PAGE = """<!doctype html><html><body>
<input type="hidden" name="cf-turnstile-response" value="TOKEN">
<button class="ckb" disabled>Check in</button>
<script>
fetch('/api/user/checkin/challenge').then(r=>r.json()).then(c=>{
 const button=document.querySelector('.ckb');button.disabled=false;
 button.onclick=async()=>{
   button.disabled=true;
   fetch('/api/user/exchange',{method:'POST',body:'blocked'}).catch(()=>{});
   const payload={token:location.hostname,nonce:c.nonce,
     proof:'fixture-proof:'+c.nonce,tsState:'ok',
     turnstile:document.querySelector('input').value};
   const r=await fetch('/api/user/checkin',{method:'POST',
     headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
   const result=await r.json();button.textContent=result.message;
 };
});
</script></body></html>"""


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, payload, status=200, content_type="application/json", cookie=None):
        raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.server.gets.append((self.path, self.headers.get("Cookie", "")))
        if self.path == "/api/user/status":
            self.respond(self.server.status)
        elif self.path == "/api/user/checkin/challenge":
            self.respond({"code": 0, "nonce": "test-nonce", "ttl": 300000,
                          "turnstile": "test-sitekey"})
        elif self.path == "/console/checkin":
            page = PAGE.replace("TOKEN", self.server.token)
            if self.server.background:
                page = page.replace("<script>", "<script>fetch('/api/user/checkin',{method:'POST',body:'background'}).catch(()=>{});")
            self.respond(page, content_type="text/html")
        else:
            self.respond({}, 404)

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.posts.append((self.path, data))
        if self.path == "/api/user/checkin":
            if self.server.stall:
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{')
                self.wfile.flush()
                time.sleep(4)
                return
            self.respond(self.server.result, cookie="gld:sess=rotated; Path=/")
        else:
            self.respond({"code": 0})


@unittest.skipUnless(os.getenv("GLADOS_BROWSER_TESTS") == "1", "启用 GLADOS_BROWSER_TESTS=1 运行离线 Chromium 测试")
class BrowserFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if browser is None:
            raise AssertionError("尚未实现官网浏览器签到适配")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = "http://127.0.0.1:%d" % cls.server.server_port
        cls.origin_patch = patch.object(browser, "ORIGIN", cls.origin)
        cls.origin_patch.start()
        cls.driver = browser.BrowserCheckin(headless=True, timeout_ms=3000)
        cls.driver.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.driver.__exit__(None, None, None)
        cls.origin_patch.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.posts = []
        self.server.gets = []
        self.server.status = {"code": 0, "data": {"email": "first@example.com"}}
        self.server.result = {"code": 0, "points": 7, "message": "Got 7 points"}
        self.server.token = "fixture-token"
        self.server.background = False
        self.server.stall = False
        self.session = requests.Session()
        self.session.cookies.set("gld:sess", "original", domain="127.0.0.1", path="/")
        self.session.cookies.set("gld:sess.sig", "sig", domain="127.0.0.1", path="/")

    def tearDown(self):
        self.session.close()

    def test_official_page_builds_payload_and_other_writes_are_blocked(self):
        result = self.driver.submit(self.session, {}, "first@example.com")
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(self.server.posts), 1)
        path, raw = self.server.posts[0]
        self.assertEqual(path, "/api/user/checkin")
        self.assertEqual(json.loads(raw)["proof"], "fixture-proof:test-nonce")
        self.assertEqual(json.loads(raw)["turnstile"], "fixture-token")
        self.assertEqual(self.session.cookies.get("gld:sess"), "rotated")

    def test_wrong_identity_stops_before_any_post(self):
        with self.assertRaisesRegex(browser.BrowserCheckinError, "身份"):
            self.driver.submit(self.session, {}, "other@example.com")
        self.assertEqual(self.server.posts, [])

    def test_expired_session_is_returned_for_existing_mail_recovery(self):
        self.server.status = {"code": -2, "message": "unauthorized"}
        result = self.driver.submit(self.session, {}, "first@example.com")
        self.assertEqual(result["code"], -2)
        self.assertEqual(self.server.posts, [])

    def test_turnstile_rejection_is_not_retried_or_classified_as_auth(self):
        self.server.result = {"code": 4, "reason": "turnstile", "message": "Cloudflare verification failed"}
        result = self.driver.submit(self.session, {}, "first@example.com")
        self.assertEqual(result["reason"], "turnstile")
        self.assertEqual(len(self.server.posts), 1)

    def test_readonly_diagnostic_observes_challenge_without_post_or_cookie_update(self):
        before = list(self.session.cookies)
        result = self.driver.diagnose(self.session, {}, "first@example.com")
        self.assertTrue(result["challenge_loaded"])
        self.assertTrue(result["turnstile_required"])
        self.assertTrue(result["turnstile_ready"])
        self.assertEqual(self.server.posts, [])
        self.assertEqual(list(self.session.cookies), before)

    def test_diagnostic_reports_unfinished_turnstile_without_claiming_success(self):
        self.server.token = ""
        result = self.driver.diagnose(self.session, {}, "first@example.com")
        self.assertFalse(result["turnstile_ready"])
        self.assertEqual(self.server.posts, [])

    def test_account_context_does_not_leak_cookies_to_next_account(self):
        self.driver.submit(self.session, {}, "first@example.com")
        empty = requests.Session()
        self.driver.diagnose(empty, {}, "first@example.com")
        self.assertEqual(list(empty.cookies), [])
        self.assertEqual([c for path, c in self.server.gets if path == "/api/user/status"][-1], "")

    def test_unreadable_post_response_is_not_retried(self):
        self.server.result = "not JSON"
        with self.assertRaisesRegex(browser.BrowserCheckinError, "不自动重复提交"):
            self.driver.submit(self.session, {}, "first@example.com")
        self.assertEqual(len(self.server.posts), 1)

    def test_background_checkin_is_blocked_until_button_is_clicked(self):
        self.server.background = True
        result = self.driver.submit(self.session, {}, "first@example.com")
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(self.server.posts), 1)
        self.assertEqual(json.loads(self.server.posts[0][1])["proof"], "fixture-proof:test-nonce")

    def test_stalled_response_body_times_out_without_another_submission(self):
        self.server.stall = True
        started = time.monotonic()
        with self.assertRaisesRegex(browser.BrowserCheckinError, "未能确认结果"):
            self.driver.submit(self.session, {}, "first@example.com")
        self.assertLess(time.monotonic() - started, 3.9)
        self.assertEqual(len(self.server.posts), 1)


if __name__ == "__main__":
    unittest.main()

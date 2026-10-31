import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import checkin
import glados_auth as auth
from test_glados_auth import QueueSession, reply, settings


def result(index, email, status="repeat", expired=False):
    return {"index": index, "email": checkin.mask_email(email), "status": "test",
            "result": status, "total_points": "0", "remaining_days": "1", "exchange": "-",
            "auth_failed": expired}


class CheckinIntegrationTests(unittest.TestCase):
    def test_http_auth_failure_is_classified_for_recovery(self):
        for code in (401, 403):
            with self.subTest(http=code):
                session = requests.Session()
                response = requests.Response()
                response.status_code = code
                response._content = b'{"code": -2, "message": "unauthorized"}'
                with patch.object(session, "post", return_value=response):
                    outcome = checkin.checkin_account(session, None, 1)
                self.assertTrue(outcome["auth_failed"])

    def test_cloudflare_html_forbidden_is_not_treated_as_expired_cookie(self):
        session = requests.Session()
        response = requests.Response()
        response.status_code = 403
        response._content = b'<html>challenge</html>'
        with patch.object(session, "post", return_value=response):
            outcome = checkin.checkin_account(session, None, 1)
        self.assertFalse(outcome["auth_failed"])

    def test_authenticated_cookie_jar_is_not_cleared_or_overridden(self):
        session = requests.Session()
        session.cookies.set("gld:sess", "valid")
        session.cookies.set("gld:sess.sig", "sig")

        def remote(s, headers):
            return {"code": 0, "message": "Got 1 points"} if s.cookies.get("gld:sess") == "valid" and "cookie" not in headers else {"code": -2}

        with patch.object(checkin, "checkin_request", side_effect=remote), \
                patch.object(checkin, "api_get", side_effect=[
                    {"code": 0, "data": {"email": "first@gmail.com", "leftDays": 1}},
                    {"code": 0, "points": 1}]):
            self.assertEqual(checkin.checkin_account(session, None, 1)["result"], "ok")

    def test_auth_failure_never_reaches_points_exchange(self):
        exchanges = []
        with patch.object(checkin, "checkin_request", return_value={"code": -2, "message": "没有权限"}), \
                patch.object(checkin, "api_get", side_effect=[{"code": 0, "data": {}}, {"points": 500}]), \
                patch.object(checkin, "exchange_request", side_effect=lambda *a: exchanges.append(a)):
            outcome = checkin.checkin_account(requests.Session(), "gld:sess=x; gld:sess.sig=y", 1, "plan500")
        self.assertEqual(exchanges, [])
        self.assertTrue(outcome["auth_failed"])

    def test_legacy_cookie_entry_point_still_runs_without_mailbox(self):
        with patch.dict("os.environ", {"COOKIES": "gld:sess=x; gld:sess.sig=y"}, clear=True), \
                patch.object(checkin, "checkin_request", return_value={"code": 1}), \
                patch.object(checkin, "api_get", side_effect=[
                    {"code": 0, "data": {"email": "first@gmail.com"}}, {"points": 1}]), \
                patch("glados_auth.GmailMailbox", side_effect=AssertionError("legacy mode opened mail")):
            self.assertEqual(checkin.main(), 0)

    def test_wrong_account_status_stops_before_exchange(self):
        exchanges = []
        with patch.object(checkin, "checkin_request", return_value={"code": 0}), \
                patch.object(checkin, "api_get", return_value={"code": 0, "data": {"email": "second@gmail.com"}}), \
                patch.object(checkin, "exchange_request", side_effect=lambda *a: exchanges.append(a)):
            outcome = checkin.checkin_account(requests.Session(), None, 1, "plan500", expected_email="first@gmail.com")
        self.assertEqual(outcome["result"], "fail")
        self.assertEqual(exchanges, [])


class EmailAccountsTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(checkin, "run_email_accounts"), "主流程尚未接入邮箱模式")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = settings()
        self.store = auth.CookieStore(Path(self.tmp.name) / "cookies.enc", self.config.key)
        self.store.put("first@gmail.com", {"gld:sess": "first-cookie", "gld:sess.sig": "sig1"})
        self.store.put("second@gmail.com", {"gld:sess": "second-cookie", "gld:sess.sig": "sig2"})

    def test_session_diagnostic_never_logs_in_checks_in_or_changes_cache(self):
        self.assertTrue(hasattr(checkin, "check_cached_sessions"), "尚无只读会话诊断入口")
        self.config.emails[:] = ["first@gmail.com", "second@gmail.com"]
        sessions = [QueueSession([reply("GET", "/user/status", {"code": 0, "data": {"email": "first@gmail.com"}})]),
                    QueueSession([reply("GET", "/user/status", {"code": -2})])]
        before = self.store.path.read_bytes()
        with patch.object(checkin.requests, "Session", side_effect=sessions), \
                patch("glados_auth.GmailMailbox", side_effect=AssertionError("diagnostic requested mail")), \
                patch.object(checkin, "checkin_request", side_effect=AssertionError("diagnostic checked in")), \
                patch.object(checkin, "exchange_request", side_effect=AssertionError("diagnostic exchanged")):
            self.assertEqual(checkin.check_cached_sessions(self.config, self.store), 1)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertTrue(all(not session.replies for session in sessions))

    def test_one_account_login_failure_does_not_block_another_account(self):
        first = QueueSession([reply("GET", "/user/status", {"code": -2}),
                              reply("POST", "/authorization", {"code": 0, "captcha_required": True})])
        second = QueueSession([reply("GET", "/user/status", {"code": 0, "data": {"email": "second@gmail.com"}})])
        mailbox = MagicMock()
        with patch.object(checkin.requests, "Session", side_effect=[first, second]), \
                patch("glados_auth.GmailMailbox", return_value=mailbox), \
                patch.object(checkin, "checkin_account", return_value=result(2, "second@gmail.com")), \
                patch.object(checkin.time, "sleep"):
            outcomes = checkin.run_email_accounts(self.config, self.store)
        self.assertEqual([x["result"] for x in outcomes], ["fail", "repeat"])
        self.assertEqual(second.sent[0][3]["gld:sess"], "second-cookie")
        self.assertEqual(self.store.get("first@gmail.com"), {})

    def test_expiry_between_preflight_and_checkin_reauthenticates_once(self):
        self.config.emails[:] = ["first@gmail.com"]
        session = QueueSession([
            reply("GET", "/user/status", {"code": 0, "data": {"email": "first@gmail.com"}}),
            reply("POST", "/authorization", {"code": 0}),
            reply("POST", "/login", {"code": 0}, {"gld:sess": "renewed", "gld:sess.sig": "sig"}),
            reply("GET", "/user/status", {"code": 0, "data": {"email": "first@gmail.com"}})])
        mailbox = MagicMock()
        mailbox.__enter__.return_value.wait_for_code.return_value = "123456"
        with patch.object(checkin.requests, "Session", return_value=session), \
                patch("glados_auth.GmailMailbox", return_value=mailbox), \
                patch.object(checkin, "checkin_account", side_effect=[
                    result(1, "first@gmail.com", "fail", True), result(1, "first@gmail.com")]):
            outcomes = checkin.run_email_accounts(self.config, self.store)
        self.assertEqual(outcomes[0]["result"], "repeat")
        self.assertEqual(self.store.get("first@gmail.com")["gld:sess"], "renewed")
        self.assertEqual(session.replies, [])


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import requests
from cryptography.fernet import Fernet

try:
    import glados_auth as auth
except ImportError:
    auth = None


NOW = datetime(2026, 10, 31, 5, 0, tzinfo=timezone.utc).timestamp()


def mail(to="first@gmail.com", sender="auth@glados.network", body="Your login code: 123456", when=NOW):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Delivered-To"] = "collector@gmail.com"
    msg["Date"] = datetime.fromtimestamp(when, timezone.utc)
    msg["Subject"] = "GLaDOS verification code"
    msg.set_content(body)
    return msg


class MailMatchingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(auth, "邮箱恢复模块尚未实现")

    def code(self, msg, target="first@gmail.com"):
        return auth.extract_mail_code(msg.as_bytes(), target, NOW, ["glados.network"])

    def test_forwarded_mail_matches_original_to_not_collector(self):
        self.assertEqual(self.code(mail()), "123456")
        self.assertIsNone(self.code(mail(), "collector@gmail.com"))

    def test_old_wrong_recipient_and_untrusted_sender_are_ignored(self):
        for msg in (mail(when=NOW-600), mail(to="second@gmail.com"),
                    mail(sender="auth@glados.network.evil.example")):
            with self.subTest(headers=str(msg.items())):
                self.assertIsNone(self.code(msg))

    def test_exact_sender_override(self):
        msg = mail(sender="codes@provider.example")
        self.assertEqual(auth.extract_mail_code(msg.as_bytes(), "first@gmail.com", NOW,
                                               ["codes@provider.example"]), "123456")

    def test_html_and_mime_encoding(self):
        msg = mail(body="No plain code here")
        msg.add_alternative("<p>验证码：<strong>654321</strong></p>", subtype="html")
        self.assertEqual(self.code(msg), "654321")

    def test_ambiguous_codes_are_not_guessed(self):
        self.assertIsNone(self.code(mail(body="Login codes: 123456 or 654321")))

    def test_gmail_aliases_are_not_collapsed(self):
        self.assertIsNone(self.code(mail(to="first+other@gmail.com")))

    def test_missing_date_and_future_date_are_not_accepted(self):
        msg = mail()
        del msg["Date"]
        self.assertIsNone(self.code(msg))
        self.assertIsNone(self.code(mail(when=NOW+3600)))


class ConfigAndStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(auth, "邮箱恢复模块尚未实现")

    def test_account_names_are_normalized_deduplicated_without_merging_aliases(self):
        self.assertEqual(auth.parse_emails("First@gmail.com\nfirst@gmail.com,first+tag@gmail.com"),
                         ["first@gmail.com", "first+tag@gmail.com"])
        with self.assertRaises(auth.AuthError):
            auth.parse_emails("first@gmail.com\nnot-an-address")

    def test_invalid_mail_settings_fail_before_network(self):
        with patch.dict("os.environ", {"GLADOS_EMAILS": "first@gmail.com"}, clear=True):
            with self.assertRaises(auth.AuthError):
                auth.MailSettings.from_env()

    def test_blank_optional_workflow_secrets_use_defaults(self):
        env = {"GLADOS_EMAILS": "first@gmail.com", "GMAIL_ADDRESS": "collector@gmail.com",
               "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop", "COOKIE_STORE_KEY": Fernet.generate_key().decode(),
               "GMAIL_FROM": ""}
        with patch.dict("os.environ", env, clear=True):
            config = auth.MailSettings.from_env()
        self.assertIn("glados.network", config.senders)
        self.assertEqual(config.password, "abcdefghijklmnop")

    def test_store_encryption_roundtrip_account_isolation_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.enc"
            key = Fernet.generate_key().decode()
            store = auth.CookieStore(path, key)
            store.put("first@gmail.com", {"gld:sess": "secret-one", "gld:sess.sig": "signature"})
            store.put("second@gmail.com", {"gld:sess": "secret-two", "gld:sess.sig": "signature"})
            self.assertNotIn(b"secret-one", path.read_bytes())
            self.assertNotIn(b"first@gmail.com", path.read_bytes())
            restored = auth.CookieStore(path, key)
            self.assertEqual(restored.get("first@gmail.com")["gld:sess"], "secret-one")
            self.assertEqual(restored.get("second@gmail.com")["gld:sess"], "secret-two")
            restored.remove("first@gmail.com")
            self.assertEqual(auth.CookieStore(path, key).get("first@gmail.com"), {})
            self.assertEqual(auth.CookieStore(path, Fernet.generate_key().decode()).get("second@gmail.com"), {})
            path.write_bytes(b"broken-token")
            self.assertEqual(auth.CookieStore(path, key).get("second@gmail.com"), {})


def settings():
    return auth.MailSettings(["first@gmail.com", "second@gmail.com"], "collector@gmail.com",
                             "app-password", Fernet.generate_key().decode(), ("glados.network",))


class FakeIMAP:
    def __init__(self):
        self.readonly = None
        self.fetched = []
        self.logged_out = False
        self.validity = 7

    def login(self, address, password):
        return "OK", []

    def select(self, folder, readonly=False):
        self.readonly = readonly
        return "OK", [b"2"]

    def status(self, folder, query):
        return "OK", [f'"INBOX" (UIDNEXT 100 UIDVALIDITY {self.validity})'.encode()]

    def uid(self, command, *args):
        if command == "search":
            # IMAP n:* can return the old highest UID when there is no new mail.
            return "OK", [b"99 100 101"]
        uid, query = args
        if query != "(BODY.PEEK[])":
            raise AssertionError("mail must not be marked as read")
        self.fetched.append(uid)
        msg = mail(to="second@gmail.com" if uid == b"100" else "first@gmail.com")
        return "OK", [(b"body", msg.as_bytes()), b")"]

    def logout(self):
        self.logged_out = True
        return "BYE", []


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(auth, "GmailMailbox"), "IMAP 读取尚未实现")

    def test_new_uid_only_peek_and_original_recipient(self):
        server = FakeIMAP()
        with patch("glados_auth.imaplib.IMAP4_SSL", return_value=server):
            with auth.GmailMailbox(settings()) as mailbox:
                self.assertEqual(mailbox.wait_for_code("first@gmail.com", NOW), "123456")
        self.assertTrue(server.readonly)
        self.assertNotIn(b"99", server.fetched)
        self.assertTrue(server.logged_out)

    def test_mailbox_reset_is_not_allowed_to_reuse_old_codes(self):
        server = FakeIMAP()
        with patch("glados_auth.imaplib.IMAP4_SSL", return_value=server):
            with auth.GmailMailbox(settings()) as mailbox:
                server.validity = 8
                with self.assertRaises(auth.AuthError):
                    mailbox.wait_for_code("first@gmail.com", NOW)

    def test_imap_login_errors_do_not_echo_passwords(self):
        server = FakeIMAP()
        server.login = lambda *args: (_ for _ in ()).throw(Exception("app-password"))
        with patch("glados_auth.imaplib.IMAP4_SSL", return_value=server):
            with self.assertRaises(auth.AuthError) as error:
                with auth.GmailMailbox(settings()):
                    pass
        self.assertNotIn("app-password", str(error.exception))
        self.assertTrue(server.logged_out)

    def test_no_new_mail_times_out_without_fetching_old_highest_uid(self):
        server = FakeIMAP()
        original_uid = server.uid
        server.uid = lambda command, *args: ("OK", [b"99"]) if command == "search" else original_uid(command, *args)
        with patch("glados_auth.imaplib.IMAP4_SSL", return_value=server), \
                patch("glados_auth.time.monotonic", side_effect=[0, 0, 119, 120, 120]), \
                patch("glados_auth.time.sleep"):
            with auth.GmailMailbox(settings()) as mailbox:
                with self.assertRaises(auth.AuthError):
                    mailbox.wait_for_code("first@gmail.com", NOW)
        self.assertEqual(server.fetched, [])

    def test_distinct_codes_across_new_messages_are_not_guessed(self):
        server = FakeIMAP()
        original_uid = server.uid

        def uid(command, *args):
            if command == "search":
                return original_uid(command, *args)
            message = mail(body="Your login code: " + ("111111" if args[0] == b"100" else "222222"))
            return "OK", [(b"body", message.as_bytes())]

        server.uid = uid
        with patch("glados_auth.imaplib.IMAP4_SSL", return_value=server):
            with auth.GmailMailbox(settings()) as mailbox:
                with self.assertRaises(auth.AuthError):
                    mailbox.wait_for_code("first@gmail.com", NOW)

    def test_slow_fetch_does_not_return_code_after_deadline(self):
        server = FakeIMAP()
        clock = [0]
        original_uid = server.uid

        def slow_uid(command, *args):
            if command == "fetch":
                clock[0] += 14
            return original_uid(command, *args)

        server.uid = slow_uid
        config = settings()
        config = auth.MailSettings(config.emails, config.address, config.password, config.key,
                                   config.senders, wait_seconds=10)
        with patch("glados_auth.imaplib.IMAP4_SSL", return_value=server), \
                patch("glados_auth.time.monotonic", side_effect=lambda: clock[0]), \
                patch("glados_auth.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0]+seconds)):
            with auth.GmailMailbox(config) as mailbox:
                with self.assertRaises(auth.AuthError):
                    mailbox.wait_for_code("first@gmail.com", NOW)


class QueueSession(requests.Session):
    """Only the external HTTP boundary is replaced; real cookie jar and headers remain."""
    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)
        self.sent = []

    def request(self, method, url, **kwargs):
        expected_method, path, body, cookies = self.replies.pop(0)
        if method.upper() != expected_method or not url.endswith(path):
            raise AssertionError((method, url, expected_method, path))
        self.sent.append((method.upper(), path, kwargs, dict(self.cookies)))
        if isinstance(body, Exception):
            raise body
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode()
        response.cookies = requests.utils.cookiejar_from_dict(cookies)
        return response


def reply(method, path, body, cookies=None):
    return method, path, body, cookies or {}


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(auth, "EmailAuthenticator"), "邮箱登录尚未实现")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = settings()
        self.store = auth.CookieStore(Path(self.tmp.name) / "cookies.enc", self.settings.key)
        self.client = auth.EmailAuthenticator(self.settings, self.store, {"user-agent": "same-ua"})

    def login_replies(self, email="first@gmail.com", challenge=False):
        return [reply("POST", "/authorization", {"code": 0, "captcha_required": challenge}),
                reply("POST", "/login", {"code": 0}, {"gld:sess": "new-session", "gld:sess.sig": "new-sig"}),
                reply("GET", "/user/status", {"code": 0, "data": {"email": email}})]

    def mailbox_patch(self):
        mailbox = unittest.mock.MagicMock()
        mailbox.__enter__.return_value.wait_for_code.return_value = "654321"
        return patch("glados_auth.GmailMailbox", return_value=mailbox)

    def test_expired_cookie_relogs_and_persists_verified_identity(self):
        self.store.put("first@gmail.com", {"gld:sess": "expired", "gld:sess.sig": "old"})
        session = QueueSession([reply("GET", "/user/status", {"code": -2, "message": "没有权限"})]
                               + self.login_replies())
        with self.mailbox_patch():
            self.assertTrue(self.client.prepare(session, "first@gmail.com"))
        self.assertEqual(self.store.get("first@gmail.com")["gld:sess"], "new-session")
        self.assertEqual(session.sent[1][2]["json"], {"address": "first@gmail.com", "site": "glados.network"})
        self.assertEqual(session.sent[2][2]["json"], {"method": "email", "site": "glados.network",
                                                      "email": "first@gmail.com", "mailcode": "654321"})
        self.assertEqual(session.sent[1][3], {})  # old account cookies never reach login
        self.assertNotIn("cookie", session.headers)
        self.assertEqual(session.replies, [])

    def test_valid_cached_cookie_does_not_open_mailbox(self):
        self.store.put("first@gmail.com", {"gld:sess": "valid", "gld:sess.sig": "sig"})
        session = QueueSession([reply("GET", "/user/status", {"code": 0, "data": {"email": "first@gmail.com"}})])
        with patch("glados_auth.GmailMailbox", side_effect=AssertionError("no email expected")):
            self.assertFalse(self.client.prepare(session, "first@gmail.com"))

    def test_second_process_reuses_cookie_saved_by_first_login(self):
        session = QueueSession(self.login_replies())
        with self.mailbox_patch():
            self.assertTrue(self.client.prepare(session, "first@gmail.com"))
        # A new store/client/session simulates restarting Python with the same key.
        restored = auth.CookieStore(self.store.path, self.settings.key)
        client = auth.EmailAuthenticator(self.settings, restored, {"user-agent": "same-ua"})
        second = QueueSession([reply("GET", "/user/status", {"code": 0, "data": {"email": "first@gmail.com"}})])
        with patch("glados_auth.GmailMailbox", side_effect=AssertionError("second process requested email")):
            self.assertFalse(client.prepare(second, "first@gmail.com"))
        self.assertEqual(second.sent[0][3]["gld:sess"], "new-session")

    def test_wrong_account_after_login_is_not_cached(self):
        session = QueueSession(self.login_replies(email="second@gmail.com"))
        with self.mailbox_patch(), self.assertRaises(auth.AuthError):
            self.client.prepare(session, "first@gmail.com")
        self.assertEqual(self.store.get("first@gmail.com"), {})

    def test_captcha_stops_without_login_or_exposing_response(self):
        session = QueueSession([reply("POST", "/authorization", {"code": 0, "captcha_required": True,
                                                                   "message": "private-server-data"})])
        with self.mailbox_patch(), self.assertRaises(auth.AuthError) as error:
            self.client.prepare(session, "first@gmail.com")
        self.assertNotIn("private-server-data", str(error.exception))
        self.assertEqual(len(session.sent), 1)

    def test_network_failure_does_not_trigger_verification_email(self):
        self.store.put("first@gmail.com", {"gld:sess": "valid", "gld:sess.sig": "sig"})
        session = QueueSession([reply("GET", "/user/status", requests.ConnectionError("secret-cookie"))])
        with patch("glados_auth.GmailMailbox", side_effect=AssertionError("no email expected")):
            with self.assertRaises(auth.AuthError) as error:
                self.client.prepare(session, "first@gmail.com")
        self.assertNotIn("secret-cookie", str(error.exception))


if __name__ == "__main__":
    unittest.main()

"""GLaDOS 邮箱验证码认证；凭据和邮件正文不得写入日志。"""
import imaplib
import json
import logging
import os
import re
import ssl
import tempfile
import time
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

import requests

logger = logging.getLogger("GLaDOS")
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
SESSION_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):sess$")


class AuthError(Exception):
    """只携带可公开显示的错误说明，不包含上游响应或凭据。"""


def parse_emails(raw):
    values = list(dict.fromkeys(x.lower() for x in re.split(r"[\s,;]+", raw.strip()) if x))
    if any(not EMAIL_RE.fullmatch(x) for x in values):
        raise AuthError("GLADOS_EMAILS 包含无效邮箱，请每行填写一个完整登录邮箱")
    return values


@dataclass(frozen=True)
class MailSettings:
    emails: list
    address: str
    password: str = field(repr=False)
    key: str = field(repr=False)
    senders: tuple
    wait_seconds: int = 120
    poll_seconds: int = 5
    folder: str = "INBOX"

    @classmethod
    def from_env(cls):
        emails = parse_emails(os.getenv("GLADOS_EMAILS", ""))
        if not emails:
            return None
        address = os.getenv("GMAIL_ADDRESS", "").strip().lower()
        password = "".join(os.getenv("GMAIL_APP_PASSWORD", "").split())
        key = os.getenv("COOKIE_STORE_KEY", "").strip()
        if not EMAIL_RE.fullmatch(address) or not password or not key:
            raise AuthError("邮箱模式需要 GMAIL_ADDRESS、GMAIL_APP_PASSWORD 和 COOKIE_STORE_KEY")
        senders = tuple(x.lower() for x in re.split(r"[\s,;]+", os.getenv(
            "GMAIL_FROM", "").strip() or "glados.network,glados.cloud,glados.one") if x)
        if not senders or any(not re.fullmatch(r"[A-Za-z0-9@._+-]+", x) for x in senders):
            raise AuthError("GMAIL_FROM 必须为验证码实际发件地址或域名，多个用逗号分隔")
        try:
            wait = int(os.getenv("GMAIL_WAIT_SECONDS", "120"))
            if not 10 <= wait <= 600:
                raise ValueError
        except ValueError:
            raise AuthError("GMAIL_WAIT_SECONDS 必须为 10 到 600 秒") from None
        return cls(emails, address, password, key, senders, wait)


def has_session(cookies):
    return any(SESSION_KEY_RE.fullmatch(k) and v and cookies.get(k + ".sig")
               for k, v in cookies.items())


class CookieStore:
    """按邮箱索引的 Fernet 密文，Actions 缓存中不出现明文邮箱或 Cookie。"""
    def __init__(self, path, key):
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError:
            raise AuthError("邮箱模式请先安装 requirements.txt 中的依赖") from None
        try:
            self.cipher = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeError):
            raise AuthError("COOKIE_STORE_KEY 无效，请按 README 生成 Fernet 密钥") from None
        self.path = Path(path)
        self.data = {}
        self.load_status = "missing"
        try:
            document = json.loads(self.cipher.decrypt(self.path.read_bytes()))
            if document.get("version") != 1 or not isinstance(document.get("accounts"), dict):
                raise ValueError
            self.data = document["accounts"]
            self.load_status = "loaded"
        except FileNotFoundError:
            pass
        except (OSError, InvalidToken, ValueError, TypeError, AttributeError):
            self.load_status = "unreadable"
            logger.warning("Cookie 缓存不可读取或密钥已更换，将按账号重新登录")

    def get(self, email):
        values = self.data.get(email, {})
        if not isinstance(values, dict) or not has_session(values):
            return {}
        if any(not isinstance(k, str) or not isinstance(v, str)
               or not re.fullmatch(r"[A-Za-z0-9_.:-]+", k) or any(c in v for c in "\r\n;")
               for k, v in values.items()):
            return {}
        return dict(values)

    def put(self, email, cookies):
        if not has_session(cookies):
            raise AuthError("服务器未返回完整会话 Cookie")
        self.data[email] = dict(cookies)
        self.save()

    def remove(self, email):
        if email in self.data:
            del self.data[email]
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self.cipher.encrypt(json.dumps({"version": 1, "accounts": self.data}).encode())
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix="cookies-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encrypted)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("br", "p", "div", "td"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("p", "div", "td"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract_mail_code(raw, target, requested_at, senders):
    """转发后的 Delivered-To 是统一邮箱，不能用它判断原始 GLaDOS 账号。"""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        recipients = getaddresses([str(v) for h in ("To", "X-Original-To", "X-Gm-Original-To")
                                   for v in msg.get_all(h, [])])
        if target.lower() not in {a.lower() for _, a in recipients}:
            return None
        sender_values = getaddresses([str(v) for v in msg.get_all("From", [])])
        if len(sender_values) != 1:
            return None
        sender = sender_values[0][1].lower()
        domain = sender.rpartition("@")[2]
        if not any(sender == rule if "@" in rule else
                   domain == rule or domain.endswith("." + rule) for rule in senders):
            return None
        date = parsedate_to_datetime(str(msg.get("Date", "")))
        if date.tzinfo is None or not requested_at - 30 <= date.timestamp() <= requested_at + 660:
            return None
        text = [str(msg.get("Subject", ""))]
        for part in msg.walk():
            if part.get_content_disposition() == "attachment" or part.is_multipart():
                continue
            if part.get_content_type() == "text/plain":
                text.append(part.get_content())
            elif part.get_content_type() == "text/html":
                parser = _HTMLText()
                parser.feed(part.get_content())
                text.append("".join(parser.parts))
        codes = set(re.findall(r"(?<![A-Za-z0-9])([0-9]{6})(?![A-Za-z0-9])", "\n".join(text)))
        return codes.pop() if len(codes) == 1 else None
    except (ValueError, TypeError, LookupError, UnicodeError, AttributeError):
        return None


class GmailMailbox:
    """只读 IMAP；在发验证码请求前进入上下文并取得 UID 边界。"""
    def __init__(self, settings):
        self.settings = settings
        self.client = None
        self.seen = set()

    def __enter__(self):
        try:
            self.client = imaplib.IMAP4_SSL("imap.gmail.com", 993,
                                           ssl_context=ssl.create_default_context(), timeout=15)
            self.client.login(self.settings.address, self.settings.password)
            code, _ = self.client.select(self.settings.folder, readonly=True)
            if code != "OK":
                raise AuthError("无法只读打开 Gmail 收件箱")
            self.next_uid, self.validity = self._uid_state()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise AuthError("Gmail IMAP 连接或认证失败，请检查应用专用密码") from None

    def __exit__(self, *args):
        if self.client is not None:
            try:
                self.client.logout()
            except Exception:
                pass

    def _uid_state(self):
        code, data = self.client.status(self.settings.folder, "(UIDNEXT UIDVALIDITY)")
        if code != "OK":
            raise AuthError("无法确认 Gmail 邮件 UID 边界")
        values = b" ".join(x for x in data if isinstance(x, bytes))
        uid = re.search(rb"UIDNEXT\s+(\d+)", values)
        validity = re.search(rb"UIDVALIDITY\s+(\d+)", values)
        if not uid or not validity:
            raise AuthError("Gmail 未返回邮件 UID 边界")
        return int(uid[1]), int(validity[1])

    def wait_for_code(self, target, requested_at):
        deadline = time.monotonic() + self.settings.wait_seconds
        try:
            while True:
                self._budget(deadline)
                if self._uid_state()[1] != self.validity:
                    raise AuthError("Gmail 邮件 UID 已重置，请下次运行重试")
                self._budget(deadline)
                code, data = self.client.uid("search", None, f"UID {self.next_uid}:*")
                if code != "OK":
                    raise AuthError("Gmail 邮件搜索失败")
                uids = sorted({int(x) for line in data if isinstance(line, bytes)
                               for x in line.split() if x.isdigit() and int(x) >= self.next_uid}, reverse=True)
                candidates = set()
                for uid in uids[:50]:
                    if uid in self.seen:
                        continue
                    self._budget(deadline)
                    code, parts = self.client.uid("fetch", str(uid).encode(), "(BODY.PEEK[])")
                    self._budget(deadline)  # 已超时的响应不得被接受
                    if code != "OK":
                        continue
                    self.seen.add(uid)
                    for item in parts:
                        if not isinstance(item, tuple) or len(item) < 2 or not isinstance(item[1], bytes):
                            continue
                        result = extract_mail_code(item[1], target, requested_at, self.settings.senders)
                        if result:
                            candidates.add(result)
                self._budget(deadline)
                if len(candidates) > 1:
                    raise AuthError("收到多个不同验证码，已停止猜测，请稍后重试")
                if candidates:
                    return candidates.pop()
                time.sleep(min(self.settings.poll_seconds, max(0, deadline - time.monotonic())))
        except AuthError:
            raise
        except Exception:
            raise AuthError("读取 Gmail 验证邮件失败，请检查邮箱连接") from None

    def _budget(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AuthError("等待验证码超时，请检查转发规则、原收件人和 GMAIL_FROM")
        connection = getattr(self.client, "sock", None)
        if connection is not None:
            connection.settimeout(min(15, remaining))


def is_auth_failure(payload):
    message = str(payload.get("message", "")).lower()
    return (payload.get("code") in (-2, "-2")
            or payload.get("reason") == "device-mismatch"
            or any(word in message for word in ("没有权限", "权限不足", "未登录", "登录已失效",
                                                 "unauthorized", "forbidden", "invalid token")))


def session_cookies(session):
    return {c.name: c.value for c in session.cookies if c.value}


class EmailAuthenticator:
    BASE = "https://glados.cloud/api"

    def __init__(self, settings, store, headers):
        self.settings = settings
        self.store = store
        self.headers = dict(headers)

    def _request(self, session, method, path, **kwargs):
        try:
            response = session.request(method, self.BASE + path, timeout=(5, 15),
                                       allow_redirects=False, **kwargs)
            session.cookies.update(response.cookies)
            if response.status_code == 401:
                return {"code": -2, "message": "unauthorized"}
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            if response.status_code in (401, 403) and is_auth_failure(payload):
                return payload
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError
            return payload
        except (requests.RequestException, ValueError):
            raise AuthError("GLaDOS 认证请求失败，请检查网络或服务状态") from None

    def _matches(self, session, email):
        payload = self._request(session, "GET", "/user/status")
        if is_auth_failure(payload):
            logger.warning("服务器拒绝已有 Cookie：登录状态鉴权失败")
            return False
        if payload.get("code") != 0:
            raise AuthError("GLaDOS 状态查询异常，本次不触发验证码登录")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("email"), str):
            raise AuthError("GLaDOS 未返回可验证的账号邮箱")
        matched = data["email"].strip().lower() == email
        if not matched:
            logger.warning("已有 Cookie 对应的邮箱与配置账号不一致")
        return matched

    def cached_status(self, session, email):
        """只恢复并检查已有 Cookie，不登录、不访问邮箱、不改写缓存。"""
        session.headers.update(self.headers)
        session.headers.pop("cookie", None)
        session.cookies.clear()
        cached = self.store.get(email)
        for key, value in cached.items():
            session.cookies.set(key, value, domain="glados.cloud", path="/", secure=True)
        if not cached:
            return "missing"
        return "valid" if self._matches(session, email) else "rejected"

    def prepare(self, session, email):
        """返回是否已执行本轮登录；网络失败不自动请求验证码。"""
        status = self.cached_status(session, email)
        if status == "valid":
            self.store.put(email, session_cookies(session))
            logger.info("Cookie 缓存有效，直接复用，不发送邮件验证码")
            return False
        if status == "missing":
            logger.info("该账号没有可用的 Cookie 缓存，需要邮件验证码登录")
        else:
            logger.info("已有 Cookie 验证未通过，开始邮件验证码登录")
        self.login(session, email)
        return True

    def login(self, session, email):
        self.store.remove(email)
        session.cookies.clear()
        with GmailMailbox(self.settings) as mailbox:
            requested_at = time.time()
            challenge = self._request(session, "POST", "/authorization",
                                      json={"address": email, "site": "glados.network"})
            if challenge.get("captcha_required"):
                raise AuthError("GLaDOS 要求人机验证，请手动登录后再重试")
            if challenge.get("code") != 0:
                raise AuthError("验证码发送失败或被限流，请稍后重试")
            code = mailbox.wait_for_code(email, requested_at)
            payload = self._request(session, "POST", "/login", json={
                "method": "email", "site": "glados.network", "email": email, "mailcode": code})
        if payload.get("code") != 0:
            raise AuthError("验证码登录失败，请检查邮件转发或稍后重试")
        if not has_session(session_cookies(session)) or not self._matches(session, email):
            session.cookies.clear()
            raise AuthError("登录账号身份不匹配或会话无效，已停止该账号操作")
        self.store.put(email, session_cookies(session))

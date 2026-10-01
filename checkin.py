"""
GLaDOS 自动签到脚本
支持多账号、多种推送渠道、重试机制、日志脱敏
"""
import os
import re
import sys
import json
import time
import random
import hashlib
import hmac
import base64
import urllib.parse
import logging
from typing import List, Dict, Any, Tuple, Optional, Callable
from functools import wraps
import requests

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("GLaDOS")

# ==================== 配置 ====================
CHECKIN_URL = "https://glados.cloud/api/user/checkin"
STATUS_URL = "https://glados.cloud/api/user/status"
POINTS_URL = "https://glados.cloud/api/user/points"
EXCHANGE_URL = "https://glados.cloud/api/user/exchange"
HEADERS_BASE = {
    "origin": "https://glados.cloud",
    "referer": "https://glados.cloud/console/checkin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    # 注意：使用 requests 的 json= 参数时会自动设置 Content-Type: application/json，
    # 此处无需（也不应）手动设置 content-type，否则与 requests 默认行为重复。
}
PAYLOAD = {"token": "glados.cloud"}
TIMEOUT = (5, 15)  # (连接超时, 读取超时)
MAX_RETRY = 3
RETRY_MIN_WAIT = 2.0
RETRY_MAX_WAIT = 10.0
MIN_DELAY = 1.0
MAX_DELAY = 2.0
TELEGRAM_MAX_LENGTH = 4000
TELEGRAM_TRUNCATE_LENGTH = 3990
CONTENT_MAX_LENGTH = 3000  # 推送汇总内容统一长度上限，避免超长导致部分渠道发送失败（#4）
COOKIE_MASK_LENGTH = 10
# Cookie 会话字段前缀。GLaDOS 早期使用 koa:sess，现网签发的是 gld:sess，
# 两者语义等价、必须都支持；同时不排除未来再次改名，故实际校验采用
# "<任意前缀>:sess" + "<同前缀>:sess.sig" 的成对匹配，而不是写死某一个前缀。
KNOWN_SESSION_PREFIXES = ("koa", "gld")
_SESS_KEY_RE = re.compile(r"^(?P<prefix>[A-Za-z0-9_.-]+):sess$")
_SIG_KEY_RE = re.compile(r"^(?P<prefix>[A-Za-z0-9_.-]+):sess\.sig$")
# Cookie 缺失/不合法时统一输出的期望格式说明
COOKIE_FORMAT_HINT = (
    "期望 Cookie 格式（koa: 与 gld: 两种前缀任选其一，且必须成对出现）:\n"
    "  koa:sess=xxxxxx; koa:sess.sig=yyyyyy\n"
    "  gld:sess=xxxxxx; gld:sess.sig=yyyyyy\n"
    "获取方式：登录 https://glados.cloud → F12 → Application → Cookies → 复制完整 Cookie 值"
)
# 前后各显示 10 个字符，因此长度必须 > 2*COOKIE_MASK_LENGTH + 3 = 23 才能安全脱敏，
# 设为 24 可避免 len∈[21,23] 时前后片段重叠导致几乎暴露完整 Cookie（M3）。
COOKIE_MIN_LENGTH = 24
# 重复签到判定关键词（L5：提升为模块级常量，便于维护/国际化）
REPEAT_KEYWORDS = ("repeat", "already", "重复", "已签到", "签到过", "请勿")
# 服务端鉴权失败关键词：命中说明 Cookie 已失效/复制不完整，而非格式问题，
# 需要给出"重新获取 Cookie"的可操作提示，避免与格式校验失败混淆。
AUTH_FAIL_KEYWORDS = ("没有权限", "权限不足", "未登录", "登录已失效", "unauthorized", "forbidden")
# 积分兑换计划（#9 功能请求）：消耗 points 积分兑换 days 天会员。
# 仅当用户显式配置 EXCHANGE_PLAN 时才执行，默认不兑换，避免静默消耗积分。
EXCHANGE_PLANS = {
    "plan100": {"points": 100, "days": 10},
    "plan200": {"points": 200, "days": 30},
    "plan500": {"points": 500, "days": 100},
}


# ==================== 工具函数 ====================
def safe_json(resp: requests.Response) -> Dict[str, Any]:
    """安全解析 JSON 响应（用于推送等非关键路径，失败返回空字典）。"""
    try:
        return resp.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        return {}


def require_json(resp: requests.Response) -> Dict[str, Any]:
    """
    严格解析 JSON 响应（用于签到/状态/积分等核心请求路径）。

    - 若响应体不是合法 JSON（如网关 502 的 HTML 错误页、空响应），抛出
      requests.exceptions.RequestException，使调用方 @retry_on_failure 能捕获并重试（M1）。
    - 同时记录原始响应片段（debug），便于排查真实失败原因。
    """
    try:
        return resp.json()
    except ValueError:
        snippet = (resp.text or "<空响应>")[:200]
        logger.debug(
            "非 JSON 响应 (status=%s, content-type=%s): %s",
            resp.status_code,
            resp.headers.get("Content-Type"),
            snippet,
        )
        raise  # requests.exceptions.JSONDecodeError 同时继承 ValueError 和 RequestException，可被 is_retryable 识别


def safe_int_str(val: Any, default: str = "-") -> str:
    """安全将值转为整数字符串，失败时返回默认值"""
    try:
        return str(int(val))
    except (TypeError, ValueError):
        try:
            return str(int(float(val)))
        except (TypeError, ValueError):
            return default


def mask_email(email: str) -> str:
    """
    邮箱脱敏：保留前两个字符和最后一个字符，中间用 *** 替代
    Examples:
        mask_email("test@example.com")   -> "te***t@example.com"
        mask_email("ab@example.com")     -> "***@example.com"
        mask_email("a@example.com")      -> "***@example.com"
        mask_email("unknown")            -> "unknown"
    """
    if not email or email == "unknown" or "@" not in email:
        return email
    try:
        name, domain = email.rsplit("@", 1)
        if not name:
            return email
        if len(name) <= 3:
            masked_name = "***"
        else:
            masked_name = f"{name[:2]}***{name[-1]}"
        return f"{masked_name}@{domain}"
    except Exception:
        return email


def mask_cookie(cookie: str) -> str:
    """Cookie 脱敏（只显示前后各10个字符）。长度不足 COOKIE_MIN_LENGTH 时整体脱敏。"""
    if not cookie or len(cookie) <= COOKIE_MIN_LENGTH:
        return "***"
    return f"{cookie[:COOKIE_MASK_LENGTH]}...{cookie[-COOKIE_MASK_LENGTH:]}"


def _escape_markdown(text: str) -> str:
    """转义 Markdown 特殊字符，防止外部文本破坏推送格式（M6）。"""
    if not text:
        return text
    for ch in ("\\", "`", "*", "_", "#", "[", "]"):
        text = text.replace(ch, f"\\{ch}")
    return text


def parse_earned_points(message: str) -> int:
    """
    从签到成功响应文本中解析本次获得的积分数（H1）。

    GLaDOS 签到接口不返回 points 字段，获得积分数写在 message 中。
    兼容中英文两种文案（与 classify_checkin 的成功判定保持一致）：
      - 英文： "Checkin success, got 1 points"
      - 中文： "已经签到成功，获得 1 点，请明天继续签到哦！"
    解析失败时优雅降级为 0。
    """
    if not message:
        return 0
    # 优先匹配英文 "got N points"（新版 GLaDOS 默认返回此文案）
    m = re.search(r"got\s+(\d+)\s+points?", message, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # 兼容旧版中文文案 "获得 N 点"
    m = re.search(r"获得\s*(\d+)\s*点", message)
    return int(m.group(1)) if m else 0


def normalize_cookie(cookie: str) -> str:
    """
    归一化用户粘贴的 Cookie 字符串（不改变语义，只清理复制过程中混入的杂质）。

    处理项：
      - 首尾空白；
      - 首尾多余的引号（从 JSON/配置文件里复制时常见）；
      - 误带的请求头名前缀 "Cookie:"（从开发者工具 Request Headers 里整行复制时常见）。

    注意：不会改写 koa: / gld: 等前缀，也不会重排字段，避免破坏可正常使用的旧 Cookie。
    """
    c = (cookie or "").strip()
    # 反复剥离可能多层嵌套的引号
    while len(c) >= 2 and c[0] == c[-1] and c[0] in ("\"", "'"):
        c = c[1:-1].strip()
    if c[:7].lower() == "cookie:":
        c = c[7:].strip()
    return c


def parse_cookie_fields(cookie: str) -> Dict[str, Dict[str, bool]]:
    """
    解析 Cookie 中的会话字段，返回 {前缀: {"sess": bool, "sig": bool}}。

    按 ; 拆分后对键名做精确正则匹配（<prefix>:sess / <prefix>:sess.sig），
    不依赖任何单一硬编码前缀，因此 koa: 与 gld: 天然同时兼容。
    """
    found: Dict[str, Dict[str, bool]] = {}
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part:
            continue
        key = part.split("=", 1)[0].strip()
        # 先尝试匹配 .sig，再匹配 sess，避免二者互相误判
        m_sig = _SIG_KEY_RE.match(key)
        if m_sig:
            found.setdefault(m_sig.group("prefix"), {"sess": False, "sig": False})["sig"] = True
            continue
        m_sess = _SESS_KEY_RE.match(key)
        if m_sess:
            found.setdefault(m_sess.group("prefix"), {"sess": False, "sig": False})["sess"] = True
    return found


def detect_session_prefix(cookie: str) -> Optional[str]:
    """返回 Cookie 中成对完整的会话前缀（如 'koa' / 'gld'），不存在则返回 None。"""
    fields = parse_cookie_fields(normalize_cookie(cookie))
    for prefix in KNOWN_SESSION_PREFIXES:  # 已知前缀优先，保证输出稳定
        entry = fields.get(prefix)
        if entry and entry["sess"] and entry["sig"]:
            return prefix
    for prefix, entry in fields.items():
        if entry["sess"] and entry["sig"]:
            return prefix
    return None


def validate_cookie(cookie: str) -> Tuple[bool, str]:
    """
    验证 Cookie 是否包含成对的会话字段（按 ; 拆分 key 精确校验，避免子串误判）。

    兼容 koa:sess/koa:sess.sig（旧版）与 gld:sess/gld:sess.sig（现网），
    也兼容未来可能出现的其它前缀，只要 sess 与 sess.sig 成对同前缀即可。
    """
    c = normalize_cookie(cookie)
    if not c:
        return False, f"Cookie 为空。{COOKIE_FORMAT_HINT}"

    fields = parse_cookie_fields(c)
    if not fields:
        return False, (
            "Cookie 中未找到任何会话字段（应为 <前缀>:sess 与 <前缀>:sess.sig）。"
            f"{COOKIE_FORMAT_HINT}"
        )

    for prefix, entry in fields.items():
        if entry["sess"] and entry["sig"]:
            return True, ""

    # 未成对：给出具体到字段的缺失说明
    details = []
    for prefix in sorted(fields.keys()):
        entry = fields[prefix]
        if entry["sess"] and not entry["sig"]:
            details.append(f"检测到 {prefix}:sess 但缺少 {prefix}:sess.sig")
        elif entry["sig"] and not entry["sess"]:
            details.append(f"检测到 {prefix}:sess.sig 但缺少 {prefix}:sess")
    return False, f"Cookie 格式不合法：{'；'.join(details)}。{COOKIE_FORMAT_HINT}"


def is_retryable(exc: Exception) -> bool:
    """
    判断异常是否可重试（M2）。

    - 网络层异常（超时/连接错误/JSON 解析失败等 RequestException，非 HTTPError）：可重试；
    - HTTPError：仅 5xx 服务端错误可重试，4xx 客户端错误（如 Cookie 失效 401/403）不可重试；
    - 其它异常：不可重试。
    """
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", 0) if resp is not None else 0
        # 429 Too Many Requests 为限流错误，应重试（默认指数退避即可）
        if status == 429:
            return True
        return 500 <= status < 600
    if isinstance(exc, requests.exceptions.RequestException):
        return True
    return False


def retry_on_failure(max_retries: int = MAX_RETRY, min_wait: float = RETRY_MIN_WAIT,
                     max_wait: float = RETRY_MAX_WAIT):
    """重试装饰器（指数退避，仅对可重试异常重试）"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.RequestException as e:
                    last_exception = e
                    if attempt < max_retries and is_retryable(e):
                        wait_time = min(min_wait * (2 ** attempt), max_wait)
                        logger.warning("第 %d 次尝试失败: %s，%.1f秒后重试...",
                                       attempt + 1, e, wait_time)
                        time.sleep(wait_time)
                        continue
                    break  # 不可重试（如 4xx）直接退出
            raise last_exception  # type: ignore
        return wrapper
    return decorator


# ==================== 推送函数 ====================
def _push_request(
    name: str,
    url: str,
    *,
    json_payload: Optional[Dict[str, Any]] = None,
    data_payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    success_check: Callable[[Dict[str, Any], requests.Response], bool],
    fail_msg_keys: Tuple[str, ...] = ("message",),
) -> bool:
    """通用推送请求函数，返回是否推送成功（M4：失败响应截断后记录）。"""
    try:
        if json_payload is not None:
            r = requests.post(url, json=json_payload, headers=headers, timeout=TIMEOUT)
        else:
            r = requests.post(url, data=data_payload, headers=headers, timeout=TIMEOUT)
        if not r.ok:
            logger.warning("%s 推送失败: HTTP %d", name, r.status_code)
            return False
        resp = safe_json(r)
        if success_check(resp, r):
            logger.info("%s 推送成功", name)
            return True
        fail_msg = r.text
        for key in fail_msg_keys:
            if resp.get(key):
                fail_msg = resp[key]
                break
        # 截断失败响应，避免大段 HTML 或可能回显账号标识的敏感信息落入日志（M4）
        if fail_msg and len(fail_msg) > 200:
            fail_msg = fail_msg[:200] + "...(已截断)"
        logger.warning("%s 推送失败: %s", name, fail_msg)
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("%s 推送异常: %s", name, e)
        return False


def push_deer(key: str, title: str, content: str) -> bool:
    """PushDeer 推送"""
    if not key:
        return False
    return _push_request(
        "PushDeer",
        "https://api2.pushdeer.com/message/push",
        json_payload={"pushkey": key, "text": f"{title}\n\n{content}", "type": "text"},
        success_check=lambda resp, r: r.ok and resp.get("code") == 0,
        fail_msg_keys=("message",),
    )


def push_serverchan(key: str, title: str, content: str) -> bool:
    """Server酱推送"""
    if not key:
        return False
    return _push_request(
        "Server酱",
        f"https://sctapi.ftqq.com/{key}.send",
        data_payload={"title": title, "desp": content},
        success_check=lambda resp, r: r.ok and resp.get("code") == 0,
        fail_msg_keys=("message",),
    )


def push_telegram(bot_token: str, chat_id: str, title: str, content: str) -> bool:
    """Telegram Bot 推送"""
    if not bot_token or not chat_id:
        return False
    text = f"{title}\n\n{content}"
    if len(text) > TELEGRAM_MAX_LENGTH:
        text = text[:TELEGRAM_TRUNCATE_LENGTH] + "\n..."
    return _push_request(
        "Telegram",
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json_payload={"chat_id": chat_id, "text": text},
        success_check=lambda resp, r: r.ok and resp.get("ok"),
        fail_msg_keys=("description",),
    )


def push_pushplus(token: str, title: str, content: str) -> bool:
    """PushPlus 推送"""
    if not token:
        return False
    return _push_request(
        "PushPlus",
        "https://www.pushplus.plus/send",
        json_payload={"token": token, "title": title, "content": content, "template": "html"},
        success_check=lambda resp, r: r.ok and resp.get("code") == 200,
        fail_msg_keys=("msg",),
    )


def push_dingtalk(webhook_url: str, title: str, content: str) -> bool:
    """钉钉机器人推送（支持加签）"""
    if not webhook_url:
        return False
    secret = os.getenv("DINGTALK_SECRET", "")
    if secret:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        separator = "&" if "?" in webhook_url else "?"
        webhook_url = f"{webhook_url}{separator}timestamp={timestamp}&sign={sign}"
    else:
        # L6：webhook 已配置但 secret 缺失，加签机器人将鉴权失败，给出明确告警
        logger.warning(
            "DINGTALK_WEBHOOK 已配置，但 DINGTALK_SECRET 缺失："
            "将发送无签名请求（若机器人启用了加签校验会失败）"
        )

    return _push_request(
        "钉钉机器人",
        webhook_url,
        json_payload={
            "msgtype": "markdown",
            "markdown": {"title": _escape_markdown(title), "text": f"### {_escape_markdown(title)}\n\n{_escape_markdown(content)}"},
        },
        headers={"Content-Type": "application/json"},
        success_check=lambda resp, r: r.ok and resp.get("errcode") == 0,
        fail_msg_keys=("errmsg",),
    )


def push_feishu(webhook_url: str, title: str, content: str) -> bool:
    """飞书机器人推送（支持加签）"""
    if not webhook_url:
        return False
    data: Dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": _escape_markdown(title)},
                "template": "blue",
            },
            "elements": [{"tag": "markdown", "content": _escape_markdown(content)}],
        },
    }

    secret = os.getenv("FEISHU_SECRET", "")
    if secret:
        timestamp = str(round(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        # 飞书签名：以 string_to_sign 为 key，空字符串为 message
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")
        data["timestamp"] = timestamp
        data["sign"] = sign
    else:
        # L6：webhook 已配置但 secret 缺失，加签机器人将鉴权失败，给出明确告警
        logger.warning(
            "FEISHU_WEBHOOK 已配置，但 FEISHU_SECRET 缺失："
            "将发送无签名请求（若机器人启用了加签校验会失败）"
        )

    return _push_request(
        "飞书机器人",
        webhook_url,
        json_payload=data,
        headers={"Content-Type": "application/json"},
        success_check=lambda resp, r: r.ok and resp.get("code") == 0,
        fail_msg_keys=("msg",),
    )


def push_wecom_bot(webhook_url: str, title: str, content: str) -> bool:
    """企业微信机器人推送"""
    if not webhook_url:
        return False
    return _push_request(
        "企业微信机器人",
        webhook_url,
        json_payload={
            "msgtype": "markdown",
            "markdown": {"content": f"### {_escape_markdown(title)}\n\n{_escape_markdown(content)}"},
        },
        headers={"Content-Type": "application/json"},
        success_check=lambda resp, r: r.ok and resp.get("errcode") == 0,
        fail_msg_keys=("errmsg",),
    )


def push_yunhu(token: str, recv_id: str, title: str, content: str) -> bool:
    """云湖机器人推送"""
    if not token or not recv_id:
        return False
    recv_type = os.getenv("YUNHU_RECV_TYPE", "group")
    if recv_type not in ("group", "private"):
        logger.warning("YUNHU_RECV_TYPE 值 '%s' 非法，应为 'group' 或 'private'，使用默认值 'group'", recv_type)
        recv_type = "group"
    return _push_request(
        "云湖机器人",
        "https://chat-go.jwzhd.com/open-apis/v1/bot/send-message",
        json_payload={
            "token": token,
            "recvId": recv_id,
            "recvType": recv_type,
            "contentType": 1,
            "content": f"**{title}**\n\n{content}",
        },
        headers={"Content-Type": "application/json"},
        success_check=lambda resp, r: r.ok and resp.get("code") == 1,
        fail_msg_keys=("msg", "message"),
    )


# ==================== 推送渠道配置（L3：数据驱动，便于扩展/维护） ====================
# 每个条目: (渠道名, 触发所需的 env 变量列表, 推送调用闭包)
PUSH_CHANNELS: List[Tuple[str, List[str], Callable[[str, str], bool]]] = [
    ("PushDeer", ["SENDKEY"],
     lambda t, c: push_deer(os.getenv("SENDKEY", ""), t, c)),
    ("Server酱", ["SERVERCHAN_KEY"],
     lambda t, c: push_serverchan(os.getenv("SERVERCHAN_KEY", ""), t, c)),
    ("Telegram", ["TG_BOT_TOKEN", "TG_CHAT_ID"],
     lambda t, c: push_telegram(os.getenv("TG_BOT_TOKEN", ""), os.getenv("TG_CHAT_ID", ""), t, c)),
    ("PushPlus", ["PUSHPLUS_TOKEN"],
     lambda t, c: push_pushplus(os.getenv("PUSHPLUS_TOKEN", ""), t, c)),
    ("钉钉机器人", ["DINGTALK_WEBHOOK"],
     lambda t, c: push_dingtalk(os.getenv("DINGTALK_WEBHOOK", ""), t, c)),
    ("飞书机器人", ["FEISHU_WEBHOOK"],
     lambda t, c: push_feishu(os.getenv("FEISHU_WEBHOOK", ""), t, c)),
    ("企业微信机器人", ["WECOM_BOT_WEBHOOK"],
     lambda t, c: push_wecom_bot(os.getenv("WECOM_BOT_WEBHOOK", ""), t, c)),
    ("云湖机器人", ["YUNHU_TOKEN", "YUNHU_RECV_ID"],
     lambda t, c: push_yunhu(os.getenv("YUNHU_TOKEN", ""), os.getenv("YUNHU_RECV_ID", ""), t, c)),
]


def push_all(title: str, content: str) -> Tuple[int, int]:
    """
    推送到所有已配置的通知渠道。

    返回 (成功数, 已配置数)，供主流程区分"业务失败"与"通知发送失败"（L4）。
    """
    results: List[Tuple[str, bool]] = []
    for name, env_vars, fn in PUSH_CHANNELS:
        if all(os.getenv(v, "").strip() for v in env_vars):
            try:
                ok_push = fn(title, content)
            except Exception as e:  # noqa: BLE001
                logger.warning("%s 推送异常: %s", name, e)
                ok_push = False
            results.append((name, bool(ok_push)))

    configured = [n for n, _ in results]
    success = sum(1 for _, ok_push in results if ok_push)
    if not configured:
        logger.warning("未配置任何推送服务，请在 Secrets 中设置至少一种推送渠道")
    else:
        logger.info("已推送至: %s（成功 %d/%d）", ", ".join(configured), success, len(configured))
    return success, len(configured)


# ==================== 签到逻辑 ====================
def classify_checkin(code: Any, message: str) -> str:
    """
    判断签到结果: ok / repeat / fail
    GLaDOS API: code=0 成功, code=1 已签到, 其他失败
    """
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = -2
    if code == 0:
        return "ok"
    if code == 1:
        return "repeat"          # GLaDOS 契约：code==1 即已签到，无条件（H4 根治）
    msg = (message or "").lower()
    # 使用精确正则匹配代替宽泛的 "got" 子串检查（H3），兼容 point/points
    if re.search(r"got\s+\d+\s+points?", msg):
        return "ok"
    if any(kw in msg for kw in REPEAT_KEYWORDS):
        return "repeat"
    return "fail"


@retry_on_failure()
def checkin_request(session: requests.Session, headers: Dict[str, str]) -> Dict[str, Any]:
    """执行签到请求（带重试）"""
    r = session.post(CHECKIN_URL, headers=headers, json=PAYLOAD, timeout=TIMEOUT)
    r.raise_for_status()
    return require_json(r)  # 非 JSON 响应抛异常进入重试（M1）


@retry_on_failure()
def api_get(session: requests.Session, url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """查询账号状态/积分（带重试）"""
    r = session.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return require_json(r)  # 非 JSON 响应抛异常进入重试（M1）


def exchange_request(session: requests.Session, headers: Dict[str, str], plan: str) -> Dict[str, Any]:
    """
    执行积分兑换请求（#9 功能请求）。

    GLaDOS 兑换接口以表单形式提交 planType（plan100/plan200/plan500），
    响应 JSON 中 code==0 表示兑换成功。

    注意：故意不加 @retry_on_failure —— 兑换是消耗积分的非幂等 POST，
    若首次请求服务端已成功但响应丢失（读超时/连接重置），重试会导致重复扣积分。
    失败仅记警告、不影响签到结果与退出码，无需重试兜底。
    """
    r = session.post(EXCHANGE_URL, headers=headers, data={"planType": plan}, timeout=TIMEOUT)
    r.raise_for_status()
    return require_json(r)


def checkin_account(
    session: requests.Session,
    cookie: str,
    index: int,
    exchange_plan: Optional[str] = None,
) -> Dict[str, Any]:
    """执行单个账号的签到，返回账号信息字典

    exchange_plan: 积分兑换计划名（plan100/plan200/plan500），为 None 时不兑换。
    """
    session.cookies.clear()  # 清除上一个账号的残留 Cookie，避免串扰
    headers = {**HEADERS_BASE}
    headers["cookie"] = cookie

    email = "unknown"
    days = "-"
    total_points = "-"
    total_points_int = None  # 积分整数值，用于兑换阈值判断
    earned = 0
    status = ""
    result = "fail"
    exchange_status = "-"  # 兑换结果描述（未配置时保持 "-"，不输出到日志）

    try:
        # 1. 签到
        j = checkin_request(session, headers)
        code = j.get("code", -2)
        message = j.get("message", "")
        # H1：GLaDOS 不返回 points 字段，从 message 文本解析本次获得积分
        earned = parse_earned_points(message)
        result = classify_checkin(code, message)

        if result == "ok":
            status = f"✅ 成功 (+{earned}积分)"
        elif result == "repeat":
            status = "🔄 已签到"
        else:
            status = f"❌ 失败({message})"
            # Cookie 本身格式没问题、但服务端拒绝鉴权：与格式校验失败区分开，
            # 明确提示"Cookie 已失效/不完整"，避免用户误以为是脚本或前缀问题。
            msg_lower = (message or "").lower()
            if any(k in msg_lower for k in AUTH_FAIL_KEYWORDS):
                logger.error(
                    "账号 %d 服务端拒绝鉴权(%s)：Cookie 已过期或复制不完整，"
                    "请重新登录 https://glados.cloud 复制完整 Cookie（含 sess 与 sess.sig）后更新 COOKIES",
                    index, message,
                )

        # 2. 查询账号状态（剩余天数、邮箱）
        try:
            s = api_get(session, STATUS_URL, headers)
            data = s.get("data") or {}
            email = data.get("email", email)
            if data.get("leftDays") is not None:
                days = f"{safe_int_str(data['leftDays'])} 天"
        except Exception as e:  # noqa: BLE001
            logger.warning("账号 %d 状态查询失败: %s", index, e)

        # 3. 查询总积分（兼容顶层 points 与 data.points 两种返回结构，#1）
        try:
            p = api_get(session, POINTS_URL, headers)
            pts = p.get("points")
            if pts is None:
                pts = (p.get("data") or {}).get("points")
            if pts is not None:
                total_points = f"{safe_int_str(pts)} 积分"
                try:
                    total_points_int = int(float(pts))
                except (TypeError, ValueError):
                    total_points_int = None
        except requests.exceptions.HTTPError as e:
            resp = getattr(e, "response", None)
            status_code = getattr(resp, "status_code", "?") if resp is not None else "?"
            logger.warning("账号 %d 积分查询失败 (HTTP %s)", index, status_code)
        except Exception as e:  # noqa: BLE001
            logger.warning("账号 %d 积分查询失败: %s", index, e)

        # 4. 积分兑换（#9，仅配置了 EXCHANGE_PLAN 时执行；默认关闭不影响现有功能）
        #    兑换独立于签到结果，但仅在成功查到积分后尝试；失败不影响签到状态/退出码。
        if exchange_plan and exchange_plan in EXCHANGE_PLANS:
            req_pts = EXCHANGE_PLANS[exchange_plan]["points"]
            days_gain = EXCHANGE_PLANS[exchange_plan]["days"]
            if total_points_int is None:
                exchange_status = "⚠️ 兑换跳过(积分查询失败)"
                logger.warning("账号 %d 积分查询失败，跳过兑换", index)
            elif total_points_int < req_pts:
                exchange_status = f"⏭️ 积分不足({total_points_int}/{req_pts})"
                logger.info(
                    "账号 %d 积分 %d < %d，未达兑换阈值，跳过",
                    index, total_points_int, req_pts,
                )
            else:
                try:
                    ex = exchange_request(session, headers, exchange_plan)
                    ex_code = ex.get("code", -2)
                    ex_msg = ex.get("message", "")
                    if ex_code == 0:
                        exchange_status = f"🎁 兑换成功(+{days_gain}天)"
                        logger.info("账号 %d 积分兑换成功: %s", index, ex_msg)
                    else:
                        exchange_status = f"⚠️ 兑换失败({ex_msg})"
                        logger.warning("账号 %d 积分兑换失败: %s", index, ex_msg)
                except Exception as e:  # noqa: BLE001
                    exchange_status = f"⚠️ 兑换异常({type(e).__name__})"
                    logger.warning("账号 %d 积分兑换异常: %s", index, e)

    except Exception as e:  # noqa: BLE001
        logger.error("账号 %d 签到异常: %s", index, e)
        status = f"❌ 异常({type(e).__name__})"
        result = "fail"

    return {
        "index": index,
        "email": mask_email(email),
        "status": status,
        "result": result,
        "total_points": total_points,
        "remaining_days": days,
        "exchange": exchange_status,
    }


# ==================== 主流程 ====================
def main() -> int:
    # H2：支持 ||| 或换行(\n)或 & 分隔多账号 Cookie；推荐使用 ||| 避免与 Cookie 值冲突
    raw = os.getenv("COOKIES", "")
    # 归一化（剥离空白/引号/"Cookie:" 头名），再过滤空串
    cookies = [c for c in (normalize_cookie(x) for x in re.split(r"\|\|\||[&\n]", raw)) if c]

    # #9：积分兑换计划（可选，默认关闭；仅显式配置且值合法时启用，避免静默消耗积分）
    raw_plan = (os.getenv("EXCHANGE_PLAN") or os.getenv("GLADOS_EXCHANGE_PLAN") or "").strip()
    if raw_plan:
        if raw_plan in EXCHANGE_PLANS:
            exchange_plan = raw_plan
            logger.info(
                "已启用积分兑换计划: %s (%d 积分 → %d 天)",
                exchange_plan, EXCHANGE_PLANS[exchange_plan]["points"],
                EXCHANGE_PLANS[exchange_plan]["days"],
            )
        else:
            logger.warning(
                "EXCHANGE_PLAN 值 '%s' 无效，可选: %s；本次跳过兑换",
                raw_plan, "/".join(EXCHANGE_PLANS.keys()),
            )
            exchange_plan = None
    else:
        exchange_plan = None

    if not cookies:
        reason = (
            "❌ 未检测到 COOKIES（环境变量为空）\n"
            "GitHub Actions：仓库 Settings → Secrets and variables → Actions → "
            "New repository secret，名称填 COOKIES（填在 Secrets，不要只填在 Variables）\n"
            "本地运行：先设置环境变量 COOKIES 再执行脚本\n"
            f"{COOKIE_FORMAT_HINT}"
        )
        logger.error("%s", reason)
        push_all("GLaDOS 签到", reason)
        return 1  # L4：配置缺失视为失败，避免 CI 误标绿

    logger.info("检测到 %d 个账号", len(cookies))

    ok = fail = repeat = 0
    lines = []

    with requests.Session() as session:  # H1：使用上下文管理器确保连接释放
        for idx, cookie in enumerate(cookies, 1):
            # 验证 Cookie 格式，无效则跳过
            is_valid, error_msg = validate_cookie(cookie)
            if not is_valid:
                # 用 error 级别并完整输出期望格式，便于直接在 Actions 日志里定位问题
                logger.error("账号 %d Cookie 无效: %s", idx, error_msg)
                logger.error("账号 %d Cookie 片段(已脱敏): %s", idx, mask_cookie(cookie))
                fail += 1
                lines.append(f"{idx}. [无效Cookie] | ❌ 失败({error_msg}) | 总积分:- | 剩余:-")
                if idx < len(cookies):
                    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                continue

            # 输出实际解析到的会话前缀，koa: / gld: 均可，便于排查"填错前缀"类问题
            logger.info(
                "正在处理账号 %d/%d... (Cookie 前缀: %s)",
                idx, len(cookies), detect_session_prefix(cookie) or "未知",
            )
            acc = checkin_account(session, cookie, idx, exchange_plan)

            if acc["result"] == "ok":
                ok += 1
            elif acc["result"] == "repeat":
                repeat += 1
            else:
                fail += 1

            line = (
                f"{acc['index']}. {acc['email']} | {acc['status']} | "
                f"总积分:{acc['total_points']} | 剩余:{acc['remaining_days']}"
            )
            # 仅当配置了兑换且产生实际兑换结果时追加，未配置时日志行格式保持不变
            if acc.get("exchange") and acc["exchange"] != "-":
                line += f" | 兑换:{acc['exchange']}"
            lines.append(line)

            # 非最后一个账号时随机延迟
            if idx < len(cookies):
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    title = f"GLaDOS 签到完成 ✅{ok} ❌{fail} 🔄{repeat}"
    content = "\n".join(lines)

    # #4：汇总内容过长时统一截断，避免部分推送渠道因超限静默失败
    if len(content) > CONTENT_MAX_LENGTH:
        content = content[:CONTENT_MAX_LENGTH] + "\n…(内容过长已截断)"

    logger.info("%s", "=" * 50)
    logger.info("%s", content)
    logger.info("%s", "=" * 50)

    pushed_success, pushed_configured = push_all(title, content)

    # L4：区分"业务失败"与"通知发送失败"，必要时非零退出避免误判成功
    if ok == 0 and repeat == 0 and len(cookies) > 0:
        # 业务全部失败：无论通知是否成功，均判运行失败
        logger.error("⚠️ 全部 %d 个账号签到失败", len(cookies))
        if pushed_configured > 0 and pushed_success == 0:
            logger.error("⚠️ 且已配置推送渠道但全部发送失败，无人收到通知")
        return 1
    # 业务存在成功/已签到：即便通知全部失败也视为运行成功，避免误报红
    if pushed_configured > 0 and pushed_success == 0:
        logger.warning("⚠️ 已配置推送渠道但全部发送失败，无人收到通知（不影响运行结果）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

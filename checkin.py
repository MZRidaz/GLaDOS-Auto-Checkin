import os
import json
import time
import random
import requests
from pypushdeer import PushDeer


CHECKIN_URL = "https://glados.cloud/api/user/checkin"
STATUS_URL = "https://glados.cloud/api/user/status"

HEADERS_BASE = {
    "origin": "https://glados.cloud",
    "referer": "https://glados.cloud/console/checkin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "content-type": "application/json;charset=UTF-8",
}

PAYLOAD = {"token": "glados.cloud"}
TIMEOUT = 10


def push_deer(sckey: str, title: str, text: str):
    """推送消息到 PushDeer"""
    if sckey:
        PushDeer(pushkey=sckey).send_text(title, desp=text)


def push_serverchan(sendkey: str, title: str, content: str):
    """推送消息到 Server 酱 (Turbo 版)"""
    if not sendkey:
        return

    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    data = {"title": title, "desp": content}

    try:
        resp = requests.post(url, data=data, timeout=TIMEOUT)
        if resp.status_code == 200:
            result = resp.json()
            if result.get("code") == 0:
                print("✅ Server 酱推送成功")
            else:
                print(f"⚠️ Server 酱推送失败: {result.get('message')}")
        else:
            print(f"⚠️ Server 酱推送失败: HTTP {resp.status_code}")
    except Exception as e:
        print(f"⚠️ Server 酱推送异常: {e}")


def push_telegram(bot_token: str, chat_id: str, title: str, content: str):
    """推送消息到 Telegram Bot"""
    if not bot_token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    text = f"{title}\n\n{content}"

    # Telegram 单条消息上限 4096 字符，做截断避免发送失败。
    if len(text) > 4000:
        text = text[:3990] + "..."

    data = {"chat_id": chat_id, "text": text}

    try:
        resp = requests.post(url, json=data, timeout=TIMEOUT)
        if resp.status_code == 200 and safe_json(resp).get("ok"):
            print("✅ Telegram 推送成功")
        else:
            print(f"⚠️ Telegram 推送失败: HTTP {resp.status_code} | {resp.text}")
    except Exception as e:
        print(f"⚠️ Telegram 推送异常: {e}")


def push_all(sendkey_deer: str, sendkey_sc: str, bot_token: str, chat_id: str, title: str, content: str):
    """推送到所有配置的服务"""
    pushed = False

    if sendkey_deer:
        push_deer(sendkey_deer, title, content)
        pushed = True

    if sendkey_sc:
        push_serverchan(sendkey_sc, title, content)
        pushed = True

    if bot_token and chat_id:
        push_telegram(bot_token, chat_id, title, content)
        pushed = True

    if not pushed:
        print("⚠️ 未配置任何推送服务，请在 Secrets 中配置 SENDKEY、SERVERCHAN_KEY 或 TG_BOT_TOKEN+TG_CHAT_ID")


def safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return {}


def main():
    # 获取推送密钥
    sendkey_deer = os.getenv("SENDKEY", "")
    sendkey_sc = os.getenv("SERVERCHAN_KEY", "")
    bot_token = os.getenv("TG_BOT_TOKEN", "")
    chat_id = os.getenv("TG_CHAT_ID", "")
    cookies_env = os.getenv("COOKIES", "")
    cookies = [c.strip() for c in cookies_env.split("&") if c.strip()]

    if not cookies:
        push_all(sendkey_deer, sendkey_sc, bot_token, chat_id, "GLaDOS 签到", "❌ 未检测到 COOKIES")
        return

    session = requests.Session()
    ok = fail = repeat = 0
    lines = []

    for idx, cookie in enumerate(cookies, 1):
        # 每个账号清空上一个账号留下的 Session Cookie，避免 Cookie 污染
        session.cookies.clear()

        headers = dict(HEADERS_BASE)
        headers["cookie"] = cookie

        email = "unknown"
        points = "-"
        days = "-"

        try:
            r = session.post(
                CHECKIN_URL,
                headers=headers,
                data=json.dumps(PAYLOAD),
                timeout=TIMEOUT,
            )

            j = safe_json(r)
            msg = j.get("message", "")
            msg_lower = msg.lower()

            if "got" in msg_lower:
                ok += 1
                points = j.get("points", "-")
                status = "✅ 成功"
            elif "repeat" in msg_lower or "already" in msg_lower:
                repeat += 1
                status = "🔁 已签到"
            else:
                fail += 1
                status = "❌ 失败"

            # 状态接口（允许失败）
            s = session.get(STATUS_URL, headers=headers, timeout=TIMEOUT)
            sj = safe_json(s).get("data") or {}
            email = sj.get("email", email)
            try:
                if sj.get("leftDays") is not None:
                    days = f"{int(float(sj['leftDays']))} 天"
            except (ValueError, TypeError):
                pass

        except Exception as e:
            fail += 1
            status = "❌ 异常"
            print(f"⚠️ 账号 {idx} 异常: {e}")

        lines.append(f"{idx}. {email} | {status} | P:{points} | 剩余:{days}")
        if idx < len(cookies):
            time.sleep(random.uniform(1, 2))

    title = f"GLaDOS 签到完成 ✅{ok} ❌{fail} 🔁{repeat}"
    content = "\n".join(lines)

    print(content)
    
    # 推送消息到所有服务
    push_all(sendkey_deer, sendkey_sc, bot_token, chat_id, title, content)


if __name__ == "__main__":
    main()
import os
import json
import time
import random
import requests
from pypushdeer import PushDeer
from urllib.parse import quote


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
    
    # Server 酱 Turbo 版 API
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    
    data = {
        "title": title,
        "desp": content
    }
    
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


def push_all(sendkey_deer: str, sendkey_sc: str, title: str, content: str):
    """推送到所有配置的服务"""
    # PushDeer 推送
    if sendkey_deer:
        push_deer(sendkey_deer, title, content)
    
    # Server 酱推送
    if sendkey_sc:
        push_serverchan(sendkey_sc, title, content)
    
    # 如果都没有配置，打印提醒
    if not sendkey_deer and not sendkey_sc:
        print("⚠️ 未配置任何推送服务，请在 Secrets 中配置 SENDKEY 或 SERVERCHAN_KEY")


def safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return {}


def main():
    # 获取推送密钥
    sendkey_deer = os.getenv("SENDKEY", "")
    sendkey_sc = os.getenv("SERVERCHAN_KEY", "")
    cookies_env = os.getenv("COOKIES", "")
    cookies = [c.strip() for c in cookies_env.split("&") if c.strip()]

    if not cookies:
        push_all(sendkey_deer, sendkey_sc, "GLaDOS 签到", "❌ 未检测到 COOKIES")
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
    push_all(sendkey_deer, sendkey_sc, title, content)


if __name__ == "__main__":
    main()
# 📌 GLaDOS 自动签到

一个基于 **GitHub Actions** 的 **GLaDOS 自动签到脚本**。

**无需服务器、无需编程基础**，每天自动帮你签到。

---

## ✨ 功能特性

- ✅ 每天自动签到，已签到自动识别
- 👥 支持多账号（`|||`、`&` 或换行连接）
- 📊 查询总积分和剩余天数
- 📬 8 种推送渠道：PushDeer / Server酱 / Telegram / PushPlus / 钉钉 / 飞书 / 企业微信 / 云湖
- 🔄 网络请求自动重试（指数退避）
- 🔒 日志脱敏（邮箱/Cookie 自动隐藏）
- ✅ Cookie 格式预验证
- 🔧 每月自动空提交保活
- 💎 积分自动兑换（可选，消耗积分兑换会员天数）
- 🆓 完全免费

---

## 📂 项目结构

```
.
├── checkin.py                 # 签到脚本
└── .github/workflows/
    └── glados.yml             # GitHub Actions 配置
```

---

## 🚀 使用教程

### 第一步：Fork 本项目

点击右上角 **Fork**，Fork 到你自己的 GitHub 账号下。

---

### 第二步：获取 GLaDOS Cookie

1. 打开浏览器，登录 https://glados.cloud
2. 按 **F12** 打开开发者工具
3. 找到 `Application` → `Cookies` → `glados.cloud`
4. 复制完整 Cookie 内容

示例：
```
koa:sess=xxxxxx; koa:sess.sig=yyyyyy
```

⚠️ **必须是完整的一整段**

---

### 第三步：添加 GitHub Secrets

进入你 Fork 后的仓库：

1. **Settings** → **Secrets and variables** → **Actions**
2. 点击 **New repository secret**
3. 添加：
   - **Name**：`COOKIES`
   - **Value**：粘贴刚才复制的 Cookie
4. 点击 **Save**

---

### 第四步：（可选）配置推送

在 GitHub Secrets 中添加对应的环境变量：

| 渠道 | 必填环境变量 | 可选 |
|------|-------------|------|
| PushDeer | `SENDKEY` | - |
| Server酱 | `SERVERCHAN_KEY` | - |
| Telegram | `TG_BOT_TOKEN` + `TG_CHAT_ID` | - |
| PushPlus | `PUSHPLUS_TOKEN` | - |
| 钉钉机器人 | `DINGTALK_WEBHOOK` | `DINGTALK_SECRET` |
| 飞书机器人 | `FEISHU_WEBHOOK` | `FEISHU_SECRET` |
| 企业微信机器人 | `WECOM_BOT_WEBHOOK` | - |
| 云湖机器人 | `YUNHU_TOKEN` + `YUNHU_RECV_ID` | `YUNHU_RECV_TYPE` |

> 🔑 **钉钉 / 飞书加签说明**：若机器人启用了「加签」校验，则 `DINGTALK_WEBHOOK` + `DINGTALK_SECRET`（或 `FEISHU_WEBHOOK` + `FEISHU_SECRET`）**必须同时配置**。只配 webhook 不配 secret 时，脚本会发送无签名请求并给出告警，加签机器人将鉴权失败。

---

### 第五步：（可选）积分自动兑换

在 GitHub Secrets 中添加 `EXCHANGE_PLAN`（或 `GLADOS_EXCHANGE_PLAN`，二选一）即可启用自动兑换积分功能。**不配置则默认不兑换**，不影响任何现有签到逻辑。

| 配置值 | 消耗积分 | 兑换天数 |
|--------|---------|---------|
| `plan100` | 100 积分 | 10 天 |
| `plan200` | 200 积分 | 30 天 |
| `plan500` | 500 积分 | 100 天 |

机制说明：

- 每次签到后，仅在**总积分 ≥ 计划所需积分**时才调用兑换接口，否则自动跳过（不会浪费积分）。
- 兑换结果会附加到签到日志行末尾，例如：`| 兑换:🎁 兑换成功(+30天)`。
- 兑换失败 / 异常**不影响**签到结果与运行退出码。

---

## 👥 多账号配置

多个账号的 Cookie 用 `|||`、`&` 或**换行**连接（三种分隔符均可混用，推荐使用 `|||` 以避免与 Cookie 值冲突）：

```
cookie_账号1 ||| cookie_账号2 ||| cookie_账号3
```

或

```
cookie_账号1
cookie_账号2
cookie_账号3
```

⚠️ Cookie 值本身不得包含 `|||`、`&` 或换行符，否则会被错误拆分。推荐使用 `|||` 作为分隔符，因为 Cookie 值中几乎不可能出现该字符串。

---

## ⏰ 签到时间

每天 **UTC 04:00**（北京时间 **中午 12 点**）自动运行。

---

## 📋 签到结果

| 状态 | 说明 |
|------|------|
| ✅ 成功 | 签到成功，显示获得积分 |
| 🔄 已签到 | 今日已签到过 |
| ❌ 失败 | 签到失败，显示原因 |

---

## ❓ 常见问题

**Q: 签到提示 Cookie 失效？**

A: Cookie 有有效期，请重新登录获取最新 Cookie 并更新 Secrets。

**Q: Actions 被暂停了？**

A: 项目内置每月空提交保活。如仍被暂停，手动触发一次 `workflow_dispatch`。

**Q: 日志中的邮箱为什么显示不完整？**

A: 出于隐私保护，邮箱会自动脱敏（如 `te***t@example.com`）。

**Q: 可以同时配置多个推送渠道吗？**

A: 可以，配置多个 Secrets 即可同时推送。

---

## 🔄 更新日志

### v2.0.0

**功能新增**
- 新增 Telegram Bot 推送
- 新增 PushPlus（推送加）推送
- 新增钉钉机器人推送（支持加签验证）
- 新增飞书机器人推送（支持加签验证）
- 新增企业微信机器人推送
- 新增云湖机器人推送
- 新增总积分查询功能
- 新增网络请求自动重试机制（指数退避）
- 新增 Cookie 格式预验证
- 新增日志脱敏处理（邮箱/Cookie 自动隐藏）
- 新增每月自动空提交保活机制

**问题修复**
- 修复 PushPlus 推送域名问题
- 修复飞书机器人加签算法

**优化改进**
- Python 版本升级至 3.11
- 多账号间请求增加随机延迟
- GitHub Actions 添加超时和并发控制
- 代码整合为单文件，简化部署

---

## 🔄 更新日志

### v2.1.0

**功能新增**
- 新增积分自动兑换功能（#9，可选配置 `EXCHANGE_PLAN` / `GLADOS_EXCHANGE_PLAN`，支持 plan100/plan200/plan500 三档策略；默认关闭，不影响现有签到）

---

### v2.0.0

MIT License

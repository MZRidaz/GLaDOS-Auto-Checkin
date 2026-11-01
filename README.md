# 📌 GLaDOS 自动签到

一个基于 **GitHub Actions** 的 **GLaDOS 自动签到脚本**。

**无需服务器、无需编程基础**，每天自动帮你签到。

---

## ✨ 功能特性

- ✅ 每天自动签到，已签到自动识别
- 📧 统一 Gmail 收件箱 + IMAP 应用专用密码：多账号 Cookie 失效后自动读取对应验证码重新登录
- 🔐 新 Cookie 按邮箱隔离，用 Fernet 加密后缓存，下一次 Actions 继续使用，无需额外 GitHub PAT
- 👥 支持多账号：邮箱模式每行一个邮箱；Cookie 模式用 `|||`、`&` 或换行连接
- 📊 查询总积分和剩余天数
- 📬 8 种推送渠道：PushDeer / Server酱 / Telegram / PushPlus / 钉钉 / 飞书 / 企业微信 / 云湖
- 🔄 网络请求自动重试（指数退避）
- 🔒 日志脱敏（邮箱/Cookie 自动隐藏）
- ✅ Cookie 结构预验证（前缀无关，自动兼容 `koa:` / `gld:` 等任意前缀）
- 🔧 每月自动空提交保活
- 💎 积分自动兑换（可选，消耗积分兑换会员天数）
- 🆓 完全免费

---

## 📂 项目结构

```
.
├── checkin.py                 # 签到脚本
├── glados_auth.py             # 多账号邮箱登录、只读 IMAP、加密 Cookie 存储
├── requirements.txt           # 运行依赖
├── tests/                     # 离线测试，不访问真实邮箱或 GLaDOS
└── .github/workflows/
    └── glados.yml             # GitHub Actions 配置
```

---

## 🚀 使用教程

先将项目 Fork 到自己的 GitHub 仓库并启用 Actions，再选择以下一种认证方式。

支持两种配置方式：推荐使用下面的**邮箱自动恢复模式**；原有 `COOKIES` 配置继续可用，配置方法在后文。配置 `GLADOS_EMAILS` 后优先使用邮箱模式，旧 `COOKIES` 不会按顺序绑定到邮箱，避免串号。

### 认证方式的优先级与切换

| 配置情况 | 实际使用方式 |
|----------|--------------|
| `GLADOS_EMAILS` 非空，即使同时配置了 `COOKIES` | 使用邮箱自动恢复模式，忽略 `COOKIES` |
| 未配置或清空 `GLADOS_EMAILS`，且配置了 `COOKIES` | 使用手动 Cookie 模式 |
| 两者均未配置 | 报配置缺失错误，无法签到 |

邮箱模式先读取并验证每个账号自己的缓存 Cookie，**有效时直接复用，只有缓存缺失或明确鉴权失败时才发送验证码重新登录**。邮箱配置错误或登录恢复失败时会报错，不会自动退回 `COOKIES`。公共配置有效时，某个账号恢复失败不影响后续账号独立处理。

- **切换到邮箱模式**：配置下文四项必填 Secrets。原 `COOKIES` 可以删除，也可以保留；保留时不会参与邮箱模式，首次没有缓存仍需通过邮件登录。
- **切换回手动 Cookie 模式**：删除或清空 `GLADOS_EMAILS`，再设置有效的 `COOKIES`。其他 Gmail 配置及加密缓存无需删除，手动模式不会使用它们。Cookie 失效后需要自行重新获取并更新 `COOKIES`。

GitHub Actions 中请在仓库 **Settings → Secrets and variables → Actions** 修改上述配置；本地运行则修改当前终端中的同名环境变量。

### 推荐：统一 Gmail 收件箱，自动恢复多个账号

GLaDOS 的登录邮箱保持不变，只把各账号的验证码邮件集中转发到一个 Gmail。脚本逐个处理账号：读取自己的缓存 → 验证登录邮箱 → 签到；Cookie 缺失或鉴权失败时才请求验证码、读取对应新邮件、重新登录并保存新 Cookie。首次运行或缓存被删除时，会分别登录所有账号。

#### 1. 设置 Gmail 转发

为每个 GLaDOS 原登录邮箱设置一次：

1. 打开 Gmail 网页版 → **设置 → 查看所有设置 → 转发和 POP/IMAP → 添加转发地址**，填写统一收件箱。
2. 在统一收件箱确认 Google 的转发验证邮件。
3. 回到原邮箱，选择一封真实的 GLaDOS 验证邮件 → **过滤此类邮件**，按实际发件人和主题建立过滤器，勾选转发到统一收件箱。
4. 仅转发验证码邮件，全局“转发所有邮件”保持关闭。若统一收件箱本身也是一个 GLaDOS 登录邮箱，它自己的邮件无需转发。
5. 检查新验证码邮件确实到达统一收件箱，且“显示原始邮件”中的 `To`、`X-Original-To` 或 `X-Gm-Original-To` 保留原登录邮箱。脚本不会把转发后的 `Delivered-To` 当作原账号，也不会根据“最新一封邮件”猜测账号。

过滤器只转发以后的新邮件。Gmail 收件箱保持邮件不归档，验证码需要出现在统一邮箱的 `INBOX` 中。[Google 转发配置说明](https://support.google.com/mail/answer/10957?hl=zh-Hans)

#### 2. 给统一收件 Gmail 创建应用专用密码

1. 登录统一收件 Gmail 对应的 [Google 账号安全设置](https://myaccount.google.com/security)，开启**两步验证**。
2. 打开[应用专用密码](https://myaccount.google.com/apppasswords)，创建名为 `GLaDOS Actions` 的应用密码。
3. 将生成的应用密码保存到下面的 `GMAIL_APP_PASSWORD`，不要使用日常登录 Google 的密码；复制时的分组空格会自动去除。

个人 Gmail 默认开启 IMAP，无需再找“启用 IMAP”开关。脚本通过 `imap.gmail.com:993` 的 SSL/TLS 连接读取邮件，使用只读模式和 `BODY.PEEK`，不删除、不标记已读。若应用密码入口不可用，请检查两步验证方式、组织策略或高级保护设置；Google 主密码修改后需要重新生成应用密码。[Google 应用密码说明](https://support.google.com/accounts/answer/185833?hl=zh-Hans)

#### 3. 添加 GitHub Secrets

仓库 → **Settings → Secrets and variables → Actions → Secrets → New repository secret**，添加以下四项：

| Secret | 填写内容 |
|--------|---------|
| `GLADOS_EMAILS` | 所有 GLaDOS **原登录邮箱**，每行一个；也支持逗号分隔。用于指定要签到的账号 |
| `GMAIL_ADDRESS` | 统一收件箱的完整 Gmail 地址，可使用现有账号；它不是密码或特殊授权地址 |
| `GMAIL_APP_PASSWORD` | 由 `GMAIL_ADDRESS` 对应的 Google 账号生成的应用专用密码；其他账号只需转发，无需分别提供密码 |
| `COOKIE_STORE_KEY` | 下一步生成的固定加密密钥，与 Gmail 密码无关，用于保护缓存中的 Cookie |

`GLADOS_EMAILS` 示例（不要填 Cookie）：

```text
account-a@gmail.com
account-b@gmail.com
account-c@gmail.com
```

邮箱大小写和重复行会归一化；不会合并 Gmail 的点号或 `+` 别名。增加账号只需增加该账号的转发规则和此列表，已有账号的 Cookie 仍按邮箱查找，与顺序无关。

在本地安装依赖并生成一次加密密钥：

```bash
python -m pip install -r requirements.txt
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

将输出保存到 `COOKIE_STORE_KEY`，不要提交到仓库，也不要每次运行重新生成。Actions 缓存 `.glados/cookies.enc` **只有密文**；缓存并非永久存储，删除、回收、分支切换导致未命中，或更换密钥后，脚本会重新进行邮箱登录。无需授予仓库 Secrets 写权限，也无需配置 PAT。

可选配置：

| 名称 | 位置 | 默认值 / 说明 |
|------|------|---------------|
| `GMAIL_FROM` | Secret | 默认接受 `glados.network`、`glados.cloud`、`glados.one` 域及其子域；若真实验证码发件人不同，填写邮件中的**实际完整发件地址**，多个可逗号分隔 |
| `GMAIL_WAIT_SECONDS` | Variable | 默认 `120` 秒，每个账号等待验证码的上限；可设 `10` 到 `600` |

脚本在请求验证码前记录 IMAP UID 边界，只接受之后新到达、原收件邮箱匹配、发件人受允许且日期接近请求时间的六位验证码。请先确认实际邮件的发件地址；当多个不同验证码同时出现时会拒绝猜测。

#### 4. 首次运行与故障处理

在 **Actions → GLaDOS Auto Checkin → Run workflow** 手动运行一次。观察每个账号的结果与汇总推送；之后按既有定时任务自动运行。单个账号失败不会阻断后面的账号，全部失败时 Actions 返回失败。推送和积分兑换仍按原有 Secrets 配置，未配置兑换计划时不兑换。

- **等待验证码超时**：核对转发过滤器、实际发件人与 `GMAIL_FROM`、收件箱/垃圾邮件位置以及原始收件人头；必要时增加等待时间。不要转发旧验证码代替新邮件。
- **Gmail IMAP 认证失败**：检查统一邮箱、两步验证和应用专用密码。脚本不会反复发送验证码来掩盖邮箱连接故障。
- **GLaDOS 要求人机验证 / 验证码发送限流**：本次账号停止并通知；需要人工处理后重试，不绕过验证码挑战，不循环发码。
- **登录账号身份不匹配**：不执行该账号后续签到/兑换，不保存错配会话，检查转发或配置。

每轮每个账号最多执行一次邮箱登录；有效 Cookie 不发验证码，普通网络错误不触发重新登录。日志和推送不输出应用密码、验证码或新 Cookie。邮箱登录仍依赖 GLaDOS 当前接口，无法保证网站改变机制或要求人工验证时完全无人值守。

本地运行同样设置以上环境变量后执行 `python checkin.py`，密文默认保存在 `.glados/cookies.enc`（已加入 `.gitignore`）。本地可通过 `COOKIE_STORE_FILE` 更改路径；Actions 使用固定缓存路径，无需设置它。

#### 5. 在本地 PowerShell 测试

在项目目录安装依赖，在**同一个 PowerShell 窗口**设置变量并运行，以下地址均为示例：

```powershell
python -m pip install -r requirements.txt

$env:GLADOS_EMAILS = "account-a@gmail.com,account-b@gmail.com"
$env:GMAIL_ADDRESS = "collector@gmail.com"
$env:GMAIL_APP_PASSWORD = [System.Net.NetworkCredential]::new("", (Read-Host "请输入 Gmail 应用专用密码" -AsSecureString)).Password
$env:COOKIE_STORE_KEY = [System.Net.NetworkCredential]::new("", (Read-Host "请输入固定 Cookie 加密密钥" -AsSecureString)).Password

# 首次测试不兑换积分
$env:EXCHANGE_PLAN = ""
$env:GLADOS_EXCHANGE_PLAN = ""
python checkin.py
```

`Read-Host` 引号内是提示文字，不是配置值；运行后出现冒号时，再粘贴真实值并按回车，输入内容不会显示。此命令会实际登录和签到。保持同一密钥与缓存再次运行，正常应看到“Cookie 缓存有效，直接复用，不发送邮件验证码”。首次运行每个账号需要一封新验证码，缓存有效时不发码。

本地 `$env:` 配置只影响该 PowerShell 及其启动的程序，关闭终端后需要重新设置；GitHub Secrets 不会同步到本地。脚本不自动读取 `.env`。请勿把密码、密钥、Cookie、邮件正文或抓包文件提交到 Git；应用密码与加密密钥只通过隐藏输入或 GitHub Secrets 配置。

如果连续运行仍收到验证码，先在配置好环境变量的**同一个终端**执行只读诊断：

```bash
python checkin.py --check-session
```

它只查询已有 Cookie 的登录状态，不发送验证码、不签到、不兑换，也不修改缓存。日志会显示缓存文件路径、是否成功解密，以及各账号缓存是缺失、有效还是被服务器拒绝。正常运行也会明确输出“Cookie 缓存有效，直接复用，不发送邮件验证码”或重新登录的原因。不要每次重新生成 `COOKIE_STORE_KEY`，也不要删除 `.glados`；切换运行目录时需确保使用同一缓存路径。

### 原有方式：手动配置 Cookie

#### 1. 获取 GLaDOS Cookie

1. 打开浏览器，登录 https://glados.cloud
2. 按 **F12** 打开开发者工具
3. 找到 `Application` → `Cookies` → `glados.cloud`
4. 复制完整 Cookie 内容

示例（官网当前签发的是 `gld:` 前缀）：
```
gld:sess=xxxxxx; gld:sess.sig=yyyyyy
```

脚本也兼容旧版 `koa:sess=xxxxxx; koa:sess.sig=yyyyyy` 的字段结构，但能否登录取决于服务器是否仍接受该会话。旧 Cookie 被撤销时需要重新获取，不能靠改前缀恢复。**使用服务器实际签发的前缀，无需手动转换。**

⚠️ **必须是完整的一整段**，且 `sess` 与 `sess.sig` 两个字段**必须同时存在、前缀一致**。
只复制其中一个会导致签到失败（脚本会明确指出缺少哪个字段）。

> 💡 脚本采用**结构校验**：只校验「`<前缀>:sess` 与同前缀 `:sess.sig` 成对」，
> 不写死具体前缀。因此 GLaDOS 即便再次变更前缀名，脚本也能自动识别，无需修改代码。
> **不要把 `gld:` 改成 `koa:`** —— 改前缀会让服务端无法识别 session，导致「没有权限」。

---

#### 2. 添加 GitHub Secrets

进入你 Fork 后的仓库：

1. **Settings** → **Secrets and variables** → **Actions** → **Secrets** 标签页
2. 点击 **New repository secret**
3. 添加：
   - **Name**：`COOKIES`
   - **Value**：粘贴刚才复制的 Cookie
4. 点击 **Save**

建议填入 **Secrets** 保护凭据。工作流也兼容 `vars.COOKIES`，但 Variables 不是敏感凭据存储；同名 Secret 非空时优先读取 Secret。变量名必须是 `COOKIES`，不能有多余空格。邮箱模式不需要配置此项。

---

## 📬 可选：配置推送（两种模式通用）

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

## 💎 可选：积分自动兑换（两种模式通用）

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

**邮箱模式**：`GLADOS_EMAILS` 每行一个原登录邮箱或用逗号分隔；每个账号单独保存 Cookie，列表重新排序不会串号。旧 `COOKIES` 不参与邮箱模式。

**手动 Cookie 模式**：不配置 `GLADOS_EMAILS`，使用下面的 `COOKIES` 配置。

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

每天 **UTC 01:17**（北京时间 **09:17**）定时触发，Actions 实际启动时间可能延迟。

---

## 📋 签到结果

| 状态 | 说明 |
|------|------|
| ✅ 成功 | 签到成功，显示获得积分 |
| 🔄 已签到 | 今日已签到过 |
| ❌ 失败 | 签到失败，显示原因 |

---

## ❓ 常见问题

**Q: 签到提示「没有权限」/ 鉴权失败？**

A: 邮箱模式会在明确鉴权失败时按账号自动重登；如果恢复失败，检查 Gmail 转发、应用密码及人机验证提示。手动 Cookie 模式需要重新登录 GLaDOS 并更新 `COOKIES`。若日志提示「会话字段不成对」或输出「实际键名」，则检查是否遗漏 `sess` / `sess.sig`。

> 💡 脚本会自动识别 `koa:` / `gld:` 等任意前缀，**无需手动修改前缀**；反之，手动把前缀改错（如把 `gld:` 改成 `koa:`）会让服务端无法识别 session。

**Q: Cookie 有有效期吗？**

A: 有，也可能在到期前被服务器撤销。邮箱自动恢复模式会按账号重新登录；手动 Cookie 模式需要自行更新 Secrets。

**Q: Actions 被暂停了？**

A: 项目内置每月空提交保活。如仍被暂停，手动触发一次 `workflow_dispatch`。

**Q: 日志中的邮箱为什么显示不完整？**

A: 出于隐私保护，邮箱会自动脱敏（如 `te***t@example.com`）。

**Q: 可以同时配置多个推送渠道吗？**

A: 可以，配置多个 Secrets 即可同时推送。

---

## 🔄 更新日志

### v2.2.0

- 新增统一 Gmail IMAP 收件、多账号验证码登录与失效恢复，保留旧 `COOKIES` 模式。
- 按邮箱隔离会话并校验登录身份；通过 UID、请求时间、原收件人与发件人过滤验证码。
- Cookie 加密缓存跨 Actions 运行复用，不需要 PAT；部分账号失败后仍保存其他账号的有效会话。
- 增加离线测试；Actions 启动签到前自动运行测试。

本地验证命令：`python -m unittest discover -s tests -v`。测试使用模拟 HTTP/IMAP，不发验证码、不访问真实账号，也不消耗积分。

### v2.1.1

**问题修复**
- 修复 GLaDOS 变更 Cookie 前缀（`koa:sess` → `gld:sess`）导致的签到失败：`validate_cookie` 原先把 `koa:sess` 写死校验，新版 Cookie 被误判为「缺少必要字段」，签到请求根本未发出，表现为「❌ 失败(没有权限)」

**优化改进**
- Cookie 校验改为**前缀无关的结构校验**：只校验「`<任意前缀>:sess` 与同前缀 `:sess.sig` 成对」，不再枚举任何前缀名——GLaDOS 未来再次变更前缀通常无需改代码即可自动适配
- 新增 `normalize_cookie`：自动剥离粘贴时混入的首尾引号、空白与 `Cookie:` 头名
- 新增自诊断日志：校验成功时打印识别到的会话前缀；失败时输出**实际解析到的键名**，前缀变化可一眼定位
- 服务端鉴权失败（没有权限 / 未登录等）与格式错误分开提示，明确引导「重新获取 Cookie」
- `glados.yml`：`COOKIES` 支持 `secrets` 与 `vars` 双路读取

---

### v2.1.0

**功能新增**
- 新增积分自动兑换功能（#9，可选配置 `EXCHANGE_PLAN` / `GLADOS_EXCHANGE_PLAN`，支持 plan100/plan200/plan500 三档策略；默认关闭，不影响现有签到）
- 兑换请求单次尝试不重试（非幂等操作，避免响应丢失后重复扣积分）

---

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

## 📄 许可证

MIT License

# 科研通（AbleSci）每日自动签到

一个**零第三方依赖**的 Python 脚本，配合 GitHub Actions 每天自动签到领积分。不碰你现有的工作区，全部文件都在本目录。

> 仅供个人学习使用，与科研通官方无关。网站接口或风控策略随时可能变化，脚本失效属正常情况；请勿用于批量注册、刷分或任何违反站点条款的用途。

---

## 1. 它能做什么

| 特性 | 说明 |
| --- | --- |
| 两种登录都支持 | 每个账号**优先用浏览器 Cookie**；Cookie 失效时**自动降级**为邮箱+密码登录（自动取 CSRF）。也可只用其中一种 |
| 幂等 | 「今日已签到」会直接判定为成功，不会重复请求；一天跑两次也只加一次分 |
| 多账号 | `accounts.json` 里配几个就跑几个，逐个独立执行 |
| 失败重试 | 网络抖动 / 5xx / 429 自动重试 3 次；业务性失败（如接口改版）不白重试 |
| 结果可见 | 终端日志 + GitHub Actions Job Summary 表格，显示本次积分、当前积分、连续天数 |
| 可选推送 | Server酱 / PushPlus / 自定义 Webhook（飞书·企微·钉钉机器人），默认不推送；未配置密钥时零外联 |
| 隐私 | 密码与 Cookie 不进日志、不进 Summary；Actions 里额外注册 `::add-mask::`；`accounts.json` / `.env` / `cookies.json` 已被 `.gitignore` 排除 |

脚本**只请求 `www.ablesci.com`**（以及你主动配置的推送地址）。

---

## 2. 文件清单

```
ablesci-checkin/
├── ablesci_checkin.py                 # 主脚本，纯标准库
├── .github/workflows/checkin.yml      # GitHub Actions 定时任务
├── accounts.example.json             # 账号配置模板
├── .env.example                       # 环境变量说明
├── .gitignore                         # 防止密钥误提交
├── run_local.sh                       # 本地一键运行
└── README.md
```

---

## 3. 五分钟部署到 GitHub Actions

### 第 1 步：建一个**私有**仓库并上传

```bash
cd ~/Desktop/ablesci-checkin
git init
git add .
git commit -m "init: 科研通每日签到"
git remote add origin https://github.com/<你的用户名>/ablesci-checkin.git
git push -u origin main
```

> 一定要 **Private**。虽然密码放的是 Secrets 而非代码，但私有仓库能避免运行日志、配置结构被公开。

### 第 2 步：拿 Cookie（推荐，最稳）

1. 浏览器打开 <https://www.ablesci.com/> 并登录；
2. 按 `F12` → **Network（网络）** → 刷新页面 → 点任意一个 `www.ablesci.com` 的请求；
3. 在 **Request Headers** 里找到 `Cookie:`，复制**冒号后面那一整串值**（形如 `PHPSESSID=xxx; _identity=yyy; ...`）。

> Cookie 即登录凭证，等同于账号密码，别发给任何人、别贴进公开 issue。

### 第 3 步：添加 Repository Secret

仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**。

新建 `ABLESCI_ACCOUNTS_JSON`，值填**单行 JSON**（推荐，多账号最清晰）：

```json
[{"label":"我的号","email":"you@example.com","password":"你的密码","cookie":"PHPSESSID=xxx; _identity=yyy"}]
```

多账号就往数组里再加对象：

```json
[{"email":"a@b.com","password":"pw1","cookie":"PHPSESSID=aaa"},{"email":"c@d.com","password":"pw2","cookie":"PHPSESSID=ccc"}]
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `cookie` | 三选一 | 浏览器 Cookie 串。填了就优先用它 |
| `email` + `password` | 三选一 | 用于自动登录 / Cookie 失效后兜底 |
| `label` | 否 | 日志里显示的名字，可随意写；不填会自动脱敏邮箱 |

也可以不用 JSON，改用两个 Secret：`ABLESCI_ACCOUNTS`（每行 `邮箱:密码`）+ `ABLESCI_COOKIE`（每行一个 Cookie）。**当两者行数一致时会自动一一配对**，实现「Cookie 优先 + 密码兜底」。详见 [.env.example](.env.example)。

### 第 4 步：手动跑一次验证

仓库 → **Actions** → 左侧 **科研通每日签到** → **Run workflow** → 运行。

- 展开 `执行签到`，看日志里出现 `✅ 签到成功` 或 `☑️ 今日已签到` 即配置成功；
- Job 页面底部会有 **Job Summary** 表格；
- 若显示 `🔑 需重新登录`，说明 Cookie 过期且自动登录没成，回到第 2 步重新复制，或确认密码正确。

### 第 5 步：等它自己跑

`.github/workflows/checkin.yml` 已配置每天两次：

| UTC cron | 北京时间 |
| --- | --- |
| `10 0 * * *` | 08:10 |
| `10 13 * * *` | 21:10 |

两次是为了容错：早上失败，晚上还有一次机会（已签到会自动跳过，不会重复加分）。

> ⚠️ GitHub Actions 的 `schedule` **不保证准点**，延迟 5~30 分钟很常见；负载高时偶尔会跳过。这是 GitHub 平台行为，不是脚本问题。想要严格准点请改用服务器 cron 或青龙面板。

修改时间：编辑 `checkin.yml` 里的 cron 后提交即可。记得是 **UTC**，北京时间减 8 小时。

---

## 4. 本地运行（可选）

不需要装任何包，Python 3.9+ 即可：

```bash
cd ~/Desktop/ablesci-checkin
cp accounts.example.json accounts.json      # 填真实值
chmod +x run_local.sh ablesci_checkin.py

./run_local.sh --selftest     # 离线自测，完全不联网
./run_local.sh --dry-run      # 只校验配置（脱敏打印），不联网
./run_local.sh                # 真正签到一次
```

其他用法：

```bash
python3 ablesci_checkin.py --config /path/to/accounts.json   # 指定配置
python3 ablesci_checkin.py --retry 5 --retry-delay 30        # 调整重试
python3 ablesci_checkin.py --save-cookies cookies.json       # 保存登录后的新 Cookie（本地用）
python3 ablesci_checkin.py --no-notify                       # 禁用推送
python3 ablesci_checkin.py --env .env                        # 指定 .env
```

想改成本机 macOS `launchd` 每天定时（不依赖 GitHub），在 `~/Library/LaunchAgents/` 放一个 plist，`ProgramArguments` 指向 `python3 /Users/lnz/Desktop/ablesci-checkin/ablesci_checkin.py`，用 `StartCalendarInterval` 指定时间即可；也可直接用 crontab：`10 8,21 * * * cd ~/Desktop/ablesci-checkin && /usr/bin/python3 ablesci_checkin.py >> checkin.log 2>&1`。本机方案要求电脑在该时刻处于开机状态。

---

## 5. 可选：签到结果推送

在 Actions Secrets 里**按需**添加（不添加就完全不推送）：

| Secret | 用途 |
| --- | --- |
| `SERVERCHAN_KEY` | [Server酱³](https://sct.ftqq.com/) 的 SendKey |
| `PUSHPLUS_TOKEN` | [PushPlus](https://www.pushplus.plus/) token |
| `NOTIFY_WEBHOOK` | 自定义 Webhook，POST JSON `{"title":..., "text":..., "content":...}`，兼容飞书/企微/钉钉机器人 |

`NOTIFY_ON`（Repository **Variable**，非 Secret）：`always` 每次推 / `failure` 仅失败推（默认）/ `never` 不推。

---

## 6. 判断逻辑

`GET https://www.ablesci.com/user/sign`，带 `X-Requested-With: XMLHttpRequest` 与 `Referer`。返回 JSON：

```json
{"code": 0, "msg": "签到成功",
 "data": {"signpoint": 5, "points": 2040, "signcount": 10}}
```

| 情况 | 判定 | 动作 |
| --- | --- | --- |
| `code=0` 且无「已签到」字样 | ✅ 成功 | 结束 |
| msg 含「今日已签到 / 您今天已于…签到」 | ☑️ 已签到 | 结束（幂等） |
| 重定向到 `/site/login`、401/403、msg 含「请先登录」 | 🔑 需登录 | 有密码则自动降级重登；否则提示更新 Cookie |
| 429 / 5xx / 超时 / 非 JSON | 🔁 可重试 | 最多 3 次，间隔 20s |
| 其他 4xx / 明确业务错误 | ❌ 失败 | 不重试，直接报错 |

登录流程：`GET /site/login` 取 `<meta name="csrf-token">`（或旧版 `<input name="_csrf">`）→ `POST /site/login` 提交 `_csrf` / `email` / `password` / `remember=off` → `code=0` 视为成功。

> 若站点要求**图形验证码**，脚本不会尝试绕过，会明确报错并建议你改用 Cookie 方式（在浏览器里人工完成一次验证后复制 Cookie）。

---

## 7. 常见问题

**Q：日志显示「Cookie 已失效」？**
Cookie 有有效期，或你在别处退出了登录。重新复制一次 Cookie；若同时配了 `email`/`password`，脚本已自动尝试重登，无需干预。

**Q：密码登录提示「未能提取 CSRF token」？**
站点页面结构改版了，或触发了人机验证。优先改用 Cookie 方式；也可对照 <https://www.ablesci.com/site/login> 的实际 HTML 更新 `PageParser`。

**Q：会不会一天签到两次、重复加分？**
不会。「今日已签到」会被识别为成功并立即结束，不会二次提交。

**Q：Actions 显示成功但我没收到积分？**
先看 Job Summary 的「说明」列：若写的是「今日已签到」，说明你今天已经领过了。

**Q：一直不自动触发？**
私有仓库的 `schedule` 需要仓库近期有活动；手动 `Run workflow` 一次或做一次提交即可唤醒。另外确认 workflow 文件在默认分支、且 Actions 未被禁用。

**Q：安全吗？**
密码只存在 GitHub Encrypted Secrets 中，脚本不写盘、不打印、不外传。但任何自动化都意味着把登录凭证交给一个持续运行的程序，请自行评估；建议用**独立密码**，并定期更新 Secret。

---

## 8. 参考（接口来源，交叉核对）

- [Walvez/ablesci-auto-checkin](https://github.com/Walvez/ablesci-auto-checkin) — ScriptCat 版，签到接口与响应分类逻辑，[源码](https://scriptcat.org/zh-CN/script-show-page/7145/code)
- [daitcl/ablesciSign](https://github.com/daitcl/ablesciSign) — Python 版，登录 CSRF 流程与 `/site/login` 表单字段
- [hai178912522/ablesci-cli](https://github.com/hai178912522/ablesci-cli) — CLI 版，幂等签到与本地 Cookie 管理思路
- [IamJiLi/KYT-Auto-checkin](https://scriptcat.org/zh-TW/script-show-page/8597/code) — 又一个独立确认 `GET /user/sign` 的实现

上述接口于 2026-11 核对；如站点改版，请以浏览器 Network 面板中的实际请求为准。

---

MIT License。

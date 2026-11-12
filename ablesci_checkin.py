#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
科研通（AbleSci）每日自动签到 —— 单文件、零第三方依赖。

设计目标：
  * 只用 Python 标准库（urllib / cookiejar / html.parser），不需要 pip install；
  * 每个账号优先用浏览器 Cookie，Cookie 失效时自动降级为「邮箱 + 密码」登录；
  * 幂等：今日已签到不会再请求第二次，重试不会重复加分；
  * 只请求 www.ablesci.com，不连接任何第三方统计/上报服务（通知渠道例外，且默认关闭）；
  * 密码、Cookie 永不进日志，并在 GitHub Actions 中额外注册 ::add-mask::。

接口来自以下公开实现，并做了交叉核对（详见 README.md「参考」）：
  * 签到：GET https://www.ablesci.com/user/sign
  * 登录：GET/POST https://www.ablesci.com/site/login
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import html.parser
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE = "https://www.ablesci.com"
LOGIN_URL = f"{BASE}/site/login"
SIGN_URL = f"{BASE}/user/sign"
REFERER = f"{BASE}/"
HTTP_TIMEOUT = 20

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SCRIPT_DIR = Path(__file__).resolve().parent


def _ssl_context():
    """优先用 certifi 的 CA 包。

    部分 macOS/Homebrew 的 Python 没把系统根证书装进 openssl，直接连 https 会报
    CERTIFICATE_VERIFY_FAILED。装了 certifi 就用它；没装则回退到系统默认。
    """
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

# ---------------------------------------------------------------- 结果类型

SUCCESS = "success"                # 本次签到成功
ALREADY = "already"                # 今天已签到（同样视为成功）
LOGIN_REQUIRED = "login_required"  # 登录态失效
RETRYABLE = "retryable"            # 网络/服务端临时问题，可重试
FATAL = "fatal"                    # 业务性失败，重试无意义

LEVEL_ICON = {"info": "ℹ️", "ok": "✅", "warn": "⚠️", "err": "❌"}

LOG_LINES: list[str] = []
SECRETS: list[str] = []


@dataclass
class Resp:
    status: int
    url: str
    body: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Verdict:
    kind: str
    message: str
    reward: str = ""
    points: str = ""
    days: str = ""


@dataclass
class Account:
    label: str
    cookie: str = ""
    email: str = ""
    password: str = ""


@dataclass
class Result:
    label: str
    kind: str
    message: str
    auth: str = ""
    reward: str = ""
    points: str = ""
    days: str = ""
    cookie: str = ""


# ---------------------------------------------------------------- 日志 / 脱敏


def beijing_now() -> dt.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        return dt.datetime.now(dt.timezone.utc).astimezone(
            dt.timezone(dt.timedelta(hours=8))
        )


def scrub(text: str) -> str:
    """把所有已知敏感值替换成 ***，长值优先，避免短值先被替换掉。"""
    for secret in sorted(set(SECRETS), key=len, reverse=True):
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def log(message: str, level: str = "info") -> None:
    line = f"[{beijing_now():%Y-%m-%d %H:%M:%S}] {LEVEL_ICON.get(level, 'ℹ️')} {scrub(str(message))}"
    print(line, flush=True)
    LOG_LINES.append(line)


# ---------------------------------------------------------------- HTML 解析

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "source", "track", "wbr"}


class PageParser(html.parser.HTMLParser):
    """提取登录页 CSRF，以及首页的积分 / 连续签到天数 / 用户名。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.csrf = ""
        self.values: dict[str, str] = {}
        self._active: str | None = None
        self._depth = 0

    @staticmethod
    def _target(tag: str, attrs: dict[str, str]) -> str | None:
        if attrs.get("id") == "user-point-now":
            return "points"
        if attrs.get("id") == "sign-count":
            return "sign_days"
        if "able-head-user-vip-username" in attrs.get("class", "").split():
            return "username"
        return None

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {str(k).lower(): (v or "") for k, v in attrs_list}
        if tag == "meta" and attrs.get("name", "").lower() == "csrf-token":
            self.csrf = self.csrf or attrs.get("content", "").strip()
        if tag == "input" and attrs.get("name") == "_csrf":
            self.csrf = self.csrf or attrs.get("value", "").strip()

        if self._active:
            if tag not in _VOID_TAGS:
                self._depth += 1
            return

        target = self._target(tag, attrs)
        if target:
            self._active = target
            self._depth = 1
            self.values.setdefault(target, "")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # 自闭合标签：开始与结束成对处理，避免破坏深度计数
        self.handle_starttag(tag, attrs)
        if self._active and tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self._active or tag in _VOID_TAGS:
            return
        self._depth -= 1
        if self._depth <= 0:
            self._active = None
            self._depth = 0

    def handle_data(self, data: str) -> None:
        if self._active:
            self.values[self._active] += data


# ---------------------------------------------------------------- HTTP 会话


class AbleSciSession:
    def __init__(self, cookie_header: str = "") -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=_ssl_context()),
        )
        self.base_headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
        }
        if cookie_header:
            self._inject_cookies(cookie_header)

    @staticmethod
    def _cookie_pairs(raw: str):
        text = raw.strip()
        if text.lower().startswith("cookie:"):
            text = text[7:].strip()
        for part in text.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name, value = name.strip(), value.strip()
            if name:
                yield name, value

    def _inject_cookies(self, raw: str) -> None:
        for name, value in self._cookie_pairs(raw):
            cookie = http.cookiejar.Cookie(
                version=0, name=name, value=value,
                port=None, port_specified=False,
                domain=".ablesci.com", domain_specified=True,
                domain_initial_dot=True,
                path="/", path_specified=True,
                secure=False, expires=None, discard=True,
                comment=None, comment_url=None, rest={}, rfc2109=False,
            )
            self.jar.set_cookie(cookie)

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    @staticmethod
    def _read(fp: Any, status: int, url: str) -> Resp:
        raw = fp.read()
        h = getattr(fp, "headers", None)
        headers = {str(k).lower(): str(v) for k, v in h.items()} if h else {}
        if headers.get("content-encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return Resp(status=status, url=url or "",
                    body=raw.decode("utf-8", "replace"), headers=headers)

    def request(
        self,
        url: str,
        method: str = "GET",
        data: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: int = HTTP_TIMEOUT,
    ) -> Resp:
        headers = dict(self.base_headers)
        if extra_headers:
            headers.update(extra_headers)
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("ascii")
            headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded; charset=UTF-8"
            )
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return self._read(r, r.status, r.geturl())
        except urllib.error.HTTPError as e:
            return self._read(e, e.code, e.geturl())

    # ------------------------------------------------------------ 登录

    def login(self, email: str, password: str) -> tuple[bool, str]:
        page = self.request(LOGIN_URL, extra_headers={"Referer": LOGIN_URL})
        if page.status != 200:
            return False, f"打开登录页失败（HTTP {page.status}）"

        parser = PageParser()
        try:
            parser.feed(page.body)
        except Exception as exc:
            return False, f"解析登录页失败：{exc}"
        if not parser.csrf:
            return False, "未能提取 CSRF token（站点页面可能已改版，或触发了人机验证）"

        resp = self.request(
            LOGIN_URL,
            method="POST",
            data={"_csrf": parser.csrf, "email": email,
                  "password": password, "remember": "off"},
            extra_headers={"Referer": LOGIN_URL},
        )
        try:
            payload = json.loads(resp.body.strip())
        except Exception:
            payload = None

        if isinstance(payload, dict):
            if str(payload.get("code")) == "0":
                return True, str(payload.get("msg") or "登录成功")
            reason = str(payload.get("msg") or payload.get("message") or "未知原因")
            if "验证码" in reason or "captcha" in reason.lower():
                reason += "；站点要求图形验证码，脚本不会绕过，请改用 Cookie 方式"
            return False, f"登录失败：{reason}"

        # 少数情况下登录返回的是 HTML 页面：出现「退出」即视为已登录
        if "退出" in resp.body or "/site/logout" in resp.body:
            return True, "登录成功（页面已出现退出入口）"
        return False, (
            f"登录响应不可识别（HTTP {resp.status}）；可能要求图形验证码，"
            "请改用 Cookie 方式，或先手动登录一次"
        )


# ---------------------------------------------------------------- 响应判定

LOGIN_MSG_RE = re.compile(
    r"请先登录|需要登录|未登录|登录已失效|登录失效|登录超时|请重新登录|"
    r"未登录或登录已过期|not\s*logged\s*in|please\s*login|login\s*required|unauthorized",
    re.I,
)
ALREADY_RE = re.compile(
    r"您今天已于.+签到|今日已签到|今天已签到|已经签到|已签到过|重复签到|"
    r"already\s*checked|already\s*signed",
    re.I,
)


def looks_like_login_html(text: str) -> bool:
    sample = text[:4000].lower()
    if not any(tag in sample for tag in ("<html", "<!doctype", "<form")):
        return False
    return (
        "/site/login" in sample
        or 'name="password"' in sample
        or "name='password'" in sample
        or ("登录" in sample and any(k in sample for k in ("password", "passwd", "用户名")))
    )


def _metric(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None or value == "":
        return ""
    return str(value).strip()


def _success_verdict(payload: dict[str, Any], msg: str) -> Verdict:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    reward = _metric(data, "signpoint")
    points = _metric(data, "points")
    days = _metric(data, "signcount") or _metric(data, "sign_days")
    parts = [msg or "签到成功"]
    if reward:
        parts.append(f"本次积分 +{reward}")
    if points:
        parts.append(f"当前积分 {points}")
    if days:
        parts.append(f"连续签到 {days} 天")
    return Verdict(SUCCESS, "；".join(parts), reward, points, days)


def classify(resp: Resp) -> Verdict:
    """把一次 /user/sign 请求归类，决定是完成、重试还是报错。"""
    path = urllib.parse.urlparse(resp.url or "").path.rstrip("/")
    if path == "/site/login":
        return Verdict(LOGIN_REQUIRED, "登录态失效（被重定向到登录页）")
    if resp.status in (401, 403):
        return Verdict(LOGIN_REQUIRED, f"登录态失效（HTTP {resp.status}）")
    if resp.status == 429:
        return Verdict(RETRYABLE, "请求过于频繁（HTTP 429）")
    if resp.status >= 500:
        return Verdict(RETRYABLE, f"科研通服务暂时异常（HTTP {resp.status}）")
    if 400 <= resp.status < 500:
        return Verdict(FATAL, f"请求被拒绝（HTTP {resp.status}）")
    if not (200 <= resp.status < 300):
        return Verdict(RETRYABLE, f"请求失败（HTTP {resp.status}）")

    body = (resp.body or "").strip()
    try:
        payload = json.loads(body)
    except Exception:
        payload = None

    if not isinstance(payload, dict):
        if looks_like_login_html(body) or LOGIN_MSG_RE.search(body):
            return Verdict(LOGIN_REQUIRED, "登录态失效（返回了登录页面）")
        return Verdict(RETRYABLE, "签到接口返回了非 JSON 响应")

    msg = str(payload.get("msg") or payload.get("message") or "").strip()
    if LOGIN_MSG_RE.search(msg):
        return Verdict(LOGIN_REQUIRED, msg or "登录态失效")
    # 「今日已签到」必须优先于 code==0：部分版本会返回 code=0 + 已签到提示
    if ALREADY_RE.search(msg):
        return Verdict(ALREADY, msg.replace("签到失败，", "", 1) or "今天已经签到")
    if str(payload.get("code")) == "0":
        return _success_verdict(payload, msg)
    if msg:
        return Verdict(FATAL, msg.replace("签到失败，", "", 1))
    return Verdict(RETRYABLE, "签到接口返回了无法识别的业务响应")


# ---------------------------------------------------------------- 单账号执行


def run_account(acc: Account, args: argparse.Namespace) -> Result:
    session = AbleSciSession(acc.cookie or "")
    auth = "Cookie" if acc.cookie else "密码登录"
    login_done = False
    log(f"开始处理 {acc.label}（{auth}）")

    if not acc.cookie:
        if not (acc.email and acc.password):
            return Result(acc.label, FATAL,
                          "该账号既没有 Cookie，也没有邮箱和密码", auth=auth)
        ok, info = session.login(acc.email, acc.password)
        login_done = True
        if not ok:
            return Result(acc.label, FATAL, info, auth="密码登录")
        log(f"  {info}", "ok")

    attempt = 0
    last_msg = ""
    while attempt < args.retry:
        attempt += 1
        try:
            resp = session.request(SIGN_URL, extra_headers={"Referer": REFERER})
            verdict = classify(resp)
        except Exception as exc:  # URLError / timeout / SSL / DNS ...
            verdict = Verdict(RETRYABLE, f"网络异常：{exc}")

        if verdict.kind == LOGIN_REQUIRED:
            if acc.email and acc.password and not login_done:
                log("  Cookie 已失效，自动改用邮箱密码登录…", "warn")
                ok, info = session.login(acc.email, acc.password)
                login_done = True
                if ok:
                    auth = "Cookie 失效 → 密码登录"
                    log(f"  {info}", "ok")
                    attempt -= 1  # 降级登录不算一次失败重试
                    continue
                return Result(acc.label, FATAL,
                              f"Cookie 失效，且自动登录失败：{info}", auth=auth)
            if not acc.cookie or login_done:
                return Result(
                    acc.label, FATAL,
                    "重新登录后仍被要求登录（可能触发图形验证码、密码错误或站点风控）；"
                    "建议在浏览器人工登录一次后改用最新的 Cookie",
                    auth=auth,
                )
            return Result(
                acc.label, FATAL,
                "Cookie 已失效：请重新复制 Cookie，或补充 email/password "
                "让脚本自动降级登录",
                auth=auth,
            )

        if verdict.kind in (SUCCESS, ALREADY):
            return Result(
                acc.label, verdict.kind, verdict.message, auth=auth,
                reward=verdict.reward, points=verdict.points,
                days=verdict.days, cookie=session.cookie_header(),
            )
        if verdict.kind == FATAL:
            return Result(acc.label, FATAL, verdict.message, auth=auth)

        last_msg = verdict.message
        log(f"  第 {attempt}/{args.retry} 次尝试失败：{verdict.message}", "warn")
        if attempt < args.retry:
            time.sleep(args.retry_delay)

    return Result(acc.label, RETRYABLE,
                  f"重试 {args.retry} 次后仍未成功：{last_msg}", auth=auth)


# ---------------------------------------------------------------- 配置加载

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def load_dotenv(path: Path) -> None:
    """极简 .env：KEY=VALUE，支持引号与 \\n 转义；不覆盖已有环境变量。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        value = value.replace("\\n", "\n")
        os.environ.setdefault(key, value)


def mask_actions_secrets() -> None:
    """GitHub Actions：把原始密钥值再注册一遍，防止拼接/解析后泄漏。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    keys = [k for k in os.environ
            if k.startswith("ABLESCI_")
            or k in ("SERVERCHAN_KEY", "PUSHPLUS_TOKEN", "NOTIFY_WEBHOOK", "XZKEY")]
    for key in keys:
        value = os.environ.get(key, "")
        for piece in re.split(r"[\s,;]+", value):
            if len(piece) >= 4:
                print(f"::add-mask::{piece}", flush=True)
                SECRETS.append(piece)
        if len(value) >= 4:
            print(f"::add-mask::{value}", flush=True)
            SECRETS.append(value)


def mask_label(email: str, idx: int) -> str:
    if not email:
        return f"账号{idx}"
    if "@" in email:
        local, domain = email.split("@", 1)
        return (local[:2] + "***" if len(local) > 2 else "***") + "@" + domain
    return email[:2] + "***"


def account_from_obj(obj: Any, idx: int) -> Account | None:
    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            return None
        head = re.split(r"[:|]", text, maxsplit=1)[0].strip()
        if EMAIL_RE.match(head) and (":" in text or "|" in text):
            sep = ":" if ":" in text else "|"
            email, password = text.split(sep, 1)
            return Account(mask_label(email.strip(), idx),
                           email=email.strip(), password=password.strip())
        # 一整行 Cookie 字符串
        return Account(f"账号{idx}（Cookie）", cookie=text)

    if not isinstance(obj, dict):
        return None
    cookie = str(obj.get("cookie") or "").strip()
    email = str(obj.get("email") or "").strip()
    password = str(obj.get("password") or "").strip()
    label = str(obj.get("label") or "").strip() or mask_label(email, idx)
    if not (cookie or (email and password)):
        log(f"第 {idx} 个账号配置不完整（需 cookie，或 email+password），已跳过", "warn")
        return None
    return Account(label=label, cookie=cookie, email=email, password=password)


def _split_lines(raw: str) -> list[str]:
    return [l.strip() for l in raw.replace("\\n", "\n").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def load_accounts(args: argparse.Namespace) -> list[Account]:
    payload: Any = None
    if args.config:
        path = Path(args.config).expanduser()
        if not path.exists():
            raise SystemExit(f"配置文件不存在：{path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        default = SCRIPT_DIR / "accounts.json"
        env_json = os.environ.get("ABLESCI_ACCOUNTS_JSON", "").strip()
        if default.exists():
            payload = json.loads(default.read_text(encoding="utf-8"))
        elif env_json:
            payload = json.loads(env_json)

    if payload is not None:
        items = payload.get("accounts", []) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise SystemExit("配置必须是数组，或是含 accounts 数组的对象")
        accounts = []
        for i, obj in enumerate(items, 1):
            acc = account_from_obj(obj, i)
            if acc:
                accounts.append(acc)
        return accounts

    # 环境变量模式：ABLESCI_ACCOUNTS（email:password）与 ABLESCI_COOKIE 可配对
    cred_lines = _split_lines(os.environ.get("ABLESCI_ACCOUNTS", ""))
    cookie_lines = _split_lines(
        os.environ.get("ABLESCI_COOKIE", "") or os.environ.get("ABLESCI_COOKIES", "")
    )
    accounts: list[Account] = []
    if cred_lines and cookie_lines and len(cred_lines) == len(cookie_lines):
        # 数量一致时一一配对：优先 Cookie，失效自动降级到密码
        for i, (cred, cookie) in enumerate(zip(cred_lines, cookie_lines), 1):
            acc = account_from_obj(cred, i)
            if acc:
                acc.cookie = cookie
                accounts.append(acc)
    else:
        for i, line in enumerate(cred_lines, 1):
            acc = account_from_obj(line, i)
            if acc:
                accounts.append(acc)
        for i, line in enumerate(cookie_lines, 1):
            acc = account_from_obj(line, i)
            if acc:
                accounts.append(acc)
    return accounts


def register_account_secrets(accounts: list[Account]) -> None:
    for acc in accounts:
        for value in (acc.cookie, acc.email, acc.password):
            if value and len(value) >= 4:
                SECRETS.append(value)
        for name, value in AbleSciSession._cookie_pairs(acc.cookie):
            SECRETS.append(f"{name}={value}")


# ---------------------------------------------------------------- 通知 / 汇总


def _post(url: str, data: dict[str, str], as_json: bool = False) -> int:
    body = (json.dumps(data, ensure_ascii=False).encode("utf-8") if as_json
            else urllib.parse.urlencode(data).encode("utf-8"))
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/json; charset=utf-8" if as_json
        else "application/x-www-form-urlencoded; charset=utf-8",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def maybe_notify(results: list[Result], args: argparse.Namespace) -> None:
    if args.no_notify:
        return
    mode = os.environ.get("NOTIFY_ON", "always").strip().lower()
    if mode == "never":
        return
    ok = sum(1 for r in results if r.kind in (SUCCESS, ALREADY))
    all_ok = ok == len(results)
    if mode == "failure" and all_ok:
        return

    title = f"科研通签到 {'✅' if all_ok else '❌'} {ok}/{len(results)}"
    text = "\n".join(LOG_LINES)
    targets: list[tuple[str, dict[str, str], bool]] = []

    hook = os.environ.get("NOTIFY_WEBHOOK", "").strip()
    if hook:
        targets.append((hook, {"title": title, "text": text, "content": text}, True))
    key = os.environ.get("SERVERCHAN_KEY", "").strip()
    if key:
        targets.append((f"https://sctapi.ftqq.com/{key}.send",
                        {"title": title, "desp": text}, False))
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if token:
        targets.append(("https://www.pushplus.plus/send",
                        {"token": token, "title": title,
                         "content": text, "template": "txt"}, True))

    for url, data, as_json in targets:
        try:
            _post(url, data, as_json)
            log("通知已发送", "ok")
        except Exception as exc:
            log(f"通知发送失败：{exc}", "warn")


def write_step_summary(results: list[Result]) -> None:
    """写入 GitHub Actions Job Summary，方便在网页里直接看结果。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    def cell(text: Any) -> str:
        return scrub(str(text or "")).replace("|", "\\|").replace("\n", " ")

    ok = sum(1 for r in results if r.kind in (SUCCESS, ALREADY))
    lines = [
        "## 科研通每日签到",
        "",
        f"执行时间：{beijing_now():%Y-%m-%d %H:%M:%S}（北京时间）　"
        f"结果：{ok}/{len(results)} 成功",
        "",
        "| 账号 | 结果 | 说明 | 登录方式 |",
        "| --- | --- | --- | --- |",
    ]
    icon = {SUCCESS: "✅ 签到成功", ALREADY: "☑️ 今日已签到",
            LOGIN_REQUIRED: "🔑 需重新登录", RETRYABLE: "🔁 可重试失败",
            FATAL: "❌ 失败"}
    for r in results:
        # 成功响应里的 message 已含积分/天数，这里不重复拼接；
        # 只有 message 未带这些信息时才补充。
        detail = r.message
        if r.kind in (SUCCESS, ALREADY) and r.points and "积分" not in detail:
            detail += f"（当前 {r.points} 分"
            if r.days:
                detail += f"，连续 {r.days} 天"
            detail += "）"
        lines.append(
            f"| {cell(r.label)} | {icon.get(r.kind, r.kind)} | "
            f"{cell(detail)} | {cell(r.auth)} |"
        )
    lines += ["", "> 若出现「需重新登录」，请更新仓库 Secrets 中的 Cookie 或账号密码。", ""]
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log("已写入 GitHub Actions Job Summary", "ok")
    except Exception as exc:
        log(f"写入 Job Summary 失败：{exc}", "warn")


def save_cookies(path: str, results: list[Result]) -> None:
    data = [{"label": r.label, "cookie": r.cookie} for r in results if r.cookie]
    target = Path(path).expanduser()
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    log(f"已保存 {len(data)} 个账号的最新 Cookie 到 {target}（权限 600；"
        "切勿提交到 Git 或上传为 Actions 产物）", "ok")


# ---------------------------------------------------------------- 离线自测


def selftest() -> int:
    failures: list[str] = []

    def eq(name: str, got: Any, want: Any) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")
            print(f"  ❌ {name}")
        else:
            print(f"  ✅ {name}")

    def j(obj: dict[str, Any]) -> str:
        return json.dumps(obj, ensure_ascii=False)

    print("运行离线自测（不发任何网络请求）…\n")

    p = PageParser()
    p.feed('<html><head><meta name="csrf-token" content="TOKEN123"></head><body>'
           '<span id="user-point-now">2040</span>'
           '<span id="sign-count">10</span>'
           '<span class="mobile-hide able-head-user-vip-username">张三</span>'
           '</body></html>')
    eq("解析 csrf-token(meta)", p.csrf, "TOKEN123")
    eq("解析积分", p.values.get("points", "").strip(), "2040")
    eq("解析连续天数", p.values.get("sign_days", "").strip(), "10")
    eq("解析用户名", p.values.get("username", "").strip(), "张三")

    p2 = PageParser()
    p2.feed('<input type="hidden" name="_csrf" value="LEGACY">')
    eq("解析旧版 _csrf input", p2.csrf, "LEGACY")

    eq("Cookie 解析（含 Cookie: 前缀）",
       list(AbleSciSession._cookie_pairs("Cookie: PHPSESSID=abc; _csrf=def")),
       [("PHPSESSID", "abc"), ("_csrf", "def")])

    ok_payload = {"code": 0, "msg": "签到成功",
                  "data": {"signpoint": 5, "points": 2040, "signcount": 10}}
    v = classify(Resp(200, SIGN_URL, j(ok_payload)))
    eq("签到成功判定", v.kind, SUCCESS)
    eq("积分/天数提取", (v.reward, v.points, v.days), ("5", "2040", "10"))
    eq("成功文案", v.message, "签到成功；本次积分 +5；当前积分 2040；连续签到 10 天")

    eq("已签到优先于 code=0",
       classify(Resp(200, SIGN_URL, j({"code": 0, "msg": "您今天已于 08:00 签到"}))).kind,
       ALREADY)
    eq("已签到（code!=0）",
       classify(Resp(200, SIGN_URL, j({"code": 1, "msg": "今天已签到"}))).kind,
       ALREADY)
    eq("需登录（重定向）",
       classify(Resp(200, LOGIN_URL, j({"code": 0}))).kind, LOGIN_REQUIRED)
    eq("需登录（401）", classify(Resp(401, SIGN_URL, "")).kind, LOGIN_REQUIRED)
    eq("需登录（msg）",
       classify(Resp(200, SIGN_URL, j({"code": 1, "msg": "请先登录"}))).kind,
       LOGIN_REQUIRED)
    eq("需登录（HTML 登录页）",
       classify(Resp(200, SIGN_URL,
                     '<html><form><input name="password"></form></html>')).kind,
       LOGIN_REQUIRED)
    eq("429 可重试", classify(Resp(429, SIGN_URL, "")).kind, RETRYABLE)
    eq("502 可重试", classify(Resp(502, SIGN_URL, "")).kind, RETRYABLE)
    eq("404 致命", classify(Resp(404, SIGN_URL, "")).kind, FATAL)
    eq("非 JSON 可重试",
       classify(Resp(200, SIGN_URL, "<html>oops</html>")).kind, RETRYABLE)
    eq("业务失败",
       classify(Resp(200, SIGN_URL, j({"code": 2, "msg": "活动已结束"}))).kind,
       FATAL)
    eq("无法识别的业务响应",
       classify(Resp(200, SIGN_URL, j({"code": 9}))).kind, RETRYABLE)

    a = account_from_obj({"email": "abc@example.com", "password": "pw",
                          "cookie": "PHPSESSID=xyz"}, 1)
    eq("账号标签脱敏", a.label if a else None, "ab***@example.com")
    eq("账号同时带 Cookie 与密码", bool(a and a.cookie and a.password), True)
    eq("字符串形式 email:password",
       (lambda x: (x.email, x.password) if x else None)(
           account_from_obj("a@b.com:p@ss:word", 2)),
       ("a@b.com", "p@ss:word"))
    eq("纯 Cookie 字符串",
       (lambda x: x.cookie if x else None)(
           account_from_obj("PHPSESSID=xyz; _identity=abc", 3)),
       "PHPSESSID=xyz; _identity=abc")

    SECRETS.extend(["SUPERSECRETVALUE", "abc"])
    eq("日志脱敏", scrub("token=SUPERSECRETVALUE"), "token=***")

    print()
    if failures:
        print(f"自测失败 {len(failures)} 项：")
        for f in failures:
            print("  - " + f)
        return 1
    print("全部自测通过 ✅")
    return 0


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="科研通（AbleSci）每日自动签到 —— 零第三方依赖",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "配置优先级：--config > 脚本目录下 accounts.json > 环境变量。\n"
            "环境变量：ABLESCI_ACCOUNTS_JSON / ABLESCI_ACCOUNTS / ABLESCI_COOKIE\n"
            "详见 README.md"
        ),
    )
    ap.add_argument("--config", metavar="PATH", help="JSON 账号配置文件")
    ap.add_argument("--env", metavar="PATH", help="额外加载的 .env 文件")
    ap.add_argument("--dry-run", action="store_true",
                    help="只校验配置并打印账号（脱敏），不发任何网络请求")
    ap.add_argument("--selftest", action="store_true",
                    help="离线自测：验证解析与响应判定逻辑，不联网")
    ap.add_argument("--retry", type=int, default=3,
                    help="单账号最大尝试次数，默认 3")
    ap.add_argument("--retry-delay", type=int, default=20,
                    help="两次尝试之间的等待秒数，默认 20")
    ap.add_argument("--save-cookies", metavar="PATH",
                    help="把本次登录后的最新 Cookie 写入本地文件（权限 600）")
    ap.add_argument("--no-notify", action="store_true", help="禁用所有推送通知")
    ap.add_argument("--verbose", action="store_true", help="打印完整接口响应体（已脱敏）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.selftest:
        return selftest()

    load_dotenv(Path(args.env).expanduser() if args.env else SCRIPT_DIR / ".env")
    mask_actions_secrets()

    try:
        accounts = load_accounts(args)
    except (json.JSONDecodeError, OSError, SystemExit) as exc:
        log(f"读取配置失败：{exc}", "err")
        return 1
    register_account_secrets(accounts)

    log(f"科研通每日签到 · 开始（北京时间 {beijing_now():%Y-%m-%d %H:%M:%S}）")
    if not accounts:
        log("没有找到有效账号。请配置 accounts.json，或设置环境变量 "
            "ABLESCI_ACCOUNTS_JSON / ABLESCI_ACCOUNTS / ABLESCI_COOKIE", "err")
        return 1
    log(f"共 {len(accounts)} 个账号")

    if args.dry_run:
        for acc in accounts:
            mode = "Cookie" if acc.cookie else "密码登录"
            fallback = "（带密码兜底）" if acc.cookie and acc.password else ""
            log(f"  · {acc.label}：{mode}{fallback}｜"
                f"Cookie {'已配置' if acc.cookie else '未配置'}，"
                f"密码 {'已配置' if acc.password else '未配置'}")
        log("配置校验通过（--dry-run，未发起任何网络请求）", "ok")
        return 0

    results = [run_account(acc, args) for acc in accounts]

    ok = sum(1 for r in results if r.kind in (SUCCESS, ALREADY))
    for r in results:
        icon = "✅" if r.kind == SUCCESS else ("☑️" if r.kind == ALREADY else "❌")
        log(f"{r.label}：{icon} {r.message}｜登录方式 {r.auth}",
            "ok" if r.kind in (SUCCESS, ALREADY) else "err")

    if args.save_cookies:
        try:
            save_cookies(args.save_cookies, results)
        except Exception as exc:
            log(f"保存 Cookie 失败：{exc}", "warn")

    write_step_summary(results)
    maybe_notify(results, args)
    log(f"完成：{ok}/{len(results)} 个账号成功", "ok" if ok == len(results) else "err")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)

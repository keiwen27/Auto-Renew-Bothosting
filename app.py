#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, json, time, random, requests, subprocess,ctypes
import urllib.parse
from seleniumbase import SB

# 环境变量配置(可以直接私库在双引号里填写)
SESSION_TOKEN = os.environ.get("SESSION_TOKEN") or "" # session token cookie,默认登录方式,非必须
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or "" # Discord Token 备用登录方式,失败时才使用,非必须
EMAIL         = os.environ.get("EMAIL") or ""           # 邮箱,密码登录方式使用,可选
PASSWORD      = os.environ.get("PASSWORD") or ""        # 密码登录方式使用,可选
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG chat id,不填写不通知，需和bot token一起填写生效
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG bot token

BASE_URL = "https://bot-hosting.net"

# 解析 DISCORD_TOKEN (格式: 邮箱,token 时取 token 部分)
DC_TOKEN = ""
if DISCORD_TOKEN:
    DC_TOKEN = DISCORD_TOKEN.split(",", 1)[-1].strip()

if not SESSION_TOKEN and not DC_TOKEN and not (EMAIL and PASSWORD):
    print("ℹ️ 未配置 SESSION_TOKEN / DISCORD_TOKEN / EMAIL+PASSWORD,脚本终止。")
    sys.exit(1)

# 构造 cookie
COOKIES = {
    "session_token": SESSION_TOKEN,
    "login": "true",
    "theme": "system",
}

# 账单页地址 (站点曾从 /a/billings 改为 /dash/billings, 判断只认 billings 关键字)
BILLINGS_URL = f"{BASE_URL}/dash/billings"

def is_billings_url(url: str) -> bool:
    return "billings" in (url or "") and "/login" not in url and "error=" not in url

# 发送tg通知
def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")

# 通知格式
_ACCOUNT_EMAIL = ""   # 登录后从 Discord API 或页面获取的账号邮箱

def fetch_discord_email() -> str:
    """用 DC_TOKEN 查询 Discord 账号邮箱"""
    if not DC_TOKEN:
        return ""
    try:
        resp = requests.get(
            "https://discord.com/api/v9/users/@me",
            headers={"authorization": DC_TOKEN, "user-agent": DISCORD_UA},
            timeout=15,
        )
        if resp.status_code == 200:
            email = (resp.json() or {}).get("email") or ""
            if email:
                print(f"📧 已获取 Discord 账号邮箱")
            return email
        print(f"⚠️ 获取 Discord 账号信息失败: HTTP {resp.status_code}")
    except Exception as e:
        print(f"⚠️ 获取 Discord 账号信息异常: {e}")
    return ""

def scrape_page_email(sb) -> str:
    """从当前页面文本里提取账号邮箱"""
    try:
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", sb.get_page_source())
        return m.group(0) if m else ""
    except Exception:
        return ""

def masked_account() -> str:
    """优先 EMAIL, 其次登录后获取的邮箱, 再次 DISCORD_TOKEN 里携带的邮箱, 均无则返回未知"""
    raw = EMAIL or _ACCOUNT_EMAIL
    if not raw and DISCORD_TOKEN and ',' in DISCORD_TOKEN:
        first = DISCORD_TOKEN.split(',', 1)[0].strip()
        if '@' in first:
            raw = first
    if '@' in raw:
        name, domain = raw.split('@', 1)
        if len(name) > 4:
            return f"{name[:2]}****{name[-2:]}@{domain}"
        return f"{name}@{domain}"
    if raw:
        return raw[:2] + '****'
    return "未知"

def format_notification(status: str, extra: str = "", error: str = "", expiry_date: str = "") -> str:
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    masked_email = masked_account()
    
    lines = [
        "🇫🇮 Bot-hosting 续期通知",
        "",
        f"{status}",
        f"👤 登录账户: {masked_email}",
    ]
    if expiry_date:
        lines.append(f"📅 到期时间: {expiry_date}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 登录时间: {now}")
    return "\n".join(lines)

# ---------- 基础工具 ----------
def js(sb, script, default=None, errors=None):
    try:
        result = sb.execute_script(script)
        return default if result is None else result
    except Exception as e:
        if errors is not None:
            errors.append(f"execute_script: {type(e).__name__}: {e}")
        try:
            result = sb.cdp.evaluate("(function(){ " + script + " })()")
            return default if result is None else result
        except Exception as e2:
            if errors is not None:
                errors.append(f"cdp.evaluate: {type(e2).__name__}: {e2}")
            return default

# Turnstile 定位失败时的现场诊断快照
def diag_turnstile_dom(sb, errors=None):
    snap = js(sb, """
        var out = [];
        var ifr = document.querySelectorAll('iframe');
        out.push('top-doc iframes: ' + ifr.length);
        for (var i = 0; i < ifr.length; i++) {
            var r = ifr[i].getBoundingClientRect();
            out.push('iframe w=' + Math.round(r.width) + ' h=' + Math.round(r.height) + ' src=' + (ifr[i].src || '').slice(0, 90));
        }
        var ts = document.querySelectorAll('[class*="turnstile"], [id*="turnstile"], input[name="cf-turnstile-response"]');
        out.push('turnstile-ish elements: ' + ts.length);
        for (var j = 0; j < ts.length; j++) {
            var rr = ts[j].getBoundingClientRect();
            var cls = (typeof ts[j].className === 'string') ? ts[j].className : (ts[j].getAttribute('class') || '');
            out.push(' - ' + ts[j].tagName + ' class=' + cls + ' w=' + Math.round(rr.width) + ' h=' + Math.round(rr.height));
        }
        out.push('window.turnstile=' + (typeof window.turnstile) + ' readyState=' + document.readyState);
        return out.join('\\n');
    """, "")
    for line in str(snap).splitlines():
        print("   [diag] " + line)
    if errors:
        for err in errors:
            print("   [diag][exec-error] " + str(err)[:200])

#  页面注入脚本
# Turnstile token（官方 JS 接口 或 表单隐藏域），未通过时为空串
TURNSTILE_TOKEN_JS = """
try { if (window.turnstile && window.turnstile.getResponse) {
        var t = window.turnstile.getResponse(); if (t) return t; } } catch (e) {}
var el = document.querySelector('input[name="cf-turnstile-response"]');
return el ? (el.value || '') : '';
"""

TURNSTILE_EXISTS_JS = """
return document.querySelector(
    'input[name="cf-turnstile-response"], iframe[src*="challenges.cloudflare.com"], .cf-turnstile'
) !== null;
"""
TS_COORDS_JS = """
function findWidget(root) {
    var els = root.querySelectorAll(
        'iframe[src*="challenges.cloudflare.com"], .cf-turnstile, [id*="cf-chl"]');
    for (var i = 0; i < els.length; i++) {
        var r = els[i].getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return els[i];
    }
    var all = root.querySelectorAll('*');
    for (var j = 0; j < all.length; j++) {
        if (all[j].shadowRoot) {
            var f = findWidget(all[j].shadowRoot);
            if (f) return f;
        }
    }
    return null;
}
function boxFromTokenInput() {
    var el = document.querySelector('input[name="cf-turnstile-response"]');
    var cur = el ? el.parentElement : null;
    for (var k = 0; k < 6 && cur; k++) {
        var r = cur.getBoundingClientRect();
        if (r.width > 100 && r.width < 600 && r.height > 30 && r.height < 140) return cur;
        cur = cur.parentElement;
    }
    return null;
}
var hit = findWidget(document) || boxFromTokenInput();
if (!hit) return null;
var hr = hit.getBoundingClientRect();
return {cx: Math.round(hr.x + 30), cy: Math.round(hr.y + hr.height / 2)};
"""

# Turnstile 脚本异步渲染，轮询等待组件出现
def wait_turnstile_present(sb, timeout=12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if js(sb, TURNSTILE_EXISTS_JS, False):
            return True
        time.sleep(1)
    return False

def _activate_window():
    if os.name == "nt":
        return

    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls], capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]], timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"], timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _windows_mouse_click(x: int, y: int):
    try:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.08)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
    except Exception:
        pass

def _xdotool_click(x: int, y: int):
    if os.name == "nt":
        _windows_mouse_click(x, y)
        return

    _activate_window()
    try:
        subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)], timeout=3, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
    except Exception:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")

def turnstile_token(sb) -> str:
    token = js(sb, TURNSTILE_TOKEN_JS, "")
    return token if isinstance(token, str) else ""

def _click_turnstile(sb, locate_timeout=10) -> bool:
    coords = None
    errors = []
    deadline = time.time() + locate_timeout
    while time.time() < deadline:
        coords = js(sb, TS_COORDS_JS, None, errors)
        if coords:
            break
        time.sleep(1)
    if not coords:
        print("⚠️ 无法定位 Turnstile 坐标, 打印现场诊断:")
        diag_turnstile_dom(sb, errors)
        return False

    try:
        wi = js(sb, "return '' + (window.screenX || 0) + '|' + (window.screenY || 0) + '|' +"
                    " window.outerHeight + '|' + window.innerHeight;", "")
        parts = str(wi).split("|")
        sx, sy = int(parts[0] or 0), int(parts[1] or 0)
        bar = int(parts[2] or 800) - int(parts[3] or 768)
        ax = int(coords["cx"]) + sx
        ay = int(coords["cy"]) + sy + bar
        print(f"🖱️ 底层点击 Turnstile ({ax}, {ay})")
        _xdotool_click(ax, ay)
        time.sleep(3)
        return True
    except Exception as e:
        print(f"⚠️ 底层点击失败: {e}")
        return False

def click_turnstile(sb) -> bool:
    if _click_turnstile(sb):
        return True
    coords = js(sb, TS_COORDS_JS)
    if coords:
        try:
            if hasattr(sb, "cdp") and hasattr(sb.cdp, "gui_click_x_y"):
                print(f"🖱️ 坐标点击 Turnstile ({coords['cx']},{coords['cy']})")
                sb.cdp.gui_click_x_y(int(coords["cx"]), int(coords["cy"]))
                return True
        except Exception as e:
            print(f"⚠️ 坐标点击失败: {e}")
    try:
        if hasattr(sb, "cdp") and hasattr(sb.cdp, "click_captcha"):
            print("🖱️ 兜底调用cdp点击 ...")
            sb.cdp.click_captcha()
            return True
        print("⚠️ 当前 SeleniumBase 版本无 sb.cdp.click_captcha")
    except Exception as e:
        print(f"⚠️ click_captcha 出错: {e}")
    return False

def solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8) -> bool:
    if turnstile_token(sb):
        return True

    deadline = time.time() + timeout
    next_click = time.time() + first_wait
    clicks = 0
    while time.time() < deadline:
        if turnstile_token(sb):
            print(f"✅ {tag}已通过(已获取 token)")
            return True
        if time.time() >= next_click:
            clicks += 1
            print(f"🖱️ 第 {clicks} 次点击 Turnstile 验证框...")
            click_turnstile(sb)
            next_click = time.time() + 10 + random.uniform(0, 3)
        time.sleep(2)
    ok = bool(turnstile_token(sb))
    print(f"{'✅' if ok else '❌'} {tag}{'已通过' if ok else '未通过'}")
    return ok

def handle_turnstile(sb) -> bool:
    print("🛡️ 处理 Turnstile 验证...")
    return solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8)
    
# 获取当前出口ip
def get_current_ip(proxy_server: str = "") -> str:
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    response.raise_for_status()
    return response.text.strip()

# 时间格式化
def format_countdown(countdown_str: str) -> str:
    try:
        h, m, _ = countdown_str.split(':')
        h = int(h)
        m = int(m)
        if h > 0:
            return f"{h}h{m}min"
        else:
            return f"{m}min"
    except:
        return countdown_str

MAX_COOLDOWN_WAIT = 90 * 60  # 冷却剩余不超过90分钟时原地等待, 超过则跳过本次

# 解析 HH:MM:SS 倒计时为秒数
def parse_countdown_seconds(text: str) -> int:
    try:
        h, m, s = [int(x) for x in text.split(':')]
        return h * 3600 + m * 60 + s
    except Exception:
        return 0

# 归一化日期为 YYYY/MM/DD
def normalize_date(date_str: str) -> str:
    parts = date_str.split('/')
    if len(parts) == 3:
        if len(parts[2]) == 4:   # MM/DD/YYYY
            return f"{parts[2]}/{int(parts[0]):02d}/{int(parts[1]):02d}"
        if len(parts[0]) == 4:   # YYYY/MM/DD
            return f"{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}"
    return date_str

DATE_RE = r"(\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}/\d{4})"

# 获取过期日期
def extract_expiry_date(page_source: str) -> str:
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*" + DATE_RE,   # Expires 10/24/2026. Renew manually to extend it for 4 days.
        DATE_RE,                                # 兜底: 账单页纯文本中通常只有到期日期
    ]
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            return normalize_date(match.group(1))
    return None

# 优先从 DOM 的 st-meta 元素提取到期日期 (不依赖页面语言)
def get_expiry_from_dom(sb) -> str:
    for selector in ['span.st-meta', '.st-meta', '[class*="st-meta"]']:
        try:
            if sb.is_element_present(selector):
                match = re.search(DATE_RE, sb.get_text(selector))
                if match:
                    return normalize_date(match.group(1))
        except Exception:
            pass
    return None

# ---------- 登录方式: SESSION_TOKEN Cookie ----------
def cookie_login(sb) -> bool:
    """注入 session_token cookie 后访问账单页验证"""
    global _ACCOUNT_EMAIL
    print("🚀 启动浏览器...")
    sb.open("https://bot-hosting.net/")
    sb.wait_for_ready_state_complete()
    sb.sleep(2)

    print("📝 注入 Cookie...")
    for name, value in COOKIES.items():
        if value:
            sb.add_cookie({"name": name, "value": value, "domain": "bot-hosting.net"})

    print(f"🌐 访问 {BILLINGS_URL} ...")
    sb.open(BILLINGS_URL)
    sb.wait_for_ready_state_complete()
    sb.sleep(3)
    current_url = sb.get_current_url()
    current_title = sb.get_title()
    print(f"📝 当前URL: {current_url}, Title: {current_title}")

    if is_billings_url(current_url):
        print("✅ SESSION_TOKEN 登录成功, 当前已到达账单页")
        _ACCOUNT_EMAIL = scrape_page_email(sb)
        return True
    print(f"❌ SESSION_TOKEN 登录失败，当前URL: {current_url}, 当前标题: {current_title}")
    return False

# ---------- 登录方式: Discord OAuth ----------
DISCORD_CLIENT_ID   = "884382422530158623"
OAUTH_REDIRECT_URI  = "https://bot-hosting.net/login"
OAUTH_SCOPE         = "identify email guilds"
DISCORD_API         = "https://discord.com/api/v9/oauth2/authorize"
DISCORD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
STATE_RE = re.compile(r"[?&]state=([^&]+)")


def capture_discord_state(sb) -> str:
    """打开 /login/discord，从落地页 URL 里提取本次会话的 state"""
    print("🔎 获取 Discord OAuth state...")
    sb.uc_open_with_reconnect("https://bot-hosting.net/login/discord", reconnect_time=4)
    time.sleep(2)

    url = sb.get_current_url()
    if "discord.com" not in url:
        print(f"⚠️ 未跳转到 Discord 相关页面，当前 URL：{url}")
        return ""

    m = STATE_RE.search(url)
    if not m:
        print(f"❌ 未能从 URL 中解析出 state，当前 URL：{url}")
        return ""

    state = urllib.parse.unquote(m.group(1))
    print(f"✅ 已捕获 state（当前落地页：{urllib.parse.urlparse(url).path}）")
    return state


def discord_authorize(state: str) -> str:
    """用 DC_TOKEN 直接完成 Discord 侧授权，返回跳转回 bot-hosting.net 的 location"""
    query = urllib.parse.urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "scope":         OAUTH_SCOPE,
        "state":         state,
    })
    authorize_url = f"{DISCORD_API}?{query}"

    referer = (
        "https://discord.com/oauth2/authorize?" +
        urllib.parse.urlencode({
            "client_id":     DISCORD_CLIENT_ID,
            "redirect_uri":  OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope":         OAUTH_SCOPE,
            "state":         state,
        })
    )

    headers = {
        "accept":           "*/*",
        "authorization":    DC_TOKEN,
        "content-type":     "application/json",
        "origin":           "https://discord.com",
        "referer":          referer,
        "user-agent":       DISCORD_UA,
        "x-discord-locale": "zh-CN",
    }

    body = json.dumps({
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    })

    # 如果配置了代理，Discord API 请求也走代理
    proxies = None
    _is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    _proxy_server = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    if _is_proxy:
        proxies = {"http": _proxy_server, "https": _proxy_server}

    try:
        resp = requests.post(authorize_url, headers=headers, data=body, proxies=proxies, timeout=20)
        if resp.status_code != 200:
            print(f"❌ Discord OAuth2 授权失败: HTTP {resp.status_code} - {resp.text[:300]}")
            return ""
        resp_data = resp.json()
    except Exception as e:
        print(f"❌ Discord OAuth2 授权异常: {e}")
        return ""

    location = resp_data.get("location", "")
    if not location:
        print(f"❌ 授权响应中未找到 location 字段: {resp_data}")
        return ""

    masked = re.sub(r"code=[^&]+", "code=***", location)
    print(f"✅ 拿到回调 URL: {masked}")
    return location


def do_discord_login(sb) -> bool:
    """通过 Discord Token 走完整 OAuth 流程登录 bot-hosting.net"""
    global _ACCOUNT_EMAIL
    print("\n🔑 通过 Discord Token 登录...")

    state = capture_discord_state(sb)
    if not state:
        sb.save_screenshot("login_no_state.png")
        return False

    location = discord_authorize(state)
    if not location:
        return False

    print("↩️ 携带授权码打开回调链接...")
    sb.uc_open_with_reconnect(location, reconnect_time=4)
    time.sleep(3)

    url = sb.get_current_url()

    if "/error/banned" in url:
        print("🚫 账号已被封禁")
        sb.save_screenshot("login_banned.png")
        return False

    if "bot-hosting.net" not in url:
        print(f"❌ 回调后未跳转至 bot-hosting.net，当前 URL：{url}")
        sb.save_screenshot("login_no_redirect.png")
        return False

    try:
        body_text = sb.get_text("body")
    except Exception:
        body_text = ""
    if "fraud" in body_text.lower():
        print("🚫 触发风控（fraud attempt），可能是 IP 被拦截")
        sb.save_screenshot("login_fraud.png")
        return False

    for _ in range(30):
        url = sb.get_current_url()
        path = urllib.parse.urlparse(url).path
        if "bot-hosting.net" in url and path != "/login" and not path.startswith("/login/discord"):
            print(f"✅ Discord OAuth 登录成功！当前页面：{url}")
            _ACCOUNT_EMAIL = fetch_discord_email() or scrape_page_email(sb)
            return True
        time.sleep(0.5)

    print(f"❌ 登录超时或未跳转成功，最终停留在：{url}")
    try:
        body_text = sb.get_text("body")
        print(f"📄 页面正文片段：{body_text[:200].strip()!r}")
    except Exception:
        pass
    sb.save_screenshot("login_timeout.png")
    return False


# 主流程
def main():
    print("#" * 25)
    print("   Bot-hosting 自动续期")
    print("#" * 25)

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true" 

    sb_kwargs = {"uc": True, "headless": HEADLESS, "locale_code": "en-US"}

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🍭 未使用代理，直连访问")

    with SB(**sb_kwargs) as sb:
        try:
            ip = get_current_ip(PROXY_SERVER if IS_PROXY else "")
            print(f"📍 当前出口IP: {ip}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        login_ok = False

        # 方式1: SESSION_TOKEN Cookie 登录（默认）
        if not login_ok and SESSION_TOKEN:
            print("🔄 尝试 SESSION_TOKEN Cookie 登录...")
            if cookie_login(sb):
                login_ok = True

        # 方式2: Discord OAuth 登录（备用）
        if not login_ok and DC_TOKEN:
            print("\n🔄 SESSION_TOKEN 登录失败或未配置，尝试 Discord OAuth 登录...")
            if do_discord_login(sb):
                print(f"🌐 访问 {BILLINGS_URL} ...")
                sb.open(BILLINGS_URL)
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                current_url = sb.get_current_url()
                print(f"📝 当前URL: {current_url}")
                if is_billings_url(current_url):
                    login_ok = True
                    print("✅ Discord OAuth 登录成功,当前已到达账单页")
                else:
                    print(f"❌ Discord OAuth 登录后仍未到达账单页，当前URL: {current_url}")
            else:
                print("❌ Discord OAuth 登录失败")

        # 方式3: 账号密码登录（兜底；整段兜异常，失败只标记 login_ok=False 走统一失败通知）
        if not login_ok and EMAIL and PASSWORD:
            print("🚀 启动浏览器...")
            try:
                print(f"🌐 访问 {BASE_URL}/login")
                sb.open(f"{BASE_URL}/login")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                sb.click('button[type="submit"]') # 点击 Continue with email 按钮
                sb.sleep(12)  # 等待页面加载
                print(f"📧 填写邮箱...")
                sb.input('input[type="email"]', EMAIL)
                sb.sleep(1)
                print(f"🔑 填写密码...")
                sb.input('input[type="password"]', PASSWORD)
                sb.sleep(3)

                if wait_turnstile_present(sb):
                    if not handle_turnstile(sb):
                        print("❌ 登录界面的 Turnstile 验证失败，尝试兜底点击...")
                        try:
                            sb.cdp.click_captcha()  # 再次尝试点击 Turnstile 验证
                            sb.sleep(4)
                        except Exception as e:
                            print(f"⚠️ 兜底点击失败: {e}")
                        # sb.save_screenshot("turnstile_failed.png")
                else:
                    print("ℹ️ 未检测到 Turnstile")
                sb.sleep(1)
                sb.click('button[type="submit"]') # 点击登录按钮
                print("🖱️ 提交登录按钮")
                sb.sleep(6)  # 等待页面加载
                sb.wait_for_ready_state_complete()
                sb.open(BILLINGS_URL)
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                current_url = sb.get_current_url()
                current_title = sb.get_title()
                print(f"📝 当前URL: {current_url}, Title: {current_title}")

                if is_billings_url(current_url):
                    login_ok = True
                    print("✅ Password 登录成功,当前已到达账单页")
                else:
                    print(f"❌ Password 登录后仍未到达账单页，当前URL: {current_url}")
            except Exception as e:
                    print(f"❌ Password 登录过程异常: {type(e).__name__}: {str(e)[:150]}")

        if not login_ok:
            error_msg = "所有登录方式均失败"
            if not SESSION_TOKEN and not DC_TOKEN:
                error_msg = "Password 登录失败"
            elif SESSION_TOKEN and DC_TOKEN:
                error_msg = "SESSION_TOKEN 和 Discord OAuth 均失败"
            send_telegram_message(format_notification("❌ 登录失败", error=error_msg))
            return

        # 提取当前到期日期
        sb.sleep(3)
        try:
            page_source = sb.get_page_text() + "\n" + sb.get_page_source()
        except Exception:
            page_source = sb.get_page_source()
        try:
            body_text = sb.execute_script("return document.body.innerText.substring(0, 600)")
            print(f"📄 账单页可见文本: {body_text!r}")
        except Exception as e:
            print(f"⚠️ 读取页面文本失败: {e}")
        current_expiry = get_expiry_from_dom(sb) or extract_expiry_date(page_source)
        if current_expiry:
            print(f"📅 当前到期日期: {current_expiry}")
        else:
            print("⚠️ 未能提取当前到期日期")

        # 寻找外部续期按钮
        outer_renew_selector = None
        countdown_text = None
        possible_selectors = [
            'button:contains("Renew")',
            'button:contains("Renew free plan")',
            'a:contains("Renew")',
            '[class*="renew"]',
            '[class*="Renew"]',
        ]

        for selector in possible_selectors:
            try:
                if sb.is_element_visible(selector):
                    button_text = sb.get_text(selector)
                    # "Renew in HH:MM:SS" / "Renewal cooldown HH:MM:SS" 都表示冷却中, 时间为距可续期的剩余时长
                    match = re.search(r"(?:Renew in|Renewal cooldown)\s+(\d{1,2}:\d{2}:\d{2})", button_text, re.I)
                    if match:
                        countdown_text = match.group(1)
                        # 定时任务常被推迟到冷却结束前几分钟才运行, 剩余时间不长就原地等冷却结束再续, 避免每天空跑
                        if parse_countdown_seconds(countdown_text) <= MAX_COOLDOWN_WAIT:
                            print(f"⏳ 续期冷却中, 剩余 {countdown_text}, 等待冷却结束后续期...")
                            time.sleep(parse_countdown_seconds(countdown_text) + 90)
                            outer_renew_selector = selector
                            countdown_text = None
                            print("✅ 冷却已结束, 继续续期流程")
                        else:
                            print(f"⏳ 续期冷却中, 剩余 {countdown_text}, 超过等待上限, 跳过本次")
                        break
                    elif "Renew" in button_text:
                        outer_renew_selector = selector
                        print(f"✅ 续期按钮可用: '{button_text}'")
                        break
            except Exception as e:
                pass

        # 点击外部续期按钮等待弹窗
        if outer_renew_selector:
            print("🔄 点击外部续期按钮，等待验证窗口...")
            try:
                sb.sleep(2)
                sb.click(outer_renew_selector)
                sb.sleep(15)  # 等待模态框加载，可能因网络因素加载慢
            except Exception as e:
                print(f"❌ 点击外部按钮失败: {e}")
                send_telegram_message(format_notification("❌ 续期失败", error="点击外部续期按钮出错"))
                return

            # 处理弹窗中的 Turnstile
            turnstile_passed = True  # 未检测到 Turnstile 时视为无需验证
            if wait_turnstile_present(sb):
                turnstile_passed = handle_turnstile(sb)
                if not turnstile_passed:
                    print("❌ Turnstile 验证失败，尝试兜底点击...")
                    try:
                        sb.cdp.click_captcha()  # 再次尝试点击 Turnstile 验证
                        sb.sleep(4)
                        turnstile_passed = bool(turnstile_token(sb))
                    except Exception as e:
                        print(f"⚠️ 兜底点击失败: {e}")
                    # sb.save_screenshot("turnstile_failed.png")
            else:
                print("ℹ️ 未检测到 Turnstile")

            if not turnstile_passed:
                print("❌ Turnstile 验证最终未通过，脚本退出")
                send_telegram_message(format_notification("❌ 续期失败", error="Turnstile 验证未通过"))
                return

            # 点击续期按钮 (冷却刚结束时弹窗可能延迟出现, 重试几次)
            print("⏳ 等待续期按钮可用并点击...")
            time.sleep(5)

            modal_button_clicked = False
            for attempt in range(1, 4):
                try:
                    sb.click('div[role="dialog"] button.btn-primary', timeout=20)
                    modal_button_clicked = True
                    print("✅ 已点击续期按钮")
                    break
                except Exception as e:
                    print(f"⏳ 第 {attempt}/3 次未点到续期按钮, 稍后重试: {type(e).__name__}")
                    time.sleep(20)
            if not modal_button_clicked:
                print("❌ 多次尝试后仍未点到续期按钮")

            print("⏳ 等待新的过期时间...")
            sb.sleep(6)

            # 提取新的到期日期和倒计时
            try:
                new_page_text = sb.get_page_text() + "\n" + sb.get_page_source()
            except Exception:
                new_page_text = sb.get_page_source()
            try:
                after_text = sb.execute_script("return document.body.innerText.substring(0, 600)")
                print(f"📄 续期后可见文本: {after_text!r}")
            except Exception as e:
                print(f"⚠️ 读取页面文本失败: {e}")
            new_expiry = get_expiry_from_dom(sb) or extract_expiry_date(new_page_text)
            new_match = re.search(r"(?:Renewal cooldown|Renew in)\s+(\d{1,2}:\d{2}:\d{2})", new_page_text)
            if new_match:
                new_countdown = new_match.group(1)
                print(f"✅ 续期成功！新的倒计时: {new_countdown}")
                if new_expiry:
                    print(f"📅 新的到期日期: {new_expiry}")
                send_telegram_message(
                    format_notification(
                        "✅ 续期成功",
                        extra=f"⏱️ 可续期时间: {format_countdown(new_countdown)}后",
                        expiry_date=new_expiry or "（未获取到）"
                    )
                )
            else:
                if new_expiry and new_expiry != current_expiry:
                    print(f"✅ 续期成功，到期日期已更新为: {new_expiry}")
                    send_telegram_message(
                        format_notification(
                            "✅ 续期成功",
                            extra="到期日期已更新",
                            expiry_date=new_expiry
                        )
                    )
                else:
                    print("⚠️ 续期结果未知，到期日期未变化，请手动检查")
                    send_telegram_message(
                        format_notification(
                            "⚠️ 续期可能未成功",
                            extra="请登录后台检查",
                            expiry_date=current_expiry or "（未获取到）"
                        )
                    )

        else:
            if countdown_text:
                friendly = format_countdown(countdown_text)
                print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
                send_telegram_message(
                    format_notification(
                        "⏳ 未到续期时间",
                        extra=f"⏱️ 可续期时间: {friendly}后",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )
            else:
                print("ℹ️ 未找到续期按钮或倒计时，状态未知")
                send_telegram_message(
                    format_notification(
                        "ℹ️ 无需续期",
                        extra="当前状态未知，请手动检查",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )

        print("🏁 脚本执行完毕")

if __name__ == "__main__":
    main()

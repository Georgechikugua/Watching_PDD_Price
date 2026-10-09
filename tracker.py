#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价格盯盘 —— 每天抓取关注商品的价格,生成手机端可查看的页面。

用法:
    python tracker.py fetch        # 抓取全部商品价格,写入 data/prices.db
    python tracker.py dashboard    # 生成 output/index.html(手机端页面)
    python tracker.py all          # fetch + dashboard(计划任务用这个)
    python tracker.py serve        # 启动本地服务,手机浏览器打开提示的地址查看
    python tracker.py list         # 查看正在盯的商品
    python tracker.py add <url> [名称]   # 添加商品
    python tracker.py remove <url或序号>  # 移除商品

商品列表保存在 products.json,直接编辑也可以。

各平台取价方式:
- 拼多多:网页端必须登录,且只给"真浏览器"正常页面(自动化浏览器会被
  风控返回假的"售罄"页)。因此用系统安装的 Chrome 本体挂调试端口来抓取:
  先运行 `python tracker.py login` 在弹出的 Chrome 里登录一次,
  之后每天抓取自动复用,登录和抓取是同一个环境,会话稳定。
- 京东:无头浏览器渲染手机版页面取价(如被风控可 login-jd 登录后抓取)。
- 其他网站:解析页面里的 JSON-LD / og:price 标签,纯标准库,无需登录。
"""

import argparse
import json
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "products.json"
DATA_DIR = BASE / "data"
DB_PATH = DATA_DIR / "prices.db"
OUT_DIR = BASE / "output"
PROFILE_DIR = DATA_DIR / "browser-profile"        # 拼多多登录态存放处
PROFILE_JD_DIR = DATA_DIR / "browser-profile-jd"  # 京东登录态存放处
CDP_PROFILE = DATA_DIR / "browser-profile-cdp"    # 真实 Chrome 专用配置(拼多多)
CDP_PORT = 9223
DEBUG_DIR = DATA_DIR / "debug"
HISTORY_DAYS = 120          # 图表最多回看的天数
FETCH_TIMEOUT = 20          # 单个页面超时(秒)
MAX_PAGE_BYTES = 3 * 1024 * 1024

UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")


# ---------------------------------------------------------------- 基础设施

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS products(
            sku        TEXT PRIMARY KEY,
            url        TEXT NOT NULL,
            name       TEXT,
            site       TEXT,
            last_ok    TEXT,
            last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS prices(
            sku    TEXT NOT NULL,
            day    TEXT NOT NULL,
            ts     TEXT NOT NULL,
            price  REAL NOT NULL,
            source TEXT,
            PRIMARY KEY(sku, day)
        );
    """)
    return conn


def load_config():
    if CONFIG.exists():
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    else:
        data = {"products": []}
    return data


def save_config(data):
    CONFIG.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def detect(url):
    """识别站点和商品标识。返回 (site, sku)。"""
    if re.search(r"(?:yangkeduo|pinduoduo)\.com", url):
        m = (re.search(r"goods_sign=([A-Za-z0-9_-]+)", url)
             or re.search(r"goods_id=(\d+)", url))
        if m:
            return "pdd", m.group(1)
        return "pdd", url          # ps= 分享码等,抓取时跟随跳转后再归一化
    m = re.search(r"item\.(?:m\.)?jd\.com/(?:product/)?(\d+)\.html", url)
    if m:
        return "jd", m.group(1)
    return "generic", url


def resolve_pdd_url(url):
    """把 ps= 分享码等短链解析成只带商品标识的规范链接。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA_MOBILE},
                                     method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            final = str(resp.url)
    except OSError:
        return url
    m = (re.search(r"goods_sign=([A-Za-z0-9_-]+)", final)
         or re.search(r"goods_id=(\d+)", final))
    if m:
        key = f"goods_sign={m.group(1)}" if "sign" in m.group(0) else f"goods_id={m.group(1)}"
        return f"https://mobile.yangkeduo.com/goods.html?{key}"
    return url


# ---------------------------------------------------------------- 通用抓取

class MetaParser(HTMLParser):
    """收集 meta 标签、title 和 JSON-LD 块,不执行任何页面内容。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metas = []
        self.jsonld_raw = []
        self.title = None
        self._in_jsonld = False
        self._buf = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            self.metas.append(a)
        elif tag == "script" and "ld+json" in (a.get("type") or ""):
            self._in_jsonld = True
            self._buf = []
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "script" and self._in_jsonld:
            self.jsonld_raw.append("".join(self._buf))
            self._in_jsonld = False
        elif tag == "title" and self._in_title:
            self._in_title = False

    def handle_data(self, data):
        if self._in_jsonld:
            self._buf.append(data)
        elif self._in_title:
            self.title = (self.title or "") + data


def _valid_price(v):
    try:
        p = float(str(v).replace(",", "").replace("¥", "").strip())
    except (TypeError, ValueError):
        return None
    return p if 0.5 <= p <= 5_000_000 else None


def _price_from_jsonld(node):
    """在 JSON-LD 节点里找 Product 的价格,深度有限防注入。"""
    if isinstance(node, list):
        for n in node:
            p = _price_from_jsonld(n)
            if p:
                return p
        return None
    if not isinstance(node, dict):
        return None
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    if "Product" in (types or []):
        offers = node.get("offers") or {}
        offers = offers if isinstance(offers, list) else [offers]
        for o in offers:
            if not isinstance(o, dict):
                continue
            p = _valid_price(o.get("price"))
            if p:
                return p
            spec = o.get("priceSpecification")
            if isinstance(spec, dict):
                p = _valid_price(spec.get("price"))
                if p:
                    return p
    return _price_from_jsonld(node.get("@graph"))


def fetch_generic(url):
    """解析任意商品页的 JSON-LD / og:price 标签。返回 (price, title, error)。"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA_PC,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            if not str(resp.url).startswith(("http://", "https://")):
                return None, None, "拒绝非 http(s) 地址"
            raw = resp.read(MAX_PAGE_BYTES + 1)
            if len(raw) > MAX_PAGE_BYTES:
                return None, None, "页面超过大小上限"
            charset = resp.headers.get_content_charset() or "utf-8"
            html = raw.decode(charset, errors="replace")
    except Exception as e:
        return None, None, f"请求失败: {e}"

    p = MetaParser()
    try:
        p.feed(html)
    except Exception:
        pass  # 解析失败就只用已收集到的部分

    price = None
    for raw in p.jsonld_raw:
        try:
            price = _price_from_jsonld(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            continue
        if price:
            break
    if not price:
        for key in ("og:price:amount", "product:price:amount", "price"):
            for m in p.metas:
                prop = m.get("property") or m.get("itemprop") or m.get("name")
                if prop == key:
                    price = _valid_price(m.get("content"))
                    if price:
                        break
            if price:
                break

    title = None
    for m in p.metas:
        if (m.get("property") or "") == "og:title":
            title = m.get("content")
            break
    title = (title or p.title or "").strip()[:120] or None

    if not price:
        return None, title, "页面里没找到价格(可能需要登录或由脚本动态加载)"
    return price, title, None


# ---------------------------------------------------------------- 京东抓取

PW = None
try:
    from playwright.sync_api import sync_playwright
    PW = True
except ImportError:
    PW = None

_CHANNEL = None
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"


def best_channel(pw):
    """优先用系统安装的真实 Chrome/Edge:过电商平台风控比测试版 Chromium 稳。"""
    global _CHANNEL
    if _CHANNEL is not None:
        return _CHANNEL
    for ch in ("chrome", "msedge"):
        try:
            b = pw.chromium.launch(channel=ch, headless=True)
            b.close()
            _CHANNEL = ch
            return ch
        except Exception:
            continue
    _CHANNEL = ""
    return ""


def launch_ctx(pw, profile, headless, mobile, ua, viewport, offscreen=False,
               window_at=None):
    kwargs = dict(user_agent=ua, viewport=viewport, locale="zh-CN",
                  args=STEALTH_ARGS)
    if mobile:
        kwargs.update(is_mobile=True, has_touch=True)
    # 无头浏览器会被拼多多风控降级成“售罄”假页面,所以 PDD 抓取用有头模式,
    # 窗口挪到屏幕外,不干扰使用
    if offscreen:
        kwargs["args"] = kwargs["args"] + ["--window-position=-32000,-32000"]
    elif window_at:
        kwargs["args"] = kwargs["args"] + [f"--window-position={window_at}"]
    profile.mkdir(parents=True, exist_ok=True)
    ch = best_channel(pw)
    if ch:
        kwargs["channel"] = ch
    return pw.chromium.launch_persistent_context(str(profile),
                                                 headless=headless, **kwargs)


# --------------------------- 真实 Chrome(调试端口)方案 ---------------------------
# 拼多多能识别 Playwright 启动的浏览器(哪怕真实 Chrome 通道)并返回假“售罄”页。
# 唯一稳定的方法:用系统安装的 Chrome 本体挂 --remote-debugging-port,
# 工具只通过 CDP 读页面,浏览器进程不带任何自动化启动参数。

def find_chrome():
    import os
    import shutil
    exe = shutil.which("chrome")
    if exe:
        return exe
    cands = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for c in cands:
        if Path(c).exists():
            return str(c)
    return None


def start_cdp_chrome(url=None, offscreen=True, window_at=None):
    """启动带调试端口的真实 Chrome,返回进程句柄;已运行则复用。"""
    import subprocess
    import time
    import urllib.request as ur

    chrome = find_chrome()
    if not chrome:
        return None, "本机没找到 Chrome,请安装 Google Chrome 后重试"
    args = [chrome,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={CDP_PROFILE}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-session-crashed-bubble"]
    if offscreen:
        args.append("--window-position=-32000,-32000")
    elif window_at:
        args.append(f"--window-position={window_at}")
    if url:
        args.append(url)
    # 端口上已有 Chrome 在跑就直接复用
    try:
        with ur.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2):
            return None, None
    except OSError:
        pass
    proc = subprocess.Popen(args)
    for _ in range(60):
        time.sleep(0.5)
        try:
            with ur.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2):
                return proc, None
        except OSError:
            continue
    return proc, "Chrome 调试端口启动超时"


def stop_cdp_chrome(proc):
    import subprocess
    if proc:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        except Exception:
            pass


def subprocess_run_taskkill(proc):
    stop_cdp_chrome(proc)


def cdp_context(pw):
    """连接调试端口上的 Chrome,返回 (browser, 默认上下文)。"""
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    return browser, ctx


JD_SHELL_TITLES = {"就是便宜", "京东", "京东登录", "登录"}


def fetch_jd(skus):
    """带登录态渲染京东手机版商品页取价。返回 {sku: (price, title, error)}。"""
    result = {sku: (None, None, None) for sku in skus}
    if not skus:
        return result
    if not PW:
        for sku in skus:
            result[sku] = (None, None, "未安装 playwright(pip install playwright 后运行 "
                                       "python -m playwright install chromium)")
        return result

    PROFILE_JD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            ctx = launch_ctx(pw, PROFILE_JD_DIR, headless=True, mobile=True,
                             ua=UA_MOBILE, viewport={"width": 390, "height": 844})
            page = ctx.new_page()
            page.add_init_script(STEALTH_JS)
            for sku in skus:
                try:
                    page.goto(f"https://item.m.jd.com/product/{sku}.html",
                              timeout=35_000, wait_until="domcontentloaded")
                    page.wait_for_timeout(4_000)
                    if "login" in page.url or "passport" in page.url:
                        result[sku] = (None, None, "京东需要登录,请在电脑上运行: "
                                                   "python tracker.py login --jd")
                        continue
                    title = None
                    t = page.query_selector("meta[property='og:title']")
                    if t:
                        title = (t.get_attribute("content") or "").strip()[:120] or None
                    if not title:
                        h = page.query_selector("h1")
                        title = (h.inner_text() if h else "").strip()[:120] or None
                    if not title or title in JD_SHELL_TITLES or "登录" in title:
                        result[sku] = (None, None, "京东需要登录(页面被重定向),"
                                                   "请运行: python tracker.py login --jd")
                        continue

                    price = None
                    for sel in (".price.J-p-" + sku, "#jd-price",
                                ".summary-price .p-price span:nth-child(2)",
                                ".p-price span:nth-child(2)", "span.price"):
                        el = page.query_selector(sel)
                        if el:
                            txt = re.sub(r"[^\d.]", "", el.inner_text())
                            price = _valid_price(txt) if txt else None
                            if price:
                                break
                    if not price:
                        # 渲染完成后价格不打码,匹配 JSON 形态
                        m = re.search(r'"p"\s*:\s*"?(\d{2,7}(?:\.\d{1,2})?)"?',
                                      page.content())
                        if m:
                            price = _valid_price(m.group(1))
                    if price:
                        result[sku] = (price, title, None)
                    else:
                        _dump_debug(page, sku)
                        result[sku] = (None, title, "页面已加载但没解析到价格,已存调试快照")
                except Exception as e:
                    result[sku] = (None, None, f"浏览器抓取失败: {e}")
            ctx.close()
    except Exception as e:
        for sku in skus:
            if result[sku][2] is None and result[sku][0] is None:
                result[sku] = (None, None, f"浏览器启动失败: {e}")
    return result


# ---------------------------------------------------------------- 拼多多抓取

PDD_DEAD_MARKERS = ("商品已售罄", "商品不存在", "已下架", "推荐以下相似商品")
PDD_DEFAULT_TITLES = {"拼多多商城", "拼多多", ""}


def _pdd_price_from_page(page, sku, body_text=""):
    """从已渲染的拼多多商品页提取 (price, title)。多层策略,失败返回 (None, title)。"""
    title = None
    t = page.query_selector("meta[property='og:title']")
    if t:
        title = (t.get_attribute("content") or "").strip()[:120] or None
    if title in PDD_DEFAULT_TITLES:
        # og:title 是通用的“拼多多商城”,真实商品名在正文元素里
        try:
            el = page.query_selector(".enable-select")
            if el:
                nm = (el.inner_text() or "").strip()[:120]
                if len(nm) >= 8:
                    title = nm
        except Exception:
            pass

    real_page = bool(title) and title not in PDD_DEFAULT_TITLES

    # 层0:aria-label 里有完整价格(拼多多把价格拆成多个小 span 防爬,
    # 但无障碍标签是完整的,如 "首件¥2051.88")
    try:
        labels = page.eval_on_selector_all(
            "[aria-label]", "els => els.map(e => e.getAttribute('aria-label') || '')")
        hits = []
        for lab in labels:
            for m in re.findall(r"[¥￥]\s*(\d{2,7}(?:\.\d{1,2})?)", lab.replace(",", "")):
                p = _valid_price(m)
                if p and p >= 30:
                    hits.append(p)
        if hits:
            return min(hits), title
    except Exception:
        pass

    if real_page:
        # 层1:常见价格元素
        for sel in (".goods-price", "[class*='price']", ".price"):
            for el in page.query_selector_all(sel)[:15]:
                txt = el.inner_text() or ""
                m = re.search(r"(\d{2,7}(?:\.\d{1,2})?)", txt.replace(",", ""))
                if m:
                    p = _valid_price(m.group(1))
                    if p:
                        return p, title

        # 层2:正文开头的 ¥ 数字(推荐流在页面后方,不采纳;
        # 先清掉拼多多页面开头大量零宽字符)
        text = re.sub(r"[\u200b\u200c\u200d\ufeff\u00a0]", "",
                      (body_text or page.inner_text("body")))[:4000]
        hits = [_valid_price(x) for x in
                re.findall(r"[¥￥]\s*(\d{2,7}(?:\.\d{1,2})?)", text.replace(",", ""))]
        hits = [h for h in hits if h and h >= 30]
        if hits:
            return min(hits), title
    return None, title


def fetch_pdd(items):
    """用系统真实 Chrome(调试端口)渲染拼多多商品页。
    items: [(sku, url)]。返回 {sku: (price, title, error, canonical_sku)}。"""
    result = {sku: (None, None, None, None) for sku, _ in items}
    if not items:
        return result
    if not PW:
        for sku, _ in items:
            result[sku] = (None, None, "未安装 playwright,无法连接 Chrome", None)
        return result
    proc, err = start_cdp_chrome(offscreen=True)
    if err:
        for sku, _ in items:
            result[sku] = (None, None, err, None)
        return result

    with sync_playwright() as pw:
        browser = None
        try:
            browser, ctx = cdp_context(pw)
            page = ctx.new_page()
            for sku, url in items:
                try:
                    page.goto(url, timeout=45_000, wait_until="domcontentloaded")
                    page.wait_for_timeout(6_000)
                    if "login" in page.url:
                        result[sku] = (None, None,
                                       "拼多多登录已失效,请在电脑上运行: python tracker.py login",
                                       None)
                        continue
                    m = (re.search(r"goods_sign=([A-Za-z0-9_-]+)", page.url)
                         or re.search(r"goods_id=(\d+)", page.url))
                    canonical = m.group(1) if m else sku
                    try:
                        body_text = page.inner_text("body")
                    except Exception:
                        body_text = ""
                    if any(k in body_text[:600] for k in PDD_DEAD_MARKERS):
                        result[sku] = (None, None, "商品已售罄或已下架,换一个在售的链接吧",
                                       canonical)
                        continue
                    price, title = _pdd_price_from_page(page, sku, body_text)
                    if price:
                        result[sku] = (price, title, None, canonical)
                    else:
                        _dump_debug(page, sku)
                        result[sku] = (None, title, "页面已打开但没解析到价格,已存调试快照",
                                       canonical)
                except Exception as e:
                    result[sku] = (None, None, f"浏览器抓取失败: {e}", None)
            try:
                page.close()
            except Exception:
                pass
        except Exception as e:
            for sku, _ in items:
                if result[sku][2] is None:
                    result[sku] = (None, None, f"连接 Chrome 失败: {e}", None)
        finally:
            if browser:
                try:
                    browser.close()   # 只关闭专用配置的 Chrome,不影响日常浏览器
                except Exception:
                    pass
    if proc:
        try:
            proc.wait(timeout=5)
        except Exception:
            subprocess_run_taskkill(proc)
    return result


def _dump_debug(page, sku):
    """解析失败时保存现场,便于排查反爬变化。"""
    try:
        d = DEBUG_DIR / sku
        d.mkdir(parents=True, exist_ok=True)
        (d / "page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(d / "shot.png"))
    except Exception:
        pass


PDD_LOGIN_COOKIES = {"pdd_user_id", "pdd_user_uid", "pdd_uid"}


def _has_login_cookie(ctx, platform):
    """判断对应平台的浏览器配置里是否已有真实登录态。"""
    if platform == "pdd":
        names = {c["name"] for c in ctx.cookies("https://mobile.yangkeduo.com")}
        return bool(names & PDD_LOGIN_COOKIES)
    names = {c["name"] for c in ctx.cookies("https://www.jd.com")}
    return "pt_key" in names or "thor" in names


def cmd_login(platform="pdd"):
    """打开浏览器窗口让用户登录。拼多多走系统真实 Chrome(风控不识别),
    京东走 Playwright 启动的浏览器。登录态长期复用。"""
    if not PW:
        print("未安装 playwright,无法登录。先运行:")
        print("  pip install playwright && python -m playwright install chromium")
        return

    if platform == "pdd":
        proc, err = start_cdp_chrome(url="https://mobile.yangkeduo.com/login.html",
                                     offscreen=False, window_at="60,80")
        if err:
            print(err)
            return
        print("已在 Chrome(专用配置)里打开拼多多登录页,请用你平时的方式登录")
        print("(手机验证码/扫码都可以)。登录成功后窗口会自动关闭,最多等 5 分钟。")
        import time
        import urllib.request as ur

        def cdp_alive():
            try:
                ur.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2)
                return True
            except OSError:
                return False

        ok = False
        start_time = datetime.now()
        with sync_playwright() as pw:
            browser = ctx = None
            while (datetime.now() - start_time).total_seconds() <= 300:
                if browser is None and not cdp_alive():
                    print("窗口被关闭,本次登录中止。")
                    break
                if browser is None:
                    try:
                        browser, ctx = cdp_context(pw)
                    except Exception:
                        time.sleep(1)
                        continue
                try:
                    if _has_login_cookie(ctx, "pdd"):
                        ok = True
                        break
                except Exception:
                    if not cdp_alive():
                        print("窗口被关闭,本次登录中止。")
                        break
                time.sleep(1)
            if ok and browser:
                browser.close()          # 登录完成,关闭专用 Chrome
        if ok:
            print("登录成功,之后每天自动抓取会复用这个登录态。")
        else:
            print("5 分钟内没有检测到登录态,本次不算登录。"
                  "请再运行一次 python tracker.py login 重试。")
        if proc:
            try:
                proc.wait(timeout=5)
            except Exception:
                stop_cdp_chrome(proc)
        return

    # ---- 京东:Playwright 启动的浏览器 ----
    profile, start, tip = (PROFILE_JD_DIR,
                           "https://passport.jd.com/new/login.aspx",
                           "京东(账号密码或扫码)")
    with sync_playwright() as pw:
        # 登录窗口不用移动端模拟:桌面鼠标事件才能拖动京东的滑块验证
        ctx = launch_ctx(pw, profile, headless=False,
                         mobile=(platform == "jd"),
                         ua=UA_PC,
                         viewport={"width": 1100, "height": 800},
                         window_at="60,80")
        page = ctx.new_page()
        page.add_init_script(STEALTH_JS)
        try:
            page.goto(start, timeout=45_000)
            print(f"请在弹出的浏览器窗口里登录{tip}……")
            print("登录成功后窗口会自动关闭,最多等 5 分钟。")
            ok = False
            start_time = datetime.now()
            while True:
                try:
                    page.wait_for_timeout(1_000)
                except Exception:
                    print("窗口被关闭,本次登录中止。")
                    return
                if _has_login_cookie(ctx, platform):
                    ok = True
                    page.wait_for_timeout(2_000)   # 等会话完全写好
                    break
                if (datetime.now() - start_time).total_seconds() > 300:
                    break
        except Exception as e:
            print(f"登录窗口异常退出:{e}")
            return
        finally:
            try:
                ctx.close()
            except Exception:
                pass
        if ok:
            print("登录成功,之后每天自动抓取会复用这个登录态。")
        else:
            print("5 分钟内没有检测到登录态(没有拿到登录 Cookie),本次不算登录。")
            print("请再运行一次 python tracker.py login-jd 重试。")




# ---------------------------------------------------------------- 主流程

def cmd_fetch():
    cfg = load_config()
    items = cfg.get("products", [])
    if not items:
        log("products.json 里还没有商品,先用 `python tracker.py add <商品页链接>` 添加。")
        return

    conn = db()
    today = datetime.now().strftime("%Y-%m-%d")
    jd_skus, pdd_items, entry_by_sku = [], [], {}
    for it in items:
        site, sku = detect(it["url"])
        conn.execute(
            "INSERT INTO products(sku,url,name,site) VALUES(?,?,?,?) "
            "ON CONFLICT(sku) DO UPDATE SET url=excluded.url, "
            "name=COALESCE(NULLIF(excluded.name,''), products.name)",
            (sku, it["url"], (it.get("name") or "").strip(), site))
        conn.commit()
        if site == "jd":
            jd_skus.append(sku)
        elif site == "pdd":
            pdd_items.append((sku, it["url"]))
        entry_by_sku[sku] = it

    results, canonical = {}, {}
    for it in items:
        site, sku = detect(it["url"])
        if site == "generic":
            results[sku] = fetch_generic(it["url"])
    for sku, (price, title, err) in fetch_jd(jd_skus).items():
        results[sku] = (price, title, err)
    for sku, (price, title, err, can) in fetch_pdd(pdd_items).items():
        results[sku] = (price, title, err)
        if can and can != sku:
            canonical[sku] = can

    # 短链归一化:记录合并到 goods_sign 对应的正式 sku 下
    for old, new in canonical.items():
        conn.execute("UPDATE OR IGNORE prices SET sku=? WHERE sku=?", (new, old))
        conn.execute("INSERT INTO products(sku,url,site) VALUES(?,?,?) "
                     "ON CONFLICT(sku) DO NOTHING", (new, entry_by_sku[old]["url"], "pdd"))
        conn.execute("DELETE FROM prices WHERE sku=?", (old,))
        conn.execute("DELETE FROM products WHERE sku=?", (old,))
        entry_by_sku[new] = entry_by_sku.pop(old)
        results[new] = results.pop(old)
        conn.commit()

    ok = fail = 0
    for sku, (price, title, err) in results.items():
        name_in_cfg = (entry_by_sku[sku].get("name") or "").strip()
        name = name_in_cfg or title
        if price:
            conn.execute(
                "INSERT INTO prices(sku,day,ts,price,source) VALUES(?,?,?,?,?) "
                "ON CONFLICT(sku,day) DO UPDATE SET ts=excluded.ts, price=excluded.price, "
                "source=excluded.source",
                (sku, today, now_str(), price, "jd-browser" if sku in jd_skus else "page-meta"))
            conn.execute("UPDATE products SET name=COALESCE(?,name), last_ok=?, last_error=NULL "
                         "WHERE sku=?", (name, now_str(), sku))
            ok += 1
            shown = name or sku
            log(f"✓ {shown}  ¥{price:,.2f}")
        else:
            conn.execute("UPDATE products SET name=COALESCE(?,name), last_error=? WHERE sku=?",
                         (name, err, sku))
            fail += 1
            log(f"✗ {name or sku}  {err}")
        conn.commit()
    conn.close()
    log(f"完成:成功 {ok} 个,失败 {fail} 个。")


def cmd_dashboard():
    conn = db()
    items = load_config().get("products", [])
    since = (datetime.now() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    data, pending = [], []
    for it in items:
        site, sku = detect(it["url"])
        prow = conn.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
        name = (it.get("name") or (prow["name"] if prow else None) or sku)
        rows = conn.execute(
            "SELECT day, ts, price FROM prices WHERE sku=? AND day>=? ORDER BY day",
            (sku, since)).fetchall()
        if not rows:
            pending.append({"name": name, "url": it["url"]})
            continue
        history = [{"d": r["day"], "p": round(r["price"], 2)} for r in rows]
        prices = [h["p"] for h in history]
        cur = prices[-1]
        data.append({
            "sku": sku, "url": it["url"], "name": name,
            "error": prow["last_error"] if prow else None,
            "lastOk": prow["last_ok"] if prow else None,
            "updated": rows[-1]["ts"],
            "history": history,
            "cur": cur, "min": min(prices), "max": max(prices),
            "atMin": len(prices) >= 3 and cur == min(prices),
            "prev": prices[-2] if len(prices) > 1 else None,
            "days": len(prices),
        })
    conn.close()

    OUT_DIR.mkdir(exist_ok=True)
    html = render_html(data, pending)
    out = OUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    log(f"已生成 {out}")


def cmd_serve(port):
    out = OUT_DIR / "index.html"
    if not out.exists():
        cmd_dashboard()
    import http.server
    import socket

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(OUT_DIR), **kw)

        def end_headers(self):  # 局域网内直接打开,禁用缓存保证看到最新数据
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    with http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler) as httpd:
        ips = {httpd.server_address[0]}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
        print(f"服务已启动,手机连同一个 Wi-Fi,浏览器打开:")
        for ip in sorted(ips):
            if not ip.startswith("127."):
                print(f"  http://{ip}:{port}")
        print("按 Ctrl+C 停止。")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")


def cmd_list():
    cfg = load_config()
    items = cfg.get("products", [])
    if not items:
        print("还没有商品。用:python tracker.py add <商品页链接>")
        return
    conn = db()
    for i, it in enumerate(items, 1):
        site, sku = detect(it["url"])
        row = conn.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
        name = (it.get("name") or (row["name"] if row else None) or sku)
        if row and row["last_ok"]:
            state = f"上次成功 {row['last_ok']}"
        elif row and row["last_error"]:
            state = f"从未成功({row['last_error']})"
        else:
            state = "尚未抓取,运行 python tracker.py all"
        print(f"{i}. [{site}] {name}\n   {it['url']}\n   {state}")
    conn.close()


def cmd_add(url, name):
    site, sku = detect(url)
    if site == "pdd":
        url = resolve_pdd_url(url)
        site, sku = detect(url)
    elif not site:
        print("暂不支持识别这个链接的站点,仍会按普通商品页尝试。")
        site, sku = "generic", url
    cfg = load_config()
    if any(detect(x["url"])[1] == sku for x in cfg["products"]):
        print("这个商品已经在列表里了。")
        return
    cfg["products"].append({"url": url, **({"name": name} if name else {})})
    save_config(cfg)
    print(f"已添加:{name or url}")


def cmd_remove(key):
    cfg = load_config()
    items = cfg["products"]
    for i, it in enumerate(items):
        site, sku = detect(it["url"])
        if str(i + 1) == str(key) or it["url"] == key or sku == key:
            removed = items.pop(i)
            save_config(cfg)
            conn = db()
            conn.execute("DELETE FROM prices WHERE sku=?", (sku,))
            conn.execute("DELETE FROM products WHERE sku=?", (sku,))
            conn.commit()
            conn.close()
            print(f"已移除:{removed['url']}")
            return
    print(f"没找到 {key},用 list 命令查看编号。")


def cmd_publish():
    """把 output/index.html 发布到 GitHub Pages(配置在 data/.github-token 与
    data/.github-repo)。失败只记日志,不影响抓取主流程。"""
    html_path = OUT_DIR / "index.html"
    if not html_path.exists():
        cmd_dashboard()
    token_f, repo_f = DATA_DIR / ".github-token", DATA_DIR / ".github-repo"
    if not token_f.exists() or not repo_f.exists():
        log("未配置 GitHub 发布(缺少 token/仓库名),跳过。")
        return
    try:
        import base64
        import urllib.request

        token = token_f.read_text().strip()
        repo = repo_f.read_text().strip()

        def gh(path, method="GET", body=None):
            req = urllib.request.Request(
                f"https://api.github.com{path}", method=method,
                data=json.dumps(body).encode() if body else None,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"})
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                try:
                    return e.code, json.loads(e.read() or b"{}")
                except Exception:
                    return e.code, {}
            except OSError as e:
                return 0, {"message": str(e)}

        code, d = gh(f"/repos/{repo}/contents/index.html")
        body = {"message": f"价格盯盘更新 {now_str()}",
                "content": base64.b64encode(html_path.read_bytes()).decode(),
                "branch": "main"}
        if code == 200 and d.get("sha"):
            body["sha"] = d["sha"]
        code, d = gh(f"/repos/{repo}/contents/index.html", "PUT", body)
        owner, name = repo.split("/")
        url = f"https://{owner.lower()}.github.io/{name}/"
        if code in (200, 201):
            log(f"已发布到 GitHub Pages: {url}")
        else:
            log(f"GitHub 发布失败({code}): {d.get('message', '')}")
    except Exception as e:
        log(f"GitHub 发布异常,跳过: {e}")


# ---------------------------------------------------------------- 页面渲染

def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_price(v):
    return f"¥{v:,.0f}" if v == int(v) else f"¥{v:,.2f}"


def sparkline(history, lo, hi):
    """生成近 90 天价格走势的 SVG 折线,标注最低点。"""
    pts = history[-90:]
    if len(pts) < 2:
        return ""
    W, H, PAD = 300, 64, 8
    span = (hi - lo) or 1
    xs = [PAD + i * (W - 2 * PAD) / (len(pts) - 1) for i in range(len(pts))]
    ys = [H - PAD - (p["p"] - lo) / span * (H - 2 * PAD) for p in pts]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    mi = pts.index(min(pts, key=lambda p: p["p"]))
    area = f"M{xs[0]:.1f},{H - PAD} L{line.replace(' ', ' L')} L{xs[-1]:.1f},{H - PAD} Z"
    dot = (f'<circle cx="{xs[mi]:.1f}" cy="{ys[mi]:.1f}" r="3.2" class="dot"/>'
           f'<text x="{min(xs[mi] + 6, W - 46)}" y="{ys[mi] - 6:.1f}" class="dotlabel">'
           f'最低 {fmt_price(pts[mi]["p"])}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">'
            f'<path d="{area}" class="area"/><polyline points="{line}" class="line"/>{dot}</svg>')


def product_html(p):
    diff = tag = ""
    if p["prev"] is not None and p["prev"] != p["cur"]:
        d = p["cur"] - p["prev"]
        cls = "drop" if d < 0 else "rise"
        arrow = "↓" if d < 0 else "↑"
        pct = abs(d) / p["prev"] * 100
        diff = (f'<p class="change {cls}">{arrow} 比上次记录 {abs(d):,.0f} 元'
                f'<span class="pct">({pct:.1f}%)</span></p>')
    if p["atMin"]:
        tag = '<span class="tag">近 {!s} 天最低</span>'.format(min(p["days"], HISTORY_DAYS))

    err = ""
    if p["error"]:
        head = esc(p["error"]).rstrip(" :：,，")
        fixcmd = ""
        if "python tracker.py" in p["error"]:
            head, tail = head.split("python tracker.py", 1)
            fixcmd = (f'<p class="sub"><code class="fixcmd">python tracker.py'
                      f'{esc(tail)}</code></p>')
        err = (f'<p class="fetcherr">今天没抓到价格:{head}</p>{fixcmd}'
               f'<p class="sub">以上是上次成功({esc((p["updated"] or "")[:10])})的记录。</p>')

    return f"""<section class="product">
  <h2><a href="{esc(p['url'])}" target="_blank" rel="noopener">{esc(p['name'])}</a>{tag}</h2>
  <p class="price">{fmt_price(p['cur'])}</p>
  {diff}{err}
  {sparkline(p['history'], p['min'], p['max'])}
  <p class="meta">最低 <b>{fmt_price(p['min'])}</b><i>·</i>最高 <b>{fmt_price(p['max'])}</b>
   <i>·</i>记录 {p['days']} 天<i>·</i>更新 {esc((p['updated'] or '')[5:10])}</p>
</section>"""


def pending_html(p):
    return f"""<section class="product pending">
  <h2><a href="{esc(p['url'])}" target="_blank" rel="noopener">{esc(p['name'])}</a></h2>
  <p class="sub">还没有抓取记录。在电脑上运行一次 <code>python tracker.py all</code> 就会记下第一笔。</p>
</section>"""


def render_html(data, pending=None):
    pending = pending or []
    data.sort(key=lambda p: p["atMin"], reverse=True)
    drops = sum(1 for p in data if p["prev"] and p["cur"] < p["prev"])
    rises = sum(1 for p in data if p["prev"] and p["cur"] > p["prev"])
    at_min = sum(1 for p in data if p["atMin"])
    today = f"{datetime.now():%m月%d日 %A}".replace("Monday", "周一").replace("Tuesday", "周二") \
        .replace("Wednesday", "周三").replace("Thursday", "周四").replace("Friday", "周五") \
        .replace("Saturday", "周六").replace("Sunday", "周日")

    if data or pending:
        summary = f"盯着 {len(data) + len(pending)} 件商品"
        bits = []
        if drops:
            bits.append(f"{drops} 件今天更便宜")
        if rises:
            bits.append(f"{rises} 件涨价")
        if at_min:
            bits.append(f"{at_min} 件处于近期最低")
        if bits:
            summary += ":" + "、".join(bits)
        body = ("\n".join(product_html(p) for p in data)
                + "\n".join(pending_html(p) for p in pending))
    else:
        summary = "还没有在盯的商品"
        body = ('<section class="empty"><p>打开 <b>products.json</b>,'
                '把想盯的商品页链接加进去;或在电脑上运行:<br>'
                '<code>python tracker.py add 商品页链接</code></p></section>')

    built = f"{datetime.now():%Y-%m-%d %H:%M}"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#fafaf8">
<title>价格盯盘</title>
<style>
  :root {{
    --paper:#fafaf8; --ink:#20242b; --muted:#878d97; --rule:#e5e2da;
    --navy:#2b4a72; --drop:#2e7d5b; --rise:#b3402e;
    --sans:-apple-system,"PingFang SC","HarmonyOS Sans SC","Microsoft YaHei",sans-serif;
    --figure:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Times New Roman",serif;
  }}
  * {{ box-sizing:border-box; margin:0; }}
  body {{ background:var(--paper); color:var(--ink);
         font:16px/1.6 var(--sans); padding:0 20px 48px; max-width:36rem; margin:0 auto; }}
  header {{ padding:28px 0 20px; border-bottom:2px solid var(--ink); }}
  h1 {{ font-size:22px; font-weight:650; letter-spacing:.02em; }}
  header time {{ float:right; color:var(--muted); font-size:13px; padding-top:6px; }}
  .summary {{ color:var(--navy); margin-top:6px; font-size:15px; }}
  .product {{ padding:22px 0 18px; border-bottom:1px solid var(--rule); }}
  .product h2 {{ font-size:16px; font-weight:600; line-height:1.45; }}
  .product h2 a {{ color:inherit; text-decoration:none; }}
  .product h2 a:focus-visible {{ outline:2px solid var(--navy); outline-offset:3px; }}
  .tag {{ display:inline-block; margin-left:8px; padding:1px 9px; border:1px solid var(--drop);
          color:var(--drop); border-radius:99px; font-size:12px; font-weight:500;
          vertical-align:2px; white-space:nowrap; }}
  .price {{ font-family:var(--figure); font-size:38px; font-weight:600; line-height:1.15;
            margin-top:6px; font-variant-numeric:tabular-nums; }}
  .change {{ font-size:14px; margin-top:2px; }}
  .change.drop {{ color:var(--drop); }} .change.rise {{ color:var(--rise); }}
  .change .pct {{ color:var(--muted); margin-left:6px; }}
  .fetcherr {{ color:var(--rise); font-size:13px; margin-top:4px; }}
  .sub {{ color:var(--muted); font-size:13px; }}
  .fixcmd {{ background:#efede7; padding:2px 8px; border-radius:4px;
             white-space:nowrap; font-size:12px; }}
  svg {{ width:100%; height:64px; margin-top:12px; }}
  svg .line {{ fill:none; stroke:var(--navy); stroke-width:1.6;
               vector-effect:non-scaling-stroke; }}
  svg .area {{ fill:var(--navy); opacity:.07; }}
  svg .dot {{ fill:var(--drop); }}
  svg .dotlabel {{ font:11px var(--sans); fill:var(--drop);
                   stroke:var(--paper); stroke-width:3px; paint-order:stroke;
                   stroke-linejoin:round; }}
  .meta {{ color:var(--muted); font-size:13px; margin-top:8px; }}
  .meta b {{ font-weight:600; color:#4a5058; font-variant-numeric:tabular-nums; }}
  .meta i {{ font-style:normal; margin:0 7px; color:var(--rule); }}
  .empty {{ padding:48px 0; color:var(--muted); }}
  .empty code {{ background:#efede7; padding:2px 8px; border-radius:4px; font-size:14px; }}
  footer {{ color:var(--muted); font-size:13px; padding-top:20px; }}
  @media (prefers-reduced-motion: no-preference) {{
    .price, .tag {{ transition:none; }}
  }}
</style>
</head>
<body>
<header><time>{today}</time><h1>价格盯盘</h1>
<p class="summary">{esc(summary)}</p></header>
<main>
{body}
</main>
<footer>数据更新于 {built}。想盯别的商品,编辑 products.json 或运行
<code>python tracker.py add 链接</code>。</footer>
</body>
</html>"""


# ---------------------------------------------------------------- 入口

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="每日商品价格盯盘")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="抓取全部商品价格")
    sub.add_parser("dashboard", help="生成手机端页面")
    sub.add_parser("publish", help="发布页面到 GitHub Pages")
    sub.add_parser("all", help="fetch + dashboard + publish")
    sub.add_parser("login", help="扫码登录拼多多(一次性,登录态长期复用)")
    sub.add_parser("login-jd", help="登录京东(商品页取价用,可选)")
    p_serve = sub.add_parser("serve", help="启动本地服务供手机查看")
    p_serve.add_argument("--port", type=int, default=8788)
    sub.add_parser("list", help="列出正在盯的商品")
    p_add = sub.add_parser("add", help="添加商品")
    p_add.add_argument("url")
    p_add.add_argument("name", nargs="?")
    p_rm = sub.add_parser("remove", help="移除商品(链接或 list 里的序号)")
    p_rm.add_argument("key")
    args = ap.parse_args()

    if args.cmd == "fetch":
        cmd_fetch()
    elif args.cmd == "dashboard":
        cmd_dashboard()
    elif args.cmd == "publish":
        cmd_publish()
    elif args.cmd == "all":
        cmd_fetch()
        cmd_dashboard()
        cmd_publish()
    elif args.cmd == "login":
        cmd_login("pdd")
    elif args.cmd == "login-jd":
        cmd_login("jd")
    elif args.cmd == "serve":
        cmd_serve(args.port)
    elif args.cmd == "list":
        cmd_list()
    elif args.cmd == "add":
        cmd_add(args.url, args.name)
    elif args.cmd == "remove":
        cmd_remove(args.key)


if __name__ == "__main__":
    main()

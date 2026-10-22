#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
miot-cloud.py —— 米家云端面板 / 控制服务（可移植）

登录小米账号（与米家 App 同源）→ 云端枚举设备 → 云端控制/读取状态，
不依赖局域网 IP/Token，适合触摸屏、平板、电视盒子等做家庭控制面板。

页面：
  /               墙屏面板（全屏、响应式、时钟/天气/日历/计量/排版）
  /login          扫码登录页（手机在同一局域网扫码输入小米账号）
  /admin          后台管理（首次进入设置 PIN；设备显隐/改名、家庭过滤、天气、日志）
  /camera         摄像头页（列表 + go2rtc 单路播放器）
API：
  /api/status    登录/设备/天气/主题状态
  /api/devices   设备列表 + 实时状态 + 排版顺序
  /api/cameras   摄像头列表（自动向 go2rtc 注册 P2P/转码流）
  /api/camera_state  单路摄像头的播放状态
  /api/control   控制（POST {did, cmd, value}）
  /api/login     小米登录（POST {username, password}）
  /api/verify    验证码（POST {bind_id, code}）
  /api/logout    小米登出
  /api/admin/*   管理后台接口（PIN 会话）

配置：
  MIOT_ROOT（默认 /etc/miio-bridge）/cloud_config.json
  MIOT_PORT（默认 80）
  MIOT_GO2RTC（默认 http://127.0.0.1:1984）
  设备名称/过滤/天气/页面大小/管理员 PIN 都在后台修改。
BLE/Zigbee 子设备和传感器通常无法通过云端 RPC 读写，面板会自动隐藏；
摄像头通过面板登录态换取 P2P 临时密钥，交给 go2rtc + ffmpeg 播放。
"""
import os
import json
import time
import threading
import logging
import urllib.parse
import urllib.request
import hashlib
import random
import secrets
import shutil
import subprocess

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from micloud import MiCloud
except Exception:
    MiCloud = None

try:
    import segno
except Exception:
    segno = None

LOG = logging.getLogger("miot-cloud")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = os.environ.get("MIOT_ROOT", "/etc/miio-bridge")
AUTH_FILE = os.path.join(ROOT, "micloud_auth.json")
DEV_CACHE = os.path.join(ROOT, "cloud_devices.json")
STATE_CACHE = os.path.join(ROOT, "cloud_states.json")
LAYOUT_FILE = os.path.join(ROOT, "cloud_layout.json")
SETTINGS_FILE = os.path.join(ROOT, "cloud_settings.json")
CONFIG_FILE = os.path.join(ROOT, "cloud_config.json")
try:
    PORT = int(os.environ.get("MIOT_PORT", "80"))
except ValueError:
    PORT = 80

GO2RTC_API = os.environ.get("MIOT_GO2RTC", "http://127.0.0.1:1984")
FFMPEG_BIN = shutil.which("ffmpeg")
CAM_TTL = 600
CAM_LOCK = threading.Lock()
CAM_CACHE = {"ts": 0, "items": []}
CAM_META = {"ts": 0, "items": []}
CAM_META_TTL = 300

# 可移植配置：家庭过滤、天气城市、页面大小、管理员 PIN 等
DEFAULT_CONFIG = {
    "home_label": "我的家",
    "wan_prefixes": [],
    "lan_prefixes": [],
    "weather_location": "",
    "weather_label": "天气",
    "country": "cn",
    "page_size": 10,
    "hidden_devices": [],
    "renames": {},
    "admin_pin_salt": "",
    "admin_pin_hash": "",
}
CONFIG_LOCK = threading.Lock()

def _str_list(value, limit=40):
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for x in value[:limit]:
        if isinstance(x, str) and x.strip():
            out.append(x.strip()[:60])
    return out

def _normalize_config(data):
    src = data if isinstance(data, dict) else {}
    out = {}
    out["home_label"] = str(src.get("home_label") or DEFAULT_CONFIG["home_label"])[:40]
    out["weather_label"] = str(src.get("weather_label") or DEFAULT_CONFIG["weather_label"])[:30]
    out["weather_location"] = str(src.get("weather_location") or "")[:80]
    out["country"] = str(src.get("country") or "cn")[:8]
    try:
        out["page_size"] = min(12, max(4, int(src.get("page_size") or 10)))
    except Exception:
        out["page_size"] = 10
    out["wan_prefixes"] = _str_list(src.get("wan_prefixes"))
    out["lan_prefixes"] = _str_list(src.get("lan_prefixes"))
    out["hidden_devices"] = _str_list(src.get("hidden_devices"), 500)
    renames = src.get("renames") if isinstance(src.get("renames"), dict) else {}
    out["renames"] = {str(k): str(v)[:40] for k, v in list(renames.items())[:500]
                      if str(v).strip()}
    out["admin_pin_salt"] = str(src.get("admin_pin_salt") or "")
    out["admin_pin_hash"] = str(src.get("admin_pin_hash") or "")
    return out

def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return _normalize_config(json.load(f))
    except Exception:
        return _normalize_config({})

CONFIG = load_config()

def save_config(updates):
    with CONFIG_LOCK:
        base = dict(CONFIG)
        if isinstance(updates, dict):
            base.update(updates)
        new = _normalize_config(base)
        os.makedirs(ROOT, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
        CONFIG.clear()
        CONFIG.update(new)
        return dict(new)

def config_value(key, default=None):
    return CONFIG.get(key, DEFAULT_CONFIG.get(key, default))

def get_lan_ip():
    """取本机局域网 IPv4，用于生成手机扫码登录的二维码 URL。"""
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=3).stdout.split()
        for ip in out:
            if ip.startswith("127.") or ":" in ip:
                continue
            return ip
    except Exception:
        pass
    return "127.0.0.1"

LAN_IP = get_lan_ip()

# ----------------------------------------------------------------- 天气（wttr.in，缓存 30 分钟）
WEATHER = {}
WEATHER_LOCK = threading.Lock()
WEATHER_WAKE = threading.Event()

def weather_cn(desc):
    s = (desc or "").lower()
    if "thunder" in s:
        return "雷雨"
    if "snow" in s or "sleet" in s or "ice" in s:
        return "雪"
    if "rain" in s or "drizzle" in s or "shower" in s:
        if "heavy" in s:
            return "大雨"
        if "patchy" in s:
            return "局部有雨"
        return "小雨"
    if "fog" in s or "mist" in s:
        return "雾"
    if "overcast" in s:
        return "阴"
    if "cloud" in s:
        return "多云"
    if "sun" in s or "clear" in s:
        return "晴"
    return desc or ""

def _wdesc(cur):
    try:
        return weather_cn(cur.get("weatherDesc", [{}])[0].get("value", ""))
    except Exception:
        return ""

def fetch_weather():
    location = str(CONFIG.get("weather_location") or "").strip()
    if not location:
        return None
    url = "https://wttr.in/%s?format=j1" % urllib.parse.quote(location)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    cur = (j.get("current_condition") or [{}])[0]
    days = j.get("weather") or []
    daily = []
    for w in days[:3]:
        hourly = w.get("hourly") or []
        noon = hourly[4] if len(hourly) > 4 else (hourly[0] if hourly else {})
        daily.append({
            "date": w.get("date"),
            "max": int(w.get("maxtempC") or 0),
            "min": int(w.get("mintempC") or 0),
            "text": _wdesc(noon),
        })
    today = days[0] if days else {}
    return {
        "temp": int(cur.get("temp_C") or 0),
        "feels": int(cur.get("FeelsLikeC") or 0),
        "humidity": int(cur.get("humidity") or 0),
        "wind": int(cur.get("windspeedKmph") or 0),
        "text": _wdesc(cur),
        "high": int(today.get("maxtempC") or 0),
        "low": int(today.get("mintempC") or 0),
        "daily": daily,
        "ts": int(time.time()),
    }

def weather_loop():
    while True:
        try:
            w = fetch_weather()
            with WEATHER_LOCK:
                WEATHER.clear()
                if w:
                    WEATHER.update(w)
        except Exception as e:
            LOG.warning("weather fetch failed: %s", e)
        WEATHER_WAKE.wait(1800)
        WEATHER_WAKE.clear()

def build_qr(url):
    """生成二维码内联 SVG；无 segno 时返回提示。"""
    if segno is None:
        return '<div style="color:#ff8a6a;font-size:13px">服务端未安装二维码库</div>'
    qr = segno.make(url, error="m")
    return qr.svg_inline(scale=9, border=2)

# ----------------------------------------------------------------- 登录链（自包含，文件持久化）
UA = "Android-7.1.2-1.0.0-ONEPLUS A3010-136-%s APP/xiaomi.smarthome APPV/62830"
_UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
XIAOMI_ERR_MAP = {
    "70016": "小米账号或密码错误",
    "87001": "需要图形验证码，请先用手机浏览器登录一次 i.mi.com 再重试",
    "61020": "账号存在二次验证，请按提示完成",
    "40029": "验证码错误，请重新输入",
    "40100": "验证码错误，请重新输入",
}
_IDENTITY_QS = {"sid": "xiaomiio", "mask": "0", "_locale": "en_US"}

_lock = threading.Lock()
PENDING = {}   # bind_id -> 验证中的 session 上下文
AUTH = None    # 当前登录态（内存缓存）

# ----------------------------------------------------------------- 工具
def _rand(n):
    return "".join(random.choice("0123456789abcdef") for _ in range(n))

def _ctx_of(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("context", [None])[0]

def _new_session():
    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA % _rand(13).upper(),
                         "Content-Type": "application/x-www-form-urlencoded"})
    sess.cookies.update({"sdkVersion": "3.8.6", "deviceId": _rand(6)})
    return sess

def _to_json(text):
    return json.loads(text.replace("&&&START&&&", ""))

def _login_steps_12(sess, username, password):
    sess.headers["User-Agent"] = UA % _rand(13).upper()
    r = sess.get("https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true", timeout=15)
    j = _to_json(r.text)
    sign = j.get("_sign", "")
    url = "https://account.xiaomi.com/pass/serviceLoginAuth2"
    post = {'sid': 'xiaomiio',
            'hash': hashlib.md5(password.encode()).hexdigest().upper(),
            'callback': 'https://sts.api.io.mi.com/sts',
            'qs': '%3Fsid%3Dxiaomiio%26_json%3Dtrue',
            'user': username, '_json': 'true'}
    if sign:
        post['_sign'] = sign
    r = sess.post(url, data=post, timeout=15)
    return _to_json(r.text)

def _finish_location(sess, location, j):
    r3 = sess.get(location, timeout=15)
    st = r3.cookies.get("serviceToken")
    if not st:
        return None, "未取得 serviceToken，请重试"
    return {"user_id": j.get("userId"), "ssecurity": j.get("ssecurity"),
            "cuser_id": j.get("cUserId"), "pass_token": j.get("passToken"),
            "service_token": st}, None

def _identity_send_ticket(sess, notification_url):
    try:
        sess.headers["User-Agent"] = _UA_BROWSER
        sess.get(notification_url, timeout=15)
        context = _ctx_of(notification_url)
        sess.get("https://account.xiaomi.com/identity/list",
                 params={"sid": "xiaomiio", "context": context, "_locale": "en_US"}, timeout=15)
    except Exception as e:
        return None, None, "打开验证页失败：%s" % e
    ick = sess.cookies.get("ick", "")
    last = "未知错误"
    for flag, api, method in ((4, "sendPhoneTicket", "sms"), (8, "sendEmailTicket", "email")):
        params = {"_dc": str(int(time.time() * 1000))}
        params.update(_IDENTITY_QS)
        params["context"] = context
        data = {"retry": "0", "icode": "", "_json": "true", "ick": ick}
        try:
            r = sess.post("https://account.xiaomi.com/identity/auth/" + api,
                          params=params, data=data, timeout=15)
            jr = _to_json(r.text)
        except Exception as e:
            last = "%s 异常：%s" % (api, e)
            continue
        code = str(jr.get("code", ""))
        if code in ("0", "") and jr.get("result") != "error":
            return method, flag, None
        if code == "87001":
            return None, None, "发送验证码需图形验证，请先用手机浏览器登录 i.mi.com 再试"
        last = "%s code=%s %s" % (api, code, jr.get("desc") or jr.get("description") or "")
    return None, None, "发送验证码失败：%s" % last

def _sts_chain(sess, finish_loc):
    try:
        end_url = finish_loc
        if "identity/result/check" in finish_loc:
            r = sess.get(finish_loc, allow_redirects=False, timeout=15)
            end_url = r.headers.get("Location") or finish_loc
        r = sess.get(end_url, allow_redirects=False, timeout=15)
        ssecurity = None
        ext = r.headers.get("extension-pragma")
        if ext:
            try:
                ssecurity = json.loads(ext).get("ssecurity")
            except Exception:
                pass
        sts = r.headers.get("Location")
        if not sts and r.text:
            i = r.text.find("https://sts.api.io.mi.com/sts")
            if i != -1:
                e = r.text.find('"', i)
                sts = r.text[i:e if e != -1 else i + 300]
        if not sts:
            return None
        sess.get(sts, allow_redirects=True, timeout=15)
        st = (sess.cookies.get("serviceToken", domain=".sts.api.io.mi.com")
              or sess.cookies.get("serviceToken"))
        uid = (sess.cookies.get("userId", domain=".xiaomi.com")
               or sess.cookies.get("userId", domain=".sts.api.io.mi.com"))
        if st and ssecurity:
            return {"user_id": uid, "ssecurity": ssecurity, "cuser_id": None,
                    "pass_token": None, "service_token": st}
    except Exception as e:
        LOG.warning("sts_chain failed: %s", e)
    return None

def login_start(username, password, country=None):
    country = country or CONFIG.get("country") or "cn"
    sess = _new_session()
    try:
        j = _login_steps_12(sess, username, password)
    except Exception as e:
        return {"state": "error", "msg": "无法连接小米账号服务：%s" % e}
    if j.get("result") != "ok":
        code = str(j.get("code", ""))
        desc = j.get("desc") or j.get("description") or ""
        msg = XIAOMI_ERR_MAP.get(code) or "小米登录被拒绝（code=%s %s）" % (code or "?", desc)
        return {"state": "error", "msg": msg}
    location = j.get("location")
    if location:
        auth, err = _finish_location(sess, location, j)
        if err:
            return {"state": "error", "msg": err}
        auth["username"] = username
        auth["country"] = country
        _save_auth(auth)
        return {"state": "ok", "auth": auth}
    notification = j.get("notificationUrl")
    if notification:
        method, flag, err = _identity_send_ticket(sess, notification)
        if err:
            return {"state": "error", "msg": err}
        bid = _rand(16)
        PENDING[bid] = {"sess": sess, "context": _ctx_of(notification), "flag": flag,
                        "method": method, "username": username, "country": country,
                        "ts": time.time()}
        return {"state": "need_verify", "bind_id": bid, "method": method}
    pt = j.get("passToken")
    if pt:
        try:
            sess.cookies.set("passToken", pt, domain="account.xiaomi.com")
            sess.cookies.set("userId", str(j.get("userId") or username), domain="account.xiaomi.com")
            r = sess.get("https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true", timeout=15)
            j2 = _to_json(r.text)
            if j2.get("location"):
                auth, err = _finish_location(sess, j2["location"], j2)
                if not err:
                    auth["username"] = username
                    auth["country"] = country
                    _save_auth(auth)
                    return {"state": "ok", "auth": auth}
        except Exception:
            pass
    return {"state": "error", "msg": "登录成功但未返回授权地址，请重试"}

def verify_finish(bind_id, code):
    bs = PENDING.pop(bind_id, None)
    if not bs:
        return {"state": "error", "msg": "验证会话已过期，请重新登录"}
    sess = bs["sess"]
    flag = bs.get("flag") or 4
    ick = sess.cookies.get("ick", "")
    api = "verifyPhone" if flag == 4 else "verifyEmail"
    params = {"_flag": str(flag), "_json": "true"}
    params.update(_IDENTITY_QS)
    params["context"] = bs.get("context")
    try:
        r = sess.post("https://account.xiaomi.com/identity/auth/" + api, params=params,
                      data={"_flag": str(flag), "ticket": code.strip(), "trust": "true",
                            "_json": "true", "ick": ick}, timeout=15)
        jr = _to_json(r.text)
    except Exception as e:
        return {"state": "error", "msg": "验证请求异常：%s" % e}
    if str(jr.get("code", "")) != "0":
        c = str(jr.get("code", ""))
        msg = XIAOMI_ERR_MAP.get(c) or "验证失败（code=%s %s）" % (c or "?", jr.get("desc") or "")
        return {"state": "error", "msg": msg}
    finish_loc = jr.get("location")
    if not finish_loc:
        try:
            sess.cookies.update({"userId": bs["username"]})
            r = sess.get("https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true", timeout=15)
            j = _to_json(r.text)
            if j.get("location"):
                auth, err = _finish_location(sess, j["location"], j)
                if not err:
                    auth["username"] = bs["username"]
                    auth["country"] = bs.get("country") or CONFIG.get("country") or "cn"
                    _save_auth(auth)
                    return {"state": "ok", "auth": auth}
        except Exception:
            pass
    if not finish_loc:
        return {"state": "error", "msg": "验证通过但未取得授权地址，请重试"}
    auth = _sts_chain(sess, finish_loc)
    if auth:
        auth["username"] = bs["username"]
        auth["country"] = bs.get("country") or CONFIG.get("country") or "cn"
        _save_auth(auth)
        return {"state": "ok", "auth": auth}
    return {"state": "error", "msg": "验证通过但登录链未走完，请重新登录"}

# ----------------------------------------------------------------- 持久化 / 云对象
def _save_auth(auth):
    global AUTH
    AUTH = auth
    try:
        json.dump(auth, open(AUTH_FILE, "w"), ensure_ascii=False)
        os.chmod(AUTH_FILE, 0o600)
    except Exception as e:
        LOG.warning("save auth failed: %s", e)

def _load_auth():
    global AUTH
    if AUTH:
        return AUTH
    try:
        if os.path.exists(AUTH_FILE):
            AUTH = json.load(open(AUTH_FILE))
            return AUTH
    except Exception:
        pass
    return None

def get_cloud():
    auth = _load_auth()
    if not auth or MiCloud is None:
        return None
    c = MiCloud(auth.get("username", ""), "")
    c.user_id = auth["user_id"]
    c.ssecurity = auth["ssecurity"]
    c.cuser_id = auth.get("cuser_id")
    c.pass_token = auth.get("pass_token")
    c.service_token = auth["service_token"]
    c.locale = "zh_CN"
    c.timezone = "GMT+08:00"
    return c

def suggest_role(model, name):
    m = (model or "").lower()
    n = (name or "").lower()
    if any(k in m + n for k in ["light", "lamp", "bulb", "灯", "yeelight", "吸顶"]):
        return "light"
    if any(k in m + n for k in ["curtain", "窗帘", "blind"]):
        return "curtain"
    if ("aircondition" in m or "airc." in m or "acpartner" in m
            or "air-condition" in m or "空调" in n or "新风" in n):
        return "ac"
    if any(k in m + n for k in ["fan", "风扇", "塔扇"]):
        return "fan"
    if any(k in m + n for k in ["plug", "socket", "插座", "switch", "开关"]):
        return "plug"
    if any(k in m + n for k in ["sensor", "temp", "湿度", "温", "env", "感应"]):
        return "sensor"
    return "other"

ROLE_ICON = {"light": "💡", "curtain": "🪟", "ac": "❄️", "plug": "🔌",
             "fan": "🌀", "sensor": "🌡️", "other": "🔧"}

# 面板只展示真正可控制的设备，避免把摄像头/手机/传感器当开关
CONTROL_ROLES = {"light", "plug", "ac", "curtain", "fan"}
DEFAULT_PROFILE = {"power": (2, 1)}

# 空调类设备的 MIOT 属性映射（来自 miot-spec）
MODEL_PROFILES = {
    "sixwgh.acpartner.ym01": {
        "power": (3, 5), "temp": (3, 3), "mode": (3, 2),
        "temp_range": (16, 30),
        "modes": [(0, "制冷"), (1, "制热"), (2, "自动"), (3, "送风"), (4, "除湿")],
    },
    "qjiang.acpartner.wb20": {
        "power": (3, 1), "temp": (3, 4), "mode": (3, 2),
        "temp_range": (16, 30),
        "modes": [(0, "制冷"), (1, "制热"), (2, "自动"), (3, "送风"), (4, "除湿")],
    },
    "xiaomi.aircondition.m9": {
        "power": (2, 1), "temp": (2, 4), "mode": (2, 2),
        "temp_range": (16, 31),
        "modes": [(2, "制冷"), (5, "制热"), (4, "送风"), (3, "除湿")],
    },
    "xiaomi.airc.h09h00": {
        "power": (2, 1), "temp": (2, 4), "mode": (2, 2),
        "temp_range": (16, 31),
        "modes": [(2, "制冷"), (5, "制热"), (4, "送风"), (3, "除湿")],
    },
}

# 计量属性：key -> (siid, piid, 倍率, 单位)
MODEL_METRICS = {
    "lxzn.switch.cbcsmj": [
        {"voltage": (3, 3, 0.1, "V"), "current": (3, 2, 0.001, "A"),
         "watt": (3, 6, 1, "W"), "energy": (3, 1, 0.01, "kWh")},
    ],
    "cuco.plug.cp1m": [
        {"voltage": (2, 3, 0.1, "V")},
        {"current": (2, 4, 0.001, "A")},
        {"watt": (4, 1, 1, "W")},
        {"energy": (2, 2, 0.01, "kWh")},
    ],
    "sixwgh.acpartner.ym01": [
        {"watt": (7, 6, 1, "W"), "energy": (7, 1, 1, "kWh")},
    ],
    "xiaomi.aircondition.m9": [{"watt": (20, 1, 1, "W")}],
    "xiaomi.airc.h09h00": [{"watt": (20, 1, 1, "W")}],
}

def profile_for(model):
    return MODEL_PROFILES.get(model or "", DEFAULT_PROFILE)

def load_layout():
    try:
        with open(LAYOUT_FILE, encoding="utf-8") as f:
            order = json.load(f).get("order") or []
        return [str(x) for x in order if isinstance(x, str)][:300]
    except Exception:
        return []

def save_layout(order):
    order = [str(x) for x in order if isinstance(x, str)][:300]
    tmp = LAYOUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"order": order}, f, ensure_ascii=False)
    os.replace(tmp, LAYOUT_FILE)
    try:
        os.chmod(LAYOUT_FILE, 0o644)
    except OSError:
        pass
    return order

def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_settings(data):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, SETTINGS_FILE)
    try:
        os.chmod(SETTINGS_FILE, 0o644)
    except OSError:
        pass

def find_device(did):
    for d in DEVICES:
        if d["did"] == did:
            return d
    return None

# ----------------------------------------------------------------- 设备 / 控制
def _device_matches_home(wan, lan):
    wan_prefixes = CONFIG.get("wan_prefixes") or []
    lan_prefixes = CONFIG.get("lan_prefixes") or []
    if not wan_prefixes and not lan_prefixes:
        return True
    return (any(wan.startswith(p) for p in wan_prefixes) or
            any(lan.startswith(p) for p in lan_prefixes))

def fetch_devices():
    c = get_cloud()
    if not c:
        return None, "未登录"
    try:
        raw = c.get_devices(country=CONFIG.get("country") or "cn")
    except Exception as e:
        return None, "设备列表拉取失败：%s" % e
    hidden = set(CONFIG.get("hidden_devices") or [])
    renames = CONFIG.get("renames") or {}
    devices = []
    for dev in (raw or []):
        did = dev.get("did") or dev.get("id")
        if did is None:
            continue
        did = str(did)
        name = dev.get("name") or "未命名设备"
        model = dev.get("model") or ""
        online = bool(dev.get("isOnline"))
        role = suggest_role(model, name)
        if role not in CONTROL_ROLES or did in hidden:
            continue
        wan = str(dev.get("internet_ip") or "")
        lan = str(dev.get("localip") or "")
        if not _device_matches_home(wan, lan):
            continue
        devices.append({"did": did, "name": renames.get(did) or name,
                        "model": model, "online": online, "role": role})
    return devices, None

def fetch_all_devices():
    """后台用：返回云端全部设备（含未显示/其他家庭），不返回 token。"""
    c = get_cloud()
    if not c:
        return None, "未登录"
    try:
        raw = c.get_devices(country=CONFIG.get("country") or "cn")
    except Exception as e:
        return None, "设备列表拉取失败：%s" % e
    hidden = set(CONFIG.get("hidden_devices") or [])
    renames = CONFIG.get("renames") or {}
    out = []
    for dev in (raw or []):
        did = dev.get("did") or dev.get("id")
        if did is None:
            continue
        did = str(did)
        name = dev.get("name") or "未命名设备"
        model = dev.get("model") or ""
        role = suggest_role(model, name)
        out.append({"did": did, "name": name, "model": model,
                    "role": role, "online": bool(dev.get("isOnline")),
                    "localip": dev.get("localip") or "",
                    "internet_ip": dev.get("internet_ip") or "",
                    "hidden": did in hidden,
                    "rename": renames.get(did, ""),
                    "home_match": _device_matches_home(
                        str(dev.get("internet_ip") or ""),
                        str(dev.get("localip") or ""))})
    return out, None

# ------------------------------------------------------------- 摄像头
# 用面板已有的小米登录态向云端换取 P2P 一次性密钥，再让 go2rtc 直连摄像头。
# 不依赖 go2rtc 的 Xiaomi 账号登录（避免短信验证码频率限制）。
_X25519_P = 2 ** 255 - 19
_X25519_A24 = 121665

def _x25519_public(raw):
    k = bytearray(raw)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    scalar = int.from_bytes(k, "little")
    x1 = 9
    x2, z2 = 1, 0
    x3, z3 = x1, 1
    swap = 0
    for t in range(254, -1, -1):
        bit = (scalar >> t) & 1
        swap ^= bit
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = bit
        a = (x2 + z2) % _X25519_P
        aa = a * a % _X25519_P
        b = (x2 - z2) % _X25519_P
        bb = b * b % _X25519_P
        e = (aa - bb) % _X25519_P
        c = (x3 + z3) % _X25519_P
        d = (x3 - z3) % _X25519_P
        da = d * a % _X25519_P
        cb = c * b % _X25519_P
        x3 = (da + cb) % _X25519_P
        x3 = x3 * x3 % _X25519_P
        z3 = (da - cb) % _X25519_P
        z3 = x1 * z3 * z3 % _X25519_P
        x2 = aa * bb % _X25519_P
        z2 = e * (aa + _X25519_A24 * e) % _X25519_P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, _X25519_P - 2, _X25519_P) % _X25519_P).to_bytes(32, "little")

def _go2rtc_put(name, src):
    q = urllib.parse.urlencode({"name": name, "src": src})
    req = urllib.request.Request(GO2RTC_API + "/api/streams?" + q, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200, ""
    except Exception as e:
        LOG.warning("go2rtc add %s failed: %s", name, type(e).__name__)
        return False, "go2rtc 未响应"

def _camera_p2p_url(c, did, model, ip):
    priv = os.urandom(32)
    pub = _x25519_public(priv).hex()
    payload = json.dumps({"app_pubkey": pub, "did": did,
                          "support_vendors": "TUTK_CS2_MTP"}, separators=(",", ":"))
    api = c._get_api_url(CONFIG.get("country") or "cn") + "/v2/device/miss_get_vendor"
    raw = c.request(api, {"data": payload})
    if not raw:
        raise RuntimeError("云端没有返回摄像头授权")
    jr = json.loads(raw)
    if int(jr.get("code") or 0) != 0:
        raise RuntimeError("云端授权失败（%s）" % jr.get("code"))
    res = jr.get("result") or {}
    vendor = res.get("vendor") or {}
    vid = vendor.get("vendor")
    vname = {1: "tutk", 4: "cs2"}.get(vid)
    if not vname:
        raise RuntimeError("摄像头的 P2P 协议暂不支持")
    if not res.get("public_key") or not res.get("sign"):
        raise RuntimeError("云端授权数据不完整")
    q = {"did": did, "model": model, "client_public": pub,
         "client_private": priv.hex(), "device_public": res["public_key"],
         "sign": res["sign"], "vendor": vname, "subtype": "hd"}
    if vid == 1:
        q["uid"] = (vendor.get("vendor_params") or {}).get("p2p_id") or ""
    return "xiaomi://%s?%s" % (ip, urllib.parse.urlencode(q))

def ensure_cameras(force=False):
    with CAM_LOCK:
        if (force and CAM_CACHE["items"]
                and time.time() - CAM_CACHE["ts"] < 30):
            force = False
        if (not force and CAM_CACHE["items"]
                and time.time() - CAM_CACHE["ts"] < CAM_TTL):
            return {"ok": True, "cameras": CAM_CACHE["items"],
                    "transcode": bool(FFMPEG_BIN), "cached": True}
        c = get_cloud()
        if not c:
            raise RuntimeError("未登录小米账号")
        try:
            raw = c.get_devices(country=CONFIG.get("country") or "cn")
        except Exception:
            raise RuntimeError("摄像头列表拉取失败")
        items = []
        for dev in raw or []:
            model = dev.get("model") or ""
            if ".camera." not in model and ".cateye." not in model:
                continue
            did = str(dev.get("did") or "")
            ip = str(dev.get("localip") or "")
            if not did:
                continue
            if not _device_matches_home(str(dev.get("internet_ip") or ""), ip):
                continue
            item = {"did": did, "name": dev.get("name") or "摄像头",
                    "model": model, "online": bool(dev.get("isOnline")),
                    "ip": ip, "stream": "", "error": ""}
            if not item["online"]:
                item["error"] = "设备离线"
            elif not ip:
                item["error"] = "没有局域网地址"
            else:
                try:
                    src = _camera_p2p_url(c, did, model, ip)
                    ok, err = _go2rtc_put("cam_" + did, src)
                    if not ok:
                        raise RuntimeError(err)
                    if FFMPEG_BIN:
                        tsrc = ("ffmpeg:cam_%s#video=h264#width=640#height=360"
                                "#bitrate=600k" % did)
                        ok, err = _go2rtc_put("cam_%s_h264" % did, tsrc)
                        if ok:
                            item["stream"] = "cam_%s_h264" % did
                        else:
                            item["error"] = "转码流注册失败"
                    else:
                        item["error"] = "未安装 ffmpeg，无法转码播放"
                except Exception as e:
                    item["error"] = str(e)[:120]
                    LOG.warning("camera %s failed: %s", did, e)
            items.append(item)
        CAM_CACHE.update(ts=time.time(), items=items)
        LOG.info("cameras refreshed: %d", len(items))
        return {"ok": True, "cameras": items,
                "transcode": bool(FFMPEG_BIN), "cached": False}

def ensure_camera_meta(force=False):
    with CAM_LOCK:
        if (not force and CAM_META["items"]
                and time.time() - CAM_META["ts"] < CAM_META_TTL):
            return {"ok": True, "cameras": CAM_META["items"], "meta": True}
        c = get_cloud()
        if not c:
            raise RuntimeError("未登录小米账号")
        try:
            raw = c.get_devices(country=CONFIG.get("country") or "cn")
        except Exception:
            raise RuntimeError("摄像头列表拉取失败")
        items = []
        for dev in raw or []:
            model = dev.get("model") or ""
            if ".camera." not in model and ".cateye." not in model:
                continue
            did = str(dev.get("did") or "")
            ip = str(dev.get("localip") or "")
            if not did:
                continue
            if not _device_matches_home(str(dev.get("internet_ip") or ""), ip):
                continue
            items.append({"did": did, "name": dev.get("name") or "摄像头",
                          "model": model, "online": bool(dev.get("isOnline")),
                          "ip": ip})
        CAM_META.update(ts=time.time(), items=items)
        LOG.info("camera meta: %d", len(items))
        return {"ok": True, "cameras": items, "meta": True}

def _rpc(c, did, method, payload):
    url = c._get_api_url(CONFIG.get("country") or "cn") + "/home/rpc/" + str(did)
    params = {"data": json.dumps({"method": method, "params": payload})}
    try:
        resp = c.request(url, params)
        return json.loads(resp)
    except Exception as e:
        raise RuntimeError("云端请求失败：%s" % e)

def _prop_values(jr):
    out = {}
    res = jr.get("result")
    if isinstance(res, list):
        for it in res:
            if isinstance(it, dict) and it.get("code") == 0:
                out[(it.get("siid"), it.get("piid"))] = it.get("value")
    return out

def cloud_set_prop(did, model, cmd, value):
    c = get_cloud()
    if not c:
        return False, "未登录"
    prof = profile_for(model)
    if cmd not in ("power", "temp", "mode") or cmd not in prof:
        return False, "该设备不支持 %s" % cmd
    siid, piid = prof[cmd]
    if cmd == "power":
        val = bool(value)
    elif cmd == "temp":
        try:
            val = float(value)
        except Exception:
            return False, "温度值无效"
    else:
        try:
            val = int(value)
        except Exception:
            return False, "模式值无效"
    try:
        jr = _rpc(c, did, "set_properties",
                  [{"siid": siid, "piid": piid, "value": val}])
    except Exception as e:
        return False, str(e)
    res = jr.get("result")
    if isinstance(res, list) and res:
        ok = all(isinstance(x, dict) and x.get("code") == 0 for x in res)
    else:
        ok = jr.get("code") == 0 or jr.get("message") == "ok"
    return (True, None) if ok else (False, str(jr.get("message") or jr))

def cloud_get_state(did, model):
    c = get_cloud()
    if not c:
        return {}
    prof = profile_for(model)
    specs = [{"siid": prof["power"][0], "piid": prof["power"][1]}]
    for k in ("temp", "mode"):
        if k in prof:
            specs.append({"siid": prof[k][0], "piid": prof[k][1]})
    try:
        vals = _prop_values(_rpc(c, did, "get_properties", specs))
    except Exception:
        return None
    if not vals:
        return None
    out = {"power": vals.get(prof["power"])}
    for k in ("temp", "mode"):
        if k in prof:
            out[k] = vals.get(prof[k])
    return out

def cloud_get_metrics(did, model):
    groups = MODEL_METRICS.get(model)
    if not groups:
        return None
    c = get_cloud()
    if not c:
        return None
    out = {}
    for group in groups:
        specs = [{"siid": p[0], "piid": p[1]} for p in group.values()]
        try:
            vals = _prop_values(_rpc(c, did, "get_properties", specs))
        except Exception:
            continue
        for key, (siid, piid, scale, unit) in group.items():
            v = vals.get((siid, piid))
            if v is None:
                continue
            try:
                out[key] = round(float(v) * scale, 2)
            except Exception:
                pass
        time.sleep(0.2)
    return out or None

# ----------------------------------------------------------------- 状态缓存 + 轮询
STATE = {}        # did -> {online, power, temp, mode, supported, metrics}
DEVICES = []      # 最近一次枚举
DEV_TS = 0
POLL_NO = 0
TRY_NEXT = {}     # 不支持/不可读的设备，2 分钟后再试

def _load_caches():
    """启动时先恢复上次设备/状态，避免重启后设备短暂消失。"""
    global DEVICES, DEV_TS, STATE
    try:
        with open(DEV_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        DEVICES = data.get("devices") or []
        DEV_TS = int(data.get("ts") or 0)
    except Exception:
        pass
    try:
        with open(STATE_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        STATE = data.get("states") or {}
    except Exception:
        pass
    LOG.info("cache loaded: %d devices / %d states", len(DEVICES), len(STATE))

def poll_once():
    global DEVICES, DEV_TS, POLL_NO
    POLL_NO += 1
    try:
        devs, err = fetch_devices()
        if devs is None:
            LOG.warning("poll fetch failed: %s", err)
            return
        DEVICES = devs
        DEV_TS = time.time()
        try:
            with open(DEV_CACHE, "w", encoding="utf-8") as f:
                json.dump({"ts": DEV_TS, "devices": devs}, f, ensure_ascii=False)
            os.chmod(DEV_CACHE, 0o600)
        except Exception:
            pass
        now = time.time()
        for d in devs:
            st = dict(STATE.get(d["did"], {}))
            st["online"] = d["online"]
            should_poll = (d["online"] and
                           (st.get("supported") or now >= TRY_NEXT.get(d["did"], 0)))
            if should_poll:
                new = cloud_get_state(d["did"], d["model"])
                if new is not None:
                    st.update(new)
                    st["supported"] = True
                    TRY_NEXT.pop(d["did"], None)
                else:
                    TRY_NEXT[d["did"]] = now + 120
                # 云端对连续请求有频率限制，串行留出间隔
                time.sleep(0.25)
            STATE[d["did"]] = st
        if POLL_NO == 1 or POLL_NO % 3 == 0:
            for d in devs:
                st = STATE.get(d["did"], {})
                if not (d["online"] and st.get("supported")):
                    continue
                if d["model"] not in MODEL_METRICS:
                    continue
                m = cloud_get_metrics(d["did"], d["model"])
                if m:
                    st["metrics"] = m
                    STATE[d["did"]] = st
                time.sleep(0.15)
        try:
            with open(STATE_CACHE, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "states": STATE}, f, ensure_ascii=False)
            os.chmod(STATE_CACHE, 0o600)
        except Exception:
            pass
        supported = sum(1 for d in devs if STATE.get(d["did"], {}).get("supported"))
        LOG.info("poll ok: %d controllable / %d cloud-readable", len(devs), supported)
    except Exception as e:
        LOG.warning("poll error: %s", e)

def poll_loop():
    while True:
        if _load_auth():
            poll_once()
        time.sleep(60)

# ----------------------------------------------------------------- HTTP
try:
    PANEL_HTML = open(os.path.join(ROOT, "cloud_panel.html"), encoding="utf-8").read()
except OSError:
    PANEL_HTML = "<h1>cloud_panel.html 缺失</h1>"
try:
    ADMIN_HTML = open(os.path.join(ROOT, "admin.html"), encoding="utf-8").read()
except OSError:
    ADMIN_HTML = "<h1>admin.html 缺失</h1>"
try:
    CAMERA_HTML = open(os.path.join(ROOT, "camera.html"), encoding="utf-8").read()
except OSError:
    CAMERA_HTML = "<h1>camera.html 缺失</h1>"


LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="google" content="notranslate">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录米家</title><style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'PingFang SC',sans-serif;background:linear-gradient(160deg,#1b1f2a,#0e1117);
color:#e9edf2;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;}
.card{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);
border-radius:20px;padding:34px 36px;max-width:420px;width:100%;}
h1{font-size:20px;font-weight:600;margin-bottom:6px;}
.sub{font-size:13px;color:#9aa4b2;margin-bottom:22px;line-height:1.7;}
label{font-size:13px;color:#c4ccd6;display:block;margin:14px 0 6px;}
input{width:100%;padding:13px 14px;border-radius:12px;border:1px solid rgba(255,255,255,.14);
background:rgba(0,0,0,.25);color:#fff;font-size:15px;}
.btn{margin-top:22px;width:100%;padding:14px;border:none;border-radius:12px;
background:#ff6900;color:#fff;font-size:16px;font-weight:600;}
.msg{margin-top:14px;font-size:13px;color:#ff8a6a;min-height:18px;}
.note{margin-top:18px;font-size:12px;color:#7a8290;line-height:1.7;}
.qr{background:#fff;border-radius:16px;padding:16px;display:inline-block;margin:4px auto 8px;}
.qr svg{display:block;width:260px;height:260px;}
.hint{font-size:12px;color:#8a93a2;line-height:1.7;margin-top:6px;}
.center{text-align:center;}
</style></head><body><div class="card">
<h1>🔐 登录米家账号</h1>

<div id="qrbox" class="center">
  <div class="sub">本屏仅支持触摸、无法输入。<br>请用<b>手机相机/微信扫一扫</b>下方二维码，在手机上完成登录：</div>
  <div class="qr">@@QR@@</div>
  <div class="hint">扫码后手机会打开登录页 → 输入账号密码（如需验证码在手机上填）→ 本屏将<b>自动跳转</b>到设备面板。</div>
</div>

<div id="form" style="display:none">
  <label>小米账号（手机号/邮箱）</label>
  <input id="user" placeholder="如 13800000000" autocomplete="username" inputmode="text">
  <label>密码</label>
  <input id="pwd" type="password" placeholder="米家 App 登录密码" autocomplete="current-password">
  <button class="btn" onclick="doLogin()">登录</button>
  <div class="msg" id="msg"></div>
  <div class="note">与米家 App 同源的小米云登录。账号密码仅在本机（玩客云）一次性用于拉取设备与控制，不经过任何第三方。若触发短信/邮箱验证码，登录后会在本页提示输入。</div>
</div>
</div>
<script>
var API='';
var phone=/[?&]m=phone/.test(location.search);
if(phone){document.getElementById('qrbox').style.display='none';document.getElementById('form').style.display='block';}
fetch(API+'/api/status').then(function(r){return r.json();}).then(function(s){if(s.logged_in)location.href=API+'/';}).catch(function(){});
function doLogin(){
  var u=document.getElementById('user').value.trim();
  var p=document.getElementById('pwd').value;
  var m=document.getElementById('msg');m.textContent='登录中…';
  fetch(API+'/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:u,password:p})}).then(r=>r.json()).then(function(j){
    if(j.state==='ok'){m.style.color='#7CFC9B';m.textContent='登录成功！正在跳转…';
      setTimeout(function(){location.href=API+'/';},800);}
    else if(j.state==='need_verify'){showVerify(j.bind_id,j.method);}
    else{m.style.color='#ff8a6a';m.textContent=j.msg||'登录失败';}
  }).catch(function(e){m.textContent='请求失败：'+e;});
}
function showVerify(bind_id,method){
  document.getElementById('form').innerHTML=
    '<label>验证码已发送至'+method+'，请输入</label>'+
    '<input id="code" placeholder="6 位验证码">'+
    '<button class="btn" onclick="doVerify(\\''+bind_id+'\\')">验证并登录</button>'+
    '<div class="msg" id="msg"></div>';
  document.getElementById('msg').textContent='请输入收到的验证码';
}
function doVerify(bind_id){
  var c=document.getElementById('code').value.trim();
  var m=document.getElementById('msg');m.textContent='验证中…';
  fetch(API+'/api/verify',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({bind_id:bind_id,code:c})}).then(r=>r.json()).then(function(j){
    if(j.state==='ok'){m.style.color='#7CFC9B';m.textContent='登录成功！正在跳转…';
      setTimeout(function(){location.href=API+'/';},800);}
    else{m.style.color='#ff8a6a';m.textContent=j.msg||'验证失败';}
  }).catch(function(e){m.textContent='请求失败：'+e;});
}
setInterval(function(){
  fetch(API+'/api/status').then(r=>r.json()).then(function(s){
    if(s.logged_in){location.href=API+'/';}
  }).catch(function(){});
},3000);
</script></body></html>"""


# ----------------------------------------------------------------- 管理后台鉴权
ADMIN_SESSIONS = {}
ADMIN_LOCK = threading.Lock()
ADMIN_TTL = 12 * 3600
ADMIN_COOKIE = "miot_admin"

def admin_pin_set():
    return bool(CONFIG.get("admin_pin_hash"))

def _pin_hash(pin, salt):
    return hashlib.sha256((salt + pin).encode("utf-8")).hexdigest()

def _cookie_value(handler, name):
    raw = handler.headers.get("Cookie", "") or ""
    for part in raw.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == name:
            return v.strip()
    return ""

def _admin_authed(handler):
    token = _cookie_value(handler, ADMIN_COOKIE)
    if not token:
        return False
    now = time.time()
    with ADMIN_LOCK:
        exp = ADMIN_SESSIONS.get(token)
        if not exp or exp < now:
            ADMIN_SESSIONS.pop(token, None)
            return False
        return True

def _new_admin_session():
    token = secrets.token_hex(24)
    with ADMIN_LOCK:
        ADMIN_SESSIONS[token] = time.time() + ADMIN_TTL
        for k in [k for k, v in ADMIN_SESSIONS.items() if v < time.time()][:20]:
            ADMIN_SESSIONS.pop(k, None)
    return token

def _admin_cookie_header(token):
    return "%s=%s; HttpOnly; Path=/; SameSite=Lax; Max-Age=%d" % (ADMIN_COOKIE, token, ADMIN_TTL)

def _public_config():
    keys = ("home_label", "wan_prefixes", "lan_prefixes", "weather_location",
            "weather_label", "country", "page_size", "hidden_devices", "renames")
    return {k: CONFIG.get(k) for k in keys}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        try:
            LOG.info("%s %s", self.address_string(), fmt % args)
        except Exception:
            pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._send(200, PANEL_HTML, "text/html; charset=utf-8")
        elif u.path == "/login":
            qr_url = "http://%s:%d/login?m=phone" % (LAN_IP, PORT)
            html = LOGIN_HTML.replace("@@QR@@", build_qr(qr_url))
            self._send(200, html, "text/html; charset=utf-8")
        elif u.path in ("/admin", "/admin/"):
            self._send(200, ADMIN_HTML, "text/html; charset=utf-8")
        elif u.path in ("/camera", "/camera/"):
            self._send(200, CAMERA_HTML, "text/html; charset=utf-8")
        elif u.path == "/api/status":
            self.api_status()
        elif u.path == "/api/devices":
            self.api_devices()
        elif u.path == "/api/cameras":
            self.api_cameras(u)
        elif u.path == "/api/camera_state":
            self.api_camera_state(u)
        elif u.path == "/api/admin/status":
            self.api_admin_status()
        elif u.path == "/api/admin/logs":
            self.api_admin_logs()
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        if u.path == "/api/login":
            self.api_login(body)
        elif u.path == "/api/verify":
            self.api_verify(body)
        elif u.path == "/api/control":
            self.api_control(body)
        elif u.path == "/api/layout":
            self.api_layout(body)
        elif u.path == "/api/theme":
            self.api_theme(body)
        elif u.path == "/api/logout":
            self.api_logout()
        elif u.path == "/api/admin/setup":
            self.api_admin_setup(body)
        elif u.path == "/api/admin/login":
            self.api_admin_login(body)
        elif u.path == "/api/admin/logout":
            self.api_admin_logout()
        elif u.path == "/api/admin/save":
            self.api_admin_save(body)
        elif u.path == "/api/admin/pin":
            self.api_admin_pin(body)
        elif u.path == "/api/admin/reset":
            self.api_admin_reset(body)
        elif u.path == "/api/admin/xiaomi_logout":
            self.api_admin_xiaomi_logout()
        elif u.path == "/api/admin/refresh":
            self.api_admin_refresh()
        else:
            self._send(404, {"error": "not found"})

    def api_status(self):
        auth = _load_auth()
        supported = sum(1 for d in DEVICES if STATE.get(d["did"], {}).get("supported"))
        blind = sum(1 for d in DEVICES
                    if d["online"] and not STATE.get(d["did"], {}).get("supported"))
        with WEATHER_LOCK:
            weather = dict(WEATHER)
        theme = load_settings().get("theme", "dark")
        if theme not in ("light", "dark"):
            theme = "dark"
        self._send(200, {"logged_in": bool(auth),
                         "home_label": CONFIG.get("home_label") or "我的家",
                         "weather_label": CONFIG.get("weather_label") or "天气",
                         "page_size": int(CONFIG.get("page_size") or 10),
                         "device_count": supported + blind,
                         "supported_count": supported, "blind_count": blind,
                         "weather": weather or None, "theme": theme})

    def api_devices(self):
        if not _load_auth():
            self._send(200, {"devices": []})
            return
        devs = []
        for d in DEVICES:
            st = STATE.get(d["did"], {})
            if not st.get("supported"):
                if d["online"]:
                    devs.append({"did": d["did"], "name": d["name"],
                                 "model": d["model"], "online": True,
                                 "role": d["role"],
                                 "icon": ROLE_ICON.get(d["role"], "🔧"),
                                 "power": None, "blind": True})
                continue
            prof = profile_for(d["model"])
            item = {"did": d["did"], "name": d["name"], "model": d["model"],
                    "online": d["online"], "role": d["role"],
                    "icon": ROLE_ICON.get(d["role"], "🔧"),
                    "power": st.get("power") if d["online"] else None,
                    "metrics": st.get("metrics") or {}}
            if d["role"] == "ac" and "mode" in prof:
                item["temp"] = st.get("temp") if d["online"] else None
                item["mode"] = st.get("mode") if d["online"] else None
                item["modes"] = prof.get("modes") or []
                item["temp_min"], item["temp_max"] = prof.get("temp_range", (16, 30))
            devs.append(item)
        self._send(200, {"devices": devs, "ts": DEV_TS, "layout": load_layout()})

    def api_cameras(self, u=None):
        q = urllib.parse.parse_qs(u.query) if u else {}
        force = (q.get("refresh") or ["0"])[0].lower() in ("1", "true", "yes")
        meta = (q.get("meta") or ["0"])[0].lower() in ("1", "true", "yes")
        if not _load_auth():
            self._send(200, {"ok": False, "error": "未登录小米账号"})
            return
        try:
            data = ensure_camera_meta(force) if meta else ensure_cameras(force)
        except Exception as e:
            LOG.warning("cameras failed: %s", e)
            self._send(200, {"ok": False, "error": str(e)[:160]})
            return
        self._send(200, data)

    def api_camera_state(self, u=None):
        name = (urllib.parse.parse_qs(u.query).get("name") or [""])[0] if u else ""
        if not name.startswith("cam_"):
            self._send(400, {"ok": False, "active": False})
            return
        try:
            req = urllib.request.Request(
                GO2RTC_API + "/api/streams?src=" + urllib.parse.quote(name))
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read() or b"{}")
            cons = data.get("consumers") or []
            prods = [p for p in (data.get("producers") or []) if isinstance(p, dict)]
            recv = sum(int(p.get("bytes_recv") or 0) for p in prods)
            self._send(200, {"ok": True, "active": bool(cons) or recv > 0,
                             "consumers": len(cons)})
        except Exception:
            self._send(200, {"ok": False, "active": False, "consumers": 0})

    def api_login(self, body):
        user = (body.get("username") or "").strip()
        pwd = body.get("password") or ""
        if not user or not pwd:
            self._send(400, {"state": "error", "msg": "账号和密码不能为空"})
            return
        res = login_start(user, pwd)
        self._send(200, res)

    def api_verify(self, body):
        bid = body.get("bind_id") or ""
        code = body.get("code") or ""
        res = verify_finish(bid, code)
        self._send(200, res)

    def api_control(self, body):
        did = str(body.get("did") or "")
        cmd = body.get("cmd") or "power"
        value = body.get("value")
        if not did:
            self._send(400, {"ok": False, "msg": "缺少 did"})
            return
        if cmd not in ("power", "temp", "mode"):
            self._send(400, {"ok": False, "msg": "不支持的指令: %s" % cmd})
            return
        d = find_device(did)
        if not d:
            self._send(404, {"ok": False, "msg": "设备不存在或已刷新，请重载页面"})
            return
        if not d.get("online"):
            self._send(200, {"ok": False, "msg": "设备离线"})
            return
        ok, err = cloud_set_prop(did, d["model"], cmd, value)
        LOG.info("control %s %s=%s -> %s", did, cmd, value,
                 "OK" if ok else (err or "fail"))
        if not ok:
            self._send(200, {"ok": False, "msg": err})
            return
        blind = not STATE.get(did, {}).get("supported")
        if not blind:
            st = STATE.get(did, {})
            try:
                if cmd == "power":
                    st["power"] = bool(value)
                elif cmd == "temp":
                    st["temp"] = float(value)
                else:
                    st["mode"] = int(value)
            except Exception:
                pass
            STATE[did] = st
        self._send(200, {"ok": True, "blind": blind})

    def api_layout(self, body):
        order = body.get("order")
        if not isinstance(order, list):
            self._send(400, {"ok": False, "msg": "order 格式错误"})
            return
        saved = save_layout(order)
        LOG.info("layout saved: %d cards", len(saved))
        self._send(200, {"ok": True, "order": saved})

    def api_theme(self, body):
        theme = body.get("theme")
        if theme not in ("light", "dark"):
            self._send(400, {"ok": False, "msg": "theme 必须是 light 或 dark"})
            return
        settings = load_settings()
        settings["theme"] = theme
        save_settings(settings)
        LOG.info("theme saved: %s", theme)
        self._send(200, {"ok": True, "theme": theme})

    def api_logout(self):
        global AUTH
        AUTH = None
        try:
            if os.path.exists(AUTH_FILE):
                os.remove(AUTH_FILE)
        except Exception:
            pass
        self._send(200, {"ok": True})

    # ---- 管理后台
    def _admin_require(self):
        if not _admin_authed(self):
            self._send(401, {"ok": False, "login": True})
            return False
        return True

    def api_admin_status(self):
        if not admin_pin_set():
            self._send(200, {"ok": False, "setup": True})
            return
        if not self._admin_require():
            return
        devices, err = fetch_all_devices()
        with WEATHER_LOCK:
            weather = dict(WEATHER)
        theme = load_settings().get("theme", "dark")
        if theme not in ("light", "dark"):
            theme = "dark"
        self._send(200, {
            "ok": True, "config": _public_config(),
            "devices": devices or [], "cloud_error": err,
            "logged_in": bool(_load_auth()), "theme": theme,
            "layout": load_layout(), "weather": weather or None,
            "port": PORT, "pin_set": True,
        })

    def api_admin_setup(self, body):
        if admin_pin_set():
            self._send(400, {"ok": False, "msg": "管理员 PIN 已设置"})
            return
        pin = str(body.get("pin") or "").strip()
        if len(pin) < 4 or len(pin) > 20:
            self._send(400, {"ok": False, "msg": "PIN 长度需为 4-20 位"})
            return
        salt = secrets.token_hex(8)
        save_config({"admin_pin_salt": salt, "admin_pin_hash": _pin_hash(pin, salt)})
        token = _new_admin_session()
        LOG.info("admin PIN initialized")
        self._send(200, {"ok": True}, headers={"Set-Cookie": _admin_cookie_header(token)})

    def api_admin_login(self, body):
        if not admin_pin_set():
            self._send(400, {"ok": False, "setup": True})
            return
        pin = str(body.get("pin") or "").strip()
        salt = CONFIG.get("admin_pin_salt") or ""
        expected = CONFIG.get("admin_pin_hash") or ""
        if not pin or not secrets.compare_digest(_pin_hash(pin, salt), expected):
            time.sleep(0.4)
            self._send(401, {"ok": False, "msg": "PIN 不正确"})
            return
        token = _new_admin_session()
        self._send(200, {"ok": True}, headers={"Set-Cookie": _admin_cookie_header(token)})

    def api_admin_logout(self):
        token = _cookie_value(self, ADMIN_COOKIE)
        if token:
            with ADMIN_LOCK:
                ADMIN_SESSIONS.pop(token, None)
        self._send(200, {"ok": True},
                   headers={"Set-Cookie": "%s=; Path=/; Max-Age=0" % ADMIN_COOKIE})

    def api_admin_save(self, body):
        if not self._admin_require():
            return
        updates = {}
        for key in ("home_label", "weather_label", "weather_location", "country"):
            if key in body and isinstance(body.get(key), str):
                updates[key] = body[key].strip()
        if "page_size" in body:
            try:
                updates["page_size"] = min(12, max(4, int(body.get("page_size"))))
            except Exception:
                pass
        if "wan_prefixes" in body:
            updates["wan_prefixes"] = _str_list(body.get("wan_prefixes"))
        if "lan_prefixes" in body:
            updates["lan_prefixes"] = _str_list(body.get("lan_prefixes"))
        if "hidden_devices" in body:
            updates["hidden_devices"] = _str_list(body.get("hidden_devices"), 500)
        if "renames" in body and isinstance(body.get("renames"), dict):
            updates["renames"] = {str(k): str(v)[:40]
                                  for k, v in list(body["renames"].items())[:500]
                                  if str(v).strip()}
        save_config(updates)
        threading.Thread(target=poll_once, daemon=True).start()
        WEATHER_WAKE.set()
        LOG.info("admin config saved")
        self._send(200, {"ok": True, "config": _public_config()})

    def api_admin_pin(self, body):
        if not self._admin_require():
            return
        pin = str(body.get("pin") or "").strip()
        if len(pin) < 4 or len(pin) > 20:
            self._send(400, {"ok": False, "msg": "PIN 长度需为 4-20 位"})
            return
        salt = secrets.token_hex(8)
        save_config({"admin_pin_salt": salt, "admin_pin_hash": _pin_hash(pin, salt)})
        self._send(200, {"ok": True})

    def api_admin_reset(self, body):
        if not self._admin_require():
            return
        what = str(body.get("what") or "all")
        if what in ("layout", "all"):
            try:
                if os.path.exists(LAYOUT_FILE):
                    os.remove(LAYOUT_FILE)
            except OSError:
                pass
        if what in ("theme", "all"):
            settings = load_settings()
            settings["theme"] = "dark"
            save_settings(settings)
        self._send(200, {"ok": True})

    def api_admin_xiaomi_logout(self):
        global AUTH
        if not self._admin_require():
            return
        AUTH = None
        try:
            if os.path.exists(AUTH_FILE):
                os.remove(AUTH_FILE)
        except OSError:
            pass
        self._send(200, {"ok": True})

    def api_admin_logs(self):
        if not self._admin_require():
            return
        try:
            out = subprocess.run(["journalctl", "-u", "miot-cloud", "-n", "120",
                                  "--no-pager", "-o", "short"],
                                 capture_output=True, timeout=10).stdout.decode(
                                     "utf-8", "replace")
            lines = [x for x in out.splitlines() if x.strip()]
        except Exception as e:
            lines = ["日志读取失败: %s" % e]
        self._send(200, {"ok": True, "lines": lines})

    def api_admin_refresh(self):
        if not self._admin_require():
            return
        TRY_NEXT.clear()
        threading.Thread(target=poll_once, daemon=True).start()
        WEATHER_WAKE.set()
        self._send(200, {"ok": True})


def main():
    _load_caches()
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=weather_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info("miot-cloud listening on :%d", PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()

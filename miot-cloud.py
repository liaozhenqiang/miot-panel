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
import sys
import json
import gzip
import time
import socket
import threading
import signal
import logging
from logging.handlers import RotatingFileHandler
import urllib.parse
import urllib.request
import urllib.error
import hashlib
import hmac
import base64
import select
import http.client
import ipaddress
import random
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import ssl
import zipfile
from pathlib import Path

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from micloud import MiCloud
except Exception:
    MiCloud = None

try:
    import segno
except Exception:
    segno = None

def _default_root():
    env = os.environ.get("MIOT_ROOT")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "miot-panel")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/miot-panel")
    return "/etc/miio-bridge"

APP_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("MIOT_APP_DIR") or os.path.dirname(os.path.abspath(__file__))))
ROOT = _default_root()


def _find_ca_bundle():
    paths = [os.path.join(APP_DIR, "cacert.pem")]
    try:
        import certifi
        paths.append(certifi.where())
    except Exception:
        pass
    for base in (sys.prefix, getattr(sys, "base_prefix", "")):
        if base:
            paths.append(os.path.join(base, "Lib", "site-packages",
                                      "certifi", "cacert.pem"))
    for path in paths:
        try:
            if path and os.path.isfile(path):
                return path
        except OSError:
            pass
    return ""


CA_BUNDLE = _find_ca_bundle()
if CA_BUNDLE:
    os.environ.setdefault("SSL_CERT_FILE", CA_BUNDLE)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", CA_BUNDLE)
    try:
        _DEFAULT_SSL_CONTEXT = ssl.create_default_context(cafile=CA_BUNDLE)
        ssl._create_default_https_context = lambda: _DEFAULT_SSL_CONTEXT
        LOG_CA = CA_BUNDLE
    except Exception:
        LOG_CA = ""
else:
    LOG_CA = ""

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_FILE = os.path.abspath(os.path.expanduser(os.environ.get("MIOT_LOG_FILE") or (
    os.path.join(ROOT, "miot-cloud.log") if os.name == "nt" else ""))) if (
        os.environ.get("MIOT_LOG_FILE") or os.name == "nt") else ""

LOG = logging.getLogger("miot-cloud")
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
                    handlers=[logging.StreamHandler()])

def _attach_file_log():
    if not LOG_FILE:
        return
    try:
        log_dir = os.path.dirname(LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.FileHandler) and os.path.abspath(h.baseFilename) == LOG_FILE:
                return
        fh = RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3,
            encoding="utf-8")
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(fh)
    except Exception:
        pass

_attach_file_log()

def _journald_available():
    return os.name != "nt" and shutil.which("journalctl") is not None

def _read_file_log(lines=120):
    if not LOG_FILE:
        return []
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            content = f.readlines()
        return [x.rstrip("\r\n") for x in content[-lines:] if x.strip()]
    except Exception:
        return []

def _clear_file_log():
    if not LOG_FILE:
        return False
    for h in list(logging.getLogger().handlers):
        if isinstance(h, logging.FileHandler) and os.path.abspath(h.baseFilename) == LOG_FILE:
            try:
                h.flush()
                h.close()
            except Exception:
                pass
            logging.getLogger().removeHandler(h)
    try:
        log_dir = os.path.dirname(LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(LOG_FILE, "w", encoding="utf-8"):
            pass
    except Exception:
        return False
    _attach_file_log()
    return True

AUTH_FILE = os.path.join(ROOT, "micloud_auth.json")
PLUGIN_ACCOUNT_FILE = os.environ.get(
    "MIOT_PLUGIN_ACCOUNT_FILE",
    "/var/lib/songloft/jsplugins_data/miot/data/accounts")
PLUGIN_AUTH_BACKUP = os.path.join(ROOT, "micloud_auth.panel-backup.json")
PLUGIN_PENDING_FILE = os.path.join(ROOT, "plugin_pending_auth.json")
PLUGIN_AUTH_SOURCE = "plugin"
DEV_CACHE = os.path.join(ROOT, "cloud_devices.json")
STATE_CACHE = os.path.join(ROOT, "cloud_states.json")
LAYOUT_FILE = os.path.join(ROOT, "cloud_layout.json")
SETTINGS_FILE = os.path.join(ROOT, "cloud_settings.json")
PAGES_DIR = os.path.join(ROOT, "pages")
PAGES_FILE = os.path.join(ROOT, "pages.json")
PAGES_LOCK = threading.RLock()
APP_VERSION = "0.7.7"
APP_NAME = "miot-panel"
try:
    DISCOVERY_PORT = int(os.environ.get("MIOT_DISCOVERY_PORT") or 37020)
except ValueError:
    DISCOVERY_PORT = 37020
CONFIG_FILE = os.path.join(ROOT, "cloud_config.json")
try:
    PORT = int(os.environ.get("MIOT_PORT") or ("8080" if os.name == "nt" else "80"))
except ValueError:
    PORT = 8080 if os.name == "nt" else 80

UPDATE_URL_DEFAULT = os.environ.get(
    "MIOT_UPDATE_URL",
    "https://raw.githubusercontent.com/liaozhenqiang/miot-panel/main/update.json")
UPDATE_DIR = os.path.join(ROOT, "update")
UPDATE_STATUS_FILE = os.path.join(UPDATE_DIR, "status.json")
UPDATE_MAX_BYTES = 40 * 1024 * 1024
UPDATE_CACHE = {"ts": 0.0, "data": None}
UPDATE_CACHE_LOCK = threading.Lock()
UPDATE_RUNNING = {"active": False, "target": ""}
UPDATE_APP_FILES = (
    "miot-cloud.py", "cloud_panel.html", "admin.html", "camera.html",
    "manifest.webmanifest", "sw.js", "sensor_models.json", "miot_models.json.gz",
    "config.example.json", "README.md", "LICENSE", "cacert.pem", "compat.js",
    "android/miot-panel-android.apk",
)
UPDATE_STATES = ("downloading", "verifying", "installing", "restarting",
                 "rolling_back")

GO2RTC_API = os.environ.get("MIOT_GO2RTC", "http://127.0.0.1:1984")
SONGLOFT_API = os.environ.get("MIOT_SONGLOFT", "http://127.0.0.1:58091")
try:
    SONGLOFT_PORT = int(os.environ.get("MIOT_SONGLOFT_PORT")
                        or urllib.parse.urlsplit(SONGLOFT_API).port or 58091)
except ValueError:
    SONGLOFT_PORT = 58091
SONGLOFT_ENV_FILE = os.environ.get("MIOT_SONGLOFT_ENV", "/etc/songloft/songloft.env")
GO_MUSIC_DL_API = os.environ.get("MIOT_GO_MUSIC_DL", "http://127.0.0.1:8080")
try:
    GO_MUSIC_DL_PORT = int(os.environ.get("MIOT_GO_MUSIC_DL_PORT")
                           or urllib.parse.urlsplit(GO_MUSIC_DL_API).port or 8080)
except ValueError:
    GO_MUSIC_DL_PORT = 8080
GO_MUSIC_DL_SERVICE = os.environ.get("MIOT_GO_MUSIC_DL_SERVICE", "go-music-dl")
MIOT_PLUGIN_API = "/api/v1/jsplugin/miot"
GMD_PLUGIN_API = "/api/v1/jsplugin/go-music-dl"
GMD_SEARCH_PATH = GMD_PLUGIN_API + "/api/search/topone"
SONGLOFT_SSO_ENTRY = "panel-sso"
SONGLOFT_SSO_ZIP = os.environ.get(
    "MIOT_SONGLOFT_SSO_ZIP",
    "/var/lib/songloft/jsplugins/panel-sso.jsplugin.zip")
MUSIC_SOURCES = ("netease", "qq", "kugou", "kuwo", "migu",
                 "qianqian", "soda", "apple", "bilibili")
MUSIC_DEFAULT_SOURCES = ("qq", "kuwo", "migu", "bilibili")
_SL_TOKEN = {"token": "", "exp": 0.0}
_SL_LOCK = threading.Lock()
FFMPEG_BIN = shutil.which("ffmpeg")
CAM_TTL = 600
CAM_LOCK = threading.Lock()
CAM_CACHE = {"ts": 0, "items": []}
CAM_META = {"ts": 0, "items": []}
CAM_META_TTL = 300
SPK_CACHE = {"ts": 0, "payload": None}
SPK_BY_ID = {}
SPK_LOCK = threading.Lock()
SPK_RATE = {}
SPK_TTL = 20

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
    "speaker_panel_enabled": True,
    "speaker_text_enabled": True,
    "speaker_voice_enabled": True,
    "speaker_answer_mode": "speak",
    "speaker_max_chars": 200,
    "speaker_hidden": [],
    "speaker_default": "",
    "songloft_url": "",
    "songloft_user": "",
    "songloft_pass": "",
    "go_music_dl_url": "",
    "go2rtc_url": "",
    "plugin_account_file": "",
    "lan_ip": "",
    "update_url": "",
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
    for key in ("speaker_panel_enabled", "speaker_text_enabled",
                "speaker_voice_enabled"):
        out[key] = src.get(key, DEFAULT_CONFIG[key]) is not False
    mode = str(src.get("speaker_answer_mode") or "speak")
    out["speaker_answer_mode"] = mode if mode in ("speak", "text") else "speak"
    try:
        out["speaker_max_chars"] = min(500, max(20, int(src.get("speaker_max_chars") or 200)))
    except Exception:
        out["speaker_max_chars"] = 200
    out["speaker_hidden"] = _str_list(src.get("speaker_hidden"), 500)
    out["speaker_default"] = str(src.get("speaker_default") or "")[:120]
    for key in ("songloft_url", "go_music_dl_url", "go2rtc_url"):
        value = str(src.get(key) or "").strip().rstrip("/")[:500]
        out[key] = value if value.startswith(("http://", "https://")) else ""
    out["songloft_user"] = str(src.get("songloft_user") or "").strip()[:120]
    out["songloft_pass"] = str(src.get("songloft_pass") or "")[:200]
    out["plugin_account_file"] = str(src.get("plugin_account_file") or "")[:500]
    out["lan_ip"] = str(src.get("lan_ip") or "").strip()[:64]
    update_url = str(src.get("update_url") or "").strip()[:500]
    out["update_url"] = update_url if update_url.startswith(("http://", "https://")) else ""
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
THEME_IDS = ("dark", "light", "apple", "flyme", "cyber", "morandi", "pixel")
THEME_LEGACY = {"midnight": "cyber", "forest": "morandi", "sunset": "apple",
                "ocean": "flyme", "rose": "morandi"}

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
        try:
            SPK_CACHE["ts"] = 0
        except Exception:
            pass
        return dict(new)

def config_value(key, default=None):
    return CONFIG.get(key, DEFAULT_CONFIG.get(key, default))

def get_lan_ip():
    """取本机局域网 IPv4，用于生成手机扫码登录的二维码 URL。"""
    override = os.environ.get("MIOT_LAN_IP")
    if override:
        return override.strip()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    if os.name != "nt":
        try:
            out = subprocess.run(["hostname", "-I"], capture_output=True,
                                 text=True, timeout=3).stdout.split()
            for ip in out:
                if ip.startswith("127.") or ":" in ip:
                    continue
                return ip
        except Exception:
            pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return "127.0.0.1"

LAN_IP = get_lan_ip()
if CONFIG.get("lan_ip"):
    LAN_IP = str(CONFIG["lan_ip"])


def _lan_candidates():
    found = []

    def add(ip, name=""):
        ip = str(ip or "").strip()
        if not ip or ip.startswith("127.") or ip.startswith("169.254."):
            return
        try:
            if not ipaddress.ip_address(ip).is_private:
                return
        except ValueError:
            return
        if any(item["ip"] == ip for item in found):
            return
        found.append({"ip": ip, "name": str(name or "")[:60]})

    seen = get_lan_ip()
    if seen:
        add(seen, "当前")
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip, "本机")
    except Exception:
        pass
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 "Get-NetIPAddress -AddressFamily IPv4 | "
                 "Select-Object IPAddress,InterfaceAlias | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=6).stdout
            data = json.loads(out or "[]")
            if isinstance(data, dict):
                data = [data]
            for item in data if isinstance(data, list) else []:
                if isinstance(item, dict):
                    add(item.get("IPAddress"), item.get("InterfaceAlias") or "")
        except Exception:
            pass
    return found


def _camera_firewall_programs():
    if os.name != "nt":
        return []
    paths = _camera_paths()
    return [paths["go2rtc"], paths["ffmpeg"]] if (
        os.path.isfile(paths["go2rtc"]) or os.path.isfile(paths["ffmpeg"])) else []


def _add_firewall_rules(ports, programs=(), udp_ports=()):
    if os.name != "nt":
        return False, "仅 Windows 支持一键放行防火墙"
    ports = [str(p) for p in ports if str(p).isdigit()]
    udp_ports = [str(p) for p in udp_ports if str(p).isdigit()]
    program_list = [str(p) for p in programs if p and os.path.isfile(str(p))]
    script = (
        "$ErrorActionPreference='SilentlyContinue'\n"
        "foreach($p in @(%s)){\n"
        "  Remove-NetFirewallRule -DisplayName \"MIOT Panel TCP $p\" | Out-Null\n"
        "  New-NetFirewallRule -DisplayName \"MIOT Panel TCP $p\" -Direction Inbound "
        "-Protocol TCP -LocalPort $p -Action Allow -Profile Any | Out-Null\n"
        "}\n"
        "foreach($p in @(%s)){\n"
        "  Remove-NetFirewallRule -DisplayName \"MIOT Panel UDP $p\" | Out-Null\n"
        "  New-NetFirewallRule -DisplayName \"MIOT Panel UDP $p\" -Direction Inbound "
        "-Protocol UDP -LocalPort $p -Action Allow -Profile Any | Out-Null\n"
        "}\n"
        "$progs=@(%s)\n"
        "foreach($prog in $progs){\n"
        "  if(Test-Path -LiteralPath $prog){\n"
        "    $name=Split-Path -Leaf $prog\n"
        "    Remove-NetFirewallRule -DisplayName \"MIOT Camera $name\" | Out-Null\n"
        "    New-NetFirewallRule -DisplayName \"MIOT Camera $name\" -Direction Inbound "
        "-Program $prog -Action Allow -Profile Any | Out-Null\n"
        "    New-NetFirewallRule -DisplayName \"MIOT Camera $name Out\" -Direction Outbound "
        "-Program $prog -Action Allow -Profile Any | Out-Null\n"
        "  }\n"
        "}\n" % (",".join(ports), ",".join(udp_ports),
                  ",".join("'%s'" % str(p).replace("'", "''") for p in program_list))
    )
    try:
        path = os.path.join(tempfile.gettempdir(), "miot-panel-firewall.ps1")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        quoted = path.replace("'", "''")
        cmd = ("Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden "
               "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%s'"
               % quoted)
        subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", cmd],
                         creationflags=0x08000000, close_fds=True)
        return True, "已请求管理员权限，请在 UAC 弹窗中确认"
    except Exception as exc:
        return False, str(exc)[:160]

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
    if "haze" in s or "hazy" in s or "smoke" in s or "smoky" in s or "smog" in s:
        return "烟霾"
    if "dust" in s or "sand" in s:
        return "浮尘"
    if "squall" in s or "tornado" in s:
        return "大风"
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

OPEN_METEO_CODES = {
    0: "晴", 1: "少云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒", 80: "阵雨",
    81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "强阵雪", 95: "雷雨",
    96: "雷暴冰雹", 99: "雷暴冰雹",
}


def _open_meteo_text(code):
    try:
        return OPEN_METEO_CODES.get(int(code), "天气未知")
    except Exception:
        return "天气未知"


def _fetch_open_meteo(location):
    geo_url = ("https://geocoding-api.open-meteo.com/v1/search?"
               + urllib.parse.urlencode(
                   {"name": location, "count": 1, "language": "zh", "format": "json"}))
    req = urllib.request.Request(geo_url, headers={"User-Agent": "miot-panel/0.3"})
    with urllib.request.urlopen(req, timeout=12) as r:
        geo = json.loads(r.read().decode("utf-8", "replace"))
    places = geo.get("results") or []
    if not places:
        return None
    place = places[0]
    lat, lon = place.get("latitude"), place.get("longitude")
    if lat is None or lon is None:
        return None
    forecast_url = ("https://api.open-meteo.com/v1/forecast?"
                    + urllib.parse.urlencode({
                        "latitude": lat, "longitude": lon,
                        "current": "temperature_2m,relative_humidity_2m,"
                                   "apparent_temperature,weather_code,wind_speed_10m",
                        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                        "timezone": "auto", "forecast_days": 3,
                    }))
    req = urllib.request.Request(forecast_url, headers={"User-Agent": "miot-panel/0.3"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    cur = data.get("current") or {}
    daily_data = data.get("daily") or {}
    dates = daily_data.get("time") or []
    codes = daily_data.get("weather_code") or []
    highs = daily_data.get("temperature_2m_max") or []
    lows = daily_data.get("temperature_2m_min") or []
    daily = []
    for i, date in enumerate(dates[:3]):
        daily.append({
            "date": date,
            "max": int(round(float(highs[i] or 0))) if i < len(highs) else 0,
            "min": int(round(float(lows[i] or 0))) if i < len(lows) else 0,
            "text": _open_meteo_text(codes[i] if i < len(codes) else None),
        })
    if not cur:
        return None
    return {
        "temp": int(round(float(cur.get("temperature_2m") or 0))),
        "feels": int(round(float(cur.get("apparent_temperature") or 0))),
        "humidity": int(round(float(cur.get("relative_humidity_2m") or 0))),
        "wind": int(round(float(cur.get("wind_speed_10m") or 0))),
        "text": _open_meteo_text(cur.get("weather_code")),
        "high": daily[0]["max"] if daily else 0,
        "low": daily[0]["min"] if daily else 0,
        "daily": daily,
        "ts": int(time.time()),
    }


def _fetch_weather_wttr(location):
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


def fetch_weather():
    location = str(CONFIG.get("weather_location") or "").strip()
    if not location:
        return None
    try:
        weather = _fetch_weather_wttr(location)
        if weather:
            return weather
    except Exception as e:
        LOG.warning("wttr.in weather failed: %s", str(e)[:120])
    try:
        return _fetch_open_meteo(location)
    except Exception as e:
        LOG.warning("open-meteo weather failed: %s", str(e)[:120])
        return None


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
    try:
        return qr.svg_inline(scale=9, border=2, omitsize=True)
    except TypeError:
        svg = qr.svg_inline(scale=9, border=2)
        head_end = svg.find(">")
        head = svg[:head_end]
        try:
            width = head.split('width="', 1)[1].split('"', 1)[0]
            height = head.split('height="', 1)[1].split('"', 1)[0]
        except IndexError:
            return svg
        head = head.replace('width="%s"' % width, 'viewBox="0 0 %s %s"' % (width, height), 1)
        head = head.replace('height="%s"' % height, "", 1)
        return head + svg[head_end:]

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
QR_SESSIONS = {}   # session_id -> 原生扫码登录上下文
QR_LOCK = threading.RLock()
AUTH = None    # 当前登录态（内存缓存）
AUTH_LOCK = threading.RLock()

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

def _read_auth_file():
    try:
        if os.path.exists(AUTH_FILE):
            with open(AUTH_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


def _plugin_account():
    """读取 Songloft MIoT 插件的账号存储，返回可用于面板的账号信息。"""
    try:
        with open(_plugin_account_file(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, str):
            data = json.loads(data)
        for acc in data or []:
            if not isinstance(acc, dict):
                continue
            uid = str(acc.get("user_id") or acc.get("id") or "").strip()
            pass_token = str(acc.get("pass_token") or "").strip()
            if not uid or not pass_token:
                continue
            services = acc.get("services") or {}
            io = services.get("xiaomiio") or {}
            return {
                "user_id": uid,
                "username": str(acc.get("account") or uid),
                "pass_token": pass_token,
                "service": io if isinstance(io, dict) else {},
            }
    except Exception as e:
        LOG.debug("read plugin account failed: %s", e)
    return None


def _same_user(a, b):
    return bool(a and b and str(a) == str(b))


def _plugin_auth_from_service(acct):
    svc = acct.get("service") or {}
    token = str(svc.get("service_token") or "")
    ssecurity = str(svc.get("ssecurity") or "")
    try:
        exp_ms = int(svc.get("expires_at") or 0)
    except Exception:
        exp_ms = 0
    if not token or not ssecurity:
        return None
    if exp_ms and exp_ms <= (time.time() + 300) * 1000:
        return None
    return {
        "username": acct["username"],
        "user_id": acct["user_id"],
        "ssecurity": ssecurity,
        "cuser_id": None,
        "pass_token": acct["pass_token"],
        "service_token": token,
        "country": CONFIG.get("country") or "cn",
        "source": PLUGIN_AUTH_SOURCE,
        "expires_at": int(exp_ms / 1000) if exp_ms else int(time.time()) + 10 * 3600,
    }


def _refresh_xiaomiio_by_pass_token(pass_token, user_id, username):
    """用插件账号的 passToken 换一张 xiaomiio serviceToken。"""
    if not pass_token or not user_id:
        return None
    try:
        sess = _new_session()
        sess.cookies.set("passToken", pass_token, domain="account.xiaomi.com")
        sess.cookies.set("userId", str(user_id), domain="account.xiaomi.com")
        r = sess.get("https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true",
                     timeout=15)
        j = _to_json(r.text)
        location = j.get("location")
        if not location:
            return None
        auth, err = _finish_location(sess, location, j)
        if err or not auth:
            return None
        if not _same_user(auth.get("user_id"), user_id):
            LOG.warning("plugin Xiaomi refresh returned a different account; ignored")
            return None
        auth["username"] = username or str(user_id)
        auth["user_id"] = str(user_id)
        auth["country"] = CONFIG.get("country") or "cn"
        auth["pass_token"] = pass_token
        auth["source"] = PLUGIN_AUTH_SOURCE
        auth["expires_at"] = int(time.time()) + 10 * 3600
        return auth
    except Exception as e:
        LOG.warning("plugin Xiaomi token refresh failed: %s", str(e)[:160])
        return None


def _sync_plugin_auth(force=False):
    """把插件里的小米账号同步为面板设备登录；失败时保留原面板登录。"""
    global AUTH
    acct = _plugin_account()
    if not acct:
        return AUTH or _read_auth_file()
    with AUTH_LOCK:
        current = AUTH or _read_auth_file()
        if current and not _same_user(current.get("user_id"), acct["user_id"]):
            LOG.warning("plugin Xiaomi account differs from panel login; keep panel login")
            return current
        if (not force and current
                and current.get("source") == PLUGIN_AUTH_SOURCE
                and current.get("service_token")
                and float(current.get("expires_at") or 0) > time.time() + 300):
            if current.get("pass_token") != acct.get("pass_token"):
                current["pass_token"] = acct.get("pass_token")
                _save_auth(current)
            return current
        auth = _plugin_auth_from_service(acct)
        if not auth:
            auth = _refresh_xiaomiio_by_pass_token(
                acct.get("pass_token"), acct.get("user_id"), acct.get("username"))
        if auth:
            if current and current.get("source") != PLUGIN_AUTH_SOURCE:
                try:
                    if not os.path.exists(PLUGIN_AUTH_BACKUP):
                        shutil.copy2(AUTH_FILE, PLUGIN_AUTH_BACKUP)
                        os.chmod(PLUGIN_AUTH_BACKUP, 0o600)
                except Exception:
                    pass
            _save_auth(auth)
            LOG.info("panel Xiaomi login linked from MIoT plugin")
            return auth
        return current


_IMPORT_LOCK = threading.Lock()
_IMPORT_CACHE = {"ts": 0.0, "result": {}}


def _read_pending_plugin_auth():
    try:
        with open(PLUGIN_PENDING_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("user_id") and data.get("pass_token"):
            return data
    except Exception:
        pass
    return None


def _write_pending_plugin_auth(user_id, pass_token, username=""):
    data = {"user_id": str(user_id), "pass_token": str(pass_token),
            "username": str(username or user_id), "created": int(time.time())}
    tmp = PLUGIN_PENDING_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PLUGIN_PENDING_FILE)
    return data


def _clear_pending_plugin_auth():
    try:
        if os.path.exists(PLUGIN_PENDING_FILE):
            os.remove(PLUGIN_PENDING_FILE)
    except OSError:
        pass


def _plugin_connected():
    try:
        _, ver = _songloft_req("GET", "/api/v1/version", timeout=2)
        return bool(isinstance(ver, dict) and str(ver.get("version") or ""))
    except Exception:
        return False


def _plugin_accounts():
    try:
        _, acc = _songloft_req("GET", MIOT_PLUGIN_API + "/accounts", timeout=5)
        data = acc.get("data") if isinstance(acc, dict) else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _import_panel_auth_to_plugin(force=False, auth=None):
    """把面板的小米登录传给 Songloft MIoT 插件，免去第二次扫码。"""
    pending = _read_pending_plugin_auth()
    auth = auth or _load_auth() or pending
    if pending and (not auth or str(auth.get("user_id") or "") != str(pending.get("user_id") or "")):
        auth = pending
    if not auth or not auth.get("user_id") or not auth.get("pass_token"):
        return {"ok": False, "error": "没有可同步的小米登录"}
    user_id = str(auth.get("user_id"))
    pass_token = str(auth.get("pass_token"))
    if not _plugin_connected():
        if not pending:
            _write_pending_plugin_auth(user_id, pass_token, auth.get("username") or "")
        return {"ok": False, "pending": True,
                "error": "音乐中心未连接，登录已暂存，连接后会自动同步"}
    accounts = _plugin_accounts()
    existing = None
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        if str(acc.get("user_id") or "") == user_id or str(acc.get("id") or "") == user_id:
            existing = acc
            break
    if existing and not force and (existing.get("services") or {}):
        _clear_pending_plugin_auth()
        return {"ok": True, "already": True, "account_id": existing.get("id") or user_id}
    account_id = str((existing or {}).get("id") or user_id)
    try:
        code, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/auth/token", {
            "account_id": account_id, "user_id": user_id, "pass_token": pass_token,
        }, timeout=45)
    except Exception as exc:
        _write_pending_plugin_auth(user_id, pass_token, auth.get("username") or "")
        return {"ok": False, "pending": True,
                "error": "同步到音乐中心失败：%s" % str(exc)[:180]}
    if code not in (200, 201, 202) or (isinstance(resp, dict) and resp.get("success") is False):
        detail = ""
        if isinstance(resp, dict):
            detail = resp.get("error") or resp.get("detail") or ""
        _write_pending_plugin_auth(user_id, pass_token, auth.get("username") or "")
        return {"ok": False, "pending": True,
                "error": (detail or "音乐中心拒绝了登录同步")[:220]}
    _clear_pending_plugin_auth()
    LOG.info("panel Xiaomi login imported to MIoT plugin")
    return {"ok": True, "imported": True, "account_id": account_id}


def _maybe_import_panel_auth(force=False):
    now = time.time()
    with _IMPORT_LOCK:
        if not force and now - float(_IMPORT_CACHE.get("ts") or 0) < 30:
            return dict(_IMPORT_CACHE.get("result") or {})
    pending = _read_pending_plugin_auth()
    auth = _load_auth()
    if auth and auth.get("source") == PLUGIN_AUTH_SOURCE and not pending:
        result = {"ok": True, "already": True, "source": "plugin"}
    else:
        result = _import_panel_auth_to_plugin(force=force, auth=auth or pending)
    with _IMPORT_LOCK:
        _IMPORT_CACHE["ts"] = time.time()
        _IMPORT_CACHE["result"] = dict(result)
    return result


def _qr_cleanup_locked():
    now = time.time()
    for sid, s in list(QR_SESSIONS.items()):
        if now - float(s.get("created") or 0) > 600:
            QR_SESSIONS.pop(sid, None)
    if len(QR_SESSIONS) > 20:
        for sid in list(QR_SESSIONS)[:-20]:
            QR_SESSIONS.pop(sid, None)


def _qr_start():
    """原生小米扫码登录：不依赖 Songloft，登录后再把凭据交给插件。"""
    if segno is None:
        raise RuntimeError("服务端缺少 segno 二维码组件")
    sess = _new_session()
    device_id = _rand(16).upper()
    sess.headers["User-Agent"] = UA % device_id
    sess.cookies.update({"sdkVersion": "3.8.6", "deviceId": device_id})
    r = sess.get("https://account.xiaomi.com/pass/serviceLogin",
                 params={"sid": "mijia", "_json": "true"}, timeout=15)
    j = _to_json(r.text)
    sign = str(j.get("_sign") or "")
    qs = str(j.get("qs") or "")
    callback = str(j.get("callback") or "")
    if not sign or not qs or not callback:
        raise RuntimeError("小米登录参数获取失败，请稍后重试")
    params = {
        "_qrsize": "240", "qs": qs, "sid": "mijia", "_sign": sign,
        "callback": callback, "_json": "true",
        "_dc": str(int(time.time() * 1000)),
    }
    r2 = sess.get("https://account.xiaomi.com/longPolling/loginUrl",
                  params=params, timeout=15)
    q = _to_json(r2.text)
    code = int(q.get("code") or 0)
    if code != 0:
        raise RuntimeError("获取二维码失败（code=%s %s）" % (code, q.get("desc") or ""))
    login_url = str(q.get("loginUrl") or "").strip()
    poll_url = str(q.get("lp") or "").strip()
    if not login_url.startswith("http") or not poll_url:
        raise RuntimeError("小米未返回可用的登录二维码")
    svg = build_qr(login_url)
    if svg.lstrip().startswith("<svg") and "xmlns=" not in svg.split(">", 1)[0]:
        svg = svg.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1)
    data_url = "data:image/svg+xml;base64," + base64.b64encode(
        svg.encode("utf-8")).decode("ascii")
    sid = _rand(24)
    with QR_LOCK:
        _qr_cleanup_locked()
        QR_SESSIONS[sid] = {
            "sess": sess, "poll_url": poll_url, "created": time.time(),
            "poll_count": 0, "in_flight": False, "non_zero": 0,
            "device_id": device_id,
        }
    return {"ok": True, "session_id": sid, "qrcode_url": data_url,
            "login_url": login_url, "expires_in": 120, "native": True}


def _qr_poll(sid):
    with QR_LOCK:
        s = QR_SESSIONS.get(sid)
        if not s:
            return {"ok": False, "state": "expired",
                    "message": "二维码已失效，请刷新"}
        if s.get("in_flight"):
            return {"ok": True, "state": "waiting", "message": "等待扫码…"}
        s["in_flight"] = True
    try:
        with QR_LOCK:
            s["poll_count"] = int(s.get("poll_count") or 0) + 1
            count = s["poll_count"]
        if count > 40:
            with QR_LOCK:
                QR_SESSIONS.pop(sid, None)
            return {"ok": True, "state": "expired", "message": "二维码已过期，请刷新"}
        try:
            r = s["sess"].get(s["poll_url"], timeout=20,
                              headers={"X-Fetch-Timeout-Ms": "19000"})
        except Exception:
            return {"ok": True, "state": "waiting", "message": "等待扫码…"}
        if getattr(r, "status_code", 0) == 403:
            with QR_LOCK:
                QR_SESSIONS.pop(sid, None)
            return {"ok": True, "state": "expired", "message": "二维码已过期，请刷新"}
        try:
            q = _to_json(r.text)
        except Exception:
            return {"ok": True, "state": "waiting", "message": "等待扫码…"}
        code = int(q.get("code") or 0)
        if code != 0:
            with QR_LOCK:
                s["non_zero"] = int(s.get("non_zero") or 0) + 1
                streak = s["non_zero"]
            if streak >= 2:
                with QR_LOCK:
                    QR_SESSIONS.pop(sid, None)
                return {"ok": True, "state": "expired",
                        "message": "二维码已过期，请刷新"}
            return {"ok": True, "state": "waiting", "message": "等待扫码…"}
        with QR_LOCK:
            s["non_zero"] = 0
        pass_token = str(q.get("passToken") or "")
        user_id = str(q.get("userId") or "")
        if not pass_token or not user_id:
            if user_id:
                return {"ok": True, "state": "scanned",
                        "message": "已扫码，请在手机上确认"}
            return {"ok": True, "state": "waiting", "message": "等待扫码…"}
        auth = _refresh_xiaomiio_by_pass_token(pass_token, user_id, user_id)
        if not auth:
            with QR_LOCK:
                QR_SESSIONS.pop(sid, None)
            return {"ok": True, "state": "failed",
                    "message": "扫码成功，但换取面板登录失败，请刷新二维码重试"}
        auth["source"] = "qrcode"
        auth["username"] = user_id
        auth["pass_token"] = pass_token
        auth["user_id"] = user_id
        auth["c_user_id"] = q.get("cUserId") or auth.get("cuser_id")
        auth["country"] = CONFIG.get("country") or "cn"
        auth["expires_at"] = int(time.time()) + 10 * 3600
        _save_auth(auth)
        with QR_LOCK:
            QR_SESSIONS.pop(sid, None)
        msg = "扫码登录成功"
        importing = False
        pending = False
        try:
            if _plugin_connected():
                importing = True
                msg += "，正在同步到音乐中心"
                threading.Thread(target=_import_panel_auth_to_plugin,
                                 kwargs={"force": True, "auth": auth},
                                 daemon=True).start()
            else:
                _write_pending_plugin_auth(user_id, pass_token, auth.get("username") or "")
                pending = True
                msg += "，音乐中心未安装，登录已暂存"
        except Exception as exc:
            LOG.warning("queue plugin import after QR failed: %s", str(exc)[:160])
            pending = True
            msg += "，音乐中心同步稍后重试"
        return {"ok": True, "state": "success", "user_id": user_id,
                "imported": False, "importing": importing,
                "pending": pending, "message": msg}
    finally:
        with QR_LOCK:
            if sid in QR_SESSIONS:
                QR_SESSIONS[sid]["in_flight"] = False


def _startup_import():
    time.sleep(8)
    try:
        _maybe_import_panel_auth()
    except Exception as exc:
        LOG.debug("startup auth import failed: %s", exc)


def _ensure_auth_fresh():
    auth = _load_auth()
    if not auth:
        return None
    try:
        expires_at = float(auth.get("expires_at") or 0)
    except Exception:
        expires_at = 0
    if not expires_at or expires_at > time.time() + 300:
        return auth
    with AUTH_LOCK:
        auth = AUTH or _read_auth_file() or auth
        try:
            expires_at = float(auth.get("expires_at") or 0)
        except Exception:
            expires_at = 0
        if not expires_at or expires_at > time.time() + 300:
            return auth
        acct = _plugin_account()
        if acct and _same_user(auth.get("user_id"), acct.get("user_id")):
            refreshed = _sync_plugin_auth(force=True)
            if refreshed and refreshed.get("service_token"):
                return refreshed
        if auth.get("pass_token") and auth.get("user_id"):
            refreshed = _refresh_xiaomiio_by_pass_token(
                auth.get("pass_token"), auth.get("user_id"),
                auth.get("username") or "")
            if refreshed:
                refreshed["source"] = auth.get("source") or "panel"
                _save_auth(refreshed)
                return refreshed
        if auth.get("source") == PLUGIN_AUTH_SOURCE:
            try:
                with open(PLUGIN_AUTH_BACKUP, encoding="utf-8") as f:
                    backup = json.load(f)
                if isinstance(backup, dict) and backup.get("service_token"):
                    _save_auth(backup)
                    LOG.warning("plugin Xiaomi refresh failed; fell back to panel auth backup")
                    return backup
            except Exception:
                pass
        return auth


def _load_auth():
    global AUTH
    if AUTH:
        return AUTH
    try:
        plugin_auth = _sync_plugin_auth()
        if plugin_auth:
            AUTH = plugin_auth
            return AUTH
    except Exception as e:
        LOG.debug("plugin auth sync failed: %s", e)
    AUTH = _read_auth_file()
    return AUTH


def _auth_source():
    auth = _load_auth()
    return (auth or {}).get("source") or "panel"


def get_cloud():
    auth = _ensure_auth_fresh()
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
    if str(model or "") in SENSOR_MODEL_TYPES:
        return "sensor"
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

ROLE_ICON = {"light": "灯", "curtain": "窗", "ac": "空", "plug": "插",
             "fan": "风", "sensor": "感", "other": "设"}

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

# 传感器不按型号写死：sensor_models.json 是 miot-spec 注册表的型号表，
# 运行时再按 type 拉取属性定义，自动识别温度/湿度/电量等 siid/piid。
SENSOR_MODEL_FILE = os.path.join(APP_DIR, "sensor_models.json")
SENSOR_SPEC_FILE = os.path.join(ROOT, "miot_sensor_specs.json")
SENSOR_SPEC_API = os.environ.get(
    "MIOT_SPEC_API", "https://miot-spec.org/miot-spec-v2/instance")
SENSOR_MODEL_TYPES = {}
SENSOR_SPECS = {}
SENSOR_SPEC_TRY = {}
SENSOR_SPEC_LOCK = threading.RLock()
SENSOR_PROP_KEYS = {
    "temperature": ("temp", "温度", "°C"),
    "relative-humidity": ("humidity", "湿度", "%"),
    "humidity": ("humidity", "湿度", "%"),
    "battery-level": ("battery", "电量", "%"),
    "battery": ("battery", "电量", "%"),
    "voltage": ("voltage", "电压", "V"),
    "pm2.5": ("pm25", "PM2.5", "µg/m³"),
    "pm2.5-density": ("pm25", "PM2.5", "µg/m³"),
    "pm10-density": ("pm10", "PM10", "µg/m³"),
    "co2-density": ("co2", "CO₂", "ppm"),
    "tvoc-density": ("tvoc", "TVOC", "mg/m³"),
    "formaldehyde-density": ("formaldehyde", "甲醛", "mg/m³"),
    "illumination": ("illumination", "光照", "lx"),
    "atmospheric-pressure": ("pressure", "气压", "Pa"),
    "electric-power": ("power", "功率", "W"),
    "electric-current": ("current", "电流", "A"),
    "pm1": ("pm1", "PM1", "µg/m³"),
    "air-quality": ("air_quality", "空气质量", ""),
    "air-quality-index": ("aqi", "AQI", ""),
    "uv-index": ("uv", "UV", ""),
    "rain-status": ("rain", "降雨", ""),
    "rainfall-value": ("rainfall", "雨量", "mm"),
    "hcho-density": ("formaldehyde", "甲醛", "mg/m³"),
    "voc-density": ("tvoc", "TVOC", "mg/m³"),
    "not-closed-duration": ("open_duration", "未关时长", "分钟"),
    "contact-state": ("contact", "开合", ""),
    "motion-state": ("motion", "移动", ""),
    "occupancy-status": ("occupancy", "有人", ""),
    "occupancy-state": ("occupancy", "有人", ""),
    "submersion-state": ("water", "漏水", ""),
    "smoke-state": ("smoke", "烟雾", ""),
    "smoke-concentration": ("smoke_conc", "烟雾浓度", ""),
    "gas-concentration": ("gas_conc", "燃气浓度", ""),
    "gas-leak": ("gas", "燃气", ""),
    "alarm": ("alarm", "报警", ""),
    "fault": ("fault", "故障", ""),
    "status": ("status", "状态", ""),
    "ph": ("ph", "pH", ""),
    "tds": ("tds", "TDS", "ppm"),
    "ec": ("ec", "EC", "µS/cm"),
    "salinity": ("salinity", "盐度", ""),
    "orp": ("orp", "ORP", "mV"),
    "chlorine": ("chlorine", "余氯", "mg/L"),
}
SENSOR_PROP_ORDER = {
    "temp": 0, "humidity": 1, "battery": 2, "pm25": 3, "pm10": 4,
    "co2": 5, "tvoc": 6, "formaldehyde": 7, "illumination": 8,
    "pressure": 9, "contact": 10, "motion": 11, "occupancy": 12,
    "water": 13, "smoke": 14, "gas": 15, "fault": 16, "status": 17,
    "smoke_conc": 18, "gas_conc": 19, "voltage": 20, "ph": 21, "tds": 22,
    "ec": 23, "salinity": 24, "orp": 25, "chlorine": 26,
    "pm1": 3, "air_quality": 3, "aqi": 3, "uv": 3, "rain": 3,
    "rainfall": 3, "power": 20, "current": 20, "open_duration": 3,
}
SENSOR_BOOL_TEXT = {
    "contact": ("关闭", "打开"),
    "motion": ("有人", "无人"),
    "occupancy": ("有人", "无人"),
    "water": ("漏水", "正常"),
    "fault": ("故障", "正常"),
    "alarm": ("报警", "正常"),
    "gas": ("泄漏", "正常"),
    "smoke": ("报警", "正常"),
}
SENSOR_STATE_TEXT = {
    "contact": {"open": "打开", "opened": "打开", "closed": "关闭", "close": "关闭"},
    "motion": {"motion": "有人", "no_motion": "无人", "detected": "有人",
               "none": "无人", "someone": "有人", "no_one": "无人",
               "present": "有人", "absent": "无人"},
    "occupancy": {"occupied": "有人", "unoccupied": "无人", "present": "有人",
                  "absent": "无人", "someone": "有人", "no_one": "无人"},
}
_SENSOR_UNIT_MAP = {
    "percentage": "%", "celsius": "°C", "lux": "lx", "pascal": "Pa",
    "mg/m3": "mg/m³", "µg/m3": "µg/m³", "ug/m3": "µg/m³",
    "ppm": "ppm", "mV": "mV", "watt": "W", "kelvin": "K",
    "minutes": "分钟", "seconds": "秒", "hours": "小时", "days": "天",
}
_SENSOR_DESC_MAP = {
    "weak": "弱", "strong": "强", "open": "打开", "opened": "打开",
    "closed": "关闭", "close": "关闭", "motion": "有人",
    "no_motion": "无人", "detected": "有人", "none": "无人",
    "someone": "有人", "no_one": "无人", "present": "有人",
    "absent": "无人", "occupied": "有人", "unoccupied": "无人",
    "normal": "正常", "alarm": "报警", "leak": "漏水", "leaking": "漏水",
    "fault": "故障", "idle": "待机", "running": "运行", "auto": "自动",
    "manual": "手动", "on": "开启", "off": "关闭", "sleep": "睡眠",
}


def _sensor_unit(raw, fallback=""):
    raw = str(raw or "").strip().replace("μ", "µ")
    return _SENSOR_UNIT_MAP.get(raw, raw or fallback or "")


def _sensor_desc(text):
    text = str(text or "").strip()
    return _SENSOR_DESC_MAP.get(text.lower(), text)


def _sensor_display(key, value, meta=None):
    meta = meta or {}
    for item in meta.get("value_list") or []:
        if str(item.get("value")) == str(value):
            return _sensor_desc(item.get("description") or value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "on", "yes"):
            value = True
        elif text in ("false", "0", "off", "no"):
            value = False
        else:
            state = SENSOR_STATE_TEXT.get(key) or {}
            if text in state:
                return state[text]
            return _sensor_desc(value)
    pair = SENSOR_BOOL_TEXT.get(key)
    if not pair:
        return None
    return pair[0] if bool(value) else pair[1]


def _load_sensor_models():
    data = {}
    try:
        with open(SENSOR_MODEL_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            data = {str(k): str(v) for k, v in raw.items() if v}
    except Exception as e:
        LOG.warning("sensor model table unavailable: %s", e)
    # 即使型号表缺失，也保留两个已验证型号
    data.setdefault("miaomiaoce.sensor_ht.t2",
                    "urn:miot-spec-v2:device:temperature-humidity-sensor:0000A00A:miaomiaoce-t2:1")
    data.setdefault("vchon.sensor_ht.mbs17",
                    "urn:miot-spec-v2:device:temperature-humidity-sensor:0000A00A:vchon-mbs17:1")
    SENSOR_MODEL_TYPES.clear()
    SENSOR_MODEL_TYPES.update(data)


def _load_sensor_specs():
    try:
        with open(SENSOR_SPEC_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for model, props in raw.items():
                if isinstance(props, list):
                    clean = [p for p in props if isinstance(p, dict)
                             and isinstance(p.get("siid"), int)
                             and isinstance(p.get("piid"), int)
                             and "format" in p]
                    if clean:
                        SENSOR_SPECS[str(model)] = clean
    except FileNotFoundError:
        pass
    except Exception as e:
        LOG.warning("sensor spec cache load failed: %s", e)


def _save_sensor_specs():
    try:
        os.makedirs(os.path.dirname(SENSOR_SPEC_FILE), exist_ok=True)
        tmp = SENSOR_SPEC_FILE + ".tmp"
        with SENSOR_SPEC_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(SENSOR_SPECS, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, SENSOR_SPEC_FILE)
        try:
            os.chmod(SENSOR_SPEC_FILE, 0o600)
        except OSError:
            pass
    except Exception as e:
        LOG.warning("sensor spec cache save failed: %s", e)


def _sensor_props(model):
    model = str(model or "")
    if not model:
        return []
    with SENSOR_SPEC_LOCK:
        cached = SENSOR_SPECS.get(model)
    if cached is not None:
        return cached
    now = time.time()
    if now < SENSOR_SPEC_TRY.get(model, 0):
        return []
    SENSOR_SPEC_TRY[model] = now + 300
    type_urn = SENSOR_MODEL_TYPES.get(model)
    if not type_urn:
        return []
    try:
        url = SENSOR_SPEC_API + "?type=" + urllib.parse.quote(type_urn, safe="")
        req = urllib.request.Request(url, headers={"User-Agent": "miot-panel/0.3"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            spec = json.loads(resp.read() or b"{}")
    except Exception as e:
        LOG.warning("sensor spec fetch failed: %s (%s)", model, e)
        return []
    props = []
    seen = set()
    for svc in spec.get("services") or []:
        if not isinstance(svc, dict):
            continue
        siid = svc.get("iid")
        for prop in svc.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            ptype = str(prop.get("type") or "")
            parts = ptype.split(":")
            token = parts[3] if len(parts) > 3 and parts[2] == "property" else ""
            meta = SENSOR_PROP_KEYS.get(token)
            if not meta or meta[0] in seen:
                continue
            access = prop.get("access") or []
            if access and "read" not in access:
                continue
            piid = prop.get("iid")
            if not isinstance(siid, int) or not isinstance(piid, int):
                continue
            seen.add(meta[0])
            value_list = []
            for item in prop.get("value-list") or prop.get("value_list") or []:
                if isinstance(item, dict):
                    value_list.append({"value": item.get("value"),
                                       "description": str(item.get("description") or "")})
            props.append({"key": meta[0], "label": meta[1],
                          "unit": _sensor_unit(prop.get("unit"), meta[2]),
                          "format": str(prop.get("format") or ""),
                          "value_list": value_list,
                          "siid": siid, "piid": piid})
    props.sort(key=lambda p: SENSOR_PROP_ORDER.get(p["key"], 99))
    if not props:
        return []
    with SENSOR_SPEC_LOCK:
        SENSOR_SPECS[model] = props
    _save_sensor_specs()
    return props


_load_sensor_models()
_load_sensor_specs()

# ---- 通用设备能力：全量 model→type、可写属性/动作 ----
MIOT_MODEL_FILE = os.path.join(APP_DIR, "miot_models.json.gz")
DEVICE_SPEC_FILE = os.path.join(ROOT, "miot_device_specs.json")
MIOT_MODEL_TYPES = {}
DEVICE_SPECS = {}
DEVICE_SPEC_TRY = {}
DEVICE_SPEC_LOCK = threading.RLock()
GEN_PROP_TOKENS = {
    "on", "switch", "power", "mode", "target-temperature", "temperature",
    "target-humidity", "humidity", "fan-level", "speed-level", "fan-percent",
    "brightness", "color-temperature", "horizontal-swing", "vertical-swing",
    "vertical-angle", "horizontal-angle", "swing", "sleep-mode", "eco", "heater",
    "dryer", "uv", "winter-mode", "quick-cool-switch", "mute", "volume",
    "play-loop-mode", "position", "target-position", "percentage", "light",
    "memory", "default-power-on-state", "anti-flicker", "physical-controls-locked",
    "child-lock", "lock-state", "ringtone",
}
GEN_PROP_LABELS = {
    "on": "电源", "switch": "开关", "power": "电源", "mode": "模式",
    "target-temperature": "目标温度", "temperature": "温度",
    "target-humidity": "目标湿度", "humidity": "湿度", "fan-level": "风速",
    "speed-level": "档位", "fan-percent": "风速百分比", "brightness": "亮度",
    "color-temperature": "色温", "horizontal-swing": "左右摆风",
    "vertical-swing": "上下摆风", "vertical-angle": "上下角度",
    "horizontal-angle": "左右角度", "swing": "摆风",
    "sleep-mode": "睡眠模式", "eco": "节能", "heater": "辅热", "dryer": "干燥",
    "uv": "UV 杀菌", "winter-mode": "冬季模式", "quick-cool-switch": "快速制冷",
    "mute": "静音", "volume": "音量", "play-loop-mode": "循环模式",
    "position": "位置", "target-position": "目标位置", "percentage": "百分比",
    "light": "指示灯", "memory": "断电记忆",
    "default-power-on-state": "上电状态", "anti-flicker": "防闪烁",
    "physical-controls-locked": "童锁", "child-lock": "童锁",
    "lock-state": "锁定", "ringtone": "铃声",
}
GEN_UNITS = {"celsius": "°C", "percentage": "%", "arcdegrees": "°",
             "minutes": "分钟", "seconds": "秒", "watt": "W", "V": "V"}
GEN_ACTION_TOKENS = {
    "toggle", "turn-left", "turn-right", "toggle-mode", "loop-gear",
    "play", "pause", "stop", "previous", "next", "wake-up", "play-radio",
    "start", "dock", "return-to-charge", "start-charge", "stop-charge",
}
GEN_ACTION_LABELS = {
    "toggle": "切换", "turn-left": "左转", "turn-right": "右转",
    "toggle-mode": "切换模式", "loop-gear": "循环档位", "play": "播放",
    "pause": "暂停", "stop": "停止", "previous": "上一首", "next": "下一首",
    "wake-up": "唤醒", "play-radio": "播放电台", "start": "启动",
    "dock": "回充", "return-to-charge": "回充", "start-charge": "开始充电",
    "stop-charge": "停止充电",
}


def _load_miot_models():
    data = {}
    try:
        with gzip.open(MIOT_MODEL_FILE, "rt", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            data = {str(k): str(v) for k, v in raw.items() if v}
    except Exception as e:
        LOG.warning("full miot model table unavailable: %s", e)
    if not data:
        data = dict(SENSOR_MODEL_TYPES)
    MIOT_MODEL_TYPES.clear()
    MIOT_MODEL_TYPES.update(data)


def _load_device_specs():
    try:
        with open(DEVICE_SPEC_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for model, caps in raw.items():
                if isinstance(caps, dict):
                    DEVICE_SPECS[str(model)] = caps
    except FileNotFoundError:
        pass
    except Exception as e:
        LOG.warning("device spec cache load failed: %s", e)


def _save_device_specs():
    try:
        os.makedirs(os.path.dirname(DEVICE_SPEC_FILE), exist_ok=True)
        tmp = DEVICE_SPEC_FILE + ".tmp"
        with DEVICE_SPEC_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(DEVICE_SPECS, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, DEVICE_SPEC_FILE)
        try:
            os.chmod(DEVICE_SPEC_FILE, 0o600)
        except OSError:
            pass
    except Exception as e:
        LOG.warning("device spec cache save failed: %s", e)


def _enum_options(raw):
    out = []
    for item in raw or []:
        if not isinstance(item, dict) or "value" not in item:
            continue
        label = item.get("comment") or item.get("description")
        if not label:
            label = str(item.get("value"))
        out.append({"value": item.get("value"), "label": str(label)})
    return out


def _device_caps(model):
    model = str(model or "")
    if not model:
        return None
    with DEVICE_SPEC_LOCK:
        cached = DEVICE_SPECS.get(model)
    if cached is not None:
        return cached
    now = time.time()
    if now < DEVICE_SPEC_TRY.get(model, 0):
        return None
    DEVICE_SPEC_TRY[model] = now + 300
    type_urn = MIOT_MODEL_TYPES.get(model)
    if not type_urn:
        return None
    try:
        req = urllib.request.Request(
            SENSOR_SPEC_API + "?type=" + urllib.parse.quote(type_urn, safe=""),
            headers={"User-Agent": "miot-panel/0.3"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            spec = json.loads(resp.read() or b"{}")
    except Exception as e:
        LOG.warning("device spec fetch failed: %s (%s)", model, e)
        return None
    props = []
    actions = []
    seen = set()
    for svc in spec.get("services") or []:
        if not isinstance(svc, dict):
            continue
        siid = svc.get("iid")
        for prop in svc.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            ptype = str(prop.get("type") or "")
            parts = ptype.split(":")
            token = parts[3] if len(parts) > 3 and parts[2] == "property" else ""
            if not token or token not in GEN_PROP_TOKENS or token in seen:
                continue
            if "write" not in (prop.get("access") or []):
                continue
            fmt = prop.get("format") or ""
            if fmt == "string":
                continue
            piid = prop.get("iid")
            if not isinstance(siid, int) or not isinstance(piid, int):
                continue
            options = _enum_options(prop.get("value-list"))
            rng = prop.get("value-range")
            if options:
                kind = "enum"
            elif isinstance(rng, list) and len(rng) >= 2:
                kind = "range"
            elif fmt == "bool":
                kind = "bool"
            elif fmt in ("uint8", "uint16", "uint32", "int8", "int16", "int32"):
                kind = "number"
            elif fmt in ("float",):
                kind = "number"
            else:
                continue
            seen.add(token)
            unit = GEN_UNITS.get(prop.get("unit") or "", prop.get("unit") or "")
            props.append({
                "siid": siid, "piid": piid, "token": token,
                "label": GEN_PROP_LABELS.get(token, token.replace("-", " ")),
                "kind": kind, "format": fmt, "unit": unit,
                "range": rng if kind == "range" else None,
                "options": options,
            })
        for action in svc.get("actions") or []:
            if not isinstance(action, dict):
                continue
            atype = str(action.get("type") or "")
            parts = atype.split(":")
            token = parts[3] if len(parts) > 3 and parts[2] == "action" else ""
            if token not in GEN_ACTION_TOKENS or action.get("in"):
                continue
            aiid = action.get("iid")
            if not isinstance(siid, int) or not isinstance(aiid, int):
                continue
            actions.append({"siid": siid, "aiid": aiid, "token": token,
                            "label": GEN_ACTION_LABELS.get(token, token.replace("-", " "))})
    caps = {"type": type_urn, "props": props, "actions": actions}
    with DEVICE_SPEC_LOCK:
        DEVICE_SPECS[model] = caps
    _save_device_specs()
    return caps


_load_miot_models()
_load_device_specs()

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

def _load_pages_meta():
    try:
        with open(PAGES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    items = data.get("items")
    if not isinstance(items, list):
        items = []
    clean = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("id") or "")
        if not pid or not all(c.isalnum() or c in "_-" for c in pid):
            continue
        clean.append({
            "id": pid, "name": str(it.get("name") or "未命名页面")[:40],
            "updated": int(it.get("updated") or 0),
            "size": int(it.get("size") or 0),
        })
    active = str(data.get("active") or "")
    if active and not any(x["id"] == active for x in clean):
        active = ""
    return {"active": active, "items": clean}


def _save_pages_meta(meta):
    os.makedirs(ROOT, exist_ok=True)
    tmp = PAGES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PAGES_FILE)
    try:
        os.chmod(PAGES_FILE, 0o600)
    except OSError:
        pass


def _find_page(meta, pid):
    for it in meta.get("items") or []:
        if it.get("id") == pid:
            return it
    return None


def _read_page_html(pid):
    it = _find_page(_load_pages_meta(), pid)
    if not it:
        return None
    path = os.path.join(PAGES_DIR, pid + ".html")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _active_page():
    with PAGES_LOCK:
        meta = _load_pages_meta()
        pid = meta.get("active") or ""
        it = _find_page(meta, pid)
        if not it:
            if meta.get("active"):
                meta["active"] = ""
                _save_pages_meta(meta)
            return None
        html = _read_page_html(pid)
        if html is None:
            meta["active"] = ""
            _save_pages_meta(meta)
            return None
        return {"id": pid, "name": it.get("name") or "", "version": it.get("updated") or 0,
                "html": html}


def _page_state():
    meta = _load_pages_meta()
    pid = meta.get("active") or ""
    it = _find_page(meta, pid)
    if not it:
        return {"ok": True, "active": "", "name": "", "version": 0}
    if not os.path.exists(os.path.join(PAGES_DIR, pid + ".html")):
        with PAGES_LOCK:
            meta["active"] = ""
            _save_pages_meta(meta)
        return {"ok": True, "active": "", "name": "", "version": 0}
    return {"ok": True, "active": pid, "name": it.get("name") or "",
            "version": it.get("updated") or 0}


def _inject_page_watch(html, pid, version):
    script = ("<script>(function(){var id=" + json.dumps(pid) +
              ",ver=" + json.dumps(str(version)) +
              ";function check(){fetch('/api/page_state',{cache:'no-store'}).then(function(r){return r.json()})"
              ".then(function(s){if(!s||!s.ok)return;if(!s.active||s.active!==id||String(s.version)!==ver){"
              "location.replace('/');}}).catch(function(){})}setInterval(check,8000);check();})();</script>")
    lower = html.lower()
    idx = lower.rfind("</body>")
    if idx >= 0:
        return html[:idx] + script + html[idx:]
    return html + script


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
        if role not in CONTROL_ROLES:
            if role != "sensor" or model not in SENSOR_MODEL_TYPES:
                continue
        if did in hidden:
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

def _go2rtc_local_hosts():
    hosts = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
    try:
        hosts.add(str(LAN_IP or "").strip())
    except Exception:
        pass
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None):
            hosts.add(str(item[4][0]).strip())
    except Exception:
        pass
    return {h for h in hosts if h}


def _go2rtc_host_is_local(value):
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = urllib.parse.urlsplit(raw if "://" in raw else "http://" + raw)
        host = (parsed.hostname or "").strip().lower()
    except Exception:
        return False
    return bool(host) and host in _go2rtc_local_hosts()


def _go2rtc_config_url():
    raw = str(CONFIG.get("go2rtc_url") or "").strip()
    if raw and _go2rtc_host_is_local(raw):
        return ""
    return raw


def _go2rtc_base():
    configured = _go2rtc_config_url()
    if configured:
        return configured.rstrip("/")
    env_url = (os.environ.get("MIOT_GO2RTC")
               or os.environ.get("MIOT_GO2RTC_API") or "").strip()
    if env_url and not _go2rtc_host_is_local(env_url):
        return env_url.rstrip("/")
    return "http://127.0.0.1:1984"


def _go2rtc_put(name, src):
    q = urllib.parse.urlencode({"name": name, "src": src})
    req = urllib.request.Request(_go2rtc_base() + "/api/streams?" + q, method="PUT")
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

def _same_lan_subnet(ip1, ip2):
    try:
        a = ipaddress.ip_network(str(ip1).strip() + "/24", strict=False)
        b = ipaddress.ip_network(str(ip2).strip() + "/24", strict=False)
        return a == b
    except Exception:
        return True


def ensure_cameras(force=False):
    remote = _camera_external_go2rtc()
    ffmpeg_bin = _camera_ffmpeg_bin()
    transcode = bool(ffmpeg_bin) or remote
    local_ip = str(CONFIG.get("lan_ip") or LAN_IP)
    with CAM_LOCK:
        if (force and CAM_CACHE["items"]
                and time.time() - CAM_CACHE["ts"] < 30):
            force = False
        if (not force and CAM_CACHE["items"]
                and time.time() - CAM_CACHE["ts"] < CAM_TTL):
            return {"ok": True, "cameras": CAM_CACHE["items"],
                    "transcode": transcode, "cached": True}
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
            elif not remote and local_ip and ip and not _same_lan_subnet(local_ip, ip):
                item["error"] = "当前电脑与摄像头不在同一网段，请在后台填写远程 go2rtc（如玩客云）"
                item["network_hint"] = True
            else:
                try:
                    src = _camera_p2p_url(c, did, model, ip)
                    ok, err = _go2rtc_put("cam_" + did, src)
                    if not ok:
                        raise RuntimeError(err)
                    if transcode:
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
        return {"ok": True, "cameras": items, "remote": remote,
                "transcode": transcode, "cached": False}

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

def _miotspec(c, path, payload):
    """官方 v2 miotspec 接口；旧 /home/rpc 对部分设备返回空结果（code=0 但没有数据）。"""
    url = c._get_api_url(CONFIG.get("country") or "cn") + path
    resp = c.request(url, {"data": json.dumps(payload)})
    return json.loads(resp)

def _miotspec_values(c, did, specs):
    params = [{"did": did, "siid": siid, "piid": piid} for siid, piid in specs]
    jr = _miotspec(c, "/v2/miotspec/prop/get",
                   {"datasource": 1, "params": params})
    out = {}
    for it in (jr.get("result") or []):
        if isinstance(it, dict) and it.get("code") == 0 and "value" in it:
            out[(it.get("siid"), it.get("piid"))] = it.get("value")
    return out

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
        jr = _miotspec(c, "/v2/miotspec/prop/set",
                       {"params": [{"did": did, "siid": siid, "piid": piid, "value": val}]})
        res = jr.get("result")
        if isinstance(res, list) and res:
            codes = [x.get("code") for x in res if isinstance(x, dict)]
            if codes and all(code in (0, 1) for code in codes):
                return True, None
            return False, str((res[0] or {}).get("message") or jr.get("message") or res)
    except Exception:
        pass
    # 兼容只支持旧 RPC 的少量设备
    try:
        jr = _rpc(c, did, "set_properties",
                  [{"siid": siid, "piid": piid, "value": val}])
    except Exception as e:
        return False, str(e)
    res = jr.get("result")
    if isinstance(res, list) and res:
        ok = all(isinstance(x, dict) and x.get("code") in (0, 1) for x in res)
    else:
        ok = jr.get("code") == 0 or jr.get("message") == "ok"
    return (True, None) if ok else (False, str(jr.get("message") or jr))

def cloud_set_cap(did, siid, piid, value):
    c = get_cloud()
    if not c:
        return False, "未登录"
    try:
        jr = _miotspec(c, "/v2/miotspec/prop/set",
                       {"params": [{"did": did, "siid": siid,
                                    "piid": piid, "value": value}]})
        res = jr.get("result")
        if isinstance(res, dict):
            res = [res]
        codes = [x.get("code") for x in (res or []) if isinstance(x, dict)]
        if codes and all(code in (0, 1) for code in codes):
            return True, None
        msg = str((res[0] or {}).get("message") or jr.get("message") or "")[:160]
        if msg:
            return False, msg
    except Exception as e:
        LOG.warning("generic set failed: %s", str(e)[:120])
    try:
        jr = _rpc(c, did, "set_properties",
                  [{"siid": siid, "piid": piid, "value": value}])
        res = jr.get("result")
        if isinstance(res, dict):
            res = [res]
        codes = [x.get("code") for x in (res or []) if isinstance(x, dict)]
        if codes and all(code in (0, 1) for code in codes):
            return True, None
        return False, str(jr.get("message") or "写入失败")[:160]
    except Exception as e:
        return False, str(e)[:160]


def cloud_call_action(did, siid, aiid):
    c = get_cloud()
    if not c:
        return False, "未登录"
    params = [{"did": did, "siid": siid, "aiid": aiid, "in": []}]
    try:
        jr = _miotspec(c, "/v2/miotspec/action", {"params": params})
        res = jr.get("result")
        if isinstance(res, dict):
            res = [res]
        codes = [x.get("code") for x in (res or []) if isinstance(x, dict)]
        if codes and all(code in (0, 1) for code in codes):
            return True, None
        msg = str((res[0] or {}).get("message") or jr.get("message") or "")[:160]
        if msg:
            return False, msg
    except Exception as e:
        LOG.warning("generic action failed: %s", str(e)[:120])
    try:
        jr = _rpc(c, did, "do_action",
                  [{"siid": siid, "aiid": aiid, "in": []}])
        res = jr.get("result")
        if isinstance(res, dict):
            res = [res]
        codes = [x.get("code") for x in (res or []) if isinstance(x, dict)]
        if codes and all(code in (0, 1) for code in codes):
            return True, None
        return False, str(jr.get("message") or "动作执行失败")[:160]
    except Exception as e:
        return False, str(e)[:160]


def _caps_values(c, did, props):
    if not props:
        return {}
    try:
        vals = _miotspec_values(
            c, did, [(p["siid"], p["piid"]) for p in props])
    except Exception:
        return {}
    return vals


def _coerce_cap(prop, raw):
    kind = prop.get("kind")
    if kind == "bool":
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "on", "open", "yes")
        return bool(raw)
    if kind == "enum":
        for option in prop.get("options") or []:
            if str(option.get("value")) == str(raw):
                return option.get("value")
        raise ValueError("不在可选范围内")
    if kind == "range":
        rng = prop.get("range") or [0, 0, 1]
        num = float(raw)
        lo, hi = float(rng[0]), float(rng[1])
        if num < lo or num > hi:
            raise ValueError("超出范围")
        fmt = prop.get("format") or ""
        if fmt.startswith("uint") or fmt.startswith("int"):
            return int(round(num))
        return round(num, 3)
    if kind == "number":
        fmt = prop.get("format") or ""
        if fmt.startswith("uint") or fmt.startswith("int"):
            return int(raw)
        return float(raw)
    raise ValueError("不支持的类型")


def cloud_get_state(did, model):
    c = get_cloud()
    if not c:
        return {}
    if model in SENSOR_MODEL_TYPES:
        props = _sensor_props(model)
        if not props:
            return None
        try:
            vals = _miotspec_values(
                c, did, [(p["siid"], p["piid"]) for p in props])
        except Exception:
            vals = {}
        out = {}
        for p in props:
            value = vals.get((p["siid"], p["piid"]))
            if value is not None:
                out[p["key"]] = value
        return {"values": out} if out else None
    prof = profile_for(model)
    specs = [{"siid": prof["power"][0], "piid": prof["power"][1]}]
    for k in ("temp", "mode"):
        if k in prof:
            specs.append({"siid": prof[k][0], "piid": prof[k][1]})
    try:
        vals = _miotspec_values(c, did, [(s["siid"], s["piid"]) for s in specs])
    except Exception:
        vals = {}
    if not vals:
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

def cloud_get_states(devs):
    """一次批量读取所有到期设备，避免逐台请求触发云端限流。"""
    c = get_cloud()
    if not c:
        return None
    req = []
    for d in devs:
        if d["model"] in SENSOR_MODEL_TYPES:
            props = _sensor_props(d["model"])
            specs = [(p["siid"], p["piid"]) for p in props]
        else:
            prof = profile_for(d["model"])
            specs = [prof["power"]]
            for k in ("temp", "mode"):
                if k in prof:
                    specs.append(prof[k])
        for siid, piid in specs:
            req.append({"did": d["did"], "siid": siid, "piid": piid})
    if not req:
        return {}
    vals = {}
    try:
        for start in range(0, len(req), 80):
            jr = _miotspec(c, "/v2/miotspec/prop/get",
                           {"datasource": 1, "params": req[start:start + 80]})
            for it in (jr.get("result") or []):
                if isinstance(it, dict) and it.get("code") == 0 and "value" in it:
                    vals[(str(it.get("did")), it.get("siid"), it.get("piid"))] = it.get("value")
    except Exception:
        return None
    states = {}
    for d in devs:
        if d["model"] in SENSOR_MODEL_TYPES:
            new = {}
            for p in _sensor_props(d["model"]):
                v = vals.get((str(d["did"]), p["siid"], p["piid"]))
                if v is not None:
                    new[p["key"]] = v
            if new:
                states[str(d["did"])] = {"values": new}
            continue
        prof = profile_for(d["model"])
        power = vals.get((str(d["did"]), prof["power"][0], prof["power"][1]))
        if power is None:
            continue
        new = {"power": power}
        for k in ("temp", "mode"):
            if k in prof:
                new[k] = vals.get((str(d["did"]), prof[k][0], prof[k][1]))
        states[str(d["did"])] = new
    return states

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
            vals = _miotspec_values(c, did, [(p[0], p[1]) for p in group.values()])
        except Exception:
            vals = {}
        if not vals:
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
STATE_LOCK = threading.RLock()
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
        with STATE_LOCK:
            DEVICES = devs
            DEV_TS = time.time()
        try:
            with open(DEV_CACHE, "w", encoding="utf-8") as f:
                json.dump({"ts": DEV_TS, "devices": devs}, f, ensure_ascii=False)
            os.chmod(DEV_CACHE, 0o600)
        except Exception:
            pass
        now = time.time()
        due = []
        with STATE_LOCK:
            for d in devs:
                st = dict(STATE.get(d["did"], {}))
                st["online"] = d["online"]
                STATE[d["did"]] = st
                if (d["online"] and
                        (st.get("supported") or now >= TRY_NEXT.get(d["did"], 0))):
                    due.append(d)
        states = cloud_get_states(due) if due else {}
        if states is None:
            # 批量接口异常时退回逐台，兼容只支持旧 RPC 的设备/账号。
            states = {}
            for d in due:
                new = cloud_get_state(d["did"], d["model"])
                if new is not None:
                    states[str(d["did"])] = new
                time.sleep(0.25)
        with STATE_LOCK:
            for d in due:
                st = STATE.get(d["did"], {})
                new = states.get(str(d["did"]))
                if new is not None:
                    st.update(new)
                    st["supported"] = True
                    TRY_NEXT.pop(d["did"], None)
                else:
                    TRY_NEXT[d["did"]] = now + 120
                STATE[d["did"]] = st
        if POLL_NO == 1 or POLL_NO % 3 == 0:
            for d in devs:
                with STATE_LOCK:
                    st = STATE.get(d["did"], {})
                    if not (d["online"] and st.get("supported")):
                        continue
                    if d["model"] not in MODEL_METRICS:
                        continue
                m = cloud_get_metrics(d["did"], d["model"])
                if m:
                    with STATE_LOCK:
                        st = STATE.get(d["did"], {})
                        st["metrics"] = m
                        STATE[d["did"]] = st
                time.sleep(0.15)
        try:
            with STATE_LOCK:
                snapshot_states = {k: dict(v) for k, v in STATE.items()}
            with open(STATE_CACHE, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "states": snapshot_states}, f, ensure_ascii=False)
            os.chmod(STATE_CACHE, 0o600)
        except Exception:
            pass
        with STATE_LOCK:
            supported = sum(1 for d in devs
                            if STATE.get(d["did"], {}).get("supported"))
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
    PANEL_HTML = open(os.path.join(APP_DIR, "cloud_panel.html"), encoding="utf-8").read()
except OSError:
    PANEL_HTML = "<h1>cloud_panel.html 缺失</h1>"
try:
    ADMIN_HTML = open(os.path.join(APP_DIR, "admin.html"), encoding="utf-8").read()
except OSError:
    ADMIN_HTML = "<h1>admin.html 缺失</h1>"
try:
    CAMERA_HTML = open(os.path.join(APP_DIR, "camera.html"), encoding="utf-8").read()
except OSError:
    CAMERA_HTML = "<h1>camera.html 缺失</h1>"

def _read_app_asset(name, binary=False):
    try:
        with open(os.path.join(APP_DIR, name), "rb" if binary else "r",
                  **({} if binary else {"encoding": "utf-8"})) as f:
            return f.read()
    except OSError:
        return b"" if binary else ""

PWA_MANIFEST = _read_app_asset("manifest.webmanifest")
PWA_SW = _read_app_asset("sw.js")
COMPAT_JS = _read_app_asset("compat.js")
PWA_ICONS = {
    "/icons/icon-192.png": _read_app_asset("icons/icon-192.png", True),
    "/icons/icon-512.png": _read_app_asset("icons/icon-512.png", True),
}

ANDROID_APK_NAME = "miot-panel-android.apk"
ANDROID_VERSION_NAME = "1.0.2"
ANDROID_VERSION_CODE = 3
ANDROID_RELEASE_URL = "https://github.com/liaozhenqiang/miot-panel/releases/latest"


def _android_apk():
    """Ship the client APK with the panel so phones update over the LAN."""
    for folder in (os.path.join(APP_DIR, "android"), APP_DIR, ROOT):
        path = os.path.join(folder, ANDROID_APK_NAME)
        if os.path.isfile(path):
            try:
                with open(path, "rb") as fh:
                    return fh.read()
            except OSError:
                return b""
    return b""


UNLOCK_HTML = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="google" content="notranslate">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>MIOT Panel 验证</title><style>
:root{--bg:#0a0d13;--card:#121722;--line:rgba(255,255,255,.1);--text:#eef2f7;--muted:#8f9aaa;--accent:#ff6a00;}
*{box-sizing:border-box;margin:0;padding:0}body{min-height:100vh;display:flex;align-items:center;justify-content:center;background:radial-gradient(1200px 600px at 30% 0,#1a2433,#0a0d13);color:var(--text);font-family:'PingFang SC','Noto Sans CJK SC','Microsoft YaHei',sans-serif;padding:20px}
.card{width:min(430px,100%);background:var(--card);border:1px solid var(--line);border-radius:20px;padding:34px 28px;box-shadow:0 24px 80px rgba(0,0,0,.4)}
h1{font-size:28px;margin-bottom:10px}.sub{font-size:14px;color:var(--muted);line-height:1.7;margin-bottom:22px}
label{font-size:13px;color:var(--muted);display:block;margin-bottom:8px}input{width:100%;height:56px;border-radius:12px;border:1px solid var(--line);background:#0b1019;color:var(--text);font-size:24px;letter-spacing:6px;padding:0 16px;outline:none}input:focus{border-color:var(--accent)}
button{width:100%;height:54px;margin-top:16px;border:0;border-radius:12px;background:var(--accent);color:#fff;font-size:18px;font-weight:700;cursor:pointer}button:disabled{opacity:.5}
.msg{min-height:24px;margin-top:14px;color:#ff8a6a;font-size:14px;line-height:1.5}.hint{margin-top:18px;color:var(--muted);font-size:12px;line-height:1.6}
</style></head><body><div class="card"><h1>MIOT Panel</h1>
<div class="sub">此设备首次访问需要验证后台 PIN。验证通过后本设备会记住登录，后续无需重复输入。</div>
<label>后台 PIN</label><input id="pin" type="password" inputmode="numeric" autocomplete="current-password" placeholder="PIN" autofocus onkeydown="if(event.key==='Enter')unlock()">
<button id="btn" onclick="unlock()">验证并进入</button><div class="msg" id="msg"></div>
<div class="hint">PIN 与后台管理 PIN 相同。忘记 PIN 时请在设备上重置面板数据后重新设置。</div></div>
<script>
function next(){var n='';try{n=new URLSearchParams(location.search).get('next')||'';}catch(e){}if(!n||n.charAt(0)!=='/'||n.charAt(1)==='/')n='/';return n;}
async function unlock(){var p=document.getElementById('pin').value;var m=document.getElementById('msg');var b=document.getElementById('btn');if(!p){m.textContent='请输入 PIN';return;}b.disabled=true;m.textContent='正在验证…';try{var r=await fetch('/api/device_unlock',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:p})});var j=await r.json().catch(function(){return {};});if(r.ok&&j.ok){location.replace(next());return;}m.textContent=j.msg||j.error||('验证失败（'+r.status+'）');}catch(e){m.textContent='网络错误：'+e;}b.disabled=false;}
</script></body></html>"""

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
.qr{background:#fff;border-radius:16px;padding:clamp(10px,2vh,18px);display:inline-block;margin:4px auto 8px;}
.qr svg{display:block;width:min(72vw,46vh,340px);height:auto;aspect-ratio:1/1;}
.hint{font-size:12px;color:#8a93a2;line-height:1.7;margin-top:6px;}
.center{text-align:center;}
.linkbtn{margin-top:10px;background:none;border:0;color:#ffb36b;font-size:13px;text-decoration:underline;cursor:pointer;}
.adminlink{margin-top:16px;display:block;text-align:center;color:#8a93a2;font-size:12px;text-decoration:none;}
</style></head><body><div class="card">
<h1>🔐 登录米家账号</h1>

<div id="qrbox" class="center">
  <div class="sub">请用<b>手机相机/微信扫一扫</b>下方二维码，在手机上完成登录；也可以直接使用账号密码登录。</div>
  <div class="qr">@@QR@@</div>
  <div class="hint">扫码后手机会打开登录页 → 输入账号密码（如需验证码在手机上填）→ 本屏将<b>自动跳转</b>到设备面板。</div>
  <button class="linkbtn" onclick="showPhone()">使用账号密码登录（需键盘）</button>
</div>

<div id="form" style="display:none">
  <button class="linkbtn" onclick="showQR()">← 返回扫码登录</button>
  <label>小米账号（手机号/邮箱）</label>
  <input id="user" placeholder="如 13800000000" autocomplete="username" inputmode="text">
  <label>密码</label>
  <input id="pwd" type="password" placeholder="米家 App 登录密码" autocomplete="current-password">
  <button class="btn" onclick="doLogin()">登录</button>
  <div class="msg" id="msg"></div>
  <div class="note">与米家 App 同源的小米云登录。账号密码仅在本机一次性用于拉取设备与控制，不经过任何第三方。若触发短信/邮箱验证码，登录后会在本页提示输入。没有键盘时，可用手机打开同一地址。</div>
</div>
<a class="adminlink" href="/admin">后台管理</a>
</div>
<script>
var API='';
function showPhone(){document.getElementById('qrbox').style.display='none';document.getElementById('form').style.display='block';}
function showQR(){document.getElementById('qrbox').style.display='block';document.getElementById('form').style.display='none';}
var phone=/[?&]m=phone/.test(location.search);
if(phone){showPhone();}
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


SETUP_HTML = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="google" content="notranslate">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>MIOT Panel 首次设置</title><style>
:root{--bg:#0d1117;--card:#171d27;--line:rgba(255,255,255,.1);--txt:#eef2f7;--muted:#98a2b3;--accent:#ff7a18;--ok:#35c77a;--err:#ff7a6a;}
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;background:radial-gradient(1000px 600px at 85% -10%,rgba(255,105,0,.18),transparent 60%),linear-gradient(165deg,#151b27,#0b0f15);color:var(--txt);font-family:'PingFang SC','Noto Sans CJK SC',sans-serif;display:flex;justify-content:center;padding:22px}
.wrap{width:100%;max-width:820px}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;margin-bottom:14px}
h1{font-size:26px;line-height:1.2}
.sub{color:var(--muted);font-size:13px;line-height:1.7;margin-top:6px}
.link{color:var(--accent);text-decoration:none;font-size:13px;white-space:nowrap;margin-top:4px}
.steps{display:flex;gap:7px;margin:12px 0 16px;flex-wrap:wrap}
.step{flex:1;min-width:110px;text-align:center;font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:9px;padding:8px 6px;background:rgba(255,255,255,.025)}
.step.active{color:#ffb36b;border-color:rgba(255,122,24,.55);background:rgba(255,122,24,.09)}
.step.done{color:var(--ok);border-color:rgba(53,199,122,.4);background:rgba(53,199,122,.07)}
.card{background:rgba(23,29,39,.92);border:1px solid var(--line);border-radius:18px;padding:22px;margin-bottom:14px;box-shadow:0 12px 38px rgba(0,0,0,.18)}
h2{font-size:18px;margin-bottom:8px}
p{font-size:13.5px;color:var(--muted);line-height:1.75}
input,select{width:100%;padding:12px 13px;border-radius:10px;border:1px solid var(--line);background:rgba(0,0,0,.24);color:#fff;font-size:15px;margin-top:8px}
button,.button{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:11px 16px;border-radius:10px;border:1px solid var(--line);background:#222a36;color:var(--txt);font-size:14px;font-weight:600;cursor:pointer;text-decoration:none}
button.primary,.button.primary{background:var(--accent);border-color:transparent;color:#fff}
button:disabled{opacity:.5;cursor:wait}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.hidden{display:none!important}
.hint{font-size:12px;color:var(--muted);line-height:1.6;margin-top:8px}
.msg{font-size:13px;color:var(--err);min-height:18px;margin-top:8px;line-height:1.5}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin:8px 0}
.chip{font-size:12px;padding:6px 10px;border-radius:999px;background:rgba(255,255,255,.05);border:1px solid var(--line);color:var(--muted)}
.chip.ok{color:var(--ok);border-color:rgba(53,199,122,.35);background:rgba(53,199,122,.08)}
.chip.off{color:var(--err);border-color:rgba(255,122,106,.35);background:rgba(255,122,106,.07)}
.bar{height:9px;background:rgba(255,255,255,.08);border-radius:999px;overflow:hidden;margin-top:12px}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#ff7a18,#ffb36b);transition:width .3s}
details{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
summary{cursor:pointer;color:#ffb36b;font-size:13px}
.qr{background:#fff;border-radius:14px;padding:12px;display:inline-block;margin:12px 0}
.qr img{display:block;width:220px;height:220px;object-fit:contain}
.qr .hint{color:#606a78;text-align:center}
.phoneSetup{display:flex;gap:18px;align-items:center;background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:14px;padding:14px;margin:14px 0}
.phoneSetup .qr{margin:0;flex:0 0 auto;padding:8px}
.phoneSetup .qr img,.phoneSetup .qr svg{display:block;width:148px;height:148px}
.phoneSetup .hint{margin-top:6px;color:var(--muted);font-size:13px;line-height:1.6}
@media(max-width:640px){.phoneSetup{flex-direction:column;align-items:flex-start}.phoneSetup .qr img,.phoneSetup .qr svg{width:180px;height:180px}}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#232c3a;border:1px solid var(--line);border-radius:10px;padding:10px 16px;font-size:13px;opacity:0;pointer-events:none;transition:.25s;max-width:90vw}
.toast.show{opacity:1}
@media(max-width:640px){body{padding:12px}h1{font-size:21px}.card{padding:16px}.grid{grid-template-columns:1fr}.step{min-width:0;flex:1;font-size:11px}}
</style></head><body>
<div class="wrap">
  <header><div><h1>MIOT Panel 首次设置</h1><div class="sub">按顺序完成：后台 PIN → 音乐中心（可跳过）→ 小米账号。没有音乐中心也可扫码登录；连接后会自动同步，无需二次扫码。</div></div><a class="link" href="/admin">后台</a></header>
  <div class="steps"><span class="step" id="step1">1 后台 PIN</span><span class="step" id="step2">2 音乐中心</span><span class="step" id="step3">3 小米账号</span><span class="step" id="step4">4 完成</span></div>

  <section class="card" id="pinCard">
    <h2>1. 设置后台 PIN</h2>
    <p>PIN 只保护后台管理页，不是小米账号密码。设置后再安装音乐中心并登录小米。</p>
    <div class="phoneSetup">
      <div class="qr">@@SETUP_QR@@</div>
      <div><b>手机扫码，在手机上完成设置</b><div class="hint">手机需和面板在同一局域网。用手机扫码后会打开同一个首次设置向导，电脑上的进度会自动同步。</div></div>
    </div>
    <div id="pinSetupBox" class="hidden">
      <input id="pin1" type="password" inputmode="numeric" placeholder="设置 4-20 位 PIN">
      <input id="pin2" type="password" inputmode="numeric" placeholder="再次输入 PIN">
      <div class="row"><button class="primary" onclick="setupPin()">设置 PIN 并继续</button></div>
    </div>
    <div id="pinLoginBox" class="hidden">
      <p>后台 PIN 已设置，请输入 PIN 继续首次设置。</p>
      <input id="pinLoginInput" type="password" inputmode="numeric" placeholder="后台 PIN">
      <div class="row"><button class="primary" onclick="loginPin()">登录并继续</button></div>
    </div>
    <div id="pinDoneBox" class="hidden"><span class="chip ok">PIN 已设置</span></div>
    <div class="msg" id="pinMsg"></div>
  </section>

  <section class="card hidden" id="musicCard">
    <h2>2. 安装/连接音乐中心</h2>
    <p>音乐中心负责小爱音箱、在线搜索和音乐播放。默认不会自动创建“全屋音箱”组，各音箱保持独立。</p>
    <div class="chips" id="musicChips"></div>
    <div id="musicInstallBox">
      <button class="primary" id="musicInstallBtn" onclick="installMusic()">检测并安装 Windows 音乐中心</button>
      <div class="hint">约 50MB；国内网络会自动尝试镜像；首次可能需要几分钟。安装期间请勿关闭页面。</div>
    </div>
    <div class="bar hidden" id="musicBar"><i id="musicBarFill"></i></div>
    <div class="hint" id="musicProgress"></div>
    <details>
      <summary>已有音乐中心？填写地址连接</summary>
      <div class="grid"><input id="mUrl" placeholder="http://192.168.1.10:58091"><input id="mUser" placeholder="管理员账号"><input id="mPass" type="password" placeholder="管理员密码"></div>
      <div class="row"><button onclick="connectMusic()">连接音乐中心</button></div>
    </details>
    <div class="row"><button onclick="skipMusic()">暂时跳过音乐中心，先扫码登录</button></div>
    <div class="hint" id="musicHint"></div>
    <div class="msg" id="musicMsg"></div>
  </section>

  <section class="card hidden" id="accountCard">
    <h2>3. 登录小米账号</h2>
    <div id="qrBox" class="hidden">
      <p>用米家 App 扫描二维码。面板会直接建立小米登录；已安装/连接音乐中心时会自动同步，不需要再扫一次。默认不自动组网。</p>
      <div class="qr"><img id="qrImg" alt="小米登录二维码"><div class="hint" id="qrState">正在获取二维码…</div></div>
      <div class="row"><button onclick="startQR()">刷新二维码</button></div>
      <div class="hint" id="qrFallback"></div>
      <div class="hint">没有音乐中心也可以先扫码登录；之后安装音乐中心会自动带入登录。</div>
    </div>
    <div id="panelLoginBox" class="hidden">
      <p>扫码失败时仍可使用面板的小米账号登录；登录后可控制设备，音乐中心稍后会自动同步。</p>
      <div class="row"><a class="button primary" href="/login?force=1">使用面板小米账号登录</a></div>
    </div>
    <div class="msg" id="accountMsg"></div>
  </section>

  <section class="card hidden" id="doneCard">
    <h2>4. 完成</h2>
    <p>设置完成。默认各音箱独立，不会自动创建“全屋音箱”组；需要时可在后台“音乐与 AI 音箱”里手动组网。</p>
    <div class="row"><a class="button primary" href="/">进入面板</a><a class="button" href="/admin">打开后台</a><a class="button" href="/setup">重新检查</a></div>
    <div class="hint" id="doneHint"></div>
  </section>

  <div class="toast" id="toast"></div>
</div>
<script>
var S={},qr=null,qrTimer=null,stackTimer=null,skipped=false,busy=false,qrStarting=false,qrFailed=false;
function $(id){return document.getElementById(id);}
function toast(t,e){var x=$('toast');x.textContent=t;x.className='toast show';clearTimeout(x._t);x._t=setTimeout(function(){x.className='toast';},2800);}
async function api(path,body){var o={method:body?'POST':'GET',credentials:'same-origin',headers:{'Content-Type':'application/json'}};if(body)o.body=JSON.stringify(body);var r=await fetch(path,o);var j={};try{j=await r.json();}catch(e){}return {status:r.status,data:j};}
function setStep(n){for(var i=1;i<=4;i++){var el=$('step'+i);el.className='step'+(i===n?' active':(i<n?' done':''));}['pinCard','musicCard','accountCard','doneCard'].forEach(function(id,i){$(id).classList.toggle('hidden',i!==n-1);});}
async function refresh(){try{S=await api('/api/setup_state');if(S.data&&S.data.ok)S=S.data;else S={};}catch(e){S={};}render();}
function render(){
  var pinDone=!!(S.pin_set&&S.admin_authed);
  var accountDone=!!(S.logged_in||S.account_count>0);
  var step=!pinDone?1:(!S.music_connected&&!skipped?2:(!accountDone?3:4));
  setStep(step);
  $('pinSetupBox').classList.toggle('hidden',!!S.pin_set);
  $('pinLoginBox').classList.toggle('hidden',!(S.pin_set&&!S.admin_authed));
  $('pinDoneBox').classList.toggle('hidden',!pinDone);
  if(step===2)renderMusic();
  if(step===3)renderAccount();
  if(step===4){$('doneHint').textContent='面板 v'+(S.version||'')+' · '+(S.music_connected?'音乐中心已连接':'音乐中心未连接')+' · '+(S.logged_in?'小米账号已登录':'账号未同步')+(S.plugin_sync_pending?' · 音乐中心登录待同步':'')+' · http://'+(S.lan_ip||location.hostname)+((S.port&&S.port!==80)?(':'+S.port):'');}
}
function renderMusic(){
  var j=S.job||{};
  var chips='<span class="chip '+(S.music_connected?'ok':'off')+'">音乐中心 '+(S.music_connected?'已连接':'未连接')+'</span>';
  chips+='<span class="chip">'+(S.platform==='windows'?'Windows':'Linux')+'</span>';
  if(j.running)chips+='<span class="chip">安装中</span>';
  $('musicChips').innerHTML=chips;
  var win=S.platform==='windows';
  $('musicInstallBox').classList.toggle('hidden',!win);
  $('musicHint').textContent=win?'':'Linux：在解压包的 music/ 目录运行 sudo bash install-music-stack.sh，或填写已有音乐中心地址。';
  if(j.running||j.percent!==undefined&&j.state!=='idle'){
    $('musicBar').classList.remove('hidden');
    $('musicBarFill').style.width=Math.max(2,Math.min(100,Number(j.percent||0)))+'%';
    $('musicProgress').textContent=(j.message||'正在安装…')+(j.state==='failed'?'（失败）':'');
    if(j.state==='failed'){$('musicInstallBtn').disabled=false;setTimeout(function(){S.job=null;},0);}
    if(j.running)startStackPoll();
  }
}
function renderAccount(){
  var done=!!(S.logged_in||S.account_count>0);
  $('qrBox').classList.toggle('hidden',done);
  $('panelLoginBox').classList.toggle('hidden',done);
  $('accountMsg').textContent='';
  if(!done&&S.admin_authed&&!qr&&!qrStarting&&!qrFailed)startQR();
}
async function setupPin(){
  var a=$('pin1').value,b=$('pin2').value,m=$('pinMsg');
  if(a.length<4||a.length>20){m.textContent='PIN 长度需为 4-20 位';return;}
  if(a!==b){m.textContent='两次 PIN 不一致';return;}
  busy=true;m.textContent='';
  var r=await api('/api/admin/setup',{pin:a});busy=false;
  if(r.data.ok){await refresh();}else m.textContent=r.data.msg||'设置失败';
}
async function loginPin(){
  var p=$('pinLoginInput').value,m=$('pinMsg');if(!p){m.textContent='请输入 PIN';return;}
  busy=true;m.textContent='';
  var r=await api('/api/admin/login',{pin:p});busy=false;
  if(r.data.ok)await refresh();else m.textContent=r.data.msg||'PIN 不正确';
}
async function installMusic(){
  if(busy)return;
  busy=true;$('musicInstallBtn').disabled=true;$('musicMsg').textContent='正在启动安装…';
  var r=await api('/api/admin/music/stack/install',{});busy=false;
  if(r.status===409){$('musicMsg').textContent=r.data.error||'安装正在进行';startStackPoll();return;}
  if(!r.data.ok){$('musicMsg').textContent=r.data.error||'安装失败';$('musicInstallBtn').disabled=false;return;}
  $('musicBar').classList.remove('hidden');startStackPoll();
}
function startStackPoll(){if(stackTimer)return;stackTimer=setInterval(pollStack,1200);pollStack();}
async function pollStack(){
  var r=await api('/api/admin/music/stack/status');if(!r.data||!r.data.ok)return;
  var j=r.data.job||{};
  if(j.message||j.running){
    $('musicBar').classList.remove('hidden');
    $('musicBarFill').style.width=Math.max(2,Math.min(100,Number(j.percent||0)))+'%';
    $('musicProgress').textContent=(j.message||'正在安装…')+(j.state==='failed'?'（失败）':'');
  }
  if(j.state==='failed'){if(stackTimer){clearInterval(stackTimer);stackTimer=null;}$('musicInstallBtn').disabled=false;$('musicMsg').textContent=(j.message||'安装失败')+'。可重试，或改用 Windows 安装器（已内置离线音乐组件）。';}
  else if(j.state==='done'|| (r.data.installed&&r.data.running)){
    if(stackTimer){clearInterval(stackTimer);stackTimer=null;}
    $('musicBarFill').style.width='100%';await refresh();
  }
}
async function connectMusic(){
  var url=$('mUrl').value.trim(),user=$('mUser').value.trim(),pass=$('mPass').value;
  if(!url){$('musicMsg').textContent='请输入音乐中心地址';return;}
  $('musicMsg').textContent='正在连接…';
  var r=await api('/api/admin/music/connect',{url:url,user:user,password:pass});
  if(!r.data.ok){$('musicMsg').textContent=r.data.error||'连接失败';return;}
  $('musicMsg').textContent='连接成功';await refresh();
}
function skipMusic(){skipped=true;qrFailed=false;render();}
async function startQR(){
  if(!S.admin_authed){$('qrState').textContent='请先在第一步输入后台 PIN';return;}
  if(qrStarting)return;qrStarting=true;qrFailed=false;
  if(qrTimer){clearInterval(qrTimer);qrTimer=null;}
  $('qrState').textContent='正在获取二维码…';$('qrFallback').textContent='';
  var r=await api('/api/auth/qr_start',{});
  if(!r.data.ok&&S.music_connected){
    r=await api('/api/admin/music/qr_start',{});
    if(r.data.ok)r.data.plugin=true;
  }
  qrStarting=false;
  if(!r.data.ok){qrFailed=true;$('qrState').textContent=r.data.error||'二维码获取失败';$('panelLoginBox').classList.remove('hidden');return;}
  qr=r.data.plugin?{account_id:r.data.account_id,plugin:true}:{session_id:r.data.session_id,native:true};
  var img=$('qrImg');img.style.display='';img.src=r.data.qrcode_url||'';
  img.onerror=function(){
    img.style.display='none';var fb=$('qrFallback');fb.textContent='二维码图片加载失败，请刷新，或';
    var u=String(r.data.login_url||'');if(/^https:\/\//i.test(u)){var a=document.createElement('a');a.href=u;a.target='_blank';a.rel='noopener';a.textContent='点此打开小米登录页';a.style.color='#ffb36b';fb.appendChild(a);}else{fb.textContent='二维码图片加载失败，请刷新。';}
  };
  $('qrState').textContent='等待扫码…（约 2 分钟有效）';
  qrTimer=setInterval(pollQR,2500);pollQR();
}
async function pollQR(){
  if(!qr)return;
  var r=qr.native
    ? await api('/api/auth/qr_poll',{session_id:qr.session_id})
    : await api('/api/admin/music/qr_poll',{account_id:qr.account_id});
  if(!r.data||!r.data.ok){$('qrState').textContent=(r.data&&r.data.error)||'等待扫码…';return;}
  var st=r.data.state;
  if(st==='success'){
    if(qrTimer){clearInterval(qrTimer);qrTimer=null;}
    var wasNative=!!qr.native;qr=null;
    $('qrState').textContent=r.data.message||'登录成功，正在同步…';
    await refresh();
    if(S.logged_in&&S.music_connected&&!S.account_count&&wasNative){
      $('accountMsg').textContent='已登录面板；音乐中心正在同步账号，若未出现请稍候刷新。';
    }
  }
  else if(st==='scanned'){$('qrState').textContent='已扫码，请在手机上确认…';}
  else if(st==='expired'||st==='failed'||st==='error'){if(qrTimer){clearInterval(qrTimer);qrTimer=null;}qr=null;qrFailed=true;$('qrState').textContent=(r.data.message||'二维码已失效，请刷新');$('panelLoginBox').classList.remove('hidden');}
  else {$('qrState').textContent=(r.data.message||'等待扫码…');}
}
refresh();setInterval(function(){if(!busy)refresh();},3000);setInterval(function(){if(S.admin_authed&&!qr&&!qrStarting&&!qrFailed&&!S.logged_in&&!S.account_count)startQR();},2500);
</script></body></html>
"""


# ----------------------------------------------------------------- 管理后台鉴权
ADMIN_SESSIONS = {}
ADMIN_LOCK = threading.Lock()
ADMIN_TTL = 12 * 3600
ADMIN_COOKIE = "miot_admin"
DEVICE_COOKIE = "miot_device"
DEVICE_TTL = 180 * 86400
RATE_LOCK = threading.RLock()
RATE_EVENTS = {}


def _rate_ok(key, limit, window, min_interval=0.0):
    """按 IP/动作滑动窗口限流；达到上限返回 False，不阻塞调用线程。"""
    now = time.time()
    with RATE_LOCK:
        arr = [x for x in (RATE_EVENTS.get(key) or []) if now - x < window]
        if arr and min_interval and now - arr[-1] < min_interval:
            RATE_EVENTS[key] = arr
            return False
        if len(arr) >= limit:
            RATE_EVENTS[key] = arr
            return False
        arr.append(now)
        RATE_EVENTS[key] = arr
        if len(RATE_EVENTS) > 5000:
            for k in [k for k, v in RATE_EVENTS.items() if not v][:200]:
                RATE_EVENTS.pop(k, None)
    return True

def admin_pin_set():
    return bool(CONFIG.get("admin_pin_hash"))


def _needs_onboarding():
    return (not admin_pin_set()) or (not bool(_load_auth()))

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

def _is_loopback(handler):
    try:
        return ipaddress.ip_address(str(handler.client_address[0])).is_loopback
    except Exception:
        return False


def _device_key():
    return ("%s|%s|miot-device-v1" % (
        CONFIG.get("admin_pin_salt") or "",
        CONFIG.get("admin_pin_hash") or "")).encode("utf-8")


def _device_cookie_header():
    exp = int(time.time()) + DEVICE_TTL
    nonce = secrets.token_hex(8)
    sig = hmac.new(_device_key(), ("%d.%s" % (exp, nonce)).encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return "%s=%d.%s.%s; HttpOnly; Path=/; SameSite=Lax; Max-Age=%d" % (
        DEVICE_COOKIE, exp, nonce, sig, DEVICE_TTL)


def _device_cookie_valid(handler):
    if not admin_pin_set():
        return False
    raw = _cookie_value(handler, DEVICE_COOKIE)
    try:
        exp_s, nonce, sig = raw.split(".", 2)
        exp = int(exp_s)
    except (TypeError, ValueError):
        return False
    if exp < time.time():
        return False
    want = hmac.new(_device_key(), ("%d.%s" % (exp, nonce)).encode("utf-8"),
                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


def _device_authed(handler):
    if not admin_pin_set() or _is_loopback(handler):
        return True
    return _device_cookie_valid(handler)


def _admin_authed(handler):
    token = _cookie_value(handler, ADMIN_COOKIE)
    if not token:
        return False
    now = time.time()
    with ADMIN_LOCK:
        exp = ADMIN_SESSIONS.get(token)
        if exp and exp >= now:
            return True
        ADMIN_SESSIONS.pop(token, None)
    return _device_cookie_valid(handler)

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
            "weather_label", "country", "page_size", "hidden_devices", "renames",
            "speaker_panel_enabled", "speaker_text_enabled", "speaker_voice_enabled",
            "speaker_answer_mode", "speaker_max_chars", "speaker_hidden",
            "speaker_default", "songloft_url", "songloft_user", "go_music_dl_url",
            "go2rtc_url", "plugin_account_file", "lan_ip", "update_url")
    data = {k: CONFIG.get(k) for k in keys}
    data["songloft_pass_set"] = bool(CONFIG.get("songloft_pass"))
    return data


# ----------------------------------------------------------------- 后台热更新
def _version_tuple(value):
    parts = []
    for item in str(value or "").strip().lstrip("vV").split("."):
        try:
            parts.append(max(0, int(item)))
        except ValueError:
            parts.append(0)
    return tuple((parts + [0, 0, 0, 0])[:4])


def _update_source():
    value = str(CONFIG.get("update_url") or "").strip()
    if value and not value.startswith(("http://", "https://")):
        value = ""
    return value or UPDATE_URL_DEFAULT


def _update_status_write(state, message="", pct=None, target="", path=None, current=None):
    data = {
        "state": state, "message": str(message)[:500], "ts": int(time.time()),
        "target": str(target or ""), "current": current or APP_VERSION,
    }
    if pct is not None:
        data["percent"] = max(0, min(100, int(pct)))
    try:
        path = path or UPDATE_STATUS_FILE
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        LOG.warning("write update status failed", exc_info=True)
    return data


def _update_status_read():
    try:
        with open(UPDATE_STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"state": "idle"}


def _fetch_update_manifest(force=False):
    now = time.time()
    with UPDATE_CACHE_LOCK:
        if not force and UPDATE_CACHE["data"] and now - UPDATE_CACHE["ts"] < 120:
            return dict(UPDATE_CACHE["data"])
    source = _update_source()
    if not source:
        raise RuntimeError("未配置更新源")
    req = urllib.request.Request(
        source, headers={"User-Agent": "%s/%s" % (APP_NAME, APP_VERSION)})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read(512 * 1024)
    data = json.loads(raw.decode("utf-8", "replace") or "{}")
    if not isinstance(data, dict):
        raise RuntimeError("更新清单格式不正确")
    version = str(data.get("version") or "").strip().lstrip("vV")
    url = str(data.get("url") or "").strip()
    digest = str(data.get("sha256") or "").strip().lower()
    if not version or not url.startswith(("http://", "https://")):
        raise RuntimeError("更新清单缺少 version/url")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise RuntimeError("更新清单的 sha256 无效")
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size > UPDATE_MAX_BYTES:
        raise RuntimeError("更新包超过大小限制")
    result = {
        "version": version, "url": url, "sha256": digest, "size": size,
        "notes": str(data.get("notes") or "")[:4000],
        "released": str(data.get("released") or "")[:40],
    }
    with UPDATE_CACHE_LOCK:
        UPDATE_CACHE["ts"] = now
        UPDATE_CACHE["data"] = result
    return dict(result)


def _file_version(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(400):
                line = f.readline()
                if not line:
                    break
                if line.startswith("APP_VERSION"):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _extract_update_package(path, dest):
    total = 0
    members = []
    with tarfile.open(path, "r:gz") as tar:
        for member in tar:
            name = member.name.replace("\\", "/")
            while name.startswith("./"):
                name = name[2:]
            if not name or name.startswith("/") or ".." in name.split("/"):
                continue
            parts = [p for p in name.split("/") if p and p != "."]
            if not parts or any(p in ("__pycache__", ".git") for p in parts):
                continue
            if len(parts) > 3 or member.issym() or member.islnk() or member.isdev():
                continue
            if not (member.isfile() or member.isdir()):
                continue
            if len(members) >= 300:
                raise RuntimeError("更新包文件数量异常")
            if member.isfile():
                total += int(member.size or 0)
                if total > 60 * 1024 * 1024:
                    raise RuntimeError("更新包解压后超过大小限制")
            members.append((member, parts))
        for member, parts in members:
            target = os.path.join(dest, *parts)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
    for root, _dirs, files in os.walk(dest):
        if "miot-cloud.py" in files and "cloud_panel.html" in files:
            return root
    return ""


def _copy_app_files(src_dir, target_dir):
    copied = []
    for name in UPDATE_APP_FILES:
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(target_dir, name)
        os.makedirs(os.path.dirname(dst) or target_dir, exist_ok=True)
        tmp = dst + ".update-tmp"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        copied.append(name)
    src_icons = os.path.join(src_dir, "icons")
    if os.path.isdir(src_icons):
        dst_icons = os.path.join(target_dir, "icons")
        os.makedirs(dst_icons, exist_ok=True)
        for name in os.listdir(src_icons):
            src = os.path.join(src_icons, name)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(dst_icons, name)
            tmp = dst + ".update-tmp"
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            copied.append("icons/" + name)
    return copied


def _backup_app_files(app_dir, backup_dir):
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    copied = _copy_app_files(app_dir, backup_dir)
    with open(os.path.join(backup_dir, "version.txt"), "w", encoding="utf-8") as f:
        f.write(_file_version(os.path.join(app_dir, "miot-cloud.py")) or APP_VERSION)
    return copied


def _health_version(port, timeout=2):
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/status" % int(port), timeout=timeout) as resp:
            data = json.loads(resp.read() or b"{}")
        if isinstance(data, dict):
            return str(data.get("version") or "")
    except Exception:
        pass
    return ""


def _wait_health(port, version, timeout):
    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        if _health_version(port) == version:
            return True
        time.sleep(1)
    return False


def _spawn_app(app_dir, port):
    env = os.environ.copy()
    env.pop("MIOT_UPDATE_WORKER", None)
    env["MIOT_PORT"] = str(port)
    env["MIOT_APP_DIR"] = app_dir
    args = [sys.executable, os.path.join(app_dir, "miot-cloud.py")]
    try:
        if os.name == "nt":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            subprocess.Popen(args, cwd=app_dir, env=env, close_fds=True,
                             creationflags=flags)
        else:
            subprocess.Popen(args, cwd=app_dir, env=env, close_fds=True,
                             start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        LOG.exception("spawn app failed")
        return False


def _windows_pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid),
                              "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10)
        return ("%d" % int(pid)) in (out.stdout or "")
    except Exception:
        return False


def _wait_pid_gone(pid, timeout=15):
    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        if not _windows_pid_alive(pid):
            return True
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
        except Exception:
            pass
        time.sleep(0.5)
    return not _windows_pid_alive(pid)


def _port_listening_pids(port):
    pids = set()
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, timeout=15)
        for line in (out.stdout or "").splitlines():
            parts = line.split()
            if (len(parts) >= 5 and parts[1].endswith(":%d" % int(port))
                    and parts[3] == "LISTENING" and parts[4].isdigit()):
                pids.add(parts[4])
    except Exception:
        pass
    return pids


def _kill_port_owners(port, exclude=()):
    exclude = set(str(p) for p in exclude)
    exclude.add(str(os.getpid()))
    targets = _port_listening_pids(port) - exclude
    for pid in sorted(targets):
        try:
            subprocess.run(["taskkill", "/PID", pid, "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
        except Exception:
            pass
    for pid in sorted(targets):
        try:
            _wait_pid_gone(int(pid), 10)
        except Exception:
            pass


def _signal_restart(parent_pid, app_dir, port):
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(parent_pid), "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
        except Exception:
            pass
        _wait_pid_gone(parent_pid, 15)
        _kill_port_owners(port)
        time.sleep(0.5)
        _spawn_app(app_dir, port)
    else:
        try:
            os.kill(parent_pid, signal.SIGUSR1)
        except OSError:
            pass


def _restart_self():
    """重启当前面板；Windows 需要独立 helper，避免 taskkill 杀掉执行线程。"""
    if os.name == "nt":
        args = [sys.executable, os.path.join(APP_DIR, "miot-cloud.py"),
                "--restart-helper", str(os.getpid()), APP_DIR, str(PORT)]
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        subprocess.Popen(args, cwd=APP_DIR, close_fds=True, creationflags=flags)
    else:
        try:
            os.kill(os.getpid(), signal.SIGUSR1)
        except OSError:
            pass


def _spawn_update_worker(src_dir, target, backup_dir):
    worker_script = os.path.join(src_dir, "miot-cloud.py")
    if not os.path.isfile(worker_script):
        worker_script = os.path.join(APP_DIR, "miot-cloud.py")
    args = [sys.executable, worker_script, "--update-worker",
            src_dir, target, backup_dir, UPDATE_STATUS_FILE, str(PORT),
            str(os.getpid()), APP_DIR]
    os.makedirs(UPDATE_DIR, exist_ok=True)
    log = open(os.path.join(UPDATE_DIR, "update.log"), "ab")
    env = os.environ.copy()
    env["MIOT_UPDATE_WORKER"] = "1"
    try:
        if os.name == "nt":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            subprocess.Popen(args, cwd=APP_DIR, env=env, stdout=log, stderr=log,
                             close_fds=True, creationflags=flags)
        else:
            subprocess.Popen(args, cwd=APP_DIR, env=env, stdout=log, stderr=log,
                             close_fds=True, start_new_session=True)
    finally:
        log.close()


def _cleanup_update_packages():
    for name in ("download.tar.gz", "download.tar.gz.part"):
        try:
            os.remove(os.path.join(UPDATE_DIR, name))
        except OSError:
            pass
    shutil.rmtree(os.path.join(UPDATE_DIR, "stage"), ignore_errors=True)


def _update_prepare(manifest):
    target = manifest["version"]
    tar_path = os.path.join(UPDATE_DIR, "download.tar.gz")
    part_path = tar_path + ".part"
    stage = os.path.join(UPDATE_DIR, "stage")
    try:
        os.makedirs(UPDATE_DIR, mode=0o700, exist_ok=True)
        _update_status_write("downloading", "开始下载 v%s" % target, 0, target)
        req = urllib.request.Request(
            manifest["url"],
            headers={"User-Agent": "%s/%s" % (APP_NAME, APP_VERSION)})
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "CERTIFICATE_VERIFY_FAILED" in reason or "certificate verify failed" in reason:
                LOG.warning("update TLS chain untrusted; retrying with SHA256-anchored download")
                resp = urllib.request.urlopen(
                    req, timeout=30, context=ssl._create_unverified_context())
            else:
                raise
        with resp, open(part_path, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            last = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                got += len(chunk)
                if got > UPDATE_MAX_BYTES:
                    raise RuntimeError("更新包超过大小限制")
                out.write(chunk)
                if got - last >= 512 * 1024:
                    pct = int(got * 100 / total) if total else 0
                    _update_status_write(
                        "downloading", "已下载 %.1f MB" % (got / 1048576.0),
                        pct, target)
                    last = got
        os.replace(part_path, tar_path)
        _update_status_write("verifying", "校验更新包", 100, target)
        digest = hashlib.sha256()
        with open(tar_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest().lower() != manifest["sha256"]:
            raise RuntimeError("SHA256 校验失败，已拒绝安装")
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage, exist_ok=True)
        src_dir = _extract_update_package(tar_path, stage)
        if not src_dir:
            raise RuntimeError("更新包中未找到程序文件")
        for required in ("miot-cloud.py", "cloud_panel.html", "admin.html", "camera.html"):
            if not os.path.isfile(os.path.join(src_dir, required)):
                raise RuntimeError("更新包缺少 %s" % required)
        if _file_version(os.path.join(src_dir, "miot-cloud.py")) != target:
            raise RuntimeError("更新包版本与清单不一致")
        _update_status_write("installing", "正在安装 v%s" % target, 100, target)
        backup_dir = os.path.join(
            UPDATE_DIR, "backup-%s-%s" % (APP_VERSION, target))
        shutil.rmtree(backup_dir, ignore_errors=True)
        _backup_app_files(APP_DIR, backup_dir)
        _spawn_update_worker(src_dir, target, backup_dir)
    except Exception as exc:
        LOG.exception("update prepare failed")
        _update_status_write("failed", str(exc)[:400], None, target)
        for path in (part_path, tar_path):
            try:
                os.remove(path)
            except OSError:
                pass
    finally:
        UPDATE_RUNNING["active"] = False


def _run_update_worker(args):
    if len(args) < 7:
        return
    src_dir, target, backup_dir, status_file, port_s, parent_pid_s, app_dir = args[:7]
    try:
        port = int(port_s)
    except ValueError:
        port = PORT
    try:
        parent_pid = int(parent_pid_s)
    except ValueError:
        parent_pid = 0
    old_version = _file_version(os.path.join(backup_dir, "miot-cloud.py")) or APP_VERSION

    def status(state, message, pct=None, current=None):
        _update_status_write(state, message, pct, target, path=status_file,
                             current=current)

    time.sleep(2.5)
    try:
        status("installing", "正在替换程序文件", 100, target)
        _copy_app_files(src_dir, app_dir)
        status("restarting", "正在重启面板", 100, target)
        _signal_restart(parent_pid, app_dir, port)
        if not _wait_health(port, target, 45):
            _kill_port_owners(port)
            _spawn_app(app_dir, port)
        if _wait_health(port, target, 90):
            status("success", "已更新到 v%s" % target, 100, target)
            _cleanup_update_packages()
            return
        status("rolling_back", "新版本未通过健康检查，正在回滚", 100, old_version)
        _copy_app_files(backup_dir, app_dir)
        if not _wait_health(port, old_version, 10) and not _health_version(port):
            _spawn_app(app_dir, port)
        if _wait_health(port, old_version, 45):
            status("rolled_back", "已回滚到 v%s，请检查更新包" % old_version, 100, old_version)
        else:
            status("failed", "更新失败，回滚后面板未恢复，请用安装包重装", 100, old_version)
    except Exception as exc:
        LOG.exception("update worker failed")
        try:
            _copy_app_files(backup_dir, app_dir)
            _signal_restart(parent_pid, app_dir, port)
        except Exception:
            pass
        status("failed", str(exc)[:400], 100)


# ----------------------------------------------------------------- 音乐 / 小爱音箱
def _read_kv_file(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _songloft_base():
    configured = str(CONFIG.get("songloft_url") or "").strip().rstrip("/")
    return configured or SONGLOFT_API.rstrip("/")


def _songloft_port():
    try:
        parsed = urllib.parse.urlsplit(_songloft_base())
        return parsed.port or (443 if parsed.scheme == "https" else 58091)
    except ValueError:
        return 58091


def _songloft_public_url():
    base = _songloft_base()
    try:
        parsed = urllib.parse.urlsplit(base)
        host = (parsed.hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            return "http://%s:%d" % (LAN_IP, parsed.port or _songloft_port())
    except ValueError:
        pass
    return base


def _songloft_server_url():
    return _songloft_public_url()


def _songloft_creds():
    user = str(CONFIG.get("songloft_user") or "").strip()
    pwd = str(CONFIG.get("songloft_pass") or "")
    if user and pwd:
        return user, pwd
    creds = _read_kv_file(SONGLOFT_ENV_FILE)
    return creds.get("ADMIN_USERNAME") or "", creds.get("ADMIN_PASSWORD") or ""


def _gmd_base():
    configured = str(CONFIG.get("go_music_dl_url") or "").strip().rstrip("/")
    return configured or GO_MUSIC_DL_API.rstrip("/")


def _gmd_port():
    try:
        parsed = urllib.parse.urlsplit(_gmd_base())
        return parsed.port or (443 if parsed.scheme == "https" else 8080)
    except ValueError:
        return 8080


def _plugin_account_file():
    return str(CONFIG.get("plugin_account_file") or PLUGIN_ACCOUNT_FILE)


def _clear_songloft_token():
    with _SL_LOCK:
        _SL_TOKEN["token"] = ""
        _SL_TOKEN["exp"] = 0.0


# ----------------------------------------------------------------- Windows 音乐组件
MUSIC_STACK_DIR = os.path.join(ROOT, "music-stack")
MUSIC_STACK_SRC = os.path.join(APP_DIR, "music-stack")
MUSIC_STACK_LOCK = threading.Lock()
MUSIC_STACK_JOB = {"running": False, "state": "idle", "message": "",
                   "percent": 0, "ts": 0.0}
MUSIC_STACK_JOB_LOCK = threading.Lock()


def _stack_job_set(state, message="", percent=None):
    with MUSIC_STACK_JOB_LOCK:
        MUSIC_STACK_JOB["state"] = str(state)
        MUSIC_STACK_JOB["message"] = str(message)[:300]
        if percent is not None:
            MUSIC_STACK_JOB["percent"] = max(0, min(100, int(percent)))
        MUSIC_STACK_JOB["ts"] = time.time()


def _stack_job_get():
    with MUSIC_STACK_JOB_LOCK:
        return dict(MUSIC_STACK_JOB)


def _stack_paths():
    bin_dir = os.path.join(MUSIC_STACK_DIR, "bin")
    sl_dir = os.path.join(MUSIC_STACK_DIR, "songloft")
    gmd_dir = os.path.join(MUSIC_STACK_DIR, "go-music-dl")
    return {
        "root": MUSIC_STACK_DIR,
        "bin": bin_dir,
        "songloft": sl_dir,
        "songloft_data": os.path.join(sl_dir, "data"),
        "plugins": os.path.join(sl_dir, "data", "jsplugins"),
        "gmd": gmd_dir,
        "gmd_data": os.path.join(gmd_dir, "data"),
        "sl_env": os.path.join(MUSIC_STACK_DIR, "songloft.env"),
        "gmd_admin": os.path.join(MUSIC_STACK_DIR, "gmd-admin.txt"),
        "state": os.path.join(MUSIC_STACK_DIR, "stack.json"),
        "sl_log": os.path.join(MUSIC_STACK_DIR, "songloft.log"),
        "gmd_log": os.path.join(MUSIC_STACK_DIR, "music-dl.log"),
    }


def _stack_state_read():
    try:
        with open(_stack_paths()["state"], encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _stack_state_write(data):
    paths = _stack_paths()
    os.makedirs(paths["root"], exist_ok=True)
    tmp = paths["state"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, paths["state"])


def _stack_creds():
    paths = _stack_paths()
    sl = _read_kv_file(paths["sl_env"])
    gmd = _read_kv_file(paths["gmd_admin"])
    return {
        "songloft_user": sl.get("ADMIN_USERNAME") or "admin",
        "songloft_pass": sl.get("ADMIN_PASSWORD") or "",
        "gmd_user": gmd.get("username") or "admin",
        "gmd_pass": gmd.get("password") or "",
    }


def _port_free(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _port_json(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except Exception:
        return None


def _pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                capture_output=True, text=True, timeout=5).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _port_available(port, probe_path):
    if not _port_free(port):
        return False
    if _wait_http("http://127.0.0.1:%d%s" % (int(port), probe_path), 1):
        return False
    return True


def _wait_http(url, timeout=25, want_json=False):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                raw = resp.read()
                if resp.status < 400:
                    if want_json:
                        return json.loads(raw or b"{}")
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return None


def _stack_popen(args, cwd, log_path):
    os.makedirs(cwd, exist_ok=True)
    log = open(log_path, "ab")
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | 0x08000000)  # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=flags, close_fds=True,
            **({} if os.name == "nt" else {"start_new_session": True}))
        return proc.pid
    finally:
        log.close()


def _stack_stop(disable=False):
    state = _stack_state_read()
    for key in ("songloft_pid", "gmd_pid"):
        pid = state.get(key)
        if not pid:
            continue
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False)
            else:
                os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass
    state["songloft_pid"] = 0
    state["gmd_pid"] = 0
    if disable:
        state["disabled"] = True
    _stack_state_write(state)
    return state


def _stack_login(base, user, pwd, timeout=8):
    req = urllib.request.Request(
        base + "/api/v1/auth/login",
        data=json.dumps({"username": user, "password": pwd}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read() or b"{}")
    token = str(data.get("access_token") or "")
    if not token:
        raise RuntimeError("音乐中心登录失败")
    return token


def _stack_plugins(base, token, timeout=25):
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                base + "/api/v1/jsplugins",
                headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(req, timeout=6) as resp:
                last = json.loads(resp.read() or b"{}").get("plugins") or []
            names = {str(p.get("entry_path") or p.get("name") or "") for p in last
                     if isinstance(p, dict)}
            if {"miot", "go-music-dl", "panel-sso"} <= names:
                return last
        except Exception:
            pass
        time.sleep(1)
    return last


def _stack_setup_gmd(gmd_dir, gmd_url, user, pwd):
    """首次运行 go-music-dl 时用日志里的 setup token 建管理员。"""
    token = ""
    try:
        with open(os.path.join(MUSIC_STACK_DIR, "music-dl.log"),
                  encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Web setup token:" in line:
                    token = line.split("Web setup token:", 1)[1].strip()
    except OSError:
        pass
    if token:
        form = urllib.parse.urlencode({
            "setup_token": token, "username": user,
            "password": pwd, "password_confirm": pwd,
        }).encode("utf-8")
        req = urllib.request.Request(
            gmd_url + "/music/setup", data=form, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status >= 400:
                    raise RuntimeError("go-music-dl 初始化失败")
        except urllib.error.HTTPError as exc:
            if exc.code >= 400:
                raise RuntimeError("go-music-dl 初始化失败: HTTP %s" % exc.code)
    form = urllib.parse.urlencode({"username": user, "password": pwd}).encode("utf-8")
    req = urllib.request.Request(
        gmd_url + "/music/login", data=form, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status < 400 or resp.status in (301, 302, 303)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303):
            return True
        raise RuntimeError("go-music-dl 登录失败: HTTP %s" % exc.code)


def _stack_start_services(sport, gport, user, pwd):
    paths = _stack_paths()
    os.makedirs(paths["songloft_data"], exist_ok=True)
    os.makedirs(paths["gmd_data"], exist_ok=True)
    os.makedirs(paths["plugins"], exist_ok=True)
    state = _stack_state_read()
    sl_exe = os.path.join(paths["bin"], "songloft.exe")
    gmd_exe = os.path.join(paths["bin"], "music-dl.exe")
    if not os.path.isfile(sl_exe) or not os.path.isfile(gmd_exe):
        raise RuntimeError("Windows 音乐组件未安装，请重新安装 MIOT Panel")
    sl_running = bool((_port_json("http://127.0.0.1:%d/api/v1/version" % sport) or {}).get("version"))
    if sl_running and not _pid_alive(state.get("songloft_pid")):
        adopted = False
        if user and pwd:
            try:
                _stack_login("http://127.0.0.1:%d" % sport, user, pwd)
                pids = _port_listening_pids(sport)
                if pids:
                    state["songloft_pid"] = int(sorted(pids)[0])
                    adopted = True
            except Exception:
                adopted = False
        if not adopted:
            raise RuntimeError("端口 %d 已被其他 Songloft 占用，请先关闭它" % sport)
    if not sl_running:
        state["songloft_pid"] = _stack_popen(
            [sl_exe, "-username", user, "-password", pwd,
             "-port", str(sport), "-db", os.path.join("data", "songloft.db")],
            paths["songloft"], paths["sl_log"])
        _stack_state_write(state)
        if _wait_http("http://127.0.0.1:%d/api/v1/version" % sport,
                      30, want_json=True) is None:
            raise RuntimeError("Songloft 启动超时，请查看 music-stack/songloft.log")
    gmd_creds = _stack_creds()
    gmd_running = bool(_wait_http("http://127.0.0.1:%d/music/" % gport, 2))
    if gmd_running and not _pid_alive(state.get("gmd_pid")):
        pids = _port_listening_pids(gport)
        if pids:
            state["gmd_pid"] = int(sorted(pids)[0])
        else:
            raise RuntimeError("端口 %d 已被其他 go-music-dl 占用，请先关闭它" % gport)
    if not gmd_running:
        state["gmd_pid"] = _stack_popen(
            [gmd_exe, "web", "--no-browser", "--port", str(gport)],
            paths["gmd"], paths["gmd_log"])
        _stack_state_write(state)
        if _wait_http("http://127.0.0.1:%d/music/" % gport, 30) is None:
            raise RuntimeError("go-music-dl 启动超时，请查看 music-stack/music-dl.log")
        _stack_setup_gmd(paths["gmd"], "http://127.0.0.1:%d" % gport,
                         gmd_creds["gmd_user"], gmd_creds["gmd_pass"])
    state.update({"installed": True, "songloft_port": sport, "gmd_port": gport,
                  "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
    _stack_state_write(state)
    return state


MUSIC_STACK_CACHE = os.path.join(ROOT, "music-stack-cache")
MUSIC_STACK_DOWNLOADS = (
    ("songloft.exe",
     "https://github.com/songloft-org/songloft/releases/download/v2.13.5/songloft-windows-amd64.exe",
     "6ea4120ff643923a879f4840b362e55d0d77e268e1bab2b1f8e6b1b1467c81a7"),
    ("miot.jsplugin.zip",
     "https://github.com/songloft-org/songloft-plugin-miot/releases/download/v2026.10.25.0/miot.jsplugin.zip",
     "40750a25b367fd9838e0f5c1623ed8b4955f6f9d75734653a3cfe6742b9ef22a"),
    ("go-music-dl.jsplugin.zip",
     "https://github.com/songloft-org/songloft-plugin-go-music-dl/releases/download/v2026.10.16/go-music-dl.jsplugin.zip",
     "71135b9f4cb5028fc7ac473e30a61576060310203fb8cafbd1279946c27d08b1"),
    ("panel-sso.jsplugin.zip",
     "https://raw.githubusercontent.com/liaozhenqiang/miot-panel/main/music/panel-sso.jsplugin.zip",
     "f0ad8826172816d358752a277586affe196cd0ae00fcc5d56d1dfc609718f36c"),
)
GMD_WIN_ZIP_URL = "https://github.com/guohuiyuan/go-music-dl/releases/download/v1.1.3/go-music-dl_windows_amd64.zip"
GMD_WIN_ZIP_SHA = "bffb84679bbba84d765331d068e28037b03993ceb493f201bd2ccfce4ce71776"
GMD_WIN_EXE_SHA = "c3bab7b23c5c37187b9209067767ec3fd2991883799696253462a5f2a6f19deb"


def _stack_required_files():
    return ("songloft.exe", "music-dl.exe",
            os.path.join("plugins", "miot.jsplugin.zip"),
            os.path.join("plugins", "go-music-dl.jsplugin.zip"),
            os.path.join("plugins", "panel-sso.jsplugin.zip"))


def _stack_missing(src):
    return [name for name in _stack_required_files()
            if not os.path.isfile(os.path.join(src, name))]


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mirror_candidates(url):
    if url.startswith(("https://github.com/", "https://raw.githubusercontent.com/")):
        return [url, "https://gh-proxy.com/" + url, "https://ghproxy.net/" + url]
    return [url]


def _download_verified(url, dest, expected_sha, timeout=180, progress=None):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and _sha256_file(dest) == expected_sha:
        return dest
    last_error = None
    for candidate in _mirror_candidates(url):
        part = str(dest) + ".part"
        try:
            req = urllib.request.Request(
                candidate, headers={"User-Agent": "%s/%s" % (APP_NAME, APP_VERSION)})
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.URLError as exc:
                reason = str(getattr(exc, "reason", exc))
                if "CERTIFICATE_VERIFY_FAILED" in reason or "certificate verify failed" in reason:
                    LOG.warning("music component TLS chain untrusted; retrying with SHA256-anchored download")
                    resp = urllib.request.urlopen(
                        req, timeout=timeout, context=ssl._create_unverified_context())
                else:
                    raise
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with resp, open(part, "wb") as out:
                for chunk in iter(lambda: resp.read(65536), b""):
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        try:
                            progress(done, total)
                        except Exception:
                            pass
            if _sha256_file(part) != expected_sha:
                raise RuntimeError("SHA256 不匹配")
            os.replace(part, dest)
            return dest
        except Exception as exc:
            last_error = exc
            LOG.warning("music component download failed (%s): %s",
                        candidate.split("/")[2] if "://" in candidate else candidate,
                        str(exc)[:120])
            try:
                os.remove(part)
            except OSError:
                pass
    raise RuntimeError("下载失败（官方源和国内镜像均不可用）：%s" % str(last_error)[:160])


def _stack_download_bundle(progress=None):
    progress = progress or (lambda *a: None)
    src = Path(MUSIC_STACK_CACHE)
    plugins = src / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    items = list(MUSIC_STACK_DOWNLOADS)
    total_items = len(items) + 1
    span = 58.0 / total_items
    index = 0
    for name, url, sha in items:
        index += 1
        label = "Songloft" if name == "songloft.exe" else (
            "panel-sso 插件" if name.startswith("panel-sso") else name)
        base = (index - 1) * span
        progress("正在下载 %s (%d/%d)" % (label, index, total_items), base)

        def on_bytes(done, total, base=base):
            pct = base + (span * done / total if total else 0)
            progress(None, pct)

        target = plugins / name if name.endswith(".jsplugin.zip") else src / name
        _download_verified(url, target, sha, progress=on_bytes)
    progress("正在下载 go-music-dl (5/5)", 4 * span)
    gmd_zip = src / "music-dl.zip"
    _download_verified(GMD_WIN_ZIP_URL, gmd_zip, GMD_WIN_ZIP_SHA,
                       progress=lambda done, total: progress(
                           None, 4 * span + (span * done / total if total else 0)))
    progress("正在解压 go-music-dl", 64)
    exe = src / "music-dl.exe"
    with zipfile.ZipFile(gmd_zip) as zf:
        data = zf.read("music-dl.exe")
    tmp = exe.with_suffix(".exe.part")
    with open(tmp, "wb") as f:
        f.write(data)
    if _sha256_file(tmp) != GMD_WIN_EXE_SHA:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("go-music-dl.exe 校验失败")
    os.replace(tmp, exe)
    progress("组件下载与校验完成", 65)
    return str(src)

def _stack_install(progress=None):
    progress = progress or (lambda *a: None)
    if os.name != "nt":
        raise RuntimeError("一键安装音乐中心仅支持 Windows；Linux 请运行 music/install-music-stack.sh")
    src = MUSIC_STACK_SRC
    missing = _stack_missing(src)
    if missing:
        LOG.info("music stack components missing, downloading: %s", ",".join(missing))
        progress("正在检测组件，准备下载", 1)
        try:
            src = _stack_download_bundle(progress)
        except Exception as exc:
            raise RuntimeError("下载 Windows 音乐组件失败：%s" % str(exc)[:200])
        missing = _stack_missing(src)
        if missing:
            raise RuntimeError("下载后仍缺少组件：%s" % ", ".join(missing))
    progress("正在安装程序文件", 68)
    with MUSIC_STACK_LOCK:
        paths = _stack_paths()
        os.makedirs(paths["root"], exist_ok=True)
        _stack_stop()
        os.makedirs(paths["bin"], exist_ok=True)
        os.makedirs(paths["plugins"], exist_ok=True)
        os.makedirs(paths["songloft_data"], exist_ok=True)
        os.makedirs(paths["gmd_data"], exist_ok=True)
        for name in ("songloft.exe", "music-dl.exe"):
            shutil.copy2(os.path.join(src, name), os.path.join(paths["bin"], name))
        for name in os.listdir(os.path.join(src, "plugins")):
            if name.endswith(".jsplugin.zip"):
                shutil.copy2(os.path.join(src, "plugins", name),
                             os.path.join(paths["plugins"], name))
        for name in ("LICENSE-songloft.txt", "LICENSE-go-music-dl.txt", "README.txt"):
            source = os.path.join(src, name)
            if os.path.isfile(source):
                shutil.copy2(source, os.path.join(paths["root"], name))
        creds = _stack_creds()
        if not creds["songloft_pass"]:
            creds["songloft_pass"] = secrets.token_urlsafe(18)
        if not creds["gmd_pass"]:
            creds["gmd_pass"] = secrets.token_urlsafe(18)
        with open(paths["sl_env"], "w", encoding="utf-8") as f:
            f.write("ADMIN_USERNAME=%s\nADMIN_PASSWORD=%s\n"
                    % (creds["songloft_user"], creds["songloft_pass"]))
        with open(paths["gmd_admin"], "w", encoding="utf-8") as f:
            f.write("username=%s\npassword=%s\n"
                    % (creds["gmd_user"], creds["gmd_pass"]))
        try:
            os.chmod(paths["sl_env"], 0o600)
            os.chmod(paths["gmd_admin"], 0o600)
        except OSError:
            pass
        sport = 58091
        while sport < 58099 and not _port_available(sport, "/api/v1/version"):
            sport += 1
        gport = 8081
        while gport < 8089 and not _port_available(gport, "/music/"):
            gport += 1
        if sport >= 58099 or gport >= 8089:
            raise RuntimeError("找不到空闲的音乐服务端口")
        progress("正在启动 Songloft 和 go-music-dl", 76)
        state = _stack_start_services(sport, gport,
                                      creds["songloft_user"], creds["songloft_pass"])
        state["disabled"] = False
        _stack_state_write(state)
        progress("正在配置插件", 88)
        sl_base = "http://127.0.0.1:%d" % sport
        gmd_base = "http://127.0.0.1:%d" % gport
        token = _stack_login(sl_base, creds["songloft_user"], creds["songloft_pass"])
        plugins = _stack_plugins(sl_base, token)
        server_host = "http://%s:%d" % (LAN_IP, sport)
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + token}
        attempts = time.time() + 20
        while time.time() < attempts:
            try:
                req = urllib.request.Request(
                    sl_base + GMD_PLUGIN_API + "/config",
                    data=json.dumps({
                        "baseUrl": gmd_base, "basePath": "/music",
                        "serverHost": server_host,
                        "gmdAuthUser": creds["gmd_user"],
                        "gmdAuthPass": creds["gmd_pass"],
                    }).encode("utf-8"), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status < 400:
                        break
            except Exception:
                pass
            time.sleep(1)
        try:
            req = urllib.request.Request(
                sl_base + MIOT_PLUGIN_API + "/config",
                data=json.dumps({
                    "server_host": server_host,
                    "external_search_enabled": True,
                    "external_search_no_import": True,
                    "external_search_sources": [{
                        "id": "go-music-dl", "name": "GoMusicDL",
                        "url": GMD_SEARCH_PATH, "token": "", "enabled": True,
                    }],
                    "external_search_timeout": 6,
                    "search_priority": "parallel",
                }).encode("utf-8"), headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=15).close()
        except Exception:
            pass
        save_config({
            "songloft_url": sl_base,
            "songloft_user": creds["songloft_user"],
            "songloft_pass": creds["songloft_pass"],
            "go_music_dl_url": gmd_base,
            "plugin_account_file": os.path.join(
                paths["songloft_data"], "jsplugins_data", "miot", "data", "accounts"),
        })
        _clear_songloft_token()
        _add_firewall_rules([PORT, sport, gport], _camera_firewall_programs(),
                            [DISCOVERY_PORT])
        progress("安装完成", 100)
        return {"ok": True, "songloft_url": sl_base, "go_music_dl_url": gmd_base,
                "songloft_port": sport, "gmd_port": gport,
                "plugins": [{"name": p.get("name"), "status": p.get("status")}
                            for p in plugins if isinstance(p, dict)],
                "server_host": server_host}


def _stack_install_job():
    def report(message, percent=None):
        message = str(message or "").strip()
        if not message:
            previous = _stack_job_get()
            message = previous.get("message") or "正在下载组件"
            state = previous.get("state") or "downloading"
            if state in ("starting", "idle", "done", "failed"):
                state = "downloading"
        elif "完成" in message:
            state = "done"
        elif "下载" in message or "组件" in message or "镜像" in message:
            state = "downloading"
        else:
            state = "installing"
        _stack_job_set(state, message, percent)

    try:
        _stack_install(progress=report)
        try:
            _import_panel_auth_to_plugin(force=True)
        except Exception as exc:
            LOG.debug("import panel auth after stack install failed: %s", exc)
        _stack_job_set("done", "安装完成", 100)
    except Exception as exc:
        LOG.exception("music stack install failed")
        try:
            _stack_stop()
        except Exception:
            pass
        _stack_job_set("failed", str(exc)[:400], 100)
    finally:
        with MUSIC_STACK_JOB_LOCK:
            MUSIC_STACK_JOB["running"] = False


def _stack_start():
    if os.name != "nt":
        raise RuntimeError("Windows 音乐中心管理仅支持 Windows")
    with MUSIC_STACK_LOCK:
        state = _stack_state_read()
        if not state.get("installed"):
            raise RuntimeError("尚未安装 Windows 音乐中心")
        creds = _stack_creds()
        sport = int(state.get("songloft_port") or 58091)
        gport = int(state.get("gmd_port") or 8081)
        state = _stack_start_services(sport, gport,
                                      creds["songloft_user"], creds["songloft_pass"])
        state["disabled"] = False
        _stack_state_write(state)
        save_config({
            "songloft_url": "http://127.0.0.1:%d" % sport,
            "songloft_user": creds["songloft_user"],
            "songloft_pass": creds["songloft_pass"],
            "go_music_dl_url": "http://127.0.0.1:%d" % gport,
        })
        _clear_songloft_token()
        try:
            threading.Thread(target=_import_panel_auth_to_plugin,
                             kwargs={"force": True}, daemon=True).start()
        except Exception:
            pass
        return state


def _stack_autostart():
    if os.name != "nt":
        return
    time.sleep(4)
    try:
        state = _stack_state_read()
        if state.get("installed") and not state.get("disabled"):
            _stack_start()
            LOG.info("Windows music stack started")
    except Exception as exc:
        LOG.warning("Windows music stack autostart failed: %s", str(exc)[:160])



# ----------------------------------------------------------------- 摄像头组件（go2rtc / ffmpeg）
CAMERA_STACK_LOCK = threading.RLock()
CAMERA_STACK_JOB_LOCK = threading.Lock()
CAMERA_STACK_JOB = {"running": False, "state": "idle", "message": "",
                    "percent": 0, "ts": 0.0}
CAMERA_STACK_VERSION = {"go2rtc": "v1.9.15 (隐藏控制台)", "ffmpeg": "b6.1.1"}
CAMERA_COMPONENTS = {
    ("windows", "x64"): {
        "go2rtc": {"url": "https://raw.githubusercontent.com/liaozhenqiang/miot-panel/main/updates/camera/go2rtc_win64_hidden.exe",
                   "sha256": "d1331144e1fc7520be8422aab8b11b8bef73517b2931e4107d9d677636ad1bd9",
                   "size": 21084160, "kind": "binary", "target": "go2rtc.exe"},
        "ffmpeg": {"url": "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/ffmpeg-win32-x64.gz",
                   "sha256": "8883a3dffbd0a16cf4ef95206ea05283f78908dbfb118f73c83f4951dcc06d77",
                   "size": 29581307, "kind": "gz", "target": "ffmpeg.exe",
                   "version": "b6.1.1"},
    },
    ("windows", "x86"): {
        "go2rtc": {"url": "https://raw.githubusercontent.com/liaozhenqiang/miot-panel/main/updates/camera/go2rtc_win32_hidden.exe",
                   "sha256": "61d90f76e707e6cc52409a9293ab51d2dc1605e909914e15567c1022091e33ab",
                   "size": 19799040, "kind": "binary", "target": "go2rtc.exe"},
        "ffmpeg": {"url": "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.0/ffmpeg-win32-ia32.gz",
                   "sha256": "af47ca5de7b9f859f48e4e96ea831bf551b100144b0b871762ab48fba1152f56",
                   "size": 31390495, "kind": "gz", "target": "ffmpeg.exe",
                   "version": "b6.0 (x86)"},
    },
    ("windows", "arm64"): {
        "go2rtc": {"url": "https://raw.githubusercontent.com/liaozhenqiang/miot-panel/main/updates/camera/go2rtc_win_arm64_hidden.exe",
                   "sha256": "3e507bc54951dfbf6ec4989d2fe7442c510a90f3c88b004bb41789b02216d8e5",
                   "size": 19315200, "kind": "binary", "target": "go2rtc.exe"},
        "ffmpeg": {"url": "https://raw.githubusercontent.com/liaozhenqiang/miot-panel/main/updates/camera/ffmpeg_win_arm64.exe.gz",
                   "sha256": "3a8e1e6f473fe96149192d2d49138e1d894f74eb05bb9cc830c27abdf916a099",
                   "size": 48625116, "kind": "gz", "target": "ffmpeg.exe",
                   "version": "ARM64 静态构建"},
    },
    ("linux", "x64"): {
        "go2rtc": {"url": "https://github.com/AlexxIT/go2rtc/releases/download/v1.9.15/go2rtc_linux_amd64",
                   "sha256": "20b46c6910734cb05ecefb234066f36868ff19b42ba1418a7a32e13a3e035712",
                   "size": 6307828, "kind": "binary", "target": "go2rtc"},
        "ffmpeg": {"url": "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/ffmpeg-linux-x64.gz",
                   "sha256": "bfe8a8fc511530457b528c48d77b5737527b504a3797a9bc4866aeca69c2dffa",
                   "size": 29354986, "kind": "gz", "target": "ffmpeg"},
    },
    ("linux", "arm"): {
        "go2rtc": {"url": "https://github.com/AlexxIT/go2rtc/releases/download/v1.9.15/go2rtc_linux_arm",
                   "sha256": "cbffe4075a004e2015fd9d7e324800ab7968b52f49e9ce803361879d5e246028",
                   "size": 5092480, "kind": "binary", "target": "go2rtc"},
        "ffmpeg": {"url": "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/ffmpeg-linux-arm.gz",
                   "sha256": "64b115a12f0ab77c277e3c418aae8b40ef881e75e746a0e2d066a206b9bc5172",
                   "size": 19167563, "kind": "gz", "target": "ffmpeg"},
    },
    ("linux", "arm64"): {
        "go2rtc": {"url": "https://github.com/AlexxIT/go2rtc/releases/download/v1.9.15/go2rtc_linux_arm64",
                   "sha256": "af416f22b21a24ab83b8d78642190b4932ae695b4e386853f11d1b0319781763",
                   "size": 5116108, "kind": "binary", "target": "go2rtc"},
        "ffmpeg": {"url": "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/ffmpeg-linux-arm64.gz",
                   "sha256": "754a678672298bc68156adff58aa7385a592c2b30b1d0ae8750c45c915c4bac0",
                   "size": 25568691, "kind": "gz", "target": "ffmpeg"},
    },
    ("linux", "x86"): {
        "go2rtc": {"url": "https://github.com/AlexxIT/go2rtc/releases/download/v1.9.15/go2rtc_linux_i386",
                   "sha256": "3a4d74b7d6ba2a04c5c7e7c7ee6208f9060fd17dfcce5a0975d44b6621a85c1e",
                   "size": 5412152, "kind": "binary", "target": "go2rtc"},
        "ffmpeg": {"url": "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/ffmpeg-linux-ia32.gz",
                   "sha256": "169b27c078a8ecedb814cac67afccf15a9868d63e9d74ef86088adefaa500d00",
                   "size": 22184031, "kind": "gz", "target": "ffmpeg"},
    },
    ("darwin", "x64"): {
        "go2rtc": {"url": "https://github.com/AlexxIT/go2rtc/releases/download/v1.9.15/go2rtc_mac_amd64.zip",
                   "sha256": "7ca6075421b824eebe0383d20bd42a6f6a7ec7591a60931d42290f1d375169b9",
                   "size": 8116920, "kind": "zip", "target": "go2rtc"},
        "ffmpeg": {"url": "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/ffmpeg-darwin-x64.gz",
                   "sha256": "929b375c1182d956c51f7ac25e0b2b0411fb01f6f407aa15c9758efeb4242106",
                   "size": 25296431, "kind": "gz", "target": "ffmpeg"},
    },
    ("darwin", "arm64"): {
        "go2rtc": {"url": "https://github.com/AlexxIT/go2rtc/releases/download/v1.9.15/go2rtc_mac_arm64.zip",
                   "sha256": "d0cf858a71803dce2377423bc830639137ee96c335f88e9912e7f7898374266d",
                   "size": 7427699, "kind": "zip", "target": "go2rtc"},
        "ffmpeg": {"url": "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/ffmpeg-darwin-arm64.gz",
                   "sha256": "8923876afa8db5585022d7860ec7e589af192f441c56793971276d450ed3bbfa",
                   "size": 19246198, "kind": "gz", "target": "ffmpeg"},
    },
}


def _camera_os_name():
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _windows_native_arch():
    """Windows on ARM runs x86/x64 Python under emulation, where platform.machine()
    reports the emulated arch instead of the chip."""
    try:
        import ctypes

        class SystemInfo(ctypes.Structure):
            _fields_ = [
                ("wProcessorArchitecture", ctypes.c_uint16),
                ("wReserved", ctypes.c_uint16),
                ("dwPageSize", ctypes.c_uint32),
                ("lpMinimumApplicationAddress", ctypes.c_void_p),
                ("lpMaximumApplicationAddress", ctypes.c_void_p),
                ("dwActiveProcessorMask", ctypes.c_size_t),
                ("dwNumberOfProcessors", ctypes.c_uint32),
                ("dwProcessorType", ctypes.c_uint32),
                ("dwAllocationGranularity", ctypes.c_uint32),
                ("wVdmLevel", ctypes.c_uint16),
                ("wReserved2", ctypes.c_uint16),
            ]

        info = SystemInfo()
        ctypes.windll.kernel32.GetNativeSystemInfo(ctypes.byref(info))
        return {0: "x86", 5: "arm", 6: "arm", 9: "x64", 12: "arm64"}.get(
            int(info.wProcessorArchitecture))
    except Exception:
        return None


def _camera_arch():
    import platform
    machine = (platform.machine() or "").lower()
    if os.name == "nt":
        machine = _windows_native_arch() or machine
    if machine in ("x86_64", "amd64", "x64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine.startswith("arm"):
        return "arm"
    if machine in ("x86", "i386", "i486", "i586", "i686", "x86_32"):
        return "x86"
    return machine


def _camera_catalog():
    return CAMERA_COMPONENTS.get((_camera_os_name(), _camera_arch()), {})


def _camera_paths():
    root = os.path.join(ROOT, "camera-stack")
    exe = ".exe" if os.name == "nt" else ""
    return {
        "root": root,
        "go2rtc": os.path.join(root, "go2rtc" + exe),
        "ffmpeg": os.path.join(root, "ffmpeg" + exe),
        "config": os.path.join(root, "go2rtc.yaml"),
        "log": os.path.join(root, "go2rtc.log"),
        "state": os.path.join(root, "state.json"),
        "downloads": os.path.join(root, "downloads"),
    }


def _camera_state_read():
    try:
        with open(_camera_paths()["state"], encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _camera_state_write(data):
    paths = _camera_paths()
    os.makedirs(paths["root"], exist_ok=True)
    tmp = paths["state"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, paths["state"])


def _camera_go2rtc_ok():
    data = _port_json(_go2rtc_base() + "/api", timeout=2)
    return bool(isinstance(data, dict) and (data.get("revision") or data.get("config_path")))


def _camera_go2rtc_bin():
    paths = _camera_paths()
    if os.path.isfile(paths["go2rtc"]):
        return paths["go2rtc"]
    found = shutil.which("go2rtc")
    if found:
        return found
    candidates = [
        "/opt/go2rtc/go2rtc", "/opt/go2rtc/go2rtc_linux_arm",
        "/opt/go2rtc/go2rtc_linux_arm64", "/opt/go2rtc/go2rtc_linux_amd64",
        "/usr/local/bin/go2rtc", "/usr/bin/go2rtc",
    ]
    for item in candidates:
        if os.path.isfile(item):
            return item
    try:
        for name in sorted(os.listdir("/opt/go2rtc")):
            if name.startswith("go2rtc") and os.path.isfile(
                    os.path.join("/opt/go2rtc", name)):
                return os.path.join("/opt/go2rtc", name)
    except OSError:
        pass
    return ""


def _camera_go2rtc_outdated():
    if os.name != "nt":
        return False
    path = _camera_paths()["go2rtc"]
    if not os.path.isfile(path):
        return False
    comp = _camera_catalog().get("go2rtc") or {}
    want = str(comp.get("sha256") or "")
    return bool(want and _sha256_file(path) != want)


def _camera_ffmpeg_bin():
    global FFMPEG_BIN
    paths = _camera_paths()
    if os.path.isfile(paths["ffmpeg"]):
        FFMPEG_BIN = paths["ffmpeg"]
        return FFMPEG_BIN
    found = shutil.which("ffmpeg")
    if found:
        FFMPEG_BIN = found
        return found
    return ""


def _camera_external_go2rtc():
    if _go2rtc_config_url():
        return True
    for key in ("MIOT_GO2RTC", "MIOT_GO2RTC_API"):
        value = str(os.environ.get(key) or "").strip()
        if value and not _go2rtc_host_is_local(value):
            return True
    return False


def _camera_normalize_go2rtc_url():
    raw = str(CONFIG.get("go2rtc_url") or "").strip()
    if not raw or not _go2rtc_host_is_local(raw):
        return False
    CONFIG["go2rtc_url"] = ""
    save_config({"go2rtc_url": ""})
    LOG.info("cleared self-referencing go2rtc_url: %s", raw[:120])
    return True


def _camera_is_docker():
    return os.path.exists("/.dockerenv") or os.environ.get("MIOT_DOCKER") == "1"


def _camera_job_set(state, message="", percent=None):
    with CAMERA_STACK_JOB_LOCK:
        CAMERA_STACK_JOB["state"] = state
        CAMERA_STACK_JOB["message"] = str(message or "")[:300]
        if percent is not None:
            try:
                CAMERA_STACK_JOB["percent"] = max(0, min(100, int(percent)))
            except (TypeError, ValueError):
                pass
        CAMERA_STACK_JOB["ts"] = time.time()


def _camera_job_get():
    with CAMERA_STACK_JOB_LOCK:
        return dict(CAMERA_STACK_JOB)


def _camera_guide():
    paths = _camera_paths()
    cat = _camera_catalog()
    items = []
    if cat:
        go = cat["go2rtc"]
        ff = cat["ffmpeg"]
        items = [
            {"name": "go2rtc",
             "version": go.get("version") or CAMERA_STACK_VERSION["go2rtc"],
             "url": go["url"], "mirror": "https://gh-proxy.com/" + go["url"],
             "target": paths["go2rtc"], "sha256": go["sha256"],
             "kind": go["kind"]},
            {"name": "ffmpeg",
             "version": ff.get("version") or CAMERA_STACK_VERSION["ffmpeg"],
             "url": ff["url"], "mirror": "https://gh-proxy.com/" + ff["url"],
             "target": paths["ffmpeg"], "sha256": ff["sha256"],
             "kind": ff["kind"]},
        ]
    return items


def _camera_setup_payload():
    paths = _camera_paths()
    go_ok = _camera_go2rtc_ok()
    go_bin = _camera_go2rtc_bin()
    ff_bin = _camera_ffmpeg_bin()
    cat = _camera_catalog()
    external = _camera_external_go2rtc()
    docker = _camera_is_docker()
    outdated = _camera_go2rtc_outdated()
    missing = []
    if (not go_ok and not go_bin) or outdated:
        missing.append("go2rtc")
    if not ff_bin and not external:
        missing.append("ffmpeg")
    can_install = bool(cat) and not external
    if external:
        reason = "使用远程 go2rtc：%s" % _go2rtc_base()
        if not go_ok:
            reason += "（未响应，请检查地址/端口/防火墙）"
    elif not cat:
        reason = "当前平台暂不支持一键安装，请按下方步骤手动安装"
    elif docker and not (go_ok and ff_bin):
        reason = "组件会安装到数据卷 /data/camera-stack；首次下载约 30–90MB"
    else:
        reason = ""
    if not go_ok and go_bin and not missing:
        reason = "go2rtc 未运行，点击按钮即可启动"
    if outdated and not external:
        reason = (reason + "；" if reason else "") + "可更新 go2rtc 以隐藏命令行窗口"
    if (ff_bin or external) and go_ok and not missing and not outdated:
        reason = reason if external else ""
    components = []
    for name in ("go2rtc", "ffmpeg"):
        info = cat.get(name) or {}
        ok = bool(go_ok if name == "go2rtc" else (ff_bin or external))
        if name == "go2rtc" and outdated:
            ok = False
        components.append({
            "name": name,
            "ok": ok,
            "needs_update": bool(outdated if name == "go2rtc" else False),
            "version": info.get("version") or CAMERA_STACK_VERSION.get(name, ""),
            "url": info.get("url", ""),
            "sha256": info.get("sha256", ""),
            "target": paths[name],
        })
    return {
        "ok": True,
        "platform": _camera_os_name(),
        "arch": _camera_arch(),
        "external": external,
        "remote": external,
        "go2rtc_page_url": _go2rtc_base() if external else "",
        "docker": docker,
        "go2rtc_ok": go_ok,
        "go2rtc_path": go_bin,
        "ffmpeg_ok": bool(ff_bin or external),
        "ffmpeg_path": ff_bin,
        "installed": bool(go_ok and (ff_bin or external)),
        "ready": bool(go_ok and (ff_bin or external)),
        "missing": missing,
        "can_install": can_install,
        "update_available": outdated,
        "reason": reason,
        "components": components,
        "guide": _camera_guide(),
        "paths": paths,
        "job": _camera_job_get(),
    }


def _camera_start():
    if _camera_external_go2rtc():
        if _camera_go2rtc_ok():
            return True
        raise RuntimeError("远程 go2rtc 未响应，请检查后台填写的地址")
    if _camera_go2rtc_ok():
        _camera_ffmpeg_bin()
        return True
    paths = _camera_paths()
    go_bin = _camera_go2rtc_bin()
    if not go_bin:
        raise RuntimeError("未安装 go2rtc")
    if not _port_free(1984):
        raise RuntimeError("端口 1984 已被占用，请先关闭占用程序")
    ff_bin = _camera_ffmpeg_bin()
    os.makedirs(paths["root"], exist_ok=True)
    config = ('api:\n  listen: "127.0.0.1:1984"\n'
              'rtsp:\n  listen: "127.0.0.1:8554"\n'
              'webrtc:\n  listen: "127.0.0.1:8555"\n')
    if ff_bin:
        config += 'ffmpeg:\n  bin: %s\n' % json.dumps(ff_bin)
    with open(paths["config"], "w", encoding="utf-8") as f:
        f.write(config)
    args = [go_bin, "-config", paths["config"]]
    pid = _stack_popen(args, paths["root"], paths["log"])
    state = _camera_state_read()
    state.update({"go2rtc_pid": pid, "installed": True, "updated_at": int(time.time())})
    _camera_state_write(state)
    if _wait_http(_go2rtc_base() + "/api", 25) is None:
        raise RuntimeError("go2rtc 启动超时，请查看 camera-stack/go2rtc.log")
    _camera_ffmpeg_bin()
    return True


def _camera_stop():
    state = _camera_state_read()
    pid = state.get("go2rtc_pid")
    if pid:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False)
            else:
                os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass
    state.pop("go2rtc_pid", None)
    _camera_state_write(state)


def _camera_extract(component, archive, target):
    name = component["target"].lower()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".new"
    if component["kind"] == "zip":
        with zipfile.ZipFile(archive) as z:
            member = None
            for item in z.namelist():
                if os.path.basename(item).lower() == name:
                    member = item
                    break
            if not member:
                raise RuntimeError("下载包中没有 %s" % component["target"])
            with z.open(member) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
    elif component["kind"] == "gz":
        with gzip.open(archive, "rb") as src, open(tmp, "wb") as out:
            shutil.copyfileobj(src, out)
    else:
        shutil.copy2(archive, tmp)
    os.replace(tmp, target)
    if os.name != "nt":
        os.chmod(target, 0o755)


def _camera_install(progress=None):
    progress = progress or (lambda *a: None)
    if _camera_external_go2rtc():
        raise RuntimeError("已配置外部 go2rtc，请在其所在设备安装 go2rtc/ffmpeg")
    cat = _camera_catalog()
    if not cat:
        raise RuntimeError("当前平台暂不支持一键安装，请按页面指引手动安装")
    paths = _camera_paths()
    os.makedirs(paths["downloads"], exist_ok=True)
    missing = []
    if (not _camera_go2rtc_ok() and not _camera_go2rtc_bin()) or _camera_go2rtc_outdated():
        missing.append("go2rtc")
    if not _camera_ffmpeg_bin():
        missing.append("ffmpeg")
    if not missing:
        _camera_start()
        return _camera_setup_payload()
    total = len(missing)
    for idx, name in enumerate(missing):
        comp = dict(cat[name])
        base = int(idx * 70 / total)
        span = int(70 / total)
        progress("正在下载 %s（%d/%d）" % (name, idx + 1, total), base + 2)
        archive = os.path.join(paths["downloads"], os.path.basename(comp["url"]))
        last = {"pct": base + 2}

        def on_bytes(done, size, _base=base, _span=span, _last=last):
            pct = _base + 2
            if size:
                pct = _base + int(min(1.0, done / float(size)) * (_span - 4))
            if pct > _last["pct"]:
                _last["pct"] = pct
                progress("", pct)

        _download_verified(comp["url"], archive, comp["sha256"], timeout=600,
                           progress=on_bytes)
        progress("正在校验并解压 %s" % name, base + span - 2)
        if name == "go2rtc" and _camera_state_read().get("go2rtc_pid"):
            _camera_stop()
        _camera_extract(comp, archive, paths[name])
        try:
            os.remove(archive)
        except OSError:
            pass
    progress("正在启动 go2rtc", 82)
    if _camera_go2rtc_ok() and _camera_state_read().get("go2rtc_pid") and "ffmpeg" in missing:
        _camera_stop()
    _camera_start()
    progress("正在检测摄像头组件", 94)
    if not _camera_go2rtc_ok():
        raise RuntimeError("go2rtc 未响应，请检查 camera-stack/go2rtc.log")
    if not _camera_ffmpeg_bin():
        raise RuntimeError("ffmpeg 安装后仍未找到可执行文件")
    state = _camera_state_read()
    state.update({"installed": True, "disabled": False,
                  "go2rtc_version": CAMERA_STACK_VERSION["go2rtc"],
                  "ffmpeg_version": CAMERA_STACK_VERSION["ffmpeg"],
                  "updated_at": int(time.time())})
    _camera_state_write(state)
    progress("安装完成", 100)
    return _camera_setup_payload()


def _camera_install_job():
    def report(message, percent=None):
        message = str(message or "").strip()
        if not message:
            previous = _camera_job_get()
            message = previous.get("message") or "正在下载摄像头组件"
        if "完成" in message:
            state = "done"
        elif "下载" in message or "校验" in message or "解压" in message:
            state = "downloading"
        else:
            state = "installing"
        _camera_job_set(state, message, percent)

    try:
        _camera_install(progress=report)
        _camera_job_set("done", "摄像头组件安装完成", 100)
    except Exception as exc:
        LOG.exception("camera stack install failed")
        try:
            _camera_stop()
        except Exception:
            pass
        _camera_job_set("failed", str(exc)[:400], 100)
    finally:
        with CAMERA_STACK_JOB_LOCK:
            CAMERA_STACK_JOB["running"] = False


def _camera_autostart():
    time.sleep(6)
    try:
        if _camera_external_go2rtc():
            return
        state = _camera_state_read()
        if state.get("installed") and not state.get("disabled"):
            _camera_start()
            LOG.info("camera stack started")
    except Exception as exc:
        LOG.warning("camera stack autostart failed: %s", str(exc)[:160])



def _service_active(name):
    try:
        return subprocess.run(["systemctl", "is-active", name],
                              capture_output=True, timeout=4
                              ).stdout.decode().strip() == "active"
    except Exception:
        return False


def _songloft_login(force=False):
    now = time.time()
    with _SL_LOCK:
        if not force and _SL_TOKEN["token"] and _SL_TOKEN["exp"] > now + 30:
            return _SL_TOKEN["token"]
        user, pwd = _songloft_creds()
        if not user or not pwd:
            raise RuntimeError("未找到音乐中心管理员凭据")
        req = urllib.request.Request(
            _songloft_base() + "/api/v1/auth/login",
            data=json.dumps({"username": user, "password": pwd}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read() or b"{}")
        token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError("音乐中心登录失败")
        try:
            ttl = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600
        _SL_TOKEN["token"] = token
        _SL_TOKEN["exp"] = now + max(60.0, ttl)
        return token


def _songloft_sso_url():
    base = _songloft_base()
    try:
        with urllib.request.urlopen(
                base + "/api/v1/jsplugin/" + SONGLOFT_SSO_ENTRY + "/",
                timeout=4) as r:
            if r.status != 200:
                raise RuntimeError("音乐中心免登录入口不可用")
    except Exception:
        raise RuntimeError("音乐中心免登录入口不可用，请确认 panel-sso 插件已安装")
    user, pwd = _songloft_creds()
    if not user or not pwd:
        raise RuntimeError("未找到音乐中心管理员凭据")
    req = urllib.request.Request(
        base + "/api/v1/auth/login",
        data=json.dumps({"username": user, "password": pwd}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read() or b"{}")
    access = str(data.get("access_token") or "")
    refresh = str(data.get("refresh_token") or "")
    if not access or not refresh:
        raise RuntimeError("音乐中心登录失败，请检查 Songloft 服务")
    payload = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": int(data.get("expires_in") or 3600),
        "username": user,
    }
    frag = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return "%s/api/v1/jsplugin/%s/#%s" % (
        _songloft_public_url(), SONGLOFT_SSO_ENTRY, frag)


def _songloft_req(method, path, payload=None, timeout=15, retry=True):
    token = _songloft_login()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        _songloft_base() + path, data=body, method=method,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw) if raw else {}
            except ValueError:
                return r.status, {"error": raw[:300].decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {"error": raw[:300].decode("utf-8", "replace")}
        if e.code == 401 and retry:
            _songloft_login(True)
            return _songloft_req(method, path, payload, timeout, False)
        return e.code, data
    except Exception as e:
        return 0, {"error": str(e)[:240]}


def _mask_account(value):
    s = str(value or "")
    if "@" in s:
        local, domain = s.split("@", 1)
        return (local[:2] + "***@" + domain) if len(local) > 2 else s
    if len(s) >= 10 and s.isdigit():
        return s[:3] + "****" + s[-4:]
    return (s[:2] + "***") if len(s) > 4 else s


def _plugin_pairs(plugins):
    out = {}
    for p in (plugins or []):
        ep = str(p.get("entry_path") or p.get("entryPath") or "")
        if ep:
            out[ep] = p
    return out


def _music_state():
    state = {
        "ok": True, "error": "",
        "songloft": {"active": False, "port": _songloft_port(),
                     "version": "", "url": _songloft_public_url()},
        "gmd": {"active": False, "port": _gmd_port(), "url": _gmd_base()},
        "plugins": {}, "accounts": [], "groups": [], "providers": [],
        "miot": {}, "ai": {}, "chat": {}, "gmd_config": {},
        "conversation": {}, "lan": str(CONFIG.get("lan_ip") or LAN_IP),
    }
    try:
        code, ver = _songloft_req("GET", "/api/v1/version", timeout=4)
        version = str(ver.get("version") or "") if isinstance(ver, dict) else ""
        if code != 200 or not version:
            raise RuntimeError("Songloft 未响应")
        state["songloft"]["version"] = version
        state["songloft"]["active"] = True
    except Exception:
        state["error"] = "Songloft 未连接"
        return state
    try:
        _, pl = _songloft_req("GET", "/api/v1/jsplugins", timeout=6)
        pairs = _plugin_pairs(pl.get("plugins") if isinstance(pl, dict) else [])
        for ep in ("miot", "go-music-dl", SONGLOFT_SSO_ENTRY):
            p = pairs.get(ep)
            if p:
                state["plugins"][ep] = {
                    "name": p.get("name"), "version": p.get("version"),
                    "active": p.get("status") == "active",
                    "status": p.get("status"),
                }
        try:
            with urllib.request.urlopen(
                    _songloft_base() + "/api/v1/jsplugin/" + SONGLOFT_SSO_ENTRY + "/",
                    timeout=4) as resp:
                state["sso_ready"] = resp.status == 200
        except Exception:
            state["sso_ready"] = False
    except Exception as e:
        state["error"] = str(e)[:160]

    _, cfg_resp = _songloft_req("GET", MIOT_PLUGIN_API + "/config", timeout=8)
    cfg = cfg_resp.get("data") if isinstance(cfg_resp, dict) else None
    if isinstance(cfg, dict):
        ai = cfg.get("ai_config") or {}
        state["miot"] = {
            "server_host": cfg.get("server_host") or "",
            "server_host_status": cfg.get("server_host_status") or "",
            "suggested_addresses": cfg.get("suggested_addresses") or [],
            "voice_command_enabled": bool(cfg.get("voice_command_enabled")),
            "conversation_monitor_enabled": bool(cfg.get("conversation_monitor_enabled")),
            "external_search_enabled": bool(cfg.get("external_search_enabled")),
            "external_search_no_import": bool(cfg.get("external_search_no_import")),
            "external_search_sources": [
                {"name": s.get("name") or "", "url": s.get("url") or "",
                 "enabled": s.get("enabled") is not False}
                for s in (cfg.get("external_search_sources") or [])
                if isinstance(s, dict)],
            "search_priority": cfg.get("search_priority") or "parallel",
        }
        state["ai"] = {
            "enabled": bool(ai.get("enabled")),
            "api_url": ai.get("api_url") or "",
            "model": ai.get("model") or "",
            "timeout": ai.get("timeout") or 6,
            "key_set": bool(ai.get("api_key")),
        }
    else:
        state["error"] = "小爱 MIoT 插件未响应"

    _, chat = _songloft_req("GET", MIOT_PLUGIN_API + "/chat/config", timeout=8)
    if isinstance(chat, dict) and "system_prompt" in chat:
        key_set = bool(chat.get("api_key"))
        state["chat"] = {
            "enabled": bool(chat.get("enabled")),
            "connection": chat.get("connection") or "analysis",
            "api_url": chat.get("api_url") or "",
            "model": chat.get("model") or "",
            "timeout": chat.get("timeout") or 30,
            "system_prompt": chat.get("system_prompt") or "",
            "trigger_words": chat.get("trigger_words") or [],
            "fallback_enabled": bool(chat.get("fallback_enabled")),
            "followup_seconds": chat.get("followup_seconds", 60),
            "resume_music": chat.get("resume_music", True),
            "waiting_feedback": chat.get("waiting_feedback", True),
            "memory_enabled": bool(chat.get("memory_enabled")),
            "context_turns": chat.get("context_turns", 3),
            "streaming_enabled": bool(chat.get("streaming_enabled")),
            "key_set": key_set,
        }

    _, acc_resp = _songloft_req("GET", MIOT_PLUGIN_API + "/accounts", timeout=8)
    for a in ((acc_resp.get("data") or []) if isinstance(acc_resp, dict) else []):
        if not isinstance(a, dict):
            continue
        state["accounts"].append({
            "id": a.get("id") or "",
            "account": _mask_account(a.get("account")),
            "auth_type": a.get("auth_type") or "",
            "login_method": a.get("login_method") or "",
            "updated_at": a.get("updated_at") or "",
        })

    _, grp_resp = _songloft_req("GET", MIOT_PLUGIN_API + "/groups", timeout=8)
    for g in ((grp_resp.get("data") or []) if isinstance(grp_resp, dict) else []):
        if not isinstance(g, dict):
            continue
        state["groups"].append({
            "id": g.get("id") or "", "name": g.get("name") or "",
            "members": g.get("members") or [],
            "member_count": len(g.get("members") or []),
        })

    _, prov = _songloft_req("GET", MIOT_PLUGIN_API + "/search-providers", timeout=8)
    if isinstance(prov, dict):
        state["providers"] = prov.get("providers") or []

    _, gmd = _songloft_req("GET", GMD_PLUGIN_API + "/config", timeout=6)
    if isinstance(gmd, dict) and "sources" in gmd:
        state["gmd_config"] = {
            "baseUrl": gmd.get("baseUrl") or "",
            "basePath": gmd.get("basePath") or "/music",
            "serverHost": gmd.get("serverHost") or "",
            "sources": gmd.get("sources") or [],
            "defaultQuality": gmd.get("defaultQuality") or "exhigh",
        }
        gmd_url = str(state["gmd_config"]["baseUrl"] or _gmd_base()).rstrip("/")
        state["gmd"]["url"] = gmd_url
        try:
            state["gmd"]["port"] = urllib.parse.urlsplit(gmd_url).port or state["gmd"]["port"]
        except ValueError:
            pass
        try:
            with urllib.request.urlopen(gmd_url + "/music/", timeout=4) as resp:
                state["gmd"]["active"] = resp.status < 500
        except urllib.error.HTTPError:
            state["gmd"]["active"] = True
        except Exception:
            state["gmd"]["active"] = False

    if state["miot"].get("conversation_monitor_enabled"):
        _, conv = _songloft_req("GET", MIOT_PLUGIN_API + "/conversation/status", timeout=6)
        if isinstance(conv, dict) and isinstance(conv.get("data"), dict):
            d = conv["data"]
            state["conversation"] = {
                "running": bool(d.get("running")),
                "device_count": d.get("device_count") or d.get("deviceCount") or 0,
            }
    return state


def _device_public(d):
    return {
        "device_id": d.get("deviceID") or d.get("device_id") or "",
        "name": d.get("name") or d.get("alias") or "",
        "alias": d.get("alias") or "",
        "model": d.get("model") or "",
        "hardware": d.get("hardware") or "",
        "presence": d.get("presence") or "offline",
        "managed": bool(d.get("managed")),
        "volume": d.get("volume") or 0,
    }


def _music_devices():
    _, resp = _songloft_req("GET", MIOT_PLUGIN_API + "/mina/devices", timeout=30)
    out = []
    if not isinstance(resp, dict) or not resp.get("success"):
        return {"ok": False, "error": (resp or {}).get("error") or "读取音箱失败", "accounts": []}
    for acc in (resp.get("data") or []):
        if not isinstance(acc, dict):
            continue
        out.append({
            "account_id": acc.get("account_id") or "",
            "account_name": _mask_account(acc.get("account_name") or acc.get("account_id")),
            "last_selected_device_id": acc.get("last_selected_device_id") or "",
            "devices": [_device_public(x) for x in (acc.get("devices") or [])
                        if isinstance(x, dict)],
        })
    return {"ok": True, "accounts": out}


def _speaker_id(account_id, device_id):
    return "spk_" + hashlib.sha256(
        (str(account_id) + "|" + str(device_id)).encode("utf-8")).hexdigest()[:16]


def _speaker_payload(force=False):
    now = time.time()
    with SPK_LOCK:
        if not force and SPK_CACHE["payload"] and now - SPK_CACHE["ts"] < SPK_TTL:
            return SPK_CACHE["payload"]
    payload = {
        "ok": True, "enabled": bool(CONFIG.get("speaker_panel_enabled", True)),
        "text_enabled": bool(CONFIG.get("speaker_text_enabled", True)),
        "voice_enabled": bool(CONFIG.get("speaker_voice_enabled", True)),
        "answer_mode": CONFIG.get("speaker_answer_mode") or "speak",
        "max_chars": int(CONFIG.get("speaker_max_chars") or 200),
        "speakers": [], "default_id": "", "error": "",
    }
    if not payload["enabled"]:
        with SPK_LOCK:
            SPK_CACHE.update(ts=now, payload=payload)
        return payload
    try:
        data = _music_devices()
        if not data.get("ok"):
            payload["error"] = data.get("error") or "读取音箱失败"
        else:
            group_map = {}
            _, gr = _songloft_req("GET", MIOT_PLUGIN_API + "/groups", timeout=10)
            for g in ((gr.get("data") or []) if isinstance(gr, dict) else []):
                if not isinstance(g, dict):
                    continue
                for m in g.get("members") or []:
                    if isinstance(m, dict):
                        group_map[str(m.get("account_id")) + "|" +
                                  str(m.get("device_id"))] = g.get("name") or ""
            hidden = set(CONFIG.get("speaker_hidden") or [])
            by_id = {}
            items = []
            for acc in data.get("accounts") or []:
                aid = acc.get("account_id") or ""
                for d in acc.get("devices") or []:
                    if not d.get("managed") or not d.get("device_id"):
                        continue
                    key = str(aid) + "|" + str(d["device_id"])
                    if key in hidden:
                        continue
                    sid = _speaker_id(aid, d["device_id"])
                    item = {
                        "id": sid, "name": d.get("name") or "小爱音箱",
                        "model": d.get("model") or "",
                        "online": str(d.get("presence") or "").lower() == "online",
                        "volume": d.get("volume") or 0,
                        "group": group_map.get(key, ""),
                    }
                    items.append(item)
                    by_id[sid] = {"account_id": aid, "device_id": d["device_id"],
                                  "name": item["name"]}
            items.sort(key=lambda x: (not x["online"], x["name"]))
            payload["speakers"] = items
            default_key = str(CONFIG.get("speaker_default") or "")
            default_id = ""
            if default_key:
                parts = default_key.split("|", 1)
                if len(parts) == 2:
                    did = _speaker_id(parts[0], parts[1])
                    if any(x["id"] == did for x in items):
                        default_id = did
            if not default_id:
                online = [x for x in items if x["online"]]
                default_id = (online[0]["id"] if online else (items[0]["id"] if items else ""))
            payload["default_id"] = default_id
            with SPK_LOCK:
                SPK_BY_ID.clear()
                SPK_BY_ID.update(by_id)
    except Exception as e:
        payload["ok"] = False
        payload["error"] = str(e)[:160]
    with SPK_LOCK:
        SPK_CACHE.update(ts=now, payload=payload)
    return payload


def _resolve_speaker(sid):
    sid = str(sid or "")
    if not sid:
        return None
    with SPK_LOCK:
        info = SPK_BY_ID.get(sid)
    if info:
        return info
    _speaker_payload(force=True)
    with SPK_LOCK:
        return SPK_BY_ID.get(sid)


def _speaker_rate_ok(ip, speaker_id):
    now = time.time()
    with SPK_LOCK:
        arr = [x for x in (SPK_RATE.get(ip) or []) if now - x < 60]
        if len(arr) >= 12:
            SPK_RATE[ip] = arr
            return False
        if arr and now - arr[-1] < 1.5:
            SPK_RATE[ip] = arr
            return False
        arr.append(now)
        SPK_RATE[ip] = arr
    return True


def _speaker_chat_ready():
    _, ch = _songloft_req("GET", MIOT_PLUGIN_API + "/chat/config", timeout=8)
    return bool(isinstance(ch, dict) and ch.get("enabled")), ch


def _speaker_listen(sid, since=0):
    info = _resolve_speaker(sid)
    if not info:
        return {"ok": False, "error": "音箱不存在或已刷新，请重载面板"}
    if not CONFIG.get("speaker_voice_enabled", True):
        return {"ok": False, "error": "语音提问已在后台关闭"}
    _, conv = _songloft_req("GET", MIOT_PLUGIN_API + "/conversation/status", timeout=8)
    cdata = conv.get("data") if isinstance(conv, dict) else None
    if not isinstance(cdata, dict) or not cdata.get("is_enabled"):
        return {"ok": False, "error": "对话监听未开启，请管理员在后台开启"}
    ready, _ = _speaker_chat_ready()
    if not ready:
        return {"ok": False, "error": "AI 对话未启用，请管理员在后台开启"}
    query = "limit=20"
    if since:
        query += "&since=" + str(int(since))
    _, resp = _songloft_req(
        "GET", MIOT_PLUGIN_API + "/conversation/messages?" + query, timeout=15)
    msgs = (resp.get("data") or []) if isinstance(resp, dict) else []
    if not isinstance(msgs, list):
        msgs = []
    target_msgs = [m for m in msgs if isinstance(m, dict)
                   and str(m.get("account_id")) == str(info["account_id"])
                   and str(m.get("device_id")) == str(info["device_id"])]
    target_max = max([int(m.get("timestamp") or 0) for m in target_msgs] or [0])
    q = urllib.parse.urlencode({"account_id": info["account_id"],
                                "device_id": info["device_id"]})
    _, cs = _songloft_req("GET", MIOT_PLUGIN_API + "/chat/status?" + q, timeout=8)
    chat = None
    if isinstance(cs, dict):
        chat = {"phase": cs.get("phase") or "", "answer": cs.get("answer") or "",
                "error": cs.get("error") or ""}
    if not since:
        baseline = target_max or int(time.time() * 1000)
        return {"ok": True, "since": baseline, "message": None, "chat": chat}
    match = [m for m in target_msgs
             if int(m.get("timestamp") or 0) > int(since)]
    match.sort(key=lambda m: int(m.get("timestamp") or 0))
    message = None
    if match:
        m = match[-1]
        message = {"query": m.get("query") or "",
                   "native_answer": m.get("answer") or "",
                   "timestamp": int(m.get("timestamp") or 0)}
    return {"ok": True, "since": max(int(since), target_max), "message": message, "chat": chat}


def _music_default_sources():
    return [s for s in MUSIC_DEFAULT_SOURCES]


def _ensure_proxy_allowlist(*hosts):
    """允许 Songloft 转码代理回源到本机/局域网音源，避免私网地址被 SSRF 白名单拒绝。"""
    try:
        code, data = _songloft_req(
            "GET", "/api/v1/settings/proxy-private-allowlist", timeout=8)
        if code != 200 or not isinstance(data, dict):
            return
        entries = [str(x) for x in (data.get("allowlist") or []) if str(x).strip()]
        wanted = {LAN_IP}
        for raw in hosts:
            try:
                host = urllib.parse.urlsplit(str(raw or "")).hostname or ""
                ipaddress.ip_address(host)
                wanted.add(host)
            except ValueError:
                continue
        add = [ip for ip in sorted(wanted) if ip and ip not in entries]
        if not add:
            return
        entries.extend(add)
        _songloft_req("PUT", "/api/v1/settings/proxy-private-allowlist",
                      {"allowlist": entries}, timeout=10)
        LOG.info("Songloft private proxy allowlist updated: %s", ",".join(add))
    except Exception as e:
        LOG.warning("Songloft proxy allowlist update failed: %s", str(e)[:160])


def _music_autoconfig():
    _, resp = _songloft_req("GET", MIOT_PLUGIN_API + "/config", timeout=10)
    cfg = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(cfg, dict):
        return
    current_host = str(cfg.get("server_host") or "").strip()
    host_name = (urllib.parse.urlsplit(current_host).hostname or "").lower() \
        if current_host else ""
    if not current_host or host_name in ("127.0.0.1", "localhost", "::1"):
        current_host = _songloft_server_url()
    patch = {
        "server_host": current_host,
        "external_search_enabled": True,
        "external_search_no_import": True,
        "external_search_sources": [{
            "id": "go-music-dl", "name": "GoMusicDL", "url": GMD_SEARCH_PATH,
            "token": "", "enabled": True,
        }],
        "external_search_timeout": 6,
        "search_priority": "parallel",
        "voice_command_enabled": True,
        "conversation_monitor_enabled": True,
    }
    _songloft_req("POST", MIOT_PLUGIN_API + "/config", patch, timeout=30)
    _songloft_req("POST", GMD_PLUGIN_API + "/config", {
        "baseUrl": _gmd_base(),
        "basePath": "/music",
        "serverHost": _songloft_server_url(),
        "sources": _music_default_sources(),
        "defaultQuality": "exhigh",
    }, timeout=10)
    _ensure_proxy_allowlist(current_host)
    try:
        devs = []
        for _ in range(4):
            devs = _music_devices().get("accounts") or []
            if any(acc.get("devices") for acc in devs):
                break
            time.sleep(2)
        for acc in devs:
            for d in acc.get("devices") or []:
                if not d.get("managed") and d.get("device_id"):
                    _songloft_req("POST", MIOT_PLUGIN_API + "/mina/device/managed", {
                        "account_id": acc["account_id"],
                        "device_id": d["device_id"],
                        "managed": True,
                    }, timeout=12)
        # 不自动创建“全屋音箱”组：默认各音箱独立，用户需要时在后台手动组网。
        LOG.info("music autoconfig: groups left unchanged by default")
    except Exception as e:
        LOG.warning("music autoconfig devices: %s", e)


def _music_search_test(keyword):
    keyword = str(keyword or "").strip()
    if not keyword:
        return {"ok": False, "error": "请输入测试歌曲名"}
    _, resp = _songloft_req("POST", GMD_SEARCH_PATH, {"keyword": keyword}, timeout=25)
    if not isinstance(resp, dict) or resp.get("code") != 0:
        return {"ok": False, "error": (resp or {}).get("msg") or "没有找到歌曲"}
    d = resp.get("data") or {}
    url = str(d.get("url") or "")
    if not url:
        return {"ok": True, "found": False, "title": d.get("title") or "",
                "artist": d.get("artist") or "", "error": "找到歌曲但没有可播放直链"}
    parsed = urllib.parse.urlsplit(url)
    safe_host = parsed.hostname in (LAN_IP, "127.0.0.1", "localhost")
    content_type = ""
    status = 0
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            status = r.status
            content_type = r.headers.get("Content-Type") or ""
            r.read(1)
    except Exception as e:
        return {"ok": True, "found": True, "title": d.get("title") or "",
                "artist": d.get("artist") or "", "duration": d.get("duration") or 0,
                "stream_ok": False, "error": str(e)[:160]}
    return {"ok": True, "found": True, "title": d.get("title") or "",
            "artist": d.get("artist") or "", "duration": d.get("duration") or 0,
            "stream_ok": status in (200, 206), "content_type": content_type,
            "lan_url": safe_host}


class Handler(BaseHTTPRequestHandler):
    timeout = 30
    max_body = 1024 * 1024

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
            if isinstance(v, (list, tuple)):
                for item in v:
                    self.send_header(k, item)
            else:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _proxy_go2rtc(self):
        base = _go2rtc_base()
        target = urllib.parse.urlsplit(base)
        host = target.hostname or "127.0.0.1"
        port = target.port or (443 if target.scheme == "https" else 80)
        path = self.path[len("/go2rtc"):] or "/"
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if upgrade == "websocket":
            self._proxy_go2rtc_ws(target, host, port, path)
        else:
            self._proxy_go2rtc_http(target, host, port, path)

    def _proxy_go2rtc_http(self, target, host, port, path):
        cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        conn = cls(host, port, timeout=25)
        headers = {}
        for key, value in self.headers.items():
            if key.lower() not in ("host", "connection", "proxy-connection",
                                   "keep-alive", "transfer-encoding", "upgrade"):
                headers[key] = value
        headers["Host"] = "%s:%d" % (host, port)
        body = None
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                body = self.rfile.read(length)
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() in ("connection", "keep-alive", "transfer-encoding",
                                   "content-length", "upgrade"):
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            self.close_connection = True
        except Exception as exc:
            LOG.debug("go2rtc proxy failed: %s", exc)
            try:
                self._send(502, {"ok": False, "error": "go2rtc 代理不可用"})
            except Exception:
                pass
        finally:
            conn.close()

    def _proxy_go2rtc_ws(self, target, host, port, path):
        upstream = None
        try:
            upstream = socket.create_connection((host, port), timeout=10)
            if target.scheme == "https":
                upstream = ssl.create_default_context().wrap_socket(
                    upstream, server_hostname=host)
            lines = ["%s %s HTTP/1.1" % (self.command, path)]
            for key, value in self.headers.items():
                if key.lower() == "origin":
                    continue  # go2rtc checks Origin against upstream Host
                if key.lower() == "host":
                    value = "%s:%d" % (host, port)
                lines.append("%s: %s" % (key, value))
            upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = upstream.recv(8192)
                if not chunk:
                    break
                raw += chunk
            if b"\r\n\r\n" not in raw:
                return
            head, _, rest = raw.partition(b"\r\n\r\n")
            self.connection.sendall(head + b"\r\n\r\n" + rest)
            if b" 101 " not in head.split(b"\r\n", 1)[0]:
                return
            self.close_connection = True
            client = self.connection
            client.setblocking(True)
            upstream.setblocking(True)
            while True:
                ready, _, _ = select.select([client, upstream], [], [], 120)
                if not ready:
                    break
                for sock in ready:
                    data = sock.recv(65536)
                    if not data:
                        return
                    if sock is client:
                        upstream.sendall(data)
                    else:
                        client.sendall(data)
        except Exception as exc:
            LOG.debug("go2rtc websocket proxy failed: %s", exc)
        finally:
            try:
                if upstream:
                    upstream.close()
            except Exception:
                pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/unlock":
            self._send(200, UNLOCK_HTML, "text/html; charset=utf-8")
            return
        if u.path.startswith("/go2rtc"):
            if not _device_authed(self):
                self._send(401, {"ok": False, "unlock": True,
                                 "error": "此设备需要先输入 PIN"})
                return
            self._proxy_go2rtc()
            return
        if not _device_authed(self):
            public = (u.path in ("/setup", "/admin", "/api/setup_state",
                                 "/manifest.webmanifest", "/sw.js", "/favicon.ico",
                                 "/compat.js", "/api/android", "/android.apk")
                      or u.path.startswith("/icons/")
                      or u.path.startswith(("/api/admin/", "/api/auth/")))
            if not public:
                if u.path.startswith("/api/"):
                    self._send(401, {"ok": False, "unlock": True,
                                     "error": "此设备需要先输入 PIN"})
                else:
                    nxt = urllib.parse.quote(self.path, safe="")
                    self._send(302, b"", "text/plain; charset=utf-8",
                               {"Location": "/unlock?next=" + nxt})
                return
        if u.path in ("/", "/index.html"):
            if _needs_onboarding():
                self._send(302, b"", "text/plain; charset=utf-8",
                           {"Location": "/setup"})
                return
            page = _active_page()
            if page:
                html = _inject_page_watch(page["html"], page["id"], page["version"])
                self._send(200, html, "text/html; charset=utf-8")
            else:
                self._send(200, PANEL_HTML, "text/html; charset=utf-8")
        elif u.path in ("/panel", "/panel/"):
            if _needs_onboarding():
                self._send(302, b"", "text/plain; charset=utf-8",
                           {"Location": "/setup"})
                return
            self._send(200, PANEL_HTML, "text/html; charset=utf-8")
        elif u.path in ("/setup", "/setup/"):
            host = (self.headers.get("Host") or "").strip()
            hostname = host.rsplit(":", 1)[0].strip("[]").lower() if host else ""
            if (not host) or hostname in ("127.0.0.1", "localhost", "::1"):
                host = "%s:%d" % (LAN_IP, PORT)
            elif PORT != 80 and ":" not in host:
                host += ":%d" % PORT
            setup_url = "http://%s/setup" % host
            html = SETUP_HTML.replace("@@SETUP_QR@@", build_qr(setup_url))
            self._send(200, html, "text/html; charset=utf-8")
        elif u.path.startswith("/custom/"):
            pid = urllib.parse.unquote(u.path[len("/custom/"):]).strip("/")
            html = _read_page_html(pid)
            if html is None:
                self._send(404, "自定义页面不存在", "text/plain; charset=utf-8")
            else:
                self._send(200, html, "text/html; charset=utf-8")
        elif u.path == "/login":
            if not admin_pin_set():
                self._send(302, b"", "text/plain; charset=utf-8",
                           {"Location": "/setup"})
                return
            force = ("force=1" in u.query) or ("m=phone" in u.query)
            if not _load_auth() and not force:
                self._send(302, b"", "text/plain; charset=utf-8",
                           {"Location": "/setup"})
                return
            host = (self.headers.get("Host") or "").strip()
            hostname = host.rsplit(":", 1)[0].strip("[]").lower() if host else ""
            if (not host) or hostname in ("127.0.0.1", "localhost", "::1"):
                host = "%s:%d" % (LAN_IP, PORT)
            elif PORT != 80 and ":" not in host:
                host += ":%d" % PORT
            qr_url = "http://%s/login?m=phone" % host
            html = LOGIN_HTML.replace("@@QR@@", build_qr(qr_url))
            self._send(200, html, "text/html; charset=utf-8")
        elif u.path in ("/admin", "/admin/"):
            self._send(200, ADMIN_HTML, "text/html; charset=utf-8")
        elif u.path in ("/camera", "/camera/"):
            self._send(200, CAMERA_HTML, "text/html; charset=utf-8")
        elif u.path == "/manifest.webmanifest":
            if PWA_MANIFEST:
                self._send(200, PWA_MANIFEST, "application/manifest+json; charset=utf-8")
            else:
                self._send(404, {"error": "manifest missing"})
        elif u.path == "/sw.js":
            if PWA_SW:
                self._send(200, PWA_SW, "application/javascript; charset=utf-8")
            else:
                self._send(404, {"error": "service worker missing"})
        elif u.path == "/compat.js":
            if COMPAT_JS:
                self._send(200, COMPAT_JS, "application/javascript; charset=utf-8",
                           {"Cache-Control": "public, max-age=86400"})
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
        elif u.path == "/api/android":
            self.api_android()
        elif u.path == "/android.apk":
            apk = _android_apk()
            if apk:
                self._send(200, apk, "application/vnd.android.package-archive",
                           {"Content-Disposition": 'attachment; filename="%s"' % ANDROID_APK_NAME,
                            "Cache-Control": "no-cache"})
            else:
                self._send(404, {"ok": False, "error": "此面板未内置安卓客户端安装包"})
        elif u.path in PWA_ICONS:
            if PWA_ICONS[u.path]:
                self._send(200, PWA_ICONS[u.path], "image/png")
            else:
                self._send(404, {"error": "icon missing"})
        elif u.path == "/favicon.ico":
            icon = PWA_ICONS.get("/icons/icon-192.png") or b""
            if icon:
                self._send(200, icon, "image/png")
            else:
                self._send(404, {"error": "favicon missing"})
        elif u.path == "/api/status":
            self.api_status()
        elif u.path == "/api/setup_state":
            self.api_setup_state()
        elif u.path == "/api/devices":
            self.api_devices()
        elif u.path == "/api/caps":
            self.api_caps(u)
        elif u.path == "/api/cameras":
            self.api_cameras(u)
        elif u.path == "/api/camera_state":
            self.api_camera_state(u)
        elif u.path == "/api/camera/setup_state":
            self.api_camera_setup_state()
        elif u.path == "/api/admin/status":
            self.api_admin_status()
        elif u.path == "/api/page_state":
            self.api_page_state()
        elif u.path == "/api/admin/logs":
            self.api_admin_logs()
        elif u.path == "/api/admin/pages":
            self.api_admin_pages(u)
        elif u.path == "/api/admin/music_info":
            self.api_admin_music_info(u)
        elif u.path == "/api/admin/laninfo":
            self.api_admin_laninfo()
        elif u.path == "/api/admin/music/stack/status":
            self.api_admin_music_stack_status()
        elif u.path == "/api/admin/music_state":
            self.api_admin_music_state()
        elif u.path == "/api/admin/music_devices":
            self.api_admin_music_devices()
        elif u.path == "/api/admin/music/web_sso":
            self.api_admin_music_web_sso()
        elif u.path == "/api/admin/update/status":
            self.api_admin_update_status()
        elif u.path == "/api/speaker_assist":
            self.api_speaker_assist()
        elif u.path == "/api/speaker/chat_status":
            self.api_speaker_chat_status(u)
        elif u.path == "/api/speaker/listen":
            self.api_speaker_listen(u)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            self._send(400, {"ok": False, "error": "Content-Length 无效"})
            return
        if length < 0 or length > self.max_body:
            self._send(413, {"ok": False, "error": "请求体超过 1MB"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        if u.path == "/api/device_unlock":
            self.api_device_unlock(body)
            return
        if not (u.path.startswith(("/api/admin/", "/api/auth/"))):
            if not _device_authed(self):
                self._send(401, {"ok": False, "unlock": True,
                                 "error": "此设备需要先输入 PIN"})
                return
        if u.path == "/api/device_lock":
            self.api_device_lock()
        elif u.path == "/api/login":
            self.api_login(body)
        elif u.path == "/api/verify":
            self.api_verify(body)
        elif u.path == "/api/control":
            self.api_control(body)
        elif u.path == "/api/set_prop":
            self.api_set_prop(body)
        elif u.path == "/api/action":
            self.api_action(body)
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
        elif u.path == "/api/admin/pages":
            self.api_admin_pages_save(body)
        elif u.path == "/api/admin/logs_clear":
            self.api_admin_logs_clear()
        elif u.path == "/api/admin/refresh":
            self.api_admin_refresh()
        elif u.path == "/api/auth/qr_start":
            self.api_auth_qr_start(body)
        elif u.path == "/api/auth/qr_poll":
            self.api_auth_qr_poll(body)
        elif u.path == "/api/admin/music/import_auth":
            self.api_admin_music_import_auth()
        elif u.path == "/api/admin/music/qr_start":
            self.api_admin_music_qr_start(body)
        elif u.path == "/api/admin/music/qr_poll":
            self.api_admin_music_qr_poll(body)
        elif u.path == "/api/admin/music/account_delete":
            self.api_admin_music_account_delete(body)
        elif u.path == "/api/admin/music/device":
            self.api_admin_music_device(body)
        elif u.path == "/api/admin/music/device_all":
            self.api_admin_music_device_all(body)
        elif u.path == "/api/admin/music/group_all":
            self.api_admin_music_group_all(body)
        elif u.path == "/api/admin/music/group":
            self.api_admin_music_group(body)
        elif u.path == "/api/admin/music/ai":
            self.api_admin_music_ai(body)
        elif u.path == "/api/admin/music/source":
            self.api_admin_music_source(body)
        elif u.path == "/api/admin/music/search_test":
            self.api_admin_music_search_test(body)
        elif u.path == "/api/admin/music/tts_test":
            self.api_admin_music_tts_test(body)
        elif u.path == "/api/admin/music/voice_test":
            self.api_admin_music_voice_test(body)
        elif u.path == "/api/admin/music/chat_test":
            self.api_admin_music_chat_test(body)
        elif u.path == "/api/admin/music/ai_test":
            self.api_admin_music_ai_test(body)
        elif u.path == "/api/admin/music/ai_check":
            self.api_admin_music_ai_check()
        elif u.path == "/api/admin/music/connect":
            self.api_admin_music_connect(body)
        elif u.path == "/api/admin/firewall":
            self.api_admin_firewall()
        elif u.path == "/api/admin/music/stack/install":
            self.api_admin_music_stack_install()
        elif u.path == "/api/admin/music/stack/start":
            self.api_admin_music_stack_start()
        elif u.path == "/api/admin/music/stack/stop":
            self.api_admin_music_stack_stop()
        elif u.path == "/api/camera/setup/install":
            self.api_camera_setup_install()
        elif u.path == "/api/camera/setup/start":
            self.api_camera_setup_start()
        elif u.path == "/api/admin/update/check":
            self.api_admin_update_check(body)
        elif u.path == "/api/admin/update/apply":
            self.api_admin_update_apply(body)
        elif u.path == "/api/speaker/chat":
            self.api_speaker_chat(body)
        else:
            self._send(404, {"error": "not found"})

    def api_android(self):
        apk = _android_apk()
        host = (self.headers.get("Host") or "").strip()
        self._send(200, {
            "ok": True,
            "version_name": ANDROID_VERSION_NAME,
            "version_code": ANDROID_VERSION_CODE,
            "url": ("http://%s/android.apk" % host) if (apk and host) else "",
            "release_url": ANDROID_RELEASE_URL,
            "apk_bytes": len(apk),
            "panel_version": APP_VERSION,
        })

    def api_status(self):
        auth = _load_auth()
        with STATE_LOCK:
            devices_snapshot = list(DEVICES)
            states_snapshot = {k: dict(v) for k, v in STATE.items()}
        supported = sum(1 for d in devices_snapshot
                        if states_snapshot.get(d["did"], {}).get("supported"))
        sensors = sum(1 for d in devices_snapshot if d["role"] == "sensor"
                      and states_snapshot.get(d["did"], {}).get("supported"))
        blind = sum(1 for d in devices_snapshot
                    if d["role"] != "sensor" and d["online"]
                    and not states_snapshot.get(d["did"], {}).get("supported"))
        with WEATHER_LOCK:
            weather = dict(WEATHER)
        theme = load_settings().get("theme", "dark")
        theme = THEME_LEGACY.get(theme, theme)
        if theme not in THEME_IDS:
            theme = "dark"
        page = _page_state()
        self._send(200, {"logged_in": bool(auth),
                         "version": APP_VERSION,
                         "home_label": CONFIG.get("home_label") or "我的家",
                         "weather_label": CONFIG.get("weather_label") or "天气",
                         "page_size": int(CONFIG.get("page_size") or 10),
                         "device_count": supported + blind,
                         "supported_count": supported,
                         "controllable_count": supported - sensors,
                         "sensor_count": sensors, "blind_count": blind,
                         "weather": weather or None, "theme": theme,
                         "active_page": ({"id": page["active"], "name": page["name"],
                                          "version": page["version"]}
                                         if page.get("active") else None)})

    def api_devices(self):
        if not _load_auth():
            self._send(200, {"devices": []})
            return
        devs = []
        with STATE_LOCK:
            devices_snapshot = list(DEVICES)
            states_snapshot = {k: dict(v) for k, v in STATE.items()}
        for d in devices_snapshot:
            st = states_snapshot.get(d["did"], {})
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
            if d["role"] == "sensor":
                values = st.get("values") or {}
                rows = []
                for p in _sensor_props(d["model"]):
                    if p["key"] not in values or values[p["key"]] is None:
                        continue
                    value = values[p["key"]]
                    unit = p.get("unit") or ""
                    display = _sensor_display(p["key"], value, p)
                    if p.get("value_list"):
                        unit = ""
                    if d["model"] == "isa.magnet.dw2hl" and p["key"] == "illumination":
                        unit = ""
                        display = {1: "弱", 2: "强"}.get(value)
                    rows.append({"key": p["key"], "label": p["label"],
                                 "unit": unit, "value": value, "display": display})
                item["values"] = rows
                for key in ("temp", "humidity", "battery"):
                    item[key] = values.get(key) if d["online"] else None
                item["readonly"] = True
            elif d["role"] == "ac" and "mode" in prof:
                item["temp"] = st.get("temp") if d["online"] else None
                item["mode"] = st.get("mode") if d["online"] else None
                item["modes"] = prof.get("modes") or []
                item["temp_min"], item["temp_max"] = prof.get("temp_range", (16, 30))
            devs.append(item)
        self._send(200, {"devices": devs, "ts": DEV_TS, "layout": load_layout()})

    def api_caps(self, u=None):
        q = urllib.parse.parse_qs(u.query) if u else {}
        did = str((q.get("did") or [""])[0] or "")
        d = find_device(did)
        if not d:
            self._send(404, {"ok": False, "error": "设备不存在或未刷新"})
            return
        caps = _device_caps(d["model"])
        if not caps or not (caps.get("props") or caps.get("actions")):
            self._send(200, {"ok": False, "error": "该型号没有可用的通用控制",
                             "name": d["name"], "model": d["model"]})
            return
        c = get_cloud()
        props = [dict(p) for p in caps.get("props") or []]
        values = _caps_values(c, did, props) if (c and d.get("online")) else {}
        for p in props:
            p["value"] = values.get((p["siid"], p["piid"]))
        self._send(200, {"ok": True, "did": did, "name": d["name"],
                         "model": d["model"], "online": d.get("online"),
                         "props": props, "actions": caps.get("actions") or []})

    def api_set_prop(self, body):
        ip = self.client_address[0] if self.client_address else "?"
        if not _rate_ok("control:" + ip, 120, 60, 0.1):
            self._send(429, {"ok": False, "msg": "控制操作过于频繁，请稍后再试"},
                       headers={"Retry-After": "60"})
            return
        did = str(body.get("did") or "")
        d = find_device(did)
        if not d:
            self._send(404, {"ok": False, "msg": "设备不存在或未刷新"})
            return
        if d["role"] == "sensor":
            self._send(400, {"ok": False, "msg": "传感器为只读设备"})
            return
        if not d.get("online"):
            self._send(200, {"ok": False, "msg": "设备离线"})
            return
        try:
            siid = int(body.get("siid"))
            piid = int(body.get("piid"))
        except Exception:
            self._send(400, {"ok": False, "msg": "属性参数无效"})
            return
        caps = _device_caps(d["model"]) or {}
        prop = next((p for p in caps.get("props") or []
                     if p["siid"] == siid and p["piid"] == piid), None)
        if not prop:
            self._send(400, {"ok": False, "msg": "该属性不允许控制"})
            return
        try:
            value = _coerce_cap(prop, body.get("value"))
        except Exception as e:
            self._send(400, {"ok": False, "msg": "值无效：%s" % e})
            return
        ok, err = cloud_set_cap(did, siid, piid, value)
        LOG.info("audit set_prop ip=%s did=%s %s-%s=%s -> %s", ip, did, siid, piid, value,
                 "OK" if ok else (err or "fail"))
        self._send(200, {"ok": ok, "msg": "" if ok else err,
                         "value": value if ok else None})

    def api_action(self, body):
        ip = self.client_address[0] if self.client_address else "?"
        if not _rate_ok("control:" + ip, 120, 60, 0.1):
            self._send(429, {"ok": False, "msg": "控制操作过于频繁，请稍后再试"},
                       headers={"Retry-After": "60"})
            return
        did = str(body.get("did") or "")
        d = find_device(did)
        if not d:
            self._send(404, {"ok": False, "msg": "设备不存在或未刷新"})
            return
        if not d.get("online"):
            self._send(200, {"ok": False, "msg": "设备离线"})
            return
        try:
            siid = int(body.get("siid"))
            aiid = int(body.get("aiid"))
        except Exception:
            self._send(400, {"ok": False, "msg": "动作参数无效"})
            return
        caps = _device_caps(d["model"]) or {}
        action = next((a for a in caps.get("actions") or []
                       if a["siid"] == siid and a["aiid"] == aiid), None)
        if not action:
            self._send(400, {"ok": False, "msg": "该动作不允许执行"})
            return
        ok, err = cloud_call_action(did, siid, aiid)
        LOG.info("audit action ip=%s did=%s %s-%s -> %s", ip, did, siid, aiid,
                 "OK" if ok else (err or "fail"))
        self._send(200, {"ok": ok, "msg": "" if ok else err})

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
                _go2rtc_base() + "/api/streams?src=" + urllib.parse.quote(name))
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read() or b"{}")
            cons = data.get("consumers") or []
            prods = [p for p in (data.get("producers") or []) if isinstance(p, dict)]
            recv = sum(int(p.get("bytes_recv") or 0) for p in prods)
            self._send(200, {"ok": True, "active": bool(cons) or recv > 0,
                             "consumers": len(cons)})
        except Exception:
            self._send(200, {"ok": False, "active": False, "consumers": 0})

    def api_camera_setup_state(self):
        payload = _camera_setup_payload()
        payload["admin_authed"] = _admin_authed(self)
        self._send(200, payload)

    def api_camera_setup_install(self):
        if not self._admin_require():
            return
        if not _camera_catalog():
            self._send(400, {"ok": False, "error": "当前平台不支持一键安装，请按页面步骤手动安装"})
            return
        if _camera_external_go2rtc():
            self._send(409, {"ok": False, "error": "已配置外部 go2rtc，请在它所在设备安装组件"})
            return
        with CAMERA_STACK_JOB_LOCK:
            if CAMERA_STACK_JOB.get("running"):
                self._send(409, {"ok": False, "error": "摄像头组件正在安装",
                                 "job": _camera_job_get()})
                return
            CAMERA_STACK_JOB.update({"running": True, "state": "starting",
                                     "message": "正在准备安装", "percent": 1,
                                     "ts": time.time()})
        threading.Thread(target=_camera_install_job, daemon=True).start()
        self._send(202, {"ok": True, "job": _camera_job_get()})

    def api_camera_setup_start(self):
        if not self._admin_require():
            return
        try:
            _camera_start()
            payload = _camera_setup_payload()
            payload["admin_authed"] = True
            self._send(200, payload)
        except Exception as exc:
            self._send(500, {"ok": False, "error": str(exc)[:240],
                             "job": _camera_job_get()})

    def api_speaker_assist(self):
        try:
            self._send(200, _speaker_payload())
        except Exception as e:
            self._send(200, {"ok": False, "enabled": False, "speakers": [],
                             "error": str(e)[:160]})

    def api_speaker_chat(self, body):
        sid = str(body.get("speaker_id") or "")
        text = str(body.get("text") or "").strip()
        if not CONFIG.get("speaker_panel_enabled", True) or not CONFIG.get("speaker_text_enabled", True):
            self._send(200, {"ok": False, "error": "前场音箱文字提问已在后台关闭"})
            return
        if not sid or not text:
            self._send(400, {"ok": False, "error": "请选择音箱并输入问题"})
            return
        max_chars = int(CONFIG.get("speaker_max_chars") or 200)
        if len(text) > max_chars:
            self._send(200, {"ok": False, "error": "问题太长，最多 %d 字" % max_chars})
            return
        info = _resolve_speaker(sid)
        if not info:
            self._send(200, {"ok": False, "error": "音箱不存在或已刷新，请重载面板"})
            return
        ip = self.client_address[0] if self.client_address else ""
        if not _speaker_rate_ok(ip, sid):
            self._send(200, {"ok": False, "error": "提问太频繁，请稍后再试"})
            return
        ready, ch = _speaker_chat_ready()
        if not ready:
            self._send(200, {"ok": False, "error": "AI 对话未启用，请在后台开启并保存"})
            return
        speak = (CONFIG.get("speaker_answer_mode") or "speak") != "text"
        code, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/chat/messages", {
            "account_id": info["account_id"], "device_id": info["device_id"],
            "text": text, "speak": speak,
        }, timeout=25)
        if code not in (200, 202) or not isinstance(resp, dict) or not resp.get("task_id"):
            detail = ""
            if isinstance(resp, dict):
                detail = resp.get("detail") or resp.get("error") or ""
            self._send(200, {"ok": False, "error": (detail or "提交失败")[:200]})
            return
        self._send(200, {"ok": True, "task_id": resp.get("task_id"),
                         "phase": resp.get("phase") or "queued", "speak": speak})

    def api_speaker_chat_status(self, u=None):
        q = urllib.parse.parse_qs(u.query) if u else {}
        info = _resolve_speaker((q.get("speaker_id") or [""])[0])
        if not info:
            self._send(200, {"ok": False, "error": "音箱不存在或已刷新"})
            return
        query = urllib.parse.urlencode({"account_id": info["account_id"],
                                        "device_id": info["device_id"]})
        _, st = _songloft_req("GET", MIOT_PLUGIN_API + "/chat/status?" + query, timeout=10)
        if not isinstance(st, dict):
            self._send(200, {"ok": False, "error": "读取回答状态失败"})
            return
        self._send(200, {"ok": True, "task_id": st.get("task_id") or "",
                         "phase": st.get("phase") or "",
                         "answer": st.get("answer") or "",
                         "error": st.get("error") or "",
                         "followup_until": st.get("followup_until") or 0})

    def api_speaker_listen(self, u=None):
        q = urllib.parse.parse_qs(u.query) if u else {}
        sid = (q.get("speaker_id") or [""])[0]
        try:
            since = int((q.get("since") or ["0"])[0] or 0)
        except ValueError:
            since = 0
        self._send(200, _speaker_listen(sid, since))

    def api_login(self, body):
        ip = self.client_address[0] if self.client_address else "?"
        if not _rate_ok("login:" + ip, 5, 600, 1.0):
            self._send(429, {"state": "error", "msg": "登录尝试过于频繁，请稍后再试"},
                       headers={"Retry-After": "600"})
            return
        user = (body.get("username") or "").strip()
        pwd = body.get("password") or ""
        if not user or not pwd:
            self._send(400, {"state": "error", "msg": "账号和密码不能为空"})
            return
        res = login_start(user, pwd)
        LOG.info("audit login ip=%s result=%s", ip, res.get("state"))
        self._send(200, res)

    def api_verify(self, body):
        ip = self.client_address[0] if self.client_address else "?"
        if not _rate_ok("verify:" + ip, 10, 600, 0.3):
            self._send(429, {"state": "error", "msg": "验证码尝试过于频繁，请稍后再试"},
                       headers={"Retry-After": "600"})
            return
        bid = body.get("bind_id") or ""
        code = body.get("code") or ""
        res = verify_finish(bid, code)
        LOG.info("audit verify ip=%s result=%s", ip, res.get("state"))
        self._send(200, res)

    def api_control(self, body):
        ip = self.client_address[0] if self.client_address else "?"
        if not _rate_ok("control:" + ip, 120, 60, 0.1):
            self._send(429, {"ok": False, "msg": "控制操作过于频繁，请稍后再试"},
                       headers={"Retry-After": "60"})
            return
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
        if d.get("role") == "sensor":
            self._send(400, {"ok": False, "msg": "温湿度传感器为只读设备"})
            return
        if not d.get("online"):
            self._send(200, {"ok": False, "msg": "设备离线"})
            return
        ok, err = cloud_set_prop(did, d["model"], cmd, value)
        LOG.info("audit control ip=%s did=%s cmd=%s=%s -> %s", ip, did, cmd, value,
                 "OK" if ok else (err or "fail"))
        if not ok:
            self._send(200, {"ok": False, "msg": err})
            return
        with STATE_LOCK:
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
        if theme not in THEME_IDS:
            self._send(400, {"ok": False, "msg": "不支持的主题"})
            return
        settings = load_settings()
        settings["theme"] = theme
        save_settings(settings)
        LOG.info("theme saved: %s", theme)
        self._send(200, {"ok": True, "theme": theme})

    def api_logout(self):
        global AUTH
        if _auth_source() == PLUGIN_AUTH_SOURCE:
            self._send(409, {"ok": False,
                             "msg": "当前设备登录来自小爱插件，请在「音乐与 AI 音箱」里解绑账号"})
            return
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

    def api_setup_state(self):
        music_connected = False
        account_count = 0
        music_url = ""
        try:
            _, ver = _songloft_req("GET", "/api/v1/version", timeout=2)
            music_connected = bool(str(ver.get("version") or "")) if isinstance(ver, dict) else False
            if music_connected:
                music_url = _songloft_public_url()
        except Exception:
            pass
        auth = _load_auth()
        pending_plugin = _read_pending_plugin_auth()
        if music_connected and (pending_plugin or (auth and auth.get("source") != PLUGIN_AUTH_SOURCE)):
            try:
                threading.Thread(target=_maybe_import_panel_auth, daemon=True).start()
            except Exception:
                pass
        if music_connected:
            try:
                _, acc = _songloft_req("GET", MIOT_PLUGIN_API + "/accounts", timeout=4)
                data = acc.get("data") if isinstance(acc, dict) else []
                account_count = len(data) if isinstance(data, list) else 0
            except Exception:
                pass
        stack = _stack_state_read()
        logged_in = bool(auth)
        pin_set = admin_pin_set()
        self._send(200, {
            "ok": True, "pin_set": pin_set,
            "admin_authed": _admin_authed(self),
            "logged_in": logged_in,
            "music_connected": music_connected,
            "music_url": music_url,
            "account_count": account_count,
            "plugin_sync_pending": bool(pending_plugin),
            "stack_installed": bool(stack.get("installed")),
            "platform": "windows" if os.name == "nt" else "linux",
            "job": _stack_job_get(),
            "lan_ip": str(CONFIG.get("lan_ip") or LAN_IP),
            "port": PORT, "version": APP_VERSION,
            "needs_onboarding": _needs_onboarding(),
        })

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
        theme = THEME_LEGACY.get(theme, theme)
        if theme not in THEME_IDS:
            theme = "dark"
        self._send(200, {
            "ok": True, "config": _public_config(),
            "devices": devices or [], "cloud_error": err,
            "logged_in": bool(_load_auth()), "auth_source": _auth_source(),
            "theme": theme,
            "layout": load_layout(), "weather": weather or None,
            "port": PORT, "pin_set": True, "version": APP_VERSION,
        })

    def api_admin_update_status(self):
        data = _update_status_read()
        payload = {k: data.get(k) for k in
                   ("state", "message", "percent", "ts", "current", "target")}
        payload["version"] = APP_VERSION
        self._send(200, payload)

    def api_admin_update_check(self, body):
        if not self._admin_require():
            return
        try:
            manifest = _fetch_update_manifest(force=bool(body.get("force")))
        except Exception as exc:
            self._send(200, {"ok": False, "current": APP_VERSION,
                             "update_url": _update_source(),
                             "error": str(exc)[:240]})
            return
        latest = manifest["version"]
        self._send(200, {
            "ok": True, "current": APP_VERSION, "latest": latest,
            "update_available": _version_tuple(latest) > _version_tuple(APP_VERSION),
            "notes": manifest["notes"], "size": manifest["size"],
            "released": manifest["released"], "update_url": _update_source(),
        })

    def api_admin_update_apply(self, body):
        if not self._admin_require():
            return
        if os.path.exists("/.dockerenv") or os.environ.get("MIOT_DOCKER") == "1":
            self._send(409, {"ok": False,
                             "error": "Docker 容器内不支持热更新，请在宿主机拉取新镜像"})
            return
        status = _update_status_read()
        try:
            age = time.time() - float(status.get("ts") or 0)
        except (TypeError, ValueError):
            age = 999999
        if status.get("state") in UPDATE_STATES and age < 300:
            self._send(409, {"ok": False,
                             "error": "已有更新正在进行：%s" % status.get("message", "")})
            return
        if UPDATE_RUNNING["active"]:
            self._send(409, {"ok": False, "error": "已有更新正在进行"})
            return
        UPDATE_RUNNING["active"] = True
        try:
            manifest = _fetch_update_manifest(force=True)
            if _version_tuple(manifest["version"]) <= _version_tuple(APP_VERSION):
                UPDATE_RUNNING["active"] = False
                self._send(400, {"ok": False,
                                 "error": "当前已是最新版本 v%s" % APP_VERSION})
                return
            threading.Thread(target=_update_prepare, args=(manifest,),
                             daemon=True).start()
            self._send(202, {"ok": True, "state": "downloading",
                             "target": manifest["version"]})
        except Exception as exc:
            UPDATE_RUNNING["active"] = False
            self._send(200, {"ok": False, "error": str(exc)[:240]})

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
        self._send(200, {"ok": True},
                   headers={"Set-Cookie": [_admin_cookie_header(token),
                                           _device_cookie_header()]})

    def api_admin_login(self, body):
        ip = self.client_address[0] if self.client_address else "?"
        if not _rate_ok("admin_login:" + ip, 10, 600, 0.4):
            self._send(429, {"ok": False, "msg": "PIN 尝试过于频繁，请 10 分钟后再试"},
                       headers={"Retry-After": "600"})
            return
        if not admin_pin_set():
            self._send(400, {"ok": False, "setup": True})
            return
        pin = str(body.get("pin") or "").strip()
        salt = CONFIG.get("admin_pin_salt") or ""
        expected = CONFIG.get("admin_pin_hash") or ""
        if not pin or not secrets.compare_digest(_pin_hash(pin, salt), expected):
            time.sleep(0.4)
            LOG.warning("audit admin_login ip=%s result=fail", ip)
            self._send(401, {"ok": False, "msg": "PIN 不正确"})
            return
        LOG.info("audit admin_login ip=%s result=ok", ip)
        token = _new_admin_session()
        self._send(200, {"ok": True},
                   headers={"Set-Cookie": [_admin_cookie_header(token),
                                           _device_cookie_header()]})

    def api_admin_logout(self):
        token = _cookie_value(self, ADMIN_COOKIE)
        if token:
            with ADMIN_LOCK:
                ADMIN_SESSIONS.pop(token, None)
        self._send(200, {"ok": True},
                   headers={"Set-Cookie": "%s=; Path=/; Max-Age=0" % ADMIN_COOKIE})

    def api_device_unlock(self, body):
        ip = self.client_address[0] if self.client_address else "?"
        if not _rate_ok("device_unlock:" + ip, 5, 600, 0.4):
            self._send(429, {"ok": False, "msg": "PIN 尝试过于频繁，请 10 分钟后再试"},
                       headers={"Retry-After": "600"})
            return
        if not admin_pin_set():
            self._send(200, {"ok": True, "setup": True})
            return
        pin = str(body.get("pin") or "").strip()
        salt = CONFIG.get("admin_pin_salt") or ""
        expected = CONFIG.get("admin_pin_hash") or ""
        if not pin or not secrets.compare_digest(_pin_hash(pin, salt), expected):
            time.sleep(0.4)
            LOG.warning("audit device_unlock ip=%s result=fail", ip)
            self._send(401, {"ok": False, "msg": "PIN 不正确"})
            return
        LOG.info("audit device_unlock ip=%s result=ok", ip)
        token = _new_admin_session()
        self._send(200, {"ok": True},
                   headers={"Set-Cookie": [_admin_cookie_header(token),
                                           _device_cookie_header()]})

    def api_device_lock(self):
        if not _device_authed(self):
            self._send(401, {"ok": False, "msg": "需要先验证 PIN"})
            return
        self._send(200, {"ok": True},
                   headers={"Set-Cookie": [
                       "%s=; Path=/; Max-Age=0" % DEVICE_COOKIE,
                       "%s=; Path=/; Max-Age=0" % ADMIN_COOKIE]})

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
        for key in ("speaker_panel_enabled", "speaker_text_enabled",
                    "speaker_voice_enabled"):
            if key in body:
                updates[key] = bool(body.get(key))
        if "speaker_answer_mode" in body:
            mode = str(body.get("speaker_answer_mode") or "speak")
            if mode in ("speak", "text"):
                updates["speaker_answer_mode"] = mode
        if "speaker_max_chars" in body:
            try:
                updates["speaker_max_chars"] = min(500, max(20, int(body.get("speaker_max_chars"))))
            except Exception:
                pass
        if "speaker_hidden" in body:
            updates["speaker_hidden"] = _str_list(body.get("speaker_hidden"), 500)
        if "speaker_default" in body:
            updates["speaker_default"] = str(body.get("speaker_default") or "")[:120]
        if "update_url" in body:
            value = str(body.get("update_url") or "").strip()[:500]
            if value and not value.startswith(("http://", "https://")):
                self._send(400, {"ok": False, "msg": "更新源必须是 http(s) 地址"})
                return
            updates["update_url"] = value
            with UPDATE_CACHE_LOCK:
                UPDATE_CACHE["ts"] = 0
                UPDATE_CACHE["data"] = None
        if "go2rtc_url" in body:
            value = str(body.get("go2rtc_url") or "").strip().rstrip("/")[:500]
            if value and not value.startswith(("http://", "https://")):
                self._send(400, {"ok": False, "msg": "go2rtc 地址必须是 http(s) 地址"})
                return
            updates["go2rtc_url"] = value
            CAM_CACHE.update(ts=0, items=[])
            CAM_META.update(ts=0, items=[])
        if "lan_ip" in body:
            global LAN_IP
            value = str(body.get("lan_ip") or "").strip()[:64]
            if value:
                try:
                    ipaddress.ip_address(value)
                except ValueError:
                    self._send(400, {"ok": False, "msg": "局域网地址格式不正确"})
                    return
            updates["lan_ip"] = value
            LAN_IP = value or get_lan_ip()
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
        global AUTH
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
        if what in ("settings", "factory"):
            if what == "settings":
                keep = ("admin_pin_salt", "admin_pin_hash", "update_url", "lan_ip",
                        "songloft_url", "songloft_user", "songloft_pass",
                        "go_music_dl_url", "go2rtc_url", "plugin_account_file", "country")
                base = dict(DEFAULT_CONFIG)
                base.update({k: CONFIG.get(k) for k in keep if CONFIG.get(k)})
                save_config(base)
            for path in (LAYOUT_FILE, SETTINGS_FILE, PAGES_FILE, DEV_CACHE,
                         STATE_CACHE, PLUGIN_AUTH_BACKUP,
                         os.path.join(ROOT, "plugin_pending_auth.json")):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
            try:
                shutil.rmtree(PAGES_DIR)
            except OSError:
                pass
            if what == "factory":
                AUTH = None
                for path in (AUTH_FILE, CONFIG_FILE):
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except OSError:
                        pass
            LOG.info("admin reset: %s", what)
            self._send(200, {"ok": True, "restarting": True, "scope": what})
            threading.Timer(0.8, _restart_self).start()
            return
        self._send(200, {"ok": True})

    def api_admin_xiaomi_logout(self):
        global AUTH
        if not self._admin_require():
            return
        if _auth_source() == PLUGIN_AUTH_SOURCE:
            self._send(409, {"ok": False,
                             "msg": "当前设备登录来自小爱插件，请在「音乐与 AI 音箱」里解绑账号"})
            return
        AUTH = None
        try:
            if os.path.exists(AUTH_FILE):
                os.remove(AUTH_FILE)
        except OSError:
            pass
        self._send(200, {"ok": True})

    def api_page_state(self):
        try:
            self._send(200, _page_state())
        except Exception as e:
            self._send(200, {"ok": False, "active": "", "error": str(e)[:160]})

    def api_admin_pages(self, u=None):
        if not self._admin_require():
            return
        meta = _load_pages_meta()
        active = meta.get("active") or ""
        items = []
        for it in meta.get("items") or []:
            page = dict(it)
            page["active"] = page["id"] == active
            items.append(page)
        out = {"ok": True, "active": active, "items": items}
        q = urllib.parse.parse_qs(u.query) if u else {}
        pid = (q.get("id") or [""])[0]
        if pid:
            html = _read_page_html(pid)
            out["html"] = html or ""
            out["page"] = next((x for x in items if x["id"] == pid), None)
        self._send(200, out)

    def api_admin_pages_save(self, body):
        if not self._admin_require():
            return
        action = str(body.get("action") or "save")
        with PAGES_LOCK:
            meta = _load_pages_meta()
            if action == "activate":
                pid = str(body.get("id") or "")
                if not _find_page(meta, pid) or _read_page_html(pid) is None:
                    self._send(400, {"ok": False, "msg": "页面不存在"})
                    return
                meta["active"] = pid
                _save_pages_meta(meta)
                self._send(200, {"ok": True, "active": pid})
                return
            if action == "deactivate":
                meta["active"] = ""
                _save_pages_meta(meta)
                self._send(200, {"ok": True, "active": ""})
                return
            if action == "delete":
                pid = str(body.get("id") or "")
                it = _find_page(meta, pid)
                if not it:
                    self._send(400, {"ok": False, "msg": "页面不存在"})
                    return
                meta["items"] = [x for x in meta["items"] if x["id"] != pid]
                if meta.get("active") == pid:
                    meta["active"] = ""
                _save_pages_meta(meta)
                try:
                    os.remove(os.path.join(PAGES_DIR, pid + ".html"))
                except OSError:
                    pass
                self._send(200, {"ok": True, "active": meta.get("active") or ""})
                return
            if action != "save":
                self._send(400, {"ok": False, "msg": "未知操作"})
                return
            name = str(body.get("name") or "").strip()[:40]
            html = body.get("html")
            if not name:
                self._send(400, {"ok": False, "msg": "页面名称不能为空"})
                return
            if not isinstance(html, str) or not html.strip():
                self._send(400, {"ok": False, "msg": "页面 HTML 不能为空"})
                return
            if len(html.encode("utf-8")) > 512 * 1024:
                self._send(400, {"ok": False, "msg": "页面 HTML 不能超过 512KB"})
                return
            pid = str(body.get("id") or "")
            it = _find_page(meta, pid)
            if not it:
                pid = "p_" + str(int(time.time())) + "_" + secrets.token_hex(3)
                it = {"id": pid, "name": name, "updated": 0, "size": 0}
                meta["items"].append(it)
            it["name"] = name
            it["updated"] = int(time.time() * 1000)
            it["size"] = len(html.encode("utf-8"))
            os.makedirs(PAGES_DIR, exist_ok=True)
            try:
                os.chmod(PAGES_DIR, 0o700)
            except OSError:
                pass
            path = os.path.join(PAGES_DIR, pid + ".html")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(html)
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            if body.get("activate"):
                meta["active"] = pid
            _save_pages_meta(meta)
            self._send(200, {"ok": True, "id": pid, "active": meta.get("active") or "",
                             "item": it})
            return

    def api_admin_logs_clear(self):
        if not self._admin_require():
            return
        if _journald_available():
            before = ""
            after = ""
            try:
                before = subprocess.run(["journalctl", "--disk-usage"], capture_output=True,
                                        timeout=15).stdout.decode("utf-8", "replace").strip()
                subprocess.run(["journalctl", "--rotate"], capture_output=True, timeout=25)
                subprocess.run(["journalctl", "--vacuum-time=1s"], capture_output=True, timeout=60)
                after = subprocess.run(["journalctl", "--disk-usage"], capture_output=True,
                                       timeout=15).stdout.decode("utf-8", "replace").strip()
                LOG.info("admin cleared journal logs")
                self._send(200, {"ok": True, "before": before, "after": after,
                                 "msg": "已清理系统历史 journal 日志"})
                return
            except Exception as e:
                LOG.warning("journal clear failed: %s", e)
        if _clear_file_log():
            LOG.info("admin cleared file log: %s", LOG_FILE)
            self._send(200, {"ok": True, "msg": "已清理面板日志文件",
                             "file": os.path.basename(LOG_FILE)})
            return
        self._send(200, {"ok": False, "msg": "当前平台没有可清理的日志（未启用 journald 或文件日志）"})

    def api_admin_logs(self):
        if not self._admin_require():
            return
        lines = []
        if _journald_available():
            try:
                out = subprocess.run(["journalctl", "-u", "miot-cloud", "-n", "120",
                                      "--no-pager", "-o", "short"],
                                     capture_output=True, timeout=10).stdout.decode(
                                         "utf-8", "replace")
                lines = [x for x in out.splitlines() if x.strip()]
            except Exception:
                lines = []
        if not lines:
            lines = _read_file_log(120)
        if not lines:
            lines = ["暂无日志：Linux 使用 journald，Windows/Docker 可在设置 MIOT_LOG_FILE 后查看文件日志。"]
        self._send(200, {"ok": True, "lines": lines, "platform": "windows" if os.name == "nt" else sys.platform})

    def api_admin_music_info(self, u=None):
        if not self._admin_require():
            return
        info = {"ok": True, "port": _songloft_port(), "active": False,
                "version": "", "plugin": False, "url": _songloft_public_url()}
        try:
            with urllib.request.urlopen(_songloft_base() + "/api/v1/version", timeout=3) as r:
                data = json.loads(r.read() or b"{}")
            info["version"] = str(data.get("version") or "")
            info["active"] = bool(info["version"])
        except Exception:
            pass
        try:
            with urllib.request.urlopen(
                    _songloft_base() + "/api/v1/jsplugin/miot/", timeout=3) as r:
                info["plugin"] = r.status == 200
        except Exception:
            pass
        self._send(200, info)

    def api_admin_music_connect(self, body):
        if not self._admin_require():
            return
        if body.get("clear"):
            save_config({"songloft_url": "", "songloft_user": "",
                         "songloft_pass": "", "go_music_dl_url": ""})
            _clear_songloft_token()
            self._send(200, {"ok": True, "cleared": True})
            return
        url = str(body.get("url") or "").strip().rstrip("/")[:500]
        user = str(body.get("user") or "").strip()[:120]
        pwd = str(body.get("password") or "")[:200]
        gmd_url = str(body.get("gmd_url") or "").strip().rstrip("/")[:500]
        if not url.startswith(("http://", "https://")):
            self._send(400, {"ok": False, "error": "请输入 http(s) 音乐中心地址"})
            return
        if not user:
            user = str(CONFIG.get("songloft_user") or "")
        if not pwd:
            pwd = str(CONFIG.get("songloft_pass") or "")
        if not user or not pwd:
            self._send(400, {"ok": False, "error": "请输入音乐中心管理员账号和密码"})
            return
        try:
            with urllib.request.urlopen(url + "/api/v1/version", timeout=6) as resp:
                version = json.loads(resp.read() or b"{}").get("version") or ""
            req = urllib.request.Request(
                url + "/api/v1/auth/login",
                data=json.dumps({"username": user, "password": pwd}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=8) as resp:
                login = json.loads(resp.read() or b"{}")
            token = str(login.get("access_token") or "")
            if not token:
                raise RuntimeError("管理员账号或密码不正确")
            headers = {"Authorization": "Bearer " + token}
            plugins = []
            try:
                req = urllib.request.Request(url + "/api/v1/jsplugins", headers=headers)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    plugins = json.loads(resp.read() or b"{}").get("plugins") or []
            except Exception:
                pass
            gmd_base = ""
            try:
                req = urllib.request.Request(
                    url + "/api/v1/jsplugin/go-music-dl/config", headers=headers)
                with urllib.request.urlopen(req, timeout=6) as resp:
                    gmd_base = str(json.loads(resp.read() or b"{}").get("baseUrl") or "")
            except Exception:
                pass
            sso_ready = False
            try:
                with urllib.request.urlopen(
                        url + "/api/v1/jsplugin/panel-sso/", timeout=4) as resp:
                    sso_ready = resp.status == 200
            except Exception:
                pass
        except Exception as exc:
            self._send(200, {"ok": False,
                             "error": "连接音乐中心失败：" + str(exc)[:180]})
            return
        if not gmd_url:
            gmd_url = gmd_base
        save_config({"songloft_url": url, "songloft_user": user,
                     "songloft_pass": pwd, "go_music_dl_url": gmd_url})
        stack = _stack_state_read()
        if stack.get("installed"):
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
            stack["disabled"] = host not in ("127.0.0.1", "localhost", LAN_IP.lower())
            _stack_state_write(stack)
        _clear_songloft_token()
        try:
            threading.Thread(target=_import_panel_auth_to_plugin,
                             kwargs={"force": True}, daemon=True).start()
        except Exception:
            pass
        names = []
        for item in plugins if isinstance(plugins, list) else []:
            if isinstance(item, dict):
                names.append({"name": item.get("name") or "",
                              "status": item.get("status") or ""})
        self._send(200, {"ok": True, "url": url, "version": version,
                         "plugins": names, "sso_ready": sso_ready,
                         "go_music_dl_url": gmd_url})

    def api_admin_music_stack_status(self):
        if not self._admin_require():
            return
        state = _stack_state_read()
        sport = int(state.get("songloft_port") or 58091)
        gport = int(state.get("gmd_port") or 8081)
        sl = _port_json("http://127.0.0.1:%d/api/v1/version" % sport) if os.name == "nt" else None
        gmd = bool(_wait_http("http://127.0.0.1:%d/music/" % gport, 2))             if os.name == "nt" else False
        logs = ""
        try:
            with open(_stack_paths()["sl_log"], "rb") as f:
                logs = f.read()[-4000:].decode("utf-8", "replace")
        except OSError:
            pass
        if os.name == "nt":
            missing_src = _stack_missing(MUSIC_STACK_SRC)
            missing_cache = _stack_missing(MUSIC_STACK_CACHE)
            source_ready = not missing_src or not missing_cache
        else:
            missing_src = []
            missing_cache = []
            source_ready = False
        can_download = os.name == "nt"
        self._send(200, {
            "ok": True, "platform": "windows" if os.name == "nt" else "linux",
            "supported": source_ready or can_download,
            "source_ready": source_ready, "can_download": can_download,
            "missing": [] if source_ready else missing_src,
            "installed": bool(state.get("installed")),
            "running": bool(sl) and gmd,
            "songloft_running": bool(sl), "gmd_running": gmd,
            "songloft_port": sport, "gmd_port": gport,
            "songloft_url": state.get("songloft_url") or
            ("http://127.0.0.1:%d" % sport if state.get("installed") else ""),
            "disabled": bool(state.get("disabled")),
            "job": _stack_job_get(),
            "log_tail": logs[-2000:],
        })

    def api_admin_music_stack_install(self):
        if not self._admin_require():
            return
        if os.name != "nt":
            self._send(400, {"ok": False,
                             "error": "Linux 请运行 music/install-music-stack.sh；Docker 请连接外部音乐中心"})
            return
        with MUSIC_STACK_JOB_LOCK:
            if MUSIC_STACK_JOB["running"]:
                self._send(409, {"ok": False, "error": "音乐组件正在安装，请稍候"})
                return
            MUSIC_STACK_JOB.update({
                "running": True, "state": "starting",
                "message": "正在启动安装", "percent": 0,
                "ts": time.time(),
            })
        threading.Thread(target=_stack_install_job, daemon=True).start()
        self._send(202, {"ok": True, "state": "starting"})

    def api_admin_music_stack_start(self):
        if not self._admin_require():
            return
        try:
            state = _stack_start()
            self._send(200, {"ok": True, "state": state})
        except Exception as exc:
            self._send(200, {"ok": False, "error": str(exc)[:300]})

    def api_admin_music_stack_stop(self):
        if not self._admin_require():
            return
        try:
            _stack_stop(disable=True)
            self._send(200, {"ok": True})
        except Exception as exc:
            self._send(200, {"ok": False, "error": str(exc)[:300]})

    def api_admin_laninfo(self):
        if not self._admin_require():
            return
        ip = str(CONFIG.get("lan_ip") or LAN_IP)
        suffix = "" if PORT == 80 else ":%d" % PORT
        base = "http://%s%s" % (ip, suffix)
        phone_url = base + "/login?m=phone"
        self._send(200, {
            "ok": True, "lan_ip": ip, "port": PORT,
            "login_url": base + "/login", "phone_url": phone_url,
            "admin_url": base + "/admin", "panel_url": base + "/",
            "qr": build_qr(phone_url), "candidates": _lan_candidates(),
            "platform": "windows" if os.name == "nt" else "linux",
        })

    def api_admin_firewall(self):
        if not self._admin_require():
            return
        ports = []
        for value in (PORT, _songloft_port(), _gmd_port()):
            if value not in ports:
                ports.append(value)
        programs = _camera_firewall_programs()
        ok, message = _add_firewall_rules(ports, programs, [DISCOVERY_PORT])
        self._send(200, {"ok": ok, "message": message, "ports": ports,
                         "udp_ports": [DISCOVERY_PORT],
                         "camera_programs": len(programs)})

    def api_admin_music_state(self):
        if not self._admin_require():
            return
        try:
            self._send(200, _music_state())
        except Exception as e:
            LOG.warning("music state failed: %s", e)
            self._send(200, {"ok": False, "error": str(e)[:200]})

    def api_admin_music_devices(self):
        if not self._admin_require():
            return
        try:
            self._send(200, _music_devices())
        except Exception as e:
            LOG.warning("music devices failed: %s", e)
            self._send(200, {"ok": False, "error": str(e)[:200], "accounts": []})

    def api_admin_music_web_sso(self):
        if not self._admin_require():
            return
        try:
            _songloft_req("GET", "/api/v1/version", timeout=3)
        except Exception:
            self._send(200, {"ok": False,
                             "error": "音乐中心未连接，请先安装或连接音乐中心"})
            return
        try:
            self._send(200, {"ok": True, "url": _songloft_sso_url()})
        except Exception as e:
            LOG.warning("music SSO failed: %s", e)
            self._send(200, {"ok": False, "error": str(e)[:200]})

    def api_auth_qr_start(self, body):
        if not self._admin_require():
            return
        try:
            data = _qr_start()
        except Exception as exc:
            LOG.warning("native QR start failed: %s", str(exc)[:200])
            self._send(200, {"ok": False, "error": str(exc)[:220]})
            return
        self._send(200, data)

    def api_auth_qr_poll(self, body):
        if not self._admin_require():
            return
        sid = str(body.get("session_id") or "").strip()
        if not sid:
            self._send(400, {"ok": False, "error": "缺少扫码会话"})
            return
        try:
            self._send(200, _qr_poll(sid))
        except Exception as exc:
            LOG.warning("native QR poll failed: %s", str(exc)[:200])
            self._send(200, {"ok": False, "state": "failed",
                             "message": str(exc)[:180]})

    def api_admin_music_import_auth(self):
        if not self._admin_require():
            return
        try:
            self._send(200, _import_panel_auth_to_plugin(force=True))
        except Exception as exc:
            self._send(200, {"ok": False, "error": str(exc)[:200]})

    def api_admin_music_qr_start(self, body):
        if not self._admin_require():
            return
        _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/auth/qrcode", {}, timeout=25)
        if not isinstance(resp, dict) or not resp.get("success"):
            self._send(200, {"ok": False,
                             "error": (resp or {}).get("error") or "二维码获取失败"})
            return
        self._send(200, {"ok": True,
                         "account_id": resp.get("account_id") or "",
                         "qrcode_url": resp.get("qrcode_url") or "",
                         "login_url": resp.get("login_url") or ""})

    def api_admin_music_qr_poll(self, body):
        if not self._admin_require():
            return
        account_id = str(body.get("account_id") or "").strip()
        if not account_id:
            self._send(400, {"ok": False, "error": "缺少扫码会话"})
            return
        _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/auth/qrcode/poll",
                                {"account_id": account_id}, timeout=40)
        if not isinstance(resp, dict) or not resp.get("success"):
            self._send(200, {"ok": False,
                             "error": (resp or {}).get("error") or "扫码状态读取失败"})
            return
        state = resp.get("state") or ""
        out = {"ok": True, "state": state,
               "account_id": resp.get("account_id") or account_id,
               "message": resp.get("message") or ""}
        if state == "success":
            try:
                _music_autoconfig()
                out["configured"] = True
            except Exception as e:
                LOG.warning("music autoconfig failed: %s", e)
                out["configured"] = False
                out["configure_error"] = str(e)[:160]
            try:
                linked = _sync_plugin_auth(force=True)
                out["panel_login_linked"] = bool(linked)
            except Exception as e:
                LOG.warning("panel auth link after QR failed: %s", e)
                out["panel_login_linked"] = False
        self._send(200, out)

    def api_admin_music_account_delete(self, body):
        if not self._admin_require():
            return
        account_id = str(body.get("account_id") or "").strip()
        if not account_id:
            self._send(400, {"ok": False, "error": "缺少账号"})
            return
        code, resp = _songloft_req(
            "DELETE", MIOT_PLUGIN_API + "/account?account_id=" +
            urllib.parse.quote(account_id), timeout=15)
        if code != 200 or not (isinstance(resp, dict) and resp.get("success")):
            self._send(200, {"ok": False,
                             "error": (resp or {}).get("error") or "解绑失败"})
            return
        _songloft_req("POST", MIOT_PLUGIN_API + "/config", {
            "conversation_monitor_enabled": False,
            "voice_command_enabled": False,
        }, timeout=15)
        panel_logged_in = True
        auth = _load_auth()
        if auth and (auth.get("source") == PLUGIN_AUTH_SOURCE
                     or _same_user(auth.get("user_id"), account_id)):
            globals()["AUTH"] = None
            try:
                if os.path.exists(AUTH_FILE):
                    os.remove(AUTH_FILE)
            except OSError:
                pass
            panel_logged_in = False
        self._send(200, {"ok": True, "panel_logged_in": panel_logged_in})

    def api_admin_music_device(self, body):
        if not self._admin_require():
            return
        account_id = str(body.get("account_id") or "").strip()
        device_id = str(body.get("device_id") or "").strip()
        if not account_id or not device_id:
            self._send(400, {"ok": False, "error": "缺少账号或设备"})
            return
        try:
            if "managed" in body:
                _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/mina/device/managed", {
                    "account_id": account_id, "device_id": device_id,
                    "managed": bool(body.get("managed")),
                }, timeout=20)
            elif "volume" in body:
                vol = max(0, min(100, int(body.get("volume") or 0)))
                _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/mina/volume", {
                    "account_id": account_id, "device_id": device_id, "volume": vol,
                }, timeout=18)
            elif body.get("selected"):
                _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/mina/last_selection", {
                    "account_id": account_id, "device_id": device_id,
                }, timeout=12)
            else:
                self._send(400, {"ok": False, "error": "没有可执行的设备操作"})
                return
        except Exception as e:
            self._send(200, {"ok": False, "error": str(e)[:160]})
            return
        ok = isinstance(resp, dict) and resp.get("success")
        self._send(200, {"ok": bool(ok),
                         "error": "" if ok else ((resp or {}).get("error") or "操作失败")})

    def api_admin_music_device_all(self, body):
        if not self._admin_require():
            return
        managed = body.get("managed", True) is not False
        data = _music_devices()
        if not data.get("ok"):
            self._send(200, {"ok": False, "error": data.get("error") or "读取音箱失败"})
            return
        count = 0
        for acc in data.get("accounts") or []:
            for d in acc.get("devices") or []:
                if bool(d.get("managed")) == managed:
                    continue
                _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/mina/device/managed", {
                    "account_id": acc["account_id"], "device_id": d["device_id"],
                    "managed": managed,
                }, timeout=15)
                if isinstance(resp, dict) and resp.get("success"):
                    count += 1
        self._send(200, {"ok": True, "updated": count})

    def api_admin_music_group_all(self, body):
        if not self._admin_require():
            return
        name = str(body.get("name") or "全屋音箱").strip()[:30] or "全屋音箱"
        data = _music_devices()
        if not data.get("ok"):
            self._send(200, {"ok": False, "error": data.get("error") or "读取音箱失败"})
            return
        members = []
        for acc in data.get("accounts") or []:
            for d in acc.get("devices") or []:
                if d.get("managed") and d.get("device_id"):
                    members.append({"account_id": acc["account_id"],
                                    "device_id": d["device_id"]})
        if len(members) < 2:
            self._send(200, {"ok": False, "error": "至少需要两台已启用的音箱才能组网"})
            return
        _, grp = _songloft_req("GET", MIOT_PLUGIN_API + "/groups", timeout=12)
        groups = (grp.get("data") or []) if isinstance(grp, dict) else []
        existing = next((g for g in groups if isinstance(g, dict)
                         and g.get("name") == name), None)
        if existing:
            _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/groups/update", {
                "id": existing.get("id"), "name": name, "members": members,
            }, timeout=20)
        else:
            _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/groups", {
                "name": name, "members": members,
            }, timeout=20)
        ok = isinstance(resp, dict) and resp.get("success")
        self._send(200, {"ok": bool(ok), "count": len(members),
                         "name": name,
                         "error": "" if ok else ((resp or {}).get("error") or "组网失败")})

    def api_admin_music_group(self, body):
        if not self._admin_require():
            return
        action = str(body.get("action") or "save")
        if action == "delete":
            gid = str(body.get("group_id") or "").strip()
            if not gid:
                self._send(400, {"ok": False, "error": "缺少分组 ID"})
                return
            code, resp = _songloft_req(
                "DELETE", MIOT_PLUGIN_API + "/groups?id=" +
                urllib.parse.quote(gid), timeout=15)
            ok = code == 200 and isinstance(resp, dict) and resp.get("success")
            self._send(200, {"ok": bool(ok),
                             "error": "" if ok else ((resp or {}).get("error") or "删除分组失败")})
            return
        name = str(body.get("name") or "").strip()[:30]
        members = []
        seen = set()
        for m in (body.get("members") or []):
            if not isinstance(m, dict):
                continue
            aid = str(m.get("account_id") or "").strip()
            did = str(m.get("device_id") or "").strip()
            if not aid or not did:
                continue
            key = aid + "|" + did
            if key in seen:
                continue
            seen.add(key)
            members.append({"account_id": aid, "device_id": did})
        if not name:
            self._send(400, {"ok": False, "error": "分组名称不能为空"})
            return
        if len(members) < 2:
            self._send(400, {"ok": False, "error": "至少选择两台音箱才能建立分组"})
            return
        gid = str(body.get("group_id") or "").strip()
        if not gid:
            _, grp = _songloft_req("GET", MIOT_PLUGIN_API + "/groups", timeout=12)
            for g in ((grp.get("data") or []) if isinstance(grp, dict) else []):
                if isinstance(g, dict) and g.get("name") == name:
                    gid = str(g.get("id") or "")
                    break
        if gid:
            _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/groups/update", {
                "id": gid, "name": name, "members": members,
            }, timeout=20)
        else:
            _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/groups", {
                "name": name, "members": members,
            }, timeout=20)
        ok = isinstance(resp, dict) and resp.get("success")
        self._send(200, {"ok": bool(ok), "group": (resp or {}).get("data") if ok else None,
                         "error": "" if ok else ((resp or {}).get("error") or "保存分组失败")})

    def api_admin_music_ai(self, body):
        if not self._admin_require():
            return
        _, cfg_resp = _songloft_req("GET", MIOT_PLUGIN_API + "/config", timeout=12)
        cfg = cfg_resp.get("data") if isinstance(cfg_resp, dict) else None
        if not isinstance(cfg, dict):
            self._send(200, {"ok": False, "error": "读取小爱插件配置失败"})
            return
        ai = dict(cfg.get("ai_config") or {})
        if "ai_enabled" in body:
            ai["enabled"] = bool(body.get("ai_enabled"))
        for src, dst in (("ai_api_url", "api_url"), ("ai_model", "model")):
            if src in body:
                ai[dst] = str(body.get(src) or "").strip()
        if "ai_timeout" in body:
            try:
                ai["timeout"] = max(1, min(30, int(body.get("ai_timeout"))))
            except (TypeError, ValueError):
                pass
        ai_key = str(body.get("ai_api_key") or "").strip()
        if ai_key:
            ai["api_key"] = ai_key
        elif body.get("ai_api_key_clear"):
            ai["api_key"] = ""
        if not str(ai.get("api_url") or "").lower().startswith(("http://", "https://")):
            self._send(400, {"ok": False, "error": "AI 接口地址必须以 http:// 或 https:// 开头"})
            return
        if ai.get("enabled") and (not ai.get("api_key") or not ai.get("model")):
            self._send(400, {"ok": False,
                             "error": "启用语音 AI 需要 API Key 和模型名；换 Key 时不要勾选“清除已保存的 Key”"})
            return
        patch = {"ai_config": ai}
        for src, dst in (("voice_command_enabled", "voice_command_enabled"),
                         ("conversation_monitor_enabled", "conversation_monitor_enabled")):
            if src in body:
                patch[dst] = bool(body.get(src))
        code, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/config", patch, timeout=35)
        if code != 200:
            self._send(200, {"ok": False,
                             "error": (resp or {}).get("error") or "保存 AI 配置失败"})
            return

        _, cur_chat = _songloft_req("GET", MIOT_PLUGIN_API + "/chat/config", timeout=12)
        chat = dict(cur_chat) if isinstance(cur_chat, dict) else {}
        fields = {}
        if "chat_enabled" in body:
            fields["enabled"] = bool(body.get("chat_enabled"))
        for k in ("enabled", "fallback_enabled", "resume_music",
                  "waiting_feedback", "memory_enabled", "streaming_enabled"):
            if k in body:
                fields[k] = bool(body.get(k))
        if "connection" in body:
            if body.get("connection") not in ("analysis", "independent"):
                self._send(400, {"ok": False, "error": "AI 对话连接方式无效"})
                return
            fields["connection"] = body.get("connection")
        for k in ("api_url", "model", "system_prompt"):
            if k in body:
                fields[k] = str(body.get(k) or "").strip()
        if "api_key" in body and str(body.get("api_key") or "").strip():
            fields["api_key"] = str(body.get("api_key")).strip()
        elif body.get("api_key_clear"):
            fields["api_key"] = ""
        for k, lo, hi in (("timeout", 1, 30), ("followup_seconds", 0, 300),
                          ("context_turns", 0, 24)):
            if k in body:
                try:
                    fields[k] = max(lo, min(hi, int(body.get(k))))
                except (TypeError, ValueError):
                    pass
        if "trigger_words" in body:
            raw = body.get("trigger_words")
            if isinstance(raw, str):
                raw = raw.replace("，", ",").replace("\n", ",").split(",")
            words = []
            for w in (raw or []):
                w = str(w).strip()
                if w and w not in words:
                    words.append(w[:30])
            if not words or len(words) > 20:
                self._send(400, {"ok": False, "error": "触发词需要 1-20 个"})
                return
            fields["trigger_words"] = words
        if "system_prompt" in fields and not fields["system_prompt"]:
            self._send(400, {"ok": False, "error": "人设提示词不能为空"})
            return

        merged = dict(chat)
        merged.update(fields)
        enabled = bool(merged.get("enabled"))
        conn = merged.get("connection") or "analysis"
        if enabled:
            if conn == "independent":
                if not str(merged.get("api_url") or "").startswith(("http://", "https://")):
                    self._send(400, {"ok": False, "error": "独立连接需要填写 AI 接口地址"})
                    return
                if not merged.get("api_key"):
                    self._send(400, {"ok": False, "error": "独立连接需要填写 API Key"})
                    return
            else:
                if not ai.get("api_key") or not str(ai.get("api_url") or "").startswith(("http://", "https://")):
                    self._send(400, {"ok": False,
                                     "error": "共用连接需要先在上方填写可用的 AI 接口和 Key"})
                    return
        if fields:
            code, resp = _songloft_req("PUT", MIOT_PLUGIN_API + "/chat/config",
                                       fields, timeout=25)
            if code != 200:
                self._send(200, {"ok": False,
                                 "error": (resp or {}).get("detail")
                                 or (resp or {}).get("error") or "保存人设配置失败"})
                return
        self._send(200, {"ok": True, "ai_key_set": bool(ai.get("api_key")),
                         "chat_key_set": bool(merged.get("api_key"))})

    def api_admin_music_source(self, body):
        if not self._admin_require():
            return
        host = str(body.get("server_host") or "").strip().rstrip("/")
        if not host:
            host = _songloft_server_url()
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        hostname = (urllib.parse.urlsplit(host).hostname or "").lower()
        if hostname in ("127.0.0.1", "localhost", "::1"):
            self._send(400, {"ok": False,
                             "error": "服务器地址不能是回环地址，音箱无法访问"})
            return
        raw_sources = body.get("sources")
        if not isinstance(raw_sources, list):
            raw_sources = list(MUSIC_DEFAULT_SOURCES)
        sources = [s for s in raw_sources if s in MUSIC_SOURCES]
        if not sources:
            sources = list(MUSIC_DEFAULT_SOURCES)
        external = body.get("external_enabled", True) is not False
        no_import = body.get("no_import", True) is not False
        quality = str(body.get("quality") or "exhigh")
        if quality not in ("standard", "exhigh", "lossless", "hires"):
            quality = "exhigh"
        patch = {
            "server_host": host,
            "external_search_enabled": external,
            "external_search_no_import": no_import,
            "external_search_sources": [{
                "id": "go-music-dl", "name": "GoMusicDL", "url": GMD_SEARCH_PATH,
                "token": "", "enabled": True,
            }],
            "external_search_timeout": 6,
            "search_priority": "parallel",
        }
        code, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/config", patch, timeout=20)
        if code != 200:
            self._send(200, {"ok": False,
                             "error": (resp or {}).get("error") or "保存音源配置失败"})
            return
        gcode, gresp = _songloft_req("POST", GMD_PLUGIN_API + "/config", {
            "baseUrl": _gmd_base(),
            "basePath": "/music",
            "serverHost": host,
            "sources": sources,
            "defaultQuality": quality,
        }, timeout=15)
        if gcode != 200:
            self._send(200, {"ok": False,
                             "error": (gresp or {}).get("error") or "保存在线音源失败"})
            return
        _ensure_proxy_allowlist(host)
        self._send(200, {"ok": True, "server_host": host, "sources": sources})

    def api_admin_music_search_test(self, body):
        if not self._admin_require():
            return
        try:
            self._send(200, _music_search_test(body.get("keyword")))
        except Exception as e:
            self._send(200, {"ok": False, "error": str(e)[:160]})

    def api_admin_music_tts_test(self, body):
        if not self._admin_require():
            return
        account_id = str(body.get("account_id") or "").strip()
        device_id = str(body.get("device_id") or "").strip()
        text = str(body.get("text") or "").strip()[:300]
        if not account_id or not device_id or not text:
            self._send(400, {"ok": False, "error": "请选择音箱并输入播报文字"})
            return
        _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/mina/tts", {
            "account_id": account_id, "device_id": device_id, "text": text,
        }, timeout=30)
        ok = isinstance(resp, dict) and resp.get("success")
        self._send(200, {"ok": bool(ok),
                         "error": "" if ok else ((resp or {}).get("error") or "TTS 下发失败")})

    def api_admin_music_voice_test(self, body):
        if not self._admin_require():
            return
        account_id = str(body.get("account_id") or "").strip()
        device_id = str(body.get("device_id") or "").strip()
        query = str(body.get("text") or "").strip()[:120]
        if not account_id or not device_id or not query:
            self._send(400, {"ok": False, "error": "请选择音箱并输入测试口令"})
            return
        _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/voice-commands/test", {
            "account_id": account_id, "device_id": device_id, "query": query,
        }, timeout=50)
        ok = isinstance(resp, dict) and resp.get("success")
        self._send(200, {"ok": bool(ok), "data": (resp or {}).get("data"),
                         "error": "" if ok else ((resp or {}).get("error") or "口令执行失败")})

    def api_admin_music_chat_test(self, body):
        if not self._admin_require():
            return
        account_id = str(body.get("account_id") or "").strip()
        device_id = str(body.get("device_id") or "").strip()
        text = str(body.get("text") or "").strip()[:300]
        if not account_id or not device_id or not text:
            self._send(400, {"ok": False, "error": "请选择音箱并输入问题"})
            return
        code, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/chat/messages", {
            "account_id": account_id, "device_id": device_id,
            "text": text, "speak": True,
        }, timeout=25)
        if code not in (200, 202):
            self._send(200, {"ok": False,
                             "error": (resp or {}).get("detail")
                             or (resp or {}).get("error") or "AI 对话提交失败"})
            return
        task_id = (resp or {}).get("task_id") or ""
        status = {}
        for _ in range(18):
            time.sleep(1)
            q = urllib.parse.urlencode({"account_id": account_id, "device_id": device_id})
            _, st = _songloft_req("GET", MIOT_PLUGIN_API + "/chat/status?" + q, timeout=8)
            if isinstance(st, dict):
                status = st
                if st.get("phase") in ("completed", "failed", "cancelled"):
                    break
        self._send(200, {"ok": True, "task_id": task_id, "phase": status.get("phase") or "queued",
                         "answer": (status.get("answer") or "")[:600],
                         "error": status.get("error") or ""})

    def api_admin_music_ai_check(self):
        if not self._admin_require():
            return
        host = ""
        try:
            _, cfg = _songloft_req("GET", MIOT_PLUGIN_API + "/config", timeout=8)
            ai = ((cfg.get("data") or {}).get("ai_config") or {}) if isinstance(cfg, dict) else {}
            host = urllib.parse.urlsplit(str(ai.get("api_url") or "")).hostname or ""
        except Exception:
            pass
        code, resp = _songloft_req("GET", MIOT_PLUGIN_API + "/chat/models", timeout=30)
        if code == 200 and isinstance(resp, dict) and isinstance(resp.get("models"), list):
            ids = [m.get("id") for m in resp["models"] if isinstance(m, dict) and m.get("id")]
            self._send(200, {"ok": True, "host": host, "count": len(ids), "models": ids[:20]})
            return
        detail = ""
        if isinstance(resp, dict):
            detail = str(resp.get("detail") or resp.get("error") or "")
        if "404" in detail or "405" in detail:
            detail = "该服务商不提供 /models 列表接口，请保存后直接用“测试指令理解”验证（不支持列表不等于 Key 无效）"
        self._send(200, {"ok": False, "host": host,
                         "error": detail[:400] or "接口检测失败"})

    def api_admin_music_ai_test(self, body):
        if not self._admin_require():
            return
        text = str(body.get("text") or "").strip()[:120]
        if not text:
            self._send(400, {"ok": False, "error": "请输入测试口令"})
            return
        _, resp = _songloft_req("POST", MIOT_PLUGIN_API + "/voice-commands/ai-test",
                                {"query": text}, timeout=30)
        ok = isinstance(resp, dict) and resp.get("success")
        self._send(200, {"ok": bool(ok), "data": (resp or {}).get("data"),
                         "elapsed_ms": (resp or {}).get("elapsed_ms"),
                         "error": "" if ok else ((resp or {}).get("error") or "AI 解析失败")})

    def api_admin_refresh(self):
        if not self._admin_require():
            return
        TRY_NEXT.clear()
        threading.Thread(target=poll_once, daemon=True).start()
        WEATHER_WAKE.set()
        self._send(200, {"ok": True})


DISCOVERY_LOCK = threading.Lock()
DISCOVERY_HITS = {}


def discovery_loop():
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        sock.bind(("", DISCOVERY_PORT))
        sock.settimeout(1.0)
    except Exception as exc:
        LOG.warning("LAN discovery unavailable on udp/%d: %s", DISCOVERY_PORT,
                    str(exc)[:160])
        try:
            if sock:
                sock.close()
        except Exception:
            pass
        return
    LOG.info("LAN discovery listening on udp/%d", DISCOVERY_PORT)
    while True:
        try:
            data, addr = sock.recvfrom(1024)
        except socket.timeout:
            continue
        except Exception:
            continue
        text = data.decode("utf-8", "ignore").strip()
        if not text.startswith(("MIOT_DISCOVER", "MIOT_PANEL_DISCOVER")):
            continue
        ip = str(addr[0] or "")
        now = time.time()
        with DISCOVERY_LOCK:
            last = DISCOVERY_HITS.get(ip, 0)
            DISCOVERY_HITS[ip] = now
        if now - last < 1.0:
            continue
        payload = {
            "service": "miot-panel", "app": APP_NAME, "version": APP_VERSION,
            "port": PORT, "pin": bool(admin_pin_set()),
            "platform": "windows" if os.name == "nt" else "linux",
            "name": socket.gethostname()[:60] or "MIOT Panel",
        }
        try:
            sock.sendto(json.dumps(payload, ensure_ascii=False,
                                   separators=(",", ":")).encode("utf-8"), addr)
        except Exception:
            pass


def _reexec_for_update(_signum=None, _frame=None):
    LOG.info("re-exec for update")
    try:
        logging.shutdown()
    except Exception:
        pass
    script = os.path.join(APP_DIR, "miot-cloud.py")
    os.execv(sys.executable, [sys.executable, script])


def main():
    if "--restart-helper" in sys.argv:
        try:
            idx = sys.argv.index("--restart-helper")
            parent_pid = int(sys.argv[idx + 1])
            app_dir = sys.argv[idx + 2]
            port = int(sys.argv[idx + 3])
        except Exception:
            return
        time.sleep(1.2)
        try:
            subprocess.run(["taskkill", "/PID", str(parent_pid), "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
        except Exception:
            pass
        _wait_pid_gone(parent_pid, 15)
        _kill_port_owners(port)
        time.sleep(0.5)
        _spawn_app(app_dir, port)
        return
    if "--update-worker" in sys.argv:
        idx = sys.argv.index("--update-worker")
        _run_update_worker(sys.argv[idx + 1:])
        return
    _load_caches()
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, _reexec_for_update)
    try:
        _camera_normalize_go2rtc_url()
    except Exception as exc:
        LOG.warning("go2rtc config normalize failed: %s", str(exc)[:160])
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=weather_loop, daemon=True).start()
    threading.Thread(target=_startup_import, daemon=True).start()
    threading.Thread(target=discovery_loop, daemon=True).start()
    if os.name == "nt":
        _kill_port_owners(PORT)
        threading.Thread(target=_stack_autostart, daemon=True).start()
    threading.Thread(target=_camera_autostart, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info("miot-cloud listening on :%d", PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()

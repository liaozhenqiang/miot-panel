#!/usr/bin/env bash
# 安装 Songloft + go-music-dl + Songloft 插件（MIoT / GoMusicDL / panel-sso）。
# 默认安装为系统服务；--prefix DIR --no-systemd 用于临时/测试目录，不碰现有服务。
set -euo pipefail

SL_VERSION="2.13.5"
GMD_VERSION="1.1.3"
MIOT_PLUGIN_VERSION="2026.10.25.0"
GMD_PLUGIN_VERSION="2026.10.16"
PREFIX="/"
NO_SYSTEMD=0
SL_PORT="${SONGLOFT_PORT:-58091}"
GMD_PORT="${GO_MUSIC_DL_PORT:-8080}"
ARCH="$(uname -m)"

usage() {
  echo "用法: sudo bash install-music-stack.sh [--prefix DIR] [--no-systemd] [--port 58091] [--gmd-port 8080]"
  echo "  --prefix DIR   安装到 DIR 下（自动启用 --no-systemd），用于测试"
  echo "  --no-systemd   直接后台启动，不创建 systemd 服务"
  echo "环境变量: SONGLOFT_PORT / GO_MUSIC_DL_PORT"
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:-}"; [ -n "$PREFIX" ] || usage; NO_SYSTEMD=1; shift 2 ;;
    --no-systemd) NO_SYSTEMD=1; shift ;;
    --port) SL_PORT="${2:-}"; shift 2 ;;
    --gmd-port) GMD_PORT="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "未知参数: $1"; usage ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 或 sudo 运行"
  exit 1
fi

case "$ARCH" in
  armv7l|armv7) SL_ARCH="armv7"; GMD_ARCH="armv7" ;;
  aarch64|arm64) SL_ARCH="arm64"; GMD_ARCH="arm64" ;;
  x86_64|amd64) SL_ARCH="amd64"; GMD_ARCH="amd64" ;;
  *) echo "不支持的架构: $ARCH"; exit 1 ;;
esac

ROOT="${PREFIX%/}"
OPT="$ROOT/opt"
ETC="$ROOT/etc"
VAR="$ROOT/var/lib"
TMP="$(mktemp -d /tmp/miot-music.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
SL_DIR="$OPT/songloft"
GMD_DIR="$OPT/go-music-dl"
SL_DATA="$VAR/songloft"
GMD_DATA="$VAR/go-music-dl"
SL_ENV="$ETC/songloft/songloft.env"
GMD_ADMIN="$ETC/go-music-dl/admin.txt"
SERVICE_DIR="$ROOT/etc/systemd/system"
LOG_DIR="$ROOT/var/log/miot-music"
mkdir -p "$TMP/dl" "$SL_DIR" "$GMD_DIR" "$SL_DATA" "$GMD_DATA" "$(dirname "$SL_ENV")" "$(dirname "$GMD_ADMIN")" "$LOG_DIR"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少命令: $1"; exit 1; }; }
for c in curl tar python3; do need "$c"; done
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[提示] 未找到 ffmpeg；在线歌曲转码需要它。Debian/Ubuntu 可执行: apt install -y ffmpeg"
fi

download() {
  local url="$1" out="$2"
  echo "下载: $url"
  curl -fL --retry 2 --max-time 180 -o "$out" "$url"
}

echo "== 下载组件 ($SL_ARCH)"
download "https://github.com/songloft-org/songloft/releases/download/v${SL_VERSION}/songloft-linux-${SL_ARCH}" "$TMP/dl/songloft"
download "https://github.com/guohuiyuan/go-music-dl/releases/download/v${GMD_VERSION}/go-music-dl_linux_${GMD_ARCH}.tar.gz" "$TMP/dl/gmd.tar.gz"
download "https://github.com/songloft-org/songloft-plugin-miot/releases/download/v${MIOT_PLUGIN_VERSION}/miot.jsplugin.zip" "$TMP/dl/miot.zip"
download "https://github.com/songloft-org/songloft-plugin-go-music-dl/releases/download/v${GMD_PLUGIN_VERSION}/go-music-dl.jsplugin.zip" "$TMP/dl/gmd.jsplugin.zip"
PANEL_SSO="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/panel-sso.jsplugin.zip"
if [ -f "$PANEL_SSO" ]; then
  cp "$PANEL_SSO" "$TMP/dl/panel-sso.jsplugin.zip"
else
  download "https://raw.githubusercontent.com/liaozhenqiang/miot-panel/main/music/panel-sso.jsplugin.zip" "$TMP/dl/panel-sso.jsplugin.zip"
fi
tar -xzf "$TMP/dl/gmd.tar.gz" -C "$TMP/dl" music-dl
install -m 755 "$TMP/dl/songloft" "$SL_DIR/songloft"
install -m 755 "$TMP/dl/music-dl" "$GMD_DIR/music-dl"

mkdir -p "$SL_DATA/jsplugins"
install -m 600 "$TMP/dl/miot.zip" "$SL_DATA/jsplugins/miot.jsplugin.zip"
install -m 600 "$TMP/dl/gmd.jsplugin.zip" "$SL_DATA/jsplugins/go-music-dl.jsplugin.zip"
install -m 600 "$TMP/dl/panel-sso.jsplugin.zip" "$SL_DATA/jsplugins/panel-sso.jsplugin.zip"

# 凭据：已存在则复用，避免覆盖现有部署
if [ -f "$SL_ENV" ]; then
  SL_USER="$(sed -n 's/^ADMIN_USERNAME=//p' "$SL_ENV" | head -1)"
  SL_PASS="$(sed -n 's/^ADMIN_PASSWORD=//p' "$SL_ENV" | head -1)"
fi
[ -n "${SL_USER:-}" ] || SL_USER="admin"
[ -n "${SL_PASS:-}" ] || SL_PASS="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
umask 077
printf 'ADMIN_USERNAME=%s\nADMIN_PASSWORD=%s\nLISTEN_PORT=%s\nDB_PATH=%s\nMUSIC_DIR=%s\n' \
  "$SL_USER" "$SL_PASS" "$SL_PORT" "$SL_DATA/songloft.db" "$SL_DATA/music" >"$SL_ENV"

if [ -f "$GMD_ADMIN" ]; then
  GMD_USER="$(sed -n 's/^username=//p' "$GMD_ADMIN" | head -1)"
  GMD_PASS="$(sed -n 's/^password=//p' "$GMD_ADMIN" | head -1)"
fi
[ -n "${GMD_USER:-}" ] || GMD_USER="admin"
[ -n "${GMD_PASS:-}" ] || GMD_PASS="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
printf 'username=%s\npassword=%s\n' "$GMD_USER" "$GMD_PASS" >"$GMD_ADMIN"
mkdir -p "$SL_DATA/music"
umask 022

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$LAN_IP" ] || LAN_IP="127.0.0.1"

if [ "$NO_SYSTEMD" -eq 0 ]; then
  case "$ROOT" in
    "") : ;;
    *) echo "系统服务模式必须使用 /（不要配合 --prefix）"; exit 1 ;;
  esac
  id -u songloft >/dev/null 2>&1 || useradd --system --home "$SL_DATA" --shell /usr/sbin/nologin songloft
  id -u go-music-dl >/dev/null 2>&1 || useradd --system --home "$GMD_DATA" --shell /usr/sbin/nologin go-music-dl
  chown -R songloft:songloft "$SL_DATA"
  chown -R go-music-dl:go-music-dl "$GMD_DATA"
  chmod 700 "$SL_DATA" "$GMD_DATA"
  cat >"$SERVICE_DIR/songloft.service" <<EOF
[Unit]
Description=Songloft music server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=songloft
Group=songloft
EnvironmentFile=$SL_ENV
WorkingDirectory=$SL_DATA
ExecStart=$SL_DIR/songloft --port $SL_PORT --db $SL_DATA/songloft.db
Restart=always
RestartSec=3
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
  cat >"$SERVICE_DIR/go-music-dl.service" <<EOF
[Unit]
Description=Go Music DL web service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=go-music-dl
Group=go-music-dl
WorkingDirectory=$GMD_DATA
ExecStart=$GMD_DIR/music-dl web --no-browser --port $GMD_PORT
Restart=always
RestartSec=3
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$GMD_DATA

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable songloft go-music-dl >/dev/null
  systemctl restart songloft go-music-dl
  GMD_LOG_CMD="journalctl -u go-music-dl -o cat --since '-5 min'"
else
  # 测试/临时目录：直接后台启动
  pushd "$GMD_DATA" >/dev/null
  nohup "$GMD_DIR/music-dl" web --no-browser --port "$GMD_PORT" </dev/null >"$LOG_DIR/gmd.log" 2>&1 &
  GMD_PID=$!
  disown "$GMD_PID" 2>/dev/null || true
  echo "$GMD_PID" >"$LOG_DIR/gmd.pid"
  popd >/dev/null
  pushd "$SL_DATA" >/dev/null
  ADMIN_USERNAME="$SL_USER" ADMIN_PASSWORD="$SL_PASS" LISTEN_PORT="$SL_PORT" DB_PATH="$SL_DATA/songloft.db" MUSIC_DIR="$SL_DATA/music" nohup "$SL_DIR/songloft" </dev/null >"$LOG_DIR/sl.log" 2>&1 &
  SL_PID=$!
  disown "$SL_PID" 2>/dev/null || true
  echo "$SL_PID" >"$LOG_DIR/sl.pid"
  popd >/dev/null
  GMD_LOG_CMD="cat $LOG_DIR/gmd.log"
  chmod 600 "$SL_ENV" "$GMD_ADMIN"
fi

echo "== 等待 Songloft"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$SL_PORT/api/v1/version" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS --max-time 3 "http://127.0.0.1:$SL_PORT/api/v1/version" >/dev/null || { echo "Songloft 启动失败"; exit 1; }

echo "== 等待 go-music-dl"
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$GMD_PORT/music/" || true)"
  [ "$code" = "200" ] && break
  sleep 1
done

# go-music-dl 首次初始化
TOKEN="$(eval "$GMD_LOG_CMD" 2>/dev/null | sed -n 's/.*Web setup token: *//p' | tail -1 | tr -d '\r')"
if [ -n "$TOKEN" ]; then
  curl -s -o /dev/null -X POST "http://127.0.0.1:$GMD_PORT/music/setup" \
    --data-urlencode "setup_token=$TOKEN" --data-urlencode "username=$GMD_USER" \
    --data-urlencode "password=$GMD_PASS" --data-urlencode "password_confirm=$GMD_PASS" || true
fi
GMD_LOGIN="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$GMD_PORT/music/login" \
  --data-urlencode "username=$GMD_USER" --data-urlencode "password=$GMD_PASS" || true)"
case "$GMD_LOGIN" in
  200|302|303) ;;
  *) echo "go-music-dl 管理员登录失败（HTTP $GMD_LOGIN）；请检查 $GMD_ADMIN"; exit 1 ;;
esac

echo "== 配置 Songloft 插件"
python3 - "$SL_PORT" "$SL_USER" "$SL_PASS" "$GMD_PORT" "$GMD_USER" "$GMD_PASS" "$LAN_IP" <<'PY'
import json, sys, time, urllib.request, urllib.error
sl_port, sl_user, sl_pass, gmd_port, gmd_user, gmd_pass, lan_ip = sys.argv[1:8]
base = "http://127.0.0.1:%s" % sl_port
def req(method, path, payload=None, token=""):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=20) as resp:
        return resp.status, json.loads(resp.read() or b"{}")
token = ""
for _ in range(30):
    try:
        _, login = req("POST", "/api/v1/auth/login",
                       {"username": sl_user, "password": sl_pass})
        token = login.get("access_token") or ""
        if token:
            break
    except Exception:
        pass
    time.sleep(1)
if not token:
    raise SystemExit("Songloft 登录失败")
server_host = "http://%s:%s" % (lan_ip, sl_port)
for attempt in range(40):
    try:
        status, _ = req("POST", "/api/v1/jsplugin/go-music-dl/config", {
            "baseUrl": "http://127.0.0.1:%s" % gmd_port,
            "basePath": "/music",
            "serverHost": server_host,
            "gmdAuthUser": gmd_user,
            "gmdAuthPass": gmd_pass,
        }, token)
        if status < 400:
            break
    except Exception:
        pass
    time.sleep(1)
for attempt in range(40):
    try:
        status, _ = req("POST", "/api/v1/jsplugin/miot/config", {
            "server_host": server_host,
            "external_search_enabled": True,
            "external_search_no_import": True,
            "external_search_sources": [{
                "id": "go-music-dl", "name": "GoMusicDL",
                "url": "/api/v1/jsplugin/go-music-dl/api/search/topone",
                "token": "", "enabled": True,
            }],
            "external_search_timeout": 6,
            "search_priority": "parallel",
        }, token)
        if status < 400:
            break
    except Exception:
        pass
    time.sleep(1)
print("插件配置完成")
PY

echo ""
echo "音乐中心已就绪："
echo "  Songloft:    http://$LAN_IP:$SL_PORT"
echo "  go-music-dl: http://$LAN_IP:$GMD_PORT/music"
echo "  凭据文件:    $SL_ENV / $GMD_ADMIN（仅 root 可读，未在终端输出密码）"
if [ "$NO_SYSTEMD" -eq 1 ]; then
  echo "  临时模式 PID: Songloft $(cat "$LOG_DIR/sl.pid" 2>/dev/null || true) / go-music-dl $(cat "$LOG_DIR/gmd.pid" 2>/dev/null || true)"
  echo "  日志/停止: $LOG_DIR/sl.pid、$LOG_DIR/gmd.pid（kill 两个 PID 即可）"
else
  echo "  接下来重启 miot-cloud，并在 /admin → 音乐与 AI 音箱 扫码绑定小米账号。"
fi

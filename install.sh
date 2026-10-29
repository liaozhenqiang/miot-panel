#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 或 sudo 运行：sudo bash install.sh"
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${MIOT_ROOT:-/opt/miot-panel}"
PORT="${MIOT_PORT:-80}"
SERVICE="${MIOT_SERVICE_NAME:-miot-cloud}"

if ! command -v python3 >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    echo "== 安装 Python 3 / pip"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip
  else
    echo "缺少 python3，请先安装 Python 3.9+ / pip，或改用 Docker 安装包。"
    exit 1
  fi
fi
if ! python3 -m pip --version >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    echo "== 安装 python3-pip"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip
  else
    echo "缺少 pip，请先安装 python3-pip，或改用 Docker 安装包。"
    exit 1
  fi
fi
PYTHON_BIN="$(command -v python3)"

echo "== 安装目录: $DIR"
echo "== 监听端口: $PORT"

pip_install() {
  # Debian 12+/Ubuntu 23+ 需要 --break-system-packages；老系统不认这个参数，所以两层回退
  python3 -m pip install --break-system-packages -q "$@" \
    || python3 -m pip install -q "$@"
}

WHEELS_DIR="$SRC/wheels"
deps_ok() {
  python3 -c "import micloud, segno, Crypto, requests" >/dev/null 2>&1
}

if ! deps_ok; then
  if [ -d "$WHEELS_DIR" ] && ls "$WHEELS_DIR"/*.whl >/dev/null 2>&1; then
    echo "== 安装依赖（离线轮子，无需编译）: $(ls "$WHEELS_DIR"/*.whl | wc -l) 个"
    pip_install --no-index --find-links "$WHEELS_DIR" micloud==0.6 segno==1.6.6 \
      || echo "   离线轮子不适用于当前 Python/glibc，改用 PyPI"
  fi
  if ! deps_ok; then
    echo "== 离线轮子装上了但 import 失败（多为 glibc 版本不匹配），卸载后改用 PyPI 重装"
    python3 -m pip uninstall -y -q pycryptodome micloud >/dev/null 2>&1 || true
    echo "== 安装依赖（PyPI；armv7 这类没有预编译轮子的平台会现场编译 pycryptodome）"
    pip_install micloud==0.6 segno==1.6.6 && deps_ok
  fi
  if ! deps_ok; then
    echo "== 仍缺依赖，尝试安装编译工具后重试"
    if command -v apt-get >/dev/null 2>&1; then
      DEBIAN_FRONTEND=noninteractive apt-get install -y -q gcc python3-dev libffi-dev >/dev/null 2>&1 || true
    fi
    pip_install micloud==0.6 segno==1.6.6 && deps_ok
  fi
  if ! deps_ok; then
    echo "依赖安装失败。可手动执行：apt install -y python3-pip gcc python3-dev && pip3 install micloud segno"
    exit 1
  fi
fi

mkdir -p "$DIR"
install -m 755 "$SRC/miot-cloud.py" "$DIR/miot-cloud.py"
install -m 644 "$SRC/cloud_panel.html" "$DIR/cloud_panel.html"
install -m 644 "$SRC/admin.html" "$DIR/admin.html"
install -m 644 "$SRC/camera.html" "$DIR/camera.html"
install -m 644 "$SRC/manifest.webmanifest" "$DIR/manifest.webmanifest"
install -m 644 "$SRC/sw.js" "$DIR/sw.js"
mkdir -p "$DIR/icons"
install -m 644 "$SRC/icons/icon-192.png" "$DIR/icons/icon-192.png"
install -m 644 "$SRC/icons/icon-512.png" "$DIR/icons/icon-512.png"
if [ ! -f "$DIR/cloud_config.json" ]; then
  install -m 600 "$SRC/config.example.json" "$DIR/cloud_config.json"
  echo "== 已创建默认配置：$DIR/cloud_config.json"
else
  echo "== 保留已有配置：$DIR/cloud_config.json"
fi

if [ "${MIOT_SKIP_SYSTEMD:-0}" = "1" ]; then
  echo "== MIOT_SKIP_SYSTEMD=1，已跳过 systemd 服务创建"
  exit 0
fi

cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Mi Home Panel (miot-cloud)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=MIOT_ROOT=$DIR
Environment=MIOT_PORT=$PORT
WorkingDirectory=$DIR
ExecStart=${PYTHON_BIN} $DIR/miot-cloud.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
URL_PORT=""
[ "$PORT" != "80" ] && URL_PORT=":$PORT"
sleep 1
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "== 摄像头提示：未安装 ffmpeg，H.265 摄像头无法在浏览器播放"
  echo "   安装：apt install -y --no-install-recommends ffmpeg"
fi
echo
echo "== 服务状态：$(systemctl is-active "$SERVICE" || true) / 开机自启：$(systemctl is-enabled "$SERVICE" || true)"
echo "== 面板： http://${IP:-设备IP}${URL_PORT}/"
echo "== 后台： http://${IP:-设备IP}${URL_PORT}/admin  （首次进入设置管理员 PIN）"
echo "== 日志： journalctl -u ${SERVICE} -f"
echo
echo "如果 80 端口已被占用，请用：MIOT_PORT=8080 sudo -E bash install.sh"

#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 或 sudo 运行：sudo bash install.sh"
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${MIOT_ROOT:-/opt/miot-panel}"
PORT="${MIOT_PORT:-80}"
SERVICE="miot-cloud"

command -v python3 >/dev/null 2>&1 || { echo "缺少 python3，请先安装：apt install -y python3 python3-pip"; exit 1; }

echo "== 安装目录: $DIR"
echo "== 监听端口: $PORT"

if ! python3 -c "import micloud, segno" >/dev/null 2>&1; then
  echo "== 安装 Python 依赖 micloud / segno"
  python3 -m pip install --break-system-packages -q micloud segno \
    || python3 -m pip install -q micloud segno \
    || { echo "依赖安装失败，请先执行：apt install -y python3-pip && pip3 install micloud segno"; exit 1; }
fi

mkdir -p "$DIR"
install -m 755 "$SRC/miot-cloud.py" "$DIR/miot-cloud.py"
install -m 644 "$SRC/cloud_panel.html" "$DIR/cloud_panel.html"
install -m 644 "$SRC/admin.html" "$DIR/admin.html"
install -m 644 "$SRC/camera.html" "$DIR/camera.html"
if [ ! -f "$DIR/cloud_config.json" ]; then
  install -m 600 "$SRC/config.example.json" "$DIR/cloud_config.json"
  echo "== 已创建默认配置：$DIR/cloud_config.json"
else
  echo "== 保留已有配置：$DIR/cloud_config.json"
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
ExecStart=/usr/bin/python3 $DIR/miot-cloud.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
sleep 1
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "== 摄像头提示：未安装 ffmpeg，H.265 摄像头无法在浏览器播放"
  echo "   安装：apt install -y --no-install-recommends ffmpeg"
fi
echo
echo "== 服务状态：$(systemctl is-active "$SERVICE" || true)"
echo "== 面板： http://${IP:-设备IP}/"
echo "== 后台： http://${IP:-设备IP}/admin  （首次进入设置管理员 PIN）"
echo "== 日志： journalctl -u ${SERVICE} -f"
echo
echo "如果 80 端口已被占用，请用：MIOT_PORT=8080 sudo -E bash install.sh"

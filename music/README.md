# 音乐与 AI 音箱：便携部署参考

这是可选的音乐/音箱模块。基础面板不安装它也能正常使用。以下命令默认在解压包的 `music/` 目录执行；以 Debian 12 / ARMv7（玩客云）为例，其他架构替换下载资产即可。

实测版本：

| 组件 | 版本 | 端口 |
| --- | --- | --- |
| Songloft（full，不是 lite） | v2.13.5 | 58091 |
| Songloft MIoT 插件 | v2026.10.25.0 | - |
| go-music-dl | v1.1.3 | 8080 |
| go-music-dl Songloft 插件 | v2026.10.16 | - |
| panel-sso 免登录桥 | 1.0.0（本包自带） | - |

前提：systemd、python3、curl、ffmpeg。所有服务只放局域网，不要映射公网。

## 1. Songloft

```bash
sudo useradd --system --home /var/lib/songloft --shell /usr/sbin/nologin songloft 2>/dev/null || true
sudo mkdir -p /opt/songloft /etc/songloft /var/lib/songloft /srv/music
sudo curl -fL -o /opt/songloft/songloft \
  https://github.com/songloft-org/songloft/releases/download/v2.13.5/songloft-linux-armv7
sudo chmod 755 /opt/songloft/songloft
sudo chown -R songloft:songloft /var/lib/songloft /srv/music
sudo chmod 700 /var/lib/songloft /srv/music /etc/songloft

PW="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
sudo tee /etc/songloft/songloft.env >/dev/null <<EOF
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$PW
LISTEN_PORT=58091
DB_PATH=/var/lib/songloft/songloft.db
MUSIC_DIR=/srv/music
EOF
sudo chmod 600 /etc/songloft/songloft.env
printf 'username=admin\npassword=%s\n' "$PW" | sudo tee /etc/songloft/admin.txt >/dev/null
sudo chmod 600 /etc/songloft/admin.txt
```

`/etc/systemd/system/songloft.service`：

```ini
[Unit]
Description=Songloft music server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=songloft
Group=songloft
EnvironmentFile=/etc/songloft/songloft.env
WorkingDirectory=/var/lib/songloft
ExecStart=/opt/songloft/songloft
Restart=always
RestartSec=3
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

先安装插件再启动/重启：

```bash
sudo mkdir -p /var/lib/songloft/jsplugins
sudo curl -fL -o /var/lib/songloft/jsplugins/miot.jsplugin.zip \
  https://github.com/songloft-org/songloft-plugin-miot/releases/download/v2026.10.25.0/miot.jsplugin.zip
sudo curl -fL -o /var/lib/songloft/jsplugins/go-music-dl.jsplugin.zip \
  https://github.com/songloft-org/songloft-plugin-go-music-dl/releases/download/v2026.10.16/go-music-dl.jsplugin.zip
sudo install -o songloft -g songloft -m 600 panel-sso.jsplugin.zip \
  /var/lib/songloft/jsplugins/panel-sso.jsplugin.zip
sudo chown songloft:songloft /var/lib/songloft/jsplugins/*.zip
sudo chmod 600 /var/lib/songloft/jsplugins/*.zip
sudo systemctl daemon-reload
sudo systemctl enable --now songloft
curl -s http://127.0.0.1:58091/api/v1/version
```

`panel-sso` 是面板免登录桥。安装后可在 Songloft 插件管理里把它设为禁用：静态入口仍可用，且不会出现在插件宫格。

## 2. go-music-dl

```bash
sudo useradd --system --home /var/lib/go-music-dl --shell /usr/sbin/nologin go-music-dl 2>/dev/null || true
sudo mkdir -p /opt/go-music-dl /var/lib/go-music-dl
curl -fL -o /tmp/go-music-dl.tar.gz \
  https://github.com/guohuiyuan/go-music-dl/releases/download/v1.1.3/go-music-dl_linux_armv7.tar.gz
tar -xzf /tmp/go-music-dl.tar.gz -C /tmp music-dl
sudo install -m 755 /tmp/music-dl /opt/go-music-dl/music-dl
sudo chown -R go-music-dl:go-music-dl /var/lib/go-music-dl
sudo chmod 700 /var/lib/go-music-dl
```

`/etc/systemd/system/go-music-dl.service`：

```ini
[Unit]
Description=Go Music DL web service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=go-music-dl
Group=go-music-dl
WorkingDirectory=/var/lib/go-music-dl
ExecStart=/opt/go-music-dl/music-dl web --no-browser --port 8080
Restart=always
RestartSec=3
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/go-music-dl

[Install]
WantedBy=multi-user.target
```

首次启动后日志会打印 `Web setup token`。打开 `http://设备IP:8080/music/setup`，用该令牌创建 go-music-dl 管理员；令牌只用一次，创建后即失效。把管理员密码保存在本机 600 文件里，不要发到公开 Issue。

## 3. 面板配置

1. 重新安装/启动面板，打开 `/admin` → “音乐与 AI 音箱”。
2. 点“扫码绑定小米账号”，用米家 App 扫码。成功后会自动启用音箱、建立“全屋音箱”组，并把同一小米账号的 `xiaomiio` 票据合并成面板设备登录。
3. 在“音源与在线搜索”确认：
   - 服务器地址：`http://设备IP:58091`
   - 在线搜索开启、不入库直接播放开启
   - 音源先选 `qq/kuwo/migu/bilibili`，其他源按网络情况测试
4. 面板保存音源或扫码成功时会自动把 `设备IP` 加进 Songloft 的 `proxy-private-allowlist`。如果旧版没有自动加，请手工在 Songloft 设置/API 中加入该 IP，否则在线歌曲会报“转码代理请求被拒绝”。
5. 在“大模型 / 自定义人设 / TTS”选服务商、填自己的 Key、模型和人设，保存后点“检测接口 / Key”。Key 不会被面板回显。
6. 在“测试与常用控制”依次验证：TTS、语音口令“播放晴天”、AI 对话。测试使用已保存配置，改完要先保存。
7. 在“前场音箱助手”设置是否在首页显示卡片、文字/语音开关、默认音箱、按音箱显隐和“音箱播报/仅面板显示”。保存后，面板最长约 60 秒刷新。
   - 文字提问直接走 AI 对话；语音提问使用**音箱自己的麦克风**：点面板上的“语音提问”后对音箱说“小爱同学，请问……”。面板本身通常没有麦克风，因此不依赖浏览器录音权限或 HTTPS。
   - 语音提问要求“对话监听”和“AI 对话”都开启。提问/回答会同时出现在面板和音箱上。

## 4. 路径/端口不同时的环境变量

面板默认按以下路径读取 Songloft（可在 `miot-cloud.service` 里覆盖）：

```ini
Environment=MIOT_SONGLOFT=http://127.0.0.1:58091
Environment=MIOT_SONGLOFT_ENV=/etc/songloft/songloft.env
Environment=MIOT_PLUGIN_ACCOUNT_FILE=/var/lib/songloft/jsplugins_data/miot/data/accounts
Environment=MIOT_SONGLOFT_SSO_ZIP=/var/lib/songloft/jsplugins/panel-sso.jsplugin.zip
Environment=MIOT_GO_MUSIC_DL=http://127.0.0.1:8080
Environment=MIOT_GO_MUSIC_DL_SERVICE=go-music-dl
```

`MIOT_SONGLOFT_PORT` / `MIOT_GO_MUSIC_DL_PORT` 也会从 URL 自动解析。

## 5. 已知限制

- 小爱原生回答先由小米云端生成，插件轮询发现后再打断，通常有 1–2 秒延迟；短句仍可能听到原生回答。这是小米云 API 限制，不是插件配置。彻底消除需自定义固件或改用面板/手机麦克风提问。
- 在线歌曲常是 FLAC/M4A，音箱不能解码时由 Songloft + ffmpeg 实时转 MP3，会增加 CPU 占用；S805 建议限制并发。
- 长期偏好记忆（ChatMemory）在部分模型上可能提取失败，默认不影响对话；需要时再开启并选用严格 JSON 输出的模型。
- 在线歌曲走 `/proxy/transcode` 时，停止播放后音箱可能仍保持 HTTP 连接，ffmpeg 会保留到 `song 时长 + 5 分钟` 超时才退出；低配设备可在停止播放后重启 `songloft` 立即清理。这是当前 Songloft 的流式转码行为，不是面板泄漏。
- 第三方音源涉及平台版权与服务条款，使用者自行承担合规责任。
- Songloft/go-music-dl 的账号、插件 token、AI Key 都在本机 600/700 权限文件中；不要分享日志、备份或截图中的 token。

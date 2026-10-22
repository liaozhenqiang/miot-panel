# 米家云端面板（miot-cloud）

一个可移植的米家 / 小米智能家居触屏面板。登录小米账号后从云端枚举设备，直接云端控制与读取状态，不依赖设备的局域网 IP/Token。适合玩客云、树莓派、电视盒子、迷你主机等 Linux 设备，也可以直接在电脑/手机上作为网页使用。

## 功能

- 触屏墙面板：全屏、响应式、时钟、天气、日历、家庭用电、设备卡片
- 设备控制：灯/插座/开关电源，空调电源/温度/模式，窗帘等可扩展
- 计量：部分插座/总闸显示电压、电流、功率、累计电量（取决于设备 MIOT 能力）
- 排版：面板右上“排版”进入拖拽排序，按每页 N 张分页
- 白天/黑夜主题，手动切换并持久化
- 后台 `/admin`：设备显示/隐藏、改名、家庭过滤、天气城市、每页数量、管理员 PIN、日志、重置
- 登录态持久化：扫码登录一次，后续不重复登录
- 面板与后台均自适应，同一账号可在多个设备/浏览器打开

## 环境要求

- Linux（Debian/Ubuntu 优先，其他发行版也可），Python 3.9+
- 可访问公网（小米云、天气）
- 手机与设备在同一局域网（扫码登录用）
- 可选：触摸屏 + Chromium/Xorg 做 kiosk；没有也可以直接用浏览器

## 安装

从源码：

```bash
git clone https://github.com/liaozhenqiang/onecloud-miot-panel.git
cd onecloud-miot-panel
sudo bash install.sh
```

如果拿到的是发布包：

```bash
tar -xzf miot-panel-dist.tar.gz
cd miot-panel-dist
sudo bash install.sh
```

默认安装到 `/opt/miot-panel`，监听 80，服务名 `miot-cloud`。

自定义：

```bash
MIOT_ROOT=/etc/miot-panel MIOT_PORT=8080 sudo -E bash install.sh
```

安装完成后：

- 面板：`http://设备IP/`
- 后台：`http://设备IP/admin/`（首次进入设置管理员 PIN）
- 小米登录：面板未登录时会跳到 `/login`，手机扫二维码登录

如果 80 端口被占用，使用 `MIOT_PORT=8080` 重新安装。

## 第一次使用

1. 打开 `http://设备IP/admin/`，设置管理员 PIN。
2. 如果小米账号不是中国大陆区，先在后台“手机地区”填对应国家代码（如 `sg`/`us`），再登录。
3. 打开面板，按提示用手机扫码登录小米账号（可能需要短信/邮箱验证码）。
4. 登录后面板会自动拉取设备；回到后台可以：
   - 设置家庭过滤（公网 IP / 局域网 IP 前缀），留空表示显示所有家庭
   - 勾选/取消设备显示，自定义名称
   - 设置天气城市（wttr.in 城市名，如 `Beijing`；留空关闭天气）
   - 调整每页卡片数（4–12，触屏建议 10）
   - 恢复默认排版/主题、查看日志、退出小米账号

## 家庭过滤

后台设备表会显示每台设备的“局域网 IP / 公网 IP”：

- 公网 IP 前缀：例如同一住所的设备公网 IP 以 `203.0.113.` 开头
- 局域网 IP 前缀：例如 `192.168.0.` / `192.168.1.`

两者填任意匹配即可保留；两个都留空 = 不过滤。设备表会把不匹配的设备标为“其他家庭”。

## 天气与日历

- 天气来自 `wttr.in`，后台填英文城市名（如 `Beijing`），服务端缓存 30 分钟
- 日历由前端生成，无需外部服务
- 天气为空时面板显示“天气暂不可用”
- 室内温湿度：BLE 温湿度计通常无法通过小米云端 RPC 读取；需要 WiFi 温湿度传感器或 BLE 网关

## 面板操作

- 右上 `排版`：拖动卡片排序，每页分页线，保存后持久化
- 右上 `白天/黑夜`：切换主题，保存到设备
- 空调卡片：温度 ±、模式、电源
- 盲控设备：云端状态不可读的设备只提供“开/关”按钮，状态未知；慎用
- 电源总闸关闭会二次确认

## 隐私与运行数据

本仓库只包含程序代码、页面和示例配置，不包含任何人的小米账号、登录态、设备 token、摄像头 P2P 密钥、家庭 IP/城市、排版/主题或日志。

登录和使用过程中生成的运行文件只写入本机 `MIOT_ROOT`（安装脚本会设置为 600 权限），不会上传到 GitHub 或任何第三方：

- `micloud_auth.json`：小米登录态
- `cloud_config.json` / `cloud_layout.json` / `cloud_settings.json`
- `cloud_devices.json` / `cloud_states.json`
- `/opt/go2rtc/go2rtc.yaml`：摄像头 P2P 临时密钥和流地址

仓库的 `.gitignore` 已忽略这些文件。迁移、备份或求助时，不要分享上述文件或 `go2rtc.yaml`。

## 安全

- `/admin` 用 PIN 保护，首次进入请立刻设置
- 默认 HTTP 80，无 HTTPS；只建议在家庭局域网使用，不要直接暴露到公网
- 建议用防火墙限制来源，或通过 VPN 访问
- 小米登录态、配置均为本地文件（600）；不要分享 `micloud_auth.json`、`cloud_config.json`
- 不要把这个面板的端口直接映射到公网

## 摄像头（可选，go2rtc + ffmpeg）

面板右上角“摄像头”或 `/camera` 是独立的摄像头页：左侧是摄像头列表，右侧只嵌入 **go2rtc 的单路播放页**（WebRTC），不再进入 go2rtc 管理界面。顶部和右下角的“返回面板”始终在 iframe 之外，所以不会被全屏页面困住。

主页也会把当前家庭的每台摄像头做成独立卡片，默认排在 日历/天气/用电 之后，点卡片直接打开对应摄像头（`/camera?did=...`）。每页固定 10 张卡片；摄像头较多时（例如 5 路）会自然分页，原有设备卡片顺延；可以用“排版”拖动调整摄像头和设备的位置。主页的摄像头状态来自轻量接口，不会在主页启动转码，只有点开卡片后才开始拉流。

**工作原理**

面板复用已登录的小米账号，向云端换取每台摄像头的 P2P 临时密钥，再通过本机 go2rtc 的 API 注册两条流：

- `cam_<did>`：小米 P2P 原始流（通常是 H.265）
- `cam_<did>_h264`：ffmpeg 转码后的 H.264，浏览器实际播放这一条

因此**不需要在 go2rtc 里再登录一次小米账号**，也不会触发短信验证码风控。密钥由面板按需刷新（默认 10 分钟缓存），不写进页面。播放器由 go2rtc 自带，面板只负责列表、拉流和返回。

**依赖与安装**

1. go2rtc v1.9.14+，二进制放到 `/opt/go2rtc/go2rtc_linux_arm`（ARMv7），`chmod +x`。
2. `/opt/go2rtc/go2rtc.yaml` 只需要：

```yaml
api:
  listen: ":1984"
log:
  level: info
```

如果设备性能较弱，建议同时限制转码帧率（S805 实测从 20fps 降到 10fps 后 CPU/温度明显下降）：

```yaml
ffmpeg:
  h264: "-c:v libx264 -g 20 -profile:v high -level:v 4.1 -preset:v superfast -tune:v zerolatency -pix_fmt:v yuv420p -r 10"
```

3. 参考包内 `go2rtc.service` 创建服务并启动。
4. 安装 ffmpeg（小米摄像头几乎都是 H.265，Chromium 不支持，必须本机转 H.264）：

```sh
apt install -y --no-install-recommends ffmpeg
```

5. 重新运行本项目的 `install.sh`，然后重启 `miot-cloud`。安装 ffmpeg 后也重启一次 `go2rtc`，让它重新检测 ffmpeg。

**注意**

- 已知支持型号见 go2rtc issue #1982：`chuangmi.camera.039c01`、`chuangmi.camera.029a02`、`chuangmi.camera.046b02`、`isa.camera.hlc7` 等；`chuangmi.camera.069a01` 在部分固件上也可用，以实际连接为准。
- 转码默认 `640x360 / 600kbps`，帧率跟随摄像头（常见 15fps），页面一次只播放一路。S805 这类 ARM 小机器首次连接约 10–30 秒，转码时 CPU 占用明显；卡顿时可在 `miot-cloud.py` 的 `ensure_cameras()` 中把 `width/height` 改小，或在 go2rtc 配置里覆盖 `ffmpeg.h264` 预设降低帧率。
- 摄像头必须和面板在同一局域网；其他家庭的摄像头（内网 IP 不通）不会自动显示。
- go2rtc 的 1984 端口、RTSP 8554、WebRTC 8555 默认没有额外鉴权，仅限家庭局域网，不要映射到公网。摄像头页本身通过面板 80 端口访问，播放器需要能访问 go2rtc 的 1984/8555。

## Kiosk（可选）

任何能启动 Chromium 的 Linux 设备都可以做墙屏：

```sh
chromium --kiosk --incognito --noerrdialogs --disable-infobars \
  --touch-events=enabled --disable-gpu http://127.0.0.1/
```

可以配合 tty 自动登录 + `startx`，或在桌面环境设置开机自启。面板已做触摸适配：隐藏光标、按压反馈、禁止文本选择/右键菜单。

## 常见问题

- **登录要验证码**：小米风控；按提示在手机完成短信/邮箱验证。登录成功后 token 会保存。
- **设备按钮没反应**：该设备可能离线、云端状态不可读，或型号不支持对应属性；在后台设备表看在线状态，并用“盲控”页试。
- **温湿度没有**：BLE 传感器云端无法 RPC，换 WiFi 传感器或加网关。
- **天气空白**：`wttr.in` 可能被网络限制，或城市名不对；后台改城市或留空。
- **端口 80 起不来**：检查是否被占用；改用 `MIOT_PORT=8080`；80 需要 root（服务默认 root）。
- **后台打不开/登录不了**：确认 `/admin` 地址；清除浏览器 Cookie 后重试；忘记 PIN 可停服务后把 `cloud_config.json` 里的 `admin_pin_salt`/`admin_pin_hash` 清空并重启，重新设置。

## 文件

- `miot-cloud.py`：Python 服务（HTTP + 小米云 + 管理 API）
- `cloud_panel.html`：墙面板页面（设备 + 摄像头卡片）
- `admin.html`：后台页面
- `camera.html`：摄像头页（列表 + go2rtc 单路播放器）
- `config.example.json`：默认配置
- `requirements.txt`：Python 依赖（micloud、segno）
- `LICENSE`：MIT 许可证
- `install.sh`：安装脚本
- `miot-cloud.service` / `go2rtc.service`：systemd 参考单元

运行数据（安装后生成）：`cloud_config.json`、`cloud_layout.json`、`cloud_settings.json`、`cloud_devices.json`、`micloud_auth.json`。

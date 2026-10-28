# MIOT Panel Android 客户端

轻量局域网客户端：自动发现面板（UDP 37020），也支持手动输入 IP/端口；原生处理设备 PIN，然后用 WebView 打开面板。

- minSdk 14（Android 4.0），targetSdk 35（Android 15）
- 无 AndroidX 依赖，体积约 40KB
- 面板 `/compat.js` 为旧 WebView 补 Promise/fetch/Array.from 等 API
- Android 4.0 的 WebView 不支持现代 CSS/MSE，建议用面板的普通设备控制；摄像头播放建议 Android 5+/新版 WebView
- 首次设置（PIN、音乐中心、小米登录）仍建议在电脑浏览器完成

## 构建

需要 JDK 17、Android SDK Platform 35 和 Build-Tools 35.0.0。当前 Release 的 APK 使用 debug 证书签名，供局域网侧载安装。

```sh
SDK=$ANDROID_SDK_ROOT
BT=$SDK/build-tools/35.0.0
AJ=$SDK/platforms/android-35/android.jar
mkdir -p build/gen build/classes build/dex build/res
$BT/aapt2 compile --dir res -o build/res/resources.zip
$BT/aapt2 link -o build/app-unsigned.apk -I "$AJ" \
  --manifest AndroidManifest.xml -R build/res/resources.zip \
  --auto-add-overlay --java build/gen \
  --min-sdk-version 14 --target-sdk-version 35 \
  --version-code 1 --version-name 1.0.0
javac -encoding UTF-8 -source 8 -target 8 -bootclasspath "$AJ" \
  -d build/classes build/gen/com/miotpanel/client/R.java \
  src/com/miotpanel/client/*.java
$BT/d8 --min-api 14 --lib "$AJ" --output build/dex build/classes/com/miotpanel/client/*.class
# 将 build/dex/classes.dex 加入 APK，再执行 zipalign + apksigner
```

## 通信

- `GET /api/status` 探测面板；401 表示需要 PIN
- `POST /api/device_unlock` 原生输入 PIN，保存 HttpOnly 设备 Cookie
- UDP 37020 广播 `MIOT_DISCOVER 1`，面板返回 `service=miot-panel`、版本和端口
- 面板仍可用“扫描本网段”兜底（80/8080）

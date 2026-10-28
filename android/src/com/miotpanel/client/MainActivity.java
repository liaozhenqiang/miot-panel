package com.miotpanel.client;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.InputMethodManager;
import android.webkit.CookieManager;
import android.webkit.CookieSyncManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.net.URL;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class MainActivity extends Activity {

    private static final int COLOR_BG = 0xff0b1220;
    private static final int COLOR_CARD = 0xff162235;
    private static final int COLOR_TEXT = 0xffe8eef7;
    private static final int COLOR_MUTED = 0xff9fb0c7;
    private static final int COLOR_ACCENT = 0xffff6b2c;
    private static final int COLOR_BORDER = 0xff2a3a52;

    private SharedPreferences prefs;
    private TextView statusView;
    private LinearLayout resultsBox;
    private final Map<String, PanelDiscovery.Panel> results =
            new LinkedHashMap<String, PanelDiscovery.Panel>();
    private boolean discovering;
    private boolean scanning;

    private WebView webView;
    private String currentHost = "";
    private int currentPort = 80;
    private String currentCookie = "";
    private boolean unlockPromptShown;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if ((getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED);
            try {
                ((android.app.KeyguardManager) getSystemService(KEYGUARD_SERVICE))
                        .newKeyguardLock("diag").disableKeyguard();
            } catch (Throwable ignored) {
            }
        }
        enterImmersive();
        prefs = getSharedPreferences("miot_panel", MODE_PRIVATE);
        showConnectScreen(false);
        String saved = prefs.getString("server", "");
        if (saved.length() > 0) {
            connectSaved(saved);
        } else {
            startDiscover();
        }
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            enterImmersive();
        }
    }

    /** 全屏墙屏运行：隐藏状态栏与导航栏，下滑可临时唤出后自动收起。 */
    private void enterImmersive() {
        android.view.Window window = getWindow();
        if (Build.VERSION.SDK_INT >= 28) {
            android.view.WindowManager.LayoutParams attrs = window.getAttributes();
            attrs.layoutInDisplayCutoutMode =
                    android.view.WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            window.setAttributes(attrs);
        }
        if (Build.VERSION.SDK_INT >= 30) {
            window.setDecorFitsSystemWindows(false);
            // getInsetsController() 在 setContentView 之前会 NPE（decor 还没建好）
            android.view.WindowInsetsController controller =
                    window.getDecorView().getWindowInsetsController();
            if (controller != null) {
                controller.hide(android.view.WindowInsets.Type.systemBars());
                controller.setSystemBarsBehavior(
                        android.view.WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
            return;
        }
        android.view.View decor = window.getDecorView();
        if (Build.VERSION.SDK_INT >= 19) {
            decor.setSystemUiVisibility(android.view.View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                    | android.view.View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                    | android.view.View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    | android.view.View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    | android.view.View.SYSTEM_UI_FLAG_FULLSCREEN
                    | android.view.View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
        } else {
            decor.setSystemUiVisibility(android.view.View.SYSTEM_UI_FLAG_FULLSCREEN
                    | android.view.View.SYSTEM_UI_FLAG_LOW_PROFILE);
        }
    }

    private void showConnectScreen(boolean autoDiscover) {
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
            webView = null;
        }
        currentCookie = "";
        unlockPromptShown = false;
        results.clear();
        discovering = false;
        scanning = false;

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(COLOR_BG);
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(22), dp(28), dp(22), dp(30));
        scroll.addView(box, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView title = text("MIOT Panel", 30, COLOR_TEXT, true);
        box.addView(title);
        TextView subtitle = text("局域网面板连接器", 14, COLOR_MUTED, false);
        LinearLayout.LayoutParams subLp = wrapLp();
        subLp.topMargin = dp(4);
        box.addView(subtitle, subLp);

        statusView = text("准备搜索局域网面板…", 14, COLOR_MUTED, false);
        LinearLayout.LayoutParams statusLp = wrapLp();
        statusLp.topMargin = dp(18);
        box.addView(statusView, statusLp);

        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        actions.setGravity(Gravity.CENTER_VERTICAL);
        LinearLayout.LayoutParams actionsLp = wrapLp();
        actionsLp.topMargin = dp(16);
        box.addView(actions, actionsLp);
        actions.addView(button("自动发现", true, new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startDiscover();
            }
        }));
        actions.addView(button("扫描本网段", false, new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startScan();
            }
        }));
        actions.addView(button("手动连接", false, new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showManualDialog();
            }
        }));

        TextView foundTitle = text("发现的面板", 13, COLOR_MUTED, true);
        LinearLayout.LayoutParams foundLp = wrapLp();
        foundLp.topMargin = dp(24);
        box.addView(foundTitle, foundLp);

        resultsBox = new LinearLayout(this);
        resultsBox.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams resultsLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        resultsLp.topMargin = dp(8);
        box.addView(resultsBox, resultsLp);

        TextView hint = text("面板需与手机在同一局域网。首次安装请先在电脑浏览器完成 PIN、音乐中心和小米登录设置。",
                13, COLOR_MUTED, false);
        LinearLayout.LayoutParams hintLp = wrapLp();
        hintLp.topMargin = dp(22);
        box.addView(hint, hintLp);
        TextView footer = text("Android 4.0+ · 直连局域网 · 不经过第三方服务器 · v" + versionName(),
                12, 0xff6f8098, false);
        LinearLayout.LayoutParams footerLp = wrapLp();
        footerLp.topMargin = dp(10);
        box.addView(footer, footerLp);

        root.addView(scroll);
        setContentView(root);
        if (autoDiscover) {
            startDiscover();
        }
    }

    private void startDiscover() {
        if (discovering) {
            return;
        }
        discovering = true;
        statusView.setText("正在广播搜索局域网面板…");
        results.clear();
        resultsBox.removeAllViews();
        PanelDiscovery.discover(new PanelDiscovery.Listener() {
            @Override
            public void onFound(final PanelDiscovery.Panel panel) {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        addResult(panel);
                    }
                });
            }

            @Override
            public void onFinished() {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        discovering = false;
                        if (results.isEmpty()) {
                            statusView.setText("没有自动发现面板，请点“扫描本网段”或“手动连接”。");
                        } else {
                            statusView.setText("发现 " + results.size() + " 个面板，点击连接。");
                        }
                    }
                });
            }
        });
    }

    private void startScan() {
        if (scanning) {
            return;
        }
        scanning = true;
        statusView.setText("正在扫描本网段 80/8080 端口，请稍候…");
        PanelDiscovery.scanSubnet(new PanelDiscovery.Listener() {
            @Override
            public void onFound(final PanelDiscovery.Panel panel) {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        addResult(panel);
                    }
                });
            }

            @Override
            public void onFinished() {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        scanning = false;
                        statusView.setText(results.isEmpty()
                                ? "扫描完成，没有找到面板。可手动输入电脑/玩客云的 IP。"
                                : "扫描完成，共发现 " + results.size() + " 个面板。");
                    }
                });
            }
        });
    }

    private void addResult(final PanelDiscovery.Panel panel) {
        if (panel == null || resultsBox == null || results.containsKey(panel.key())) {
            return;
        }
        results.put(panel.key(), panel);
        Button item = new Button(this);
        item.setText(panel.label());
        item.setTextSize(14);
        item.setTextColor(COLOR_TEXT);
        item.setAllCaps(false);
        item.setGravity(Gravity.LEFT | Gravity.CENTER_VERTICAL);
        item.setPadding(dp(14), dp(10), dp(14), dp(10));
        item.setBackgroundDrawable(rounded(COLOR_CARD, COLOR_BORDER, 12));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = dp(8);
        item.setLayoutParams(lp);
        item.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                connectTo(panel.host, panel.port);
            }
        });
        resultsBox.addView(item);
    }

    private void showManualDialog() {
        final EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setHint("例如 192.168.1.10 或 192.168.1.20:8080");
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        String saved = prefs.getString("server", "");
        if (saved.length() > 0) {
            input.setText(saved.replace("http://", "").replace("https://", ""));
            input.setSelection(input.getText().length());
        }
        FrameLayout holder = new FrameLayout(this);
        holder.setPadding(dp(20), dp(8), dp(20), dp(4));
        holder.addView(input, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        new AlertDialog.Builder(this)
                .setTitle("手动连接")
                .setView(holder)
                .setPositiveButton("连接", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface dialog, int which) {
                        connectManual(input.getText().toString());
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void connectManual(final String raw) {
        final String value = raw == null ? "" : raw.trim();
        if (value.length() == 0) {
            Toast.makeText(this, "请输入 IP 地址", Toast.LENGTH_SHORT).show();
            return;
        }
        statusView.setText("正在检测 " + value + "…");
        new Thread(new Runnable() {
            @Override
            public void run() {
                String host = "";
                int port = 0;
                try {
                    String target = value.indexOf("://") >= 0 ? value : "http://" + value;
                    URL url = new URL(target);
                    host = url.getHost();
                    port = url.getPort();
                } catch (Exception ignored) {
                }
                if (host == null || host.length() == 0) {
                    host = value;
                    int colon = value.lastIndexOf(':');
                    if (colon > 0 && value.indexOf(':') == colon) {
                        host = value.substring(0, colon);
                        try {
                            port = Integer.parseInt(value.substring(colon + 1));
                        } catch (Exception ignored) {
                        }
                    }
                }
                final List<Integer> ports = new ArrayList<Integer>();
                if (port > 0) {
                    ports.add(Integer.valueOf(port));
                } else {
                    ports.add(Integer.valueOf(80));
                    ports.add(Integer.valueOf(8080));
                }
                final String finalHost = host;
                PanelClient.Probe found = null;
                for (int i = 0; i < ports.size(); i++) {
                    int p = ports.get(i).intValue();
                    PanelClient.Probe probe = PanelClient.probe(finalHost, p,
                            prefs.getString("cookie_" + finalHost + ":" + p, ""));
                    if (probe.found) {
                        found = probe;
                        break;
                    }
                }
                if (found == null) {
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            statusView.setText("连接失败：请检查 IP、端口和面板是否启动。");
                        }
                    });
                    return;
                }
                final PanelClient.Probe probe = found;
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        handleProbe(finalHost, portOf(probe.base), probe);
                    }
                });
            }
        }, "miot-manual").start();
    }

    private int portOf(String base) {
        try {
            URL url = new URL(base);
            return url.getPort() > 0 ? url.getPort() : 80;
        } catch (Exception ignored) {
            return 80;
        }
    }

    private void connectSaved(String saved) {
        try {
            URL url = new URL(saved.indexOf("://") >= 0 ? saved : "http://" + saved);
            final String host = url.getHost();
            final int port = url.getPort() > 0 ? url.getPort() : 80;
            connectTo(host, port);
        } catch (Exception ignored) {
            startDiscover();
        }
    }

    private void connectTo(final String host, final int port) {
        statusView.setText("正在连接 " + host + ":" + port + "…");
        new Thread(new Runnable() {
            @Override
            public void run() {
                final PanelClient.Probe probe = PanelClient.probe(host, port,
                        prefs.getString("cookie_" + host + ":" + port, ""));
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        handleProbe(host, port, probe);
                    }
                });
            }
        }, "miot-connect").start();
    }

    private void handleProbe(final String host, final int port, final PanelClient.Probe probe) {
        if (probe.ok) {
            saveSession(host, port, prefs.getString("cookie_" + host + ":" + port, ""));
            showWeb(host, port, prefs.getString("cookie_" + host + ":" + port, ""), "/");
            return;
        }
        if (probe.needsPin) {
            askPin(host, port, probe.message, new Runnable() {
                @Override
                public void run() {
                    // PIN entry is handled by askPin itself.
                }
            });
            return;
        }
        if (probe.setup) {
            new AlertDialog.Builder(this)
                    .setTitle("面板尚未完成首次设置")
                    .setMessage("请先在电脑浏览器打开 http://" + host + ":" + port +
                            "/setup，完成 PIN、音乐中心和小米登录后再用手机连接。")
                    .setPositiveButton("知道了", null)
                    .show();
            return;
        }
        statusView.setText("连接失败（" + probe.message + "），请重试或自动发现。");
    }

    private void askPin(final String host, final int port, String message, final Runnable after) {
        final EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setHint("面板 PIN");
        input.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_VARIATION_PASSWORD);
        FrameLayout holder = new FrameLayout(this);
        holder.setPadding(dp(20), dp(8), dp(20), dp(4));
        holder.addView(input, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        String text = message != null && message.length() > 0 && !"需要 PIN".equals(message)
                ? message : "请输入面板 PIN（与后台 PIN 相同）";
        final AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle("解锁面板")
                .setMessage(text)
                .setView(holder)
                .setPositiveButton("连接", null)
                .setNegativeButton("取消", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        unlockPromptShown = false;
                    }
                })
                .create();
        dialog.setOnShowListener(new DialogInterface.OnShowListener() {
            @Override
            public void onShow(DialogInterface d) {
                input.requestFocus();
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(
                        new View.OnClickListener() {
                            @Override
                            public void onClick(View v) {
                                final String pin = input.getText().toString().trim();
                                if (pin.length() < 4) {
                                    input.setError("PIN 至少 4 位");
                                    return;
                                }
                                dialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(false);
                                new Thread(new Runnable() {
                                    @Override
                                    public void run() {
                                        final PanelClient.UnlockResult result =
                                                PanelClient.unlock(host, port, pin);
                                        runOnUiThread(new Runnable() {
                                            @Override
                                            public void run() {
                                                if (result.ok) {
                                                    saveSession(host, port, result.cookie);
                                                    dialog.dismiss();
                                                    unlockPromptShown = false;
                                                    if (webView != null) {
                                                        currentCookie = result.cookie;
                                                        loadPanel();
                                                    } else {
                                                        showWeb(host, port, result.cookie, "/");
                                                    }
                                                } else {
                                                    dialog.getButton(
                                                            AlertDialog.BUTTON_POSITIVE).setEnabled(true);
                                                    input.setError(result.message.length() > 0
                                                            ? result.message : "PIN 不正确");
                                                }
                                            }
                                        });
                                    }
                                }, "miot-unlock").start();
                            }
                        });
            }
        });
        dialog.show();
    }

    private void saveSession(String host, int port, String cookie) {
        currentHost = host;
        currentPort = port;
        currentCookie = cookie == null ? "" : cookie;
        prefs.edit()
                .putString("server", "http://" + host + ":" + port)
                .putString("cookie_" + host + ":" + port, currentCookie)
                .apply();
    }

    private void showWeb(final String host, int port, String cookie, String path) {
        currentHost = host;
        currentPort = port;
        currentCookie = cookie == null ? "" : cookie;
        final String base = "http://" + host + ":" + port;

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(COLOR_BG);

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(10), dp(4), dp(8), dp(4));
        bar.setBackgroundColor(0xff121d2d);
        TextView title = text(host + ":" + port, 13, COLOR_TEXT, true);
        title.setSingleLine(true);
        bar.addView(title, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1.0f));
        bar.addView(button("更新", false, new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                checkUpdate();
            }
        }));
        bar.addView(button("刷新", false, new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                loadPanel();
            }
        }));
        bar.addView(button("切换", false, new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showConnectScreen(true);
            }
        }));
        root.addView(bar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        webView = new WebView(this);
        webView.setBackgroundColor(Color.BLACK);
        if (Build.VERSION.SDK_INT >= 19
                && (getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            WebView.setWebContentsDebuggingEnabled(true);
        }
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(false);
        settings.setSupportZoom(true);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setJavaScriptCanOpenWindowsAutomatically(true);
        webView.setScrollBarStyle(View.SCROLLBARS_INSIDE_OVERLAY);
        webView.setOverScrollMode(View.OVER_SCROLL_NEVER);
        if (Build.VERSION.SDK_INT >= 16) {
            settings.setAllowFileAccess(false);
        }
        if (Build.VERSION.SDK_INT >= 21) {
            settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        }
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                if (url != null && url.indexOf("/unlock") >= 0) {
                    handleUnlockUrl();
                    return true;
                }
                return false;
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                if (url != null && url.indexOf("/unlock") >= 0) {
                    view.stopLoading();
                    handleUnlockUrl();
                }
            }

            @Override
            public void onReceivedError(WebView view, int errorCode,
                                        String description, String failingUrl) {
                if (statusView != null) {
                    statusView.setText("页面加载失败：" + description);
                }
            }
        });
        root.addView(webView, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1.0f));
        setContentView(root);
        enterImmersive();
        setCookies(base, currentCookie);
        webView.loadUrl(base + (path == null || path.length() == 0 ? "/" : path));
    }

    private void handleUnlockUrl() {
        if (unlockPromptShown) {
            return;
        }
        unlockPromptShown = true;
        if (webView != null) {
            webView.loadUrl("about:blank");
        }
        askPin(currentHost, currentPort, "面板 PIN 已失效，请重新输入", null);
    }

    private void loadPanel() {
        if (webView == null) {
            return;
        }
        String base = "http://" + currentHost + ":" + currentPort;
        setCookies(base, currentCookie);
        webView.loadUrl(base + "/");
    }

    private String versionName() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "";
        }
    }

    private int versionCode() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionCode;
        } catch (Exception e) {
            return 0;
        }
    }

    private void checkUpdate() {
        final int mine = versionCode();
        final String mineName = versionName();
        final String base = "http://" + currentHost + ":" + currentPort;
        Toast.makeText(this, "正在检查更新…", Toast.LENGTH_SHORT).show();
        new Thread(new Runnable() {
            @Override
            public void run() {
                final JSONObject info;
                String problem = null;
                String body = PanelClient.fetch(base + "/api/android", currentCookie, 4000);
                JSONObject parsed = null;
                try {
                    parsed = new JSONObject(body);
                } catch (Exception e) {
                    problem = "面板没有返回版本信息。旧版面板（0.7.6 及更早）不支持检查更新，"
                            + "升级面板后即可使用。";
                }
                info = parsed;
                final String why = problem;
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        if (info == null) {
                            showUpdateMessage("检查更新失败", why);
                            return;
                        }
                        int code = info.optInt("version_code", 0);
                        String name = info.optString("version_name", "");
                        String url = info.optString("url", "");
                        if (url.length() == 0) {
                            url = info.optString("release_url", "");
                        }
                        if (code <= mine) {
                            Toast.makeText(MainActivity.this, "已是最新版本 v" + mineName,
                                    Toast.LENGTH_LONG).show();
                            return;
                        }
                        showUpdateDialog(mineName, name, url);
                    }
                });
            }
        }).start();
    }

    private void showUpdateMessage(String title, String message) {
        new AlertDialog.Builder(this)
                .setTitle(title)
                .setMessage(message == null ? "未知错误" : message)
                .setPositiveButton("知道了", null)
                .show();
    }

    private void showUpdateDialog(String mineName, String name, final String url) {
        String message = "当前版本 v" + mineName + "，面板上提供 v" + name + "。\n\n"
                + (url.length() > 0 ? url : "面板里没有放安装包，请到发布页下载")
                + "\n\n用浏览器下载后按提示覆盖安装即可，配置不会丢。";
        new AlertDialog.Builder(this)
                .setTitle("发现新版本 v" + name)
                .setMessage(message)
                .setPositiveButton("浏览器打开", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface dialog, int which) {
                        openUrl(url);
                    }
                })
                .setNeutralButton("复制地址", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface dialog, int which) {
                        copyText(url);
                    }
                })
                .setNegativeButton("稍后", null)
                .show();
    }

    private void openUrl(String url) {
        if (url == null || url.length() == 0) {
            Toast.makeText(this, "没有可用的下载地址", Toast.LENGTH_SHORT).show();
            return;
        }
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        } catch (Exception e) {
            Toast.makeText(this, "没有可用浏览器：" + e, Toast.LENGTH_LONG).show();
        }
    }

    private void copyText(String text) {
        if (text == null || text.length() == 0) {
            return;
        }
        try {
            ClipboardManager manager = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            manager.setPrimaryClip(ClipData.newPlainText("MIOT Panel", text));
            Toast.makeText(this, "下载地址已复制", Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            Toast.makeText(this, "复制失败：" + e, Toast.LENGTH_SHORT).show();
        }
    }

    private void setCookies(String base, String cookie) {
        try {
            if (Build.VERSION.SDK_INT < 21) {
                CookieSyncManager.createInstance(getApplicationContext());
            }
            CookieManager manager = CookieManager.getInstance();
            manager.setAcceptCookie(true);
            if (cookie != null && cookie.length() > 0) {
                String[] parts = cookie.split("; ");
                for (int i = 0; i < parts.length; i++) {
                    if (parts[i].indexOf('=') > 0) {
                        manager.setCookie(base, parts[i]);
                    }
                }
            }
            if (Build.VERSION.SDK_INT >= 21) {
                manager.flush();
            } else {
                CookieSyncManager.getInstance().sync();
            }
        } catch (Exception ignored) {
        }
    }

    @Override
    public void onBackPressed() {
        if (webView != null) {
            if (webView.canGoBack()) {
                webView.goBack();
            } else {
                showConnectScreen(true);
            }
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onPause() {
        if (webView != null) {
            webView.onPause();
        }
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) {
            webView.onResume();
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }

    private TextView text(String value, int sizeSp, int color, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, sizeSp);
        view.setTextColor(color);
        if (bold) {
            view.setTypeface(view.getTypeface(), android.graphics.Typeface.BOLD);
        }
        return view;
    }

    private Button button(String value, boolean primary, View.OnClickListener listener) {
        Button view = new Button(this);
        view.setText(value);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        view.setTextColor(0xffffffff);
        view.setAllCaps(false);
        view.setMinWidth(0);
        view.setMinimumWidth(0);
        view.setPadding(dp(12), dp(7), dp(12), dp(7));
        view.setBackgroundDrawable(rounded(primary ? COLOR_ACCENT : COLOR_CARD,
                primary ? COLOR_ACCENT : COLOR_BORDER, 10));
        view.setOnClickListener(listener);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.rightMargin = dp(8);
        view.setLayoutParams(lp);
        return view;
    }

    private GradientDrawable rounded(int fill, int stroke, int radiusDp) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(fill);
        drawable.setCornerRadius(dp(radiusDp));
        drawable.setStroke(dp(1), stroke);
        return drawable;
    }

    private LinearLayout.LayoutParams wrapLp() {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private int dp(int value) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value,
                getResources().getDisplayMetrics());
    }
}

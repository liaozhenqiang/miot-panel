package com.miotpanel.client;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.List;
import java.util.Map;

public final class PanelClient {

    public static class Probe {
        public boolean found;
        public boolean ok;
        public boolean needsPin;
        public boolean setup;
        public String base = "";
        public String version = "";
        public String message = "";
    }

    public static class UnlockResult {
        public boolean ok;
        public String cookie = "";
        public String message = "";
    }

    private PanelClient() {
    }

    public static Probe probe(String host, int port, String cookie) {
        Probe result = new Probe();
        result.base = "http://" + host + ":" + port;
        HttpURLConnection conn = null;
        try {
            URL url = new URL(result.base + "/api/status");
            conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(2500);
            conn.setReadTimeout(2500);
            conn.setInstanceFollowRedirects(false);
            conn.setRequestMethod("GET");
            if (cookie != null && cookie.length() > 0) {
                conn.setRequestProperty("Cookie", cookie);
            }
            int code = conn.getResponseCode();
            String body = read(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
            if (code == 200 && body.indexOf("\"version\"") >= 0) {
                result.found = true;
                try {
                    JSONObject json = new JSONObject(body);
                    result.version = json.optString("version", "");
                    if (!json.optBoolean("logged_in", true)) {
                        result.setup = true;
                        result.message = "面板尚未登录小米账号";
                        return result;
                    }
                } catch (Exception ignored) {
                }
                result.ok = true;
                return result;
            }
            if (code == 401) {
                result.found = true;
                result.needsPin = true;
                result.message = messageOf(body);
                return result;
            }
            if (code == 301 || code == 302 || code == 303 || code == 307 || code == 308) {
                String location = conn.getHeaderField("Location");
                if (location != null) {
                    if (location.indexOf("/setup") >= 0) {
                        result.found = true;
                        result.setup = true;
                        result.message = "面板尚未完成首次设置";
                        return result;
                    }
                    if (location.indexOf("/unlock") >= 0) {
                        result.found = true;
                        result.needsPin = true;
                        result.message = "需要 PIN";
                        return result;
                    }
                }
            }
            if (code > 0 && body.indexOf("miot") >= 0) {
                result.found = true;
                result.message = "HTTP " + code;
                return result;
            }
            result.message = "HTTP " + code;
        } catch (Exception exc) {
            result.message = exc.getClass().getSimpleName();
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
        return result;
    }

    public static UnlockResult unlock(String host, int port, String pin) {
        UnlockResult result = new UnlockResult();
        HttpURLConnection conn = null;
        try {
            URL url = new URL("http://" + host + ":" + port + "/api/device_unlock");
            conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(3000);
            conn.setReadTimeout(3000);
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json");
            JSONObject body = new JSONObject();
            body.put("pin", pin);
            byte[] data = body.toString().getBytes("UTF-8");
            conn.setFixedLengthStreamingMode(data.length);
            OutputStream out = conn.getOutputStream();
            out.write(data);
            out.close();
            int code = conn.getResponseCode();
            String text = read(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
            if (code == 200 && text.indexOf("\"ok\"") >= 0 && text.indexOf("false") < 0) {
                result.ok = true;
                result.cookie = extractCookies(conn);
                if (result.cookie.length() == 0) {
                    result.ok = false;
                    result.message = "面板未返回登录 Cookie";
                    return result;
                }
                return result;
            }
            result.message = messageOf(text);
            if (result.message.length() == 0) {
                result.message = "PIN 验证失败（HTTP " + code + "）";
            }
        } catch (Exception exc) {
            result.message = exc.getClass().getSimpleName();
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
        return result;
    }

    private static String messageOf(String body) {
        if (body == null || body.length() == 0) {
            return "";
        }
        try {
            JSONObject json = new JSONObject(body);
            return json.optString("msg", json.optString("error", ""));
        } catch (Exception ignored) {
            return "";
        }
    }

    private static String extractCookies(HttpURLConnection conn) {
        StringBuilder out = new StringBuilder();
        for (Map.Entry<String, List<String>> entry : conn.getHeaderFields().entrySet()) {
            String key = entry.getKey();
            if (key == null || !"Set-Cookie".equalsIgnoreCase(key)) {
                continue;
            }
            List<String> values = entry.getValue();
            if (values == null) {
                continue;
            }
            for (String value : values) {
                if (value == null) {
                    continue;
                }
                String part = value.split(";", 2)[0].trim();
                if (part.indexOf('=') <= 0) {
                    continue;
                }
                if (out.length() > 0) {
                    out.append("; ");
                }
                out.append(part);
            }
        }
        return out.toString();
    }

    public static String fetch(String url, String cookie, int timeoutMs) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setConnectTimeout(timeoutMs);
            conn.setReadTimeout(timeoutMs);
            conn.setRequestMethod("GET");
            if (cookie != null && cookie.length() > 0) {
                conn.setRequestProperty("Cookie", cookie);
            }
            int code = conn.getResponseCode();
            return read(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
        } catch (Exception ignored) {
            return "";
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    private static String read(InputStream input) {
        if (input == null) {
            return "";
        }
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[4096];
            int count;
            while ((count = input.read(buffer)) >= 0) {
                out.write(buffer, 0, count);
            }
            return new String(out.toByteArray(), "UTF-8");
        } catch (Exception ignored) {
            return "";
        } finally {
            try {
                input.close();
            } catch (Exception ignored) {
            }
        }
    }
}

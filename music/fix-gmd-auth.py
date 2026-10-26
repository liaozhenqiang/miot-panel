#!/usr/bin/env python3
"""修复 Songloft 里 go-music-dl 插件的扫码登录 401（请先登录）。

go-music-dl 开启 Web 管理员登录后，Songloft 插件必须带上管理员账号，
否则 /qr_login/* 会返回 401。本脚本只在本机读取两个服务的账号文件，
把凭据写入插件配置并逐个音源自检，不回显密码、不访问外网。

用法：
  sudo python3 fix-gmd-auth.py            # 写入并自检
  sudo python3 fix-gmd-auth.py --check    # 只检查，不写入
  python3 fix-gmd-auth.py --gmd-admin ./admin.txt --songloft http://127.0.0.1:58091
"""

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_SL_ENV = "/etc/songloft/songloft.env"
DEFAULT_GMD_ADMIN = "/etc/go-music-dl/admin.txt"
DEFAULT_SL_URL = os.environ.get("MIOT_SONGLOFT") or "http://127.0.0.1:58091"
PLUGIN_API = "/api/v1/jsplugin/go-music-dl"
SOURCES = ("netease", "qq", "qq_wx", "kugou", "bilibili")


def read_kv(path):
    data = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return data
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def http(base, method, path, payload=None, token="", timeout=20):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw or b"{}")
            except ValueError:
                return resp.status, {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {"error": raw[:200].decode("utf-8", "replace")}
        return exc.code, data
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc.reason)}
    except OSError as exc:
        return 0, {"error": str(exc)}


def die(message):
    print("错误：" + message, file=sys.stderr)
    sys.exit(2)


def load_gmd_credentials(args):
    if args.gmd_user or args.gmd_pass:
        return args.gmd_user or "", args.gmd_pass or ""
    env_user = os.environ.get("GMD_ADMIN_USER") or os.environ.get("GMD_USER") or ""
    env_pass = os.environ.get("GMD_ADMIN_PASS") or os.environ.get("GMD_PASS") or ""
    if env_user and env_pass:
        return env_user, env_pass
    data = read_kv(args.gmd_admin)
    user = data.get("username") or data.get("user") or ""
    password = data.get("password") or data.get("pass") or ""
    if user and password:
        return user, password
    return "", ""


def login_songloft(base, env_file):
    creds = read_kv(env_file)
    user = creds.get("ADMIN_USERNAME") or ""
    password = creds.get("ADMIN_PASSWORD") or ""
    if not user or not password:
        die("未找到 Songloft 管理员凭据，请用 --songloft-env 指定 env 文件")
    status, data = http(base, "POST", "/api/v1/auth/login",
                        {"username": user, "password": password})
    if status != 200 or not data.get("access_token"):
        die("Songloft 登录失败（HTTP %s），请确认地址与服务状态" % status)
    return str(data["access_token"])


def qr_check(base, token, sources, timeout):
    results = []
    for source in sources:
        status, data = http(base, "POST", PLUGIN_API + "/qr/start",
                            {"source": source}, token, timeout)
        session = data.get("session") if isinstance(data, dict) else None
        ok = status == 200 and isinstance(session, dict) and bool(
            session.get("url") or session.get("image_url"))
        detail = ""
        if not ok:
            detail = str((data or {}).get("error") or (data or {}).get("message") or "")
            if status == 0:
                detail = "无法连接 Songloft：" + detail
            elif not detail:
                detail = "HTTP %s" % status
            if "请先登录" in detail or status == 502:
                detail += "（检查 go-music-dl 管理员账号是否与当前密码一致）"
        results.append((source, ok, detail))
    return results


def main():
    parser = argparse.ArgumentParser(
        description="把 go-music-dl 管理员凭据写入 Songloft 插件，修复扫码登录 401")
    parser.add_argument("--check", action="store_true", help="只检查，不写入配置")
    parser.add_argument("--songloft", default=DEFAULT_SL_URL, help="Songloft 地址，默认 %(default)s")
    parser.add_argument("--songloft-env", default=DEFAULT_SL_ENV, help="Songloft env 文件")
    parser.add_argument("--gmd-admin", default=DEFAULT_GMD_ADMIN, help="go-music-dl 管理员文件")
    parser.add_argument("--gmd-user", default="", help="go-music-dl 管理员用户名（覆盖文件）")
    parser.add_argument("--gmd-pass", default="", help="go-music-dl 管理员密码（覆盖文件）")
    parser.add_argument("--source", action="append", default=[],
                        help="只检查指定音源，可重复；默认全部")
    parser.add_argument("--timeout", type=float, default=20.0, help="请求超时秒数")
    args = parser.parse_args()

    base = args.songloft.rstrip("/")
    sources = tuple(args.source) or SOURCES

    token = login_songloft(base, args.songloft_env)
    print("Songloft：%s（已登录）" % base)

    status, config = http(base, "GET", PLUGIN_API + "/config", token=token, timeout=args.timeout)
    if status != 200 or not isinstance(config, dict):
        die("读取插件配置失败（HTTP %s），请确认已安装 GoMusicDL 插件" % status)

    user, password = load_gmd_credentials(args)
    if not user or not password:
        if args.check:
            print("go-music-dl 管理员：未找到凭据文件 %s" % args.gmd_admin)
        else:
            if not sys.stdin.isatty():
                die("未找到 go-music-dl 管理员凭据，请用 --gmd-admin 或 --gmd-user/--gmd-pass 指定")
            print("未找到 %s，请手工输入 go-music-dl 管理员账号（只保存在本机）" % args.gmd_admin)
            user = input("用户名：").strip()
            password = getpass.getpass("密码：")
            if not user or not password:
                die("账号或密码为空")

    current_user = str(config.get("gmdAuthUser") or "")
    current_pass = str(config.get("gmdAuthPass") or "")
    configured = bool(current_user and current_pass)
    same = configured and current_user == user and current_pass == password
    print("插件账号：%s" % ("已配置" if configured else "未配置"))
    if configured:
        print("账号匹配：%s" % ("是" if same else "否（将更新）"))

    if args.check:
        if not configured:
            print("建议：去掉 --check 运行一次，写入账号后再扫码")
    elif same:
        print("配置：无需修改")
    else:
        status, result = http(base, "POST", PLUGIN_API + "/config",
                              {"gmdAuthUser": user, "gmdAuthPass": password},
                              token, args.timeout)
        if status != 200 or not (isinstance(result, dict) and result.get("success")):
            die("写入插件配置失败（HTTP %s）：%s" % (status, str(result)[:200]))
        _, verify = http(base, "GET", PLUGIN_API + "/config", token=token, timeout=args.timeout)
        if not (verify.get("gmdAuthUser") and verify.get("gmdAuthPass")):
            die("写入后校验失败，请刷新 Songloft 后重试")
        print("配置：已写入并校验通过")
        print("提示：如果 Songloft 设置页还开着，请先刷新再做其他操作；")
        print("      在「账号登录」为空时点保存会覆盖刚写入的凭据。")

    results = qr_check(base, token, sources, args.timeout)
    print("\n扫码接口自检：")
    failed = 0
    for source, ok, detail in results:
        print("  %-8s %s%s" % (source, "OK" if ok else "失败", ("：" + detail) if detail else ""))
        failed += 0 if ok else 1
    total = len(results)
    print("\n结果：%d/%d 个音源可创建二维码" % (total - failed, total))
    if "qq_wx" in [r[0] for r in results]:
        print("提示：QQ 音乐（微信）上游状态查询约 15 秒，插件轮询只等 8 秒；")
        print("      该入口可能提示连接失败，请改用「QQ 音乐」入口。")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

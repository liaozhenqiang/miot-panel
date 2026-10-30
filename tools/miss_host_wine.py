"""32-bit Wine helper: official libmiss -> raw HEVC on stdout.

Runs inside the Docker image under Wine (32-bit Windows Python). The panel pipes
stdout into ffmpeg (Linux) to build MPEG-TS for go2rtc.

usage: wine python.exe miss_host_wine.py --did X --model Y --rpc URL
                                       --token-file F --dll-dir D --log F [--parent PID]
"""
import ctypes
import json
import os
import sys
import time
import urllib.request

ARGS = {}
argv = sys.argv[1:]
for i in range(0, len(argv) - 1, 2):
    ARGS[argv[i]] = argv[i + 1]

DID = ARGS.get("--did", "")
MODEL = ARGS.get("--model", "")
RPC = ARGS.get("--rpc", "")
TOKEN_FILE = ARGS.get("--token-file", "")
DLL_DIR = ARGS.get("--dll-dir", "")
LOGPATH = ARGS.get("--log", "")
PARENT = int(ARGS.get("--parent", "0") or 0)

try:
    TOKEN = open(TOKEN_FILE, encoding="ascii").read().strip()
except Exception:
    TOKEN = ""

logf = None
if LOGPATH:
    logf = open(LOGPATH, "a", encoding="utf-8")


def log(msg):
    if logf:
        logf.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
        logf.flush()


class DevInfo(ctypes.Structure):
    _fields_ = [("did", ctypes.c_char_p), ("did_len", ctypes.c_int),
                ("model", ctypes.c_char_p), ("model_len", ctypes.c_int),
                ("platform", ctypes.c_char_p), ("platform_len", ctypes.c_int)]


CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.c_void_p, ctypes.c_void_p)

state = {"frames": 0, "bytes": 0, "codec": 0, "first": 0.0}

# libmiss 在 wine 下会往 stdout 打日志，会污染视频流：
# 先把真正的 stdout 复制出来给帧用，再把 fd 1 指向空设备。
try:
    out = os.fdopen(os.dup(1), "wb", 0)
except Exception:
    out = sys.stdout.buffer
try:
    _null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_null, 1)
    os.close(_null)
except Exception:
    pass


def rpc_forward(ctx, path, body):
    try:
        req = urllib.request.Request(
            RPC, data=json.dumps({"path": path, "body": body}).encode(),
            headers={"Content-Type": "application/json", "X-Miss-Token": TOKEN})
        with urllib.request.urlopen(req, timeout=40) as resp:
            payload = resp.read().decode("utf-8")
        try:
            env = json.loads(payload)
            if isinstance(env, dict) and "ok" in env and "data" in env:
                if not env.get("ok"):
                    log("rpc %s rejected: %s" % (path, env.get("error")))
                    lib.invoke_miss_rpc_process(ctx, None, 0)
                    return 0
                payload = env.get("data") or ""
        except ValueError:
            pass
        if not payload:
            lib.invoke_miss_rpc_process(ctx, None, 0)
            return 0
        log("rpc %s ok %d bytes head=%r" % (path, len(payload), payload[:110]))
        return lib.invoke_miss_rpc_process(ctx, payload.encode(), len(payload.encode()))
    except Exception as exc:
        log("rpc %s failed: %r" % (path, exc))
        lib.invoke_miss_rpc_process(ctx, None, 0)
        return 0


def make_cb(idx):
    def cb(a1, a2, a3, a4, a5, a6, a7):
        if idx == 1:
            path = ""
            try:
                path = ctypes.string_at(a2, 200).split(b"\x00")[0].decode("utf-8", "replace")
            except Exception:
                return 0
            blen = int(a4 or 0)
            body = ""
            if 0 < blen <= 65536:
                try:
                    body = ctypes.string_at(a3, blen).decode("utf-8", "replace")
                except Exception:
                    body = ""
            if path and body:
                rpc_forward(a1, path, body)
            return 0
        if idx == 4:
            try:
                ln = ctypes.c_uint32.from_address(a2).value
                codec = ctypes.c_uint32.from_address(a2 + 4).value
                if 0 < ln <= 4000000:
                    out.write(ctypes.string_at(a3, ln))
                    out.flush()
                    state["frames"] += 1
                    state["bytes"] += ln
                    if not state["first"]:
                        state["first"] = time.time()
                        state["codec"] = codec
                        log("first frame codec=%d len=%d" % (codec, ln))
            except Exception as exc:
                log("frame error %r" % (exc,))
        elif idx == 2:
            log("connected")
        return 0
    return CB(cb)


cbs = [make_cb(i + 1) for i in range(9)]

if not (DID and MODEL and RPC and DLL_DIR and os.path.isfile(os.path.join(DLL_DIR, "libmiss.dll"))):
    log("missing args or libmiss.dll")
    sys.exit(2)

os.environ["PATH"] = DLL_DIR + os.pathsep + os.environ.get("PATH", "")
os.chdir(DLL_DIR)
try:
    os.add_dll_directory(DLL_DIR)
except Exception:
    pass

lib = ctypes.WinDLL(os.path.join(DLL_DIR, "libmiss.dll"))
lib.miss_log_set_level.argtypes = [ctypes.c_int]
lib.miss_log_set_path.argtypes = [ctypes.c_char_p]
lib.miss_client_session_open.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
lib.miss_client_session_open.restype = ctypes.c_void_p
lib.miss_client_session_close.argtypes = [ctypes.c_void_p]
lib.miss_cmd_send.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
lib.miss_cmd_send.restype = ctypes.c_int
lib.invoke_miss_rpc_process.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
lib.invoke_miss_rpc_process.restype = ctypes.c_int

lib.miss_log_set_level(2)
if LOGPATH:
    try:
        # 必须是文件路径：传目录会让 libmiss 退回 stdout，把视频流写坏
        lib.miss_log_set_path((LOGPATH + ".libmiss.log").encode("ascii"))
    except Exception:
        pass

log("host start did=%s model=%s dll=%s" % (DID, MODEL, DLL_DIR))
dev = DevInfo(DID.encode(), len(DID), MODEL.encode(), len(MODEL), b"pc", 2)
log("register -> %s" % lib.register_miss_client_callback(*cbs))
time.sleep(0.2)
sess = lib.miss_client_session_open(ctypes.byref(dev), None)
log("session = %s" % sess)
if not sess:
    sys.exit(3)

deadline = time.time() + 60
while time.time() < deadline and state["frames"] == 0:
    rc = lib.miss_cmd_send(sess, 0x102, None, 0)
    log("video start rc=%s" % rc)
    time.sleep(3)

if state["frames"] == 0:
    log("no frame")
    lib.miss_client_session_close(sess)
    sys.exit(4)

while True:
    if PARENT:
        try:
            alive = (os.path.exists(r"Z:\proc\%d" % PARENT)
                     or os.path.exists("/proc/%d" % PARENT))
        except Exception:
            alive = True
        if not alive:
            log("parent gone")
            break
    time.sleep(3)
log("stop frames=%d bytes=%d" % (state["frames"], state["bytes"]))
try:
    lib.miss_cmd_send(sess, 0x103, None, 0)
    lib.miss_client_session_close(sess)
except Exception:
    pass

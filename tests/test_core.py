import importlib.util
import json
import os
import tarfile
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ["MIOT_ROOT"] = tempfile.mkdtemp(prefix="miot-test-")
os.environ["MIOT_APP_DIR"] = str(ROOT)
os.environ["MIOT_LOG_FILE"] = ""

spec = importlib.util.spec_from_file_location("miot_cloud", ROOT / "miot-cloud.py")
assert spec and spec.loader
miot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(miot)


def test_rate_limit_window():
    miot.RATE_EVENTS.clear()
    assert miot._rate_ok("test:ip", 2, 60)
    assert miot._rate_ok("test:ip", 2, 60)
    assert not miot._rate_ok("test:ip", 2, 60)
    miot.RATE_EVENTS.clear()


def test_rate_limit_min_interval():
    miot.RATE_EVENTS.clear()
    assert miot._rate_ok("test:fast", 10, 60, 5.0)
    assert not miot._rate_ok("test:fast", 10, 60, 5.0)
    miot.RATE_EVENTS.clear()


def test_coerce_bool():
    assert miot._coerce_cap({"kind": "bool"}, "true") is True
    assert miot._coerce_cap({"kind": "bool"}, 0) is False


def test_coerce_enum():
    prop = {"kind": "enum", "options": [{"value": 0}, {"value": 2}]}
    assert miot._coerce_cap(prop, "2") == 2
    with pytest.raises(ValueError):
        miot._coerce_cap(prop, "1")


def test_coerce_range():
    prop = {"kind": "range", "format": "float", "range": [16, 30, 0.5]}
    assert miot._coerce_cap(prop, "23.5") == 23.5
    with pytest.raises(ValueError):
        miot._coerce_cap(prop, "35")


def test_sensor_display():
    assert miot._sensor_display("contact", 1) == "关闭"
    assert miot._sensor_display("contact", 0) == "打开"
    assert miot._sensor_display("water", 0) == "正常"
    assert miot._sensor_display("temp", 21.5) is None


def test_weather_code_text():
    assert miot._open_meteo_text(0) == "晴"
    assert miot._open_meteo_text(61) == "小雨"
    assert miot._open_meteo_text(None) == "天气未知"


def test_body_limit_constant():
    assert miot.Handler.max_body == 1024 * 1024


def test_update_version_compare():
    assert miot._version_tuple("v0.3.5") == (0, 3, 5, 0)
    assert miot._version_tuple("0.3.5") < miot._version_tuple("0.3.10")
    assert miot._version_tuple(None) == (0, 0, 0, 0)


def test_update_extract_rejects_traversal(tmp_path):
    package = tmp_path / "pkg.tar.gz"
    stage = tmp_path / "stage"
    stage.mkdir()
    src = tmp_path / "src"
    files = {
        "../evil.txt": b"x",
        "miot-panel-linux-v9/miot-cloud.py": b'APP_VERSION = "9.9.9"\n',
        "miot-panel-linux-v9/cloud_panel.html": b"<html></html>\n",
    }
    with tarfile.open(package, "w:gz") as tar:
        for name, data in files.items():
            path = src / Path(name).name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            tar.add(path, arcname=name)
    found = miot._extract_update_package(str(package), str(stage))
    assert found and found.endswith("miot-panel-linux-v9")
    assert not (tmp_path / "evil.txt").exists()
    assert not (stage / "evil.txt").exists()
    assert miot._file_version(os.path.join(found, "miot-cloud.py")) == "9.9.9"


def test_camera_component_catalog_is_pinned():
    assert miot.CAMERA_COMPONENTS
    for key, parts in miot.CAMERA_COMPONENTS.items():
        assert set(parts) == {"go2rtc", "ffmpeg"}, key
        for part in parts.values():
            assert part["url"].startswith(("https://github.com/", "https://raw.githubusercontent.com/"))
            assert len(part["sha256"]) == 64
            assert part["size"] > 0
            assert part["target"]
    for key in (("windows", "x64"), ("windows", "x86"), ("windows", "arm64"),
                ("linux", "x64"), ("linux", "arm"), ("linux", "arm64"),
                ("linux", "x86"), ("darwin", "x64"), ("darwin", "arm64")):
        assert key in miot.CAMERA_COMPONENTS, key


def test_go2rtc_self_url_is_not_external(monkeypatch):
    monkeypatch.setattr(miot, "LAN_IP", "192.168.1.10")
    monkeypatch.setitem(miot.CONFIG, "go2rtc_url", "http://192.168.1.10:1984")
    assert miot._go2rtc_host_is_local("192.168.1.10")
    assert not miot._camera_external_go2rtc()
    assert miot._go2rtc_base() == "http://127.0.0.1:1984"


def test_windows_pid_alive():
    import os as _os
    assert not miot._windows_pid_alive(999999999)
    assert miot._windows_pid_alive(_os.getpid())


def test_port_listening_pids():
    import os as _os
    if _os.name != "nt":
        return
    holder = miot.socket.socket(miot.socket.AF_INET, miot.socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.listen(1)
    try:
        assert str(_os.getpid()) in miot._port_listening_pids(port)
    finally:
        holder.close()


def test_qr_poll_success_saves_qrcode_auth(monkeypatch):
    class Resp:
        status_code = 200
        text = '&&&START&&&{"code":0,"passToken":"pt","userId":"42","cUserId":"c"}'

    class Sess:
        def get(self, *args, **kwargs):
            return Resp()

    miot.QR_SESSIONS.clear()
    miot.QR_SESSIONS["sid"] = {
        "sess": Sess(), "poll_url": "http://example.invalid/",
        "created": miot.time.time(), "poll_count": 0,
        "in_flight": False, "non_zero": 0,
    }
    monkeypatch.setattr(miot, "_refresh_xiaomiio_by_pass_token",
                        lambda pt, uid, name: {
                            "user_id": uid, "service_token": "io", "ssecurity": "ss",
                            "pass_token": pt, "expires_at": int(miot.time.time()) + 3600})
    monkeypatch.setattr(miot, "_plugin_connected", lambda: False)
    result = miot._qr_poll("sid")
    assert result["state"] == "success"
    assert result["user_id"] == "42"
    assert result["pending"] is True
    assert miot._read_auth_file()["source"] == "qrcode"
    assert miot._read_pending_plugin_auth()["user_id"] == "42"
    assert "sid" not in miot.QR_SESSIONS


def test_device_cookie_survives_pin_change():
    """设备 cookie 用独立随机密钥签名，改 PIN 不会把已授权设备踢下线。"""
    class FakeHandler:
        client_address = ("192.168.1.5", 12345)

        def __init__(self, cookie):
            self.headers = {"Cookie": cookie}

    old_salt = miot.CONFIG.get("admin_pin_salt")
    old_hash = miot.CONFIG.get("admin_pin_hash")
    old_secret = miot.CONFIG.get("device_secret")
    try:
        miot.CONFIG["admin_pin_salt"] = "salt"
        miot.CONFIG["admin_pin_hash"] = "hash-a"
        miot.CONFIG["device_secret"] = "s" * 64
        value = miot._device_cookie_header().split(";", 1)[0]
        assert miot._device_cookie_valid(FakeHandler(value))
        miot.CONFIG["admin_pin_hash"] = "hash-b"
        assert miot._device_cookie_valid(FakeHandler(value))
        miot.CONFIG["device_secret"] = "t" * 64
        assert not miot._device_cookie_valid(FakeHandler(value))
    finally:
        miot.CONFIG["admin_pin_salt"] = old_salt
        miot.CONFIG["admin_pin_hash"] = old_hash
        miot.CONFIG["device_secret"] = old_secret


def test_sensor_semantics_and_units():
    assert miot._sensor_display("contact", True) == "关闭"
    assert miot._sensor_display("contact", False) == "打开"
    assert miot._sensor_display("contact", "open") == "打开"
    assert miot._sensor_display("motion", "motion") == "有人"
    assert miot._sensor_display("motion", "no_motion") == "无人"
    meta = {"value_list": [{"value": 0, "description": "open"},
                           {"value": 1, "description": "closed"}]}
    assert miot._sensor_display("contact", 0, meta) == "打开"
    assert miot._sensor_display("contact", 1, meta) == "关闭"
    assert miot._sensor_unit("minutes", "") == "分钟"
    assert miot._sensor_unit("μg/m3", "").replace("μ", "µ") == "µg/m³"
    assert miot._sensor_unit("pascal", "") == "Pa"


def test_sensor_spec_cache_skips_stale_and_empty_entries(tmp_path, monkeypatch):
    cache = tmp_path / "miot_sensor_specs.json"
    stale = {"key": "temperature", "label": "温度", "unit": "°C",
             "siid": 2, "piid": 1}
    fresh = {"key": "humidity", "label": "湿度", "unit": "%",
             "format": "uint8", "siid": 2, "piid": 2}
    cache.write_text(json.dumps({
        "stale.sensor": [stale],
        "empty.sensor": [],
        "fresh.sensor": [fresh],
    }), encoding="utf-8")
    monkeypatch.setattr(miot, "SENSOR_SPEC_FILE", str(cache))
    monkeypatch.setattr(miot, "SENSOR_SPECS", {})
    miot._load_sensor_specs()
    assert "stale.sensor" not in miot.SENSOR_SPECS
    assert "empty.sensor" not in miot.SENSOR_SPECS
    assert miot.SENSOR_SPECS["fresh.sensor"][0]["key"] == "humidity"


def test_sensor_spec_empty_fetch_is_not_cached(tmp_path, monkeypatch):
    cache = tmp_path / "miot_sensor_specs.json"
    monkeypatch.setattr(miot, "SENSOR_SPEC_FILE", str(cache))
    monkeypatch.setattr(miot, "SENSOR_SPECS", {})
    monkeypatch.setattr(miot, "SENSOR_SPEC_TRY", {})
    monkeypatch.setattr(miot, "SENSOR_MODEL_TYPES", {"test.sensor": "urn:test"})

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"services": []}'

    monkeypatch.setattr(miot.urllib.request, "urlopen", lambda *a, **k: Resp())
    assert miot._sensor_props("test.sensor") == []
    assert "test.sensor" not in miot.SENSOR_SPECS
    assert not cache.exists()


def test_stale_sensor_cache_refetches_and_saves_props(tmp_path, monkeypatch):
    cache = tmp_path / "miot_sensor_specs.json"
    stale = {"key": "temperature", "label": "温度", "unit": "°C",
             "siid": 2, "piid": 1}
    cache.write_text(json.dumps({"test.sensor": [stale]}), encoding="utf-8")
    monkeypatch.setattr(miot, "SENSOR_SPEC_FILE", str(cache))
    monkeypatch.setattr(miot, "SENSOR_SPECS", {})
    monkeypatch.setattr(miot, "SENSOR_SPEC_TRY", {})
    monkeypatch.setattr(miot, "SENSOR_MODEL_TYPES", {"test.sensor": "urn:test"})
    miot._load_sensor_specs()
    assert "test.sensor" not in miot.SENSOR_SPECS

    spec = {"services": [{"iid": 2, "properties": [{
        "iid": 1,
        "type": "urn:miot-spec-v2:property:temperature:00000020:test:1",
        "format": "float",
        "access": ["read"],
        "unit": "celsius",
    }]}]}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(spec).encode("utf-8")

    monkeypatch.setattr(miot.urllib.request, "urlopen", lambda *a, **k: Resp())
    props = miot._sensor_props("test.sensor")
    assert props and props[0]["key"] == "temp"
    assert props[0]["unit"] == "°C"
    assert json.loads(cache.read_text(encoding="utf-8"))["test.sensor"]


def _ts_packet(pid, payload, adapt_len=0):
    head = bytearray([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x30 if adapt_len else 0x10])
    if adapt_len:
        head.append(adapt_len)
        head += bytearray([0x00] * adapt_len)
    body = bytearray([0x00]) + bytearray(payload)          # section pointer + table
    pkt = bytes(head) + bytes(body)
    return pkt + b"\xff" * (188 - len(pkt))


def _ts_capture(video_type):
    pat = [0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x04, 0x00, 0x01, 0xE0, 0x10, 0, 0, 0, 0]
    pmt = [0x02, 0xB0, 0x10, 0x00, 0x01, 0xC1, 0xE1, 0x01, 0xF0, 0x00,
           video_type, 0xE1, 0x01, 0xF0, 0x00, 0, 0, 0, 0]
    return _ts_packet(0, pat) + _ts_packet(16, pmt, adapt_len=3) + _ts_packet(16, [0x00] * 40)


def test_ts_video_codec_detects_h264():
    assert miot._ts_video_codec(_ts_capture(miot.TS_VIDEO_H264)) == "h264"


def test_ts_video_codec_resyncs_mid_stream():
    # 从流中间截断时包头不在 0 偏移，必须自己找回步长
    stream = _ts_capture_go2rtc(miot.TS_VIDEO_H264)
    assert miot._ts_video_codec(b"\x12\x34\x56" + stream) == "h264"


def _ts_capture_go2rtc(video_type):
    """go2rtc 的写法：PAT 的 following_len 是 0，PMT 的 program_info_length 写成 0xFFFF。"""
    pat = [0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x00, 0x00, 0x01, 0xF0, 0x00, 0, 0, 0, 0]
    pmt = [0x02, 0xB0, 0x12, 0x00, 0x01, 0xC1, 0x00, 0x00, 0xFF, 0xFF, 0xF0, 0x00,
           video_type, 0xE1, 0x00, 0xF0, 0x00, 0, 0, 0, 0]
    return _ts_packet(0, pat) + _ts_packet(4096, pmt) + _ts_packet(256, [0x00] * 60)


def test_ts_video_codec_handles_go2rtc_pmt_quirk():
    assert miot._ts_video_codec(_ts_capture_go2rtc(miot.TS_VIDEO_H264)) == "h264"
    assert miot._ts_video_codec(_ts_capture_go2rtc(miot.TS_VIDEO_HEVC)) == "hevc"


def test_ts_video_codec_detects_hevc():
    assert miot._ts_video_codec(_ts_capture(miot.TS_VIDEO_HEVC)) == "hevc"


def test_ts_video_codec_returns_empty_on_junk():
    assert miot._ts_video_codec(b"not a transport stream at all") == ""
    assert miot._ts_video_codec(_ts_packet(7, [0x00] * 100)) == ""


def test_camera_snapshot_serves_fresh_cache_without_go2rtc(tmp_path, monkeypatch):
    root = tmp_path / "camera-stack"
    (root / "snapshots").mkdir(parents=True)
    monkeypatch.setattr(miot, "_camera_paths",
                        lambda: {"root": str(root)})
    jpg = b"\xff\xd8" + b"frame" * 200
    (root / "snapshots" / "77.jpg").write_bytes(jpg)
    called = []
    monkeypatch.setattr(miot, "_camera_go2rtc_ok", lambda: called.append(1) or True)
    assert miot._camera_snapshot("77") == jpg
    assert called == []                      # 45 秒内的缓存不该再去抓帧


def test_camera_snapshot_fetches_and_caches(tmp_path, monkeypatch):
    root = tmp_path / "camera-stack"
    root.mkdir()
    monkeypatch.setattr(miot, "_camera_paths", lambda: {"root": str(root)})
    monkeypatch.setattr(miot, "_camera_go2rtc_ok", lambda: True)
    monkeypatch.setattr(miot, "_go2rtc_base", lambda: "http://127.0.0.1:1984")
    jpg = b"\xff\xd8" + b"x" * 3000

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return jpg

    seen = []
    monkeypatch.setattr(miot.urllib.request, "urlopen",
                        lambda req, *a, **k: seen.append(req.full_url) or Resp())
    assert miot._camera_snapshot("88") == jpg
    assert "cam_88" in seen[0]
    assert (root / "snapshots" / "88.jpg").read_bytes() == jpg


def test_camera_snapshot_falls_back_to_last_frame(tmp_path, monkeypatch):
    root = tmp_path / "camera-stack"
    (root / "snapshots").mkdir(parents=True)
    monkeypatch.setattr(miot, "_camera_paths", lambda: {"root": str(root)})
    stale = b"\xff\xd8" + b"old" * 400
    (root / "snapshots" / "99.jpg").write_bytes(stale)
    monkeypatch.setattr(miot, "_camera_go2rtc_ok", lambda: True)
    monkeypatch.setattr(miot, "_go2rtc_base", lambda: "http://127.0.0.1:1984")

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return b"<html>ffmpeg missing</html>"      # 不是 JPEG

    monkeypatch.setattr(miot.urllib.request, "urlopen", lambda *a, **k: Resp())
    assert miot._camera_snapshot("99", refresh=True) == stale
    assert (root / "snapshots" / "99.jpg").read_bytes() == stale


def test_camera_codec_config_round_trip(tmp_path, monkeypatch):
    cfg = tmp_path / "cloud_config.json"
    monkeypatch.setattr(miot, "CONFIG_FILE", str(cfg))
    miot.CONFIG["camera_codec"] = {"12": "h264", "13": "hevc", "14": "bogus"}
    miot.save_config({"camera_codec": {"12": "h264", "13": "hevc", "14": "bogus"}})
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["camera_codec"] == {"12": "h264", "13": "hevc"}   # 只认两种编码
    assert miot._normalize_config({})["camera_codec"] == {}


def test_camera_play_src_prefers_direct_for_h264():
    cam, quality = dict(miot.CAM_CODEC), dict(miot.CAM_QUALITY)
    try:
        miot.CAM_CODEC["1"] = "h264"
        miot.CAM_CODEC["2"] = "hevc"
        assert miot._camera_play_src("1", True) == "cam_1"
        assert miot._camera_play_src("2", True) == "cam_2_h264"
        assert miot._camera_play_src("3", True) == "cam_3_h264"       # 未知编码保守走转码
        miot.CAM_QUALITY["1"] = "hd"
        assert miot._camera_play_src("1", True) == "cam_1_h264"       # 点名清晰度就要转码
        assert miot._camera_play_src("3", False) == ""
        assert miot._camera_play_src("1", False) == "cam_1"
    finally:
        miot.CAM_CODEC.clear(), miot.CAM_QUALITY.clear()
        miot.CAM_CODEC.update(cam), miot.CAM_QUALITY.update(quality)


def test_camera_ready_payload_switches_to_direct_when_registered(monkeypatch):
    cam, quality = dict(miot.CAM_CODEC), dict(miot.CAM_QUALITY)
    probed = []
    monkeypatch.setattr(miot, "_camera_ffmpeg_bin", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(miot, "_camera_external_go2rtc", lambda: "")
    monkeypatch.setattr(miot, "_go2rtc_put", lambda *a: (True, ""))
    monkeypatch.setattr(miot, "_camera_codec_probe_async", probed.append)
    try:
        miot.CAM_CODEC.clear()
        miot.CAM_CODEC["5"] = "h264"
        miot.CAM_QUALITY.clear()
        got = miot._camera_ready_payload("5", "cam_5_h264", {"cam_5", "cam_5_h264"})
        assert got["src"] == "cam_5" and got["direct"] is True
        assert probed == ["5"]
        # 未知编码时保持原样，并且会安排一次探测
        miot.CAM_CODEC.clear()
        got = miot._camera_ready_payload("6", "cam_6_h264", {"cam_6", "cam_6_h264"})
        assert got["src"] == "cam_6_h264" and got.get("direct") is False
        assert probed == ["5", "6"]
        # 用户选了清晰度就不再自动切回直连
        miot.CAM_CODEC["7"] = "h264"
        miot.CAM_QUALITY["7"] = "sd"
        got = miot._camera_ready_payload("7", "cam_7", {"cam_7", "cam_7_h264"})
        assert got["src"] == "cam_7_h264"
        assert probed == ["5", "6"]                      # 已选档位不重复探测
    finally:
        miot.CAM_CODEC.clear(), miot.CAM_QUALITY.clear()
        miot.CAM_CODEC.update(cam), miot.CAM_QUALITY.update(quality)


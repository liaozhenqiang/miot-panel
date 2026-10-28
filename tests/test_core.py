import importlib.util
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


def test_device_cookie_invalidates_on_pin_change():
    class FakeHandler:
        client_address = ("192.168.1.5", 12345)

        def __init__(self, cookie):
            self.headers = {"Cookie": cookie}

    old_salt = miot.CONFIG.get("admin_pin_salt")
    old_hash = miot.CONFIG.get("admin_pin_hash")
    try:
        miot.CONFIG["admin_pin_salt"] = "salt"
        miot.CONFIG["admin_pin_hash"] = "hash-a"
        header = miot._device_cookie_header()
        value = header.split(";", 1)[0]
        assert miot._device_cookie_valid(FakeHandler(value))
        miot.CONFIG["admin_pin_hash"] = "hash-b"
        assert not miot._device_cookie_valid(FakeHandler(value))
    finally:
        miot.CONFIG["admin_pin_salt"] = old_salt
        miot.CONFIG["admin_pin_hash"] = old_hash


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

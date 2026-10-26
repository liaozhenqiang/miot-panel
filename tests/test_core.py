import importlib.util
import os
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
    assert miot._sensor_display("contact", 1) == "打开"
    assert miot._sensor_display("water", 0) == "正常"
    assert miot._sensor_display("temp", 21.5) is None


def test_weather_code_text():
    assert miot._open_meteo_text(0) == "晴"
    assert miot._open_meteo_text(61) == "小雨"
    assert miot._open_meteo_text(None) == "天气未知"


def test_body_limit_constant():
    assert miot.Handler.max_body == 1024 * 1024

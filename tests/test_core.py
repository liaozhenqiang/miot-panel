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
    assert miot._sensor_display("contact", 1) == "打开"
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

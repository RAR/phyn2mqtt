import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "phyn2mqtt.py"
sys.path.insert(0, str(SRC.parent))
import phyn2mqtt as p2m  # noqa: E402


def test_only_the_two_read_actions_can_be_sent():
    assert p2m.READ_ACTIONS == {"http://phyn.com/jnap/core/GetDeviceInfo", "http://phyn.com/jnap/attribute/get"}
    with pytest.raises(p2m.Refused):
        p2m.request_bytes("10.0.0.1", "http://phyn.com/jnap/shutoff/" + "SetShutoff" + "ValveState")
    req = p2m.request_bytes("10.0.0.1", p2m.GET_ATTRIBUTES)
    assert req.startswith(b"POST /JNAP/ HTTP/1.1\r\n") and b"X-JNAP-Action: http://phyn.com/jnap/attribute/get\r\n" in req
    assert req.endswith(b"\r\n\r\n{}")


def test_the_valve_action_appears_nowhere_in_the_poller():
    text = SRC.read_text()
    assert "SetShutoff" not in text and "shutoff/" not in text
    assert ".subscribe(" not in text   # nothing on MQTT can reach it


def test_a_malformed_reply_is_read_leniently():
    body = json.dumps({"result": "OK", "product": {"sensor_flow": 1.5}}).encode()
    raw = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nDEBUG attribute dump begins\r\n"
           b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body + b"HTTP/1.1 200 OK\r\n\r\n{\"garbage\"")
    assert p2m.parse_response(raw)["product"]["sensor_flow"] == 1.5


def test_a_refusal_or_bad_status_is_an_error():
    with pytest.raises(ValueError, match="answered"):
        p2m.parse_response(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 2\r\n\r\n{}")
    with pytest.raises(ValueError, match="refused"):
        p2m.parse_response(b'HTTP/1.1 200 OK\r\nContent-Length: 26\r\n\r\n{"result": "ErrorUnknown"}')


def test_the_state_keeps_only_chosen_readings():
    attrs = {"result": "OK", "system": {"external_ip": "172.0.0.1", "admin_password": "<hidden>"},
             "product": {"sensor_flow": -0.00254, "sensor_flow_low_to_off": 0.01, "sensor_flow_state": "off",
                         "sensor_pressure_1": 48.95, "sensor_temperature_1": 78.2, "consumption_total": 123456.78,
                         "sov_state_str": "opened", "sov_close_count": 74, "ml_plumbing_check_state_str": "<unset>",
                         "alert_notifier_fp_state_str": "normal", "ml_offline_leak_detector_state_str": "enabled"},
             "stats": {"wifi_rssi_avg": -34, "device_up_time_sec": 923314, "connection_state_str": "iot_streaming"}}
    s = p2m.state_of(attrs, 100.0)
    assert s["flow_gpm"] == 0.0 and s["flow_state"] == "off" and s["pressure_psi"] == 48.95
    assert s["total_gal"] == 123456.78 and s["valve"] == "opened" and s["plumbing_check"] is None
    assert "172.0.0.1" not in json.dumps(s) and "hidden" not in json.dumps(s)
    attrs["product"]["sensor_flow"] = 2.4567
    assert p2m.state_of(attrs, 100.0)["flow_gpm"] == 2.457


def test_home_assistant_discovery_is_read_only_sensors():
    info = {"serial": "S1", "model": "PP2", "firmware": "4.9", "mac": "28:F5:37:00:00:01"}
    configs = p2m.discovery("phyn/S1", info)
    topics = [t for t, _ in configs]
    assert all(t.startswith("homeassistant/sensor/phyn_S1/") and t.endswith("/config") for t in topics)
    by_key = {c["unique_id"].removeprefix("phyn_S1_"): c for _, c in configs}
    assert by_key["total_gal"]["state_class"] == "total_increasing" and by_key["total_gal"]["device_class"] == "water"
    assert by_key["flow_gpm"]["value_template"] == "{{ value_json.flow_gpm }}"
    assert by_key["wifi_rssi"]["entity_category"] == "diagnostic"
    assert not any("command_topic" in c for _, c in configs)    # nothing in HA can act through it
    assert set(by_key) <= set(p2m.state_of({"product": {}, "stats": {}}, 0))

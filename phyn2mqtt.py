"""phyn2mqtt: a Phyn Plus water meter's readings, read on the LAN and published on MQTT.

The Phyn Plus answers JNAP on port 80 (unofficial, fixed credentials). This
poller sends only the two read actions below, every PHYN_INTERVAL seconds,
and publishes a chosen set of readings, retained:

  phyn/<serial>/state         {"flow_gpm", "flow_state", "pressure_psi", "temperature_f", "total_gal", "valve", …}
  phyn/<serial>/info          {"model", "firmware", "mac", "name"}
  phyn/<serial>/availability  online | offline

It is read-only by construction: an action not in READ_ACTIONS is refused
before anything is sent, and it subscribes to nothing, so no message can make
it act. The device's shutoff valve is never touched from here. Nothing from
the device's "system" section (cloud settings, addresses) is published.

With PHYN_HA_DISCOVERY=1 it also publishes Home Assistant MQTT discovery for
read-only sensors (flow, pressure, temperature, total, valve state, the leak
and freeze watches, Wi-Fi), so Home Assistant shows the meter with no
integration. There is no valve switch, by design.

Firmware 4.9.0.23 sent malformed HTTP for attribute/get (a debug line in the
headers, a second response after the body), so the reply is read from the raw
socket and parsed leniently: header lines without a colon are skipped and
exactly Content-Length bytes of body are taken.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

import aiomqtt

log = logging.getLogger("phyn2mqtt")

GET_DEVICE_INFO = "http://phyn.com/jnap/core/GetDeviceInfo"
GET_ATTRIBUTES = "http://phyn.com/jnap/attribute/get"
READ_ACTIONS = frozenset({GET_DEVICE_INFO, GET_ATTRIBUTES})
AUTH = "Basic " + base64.b64encode(b"admin:admin").decode()   # the firmware's own fixed local login
INFO_EVERY = 3600.0
MAX_BACKOFF = 60.0


class Refused(ValueError):
    """An action that is not a read: never sent."""


def request_bytes(host: str, action: str) -> bytes:
    if action not in READ_ACTIONS:
        raise Refused(f"phyn2mqtt only reads; {action!r} is not sent")
    body = b"{}"
    head = (f"POST /JNAP/ HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
            f"X-JNAP-Action: {action}\r\nX-JNAP-Authorization: {AUTH}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
    return head.encode() + body


def parse_response(raw: bytes) -> dict:
    """The JSON body of one HTTP reply, read leniently (see the module notes)."""
    head, sep, rest = raw.partition(b"\r\n\r\n")
    if not sep:
        raise ValueError("no end of headers in the reply")
    lines = head.decode(errors="replace").split("\r\n")
    status = lines[0].split(" ")
    if len(status) < 2 or status[1] != "200":
        raise ValueError(f"the Phyn answered {lines[0]!r}")
    length = None
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon and name.strip().lower() == "content-length":
            length = int(value.strip())
    body = rest[:length] if length is not None else rest
    data = json.loads(body)
    if not isinstance(data, dict) or data.get("result") != "OK":
        raise ValueError(f"the Phyn refused: {str(data)[:200]}")
    return data


async def jnap(host: str, action: str, timeout: float = 8.0) -> dict:
    payload = request_bytes(host, action)   # refuses anything but a read, before connecting
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 80), timeout)
    try:
        writer.write(payload)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), timeout)
    finally:
        writer.close()
    return parse_response(raw)


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value != "<unset>" else None


def state_of(attrs: dict, now: float) -> dict:
    """The readings worth keeping, from attribute/get's product and stats sections."""
    p = attrs.get("product") or {}
    s = attrs.get("stats") or {}
    flow = _num(p.get("sensor_flow"))
    return {
        "ts": now,
        # A still meter reads a hair either side of zero.
        "flow_gpm": None if flow is None else (0.0 if abs(flow) < float(p.get("sensor_flow_low_to_off") or 0.01) else round(flow, 3)),
        "flow_state": _text(p.get("sensor_flow_state")),
        "pressure_psi": _num(p.get("sensor_pressure_1")),
        "pressure_avg_psi": _num(p.get("ml_oor_pressure_win_average")),
        "pressure_normal_min_psi": _num(p.get("ml_oor_pressure_state_normal_min")),
        "pressure_normal_max_psi": _num(p.get("ml_oor_pressure_state_normal_max")),
        "temperature_f": _num(p.get("sensor_temperature_1")),
        "total_gal": _num(p.get("consumption_total")),
        "valve": _text(p.get("sov_state_str")),
        "valve_close_count": _num(p.get("sov_close_count")),
        "leak_detector": _text(p.get("ml_offline_leak_detector_state_str")),
        "freeze_state": _text(p.get("alert_notifier_fp_state_str")),
        "oor_alert_state": _num(p.get("ml_oor_alert_state")),
        "pressure_state": _num(p.get("ml_oor_pressure_state")),
        "flow_alert_state": _num(p.get("ml_oor_flow_state")),
        "plumbing_check": _text(p.get("ml_plumbing_check_state_str")),
        "wifi_rssi": _num(s.get("wifi_rssi_avg")),
        "uptime_s": _num(s.get("device_up_time_sec")),
        "cloud": _text(s.get("connection_state_str")),
    }


# Home Assistant sensors: key in the state JSON -> (name, unit, device class, state class, icon, diagnostic).
HA_SENSORS = {
    "flow_gpm": ("Flow", "gal/min", "volume_flow_rate", "measurement", None, False),
    "pressure_psi": ("Pressure", "psi", "pressure", "measurement", None, False),
    "temperature_f": ("Water temperature", "°F", "temperature", "measurement", None, False),
    "total_gal": ("Total water", "gal", "water", "total_increasing", None, False),
    "valve": ("Valve", None, None, None, "mdi:valve", False),
    "flow_state": ("Flow state", None, None, None, "mdi:waves", False),
    "leak_detector": ("Leak watch", None, None, None, "mdi:water-alert", False),
    "freeze_state": ("Freeze watch", None, None, None, "mdi:snowflake-alert", False),
    "wifi_rssi": ("Wi-Fi signal", "dBm", "signal_strength", "measurement", None, True),
    "uptime_s": ("Uptime", "s", "duration", "measurement", None, True),
}


def discovery(topic: str, info: dict, prefix: str = "homeassistant") -> list[tuple[str, dict]]:
    """(config topic, payload) for each read-only sensor, for Home Assistant's MQTT discovery."""
    serial = info["serial"]
    device = {"identifiers": [f"phyn_{serial}"], "name": f"Phyn {info.get('model') or ''}".strip(), "manufacturer": "Phyn",
              "model": info.get("model"), "sw_version": info.get("firmware"), "serial_number": serial,
              "connections": [["mac", info["mac"]]] if info.get("mac") else []}
    out = []
    for key, (name, unit, dev_class, state_class, icon, diagnostic) in HA_SENSORS.items():
        config = {"name": name, "unique_id": f"phyn_{serial}_{key}", "object_id": f"phyn_{key}",
                  "state_topic": f"{topic}/state", "value_template": f"{{{{ value_json.{key} }}}}",
                  "availability_topic": f"{topic}/availability", "device": device}
        for field, value in (("unit_of_measurement", unit), ("device_class", dev_class), ("state_class", state_class), ("icon", icon)):
            if value:
                config[field] = value
        if diagnostic:
            config["entity_category"] = "diagnostic"
        out.append((f"{prefix}/sensor/phyn_{serial}/{key}/config", config))
    return out


def info_of(out: dict) -> dict:
    return {"model": out.get("productCode"), "firmware": out.get("firmwareVersion"), "mac": out.get("deviceID"),
            "name": out.get("deviceName"), "serial": out.get("serialNumber")}


async def run(host: str, interval: float, mqtt: dict, base: str, ha_discovery: bool = False,
              ha_prefix: str = "homeassistant") -> None:
    info = info_of((await jnap(host, GET_DEVICE_INFO)).get("output") or {})
    serial = info.get("serial")
    if not serial:
        raise SystemExit("the Phyn did not give a serial number")
    topic = f"{base}/{serial}"
    will = aiomqtt.Will(f"{topic}/availability", "offline", retain=True)
    async with aiomqtt.Client(hostname=mqtt["host"], port=mqtt["port"], username=mqtt["user"], password=mqtt["password"],
                              identifier=f"phyn2mqtt-{serial}", will=will) as client:
        await client.publish(f"{topic}/info", json.dumps(info), retain=True)
        if ha_discovery:
            for config_topic, config in discovery(topic, info, ha_prefix):
                await client.publish(config_topic, json.dumps(config), retain=True)
        log.info("publishing %s (%s, %s) every %.0f s", topic, info.get("model"), info.get("firmware"), interval)
        info_at, failures, online = time.monotonic(), 0, None
        while True:
            try:
                if time.monotonic() - info_at > INFO_EVERY:
                    fresh = info_of((await jnap(host, GET_DEVICE_INFO)).get("output") or {})
                    if fresh.get("serial") != serial:
                        raise ValueError(f"{host} now answers as {fresh.get('serial')}, not {serial}")
                    await client.publish(f"{topic}/info", json.dumps(fresh), retain=True)
                    info_at = time.monotonic()
                state = state_of(await jnap(host, GET_ATTRIBUTES), time.time())
                await client.publish(f"{topic}/state", json.dumps(state), retain=True)
                if online is not True:
                    await client.publish(f"{topic}/availability", "online", retain=True)
                    online, failures = True, 0
                await asyncio.sleep(interval)
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                failures += 1
                if online is not False:
                    log.warning("the Phyn is not answering (%s); retrying", exc)
                    await client.publish(f"{topic}/availability", "offline", retain=True)
                    online = False
                await asyncio.sleep(min(MAX_BACKOFF, interval * 2 ** min(failures, 6)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host = os.environ["PHYN_HOST"].strip()
    mqtt = {"host": os.environ["MQTT_HOST"], "port": int(os.environ.get("MQTT_PORT", "1883")),
            "user": os.environ.get("MQTT_USERNAME") or None, "password": os.environ.get("MQTT_PASSWORD") or None}
    while True:
        try:
            asyncio.run(run(host, float(os.environ.get("PHYN_INTERVAL", "10")), mqtt, os.environ.get("PHYN_TOPIC", "phyn"),
                            os.environ.get("PHYN_HA_DISCOVERY", "0").strip().lower() in ("1", "true", "yes", "on"),
                            os.environ.get("PHYN_HA_PREFIX", "homeassistant")))
        except (OSError, ValueError, aiomqtt.MqttError) as exc:
            log.warning("restarting in 30 s: %s", exc)
            time.sleep(30)


if __name__ == "__main__":
    main()

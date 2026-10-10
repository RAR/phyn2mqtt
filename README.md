# phyn2mqtt

Reads a **Phyn Plus** smart water meter on your local network and publishes its
readings to MQTT: flow, pressure, water temperature, total gallons, valve state
and the leak and freeze watches. Optionally it publishes **Home Assistant MQTT
discovery** too, so the meter shows up in Home Assistant with no cloud and no
custom integration.

It is **read-only by construction**. It can only send the meter's two read
requests, it subscribes to nothing, and there is no valve control anywhere in
it. Nothing you publish on MQTT can make it act, and nothing it publishes can
close your water.

## What it publishes

Every `PHYN_INTERVAL` seconds (default 10), retained:

| Topic | Payload |
|---|---|
| `phyn/<serial>/state` | JSON: `flow_gpm`, `flow_state`, `pressure_psi`, `pressure_avg_psi`, `pressure_normal_min_psi`, `pressure_normal_max_psi`, `temperature_f`, `total_gal`, `valve`, `valve_close_count`, `leak_detector`, `freeze_state`, `oor_alert_state`, `pressure_state`, `flow_alert_state`, `plumbing_check`, `wifi_rssi`, `uptime_s`, `cloud`, `ts` |
| `phyn/<serial>/info` | JSON: `model`, `firmware`, `mac`, `name`, `serial` |
| `phyn/<serial>/availability` | `online` / `offline` (also the MQTT last will) |

Example state:

```json
{"flow_gpm": 0.0, "flow_state": "off", "pressure_psi": 48.98, "pressure_normal_min_psi": 40.04,
 "pressure_normal_max_psi": 52.8, "temperature_f": 78.23, "total_gal": 123456.78, "valve": "opened",
 "valve_close_count": 74, "leak_detector": "enabled", "freeze_state": "normal", "wifi_rssi": -34}
```

A still meter reads a hair either side of zero; flow below the meter's own
off threshold is published as `0.0`. Nothing from the device's `system`
section (cloud settings, network addresses) is published.

With `PHYN_HA_DISCOVERY=1`, Home Assistant gets a "Phyn PP2" device with
sensors for flow, pressure, water temperature, total water (usable in the
Energy dashboard's water section), valve, flow state, leak watch, freeze
watch, and Wi-Fi signal and uptime as diagnostics. All sensors; no switches,
no valve entity.

## Run it

```sh
docker build -t phyn2mqtt .
docker run -d --restart unless-stopped \
  -e PHYN_HOST=192.168.1.50 -e MQTT_HOST=mqtt.local \
  -e MQTT_USERNAME=phyn2mqtt -e MQTT_PASSWORD=change-me \
  -e PHYN_HA_DISCOVERY=1 phyn2mqtt
```

or see `docker-compose.example.yml`, or run it directly with Python 3.11+:
`pip install -r requirements.txt && PHYN_HOST=… MQTT_HOST=… python phyn2mqtt.py`.

| Variable | Default | |
|---|---|---|
| `PHYN_HOST` | (required) | the meter's IP address; give it a DHCP reservation |
| `PHYN_INTERVAL` | `10` | seconds between reads |
| `PHYN_TOPIC` | `phyn` | base MQTT topic |
| `PHYN_HA_DISCOVERY` | `0` | `1` to publish Home Assistant discovery |
| `PHYN_HA_PREFIX` | `homeassistant` | Home Assistant's discovery prefix |
| `MQTT_HOST`, `MQTT_PORT` | (required), `1883` | the broker |
| `MQTT_USERNAME`, `MQTT_PASSWORD` | none | broker login |

If the meter stops answering it publishes `offline`, backs off up to a minute
between tries, and carries on when the meter returns. At start-up and hourly it
checks the device's serial number, so it will not publish another device's
readings under the wrong topic if an address changes.

## How it talks to the meter

The Phyn Plus runs a small web server on port 80 that speaks **JNAP** (the
JSON protocol Belkin/Linksys routers use; Phyn began as a Belkin/Uponor joint
venture). Every call is `POST /JNAP/` with the action in an `X-JNAP-Action`
header and the firmware's fixed local login in `X-JNAP-Authorization`.
phyn2mqtt sends only:

- `http://phyn.com/jnap/core/GetDeviceInfo`: model, firmware, MAC, serial
- `http://phyn.com/jnap/attribute/get` (body `{}`): the telemetry

These two are an allow-list checked before any connection is made; anything
else raises an error instead of being sent. The meter keeps its cloud
connection: this runs alongside the Phyn app and any cloud integration.

Firmware 4.9.0.23 returns malformed HTTP for `attribute/get` (a debug line in
the headers and a second response after the body), which strict HTTP clients
reject. phyn2mqtt reads the raw socket and takes exactly `Content-Length` bytes
of body, so it works on that firmware and on 4.9.0.28, which is clean.

**Caveats.** This is an unofficial interface on fixed credentials. A firmware
update could change or close it; if that happens phyn2mqtt reports `offline`
and nothing else breaks. Tested on a Phyn Plus PP2 with firmware 4.9.0.28. The
original Phyn Plus (PP1) may work but is untested; the Phyn Classic and the
battery water sensors have no local interface.

## Credits

The local JNAP interface was documented by others first:

- [trooperthorn/ha_int_phyn](https://github.com/trooperthorn/ha_int_phyn), whose
  [LOCAL_ACCESS.md](https://github.com/trooperthorn/ha_int_phyn/blob/main/LOCAL_ACCESS.md)
  collects the protocol, the firmware quirk and the risks
- [rplankenhorn/ha-phyn-local](https://github.com/rplankenhorn/ha-phyn-local),
  a LAN-only Home Assistant integration for the PP2, and its `PROTOCOL.md`

## Tests

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

They include checks that only the two read actions can be sent, that the valve
action appears nowhere in the code, that it never subscribes, and that the
discovery configs carry no command topics.

## License

MIT, see `LICENSE`. Not affiliated with or endorsed by Phyn.

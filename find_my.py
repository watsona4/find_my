import json
import logging
import os
import os.path
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paramiko import SSHClient, WarningPolicy

# ---- Env ----
MQTT_HOST: str = str(os.environ.get("MQTT_HOST", ""))
MQTT_PORT: int = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME: str = str(os.environ.get("MQTT_USERNAME", ""))
MQTT_PASSWORD: str = str(os.environ.get("MQTT_PASSWORD", ""))

DISCOVERY_PREFIX: str = str(os.environ.get("DISCOVERY_PREFIX", "homeassistant"))
BASE_TOPIC: str = str(os.environ.get("BASE_TOPIC", "find_my"))
TZ: str = str(os.environ.get("TZ", "UTC"))

IOS_URL: str = str(os.environ.get("IOS_URL", ""))
IOS_USERNAME: str = "root"
IOS_KEYFILE: str = "/ssh/id_rsa"
KNOWN_HOSTS: str = "/ssh/known_hosts"
IOS_DEVICESPATH: str = "/private/var/mobile/Library/Caches/com.apple.findmy.fmipcore/Devices.data"
IOS_ITEMSPATH: str = "/private/var/mobile/Library/Caches/com.apple.findmy.fmipcore/Items.data"

APP_AVAIL = f"{BASE_TOPIC}/availability"

# expire_after recommendations (seconds)
EXPIRE_DEFAULT = 3600
EXPIRE_BY_CLASS = {
    "iPhone": 1800,
    "iPad": 1800,
    "Watch": 1800,
    "Mac": 7200,
    "AirTag": 21600,
    "Accessory": 21600,
}

logging.basicConfig(format="%(asctime)s [%(levelname)s]: %(message)s", level=logging.DEBUG)


def expire_for(model_or_class: str) -> int:
    for k, v in EXPIRE_BY_CLASS.items():
        if k.lower() in (model_or_class or "").lower():
            return v
    return EXPIRE_DEFAULT


def normalize_device_id(raw: str) -> str:
    return (raw or "").replace(":", "").replace("-", "").strip()


def pub(
    client: mqtt.Client,
    topic: str,
    payload: Any,
    qos: int = 0,
    retain: bool = False,
    label: str = "",
):
    """Publish with logging."""
    payload_str = payload if isinstance(payload, str) else json.dumps(payload)
    # Truncate if very long
    display_payload = (
        payload_str if len(payload_str) <= 200 else payload_str[:200] + "...[truncated]"
    )
    logging.info(
        "Publishing%s: topic='%s' qos=%s retain=%s payload=%s",
        f" ({label})" if label else "",
        topic,
        qos,
        retain,
        display_payload,
    )
    info = client.publish(topic, payload_str, qos=qos, retain=retain)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        logging.error("Publish failed rc=%s topic=%s", info.rc, topic)
    return info


def parse_item(obj: Dict[str, Any], tz: str) -> Optional[Dict[str, Any]]:
    # Items.data entries (AirTag, accessories). Must have location.
    try:
        logging.info("Found item named %s", obj["name"])
        identifier = obj["identifier"]
        name = obj["name"]
        manufacturer = obj["productType"]["productInformation"]["manufacturerName"]
        model = obj["productType"]["productInformation"]["modelName"]
        serial_number = obj.get("serialNumber", identifier)
        sw_version = obj["systemVersion"]
        address = (obj.get("address") or {}).get("mapItemFullAddress") or ""
        loc = obj["location"]
        if not loc:
            return None
        latitude = loc["latitude"]
        longitude = loc["longitude"]
        altitude = loc.get("altitude")
        v_acc = loc.get("verticalAccuracy")
        h_acc = loc.get("horizontalAccuracy")
        ts = datetime.fromtimestamp(int(loc["timeStamp"]) // 1000, timezone.utc).astimezone(
            ZoneInfo(tz)
        )
        battery_status = obj.get("batteryStatus")
        antenna_power = obj["productType"]["productInformation"].get("antennaPower")

        return {
            "device_id": normalize_device_id(serial_number),
            "unique_id": identifier,
            "name": name,
            "manufacturer": manufacturer,
            "model": model,
            "sw_version": sw_version,
            "address": address,
            "latitude": latitude,
            "longitude": longitude,
            "altitude": altitude,
            "vertical_accuracy": v_acc,
            "gps_accuracy": h_acc,
            "timestamp": ts,
            "battery_status": battery_status,
            "antenna_power": antenna_power,
            "raw": obj,
            "model_or_class": model,
        }
    except Exception:
        return None


def parse_device(obj: Dict[str, Any], tz: str) -> Optional[Dict[str, Any]]:
    # Devices.data entries (phones, macs, watches). Must be locationCapable and have location.
    try:
        logging.info("Found device named %s", obj["name"])
        if not obj.get("locationCapable"):
            return None

        identifier = obj["deviceDiscoveryId"]
        name = obj["name"]
        manufacturer = obj.get("deviceClass", "Apple")
        model = obj["deviceModel"]
        serial_number = identifier
        sw_version = obj.get("deviceDisplayName", "")
        address = (obj.get("address") or {}).get("mapItemFullAddress") or ""
        battery_status = obj.get("batteryStatus")

        data = {
            "device_id": normalize_device_id(serial_number),
            "unique_id": identifier,
            "name": name,
            "manufacturer": manufacturer,
            "model": model,
            "sw_version": sw_version,
            "address": address,
            "battery_status": battery_status,
            "antenna_power": None,
            "raw": obj,
            "model_or_class": manufacturer if manufacturer else model,
        }

        loc = obj.get("location")
        if loc and "latitude" in loc and "longitude" in loc:
            data.update({
                "latitude": loc["latitude"],
                "longitude": loc["longitude"],
                "altitude": loc.get("altitude"),
                "verical_accuracy": loc.get("verticalAccuracy"),
                "gps_accuracy": loc.get("horizontalAccuracy"),
                "timestamp": datetime.fromtimestamp(
                    int(loc["timeStamp"]) // 1000, timezone.utc
                ).astimezone(ZoneInfo(tz)),
            })

        return data

    except Exception:
        return None


def discovery_payload(entry: Dict[str, Any], expire_after: int) -> Tuple[str, Dict[str, Any]]:
    device_id = entry["device_id"]
    disc_topic = f"{DISCOVERY_PREFIX}/device_tracker/find_my/{device_id}/config"
    state_topic = f"{BASE_TOPIC}/devices/{device_id}/state"
    attr_topic = f"{BASE_TOPIC}/devices/{device_id}/attributes"
    avail_topic = f"{BASE_TOPIC}/devices/{device_id}/availability"
    config = {
        "unique_id": f"findmy_{device_id}",
        "name": entry["name"],
        "state_topic": state_topic,
        "json_attributes_topic": attr_topic,
        "availability_topic": avail_topic,
        "source_type": "gps",
        "icon": "mdi:map-marker",
        "expire_after": expire_after,
        "device": {
            "manufacturer": entry["manufacturer"],
            "model": entry["model"],
            "identifiers": device_id,
            "name": entry["name"],
            "sw_version": entry["sw_version"],
        },
    }
    return disc_topic, config


def publish_entry(mqtt_client: mqtt.Client, entry: Dict[str, Any]) -> None:
    device_id = entry["device_id"]
    state_topic = f"{BASE_TOPIC}/devices/{device_id}/state"
    attr_topic = f"{BASE_TOPIC}/devices/{device_id}/attributes"
    avail_topic = f"{BASE_TOPIC}/devices/{device_id}/availability"
    raw_topic = f"{BASE_TOPIC}/devices/{device_id}/raw"

    # mark device online for this update
    pub(mqtt_client, avail_topic, "online", qos=1, retain=True)

    # state: "None" so HA computes from zones when lat/lon are omitted
    pub(mqtt_client, state_topic, "None", qos=1, retain=False)

    # build attributes only for present, non-None fields
    attrs: Dict[str, Any] = {}
    for k in (
        "latitude",
        "longitude",
        "altitude",
        "vertical_accuracy",
        "gps_accuracy",
        "battery_status",
        "antenna_power",
        "address",
    ):
        v = entry.get(k)
        if v is not None:
            attrs[k] = v

    ts = entry.get("timestamp")
    if ts:
        # ts may already be aware; serialize safely
        attrs["timestamp"] = ts.isoformat()

    pub(mqtt_client, attr_topic, json.dumps(attrs, default=str), qos=1, retain=False)

    # optional full object for debugging
    raw = entry.get("raw")
    if raw is not None:
        pub(mqtt_client, raw_topic, json.dumps(raw, default=str), qos=0, retain=False)


def main():
    mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2)
    mqtt_client.enable_logger()
    if MQTT_USERNAME:
        mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    mqtt_client.will_set(APP_AVAIL, "offline", qos=1, retain=True)
    mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
    mqtt_client.loop_start()
    pub(mqtt_client, APP_AVAIL, "online", qos=1, retain=True)

    ssh_client = SSHClient()
    ssh_client.load_system_host_keys(filename=KNOWN_HOSTS)
    ssh_client.set_missing_host_key_policy(WarningPolicy)
    ssh_client.connect(IOS_URL, username=IOS_USERNAME, key_filename=IOS_KEYFILE)

    published_configs: set[str] = set()

    while True:
        published_any = False

        logging.info("Opening FindMy app")
        ssh_client.exec_command("open com.apple.findmy")
        time.sleep(5)

        sftp = ssh_client.open_sftp()
        logging.info("Downloading Items.data from iOS device")
        sftp.get(remotepath=IOS_ITEMSPATH, localpath=os.path.basename(IOS_ITEMSPATH))
        logging.info("Downloading Devices.data from iOS device")
        sftp.get(remotepath=IOS_DEVICESPATH, localpath=os.path.basename(IOS_DEVICESPATH))
        sftp.close()

        # Items
        logging.info("Extracting items data into JSON object")
        with open(os.path.basename(IOS_ITEMSPATH)) as f:
            items_data = json.load(f)

        logging.info("Number of Apple Find My items: %d", len(items_data))
        for obj in items_data:
            entry = parse_item(obj, TZ)
            if not entry:
                continue

            device_id = entry["device_id"]
            # discovery once per run per id
            if device_id not in published_configs:
                disc_topic, config = discovery_payload(entry, expire_for(entry["model_or_class"]))
                pub(mqtt_client, disc_topic, json.dumps(config), retain=True)
                published_configs.add(device_id)

            logging.info("Publishing item: %s", entry["name"])
            publish_entry(mqtt_client, entry)
            published_any = True

        # Devices
        logging.info("Extracting devices data into JSON object")
        with open(os.path.basename(IOS_DEVICESPATH)) as f:
            devices_data = json.load(f)

        logging.info("Number of Apple Find My devices: %d", len(devices_data))
        for obj in devices_data:
            entry = parse_device(obj, TZ)
            if not entry:
                continue

            device_id = entry["device_id"]
            if device_id not in published_configs:
                disc_topic, config = discovery_payload(entry, expire_for(entry["model_or_class"]))
                pub(mqtt_client, disc_topic, json.dumps(config), retain=True)
                published_configs.add(device_id)

            logging.info("Publishing device: %s", entry["name"])
            publish_entry(mqtt_client, entry)
            published_any = True

        # heartbeat only if at least one device was published
        if published_any:
            Path("/tmp/last_cycle").write_text(str(int(time.time())))

        # optional: bump another app to keep location fresh
        logging.info("Opening Weather app")
        ssh_client.exec_command("open com.apple.weather")
        time.sleep(5)


if __name__ == "__main__":
    main()

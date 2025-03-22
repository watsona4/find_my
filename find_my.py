import json
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paramiko import AutoAddPolicy, SSHClient

MQTT_HOST: str = str(os.environ.get("MQTT_HOST", ""))
MQTT_PORT: int = int(os.environ.get("MQTT_PORT", 1883))

TZ: str = str(os.environ.get("TZ", "UTC"))

IOS_URL: str = str(os.environ.get("IOS_URL", ""))
IOS_USERNAME: str = "root"
IOS_KEYFILE: str = "/ssh/id_rsa"
KNOWN_HOSTS: str = "/ssh/known_hosts"
IOS_DATAPATH: str = "/private/var/mobile/Library/Caches/com.apple.findmy.fmipcore/Items.data"

logging.basicConfig(format="%(asctime)s [%(levelname)s]: %(message)s", level=logging.DEBUG)


def main():

    mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2)
    mqtt_client.enable_logger()

    mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
    mqtt_client.loop_start()

    ssh_client = SSHClient()
    ssh_client.load_system_host_keys(filename=KNOWN_HOSTS)
    ssh_client.set_missing_host_key_policy(AutoAddPolicy)

    while True:

        ssh_client.connect(IOS_URL, username=IOS_USERNAME, key_filename=IOS_KEYFILE)

        sftp = ssh_client.open_sftp()

        logging.info("Downloading Items.data from iOS device")
        sftp.get(remotepath=IOS_DATAPATH, localpath="Items.data")

        ssh_client.close()

        logging.info("Extracting data into JSON object")
        with open("Items.data") as datafile:
            data = json.load(datafile)

        logging.info("Number of Apple Find My objects to process: %d", len(data))
        for obj in data:

            logging.info("Gathering data for next Apple Find My object to process")

            logging.info("Data gathered, sending to MQTT broker %s", MQTT_HOST)
            identifier = obj["identifier"]
            name = obj["name"]
            manufacturer = obj["productType"]["productInformation"]["manufacturerName"]
            model = obj["productType"]["productInformation"]["modelName"]
            serial_number = obj["serialNumber"]
            sw_version = obj["systemVersion"]

            latitude = obj["location"]["latitude"]
            longitude = obj["location"]["longitude"]
            altitude = obj["location"]["altitude"]
            vertical_accuracy = obj["location"]["verticalAccuracy"]
            horizontal_accuracy = obj["location"]["horizontalAccuracy"]
            timestamp = datetime.fromtimestamp(
                int(obj["location"]["timeStamp"]) // 1000, timezone.utc
            ).astimezone(ZoneInfo(TZ))

            battery_status = obj["batteryStatus"]
            antenna_power = obj["productType"]["productInformation"]["antennaPower"]

            topic_base = f"homeassistant/device_tracker/findmy_{serial_number}/"

            config_topic = topic_base + "config"
            state_topic = topic_base + "state"
            attributes_topic = topic_base + "attributes"
            data_topic = topic_base + "data"

            config_data = {
                "unique_id": identifier,
                "name": "Tracker",
                "state_topic": state_topic,
                "json_attributes_topic": attributes_topic,
                "device": {
                    "manufacturer": manufacturer,
                    "model_id": model,
                    "identifiers": serial_number,
                    "name": name,
                    "sw_version": sw_version,
                },
            }

            state = "None"

            attributes = {
                "latitude": latitude,
                "longitude": longitude,
                "altitude": altitude,
                "vertical_accuracy": vertical_accuracy,
                "gps_accuracy": horizontal_accuracy,
                "battery_status": battery_status,
                "antenna_power": antenna_power,
                "timestamp": timestamp.strftime("%c"),
            }

            logging.info("Sending MQTT data of Apple Find My object: %s", name)

            mqtt_client.publish(config_topic, json.dumps(config_data), retain=True)
            mqtt_client.publish(state_topic, state, retain=True)
            mqtt_client.publish(attributes_topic, json.dumps(attributes), retain=True)
            mqtt_client.publish(data_topic, json.dumps(obj), retain=True)

        time.sleep(60)


if __name__ == "__main__":
    main()

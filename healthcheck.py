#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
import socket
import paho.mqtt.client as mqtt
import paramiko


def fail(msg):
    print(msg)
    sys.exit(1)


# 1) recent cycle
p = Path("/tmp/last_cycle")
if not p.exists():
    fail("no heartbeat")
try:
    last = int(p.read_text().strip())
except Exception as e:
    fail(f"bad heartbeat: {e}")
# your loop opens apps, downloads, parses, publishes; allow generous slack
if time.time() - last > 900:  # > 15 minutes since last successful cycle
    fail("stale heartbeat")

# 2) MQTT reachable
host = os.getenv("MQTT_HOST", "")
port = int(os.getenv("MQTT_PORT", "1883"))
user = os.getenv("MQTT_USERNAME", "")
pwd = os.getenv("MQTT_PASSWORD", "")
if not host:
    fail("no MQTT_HOST")

c = mqtt.Client()
if user:
    c.username_pw_set(user, pwd)
try:
    c.connect(host, port, 10)
    c.disconnect()
except Exception as e:
    fail(f"mqtt down: {e}")

# 3) iOS reachable over SSH
ios = os.getenv("IOS_URL", "")
keyfile = os.getenv("IOS_KEYFILE", "/ssh/id_rsa")
if not ios:
    fail("no IOS_URL")
try:
    # quick TCP check first
    with socket.create_connection((ios, 22), timeout=5):
        pass
    # optional fast auth handshake
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.WarningPolicy)
    ssh.load_system_host_keys(filename=os.getenv("KNOWN_HOSTS", "/ssh/known_hosts"))
    ssh.connect(ios, username=os.getenv("IOS_USERNAME", "root"), key_filename=keyfile, timeout=8)
    ssh.close()
except Exception as e:
    fail(f"ssh down: {e}")

sys.exit(0)

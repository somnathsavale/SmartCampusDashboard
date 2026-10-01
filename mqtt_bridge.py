"""Forward Arduino USB serial lines to the hosted dashboard over MQTT."""

import json
import os
import threading
import time
import urllib.error
import urllib.request

import paho.mqtt.client as mqtt
import serial


SERIAL_BAUD = int(os.environ.get("SMART_BAUD", "9600"))
MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "smart-campus/telemetry")
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TLS = os.environ.get("MQTT_TLS", "0") == "1"
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
ARDUINO_BRIDGE_TOKEN = os.environ.get("ARDUINO_BRIDGE_TOKEN", "")
BOARD_PORTS = {
    "irrigation": os.environ.get("ARDUINO_1_PORT", "COM5"),
    "canteen": os.environ.get("ARDUINO_1_PORT", "COM5"),
    "dustbin": os.environ.get("ARDUINO_2_PORT", "COM6"),
    "classroom": os.environ.get("ARDUINO_2_PORT", "COM6"),
}
connections = {}
connections_lock = threading.Lock()


def configure_mqtt_client(client):
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    if MQTT_TLS:
        client.tls_set()


def dashboard_request(path, payload=None, method=None):
    if not DASHBOARD_URL or not ARDUINO_BRIDGE_TOKEN:
        raise RuntimeError("Set DASHBOARD_URL and ARDUINO_BRIDGE_TOKEN for HTTP mode")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{DASHBOARD_URL}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {ARDUINO_BRIDGE_TOKEN}",
            "Content-Type": "application/json",
        },
        method=method or ("POST" if data is not None else "GET"),
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response_data = response.read()
        return json.loads(response_data.decode("utf-8")) if response_data else {}


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(f"{MQTT_TOPIC}/commands/+")
        print("Connected to MQTT broker; listening for dashboard commands")
    else:
        print(f"MQTT connection failed: {reason_code}")


def on_message(client, userdata, message):
    board = message.topic.rsplit("/", 1)[-1]
    port = BOARD_PORTS.get(board)
    if port is None:
        return
    command = message.payload.decode("utf-8", errors="replace").strip()
    if command != "EMPTY" and not command.startswith("DISPLAY:"):
        print(f"Ignored unsupported command for {board}: {command}")
        return
    if command.startswith("DISPLAY:") and not command.removeprefix("DISPLAY:").isdigit():
        print(f"Ignored invalid display command: {command}")
        return
    with connections_lock:
        connection = connections.get(port)
        if connection is None or not connection.is_open:
            print(f"Cannot deliver command; {port} is not connected")
            return
        connection.write(f"{command}\n".encode("utf-8"))


def serial_worker(port, boards, client):
    while True:
        try:
            with serial.Serial(port, SERIAL_BAUD, timeout=1) as connection:
                with connections_lock:
                    connections[port] = connection
                print(f"Arduino connected on {port}: {', '.join(boards)}")
                while True:
                    raw = connection.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    for board in boards:
                        try:
                            if DASHBOARD_URL:
                                dashboard_request(
                                    "/api/bridge/telemetry",
                                    {"board": board, "line": line},
                                )
                            elif client is not None:
                                result = client.publish(
                                    f"{MQTT_TOPIC}/serial/{board}",
                                    json.dumps({"line": line}),
                                )
                                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                                    print(f"MQTT publish failed for {board}: {result.rc}")
                        except Exception as exc:
                            print(f"Could not forward {board} telemetry: {exc}")
        except Exception as exc:
            print(f"Serial connection {port} unavailable: {exc}; retrying in 4 seconds")
        finally:
            with connections_lock:
                connections.pop(port, None)
        time.sleep(4)


def command_worker():
    port = BOARD_PORTS["canteen"]
    while True:
        try:
            with connections_lock:
                connection = connections.get(port)
                if connection is not None and connection.is_open:
                    response = dashboard_request("/api/bridge/commands?board=canteen")
                    for item in response.get("commands", []):
                        command = item.get("command", "")
                        if command != "EMPTY" and not command.startswith("DISPLAY:"):
                            continue
                        if command.startswith("DISPLAY:") and not command.removeprefix("DISPLAY:").isdigit():
                            continue
                        connection.write(f"{command}\n".encode("utf-8"))
                        dashboard_request(
                            f"/api/bridge/commands/{int(item['id'])}/ack",
                            method="POST",
                        )
        except Exception as exc:
            print(f"Could not check dashboard commands: {exc}")
        time.sleep(1)


def main():
    if DASHBOARD_URL and not ARDUINO_BRIDGE_TOKEN:
        raise SystemExit("Set ARDUINO_BRIDGE_TOKEN for HTTP mode.")
    if not DASHBOARD_URL and not MQTT_HOST:
        raise SystemExit("Set DASHBOARD_URL for HTTP mode or MQTT_HOST for MQTT mode.")

    grouped_ports = {}
    for board, port in BOARD_PORTS.items():
        if not port:
            continue
        grouped_ports.setdefault(port, []).append(board)

    client = None
    if not DASHBOARD_URL:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        configure_mqtt_client(client)
        client.on_connect = on_connect
        client.on_message = on_message
        client.connect(MQTT_HOST, MQTT_PORT, 30)
        client.loop_start()

    for port, boards in grouped_ports.items():
        threading.Thread(target=serial_worker, args=(port, boards, client), daemon=True).start()
    if DASHBOARD_URL:
        threading.Thread(target=command_worker, daemon=True).start()

    print("Arduino bridge is running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping Arduino MQTT bridge")
    finally:
        if client is not None:
            client.loop_stop()
            client.disconnect()


if __name__ == "__main__":
    main()
import os
import threading
import time

import serial
from serial.tools import list_ports
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# ==========================================
# ARDUINO CONNECTION
# ==========================================

SERIAL_BAUD = int(os.environ.get("CANTEEN_BAUD", "9600"))
configured_port = os.environ.get("CANTEEN_PORT", "").strip()
arduino = None


def find_arduino_port():
    if configured_port:
        return configured_port

    candidates = []
    for port in list_ports.comports():
        description = (port.description or "").lower()
        device = port.device

        if "bluetooth" in description:
            continue

        if any(keyword in description for keyword in ["arduino", "usb serial", "usb serial device", "ch340", "cp210", "ftdi"]):
            candidates.append(device)

    if candidates:
        return candidates[0]

    for port in list_ports.comports():
        description = (port.description or "").lower()
        if "bluetooth" in description:
            continue
        return port.device

    return None


def connect_to_arduino():
    global arduino

    if arduino is not None and arduino.is_open:
        return True

    port = find_arduino_port()
    if not port:
        print("No Arduino port detected")
        return False

    try:
        arduino = serial.Serial(port, SERIAL_BAUD, timeout=1)
        time.sleep(2)
        print("Arduino connected on", port)
        return True
    except serial.SerialException as error:
        arduino = None
        print("Arduino connection error:", error)
        return False


# ==========================================
# ORDER DATA
# ==========================================

orders = []
next_token = 1

lock = threading.Lock()
state = {
    "canteen": {
        "queue": 0,
        "current_order": None,
        "orders_served": 0,
        "orders": [],
        "connected": False,
        "last_seen": None,
    },
    "system": {
        "serial_available": True,
        "auto_detect": True,
        "arduino_1": None,
        "arduino_2": None,
        "mqtt": False,
    },
}


def refresh_state():
    with lock:
        current = next((order for order in orders if order["status"] == "Serving"), None)
        state["canteen"]["queue"] = len(orders)
        state["canteen"]["current_order"] = current
        state["canteen"]["orders"] = list(orders)
        state["canteen"]["connected"] = arduino is not None and arduino.is_open
        state["canteen"]["last_seen"] = time.time()
        state["system"]["arduino_1"] = getattr(arduino, "port", None)
        state["system"]["arduino_2"] = None


# ==========================================
# SEND COMMAND TO ARDUINO
# ==========================================

def send_to_arduino(command):
    try:
        if arduino is None:
            return
        arduino.write((command + "\n").encode())
        print("Sent to Arduino:", command)
        refresh_state()
    except Exception as e:
        print("Arduino send error:", e)


# ==========================================
# ARDUINO SERIAL LISTENER
# ==========================================

def listen_to_arduino():

    global arduino
    global orders

    while True:

        try:

            if arduino is None:
                connect_to_arduino()
                time.sleep(1)
                continue

            if arduino.in_waiting > 0:

                message = arduino.readline().decode(
                    errors="ignore"
                ).strip()

                if message:
                    print("Arduino:", message)

                # ==================================
                # PUSH BUTTON PRESSED
                # ==================================

                if message == "NEXT":

                    with lock:

                        if len(orders) == 0:

                            print("Queue is empty")

                            send_to_arduino("EMPTY")

                        else:

                            # Current order becomes completed
                            orders[0]["status"] = "Completed"

                            print(
                                "Completed Token:",
                                orders[0]["token"]
                            )

                            # Find next waiting order
                            next_order = None

                            for order in orders:

                                if order["status"] == "Waiting":
                                    next_order = order
                                    break

                            if next_order:

                                next_order["status"] = "Serving"

                                print(
                                    "Now Serving:",
                                    next_order["token"]
                                )

                                # Display next token
                                send_to_arduino(
                                    f"DISPLAY:{next_order['token']}"
                                )

                                # Remove completed orders
                                orders = [
                                    order
                                    for order in orders
                                    if order["status"] != "Completed"
                                ]

                            else:

                                # No more orders
                                orders.clear()

                                send_to_arduino("EMPTY")

                                print("Queue is empty")

        except Exception as e:

            print("Arduino listener error:", e)
            if arduino is not None:
                try:
                    arduino.close()
                except Exception:
                    pass
            arduino = None

        time.sleep(0.05)


# ==========================================
# START ARDUINO LISTENER
# ==========================================

threading.Thread(
    target=listen_to_arduino,
    daemon=True
).start()


# ==========================================
# HOME PAGE
# ==========================================

@app.route("/", methods=["GET", "POST"])
def home():

    global next_token

    if request.method == "POST":

        name = request.form["name"]
        food = request.form["food"]

        with lock:

            order = {
                "token": next_token,
                "name": name,
                "food": food,
                "status": "Serving"
                if len(orders) == 0
                else "Waiting"
            }

            orders.append(order)

            token = next_token

            next_token += 1

            # If first order, show it immediately
            if order["status"] == "Serving":

                send_to_arduino(
                    f"DISPLAY:{token}"
                )

        print(
            f"New Order: Token {token} | "
            f"{name} | {food}"
        )

    refresh_state()

    with lock:

        current = None

        for order in orders:

            if order["status"] == "Serving":
                current = order
                break

    return render_template(
        "index.html",
        orders=orders,
        current=current
    )


@app.get("/api/state")
def api_state():
    refresh_state()
    return jsonify(state)


@app.get("/api/ports")
def api_ports():
    ports = []
    for port in list_ports.comports():
        ports.append({"device": port.device, "label": f"{port.device} - {port.description}"})
    return jsonify(ports)


@app.post("/api/canteen/orders")
def api_order():
    global next_token
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "Guest")).strip() or "Guest"
    food = str(data.get("food", "Meal")).strip() or "Meal"

    with lock:
        order = {
            "token": next_token,
            "name": name,
            "food": food,
            "status": "Serving" if not orders else "Waiting"
        }
        orders.append(order)
        token = next_token
        next_token += 1

        if order["status"] == "Serving":
            send_to_arduino(f"DISPLAY:{token}")

    refresh_state()
    return jsonify({"ok": True, "order": order, "queue": len(orders)})


@app.post("/api/canteen/next")
def api_next():
    global orders
    with lock:
        if not orders:
            send_to_arduino("EMPTY")
            refresh_state()
            return jsonify({"token": None})

        current = next((o for o in orders if o["status"] == "Serving"), None)
        if current:
            current["status"] = "Completed"
        orders = [o for o in orders if o["status"] != "Completed"]
        next_order = next((o for o in orders if o["status"] == "Waiting"), None)
        if next_order:
            next_order["status"] = "Serving"
            send_to_arduino(f"DISPLAY:{next_order['token']}")
        else:
            send_to_arduino("EMPTY")
    refresh_state()
    return jsonify({"token": next_order["token"] if next_order else None})


# ==========================================
# LIVE QUEUE API
# ==========================================

@app.route("/api/queue")
def queue_api():

    refresh_state()

    with lock:

        current = None

        for order in orders:

            if order["status"] == "Serving":
                current = order
                break

        return jsonify({
            "current": current,
            "orders": orders,
            "count": len(orders)
        })


@app.get("/api/history")
def api_history():
    refresh_state()
    return jsonify([])


# ==========================================
# RUN FLASK
# ==========================================

if __name__ == "__main__":

    app.run(
        debug=True,
        use_reloader=False
    )
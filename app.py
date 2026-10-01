import json
import hmac
import os
import re
import sqlite3
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

try:
	import serial
	from serial.tools import list_ports
except ImportError:
	serial = None
	list_ports = None

try:
	import paho.mqtt.client as mqtt
except ImportError:
	mqtt = None

app = Flask(__name__)
if os.environ.get("VERCEL") == "1":
	DB_PATH = os.path.join(tempfile.gettempdir(), "smart_dashboard.db")
else:
	DB_PATH = os.environ.get("SMART_DB", os.path.join(os.path.dirname(__file__), "smart_dashboard.db"))
CANTEEN_ORDER_URL = os.environ.get("CANTEEN_ORDER_URL", "http://127.0.0.1:8000").rstrip("/")
SERIAL_BAUD = int(os.environ.get("SMART_BAUD", "9600"))
MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "smart-campus/telemetry")
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TLS = os.environ.get("MQTT_TLS", "0") == "1"
SERIAL_ENABLED = os.environ.get("SMART_SERIAL_ENABLED", "1") != "0"
ARDUINO_BRIDGE_TOKEN = os.environ.get("ARDUINO_BRIDGE_TOKEN", "")
lock = threading.Lock()
serial_detection_lock = threading.Lock()
serial_detection_started = False
mqtt_subscriber_lock = threading.Lock()
mqtt_subscriber_started = False
mqtt_subscriber_client = None
serial_ports = {}
serial_threads = {}
serial_connections = {}
BOARD_GROUPS = {"irrigation": ("irrigation", "canteen"), "canteen": ("irrigation", "canteen"), "dustbin": ("dustbin", "classroom"), "classroom": ("dustbin", "classroom")}
FIXED_PORT_MAP = {"irrigation": "COM5", "canteen": "COM5", "dustbin": "COM6", "classroom": "COM6"}
logs = deque(maxlen=80)
orders = []
state = {
	"irrigation": {"soil_value": None, "soil_status": "Waiting", "pump": False, "connected": False, "last_seen": None},
	"canteen": {"queue": 0, "current_order": None, "orders_served": 0, "orders": [], "connected": False, "last_seen": None},
	"dustbin": {"fill": None, "distance": None, "sensor_status": "Waiting", "warning": False, "connected": False, "last_seen": None},
	"classroom": {"people": 0, "light": False, "fan": False, "connected": False, "last_seen": None},
}

def now_iso():
	return datetime.now(timezone.utc).isoformat(timespec="seconds")

@contextmanager
def database_connection():
	connection = sqlite3.connect(DB_PATH)
	try:
		yield connection
		connection.commit()
	except Exception:
		connection.rollback()
		raise
	finally:
		connection.close()

def db_init():
	with database_connection() as db:
		db.execute("CREATE TABLE IF NOT EXISTS readings (id INTEGER PRIMARY KEY, created_at TEXT, module TEXT, payload TEXT)")
		db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, created_at TEXT, level TEXT, module TEXT, message TEXT)")
		db.execute("CREATE TABLE IF NOT EXISTS canteen_orders (token INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, food TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)")
		db.execute("CREATE TABLE IF NOT EXISTS bridge_commands (id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT NOT NULL, command TEXT NOT NULL, created_at TEXT NOT NULL)")

def load_active_orders(db=None):
	if db is None:
		with database_connection() as connection:
			return load_active_orders(connection)
	rows = db.execute(
		"SELECT token, name, food, status FROM canteen_orders WHERE status IN ('Serving', 'Waiting') ORDER BY token"
	).fetchall()
	return [{"token": row[0], "name": row[1], "food": row[2], "status": row[3]} for row in rows]

def record(module, payload):
	with database_connection() as db:
		db.execute("INSERT INTO readings(created_at, module, payload) VALUES (?, ?, ?)", (now_iso(), module, json.dumps(payload)))

def add_event(level, module, message):
	item = {"time": now_iso(), "level": level, "module": module, "message": message}
	with lock:
		logs.appendleft(item)
	with database_connection() as db:
		db.execute("INSERT INTO events(created_at, level, module, message) VALUES (?, ?, ?, ?)", (item["time"], level, module, message))

def get_board_port(board):
	if serial_ports.get(board):
		return serial_ports[board]
	if board == "canteen":
		return serial_ports.get("irrigation")
	if board == "irrigation":
		return serial_ports.get("canteen")
	if board == "classroom":
		return serial_ports.get("dustbin")
	if board == "dustbin":
		return serial_ports.get("classroom")
	return None


def module_connection_summary(port_map=None, module_state=None):
	port_map = port_map or serial_ports
	def group_connected(modules):
		mapped = any(port_map.get(module) for module in modules)
		if module_state is None:
			return mapped
		return any(module_state.get(module, {}).get("connected", False) for module in modules)

	return {
		"irrigation": group_connected(("irrigation", "canteen")),
		"canteen": group_connected(("irrigation", "canteen")),
		"dustbin": group_connected(("dustbin", "classroom")),
		"classroom": group_connected(("dustbin", "classroom")),
	}


def apply_fixed_port_map():
	if serial is None:
		return []
	connected = []
	for module, port in FIXED_PORT_MAP.items():
		if module in serial_threads and serial_threads.get(module) is not None and serial_threads[module].is_alive():
			connected.append(module)
			continue
		if port and not any(item.device.lower() == port.lower() for item in list_ports.comports()):
			continue
		ok, message = start_serial(module, port)
		if ok:
			connected.append(module)
			add_event("info", "system", f"Linked {module} to {port}: {message}")
		else:
			add_event("warning", "system", f"Could not link {module} to {port}: {message}")
	return connected


def mqtt_publish(module, payload):
	if mqtt is None or not MQTT_HOST:
		return
	try:
		client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
		configure_mqtt_client(client)
		client.connect(MQTT_HOST, MQTT_PORT, 5)
		client.publish(f"{MQTT_TOPIC}/{module}", json.dumps(payload))
		client.disconnect()
	except Exception as exc:
		add_event("warning", "system", f"MQTT unavailable: {exc}")

def configure_mqtt_client(client):
	if MQTT_USERNAME:
		client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
	if MQTT_TLS:
		client.tls_set()

def mqtt_publish_command(board, command):
	if mqtt is None or not MQTT_HOST:
		return False
	try:
		client = mqtt_subscriber_client
		if client is not None and client.is_connected():
			result = client.publish(f"{MQTT_TOPIC}/commands/{board}", command)
			return result.rc == mqtt.MQTT_ERR_SUCCESS
		client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
		configure_mqtt_client(client)
		client.connect(MQTT_HOST, MQTT_PORT, 5)
		result = client.publish(f"{MQTT_TOPIC}/commands/{board}", command)
		client.disconnect()
		return result.rc == mqtt.MQTT_ERR_SUCCESS
	except Exception as exc:
		add_event("warning", "system", f"MQTT command unavailable: {exc}")
		return False

def handle_serial_line(board, line):
	if board not in BOARD_GROUPS or not isinstance(line, str) or not line.strip():
		return False
	line = line.strip()
	with lock:
		for module in BOARD_GROUPS[board]:
			state[module].update(connected=True, last_seen=now_iso())
	parse_line(line, board)
	return True

def handle_mqtt_serial_message(topic, payload):
	prefix = f"{MQTT_TOPIC}/serial/"
	if not topic.startswith(prefix):
		return False
	board = topic[len(prefix):]
	if board not in BOARD_GROUPS:
		return False
	try:
		message = json.loads(payload.decode("utf-8"))
		line = message.get("line", "") if isinstance(message, dict) else str(message)
	except (UnicodeDecodeError, json.JSONDecodeError):
		line = payload.decode("utf-8", errors="replace")
	line = str(line).strip()
	return handle_serial_line(board, line)

def bridge_authorized():
	provided = request.headers.get("Authorization", "")
	return bool(ARDUINO_BRIDGE_TOKEN) and hmac.compare_digest(
		provided, f"Bearer {ARDUINO_BRIDGE_TOKEN}"
	)

def enqueue_bridge_command(board, command):
	with database_connection() as db:
		db.execute(
			"INSERT INTO bridge_commands(board, command, created_at) VALUES (?, ?, ?)",
			(board, command, now_iso()),
		)

def start_mqtt_subscriber_once():
	global mqtt_subscriber_started, mqtt_subscriber_client
	if mqtt is None or not MQTT_HOST:
		return
	with mqtt_subscriber_lock:
		if mqtt_subscriber_started:
			return
		mqtt_subscriber_started = True
	try:
		client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
		configure_mqtt_client(client)
		client.on_connect = lambda connected_client, userdata, flags, reason, properties: (
			connected_client.subscribe(f"{MQTT_TOPIC}/serial/+") if reason == 0 else None
		)
		client.on_message = lambda connected_client, userdata, message: handle_mqtt_serial_message(
			message.topic, message.payload
		)
		client.connect_async(MQTT_HOST, MQTT_PORT, 30)
		client.loop_start()
		mqtt_subscriber_client = client
	except Exception as exc:
		add_event("warning", "system", f"Could not start MQTT listener: {exc}")

def update_module(module, values):
	with lock:
		state[module].update(values, connected=True, last_seen=now_iso())
		snapshot = dict(state[module])
	record(module, snapshot)
	mqtt_publish(module, snapshot)

def serve_next_order():
	global orders
	with lock:
		with database_connection() as db:
			db.execute("BEGIN IMMEDIATE")
			current = db.execute("SELECT token FROM canteen_orders WHERE status = 'Serving' ORDER BY token LIMIT 1").fetchone()
			if current:
				db.execute("UPDATE canteen_orders SET status = 'Completed' WHERE token = ?", (current[0],))
			waiting = db.execute("SELECT token FROM canteen_orders WHERE status = 'Waiting' ORDER BY token LIMIT 1").fetchone()
			if waiting:
				db.execute("UPDATE canteen_orders SET status = 'Serving' WHERE token = ?", (waiting[0],))
			orders = load_active_orders(db)
		next_order = next((order for order in orders if order["status"] == "Serving"), None)
		state["canteen"]["queue"] = len(orders)
		state["canteen"]["current_order"] = next_order
		state["canteen"]["orders"] = list(orders)
		if next_order:
			state["canteen"]["orders_served"] += 1
		port = get_board_port("canteen")
	if next_order:
		send_serial_command(port, f"DISPLAY:{next_order['token']}")
	elif port or ARDUINO_BRIDGE_TOKEN or (MQTT_HOST and mqtt):
		send_serial_command(port, "EMPTY")
	return next_order

def send_serial_command(port, command):
	connection = serial_connections.get(port)
	if connection is None:
		if mqtt_publish_command("canteen", command):
			return
		if ARDUINO_BRIDGE_TOKEN:
			enqueue_bridge_command("canteen", command)
			add_event("info", "canteen", "Queued Arduino command for the local bridge")
		else:
			add_event("warning", "canteen", f"Could not send command: {port} is not connected and MQTT is unavailable")
		return
	try:
		connection.write(f"{command}\n".encode())
	except Exception as exc:
		add_event("warning", "canteen", f"Could not send command: {exc}")

def parse_line(line, board):
	line = line.strip()
	if not line:
		return
	add_event("info", board, line)
	lower = line.lower()
	if board == "irrigation":
		match = re.search(r"soil value\s*=\s*(\d+)", lower)
		if match:
			value = int(match.group(1))
			update_module("irrigation", {"soil_value": value, "soil_status": "Dry" if value > 700 else "Medium" if value > 400 else "Wet", "pump": value > 700})
		elif "soil dry" in lower:
			update_module("irrigation", {"soil_status": "Dry"})
		elif "soil medium" in lower:
			update_module("irrigation", {"soil_status": "Medium"})
		elif "soil wet" in lower:
			update_module("irrigation", {"soil_status": "Wet"})
		elif "pump on" in lower:
			update_module("irrigation", {"pump": True})
		elif "pump off" in lower:
			update_module("irrigation", {"pump": False})
	elif board == "canteen" and "next" in lower:
		serve_next_order()
	elif board == "dustbin":
		if "dustbin sensor error" in lower:
			update_module("dustbin", {"sensor_status": "Error", "warning": False})
			return
		match = re.search(r"distance:\s*([\d.-]+).*fill:\s*(\d+)", lower)
		if match:
			fill = min(100, max(0, int(match.group(2))))
			with lock:
				was_warning = state["dustbin"]["warning"]
			warning = fill >= 80
			update_module("dustbin", {"distance": float(match.group(1)), "fill": fill, "sensor_status": "OK", "warning": warning})
			if warning and not was_warning:
				add_event("critical", "dustbin", f"Bin is {fill}% full")
	elif board == "classroom":
		values = {}
		match = re.search(r"people count:\s*(\d+)", lower)
		if match:
			values["people"] = int(match.group(1))
		match = re.search(r"\blight\s*:?\s*(on|off)\b", lower)
		if match:
			values["light"] = match.group(1) == "on"
		match = re.search(r"\bfan\s*:?\s*(on|off)\b", lower)
		if match:
			values["fan"] = match.group(1) == "on"
		if values:
			update_module("classroom", values)

def serial_worker(port, modules):
	while True:
		try:
			with serial.Serial(port, SERIAL_BAUD, timeout=1) as connection:
				serial_connections[port] = connection
				for module in modules:
					add_event("info", module, f"Connected on {port}")
				with lock:
					for module in modules:
						state[module]["connected"] = True
				while True:
					raw = connection.readline()
					if raw:
						line = raw.decode("utf-8", errors="replace")
						for module in modules:
							parse_line(line, module)
		except Exception as exc:
			with lock:
				for module in modules:
					state[module]["connected"] = False
			add_event("warning", modules[0], f"USB disconnected: {exc}")
			serial_connections.pop(port, None)
			time.sleep(4)

def start_serial(board, port):
	if serial is None:
		return False, "PySerial is not installed"
	if not port:
		return False, "Select a USB port first"
	modules = BOARD_GROUPS[board]
	current = serial_threads.get(modules[0])
	if current and current.is_alive():
		serial_ports[board] = port
		return True, "Already connected"
	thread = threading.Thread(target=serial_worker, args=(port, modules), daemon=True)
	for module in modules:
		serial_ports[module] = port
		serial_threads[module] = thread
	thread.start()
	return True, f"Connecting to {port}"

def detect_board_on_port(port):
	if serial is None:
		return
	seen = ""
	detected_board = None
	try:
		with serial.Serial(port, SERIAL_BAUD, timeout=1) as connection:
			deadline = time.time() + 35
			while time.time() < deadline:
				raw = connection.readline()
				if not raw:
					continue
				seen = f"{seen} {raw.decode('utf-8', errors='replace').lower()}"
				if re.search(r"soil value|soil dry|soil wet|pump on|pump off|displaying token|queue empty", seen):
					detected_board = "irrigation"
					break
				if re.search(r"dustbin|pir 1|pir 2|people count|classroom|system ready", seen):
					detected_board = "dustbin"
					break
	except Exception:
		return
	if detected_board:
		start_serial(detected_board, port)
		add_event("info", "system", f"Auto-linked {'Arduino 1' if detected_board == 'irrigation' else 'Arduino 2'} on {port}")

def auto_detect_boards():
	apply_fixed_port_map()
	if list_ports is None or serial is None:
		add_event("warning", "system", "Automatic board detection unavailable")
		return
	seen_ports = set()
	for port in [item.device for item in list_ports.comports()]:
		if not port or port in seen_ports:
			continue
		seen_ports.add(port)
		if port.upper() == "COM5":
			start_serial("irrigation", port)
			start_serial("canteen", port)
			continue
		if port.upper() == "COM6":
			start_serial("dustbin", port)
			start_serial("classroom", port)
			continue
		threading.Thread(target=detect_board_on_port, args=(port,), daemon=True).start()

def start_serial_detection_once():
	global serial_detection_started
	if not SERIAL_ENABLED:
		return
	with serial_detection_lock:
		if serial_detection_started:
			return
		serial_detection_started = True
	threading.Thread(target=auto_detect_boards, daemon=True).start()

@app.before_request
def start_serial_detection_for_flask():
	start_serial_detection_once()
	start_mqtt_subscriber_once()

@app.route("/")
def index():
	return render_template("index.html", canteen_order_url=CANTEEN_ORDER_URL)

@app.get("/api/state")
def api_state():
	global orders
	active_orders = load_active_orders()
	with lock:
		orders = active_orders
		state["canteen"]["queue"] = len(orders)
		state["canteen"]["current_order"] = next((order for order in orders if order["status"] == "Serving"), None)
		state["canteen"]["orders"] = list(orders)
		snapshot = json.loads(json.dumps(state))
		board_status = module_connection_summary(serial_ports, state)
		snapshot["system"] = {
			"mqtt": bool(mqtt_subscriber_client and mqtt_subscriber_client.is_connected()),
			"serial_available": serial is not None,
			"auto_detect": True,
			"arduino_1": serial_ports.get("irrigation") or serial_ports.get("canteen"),
			"arduino_2": serial_ports.get("dustbin") or serial_ports.get("classroom"),
			"connections": board_status,
		}
		snapshot["connections"] = board_status
		snapshot["alerts"] = list(logs)[:20]
	return jsonify(snapshot)

@app.get("/api/ports")
def api_ports():
	ports = [] if list_ports is None else [{"device": p.device, "label": f"{p.device} - {p.description}"} for p in list_ports.comports()]
	return jsonify(ports)

@app.post("/api/connect")
def api_connect():
	data = request.get_json(silent=True) or {}
	board = data.get("board")
	if board not in state:
		return jsonify({"error": "Unknown module"}), 400
	ok, message = start_serial(board, data.get("port", ""))
	return jsonify({"ok": ok, "message": message}), 200 if ok else 400

@app.post("/api/canteen/orders")
def api_order():
	global orders
	data = request.get_json(silent=True) or {}
	name = str(data.get("name", "Guest")).strip() or "Guest"
	food = str(data.get("food", "Meal")).strip() or "Meal"
	with lock:
		with database_connection() as db:
			db.execute("BEGIN IMMEDIATE")
			status = "Waiting" if db.execute("SELECT 1 FROM canteen_orders WHERE status IN ('Serving', 'Waiting') LIMIT 1").fetchone() else "Serving"
			cursor = db.execute(
				"INSERT INTO canteen_orders(name, food, status, created_at) VALUES (?, ?, ?, ?)",
				(name, food, status, now_iso()),
			)
			token = cursor.lastrowid
			order = {"token": token, "name": name, "food": food, "status": status}
			orders = load_active_orders(db)
		state["canteen"]["queue"] = len(orders)
		state["canteen"]["current_order"] = next((item for item in orders if item["status"] == "Serving"), None)
		state["canteen"]["orders"] = list(orders)
		port = get_board_port("canteen")
	if order["status"] == "Serving":
		send_serial_command(port, f"DISPLAY:{order['token']}")
	add_event("info", "canteen", f"Order {order['token']} added for {name}")
	return jsonify({"ok": True, "order": order, "queue": len(orders)})

@app.post("/api/bridge/telemetry")
def api_bridge_telemetry():
	if not bridge_authorized():
		return jsonify({"error": "Unauthorized"}), 401
	data = request.get_json(silent=True) or {}
	board = data.get("board")
	line = data.get("line")
	if board not in BOARD_GROUPS or not isinstance(line, str) or len(line) > 512:
		return jsonify({"error": "Invalid telemetry"}), 400
	if not handle_serial_line(board, line):
		return jsonify({"error": "Invalid telemetry"}), 400
	return jsonify({"ok": True})

@app.get("/api/bridge/commands")
def api_bridge_commands():
	if not bridge_authorized():
		return jsonify({"error": "Unauthorized"}), 401
	board = request.args.get("board", "")
	if board not in BOARD_GROUPS:
		return jsonify({"error": "Unknown board"}), 400
	with database_connection() as db:
		rows = db.execute(
			"SELECT id, command FROM bridge_commands WHERE board = ? ORDER BY id",
			(board,),
		).fetchall()
	return jsonify({"commands": [{"id": row[0], "command": row[1]} for row in rows]})

@app.post("/api/bridge/commands/<int:command_id>/ack")
def api_bridge_command_ack(command_id):
	if not bridge_authorized():
		return jsonify({"error": "Unauthorized"}), 401
	with database_connection() as db:
		cursor = db.execute("DELETE FROM bridge_commands WHERE id = ?", (command_id,))
	if cursor.rowcount == 0:
		return jsonify({"error": "Command not found"}), 404
	return jsonify({"ok": True})

@app.post("/api/canteen/next")
def api_next():
	return jsonify({"token": serve_next_order()})

@app.get("/api/history")
def api_history():
	with database_connection() as db:
		rows = db.execute("SELECT created_at, module, payload FROM readings ORDER BY id DESC LIMIT 60").fetchall()
	return jsonify([{"time": row[0], "module": row[1], "payload": json.loads(row[2])} for row in rows])

db_init()

if __name__ == "__main__":
	app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)

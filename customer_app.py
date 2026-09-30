import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, render_template, request

app = Flask(__name__)
DASHBOARD_URL = os.environ.get("CANTEEN_DASHBOARD_URL", "http://127.0.0.1:5000").rstrip("/")


def create_dashboard_order(name, food):
	order_request = Request(
		f"{DASHBOARD_URL}/api/canteen/orders",
		data=json.dumps({"name": name, "food": food}).encode("utf-8"),
		headers={"Content-Type": "application/json"},
		method="POST",
	)
	with urlopen(order_request, timeout=10) as response:
		return json.loads(response.read().decode("utf-8"))


@app.route("/", methods=["GET", "POST"])
def order_page():
	order = None
	error = None
	queue_ahead = 0
	status_code = 200

	if request.method == "POST":
		name = request.form.get("name", "").strip()
		food = request.form.get("food", "").strip()
		if not name or not food:
			error = "Enter your name and food item to place an order."
			status_code = 400
		elif len(name) > 80 or len(food) > 120:
			error = "Keep your name under 80 characters and food item under 120."
			status_code = 400
		else:
			try:
				result = create_dashboard_order(name, food)
				order = result.get("order")
				if not result.get("ok") or not order:
					error = "The canteen could not accept this order. Please try again."
					status_code = 502
				elif order["status"] == "Waiting":
					queue_ahead = max(0, int(result.get("queue", 1)) - 1)
			except (HTTPError, URLError, TimeoutError, ValueError):
				error = "The canteen service is temporarily unavailable. Please try again shortly."
				status_code = 502

	return render_template("order.html", order=order, error=error, queue_ahead=queue_ahead), status_code


if __name__ == "__main__":
	app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
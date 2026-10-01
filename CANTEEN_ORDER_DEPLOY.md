# Canteen customer ordering site

`customer_app.py` is a separate customer-facing Flask site. It forwards the existing `name` and `food` form fields to the dashboard's `/api/canteen/orders` endpoint. The dashboard creates the token and stores the order in its `canteen_orders` SQLite table using the same `{token, name, food, status}` structure consumed by its canteen queue.

## Run locally

Start the dashboard with `python app.py`, then in another terminal start the customer site:

```powershell
$env:CANTEEN_DASHBOARD_URL = "http://127.0.0.1:5000"
python customer_app.py
```

Open `http://127.0.0.1:8000` for the customer form. The dashboard uses `SMART_DB` for its database path and defaults to `smart_dashboard.db` in the project directory.

## Deploy as two services

Deploy the dashboard and customer app as separate web services from this repository. Use `python app.py` as the dashboard start command and `python customer_app.py` as the customer-site start command; both use the root `requirements.txt` and bind to the hosting provider's `PORT`.

Set `CANTEEN_DASHBOARD_URL` on the customer service to the dashboard's reachable service URL, without an API path. Set `SMART_DB` on the dashboard to a path on persistent storage provided by the host. Keep the customer service pointed at that dashboard service so orders continue to enter the dashboard's database and queue. SQLite requires persistent storage attached to the dashboard service; do not point the customer service at a second local database file.

On Vercel, the dashboard stores SQLite in the function's writable temporary directory because the deployment filesystem is read-only. Vercel temporary storage can be cleared on cold starts and is not durable; use a managed database or a host with a persistent disk if orders must survive restarts or scale across function instances.

For reliable orders and Arduino token display on Vercel, provision a managed PostgreSQL database and set its connection string as the dashboard's `DATABASE_URL` environment variable, then redeploy. The app creates its tables automatically and uses a transaction-level lock when advancing orders. Without `DATABASE_URL`, Vercel falls back to temporary SQLite, which is not shared reliably between serverless instances.

Set `CANTEEN_ORDER_URL` on the dashboard service to the customer service's public URL so the dashboard's Canteen tab opens the deployed order page. It defaults to `http://127.0.0.1:8000` for local development.

## Connect Arduino boards to the deployed dashboard

A cloud service cannot read USB serial ports on your computer. Run `mqtt_bridge.py` on the computer connected to the Arduino; HTTP mode forwards readings to the dashboard's public API and reconciles the TM1637 display to the current Serving order. It clears the display only after the queue stays empty for five seconds. No MQTT broker is needed for HTTP mode.

Configure the dashboard service with:

- `ARDUINO_BRIDGE_TOKEN`: a long random secret shared with the local bridge.
- `SMART_SERIAL_ENABLED=0`: disable USB scanning on the cloud host.

On the Arduino-connected computer, install the project requirements and set:

- `DASHBOARD_URL`: the deployed dashboard's base URL, without an API path.
- `ARDUINO_BRIDGE_TOKEN`: the same secret as the dashboard service.
- `ARDUINO_1_PORT` and `ARDUINO_2_PORT`: serial ports; defaults are `COM5` and `COM6`. Set an unused board's port to an empty value.

Then run `python mqtt_bridge.py` and leave it running. The Arduino sketch and bridge must use the same `SMART_BAUD`, which defaults to `9600`. Use HTTPS for the deployed dashboard URL, and do not commit the shared token to the repository.

MQTT mode is also available when `DASHBOARD_URL` is unset; set the same broker settings on both the dashboard and bridge in that case.
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

Set `CANTEEN_ORDER_URL` on the dashboard service to the customer service's public URL so the dashboard's Canteen tab opens the deployed order page. It defaults to `http://127.0.0.1:8000` for local development.
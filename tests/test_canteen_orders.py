import copy
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app as dashboard


class CanteenOrderPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.previous_orders = dashboard.orders
        self.previous_canteen_state = copy.deepcopy(dashboard.state["canteen"])
        self.db_path_patch = patch.object(
            dashboard, "DB_PATH", os.path.join(self.temp_directory.name, "test.db")
        )
        self.db_path_patch.start()
        dashboard.orders = []
        dashboard.db_init()
        self.client = dashboard.app.test_client()

    def tearDown(self):
        dashboard.orders = self.previous_orders
        dashboard.state["canteen"] = self.previous_canteen_state
        self.db_path_patch.stop()
        self.temp_directory.cleanup()

    def test_orders_keep_existing_shape_and_persist_through_serving(self):
        with patch.object(dashboard, "start_serial_detection_once"):
            first = self.client.post(
                "/api/canteen/orders", json={"name": "Alex", "food": "Soup"}
            )
            second = self.client.post(
                "/api/canteen/orders", json={"name": "Sam", "food": "Salad"}
            )
            next_order = self.client.post("/api/canteen/next")
            state_response = self.client.get("/api/state")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["order"], {
            "token": 1, "name": "Alex", "food": "Soup", "status": "Serving"
        })
        self.assertEqual(second.json["order"], {
            "token": 2, "name": "Sam", "food": "Salad", "status": "Waiting"
        })
        self.assertEqual(next_order.json["token"]["token"], 2)
        self.assertEqual(state_response.json["canteen"]["current_order"]["token"], 2)
        self.assertEqual(state_response.json["canteen"]["queue"], 1)

        db = sqlite3.connect(dashboard.DB_PATH)
        try:
            saved = db.execute(
                "SELECT token, name, food, status FROM canteen_orders ORDER BY token"
            ).fetchall()
        finally:
            db.close()
        self.assertEqual(saved, [(1, "Alex", "Soup", "Completed"), (2, "Sam", "Salad", "Serving")])


if __name__ == "__main__":
    unittest.main()
import unittest
from unittest.mock import patch

import customer_app


class CustomerOrderPageTests(unittest.TestCase):
    def test_order_submission_forwards_legacy_fields_and_shows_token(self):
        order = {"token": 27, "name": "Taylor", "food": "Noodles", "status": "Waiting"}
        with patch.object(
            customer_app,
            "create_dashboard_order",
            return_value={"ok": True, "order": order, "queue": 3},
        ) as create_order:
            response = customer_app.app.test_client().post(
                "/", data={"name": "Taylor", "food": "Noodles"}
            )

        self.assertEqual(response.status_code, 200)
        create_order.assert_called_once_with("Taylor", "Noodles")
        self.assertIn(b"#0027", response.data)
        self.assertIn(b"Orders ahead: 2", response.data)


if __name__ == "__main__":
    unittest.main()
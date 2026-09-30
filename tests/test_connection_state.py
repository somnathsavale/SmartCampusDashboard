import unittest
from unittest.mock import patch

import app as dashboard
from app import module_connection_summary, parse_line


class ConnectionSummaryTests(unittest.TestCase):
    def test_module_connection_summary_marks_both_board_pairs(self):
        serial_ports = {
            "irrigation": "COM5",
            "canteen": "COM5",
            "dustbin": "COM6",
            "classroom": "COM6",
        }

        result = module_connection_summary(serial_ports)

        self.assertTrue(result["irrigation"])
        self.assertTrue(result["canteen"])
        self.assertTrue(result["dustbin"])
        self.assertTrue(result["classroom"])


class ModuleTelemetryTests(unittest.TestCase):
    def test_classroom_parses_colon_formatted_relay_states(self):
        with patch("app.add_event"), patch("app.update_module") as update_module:
            parse_line("LIGHT: ON", "classroom")
            parse_line("FAN: OFF", "classroom")

        self.assertEqual(update_module.call_args_list[0].args, ("classroom", {"light": True}))
        self.assertEqual(update_module.call_args_list[1].args, ("classroom", {"fan": False}))

    def test_dustbin_parses_warning_threshold_and_sensor_errors(self):
        with patch("app.add_event"), patch("app.update_module") as update_module:
            parse_line("DUSTBIN | Distance: 2.4 cm | Fill: 80%", "dustbin")
            parse_line("Dustbin Sensor Error", "dustbin")

        self.assertEqual(update_module.call_args_list[0].args, (
            "dustbin", {"distance": 2.4, "fill": 80, "sensor_status": "OK", "warning": True}
        ))
        self.assertEqual(update_module.call_args_list[1].args, (
            "dustbin", {"sensor_status": "Error", "warning": False}
        ))


class SerialStartupTests(unittest.TestCase):
    def test_flask_request_starts_serial_detection_once(self):
        dashboard.serial_detection_started = False
        try:
            with patch.object(dashboard, "start_serial_detection_once") as start_detection:
                response = dashboard.app.test_client().get("/api/state")

            self.assertEqual(response.status_code, 200)
            start_detection.assert_called_once_with()
        finally:
            dashboard.serial_detection_started = False

    def test_module_connection_summary_handles_missing_boards(self):
        result = module_connection_summary({})

        self.assertFalse(result["irrigation"])
        self.assertFalse(result["canteen"])
        self.assertFalse(result["dustbin"])
        self.assertFalse(result["classroom"])

    def test_module_connection_summary_requires_open_link_when_state_is_given(self):
        serial_ports = {"irrigation": "COM5", "canteen": "COM5", "dustbin": "COM6", "classroom": "COM6"}
        module_state = {
            "irrigation": {"connected": False},
            "canteen": {"connected": False},
            "dustbin": {"connected": True},
            "classroom": {"connected": True},
        }

        result = module_connection_summary(serial_ports, module_state)

        self.assertFalse(result["irrigation"])
        self.assertFalse(result["canteen"])
        self.assertTrue(result["dustbin"])
        self.assertTrue(result["classroom"])

    def test_default_com5_mapping_for_irrigation_and_canteen(self):
        mapping = {
            "irrigation": "COM5",
            "canteen": "COM5",
            "dustbin": "COM6",
            "classroom": "COM6",
        }

        result = module_connection_summary(mapping)

        self.assertTrue(result["irrigation"])
        self.assertTrue(result["canteen"])
        self.assertTrue(result["dustbin"])
        self.assertTrue(result["classroom"])


if __name__ == "__main__":
    unittest.main()

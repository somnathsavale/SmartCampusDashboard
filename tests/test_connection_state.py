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
    def test_canteen_parses_displayed_token_and_queue_empty_messages(self):
        with patch("app.add_event"), patch("app.update_module") as update_module:
            parse_line("Displaying Token: 42", "canteen")
            parse_line("Queue Empty", "canteen")

        self.assertEqual(update_module.call_args_list[0].args, (
            "canteen", {"displayed_token": 42}
        ))
        self.assertEqual(update_module.call_args_list[1].args, (
            "canteen", {"displayed_token": None}
        ))

    def test_irrigation_parses_explicit_soil_condition_and_pump_lines(self):
        with patch("app.add_event"), patch("app.update_module") as update_module:
            parse_line("SOIL MEDIUM", "irrigation")
            parse_line("SOIL WET", "irrigation")
            parse_line("PUMP OFF", "irrigation")

        self.assertEqual(update_module.call_args_list[0].args, ("irrigation", {"soil_status": "Medium"}))
        self.assertEqual(update_module.call_args_list[1].args, ("irrigation", {"soil_status": "Wet"}))
        self.assertEqual(update_module.call_args_list[2].args, ("irrigation", {"pump": False}))

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


class MqttBridgeTests(unittest.TestCase):
    def test_mqtt_serial_message_routes_line_to_existing_parser(self):
        with patch.object(dashboard, "parse_line") as parse_line:
            handled = dashboard.handle_mqtt_serial_message(
                f"{dashboard.MQTT_TOPIC}/serial/irrigation",
                b'{"line":"Soil Value = 812"}',
            )

        self.assertTrue(handled)
        parse_line.assert_called_once_with("Soil Value = 812", "irrigation")

    def test_mqtt_connection_state_counts_without_a_local_com_port(self):
        module_state = {
            "irrigation": {"connected": True},
            "canteen": {"connected": True},
            "dustbin": {"connected": False},
            "classroom": {"connected": False},
        }

        result = module_connection_summary({}, module_state)

        self.assertTrue(result["irrigation"])
        self.assertTrue(result["canteen"])
        self.assertFalse(result["dustbin"])
        self.assertFalse(result["classroom"])

    def test_canteen_commands_fall_back_to_mqtt(self):
        with patch.object(dashboard, "serial_connections", {}), patch.object(
            dashboard, "mqtt_publish_command", return_value=True
        ) as publish_command:
            dashboard.send_serial_command("COM5", "DISPLAY:12")

        publish_command.assert_called_once_with("canteen", "DISPLAY:12")


class HttpBridgeTests(unittest.TestCase):
    def test_irrigation_telemetry_keeps_latest_raw_serial_line(self):
        previous_state = dashboard.state["irrigation"].copy()
        try:
            with patch.object(dashboard, "parse_line"):
                handled = dashboard.handle_serial_line("irrigation", "Soil Value = 1003")

            self.assertTrue(handled)
            self.assertEqual(dashboard.state["irrigation"]["last_message"], "Soil Value = 1003")
        finally:
            dashboard.state["irrigation"].clear()
            dashboard.state["irrigation"].update(previous_state)

    def test_telemetry_endpoint_requires_token_and_routes_serial_line(self):
        headers = {"Authorization": "Bearer bridge-secret"}
        with patch.object(dashboard, "ARDUINO_BRIDGE_TOKEN", "bridge-secret"), patch.object(
            dashboard, "handle_serial_line"
        ) as handle_line, patch.object(dashboard, "start_serial_detection_once"), patch.object(
            dashboard, "start_mqtt_subscriber_once"
        ):
            client = dashboard.app.test_client()
            denied = client.post(
                "/api/bridge/telemetry", json={"board": "irrigation", "line": "SOIL DRY"}
            )
            accepted = client.post(
                "/api/bridge/telemetry",
                json={"board": "irrigation", "line": "SOIL DRY"},
                headers=headers,
            )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        handle_line.assert_called_once_with("irrigation", "SOIL DRY")


if __name__ == "__main__":
    unittest.main()

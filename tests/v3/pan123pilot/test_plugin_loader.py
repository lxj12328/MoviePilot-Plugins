import os
import sys
import unittest

plugins_v3_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../plugins.v3"))
if plugins_v3_dir not in sys.path:
    sys.path.insert(0, plugins_v3_dir)

from pan123pilot import Pan123Pilot, pan123_on_transfer_completed


class TestPluginLoader(unittest.TestCase):
    def test_plugin_instantiation_and_lifecycle(self):
        plugin = Pan123Pilot()
        self.assertEqual(plugin.plugin_name, "123云盘助手")
        self.assertFalse(plugin.get_state())

        # Test init_plugin
        config = {
            "enabled": True,
            "client_id": "test_id",
            "client_secret": "test_secret",
            "parent_file_id": 12345,
            "max_retry_count": 5,
        }
        plugin.init_plugin(config)
        self.assertTrue(plugin.get_state())

        # Test get_api
        apis = plugin.get_api()
        self.assertTrue(len(apis) > 0)
        for api in apis:
            self.assertIn("path", api)
            self.assertIn("endpoint", api)
            self.assertEqual(api.get("auth"), "bear")

        # Test get_form
        fields, defaults = plugin.get_form()
        self.assertTrue(len(fields) > 0)
        self.assertIn("client_id", defaults)

        # Test get_page
        page = plugin.get_page()
        self.assertTrue(len(page) > 0)

        # Test get_service
        services = plugin.get_service()
        self.assertEqual(len(services), 2)
        job_ids = [s["id"] for s in services]
        self.assertIn("pan123_retry_job", job_ids)
        self.assertIn("pan123_cleanup_job", job_ids)
        for s in services:
            self.assertIn("trigger", s)
            self.assertIn("func", s)
            self.assertIn("kwargs", s)

        # Test silent execution when no tasks exist
        plugin._service_retry_tasks()
        plugin._service_cleanup_tasks()

        # Test stop_service
        plugin.stop_service()
        self.assertFalse(plugin.get_state())
        self.assertEqual(plugin.get_service(), [])

    def test_trigger_parsing(self):
        plugin = Pan123Pilot()
        # Parse minutes
        t1 = plugin._parse_trigger("45", default_minutes=30)
        self.assertIsNotNone(t1)

        # Parse cron expression
        t2 = plugin._parse_trigger("*/15 * * * *", default_minutes=30)
        self.assertIsNotNone(t2)

        # Parse invalid fallback
        t3 = plugin._parse_trigger("invalid_expression", default_minutes=30)
        self.assertIsNotNone(t3)

        # Test init_plugin with custom schedule configuration
        plugin.init_plugin({
            "enabled": True,
            "retry_interval": "15",
            "cleanup_interval": "0 */2 * * *",
        })
        self.assertEqual(plugin._retry_interval, "15")
        self.assertEqual(plugin._cleanup_interval, "0 */2 * * *")
        services = plugin.get_service()
        self.assertEqual(len(services), 2)

    def test_transfer_completed_handler(self):
        class DummyEvent:
            event_data = {
                "target_path": "/tmp/non_existent_test_movie.mkv",
                "torrent_hash": "abcdef123456",
            }
        # Calling handler should not raise exception
        pan123_on_transfer_completed(DummyEvent())


if __name__ == "__main__":
    unittest.main()

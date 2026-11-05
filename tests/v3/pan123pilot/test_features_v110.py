import os
import sys
import unittest
from unittest.mock import MagicMock, patch

plugins_v3_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../plugins.v3"))
if plugins_v3_dir not in sys.path:
    sys.path.insert(0, plugins_v3_dir)

from pan123pilot.client import Pan123Client
from pan123pilot.symedia import send_symedia_file_notify
from pan123pilot.uploader import TaskScheduler
from pan123pilot import Pan123Pilot


class TestFeaturesV110(unittest.TestCase):
    def test_dir_cascade_recursive(self):
        client = Pan123Client("test_id", "test_secret")
        # Mock mkdir: creates subfolder 101, then fails for second (simulates already exists)
        client.mkdir = MagicMock(side_effect=[
            (True, 101, "创建成功"),
            (False, 0, "目录已存在"),
        ])
        # Mock list_files to return the existing second subfolder 202
        client.list_files = MagicMock(return_value=(
            True,
            [{"type": 1, "filename": "国产电影", "fileId": 202, "trashed": 0}],
            "ok"
        ))

        success, dir_id, msg = client.get_or_create_dir_by_path(
            root_parent_id=1000,
            relative_dir_path="emby-movies/国产电影"
        )
        self.assertTrue(success)
        self.assertEqual(dir_id, 202)

        # Check caching: second query should hit cache directly without calling mkdir/list_files again
        client.mkdir.reset_mock()
        client.list_files.reset_mock()
        success, dir_id_cached, _ = client.get_or_create_dir_by_path(
            root_parent_id=1000,
            relative_dir_path="emby-movies/国产电影"
        )
        self.assertTrue(success)
        self.assertEqual(dir_id_cached, 202)
        client.mkdir.assert_not_called()
        client.list_files.assert_not_called()

    def test_symedia_webhook_formatting(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            ok, msg = send_symedia_file_notify(
                server_url="http://127.0.0.1:8095",
                target_path="/video/emby-movies/国产电影/流浪地球.mkv",
                local_library_root="/video",
                mount_prefix="/CloudNAS/123/video",
                auth_header="basic testtoken",
                action="create",
            )
            self.assertTrue(ok)
            self.assertIn("200", msg)

            # Verify request
            args, kwargs = mock_urlopen.call_args
            req = args[0]
            self.assertEqual(req.get_header("Content-type"), "application/json")
            self.assertEqual(req.get_header("Authorization"), "basic testtoken")
            self.assertIn("clouddrive2/file_notify", req.full_url)

    def test_task_scheduler_resolve_target_parent_id(self):
        client = MagicMock()
        client.get_or_create_dir_by_path.return_value = (True, 9999, "ok")
        qb = MagicMock()
        db = MagicMock()

        scheduler = TaskScheduler(
            client=client,
            qb_helper=qb,
            db_helper=db,
            tags_config={},
            target_parent_file_id=1000,
            local_library_root="/video",
            enable_path_cascade=True,
        )

        resolved_id = scheduler.resolve_target_parent_id(
            "/video/emby-movies/国产电影/流浪地球 (2019)/流浪地球.mkv"
        )
        self.assertEqual(resolved_id, 9999)
        client.get_or_create_dir_by_path.assert_called_with(
            root_parent_id=1000,
            relative_dir_path="emby-movies/国产电影/流浪地球 (2019)"
        )

    def test_plugin_downloader_form_and_init(self):
        plugin = Pan123Pilot()
        fields, defaults = plugin.get_form()
        self.assertIn("local_library_root", defaults)
        self.assertIn("enable_path_cascade", defaults)
        self.assertIn("downloader_select", defaults)
        self.assertIn("symedia_enabled", defaults)
        self.assertIn("symedia_url", defaults)

        # Test init_plugin with symedia and path cascade
        plugin.init_plugin({
            "enabled": True,
            "client_id": "test_id",
            "client_secret": "test_secret",
            "local_library_root": "/video",
            "enable_path_cascade": True,
            "symedia_enabled": True,
            "symedia_url": "http://127.0.0.1:8095",
            "downloader_select": "manual",
            "qb_host": "http://127.0.0.1:8080",
        })
        self.assertTrue(plugin.get_state())
        self.assertEqual(plugin._local_library_root, "/video")
        self.assertTrue(plugin._enable_path_cascade)
        self.assertTrue(plugin._symedia_enabled)
        plugin.stop_service()


if __name__ == "__main__":
    unittest.main()

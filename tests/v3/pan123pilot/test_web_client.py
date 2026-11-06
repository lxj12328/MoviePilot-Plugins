import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

plugins_v3_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../plugins.v3"))
if plugins_v3_dir not in sys.path:
    sys.path.insert(0, plugins_v3_dir)

from pan123pilot.web_client import Pan123WebClient


class TestPan123WebClient(unittest.TestCase):
    def test_web_client_is_configured(self):
        c1 = Pan123WebClient(token="my_token", auth_mode="web_token")
        self.assertTrue(c1.is_configured())

        c2 = Pan123WebClient(token="", auth_mode="web_token")
        self.assertFalse(c2.is_configured())

        c3 = Pan123WebClient(username="13800000000", password="pwd", auth_mode="web_account")
        self.assertTrue(c3.is_configured())

    def test_token_validity_check(self):
        client = Pan123WebClient(token="mock_valid_token_123")
        with patch.object(client, "_http_post") as mock_post:
            mock_post.return_value = (True, {"code": 0, "message": "ok"}, "ok")
            valid = client.check_token_validity()
            self.assertTrue(valid)

    def test_password_login_success(self):
        client = Pan123WebClient(username="13800000000", password="mock_password")
        with patch.object(client, "_http_post") as mock_post:
            mock_post.return_value = (
                True,
                {"code": 0, "data": {"token": "mock_login_token_abc"}},
                "ok",
            )
            ok, msg = client.login_by_password()
            self.assertTrue(ok)
            self.assertEqual(client.token, "mock_login_token_abc")

    def test_ensure_authenticated_token_valid(self):
        client = Pan123WebClient(token="mock_valid_token", auth_mode="web_token")
        with patch.object(client, "check_token_validity", return_value=True):
            ok, msg = client.ensure_authenticated()
            self.assertTrue(ok)
            self.assertIn("有效", msg)

    def test_ensure_authenticated_token_expired(self):
        client = Pan123WebClient(token="mock_expired_token", auth_mode="web_token")
        with patch.object(client, "check_token_validity", return_value=False):
            ok, msg = client.ensure_authenticated()
            self.assertFalse(ok)
            self.assertIn("authorToken", msg)

    def test_sha1_reuse_hit(self):
        client = Pan123WebClient(token="mock_token")
        with patch.object(client, "check_token_validity", return_value=True):
            with patch.object(client, "_http_post") as mock_post:
                mock_post.return_value = (
                    True,
                    {"code": 0, "data": {"reuse": True, "fileId": 987654}},
                    "ok",
                )
                reuse, file_id, msg = client.sha1_reuse(
                    filename="sample.mkv",
                    sha1_str="dummy_sha1",
                    size=1024,
                    parent_file_id=0,
                    md5_str="dummy_md5",
                )
                self.assertTrue(reuse)
                self.assertEqual(file_id, 987654)

    def test_sha1_reuse_miss(self):
        client = Pan123WebClient(token="mock_token")
        with patch.object(client, "check_token_validity", return_value=True):
            with patch.object(client, "_http_post") as mock_post:
                mock_post.return_value = (
                    True,
                    {"code": 0, "data": {"reuse": False}},
                    "ok",
                )
                reuse, file_id, msg = client.sha1_reuse(
                    filename="sample.mkv",
                    sha1_str="dummy_sha1",
                    size=1024,
                    parent_file_id=0,
                    md5_str="dummy_md5",
                )
                self.assertFalse(reuse)
                self.assertIsNone(file_id)

    def test_dir_cascade_web_client(self):
        client = Pan123WebClient(token="mock_token")
        with patch.object(client, "check_token_validity", return_value=True):
            # Mock mkdir to create subfolder
            client.mkdir = MagicMock(return_value=(True, 2001, "ok"))
            client.list_files = MagicMock(return_value=(True, [], "ok"))

            ok, dir_id, msg = client.get_or_create_dir_by_path(
                root_parent_id=0,
                relative_dir_path="emby-movies/国产电影",
            )
            self.assertTrue(ok)
            self.assertEqual(dir_id, 2001)


    def test_token_cleaner(self):
        # 测试自动剔除双引号、单引号与 Bearer 前缀
        self.assertEqual(Pan123WebClient._clean_token('"my_token_123"'), "my_token_123")
        self.assertEqual(Pan123WebClient._clean_token("'my_token_456'"), "my_token_456")
        self.assertEqual(Pan123WebClient._clean_token('Bearer my_token_789'), "my_token_789")
        self.assertEqual(Pan123WebClient._clean_token('Bearer "my_token_000"'), "my_token_000")
        self.assertEqual(Pan123WebClient._clean_token('  "bearer token_abc"  '), "token_abc")

    def test_auth_mode_isolation_web_token_never_calls_password_login(self):
        # 在 web_token 直填模式下，即使配置了账号密码且 Token 失效，也绝不擅自调用账号密码登录
        client = Pan123WebClient(
            username="13800000000",
            password="mock_password",
            token="invalid_token",
            auth_mode="web_token",
        )
        with patch.object(client, "check_token_validity", return_value=False):
            with patch.object(client, "login_by_password") as mock_login_pwd:
                ok, msg = client.ensure_authenticated()
                self.assertFalse(ok)
                self.assertIn("authorToken", msg)
                mock_login_pwd.assert_not_called()

    def test_user_info_validity_check(self):
        client = Pan123WebClient(token="mock_valid_token_123")
        with patch.object(client, "_http_get") as mock_get:
            mock_get.return_value = (True, {"code": 0, "data": {"nickname": "TestUser", "uid": 12345}}, "ok")
            valid = client.check_token_validity()
            self.assertTrue(valid)
            self.assertEqual(client.user_info.get("nickname"), "TestUser")


    def test_sha1_reuse_token_expired_response(self):
        client = Pan123WebClient(token="mock_expired_token")
        with patch.object(client, "check_token_validity", return_value=True):
            with patch.object(client, "_http_post") as mock_post:
                # 模拟 123 接口在请求时返回 401 或 token 失效
                mock_post.return_value = (False, None, "HTTP 401: Unauthorized")
                reuse, file_id, msg = client.sha1_reuse(
                    filename="sample.mkv",
                    sha1_str="dummy_sha1",
                    size=1024,
                    parent_file_id=0,
                    md5_str="dummy_md5",
                )
                self.assertFalse(reuse)
                self.assertIsNone(file_id)
                self.assertIn("TOKEN_EXPIRED", msg)

    def test_pilot_token_expired_notification_debounce(self):
        from pan123pilot import Pan123Pilot
        pilot = Pan123Pilot()
        notifications = []
        pilot.notify = lambda title, text: notifications.append((title, text))

        # 首次触发：应发送通知并更新状态
        pilot._notify_token_expired("Token 已失效")
        self.assertEqual(len(notifications), 1)
        self.assertIn("报警", notifications[0][0])
        self.assertIn("🚨", pilot._auth_status_msg)

        # 立即再次触发：24小时防刷屏静默期，不应重复发送
        pilot._notify_token_expired("Token 再次检测失效")
        self.assertEqual(len(notifications), 1)

    def test_scheduler_token_expired_callback(self):
        from pan123pilot.uploader import TaskScheduler
        mock_client = MagicMock()
        mock_qb = MagicMock()
        mock_db = MagicMock()
        expired_calls = []

        scheduler = TaskScheduler(
            client=mock_client,
            qb_helper=mock_qb,
            db_helper=mock_db,
            tags_config={},
            on_token_expired=lambda reason: expired_calls.append(reason),
        )

        mock_db.list_tasks_by_status.return_value = [
            {
                "id": 101,
                "target_path": "/video/test.mkv",
                "torrent_hash": "hash123",
                "sha1": "dummy_sha1",
                "md5": "dummy_md5",
                "file_size": 100,
                "retry_count": 1,
                "max_retry": 10,
            }
        ]
        mock_client.sha1_reuse.return_value = (False, None, "TOKEN_EXPIRED: 401 Unauthorized")

        with patch("os.path.exists", return_value=True):
            scheduler.process_pending_tasks(enable_real_upload=True)

        self.assertEqual(len(expired_calls), 1)
        self.assertIn("TOKEN_EXPIRED", expired_calls[0])
        # 验证任务未被当作正常失败，未扣减重试上限
        mock_db.update_task_status.assert_called_with(
            task_id=101,
            status="RETRYING",
            sha1="dummy_sha1",
            md5="dummy_md5",
            error_msg="123云盘 Web Token 已过期，等待更新 Token 后自动恢复",
        )


if __name__ == "__main__":
    unittest.main()

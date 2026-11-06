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

    def test_sms_code_login_success(self):
        client = Pan123WebClient(username="13800000000")
        with patch.object(client, "_http_post") as mock_post:
            mock_post.return_value = (
                True,
                {"code": 0, "data": {"token": "mock_sms_token_xyz"}},
                "ok",
            )
            ok, msg = client.login_by_sms_code("123456")
            self.assertTrue(ok)
            self.assertEqual(client.token, "mock_sms_token_xyz")

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


    def test_generate_qr_login(self):
        client = Pan123WebClient()
        ok, data, msg = client.generate_qr_login()
        self.assertTrue(ok)
        self.assertIn("uuid", data)
        self.assertIn("img_url", data)
        self.assertIn("url", data)

    def test_check_qr_login_status_waiting(self):
        client = Pan123WebClient()
        with patch.object(client, "_http_post") as mock_post:
            mock_post.return_value = (True, {"code": 1001, "message": "等待扫码"}, "ok")
            is_done, tok, msg = client.check_qr_login_status("mock_uuid_123")
            self.assertFalse(is_done)
            self.assertIsNone(tok)
            self.assertIn("等待", msg)

    def test_check_qr_login_status_success(self):
        client = Pan123WebClient()
        # 第一次请求获取 wechat_code，第二次换取 token
        with patch.object(client, "_http_post") as mock_post:
            mock_post.side_effect = [
                (True, {"code": 0, "data": {"code": "mock_wx_code_abc"}}, "ok"),
                (True, {"code": 0, "data": {"token": "mock_qr_token_success_999"}}, "ok"),
            ]
            is_done, tok, msg = client.check_qr_login_status("mock_uuid_123")
            self.assertTrue(is_done)
            self.assertEqual(tok, "mock_qr_token_success_999")
            self.assertEqual(client.token, "mock_qr_token_success_999")


if __name__ == "__main__":
    unittest.main()

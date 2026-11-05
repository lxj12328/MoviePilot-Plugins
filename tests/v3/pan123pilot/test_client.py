"""Pan123Client 单元测试"""

from datetime import datetime, timedelta
import io
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

# 支持独立单测时导入 plugins.v3 源码
plugins_v3_dir = Path(__file__).resolve().parents[3] / "plugins.v3"
if str(plugins_v3_dir) not in sys.path:
    sys.path.insert(0, str(plugins_v3_dir))

from pan123pilot.client import Pan123Client


class TestPan123Client(unittest.TestCase):
    def setUp(self):
        self.client = Pan123Client(client_id="test_client_id", client_secret="test_secret")

    def test_is_configured(self):
        self.assertTrue(self.client.is_configured())
        empty_client = Pan123Client(client_id="", client_secret="")
        self.assertFalse(empty_client.is_configured())

    @patch("urllib.request.urlopen")
    def test_get_access_token_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "code": 0,
            "message": "ok",
            "data": {
                "accessToken": "mocked_jwt_token",
                "expiredAt": (datetime.now() + timedelta(hours=2)).isoformat(),
            },
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        token = self.client.get_access_token()
        self.assertEqual(token, "mocked_jwt_token")

    @patch.object(Pan123Client, "get_access_token", return_value="mocked_token")
    @patch("urllib.request.urlopen")
    def test_sha1_reuse_hit(self, mock_urlopen, mock_token):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "code": 0,
            "message": "ok",
            "data": {
                "reuse": True,
                "fileID": 987654321,
            },
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        reuse, file_id, msg = self.client.sha1_reuse(
            filename="Avatar.2009.mp4",
            sha1_str="4b5c549c4abd0a079caf92d6cad2412711223344",
            size=50650928,
            parent_file_id=0,
        )

        self.assertTrue(reuse)
        self.assertEqual(file_id, 987654321)
        self.assertIn("秒传成功", msg)

    @patch.object(Pan123Client, "get_access_token", return_value="mocked_token")
    @patch("urllib.request.urlopen")
    def test_sha1_reuse_miss(self, mock_urlopen, mock_token):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "code": 0,
            "message": "ok",
            "data": {
                "reuse": False,
                "fileID": 0,
            },
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        reuse, file_id, msg = self.client.sha1_reuse(
            filename="Avatar.2009.mp4",
            sha1_str="4b5c549c4abd0a079caf92d6cad2412711223344",
            size=50650928,
            parent_file_id=0,
        )

        self.assertFalse(reuse)
        self.assertEqual(file_id, 0)
        self.assertIn("未命中秒传", msg)


if __name__ == "__main__":
    unittest.main()

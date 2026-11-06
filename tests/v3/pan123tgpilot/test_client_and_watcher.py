import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock

plugins_v3_dir = Path(__file__).resolve().parents[3] / "plugins.v3"
if str(plugins_v3_dir) not in sys.path:
    sys.path.insert(0, str(plugins_v3_dir))

from pan123tgpilot.client import Pan123TgClient
from pan123tgpilot.tg_watcher import TgChannelWatcher


class TestClientAndWatcher(unittest.TestCase):
    def test_client_clean_token(self):
        client = Pan123TgClient(token="Bearer test_token_xyz")
        self.assertEqual(client.token, "test_token_xyz")
        headers = client._get_headers(with_auth=True)
        self.assertEqual(headers["authorization"], "Bearer test_token_xyz")

    def test_client_mkdir(self):
        client = Pan123TgClient(token="test_token")
        client._http_request = MagicMock(return_value=(True, {
            "code": 0,
            "message": "ok",
            "data": {"fileId": 778899},
        }, "ok"))

        ok, dir_id, msg = client.mkdir("八仙！ (2026)", parent_file_id=12345)
        self.assertTrue(ok)
        self.assertEqual(dir_id, 778899)

    def test_client_save_share_link(self):
        client = Pan123TgClient(token="test_token")

        def mock_request(url, method="GET", payload=None, with_auth=True):
            if "share/get" in url:
                return True, {
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "InfoList": [
                            {"fileId": 101, "fileName": "movie.mkv", "type": 0},
                            {"fileId": 102, "fileName": "movie.nfo", "type": 0},
                            {"fileId": 103, "fileName": "movie.srt", "type": 0},
                        ],
                        "next": 0,
                    },
                }, "ok"
            elif "upload_request" in url:
                return True, {"code": 0, "data": {"fileId": 999}}, "ok"
            elif "share/save" in url:
                return True, {"code": 0, "message": "ok", "data": {"fileId": 999}}, "ok"
            return False, {}, "error"

        client._http_request = MagicMock(side_effect=mock_request)
        ok, fid, msg = client.save_share_link(
            share_key="7Tx1jv-8e4tv",
            share_pwd="xoxo",
            target_parent_id=12345,
            folder_name="测试影视 (2026)",
        )
        self.assertTrue(ok)
        self.assertIn("3 个文件", msg)

    def test_client_save_fslink(self):
        client = Pan123TgClient(token="test_token")
        client._http_request = MagicMock(return_value=(True, {
            "code": 0,
            "message": "ok",
            "data": {
                "reuse": True,
                "fileId": 172012630879389,
                "async": False,
            },
        }, "ok"))

        success, file_id, msg = client.save_fslink(
            fslink="123FSLinkV2$82467be2147f26532298438bf7015238#32855978205#八仙.mkv",
            parent_file_id=12345,
        )
        self.assertTrue(success)
        self.assertEqual(file_id, "172012630879389")

    def test_client_trash_files(self):
        client = Pan123TgClient(token="test_token")
        client._http_request = MagicMock(return_value=(True, {"code": 0, "message": "success"}, "ok"))

        success, msg = client.trash_files([172012630879389])
        self.assertTrue(success)

    def test_proxy_normalization(self):
        w1 = TgChannelWatcher(proxy="127.0.0.1:7890")
        self.assertEqual(w1.proxy, "http://127.0.0.1:7890")
        w2 = TgChannelWatcher(proxy="socks5://127.0.0.1:7890")
        self.assertEqual(w2.proxy, "socks5://127.0.0.1:7890")
        w3 = TgChannelWatcher(proxy="")
        self.assertEqual(w3.proxy, "")

    def test_tg_watcher_parse_preview_html(self):
        html = """
        <div class="tgme_widget_message_wrap">
            <div class="tgme_widget_message" data-post="Pan123Movie/1024">
                <div class="tgme_widget_message_text">
                    🎬 <b>斗破苍穹</b> (2017)<br>
                    <a href="https://www.123pan.com/s/7Tx1jv-8e4tv?pwd=xoxo">123网盘</a>
                </div>
            </div>
        </div>
        """
        messages = TgChannelWatcher._parse_tg_web_html(html, "Pan123Movie")
        self.assertEqual(len(messages), 1)
        msg_id, text = messages[0]
        self.assertEqual(msg_id, "1024")
        self.assertIn("https://www.123pan.com/s/7Tx1jv-8e4tv?pwd=xoxo", text)
        self.assertIn("斗破苍穹", text)


if __name__ == "__main__":
    unittest.main()

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

    def test_client_save_share_link_via_etag_rapid_upload_without_404(self):
        """验证真实 123 云盘分享链接通过 /b/api/share/get 提取 Etag + /b/api/file/upload_request 递归秒传转存，无需调用不存在的 /b/api/share/save"""
        client = Pan123TgClient(token="test_token")
        uploaded_files = []

        def mock_request(url, method="GET", payload=None, with_auth=True):
            if "file/list/new" in url:
                return True, {"code": 0, "data": {"InfoList": []}}, "ok"
            if "share/get" in url:
                if "ParentFileId=5001" in url:
                    return True, {
                        "code": 0,
                        "data": {
                            "InfoList": [
                                {"FileId": 6001, "FileName": "谷雨.S01E06.2160p.mkv", "Type": 0, "Size": 5680000000, "Etag": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"},
                                {"FileId": 6002, "FileName": "谷雨.S01E06.nfo", "Type": 0, "Size": 2048, "Etag": "b1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"},
                            ],
                            "next": 0,
                        },
                    }, "ok"
                return True, {
                    "code": 0,
                    "data": {
                        "InfoList": [
                            {"FileId": 5001, "FileName": "Season 1", "Type": 1, "Size": 0, "Etag": ""},
                            {"FileId": 5002, "FileName": "tvshow.nfo", "Type": 0, "Size": 1024, "Etag": "c1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"},
                        ],
                        "next": 0,
                    },
                }, "ok"
            if "upload_request" in url:
                if payload and payload.get("type") == 1:
                    return True, {"code": 0, "data": {"Info": {"FileId": 77701}}}, "ok"
                uploaded_files.append(payload)
                return True, {"code": 0, "data": {"Reuse": True, "Info": {"FileId": 88899}}}, "ok"
            if "share/save" in url:
                return False, {}, "HTTP 404: Not Found"
            return False, {}, "error"

        client._http_request = MagicMock(side_effect=mock_request)
        ok, fid, msg = client.save_share_link(
            share_key="Oqtgvd-d5i3h",
            share_pwd="YSRG",
            target_parent_id=42149741,
            folder_name="谷雨 (2026)",
        )
        self.assertTrue(ok)
        self.assertEqual(fid, "88899")
        self.assertEqual(len(uploaded_files), 3)
        filenames = [p["fileName"] for p in uploaded_files]
        self.assertIn("谷雨.S01E06.2160p.mkv", filenames)
        self.assertIn("谷雨.S01E06.nfo", filenames)
        self.assertIn("tvshow.nfo", filenames)

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

    def test_client_save_fslink_fallback_on_folder_not_found(self):
        client = Pan123TgClient(token="test_token")
        # 模拟：
        # 1. list_files 首次返回旧失效的子文件夹 ID 52958217；
        # 2. 尝试向 52958217 秒传时报错 "文件夹不存在"；
        # 3. 清除缓存后重新在用户配置的目录 42149741 下 list_files (为空) 并 mkdir 创建新子文件夹 99990；
        # 4. 秒传至 99990（在 42149741 下，绝不降级到根目录 0）成功！
        call_log = []

        def mock_req(url, method="GET", payload=None, with_auth=True):
            call_log.append((url, method, dict(payload) if payload else None))
            if "file/list/new" in url:
                if len(call_log) == 1:
                    return True, {
                        "code": 0,
                        "data": {"InfoList": [{"fileId": 52958217, "fileName": "夜曲 (2026)", "type": 1}]},
                    }, "ok"
                return True, {"code": 0, "data": {"InfoList": []}}, "ok"
            if "upload_request" in url and payload:
                if payload.get("type") == 0 and payload.get("parentFileId") == 52958217:
                    return False, {"code": 10404, "message": "文件夹不存在"}, "文件夹不存在"
                if payload.get("type") == 1 and payload.get("parentFileId") == 42149741:
                    return True, {"code": 0, "data": {"fileId": 99990}}, "ok"
                if payload.get("type") == 0 and payload.get("parentFileId") == 99990:
                    return True, {"code": 0, "data": {"reuse": True, "fileId": 888123}}, "ok"
            return False, {}, "error"

        client._http_request = MagicMock(side_effect=mock_req)
        success, file_id, msg = client.save_fslink(
            fslink="123FSLinkV2$82467be2147f26532298438bf7015238#32855978205#夜曲.mkv",
            parent_file_id=42149741,
            folder_name="夜曲 (2026)",
        )
        self.assertTrue(success)
        self.assertEqual(file_id, "888123")
        # 验证全程没有任何请求将 parentFileId 设为根目录 0
        for _, _, pl in call_log:
            if pl and "parentFileId" in pl:
                self.assertNotEqual(pl["parentFileId"], 0)

    def test_client_trash_files(self):
        client = Pan123TgClient(token="test_token")
        captured_payloads = []

        def mock_trash(url, method="POST", payload=None, with_auth=True):
            captured_payloads.append(payload)
            return True, {"code": 0, "message": "success"}, "ok"

        client._http_request = MagicMock(side_effect=mock_trash)

        success, msg = client.trash_files([78719249199166])
        self.assertTrue(success)
        self.assertEqual(len(captured_payloads), 1)
        self.assertEqual(captured_payloads[0]["fileTrashInfoList"], [{"FileId": 78719249199166}])
        self.assertTrue(captured_payloads[0]["operation"])
        self.assertEqual(captured_payloads[0]["event"], "intoRecycle")

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

    def test_tg_watcher_parse_inline_button_and_nested_div(self):
        """验证 TG 消息中包含嵌套 blockquote/div 或底部 inline_button 分享链接时能完整提取"""
        html = """
        <div class="tgme_widget_message_wrap">
            <div class="tgme_widget_message" data-post="regeng123/2048">
                <div class="tgme_widget_message_text js-message_text" dir="auto">
                    <div>名称：凡人修仙传 (2020)</div>
                    <blockquote>S01E120 2160p H.265</blockquote>
                </div>
                <div class="tgme_widget_message_footer"></div>
                <div class="tgme_widget_message_inline_keyboard">
                    <a class="tgme_widget_message_inline_button" href="https://www.123865.com/s/XyZ987-abcd?pwd=6666">点击保存 123云盘</a>
                </div>
            </div>
        </div>
        """
        messages = TgChannelWatcher._parse_tg_web_html(html, "regeng123")
        self.assertEqual(len(messages), 1)
        msg_id, text = messages[0]
        self.assertEqual(msg_id, "2048")
        self.assertIn("凡人修仙传", text)
        self.assertIn("https://www.123865.com/s/XyZ987-abcd?pwd=6666", text)

    def test_tg_watcher_strips_reply_block_to_avoid_pollution(self):
        """验证 TG 消息中包含上一条被引用消息 (tgme_widget_message_reply) 时能干净剥离，不污染当前正文"""
        html = """
        <div class="tgme_widget_message_wrap">
            <div class="tgme_widget_message" data-post="regeng123/3001">
                <a class="tgme_widget_message_reply" href="https://t.me/regeng123/3000">
                    <span class="tgme_widget_message_author_name">热更频道</span>
                    <div class="tgme_widget_message_text">折月亮 (2026) S01E05</div>
                </a>
                <div class="tgme_widget_message_text js-message_text" dir="auto">
                    奥德赛 (2026)
                    📺类型 : 欧美电影
                    ⭐评分 : TMDB: [8.0](https://www.themoviedb.org/movie/1368337)
                    🔗分享链接 : 123FSLinkV2$8cea83224545651bb8ad54ea015c0801#88246460648#奥德赛.mkv
                </div>
            </div>
        </div>
        """
        messages = TgChannelWatcher._parse_tg_web_html(html, "regeng123")
        self.assertEqual(len(messages), 1)
        msg_id, text = messages[0]
        self.assertEqual(msg_id, "3001")
        self.assertNotIn("折月亮", text)
        self.assertNotIn("S01E05", text)
        self.assertIn("奥德赛", text)
        self.assertIn("欧美电影", text)


if __name__ == "__main__":
    unittest.main()

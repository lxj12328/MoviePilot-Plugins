import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock

plugins_v3_dir = Path(__file__).resolve().parents[3] / "plugins.v3"
if str(plugins_v3_dir) not in sys.path:
    sys.path.insert(0, str(plugins_v3_dir))

from pan123tgpilot import Pan123TgPilot


class TestPan123TgPilotPlugin(unittest.TestCase):
    def setUp(self):
        self.plugin = Pan123TgPilot()

    def test_init_and_form(self):
        config = {
            "enabled": True,
            "pan123_token": "token_abc123",
            "movie_target_dir_id": "1001",
            "tv_target_dir_id": "1002",
            "tg_channels": "@Pan123Movie, @regeng123",
            "check_interval": "15",
            "auto_upgrade": True,
            "auto_delete_old_version": True,
            "notify_enabled": False,
        }
        self.plugin.init_plugin(config)
        self.assertTrue(self.plugin.get_state())
        self.assertEqual(self.plugin._movie_target_dir_id, 1001)
        self.assertEqual(self.plugin._tv_target_dir_id, 1002)
        self.assertEqual(len(self.plugin._tg_channels), 2)
        self.assertEqual(self.plugin._check_interval, 15)

        form, model = self.plugin.get_form()
        self.assertIsInstance(form, list)
        self.assertIsInstance(model, dict)
        self.assertIn("enabled", model)
        self.assertIn("pan123_token", model)

        page = self.plugin.get_page()
        self.assertIsInstance(page, list)

        services = self.plugin.get_service()
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["id"], "pan123_tg_check_job")

        apis = self.plugin.get_api()
        self.assertEqual(len(apis), 3)

    def test_process_message_transfer_and_upgrade(self):
        self.plugin.init_plugin({
            "enabled": True,
            "pan123_token": "token_abc",
            "movie_target_dir_id": "1001",
            "tv_target_dir_id": "1002",
            "auto_upgrade": True,
            "auto_delete_old_version": True,
        })
        # Mock client & db
        self.plugin._client = MagicMock()
        self.plugin._client.save_fslink.return_value = (True, "99999", "success")
        self.plugin._client.trash_files.return_value = (True, "success")

        self.plugin._db = MagicMock()
        # 第一次无记录
        self.plugin._db.get_record_by_media_key.return_value = None

        sample_msg = """
八仙！ (2026)
🏷️标签 : #八仙 #动画
⭐评分 : TMDB: 8.0
🆔TMDB : 1633056
🖼️版本 : 1080p.WEB-DL.H.264
💾体积 : 5.00 GB
🔗分享链接 : 123FSLinkV2$82467be2147f26532298438bf7015238#5368709120#八仙1080p.mkv
        """
        res1 = self.plugin._process_single_message(channel="Pan123Movie", msg_id="1", raw_text=sample_msg)
        self.assertEqual(res1, "transferred")
        self.plugin._client.save_fslink.assert_called()

        # 第二次推送 4K DoVi 资源，模拟数据库已有记录且旧得分为 75
        mock_rec = {
            "media_key": "movie_1633056",
            "quality_score": 75,
            "pan_file_id": 99999,
            "resolution": "1080p",
            "codec": "H.264",
            "dovi_hdr": "",
        }
        self.plugin._db.get_record_by_media_key.return_value = mock_rec

        sample_msg_4k = """
八仙！ (2026)
🏷️标签 : #八仙 #动画
⭐评分 : TMDB: 8.0
🆔TMDB : 1633056
🖼️版本 : 2160p.WEB-DL.H.265.DoVi.10bit
💾体积 : 30.60 GB
🔗分享链接 : 123FSLinkV2$abcdefabcdefabcdefabcdefabcdef12#32855978205#八仙4K.mkv
        """
        res2 = self.plugin._process_single_message(channel="Pan123Movie", msg_id="2", raw_text=sample_msg_4k)
        self.assertEqual(res2, "upgraded")
        # 验证旧版本被自动删除
        self.plugin._client.trash_files.assert_called_with([99999])


if __name__ == "__main__":
    unittest.main()

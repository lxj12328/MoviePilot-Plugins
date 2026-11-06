import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

plugins_v3_dir = Path(__file__).resolve().parents[3] / "plugins.v3"
if str(plugins_v3_dir) not in sys.path:
    sys.path.insert(0, str(plugins_v3_dir))

from pan123tgpilot import Pan123TgPilot
from pan123tgpilot.models import Pan123TgDatabase


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
        self.assertIn("run_now_on_save", model)
        self.assertIn("pan123_token", model)

        page = self.plugin.get_page()
        self.assertIsInstance(page, list)

        services = self.plugin.get_service()
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["id"], "pan123_tg_check_job")

        commands = self.plugin.get_command()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["cmd"], "/pan123_tg_run")

        apis = self.plugin.get_api()
        self.assertEqual(len(apis), 4)

    @patch("threading.Thread")
    def test_run_now_on_save(self, mock_thread):
        config = {
            "enabled": True,
            "run_now_on_save": True,
            "pan123_token": "token_test",
        }
        self.plugin.init_plugin(config)
        mock_thread.assert_called()

    @patch("threading.Thread")
    def test_run_action(self, mock_thread):
        self.plugin.run_action("pan123_tg_run")
        mock_thread.assert_called()

    def test_database_save_or_update_flexibility(self):
        db = Pan123TgDatabase()
        mock_session = MagicMock()
        mock_session.execute.return_value.scalar_one_or_none.return_value = None

        record_data = {
            "media_key": "movie_12345",
            "channel_name": "@Pan123Movie",
            "msg_id": "100",
            "title": "测试电影",
            "resolution": "1080p",
            "codec": "H.265",
            "dovi_hdr": "",
            "quality_score": 80,
            "file_size": 1024,
            "pan_dir_id": 42149741,
            "pan_file_id": 998877,
        }
        # 测试直接传单一 dict 且不传 db 关键字时的兼容性
        res_id = db.save_or_update_record(record_data, db=mock_session)
        mock_session.add.assert_called()
        mock_session.flush.assert_called()

        # 测试通过 **record_data 关键字参数持久化
        res_id2 = db.save_or_update_record(db=mock_session, **record_data)
        self.assertIsNotNone(res_id2)

        # 测试无效空记录拒绝入库
        empty_id = db.save_or_update_record(db=mock_session, title="", media_key="")
        self.assertEqual(empty_id, 0)

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
        self.plugin._db.has_link_processed.return_value = False
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

    def test_share_link_priority_over_fslink(self):
        self.plugin.init_plugin({
            "enabled": True,
            "pan123_token": "token_abc",
            "movie_target_dir_id": "1001",
            "tv_target_dir_id": "1002",
        })
        self.plugin._client = MagicMock()
        self.plugin._client.save_share_link.return_value = (True, "88888", "success")
        self.plugin._db = MagicMock()
        self.plugin._db.has_link_processed.return_value = False
        self.plugin._db.get_record_by_media_key.return_value = None

        # 同时含有分享链接与秒传链接的消息，必须优先转存分享链接的完整文件夹与伴生文件
        msg = """
🎬 斗破苍穹 (2017)
🎭 类型：剧集
S05E217 4K WEB-DL H.265
🔗 链接：[123网盘](https://www.123pan.com/s/7Tx1jv-8e4tv?pwd=xoxo#)
🔗 秒传：123FSLinkV2$82467be2147f26532298438bf7015238#1234567#斗破S05E217.mkv
        """
        res = self.plugin._process_single_message(channel="Pan123Movie", msg_id="10", raw_text=msg)
        self.assertEqual(res, "transferred")
        self.plugin._client.save_share_link.assert_called_once()
        self.plugin._client.save_fslink.assert_not_called()

    def test_link_level_deduplication_skips(self):
        self.plugin.init_plugin({
            "enabled": True,
            "pan123_token": "token_abc",
            "movie_target_dir_id": "1001",
            "tv_target_dir_id": "1002",
        })
        self.plugin._client = MagicMock()
        self.plugin._db = MagicMock()
        # 链接级别去重命中
        self.plugin._db.has_link_processed.return_value = True

        msg = "🔗分享链接 : 123FSLinkV2$82467be2147f26532298438bf7015238#32855978205#八仙.mkv"
        res = self.plugin._process_single_message(channel="channel_a", msg_id="1", raw_text=msg)
        self.assertEqual(res, "skipped")
        self.plugin._client.save_fslink.assert_not_called()

    def test_get_page_returns_status_and_table(self):
        self.plugin.init_plugin({
            "enabled": True,
            "pan123_token": "token_abc",
            "movie_target_dir_id": "1001",
            "tv_target_dir_id": "1002",
        })
        self.plugin._db = MagicMock()
        self.plugin._db.count_records.return_value = 1
        self.plugin._db.list_records.return_value = [
            {
                "id": 1,
                "media_title": "八仙！ (2026)",
                "resolution": "2160p",
                "codec": "H.265",
                "dovi_hdr": "DoVi",
                "file_size": 32855978205,
                "quality_score": 120,
                "channel": "@Pan123Movie",
                "status": "saved",
                "created_at": "2026-11-06 20:00:00",
            }
        ]
        page = self.plugin.get_page()
        self.assertEqual(len(page), 2)
        self.assertEqual(page[0]["component"], "VCard")
        table_card = page[1]
        self.assertIn("共 1 条", table_card["content"][0]["content"][0]["text"])
        table = table_card["content"][1]
        self.assertEqual(table["component"], "VDataTable")
        self.assertEqual(table["props"]["items-per-page"], 10)
        item = table["props"]["items"][0]
        self.assertEqual(item["title"], "八仙！ (2026)")
        self.assertEqual(item["spec"], "2160p / H.265 / DoVi")
        self.assertEqual(item["status"], "首发转存")
        self.assertEqual(item["score"], 120)

    def test_api_records_pagination(self):
        self.plugin.init_plugin({"enabled": True})
        self.plugin._db = MagicMock()
        self.plugin._db.count_records.return_value = 45
        self.plugin._db.list_records.return_value = [{"title": "八仙"}]
        res = self.plugin._api_get_records(keyword="八仙", status="saved", page=3, page_size=20)
        self.assertEqual(res["code"], 200)
        self.assertEqual(res["total"], 45)
        self.assertEqual(res["pages"], 3)
        self.plugin._db.list_records.assert_called_with(limit=20, offset=40, keyword="八仙", status="saved")

    def test_api_records_bad_params_fallback(self):
        self.plugin.init_plugin({"enabled": True})
        self.plugin._db = MagicMock()
        self.plugin._db.count_records.return_value = 0
        self.plugin._db.list_records.return_value = []
        res = self.plugin._api_get_records(page="x", page_size="y")
        self.assertEqual((res["page"], res["page_size"]), (1, 20))

    def test_form_is_mobile_friendly(self):
        form, model = self.plugin.get_form()
        self.assertIn("retention_days", model)
        self.assertEqual(model["retention_days"], 0)

        def walk(node):
            if isinstance(node, dict):
                yield node
                for v in node.values():
                    yield from walk(v)
            elif isinstance(node, list):
                for v in node:
                    yield from walk(v)

        cols = [n for n in walk(form) if n.get("component") == "VCol"]
        self.assertTrue(cols)
        # 手机端 (cols=12) 一律单列堆叠
        self.assertTrue(all(c["props"].get("cols") == 12 for c in cols))
        # 所有表单 model 都有默认值
        models = {n["props"]["model"] for n in walk(form) if isinstance(n.get("props"), dict) and "model" in n["props"]}
        self.assertTrue(models.issubset(model.keys()))

    def test_retention_service_and_purge(self):
        self.plugin.init_plugin({"enabled": True, "retention_days": "30"})
        self.assertEqual(self.plugin._retention_days, 30)
        self.plugin._db = MagicMock()
        self.plugin._db.purge_expired.return_value = 3
        self.assertEqual(self.plugin._api_purge()["deleted"], 3)
        self.plugin._db.purge_expired.assert_called_with(days=30)
        ids = [s["id"] for s in self.plugin.get_service()]
        if "pan123_tg_purge_job" not in ids:  # 无 apscheduler 的最小环境允许缺省
            self.assertEqual(ids, ["pan123_tg_check_job"])

    def test_retention_zero_means_forever(self):
        self.plugin.init_plugin({"enabled": True, "retention_days": 0})
        self.plugin._db = MagicMock()
        self.assertEqual(self.plugin._api_purge()["deleted"], 0)
        self.plugin._db.purge_expired.assert_not_called()
        self.assertEqual([s["id"] for s in self.plugin.get_service()], ["pan123_tg_check_job"])

    def test_retention_invalid_value_falls_back(self):
        self.plugin.init_plugin({"enabled": True, "retention_days": "abc"})
        self.assertEqual(self.plugin._retention_days, 0)
        self.plugin.init_plugin({"enabled": True, "retention_days": -5})
        self.assertEqual(self.plugin._retention_days, 0)


if __name__ == "__main__":
    unittest.main()

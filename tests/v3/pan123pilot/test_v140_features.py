import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

plugins_v3_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../plugins.v3"))
if plugins_v3_dir not in sys.path:
    sys.path.insert(0, plugins_v3_dir)

from pan123pilot.qb_helper import QBHelper
from pan123pilot.uploader import TaskScheduler, SCRAPED_EXTENSIONS, VIDEO_EXTENSIONS


class TestV140Features(unittest.TestCase):
    def test_qb_get_free_space_api(self):
        qb = QBHelper(host="http://127.0.0.1:8080", username="admin", password="pwd")
        with patch.object(qb, "_request") as mock_req:
            mock_req.return_value = (
                True,
                {"server_state": {"free_space_on_disk": 10 * 1024 * 1024 * 1024}},
            )
            free_bytes = qb.get_free_space_bytes()
            self.assertEqual(free_bytes, 10 * 1024 * 1024 * 1024)

    def test_qb_get_free_space_fallback(self):
        qb = QBHelper(host="", username="", password="")
        with tempfile.TemporaryDirectory() as tmpdir:
            free_bytes = qb.get_free_space_bytes(local_path=tmpdir)
            self.assertGreater(free_bytes, 0)

    def test_cleanup_emergency_disk_space_reclamation(self):
        mock_client = MagicMock()
        mock_qb = MagicMock()
        mock_db = MagicMock()
        mock_notify = MagicMock()

        scheduler = TaskScheduler(
            client=mock_client,
            qb_helper=mock_qb,
            db_helper=mock_db,
            tags_config={},
            notify_callback=mock_notify,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            video_file = os.path.join(tmpdir, "movie.mkv")
            nfo_file = os.path.join(tmpdir, "movie.nfo")
            srt_file = os.path.join(tmpdir, "movie.zh-cn.srt")
            poster_file = os.path.join(tmpdir, "folder.jpg")

            for f in [video_file, nfo_file, srt_file, poster_file]:
                with open(f, "w") as fp:
                    fp.write("dummy content")

            # 模拟仅剩 2GB (低于设定的 5GB 阈值)
            mock_qb.get_free_space_bytes.return_value = 2 * (1024 ** 3)
            # 种子即使做种未达标 (False)
            mock_qb.check_seeding_criteria.return_value = (False, "未达标")

            mock_db.list_tasks_by_status.return_value = [
                {
                    "id": 1,
                    "torrent_hash": "hash_123",
                    "target_path": video_file,
                }
            ]

            scheduler.cleanup_completed_tasks(
                min_seed_time_hours=48.0,
                min_ratio=2.0,
                delete_torrent_file=True,
                delete_hardlink_file=True,
                emergency_free_space_gb=5.0,
            )

            # 验证即使未达标，也因为紧急空间腾退被强制清理
            mock_qb.delete_torrent.assert_called_with("hash_123", delete_files=True)
            self.assertFalse(os.path.exists(video_file))
            # 伴生文件一同被安全清理
            self.assertFalse(os.path.exists(nfo_file))
            self.assertFalse(os.path.exists(srt_file))
            self.assertFalse(os.path.exists(poster_file))
            mock_db.update_task_status.assert_called_with(
                task_id=1,
                status="CLEANED",
                error_msg="已完成自动清理: 🚨 磁盘空间告急 (剩余 2.00GB < 阈值 5.0GB)，紧急腾退强制清理",
            )
            # 验证通知标题包含紧急腾退
            self.assertIn("紧急腾退", mock_notify.call_args[0][0])

    def test_scraped_asset_direct_upload_on_reuse_miss(self):
        mock_client = MagicMock()
        mock_qb = MagicMock()
        mock_db = MagicMock()

        scheduler = TaskScheduler(
            client=mock_client,
            qb_helper=mock_qb,
            db_helper=mock_db,
            tags_config={},
        )

        with tempfile.NamedTemporaryFile(suffix=".nfo") as tmp_nfo:
            mock_db.list_tasks_by_status.return_value = [
                {
                    "id": 99,
                    "target_path": tmp_nfo.name,
                    "torrent_hash": "dummy_hash",
                    "sha1": "sha1_nfo",
                    "md5": "md5_nfo",
                    "file_size": 1024,
                    "retry_count": 0,
                    "max_retry": 10,
                }
            ]
            # 模拟秒传未命中
            mock_client.sha1_reuse.return_value = (False, None, "未命中")
            # 模拟真实上传成功
            mock_client.upload_file_v2.return_value = (True, 99999, "ok")

            scheduler.process_pending_tasks(enable_real_upload=True)

            # 验证伴生文件直接启动真实上传，而不是等待 10 次重试
            mock_client.upload_file_v2.assert_called_once()
            mock_db.update_task_status.assert_called_with(
                task_id=99,
                status="COMPLETED",
                sha1="sha1_nfo",
                md5="md5_nfo",
                file_id=99999,
                parent_file_id=0,
                error_msg="分片真实上传完成",
            )

    def test_seeding_criteria_zero_ratio_only_checks_time(self):
        qb = QBHelper(host="http://127.0.0.1:8080", username="admin", password="pwd")
        with patch.object(qb, "get_torrent") as mock_get_torrent:
            # 模拟种子已做种 0.5 小时，分享率 0.05
            mock_get_torrent.return_value = {
                "seeding_time": 1800,  # 0.5h
                "ratio": 0.05,
            }
            # 当用户设置最低做种时间 2.0h，最低分享率 0.0 时（代表不考核分享率）
            qualified, reason = qb.check_seeding_criteria(
                torrent_hash="test_hash",
                min_seed_time_hours=2.0,
                min_ratio=0.0,
            )
            # 必须判定为未达标，严禁因 0.05 >= 0.0 导致提前清理
            self.assertFalse(qualified)
            self.assertIn("未达做种时长目标", reason)

            # 当做种满 2.5 小时后
            mock_get_torrent.return_value = {
                "seeding_time": 9000,  # 2.5h
                "ratio": 0.05,
            }
            qualified, reason = qb.check_seeding_criteria(
                torrent_hash="test_hash",
                min_seed_time_hours=2.0,
                min_ratio=0.0,
            )
            self.assertTrue(qualified)
            self.assertIn("做种时长已达标", reason)

    def test_single_file_transfer_includes_companions(self):
        from pan123pilot import _record_transferred_target
        with tempfile.TemporaryDirectory() as tmpdir:
            media_folder = os.path.join(tmpdir, "假如爱有如果 (2026)")
            os.makedirs(media_folder, exist_ok=True)
            video_file = os.path.join(media_folder, "假如爱有如果.mkv")
            nfo_file = os.path.join(media_folder, "假如爱有如果.nfo")
            srt_file = os.path.join(media_folder, "假如爱有如果.zh-CN.srt")
            poster_file = os.path.join(media_folder, "poster.jpg")

            for f in [video_file, nfo_file, srt_file, poster_file]:
                with open(f, "w") as fp:
                    fp.write("dummy")

            # 模拟 MoviePilot TransferComplete 事件传入单个视频文件路径
            with patch("pan123pilot._plugin_instance", None):
                task_ids = _record_transferred_target(
                    target_path=video_file,
                    torrent_hash="hash_abc",
                    sync_scraped_assets=True,
                )
                # 必须检索到视频本身 + nfo + srt + poster 全部 4 个文件
                self.assertEqual(len(task_ids), 4)


if __name__ == "__main__":
    unittest.main()

"""TaskScheduler 与 QBHelper 单元测试"""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

# 支持独立单测时导入 plugins.v3 源码
plugins_v3_dir = Path(__file__).resolve().parents[3] / "plugins.v3"
if str(plugins_v3_dir) not in sys.path:
    sys.path.insert(0, str(plugins_v3_dir))

from pan123pilot.client import Pan123Client
from pan123pilot.models import Pan123TaskDatabase
from pan123pilot.qb_helper import QBHelper
from pan123pilot.uploader import TaskScheduler


class TestTaskScheduler(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock(spec=Pan123Client)
        self.mock_qb = MagicMock(spec=QBHelper)
        self.mock_qb.is_configured.return_value = True

        self.mock_db = MagicMock(spec=Pan123TaskDatabase)
        self.tags = {
            "pending": "123_待处理",
            "uploading": "123_上传中",
            "retrying": "123_待重试",
            "failed": "123_秒传失败",
            "completed": "123_已上传",
        }
        self.scheduler = TaskScheduler(
            client=self.mock_client,
            qb_helper=self.mock_qb,
            db_helper=self.mock_db,
            tags_config=self.tags,
        )

    def test_calculate_file_hashes(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(b"Hello MoviePilot and 123pan!")
            temp_path = f.name

        try:
            sha1, md5, size = TaskScheduler.calculate_file_hashes(temp_path)
            self.assertEqual(len(sha1), 40)
            self.assertEqual(len(md5), 32)
            self.assertEqual(size, len(b"Hello MoviePilot and 123pan!"))
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_qb_seeding_criteria(self):
        qb = QBHelper(host="http://localhost:8080")
        qb.get_torrent = MagicMock(return_value={
            "seeding_time": 49 * 3600,  # 49 hours
            "ratio": 1.5,
        })
        qualified, reason = qb.check_seeding_criteria("mock_hash", min_seed_time_hours=48.0, min_ratio=2.0)
        self.assertTrue(qualified)
        self.assertIn("做种已达标", reason)

        qb.get_torrent = MagicMock(return_value={
            "seeding_time": 10 * 3600,  # 10 hours
            "ratio": 0.5,
        })
        qualified2, reason2 = qb.check_seeding_criteria("mock_hash", min_seed_time_hours=48.0, min_ratio=2.0)
        self.assertFalse(qualified2)
        self.assertIn("未达做种目标", reason2)

    def test_process_pending_tasks_hit_reuse(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(b"avatar video sample content")
            target_file = f.name

        try:
            self.mock_db.list_tasks_by_status.return_value = [
                {
                    "id": 1,
                    "target_path": target_file,
                    "torrent_hash": "hash123",
                    "file_name": os.path.basename(target_file),
                    "file_size": 27,
                    "sha1": "dummy_sha1",
                    "md5": "dummy_md5",
                    "parent_file_id": 0,
                    "retry_count": 0,
                    "max_retry": 10,
                }
            ]
            self.mock_client.sha1_reuse.return_value = (True, 99999, "秒传成功")

            self.scheduler.process_pending_tasks(enable_real_upload=True)

            self.mock_db.update_task_status.assert_any_call(
                task_id=1,
                status="COMPLETED",
                sha1="dummy_sha1",
                md5="dummy_md5",
                file_id=99999,
                error_msg="",
            )
            self.mock_qb.update_tags.assert_any_call("hash123", "123_已上传", self.scheduler.all_plugin_tags)
        finally:
            if os.path.exists(target_file):
                os.remove(target_file)


if __name__ == "__main__":
    unittest.main()

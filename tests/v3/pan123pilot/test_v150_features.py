"""Pan123Pilot v1.5.0 新特性单元测试 (并发单飞锁、C级单哈希、Web真实上传、数据库分页与UI看板)"""

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

plugins_v3_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../plugins.v3"))
if plugins_v3_dir not in sys.path:
    sys.path.insert(0, plugins_v3_dir)

from pan123pilot import Pan123Pilot
from pan123pilot.models import Pan123Task, Pan123TaskDatabase
from pan123pilot.uploader import TaskScheduler
from pan123pilot.web_client import Pan123WebClient


class TestV150Features(unittest.TestCase):
    def test_single_flight_lock_prevents_concurrent_duplicate_hashing(self):
        """验证 29 个并发线程同时触发 process_pending_tasks 时，单飞互斥锁保证同一文件绝不重复读盘计算哈希"""
        mock_client = MagicMock()
        mock_client.auth_mode = "web_token"
        mock_client.sha1_reuse.return_value = (True, 88888, "秒传命中")
        mock_qb = MagicMock()
        mock_qb.is_configured.return_value = False
        mock_db = MagicMock()

        with tempfile.NamedTemporaryFile("wb", suffix=".mkv", delete=False) as tmp:
            tmp.write(b"x" * 1024 * 64)
            video_path = tmp.name

        try:
            task_state = {"status": "PENDING"}

            def list_by_status(statuses):
                if task_state["status"] in statuses:
                    time.sleep(0.05)  # 模拟数据库与I/O耗时，放大并发窗口
                    return [
                        {
                            "id": 1,
                            "target_path": video_path,
                            "torrent_hash": "hash_29ep",
                            "file_name": os.path.basename(video_path),
                            "file_size": 65536,
                            "sha1": None,
                            "md5": None,
                            "parent_file_id": 0,
                            "retry_count": 0,
                            "max_retry": 10,
                        }
                    ]
                return []

            def update_status(**kwargs):
                task_state["status"] = kwargs.get("status", "COMPLETED")
                return True

            mock_db.list_tasks_by_status.side_effect = list_by_status
            mock_db.update_task_status.side_effect = update_status

            scheduler = TaskScheduler(
                client=mock_client,
                qb_helper=mock_qb,
                db_helper=mock_db,
                tags_config={},
            )

            # 29 个线程同时调用 process_pending_tasks
            threads = [
                threading.Thread(target=scheduler.process_pending_tasks, kwargs={"enable_real_upload": True})
                for _ in range(29)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            # 核心断言：尽管 29 个线程并发触发，sha1_reuse 最多只被调用 1 次！
            self.assertEqual(mock_client.sha1_reuse.call_count, 1)
        finally:
            if os.path.exists(video_path):
                os.remove(video_path)

    def test_video_prioritized_before_scraped_companions(self):
        """验证队列中混合正片视频与 .nfo/.jpg 伴生文件时，正片优先秒传"""
        mock_client = MagicMock()
        mock_client.auth_mode = "web_token"
        call_order = []

        def fake_reuse(filename, **kwargs):
            call_order.append(filename)
            return True, 1001, "ok"

        mock_client.sha1_reuse.side_effect = fake_reuse
        mock_qb = MagicMock()
        mock_qb.is_configured.return_value = False
        mock_db = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            nfo_path = os.path.join(tmpdir, "tvshow.nfo")
            jpg_path = os.path.join(tmpdir, "poster.jpg")
            ep1_path = os.path.join(tmpdir, "S01E01.mkv")
            ep2_path = os.path.join(tmpdir, "S01E02.mp4")
            for p in (nfo_path, jpg_path, ep1_path, ep2_path):
                with open(p, "wb") as f:
                    f.write(b"content")

            mock_db.list_tasks_by_status.return_value = [
                {"id": 1, "target_path": nfo_path, "torrent_hash": "h", "file_size": 7, "sha1": "a", "md5": "a"},
                {"id": 2, "target_path": jpg_path, "torrent_hash": "h", "file_size": 7, "sha1": "b", "md5": "b"},
                {"id": 3, "target_path": ep1_path, "torrent_hash": "h", "file_size": 7, "sha1": "c", "md5": "c"},
                {"id": 4, "target_path": ep2_path, "torrent_hash": "h", "file_size": 7, "sha1": "d", "md5": "d"},
            ]

            scheduler = TaskScheduler(
                client=mock_client,
                qb_helper=mock_qb,
                db_helper=mock_db,
                tags_config={},
            )
            scheduler.process_pending_tasks(enable_real_upload=True)

            # 正片 S01E01.mkv 和 S01E02.mp4 必须在 tvshow.nfo 和 poster.jpg 之前执行
            self.assertEqual(call_order[:2], ["S01E01.mkv", "S01E02.mp4"])
            self.assertEqual(set(call_order[2:]), {"tvshow.nfo", "poster.jpg"})

    def test_web_client_upload_file_v2_on_scraped_companion(self):
        """验证 Pan123WebClient 实现 upload_file_v2，伴生文件不会抛出 AttributeError"""
        web_client = Pan123WebClient(token="dummy_token", auth_mode="web_token")
        with tempfile.NamedTemporaryFile("wb", suffix=".nfo", delete=False) as tmp:
            tmp.write(b"<movie><title>Test</title></movie>")
            nfo_path = tmp.name

        try:
            with patch.object(web_client, "ensure_authenticated", return_value=(True, "ok")), \
                 patch.object(web_client, "_http_post") as mock_post, \
                 patch("urllib.request.urlopen") as mock_urlopen:

                # 依次模拟: 1. upload_request 返回 S3 参数; 2. s3_re_ense_upload_parts 返回预签名URL; 3. complete 返回成功
                mock_post.side_effect = [
                    (
                        True,
                        {
                            "code": 0,
                            "data": {
                                "Reuse": False,
                                "FileId": 777666,
                                "Bucket": "bkt",
                                "Key": "k1",
                                "UploadId": "u1",
                                "StorageNode": "s1",
                                "SliceSize": 1048576,
                            },
                        },
                        "ok",
                    ),
                    (
                        True,
                        {
                            "code": 0,
                            "data": {
                                "presignedUrls": {"1": "https://s3.example.com/upload/part1"}
                            },
                        },
                        "ok",
                    ),
                    (
                        True,
                        {
                            "code": 0,
                            "data": {"FileId": 777666},
                        },
                        "ok",
                    ),
                ]
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.__enter__.return_value = mock_resp
                mock_urlopen.return_value = mock_resp

                ok, fid, msg = web_client.upload_file_v2(nfo_path, parent_file_id=100)
                self.assertTrue(ok)
                self.assertEqual(fid, 777666)
        finally:
            if os.path.exists(nfo_path):
                os.remove(nfo_path)

    def test_get_page_renders_vtable_and_expansion_panels(self):
        """验证 Pan123Pilot.get_page() 渲染统计卡片、VTable 数据行及 VExpansionPanels 分页"""
        plugin = Pan123Pilot()
        mock_db = MagicMock()
        mock_db.get_status_counts.return_value = {
            "TOTAL": 25,
            "COMPLETED": 20,
            "CLEANED": 3,
            "PENDING": 1,
            "UPLOADING": 0,
            "RETRYING": 1,
            "FAILED": 0,
        }
        fake_records = [
            {
                "id": i,
                "file_name": f"庆余年.S02E{i:02d}.2160p.mkv",
                "target_path": f"/video/tv/庆余年/Season 2/庆余年.S02E{i:02d}.2160p.mkv",
                "file_size": 3 * 1024 * 1024 * 1024,
                "status": "COMPLETED",
                "file_id": 900000 + i,
                "parent_file_id": 5001,
                "retry_count": 0,
                "max_retry": 10,
                "error_msg": "",
                "updated_at": "2026-11-06 22:00:00",
            }
            for i in range(1, 26)
        ]
        mock_db.list_tasks_paginated.return_value = fake_records
        plugin.db_helper = mock_db
        plugin._enabled = True
        plugin._local_library_root = "/video"

        page = plugin.get_page()
        self.assertIsInstance(page, list)
        page_str = str(page)
        self.assertIn("VTable", page_str)
        self.assertIn("VExpansionPanels", page_str)
        self.assertIn("庆余年.S02E01.2160p.mkv", page_str)
        self.assertIn("庆余年.S02E25.2160p.mkv", page_str)
        self.assertIn("已秒传", page_str)

    def test_real_sqlite_database_pagination_counts_and_purge(self):
        """使用真实 SQLite + SQLAlchemy 验证状态聚合、分页模糊检索与超期清理"""
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
        except ImportError:
            self.skipTest("SQLAlchemy not installed")

        engine = create_engine("sqlite:///:memory:")
        Pan123Task.__table__.create(bind=engine, checkfirst=True)
        SessionLocal = sessionmaker(bind=engine)
        db = Pan123TaskDatabase()

        with SessionLocal() as session:
            tid1 = db.add_or_update_task(
                db=session,
                target_path="/video/movies/流浪地球2.mkv",
                file_name="流浪地球2.mkv",
                file_size=10 * 1024 * 1024 * 1024,
                torrent_hash="h1",
            )
            tid2 = db.add_or_update_task(
                db=session,
                target_path="/video/movies/流浪地球2.nfo",
                file_name="流浪地球2.nfo",
                file_size=4096,
                torrent_hash="h1",
            )
            db.update_task_status(
                db=session,
                task_id=tid1,
                status="COMPLETED",
                file_id=123456,
                parent_file_id=888,
            )
            session.commit()

            counts = db.get_status_counts(db=session)
            self.assertEqual(counts["TOTAL"], 2)
            self.assertEqual(counts["COMPLETED"], 1)
            self.assertEqual(counts["PENDING"], 1)

            self.assertEqual(db.count_tasks(db=session, keyword="流浪地球2"), 2)
            self.assertEqual(db.count_tasks(db=session, status="COMPLETED"), 1)

            page_items = db.list_tasks_paginated(db=session, limit=1, offset=0, status="COMPLETED")
            self.assertEqual(len(page_items), 1)
            self.assertEqual(page_items[0]["file_name"], "流浪地球2.mkv")
            self.assertEqual(page_items[0]["file_id"], 123456)

            # 默认 days=0 不清理
            self.assertEqual(db.purge_expired_tasks(db=session, days=0), 0)


if __name__ == "__main__":
    unittest.main()

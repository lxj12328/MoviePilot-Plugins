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

    def test_get_page_renders_interactive_pagination_and_filters(self):
        """验证 Pan123Pilot.get_page() 渲染统计卡片、VTable 数据行及交互式分页控件（首页/上一页/页码/下一页/末页）"""
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
        mock_db.count_tasks.return_value = 25
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
            for i in range(1, 16)
        ]
        mock_db.list_tasks_paginated.return_value = fake_records
        plugin.db_helper = mock_db
        plugin._enabled = True
        plugin._local_library_root = "/video"

        page = plugin.get_page()
        self.assertIsInstance(page, list)
        page_str = str(page)
        self.assertIn("VTable", page_str)
        self.assertIn("⏮ 首页", page_str)
        self.assertIn("◀ 上一页", page_str)
        self.assertIn("下一页 ▶", page_str)
        self.assertIn("末页 ⏭", page_str)
        self.assertIn("plugin/Pan123Pilot/ui_page", page_str)
        self.assertIn("庆余年.S02E01.2160p.mkv", page_str)
        self.assertIn("庆余年.S02E15.2160p.mkv", page_str)
        self.assertIn("已秒传", page_str)

        # 测试点击跳转第 2 页（验证 V3 标准三段式信封: 顶层仅含 success/message/data）
        res = plugin._api_ui_page(page=2)
        self.assertEqual(set(res.keys()), {"success", "message", "data"})
        self.assertTrue(res["success"])
        self.assertEqual(res["data"]["code"], 0)
        self.assertEqual(plugin._ui_page, 2)

        # 测试切换每页条数/筛选类型（会自动重置页码为 1）
        res2 = plugin._api_ui_page(page_size=30, file_type="companion", status="COMPLETED_GROUP")
        self.assertEqual(set(res2.keys()), {"success", "message", "data"})
        self.assertTrue(res2["success"])
        self.assertEqual(res2["data"]["code"], 0)
        self.assertEqual(plugin._ui_page, 1)
        self.assertEqual(plugin._ui_page_size, 30)
        self.assertEqual(plugin._ui_file_type, "companion")
        self.assertEqual(plugin._ui_status, "COMPLETED_GROUP")

    def test_companion_files_never_trigger_qb_cleanup_or_bind_hash(self):
        """验证刮削伴生文件 (.nfo/.jpg/.png/.srt) 不关联 qB 种子 Hash，也不触发 qB 自动清理"""
        mock_db = MagicMock()
        mock_db.list_tasks_by_status.return_value = [
            {
                "id": 101,
                "file_name": "tvshow.nfo",
                "target_path": "/video/tv/Small Town Remedy (2026)/tvshow.nfo",
                "torrent_hash": None,
                "status": "COMPLETED",
            },
            {
                "id": 102,
                "file_name": "poster.jpg",
                "target_path": "/video/tv/Small Town Remedy (2026)/poster.jpg",
                "torrent_hash": "hash_should_be_ignored",
                "status": "COMPLETED",
            },
        ]
        mock_qb = MagicMock()
        mock_qb.is_configured.return_value = True

        scheduler = TaskScheduler(
            db_helper=mock_db,
            client=MagicMock(),
            qb_helper=mock_qb,
            tags_config={},
        )
        scheduler.cleanup_completed_tasks(
            min_seed_time_hours=1.0,
            min_ratio=1.0,
            delete_torrent_file=True,
            delete_hardlink_file=True,
            emergency_free_space_gb=0.0,
        )
        # 刮削伴生文件必须全部跳过，绝不执行清理或标记“无对应 QB 种子”
        mock_db.update_task_status.assert_not_called()
        mock_qb.delete_torrent.assert_not_called()

    def test_real_sqlite_database_pagination_counts_and_purge(self):
        """使用真实 SQLite + SQLAlchemy 验证状态聚合、文件类型过滤、旧数据修复与分页模糊检索"""
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
                target_path="/video/movies/poster.jpg",
                file_name="poster.jpg",
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
            # 模拟 v1.5.0 误标为 CLEANED + 无对应 QB 种子的伴生文件记录
            db.update_task_status(
                db=session,
                task_id=tid2,
                status="CLEANED",
                file_id=654321,
                error_msg="已完成自动清理: 无对应 QB 种子",
            )
            session.commit()

            # 执行历史脏数据自动修复
            fixed = db.fix_companion_records(db=session)
            self.assertGreaterEqual(fixed, 1)

            # 验证修复后 poster.jpg 恢复为 COMPLETED、清空了 error_msg 和 torrent_hash
            companion_items = db.list_tasks_paginated(db=session, file_type="companion")
            self.assertEqual(len(companion_items), 1)
            self.assertEqual(companion_items[0]["file_name"], "poster.jpg")
            self.assertEqual(companion_items[0]["status"], "COMPLETED")
            self.assertEqual(companion_items[0]["error_msg"], "")
            self.assertIsNone(companion_items[0]["torrent_hash"])

            # 验证按正片视频筛选
            video_counts = db.get_status_counts(db=session, file_type="video")
            self.assertEqual(video_counts["TOTAL"], 1)
            self.assertEqual(video_counts["COMPLETED"], 1)

            counts = db.get_status_counts(db=session, file_type="all")
            self.assertEqual(counts["TOTAL"], 2)
            self.assertEqual(counts["COMPLETED"], 2)

            self.assertEqual(db.count_tasks(db=session, keyword="流浪地球2"), 1)
            self.assertEqual(db.count_tasks(db=session, status="COMPLETED", file_type="video"), 1)

            page_items = db.list_tasks_paginated(db=session, limit=1, offset=0, status="COMPLETED", file_type="video")
            self.assertEqual(len(page_items), 1)
            self.assertEqual(page_items[0]["file_name"], "流浪地球2.mkv")
            self.assertEqual(page_items[0]["file_id"], 123456)

            # 默认 days=0 不清理
            self.assertEqual(db.purge_expired_tasks(db=session, days=0), 0)


if __name__ == "__main__":
    unittest.main()

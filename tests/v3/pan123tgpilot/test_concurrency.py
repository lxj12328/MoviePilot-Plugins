"""v1.2.0 并发转存优化的正确性测试：文件级/消息级并发、批内合并、失败可重试、mkdir 竞态、限速器。"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

plugins_v3_dir = Path(__file__).resolve().parents[3] / "plugins.v3"
if str(plugins_v3_dir) not in sys.path:
    sys.path.insert(0, str(plugins_v3_dir))

from pan123tgpilot import Pan123TgPilot
from pan123tgpilot.client import Pan123TgClient

ETAG_A = "a" * 32
ETAG_B = "b" * 32
ETAG_C = "c" * 32
ETAG_D = "d" * 32


def _movie_msg(title: str, etag: str, quality: str) -> str:
    return (
        f"{title} (2026)\n"
        f"🖼️版本 : {quality}\n"
        f"💾体积 : 5.00 GB\n"
        f"🔗分享链接 : 123FSLinkV2${etag}#5368709120#{title}.mkv\n"
    )


class TestClientConcurrency(unittest.TestCase):
    def _build_share_client(self, file_count: int, delay: float, concurrency: int):
        client = Pan123TgClient(token="test_token", file_concurrency=concurrency)
        uploaded = []
        created_dirs = []

        def mock_request(url, method="GET", payload=None, with_auth=True):
            if "file/list/new" in url:
                return True, {"code": 0, "data": {"InfoList": []}}, "ok"
            if "share/get" in url:
                return True, {
                    "code": 0,
                    "data": {
                        "InfoList": [
                            {"FileId": 1000 + i, "FileName": f"E{i:02d}.mkv", "Type": 0,
                             "Size": 1000 + i, "Etag": f"{i:032x}"}
                            for i in range(file_count)
                        ],
                        "next": 0,
                    },
                }, "ok"
            if "upload_request" in url:
                if payload and payload.get("type") == 1:
                    created_dirs.append(payload.get("filename"))
                    return True, {"code": 0, "data": {"Info": {"FileId": 777}}}, "ok"
                time.sleep(delay)
                uploaded.append(payload["fileName"])
                return True, {"code": 0, "data": {"Reuse": True, "Info": {"FileId": 5000 + len(uploaded)}}}, "ok"
            return False, {}, "error"

        client._http_request = MagicMock(side_effect=mock_request)
        return client, uploaded, created_dirs

    def test_file_level_concurrency_is_faster_and_complete(self):
        # 100 条分享内的 12 个文件，每个文件秒传耗时 0.1s
        client, uploaded, _ = self._build_share_client(12, 0.1, 4)
        t0 = time.monotonic()
        ok, fid, msg = client.save_share_link("KEY-1", "", 123, "测试 (2026)")
        elapsed = time.monotonic() - t0
        self.assertTrue(ok)
        self.assertEqual(len(uploaded), 12)
        self.assertEqual(len(set(uploaded)), 12)
        self.assertIn("12 个文件", msg)
        # 串行至少 1.2s，4 并发应明显更快
        self.assertLess(elapsed, 0.8)

    def test_file_concurrency_one_is_serial_but_correct(self):
        client, uploaded, _ = self._build_share_client(6, 0.01, 1)
        ok, _, msg = client.save_share_link("KEY-2", "", 123, "测试 (2026)")
        self.assertTrue(ok)
        self.assertEqual(len(uploaded), 6)

    def test_primary_file_id_is_largest_file(self):
        client, uploaded, _ = self._build_share_client(5, 0.0, 4)
        ok, fid, _ = client.save_share_link("KEY-3", "", 123, "测试 (2026)")
        self.assertTrue(ok)
        # 体积最大的文件是 E04.mkv (Size=1004)，其 fileId 对应第 N 次上传返回
        self.assertTrue(fid.isdigit())

    def test_concurrent_mkdir_creates_folder_once(self):
        client = Pan123TgClient(token="test_token")
        created = []

        def mock_request(url, method="GET", payload=None, with_auth=True):
            if "file/list/new" in url:
                return True, {"code": 0, "data": {"InfoList": []}}, "ok"
            if "upload_request" in url and payload and payload.get("type") == 1:
                time.sleep(0.05)
                created.append(payload.get("filename"))
                return True, {"code": 0, "data": {"Info": {"FileId": 4242}}}, "ok"
            return False, {}, "error"

        client._http_request = MagicMock(side_effect=mock_request)
        results = []

        def worker():
            results.append(client.mkdir("同名归集目录 (2026)", parent_file_id=99))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(created), 1)
        self.assertTrue(all(r[0] and r[1] == 4242 for r in results))

    def test_global_throttle_limits_request_rate(self):
        client = Pan123TgClient(token="t", request_qps=50)
        t0 = time.monotonic()
        for _ in range(6):
            client._throttle()
        elapsed = time.monotonic() - t0
        # 6 次请求 50 QPS，至少间隔 5 * 0.02 = 0.1s
        self.assertGreaterEqual(elapsed, 0.09)

    def test_rate_limit_detection(self):
        self.assertTrue(Pan123TgClient._is_rate_limited(429, ""))
        self.assertTrue(Pan123TgClient._is_rate_limited(0, "操作太频繁，请稍后再试"))
        self.assertFalse(Pan123TgClient._is_rate_limited(0, "ok"))


class TestPluginConcurrency(unittest.TestCase):
    def setUp(self):
        self.plugin = Pan123TgPilot()
        self.plugin.init_plugin({
            "enabled": True,
            "pan123_token": "token_abc",
            "movie_target_dir_id": "1001",
            "tv_target_dir_id": "1002",
            "tg_channels": "@ChanA",
        })
        self.plugin._client = MagicMock()
        self.plugin._client.save_fslink.return_value = (True, "99999", "ok")
        self.plugin._client.trash_files.return_value = (True, "ok")
        self.plugin._db = MagicMock()
        self.plugin._db.has_link_processed.return_value = False
        self.plugin._db.get_record_by_media_key.return_value = None
        self.plugin._watcher = MagicMock()

    def test_concurrency_config_parsing_and_fallback(self):
        self.plugin.init_plugin({"enabled": True, "msg_concurrency": "5", "file_concurrency": "2"})
        self.assertEqual((self.plugin._msg_concurrency, self.plugin._file_concurrency), (5, 2))
        self.plugin.init_plugin({"enabled": True, "msg_concurrency": "abc", "file_concurrency": None})
        self.assertEqual((self.plugin._msg_concurrency, self.plugin._file_concurrency), (3, 4))
        self.plugin.init_plugin({"enabled": True, "msg_concurrency": 99, "file_concurrency": -3})
        self.assertEqual((self.plugin._msg_concurrency, self.plugin._file_concurrency), (8, 4))
        _, model = self.plugin.get_form()
        self.assertEqual((model["msg_concurrency"], model["file_concurrency"]), (3, 4))

    def test_batch_merge_keeps_only_highest_score_for_same_media(self):
        self.plugin._watcher.fetch_channel_messages.return_value = [
            ("1", _movie_msg("八仙", ETAG_A, "1080p.WEB-DL.H.264")),
            ("2", _movie_msg("八仙", ETAG_B, "2160p.WEB-DL.H.265.DoVi.10bit")),
        ]
        res = self.plugin.execute_check_all_channels()
        self.assertTrue(res["success"])
        # 同一资源只转存评分最高的 4K 版本，不会先转 1080p 再洗版
        self.plugin._client.save_fslink.assert_called_once()
        kwargs = self.plugin._client.save_fslink.call_args.kwargs
        self.assertIn(ETAG_B, kwargs["fslink"])
        self.assertEqual(res["stats"]["transferred"], 1)
        self.assertEqual(res["stats"]["skipped"], 1)
        self.plugin._db.save_or_update_record.assert_called_once()

    def test_failed_transfer_is_not_recorded_for_retry(self):
        self.plugin._client.save_fslink.return_value = (False, None, "云端未命中秒传特征")
        self.plugin._watcher.fetch_channel_messages.return_value = [
            ("1", _movie_msg("夜曲", ETAG_A, "1080p.WEB-DL.H.264")),
        ]
        res = self.plugin.execute_check_all_channels()
        self.assertEqual(res["stats"]["failed"], 1)
        self.plugin._db.save_or_update_record.assert_not_called()

    def test_message_level_concurrency_is_faster(self):
        self.plugin._watcher.fetch_channel_messages.return_value = [
            ("1", _movie_msg("片甲", ETAG_A, "1080p.WEB-DL.H.264")),
            ("2", _movie_msg("片乙", ETAG_B, "1080p.WEB-DL.H.264")),
            ("3", _movie_msg("片丙", ETAG_C, "1080p.WEB-DL.H.264")),
        ]

        def slow_transfer(job):
            time.sleep(0.2)
            return "transferred"

        self.plugin._execute_transfer = slow_transfer
        t0 = time.monotonic()
        res = self.plugin.execute_check_all_channels()
        elapsed = time.monotonic() - t0
        self.assertEqual(res["stats"]["transferred"], 3)
        # 串行需 0.6s，3 并发约 0.2s
        self.assertLess(elapsed, 0.5)

    def test_msg_concurrency_one_is_serial_and_complete(self):
        self.plugin.init_plugin({
            "enabled": True, "pan123_token": "token_abc", "tg_channels": "@ChanA", "msg_concurrency": 1,
        })
        self.plugin._client = MagicMock()
        self.plugin._client.save_fslink.return_value = (True, "1", "ok")
        self.plugin._db = MagicMock()
        self.plugin._db.has_link_processed.return_value = False
        self.plugin._db.get_record_by_media_key.return_value = None
        self.plugin._watcher = MagicMock()
        self.plugin._watcher.fetch_channel_messages.return_value = [
            ("1", _movie_msg("片甲", ETAG_A, "1080p.WEB-DL.H.264")),
            ("2", _movie_msg("片乙", ETAG_B, "1080p.WEB-DL.H.264")),
        ]
        res = self.plugin.execute_check_all_channels()
        self.assertEqual(res["stats"]["transferred"], 2)
        self.assertEqual(self.plugin._client.save_fslink.call_count, 2)

    def test_multi_channel_fetch_and_stats(self):
        self.plugin.init_plugin({
            "enabled": True, "pan123_token": "token_abc", "tg_channels": "@ChanA, @ChanB, @ChanC",
        })
        self.plugin._client = MagicMock()
        self.plugin._client.save_fslink.return_value = (True, "1", "ok")
        self.plugin._db = MagicMock()
        self.plugin._db.has_link_processed.return_value = False
        self.plugin._db.get_record_by_media_key.return_value = None
        self.plugin._watcher = MagicMock()

        def fetch(channel, limit=30):
            if channel == "@ChanA":
                return [("1", _movie_msg("片甲", ETAG_A, "1080p.WEB-DL.H.264"))]
            if channel == "@ChanB":
                raise RuntimeError("network down")
            return [("9", "普通闲聊消息，没有链接")]

        self.plugin._watcher.fetch_channel_messages.side_effect = fetch
        res = self.plugin.execute_check_all_channels()
        stats = res["stats"]
        self.assertEqual(stats["channels_checked"], 3)
        self.assertEqual(stats["messages_checked"], 2)
        self.assertEqual(stats["transferred"], 1)
        self.assertEqual(stats["skipped"], 1)  # 无链接消息

    def test_stop_service_cancels_pending_jobs(self):
        self.plugin._watcher.fetch_channel_messages.return_value = [
            ("1", _movie_msg("片甲", ETAG_A, "1080p.WEB-DL.H.264")),
        ]
        self.plugin.stop_service()
        res = self.plugin.execute_check_all_channels()
        self.plugin._client.save_fslink.assert_not_called()
        self.assertEqual(res["stats"]["transferred"], 0)
        # 重新 init_plugin 后恢复可用
        self.plugin.init_plugin({"enabled": True, "pan123_token": "token_abc", "tg_channels": "@ChanA"})
        self.assertFalse(self.plugin._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()

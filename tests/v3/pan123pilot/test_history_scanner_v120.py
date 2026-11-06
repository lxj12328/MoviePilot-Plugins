import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

plugins_v3_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../plugins.v3"))
if plugins_v3_dir not in sys.path:
    sys.path.insert(0, plugins_v3_dir)

import pan123pilot
from pan123pilot import Pan123Pilot, _query_transfer_history
from pan123pilot.models import Pan123TaskDatabase
from pan123pilot.qb_helper import QBHelper


class TestHistoryScannerV120(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()
        pan123pilot._plugin_instance = None

    def test_qb_helper_match_torrent_by_path(self):
        torrents_data = [
            {
                "hash": "hash_sample_movie_123",
                "name": "To.Kill.a.Mastermind.1979.1080p.BluRay.x265.10bit-WiKi",
                "save_path": "/downloads/emby-movies/港台电影",
                "content_path": "/downloads/emby-movies/港台电影/To.Kill.a.Mastermind.1979.1080p.BluRay.x265.10bit-WiKi.mkv",
            },
            {
                "hash": "hash_sample_tv_456",
                "name": "Breaking.Bad.S01",
                "save_path": "/downloads/tv",
                "content_path": "/downloads/tv/Breaking.Bad.S01",
            }
        ]

        helper = QBHelper("http://127.0.0.1:8080", "admin", "admin")
        helper._logged_in = True
        helper._request = MagicMock(return_value=(True, json.dumps(torrents_data)))

        # 1. Match by exact content path
        h1 = helper.match_torrent_by_path("/downloads/emby-movies/港台电影/To.Kill.a.Mastermind.1979.1080p.BluRay.x265.10bit-WiKi.mkv")
        self.assertEqual(h1, "hash_sample_movie_123")

        # 2. Match by fuzzy directory name in dest path
        h2 = helper.match_torrent_by_path("/video/emby-movies/港台电影/To.Kill.a.Mastermind.1979.1080p.BluRay.x265.10bit-WiKi/movie.mkv")
        self.assertEqual(h2, "hash_sample_movie_123")

        # 3. Match TV series folder
        h3 = helper.match_torrent_by_path("/video/tv/Breaking.Bad.S01/Season 1/Breaking.Bad.S01E01.mkv")
        self.assertEqual(h3, "hash_sample_tv_456")

        # 4. Unmatched path
        h_none = helper.match_torrent_by_path("/video/anime/Unknown_Show_2026/ep01.mkv")
        self.assertIsNone(h_none)

    def test_query_transfer_history(self):
        mock_row = ("test_download_hash_999", "/downloads/movie/test.mkv")

        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = mock_row
        mock_engine.connect.return_value.__enter__.return_value = mock_conn

        mock_app = MagicMock()
        mock_app_db = MagicMock()
        mock_app_db.Engine = mock_engine

        with patch.dict(sys.modules, {"app": mock_app, "app.db": mock_app_db}):
            h, src = _query_transfer_history("/video/movies/test/test.mkv")
            self.assertEqual(h, "test_download_hash_999")
            self.assertEqual(src, "/downloads/movie/test.mkv")

    def test_scan_and_sync_history_library(self):
        # Create directory structure with video files and non-video files
        library_dir = os.path.join(self.temp_dir.name, "library")
        movie_dir = os.path.join(library_dir, "movies", "Movie A (2023)")
        os.makedirs(movie_dir, exist_ok=True)

        vid1 = os.path.join(movie_dir, "Movie.A.2023.mkv")
        nfo1 = os.path.join(movie_dir, "Movie.A.2023.nfo")
        with open(vid1, "wb") as f:
            f.write(b"dummy video content 1")
        with open(nfo1, "w") as f:
            f.write("<movie></movie>")

        mock_db = MagicMock(spec=Pan123TaskDatabase)
        # First query: not in DB (None)
        # Second query: returns existing completed task
        mock_db.get_task_by_target.side_effect = [None, {"status": "COMPLETED"}]
        mock_db.add_or_update_task.return_value = 101

        pilot = Pan123Pilot()
        pilot._db = mock_db
        pilot._enabled = True
        pilot._local_library_root = library_dir
        pilot._client_id = "test"
        pilot._client_secret = "test"
        pilot._client = MagicMock()
        pilot._scheduler = MagicMock()

        mock_qb = MagicMock(spec=QBHelper)
        mock_qb.is_configured.return_value = True
        pilot.qb_helper = mock_qb

        pan123pilot._plugin_instance = pilot

        # Mock module-level query transfer history
        with patch("pan123pilot._query_transfer_history", return_value=("hash_movie_a", "/downloads/Movie.A.2023.mkv")):
            # 1. Execute first scan
            recorded_count = pilot._scan_and_sync_history_library()
            self.assertEqual(recorded_count, 1)

            # Verify task was added to DB
            mock_db.add_or_update_task.assert_called_once()
            call_kwargs = mock_db.add_or_update_task.call_args[1]
            self.assertEqual(call_kwargs["target_path"], vid1)
            self.assertEqual(call_kwargs["torrent_hash"], "hash_movie_a")
            self.assertEqual(call_kwargs["source_path"], "/downloads/Movie.A.2023.mkv")

            # Verify qB helper tag was called
            mock_qb.update_tags.assert_called_once()
            self.assertEqual(mock_qb.update_tags.call_args[0][0], "hash_movie_a")
            self.assertEqual(mock_qb.update_tags.call_args[0][1], "123_待处理")

            # 2. Second scan should skip already completed task
            recorded_second = pilot._scan_and_sync_history_library()
            self.assertEqual(recorded_second, 0)


if __name__ == "__main__":
    unittest.main()

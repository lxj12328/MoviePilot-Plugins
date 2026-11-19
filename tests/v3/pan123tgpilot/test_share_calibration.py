import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

plugins_v3_dir = Path(__file__).resolve().parents[3] / "plugins.v3"
if str(plugins_v3_dir) not in sys.path:
    sys.path.insert(0, str(plugins_v3_dir))

from pan123tgpilot import Pan123TgPilot
from pan123tgpilot.client import Pan123TgClient
import pan123tgpilot.parser as parser_mod
from pan123tgpilot.parser import TgMessageParser


class _InMemoryFakeDb:
    """无 SQLAlchemy 环境下的内存数据库桩，用于端到端验证校准与跨频道去重逻辑"""

    def __init__(self):
        self.records: list[dict] = []
        self._next_id = 1

    def ensure_table(self):
        return None

    def get_record_by_link(self, share_url: str = "", fslink: str = ""):
        for rec in reversed(self.records):
            if share_url and rec.get("share_url") == share_url:
                return dict(rec)
            if fslink and rec.get("fslink") == fslink:
                return dict(rec)
        return None

    def has_link_processed(self, share_url: str = "", fslink: str = "") -> bool:
        return self.get_record_by_link(share_url=share_url, fslink=fslink) is not None

    def get_record_by_media_key(
        self,
        media_key: str = "",
        title: str = "",
        year: str = "",
        tmdb_id: str = "",
        media_type: str = "",
        ep_tag: str = "",
    ):
        for rec in reversed(self.records):
            if media_key and rec.get("media_key") == media_key:
                return dict(rec)
        return None

    def save_or_update_record(self, **kwargs):
        rec = dict(kwargs)
        replace_id = rec.pop("replace_record_id", None)
        if replace_id:
            for idx, old in enumerate(self.records):
                if old.get("id") == replace_id:
                    rec["id"] = replace_id
                    self.records[idx] = rec
                    return replace_id
        rec["id"] = self._next_id
        self._next_id += 1
        self.records.append(rec)
        return rec["id"]


class TestShareCalibration(unittest.TestCase):
    def setUp(self):
        self.parser = TgMessageParser()

    def test_calibrate_s01_to_s01e01_e20_with_companions_and_trailers(self):
        """验证 S01 文本通过分享文件树校准为 S01E01-E20，并剔除伴生/预告体积且不修改原字典"""
        msg = """🎬 北京女子图鉴 (2018)
🎭 类型：剧集
🖥 画质：2160p
📹 视频：WEB-DL.H.265
📦 大小：92.89GB
S01 4K WEB-DL [国语][内嵌简中字幕]
🔗 链接：[123网盘](https://www.123pan.com/s/kyeA-kBV7v?pwd=t00N#)"""
        parsed = self.parser.parse(msg)
        self.assertEqual(parsed["ep_tag"], "S01")
        parsed_snapshot = dict(parsed)

        gb4 = 4 * (1024 ** 3)
        files = [
            {
                "rel_path": f"北京女子图鉴 (2018)/Season 1/北京女子图鉴.S01E{ep:02d}.2160p.WEB-DL.H265.mp4",
                "name": f"北京女子图鉴.S01E{ep:02d}.2160p.WEB-DL.H265.mp4",
                "size": gb4,
            }
            for ep in range(1, 21)
        ]
        # 添加字幕、NFO 及 Extras/预告片、花絮干扰文件
        files.extend(
            [
                {
                    "rel_path": "北京女子图鉴 (2018)/Season 1/北京女子图鉴.S01E01.chs.srt",
                    "name": "北京女子图鉴.S01E01.chs.srt",
                    "size": 120 * 1024,
                },
                {
                    "rel_path": "北京女子图鉴 (2018)/tvshow.nfo",
                    "name": "tvshow.nfo",
                    "size": 8 * 1024,
                },
                {
                    "rel_path": "北京女子图鉴 (2018)/Extras/trailer.mp4",
                    "name": "trailer.mp4",
                    "size": 300 * (1024 ** 2),
                },
                {
                    "rel_path": "北京女子图鉴 (2018)/花絮_01.mkv",
                    "name": "花絮_01.mkv",
                    "size": 500 * (1024 ** 2),
                },
            ]
        )

        calibrated = self.parser.calibrate_resource_from_files(parsed, files)
        # 不应修改原资源字典
        self.assertEqual(parsed, parsed_snapshot)
        self.assertEqual(calibrated["ep_tag"], "S01E01-E20")
        self.assertEqual(calibrated["display_title"], "北京女子图鉴 S01E01-E20")
        self.assertEqual(calibrated["media_key"], "tv_北京女子图鉴_S01E01-E20")
        self.assertEqual(calibrated["ep_start"], 1)
        self.assertEqual(calibrated["ep_end"], 20)
        self.assertTrue(calibrated["is_ep_range"])
        self.assertEqual(calibrated["file_size"], 20 * gb4)
        self.assertEqual(calibrated["video_count"], 20)

    def test_calibrate_multi_season_nested_dirs(self):
        """验证嵌套多季目录 (爸爸去哪儿 S01-S06/Season X/XX.mp4) 校准为 S01-S06"""
        base = {
            "title": "爸爸去哪儿",
            "display_title": "爸爸去哪儿",
            "media_title": "爸爸去哪儿",
            "year": "2013",
            "tmdb_id": None,
            "media_type": "tv",
            "season": 1,
            "episode": None,
            "ep_start": None,
            "ep_end": None,
            "is_ep_range": False,
            "ep_tag": "S01",
            "resolution": "1080p",
            "codec": "H.265",
            "dovi_hdr": "",
            "file_size": 0,
            "quality_score": 75,
            "raw_text": "爸爸去哪儿\nhttps://www.123pan.com/s/test-123",
        }
        files = []
        for s in range(1, 7):
            for ep in range(1, 13):
                files.append(
                    {
                        "rel_path": f"爸爸去哪儿 S01-S06/Season {s}/{ep:02d}.mp4",
                        "name": f"{ep:02d}.mp4",
                        "size": 2 * (1024 ** 3),
                    }
                )

        calibrated = self.parser.calibrate_resource_from_files(base, files)
        self.assertEqual(calibrated["media_type"], "tv")
        self.assertEqual(calibrated["ep_tag"], "S01-S06")
        self.assertTrue(calibrated["is_ep_range"])
        self.assertEqual(calibrated["video_count"], 72)

    def test_calibrate_season_hint_for_bare_numbered_files(self):
        """验证文本标注 S02 且分享内为纯数字集号 01.mkv..12.mkv 时准确识别为 S02E01-E12"""
        msg = """📺 庆余年 (2024)
🎭 类型：剧集
S02 2160p H.265
🔗 https://www.123pan.com/s/qyn2-abcd"""
        parsed = self.parser.parse(msg)
        files = [
            {"rel_path": f"{ep:02d}.mkv", "name": f"{ep:02d}.mkv", "size": 3 * (1024 ** 3)}
            for ep in range(1, 13)
        ]
        calibrated = self.parser.calibrate_resource_from_files(parsed, files)
        self.assertEqual(calibrated["ep_tag"], "S02E01-E12")
        self.assertEqual(calibrated["season"], 2)
        self.assertEqual(calibrated["ep_start"], 1)
        self.assertEqual(calibrated["ep_end"], 12)

    def test_calibrate_vague_single_ep_fills_missing_specs(self):
        """验证仅标注片名/剧集而缺失规格的消息，从分享文件名反推出 S01E06 及 2160p/H.265/DoVi"""
        msg = """剧集：测试剧
🔗 https://www.123pan.com/s/csj1-efgh"""
        parsed = self.parser.parse(msg)
        files = [
            {
                "rel_path": "测试剧.S01E06.2160p.WEB-DL.H265.DoVi.mkv",
                "name": "测试剧.S01E06.2160p.WEB-DL.H265.DoVi.mkv",
                "size": 5 * (1024 ** 3),
            }
        ]
        calibrated = self.parser.calibrate_resource_from_files(parsed, files)
        self.assertEqual(calibrated["ep_tag"], "S01E06")
        self.assertEqual(calibrated["resolution"], "2160p")
        self.assertEqual(calibrated["codec"], "H.265")
        self.assertEqual(calibrated["dovi_hdr"], "DoVi")

    def test_calibrate_respects_tmdb_movie_lock_and_tv_single_unlabeled(self):
        """验证 TMDB /movie/ 强制锁定电影不被误转为剧集，以及显式剧集单未编号视频不被降级为电影"""
        # (a) TMDB movie 权威锁定
        movie_msg = """🎬 特别电影 (2025)
https://www.themoviedb.org/movie/999001
🔗 https://www.123pan.com/s/mov1-1111"""
        p_movie = self.parser.parse(movie_msg)
        self.assertEqual(p_movie["media_type"], "movie")
        cal_movie = self.parser.calibrate_resource_from_files(
            p_movie,
            [{"rel_path": "特别电影.S01E01.2160p.mkv", "name": "特别电影.S01E01.2160p.mkv", "size": 10 * (1024 ** 3)}],
        )
        self.assertEqual(cal_movie["media_type"], "movie")
        self.assertEqual(cal_movie["ep_tag"], "")

        # (b) 显式 TV 单未编号正片保持 TV
        tv_msg = """🎭 类型：剧集
📺 测试特辑剧 (2025)
S01 1080p
🔗 https://www.123pan.com/s/tv11-2222"""
        p_tv = self.parser.parse(tv_msg)
        self.assertEqual(p_tv["media_type"], "tv")
        cal_tv = self.parser.calibrate_resource_from_files(
            p_tv,
            [{"rel_path": "特辑正片.mp4", "name": "特辑正片.mp4", "size": 2 * (1024 ** 3)}],
        )
        self.assertEqual(cal_tv["media_type"], "tv")
        self.assertEqual(cal_tv["ep_tag"], "S01")

    def test_inspect_share_tree_and_save_share_link_prefetched_single_get(self):
        """验证 inspect_share_tree + save_share_link(prefetched_tree=...) 全程仅一次探测各目录，转存零重复请求 /b/api/share/get"""
        client = Pan123TgClient(token="fake_token")
        share_get_calls = []

        def fake_http(url, method="GET", payload=None, with_auth=True):
            if "/b/api/share/get" in url:
                share_get_calls.append(url)
                if "parentFileId=0" in url:
                    return True, {
                        "code": 0,
                        "data": {
                            "InfoList": [
                                {"FileId": 100, "FileName": "北京女子图鉴 (2018)", "Type": 1, "Size": 0}
                            ],
                            "next": -1,
                        },
                    }, ""
                if "parentFileId=100" in url:
                    return True, {
                        "code": 0,
                        "data": {
                            "InfoList": [
                                {"FileId": 200, "FileName": "Season 1", "Type": 1, "Size": 0}
                            ],
                            "next": -1,
                        },
                    }, ""
                if "parentFileId=200" in url:
                    return True, {
                        "code": 0,
                        "data": {
                            "InfoList": [
                                {
                                    "FileId": 301,
                                    "FileName": "S01E01.mkv",
                                    "Type": 0,
                                    "Etag": "aaa111",
                                    "Size": 1024,
                                },
                                {
                                    "FileId": 302,
                                    "FileName": "S01E02.mkv",
                                    "Type": 0,
                                    "Etag": "bbb222",
                                    "Size": 2048,
                                },
                            ],
                            "next": -1,
                        },
                    }, ""
            return False, {}, "unexpected"

        mkdir_calls = []
        created_dirs = {
            (50, "北京女子图鉴 (2018)"): 501,
            (501, "Season 1"): 502,
        }

        def fake_mkdir(dirname, parent_file_id=0):
            mkdir_calls.append((parent_file_id, dirname))
            return True, created_dirs.get((parent_file_id, dirname), 999), "ok"

        saved_fs = []

        def fake_save_fslink(filename, etag, size, target_parent_id=0, folder_name=""):
            saved_fs.append((target_parent_id, filename, etag, size))
            return True, f"fid_{filename}", "ok"

        client._http_request = fake_http
        client.mkdir = fake_mkdir
        client.list_files = MagicMock(return_value=(True, [], "ok"))
        client.save_fslink = fake_save_fslink

        ok_ins, tree_data, err_ins = client.inspect_share_tree(share_key="kyeA-kBV7v", share_pwd="t00N")
        self.assertTrue(ok_ins, err_ins)
        self.assertEqual(len(tree_data["files"]), 2)
        self.assertEqual(tree_data["files"][0]["rel_path"], "北京女子图鉴 (2018)/Season 1/S01E01.mkv")
        get_count_after_inspect = len(share_get_calls)
        self.assertEqual(get_count_after_inspect, 3)

        ok_save, saved_fid, _ = client.save_share_link(
            share_key="kyeA-kBV7v",
            share_pwd="t00N",
            target_parent_id=50,
            folder_name="北京女子图鉴 (2018)",
            prefetched_tree=tree_data,
        )
        self.assertTrue(ok_save)
        self.assertEqual(saved_fid, "fid_S01E02.mkv")
        # 复用预取树转存期间，零新增 /b/api/share/get 请求
        self.assertEqual(len(share_get_calls), get_count_after_inspect)
        # 确认未出现双层「北京女子图鉴 (2018)/北京女子图鉴 (2018)」套娃目录
        self.assertEqual(mkdir_calls, [(50, "北京女子图鉴 (2018)"), (501, "Season 1")])
        self.assertEqual(
            saved_fs,
            [(502, "S01E01.mkv", "aaa111", 1024), (502, "S01E02.mkv", "bbb222", 2048)],
        )

    def test_plugin_end_to_end_calibration_and_cross_channel_dedup(self):
        """端到端验证：频道 A 转存前通过分享文件树校准为 S01E01-E20，频道 B 同剧不同链接校准后触发去重跳过"""
        plugin = Pan123TgPilot()
        fake_db = _InMemoryFakeDb()
        plugin._db = fake_db
        plugin._tv_target_dir_id = 888
        plugin._movie_target_dir_id = 777
        plugin._auto_upgrade = True
        plugin._share_file_inspect = True
        plugin._notify_enabled = False

        gb4 = 4 * (1024 ** 3)
        tree_files = [
            {
                "rel_path": f"北京女子图鉴 (2018)/S01E{ep:02d}.2160p.WEB-DL.H265.mp4",
                "name": f"S01E{ep:02d}.2160p.WEB-DL.H265.mp4",
                "size": gb4,
                "etag": f"etag{ep:02d}",
            }
            for ep in range(1, 21)
        ]

        mock_client = MagicMock()
        mock_client.inspect_share_tree.side_effect = lambda share_key, share_pwd="": (
            True,
            {
                "host": "https://www.123pan.com",
                "share_key": share_key,
                "share_pwd": share_pwd,
                "root_items": [{"FileId": 1, "FileName": "S01E01.mp4", "Type": 0}],
                "files": tree_files,
            },
            "",
        )
        mock_client.save_share_link.return_value = (True, "9001", "分享转存成功")
        plugin._client = mock_client

        msg_a = """🎬 北京女子图鉴 (2018)
🎭 类型：剧集
🖥 画质：2160p
📹 视频：WEB-DL.H.265
S01 4K WEB-DL
🔗 [123网盘](https://www.123pan.com/s/keyA-1111?pwd=aaaa#)"""

        res_a = plugin._process_single_message("@ChannelA", "101", msg_a)
        self.assertEqual(res_a, "transferred")
        self.assertEqual(mock_client.save_share_link.call_count, 1)
        # 验证传给 save_share_link 的 prefetched_tree 已成功复用
        call_kwargs = mock_client.save_share_link.call_args.kwargs
        self.assertIn("prefetched_tree", call_kwargs)
        self.assertEqual(call_kwargs["prefetched_tree"]["share_key"], "keyA-1111")
        # 验证数据库记录为校准后的 S01E01-E20
        self.assertEqual(len(fake_db.records), 1)
        self.assertEqual(fake_db.records[0]["media_key"], "tv_北京女子图鉴_S01E01-E20")
        self.assertEqual(fake_db.records[0]["media_title"], "北京女子图鉴 S01E01-E20")

        # 频道 B 发布同一部剧但使用不同分享链接且仅标注 S01
        msg_b = """🎬 北京女子图鉴 (2018)
🎭 类型：剧集
🖥 画质：2160p
📹 视频：WEB-DL.H.265
S01 4K WEB-DL
🔗 https://www.123pan.com/s/keyB-2222?pwd=bbbb"""

        res_b = plugin._process_single_message("@ChannelB", "202", msg_b)
        self.assertEqual(res_b, "skipped")
        # 校准后命中同一 media_key 且质量未提升，绝不触发第二次 save_share_link
        self.assertEqual(mock_client.save_share_link.call_count, 1)

    def test_mp_metainfo_and_metainfo_path_stub_and_error_fallback(self):
        """验证接入 MoviePilot MetaInfo / MetaInfoPath 桩工作正常，且宿主接口异常时无损降级本地正则"""
        ok_meta = SimpleNamespace(
            type="电视剧",
            begin_season=2,
            end_season=2,
            begin_episode=1,
            end_episode=10,
            resource_pix="2160p",
            video_encode="HEVC",
            resource_effect="DoVi",
            resource_type="WEB-DL",
        )
        ok_path_meta = SimpleNamespace(
            type="电视剧",
            begin_season=3,
            end_season=3,
            begin_episode=7,
            end_episode=7,
            resource_pix="2160p",
            video_encode="HEVC",
            resource_effect="HDR10+",
            resource_type="BluRay",
        )

        # (a) 正常桩集成测试
        with patch.object(parser_mod, "_MPMetaInfo", side_effect=lambda *a, **kw: ok_meta), patch.object(
            parser_mod, "_MPMetaInfoPath", side_effect=lambda *a, **kw: ok_path_meta
        ):
            parsed = self.parser.parse("🎬 神秘剧集 (2025)\n🎭 类型：剧集\nS02\n🔗 https://www.123pan.com/s/stub-1111")
            self.assertEqual(parsed["ep_tag"], "S02E01-E10")

            season, ep, specs = self.parser._extract_file_se("unlabeled/clip.mkv", "clip.mkv")
            self.assertEqual((season, ep), (3, 7))
            self.assertEqual(specs["resolution"], "2160p")
            self.assertEqual(specs["codec"], "H.265")
            self.assertEqual(specs["dovi_hdr"], "HDR10+")
            self.assertEqual(specs["source"], "BluRay")

        # (b) 异常回退测试
        with patch.object(parser_mod, "_MPMetaInfo", side_effect=RuntimeError("boom")), patch.object(
            parser_mod, "_MPMetaInfoPath", side_effect=RuntimeError("boom")
        ):
            parsed_fb = self.parser.parse(
                "🎬 测试回退剧 (2025)\n🎭 类型：剧集\nS01E03 1080p H.265\n🔗 https://www.123pan.com/s/stub-2222"
            )
            self.assertIsNotNone(parsed_fb)
            self.assertEqual(parsed_fb["ep_tag"], "S01E03")

            s_fb, e_fb, specs_fb = self.parser._extract_file_se(
                "Season 4/测试回退剧.E08.1080p.H265.mp4",
                "测试回退剧.E08.1080p.H265.mp4",
            )
            self.assertEqual((s_fb, e_fb), (4, 8))
            self.assertEqual(specs_fb["resolution"], "1080p")
            self.assertEqual(specs_fb["codec"], "H.265")


if __name__ == "__main__":
    unittest.main()

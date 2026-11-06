import sys
from pathlib import Path
import unittest

plugins_v3_dir = Path(__file__).resolve().parents[3] / "plugins.v3"
if str(plugins_v3_dir) not in sys.path:
    sys.path.insert(0, str(plugins_v3_dir))

from pan123tgpilot.parser import TgMessageParser


class TestTgMessageParser(unittest.TestCase):
    def setUp(self):
        self.parser = TgMessageParser()

    def test_parse_example_1_tv_share(self):
        msg = """
🎬 斗破苍穹 (2017)
🎭 类型：剧集
⭐ TMDB评分：8.0/10
🍅 豆瓣评分：5.5
🖥 画质：
📹 视频：
💬 字幕：简中（内嵌）
📦 大小：1.20GB
👤 分享：匿名
S05E217 4K WEB-DL H.265 10bit 25fps [国语][内嵌简中字幕]
🔗 链接：[123网盘](https://www.123pan.com/s/7Tx1jv-8e4tv?pwd=xoxo#)
📖 简介：
萧炎曾是家族里公认的斗气天才，年仅11岁便已经抵达了常人穷尽一生都无法修炼到的境界。
        """
        parsed = self.parser.parse(msg)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["title"], "斗破苍穹")
        self.assertEqual(parsed["year"], "2017")
        self.assertEqual(parsed["media_type"], "tv")
        self.assertEqual(parsed["season"], 5)
        self.assertEqual(parsed["episode"], 217)
        self.assertEqual(parsed["ep_tag"], "S05E217")
        self.assertEqual(parsed["resolution"], "2160p")
        self.assertEqual(parsed["codec"], "H.265")
        self.assertEqual(parsed["share_key"], "7Tx1jv-8e4tv")
        self.assertEqual(parsed["share_pwd"], "xoxo")
        self.assertAlmostEqual(parsed["file_size"] / (1024 ** 3), 1.20, delta=0.1)

    def test_parse_example_2_movie_fslink(self):
        msg = """
八仙！ (2026)
🏷️标签 : #八仙 #喜剧 #动画 #奇幻 #冒险
📺类型 : 国漫
📅首播 : 2026-07-10
⭐评分 : TMDB: [8.0](https://www.themoviedb.org/movie/1633056) | 豆瓣: [8.2](https://movie.douban.com/subject/36882191/)
🆔TMDB : [1633056](https://www.themoviedb.org/movie/1633056)
📝简介 : “八仙”在成仙和成功之间选择了“成团”，在搞钱和搞事业之间选择了“搞事情”？！
🖼️版本 : 2160p.WEB-DL.H.265.DoVi.10bit.60fps.DTS 5.1
💾体积 : 30.60 GB
🔗分享链接 : 123FSLinkV2$82467be2147f26532298438bf7015238#32855978205#八仙！ (2026) - 2160p.WEB-DL.DoVi.H.265.10-bit.60fps.HQ.DTS 5.1 {tmdb-1633056}.mkv
        """
        parsed = self.parser.parse(msg)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["title"], "八仙！")
        self.assertEqual(parsed["year"], "2026")
        self.assertEqual(parsed["tmdb_id"], "1633056")
        self.assertEqual(parsed["media_type"], "movie")
        self.assertEqual(parsed["resolution"], "2160p")
        self.assertEqual(parsed["codec"], "H.265")
        self.assertEqual(parsed["dovi_hdr"], "DoVi")
        self.assertIn("123FSLinkV2", parsed["fslink"])
        self.assertEqual(parsed["fs_etag"], "82467be2147f26532298438bf7015238")
        self.assertEqual(parsed["fs_size"], 32855978205)
        self.assertEqual(parsed["fs_filename"], "八仙！ (2026) - 2160p.WEB-DL.DoVi.H.265.10-bit.60fps.HQ.DTS 5.1 {tmdb-1633056}.mkv")
        self.assertTrue(parsed["fs_filename"].endswith(".mkv"))
        self.assertEqual(parsed["media_key"], "movie_tmdb_1633056")

    def test_parse_fslink_with_trailing_punctuation(self):
        msg = "🔗分享链接 : 123FSLinkV2$82467be2147f26532298438bf7015238#32855978205#八仙！ (2026) - 2160p.WEB-DL.DoVi.H.265.10-bit.60fps.HQ.DTS 5.1 {tmdb-1633056}.mkv。"
        parsed = self.parser.parse(msg)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["fs_filename"], "八仙！ (2026) - 2160p.WEB-DL.DoVi.H.265.10-bit.60fps.HQ.DTS 5.1 {tmdb-1633056}.mkv")
        self.assertTrue(parsed["fs_filename"].endswith(".mkv"))

    def test_parse_movie_without_link_returns_none(self):
        msg = "这是一个普通文本通知，没有 123 分享或秒传链接。"
        parsed = self.parser.parse(msg)
        self.assertIsNone(parsed)


if __name__ == "__main__":
    unittest.main()

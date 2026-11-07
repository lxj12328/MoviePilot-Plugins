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
        self.assertEqual(parsed["media_key"], "movie_八仙_2026")

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

    def test_parse_alternate_123pan_share_domains_and_title_prefix(self):
        """验证 @regeng123 / @yun123pan 常用的 123865.com / 123912.com / 123952.com 等分流域名与「名称：」标题前缀"""
        for domain in ("www.123865.com", "www.123912.com", "www.123952.com", "www.123592.com", "123685.com"):
            msg = f"""
名称：《仙逆》 (2023) 更新至第82集
类型：剧集 / 动画
画质：4K 2160p H.265 DoVi
链接：https://{domain}/s/AbCdEf-1234?pwd=abcd
            """
            parsed = self.parser.parse(msg, msg_id="88", channel="@regeng123")
            self.assertIsNotNone(parsed, f"Failed to parse domain {domain}")
            self.assertEqual(parsed["title"], "仙逆")
            self.assertEqual(parsed["year"], "2023")
            self.assertEqual(parsed["media_type"], "tv")
            self.assertEqual(parsed["share_key"], "AbCdEf-1234")
            self.assertEqual(parsed["share_pwd"], "abcd")

    def test_parse_regeng123_guyu_and_zheyueliang(self):
        """验证 @regeng123 频道格式：📺 剧集：谷雨 (2026) S01E06 及无括号年份但含 📅 播出日期 的剧集：折月亮"""
        msg_guyu = """📺 剧集：谷雨 (2026) S01E06
📝 简介：
鹏州市一著名集团公司的财务人员，在电梯井“意外”坠亡了。她生前的最后联人——服装企业老板闫平安对此感到了极度恐慌。刑警出身的闫平安凭借直觉和经验，认定这场意外是该集团总裁何东平，对其二人展开灭口的信号……面对如困兽般的绝境，闫平安九死一生地逃到北京，寻找时任国税总局稽查处处长的旧识欧阳旭，希望...
⭐️ TMDB评分：[9.0/10](https://www.themoviedb.org/tv/279155)
📅 播出日期：2026-11-06
🗂 分类：国产剧集
🏷 类型：#谷雨 #剧情
🎞 版本：2160p Dolby Vision HQ WEB-DL H.265 AAC
💾 体积：5.29 GB
🔗 链接：[点击保存到123云盘](https://www.123865.com/s/Oqtgvd-d5i3h?pwd=YSRG)"""

        parsed = self.parser.parse(msg_guyu, msg_id="101", channel="@regeng123")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["title"], "谷雨")
        self.assertEqual(parsed["year"], "2026")
        self.assertEqual(parsed["tmdb_id"], "279155")
        self.assertEqual(parsed["media_type"], "tv")
        self.assertEqual(parsed["season"], 1)
        self.assertEqual(parsed["episode"], 6)
        self.assertEqual(parsed["ep_tag"], "S01E06")
        self.assertEqual(parsed["display_title"], "谷雨 S01E06")
        self.assertEqual(parsed["media_title"], "谷雨 S01E06")
        self.assertEqual(parsed["resolution"], "2160p")
        self.assertEqual(parsed["dovi_hdr"], "DoVi")
        self.assertEqual(parsed["codec"], "H.265")
        self.assertEqual(parsed["share_key"], "Oqtgvd-d5i3h")
        self.assertEqual(parsed["share_pwd"], "YSRG")

        # 测试首行无括号年份（📺 剧集：折月亮），通过 📅 播出日期：2026-11-06 提取年份并剥离「剧集：」前缀
        msg_zheyueliang = """📺 剧集：折月亮
📅 播出日期：2026-11-06
🗂 分类：国产剧集
🎞 版本：2160p WEB-DL H.265
🔗 链接：[点击保存到123云盘](https://www.123865.com/s/Oqtgvd-f2n3h?pwd=YSRG)"""
        parsed2 = self.parser.parse(msg_zheyueliang, msg_id="102", channel="@regeng123")
        self.assertIsNotNone(parsed2)
        self.assertEqual(parsed2["title"], "折月亮")
        self.assertEqual(parsed2["display_title"], "折月亮 S01")
        self.assertEqual(parsed2["year"], "2026")
        self.assertEqual(parsed2["media_type"], "tv")
        self.assertEqual(parsed2["share_key"], "Oqtgvd-f2n3h")

    def test_parse_yun123pan_share_domain_and_episode_range(self):
        """验证 @yun123pan 频道的 *.share.123pan.cn/123pan/<key> 链接格式与 📼 集数：E01-E04 集数范围"""
        msg_yun = """🎬 名称：玫瑰战争 (2026)
📁 类型：国产剧集
🖥️ 画质：2160p
🎥 视频：WEB-DL.HDR.H.265
🔊 音频：DDP 5.1
📦 大小：18.3 GB
📼 集数：E01-E04
🔄 状态：更新中

🔗 链接：
https://1840976528.share.123pan.cn/123pan/cHCOTd-RLSP"""
        parsed = self.parser.parse(msg_yun, msg_id="201", channel="@yun123pan")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["title"], "玫瑰战争")
        self.assertEqual(parsed["display_title"], "玫瑰战争 S01E01-E04")
        self.assertEqual(parsed["media_title"], "玫瑰战争 S01E01-E04")
        self.assertEqual(parsed["year"], "2026")
        self.assertEqual(parsed["media_type"], "tv")
        self.assertEqual(parsed["season"], 1)
        self.assertEqual(parsed["episode"], 4)
        self.assertEqual(parsed["ep_tag"], "S01E01-E04")
        self.assertEqual(parsed["media_key"], "tv_玫瑰战争_S01E01-E04")
        self.assertEqual(parsed["resolution"], "2160p")
        self.assertEqual(parsed["codec"], "H.265")
        self.assertEqual(parsed["dovi_hdr"], "HDR")
        self.assertEqual(parsed["share_key"], "cHCOTd-RLSP")
        self.assertEqual(parsed["share_url"], "https://1840976528.share.123pan.cn/123pan/cHCOTd-RLSP")


if __name__ == "__main__":
    unittest.main()

"""pan1.me (Xiuno BBS 4.0.4) 论坛适配器与每日签到单元测试"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

plugins_v3_dir = Path(__file__).resolve().parents[3] / "plugins.v3"
if str(plugins_v3_dir) not in sys.path:
    sys.path.insert(0, str(plugins_v3_dir))

from pan123tgpilot import Pan123TgPilot
from pan123tgpilot.forum_watcher import XiunoForumWatcher
from pan123tgpilot.parser import TgMessageParser


class TestXiunoForumWatcher(unittest.TestCase):
    def test_parse_forum_ids_supports_numbers_and_urls(self):
        raw = "https://pan1.me/?forum-2.htm, https://pan1.me/?forum-48.htm\n37，52 2"
        self.assertEqual(XiunoForumWatcher.parse_forum_ids(raw), ["2", "48", "37", "52"])

    def test_login_with_cookie_and_md5_password_and_captcha_block(self):
        # 1. Cookie 有效
        w_cookie = XiunoForumWatcher(cookie="bbs_token=abc")
        w_cookie._request = MagicMock(return_value=(200, '<a href="?user-logout.htm">退出登录</a>'))
        self.assertTrue(w_cookie.ensure_logged_in())

        # 2. 账号密码 MD5 登录 (?user-login.htm)
        w_pwd = XiunoForumWatcher(username="user@example.com", password="secret")

        def mock_route(route, method="GET", data=None, extra_headers=None):
            if "user-login" in route and method == "GET":
                return 200, '<form><input name="csrf_token" value="tok_123"></form>', "https://pan1.me/?user-login.htm"
            if "user-login" in route and method == "POST":
                # 验证提交的是 MD5 且带 csrf_token
                self.assertEqual(data["email"], "user@example.com")
                self.assertEqual(len(data["password"]), 32)
                self.assertEqual(data["csrf_token"], "tok_123")
                return 200, json.dumps({"code": 0, "message": "登录成功"}), "https://pan1.me/?user-login.htm"
            return 404, "", ""

        w_pwd._request_route = mock_route
        self.assertTrue(w_pwd.ensure_logged_in())

        # 3. 登录页包含验证码时不绕过，直接返回 False
        w_cap = XiunoForumWatcher(username="user@example.com", password="secret")
        w_cap._request_route = MagicMock(return_value=(200, '<div class="cf-turnstile"></div>', "https://pan1.me/?user-login.htm"))
        self.assertFalse(w_cap.ensure_logged_in())

    def test_fetch_threads_public_and_reply_to_unlock_and_limit(self):
        watcher = XiunoForumWatcher(
            username="u", password="p", auto_reply=True, max_reply_per_run=1, reply_cooldown=0.0
        )
        watcher._logged_in = True

        forum_html = """
        <ul class="threadlist">
          <li class="media thread top_3" data-tid="1"><a href="?thread-1.htm">全站公告置顶贴</a></li>
          <li class="media thread" data-tid="100"><a href="?thread-100.htm">[4K] 流浪地球3 (2026) 2160p H.265</a></li>
          <li class="media thread" data-tid="101"><a href="?thread-101.htm">玫瑰战争 (2026) S01E01-E10</a></li>
          <li class="media thread" data-tid="102"><a href="?thread-102.htm">隐藏剧集2 (2026) S01E02</a></li>
        </ul>
        """
        t100_html = '<div class="message break-all" isfirst="1">直接分享：<a href="https://www.123pan.com/s/PubKey-100?pwd=abcd">123云盘</a></div>'
        t101_locked = '<div class="message break-all" isfirst="1">本帖隐藏的内容，回复后即可查看下载链接</div>'
        t101_unlocked = '<div class="message break-all" isfirst="1">链接：https://www.123865.com/s/UnLock-101?pwd=6666</div>'
        t102_locked = '<div class="message break-all" isfirst="1">回复可见</div>'

        replied_calls = []
        t101_unlocked_flag = {"done": False}

        def mock_request(url, method="GET", data=None, extra_headers=None, timeout=18):
            if "thread-100" in url:
                return 200, t100_html
            if "thread-101" in url:
                return 200, (t101_unlocked if t101_unlocked_flag["done"] else t101_locked)
            if "thread-102" in url:
                return 200, t102_locked
            return 404, ""

        def mock_route(route, method="GET", data=None, extra_headers=None):
            if "forum-2.htm" in route:
                return 200, forum_html, "https://pan1.me/?forum-2.htm"
            if "post-create-101-1.htm" in route and method == "POST":
                replied_calls.append(route)
                t101_unlocked_flag["done"] = True
                return 200, json.dumps({"code": 0, "message": "回帖成功"}), "https://pan1.me/?post-create-101-1.htm"
            return 404, "", ""

        watcher._request = mock_request
        watcher._request_route = mock_route

        msgs = watcher.fetch_forum_messages("2")
        # thread-100 公开直接提取，未触发回帖；thread-101 回帖后解锁提取；thread-102 超出 max_reply_per_run=1 被保护跳过
        self.assertEqual(len(replied_calls), 1)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0][1], "pan1me_100")
        self.assertEqual(msgs[1][1], "pan1me_101")

        # 解析结果校验：电影板块保留 movie，剧集标题带 S01E01-E10 正确识别为 tv
        p1 = TgMessageParser.parse_message(msgs[0][2])
        p2 = TgMessageParser.parse_message(msgs[1][2])
        self.assertEqual(p1["media_type"], "movie")
        self.assertEqual(p1["title"], "流浪地球3")
        self.assertEqual(p1["share_key"], "PubKey-100")
        self.assertEqual(p2["media_type"], "tv")
        self.assertEqual(p2["ep_tag"], "S01E01-E10")
        self.assertEqual(p2["share_key"], "UnLock-101")

    def test_daily_checkin_sniffs_route_and_handles_already_signed(self):
        watcher = XiunoForumWatcher(username="u", password="p")
        watcher._logged_in = True

        # 1. 动态嗅探到 sg_sign-create.htm 并签到成功
        watcher._request = MagicMock(
            return_value=(200, '<button onclick="$.xpost(\'?sg_sign-create.htm\')">每日签到</button>')
        )
        watcher._request_route = MagicMock(
            return_value=(200, json.dumps({"code": 0, "message": "签到成功，获得 5 金币"}), "https://pan1.me/?sg_sign-create.htm")
        )
        ok, msg = watcher.daily_checkin()
        self.assertTrue(ok)
        self.assertIn("5 金币", msg)
        watcher._request_route.assert_called_once()
        self.assertEqual(watcher._request_route.call_args.args[0], "sg_sign-create.htm")

        # 2. 首页已展示今日已签，直接返回 True 不再重复发 POST
        watcher._request = MagicMock(return_value=(200, '<span>今日已签，已连续签到 10 天</span>'))
        watcher._request_route = MagicMock()
        ok2, msg2 = watcher.daily_checkin()
        self.assertTrue(ok2)
        self.assertIn("已签", msg2)
        watcher._request_route.assert_not_called()

    def test_plugin_integration_forum_and_daily_checkin_idempotent(self):
        plugin = Pan123TgPilot()
        plugin.init_plugin({
            "enabled": True,
            "pan123_token": "tok",
            "movie_target_dir_id": "1001",
            "tv_target_dir_id": "1002",
            "tg_channels": "",
            "pan1me_enabled": True,
            "pan1me_forum_ids": "2,48",
            "pan1me_username": "u",
            "pan1me_password": "p",
            "pan1me_auto_checkin": True,
        })
        plugin._client = MagicMock()
        plugin._client.save_share_link.return_value = (True, "55555", "ok")
        plugin._db = MagicMock()
        plugin._db.has_msg_id_processed.return_value = False
        plugin._db.has_link_processed.return_value = False
        plugin._db.get_record_by_media_key.return_value = None

        plugin._forum_watcher = MagicMock()
        plugin._forum_watcher.daily_checkin.return_value = (True, "每日签到完成 (获得 5 金币)")
        plugin._forum_watcher.fetch_forum_messages.return_value = [
            ("@pan1.me(板块2)", "pan1me_201", "科幻电影 (2026)\n类型：电影\nhttps://www.123pan.com/s/Mov-201"),
            ("@pan1.me(板块48)", "pan1me_202", "热门大剧 S01E01-E05 (2026)\n类型：电视剧\nhttps://www.123pan.com/s/Tv-202"),
        ]

        res = plugin.execute_check_all_channels()
        self.assertTrue(res["success"])
        self.assertEqual(res["stats"]["transferred"], 2)
        plugin._forum_watcher.daily_checkin.assert_called_once()
        # 验证电影进 1001 目录，电视剧进 1002 目录
        calls = plugin._client.save_share_link.call_args_list
        dirs = {c.kwargs["share_key"]: c.kwargs["target_parent_id"] for c in calls}
        self.assertEqual(dirs["Mov-201"], 1001)
        self.assertEqual(dirs["Tv-202"], 1002)

        # 同一天第二次巡检不再重复调用 daily_checkin
        plugin._forum_watcher.fetch_forum_messages.return_value = []
        plugin.execute_check_all_channels()
        plugin._forum_watcher.daily_checkin.assert_called_once()


if __name__ == "__main__":
    unittest.main()

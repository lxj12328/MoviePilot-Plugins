"""
Pan123TgPilot 论坛站点适配器 (专用于 pan1.me / Xiuno BBS 4.0.4)
支持 Cookie 会话与账号密码登录、板块新帖抓取、回复可见按需安全解锁、123 链接提取
"""

import hashlib
from html import unescape
import http.cookiejar
import json
import random
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

try:
    from app.sdk.logging import logger
except ImportError:
    import logging
    logger = logging.getLogger("pan123tgpilot")  # type: ignore


# 默认支持的 pan1.me 核心影视板块类型提示
DEFAULT_FORUM_TYPE_HINTS: Dict[str, str] = {
    "2": "电影",
    "48": "电视剧",
    "37": "动漫",
    "52": "综艺 剧集",
}

# 礼貌回复文案池 (降低固定文本触发论坛反灌水风控的风险)
DEFAULT_REPLY_MESSAGES: Tuple[str, ...] = (
    "感谢大佬热情整理分享，收藏备用！",
    "感谢楼主分享 123 云盘资源，辛苦了！",
    "谢谢分享高清资源，好人一生平安！",
    "感谢无私分享，非常需要的资源！",
    "支持一下楼主，感谢分享优质影视资源！",
)


class XiunoForumWatcher:
    """
    适配 Xiuno BBS 4.0.4 架构论坛（如 pan1.me）的客户端与抓取器。
    """

    DEFAULT_BASE_URL = "https://pan1.me"
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    # 检查是否存在人机验证 / 图形验证码 (遇阻时不绕过，严格遵循安全边界并提醒用户)
    RE_CAPTCHA_HINT = re.compile(
        r"(?:cf-turnstile|g-recaptcha|h-captcha|geetest|vcode\.(?:htm|php)|验证码|人机验证|verify\s*you\s*are\s*human)",
        re.IGNORECASE,
    )
    RE_LOGIN_OK_HTML = re.compile(r"(?:user-logout|退出登录|个人中心|my\.htm)", re.IGNORECASE)
    RE_REPLY_HIDE_HINT = re.compile(
        r"(?:请?回复后(?:即可|才能|再)?查看|回复可见|本帖(?:含有|包含)?(?:特定|隐藏)内容|隐藏内容.*回复|本帖隐藏的内容|tt-hide|reply_hide|hide_content|登录后回复)",
        re.IGNORECASE,
    )
    RE_HAS_123_LINK = re.compile(
        r"(?:123FSLink(?:V2)?\$|https?://(?:[a-zA-Z0-9-]+\.)*(?:123pan\.(?:com|cn)|123(?:684|685|865|912|952|592|\d{3})\.(?:com|cn))/(?:s|123pan)/)",
        re.IGNORECASE,
    )
    RE_SHARE_WITH_PWD = re.compile(r"[?&]pwd=[a-zA-Z0-9]{4,6}\b", re.IGNORECASE)
    RE_TEXT_PWD = re.compile(r"(?:提取码|密码|访问码|pwd)[：:\s=]*(?:是|为)?\s*([a-zA-Z0-9]{4,6})", re.IGNORECASE)
    RE_ALREADY_CHECKED_IN = re.compile(
        r"(?:今日已签|已连续签到|已经签到|今天已经签过|已签到\s*\d+\s*天)",
        re.IGNORECASE,
    )
    RE_CHECKIN_OK_MSG = re.compile(
        r"(?:签到成功|恭喜|获得|积分|金币|经验|已签|重复签到|已经签过)",
        re.IGNORECASE,
    )

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        username: str = "",
        password: str = "",
        cookie: str = "",
        proxy: str = "",
        auto_reply: bool = True,
        max_reply_per_run: int = 3,
        reply_cooldown: float = 5.0,
        keywords: str = "",
    ):
        self.base_url = self._normalize_base_url(base_url)
        self.username = (username or "").strip()
        self.password = password or ""
        self.cookie_str = (cookie or "").strip()
        self.proxy = self._normalize_proxy(proxy)
        self.auto_reply = bool(auto_reply)
        try:
            mr = int(max_reply_per_run if max_reply_per_run is not None else 3)
            self.max_reply_per_run = max(0, min(20, mr))
        except (ValueError, TypeError):
            self.max_reply_per_run = 3
        self.reply_cooldown = max(0.0, float(reply_cooldown if reply_cooldown is not None else 5.0))
        self.keywords = self._parse_keywords(keywords)

        self._cookie_jar = http.cookiejar.CookieJar()
        self._session_lock = threading.Lock()
        self._logged_in: bool = False
        self._last_reply_ts: float = 0.0
        # 缓存已尝试解锁过或判定无法自动解锁的 tid，避免同一运行期内反复重试回帖
        self._replied_tids: set[str] = set()
        # 标记是否支持 ?route.htm 风格（pan1.me 默认使用 ?forum-2.htm 查询参数风格）
        self._use_query_route: bool = True

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        u = (url or "").strip().rstrip("/")
        if not u:
            return XiunoForumWatcher.DEFAULT_BASE_URL
        if not u.startswith(("http://", "https://")):
            u = f"https://{u}"
        return u

    @staticmethod
    def _normalize_proxy(proxy: str) -> str:
        p = (proxy or "").strip()
        if not p:
            return ""
        if "://" not in p:
            p = f"http://{p}"
        return p

    @staticmethod
    def _parse_keywords(raw: str) -> List[str]:
        if not raw:
            return []
        out = []
        for item in str(raw).replace("，", ",").replace("\n", ",").split(","):
            kw = item.strip().lower()
            if kw:
                out.append(kw)
        return out

    @staticmethod
    def parse_forum_ids(raw: str) -> List[str]:
        """
        支持用户填写纯板块 ID (如 "2, 48, 37, 52") 或完整 URL (如 "https://pan1.me/?forum-2.htm")。
        自动提取去重并保持顺序。
        """
        if not raw:
            return []
        seen = set()
        ids: List[str] = []
        for token in re.split(r"[,，\s\r\n]+", str(raw).strip()):
            t = token.strip()
            if not t:
                continue
            m = re.search(r"forum-(\d+)", t, re.IGNORECASE)
            fid = m.group(1) if m else (t if t.isdigit() else "")
            if fid and fid not in seen:
                seen.add(fid)
                ids.append(fid)
        return ids

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: List[urllib.request.BaseHandler] = [
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        ]
        if self.proxy:
            handlers.append(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            )
        return urllib.request.build_opener(*handlers)

    def _request(
        self,
        url: str,
        method: str = "GET",
        data: Optional[dict] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: int = 18,
    ) -> Tuple[int, str]:
        """发送 HTTP 请求并返回 (status_code, text)"""
        headers = dict(self.DEFAULT_HEADERS)
        if self.cookie_str:
            headers["Cookie"] = self.cookie_str
        if extra_headers:
            headers.update(extra_headers)

        encoded_data: Optional[bytes] = None
        if data is not None:
            encoded_data = urllib.parse.urlencode(data).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")

        req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method.upper())
        opener = self._build_opener()
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode() or 200
                body = resp.read().decode("utf-8", errors="replace")
                return int(status), body
        except urllib.error.HTTPError as he:
            body = ""
            try:
                body = he.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            return int(he.code), body

    def _route_url(self, route: str) -> str:
        clean = route.lstrip("/?")
        prefix = "?" if self._use_query_route else ""
        return f"{self.base_url}/{prefix}{clean}"

    def _request_route(
        self,
        route: str,
        method: str = "GET",
        data: Optional[dict] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str, str]:
        """自动兼容 ?route.htm 与 /route.htm 两种 Xiuno URL 风格"""
        url = self._route_url(route)
        status, body = self._request(url, method=method, data=data, extra_headers=extra_headers)
        if status == 404:
            alt_prefix = "" if self._use_query_route else "?"
            alt_url = f"{self.base_url}/{alt_prefix}{route.lstrip('/?')}"
            alt_status, alt_body = self._request(alt_url, method=method, data=data, extra_headers=extra_headers)
            if alt_status < 400:
                self._use_query_route = not self._use_query_route
                return alt_status, alt_body, alt_url
        return status, body, url

    def ensure_logged_in(self) -> bool:
        """
        确认当前会话已登录：
        1. 若本地已有 Cookie 且验证首页已处于登录态，直接返回 True；
        2. 否则使用账号密码请求 ?user-login.htm 登录 (优先前端标准 MD5 加密密码，若失败再尝试原文兜底)；
        3. 若检测到人机校验 / 图形验证码，立即停止自动登录并提示用户填入手工 Cookie，绝不尝试绕过验证。
        """
        with self._session_lock:
            if self._logged_in:
                return True

            # 1. 若有初始 Cookie，先检查首页登录态
            if self.cookie_str or len(self._cookie_jar) > 0:
                try:
                    status, html = self._request(self.base_url, method="GET")
                    if status == 200 and self.RE_LOGIN_OK_HTML.search(html or ""):
                        self._logged_in = True
                        logger.info(f"Pan123TgPilot 论坛 ({self.base_url}) Cookie 会话验证有效")
                        return True
                except Exception as e:
                    logger.debug(f"Pan123TgPilot 论坛 Cookie 校验异常: {e}")

            if not self.username or not self.password:
                return False

            # 2. 先 GET 登录页面检测是否存在验证码/人机拦截
            try:
                st_get, login_html, login_url = self._request_route("user-login.htm", method="GET")
                if st_get == 200 and self.RE_LOGIN_OK_HTML.search(login_html or ""):
                    self._logged_in = True
                    return True
                if self.RE_CAPTCHA_HINT.search(login_html or ""):
                    logger.warning(
                        f"Pan123TgPilot 论坛 ({self.base_url}) 登录页面存在图形验证码或人机校验，"
                        f"为遵循安全策略不会自动绕过，请您在浏览器登录后复制 Cookie 填入插件配置中。"
                    )
                    return False

                # 提取可能存在的隐藏 csrf_token 字段
                csrf_val = ""
                m_csrf = re.search(
                    r'<input[^>]+name=["\'](?:csrf_token|_token)["\'][^>]+value=["\']([^"\']*)["\']',
                    login_html or "",
                    re.IGNORECASE,
                )
                if m_csrf:
                    csrf_val = m_csrf.group(1)

                # Xiuno BBS 4.0.4 标准前端提交 MD5(password)
                pwd_md5 = hashlib.md5(self.password.encode("utf-8")).hexdigest()
                candidates = [pwd_md5, self.password]
                ajax_headers = {
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": login_url,
                    "Origin": self.base_url,
                }

                for idx, pwd_payload in enumerate(candidates):
                    form = {"email": self.username, "password": pwd_payload}
                    if csrf_val:
                        form["csrf_token"] = csrf_val

                    st_post, resp_text, _ = self._request_route(
                        "user-login.htm", method="POST", data=form, extra_headers=ajax_headers
                    )
                    if self.RE_CAPTCHA_HINT.search(resp_text or ""):
                        logger.warning(
                            f"Pan123TgPilot 论坛 ({self.base_url}) 登录触发验证码校验，请改为在配置中填入浏览器登录 Cookie。"
                        )
                        return False

                    ok_login, msg_login = self._parse_xiuno_json_result(st_post, resp_text)
                    if ok_login:
                        self._logged_in = True
                        logger.info(f"Pan123TgPilot 成功登录论坛 ({self.base_url}) 账号: {self.username}")
                        return True
                    # 仅当提示密码错误时尝试原文兜底，其余情况直接停止重试
                    if idx == 0 and ("密码" in msg_login or "password" in msg_login.lower()):
                        continue
                    logger.warning(f"Pan123TgPilot 登录论坛 ({self.base_url}) 未成功: {msg_login}")
                    return False
            except Exception as e:
                logger.warning(f"Pan123TgPilot 登录论坛 ({self.base_url}) 异常: {e}")

            return False

    @staticmethod
    def _parse_xiuno_json_result(status_code: int, body: str) -> Tuple[bool, str]:
        """解析 Xiuno BBS AJAX JSON 或跳转 HTML 响应"""
        text = (body or "").strip()
        if not text:
            return (200 <= status_code < 400), f"HTTP {status_code}"
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                code = obj.get("code")
                msg = str(obj.get("message") or obj.get("msg") or "")
                if str(code) == "0":
                    return True, msg or "ok"
                return False, msg or f"code={code}"
        except Exception:
            pass
        if status_code < 400 and XiunoForumWatcher.RE_LOGIN_OK_HTML.search(text):
            return True, "ok"
        return False, text[:120]

    @classmethod
    def _parse_forum_thread_list(cls, html: str, forum_id: str) -> List[Dict[str, str]]:
        """从板块 HTML 页面提取非置顶/最新主题列表 [(tid, href, subject)]"""
        if not html:
            return []
        results: List[Dict[str, str]] = []
        seen_tids: set[str] = set()

        # Xiuno BBS 4.0.4 主题列表典型模式: <li class="media thread ..." data-tid="123"> 或 <a href="?thread-123.htm">标题</a>
        # 优先按 <li ... data-tid="..."> 块提取，跳过置顶帖 (top_1/top_3) 以聚焦最新发帖
        li_blocks = re.findall(
            r'<li\b([^>]*class=["\'][^"\']*thread[^"\']*["\'][^>]*)>(.*?)</li>',
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        for attrs, inner in li_blocks:
            # 跳过全局置顶公告帖 (top_3 / istop)，保留板块正常新帖与普通帖
            if re.search(r'\btop_3\b', attrs, re.IGNORECASE):
                continue
            m_link = re.search(
                r'<a\b[^>]*href=["\']([^"\']*thread-(\d+)[^"\']*\.htm[^"\']*)["\'][^>]*>(.*?)</a>',
                inner,
                flags=re.DOTALL | re.IGNORECASE,
            )
            if not m_link:
                continue
            href = unescape(m_link.group(1).strip())
            tid = m_link.group(2).strip()
            subject = unescape(re.sub(r"<[^>]+>", "", m_link.group(3))).strip()
            subject = re.sub(r"\s+", " ", subject).strip()
            if tid and subject and tid not in seen_tids:
                seen_tids.add(tid)
                results.append({"tid": tid, "href": href, "subject": subject, "forum_id": forum_id})

        # 兜底匹配：部分 NoteWeb/自定义主题可能使用 <div> 或 <tr> 包裹主题行
        if not results:
            for m_link in re.finditer(
                r'<a\b[^>]*href=["\']([^"\']*thread-(\d+)(?:-\d+)?\.htm[^"\']*)["\'][^>]*>(.*?)</a>',
                html,
                flags=re.DOTALL | re.IGNORECASE,
            ):
                href = unescape(m_link.group(1).strip())
                tid = m_link.group(2).strip()
                subject = unescape(re.sub(r"<[^>]+>", "", m_link.group(3))).strip()
                subject = re.sub(r"\s+", " ", subject).strip()
                # 忽略纯数字页码链接或 "最后回复" 短锚点
                if not subject or len(subject) < 2 or subject.isdigit():
                    continue
                if tid not in seen_tids:
                    seen_tids.add(tid)
                    results.append({"tid": tid, "href": href, "subject": subject, "forum_id": forum_id})

        return results

    @classmethod
    def _extract_thread_body(cls, html: str) -> Tuple[str, bool, str]:
        """
        提取主题帖首楼正文并判断是否包含回复可见提示。
        返回: (纯文本正文, 是否有回复可见遮罩, 提取到的页面标题)
        """
        if not html:
            return "", False, ""

        page_title = ""
        m_h4 = re.search(r'<h4[^>]*class=["\'][^"\']*break-all[^"\']*["\'][^>]*>(.*?)</h4>', html, re.DOTALL | re.IGNORECASE)
        if m_h4:
            page_title = unescape(re.sub(r"<[^>]+>", "", m_h4.group(1))).strip()

        # 提取首楼 message 块 (Xiuno BBS 首楼通常标记 isfirst="1" 或为正文第一个 div.message)
        m_msg = re.search(
            r'<div\b[^>]*class=["\'][^"\']*\bmessage\b[^"\']*["\'][^>]*isfirst=["\']1["\'][^>]*>(.*?)</div>\s*(?:<div[^>]+class=["\'][^"\']*(?:plugin|filelist|mt-)|</td|</li>|</div>)',
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not m_msg:
            m_msg = re.search(
                r'<div\b[^>]*class=["\'][^"\']*\bmessage\b[^"\']*["\'][^>]*>(.*?)</div>',
                html,
                flags=re.DOTALL | re.IGNORECASE,
            )

        body_html = m_msg.group(1) if m_msg else html
        # 优先检测首楼正文及其紧随插件块（限制在主帖区，避免误扫评论区；当 html 本身不含 postlist 时直接扫描整段）
        main_post_area = html.split('class="card card-postlist"', 1)[0] if 'card-postlist' in html else html
        has_hide_hint = bool(cls.RE_REPLY_HIDE_HINT.search(body_html) or cls.RE_REPLY_HIDE_HINT.search(main_post_area))

        def _replace_anchor(match: re.Match) -> str:
            href = unescape(match.group(1).strip())
            anchor_text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            if href.startswith(("http://", "https://")):
                return f" [{anchor_text}]({href}) "
            return f" {anchor_text} "

        text = re.sub(
            r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            _replace_anchor,
            body_html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p>|</div>|</li>|</tr>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = unescape(text)
        # 压缩多余空行
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        clean_text = "\n".join(lines)
        return clean_text, has_hide_hint, page_title

    def _reply_to_unlock(self, tid: str, thread_url: str, stop_event: Optional[threading.Event] = None) -> bool:
        """
        对目标主题提交一次礼貌回帖以解锁回复可见隐藏内容，严格遵守冷却间隔。
        """
        if not self._logged_in and not self.ensure_logged_in():
            return False

        # 冷却控制，防触发论坛频率限制
        now = time.monotonic()
        elapsed = now - self._last_reply_ts
        if self._last_reply_ts > 0 and elapsed < self.reply_cooldown:
            wait_sec = self.reply_cooldown - elapsed + random.uniform(0.3, 1.5)
            if stop_event is not None:
                if stop_event.wait(wait_sec):
                    return False
            else:
                time.sleep(wait_sec)

        reply_text = random.choice(DEFAULT_REPLY_MESSAGES)
        ajax_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": thread_url,
            "Origin": self.base_url,
        }
        payload = {
            "doctype": "1",
            "return_html": "0",
            "quotepid": "0",
            "message": reply_text,
        }
        st_post, resp_text, _ = self._request_route(
            f"post-create-{tid}-1.htm", method="POST", data=payload, extra_headers=ajax_headers
        )
        self._last_reply_ts = time.monotonic()

        if self.RE_CAPTCHA_HINT.search(resp_text or ""):
            logger.warning(f"Pan123TgPilot 论坛回复解锁帖 [{tid}] 触发验证码要求，已自动中止本轮剩余自动回复")
            return False

        ok_reply, msg_reply = self._parse_xiuno_json_result(st_post, resp_text)
        if ok_reply:
            logger.info(f"Pan123TgPilot 已自动回复论坛帖 [tid={tid}] 解锁隐藏资源")
            return True

        logger.warning(f"Pan123TgPilot 自动回复论坛帖 [tid={tid}] 未成功: {msg_reply}")
        return False

    def _build_thread_url(self, tid: str, href: str) -> str:
        if href.startswith(("http://", "https://")):
            return href
        if href:
            return f"{self.base_url}/{href.lstrip('/')}"
        return self._route_url(f"thread-{tid}.htm")

    def fetch_forum_messages(
        self,
        forum_ids_raw: str,
        limit_per_forum: int = 20,
        skip_tid_fn: Optional[Callable[[str], bool]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> List[Tuple[str, str, str]]:
        """
        抓取指定板块列表下的最新影视帖子，按需自动回复解锁隐藏 123 链接。
        返回: [(forum_label, msg_id("pan1me_{tid}"), synthesized_text), ...]
        """
        fids = self.parse_forum_ids(forum_ids_raw)
        if not fids:
            return []

        # 先尝试确保登录态 (无账号/Cookie 时亦可先抓公开直出帖)
        if self.username or self.cookie_str:
            self.ensure_logged_in()

        collected: List[Tuple[str, str, str]] = []
        replies_used = 0
        reply_blocked = False

        for fid in fids:
            if stop_event and stop_event.is_set():
                break

            try:
                st_list, list_html, _ = self._request_route(f"forum-{fid}.htm", method="GET")
                if st_list >= 400 or not list_html:
                    logger.warning(f"Pan123TgPilot 获取 pan1.me 板块 [forum-{fid}] 失败: HTTP {st_list}")
                    continue
            except Exception as e:
                logger.warning(f"Pan123TgPilot 访问 pan1.me 板块 [forum-{fid}] 异常: {e}")
                continue

            threads = self._parse_forum_thread_list(list_html, forum_id=fid)
            if limit_per_forum > 0:
                threads = threads[:limit_per_forum]

            forum_hint = DEFAULT_FORUM_TYPE_HINTS.get(fid, "")
            forum_label = f"@pan1.me(板块{fid})"
            logger.info(f"Pan123TgPilot 成功获取 {forum_label} 的 {len(threads)} 条最新主题帖")

            for th in threads:
                if stop_event and stop_event.is_set():
                    break

                tid = th["tid"]
                subject = th["subject"]
                msg_id = f"pan1me_{tid}"

                # 关键词白名单过滤 (若用户配置了关键词)
                if self.keywords and not any(kw in subject.lower() for kw in self.keywords):
                    continue

                # 已在本地库处理过的主题直接跳过，绝不反复打开或回帖
                if skip_tid_fn and skip_tid_fn(msg_id):
                    continue

                thread_url = self._build_thread_url(tid, th.get("href", ""))
                try:
                    st_th, th_html = self._request(thread_url, method="GET")
                    if st_th >= 400 or not th_html:
                        continue
                except Exception:
                    continue

                body_text, has_hide, page_title = self._extract_thread_body(th_html)
                final_title = subject or page_title
                has_link = bool(self.RE_HAS_123_LINK.search(body_text))
                has_fslink = "123FSLink" in body_text
                has_pwd = bool(self.RE_SHARE_WITH_PWD.search(body_text) or self.RE_TEXT_PWD.search(body_text))

                # 两种场景均需自动回复解锁：
                # 1) 整个 123 链接在隐藏区 (not has_link)；
                # 2) 链接已公开但未带提取码 (not has_fslink and not has_pwd)，提取码被作者锁在「请回复后再查看」隐藏区！
                need_unlock = (not has_link) or (has_link and not has_fslink and not has_pwd)

                # 若帖子需要回复解锁、且处于安全配额内，执行一次礼貌回帖后再重抓
                unlocked_this_turn = False
                if need_unlock and has_hide and self.auto_reply:
                    if not reply_blocked and replies_used < self.max_reply_per_run and tid not in self._replied_tids:
                        self._replied_tids.add(tid)
                        ok_unlock = self._reply_to_unlock(tid=tid, thread_url=thread_url, stop_event=stop_event)
                        if ok_unlock:
                            replies_used += 1
                            unlocked_this_turn = True
                            try:
                                st_th2, th_html2 = self._request(thread_url, method="GET")
                                if st_th2 < 400 and th_html2:
                                    body_text2, _, _ = self._extract_thread_body(th_html2)
                                    if body_text2:
                                        body_text = body_text2
                                        has_link = bool(self.RE_HAS_123_LINK.search(body_text))
                            except Exception:
                                pass
                        else:
                            # 一旦触发验证码或回帖被拒，停掉本轮剩余自动回帖以防风控
                            reply_blocked = True
                    elif replies_used >= self.max_reply_per_run and not has_pwd and not has_fslink:
                        # 仅因达到单轮回复限额暂未解锁提取码时，留给下一轮再解锁，不拿残缺链接试错
                        continue

                if not has_link:
                    continue

                # 组装规范化消息文本，无缝喂入 TgMessageParser
                # 若帖子标题或正文未自带明确类型且不含季集特征，再注入所属板块默认分类提示
                header_lines = [final_title]
                combined_hint_check = f"{final_title}\n{body_text[:200]}"
                has_strong_ep = bool(re.search(r"\bS\d{1,3}\s*E\d{1,4}\b|第\s*\d+\s*集", combined_hint_check, re.IGNORECASE))
                if forum_hint and "类型" not in body_text[:120] and not has_strong_ep:
                    header_lines.append(f"类型：{forum_hint}")
                synthesized_text = "\n".join(header_lines) + "\n" + body_text
                collected.append((forum_label, msg_id, synthesized_text))

        return collected

    def daily_checkin(self) -> Tuple[bool, str]:
        """
        执行 pan1.me (Xiuno BBS 4.0.4) 每日签到：
        1. 确保会话已登录；
        2. 抓取首页，若页面直接显示今日已签到则返回成功；
        3. 动态嗅探页面中真实的签到路由 (如 ?sg_sign.htm / ?sg_sign-create.htm / ?my-sign.htm 等) 并携带 token 提交 AJAX POST；
        4. 返回 (success_or_already_done, message)。
        """
        if not self.ensure_logged_in():
            return False, "论坛尚未登录或遇到验证码校验，跳过每日签到"

        home_html = ""
        try:
            st_home, home_html = self._request(self.base_url, method="GET")
            if st_home == 200 and self.RE_ALREADY_CHECKED_IN.search(home_html or ""):
                return True, "今日已签到"
        except Exception as e:
            logger.debug(f"Pan123TgPilot 获取论坛首页检查签到状态异常: {e}")

        # 动态从首页 HTML / JS 中嗅探签到路由 (兼容 sg_sign / tt_sign / my-sign 等各类插件)
        candidate_routes: List[str] = []
        if home_html:
            for m_r in re.finditer(
                r'["\'\(]\s*\??([a-zA-Z0-9_-]*(?:sign|checkin|qiandao)[a-zA-Z0-9_-]*\.htm(?:\?[^"\'\s\)]*)?)',
                home_html,
                re.IGNORECASE,
            ):
                route = m_r.group(1).strip().lstrip("/?")
                if route and route not in candidate_routes:
                    candidate_routes.append(route)

        # 补充 Xiuno BBS 常见官方/第三方签到插件默认路由兜底
        for default_r in (
            "sg_sign-create.htm",
            "sg_sign.htm",
            "my-sign.htm",
            "user-sign.htm",
            "sign-create.htm",
            "sign.htm",
        ):
            if default_r not in candidate_routes:
                candidate_routes.append(default_r)

        csrf_val = ""
        if home_html:
            m_csrf = re.search(
                r'<input[^>]+name=["\'](?:csrf_token|form_token|_token)["\'][^>]+value=["\']([^"\']*)["\']',
                home_html,
                re.IGNORECASE,
            )
            if m_csrf:
                csrf_val = m_csrf.group(1)

        ajax_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.base_url,
            "Origin": self.base_url,
        }

        last_err = "未找到可用的签到接口"
        for route in candidate_routes:
            try:
                post_data = {"csrf_token": csrf_val} if csrf_val else {}
                st, resp_text, req_url = self._request_route(
                    route, method="POST", data=post_data, extra_headers=ajax_headers
                )
                if st == 404:
                    continue

                if self.RE_CAPTCHA_HINT.search(resp_text or ""):
                    return False, "论坛签到触发图形验证码或人机验证，请手动处理"

                # 若某些插件先访问页面才给出表单或仅支持 GET 签到
                ok_json, msg_json = self._parse_xiuno_json_result(st, resp_text)
                if ok_json or self.RE_CHECKIN_OK_MSG.search(msg_json):
                    return True, f"每日签到完成 ({msg_json})"

                if st == 200 and self.RE_ALREADY_CHECKED_IN.search(resp_text or ""):
                    return True, "今日已签到"

                # 如果是 POST 到展示页面 (如 ?sg_sign.htm) 返回了 HTML，再从中提取二次动作或尝试 GET
                if st == 200 and "<html" in (resp_text or "").lower():
                    m_sub = re.search(
                        r'(?:action|href|xpost\()\s*[=:]?\s*["\']?\??([a-zA-Z0-9_-]*(?:sign|checkin)[a-zA-Z0-9_-]*create\.htm)',
                        resp_text,
                        re.IGNORECASE,
                    )
                    if m_sub and m_sub.group(1).lstrip("/?") != route:
                        sub_route = m_sub.group(1).lstrip("/?")
                        st2, resp2, _ = self._request_route(
                            sub_route, method="POST", data=post_data, extra_headers=ajax_headers
                        )
                        ok2, msg2 = self._parse_xiuno_json_result(st2, resp2)
                        if ok2 or self.RE_CHECKIN_OK_MSG.search(msg2):
                            return True, f"每日签到完成 ({msg2})"

                last_err = msg_json or f"HTTP {st} ({req_url})"
            except Exception as e:
                last_err = str(e)
                continue

        return False, f"签到未成功: {last_err}"

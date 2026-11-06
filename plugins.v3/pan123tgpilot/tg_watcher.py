"""
Pan123TgPilot Telegram 频道监控模块
支持通过 Telegram 官方公开预览流 (t.me/s/channel) 免密抓取，支持代理与 Bot 模式
"""

from html import unescape
import re
import ssl
import time
from typing import Any, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

try:
    from app.log import logger
except ImportError:
    import logging
    logger = logging.getLogger("pan123tgpilot")


class TgChannelWatcher:
    """Telegram 频道监听器"""

    def __init__(self, proxy: str = "", bot_token: str = ""):
        self.proxy = (proxy or "").strip()
        self.bot_token = (bot_token or "").strip()
        self._last_msg_ids: dict = {}  # channel -> max_msg_id

    def _build_opener(self) -> urllib.request.OpenerDirector:
        """构建支持代理的 HTTP 打开器"""
        handlers: list[Any] = []
        if self.proxy:
            proxy_clean = self.proxy.strip()
            handlers.append(urllib.request.ProxyHandler({"http": proxy_clean, "https": proxy_clean}))

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))

        return urllib.request.build_opener(*handlers)

    def fetch_channel_messages(self, channel: str, limit: int = 20) -> List[Tuple[str, str]]:
        """
        抓取指定频道最新消息列表
        :return: [(msg_id, message_text), ...] 按时间升序排列
        """
        chan_name = channel.strip().lstrip("@").strip()
        if not chan_name:
            return []

        # 优先通过公开 Web 预览接口 (t.me/s/xxx)
        messages = self._fetch_via_web_preview(chan_name)
        if messages:
            return messages[-limit:]

        # 备选通过 Bot Token 抓取
        if self.bot_token:
            messages_bot = self._fetch_via_bot(chan_name)
            if messages_bot:
                return messages_bot[-limit:]

        return []

    def _fetch_via_web_preview(self, channel_username: str) -> List[Tuple[str, str]]:
        """通过 https://t.me/s/{username} 抓取公开消息"""
        url = f"https://t.me/s/{channel_username}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        opener = self._build_opener()
        req = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with opener.open(req, timeout=20) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                return self._parse_tg_web_html(html, channel_username)
        except Exception as e:
            logger.warning(f"Pan123TgPilot 抓取公开频道【@{channel_username}】失败: {e}")
            return []

    @staticmethod
    def _parse_tg_web_html(html: str, channel_username: str) -> List[Tuple[str, str]]:
        """解析 t.me/s HTML 中的消息内容"""
        results: List[Tuple[str, str]] = []
        if not html:
            return results

        # 匹配单个消息块: data-post="channel/123"
        post_blocks = re.findall(
            r'(<div class="tgme_widget_message\b[^>]*data-post="([^"]+)"[^>]*>.*?)(?=<div class="tgme_widget_message\b|$)',
            html,
            re.DOTALL,
        )

        for block_html, post_id in post_blocks:
            msg_id = post_id.split("/")[-1] if "/" in post_id else post_id

            # 提取文本区: class="tgme_widget_message_text ..."
            m_text = re.search(r'<div class="[^"]*tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block_html, re.DOTALL)
            raw_content = m_text.group(1) if m_text else ""
            if not raw_content:
                continue

            # 将 <a href="...">文本</a> 转换为 [文本](href) 格式，避免超链接 URL 丢失
            processed = re.sub(
                r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                r'[\2](\1)',
                raw_content,
                flags=re.DOTALL | re.IGNORECASE,
            )

            # 替换 <br/> 与 <p> 为换行
            processed = re.sub(r'<br\s*/?>', '\n', processed, flags=re.IGNORECASE)
            processed = re.sub(r'</p>', '\n', processed, flags=re.IGNORECASE)

            # 去除其余 HTML 标签
            text = re.sub(r'<[^>]+>', '', processed)
            text = unescape(text).strip()

            if text:
                results.append((msg_id, text))

        return results

    def _fetch_via_bot(self, channel_username: str) -> List[Tuple[str, str]]:
        """通过 Telegram Bot getUpdates 接口抓取"""
        if not self.bot_token:
            return []
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates?timeout=5&limit=20"
        opener = self._build_opener()
        req = urllib.request.Request(url, method="GET")

        results: List[Tuple[str, str]] = []
        try:
            with opener.open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for update in data.get("result", []):
                    post = update.get("channel_post") or update.get("message")
                    if not post:
                        continue
                    chat = post.get("chat") or {}
                    uname = (chat.get("username") or "").lower()
                    if uname and uname != channel_username.lower():
                        continue
                    msg_id = str(post.get("message_id") or "")
                    text = post.get("text") or post.get("caption") or ""
                    if text:
                        results.append((msg_id, text))
        except Exception:
            pass
        return results

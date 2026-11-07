"""
Pan123TgPilot Telegram 频道监控模块
支持通过 Telegram 官方公开预览流 (t.me/s/channel) 免密抓取，支持代理与 Bot 模式
"""

from html import unescape
import json
import os
import re
import ssl
import time
from typing import Any, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

try:
    import requests
except ImportError:
    requests = None

try:
    from app.log import logger
except ImportError:
    import logging
    logger = logging.getLogger("pan123tgpilot")


class TgChannelWatcher:
    """Telegram 频道监听器"""

    def __init__(self, proxy: str = "", bot_token: str = ""):
        self.proxy = self._normalize_proxy(proxy)
        self.bot_token = (bot_token or "").strip()
        self._last_msg_ids: dict = {}  # channel -> max_msg_id

    @staticmethod
    def _normalize_proxy(proxy: Optional[str]) -> str:
        """规范化代理地址格式，自动补全协议前缀"""
        if not proxy:
            return ""
        p = str(proxy).strip()
        if not p:
            return ""
        if not re.match(r"^[a-zA-Z0-9]+://", p):
            p = f"http://{p}"
        return p

    def _build_opener(self) -> urllib.request.OpenerDirector:
        """构建支持代理的 HTTP 打开器"""
        handlers: list[Any] = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))

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

        # 1. 优先尝试 requests (支持 SOCKS5 / 更好的 SSL 与连接池)
        if requests is not None:
            try:
                proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
                resp = requests.get(url, headers=headers, proxies=proxies, timeout=(8, 15), verify=False)
                if resp.status_code == 200:
                    return self._parse_tg_web_html(resp.text, channel_username)
                else:
                    logger.warning(
                        f"Pan123TgPilot 访问公开频道【@{channel_username}】返回 HTTP {resp.status_code}"
                    )
            except Exception as e:
                self._log_network_failure(channel_username, e)
                return []

        # 2. 备选使用原生 urllib
        opener = self._build_opener()
        req = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with opener.open(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                return self._parse_tg_web_html(html, channel_username)
        except Exception as e:
            self._log_network_failure(channel_username, e)
            return []

    def _log_network_failure(self, channel_username: str, e: Exception) -> None:
        """对常见的网络异常提供排查指引"""
        err_msg = str(e)
        if "timed out" in err_msg.lower() or "handshake" in err_msg.lower() or "connection" in err_msg.lower():
            if not self.proxy:
                logger.warning(
                    f"Pan123TgPilot 抓取公开频道【@{channel_username}】超时: {err_msg}。\n"
                    f"💡 原因诊断：当前运行在直连模式 (未配置代理)。由于 Telegram (t.me) 在中国大陆地区被防火墙阻断 TLS 握手，直连必然会超时或被重置。\n"
                    f"👉 解决方案：请在插件配置中填写「Telegram 网络代理」（例如 http://192.168.1.100:7890 或 http://host.docker.internal:7890），或在 MoviePilot 系统设置中配置全局代理！"
                )
            else:
                logger.warning(
                    f"Pan123TgPilot 抓取公开频道【@{channel_username}】连接超时: {err_msg} (已配置代理: {self.proxy})。\n"
                    f"💡 原因诊断：通过代理连接 Telegram 失败，常见原因包括：\n"
                    f"  1. MoviePilot 运行在 Docker 容器内：若代理地址填写为 127.0.0.1，在容器内指的是容器自身而非宿主机。请将 127.0.0.1 替换为宿主机局域网 IP (如 http://192.168.x.x:7890) 或 http://host.docker.internal:7890；\n"
                    f"  2. 代理客户端限制：请确认宿主机上的代理软件 (Clash/v2ray/Surge等) 已开启「允许局域网连接 (Allow LAN)」；\n"
                    f"  3. 代理节点故障：请检查该代理节点是否可正常访问 Telegram。"
                )
        else:
            logger.warning(f"Pan123TgPilot 抓取公开频道【@{channel_username}】失败: {e}")

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

        for block_html, post_attr in post_blocks:
            try:
                # 提取 msg_id
                msg_id = ""
                if "/" in post_attr:
                    msg_id = post_attr.split("/")[-1].strip()
                elif post_attr.isdigit():
                    msg_id = post_attr.strip()

                # 剥离引用回复块 (tgme_widget_message_reply) 与转发源块，杜绝上一条消息的剧集/季集信息与链接污染当前消息
                clean_block = re.sub(
                    r'<a\s+[^>]*class=["\'][^"\']*tgme_widget_message_reply[^"\']*["\'][^>]*>.*?</a>',
                    "",
                    block_html,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                clean_block = re.sub(
                    r'<div\s+[^>]*class=["\'][^"\']*tgme_widget_message_reply[^"\']*["\'][^>]*>.*?</div>',
                    "",
                    clean_block,
                    flags=re.DOTALL | re.IGNORECASE,
                )

                # 提取正文容器内容：优先匹配到 tgme_widget_message_footer 之前，避免正文内嵌套 <div> 或 <blockquote> 导致提前截断
                m_body = re.search(
                    r'<div class="[^"]*tgme_widget_message_text[^"]*"[^>]*>(.*?)(?=<div class="[^"]*(?:tgme_widget_message_footer|tgme_widget_message_link_preview|tgme_widget_message_inline_keyboard)|\Z)',
                    clean_block,
                    re.DOTALL,
                )
                body_html = m_body.group(1) if m_body else ""

                # 额外提取内联按钮 (tgme_widget_message_inline_button) 与链接卡片中的 123 云盘链接
                extra_links: List[str] = []
                for m_href in re.finditer(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', clean_block, flags=re.DOTALL | re.IGNORECASE):
                    href_val = unescape(m_href.group(1).strip())
                    if "/s/" in href_val or "123" in href_val:
                        extra_links.append(href_val)

                if not body_html and not extra_links:
                    continue

                # 将 <a> 链接保留为 markdown [文本](url) 形式，避免链接文本丢失
                def _replace_anchor(match: re.Match) -> str:
                    href = unescape(match.group(1).strip())
                    anchor_text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
                    return f" [{anchor_text}]({href}) "

                text = re.sub(
                    r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                    _replace_anchor,
                    body_html,
                    flags=re.DOTALL | re.IGNORECASE,
                )

                # 替换换行与空格
                text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
                text = re.sub(r"</p>|</div>|</blockquote>", "\n", text, flags=re.IGNORECASE)
                text = re.sub(r"<[^>]+>", "", text)
                text = unescape(text).strip()

                for lk in extra_links:
                    if lk not in text:
                        text = f"{text}\n{lk}".strip()

                if text:
                    results.append((msg_id, text))
            except Exception:
                continue

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

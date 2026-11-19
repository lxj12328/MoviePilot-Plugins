"""
Pan123TgPilot Telegram 消息智能提取器
负责解析频道文本中的影视名称、分类、季集、画质参数、标准分享链接与秒传码
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .ranker import calculate_quality_score

try:
    from app.sdk.media import MetaInfo as _MPMetaInfo, MetaInfoPath as _MPMetaInfoPath
except Exception:  # pragma: no cover - 仅在无宿主环境的单元测试中回退
    _MPMetaInfo = None
    _MPMetaInfoPath = None


class TgMessageParser:
    """Telegram 消息解析引擎 (融合文本去噪、MoviePilot MetaInfo 与分享文件树校准)"""

    # 支持的视频文件扩展名与花絮/预告过滤特征
    VIDEO_EXTS = {
        ".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv",
        ".flv", ".rmvb", ".webm", ".iso", ".mpg", ".mpeg",
    }
    RE_EXTRA_VIDEO = re.compile(
        r"(?:^|[\s._\-\[\(【（])(?:extras?|featurettes?|trailers?|samples?|teaser|preview|pv\d*|cm\d*|ncop\d*|nced\d*|menu|making|behind[\s._-]*the[\s._-]*scenes|预告片?|花絮|片花|特辑|彩蛋)(?:[\s._\-\]\)】）]|$|\.)",
        re.IGNORECASE,
    )

    # 正则规则库 (全面覆盖 123pan.com / 123pan.cn / *.share.123pan.cn 及 123684/123865/123912/123952/123592 等官方分流域名，兼容 /s/ 与 /123pan/ 路径)
    RE_FS_LINK = re.compile(r"(123FSLink(?:V2)?\$[a-fA-F0-9]{32,40}#\d+#[^\r\n]+)", re.IGNORECASE)
    RE_SHARE_URL = re.compile(
        r"https?://(?:[a-zA-Z0-9-]+\.)*(?:123pan\.(?:com|cn)|123(?:684|685|865|912|952|592|\d{3})\.(?:com|cn))/(?:s|123pan)/([a-zA-Z0-9_-]+)(?:\?[^ \r\n\"'\)\]]*)?",
        re.IGNORECASE,
    )
    RE_EXTRACT_PWD = re.compile(r"(?:提取码|密码|访问码|pwd)[：:\s=]*(?:是|为)?\s*([a-zA-Z0-9]{4,6})", re.IGNORECASE)

    RE_SEASON_EP_RANGE = re.compile(
        r"\bS(\d{1,3})\s*E(\d{1,4})\s*[-~～至]\s*(?:S(\d{1,3})\s*)?E?(\d{1,4})\b",
        re.IGNORECASE,
    )
    RE_SEASON_EPISODE = re.compile(r"\bS(\d{1,3})(?:E(\d{1,4}))?\b", re.IGNORECASE)
    RE_SEASON_RANGE = re.compile(
        r"(?:\bS(\d{1,2})\s*[-~～至]\s*S(\d{1,2})\b|第\s*(\d{1,2})\s*[-~～至]\s*(\d{1,2})\s*季)",
        re.IGNORECASE,
    )
    RE_EP_RANGE_ONLY = re.compile(
        r"(?:(?:(?<!%)\b(?:EP|E)|第|(?:集数|全集)[：:\s]*)\s*(\d{1,4})\s*[-~～至]\s*(?:EP|E|第)?\s*(\d{1,4})\s*(?:集|话|期|\b)|\b(\d{1,4})\s*[-~～至]\s*(\d{1,4})\s*(?:集|话|期))",
        re.IGNORECASE,
    )
    # 严格匹配集数：必须带有集/话/期，或明确写有 EPxx/Exx/更新至第xx集，杜绝将「第5部/第2场」与 URL %E5 误判为集数
    RE_EPISODE_ONLY = re.compile(
        r"(?:(?<!%)\b(?:EP|E)\s*(\d{1,4})\b|(?:更新至|更至)\s*第?\s*(\d{1,4})\s*(?:集|话|期)?|第\s*(\d{1,4})\s*(?:集|话|期))",
        re.IGNORECASE,
    )
    RE_SEASON_ONLY = re.compile(r"(?:\b(?:Season|S)\s*(\d{1,2})\b|第\s*(\d{1,2})\s*季)", re.IGNORECASE)

    RE_TMDB = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)", re.IGNORECASE)
    RE_TMDB_TAG = re.compile(r"\[tmdbid=(\d+)\]|\{tmdb-(\d+)\}|(?:🆔\s*TMDB|TMDB\s*ID)[：:\s]*(\d+)", re.IGNORECASE)

    RE_RESOLUTION = re.compile(r"\b(2160p|4k|uhd|1080p|1080i|720p|480p|576p)\b", re.IGNORECASE)
    RE_CODEC = re.compile(r"\b(H\.?265|HEVC|x265|H\.?264|AVC|x264|AV1)\b", re.IGNORECASE)
    RE_DOVI_HDR = re.compile(r"\b(DoVi|Dolby\s*Vision|HDR10\+|HDR10|HDR|HLG)\b", re.IGNORECASE)
    RE_SOURCE = re.compile(
        r"\b(Remux|UHD[\s.-]*Blu-?Ray|Blu-?Ray|WEB-?DL|WEBRip|HDTV|TVRip|DVDRip|CAMRip|HDTS|HDTC|枪版)\b",
        re.IGNORECASE,
    )
    RE_SIZE = re.compile(r"(?:体积|大小|Size|💾|📦)[：:\s]*([\d.]+)\s*(GB|MB|TB|G|M)", re.IGNORECASE)

    # 识别字段头与简介行的辅助正则，用于噪声隔离
    _RE_SYNOPSIS_START = re.compile(
        r"^[📖📝\s*]*?(?:(?:剧情|内容)?简介)\s*[：:]?",
        re.IGNORECASE,
    )
    _RE_META_LINE_START = re.compile(
        r"^(?:[🎬📺🎥🏷️🏷📌📢🔥✨🌟⭐️⭐🔗📁📂📅🗂️🗂🎞️🎞💾📝🖥️🖥🔊📦📼🔄🎭🍅📹💬👤📖🆔🖼️🖼]"
        r"|(?:类型|分类|画质|视频|音频|字幕|集数|全集|大小|体积|版本|链接|分享链接|状态|分享|首播|首播日期|播出日期|上映|上映日期|年份|标签|评分|TMDB|TMDB评分|豆瓣评分|名称|片名|剧名|剧集|电影|提取码|密码|访问码|Size)\s*[：:]"
        r"|S\d{1,3}(?:E\d{1,4}|\s*[-~～至]\s*S\d{1,3}|\s|$)"
        r"|Season\s*\d+"
        r"|第\s*\d+\s*[季集])",
        re.IGNORECASE,
    )
    _RE_TAG_OR_RATING_LINE = re.compile(
        r"^(?:[🏷️🏷\s*]*(?:标签)\s*[：:]"
        r"|[🏷️🏷\s*]*类型\s*[：:]\s*#"
        r"|[⭐⭐️🍅]+"
        r"|(?:TMDB|豆瓣)\s*评分\s*[：:]"
        r"|(?:评分)\s*[：:]"
        r"|(?:[🏷️🏷\s]*#[^\s#]+(?:\s+#[^\s#]+)*)\s*$)",
        re.IGNORECASE,
    )

    @classmethod
    def parse_message(cls, text: str, msg_id: Optional[str] = None, channel: str = "") -> Optional[Dict[str, Any]]:
        """
        全量解析一条 Telegram 频道消息文本，若提取到有效的 123 链接或秒传码则返回结构化资源字典
        """
        if not text or not isinstance(text, str):
            return None

        # 1. 提取 123 分享链接与秒传码
        fs_match = cls.RE_FS_LINK.search(text)
        fslink = None
        if fs_match:
            # 允许文件名中包含空格等字符，剥离末尾的标点符号、引号及闭合括号
            fslink = fs_match.group(1).rstrip("。，,.）)]】\"' \t\r\n")

        share_match = cls.RE_SHARE_URL.search(text)
        share_url = share_match.group(0).strip() if share_match else None
        share_key = share_match.group(1).strip() if share_match else None

        if not fslink and not share_url:
            return None  # 无 123 资源，忽略

        # 提取密码
        share_pwd = ""
        if share_url:
            parsed = urlparse(share_url)
            qs = parse_qs(parsed.query)
            if "pwd" in qs and qs["pwd"]:
                share_pwd = qs["pwd"][0].strip()
            if not share_pwd:
                # 尝试从正文匹配
                pwd_m = cls.RE_EXTRACT_PWD.search(text)
                if pwd_m:
                    share_pwd = pwd_m.group(1).strip()

        # 2. 提取秒传码元数据 (若有)
        fs_etag, fs_size, fs_filename = "", 0, ""
        if fslink:
            try:
                # 123FSLinkV2$<md5_or_sha1>#<size>#<filename>
                parts = fslink.split("$", 1)[1].split("#", 2)
                if len(parts) >= 3:
                    fs_etag = parts[0].strip().lower()
                    fs_size = int(parts[1].strip())
                    raw_fn = parts[2].strip().rstrip("。，,.）)]】\"' \t\r\n")
                    # 若因原发布者缺失扩展名或异常截断，智能兜底补齐 .mkv
                    if "." not in raw_fn or raw_fn.endswith("."):
                        raw_fn = raw_fn.rstrip(".") + ".mkv"
                    fs_filename = raw_fn
            except Exception:
                pass

        # 3. 提取影视标题与年份
        title, year = cls._extract_title_and_year(text, fs_filename)
        clean_text = cls._strip_noise_for_meta(text)

        # 4. 识别影视分类 (movie vs tv) 与季集标识 (基于去噪文本 + 宿主 MetaInfo)
        media_type, season, episode, ep_tag = cls._determine_media_type(
            clean_text, fs_filename, raw_text=text
        )
        _, ep_start, ep_end, is_ep_range = cls.parse_ep_range(ep_tag)
        if ep_end is None and episode is not None:
            ep_start = episode
            ep_end = episode

        # 5. 构建展示用标题：剧集统一附加 SxxExx 标识，便于清单展示与按集区分；title 仍保持纯净剧名供网盘创建总文件夹
        display_title = title
        if media_type == "tv" and ep_tag:
            if ep_tag.upper() not in title.upper():
                display_title = f"{title} {ep_tag}"

        # 6. 提取画质参数与体积 (优先使用去噪文本 + 秒传文件名)
        spec_text = f"{clean_text}\n{fs_filename}" if fs_filename else clean_text
        resolution, codec, dovi_hdr, file_size_bytes = cls._extract_quality_params(spec_text, fs_size)
        if file_size_bytes <= 0:
            _, _, _, file_size_bytes = cls._extract_quality_params(text, fs_size)

        # 7. 计算 TMDB ID 与唯一媒体洗版锚点 (media_key)
        tmdb_id = cls._extract_tmdb_id(text)
        media_key = cls._build_media_key(title, year, tmdb_id, media_type, ep_tag)

        # 8. 提取片源并计算综合质量得分
        source_val = cls._extract_source(spec_text)
        score = calculate_quality_score(
            resolution=resolution,
            dovi_hdr=dovi_hdr,
            codec=codec,
            source=source_val,
            file_size_bytes=file_size_bytes,
        )

        return {
            "channel": channel,
            "msg_id": msg_id,
            "media_key": media_key,
            "title": title,
            "display_title": display_title,
            "media_title": display_title,
            "year": year,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "season": season,
            "episode": episode,
            "ep_start": ep_start,
            "ep_end": ep_end,
            "is_ep_range": is_ep_range,
            "ep_tag": ep_tag,
            "resolution": resolution,
            "codec": codec,
            "dovi_hdr": dovi_hdr,
            "file_size": file_size_bytes,
            "quality_score": score,
            "share_url": share_url,
            "share_key": share_key,
            "share_pwd": share_pwd,
            "fslink": fslink,
            "fs_etag": fs_etag,
            "fs_size": fs_size,
            "fs_filename": fs_filename,
            "raw_text": text,
        }

    # 快捷调用别名
    parse = parse_message

    @classmethod
    def _extract_title_and_year(cls, text: str, fallback_filename: str = "") -> Tuple[str, str]:
        """提取影视主标题与上映年份（兼容 @Pan123Movie / @regeng123 / @yun123pan 等多频道排版）"""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        candidate_title = ""
        year = ""

        meta_only_icon_re = re.compile(r"^[🆔🏷⭐🍅📅🗓💾📦🖥📹💬👤📖📝🔗🖼🔊📼🔄🎭🗂]")
        meta_field_re = re.compile(
            r"^(?:(?:TMDB|豆瓣|IMDB)\s*(?:ID|评分)?|类型|分类|标签|评分|简介|剧情简介|内容简介|分享链接|链接|版本"
            r"|提取码|访问码|密码|大小|体积|画质|编码|字幕|音频|视频|集数|全集|状态|频道|群组|投稿|注意|提示|声明|免责"
            r"|播出日期|首播日期|首播|上映日期|上映|年份|分享|地区|国家|语言|导演|主演|演员|更新|Size)\s*[：:]",
            re.IGNORECASE,
        )
        promo_prefix_re = re.compile(r"^(?:频道|群组|投稿|注意|提示|声明|免责|关注)\s*(?:[：:]|\s|@)", re.IGNORECASE)

        title_prefix_pattern = (
            r"^(?:(?:影视|资源|影片|剧集|电视剧|电影|动[画漫]|番剧|综艺|短剧|纪录片|节目|中文)?(?:名称|片名|剧名|番名|标题|译名)"
            r"|(?:剧集|电视剧|电影|动[画漫]|番剧|综艺|短剧|纪录片|资源|影片))[：:\s]+"
        )

        # 1. 优先扫描前 8 行，寻找带「名称：/片名：/剧集：/电影：」或带年份或首行标题
        for line in lines[:8]:
            # 清理常见 emoji、markdown 加粗星号与话题符号
            cleaned = re.sub(
                r"^[🎬📺🎥🏷️📌📢🔥✨🌟⭐️💥🎉🔗📁📂▶️✅🔸🔹📅🗂🏷🎞💾📝🖥️🔊📦📼🔄🎭🍅🖥📹💬👤📖🆔🖼\ufe0f\s#*]+",
                "",
                line,
            ).strip()
            cleaned = re.sub(r"\*+", "", cleaned).strip()
            if not cleaned:
                continue

            # 剥离常见字段前缀，例如 "名称：仙逆 (2023)" / "剧集：谷雨 (2026) S01E06" / "剧集：折月亮" / "电影：xxx"
            has_title_prefix = bool(re.match(title_prefix_pattern, cleaned, flags=re.IGNORECASE))
            cleaned = re.sub(title_prefix_pattern, "", cleaned, flags=re.IGNORECASE).strip()

            # 移除 Markdown 链接包裹 [标题](url 或 ?q=...) -> 标题
            cleaned = re.sub(r"\[#?([^\]]+)\]\([^\)]+\)", r"\1", cleaned).strip()

            if not cleaned:
                continue

            if not has_title_prefix and (
                meta_only_icon_re.match(line.lstrip())
                or meta_field_re.match(cleaned)
                or promo_prefix_re.match(cleaned)
                or any(tok in cleaned.lower() for tok in ("http://", "https://", "123fslink"))
            ):
                continue

            # 匹配 标题 (2026) 格式
            m_year = re.search(r"^(.*?)(?:\s*[（\(](\d{4})[）\)]|\s*\[(\d{4})\])", cleaned)
            if m_year and m_year.group(1).strip():
                candidate_title = m_year.group(1).strip()
                year = m_year.group(2) or m_year.group(3) or ""
                break

            # 若显式带有「名称：/片名：/剧集：/电影：」前缀，直接采纳
            if has_title_prefix and cleaned:
                candidate_title = cleaned
                break

            if not candidate_title:
                candidate_title = cleaned

        if not candidate_title and fallback_filename:
            # 从秒传文件名逆向提取
            m_fn = re.search(r"^(.*?)(?:\s*[（\(](\d{4})[）\)]|\s*\[(\d{4})\])", fallback_filename)
            if m_fn:
                candidate_title = m_fn.group(1).strip()
                year = m_fn.group(2) or m_fn.group(3) or ""
            else:
                m_dot_yr = re.search(r"^(.*?)[.\s]+((?:19|20)\d{2})(?:[.\s]|$)", fallback_filename)
                if m_dot_yr and m_dot_yr.group(1).strip():
                    candidate_title = m_dot_yr.group(1).strip()
                    year = m_dot_yr.group(2)
                else:
                    candidate_title = re.sub(r"-\s*\d{3,4}p.*", "", fallback_filename).strip()

        # 剥离前导论坛分类标签 (例如 "[电影] [123云盘] 《流浪地球3》" / "【4K】片名")，但保留单独完整片名【片名】
        candidate_title = re.sub(r"^[🎬📺🎥🏷️📌📢🔥✨\s]+", "", candidate_title).strip()
        while True:
            m_tag_prefix = re.match(
                r"^(?:\[(?:电影|剧集|电视剧|国漫|动漫|综艺|短剧|纪录片|123云?盘|4K|2160[pP]|1080[pP]|HDR|DoVi|资源|分享|\d{4})\]"
                r"|【(?:电影|剧集|电视剧|国漫|动漫|综艺|短剧|纪录片|123云?盘|4K|2160[pP]|1080[pP]|HDR|DoVi|资源|分享|\d{4})】)\s*(.+)$",
                candidate_title,
                flags=re.IGNORECASE,
            )
            if not m_tag_prefix:
                break
            candidate_title = m_tag_prefix.group(1).strip()

        candidate_title = re.sub(title_prefix_pattern, "", candidate_title, flags=re.IGNORECASE).strip()
        m_book = re.match(r"^《([^》]+)》", candidate_title)
        if m_book:
            candidate_title = m_book.group(1).strip()
        else:
            m_bracket = re.match(r"^【([^】]+)】$", candidate_title)
            if m_bracket:
                candidate_title = m_bracket.group(1).strip()

        # 若标题尾部跟有明显的更新/集数/规格标签（如 "仙逆 更新至80集"、"谷雨 S01E06"、"片名 [2160p]"），截取主标题
        candidate_title = re.sub(
            r"\s+(?:更新至|更至|全\d+|第\s*\d+\s*[集季话期]|S\d{1,2}(?:E\d{1,4})?|E\d{1,4}|4K|2160[pP]|1080[pP]|\[(?:2160[pP]|1080[pP]|4K|123云?盘).*$).*$",
            "",
            candidate_title,
            flags=re.IGNORECASE,
        ).strip()

        if not year:
            m_any_yr = re.search(r"[（\(\[](\d{4})[）\)\]]", text)
            if m_any_yr:
                year = m_any_yr.group(1)
            else:
                # 尝试从「播出日期：2026-11-06」「首播：2026-07-10」「上映日期：2026」提取年份
                m_date_yr = re.search(
                    r"(?:播出日期|首播日期|首播|上映日期|上映|发行日期|日期|年份)[：:\s]*((?:19|20)\d{2})\b",
                    text,
                )
                if m_date_yr:
                    year = m_date_yr.group(1)

        return candidate_title or "未知影视", year

    @classmethod
    def _strip_noise_for_meta(cls, text: str) -> str:
        """
        剥离 URL、秒传负载、简介剧情段落、标签与评分行，避免 `?pwd=e05` 或剧情数字污染 SxxExx 识别。
        注：标题、年份、TMDB 链接与分享链接仍从原始文本直接提取。
        """
        if not text:
            return ""
        # 移除秒传特征码、Markdown 链接目标 (含相对标签 ?q=%23%E5...)、HTTP(S) URL 与百分号编码噪声
        scrubbed = cls.RE_FS_LINK.sub("", text)
        scrubbed = re.sub(r"\]\([^)\r\n]+\)", "]", scrubbed)
        scrubbed = re.sub(r"https?://[^\s\)\]\>\"']+", "", scrubbed)
        scrubbed = re.sub(r"%[0-9A-Fa-f]{2}", " ", scrubbed)

        kept_lines: List[str] = []
        in_synopsis = False
        for raw_line in scrubbed.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if cls._RE_SYNOPSIS_START.match(line):
                in_synopsis = True
                continue

            if in_synopsis:
                if cls._RE_META_LINE_START.match(line):
                    in_synopsis = False
                else:
                    continue

            if cls._RE_TAG_OR_RATING_LINE.match(line):
                continue

            kept_lines.append(line)

        return "\n".join(kept_lines)

    @staticmethod
    def _normalize_mp_type(val: Any) -> str:
        """将 MoviePilot MediaType 枚举或字符串归一化为 'movie' / 'tv' / ''"""
        if val is None:
            return ""
        raw = getattr(val, "value", None) or getattr(val, "name", None) or val
        s = str(raw or "").strip().lower()
        if any(k in s for k in ("tv", "剧", "anime", "动漫", "综艺", "series")):
            return "tv"
        if any(k in s for k in ("movie", "film", "电影")):
            return "movie"
        return ""

    @classmethod
    def _parse_with_mp_metainfo(cls, title_text: str, subtitle_text: str = "") -> Optional[Dict[str, Any]]:
        """
        调用 MoviePilot V3 宿主 SDK (`app.sdk.media.MetaInfo`) 执行通用命名识别。
        在非宿主环境或异常时安全返回 None 并交由本地增强解析器兜底。
        """
        if _MPMetaInfo is None:
            return None
        try:
            meta = _MPMetaInfo(title=title_text, subtitle=subtitle_text)
        except Exception:
            return None
        if meta is None:
            return None

        def _opt_int(attr: str) -> Optional[int]:
            v = getattr(meta, attr, None)
            if v is None or isinstance(v, bool):
                return None
            try:
                return int(v)
            except (ValueError, TypeError):
                return None

        b_season = _opt_int("begin_season")
        e_season = _opt_int("end_season")
        b_ep = _opt_int("begin_episode")
        e_ep = _opt_int("end_episode")
        m_type = cls._normalize_mp_type(getattr(meta, "type", None))

        ep_tag = ""
        season = b_season
        episode = e_ep if e_ep is not None else b_ep
        is_range = False

        if b_season is not None and e_season is not None and e_season > b_season and b_ep is None:
            ep_tag = f"S{b_season:02d}-S{e_season:02d}"
            m_type = "tv"
            is_range = True
        elif b_ep is not None:
            s_num = b_season if b_season is not None else 1
            season = s_num
            if e_ep is not None and e_ep > b_ep:
                ep_tag = f"S{s_num:02d}E{b_ep:02d}-E{e_ep:02d}"
                episode = e_ep
                is_range = True
            else:
                ep_tag = f"S{s_num:02d}E{b_ep:02d}"
                episode = b_ep
            m_type = "tv"
        elif b_season is not None:
            ep_tag = f"S{b_season:02d}"
            m_type = "tv"

        return {
            "media_type": m_type,
            "season": season,
            "episode": episode,
            "ep_tag": ep_tag,
            "is_range": is_range,
            "resolution": str(getattr(meta, "resource_pix", "") or "").strip(),
            "codec": str(getattr(meta, "video_encode", "") or "").strip(),
            "dovi_hdr": str(getattr(meta, "resource_effect", "") or "").strip(),
            "source": str(getattr(meta, "resource_type", "") or getattr(meta, "edition", "") or "").strip(),
        }

    @classmethod
    def _determine_media_type(
        cls,
        text: str,
        filename: str = "",
        raw_text: str = "",
    ) -> Tuple[str, Optional[int], Optional[int], str]:
        """
        判断是电影 (movie) 还是电视剧/剧集 (tv)，结合文本去噪与 MoviePilot V3 MetaInfo。
        :return: (media_type: 'movie'|'tv', season: int|None, episode: int|None, ep_tag: str)
        """
        orig_text = raw_text or text
        tmdb_combined = f"{orig_text}\n{filename}"
        clean_text = cls._strip_noise_for_meta(text)
        combined = f"{clean_text}\n{filename}" if filename else clean_text

        # ---------------------------------------------------------
        # 1. 优先检查 TMDB 链接特征 (最权威类型指示，防止电影被弱正则误判为电视剧)
        # ---------------------------------------------------------
        tmdb_m = cls.RE_TMDB.search(tmdb_combined)
        is_tmdb_movie = bool(tmdb_m and tmdb_m.group(1).lower() == "movie")
        is_tmdb_tv = bool(tmdb_m and tmdb_m.group(1).lower() == "tv")

        # ---------------------------------------------------------
        # 2. 检查正文中的显式类型/分类声明 (例如: 📺类型 : 欧美电影, 🎬 电影：xxx)
        # ---------------------------------------------------------
        has_explicit_movie = bool(
            re.search(r"(?:^|\n)\s*[🎬📺🎥🏷️📌📢🔥✨\s#*]*(?:电影)[：:\s]", combined)
            or re.search(r"(?:类型|分类)[：:\s]*[^\r\n]*(?:欧美电影|华语电影|日韩电影|网络电影|动画电影|纪录片电影|电影)", combined)
        )
        has_explicit_tv = bool(
            re.search(r"(?:^|\n)\s*[🎬📺🎥🏷️📌📢🔥✨\s#*]*(?:剧集|电视剧|短剧|番剧)[：:\s]", combined)
            or re.search(r"(?:类型|分类)[：:\s]*[^\r\n]*(?:国产剧集|欧美剧集|日韩剧集|剧集|电视剧|美剧|日剧|韩剧|国剧|国产剧|泰剧|连续剧|短剧|番剧)", combined)
        )

        # 权威电影判定：若 TMDB 确认为 movie，或正文明确声明为电影 (且未声明为剧集)，则强制判定为电影，清空所有季集
        if is_tmdb_movie or (has_explicit_movie and not has_explicit_tv):
            return "movie", None, None, ""

        # ---------------------------------------------------------
        # 3. 候选识别：并行采集 MoviePilot MetaInfo 与本地增强正则结果，按精确度择优
        # ---------------------------------------------------------
        m_s_only = cls.RE_SEASON_ONLY.search(combined)
        detected_season = int(m_s_only.group(1) or m_s_only.group(2)) if m_s_only else 1

        # 3.1 本地正则识别
        local_result: Optional[Tuple[int, str, Optional[int], Optional[int], str]] = None
        # 优先级评分: 4=剧集区间, 3=单集(季+集), 2=多季范围, 1=仅单季
        for m_ser in cls.RE_SEASON_EP_RANGE.finditer(combined):
            season = int(m_ser.group(1))
            end_season = int(m_ser.group(3)) if m_ser.group(3) else season
            ep_start = int(m_ser.group(2))
            ep_end = int(m_ser.group(4))
            if end_season == season and ep_start <= ep_end:
                is_rng = ep_end > ep_start
                ep_tag = (
                    f"S{season:02d}E{ep_start:02d}-E{ep_end:02d}"
                    if is_rng
                    else f"S{season:02d}E{ep_start:02d}"
                )
                local_result = (4 if is_rng else 3, "tv", season, ep_end, ep_tag)
                break

        m_se = cls.RE_SEASON_EPISODE.search(combined)
        if local_result is None and m_se and m_se.group(2):
            season = int(m_se.group(1))
            episode = int(m_se.group(2))
            local_result = (3, "tv", season, episode, f"S{season:02d}E{episode:02d}")

        if local_result is None:
            for m_epr in cls.RE_EP_RANGE_ONLY.finditer(combined):
                s_raw = m_epr.group(1) or m_epr.group(3)
                e_raw = m_epr.group(2) or m_epr.group(4)
                if not s_raw or not e_raw:
                    continue
                ep_start, ep_end = int(s_raw), int(e_raw)
                if ep_start <= ep_end:
                    is_rng = ep_end > ep_start
                    season = int(m_se.group(1)) if m_se else detected_season
                    ep_tag = (
                        f"S{season:02d}E{ep_start:02d}-E{ep_end:02d}"
                        if is_rng
                        else f"S{season:02d}E{ep_start:02d}"
                    )
                    local_result = (4 if is_rng else 3, "tv", season, ep_end, ep_tag)
                    break

        if local_result is None:
            m_ep = cls.RE_EPISODE_ONLY.search(combined)
            if m_ep:
                ep_val = m_ep.group(1) or m_ep.group(2) or m_ep.group(3)
                if ep_val:
                    episode = int(ep_val)
                    season = int(m_se.group(1)) if m_se else detected_season
                    local_result = (3, "tv", season, episode, f"S{season:02d}E{episode:02d}")

        if local_result is None:
            m_sr = cls.RE_SEASON_RANGE.search(combined)
            if m_sr:
                s1 = int(m_sr.group(1) or m_sr.group(3))
                s2 = int(m_sr.group(2) or m_sr.group(4))
                if s1 < s2:
                    local_result = (2, "tv", s1, None, f"S{s1:02d}-S{s2:02d}")

        if local_result is None:
            if m_se:
                season = int(m_se.group(1))
                local_result = (1, "tv", season, None, f"S{season:02d}")
            elif m_s_only:
                local_result = (1, "tv", detected_season, None, f"S{detected_season:02d}")

        # 3.2 宿主 MetaInfo 识别 (当可用时捕获常规正则可能遗漏的复杂命名格式)
        mp_result: Optional[Tuple[int, str, Optional[int], Optional[int], str]] = None
        if _MPMetaInfo is not None:
            clean_lines = [ln.strip() for ln in clean_text.splitlines() if ln.strip()]
            primary_title = filename or (clean_lines[0] if clean_lines else "")
            mp_info = cls._parse_with_mp_metainfo(primary_title, clean_text)
            if mp_info and mp_info.get("media_type") == "tv" and mp_info.get("ep_tag"):
                if mp_info.get("episode") is not None and mp_info.get("is_range"):
                    mp_prec = 4
                elif mp_info.get("episode") is not None:
                    mp_prec = 3
                elif mp_info.get("is_range"):
                    mp_prec = 2
                else:
                    mp_prec = 1
                mp_result = (
                    mp_prec,
                    "tv",
                    mp_info.get("season"),
                    mp_info.get("episode"),
                    str(mp_info.get("ep_tag") or ""),
                )

        if mp_result and (local_result is None or mp_result[0] > local_result[0]):
            return mp_result[1], mp_result[2], mp_result[3], mp_result[4]
        if local_result:
            return local_result[1], local_result[2], local_result[3], local_result[4]

        # ---------------------------------------------------------
        # 4. 若无具体集数，但 TMDB 或类型明确为剧集
        # ---------------------------------------------------------
        if is_tmdb_tv or has_explicit_tv:
            return "tv", detected_season, None, f"S{detected_season:02d}"

        # 默认归为电影 (无季集)
        return "movie", None, None, ""

    @staticmethod
    def _normalize_resolution(res_raw: str) -> str:
        val = (res_raw or "").strip().lower()
        if not val:
            return ""
        if val in ("4k", "2160p", "uhd"):
            return "2160p"
        if val in ("1080p", "1080i", "fhd"):
            return "1080p"
        if val in ("720p", "hd"):
            return "720p"
        if val in ("480p", "sd", "576p"):
            return "480p"
        return val

    @staticmethod
    def _normalize_codec(codec_raw: str) -> str:
        val = (codec_raw or "").strip().upper()
        if not val:
            return ""
        if "265" in val or "HEVC" in val:
            return "H.265"
        if "264" in val or "AVC" in val:
            return "H.264"
        if "AV1" in val:
            return "AV1"
        return val

    @staticmethod
    def _normalize_dovi_hdr(dovi_raw: str) -> str:
        val = (dovi_raw or "").strip().upper()
        if not val:
            return ""
        if "DOVI" in val or "VISION" in val or val == "DV":
            return "DoVi"
        if "HDR10+" in val:
            return "HDR10+"
        if "HDR" in val:
            return "HDR"
        if "HLG" in val:
            return "HLG"
        return dovi_raw.strip()

    @classmethod
    def _extract_source(cls, text: str) -> str:
        """提取并标准化片源介质 (BluRay / WEB-DL / HDTV / DVDRip / CAM)"""
        m = cls.RE_SOURCE.search(text or "")
        if not m:
            return ""
        raw = m.group(1).lower()
        if "remux" in raw or "blu" in raw:
            return "BluRay"
        if "web" in raw:
            return "WEB-DL"
        if "hdtv" in raw or "tvrip" in raw:
            return "HDTV"
        if "dvdrip" in raw:
            return "DVDRip"
        if any(k in raw for k in ("cam", "hdts", "hdtc", "枪版")):
            return "CAM"
        return m.group(1).strip()

    @classmethod
    def _extract_explicit_specs(cls, text: str) -> Dict[str, str]:
        """提取文本/文件名中显式出现的高清规格字段（不填充默认值）"""
        probe = text or ""
        res_m = cls.RE_RESOLUTION.search(probe)
        codec_m = cls.RE_CODEC.search(probe)
        dovi_m = cls.RE_DOVI_HDR.search(probe)
        return {
            "resolution": cls._normalize_resolution(res_m.group(1)) if res_m else "",
            "codec": cls._normalize_codec(codec_m.group(1)) if codec_m else "",
            "dovi_hdr": cls._normalize_dovi_hdr(dovi_m.group(1)) if dovi_m else "",
            "source": cls._extract_source(probe),
        }

    @classmethod
    def _extract_quality_params(cls, text: str, fallback_size: int = 0) -> Tuple[str, str, str, int]:
        """提取标准化分辨率、编码、HDR 和文件体积"""
        specs = cls._extract_explicit_specs(text)
        resolution = specs["resolution"] or "1080p"
        codec = specs["codec"] or "H.264"
        dovi_hdr = specs["dovi_hdr"]

        file_size_bytes = fallback_size
        if file_size_bytes <= 0:
            sz_m = cls.RE_SIZE.search(text)
            if sz_m:
                val = float(sz_m.group(1))
                unit = sz_m.group(2).upper()
                if "T" in unit:
                    file_size_bytes = int(val * 1024 * 1024 * 1024 * 1024)
                elif "G" in unit:
                    file_size_bytes = int(val * 1024 * 1024 * 1024)
                elif "M" in unit:
                    file_size_bytes = int(val * 1024 * 1024)

        return resolution, codec, dovi_hdr, file_size_bytes

    @classmethod
    def _extract_tmdb_id(cls, text: str) -> Optional[str]:
        """提取 TMDB ID"""
        m_tag = cls.RE_TMDB_TAG.search(text)
        if m_tag:
            return m_tag.group(1) or m_tag.group(2) or m_tag.group(3)
        m_url = cls.RE_TMDB.search(text)
        if m_url:
            return m_url.group(2)
        return None

    @classmethod
    def _build_media_key(
        cls,
        title: str,
        year: str,
        tmdb_id: Optional[str],
        media_type: str,
        ep_tag: str,
    ) -> str:
        """
        组装媒体唯一身份键 (用于去重与洗版对比)。
        统一采用规范化影视主标题 + 年份 + 季集组合，
        确保不同频道（无论是否附带 TMDB 链接）发布同一部作品时都能生成完全一致的识别键！
        """
        norm_title = re.sub(r"[^\w\u4e00-\u9fa5]", "", title).lower()
        if media_type == "tv":
            tag = f"_{ep_tag}" if ep_tag else ""
            return f"tv_{norm_title}{tag}"
        else:
            yr_str = f"_{year}" if year else ""
            return f"movie_{norm_title}{yr_str}"

    @classmethod
    def parse_ep_range(cls, ep_tag_or_text: str) -> Tuple[Optional[int], Optional[int], Optional[int], bool]:
        """
        从标准化 ep_tag 或标题中提取 (season, ep_start, ep_end, is_ep_range)。
        例如:
          "S01E01-E20" -> (1, 1, 20, True)
          "今天是太阳雨 S01E01-E17" -> (1, 1, 17, True)
          "S01-S06" -> (1, None, None, True)
          "S01E05" -> (1, 5, 5, False)
          "S02" -> (2, None, None, False)
        """
        text = str(ep_tag_or_text or "").strip()
        if not text:
            return None, None, None, False
        m_rng = re.search(
            r"\bS(\d{1,3})E(\d{1,4})\s*[-~～至]\s*E?(\d{1,4})\b",
            text,
            re.IGNORECASE,
        )
        if m_rng:
            s = int(m_rng.group(1))
            e1 = int(m_rng.group(2))
            e2 = int(m_rng.group(3))
            if e1 <= e2:
                return s, e1, e2, (e2 > e1)
        m_one = re.search(r"\bS(\d{1,3})E(\d{1,4})\b", text, re.IGNORECASE)
        if m_one:
            s = int(m_one.group(1))
            ep = int(m_one.group(2))
            return s, ep, ep, False
        m_sr = cls.RE_SEASON_RANGE.search(text)
        if m_sr:
            s1 = int(m_sr.group(1) or m_sr.group(3))
            s2 = int(m_sr.group(2) or m_sr.group(4))
            if s1 <= s2:
                return s1, None, None, (s2 > s1)
        m_s = re.search(r"\bS(\d{1,3})\b", text, re.IGNORECASE)
        if m_s:
            return int(m_s.group(1)), None, None, False
        return None, None, None, False

    @classmethod
    def _extract_file_se(
        cls,
        rel_path: str,
        filename: str,
        allow_bare_ep: bool = False,
        default_season: Optional[int] = None,
    ) -> Tuple[Optional[int], Optional[int], Dict[str, str]]:
        """
        从单个分享文件路径及文件名中提取 (season, episode, explicit_specs)。
        优先结合 MoviePilot SDK MetaInfoPath 与本地路径/文件名规则，
        支持目录表达季号 + 文件名表达集号 (如 Season 2/EP05.mkv 或 Season 1/01.mp4)。
        """
        path_str = rel_path or filename or ""
        fn_str = filename or Path(path_str).name
        stem = Path(fn_str).stem
        parent_str = str(Path(rel_path).parent) if rel_path and str(Path(rel_path).parent) not in ("", ".") else ""

        mp_season: Optional[int] = None
        mp_ep: Optional[int] = None
        mp_specs: Dict[str, str] = {"resolution": "", "codec": "", "dovi_hdr": "", "source": ""}

        if _MPMetaInfoPath is not None and path_str:
            try:
                meta = _MPMetaInfoPath(Path(path_str))
                b_s = getattr(meta, "begin_season", None)
                b_e = getattr(meta, "begin_episode", None)
                if isinstance(b_s, int) and not isinstance(b_s, bool) and b_s >= 0:
                    mp_season = b_s
                if isinstance(b_e, int) and not isinstance(b_e, bool) and b_e >= 0:
                    mp_ep = b_e
                mp_specs = {
                    "resolution": cls._normalize_resolution(str(getattr(meta, "resource_pix", "") or "")),
                    "codec": cls._normalize_codec(str(getattr(meta, "video_encode", "") or "")),
                    "dovi_hdr": cls._normalize_dovi_hdr(str(getattr(meta, "resource_effect", "") or "")),
                    "source": cls._extract_source(
                        str(getattr(meta, "resource_type", "") or getattr(meta, "edition", "") or "")
                    ),
                }
            except Exception:
                pass

        # 本地正则解析（覆盖目录季号 + 文件名集号/纯数字集号）
        local_season: Optional[int] = None
        local_ep: Optional[int] = None

        m_se = re.search(r"\bS(\d{1,3})\s*E(\d{1,4})\b", fn_str, re.IGNORECASE)
        if not m_se and path_str:
            m_se = re.search(r"\bS(\d{1,3})\s*E(\d{1,4})\b", path_str, re.IGNORECASE)
        if m_se:
            local_season = int(m_se.group(1))
            local_ep = int(m_se.group(2))
        else:
            # 检查目录或文件名中的季号 (自内向外遍历目录层级，并跳过多季总目录名如 S01-S06)
            season_candidates = list(reversed(Path(parent_str).parts)) if parent_str else []
            season_candidates.append(stem)
            for candidate in season_candidates:
                if not candidate or cls.RE_SEASON_RANGE.search(candidate):
                    continue
                m_s = cls.RE_SEASON_ONLY.search(candidate)
                if m_s:
                    local_season = int(m_s.group(1) or m_s.group(2))
                    break

            # 检查文件名中的显式集号 (EP01 / E01 / 第01集)
            m_ep = cls.RE_EPISODE_ONLY.search(stem)
            if m_ep:
                ep_raw = m_ep.group(1) or m_ep.group(2) or m_ep.group(3)
                if ep_raw:
                    local_ep = int(ep_raw)
            elif allow_bare_ep and stem:
                # 仅在剧集上下文且未匹配常规标签时，识别独立纯数字集号 (如 "01.mkv", "Show - 05 [2160p].mkv")
                if not re.match(r"^(?:CD|Disc|Part|Pt)\s*\d+$", stem, re.IGNORECASE):
                    scrubbed = re.sub(
                        r"\b(?:19\d\d|20\d\d|2160p?|1080[pi]?|720p?|480p?|576p?|x264|x265|h\.?264|h\.?265|10bit|8bit|60fps|24fps)\b",
                        " ",
                        stem,
                        flags=re.IGNORECASE,
                    )
                    m_bare = re.match(r"^\s*0*(\d{1,3})(?:$|[\s._\-\[\(【（])", scrubbed)
                    if not m_bare:
                        m_bare = re.search(r"(?:^|[\s._\-\[\(【（])0*(\d{1,3})\s*$", scrubbed)
                    if m_bare:
                        cand_ep = int(m_bare.group(1))
                        if 1 <= cand_ep <= 300:
                            local_ep = cand_ep

        final_season = local_season if local_season is not None else mp_season
        final_ep = local_ep if local_ep is not None else mp_ep
        if final_ep is not None and final_season is None:
            final_season = default_season if default_season is not None else 1

        local_specs = cls._extract_explicit_specs(f"{parent_str} {fn_str}")
        merged_specs = {
            k: local_specs.get(k) or mp_specs.get(k) or ""
            for k in ("resolution", "codec", "dovi_hdr", "source")
        }
        return final_season, final_ep, merged_specs

    @classmethod
    def analyze_video_files(
        cls,
        files: List[Dict[str, Any]],
        hint_media_type: str = "",
        hint_season: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        汇总分析分享目录内的正片视频文件列表，自动剔除字幕/nfo 伴生文件及花絮/预告片，
        统计真实季集范围、正片数量、真实视频总体积与文件名中附带的高清规格。
        """
        video_files: List[Dict[str, Any]] = []
        for item in files or []:
            if not isinstance(item, dict):
                continue
            rel_path = str(item.get("rel_path") or item.get("name") or "").strip()
            name = str(item.get("name") or Path(rel_path).name or "").strip()
            if not name:
                continue
            if Path(name).suffix.lower() not in cls.VIDEO_EXTS:
                continue
            # 过滤预告、花絮、特典等非正片文件或目录
            parts = Path(rel_path or name).parts
            if any(cls.RE_EXTRA_VIDEO.search(part) for part in parts):
                continue
            video_files.append({"rel_path": rel_path or name, "name": name, "size": int(item.get("size") or 0)})

        if not video_files:
            return {
                "video_count": 0,
                "total_size": 0,
                "inferred_type": "",
                "season": None,
                "episode": None,
                "ep_start": None,
                "ep_end": None,
                "is_ep_range": False,
                "ep_tag": "",
                "specs": {},
            }

        total_size = sum(vf["size"] for vf in video_files if vf["size"] > 0)
        allow_bare_ep = (hint_media_type == "tv") or (len(video_files) >= 2)

        seasons_seen: set = set()
        season_eps: Dict[int, set] = {}
        spec_votes: Dict[str, Dict[str, int]] = {
            "resolution": {},
            "codec": {},
            "dovi_hdr": {},
            "source": {},
        }

        for vf in video_files:
            s_num, e_num, f_specs = cls._extract_file_se(
                vf["rel_path"],
                vf["name"],
                allow_bare_ep=allow_bare_ep,
                default_season=hint_season,
            )
            if s_num is not None:
                seasons_seen.add(s_num)
                if e_num is not None:
                    season_eps.setdefault(s_num, set()).add(e_num)
            for k, val in f_specs.items():
                if val and k in spec_votes:
                    spec_votes[k][val] = spec_votes[k].get(val, 0) + 1

        spec_rank = {
            "resolution": {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1},
            "codec": {"AV1": 3, "H.265": 2, "H.264": 1},
            "dovi_hdr": {"DoVi": 4, "HDR10+": 3, "HDR": 2, "HLG": 1},
            "source": {"BluRay": 5, "WEB-DL": 4, "HDTV": 3, "DVDRip": 2, "CAM": 1},
        }
        best_specs: Dict[str, str] = {}
        for k, counts in spec_votes.items():
            if not counts:
                continue
            best_specs[k] = max(
                counts.keys(),
                key=lambda v: (counts[v], spec_rank[k].get(v, 0)),
            )

        inferred_type = ""
        out_season: Optional[int] = None
        out_episode: Optional[int] = None
        ep_start: Optional[int] = None
        ep_end: Optional[int] = None
        is_ep_range = False
        ep_tag = ""

        if season_eps:
            inferred_type = "tv"
            reg_seasons = sorted(s for s in season_eps.keys() if s > 0) or sorted(season_eps.keys())
            if len(reg_seasons) >= 2:
                s_min, s_max = reg_seasons[0], reg_seasons[-1]
                out_season = s_min
                is_ep_range = s_max > s_min
                ep_tag = f"S{s_min:02d}-S{s_max:02d}"
            else:
                s_only = reg_seasons[0]
                eps = sorted(season_eps[s_only])
                out_season = s_only
                if len(eps) == 1:
                    ep_start = eps[0]
                    ep_end = eps[0]
                    out_episode = eps[0]
                    ep_tag = f"S{s_only:02d}E{eps[0]:02d}"
                else:
                    ep_start = eps[0]
                    ep_end = eps[-1]
                    out_episode = eps[-1]
                    is_ep_range = True
                    ep_tag = f"S{s_only:02d}E{ep_start:02d}-E{ep_end:02d}"
        elif seasons_seen:
            inferred_type = "tv"
            reg_seasons = sorted(s for s in seasons_seen if s > 0) or sorted(seasons_seen)
            if len(reg_seasons) >= 2:
                s_min, s_max = reg_seasons[0], reg_seasons[-1]
                out_season = s_min
                is_ep_range = s_max > s_min
                ep_tag = f"S{s_min:02d}-S{s_max:02d}"
            else:
                out_season = reg_seasons[0]
                ep_tag = f"S{out_season:02d}"
        elif len(video_files) == 1:
            inferred_type = "movie"
        elif hint_media_type == "tv":
            inferred_type = "tv"

        return {
            "video_count": len(video_files),
            "total_size": total_size,
            "inferred_type": inferred_type,
            "season": out_season,
            "episode": out_episode,
            "ep_start": ep_start,
            "ep_end": ep_end,
            "is_ep_range": is_ep_range,
            "ep_tag": ep_tag,
            "specs": best_specs,
        }

    @classmethod
    def calibrate_resource_from_files(
        cls,
        resource: Dict[str, Any],
        files: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Tier 2 校准：基于 123 分享文件树真实文件列表反推精确的 media_type / ep_tag / 正片体积 / 缺失规格，
        返回校准后的资源浅拷贝字典（不修改原对象）。
        """
        updated = dict(resource or {})
        hint_season_val = updated.get("season")
        analysis = cls.analyze_video_files(
            files,
            hint_media_type=str(updated.get("media_type") or ""),
            hint_season=hint_season_val if isinstance(hint_season_val, int) else None,
        )
        if analysis["video_count"] <= 0:
            return updated

        raw_text = str(updated.get("raw_text") or "")
        fs_filename = str(updated.get("fs_filename") or "")
        spec_probe = f"{raw_text}\n{fs_filename}" if fs_filename else raw_text

        tmdb_m = cls.RE_TMDB.search(raw_text)
        is_tmdb_movie = bool(tmdb_m and tmdb_m.group(1).lower() == "movie")

        media_type = str(updated.get("media_type") or "movie")
        season = updated.get("season")
        episode = updated.get("episode")
        ep_start = updated.get("ep_start")
        ep_end = updated.get("ep_end")
        is_ep_range = bool(updated.get("is_ep_range", False))
        ep_tag = str(updated.get("ep_tag") or "")

        # 仅在非 TMDB 权威电影锁定时，采纳文件树识别出的剧集季集信息；
        # 若文本已明确为剧集而分享内仅含 1 个未编号视频文件，也保持其剧集身份不被降级为电影
        if not is_tmdb_movie:
            inferred_type = analysis["inferred_type"]
            file_ep_tag = analysis["ep_tag"]
            if file_ep_tag:
                media_type = "tv"
                season = analysis["season"]
                episode = analysis["episode"]
                ep_start = analysis["ep_start"]
                ep_end = analysis["ep_end"]
                is_ep_range = bool(analysis["is_ep_range"])
                ep_tag = file_ep_tag
            elif media_type == "movie" and inferred_type == "tv":
                media_type = "tv"

        title = str(updated.get("title") or "")
        year = str(updated.get("year") or "")
        tmdb_id = updated.get("tmdb_id")
        display_title = title
        if media_type == "tv" and ep_tag:
            if ep_tag.upper() not in title.upper():
                display_title = f"{title} {ep_tag}"
        elif media_type == "movie":
            season = None
            episode = None
            ep_start = None
            ep_end = None
            is_ep_range = False
            ep_tag = ""

        # 以正片真实视频总体积兜底/校准（排除 nfo/srt 及预告片）
        file_size = int(updated.get("file_size") or 0)
        if analysis["total_size"] > 0:
            file_size = int(analysis["total_size"])

        file_specs = analysis.get("specs") or {}
        resolution = str(updated.get("resolution") or "1080p")
        if file_specs.get("resolution") and not cls.RE_RESOLUTION.search(spec_probe):
            resolution = file_specs["resolution"]

        codec = str(updated.get("codec") or "H.264")
        if file_specs.get("codec") and not cls.RE_CODEC.search(spec_probe):
            codec = file_specs["codec"]

        dovi_hdr = str(updated.get("dovi_hdr") or "")
        if file_specs.get("dovi_hdr") and not dovi_hdr:
            dovi_hdr = file_specs["dovi_hdr"]

        source_val = cls._extract_source(spec_probe) or str(file_specs.get("source") or "")

        media_key = cls._build_media_key(title, year, tmdb_id, media_type, ep_tag)
        quality_score = calculate_quality_score(
            resolution=resolution,
            dovi_hdr=dovi_hdr,
            codec=codec,
            source=source_val,
            file_size_bytes=file_size,
        )

        updated.update(
            {
                "media_key": media_key,
                "display_title": display_title,
                "media_title": display_title,
                "media_type": media_type,
                "season": season,
                "episode": episode,
                "ep_start": ep_start,
                "ep_end": ep_end,
                "is_ep_range": is_ep_range,
                "ep_tag": ep_tag,
                "resolution": resolution,
                "codec": codec,
                "dovi_hdr": dovi_hdr,
                "file_size": file_size,
                "quality_score": quality_score,
                "tree_calibrated": True,
                "video_count": analysis["video_count"],
            }
        )
        return updated

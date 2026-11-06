"""
Pan123TgPilot Telegram 消息智能提取器
负责解析频道文本中的影视名称、分类、季集、画质参数、标准分享链接与秒传码
"""

import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .ranker import calculate_quality_score


class TgMessageParser:
    """Telegram 消息解析引擎"""

    # 正则规则库
    RE_FS_LINK = re.compile(r"(123FSLink(?:V2)?\$[a-fA-F0-9]{32,40}#\d+#[^\r\n]+)")
    RE_SHARE_URL = re.compile(r"https?://(?:www\.)?(?:123pan\.com|123684\.com|123pan\.cn)/s/([a-zA-Z0-9_-]+)(?:\?[^ \r\n\"'\)\]]*)?")
    RE_EXTRACT_PWD = re.compile(r"(?:提取码|密码|访问码|pwd)[：:\s=]*([a-zA-Z0-9]{4,6})", re.IGNORECASE)

    RE_SEASON_EPISODE = re.compile(r"\bS(\d{1,3})(?:E(\d{1,4}))?\b", re.IGNORECASE)
    RE_EPISODE_ONLY = re.compile(r"\b(?:EP|E|第)\s*(\d{1,4})\s*(?:集|话)?\b", re.IGNORECASE)
    RE_SEASON_ONLY = re.compile(r"\b(?:Season|S|第)\s*(\d{1,2})\s*(?:季)?\b", re.IGNORECASE)

    RE_TMDB = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)", re.IGNORECASE)
    RE_TMDB_TAG = re.compile(r"\[tmdbid=(\d+)\]|\{tmdb-(\d+)\}|(?:🆔\s*TMDB|TMDB\s*ID)[：:\s]*(\d+)", re.IGNORECASE)

    RE_RESOLUTION = re.compile(r"\b(2160p|4k|1080p|1080i|720p|480p)\b", re.IGNORECASE)
    RE_CODEC = re.compile(r"\b(H\.?265|HEVC|H\.?264|AVC|AV1|x265|x264)\b", re.IGNORECASE)
    RE_DOVI_HDR = re.compile(r"\b(DoVi|Dolby\s*Vision|HDR10\+|HDR10|HDR|HLG)\b", re.IGNORECASE)
    RE_SIZE = re.compile(r"(?:体积|大小|Size|💾|📦)[：:\s]*([\d.]+)\s*(GB|MB|TB|G|M)", re.IGNORECASE)

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

        # 4. 识别影视分类 (movie vs tv) 与季集标识
        media_type, season, episode, ep_tag = cls._determine_media_type(text, fs_filename)

        # 5. 提取画质参数与体积
        resolution, codec, dovi_hdr, file_size_bytes = cls._extract_quality_params(text, fs_size)

        # 6. 计算 TMDB ID 与唯一媒体洗版锚点 (media_key)
        tmdb_id = cls._extract_tmdb_id(text)
        media_key = cls._build_media_key(title, year, tmdb_id, media_type, ep_tag)

        # 7. 计算综合质量得分
        score = calculate_quality_score(
            resolution=resolution,
            dovi_hdr=dovi_hdr,
            codec=codec,
            source="WEB-DL" if "WEB" in text.upper() else "",
            file_size_bytes=file_size_bytes,
        )

        return {
            "channel": channel,
            "msg_id": msg_id,
            "media_key": media_key,
            "title": title,
            "media_title": title,
            "year": year,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "season": season,
            "episode": episode,
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
        """提取影视主标题与上映年份"""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        candidate_title = ""
        year = ""

        # 1. 优先扫描首行或带电影/剧集图标的行
        for line in lines[:5]:
            # 清理常见 emoji 与前缀
            cleaned = re.sub(r"^[🎬📺🎥🏷️📌\s#]+", "", line).strip()
            # 匹配 标题 (2026) 格式
            m_year = re.search(r"^(.*?)(?:\s*[（\(](\d{4})[）\)]|\s*\[(\d{4})\])", cleaned)
            if m_year:
                candidate_title = m_year.group(1).strip()
                year = m_year.group(2) or m_year.group(3) or ""
                break
            elif not candidate_title and not any(k in cleaned for k in ("类型", "标签", "评分", "简介", "链接", "版本")):
                candidate_title = cleaned

        if not candidate_title and fallback_filename:
            # 从秒传文件名逆向提取
            m_fn = re.search(r"^(.*?)(?:\s*[（\(](\d{4})[）\)]|\s*\[(\d{4})\])", fallback_filename)
            if m_fn:
                candidate_title = m_fn.group(1).strip()
                year = m_fn.group(2) or m_fn.group(3) or ""
            else:
                candidate_title = re.sub(r"-\s*\d{3,4}p.*", "", fallback_filename).strip()

        # 进一步剔除中英文标点残留
        candidate_title = re.sub(r"^[🎬📺🎥🏷️\s]+", "", candidate_title).strip()
        return candidate_title or "未知影视", year

    @classmethod
    def _determine_media_type(cls, text: str, filename: str = "") -> Tuple[str, Optional[int], Optional[int], str]:
        """
        判断是电影 (movie) 还是电视剧/剧集 (tv)
        :return: (media_type: 'movie'|'tv', season: int|None, episode: int|None, ep_tag: str)
        """
        combined = f"{text}\n{filename}"
        season: Optional[int] = None
        episode: Optional[int] = None
        ep_tag = ""

        # 1. 季集正则探测 (S05E217 / S01 / E10)
        m_se = cls.RE_SEASON_EPISODE.search(combined)
        if m_se:
            season = int(m_se.group(1))
            episode = int(m_se.group(2)) if m_se.group(2) else None
            ep_tag = f"S{season:02d}E{episode:02d}" if episode is not None else f"S{season:02d}"
            return "tv", season, episode, ep_tag

        m_ep = cls.RE_EPISODE_ONLY.search(combined)
        if m_ep:
            episode = int(m_ep.group(1))
            season = 1
            ep_tag = f"S01E{episode:02d}"
            return "tv", season, episode, ep_tag

        m_s = cls.RE_SEASON_ONLY.search(combined)
        if m_s:
            season = int(m_s.group(1))
            ep_tag = f"S{season:02d}"
            return "tv", season, None, ep_tag

        # 2. 检查 TMDB 链接特征 (权威判定)
        tmdb_m = cls.RE_TMDB.search(combined)
        if tmdb_m:
            t = tmdb_m.group(1).lower()
            if t == "tv":
                return "tv", 1, None, "S01"
            elif t == "movie":
                return "movie", None, None, ""

        # 3. 文本类型关键字匹配
        if re.search(r"类型[：:\s]*(?:剧集|电视剧|美剧|日剧|韩剧|国剧|泰剧|连续剧)", combined):
            return "tv", 1, None, "S01"

        if re.search(r"类型[：:\s]*(?:电影|网络电影|动画电影|纪录片电影)", combined):
            return "movie", None, None, ""

        if re.search(r"类型[：:\s]*(?:动漫|国漫)", combined):
            return "movie", None, None, ""

        # 默认归为电影
        return "movie", None, None, ""

    @classmethod
    def _extract_quality_params(cls, text: str, fallback_size: int = 0) -> Tuple[str, str, str, int]:
        """提取标准化分辨率、编码、HDR 和文件体积"""
        res_m = cls.RE_RESOLUTION.search(text)
        res_raw = res_m.group(1).lower() if res_m else "1080p"
        if res_raw in ("4k", "2160p", "uhd"):
            resolution = "2160p"
        elif res_raw in ("1080p", "1080i", "fhd"):
            resolution = "1080p"
        elif res_raw in ("720p", "hd"):
            resolution = "720p"
        elif res_raw in ("480p", "sd", "576p"):
            resolution = "480p"
        else:
            resolution = res_raw

        codec_m = cls.RE_CODEC.search(text)
        codec_raw = codec_m.group(1).upper() if codec_m else "H.264"
        if "265" in codec_raw or "HEVC" in codec_raw:
            codec = "H.265"
        elif "264" in codec_raw or "AVC" in codec_raw:
            codec = "H.264"
        elif "AV1" in codec_raw:
            codec = "AV1"
        else:
            codec = codec_raw

        dovi_m = cls.RE_DOVI_HDR.search(text)
        dovi_raw = dovi_m.group(1).upper() if dovi_m else ""
        if "DOVI" in dovi_raw or "VISION" in dovi_raw:
            dovi_hdr = "DoVi"
        elif "HDR10+" in dovi_raw:
            dovi_hdr = "HDR10+"
        elif "HDR" in dovi_raw:
            dovi_hdr = "HDR"
        elif "HLG" in dovi_raw:
            dovi_hdr = "HLG"
        else:
            dovi_hdr = dovi_raw

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

"""
Pan123TgPilot 智能洗版评估引擎
负责计算影视资源综合质量得分，判断是否满足洗版升级条件
"""

from typing import Optional, Tuple


def calculate_quality_score(
    resolution: str = "",
    dovi_hdr: str = "",
    codec: str = "",
    source: str = "",
    file_size_bytes: int = 0,
) -> int:
    """
    根据分辨率、动态范围、编码格式及体积计算资源综合质量得分 (Quality Score)
    """
    score = 0
    res_lower = (resolution or "").lower()
    hdr_lower = (dovi_hdr or "").lower()
    codec_lower = (codec or "").lower()
    src_lower = (source or "").lower()

    # 1. 分辨率得分 (基础分)
    if any(k in res_lower for k in ("2160p", "4k", "uhd")):
        score += 100
    elif any(k in res_lower for k in ("1080p", "fhd", "1080i")):
        score += 60
    elif any(k in res_lower for k in ("720p", "hd")):
        score += 30
    elif any(k in res_lower for k in ("480p", "sd", "576p")):
        score += 10
    else:
        score += 20  # 未标注分辨率兜底

    # 2. 动态范围与杜比视界
    if any(k in hdr_lower for k in ("dovi", "dolby vision", "dv", "杜比视界")):
        score += 25
    elif any(k in hdr_lower for k in ("hdr10+", "hdr10", "hdr", "hlg")):
        score += 15

    # 3. 编码格式加分
    if any(k in codec_lower for k in ("h.265", "h265", "hevc", "av1")):
        score += 15
    elif any(k in codec_lower for k in ("h.264", "h264", "avc", "x264")):
        score += 5

    # 4. 来源与压制品质
    if any(k in src_lower for k in ("remux", "bluray", "blu-ray", "原盘")):
        score += 20
    elif any(k in src_lower for k in ("web-dl", "webrip", "web")):
        score += 10
    elif any(k in src_lower for k in ("hdtv", "tvrip")):
        score += 5
    elif any(k in src_lower for k in ("cam", "ts", "tc", "枪版")):
        score -= 50

    # 5. 体积加权 (每 10GB +1分，上限 5 分)
    if file_size_bytes > 0:
        gb = file_size_bytes / (1024.0 ** 3)
        score += min(5, int(gb // 10))

    return score


def evaluate_upgrade(old_record: Optional[dict], new_resource: dict) -> Tuple[bool, str]:
    """
    对比旧记录与新抓取资源，判断是否触发智能洗版
    :return: (is_upgrade: 是否应该洗版转存, reason: 决策说明)
    """
    if not old_record:
        return True, "首发资源，直接转存"

    old_score = old_record.get("quality_score", 0)
    new_score = new_resource.get("quality_score", 0)

    # 如果新版本得分更高
    if new_score > old_score:
        old_desc = f"{old_record.get('resolution') or '标清'} {old_record.get('codec') or ''} {old_record.get('dovi_hdr') or ''}".strip()
        new_desc = f"{new_resource.get('resolution') or '未知'} {new_resource.get('codec') or ''} {new_resource.get('dovi_hdr') or ''}".strip()
        return (
            True,
            f"触发洗版升级: [{old_desc}] (得分 {old_score}) -> [{new_desc}] (得分 {new_score})",
        )
    else:
        return (
            False,
            f"已存在同质或更优资源 (当前得分 {old_score} >= 新版本得分 {new_score})，跳过转存",
        )

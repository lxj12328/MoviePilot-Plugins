"""
qBittorrent 联动客户端封装
负责种子状态查询、标签打标与流转、做种指标校验与安全删除
"""

import http.cookiejar
import json
from typing import Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request


class QBHelper:
    """qBittorrent WebUI API 交互帮助类"""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.host = host.rstrip("/") if host else ""
        self.port = port
        self.username = username or ""
        self.password = password or ""
        self._cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._cookie_jar))
        self._logged_in = False

    def is_configured(self) -> bool:
        """检查是否有基本连接配置"""
        return bool(self.host)

    @property
    def base_url(self) -> str:
        if self.port and not (self.host.endswith(f":{self.port}")):
            return f"{self.host}:{self.port}"
        return self.host

    def login(self) -> bool:
        """登录 qBittorrent 获取会话凭据"""
        if not self.is_configured():
            return False

        url = f"{self.base_url}/api/v2/auth/login"
        data = urllib.parse.urlencode({
            "username": self.username,
            "password": self.password,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with self._opener.open(req, timeout=10) as resp:
                text = resp.read().decode("utf-8")
                self._logged_in = "Ok." in text or resp.status == 200
                return self._logged_in
        except Exception:
            self._logged_in = False
            return False

    def _request(self, path: str, method: str = "GET", data: Optional[dict] = None) -> Tuple[bool, str]:
        """发起鉴权请求并在会话失效时自动重试登录"""
        if not self._logged_in and not self.login():
            return False, "QB 未登录且重新登录失败"

        url = f"{self.base_url}{path}"
        body = urllib.parse.urlencode(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, method=method)

        try:
            with self._opener.open(req, timeout=10) as resp:
                return True, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # 重新登录再尝试一次
                if self.login():
                    try:
                        with self._opener.open(req, timeout=10) as resp2:
                            return True, resp2.read().decode("utf-8")
                    except Exception as e2:
                        return False, str(e2)
            return False, str(e)
        except Exception as e:
            return False, str(e)

    def get_torrent(self, torrent_hash: str) -> Optional[dict]:
        """获取单个种子的详细信息"""
        if not torrent_hash:
            return None
        ok, res = self._request(f"/api/v2/torrents/info?hashes={torrent_hash}")
        if not ok:
            return None
        try:
            torrents = json.loads(res)
            return torrents[0] if torrents else None
        except Exception:
            return None

    def match_torrent_by_path(self, target_or_source_path: str) -> Optional[str]:
        """通过文件或目录路径逆向在 QB 中搜寻对应种子 Hash"""
        ok, res = self._request("/api/v2/torrents/info")
        if not ok:
            return None
        try:
            torrents = json.loads(res)
            for t in torrents:
                content_path = t.get("content_path") or ""
                name = t.get("name") or ""
                if (content_path and (content_path in target_or_source_path or target_or_source_path in content_path)) or (
                    name and name in target_or_source_path
                ):
                    return t.get("hash")
        except Exception:
            pass
        return None

    def update_tags(self, torrent_hash: str, new_tag: str, all_plugin_tags: list[str]) -> bool:
        """
        流转种子标签：移除旧插件标签，并打上新的状态标签
        """
        if not torrent_hash or not new_tag:
            return False

        torrent = self.get_torrent(torrent_hash)
        if not torrent:
            return False

        existing_tags = [t.strip() for t in (torrent.get("tags") or "").split(",") if t.strip()]

        # 1. 过滤需要移除的历史插件标签
        tags_to_remove = [t for t in existing_tags if t in all_plugin_tags and t != new_tag]
        if tags_to_remove:
            self._request(
                "/api/v2/torrents/removeTags",
                method="POST",
                data={"hashes": torrent_hash, "tags": ",".join(tags_to_remove)},
            )

        # 2. 如果新标签尚不存在，则打上新标签
        if new_tag not in existing_tags:
            self._request(
                "/api/v2/torrents/addTags",
                method="POST",
                data={"hashes": torrent_hash, "tags": new_tag},
            )

        return True

    def check_seeding_criteria(
        self,
        torrent_hash: str,
        min_seed_time_hours: float = 48.0,
        min_ratio: float = 2.0,
    ) -> Tuple[bool, str]:
        """
        检查种子是否满足做种保留期与分享率指标
        :return: (is_qualified: 是否达标, reason: 详情描述)
        """
        torrent = self.get_torrent(torrent_hash)
        if not torrent:
            # 种子如果在 QB 中已被手动删除，允许通过清理判定
            return True, "种子不存在或已被移出 QB"

        seeding_time_sec = torrent.get("seeding_time") or 0
        seeding_hours = seeding_time_sec / 3600.0
        ratio = float(torrent.get("ratio") or 0.0)

        # 判定条件：做种时长或分享率其一达标（或根据策略兼顾）
        time_qualified = seeding_hours >= min_seed_time_hours
        ratio_qualified = ratio >= min_ratio

        if time_qualified or ratio_qualified:
            return (
                True,
                f"做种已达标 (做种时长: {seeding_hours:.1f}h / 目标: {min_seed_time_hours}h, 分享率: {ratio:.2f} / 目标: {min_ratio})",
            )

        return (
            False,
            f"未达做种目标 (做种时长: {seeding_hours:.1f}h / 目标: {min_seed_time_hours}h, 分享率: {ratio:.2f} / 目标: {min_ratio})",
        )

    def delete_torrent(self, torrent_hash: str, delete_files: bool = True) -> bool:
        """从 qBittorrent 中删除种子（以及可选的源下载文件）"""
        if not torrent_hash:
            return False
        ok, _ = self._request(
            "/api/v2/torrents/delete",
            method="POST",
            data={"hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false"},
        )
        return ok

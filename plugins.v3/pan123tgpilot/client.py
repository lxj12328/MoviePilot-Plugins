"""
Pan123TgPilot 123云盘 Web 端转存与秒传客户端
实现标准分享链接转存、秒传码秒传、文件移动与旧版本清理
"""

import json
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


class Pan123TgClient:
    """123云盘 Web 端转存客户端"""

    WEB_BASE_URL = "https://www.123684.com"
    BACKUP_BASE_URL = "https://yun.123pan.com"

    def __init__(self, token: str = ""):
        self.token = self._clean_token(token)
        self.user_info: dict = {}
        self._last_token_check = 0.0

    @staticmethod
    def _clean_token(token: Optional[str]) -> str:
        """剥离双引号、单引号及 Bearer 前缀"""
        if not token:
            return ""
        tk = str(token).strip().strip("'").strip('"').strip()
        if tk.lower().startswith("bearer "):
            tk = tk[7:].strip()
        return tk

    def is_configured(self) -> bool:
        return bool(self.token)

    def _get_headers(self, with_auth: bool = True) -> dict:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "platform": "web",
            "App-Version": "3",
            "Origin": self.WEB_BASE_URL,
            "Referer": f"{self.WEB_BASE_URL}/",
        }
        if with_auth and self.token:
            headers["authorization"] = f"Bearer {self.token}"
            headers["authorToken"] = self.token
            headers["token"] = self.token
        return headers

    def _http_request(
        self,
        url: str,
        method: str = "GET",
        payload: Optional[dict] = None,
        with_auth: bool = True,
        timeout: int = 25,
        max_retries: int = 3,
    ) -> Tuple[bool, dict, str]:
        """具备 SSL 容错与重试机制的 HTTP 传输方法"""
        headers = self._get_headers(with_auth=with_auth)
        data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        last_err = ""
        for attempt in range(max_retries):
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    resp_data = resp.read().decode("utf-8")
                    try:
                        res_json = json.loads(resp_data)
                        return True, res_json, "ok"
                    except Exception:
                        return True, {"code": 0, "raw": resp_data}, "ok"
            except urllib.error.HTTPError as e:
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8")
                    res_json = json.loads(err_text)
                    return False, res_json, f"HTTP {e.code}: {res_json.get('message', e.reason)}"
                except Exception:
                    last_err = f"HTTP {e.code}: {e.reason} ({err_text})"
                if e.code in (401, 403):
                    break
            except Exception as ex:
                last_err = str(ex)
            time.sleep(0.5)

        return False, {}, last_err

    def check_token_validity(self) -> bool:
        """测试 Token 有效性"""
        if not self.is_configured():
            return False
        url = f"{self.WEB_BASE_URL}/b/api/user/info"
        ok, res, _ = self._http_request(url, method="GET", with_auth=True)
        if ok and res.get("code") == 0:
            self.user_info = res.get("data") or {}
            return True
        return False

    def save_share_link(
        self,
        share_key: str,
        share_pwd: str = "",
        target_parent_id: int = 0,
    ) -> Tuple[bool, Optional[str], str]:
        """
        转存标准 123 分享链接至指定云盘目录
        :return: (ok: 是否成功, file_id: 云盘文件ID, msg: 说明)
        """
        if not self.is_configured():
            return False, None, "123 云盘 Web Token 未配置"

        hosts = [self.WEB_BASE_URL, self.BACKUP_BASE_URL]
        last_msg = ""

        for host in hosts:
            # 1. 查询分享提取文件列表
            query_params = urllib.parse.urlencode({
                "limit": 100,
                "next": 0,
                "orderBy": "file_name",
                "orderDirection": "asc",
                "shareKey": share_key,
                "SharePwd": share_pwd,
                "parentFileId": 0,
                "Page": 1,
            })
            get_url = f"{host}/b/api/share/get?{query_params}"
            ok_get, res_get, msg_get = self._http_request(get_url, method="GET", with_auth=True)
            if not ok_get or res_get.get("code") != 0:
                last_msg = res_get.get("message") if ok_get else msg_get
                continue

            data_get = res_get.get("data") or {}
            items = data_get.get("InfoList") or data_get.get("fileList") or []
            file_id_list = []
            for item in items:
                fid = item.get("FileId") or item.get("fileId")
                if fid:
                    file_id_list.append(int(fid))

            # 2. 调用转存接口
            save_payload = {
                "shareKey": share_key,
                "sharePwd": share_pwd,
                "parentFileId": int(target_parent_id or 0),
                "fileIdList": file_id_list,
                "duplicate": 2,  # 覆盖/重用
            }
            save_url = f"{host}/b/api/share/save"
            ok_save, res_save, msg_save = self._http_request(
                save_url, method="POST", payload=save_payload, with_auth=True
            )
            if ok_save and res_save.get("code") == 0:
                data_s = res_save.get("data") or {}
                fid = data_s.get("fileId") or data_s.get("FileId") or str(file_id_list[0] if file_id_list else "")
                return True, str(fid), "分享转存成功"

            last_msg = res_save.get("message") if ok_save else msg_save

        return False, None, f"分享转存失败: {last_msg}"

    def save_fslink(
        self,
        filename: str = "",
        etag: str = "",
        size: int = 0,
        target_parent_id: int = 0,
        fslink: str = "",
        parent_file_id: int = 0,
    ) -> Tuple[bool, Optional[str], str]:
        """
        解析 123 秒传码 (123FSLink) 并零流量直接秒传至指定目录
        """
        if not self.is_configured():
            return False, None, "123 云盘 Web Token 未配置"

        target_id = int(parent_file_id or target_parent_id or 0)

        # 若直接传入了秒传码字符串，自动解构
        if fslink and (not etag or not size or not filename):
            try:
                # 123FSLinkV2$<md5>#<size>#<filename>
                parts = fslink.split("$", 1)[1].split("#")
                if len(parts) >= 3:
                    etag = etag or parts[0].strip().lower()
                    size = size or int(parts[1].strip())
                    filename = filename or parts[2].strip()
            except Exception:
                pass

        payload = {
            "parentFileId": target_id,
            "filename": filename,
            "fileName": filename,
            "etag": (etag or "").lower(),
            "size": int(size or 0),
            "type": 0,
            "duplicate": 2,
        }

        urls = [
            f"{self.WEB_BASE_URL}/b/api/file/upload_request",
            f"{self.BACKUP_BASE_URL}/b/api/file/upload_request",
        ]

        last_msg = ""
        for url in urls:
            ok, res, msg = self._http_request(url, method="POST", payload=payload, with_auth=True)
            if ok and res.get("code") == 0:
                data = res.get("data") or {}
                reuse = bool(data.get("reuse") or data.get("Reuse"))
                info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
                file_id = (
                    data.get("fileId")
                    or data.get("FileId")
                    or info.get("FileId")
                    or info.get("fileId")
                )
                if reuse or (file_id and not data.get("preuploadID")):
                    return True, str(file_id or 0), "秒传转存成功"
                else:
                    return False, None, "云端未命中秒传特征"
            last_msg = res.get("message") if ok else msg

        return False, None, f"秒传转存失败: {last_msg}"

    def trash_files(self, file_ids: List[int]) -> Tuple[bool, str]:
        """
        洗版时将旧版本文件移至回收站
        """
        if not file_ids or not self.is_configured():
            return False, "无有效文件 ID"

        urls = [
            f"{self.WEB_BASE_URL}/b/api/file/trash",
            f"{self.BACKUP_BASE_URL}/b/api/file/trash",
        ]
        payload = {"fileIdList": file_ids}

        for url in urls:
            ok, res, msg = self._http_request(url, method="POST", payload=payload, with_auth=True)
            if ok and res.get("code") == 0:
                return True, "已成功移至回收站"

        return False, "移至回收站失败"

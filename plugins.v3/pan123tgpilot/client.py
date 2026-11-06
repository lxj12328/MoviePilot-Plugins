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

    def mkdir(self, dirname: str, parent_file_id: int = 0) -> Tuple[bool, int, str]:
        """在指定目录下创建新文件夹 (若同名则复用现有文件夹)"""
        clean_name = (dirname or "").strip()
        if not clean_name:
            return False, 0, "文件夹名称为空"

        parent_id = int(parent_file_id or 0)
        cache_key = (parent_id, clean_name)
        if hasattr(self, "_dir_cache") and cache_key in self._dir_cache:
            return True, self._dir_cache[cache_key], "命中缓存"

        payload = {
            "driveId": 0,
            "parentFileId": parent_id,
            "filename": clean_name,
            "fileName": clean_name,
            "type": 1,  # 1 代表文件夹
            "size": 0,
            "etag": "",
            "duplicate": 2,  # 2: 若同名则重用/覆盖
            "NotReuse": True,
            "event": "newCreateFolder",
            "operateType": 1,
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
                info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
                dir_id = (
                    info.get("FileId")
                    or info.get("fileId")
                    or data.get("fileId")
                    or data.get("FileId")
                    or data.get("id")
                )
                if dir_id:
                    did = int(dir_id)
                    if not hasattr(self, "_dir_cache"):
                        self._dir_cache = {}
                    self._dir_cache[cache_key] = did
                    return True, did, "创建/获取文件夹成功"
            last_msg = res.get("message") if ok else msg

        return False, 0, f"创建文件夹失败: {last_msg}"

    def save_share_link(
        self,
        share_key: str,
        share_pwd: str = "",
        target_parent_id: int = 0,
        folder_name: str = "",
    ) -> Tuple[bool, Optional[str], str]:
        """
        转存标准 123 分享链接中的全量文件夹与所有文件 (包含视频、字幕、nfo、图片等刮削信息)
        :return: (ok: 是否成功, file_id: 云盘文件ID, msg: 说明)
        """
        if not self.is_configured():
            return False, None, "123 云盘 Web Token 未配置"

        hosts = [self.WEB_BASE_URL, self.BACKUP_BASE_URL]
        last_msg = ""

        for host in hosts:
            # 1. 多页遍历查询分享提取全量文件与文件夹列表 (完整抓取视频、字幕、nfo元数据、海报)
            file_id_list = []
            items = []
            next_cursor = 0

            while True:
                query_params = urllib.parse.urlencode({
                    "limit": 100,
                    "next": next_cursor,
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
                    break

                data_get = res_get.get("data") or {}
                batch_items = data_get.get("InfoList") or data_get.get("fileList") or []
                if not batch_items:
                    break

                for it in batch_items:
                    fid = it.get("FileId") or it.get("fileId")
                    if fid and int(fid) not in file_id_list:
                        file_id_list.append(int(fid))
                        items.append(it)

                next_val = data_get.get("next")
                if not next_val or int(next_val) == 0 or len(batch_items) < 100:
                    break
                next_cursor = int(next_val)

            if not file_id_list:
                continue

            # 判断是否需要归集至影视同名子文件夹：
            # 若分享根目录本身就是一个文件夹 (Type == 1)，直接转存至 target_parent_id 即可完整保留该文件夹；
            # 若分享根目录下直接平铺了多个文件 (如 .mkv + .nfo + .srt + .jpg)，则自动在目标目录下创建影视同名文件夹并转存入内
            final_target_id = int(target_parent_id or 0)
            has_single_root_folder = (
                len(items) == 1 and int(items[0].get("type") or items[0].get("Type") or 0) == 1
            )
            if not has_single_root_folder and folder_name:
                ok_mk, dir_id, msg_mk = self.mkdir(dirname=folder_name, parent_file_id=final_target_id)
                if ok_mk and dir_id:
                    final_target_id = dir_id
                elif "文件夹不存在" in str(msg_mk) and final_target_id != 0:
                    logger.warning(
                        f"Pan123TgPilot 目标目录ID [{final_target_id}] 在云盘中不存在 (可能已被删除)，"
                        f"自动降级至根目录 (0) 下创建影视文件夹【{folder_name}】并转存..."
                    )
                    ok_retry, dir_retry, _ = self.mkdir(dirname=folder_name, parent_file_id=0)
                    if ok_retry and dir_retry:
                        final_target_id = dir_retry
                    else:
                        final_target_id = 0

            # 2. 调用批量转存接口，全量转存所有文件夹与伴生文件
            save_payload = {
                "shareKey": share_key,
                "sharePwd": share_pwd,
                "parentFileId": final_target_id,
                "fileIdList": file_id_list,
                "duplicate": 2,  # 覆盖/重用
            }
            save_url = f"{host}/b/api/share/save"
            ok_save, res_save, msg_save = self._http_request(
                save_url, method="POST", payload=save_payload, with_auth=True
            )
            if ok_save and res_save.get("code") == 0:
                data_s = res_save.get("data") or {}
                fid = data_s.get("fileId") or data_s.get("FileId") or str(final_target_id or file_id_list[0])
                return True, str(fid), f"分享转存成功 (共包含 {len(file_id_list)} 个文件/文件夹)"
            elif ok_save and "文件夹不存在" in str(res_save.get("message")) and final_target_id != 0:
                logger.warning(
                    f"Pan123TgPilot 目录ID [{final_target_id}] 不存在，自动降级至根目录 (0) 重试分享转存..."
                )
                save_payload["parentFileId"] = 0
                ok_fb, res_fb, msg_fb = self._http_request(
                    save_url, method="POST", payload=save_payload, with_auth=True
                )
                if ok_fb and res_fb.get("code") == 0:
                    data_s = res_fb.get("data") or {}
                    fid = data_s.get("fileId") or data_s.get("FileId") or str(file_id_list[0])
                    return True, str(fid), f"分享转存成功 (已降级至根目录，共 {len(file_id_list)} 个文件/文件夹)"

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
        folder_name: str = "",
    ) -> Tuple[bool, Optional[str], str]:
        """
        解析 123 秒传码 (123FSLink) 并零流量直接秒传至指定目录 (支持自动归集至同名文件夹)
        """
        if not self.is_configured():
            return False, None, "123 云盘 Web Token 未配置"

        target_id = int(parent_file_id or target_parent_id or 0)

        # 若提供了影视文件夹名称，自动创建或复用该子文件夹
        if folder_name:
            ok_mk, dir_id, msg_mk = self.mkdir(dirname=folder_name, parent_file_id=target_id)
            if ok_mk and dir_id:
                target_id = dir_id
            elif "文件夹不存在" in str(msg_mk) and target_id != 0:
                logger.warning(
                    f"Pan123TgPilot 检测到配置的目标目录ID [{target_id}] 在 123 云盘中不存在 (可能已被删除或移入回收站)，"
                    f"自动降级至根目录 (0) 下创建同名影视文件夹【{folder_name}】并重试秒传..."
                )
                ok_retry, dir_retry, _ = self.mkdir(dirname=folder_name, parent_file_id=0)
                if ok_retry and dir_retry:
                    target_id = dir_retry
                else:
                    target_id = 0

        # 若直接传入了秒传码字符串，自动解构
        if fslink and (not etag or not size or not filename):
            try:
                # 123FSLinkV2$<md5_or_sha1>#<size>#<filename>
                clean_link = fslink.rstrip("。，,.）)]】\"' \t\r\n")
                parts = clean_link.split("$", 1)[1].split("#", 2)
                if len(parts) >= 3:
                    etag = etag or parts[0].strip().lower()
                    size = size or int(parts[1].strip())
                    fn = parts[2].strip().rstrip("。，,.）)]】\"' \t\r\n")
                    if "." not in fn or fn.endswith("."):
                        fn = fn.rstrip(".") + ".mkv"
                    filename = filename or fn
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
            else:
                resp_msg = res.get("message") if ok else msg
                if "文件夹不存在" in str(resp_msg) and payload.get("parentFileId") != 0:
                    logger.warning(
                        f"Pan123TgPilot 目标目录ID [{payload.get('parentFileId')}] 在云端不存在，正在降级至根目录 (0) 重试秒传..."
                    )
                    payload["parentFileId"] = 0
                    ok_fb, res_fb, msg_fb = self._http_request(url, method="POST", payload=payload, with_auth=True)
                    if ok_fb and res_fb.get("code") == 0:
                        data = res_fb.get("data") or {}
                        reuse = bool(data.get("reuse") or data.get("Reuse"))
                        info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
                        file_id = (
                            data.get("fileId")
                            or data.get("FileId")
                            or info.get("FileId")
                            or info.get("fileId")
                        )
                        if reuse or (file_id and not data.get("preuploadID")):
                            return True, str(file_id or 0), "秒传转存成功 (已降级至根目录)"
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

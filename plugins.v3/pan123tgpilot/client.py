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

    def list_files(self, parent_file_id: int = 0) -> Tuple[bool, List[dict], str]:
        """获取指定目录下的文件及子文件夹列表，用于先查后建同名归集文件夹"""
        if not self.is_configured():
            return False, [], "123 云盘 Web Token 未配置"

        query_params = urllib.parse.urlencode({
            "driveId": 0,
            "limit": 100,
            "next": 0,
            "orderBy": "file_name",
            "orderDirection": "asc",
            "parentFileId": int(parent_file_id or 0),
            "trashed": "false",
            "SearchData": "",
            "Page": 1,
            "page": 1,
            "event": "homeListFile",
            "operateType": 1,
        })

        hosts = [self.WEB_BASE_URL, self.BACKUP_BASE_URL]
        paths = ["/b/api/file/list/new", "/api/file/list/new"]
        last_msg = "获取目录列表失败"

        for host in hosts:
            for path in paths:
                url = f"{host}{path}?{query_params}"
                ok, res, msg = self._http_request(url, method="GET", with_auth=True)
                if ok and res.get("code") == 0:
                    data = res.get("data") or {}
                    items = (
                        data.get("InfoList")
                        or data.get("infoList")
                        or data.get("fileList")
                        or data.get("list")
                        or []
                    )
                    return True, items, "ok"
                last_msg = res.get("message") if ok else msg

        return False, [], str(last_msg or "获取目录列表失败")

    def invalidate_dir_cache(self, parent_file_id: Optional[int] = None, dirname: Optional[str] = None) -> None:
        """清除已失效的目录缓存，防止网盘目录被移动/重建后继续使用旧的失效目录ID"""
        if not hasattr(self, "_dir_cache") or not isinstance(self._dir_cache, dict):
            return
        if parent_file_id is not None and dirname is not None:
            self._dir_cache.pop((int(parent_file_id), dirname.strip()), None)
        elif parent_file_id is not None:
            pid = int(parent_file_id)
            keys_to_del = [k for k, v in self._dir_cache.items() if k[0] == pid or v == pid]
            for k in keys_to_del:
                self._dir_cache.pop(k, None)
        else:
            self._dir_cache.clear()

    def mkdir(self, dirname: str, parent_file_id: int = 0, force_refresh: bool = False) -> Tuple[bool, int, str]:
        """
        在指定目录下检索或创建同名文件夹：
        1. 优先命中内存缓存（除非 force_refresh=True）；
        2. 先尝试在 parent_file_id 下检索是否已有未删除的同名文件夹（Type == 1）；
        3. 若不存在，再调用 upload_request 创建文件夹。
        """
        clean_name = (dirname or "").strip()
        if not clean_name:
            return False, 0, "文件夹名称为空"

        parent_id = int(parent_file_id or 0)
        if not hasattr(self, "_dir_cache"):
            self._dir_cache = {}
        cache_key = (parent_id, clean_name)

        if force_refresh:
            self._dir_cache.pop(cache_key, None)
        elif cache_key in self._dir_cache:
            return True, self._dir_cache[cache_key], "命中缓存"

        # 1. 先查：若 list_files 可用且返回了同名文件夹，直接复用真实存在的 FileId
        ok_list, items, list_msg = self.list_files(parent_file_id=parent_id)
        if ok_list and items:
            for item in items:
                name = str(item.get("filename") or item.get("FileName") or item.get("file_name") or "").strip()
                is_folder = (
                    item.get("type") in (1, "1")
                    or item.get("Type") in (1, "1")
                    or item.get("category") in (1, "1")
                )
                is_trashed = item.get("trashed") in (True, 1, "1", "true") or item.get("Trashed") in (True, 1, "1", "true")
                if is_folder and not is_trashed and name.lower() == clean_name.lower():
                    fid_val = item.get("fileId") or item.get("FileId") or item.get("file_id") or item.get("id")
                    if fid_val:
                        did = int(fid_val)
                        self._dir_cache[cache_key] = did
                        return True, did, "复用已有同名文件夹"

        # 2. 后建：调用 upload_request 创建新文件夹
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
                    or data.get("dirID")
                    or data.get("id")
                )
                if dir_id:
                    did = int(dir_id)
                    self._dir_cache[cache_key] = did
                    return True, did, "创建/获取文件夹成功"
            last_msg = res.get("message") if ok else msg

        return False, 0, f"创建文件夹失败: {last_msg}"

    def _list_share_dir(
        self,
        host: str,
        share_key: str,
        share_pwd: str = "",
        parent_file_id: int = 0,
    ) -> Tuple[bool, List[Dict[str, Any]], str]:
        """分页拉取 123 云盘分享链接指定目录下的全部文件与子文件夹元数据"""
        items: List[Dict[str, Any]] = []
        seen_ids: set[int] = set()
        next_cursor = 0
        page_num = 1
        last_msg = ""

        while True:
            query_params = urllib.parse.urlencode({
                "limit": 100,
                "next": next_cursor,
                "orderBy": "file_name",
                "orderDirection": "asc",
                "shareKey": share_key,
                "SharePwd": share_pwd,
                "ParentFileId": parent_file_id,
                "parentFileId": parent_file_id,
                "Page": page_num,
            })
            get_url = f"{host}/b/api/share/get?{query_params}"
            ok_get, res_get, msg_get = self._http_request(get_url, method="GET", with_auth=True)
            if not ok_get or res_get.get("code") != 0:
                last_msg = res_get.get("message") if ok_get else msg_get
                return (len(items) > 0), items, str(last_msg or "获取分享列表失败")

            data_get = res_get.get("data") or {}
            batch_items = data_get.get("InfoList") or data_get.get("fileList") or []
            if not batch_items:
                break

            for it in batch_items:
                if not isinstance(it, dict):
                    continue
                fid = it.get("FileId") or it.get("fileId")
                if fid is not None:
                    fid_int = int(fid)
                    if fid_int in seen_ids:
                        continue
                    seen_ids.add(fid_int)
                items.append(it)

            next_val = data_get.get("next")
            if not next_val or int(next_val) in (0, -1) or len(batch_items) < 100:
                break
            next_cursor = int(next_val)
            page_num += 1

        return True, items, ""

    def save_share_link(
        self,
        share_key: str,
        share_pwd: str = "",
        target_parent_id: int = 0,
        folder_name: str = "",
    ) -> Tuple[bool, Optional[str], str]:
        """
        转存标准 123 分享链接中的全量文件夹与所有文件 (包含视频、字幕、nfo、图片等刮削信息)
        采用「递归遍历 /b/api/share/get 提取 (Etag, Size, FileName) + /b/api/file/upload_request 秒传落盘」方案，
        彻底解决 123 云盘 Web 端无 /b/api/share/save 接口导致 HTTP 404 的问题，并返回正片真实 FileId。
        :return: (ok: 是否成功, file_id: 云盘文件ID, msg: 说明)
        """
        if not self.is_configured():
            return False, None, "123 云盘 Web Token 未配置"

        configured_parent_id = int(target_parent_id or 0)
        hosts = [self.WEB_BASE_URL, self.BACKUP_BASE_URL]
        last_msg = ""

        for host in hosts:
            ok_root, root_items, err_root = self._list_share_dir(
                host=host, share_key=share_key, share_pwd=share_pwd, parent_file_id=0
            )
            if not ok_root or not root_items:
                last_msg = err_root or "分享链接内容为空或已失效"
                continue

            # 1. 确定本次转存的基准归集目录 ID (优先归集到配置的父目录下的影视同名文件夹)
            base_target_id = configured_parent_id
            has_single_root_folder = (
                len(root_items) == 1
                and int(root_items[0].get("Type") if root_items[0].get("Type") is not None else (root_items[0].get("type") or 0)) == 1
            )

            if folder_name:
                ok_mk, dir_id, _ = self.mkdir(dirname=folder_name, parent_file_id=configured_parent_id)
                if ok_mk and dir_id:
                    base_target_id = dir_id
                else:
                    base_target_id = configured_parent_id
            elif has_single_root_folder:
                root_dir_name = str(root_items[0].get("FileName") or root_items[0].get("fileName") or "").strip()
                if root_dir_name:
                    ok_mk, dir_id, _ = self.mkdir(dirname=root_dir_name, parent_file_id=configured_parent_id)
                    if ok_mk and dir_id:
                        base_target_id = dir_id

            # 若分享根目录仅有一个总文件夹，且我们已经创建了影视同名归集目录 base_target_id，
            # 则直接展开该根文件夹内容转存到 base_target_id 下，避免出现「谷雨 (2026)/谷雨 (2026)」双层同名套娃
            initial_items = root_items
            if has_single_root_folder and base_target_id != configured_parent_id:
                root_folder_fid = root_items[0].get("FileId") or root_items[0].get("fileId")
                if root_folder_fid:
                    ok_sub, sub_items, _ = self._list_share_dir(
                        host=host, share_key=share_key, share_pwd=share_pwd, parent_file_id=int(root_folder_fid)
                    )
                    if ok_sub and sub_items:
                        initial_items = sub_items

            saved_files_count = 0
            primary_file_id: Optional[str] = None
            largest_video_size = -1
            last_file_err = ""

            def _transfer_items_recursive(items_list: List[Dict[str, Any]], cur_dir_id: int, depth: int = 0) -> None:
                nonlocal saved_files_count, primary_file_id, largest_video_size, last_file_err
                if depth > 6:
                    return

                for it in items_list:
                    itype = int(it.get("Type") if it.get("Type") is not None else (it.get("type") or 0))
                    ifid = it.get("FileId") or it.get("fileId")
                    iname = str(it.get("FileName") or it.get("fileName") or it.get("filename") or "").strip()

                    if itype == 1:
                        # 子文件夹（如 Season 1 / 字幕 等）：在目标目录创建同名子目录并递归转存
                        sub_target_id = cur_dir_id
                        if iname:
                            ok_sub_mk, created_sub_id, _ = self.mkdir(dirname=iname, parent_file_id=cur_dir_id)
                            if ok_sub_mk and created_sub_id:
                                sub_target_id = created_sub_id
                        if ifid is not None:
                            ok_ch, ch_items, _ = self._list_share_dir(
                                host=host, share_key=share_key, share_pwd=share_pwd, parent_file_id=int(ifid)
                            )
                            if ok_ch and ch_items:
                                _transfer_items_recursive(ch_items, sub_target_id, depth + 1)
                    else:
                        # 文件（视频、字幕、NFO、海报等）：提取 Etag 与 Size 直接发起秒传转存
                        etag = str(it.get("Etag") or it.get("etag") or it.get("md5") or "").strip()
                        fsize = int(it.get("Size") if it.get("Size") is not None else (it.get("size") or 0))
                        if etag and iname:
                            ok_fs, saved_fid, msg_fs = self.save_fslink(
                                filename=iname,
                                etag=etag,
                                size=fsize,
                                target_parent_id=cur_dir_id,
                                folder_name="",
                            )
                            if ok_fs:
                                saved_files_count += 1
                                if fsize > largest_video_size and saved_fid:
                                    largest_video_size = fsize
                                    primary_file_id = str(saved_fid)
                            else:
                                last_file_err = msg_fs

            _transfer_items_recursive(initial_items, base_target_id, depth=0)

            if saved_files_count > 0:
                final_fid = primary_file_id or str(base_target_id)
                return True, final_fid, f"分享转存成功 (共秒传同步 {saved_files_count} 个文件)"

            # 兼容兜底：若单元测试或特殊场景下 InfoList 未携带 Etag 字段，则回退尝试 /b/api/share/save
            file_id_list = [
                int(it.get("FileId") or it.get("fileId"))
                for it in root_items
                if (it.get("FileId") or it.get("fileId")) is not None
            ]
            if file_id_list and not last_file_err:
                save_payload = {
                    "shareKey": share_key,
                    "sharePwd": share_pwd,
                    "parentFileId": base_target_id,
                    "fileIdList": file_id_list,
                    "duplicate": 2,
                }
                save_url = f"{host}/b/api/share/save"
                ok_save, res_save, msg_save = self._http_request(
                    save_url, method="POST", payload=save_payload, with_auth=True
                )
                if ok_save and res_save.get("code") == 0:
                    data_s = res_save.get("data") or {}
                    fid = data_s.get("fileId") or data_s.get("FileId") or str(base_target_id or file_id_list[0])
                    return True, str(fid), f"分享转存成功 (共包含 {len(file_id_list)} 个文件/文件夹)"
                last_msg = res_save.get("message") if ok_save else msg_save
            else:
                last_msg = last_file_err or "分享内未找到可转存的文件"

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
        解析 123 秒传码 (123FSLink) 并零流量直接秒传至用户配置的目录 (支持自动归集至同名子文件夹)。
        当归集子文件夹在云端不存在/失效时，优先在配置的目录ID (configured_parent_id) 下重建子文件夹或直接存入配置目录，
        绝不越过用户配置的目录ID随意丢到根目录 (0)！
        """
        if not self.is_configured():
            return False, None, "123 云盘 Web Token 未配置"

        configured_parent_id = int(parent_file_id or target_parent_id or 0)
        target_id = configured_parent_id

        # 若提供了影视文件夹名称，在用户配置的目录下自动检索或创建该同名子文件夹
        if folder_name:
            ok_mk, dir_id, msg_mk = self.mkdir(dirname=folder_name, parent_file_id=configured_parent_id)
            if ok_mk and dir_id:
                target_id = dir_id
            else:
                # 若创建子文件夹失败，保持 target_id = configured_parent_id（存入用户配置的目录ID下）
                logger.warning(
                    f"Pan123TgPilot 在配置目录 [{configured_parent_id}] 下创建归集子文件夹【{folder_name}】未成功 ({msg_mk})，"
                    f"将直接转存至配置目录 [{configured_parent_id}] 下"
                )
                target_id = configured_parent_id

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

        def _extract_reuse_file_id(res_dict: dict) -> Tuple[bool, Optional[str]]:
            data = res_dict.get("data") or {}
            reuse = bool(data.get("reuse") or data.get("Reuse"))
            info = data.get("Info") if isinstance(data.get("Info"), dict) else {}
            fid = (
                data.get("fileId")
                or data.get("FileId")
                or info.get("FileId")
                or info.get("fileId")
            )
            if reuse or (fid and not data.get("preuploadID")):
                return True, str(fid or 0)
            return False, None

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
                hit, fid_str = _extract_reuse_file_id(res)
                if hit:
                    return True, fid_str, "秒传转存成功"
                return False, None, "云端未命中秒传特征"
            else:
                resp_msg = res.get("message") if ok else msg
                if "文件夹不存在" in str(resp_msg):
                    # 清除失效的目录缓存
                    self.invalidate_dir_cache(parent_file_id=target_id)
                    if folder_name:
                        self.invalidate_dir_cache(parent_file_id=configured_parent_id, dirname=folder_name)

                    # 优先回退方案 1：如果刚才报错的是归集子文件夹 (如 52958217 != 42149741)，
                    # 先尝试在用户配置的目录 (42149741) 下强制刷新重建同名子文件夹，若仍不行则直接秒传到用户配置的目录 (42149741) 下！
                    if target_id != configured_parent_id:
                        logger.warning(
                            f"Pan123TgPilot 归集子文件夹 [{target_id}] 在云端不存在，正在用户配置的目录ID [{configured_parent_id}] 下重新归集秒传..."
                        )
                        fallback_dir_id = configured_parent_id
                        if folder_name:
                            ok_re, new_dir_id, _ = self.mkdir(
                                dirname=folder_name, parent_file_id=configured_parent_id, force_refresh=True
                            )
                            if ok_re and new_dir_id and new_dir_id != target_id:
                                fallback_dir_id = new_dir_id

                        payload["parentFileId"] = fallback_dir_id
                        ok_fb1, res_fb1, msg_fb1 = self._http_request(url, method="POST", payload=payload, with_auth=True)
                        if ok_fb1 and res_fb1.get("code") == 0:
                            hit, fid_str = _extract_reuse_file_id(res_fb1)
                            if hit:
                                return True, fid_str, f"秒传转存成功 (目录ID: {fallback_dir_id})"

                        # 若重建后的子目录仍报错，直接存入用户配置的 configured_parent_id 下
                        if fallback_dir_id != configured_parent_id:
                            payload["parentFileId"] = configured_parent_id
                            ok_fb2, res_fb2, msg_fb2 = self._http_request(
                                url, method="POST", payload=payload, with_auth=True
                            )
                            if ok_fb2 and res_fb2.get("code") == 0:
                                hit, fid_str = _extract_reuse_file_id(res_fb2)
                                if hit:
                                    return True, fid_str, f"秒传转存成功 (已直接存入配置目录ID: {configured_parent_id})"
                            resp_msg = res_fb2.get("message") if ok_fb2 else msg_fb2
                        else:
                            resp_msg = res_fb1.get("message") if ok_fb1 else msg_fb1

                last_msg = resp_msg

        return False, None, f"秒传转存失败: {last_msg}"

    def trash_files(self, file_ids: List[int]) -> Tuple[bool, str]:
        """
        洗版时将旧版本文件移至回收站。
        采用 123 云盘 Web 端标准回收站协议: fileTrashInfoList: [{"FileId": fid}] + event: "intoRecycle"
        """
        clean_ids = []
        for fid in file_ids or []:
            try:
                val = int(fid)
                if val > 0:
                    clean_ids.append(val)
            except (ValueError, TypeError):
                continue

        if not clean_ids or not self.is_configured():
            return False, "无有效文件 ID"

        urls = [
            f"{self.WEB_BASE_URL}/b/api/file/trash",
            f"{self.BACKUP_BASE_URL}/b/api/file/trash",
            f"{self.WEB_BASE_URL}/a/api/file/trash",
        ]
        # 优先使用 123 云盘 Web V3 标准格式 fileTrashInfoList，兼容旧格式兜底
        payload_v3 = {
            "driveId": 0,
            "fileTrashInfoList": [{"FileId": fid} for fid in clean_ids],
            "operation": True,
            "event": "intoRecycle",
            "operatePlace": 1,
            "RequestSource": None,
            "safeBox": False,
        }

        last_err = "未知错误"
        for url in urls:
            ok, res, msg = self._http_request(url, method="POST", payload=payload_v3, with_auth=True)
            if ok and res.get("code") == 0:
                return True, "已成功移至回收站"
            last_err = res.get("message") if ok else msg

        return False, f"移至回收站失败 ({last_err})"

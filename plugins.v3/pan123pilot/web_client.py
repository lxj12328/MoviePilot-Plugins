"""
Pan123Pilot 123云盘 Web/App 协议客户端
完全免除官方 OpenAPI 开发者权益包付费限制，通过 Web Token 或账号密码直连实现零流量秒传
"""

import hashlib
import json
import os
import threading
import time
from typing import Any, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

try:
    from app.log import logger
except ImportError:
    import logging
    logger = logging.getLogger("pan123pilot")


class Pan123WebClient:
    """123云盘 Web/App 协议客户端"""

    WEB_BASE_URL = "https://www.123684.com"
    BACKUP_BASE_URL = "https://yun.123pan.com"
    USER_API_URL = "https://user.123pan.cn"

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        username: str = "",
        password: str = "",
        token: str = "",
        auth_mode: str = "web_token",
    ):
        self.username = (username or "").strip()
        self.password = (password or "").strip()
        self.token = self._clean_token(token)
        self.auth_mode = auth_mode  # web_token / web_sms / web_account
        self._dir_cache: dict[str, int] = {}
        self._dir_lock = threading.Lock()
        self._last_token_check = 0.0
        self.user_info: dict = {}

    def is_configured(self) -> bool:
        """检查 Web 客户端配置是否就绪"""
        if self.auth_mode == "web_token":
            return bool(self.token)
        elif self.auth_mode == "web_account":
            return bool((self.username and self.password) or self.token)
        return bool(self.token or (self.username and self.password))

    @staticmethod
    def _clean_token(raw_token: Any) -> str:
        """清洗 Token：去除首尾空格、包裹的双引号/单引号及重复的 Bearer 前缀"""
        if not raw_token:
            return ""
        tok = str(raw_token).strip().strip('"').strip("'").strip()
        if tok.lower().startswith("bearer "):
            tok = tok[7:].strip().strip('"').strip("'").strip()
        return tok

    def set_token(self, token: str) -> None:
        """动态更新 Token 并自动清洗"""
        self.token = self._clean_token(token)
        self._last_token_check = 0.0

    def _get_headers(self, with_auth: bool = True, extra_headers: Optional[dict] = None) -> dict[str, str]:
        """构造 Web 端标准请求头"""
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Platform": "web",
            "App-Version": "3",
            "User-Agent": self.DEFAULT_USER_AGENT,
            "Origin": "https://www.123pan.com",
            "Referer": "https://www.123pan.com/",
            "Connection": "keep-alive",
        }
        if with_auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _http_get(
        self,
        url: str,
        with_auth: bool = True,
        timeout: int = 30,
        extra_headers: Optional[dict] = None,
        max_retries: int = 2,
    ) -> tuple[bool, dict, str]:
        """统一 HTTP GET 请求方法，支持 SSL 上下文与超时自动重试"""
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        headers = self._get_headers(with_auth=with_auth, extra_headers=extra_headers)
        last_err = "未知错误"
        for attempt in range(max_retries + 1):
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    raw = resp.read().decode("utf-8")
                    res_json = json.loads(raw)
                    return True, res_json, "ok"
            except urllib.error.HTTPError as e:
                try:
                    err_raw = e.read().decode("utf-8")
                    err_json = json.loads(err_raw)
                    return False, err_json, f"HTTP {e.code}: {err_json.get('message', e.reason)}"
                except Exception:
                    return False, {}, f"HTTP {e.code}: {e.reason}"
            except Exception as e:
                last_err = str(e)
                if attempt < max_retries:
                    time.sleep(1)
                    continue
        return False, {}, last_err

    def _http_post(
        self,
        url: str,
        data: dict,
        with_auth: bool = True,
        timeout: int = 30,
        extra_headers: Optional[dict] = None,
        max_retries: int = 2,
    ) -> tuple[bool, dict, str]:
        """统一 HTTP POST 请求方法，支持 SSL 上下文与超时自动重试"""
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        headers = self._get_headers(with_auth=with_auth, extra_headers=extra_headers)
        last_err = "未知错误"
        for attempt in range(max_retries + 1):
            try:
                body_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    raw = resp.read().decode("utf-8")
                    res_json = json.loads(raw)
                    return True, res_json, "ok"
            except urllib.error.HTTPError as e:
                try:
                    err_raw = e.read().decode("utf-8")
                    err_json = json.loads(err_raw)
                    return False, err_json, f"HTTP {e.code}: {err_json.get('message', e.reason)}"
                except Exception:
                    return False, {}, f"HTTP {e.code}: {e.reason}"
            except Exception as e:
                last_err = str(e)
                if attempt < max_retries:
                    time.sleep(1)
                    continue
        return False, {}, last_err

    def check_token_validity(self) -> bool:
        """检查当前 Token 是否有效 (支持 user/info 探活与列表双重校验)"""
        if not self.token:
            return False

        # 5分钟内检查过有效则直接返回
        if time.time() - self._last_token_check < 300:
            return True

        # 1. 优先调用 123 官方 Web 端用户信息接口 GET api/user/info
        user_info_urls = [
            f"{self.WEB_BASE_URL}/api/user/info",
            f"{self.USER_API_URL}/api/user/info",
        ]
        for url in user_info_urls:
            ok, res, _ = self._http_get(url, with_auth=True)
            if ok and res.get("code") == 0:
                self.user_info = res.get("data") or {}
                self._last_token_check = time.time()
                return True

        # 2. 备用调用 POST /b/api/file/list/new 进行探活
        list_url = f"{self.WEB_BASE_URL}/b/api/file/list/new"
        payload = {"parentFileId": 0, "page": 1, "limit": 1}
        ok_l, res_l, _ = self._http_post(list_url, payload, with_auth=True)
        if ok_l and res_l.get("code") == 0:
            self._last_token_check = time.time()
            return True

        return False

    def login_by_password(self) -> tuple[bool, str]:
        """使用账号密码模拟 Web 登录"""
        if not self.username or not self.password:
            return False, "用户名或密码未配置"

        # 尝试两个官方登录端点与兼容负载格式
        endpoints = [
            f"{self.WEB_BASE_URL}/api/user/sign_in",
            f"{self.USER_API_URL}/api/user/sign_in",
        ]

        payload_candidates = [
            {"passport": self.username, "password": self.password, "type": 1},
            {"passport": self.username, "password": self.password, "type": 0},
            {"unicode": self.username, "password": self.password, "type": 0},
        ]

        last_error = "登录未响应"
        for url in endpoints:
            for payload in payload_candidates:
                ok, res, msg = self._http_post(url, payload, with_auth=False)
                if ok:
                    code = res.get("code")
                    if code == 0:
                        data = res.get("data") or {}
                        token = data.get("token") or data.get("accessToken")
                        if token:
                            self.token = token
                            self._last_token_check = time.time()
                            logger.info("Pan123Pilot Web 账号密码登录成功！已获取有效 Token")
                            return True, "登录成功"
                    else:
                        msg_text = res.get("message") or msg
                        last_error = f"登录失败 [{code}]: {msg_text}"
                        if "安全风险" in msg_text or "验证码" in msg_text or "滑块" in msg_text:
                            last_error += "（提示：环境触发风控验证码，请改用「Web Token 直填模式」复制 authorToken，100% 免滑块）"
                            return False, last_error
                else:
                    last_error = msg

        return False, last_error

    def ensure_authenticated(self) -> tuple[bool, str]:
        """确保客户端处于已鉴权状态，严格遵循选定的 auth_mode 隔离执行"""
        # 1. 直填 Token 模式 (web_token): 严格以用户填写的 authorToken 为准，绝不越权调用账号密码登录
        if self.auth_mode == "web_token":
            if not self.token:
                return False, "未配置 Web Token，请在插件表单中填入 123 网页端 authorToken"
            if self.check_token_validity():
                return True, "Web Token 有效"
            return False, "TOKEN_EXPIRED: Web Token 校验未通过或已失效，请重新复制 authorToken 填入"

        # 2. 账号密码模式 (web_account): 若已有有效 Token 优先复用，否则尝试账号密码登录
        if self.token and self.check_token_validity():
            return True, "Token 有效"

        if self.username and self.password:
            return self.login_by_password()

        return False, "未配置有效的 123 账号或密码"

    def sha1_reuse(
        self,
        filename: str,
        sha1_str: str,
        size: int,
        parent_file_id: int = 0,
        md5_str: Optional[str] = None,
    ) -> tuple[bool, Optional[int], str]:
        """
        向 123 云盘 Web 端 /b/api/file/upload_request 发起预上传校验进行秒传。
        使用 etag (MD5) 与 size 校验，完全免除 OpenAPI 开发者包付费限制。
        :return: (reuse: 是否秒传成功, file_id: 云盘文件ID, message: 详情描述)
        """
        ok_auth, auth_msg = self.ensure_authenticated()
        if not ok_auth:
            return False, None, auth_msg

        # 123 Web 端使用 etag (文件 MD5) 进行秒传识别
        etag = (md5_str or sha1_str or "").lower()
        payload = {
            "parentFileId": int(parent_file_id or 0),
            "filename": filename,
            "fileName": filename,
            "etag": etag,
            "size": int(size),
            "type": 0,
            "duplicate": 2,
        }

        candidate_urls = [
            f"{self.WEB_BASE_URL}/b/api/file/upload_request",
            f"{self.BACKUP_BASE_URL}/b/api/file/upload_request",
        ]

        last_fail_msg = "未知错误"
        for url in candidate_urls:
            ok, res, msg = self._http_post(url, payload, with_auth=True)
            if not ok:
                if "401" in msg or "Token" in msg or "token" in msg:
                    self._last_token_check = 0.0
                    if self.auth_mode == "web_account" and self.username and self.password:
                        ok_relogin, _ = self.login_by_password()
                        if ok_relogin:
                            return self.sha1_reuse(filename, sha1_str, size, parent_file_id, md5_str)
                    return False, None, f"TOKEN_EXPIRED: 123云盘 Web Token 已过期 ({msg})，请在插件配置中更新 authorToken"
                last_fail_msg = msg
                continue

            code = res.get("code")
            if code == 0:
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
                    return True, int(file_id or 0), "秒传命中成功"
                else:
                    return False, None, "云端未命中秒传特征"
            elif code in (401, 20101):
                # Token 偶发失效或未登录
                self._last_token_check = 0.0
                if self.auth_mode == "web_account" and self.username and self.password:
                    ok_relogin, _ = self.login_by_password()
                    if ok_relogin:
                        return self.sha1_reuse(filename, sha1_str, size, parent_file_id, md5_str)
                return False, None, "TOKEN_EXPIRED: 123云盘 Web Token 已过期，请在插件配置中更新 authorToken"
            else:
                return False, None, f"秒传返回错误 [{code}]: {res.get('message', '未知错误')}"

        return False, None, f"请求秒传接口失败: {last_fail_msg}"

    def list_files(self, parent_file_id: int = 0) -> tuple[bool, list[dict], str]:
        """获取指定目录下的文件及子文件夹列表"""
        ok_auth, auth_msg = self.ensure_authenticated()
        if not ok_auth:
            return False, [], auth_msg

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

        candidate_hosts = [self.WEB_BASE_URL, self.BACKUP_BASE_URL]
        candidate_paths = ["/b/api/file/list/new", "/api/file/list/new"]

        last_fail_msg = "获取文件列表失败"
        for host in candidate_hosts:
            for path in candidate_paths:
                url = f"{host}{path}?{query_params}"
                ok, res, msg = self._http_get(url, with_auth=True)
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
                last_fail_msg = res.get("message") if ok else msg

        return False, [], last_fail_msg

    def mkdir(self, dirname: str, parent_file_id: int = 0) -> tuple[bool, int, str]:
        """在指定目录下创建新文件夹"""
        ok_auth, auth_msg = self.ensure_authenticated()
        if not ok_auth:
            return False, 0, auth_msg

        payload = {
            "driveId": 0,
            "parentFileId": int(parent_file_id or 0),
            "filename": dirname,
            "fileName": dirname,
            "type": 1,  # 1 代表文件夹
            "size": 0,
            "etag": "",
            "duplicate": 2,  # 2: 若同名则重用/覆盖，避免自动生成 (1), (2) 副本
            "NotReuse": True,
            "event": "newCreateFolder",
            "operateType": 1,
        }

        candidate_urls = [
            f"{self.WEB_BASE_URL}/b/api/file/upload_request",
            f"{self.BACKUP_BASE_URL}/b/api/file/upload_request",
        ]

        last_msg = ""
        for url in candidate_urls:
            ok, res, msg = self._http_post(url, payload, with_auth=True)
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
                    return True, int(dir_id), "创建文件夹成功"
            last_msg = res.get("message") if ok else msg
        return False, 0, last_msg

    def get_or_create_dir_by_path(
        self,
        root_parent_id: int,
        relative_dir_path: str,
    ) -> tuple[bool, int, str]:
        """
        根据相对路径多级级联创建或检索云盘文件夹，带内存缓存加速
        """
        if not relative_dir_path or relative_dir_path in (".", "/", "\\"):
            return True, root_parent_id, "ok"

        normalized_path = relative_dir_path.replace("\\", "/").strip("/")
        parts = [p.strip() for p in normalized_path.split("/") if p.strip()]

        with self._dir_lock:
            current_parent_id = root_parent_id
            current_path_acc = ""

            for part in parts:
                current_path_acc = f"{current_path_acc}/{part}" if current_path_acc else part
                cache_key = f"{root_parent_id}:{current_path_acc}"

                # 1. 命中缓存
                if cache_key in self._dir_cache:
                    current_parent_id = self._dir_cache[cache_key]
                    continue

                # 2. 检索远端当前目录下是否存在同名文件夹
                found = False
                ok_list, items, _ = self.list_files(parent_file_id=current_parent_id)
                if ok_list:
                    for item in items:
                        name = str(item.get("filename") or item.get("FileName") or item.get("file_name") or "").strip()
                        is_folder = (
                            item.get("type") in (1, "1")
                            or item.get("Type") in (1, "1")
                            or item.get("category") in (1, "1")
                        )
                        if is_folder and name.lower() == part.lower():
                            current_parent_id = int(item.get("fileId") or item.get("FileId") or item.get("file_id"))
                            self._dir_cache[cache_key] = current_parent_id
                            found = True
                            break

                # 3. 远端不存在，创建新文件夹
                if not found:
                    ok_mk, new_dir_id, err_msg = self.mkdir(part, parent_file_id=current_parent_id)
                    if not ok_mk or new_dir_id <= 0:
                        # 再次重试列表，防止已有同名文件夹或并发冲突
                        ok_retry, retry_items, _ = self.list_files(parent_file_id=current_parent_id)
                        for item in retry_items:
                            name = str(item.get("filename") or item.get("FileName") or item.get("file_name") or "").strip()
                            is_folder = (
                                item.get("type") in (1, "1")
                                or item.get("Type") in (1, "1")
                                or item.get("category") in (1, "1")
                            )
                            if is_folder and name.lower() == part.lower():
                                current_parent_id = int(item.get("fileId") or item.get("FileId") or item.get("file_id"))
                                self._dir_cache[cache_key] = current_parent_id
                                found = True
                                break
                        if not found:
                            return False, current_parent_id, f"创建或定位目录【{part}】失败: {err_msg}"
                    else:
                        current_parent_id = new_dir_id
                        self._dir_cache[cache_key] = current_parent_id

            return True, current_parent_id, "ok"

    def upload_file_v2(
        self,
        filepath: str,
        parent_file_id: int = 0,
        etag: Optional[str] = None,
    ) -> tuple[bool, int, str]:
        """
        Web 端真实文件分片上传实现（主要用于无法秒传的 .nfo/.srt/.jpg 刮削伴生文件及兜底上传）
        支持预上传秒传命中、S3 预签名分片上传确认
        """
        if not os.path.exists(filepath):
            return False, 0, f"本地文件不存在: {filepath}"

        ok_auth, auth_msg = self.ensure_authenticated()
        if not ok_auth:
            return False, 0, auth_msg

        file_size = os.path.getsize(filepath)
        filename = os.path.basename(filepath)

        if not etag:
            try:
                with open(filepath, "rb") as f:
                    if hasattr(hashlib, "file_digest"):
                        etag = hashlib.file_digest(f, "md5").hexdigest()
                    else:
                        md5_hash = hashlib.md5()
                        for chunk in iter(lambda: f.read(1024 * 1024 * 4), b""):
                            md5_hash.update(chunk)
                        etag = md5_hash.hexdigest()
            except Exception as e:
                return False, 0, f"计算文件 MD5 失败: {e}"

        req_payload = {
            "driveId": 0,
            "parentFileId": int(parent_file_id or 0),
            "filename": filename,
            "fileName": filename,
            "etag": etag.lower(),
            "size": int(file_size),
            "type": 0,
            "duplicate": 2,
        }

        candidate_urls = [
            f"{self.WEB_BASE_URL}/b/api/file/upload_request",
            f"{self.BACKUP_BASE_URL}/b/api/file/upload_request",
        ]

        res_data: dict = {}
        last_err = "请求预上传失败"
        for url in candidate_urls:
            ok, res, msg = self._http_post(url, req_payload, with_auth=True)
            if ok and res.get("code") == 0:
                res_data = res.get("data") or {}
                break
            last_err = res.get("message") if ok else msg

        if not res_data:
            return False, 0, f"创建 Web 上传任务失败: {last_err}"

        # 1. 若预上传直接命中秒传
        reuse = bool(res_data.get("reuse") or res_data.get("Reuse"))
        info = res_data.get("Info") if isinstance(res_data.get("Info"), dict) else {}
        file_id = int(
            res_data.get("FileId")
            or res_data.get("fileId")
            or info.get("FileId")
            or info.get("fileId")
            or 0
        )
        if reuse:
            return True, file_id, "创建时命中秒传"

        # 2. 提取 S3 分片上传参数
        bucket = res_data.get("Bucket") or res_data.get("bucket")
        key = res_data.get("Key") or res_data.get("key")
        upload_id = res_data.get("UploadId") or res_data.get("uploadId") or res_data.get("preuploadID")
        storage_node = res_data.get("StorageNode") or res_data.get("storageNode") or ""
        slice_size = int(res_data.get("SliceSize") or res_data.get("sliceSize") or 16777216)
        if slice_size <= 0:
            slice_size = 16777216

        if not bucket or not key or not upload_id:
            return False, 0, "云端未命中秒传且未返回 Web S3 上传凭证"

        num_parts = max(1, (file_size + slice_size - 1) // slice_size)
        s3_auth_payload = {
            "bucket": bucket,
            "key": key,
            "uploadId": upload_id,
            "storageNode": storage_node,
            "partNumberStart": 1,
            "partNumberEnd": num_parts + 1,
        }

        s3_urls_endpoints = [
            f"{self.WEB_BASE_URL}/b/api/file/s3_re_ense_upload_parts",
            f"{self.WEB_BASE_URL}/b/api/file/s3_upload_object/auth",
            f"{self.BACKUP_BASE_URL}/b/api/file/s3_re_ense_upload_parts",
        ]

        presigned_urls: dict = {}
        for s3_ep in s3_urls_endpoints:
            ok_s3, res_s3, msg_s3 = self._http_post(s3_ep, s3_auth_payload, with_auth=True)
            if ok_s3 and res_s3.get("code") == 0:
                s3_data = res_s3.get("data") or {}
                presigned_urls = s3_data.get("presignedUrls") or s3_data.get("presigned_urls") or {}
                if presigned_urls:
                    break
            last_err = res_s3.get("message") if ok_s3 else msg_s3

        if not presigned_urls:
            return False, 0, f"获取 S3 预签名上传地址失败: {last_err}"

        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        try:
            with open(filepath, "rb") as f:
                for part_no in range(1, num_parts + 1):
                    chunk = f.read(slice_size)
                    if not chunk and file_size > 0:
                        break
                    put_url = presigned_urls.get(str(part_no)) or presigned_urls.get(part_no)
                    if not put_url:
                        return False, 0, f"缺少分片 {part_no} 的预签名 URL"
                    req_put = urllib.request.Request(
                        put_url,
                        data=chunk,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(len(chunk)),
                            "User-Agent": self.DEFAULT_USER_AGENT,
                        },
                        method="PUT",
                    )
                    with urllib.request.urlopen(req_put, timeout=60, context=ctx) as resp_put:
                        if resp_put.status not in (200, 201, 204):
                            return False, 0, f"S3 分片上传返回 HTTP {resp_put.status}"
        except Exception as e:
            return False, 0, f"上传数据分片异常: {e}"

        # 3. 完成上传确认
        complete_payload = {
            "fileId": file_id,
            "bucket": bucket,
            "key": key,
            "uploadId": upload_id,
            "storageNode": storage_node,
            "fileSize": int(file_size),
            "isMultipart": num_parts > 1,
        }
        complete_endpoints = [
            f"{self.WEB_BASE_URL}/b/api/file/s3_complete_multipart_upload",
            f"{self.WEB_BASE_URL}/b/api/file/upload_complete",
            f"{self.BACKUP_BASE_URL}/b/api/file/s3_complete_multipart_upload",
        ]
        for comp_ep in complete_endpoints:
            ok_c, res_c, msg_c = self._http_post(comp_ep, complete_payload, with_auth=True)
            if ok_c and res_c.get("code") == 0:
                c_data = res_c.get("data") or {}
                c_info = c_data.get("file_info") or c_data.get("Info") or {}
                final_id = int(
                    c_data.get("FileId")
                    or c_data.get("fileId")
                    or (c_info.get("FileId") if isinstance(c_info, dict) else 0)
                    or (c_info.get("fileId") if isinstance(c_info, dict) else 0)
                    or file_id
                    or 0
                )
                return True, final_id, "Web 分片上传成功"
            last_err = res_c.get("message") if ok_c else msg_c

        return False, 0, f"确认分片上传完成失败: {last_err}"

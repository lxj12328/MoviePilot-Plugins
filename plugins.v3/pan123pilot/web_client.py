"""
Pan123Pilot 123云盘 Web/App 协议客户端
完全免除官方 OpenAPI 开发者权益包付费限制，通过 Web Token 或账号密码直连实现零流量秒传
"""

import json
import os
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

    WEB_BASE_URL = "https://www.123pan.com"
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
            "Content-Type": "application/json",
            "Platform": "web",
            "App-Version": "3",
            "User-Agent": self.DEFAULT_USER_AGENT,
            "Origin": self.WEB_BASE_URL,
            "Referer": f"{self.WEB_BASE_URL}/",
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
        timeout: int = 15,
        extra_headers: Optional[dict] = None,
    ) -> tuple[bool, dict, str]:
        """统一 HTTP GET 请求方法"""
        headers = self._get_headers(with_auth=with_auth, extra_headers=extra_headers)
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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
            return False, {}, str(e)

    def _http_post(
        self,
        url: str,
        data: dict,
        with_auth: bool = True,
        timeout: int = 15,
        extra_headers: Optional[dict] = None,
    ) -> tuple[bool, dict, str]:
        """统一 HTTP POST 请求方法"""
        headers = self._get_headers(with_auth=with_auth, extra_headers=extra_headers)
        try:
            body_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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
            return False, {}, str(e)

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

        url = f"{self.WEB_BASE_URL}/b/api/file/upload_request"
        # 123 Web 端使用 etag (文件 MD5) 进行秒传识别
        etag = (md5_str or sha1_str or "").lower()
        payload = {
            "parentFileId": int(parent_file_id or 0),
            "filename": filename,
            "etag": etag,
            "size": int(size),
            "type": 0,
            "duplicate": 1,
        }

        ok, res, msg = self._http_post(url, payload, with_auth=True)
        if not ok:
            if "401" in msg or "Token" in msg or "token" in msg:
                self._last_token_check = 0.0
                if self.auth_mode == "web_account" and self.username and self.password:
                    ok_relogin, _ = self.login_by_password()
                    if ok_relogin:
                        return self.sha1_reuse(filename, sha1_str, size, parent_file_id, md5_str)
                return False, None, f"TOKEN_EXPIRED: 123云盘 Web Token 已过期 ({msg})，请在插件配置中更新 authorToken"
            return False, None, f"请求秒传接口失败: {msg}"

        code = res.get("code")
        if code == 0:
            data = res.get("data") or {}
            reuse = bool(data.get("reuse") or data.get("Reuse"))
            file_id = data.get("fileId") or data.get("FileId")
            if reuse:
                return True, int(file_id or 0), "秒传命中成功"
            else:
                return False, None, "云端未命中秒传特征"
        elif code == 401:
            # Token 偶发失效
            self._last_token_check = 0.0
            if self.auth_mode == "web_account" and self.username and self.password:
                ok_relogin, _ = self.login_by_password()
                if ok_relogin:
                    return self.sha1_reuse(filename, sha1_str, size, parent_file_id, md5_str)
            return False, None, "TOKEN_EXPIRED: 123云盘 Web Token 已过期，请在插件配置中更新 authorToken"
        else:
            return False, None, f"秒传返回错误 [{code}]: {res.get('message', '未知错误')}"

    def list_files(self, parent_file_id: int = 0) -> tuple[bool, list[dict], str]:
        """获取指定目录下的文件及子文件夹列表"""
        ok_auth, auth_msg = self.ensure_authenticated()
        if not ok_auth:
            return False, [], auth_msg

        url = f"{self.WEB_BASE_URL}/b/api/file/list/new"
        payload = {
            "parentFileId": int(parent_file_id or 0),
            "page": 1,
            "limit": 100,
            "order": "filename",
            "orderBy": "asc",
        }

        ok, res, msg = self._http_post(url, payload, with_auth=True)
        if ok and res.get("code") == 0:
            data = res.get("data") or {}
            items = data.get("fileList") or data.get("InfoList") or []
            return True, items, "ok"
        return False, [], res.get("message") if ok else msg

    def mkdir(self, dirname: str, parent_file_id: int = 0) -> tuple[bool, int, str]:
        """在指定目录下创建新文件夹"""
        ok_auth, auth_msg = self.ensure_authenticated()
        if not ok_auth:
            return False, 0, auth_msg

        url = f"{self.WEB_BASE_URL}/b/api/file/upload_request"
        payload = {
            "parentFileId": int(parent_file_id or 0),
            "filename": dirname,
            "type": 1,  # 1 代表文件夹
            "size": 0,
            "etag": "",
            "duplicate": 1,
        }

        ok, res, msg = self._http_post(url, payload, with_auth=True)
        if ok and res.get("code") == 0:
            data = res.get("data") or {}
            dir_id = data.get("fileId") or data.get("FileId")
            if dir_id:
                return True, int(dir_id), "创建文件夹成功"
        return False, 0, res.get("message") if ok else msg

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
                    name = item.get("filename") or item.get("FileName")
                    is_folder = item.get("type") == 1 or item.get("Type") == 1
                    if is_folder and name == part:
                        current_parent_id = int(item.get("fileId") or item.get("FileId"))
                        self._dir_cache[cache_key] = current_parent_id
                        found = True
                        break

            # 3. 远端不存在，创建新文件夹
            if not found:
                ok_mk, new_dir_id, err_msg = self.mkdir(part, parent_file_id=current_parent_id)
                if not ok_mk:
                    # 再次重试列表，防止并发重名创建冲突
                    ok_retry, retry_items, _ = self.list_files(parent_file_id=current_parent_id)
                    for item in retry_items:
                        if (item.get("type") == 1 or item.get("Type") == 1) and (
                            item.get("filename") == part or item.get("FileName") == part
                        ):
                            current_parent_id = int(item.get("fileId") or item.get("FileId"))
                            self._dir_cache[cache_key] = current_parent_id
                            found = True
                            break
                    if not found:
                        return False, current_parent_id, f"创建目录【{part}】失败: {err_msg}"
                else:
                    current_parent_id = new_dir_id
                    self._dir_cache[cache_key] = current_parent_id

        return True, current_parent_id, "ok"

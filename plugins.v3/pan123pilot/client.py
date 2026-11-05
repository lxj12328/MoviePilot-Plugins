"""
123云盘开放平台 API 客户端封装
支持 Token 自动保活换发、SHA1 秒传检测、V2 分片真实上传与目录创建
"""

from datetime import datetime, timedelta
import hashlib
import json
import os
import time
from typing import Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request


class Pan123Client:
    """123云盘开放平台 API 交互客户端"""

    API_BASE = "https://open-api.123pan.com"

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id.strip() if client_id else ""
        self.client_secret = client_secret.strip() if client_secret else ""
        self._access_token: Optional[str] = None
        self._expired_at: Optional[datetime] = None

    def is_configured(self) -> bool:
        """检查凭证是否已配置"""
        return bool(self.client_id and self.client_secret)

    def get_access_token(self, force_refresh: bool = False) -> str:
        """
        获取或刷新 access_token。
        自动根据 expiredAt 在过期前 5 分钟发起刷新。
        """
        if not self.is_configured():
            raise ValueError("123云盘 Client ID 或 Client Secret 未配置")

        now = datetime.now()
        if (
            not force_refresh
            and self._access_token
            and self._expired_at
            and now < (self._expired_at - timedelta(minutes=5))
        ):
            return self._access_token

        url = f"{self.API_BASE}/api/v1/access_token"
        headers = {
            "Platform": "open_platform",
            "Content-Type": "application/json",
        }
        payload = json.dumps({
            "clientID": self.client_id,
            "clientSecret": self.client_secret,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"请求 123 云盘 Access Token 失败: {e}")

        if result.get("code") != 0:
            raise RuntimeError(f"获取 Access Token 失败 [{result.get('code')}]: {result.get('message')}")

        data = result.get("data") or {}
        token = data.get("accessToken")
        expired_at_str = data.get("expiredAt")

        if not token:
            raise RuntimeError("123 云盘未返回有效的 accessToken")

        self._access_token = token
        if expired_at_str:
            try:
                # 解析 ISO 8601 格式: 2025-03-23T15:48:37+08:00
                clean_time = expired_at_str.split("+")[0]
                self._expired_at = datetime.fromisoformat(clean_time)
            except Exception:
                self._expired_at = now + timedelta(hours=2)
        else:
            self._expired_at = now + timedelta(hours=2)

        return self._access_token

    def _auth_headers(self) -> dict:
        """生成带鉴权的通用头部"""
        token = self.get_access_token()
        return {
            "Platform": "open_platform",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def sha1_reuse(
        self,
        filename: str,
        sha1_str: str,
        size: int,
        parent_file_id: int = 0,
        duplicate: int = 1,
    ) -> Tuple[bool, int, str]:
        """
        SHA1 秒传检测与复用接口
        :return: (reuse: 是否秒传成功, fileID: 云盘文件ID, message: 响应说明)
        """
        url = f"{self.API_BASE}/upload/v2/file/sha1_reuse"
        payload = json.dumps({
            "parentFileID": int(parent_file_id),
            "filename": filename,
            "sha1": sha1_str,
            "size": int(size),
            "duplicate": duplicate,
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=self._auth_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return False, 0, f"网络请求异常: {e}"

        if res.get("code") != 0:
            return False, 0, f"接口返回错误 [{res.get('code')}]: {res.get('message')}"

        data = res.get("data") or {}
        reuse = bool(data.get("reuse"))
        file_id = int(data.get("fileID") or 0)
        return reuse, file_id, "秒传成功" if reuse else "未命中秒传"

    def mkdir(self, name: str, parent_id: int = 0) -> Tuple[bool, int, str]:
        """
        创建目录
        :return: (success: 是否成功, dirID: 创建的目录ID, message: 说明)
        """
        url = f"{self.API_BASE}/upload/v1/file/mkdir"
        payload = json.dumps({
            "name": name,
            "parentID": int(parent_id),
        }).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=self._auth_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return False, 0, f"创建目录网络请求异常: {e}"

        if res.get("code") != 0:
            return False, 0, f"创建目录失败 [{res.get('code')}]: {res.get('message')}"

        dir_id = int(res.get("data", {}).get("dirID") or 0)
        return True, dir_id, "创建成功"

    def upload_file_v2(
        self,
        filepath: str,
        parent_file_id: int = 0,
        etag: Optional[str] = None,
    ) -> Tuple[bool, int, str]:
        """
        V2 分片断点真实上传
        :return: (success: 是否成功, fileID: 上传完成的文件ID, message: 描述)
        """
        if not os.path.exists(filepath):
            return False, 0, f"本地文件不存在: {filepath}"

        file_size = os.path.getsize(filepath)
        filename = os.path.basename(filepath)

        # 1. 计算文件整体 MD5 (etag)
        if not etag:
            md5_hash = hashlib.md5()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024 * 2), b""):
                    md5_hash.update(chunk)
            etag = md5_hash.hexdigest()

        # 2. 调用创建文件接口
        create_url = f"{self.API_BASE}/upload/v2/file/create"
        create_payload = json.dumps({
            "parentFileID": int(parent_file_id),
            "filename": filename,
            "etag": etag,
            "size": file_size,
        }).encode("utf-8")

        req = urllib.request.Request(create_url, data=create_payload, headers=self._auth_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return False, 0, f"创建上传任务异常: {e}"

        if res.get("code") != 0:
            return False, 0, f"创建上传任务失败 [{res.get('code')}]: {res.get('message')}"

        data = res.get("data") or {}
        if data.get("reuse"):
            # 在创建时发生了 MD5 秒传
            return True, int(data.get("fileID") or 0), "创建时命中秒传"

        preupload_id = data.get("preuploadID")
        slice_size = int(data.get("sliceSize") or 16777216)  # 默认 16MB
        servers = data.get("servers") or []

        if not preupload_id or not servers:
            return False, 0, "接口未返回合法的 preuploadID 或上传服务器地址"

        upload_server = servers[0]

        # 3. 分片上传
        slice_no = 1
        with open(filepath, "rb") as f:
            while True:
                slice_data = f.read(slice_size)
                if not slice_data:
                    break
                slice_md5 = hashlib.md5(slice_data).hexdigest()
                ok, err = self._upload_slice(
                    server=upload_server,
                    preupload_id=preupload_id,
                    slice_no=slice_no,
                    slice_md5=slice_md5,
                    slice_data=slice_data,
                    filename=filename,
                )
                if not ok:
                    return False, 0, f"分片 {slice_no} 上传失败: {err}"
                slice_no += 1

        # 4. 上传完毕确认与轮询
        return self._complete_upload(preupload_id)

    def _upload_slice(
        self,
        server: str,
        preupload_id: str,
        slice_no: int,
        slice_md5: str,
        slice_data: bytes,
        filename: str,
    ) -> Tuple[bool, str]:
        """上传单分片数据 (multipart/form-data)"""
        url = f"{server.rstrip('/')}/upload/v2/file/slice"
        boundary = f"----WebKitFormBoundary{int(time.time()*1000)}"

        body_parts = []

        def add_field(name: str, val: str):
            body_parts.append(f"--{boundary}\r\n".encode("utf-8"))
            body_parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            body_parts.append(f"{val}\r\n".encode("utf-8"))

        add_field("preuploadID", preupload_id)
        add_field("sliceNo", str(slice_no))
        add_field("sliceMD5", slice_md5)

        # 文件字段
        body_parts.append(f"--{boundary}\r\n".encode("utf-8"))
        body_parts.append(
            f'Content-Disposition: form-data; name="slice"; filename="{filename}.part{slice_no}"\r\n'.encode("utf-8")
        )
        body_parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
        body_parts.append(slice_data)
        body_parts.append(b"\r\n")
        body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))

        full_body = b"".join(body_parts)

        headers = {
            "Platform": "open_platform",
            "Authorization": f"Bearer {self.get_access_token()}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }

        req = urllib.request.Request(url, data=full_body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("code") == 0:
                    return True, ""
                return False, f"[{res.get('code')}] {res.get('message')}"
        except Exception as e:
            return False, str(e)

    def _complete_upload(self, preupload_id: str, max_poll: int = 10) -> Tuple[bool, int, str]:
        """完成上传确认并进行结果轮询"""
        url = f"{self.API_BASE}/upload/v2/file/upload_complete"
        payload = json.dumps({"preuploadID": preupload_id}).encode("utf-8")

        for _ in range(max_poll):
            req = urllib.request.Request(url, data=payload, headers=self._auth_headers(), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                time.sleep(1)
                continue

            if res.get("code") == 0:
                data = res.get("data") or {}
                completed = bool(data.get("completed"))
                file_id = int(data.get("fileID") or 0)
                if completed and file_id != 0:
                    return True, file_id, "上传完成"
            time.sleep(1)

        return False, 0, "轮询上传结果超时"

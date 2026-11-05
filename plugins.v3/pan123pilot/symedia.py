"""
Symedia Webhook 通知模块
兼容 CloudDrive2 (CD2) 文件系统事件格式，向 Symedia 推送增量文件创建/删除事件，
触发即时生成 .strm 302 直链播放文件。
"""

import json
import os
import time
from typing import Optional, Tuple
import urllib.error
import urllib.request


def send_symedia_file_notify(
    server_url: str,
    target_path: str,
    local_library_root: str = "",
    mount_prefix: str = "",
    auth_header: str = "",
    action: str = "create",
    device_name: str = "MoviePilot-Pan123",
    user_name: str = "admin",
    timeout: int = 10,
) -> Tuple[bool, str]:
    """
    发送符合 CloudDrive2 规范的 file_notify Webhook 给 Symedia
    :param server_url: Symedia 服务地址，如 http://127.0.0.1:8095
    :param target_path: 本地文件实际路径，如 /video/emby-movies/国产电影/流浪地球.mkv
    :param local_library_root: 本地媒体库根目录，如 /video
    :param mount_prefix: Symedia 端的挂载前缀，如 /CloudNAS/123/video
    :param auth_header: 请求头 Authorization 凭证（可选，如 basic usernamepassword）
    :param action: 操作类型 (create, delete)
    :param device_name: 设备标识
    :param user_name: 用户名
    :param timeout: 超时秒数
    :return: (success, message)
    """
    if not server_url:
        return False, "Symedia 服务地址为空"

    base_url = server_url.strip().rstrip("/")
    # 规范化 endpoint URL
    url = f"{base_url}/api/v1/webhook/clouddrive2/file_notify?device_name={device_name}&user_name={user_name}&type=notify"

    # 计算 Symedia 端对应的 source_file 路径
    norm_target = os.path.normpath(target_path)
    if local_library_root:
        norm_root = os.path.normpath(local_library_root)
        if norm_target.startswith(norm_root):
            rel_path = os.path.relpath(norm_target, norm_root)
        else:
            rel_path = os.path.basename(norm_target)
    else:
        rel_path = os.path.basename(norm_target)

    if mount_prefix:
        norm_mount = mount_prefix.strip().rstrip("/")
        source_file = f"{norm_mount}/{rel_path.lstrip('/')}"
    else:
        source_file = norm_target

    now_ts = int(time.time())
    payload_data = {
        "device_name": device_name,
        "user_name": user_name,
        "version": "0.7.1",
        "event_category": "file",
        "event_name": "notify",
        "event_time": str(now_ts),
        "send_time": str(now_ts),
        "data": [
            {
                "action": action,
                "is_dir": os.path.isdir(target_path) if os.path.exists(target_path) else False,
                "source_file": source_file,
                "destination_file": "",
            }
        ],
    }

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "clouddrive2/0.7.1",
    }
    if auth_header:
        headers["Authorization"] = auth_header.strip()

    req_data = json.dumps(payload_data).encode("utf-8")
    req = urllib.request.Request(url, data=req_data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            if 200 <= code < 300:
                return True, f"Symedia Webhook 通知成功 [HTTP {code}] (路径: {source_file})"
            return False, f"Symedia 返回状态码: {code}"
    except Exception as e:
        return False, f"Symedia Webhook 发送失败: {e}"

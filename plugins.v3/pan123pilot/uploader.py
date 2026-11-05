"""
Pan123Pilot 任务调度器与执行引擎
提供流式哈希计算、秒传与真实分片上传状态流转、重试轮询与做种安全清理
"""

from datetime import datetime
import hashlib
import os
import threading
import time
from typing import Callable, Optional

from .client import Pan123Client
from .models import Pan123TaskDatabase
from .qb_helper import QBHelper
from .symedia import send_symedia_file_notify


class TaskScheduler:
    """任务调度与后台执行器"""

    def __init__(
        self,
        client: Pan123Client,
        qb_helper: QBHelper,
        db_helper: Pan123TaskDatabase,
        tags_config: dict,
        notify_callback: Optional[Callable[[str, str], None]] = None,
        notify_config: Optional[dict] = None,
        target_parent_file_id: int = 0,
        local_library_root: str = "",
        enable_path_cascade: bool = True,
        symedia_config: Optional[dict] = None,
    ):
        self.client = client
        self.qb = qb_helper
        self.db = db_helper
        self.tags = tags_config
        self.notify_callback = notify_callback
        self.notify_config = notify_config or {}
        self.target_parent_file_id = int(target_parent_file_id or 0)
        self.local_library_root = (local_library_root or "").strip()
        self.enable_path_cascade = bool(enable_path_cascade)
        self.symedia_config = symedia_config or {}

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        # 缓存所有插件定义的标签集合，便于流转时精准清理旧标签
        self.all_plugin_tags = [
            tags_config.get("pending", "123_待处理"),
            tags_config.get("uploading", "123_上传中"),
            tags_config.get("retrying", "123_待重试"),
            tags_config.get("failed", "123_秒传失败"),
            tags_config.get("completed", "123_已上传"),
        ]

    def resolve_target_parent_id(self, target_path: str) -> int:
        """
        根据本地路径、媒体库根路径及级联配置，递归计算最终对应的 123 云盘父目录 ID
        """
        root_id = int(self.target_parent_file_id or 0)
        if not self.enable_path_cascade or not self.local_library_root:
            return root_id

        norm_root = os.path.normpath(self.local_library_root)
        file_dir = os.path.dirname(os.path.normpath(target_path))
        if file_dir.startswith(norm_root):
            rel_dir = os.path.relpath(file_dir, norm_root)
            if rel_dir and rel_dir not in (".", "/", "\\"):
                ok, dir_id, _ = self.client.get_or_create_dir_by_path(
                    root_parent_id=root_id,
                    relative_dir_path=rel_dir,
                )
                if ok and dir_id > 0:
                    return dir_id
        return root_id

    def _trigger_symedia_notify(self, target_path: str, action: str = "create") -> None:
        """若启用了 Symedia 联动，发送 Webhook 通知即时生成或清理 strm"""
        if not self.symedia_config or not self.symedia_config.get("enabled"):
            return
        server_url = self.symedia_config.get("url", "")
        if not server_url:
            return
        try:
            send_symedia_file_notify(
                server_url=server_url,
                target_path=target_path,
                local_library_root=self.local_library_root,
                mount_prefix=self.symedia_config.get("mount_prefix", ""),
                auth_header=self.symedia_config.get("auth", ""),
                action=action,
                device_name=self.symedia_config.get("device_name", "MoviePilot-Pan123"),
                user_name=self.symedia_config.get("user_name", "admin"),
            )
        except Exception:
            pass

    @staticmethod
    def format_size(size_bytes: int) -> str:
        """友好格式化文件大小"""
        val = float(size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if val < 1024.0:
                return f"{val:.2f} {unit}"
            val /= 1024.0
        return f"{val:.2f} PB"

    def _notify(self, ntype: str, title: str, text: str) -> None:
        """向 MoviePilot 统一通知渠道发送消息"""
        if not self.notify_callback:
            return
        if self.notify_config and not self.notify_config.get("enabled", True):
            return
        if self.notify_config and not self.notify_config.get(f"notify_{ntype}", True):
            return
        try:
            self.notify_callback(title, text)
        except Exception:
            pass

    def start(self) -> None:
        """启动后台工作线程"""
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._run_loop, name="Pan123PilotWorker", daemon=True)
        self._worker_thread.start()

    def stop(self) -> None:
        """安全停止后台工作线程"""
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3)

    @staticmethod
    def calculate_file_hashes(filepath: str, chunk_size: int = 1024 * 1024 * 2) -> tuple[str, str, int]:
        """
        流式分块读取文件并计算 SHA1 与 MD5
        对内存极度友好，同时避免大文件读取 I/O 阻塞
        """
        sha1_hash = hashlib.sha1()
        md5_hash = hashlib.md5()
        total_size = 0

        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                sha1_hash.update(chunk)
                md5_hash.update(chunk)
                total_size += len(chunk)

        return sha1_hash.hexdigest(), md5_hash.hexdigest(), total_size

    def _sync_qb_tag(self, torrent_hash: Optional[str], new_tag: str) -> None:
        """向关联的 qB 种子回写新状态标签"""
        if torrent_hash and self.qb.is_configured():
            try:
                self.qb.update_tags(torrent_hash, new_tag, self.all_plugin_tags)
            except Exception:
                pass

    def process_pending_tasks(self, enable_real_upload: bool = True) -> None:
        """处理新录入或处于待处理状态的任务"""
        pending_tasks = self.db.list_tasks_by_status(statuses=["PENDING"])
        for task in pending_tasks:
            if self._stop_event.is_set():
                break

            task_id = task["id"]
            target_path = task["target_path"]
            torrent_hash = task["torrent_hash"]

            if not os.path.exists(target_path):
                self.db.update_task_status(
                    task_id=task_id, status="FAILED", error_msg="目标文件不存在"
                )
                self._sync_qb_tag(torrent_hash, self.tags.get("failed", "123_秒传失败"))
                continue

            # 1. 标记为处理中
            self.db.update_task_status(task_id=task_id, status="UPLOADING")
            self._sync_qb_tag(torrent_hash, self.tags.get("uploading", "123_上传中"))

            # 2. 计算或复用 SHA1 / MD5
            sha1_str = task.get("sha1")
            md5_str = task.get("md5")
            file_size = task.get("file_size") or 0

            if not sha1_str or not md5_str:
                try:
                    sha1_str, md5_str, file_size = self.calculate_file_hashes(target_path)
                except Exception as e:
                    self.db.update_task_status(
                        task_id=task_id, status="FAILED", error_msg=f"计算哈希失败: {e}"
                    )
                    self._sync_qb_tag(torrent_hash, self.tags.get("failed", "123_秒传失败"))
                    continue

            # 3. 尝试 SHA1 秒传（先计算或级联解析父目录 ID）
            filename = os.path.basename(target_path)
            parent_id = self.resolve_target_parent_id(target_path)

            reuse, file_id, msg = self.client.sha1_reuse(
                filename=filename,
                sha1_str=sha1_str,
                size=file_size,
                parent_file_id=parent_id,
            )

            if reuse:
                # 秒传成功！
                self.db.update_task_status(
                    task_id=task_id,
                    status="COMPLETED",
                    sha1=sha1_str,
                    md5=md5_str,
                    file_id=file_id,
                    parent_file_id=parent_id,
                    error_msg="",
                )
                self._sync_qb_tag(torrent_hash, self.tags.get("completed", "123_已上传"))
                self._trigger_symedia_notify(target_path, action="create")
                self._notify(
                    "upload",
                    "123云盘秒传成功",
                    f"媒体文件【{filename}】已成功零流量秒传至 123 云盘！\n文件大小: {self.format_size(file_size)}\n云盘文件ID: {file_id}",
                )
            else:
                # 秒传未命中，进入重试流转
                retry_count = task.get("retry_count", 0) + 1
                max_retry = task.get("max_retry", 10)

                if retry_count < max_retry:
                    self.db.update_task_status(
                        task_id=task_id,
                        status="RETRYING",
                        sha1=sha1_str,
                        md5=md5_str,
                        retry_increment=True,
                        error_msg=f"第 {retry_count} 次秒传未命中，等待定时重试",
                    )
                    self._sync_qb_tag(torrent_hash, self.tags.get("retrying", "123_待重试"))
                else:
                    # 达到最大重试次数
                    self._handle_max_retry_reached(task, sha1_str, md5_str, enable_real_upload)

    def _handle_max_retry_reached(
        self,
        task: dict,
        sha1_str: str,
        md5_str: str,
        enable_real_upload: bool,
    ) -> None:
        """处理达到重试上限的任务"""
        task_id = task["id"]
        torrent_hash = task["torrent_hash"]
        target_path = task["target_path"]
        parent_id = task.get("parent_file_id", 0)

        if not enable_real_upload:
            err_text = f"达到最大重试次数 ({task.get('max_retry', 10)})，未启用真实上传"
            self.db.update_task_status(
                task_id=task_id,
                status="FAILED",
                sha1=sha1_str,
                md5=md5_str,
                error_msg=err_text,
            )
            self._sync_qb_tag(torrent_hash, self.tags.get("failed", "123_秒传失败"))
            self._notify(
                "failed",
                "123云盘秒传失败",
                f"媒体【{os.path.basename(target_path)}】{err_text}",
            )
            return

        # 启动真实分片上传（先计算或级联解析父目录 ID）
        parent_id = self.resolve_target_parent_id(target_path)
        self._sync_qb_tag(torrent_hash, self.tags.get("uploading", "123_上传中"))
        ok, file_id, err = self.client.upload_file_v2(
            filepath=target_path,
            parent_file_id=parent_id,
            etag=md5_str,
        )

        if ok:
            self.db.update_task_status(
                task_id=task_id,
                status="COMPLETED",
                sha1=sha1_str,
                md5=md5_str,
                file_id=file_id,
                parent_file_id=parent_id,
                error_msg="分片真实上传完成",
            )
            self._sync_qb_tag(torrent_hash, self.tags.get("completed", "123_已上传"))
            self._trigger_symedia_notify(target_path, action="create")
            self._notify(
                "upload",
                "123云盘上传成功",
                f"媒体【{os.path.basename(target_path)}】分片断点真实上传完成！\n文件大小: {self.format_size(task.get('file_size') or 0)}\n云盘文件ID: {file_id}",
            )
        else:
            self.db.update_task_status(
                task_id=task_id,
                status="FAILED",
                sha1=sha1_str,
                md5=md5_str,
                error_msg=f"真实上传失败: {err}",
            )
            self._sync_qb_tag(torrent_hash, self.tags.get("failed", "123_秒传失败"))
            self._notify(
                "failed",
                "123云盘上传失败",
                f"媒体【{os.path.basename(target_path)}】真实上传失败: {err}",
            )

    def retry_retrying_tasks(
        self,
        enable_real_upload: bool = True,
        retry_interval_minutes: int = 0,
    ) -> None:
        """周期性扫描 RETRYING 任务并重新发起 SHA1 秒传检测"""
        retrying_tasks = self.db.list_tasks_by_status(statuses=["RETRYING"])
        for task in retrying_tasks:
            if self._stop_event.is_set():
                break

            task_id = task["id"]
            target_path = task["target_path"]
            torrent_hash = task["torrent_hash"]
            sha1_str = task.get("sha1")
            file_size = task.get("file_size") or 0
            parent_id = task.get("parent_file_id", 0)
            filename = task.get("file_name") or os.path.basename(target_path)

            if not sha1_str:
                continue

            # 校验任务距离上次重试/更新是否已达到设定的重试间隔时间
            if retry_interval_minutes > 0:
                updated_at_val = task.get("updated_at")
                if updated_at_val:
                    try:
                        if isinstance(updated_at_val, datetime):
                            last_time = updated_at_val
                        else:
                            last_time = datetime.strptime(str(updated_at_val), "%Y-%m-%d %H:%M:%S")
                        elapsed_seconds = (datetime.now() - last_time).total_seconds()
                        if elapsed_seconds < retry_interval_minutes * 60:
                            # 距离上次尝试尚未满用户设定的重试间隔时间，暂不触发
                            continue
                    except Exception:
                        pass

            parent_id = self.resolve_target_parent_id(target_path)
            reuse, file_id, _ = self.client.sha1_reuse(
                filename=filename,
                sha1_str=sha1_str,
                size=file_size,
                parent_file_id=parent_id,
            )

            if reuse:
                self.db.update_task_status(
                    task_id=task_id,
                    status="COMPLETED",
                    file_id=file_id,
                    parent_file_id=parent_id,
                    error_msg="重试期间成功命中秒传",
                )
                self._sync_qb_tag(torrent_hash, self.tags.get("completed", "123_已上传"))
                self._trigger_symedia_notify(target_path, action="create")
                self._notify(
                    "upload",
                    "123云盘秒传成功 (重试命中)",
                    f"媒体文件【{filename}】在重试期间成功命中秒传！\n文件大小: {self.format_size(file_size)}\n云盘文件ID: {file_id}",
                )
            else:
                retry_count = task.get("retry_count", 0) + 1
                max_retry = task.get("max_retry", 10)
                if retry_count < max_retry:
                    self.db.update_task_status(
                        task_id=task_id,
                        status="RETRYING",
                        retry_increment=True,
                        error_msg=f"第 {retry_count} 次秒传仍未命中",
                    )
                else:
                    self._handle_max_retry_reached(
                        task, sha1_str, task.get("md5") or "", enable_real_upload
                    )

    def cleanup_completed_tasks(
        self,
        min_seed_time_hours: float = 48.0,
        min_ratio: float = 2.0,
        delete_torrent_file: bool = True,
        delete_hardlink_file: bool = True,
    ) -> None:
        """
        自动清理已同步完成且满足做种指标的任务
        """
        completed_tasks = self.db.list_tasks_by_status(statuses=["COMPLETED"])
        for task in completed_tasks:
            if self._stop_event.is_set():
                break

            task_id = task["id"]
            torrent_hash = task.get("torrent_hash")
            target_path = task.get("target_path")

            # 1. 检查做种条件是否达标
            qualified = True
            reason = "无对应 QB 种子"
            if torrent_hash and self.qb.is_configured():
                qualified, reason = self.qb.check_seeding_criteria(
                    torrent_hash=torrent_hash,
                    min_seed_time_hours=min_seed_time_hours,
                    min_ratio=min_ratio,
                )

            if not qualified:
                continue

            # 2. 清理 QB 种子与源文件
            if torrent_hash and delete_torrent_file and self.qb.is_configured():
                try:
                    self.qb.delete_torrent(torrent_hash, delete_files=True)
                except Exception:
                    pass

            # 3. 清理本地硬链接文件并递归修剪空目录
            if delete_hardlink_file and target_path and os.path.exists(target_path):
                try:
                    os.remove(target_path)
                    # 向上递归清理各级空父目录（直至媒体库根目录或遇到非空目录）
                    parent_dir = os.path.dirname(target_path)
                    norm_root = os.path.normpath(self.local_library_root) if self.local_library_root else None
                    while parent_dir and os.path.exists(parent_dir):
                        if norm_root and os.path.abspath(parent_dir) == os.path.abspath(norm_root):
                            break
                        if not os.listdir(parent_dir):
                            os.rmdir(parent_dir)
                            parent_dir = os.path.dirname(parent_dir)
                        else:
                            break
                except Exception:
                    pass

            # 4. 更新数据库状态为 CLEANED
            self.db.update_task_status(
                task_id=task_id,
                status="CLEANED",
                error_msg=f"做种达标并已完成自动清理: {reason}",
            )
            self._notify(
                "cleanup",
                "123云盘做种达标清理完成",
                (
                    f"媒体【{os.path.basename(target_path)}】已在 123 云盘就绪且满足做种要求！\n"
                    f"做种考核结果: {reason}\n"
                    f"清理动作: {'已删除 qBittorrent 种子与源文件 ' if delete_torrent_file else ''}"
                    f"{'已删除本地刮削硬链接文件' if delete_hardlink_file else ''}"
                ),
            )

    def _run_loop(self) -> None:
        """后台轮询主循环"""
        while not self._stop_event.is_set():
            try:
                if self.client.is_configured():
                    self.process_pending_tasks()
            except Exception:
                pass
            # 每隔 10 秒唤醒一次检测是否有新增的待处理任务
            for _ in range(10):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

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

try:
    from app.log import logger
except ImportError:
    import logging
    logger = logging.getLogger("pan123pilot")

from .client import Pan123Client
from .models import Pan123TaskDatabase
from .qb_helper import QBHelper
from .symedia import send_symedia_file_notify

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m2ts",
    ".iso", ".rmvb", ".webm", ".strm",
}
SCRAPED_EXTENSIONS = {
    ".nfo", ".xml",
    ".srt", ".vtt", ".ass", ".ssa", ".sub", ".idx",
    ".jpg", ".jpeg", ".png", ".webp", ".tbn",
}


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
        on_token_expired: Optional[Callable[[str], None]] = None,
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
        self.on_token_expired = on_token_expired

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._process_lock = threading.Lock()
        self._retry_lock = threading.Lock()
        self._recheck_pending = False

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
                ok, dir_id, err = self.client.get_or_create_dir_by_path(
                    root_parent_id=root_id,
                    relative_dir_path=rel_dir,
                )
                if ok and dir_id > 0:
                    logger.info(f"Pan123Pilot 目录级联成功: 本地【{rel_dir}】-> 123云盘 ParentID: {dir_id}")
                    return dir_id
                else:
                    logger.error(
                        f"Pan123Pilot 目录级联未完全命中: 本地【{rel_dir}】在 123 云盘递归创建/查找失败: {err}，将回退至根 ParentID: {root_id}"
                    )
        return root_id

    def _trigger_symedia_notify(self, target_path: str, action: str = "create") -> None:
        """若启用了 Symedia 联动，发送 Webhook 通知即时生成或清理 strm 及同步刮削伴生元数据"""
        if not self.symedia_config or not self.symedia_config.get("enabled"):
            return
        server_url = self.symedia_config.get("url", "")
        if not server_url:
            return
        try:
            ok, msg = send_symedia_file_notify(
                server_url=server_url,
                target_path=target_path,
                local_library_root=self.local_library_root,
                mount_prefix=self.symedia_config.get("mount_prefix", ""),
                auth_header=self.symedia_config.get("auth", ""),
                action=action,
                device_name=self.symedia_config.get("device_name", "MoviePilot-Pan123"),
                user_name=self.symedia_config.get("user_name", "admin"),
            )
            if ok:
                logger.info(f"Pan123Pilot {msg}")
            else:
                logger.warning(f"Pan123Pilot {msg}")
        except Exception as e:
            logger.warning(f"Pan123Pilot 触发 Symedia Webhook 异常 [{os.path.basename(target_path)}]: {e}")

    def _handle_token_expired(self, reason: str = "") -> None:
        """统一处理 123 云盘 Web Token 过期事件，触发回调并不扣减任务重试上限"""
        logger.error(f"Pan123Pilot 触发 Token 过期拦截: {reason}")
        if self.on_token_expired:
            try:
                self.on_token_expired(reason)
            except Exception as e:
                logger.error(f"Pan123Pilot 调用 on_token_expired 回调失败: {e}")

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
    def calculate_file_hashes(
        filepath: str,
        chunk_size: int = 1024 * 1024 * 8,
        need_sha1: bool = True,
        need_md5: bool = True,
    ) -> tuple[str, str, int]:
        """
        流式分块读取文件并按需计算 SHA1 与 MD5
        优先利用 Python 3.11+ C 语言级 hashlib.file_digest 释放 GIL 高速计算
        """
        total_size = os.path.getsize(filepath)
        if hasattr(hashlib, "file_digest"):
            if need_md5 and not need_sha1:
                with open(filepath, "rb") as f:
                    md5_hex = hashlib.file_digest(f, "md5").hexdigest()
                return md5_hex, md5_hex, total_size
            if need_sha1 and not need_md5:
                with open(filepath, "rb") as f:
                    sha1_hex = hashlib.file_digest(f, "sha1").hexdigest()
                return sha1_hex, "", total_size

        sha1_hash = hashlib.sha1() if need_sha1 else None
        md5_hash = hashlib.md5() if need_md5 else None
        read_size = 0

        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                if sha1_hash is not None:
                    sha1_hash.update(chunk)
                if md5_hash is not None:
                    md5_hash.update(chunk)
                read_size += len(chunk)

        sha1_res = sha1_hash.hexdigest() if sha1_hash is not None else ""
        md5_res = md5_hash.hexdigest() if md5_hash is not None else ""
        if not sha1_res and md5_res:
            sha1_res = md5_res
        return sha1_res, md5_res, read_size or total_size

    def _sync_qb_tag(self, torrent_hash: Optional[str], new_tag: str, tag_cache: Optional[dict[str, str]] = None) -> None:
        """向关联的 qB 种子回写新状态标签（支持批次内去重缓存）"""
        if not torrent_hash or not new_tag or not self.qb.is_configured():
            return
        if tag_cache is not None and tag_cache.get(torrent_hash) == new_tag:
            return
        try:
            self.qb.update_tags(torrent_hash, new_tag, self.all_plugin_tags)
            if tag_cache is not None:
                tag_cache[torrent_hash] = new_tag
        except Exception:
            pass

    def process_pending_tasks(self, enable_real_upload: bool = True) -> None:
        """
        单飞互斥处理待同步任务队列：
        若已有工作线程正在处理，则仅设置 _recheck_pending 标记并立即返回，
        彻底避免多集剧集并发触发时多线程同时读盘计算哈希导致 I/O 踩踏
        """
        if not self._process_lock.acquire(blocking=False):
            self._recheck_pending = True
            logger.debug("Pan123Pilot 队列正在处理中，已合并本次触发请求至当前工作线程")
            return

        try:
            while not self._stop_event.is_set():
                self._recheck_pending = False
                self._process_pending_batch(enable_real_upload=enable_real_upload)
                if not self._recheck_pending:
                    break
        finally:
            self._process_lock.release()

    def _process_pending_batch(self, enable_real_upload: bool = True) -> None:
        """执行单批次待处理任务（正片视频优先，刮削伴生文件其次）"""
        pending_tasks = self.db.list_tasks_by_status(statuses=["PENDING"])
        if not pending_tasks:
            return

        # 视频正片优先秒传，刮削伴生小文件排在后面，确保 29 集剧集正片极速秒传完成
        pending_tasks.sort(
            key=lambda t: (
                0 if os.path.splitext(str(t.get("target_path") or ""))[1].lower() in VIDEO_EXTENSIONS else 1,
                int(t.get("id") or 0),
            )
        )

        is_web_mode = getattr(self.client, "auth_mode", "") in ("web_token", "web_account", "web_sms")
        batch_tag_cache: dict[str, str] = {}

        logger.info(f"Pan123Pilot 待处理任务队列共 {len(pending_tasks)} 个媒体任务待执行")
        for task in pending_tasks:
            if self._stop_event.is_set():
                break

            task_id = task["id"]
            target_path = task["target_path"]
            filename = os.path.basename(target_path)
            ext = os.path.splitext(filename)[1].lower()
            is_companion = ext in SCRAPED_EXTENSIONS
            # 刮削伴生文件不关联与操作 qB 种子标签
            torrent_hash = None if is_companion else task.get("torrent_hash")

            if not os.path.exists(target_path):
                logger.error(f"Pan123Pilot 任务【{filename}】(ID: {task_id}) 目标文件在磁盘上不存在: {target_path}")
                self.db.update_task_status(
                    task_id=task_id, status="FAILED", error_msg="目标文件不存在"
                )
                if not is_companion:
                    self._sync_qb_tag(torrent_hash, self.tags.get("failed", "123_秒传失败"), batch_tag_cache)
                continue

            logger.info(f"Pan123Pilot 开始执行待同步任务: 【{filename}】(ID: {task_id})")

            # 1. 标记为处理中
            self.db.update_task_status(task_id=task_id, status="UPLOADING")
            if not is_companion:
                self._sync_qb_tag(torrent_hash, self.tags.get("uploading", "123_上传中"), batch_tag_cache)

            # 2. 计算或复用哈希 (Web 模式仅需计算 MD5，利用 C 级 file_digest 提速 2~3 倍)
            sha1_str = task.get("sha1")
            md5_str = task.get("md5")
            file_size = task.get("file_size") or 0

            if not sha1_str and not md5_str:
                try:
                    t0 = time.time()
                    hash_label = "MD5" if is_web_mode else "SHA1/MD5"
                    logger.info(f"Pan123Pilot 正在计算哈希 ({hash_label}): 【{filename}】...")
                    sha1_str, md5_str, file_size = self.calculate_file_hashes(
                        target_path,
                        need_sha1=not is_web_mode,
                        need_md5=True,
                    )
                    elapsed = time.time() - t0
                    logger.info(
                        f"Pan123Pilot 哈希计算完成: 【{filename}】(Hash: {(md5_str or sha1_str)[:8]}..., "
                        f"大小: {self.format_size(file_size)}, 耗时: {elapsed:.2f}s)"
                    )
                except Exception as e:
                    logger.error(f"Pan123Pilot 计算哈希失败: 【{filename}】: {e}")
                    self.db.update_task_status(
                        task_id=task_id, status="FAILED", error_msg=f"计算哈希失败: {e}"
                    )
                    self._sync_qb_tag(torrent_hash, self.tags.get("failed", "123_秒传失败"), batch_tag_cache)
                    continue

            # 3. 尝试秒传（先计算或级联解析父目录 ID）
            parent_id = self.resolve_target_parent_id(target_path)
            logger.info(f"Pan123Pilot 发起 123 云盘秒传: 【{filename}】(云盘 ParentID: {parent_id})...")

            try:
                reuse, file_id, msg = self.client.sha1_reuse(
                    filename=filename,
                    sha1_str=sha1_str or md5_str or "",
                    size=file_size,
                    parent_file_id=parent_id,
                    md5_str=md5_str or sha1_str,
                )
            except TypeError:
                reuse, file_id, msg = self.client.sha1_reuse(
                    filename=filename,
                    sha1_str=sha1_str or md5_str or "",
                    size=file_size,
                    parent_file_id=parent_id,
                )

            if reuse:
                # 秒传成功！无论正片视频还是刮削伴生文件，均触发 Symedia Webhook 联动
                logger.info(f"Pan123Pilot 【{filename}】秒传成功！云盘 FileID: {file_id}")
                self.db.update_task_status(
                    task_id=task_id,
                    status="COMPLETED",
                    sha1=sha1_str,
                    md5=md5_str,
                    file_id=file_id,
                    parent_file_id=parent_id,
                    error_msg="",
                )
                self._trigger_symedia_notify(target_path, action="create")
                if not is_companion:
                    self._sync_qb_tag(torrent_hash, self.tags.get("completed", "123_已上传"), batch_tag_cache)
                    self._notify(
                        "upload",
                        "123云盘秒传成功",
                        f"媒体文件【{filename}】已成功零流量秒传至 123 云盘！\n文件大小: {self.format_size(file_size)}\n云盘文件ID: {file_id}",
                    )
            else:
                # 若因 Token 过期导致鉴权失败，不消耗重试次数并触发报警
                if "TOKEN_EXPIRED" in str(msg):
                    logger.error(f"Pan123Pilot 【{filename}】秒传中断: Web Token 已过期，将任务保留在重试队列中")
                    self.db.update_task_status(
                        task_id=task_id,
                        status="RETRYING",
                        sha1=sha1_str,
                        md5=md5_str,
                        error_msg="123云盘 Web Token 已过期，等待更新 Token 后自动恢复",
                    )
                    self._sync_qb_tag(torrent_hash, self.tags.get("retrying", "123_待重试"), batch_tag_cache)
                    self._handle_token_expired(str(msg))
                    break

                # 针对刮削伴生文件（字幕、海报、NFO 等小文件），秒传未命中时无需轮询等待，直接启动极速上传
                ext = os.path.splitext(filename)[1].lower()
                if ext in SCRAPED_EXTENSIONS or file_size < 10 * 1024 * 1024:
                    if enable_real_upload:
                        logger.info(f"Pan123Pilot 【{filename}】为刮削伴生小文件 ({self.format_size(file_size)})，秒传未命中立即执行快速真实上传...")
                        self._handle_max_retry_reached(task, sha1_str or "", md5_str or "", enable_real_upload=True)
                        continue

                # 正常大文件秒传未命中，进入重试流转
                retry_count = task.get("retry_count", 0) + 1
                max_retry = task.get("max_retry", 10)
                logger.warning(
                    f"Pan123Pilot 【{filename}】秒传未命中 ({msg})，进入重试流转 (进度: {retry_count}/{max_retry})"
                )

                if retry_count < max_retry:
                    self.db.update_task_status(
                        task_id=task_id,
                        status="RETRYING",
                        sha1=sha1_str,
                        md5=md5_str,
                        retry_increment=True,
                        error_msg=f"第 {retry_count} 次秒传未命中，等待定时重试",
                    )
                    self._sync_qb_tag(torrent_hash, self.tags.get("retrying", "123_待重试"), batch_tag_cache)
                else:
                    # 达到最大重试次数
                    self._handle_max_retry_reached(task, sha1_str or "", md5_str or "", enable_real_upload)

    def _handle_max_retry_reached(
        self,
        task: dict,
        sha1_str: str,
        md5_str: str,
        enable_real_upload: bool,
    ) -> None:
        """处理达到重试上限的任务"""
        task_id = task["id"]
        target_path = task["target_path"]
        is_companion = os.path.splitext(target_path)[1].lower() in SCRAPED_EXTENSIONS
        torrent_hash = None if is_companion else task.get("torrent_hash")
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
            if not is_companion:
                self._sync_qb_tag(torrent_hash, self.tags.get("failed", "123_秒传失败"))
                self._notify(
                    "failed",
                    "123云盘秒传失败",
                    f"媒体【{os.path.basename(target_path)}】{err_text}",
                )
            return

        # 启动真实分片上传（先计算或级联解析父目录 ID）
        parent_id = self.resolve_target_parent_id(target_path)
        logger.info(f"Pan123Pilot 【{os.path.basename(target_path)}】达到重试上限，启动 123 分片真实上传 (ParentID: {parent_id})...")
        if not is_companion:
            self._sync_qb_tag(torrent_hash, self.tags.get("uploading", "123_上传中"))

        if not hasattr(self.client, "upload_file_v2"):
            ok, file_id, err = False, 0, "当前客户端未实现分片真实上传"
        else:
            try:
                ok, file_id, err = self.client.upload_file_v2(
                    filepath=target_path,
                    parent_file_id=parent_id,
                    etag=md5_str or sha1_str,
                )
            except Exception as e:
                ok, file_id, err = False, 0, str(e)

        if ok:
            logger.info(f"Pan123Pilot 【{os.path.basename(target_path)}】分片真实上传完成！云盘 FileID: {file_id}")
            self.db.update_task_status(
                task_id=task_id,
                status="COMPLETED",
                sha1=sha1_str,
                md5=md5_str,
                file_id=file_id,
                parent_file_id=parent_id,
                error_msg="分片真实上传完成",
            )
            # 无论正片视频还是刮削伴生文件，上传完成后均触发 Symedia Webhook 同步
            self._trigger_symedia_notify(target_path, action="create")
            if not is_companion:
                self._sync_qb_tag(torrent_hash, self.tags.get("completed", "123_已上传"))
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
            if not is_companion:
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
        """周期性扫描 RETRYING 任务并重新发起 SHA1 秒传检测（带单飞互斥锁）"""
        if not self._retry_lock.acquire(blocking=False):
            return

        try:
            retrying_tasks = self.db.list_tasks_by_status(statuses=["RETRYING"])
            for task in retrying_tasks:
                if self._stop_event.is_set():
                    break

                task_id = task["id"]
                target_path = task["target_path"]
                torrent_hash = task["torrent_hash"]
                sha1_str = task.get("sha1")
                md5_str = task.get("md5")
                file_size = task.get("file_size") or 0
                parent_id = task.get("parent_file_id", 0)
                filename = task.get("file_name") or os.path.basename(target_path)

                if not sha1_str and not md5_str:
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
                try:
                    reuse, file_id, msg = self.client.sha1_reuse(
                        filename=filename,
                        sha1_str=sha1_str or md5_str or "",
                        size=file_size,
                        parent_file_id=parent_id,
                        md5_str=md5_str or sha1_str,
                    )
                except TypeError:
                    reuse, file_id, msg = self.client.sha1_reuse(
                        filename=filename,
                        sha1_str=sha1_str or md5_str or "",
                        size=file_size,
                        parent_file_id=parent_id,
                    )

                if reuse:
                    is_companion = os.path.splitext(filename)[1].lower() in SCRAPED_EXTENSIONS
                    self.db.update_task_status(
                        task_id=task_id,
                        status="COMPLETED",
                        file_id=file_id,
                        parent_file_id=parent_id,
                        error_msg="重试期间成功命中秒传",
                    )
                    self._trigger_symedia_notify(target_path, action="create")
                    if not is_companion:
                        self._sync_qb_tag(torrent_hash, self.tags.get("completed", "123_已上传"))
                        self._notify(
                            "upload",
                            "123云盘秒传成功 (重试命中)",
                            f"媒体文件【{filename}】在重试期间成功命中秒传！\n文件大小: {self.format_size(file_size)}\n云盘文件ID: {file_id}",
                        )
                else:
                    if "TOKEN_EXPIRED" in str(msg):
                        logger.error("Pan123Pilot 周期重试中断: 123云盘 Web Token 已过期，暂停重试轮询")
                        self.db.update_task_status(
                            task_id=task_id,
                            status="RETRYING",
                            error_msg="123云盘 Web Token 已过期，等待更新 Token 后自动恢复",
                        )
                        self._handle_token_expired(str(msg))
                        break

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
                            task, sha1_str or "", md5_str or "", enable_real_upload
                        )
        finally:
            self._retry_lock.release()

    def cleanup_completed_tasks(
        self,
        min_seed_time_hours: float = 48.0,
        min_ratio: float = 2.0,
        delete_torrent_file: bool = True,
        delete_hardlink_file: bool = True,
        emergency_free_space_gb: float = 5.0,
    ) -> None:
        """
        自动清理已同步完成且满足做种指标的任务。
        支持实时监控 qB 磁盘空间：当剩余空间不足阈值时，自动触发紧急空间腾退，无视做种指标强制清理。
        """
        # 0. 检查 qBittorrent 磁盘剩余空间
        is_emergency = False
        current_free_gb = 9999.0
        if self.qb and emergency_free_space_gb > 0:
            free_bytes = self.qb.get_free_space_bytes(local_path=self.local_library_root)
            current_free_gb = free_bytes / (1024.0 ** 3)
            if current_free_gb < emergency_free_space_gb:
                is_emergency = True
                logger.warning(
                    f"🚨 Pan123Pilot 检测到 qBittorrent 磁盘剩余空间仅剩 {current_free_gb:.2f} GB (低于安全阈值 {emergency_free_space_gb:.1f} GB)，启动紧急磁盘腾退机制！"
                )

        completed_tasks = self.db.list_tasks_by_status(statuses=["COMPLETED"])
        for task in completed_tasks:
            if self._stop_event.is_set():
                break

            task_id = task["id"]
            torrent_hash = task.get("torrent_hash")
            target_path = task.get("target_path") or ""
            fname_check = str(task.get("file_name") or target_path)
            ext_check = os.path.splitext(fname_check)[1].lower()

            # 核心规则：刮削伴生文件 (.nfo/.jpg/.png/.srt 等) 绝不独立关联或触发 qB 任务清理！
            # 只有正片视频任务才执行 qB 做种考核与任务清理
            if ext_check in SCRAPED_EXTENSIONS:
                continue

            # 1. 检查做种条件是否达标（若触发紧急空间腾退，则无视指标直接强制放行）
            qualified = True
            reason = "无对应 QB 种子"
            if is_emergency:
                qualified = True
                reason = f"🚨 磁盘空间告急 (剩余 {current_free_gb:.2f}GB < 阈值 {emergency_free_space_gb:.1f}GB)，紧急腾退强制清理"
            elif torrent_hash and self.qb.is_configured():
                qualified, reason = self.qb.check_seeding_criteria(
                    torrent_hash=torrent_hash,
                    min_seed_time_hours=min_seed_time_hours,
                    min_ratio=min_ratio,
                )

            if not qualified:
                continue

            # 安全防护：若同级目录下仍存在未完成同步的刮削伴生文件，暂缓清理本地与种子
            parent_dir = os.path.dirname(target_path) if target_path else None
            if parent_dir and os.path.exists(parent_dir) and not is_emergency:
                has_pending_companions = False
                for fname in os.listdir(parent_dir):
                    fpath = os.path.join(parent_dir, fname)
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in SCRAPED_EXTENSIONS:
                        c_task = self.db.get_task_by_target(target_path=fpath)
                        # 若已有任务但状态仍为处理中或重试中
                        if c_task and c_task.get("status") in ("PENDING", "UPLOADING", "RETRYING"):
                            has_pending_companions = True
                            break
                        # 若本地存在伴生文件但尚未入库同步（如刮削完成稍晚），立即自动补充入库并暂缓本次清理
                        elif not c_task and os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
                            has_pending_companions = True
                            self.db.add_task(
                                target_path=fpath,
                                torrent_hash=None,
                                status="PENDING",
                            )
                            logger.info(f"Pan123Pilot 补充发现尚未同步的刮削伴生文件，自动录入同步队列: {fname}")
                            break
                if has_pending_companions:
                    logger.info(f"Pan123Pilot 同目录下尚有刮削伴生文件未完成同步，暂不执行清理: {target_path}")
                    continue

            # 2. 清理 QB 种子与源文件
            if torrent_hash and delete_torrent_file and self.qb.is_configured():
                try:
                    self.qb.delete_torrent(torrent_hash, delete_files=True)
                except Exception:
                    pass

            # 3. 清理本地硬链接文件、伴生刮削文件（字幕/海报/NFO）并递归修剪空目录
            if delete_hardlink_file and target_path and os.path.exists(target_path):
                try:
                    os.remove(target_path)
                    # 清理同级伴生刮削文件（同名前缀的 .nfo, .srt, .ass, .jpg 等）
                    parent_dir = os.path.dirname(target_path)
                    base_prefix = os.path.splitext(target_path)[0]
                    if os.path.exists(parent_dir):
                        for fname in os.listdir(parent_dir):
                            fpath = os.path.join(parent_dir, fname)
                            ext = os.path.splitext(fname)[1].lower()
                            if ext in SCRAPED_EXTENSIONS:
                                if fpath.startswith(base_prefix) or fname.lower() in (
                                    "folder.jpg", "poster.jpg", "fanart.jpg", "backdrop.jpg", "season.nfo", "tvshow.nfo"
                                ):
                                    try:
                                        # 公共海报文件：若目录下已无其他视频文件则一并清理
                                        if fname.lower() in ("folder.jpg", "poster.jpg", "fanart.jpg", "backdrop.jpg", "season.nfo", "tvshow.nfo"):
                                            has_other_videos = any(
                                                os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
                                                for f in os.listdir(parent_dir)
                                                if os.path.join(parent_dir, f) != target_path
                                            )
                                            if not has_other_videos:
                                                os.remove(fpath)
                                        else:
                                            os.remove(fpath)
                                    except Exception:
                                        pass

                    # 向上递归清理各级空父目录（直至媒体库根目录或遇到非空目录）
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
                error_msg=f"已完成自动清理: {reason}",
            )
            notify_title = "123云盘空间紧急腾退完成" if is_emergency else "123云盘做种达标清理完成"
            self._notify(
                "cleanup",
                notify_title,
                (
                    f"媒体【{os.path.basename(target_path)}】已在 123 云盘就绪并执行清理！\n"
                    f"清理结果: {reason}\n"
                    f"清理动作: {'已删除 qBittorrent 种子与源文件 ' if delete_torrent_file else ''}"
                    f"{'已删除本地刮削硬链接及伴生文件' if delete_hardlink_file else ''}"
                ),
            )

    def _run_loop(self) -> None:
        """后台轮询主循环"""
        while not self._stop_event.is_set():
            try:
                is_conf = False
                if hasattr(self.client, "is_configured"):
                    is_conf = self.client.is_configured()
                else:
                    is_conf = bool(getattr(self.client, "token", None))
                if is_conf:
                    self.process_pending_tasks()
            except Exception as e:
                logger.error(f"Pan123Pilot 后台主循环异常: {e}")
            # 每隔 10 秒唤醒一次检测是否有新增的待处理任务
            for _ in range(10):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

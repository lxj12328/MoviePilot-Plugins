"""
Pan123Pilot 本地任务持久化数据模型
使用 SQLAlchemy 2.0 规范，提供任务存储与状态跟踪
"""

from datetime import datetime, timedelta
import os
from typing import Any, Optional

MODEL_VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m2ts",
    ".iso", ".rmvb", ".webm", ".strm",
)
MODEL_SCRAPED_EXTENSIONS = (
    ".nfo", ".xml",
    ".srt", ".vtt", ".ass", ".ssa", ".sub", ".idx",
    ".jpg", ".jpeg", ".png", ".webp", ".tbn",
)

try:
    from sqlalchemy import BigInteger, DateTime, Integer, String, Text, delete, func, or_, select
    from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
except ImportError:
    class DummyColType:
        def __init__(self, *args, **kwargs): pass
        def __call__(self, *args, **kwargs): return self

    Mapped = Any  # type: ignore
    mapped_column = lambda *args, **kwargs: None  # type: ignore
    Integer = String = BigInteger = DateTime = Text = DummyColType  # type: ignore
    select = delete = func = or_ = lambda *args, **kwargs: None  # type: ignore
    Session = Any  # type: ignore

try:
    from app.db import Base, db_query, db_update
except ImportError:
    try:
        class DeclarativeBaseFallback(DeclarativeBase):
            pass
    except Exception:
        class DeclarativeBaseFallback:  # type: ignore
            pass

    Base = DeclarativeBaseFallback  # type: ignore
    db_query = lambda func: func
    db_update = lambda func: func


class Pan123Task(Base):
    """123云盘上传与清理任务表"""

    __tablename__ = "plugin_pan123_task"
    __table_args__ = {"extend_existing": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_id: Mapped[str] = mapped_column(String(100), nullable=False, default="Pan123Pilot", index=True)
    torrent_hash: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    source_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    target_path: Mapped[str] = mapped_column(String(1000), nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha1: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    md5: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    file_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    parent_file_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="PENDING", index=True)
    # 状态枚举: PENDING(待处理), UPLOADING(处理/上传中), RETRYING(等待重试), FAILED(失败), COMPLETED(已上传), CLEANED(已清理)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Pan123TaskDatabase:
    """任务数据库操作管理封装"""

    @staticmethod
    def ensure_table() -> None:
        """按需创建本插件的自有表"""
        try:
            from app.db import Engine
            if hasattr(Pan123Task, "__table__") and Engine is not None:
                Pan123Task.__table__.create(bind=Engine, checkfirst=True)
        except Exception:
            pass

    @db_update
    def add_or_update_task(
        self,
        db: Optional[Session] = None,
        *,
        target_path: str,
        file_name: str,
        file_size: int,
        torrent_hash: Optional[str] = None,
        source_path: Optional[str] = None,
        parent_file_id: int = 0,
        max_retry: int = 10,
        force_reset: bool = False,
    ) -> int:
        """添加新任务或更新已有任务信息"""
        if db is None:
            return 0
        task = db.execute(
            select(Pan123Task).where(Pan123Task.target_path == target_path)
        ).scalar_one_or_none()

        if task:
            if torrent_hash:
                task.torrent_hash = torrent_hash
            if source_path:
                task.source_path = source_path
            task.file_size = file_size
            task.parent_file_id = parent_file_id
            task.max_retry = max_retry
            task.updated_at = datetime.now()
            # 若已有任务处于未完成且非正在上传状态，或要求强制重置，则重置为 PENDING 立即触发流转
            if force_reset or task.status not in ("COMPLETED", "CLEANED", "UPLOADING"):
                task.status = "PENDING"
                task.retry_count = 0
                task.error_msg = ""
        else:
            task = Pan123Task(
                target_path=target_path,
                file_name=file_name,
                file_size=file_size,
                torrent_hash=torrent_hash,
                source_path=source_path,
                parent_file_id=parent_file_id,
                max_retry=max_retry,
                status="PENDING",
            )
            db.add(task)
        db.flush()
        return task.id

    def add_task(
        self,
        *,
        target_path: str,
        torrent_hash: Optional[str] = None,
        status: str = "PENDING",
        file_name: Optional[str] = None,
        file_size: int = 0,
        parent_file_id: int = 0,
    ) -> int:
        """兼容快捷录入方法"""
        import os
        fname = file_name or os.path.basename(target_path)
        fsize = file_size or (os.path.getsize(target_path) if os.path.exists(target_path) else 0)
        return self.add_or_update_task(
            target_path=target_path,
            file_name=fname,
            file_size=fsize,
            torrent_hash=torrent_hash,
            parent_file_id=parent_file_id,
        )

    @db_update
    def reset_tasks_status(
        self,
        db: Optional[Session] = None,
        *,
        statuses: Optional[list[str]] = None,
        to_status: str = "PENDING",
    ) -> int:
        """重置指定或全部任务状态为 PENDING，并清空重试计数以便全量重新流转"""
        if db is None:
            return 0
        stmt = select(Pan123Task)
        if statuses:
            stmt = stmt.where(Pan123Task.status.in_(statuses))
        records = db.execute(stmt).scalars().all()
        count = 0
        for task in records:
            task.status = to_status
            task.retry_count = 0
            task.error_msg = ""
            task.updated_at = datetime.now()
            count += 1
        db.flush()
        return count

    @db_update
    def update_task_status(
        self,
        db: Optional[Session] = None,
        *,
        task_id: int,
        status: str,
        sha1: Optional[str] = None,
        md5: Optional[str] = None,
        file_id: Optional[int] = None,
        parent_file_id: Optional[int] = None,
        retry_increment: bool = False,
        error_msg: Optional[str] = None,
    ) -> bool:
        """更新任务状态及关键字段"""
        if db is None:
            return False
        task = db.get(Pan123Task, task_id)
        if not task:
            return False
        task.status = status
        if sha1 is not None:
            task.sha1 = sha1
        if md5 is not None:
            task.md5 = md5
        if file_id is not None:
            task.file_id = file_id
        if parent_file_id is not None:
            task.parent_file_id = parent_file_id
        if retry_increment:
            task.retry_count += 1
        if error_msg is not None:
            task.error_msg = error_msg
        task.updated_at = datetime.now()
        db.flush()
        return True

    @db_query
    def get_task(self, db: Optional[Session] = None, *, task_id: int) -> Optional[dict]:
        """获取单个任务字典"""
        if db is None:
            return None
        task = db.get(Pan123Task, task_id)
        return self._to_dict(task) if task else None

    @db_query
    def get_task_by_target(self, db: Optional[Session] = None, *, target_path: str) -> Optional[dict]:
        """根据目标路径查找任务"""
        if db is None:
            return None
        task = db.execute(
            select(Pan123Task).where(Pan123Task.target_path == target_path)
        ).scalar_one_or_none()
        return self._to_dict(task) if task else None

    @db_query
    def list_tasks_by_status(self, db: Optional[Session] = None, *, statuses: list[str]) -> list[dict]:
        """按状态列表筛选任务"""
        if db is None:
            return []
        records = db.execute(
            select(Pan123Task).where(Pan123Task.status.in_(statuses)).order_by(Pan123Task.id.asc())
        ).scalars().all()
        return [self._to_dict(r) for r in records]

    @db_query
    def list_all_tasks(self, db: Optional[Session] = None, *, limit: int = 50) -> list[dict]:
        """获取最近任务列表"""
        if db is None:
            return []
        records = db.execute(
            select(Pan123Task).order_by(Pan123Task.updated_at.desc(), Pan123Task.id.desc()).limit(limit)
        ).scalars().all()
        return [self._to_dict(r) for r in records]

    @db_update
    def fix_companion_records(self, db: Optional[Session] = None) -> int:
        """
        自动修正历史存量刮削伴生文件记录：
        1. 剥离伴生文件上误绑定的 torrent_hash
        2. 将伴生文件因「无对应 QB 种子」被误标为 CLEANED 的记录恢复为 COMPLETED 并清除误导文案
        """
        if db is None:
            return 0
        fixed = 0
        try:
            records = db.execute(select(Pan123Task)).scalars().all()
            for task in records:
                fname = str(task.file_name or task.target_path or "")
                ext = os.path.splitext(fname)[1].lower()
                if ext in MODEL_SCRAPED_EXTENSIONS:
                    changed = False
                    if task.torrent_hash:
                        task.torrent_hash = None
                        changed = True
                    err = str(task.error_msg or "")
                    if "无对应 QB 种子" in err:
                        task.error_msg = ""
                        if task.status == "CLEANED" and (task.file_id or not os.path.exists(str(task.target_path or ""))):
                            task.status = "COMPLETED"
                        changed = True
                    if changed:
                        fixed += 1
            if fixed > 0:
                db.flush()
        except Exception:
            pass
        return fixed

    @staticmethod
    def _apply_query_filters(stmt: Any, keyword: str = "", status: str = "", file_type: str = "all") -> Any:
        """统一拼接 SQL 过滤条件 (支持正片/伴生类型、状态组、关键词模糊匹配)"""
        ft = (file_type or "all").strip().lower()
        if ft == "video":
            stmt = stmt.where(or_(*(Pan123Task.file_name.ilike(f"%{ext}") for ext in MODEL_VIDEO_EXTENSIONS)))
        elif ft == "companion":
            stmt = stmt.where(or_(*(Pan123Task.file_name.ilike(f"%{ext}") for ext in MODEL_SCRAPED_EXTENSIONS)))

        st = (status or "").strip().upper()
        if st and st != "ALL":
            if st == "PENDING_GROUP":
                stmt = stmt.where(Pan123Task.status.in_(["PENDING", "UPLOADING", "RETRYING"]))
            elif st == "COMPLETED_GROUP":
                stmt = stmt.where(Pan123Task.status.in_(["COMPLETED", "CLEANED"]))
            else:
                stmt = stmt.where(Pan123Task.status == st)

        if keyword and keyword.strip():
            kw = f"%{keyword.strip()}%"
            stmt = stmt.where(
                or_(
                    Pan123Task.file_name.ilike(kw),
                    Pan123Task.target_path.ilike(kw),
                    Pan123Task.error_msg.ilike(kw),
                )
            )
        return stmt

    @db_query
    def get_status_counts(self, db: Optional[Session] = None, *, file_type: str = "all") -> dict[str, int]:
        """聚合统计各状态任务数量及正片/伴生分类数量"""
        counts = {
            "TOTAL": 0,
            "VIDEO_TOTAL": 0,
            "COMPANION_TOTAL": 0,
            "PENDING": 0,
            "UPLOADING": 0,
            "RETRYING": 0,
            "COMPLETED": 0,
            "CLEANED": 0,
            "FAILED": 0,
        }
        if db is None:
            return counts
        try:
            # 1. 统计正片与伴生总数
            all_rows = db.execute(select(Pan123Task.file_name, Pan123Task.status)).all()
            ft = (file_type or "all").strip().lower()
            total = 0
            v_total = 0
            c_total = 0
            for fname, st in all_rows:
                ext = os.path.splitext(str(fname or ""))[1].lower()
                is_vid = ext in MODEL_VIDEO_EXTENSIONS
                if is_vid:
                    v_total += 1
                else:
                    c_total += 1
                if ft == "video" and not is_vid:
                    continue
                if ft == "companion" and is_vid:
                    continue
                total += 1
                st_key = str(st or "").upper()
                counts[st_key] = counts.get(st_key, 0) + 1
            counts["TOTAL"] = total
            counts["VIDEO_TOTAL"] = v_total
            counts["COMPANION_TOTAL"] = c_total
        except Exception:
            pass
        return counts

    @db_query
    def count_tasks(
        self,
        db: Optional[Session] = None,
        *,
        keyword: str = "",
        status: str = "",
        file_type: str = "all",
    ) -> int:
        """按条件统计任务总数"""
        if db is None:
            return 0
        try:
            stmt = select(func.count(Pan123Task.id))
            stmt = self._apply_query_filters(stmt, keyword=keyword, status=status, file_type=file_type)
            return int(db.execute(stmt).scalar() or 0)
        except Exception:
            return 0

    @db_query
    def list_tasks_paginated(
        self,
        db: Optional[Session] = None,
        *,
        limit: int = 50,
        offset: int = 0,
        keyword: str = "",
        status: str = "",
        file_type: str = "all",
    ) -> list[dict]:
        """分页、模糊检索任务列表（按更新时间与ID倒序）"""
        if db is None:
            return []
        try:
            stmt = select(Pan123Task)
            stmt = self._apply_query_filters(stmt, keyword=keyword, status=status, file_type=file_type)
            stmt = stmt.order_by(Pan123Task.updated_at.desc(), Pan123Task.id.desc()).offset(max(0, int(offset))).limit(max(1, int(limit)))
            records = db.execute(stmt).scalars().all()
            return [self._to_dict(r) for r in records]
        except Exception:
            return []

    @db_update
    def purge_expired_tasks(
        self,
        db: Optional[Session] = None,
        *,
        days: int = 0,
    ) -> int:
        """清理超过保留天数的已完结记录（仅清理 COMPLETED / CLEANED / FAILED，保留进行中任务）"""
        if db is None or int(days or 0) <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=int(days))
        res = db.execute(
            delete(Pan123Task).where(
                Pan123Task.updated_at < cutoff,
                Pan123Task.status.in_(["COMPLETED", "CLEANED", "FAILED"]),
            )
        )
        db.flush()
        return int(getattr(res, "rowcount", 0) or 0)

    @staticmethod
    def _to_dict(task: Pan123Task) -> dict:
        return {
            "id": task.id,
            "torrent_hash": task.torrent_hash,
            "source_path": task.source_path,
            "target_path": task.target_path,
            "file_name": task.file_name,
            "file_size": task.file_size,
            "sha1": task.sha1,
            "md5": task.md5,
            "file_id": task.file_id,
            "parent_file_id": task.parent_file_id,
            "status": task.status,
            "retry_count": task.retry_count,
            "max_retry": task.max_retry,
            "error_msg": task.error_msg,
            "created_at": task.created_at.strftime("%Y-%m-%d %H:%M:%S") if task.created_at else "",
            "updated_at": task.updated_at.strftime("%Y-%m-%d %H:%M:%S") if task.updated_at else "",
        }

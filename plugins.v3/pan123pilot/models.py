"""
Pan123Pilot 本地任务持久化数据模型
使用 SQLAlchemy 2.0 规范，提供任务存储与状态跟踪
"""

from datetime import datetime
from typing import Any, Optional

try:
    from sqlalchemy import BigInteger, DateTime, Integer, String, Text, select
    from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
except ImportError:
    class DummyColType:
        def __init__(self, *args, **kwargs): pass
        def __call__(self, *args, **kwargs): return self

    Mapped = Any  # type: ignore
    mapped_column = lambda *args, **kwargs: None  # type: ignore
    Integer = String = BigInteger = DateTime = Text = DummyColType  # type: ignore
    select = lambda *args, **kwargs: None  # type: ignore
    Session = Any  # type: ignore

try:
    from app.db import Base, db_query, db_update
except ImportError:
    class DeclarativeBaseFallback:
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
            select(Pan123Task).order_by(Pan123Task.updated_at.desc()).limit(limit)
        ).scalars().all()
        return [self._to_dict(r) for r in records]

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

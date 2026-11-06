"""
Pan123TgPilot 数据模型
负责 Telegram 频道转存历史记录与画质洗版版本管理
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


class Pan123TgRecord(Base):
    """123云盘 Telegram 频道转存与洗版记录表"""

    __tablename__ = "plugin_pan123_tg_record"
    __table_args__ = {"extend_existing": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(100), nullable=False, default="", index=True)
    msg_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    media_key: Mapped[str] = mapped_column(String(255), nullable=False, default="", index=True)
    media_title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    media_type: Mapped[str] = mapped_column(String(20), nullable=False, default="movie", index=True)
    resolution: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    codec: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    dovi_hdr: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    quality_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    share_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    fslink: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_dir_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cloud_file_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="SAVED", index=True)
    error_msg: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Pan123TgDatabase:
    """数据持久化门面类"""

    @db_update
    def ensure_table(self, db: Optional[Session] = None) -> None:
        """确保数据库表存在"""
        if db is None:
            return
        try:
            from app.db import Engine
            if Engine is not None and hasattr(Base, "metadata"):
                Base.metadata.create_all(bind=Engine, tables=[Pan123TgRecord.__table__], checkfirst=True)
        except Exception:
            pass

    @db_query
    def get_record_by_media_key(self, db: Optional[Session] = None, *, media_key: str) -> Optional[dict]:
        """根据媒体唯一键查找最新记录"""
        if db is None or not media_key:
            return None
        stmt = (
            select(Pan123TgRecord)
            .where(Pan123TgRecord.media_key == media_key)
            .order_by(Pan123TgRecord.id.desc())
            .limit(1)
        )
        rec = db.execute(stmt).scalar_one_or_none()
        return self._to_dict(rec) if rec else None

    @db_query
    def has_link_processed(self, db: Optional[Session] = None, *, share_url: str = "", fslink: str = "") -> bool:
        """检查特定链接是否已转存过"""
        if db is None:
            return False
        stmt = select(Pan123TgRecord)
        if share_url and fslink:
            stmt = stmt.where((Pan123TgRecord.share_url == share_url) | (Pan123TgRecord.fslink == fslink))
        elif share_url:
            stmt = stmt.where(Pan123TgRecord.share_url == share_url)
        elif fslink:
            stmt = stmt.where(Pan123TgRecord.fslink == fslink)
        else:
            return False
        rec = db.execute(stmt.limit(1)).scalar_one_or_none()
        return rec is not None

    @db_update
    def save_or_update_record(
        self,
        db: Optional[Session] = None,
        *,
        channel: str,
        msg_id: Optional[str],
        media_key: str,
        media_title: str,
        media_type: str,
        resolution: str,
        codec: str,
        dovi_hdr: str,
        quality_score: int,
        file_size: int,
        share_url: Optional[str] = None,
        fslink: Optional[str] = None,
        target_dir_id: int = 0,
        cloud_file_id: Optional[str] = None,
        status: str = "SAVED",
        error_msg: str = "",
    ) -> int:
        """添加或更新转存记录"""
        if db is None:
            return 0

        existing = None
        if media_key:
            stmt = select(Pan123TgRecord).where(Pan123TgRecord.media_key == media_key).order_by(Pan123TgRecord.id.desc()).limit(1)
            existing = db.execute(stmt).scalar_one_or_none()

        if existing:
            existing.channel = channel
            existing.msg_id = msg_id
            existing.media_title = media_title
            existing.media_type = media_type
            existing.resolution = resolution
            existing.codec = codec
            existing.dovi_hdr = dovi_hdr
            existing.quality_score = quality_score
            existing.file_size = file_size
            if share_url:
                existing.share_url = share_url
            if fslink:
                existing.fslink = fslink
            existing.target_dir_id = target_dir_id
            if cloud_file_id:
                existing.cloud_file_id = cloud_file_id
            existing.status = status
            existing.error_msg = error_msg
            existing.updated_at = datetime.now()
            db.flush()
            return existing.id
        else:
            new_rec = Pan123TgRecord(
                channel=channel,
                msg_id=msg_id,
                media_key=media_key,
                media_title=media_title,
                media_type=media_type,
                resolution=resolution,
                codec=codec,
                dovi_hdr=dovi_hdr,
                quality_score=quality_score,
                file_size=file_size,
                share_url=share_url,
                fslink=fslink,
                target_dir_id=target_dir_id,
                cloud_file_id=cloud_file_id,
                status=status,
                error_msg=error_msg,
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            db.add(new_rec)
            db.flush()
            return new_rec.id

    @db_query
    def list_records(self, db: Optional[Session] = None, *, limit: int = 50) -> list[dict]:
        """获取最近转存记录"""
        if db is None:
            return []
        records = db.execute(
            select(Pan123TgRecord).order_by(Pan123TgRecord.updated_at.desc()).limit(limit)
        ).scalars().all()
        return [self._to_dict(r) for r in records]

    @staticmethod
    def _to_dict(rec: Pan123TgRecord) -> dict:
        return {
            "id": rec.id,
            "channel": rec.channel,
            "msg_id": rec.msg_id,
            "media_key": rec.media_key,
            "media_title": rec.media_title,
            "media_type": rec.media_type,
            "resolution": rec.resolution,
            "codec": rec.codec,
            "dovi_hdr": rec.dovi_hdr,
            "quality_score": rec.quality_score,
            "file_size": rec.file_size,
            "share_url": rec.share_url,
            "fslink": rec.fslink,
            "target_dir_id": rec.target_dir_id,
            "cloud_file_id": rec.cloud_file_id,
            "status": rec.status,
            "error_msg": rec.error_msg,
            "created_at": rec.created_at.strftime("%Y-%m-%d %H:%M:%S") if rec.created_at else "",
            "updated_at": rec.updated_at.strftime("%Y-%m-%d %H:%M:%S") if rec.updated_at else "",
        }

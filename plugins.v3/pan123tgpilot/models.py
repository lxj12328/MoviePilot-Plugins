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
        def __eq__(self, other): return self
        def desc(self): return self

    class DummySelect:
        def __init__(self, *args, **kwargs): pass
        def where(self, *args, **kwargs): return self
        def order_by(self, *args, **kwargs): return self
        def limit(self, *args, **kwargs): return self

    Mapped = Any  # type: ignore
    mapped_column = lambda *args, **kwargs: DummyColType()  # type: ignore
    Integer = String = BigInteger = DateTime = Text = DummyColType  # type: ignore
    select = lambda *args, **kwargs: DummySelect()  # type: ignore
    Session = Any  # type: ignore

try:
    from app.db import Base, db_query, db_update
except ImportError:
    class DeclarativeBaseFallback:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
            if not hasattr(self, "id"):
                self.id = 1

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
    def get_record_by_media_key(self, *args, **kwargs) -> Optional[dict]:
        """根据媒体唯一键查找最新记录 (自适应适配 MoviePilot @db_query 注入)"""
        db = kwargs.get("db")
        media_key = str(kwargs.get("media_key") or "")
        for arg in args:
            if hasattr(arg, "execute"):
                db = arg
            elif isinstance(arg, str) and not media_key:
                media_key = arg

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
    def has_link_processed(self, *args, **kwargs) -> bool:
        """检查特定链接是否已转存过 (自适应适配 MoviePilot @db_query 注入)"""
        db = kwargs.get("db")
        share_url = str(kwargs.get("share_url") or "")
        fslink = str(kwargs.get("fslink") or "")
        for arg in args:
            if hasattr(arg, "execute"):
                db = arg
            elif isinstance(arg, str):
                if not share_url:
                    share_url = arg
                elif not fslink:
                    fslink = arg

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
    def save_or_update_record(self, *args, **kwargs) -> int:
        """
        添加或更新转存记录。
        同时兼容传入单一 dict 字典或任意关键字参数 (自适应适配 MoviePilot @db_update 注入)。
        """
        db = kwargs.get("db")
        merged: dict[str, Any] = {}
        for arg in args:
            if hasattr(arg, "execute"):
                db = arg
            elif isinstance(arg, dict):
                merged.update(arg)

        data_kw = kwargs.get("data")
        if isinstance(data_kw, dict):
            merged.update(data_kw)
        merged.update(kwargs)

        if db is None:
            return 0

        channel = str(merged.get("channel") or merged.get("channel_name") or "")
        msg_id_val = merged.get("msg_id")
        msg_id = str(msg_id_val) if msg_id_val is not None else None
        media_key = str(merged.get("media_key") or "")
        media_title = str(merged.get("media_title") or merged.get("title") or "")
        media_type = str(merged.get("media_type") or "movie")
        resolution = str(merged.get("resolution") or "")
        codec = str(merged.get("codec") or "")
        dovi_hdr = str(merged.get("dovi_hdr") or "")
        quality_score = int(merged.get("quality_score") or 0)
        file_size = int(merged.get("file_size") or 0)
        share_url = merged.get("share_url")
        fslink = merged.get("fslink")
        target_dir_id = int(merged.get("target_dir_id") or merged.get("pan_dir_id") or 0)
        cloud_file_id_raw = merged.get("cloud_file_id") or merged.get("pan_file_id")
        cloud_file_id = str(cloud_file_id_raw) if cloud_file_id_raw is not None else None
        status = str(merged.get("status") or "SAVED")
        error_msg = str(merged.get("error_msg") or merged.get("remark") or "")

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
    def list_records(self, *args, **kwargs) -> list[dict]:
        """获取最近转存记录 (自适应适配 MoviePilot @db_query 注入)"""
        db = kwargs.get("db")
        limit = kwargs.get("limit")
        for arg in args:
            if hasattr(arg, "execute"):
                db = arg
            elif isinstance(arg, int):
                limit = arg

        limit = int(limit or 50)
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
            "channel_name": rec.channel,
            "msg_id": rec.msg_id,
            "media_key": rec.media_key,
            "media_title": rec.media_title,
            "title": rec.media_title,
            "media_type": rec.media_type,
            "resolution": rec.resolution,
            "codec": rec.codec,
            "dovi_hdr": rec.dovi_hdr,
            "quality_score": rec.quality_score,
            "file_size": rec.file_size,
            "share_url": rec.share_url,
            "fslink": rec.fslink,
            "target_dir_id": rec.target_dir_id,
            "pan_dir_id": rec.target_dir_id,
            "cloud_file_id": rec.cloud_file_id,
            "pan_file_id": rec.cloud_file_id,
            "status": rec.status,
            "error_msg": rec.error_msg,
            "remark": rec.error_msg,
            "created_at": rec.created_at.strftime("%Y-%m-%d %H:%M:%S") if rec.created_at else "",
            "updated_at": rec.updated_at.strftime("%Y-%m-%d %H:%M:%S") if rec.updated_at else "",
        }

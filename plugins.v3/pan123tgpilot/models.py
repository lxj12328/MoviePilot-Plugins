"""
Pan123TgPilot 数据模型
负责 Telegram 频道转存历史记录与画质洗版版本管理
"""

import re
from datetime import datetime
from typing import Any, Optional

try:
    from sqlalchemy import BigInteger, DateTime, Integer, String, Text, or_, select
    from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
except ImportError:
    class DummyColType:
        def __init__(self, *args, **kwargs): pass
        def __call__(self, *args, **kwargs): return self
        def __eq__(self, other): return self
        def desc(self): return self
        def like(self, other): return self

    class DummySelect:
        def __init__(self, *args, **kwargs): pass
        def where(self, *args, **kwargs): return self
        def order_by(self, *args, **kwargs): return self
        def limit(self, *args, **kwargs): return self

    Mapped = Any  # type: ignore
    mapped_column = lambda *args, **kwargs: DummyColType()  # type: ignore
    Integer = String = BigInteger = DateTime = Text = DummyColType  # type: ignore
    select = lambda *args, **kwargs: DummySelect()  # type: ignore
    or_ = lambda *args: None  # type: ignore
    Session = Any  # type: ignore

try:
    from app.sdk.logging import logger
except ImportError:
    import logging
    logger = logging.getLogger("pan123tgpilot")  # type: ignore

try:
    from app.db import Base, db_query, db_update
except ImportError:
    try:
        class DeclarativeBaseFallback(DeclarativeBase):
            pass
    except Exception:
        class DeclarativeBaseFallback:  # type: ignore
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)
                if not hasattr(self, "id"):
                    self.id = 1

    Base = DeclarativeBaseFallback  # type: ignore
    db_query = lambda func: func
    db_update = lambda func: func


# 表结构版本：升级时在 ensure_table 中执行可重复运行的迁移
SCHEMA_VERSION = 2


def compute_link_hash(share_url: Optional[str] = None, fslink: Optional[str] = None) -> str:
    """
    计算链接指纹：优先使用分享链接短码 (s:<key>)，否则使用秒传码文件哈希 (f:<etag>)。
    用于带索引的等值查询，替代全表 LIKE 扫描。
    """
    if share_url:
        m = re.search(r"/s/([a-zA-Z0-9_-]+)", str(share_url))
        if m:
            return f"s:{m.group(1)}"
    if fslink:
        m = re.search(r"\$([a-fA-F0-9]{32,40})#", str(fslink))
        if m:
            return f"f:{m.group(1).lower()}"
    return ""


def link_hash_candidates(share_url: Optional[str] = None, fslink: Optional[str] = None) -> list[str]:
    """返回一条消息可能命中的全部链接指纹"""
    out = []
    if share_url:
        m = re.search(r"/s/([a-zA-Z0-9_-]+)", str(share_url))
        if m:
            out.append(f"s:{m.group(1)}")
    if fslink:
        m = re.search(r"\$([a-fA-F0-9]{32,40})#", str(fslink))
        if m:
            out.append(f"f:{m.group(1).lower()}")
    return out


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
    link_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    target_dir_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cloud_file_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="SAVED", index=True)
    error_msg: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)


class Pan123TgDatabase:
    """数据持久化门面类"""

    @staticmethod
    def _extract_db(args: tuple, kwargs: dict) -> Any:
        db = kwargs.get("db")
        for arg in args:
            if hasattr(arg, "execute"):
                db = arg
                break
        return db

    @db_update
    def ensure_table(self, *args, **kwargs) -> None:
        """确保数据库表存在，并执行可重复运行的结构迁移 (新增 link_hash 列与索引、回填)"""
        db = self._extract_db(args, kwargs)
        try:
            from app.db import Engine
            if Engine is not None and hasattr(Base, "metadata"):
                Base.metadata.create_all(bind=Engine, tables=[Pan123TgRecord.__table__], checkfirst=True)
        except Exception:
            pass
        if db is None:
            return
        try:
            self._migrate(db)
        except Exception as e:
            logger.warning(f"Pan123TgPilot 数据库迁移异常 (不影响基础功能): {e}")
        try:
            self._delete_empty_records(db)
        except Exception as e:
            logger.debug(f"Pan123TgPilot 清理空记录异常: {e}")

    @staticmethod
    def _migrate(db: Any) -> None:
        """可重复执行的迁移：补齐 link_hash 列、索引，并回填历史记录"""
        from sqlalchemy import inspect, select, text

        table = Pan123TgRecord.__tablename__
        conn = db.connection()
        cols = {c["name"] for c in inspect(conn).get_columns(table)}
        if "link_hash" not in cols:
            db.execute(text(f"ALTER TABLE {table} ADD COLUMN link_hash VARCHAR(64)"))
        db.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_link_hash ON {table} (link_hash)"))
        db.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_updated_at ON {table} (updated_at)"))

        rows = db.execute(
            select(Pan123TgRecord).where(Pan123TgRecord.link_hash.is_(None))
        ).scalars().all()
        for rec in rows:
            rec.link_hash = compute_link_hash(rec.share_url, rec.fslink)
        db.flush()
        try:
            db.commit()
        except Exception:
            pass

    @staticmethod
    def _delete_empty_records(db: Any) -> int:
        from sqlalchemy import delete
        res = db.execute(
            delete(Pan123TgRecord).where(
                or_(Pan123TgRecord.media_title == "", Pan123TgRecord.media_title.is_(None))
            )
        )
        db.flush()
        try:
            db.commit()
        except Exception:
            pass
        return getattr(res, "rowcount", 0) or 0

    @db_update
    def clean_invalid_records(self, *args, **kwargs) -> int:
        """清理历史因传参异常导致的空记录 (media_title 为空)"""
        db = self._extract_db(args, kwargs)
        if db is None:
            return 0
        try:
            return self._delete_empty_records(db)
        except Exception as e:
            logger.debug(f"Pan123TgPilot 清理无效记录异常: {e}")
            return 0

    @db_update
    def purge_expired(self, *args, **kwargs) -> int:
        """删除 updated_at 早于 days 天前的记录；days<=0 表示永久保留，不执行任何删除"""
        db = self._extract_db(args, kwargs)
        days = kwargs.get("days", 0)
        for arg in args:
            if isinstance(arg, int) and not isinstance(arg, bool):
                days = arg
        try:
            days = int(days or 0)
        except (ValueError, TypeError):
            days = 0
        if db is None or days <= 0:
            return 0
        from datetime import timedelta
        from sqlalchemy import delete
        cutoff = datetime.now() - timedelta(days=days)
        res = db.execute(delete(Pan123TgRecord).where(Pan123TgRecord.updated_at < cutoff))
        db.flush()
        try:
            db.commit()
        except Exception:
            pass
        return getattr(res, "rowcount", 0) or 0

    @db_query
    def get_record_by_media_key(self, *args, **kwargs) -> Optional[dict]:
        """
        根据媒体唯一键或标题+年份+季集查找最新记录。
        同时支持匹配当前 media_key、历史 TMDB 格式 key 及归一化影视标题，
        确保不同频道以不同格式发布同一资源时能够百分之百命中本地库去重！
        """
        db = kwargs.get("db")
        media_key = str(kwargs.get("media_key") or "")
        title = str(kwargs.get("title") or kwargs.get("media_title") or "")
        year = str(kwargs.get("year") or "")
        tmdb_id = str(kwargs.get("tmdb_id") or "")
        media_type = str(kwargs.get("media_type") or "")
        ep_tag = str(kwargs.get("ep_tag") or "")

        for arg in args:
            if hasattr(arg, "execute"):
                db = arg
            elif isinstance(arg, str) and not media_key:
                media_key = arg

        if db is None:
            return None

        conditions = []
        if media_key:
            conditions.append(Pan123TgRecord.media_key == media_key)
        if tmdb_id:
            conditions.append(Pan123TgRecord.media_key == f"movie_tmdb_{tmdb_id}")
            if ep_tag:
                conditions.append(Pan123TgRecord.media_key == f"tv_tmdb_{tmdb_id}_{ep_tag}")
            else:
                conditions.append(Pan123TgRecord.media_key == f"tv_tmdb_{tmdb_id}")
        if title:
            norm_title = re.sub(r"[^\w\u4e00-\u9fa5]", "", title).lower()
            if norm_title:
                if ep_tag:
                    conditions.append(Pan123TgRecord.media_key == f"tv_{norm_title}_{ep_tag}")
                elif year:
                    conditions.append(Pan123TgRecord.media_key == f"movie_{norm_title}_{year}")
                conditions.append(Pan123TgRecord.media_title == title)
                conditions.append(Pan123TgRecord.media_title == norm_title)

        if not conditions:
            return None

        stmt = select(Pan123TgRecord)
        if len(conditions) == 1:
            stmt = stmt.where(conditions[0])
        else:
            stmt = stmt.where(or_(*conditions))

        rec = db.execute(stmt.order_by(Pan123TgRecord.id.desc()).limit(1)).scalar_one_or_none()
        return self._to_dict(rec) if rec else None

    @db_query
    def has_link_processed(self, *args, **kwargs) -> bool:
        """检查特定分享链接或秒传哈希码是否已转存过 (自适应适配 MoviePilot @db_query 注入)"""
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

        tokens = link_hash_candidates(share_url, fslink)
        if not tokens:
            return False

        # 走 link_hash 索引的等值/IN 查询，避免 LIKE 全表扫描
        stmt = select(Pan123TgRecord.id).where(Pan123TgRecord.link_hash.in_(tokens)).limit(1)
        return db.execute(stmt).first() is not None

    @db_update
    def save_or_update_record(self, *args, **kwargs) -> int:
        """
        添加或更新转存记录。
        同时兼容传入单一 dict 字典或完整关键字参数 (自适应适配 MoviePilot @db_update 注入)。
        """
        db = kwargs.get("db")
        for arg in args:
            if hasattr(arg, "execute"):
                db = arg
                break

        merged: dict[str, Any] = {}
        # 从 args 中提取字典对象 (兼容 positional dict)
        for arg in args:
            if isinstance(arg, dict):
                merged.update(arg)

        # 从 kwargs 中提取结构化字典
        if isinstance(kwargs.get("data"), dict):
            merged.update(kwargs["data"])
        if isinstance(kwargs.get("record"), dict):
            merged.update(kwargs["record"])

        # 从 kwargs 提取独立字段
        for k, v in kwargs.items():
            if k != "db" and v is not None:
                merged[k] = v

        if db is None:
            return 0

        channel = str(merged.get("channel") or merged.get("channel_name") or "").strip()
        msg_id_val = merged.get("msg_id")
        msg_id = str(msg_id_val) if msg_id_val is not None else None
        media_key = str(merged.get("media_key") or "").strip()
        media_title = str(merged.get("media_title") or merged.get("title") or "").strip()
        media_type = str(merged.get("media_type") or "movie")
        resolution = str(merged.get("resolution") or "").strip()
        codec = str(merged.get("codec") or "").strip()
        dovi_hdr = str(merged.get("dovi_hdr") or "").strip()
        quality_score = int(merged.get("quality_score") or 0)
        file_size = int(merged.get("file_size") or 0)
        share_url = merged.get("share_url")
        fslink = merged.get("fslink")
        target_dir_id = int(merged.get("target_dir_id") or merged.get("pan_dir_id") or 0)
        cloud_file_id_raw = merged.get("cloud_file_id") or merged.get("pan_file_id")
        cloud_file_id = str(cloud_file_id_raw) if cloud_file_id_raw is not None else None
        status = str(merged.get("status") or "saved").lower()
        error_msg = str(merged.get("error_msg") or merged.get("remark") or "")

        # 杜绝无意义空记录写入
        if not media_title and not media_key:
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
            lh = compute_link_hash(share_url, fslink)
            if lh:
                existing.link_hash = lh
            existing.target_dir_id = target_dir_id
            if cloud_file_id:
                existing.cloud_file_id = cloud_file_id
            existing.status = status
            existing.error_msg = error_msg
            existing.updated_at = datetime.now()
            db.flush()
            try:
                db.commit()
            except Exception:
                pass
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
                link_hash=compute_link_hash(share_url, fslink),
                target_dir_id=target_dir_id,
                cloud_file_id=cloud_file_id,
                status=status,
                error_msg=error_msg,
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            db.add(new_rec)
            db.flush()
            try:
                db.commit()
            except Exception:
                pass
            return int(new_rec.id if new_rec.id is not None else 1)

    @staticmethod
    def _build_filter(keyword: Any = None, status: Any = None):
        """构造列表/计数共用的过滤条件"""
        stmt_filters = [
            Pan123TgRecord.media_title != "",
            Pan123TgRecord.media_title.is_not(None),
        ]
        if keyword and str(keyword).strip():
            kw = f"%{str(keyword).strip()}%"
            stmt_filters.append(
                or_(
                    Pan123TgRecord.media_title.like(kw),
                    Pan123TgRecord.channel.like(kw),
                    Pan123TgRecord.resolution.like(kw),
                    Pan123TgRecord.codec.like(kw),
                    Pan123TgRecord.dovi_hdr.like(kw),
                    Pan123TgRecord.media_key.like(kw),
                )
            )
        if status and str(status).strip():
            stmt_filters.append(Pan123TgRecord.status == str(status).strip().lower())
        return stmt_filters

    @db_query
    def list_records(self, *args, **kwargs) -> list[dict]:
        """分页获取转存记录，支持关键词与状态筛选 (自适应适配 MoviePilot @db_query 注入)"""
        db = kwargs.get("db")
        limit = kwargs.get("limit")
        offset = kwargs.get("offset") or 0
        keyword = kwargs.get("keyword") or kwargs.get("search")
        status = kwargs.get("status")
        for arg in args:
            if hasattr(arg, "execute"):
                db = arg
            elif isinstance(arg, int) and not isinstance(arg, bool):
                limit = arg
            elif isinstance(arg, str):
                keyword = arg

        try:
            limit = max(1, int(limit or 100))
            offset = max(0, int(offset or 0))
        except (ValueError, TypeError):
            limit, offset = 100, 0
        if db is None:
            return []

        stmt = select(Pan123TgRecord).where(*self._build_filter(keyword, status))
        records = db.execute(
            stmt.order_by(Pan123TgRecord.updated_at.desc(), Pan123TgRecord.id.desc())
            .offset(offset)
            .limit(limit)
        ).scalars().all()
        return [self._to_dict(r) for r in records]

    @db_query
    def count_records(self, *args, **kwargs) -> int:
        """统计符合条件的记录总数，用于分页"""
        db = self._extract_db(args, kwargs)
        if db is None:
            return 0
        from sqlalchemy import func
        stmt = select(func.count(Pan123TgRecord.id)).where(
            *self._build_filter(kwargs.get("keyword") or kwargs.get("search"), kwargs.get("status"))
        )
        return int(db.execute(stmt).scalar() or 0)

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

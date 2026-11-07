"""
使用真实 SQLAlchemy + 内存 SQLite 验证 Pan123TgDatabase 的迁移、分页、检索与保留期清理。
未安装 sqlalchemy 时自动跳过 (venv: pip install sqlalchemy)。
"""
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

try:
    import sqlalchemy
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import DeclarativeBase, Session
    from sqlalchemy.pool import StaticPool
    HAS_SA = True
except ImportError:  # pragma: no cover
    HAS_SA = False

MODELS_PATH = Path(__file__).resolve().parents[3] / "plugins.v3" / "pan123tgpilot" / "models.py"


def _load_models():
    """在注入了桩 app.db 的环境下加载 models.py 的独立副本"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    class Base(DeclarativeBase):
        pass

    def _wrap(func):
        def wrapper(self, *args, **kwargs):
            session = Session(engine)
            try:
                result = func(self, session, *args, **kwargs)
                session.commit()
                return result
            finally:
                session.close()
        return wrapper

    stub_app = types.ModuleType("app")
    stub_db = types.ModuleType("app.db")
    stub_db.Base = Base
    stub_db.Engine = engine
    stub_db.db_query = _wrap
    stub_db.db_update = _wrap
    saved = {k: sys.modules.get(k) for k in ("app", "app.db")}
    sys.modules["app"], sys.modules["app.db"] = stub_app, stub_db
    spec = importlib.util.spec_from_file_location("pan123tg_models_real", MODELS_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pan123tg_models_real"] = mod
    spec.loader.exec_module(mod)

    def restore():
        # ensure_table 在运行期仍需 app.db.Engine，因此测试结束后再还原
        sys.modules.pop("pan123tg_models_real", None)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    return mod, engine, restore


@unittest.skipUnless(HAS_SA, "sqlalchemy 未安装")
class TestPan123TgDatabaseReal(unittest.TestCase):
    def setUp(self):
        self.m, self.engine, restore = _load_models()
        self.addCleanup(restore)
        self.db = self.m.Pan123TgDatabase()
        self.db.ensure_table()

    def _add(self, i, **kw):
        data = dict(
            channel="@Pan123Movie", msg_id=str(i), media_key=f"movie_t{i}_2026",
            media_title=f"测试电影{i}", media_type="movie", resolution="1080p", codec="H.265",
            quality_score=60 + i, file_size=1024 ** 3, status="saved",
            share_url=f"https://www.123pan.com/s/key{i}-abcd",
        )
        data.update(kw)
        return self.db.save_or_update_record(**data)

    def test_compute_link_hash(self):
        self.assertEqual(self.m.compute_link_hash("https://www.123pan.com/s/abc-123?pwd=x"), "s:abc-123")
        self.assertEqual(
            self.m.compute_link_hash("https://1840976528.share.123pan.cn/123pan/cHCOTd-RLSP"),
            "s:cHCOTd-RLSP",
        )
        etag = "82467be2147f26532298438bf7015238"
        self.assertEqual(self.m.compute_link_hash(None, f"123FSLinkV2${etag}#10#a.mkv"), f"f:{etag}")
        self.assertEqual(self.m.compute_link_hash(None, None), "")

    def test_tv_different_episodes_do_not_cross_match(self):
        """验证同一剧集的不同集数 (如 S01E05 与 S01E06) 不会互相匹配导致误判洗版"""
        self._add(
            1,
            media_key="tv_谷雨_S01E05",
            media_title="谷雨 S01E05",
            media_type="tv",
            quality_score=125,
        )
        # 查询 S01E06 时不应命中 S01E05
        rec_e06 = self.db.get_record_by_media_key(
            media_key="tv_谷雨_S01E06",
            title="谷雨",
            year="2026",
            media_type="tv",
            ep_tag="S01E06",
        )
        self.assertIsNone(rec_e06)

        # 查询同集 S01E05 时应准确命中
        rec_e05 = self.db.get_record_by_media_key(
            media_key="tv_谷雨_S01E05",
            title="谷雨",
            year="2026",
            media_type="tv",
            ep_tag="S01E05",
        )
        self.assertIsNotNone(rec_e05)
        self.assertEqual(rec_e05["media_title"], "谷雨 S01E05")

    def test_pagination_and_count(self):
        for i in range(25):
            self._add(i)
        self.assertEqual(self.db.count_records(), 25)
        p1 = self.db.list_records(limit=10, offset=0)
        p3 = self.db.list_records(limit=10, offset=20)
        self.assertEqual(len(p1), 10)
        self.assertEqual(len(p3), 5)
        ids = {r["id"] for r in p1} | {r["id"] for r in self.db.list_records(limit=10, offset=10)} | {r["id"] for r in p3}
        self.assertEqual(len(ids), 25)

    def test_keyword_and_status_filter(self):
        self._add(1, media_title="八仙！", resolution="2160p")
        self._add(2, media_title="夜曲", status="upgraded")
        self.assertEqual(self.db.count_records(keyword="八仙"), 1)
        self.assertEqual(self.db.count_records(keyword="2160p"), 1)
        self.assertEqual(self.db.count_records(status="upgraded"), 1)
        self.assertEqual(len(self.db.list_records(keyword="夜曲", status="upgraded")), 1)
        self.assertEqual(len(self.db.list_records(keyword="不存在")), 0)

    def test_has_link_processed_uses_hash(self):
        self._add(1, share_url="https://www.123pan.com/s/Zz9-Key?pwd=1234")
        self.assertTrue(self.db.has_link_processed(share_url="https://www.123pan.com/s/Zz9-Key"))
        self.assertFalse(self.db.has_link_processed(share_url="https://www.123pan.com/s/other"))
        etag = "a" * 32
        self._add(2, share_url=None, fslink=f"123FSLinkV2${etag}#5#x.mkv", media_key="movie_x_2026")
        self.assertTrue(self.db.has_link_processed(fslink=f"123FSLinkV2${etag.upper()}#5#y.mkv"))

    def test_purge_expired(self):
        self._add(1)
        self._add(2)
        old = datetime.now() - timedelta(days=100)
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE plugin_pan123_tg_record SET updated_at=:t WHERE msg_id='1'"), {"t": old}
            )
        # 0 = 永久保留，不删除
        self.assertEqual(self.db.purge_expired(days=0), 0)
        self.assertEqual(self.db.count_records(), 2)
        # 保留 30 天，仅清理 100 天前的那条
        self.assertEqual(self.db.purge_expired(days=30), 1)
        self.assertEqual(self.db.count_records(), 1)

    def test_migration_adds_link_hash_to_legacy_table(self):
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE plugin_pan123_tg_record"))
            conn.execute(text(
                "CREATE TABLE plugin_pan123_tg_record ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, channel VARCHAR(100) NOT NULL DEFAULT '', "
                "msg_id VARCHAR(50), media_key VARCHAR(255) NOT NULL DEFAULT '', "
                "media_title VARCHAR(255) NOT NULL DEFAULT '', media_type VARCHAR(20) NOT NULL DEFAULT 'movie', "
                "resolution VARCHAR(50) NOT NULL DEFAULT '', codec VARCHAR(50) NOT NULL DEFAULT '', "
                "dovi_hdr VARCHAR(50) NOT NULL DEFAULT '', quality_score INTEGER NOT NULL DEFAULT 0, "
                "file_size BIGINT NOT NULL DEFAULT 0, share_url VARCHAR(1000), fslink TEXT, "
                "target_dir_id BIGINT NOT NULL DEFAULT 0, cloud_file_id VARCHAR(100), "
                "status VARCHAR(50) NOT NULL DEFAULT 'SAVED', error_msg TEXT NOT NULL DEFAULT '', "
                "created_at DATETIME, updated_at DATETIME)"
            ))
            conn.execute(text(
                "INSERT INTO plugin_pan123_tg_record (media_key, media_title, share_url, created_at, updated_at) "
                "VALUES ('movie_a_2026', '旧记录', 'https://www.123pan.com/s/Old-Key', datetime('now'), datetime('now'))"
            ))
        # 重复执行迁移应安全且结果一致
        self.db.ensure_table()
        self.db.ensure_table()
        self.assertTrue(self.db.has_link_processed(share_url="https://www.123pan.com/s/Old-Key"))
        cols = {c["name"] for c in sqlalchemy.inspect(self.engine).get_columns("plugin_pan123_tg_record")}
        self.assertIn("link_hash", cols)

    def test_empty_title_rejected(self):
        self.assertEqual(self.db.save_or_update_record(title="", media_key=""), 0)
        self.assertEqual(self.db.count_records(), 0)


if __name__ == "__main__":
    unittest.main()

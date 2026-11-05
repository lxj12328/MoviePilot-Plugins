"""
123云盘助手 (Pan123Pilot) - MoviePilot V3 插件入口
支持 123 云盘 SHA1 零流量秒传、断点续传、qBittorrent 状态标签联动与做种清理
"""

import os
from typing import Any, Optional

try:
    from app.sdk.plugin import _PluginBase
except ImportError:
    class _PluginBase:
        def __init__(self):
            pass

try:
    from app.sdk.logging import logger
except ImportError:
    class DummyLogger:
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass
        def debug(self, *args, **kwargs): pass
    logger = DummyLogger()  # type: ignore

try:
    from app.schemas.types import EventType
except ImportError:
    try:
        from app.sdk.events import EventType  # type: ignore
    except ImportError:
        class EventType:  # type: ignore
            TransferComplete = "TransferComplete"
            TransferCompleted = "TransferCompleted"
            PluginAction = "PluginAction"

try:
    from app.sdk.events import Event, eventmanager
except ImportError:
    try:
        from app.sdk.events import Event, event_manager as eventmanager  # type: ignore
    except ImportError:
        class EventManagerFallback:
            def register(self, *args, **kwargs):
                def decorator(func):
                    return func
                return decorator
        eventmanager = EventManagerFallback()  # type: ignore

try:
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger
except ImportError:
    IntervalTrigger = None
    CronTrigger = None

from .client import Pan123Client
from .models import Pan123TaskDatabase
from .qb_helper import QBHelper
from .uploader import TaskScheduler


def get_system_qbittorrents() -> list[dict[str, Any]]:
    """
    动态检测并检索 MoviePilot 系统中已配置的 qBittorrent 下载器。
    综合四级高可用检索机制：
    1. 通过 system_config_oper / SystemConfigOper 查询 SystemConfigKey.Downloaders / 'Downloaders'
    2. 通过 SQLAlchemy Engine 直接物理查询 SQLite systemconfig 表中的 'Downloaders'
    3. 通过 ServiceConfigHelper 服务契约查询
    4. 通过 DownloadChain 下载链实例检索
    """
    qb_list: list[dict[str, Any]] = []
    seen_hosts: set[str] = set()

    def _parse_and_append(items: Any) -> None:
        if not items:
            return
        if isinstance(items, str):
            try:
                import json
                items = json.loads(items)
            except Exception:
                return

        # 若为字典，转为其 values 列表
        if isinstance(items, dict):
            items = list(items.values())

        if not isinstance(items, (list, tuple)):
            items = [items]

        for idx, item in enumerate(items):
            if not item:
                continue
            # 支持 dict 或 Pydantic 对象
            if hasattr(item, "model_dump"):
                d = item.model_dump()
            elif hasattr(item, "dict"):
                d = item.dict()
            elif isinstance(item, dict):
                d = item
            else:
                d = getattr(item, "__dict__", {})

            dtype = str(d.get("type") or d.get("downloader_type") or getattr(item, "type", "")).lower()
            name = str(d.get("name") or getattr(item, "name", "") or f"qBittorrent-{idx+1}").strip()

            # 筛选 qBittorrent 下载器
            if "qb" not in dtype and "qbittorrent" not in dtype and "qb" not in name.lower():
                continue

            cfg = d.get("config") if isinstance(d.get("config"), dict) else {}
            host = str(d.get("host") or cfg.get("host") or d.get("url") or cfg.get("url") or getattr(item, "host", "") or "").strip()
            port = d.get("port") or cfg.get("port") or getattr(item, "port", None)
            username = str(d.get("username") or cfg.get("username") or getattr(item, "username", "") or "").strip()
            password = str(d.get("password") or cfg.get("password") or getattr(item, "password", "") or "").strip()
            dl_id = str(d.get("id") or cfg.get("id") or getattr(item, "id", "") or name or f"qb_{idx+1}")

            if host and port and ":" not in host.replace("http://", "").replace("https://", ""):
                host = f"{host}:{port}"
            if host and not host.startswith("http://") and not host.startswith("https://"):
                host = f"http://{host}"

            host_key = f"{host}_{username}"
            if host and host_key not in seen_hosts:
                seen_hosts.add(host_key)
                qb_list.append({
                    "id": dl_id,
                    "name": name,
                    "host": host,
                    "username": username,
                    "password": password,
                })

    # 1. 尝试通过 system_config_oper / SystemConfigOper 获取 (MoviePilot V3 核心配置源)
    try:
        from app.db.systemconfig_oper import SystemConfigOper, system_config_oper
        oper = system_config_oper or SystemConfigOper()
        try:
            from app.schemas.types import SystemConfigKey
            raw = oper.get(SystemConfigKey.Downloaders)
            _parse_and_append(raw)
        except Exception:
            pass
        if not qb_list:
            raw = oper.get("Downloaders") or oper.get("downloaders")
            _parse_and_append(raw)
    except Exception:
        pass

    # 2. 尝试直接从系统 SQLite 数据库的 systemconfig 表查询（直接读取底层持久化表）
    if not qb_list:
        try:
            from app.db import Engine
            from sqlalchemy import text
            if Engine is not None:
                with Engine.connect() as conn:
                    rows = conn.execute(
                        text("SELECT value FROM systemconfig WHERE key = 'Downloaders' OR key = 'downloaders' OR LOWER(key) LIKE '%downloader%'")
                    ).fetchall()
                    for r in rows:
                        if r and r[0]:
                            _parse_and_append(r[0])
        except Exception:
            pass

    # 3. 尝试通过 ServiceConfigHelper 查询
    if not qb_list:
        try:
            from app.helper.service import ServiceConfigHelper
            from app.schemas.types import SystemConfigKey
            from app.schemas import DownloaderConf
            items = ServiceConfigHelper.get_configs(SystemConfigKey.Downloaders, DownloaderConf)
            _parse_and_append(items)
        except Exception:
            pass

    # 4. 尝试通过 DownloadChain 查询
    if not qb_list:
        try:
            from app.chain.download import DownloadChain
            chain = DownloadChain()
            if hasattr(chain, "get_downloaders"):
                items = chain.get_downloaders()
                _parse_and_append(items)
        except Exception:
            pass

    logger.info(f"Pan123Pilot 动态扫描发现 MoviePilot 系统 qB 下载器数量: {len(qb_list)}")
    return qb_list


class Pan123Pilot(_PluginBase):
    """123云盘自动化秒传与做种清理插件主类"""

    plugin_name = "123云盘助手"
    plugin_desc = "MoviePilot V3 的 123 云盘自动化秒传与做种清理助手。优先 SHA1 秒传，未命中定时重试，支持失败转真实分片上传，联动 qBittorrent 标签并按 H&R 规则安全清理。"
    plugin_icon = "https://raw.githubusercontent.com/lxj12328/MoviePilot-Plugins/main/icons/123pan.png"
    plugin_version = "1.1.3"
    plugin_author = "lxj12328"
    author_url = "https://github.com/lxj12328/MoviePilot-Plugins"
    plugin_config_prefix = "pan123pilot_"
    plugin_order = 20
    auth_level = 1

    _enabled: bool = False
    _client_id: str = ""
    _client_secret: str = ""
    _parent_file_id: int = 0
    _local_library_root: str = ""
    _enable_path_cascade: bool = True
    _retry_interval: int = 30
    _max_retry_count: int = 10
    _enable_real_upload: bool = True

    _downloader_select: str = "manual"
    _qb_host: str = ""
    _qb_port: Optional[int] = None
    _qb_username: str = ""
    _qb_password: str = ""

    _tag_pending: str = "123_待处理"
    _tag_uploading: str = "123_上传中"
    _tag_retrying: str = "123_待重试"
    _tag_failed: str = "123_秒传失败"
    _tag_completed: str = "123_已上传"

    _symedia_enabled: bool = False
    _symedia_url: str = ""
    _symedia_auth: str = ""
    _symedia_mount_prefix: str = ""
    _symedia_device_name: str = "MoviePilot-Pan123"
    _symedia_user_name: str = "admin"

    _enable_auto_cleanup: bool = False
    _min_seed_time: float = 48.0
    _min_seed_ratio: float = 2.0
    _delete_torrent_file: bool = True
    _delete_hardlink_file: bool = True

    _notify_enabled: bool = True
    _notify_on_upload: bool = True
    _notify_on_cleanup: bool = True
    _notify_on_failed: bool = True

    client: Optional[Pan123Client] = None
    qb_helper: Optional[QBHelper] = None
    db_helper: Optional[Pan123TaskDatabase] = None
    scheduler: Optional[TaskScheduler] = None

    def notify(self, title: str, text: str) -> None:
        """调用 MoviePilot 渠道发送消息通知"""
        if not self._notify_enabled:
            return
        try:
            # 兼容多种 MoviePilot 版本的 post_message 参数调用
            if hasattr(self, "post_message"):
                self.post_message(title=title, text=text)
        except TypeError:
            try:
                self.post_message(text=f"【{title}】\n{text}")
            except Exception as e:
                logger.error(f"Pan123Pilot 发送系统通知失败: {e}")
        except Exception as e:
            logger.error(f"Pan123Pilot 发送系统通知失败: {e}")

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置并初始化插件运行时状态，必须支持重复调用"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._client_id = str(config.get("client_id") or "").strip()
        self._client_secret = str(config.get("client_secret") or "").strip()
        self._parent_file_id = int(config.get("target_parent_file_id") or 0)
        self._local_library_root = str(config.get("local_library_root") or "").strip()
        self._enable_path_cascade = bool(config.get("enable_path_cascade", True))
        self._retry_interval = int(config.get("retry_interval") or 30)
        self._max_retry_count = int(config.get("max_retry_count") or 10)
        self._enable_real_upload = bool(config.get("enable_real_upload", True))

        self._downloader_select = str(config.get("downloader_select") or "manual").strip()
        self._qb_host = str(config.get("qb_host") or "").strip()
        qb_port_raw = config.get("qb_port")
        self._qb_port = int(qb_port_raw) if qb_port_raw else None
        self._qb_username = str(config.get("qb_username") or "").strip()
        self._qb_password = str(config.get("qb_password") or "").strip()

        # 若选择使用系统 QB 下载器，自动复用系统凭据
        if self._downloader_select and self._downloader_select != "manual":
            sys_qbs = get_system_qbittorrents()
            for q in sys_qbs:
                if q["id"] == self._downloader_select or q["name"] == self._downloader_select:
                    self._qb_host = q["host"]
                    self._qb_username = q["username"]
                    self._qb_password = q["password"]
                    self._qb_port = None
                    logger.info(f"Pan123Pilot 已自动绑定 MoviePilot 系统下载器: {q['name']} ({self._qb_host})")
                    break

        self._tag_pending = str(config.get("tag_pending") or "123_待处理").strip()
        self._tag_uploading = str(config.get("tag_uploading") or "123_上传中").strip()
        self._tag_retrying = str(config.get("tag_retrying") or "123_待重试").strip()
        self._tag_failed = str(config.get("tag_failed") or "123_秒传失败").strip()
        self._tag_completed = str(config.get("tag_completed") or "123_已上传").strip()

        self._symedia_enabled = bool(config.get("symedia_enabled", False))
        self._symedia_url = str(config.get("symedia_url") or "").strip()
        self._symedia_auth = str(config.get("symedia_auth") or "").strip()
        self._symedia_mount_prefix = str(config.get("symedia_mount_prefix") or "").strip()
        self._symedia_device_name = str(config.get("symedia_device_name") or "MoviePilot-Pan123").strip()
        self._symedia_user_name = str(config.get("symedia_user_name") or "admin").strip()

        self._enable_auto_cleanup = bool(config.get("enable_auto_cleanup", False))
        self._min_seed_time = float(config.get("min_seed_time") or 48.0)
        self._min_seed_ratio = float(config.get("min_seed_ratio") or 2.0)
        self._delete_torrent_file = bool(config.get("delete_torrent_file", True))
        self._delete_hardlink_file = bool(config.get("delete_hardlink_file", True))

        self._notify_enabled = bool(config.get("notify_enabled", True))
        self._notify_on_upload = bool(config.get("notify_on_upload", True))
        self._notify_on_cleanup = bool(config.get("notify_on_cleanup", True))
        self._notify_on_failed = bool(config.get("notify_on_failed", True))

        # 1. 停止旧调度器实例（避免配置重载遗留后台线程）
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None

        # 2. 基础组件初始化（即使未启用也能安全响应页面与状态查询）
        self.db_helper = Pan123TaskDatabase()
        self.db_helper.ensure_table()
        self.client = Pan123Client(client_id=self._client_id, client_secret=self._client_secret)
        self.qb_helper = QBHelper(
            host=self._qb_host,
            port=self._qb_port,
            username=self._qb_username,
            password=self._qb_password,
        )

        if not self._enabled:
            return

        tags_config = {
            "pending": self._tag_pending,
            "uploading": self._tag_uploading,
            "retrying": self._tag_retrying,
            "failed": self._tag_failed,
            "completed": self._tag_completed,
        }

        notify_config = {
            "enabled": self._notify_enabled,
            "notify_upload": self._notify_on_upload,
            "notify_cleanup": self._notify_on_cleanup,
            "notify_failed": self._notify_on_failed,
        }

        symedia_config = {
            "enabled": self._symedia_enabled,
            "url": self._symedia_url,
            "auth": self._symedia_auth,
            "mount_prefix": self._symedia_mount_prefix,
            "device_name": self._symedia_device_name,
            "user_name": self._symedia_user_name,
        }

        # 3. 启动任务调度器
        self.scheduler = TaskScheduler(
            client=self.client,
            qb_helper=self.qb_helper,
            db_helper=self.db_helper,
            tags_config=tags_config,
            notify_callback=self.notify,
            notify_config=notify_config,
            target_parent_file_id=self._parent_file_id,
            local_library_root=self._local_library_root,
            enable_path_cascade=self._enable_path_cascade,
            symedia_config=symedia_config,
        )
        self.scheduler.start()
        logger.info("Pan123Pilot (123云盘助手) 初始化完成并已启动后台调度")

    def get_state(self) -> bool:
        """返回插件是否启用"""
        return self._enabled

    def get_service(self) -> list[dict[str, Any]]:
        """注册周期性服务任务"""
        if not self._enabled:
            return []

        interval_min = max(1, int(self._retry_interval or 30))
        if IntervalTrigger:
            retry_trigger = IntervalTrigger(minutes=interval_min)
            cleanup_trigger = IntervalTrigger(hours=1)
        elif CronTrigger:
            cron_min = min(interval_min, 59)
            retry_trigger = CronTrigger.from_crontab(f"*/{cron_min} * * * *")
            cleanup_trigger = CronTrigger.from_crontab("0 * * * *")
        else:
            retry_trigger = "interval"
            cleanup_trigger = "interval"

        return [
            {
                "id": "pan123_retry_job",
                "name": "123云盘秒传定时重试",
                "trigger": retry_trigger,
                "func": self._service_retry_tasks,
                "kwargs": {},
            },
            {
                "id": "pan123_cleanup_job",
                "name": "123云盘做种达标安全清理",
                "trigger": cleanup_trigger,
                "func": self._service_cleanup_tasks,
                "kwargs": {},
            },
        ]

    def _service_retry_tasks(self) -> None:
        """定时重试服务绑定方法"""
        if not self.scheduler or not self._enabled:
            return
        retrying_tasks = self.db_helper.list_tasks_by_status(statuses=["RETRYING"]) if self.db_helper else []
        if not retrying_tasks:
            logger.debug("Pan123Pilot 定时重试检查：当前无待重试任务")
            return
        logger.info(f"Pan123Pilot 开始执行秒传任务定时重试，当前待重试任务数: {len(retrying_tasks)}...")
        self.scheduler.retry_retrying_tasks(enable_real_upload=self._enable_real_upload)

    def _service_cleanup_tasks(self) -> None:
        """定时做种达标清理绑定方法"""
        if not self.scheduler or not self._enabled or not self._enable_auto_cleanup:
            return
        completed_tasks = self.db_helper.list_tasks_by_status(statuses=["COMPLETED"]) if self.db_helper else []
        if not completed_tasks:
            logger.debug("Pan123Pilot 定时做种清理检查：当前无已完成任务")
            return
        logger.info(f"Pan123Pilot 开始执行做种达标检测与自动清理，当前待检测任务数: {len(completed_tasks)}...")
        self.scheduler.cleanup_completed_tasks(
            min_seed_time_hours=self._min_seed_time,
            min_ratio=self._min_seed_ratio,
            delete_torrent_file=self._delete_torrent_file,
            delete_hardlink_file=self._delete_hardlink_file,
        )

    def get_api(self) -> list[dict[str, Any]]:
        """动态 API 路由注册"""
        return [
            {
                "path": "/tasks",
                "endpoint": self._api_get_tasks,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取123云盘同步任务列表",
            },
            {
                "path": "/trigger_retry",
                "endpoint": self._api_trigger_retry,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "手动触发一次秒传重试",
            },
            {
                "path": "/trigger_cleanup",
                "endpoint": self._api_trigger_cleanup,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "手动触发一次做种清理检查",
            },
        ]

    def _api_get_tasks(self) -> dict:
        tasks = self.db_helper.list_all_tasks(limit=50) if self.db_helper else []
        return {"code": 0, "message": "ok", "data": tasks}

    def _api_trigger_retry(self) -> dict:
        self._service_retry_tasks()
        return {"code": 0, "message": "已手动触发秒传重试"}

    def _api_trigger_cleanup(self) -> dict:
        self._service_cleanup_tasks()
        return {"code": 0, "message": "已手动触发做种达标清理"}

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """Vuetify 配置表单及默认模型"""
        sys_qbs = get_system_qbittorrents()
        downloader_options = [
            {"title": f"【系统下载器】{q['name']} ({q['host']})", "value": q["id"]}
            for q in sys_qbs
        ]
        downloader_options.append({"title": "手动自定义配置 (手动填写 IP 与凭据)", "value": "manual"})

        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用 123云盘助手"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-3"},
                    },
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1 font-weight-bold"},
                        "text": "123 云盘开放平台凭证",
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "client_id",
                                            "label": "Client ID",
                                            "placeholder": "请输入 123 开放平台 Client ID",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "client_secret",
                                            "label": "Client Secret",
                                            "type": "password",
                                            "placeholder": "请输入 123 开放平台 Client Secret",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-3"},
                    },
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1 font-weight-bold"},
                        "text": "媒体库目录与多级级联设置",
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "local_library_root",
                                            "label": "本地媒体库根路径",
                                            "hint": "如 /video 或 /media，用于计算影视相对层级，留空则直传目标ID",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "target_parent_file_id",
                                            "label": "123 云盘根目录 ID",
                                            "hint": "对应本地根路径的云盘文件夹 ID，根目录请填 0",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enable_path_cascade",
                                            "label": "启用目录级联同步",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "retry_interval",
                                            "label": "秒传重试间隔 (分钟)",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_retry_count",
                                            "label": "最大秒传重试次数",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enable_real_upload",
                                            "label": "重试超限后允许真实分片上传",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-3"},
                    },
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1 font-weight-bold"},
                        "text": "qBittorrent 联动与标签设置",
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "downloader_select",
                                            "label": "选择已配置的 qBittorrent（自动复用凭证免输密码）",
                                            "items": downloader_options,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "qb_host",
                                            "label": "QB 访问地址 (手动模式生效)",
                                            "placeholder": "http://127.0.0.1:8080",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "qb_username", "label": "QB 用户名 (手动模式)"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "qb_password",
                                            "label": "QB 密码 (手动模式)",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [
                                    {"component": "VTextField", "props": {"model": "tag_pending", "label": "待处理标签"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [
                                    {"component": "VTextField", "props": {"model": "tag_uploading", "label": "处理中标签"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [
                                    {"component": "VTextField", "props": {"model": "tag_retrying", "label": "待重试标签"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VTextField", "props": {"model": "tag_failed", "label": "秒传失败标签"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VTextField", "props": {"model": "tag_completed", "label": "已上传标签"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-3"},
                    },
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1 font-weight-bold"},
                        "text": "Symedia Webhook 联动 (302直链播放与strm秒级生成)",
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "symedia_enabled",
                                            "label": "启用 Symedia Webhook",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "symedia_url",
                                            "label": "Symedia 服务地址",
                                            "placeholder": "http://127.0.0.1:8095",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "symedia_mount_prefix",
                                            "label": "Symedia 挂载前缀",
                                            "placeholder": "/CloudNAS/123/video",
                                            "hint": "合成发送给 Symedia 的 source_file 路径前缀",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "symedia_auth",
                                            "label": "Authorization 请求头 (选填)",
                                            "placeholder": "basic usernamepassword",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-3"},
                    },
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1 font-weight-bold"},
                        "text": "做种达标自动清理 (H&R 规则)",
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enable_auto_cleanup",
                                            "label": "启用做种达标清理",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_seed_time",
                                            "label": "最低做种时间 (小时)",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_seed_ratio",
                                            "label": "最低分享率要求",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "delete_torrent_file",
                                            "label": "清理 QB 种子及源数据",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "delete_hardlink_file",
                                            "label": "清理本地硬链接文件",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-3"},
                    },
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1 font-weight-bold"},
                        "text": "MoviePilot 系统消息通知",
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify_enabled",
                                            "label": "启用消息通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify_on_upload",
                                            "label": "秒传/上传成功通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify_on_cleanup",
                                            "label": "做种达标清理通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify_on_failed",
                                            "label": "同步失败报警通知",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ]

        defaults = {
            "enabled": False,
            "client_id": "",
            "client_secret": "",
            "target_parent_file_id": 0,
            "local_library_root": "",
            "enable_path_cascade": True,
            "retry_interval": 30,
            "max_retry_count": 10,
            "enable_real_upload": True,
            "downloader_select": "manual",
            "qb_host": "",
            "qb_username": "",
            "qb_password": "",
            "tag_pending": "123_待处理",
            "tag_uploading": "123_上传中",
            "tag_retrying": "123_待重试",
            "tag_failed": "123_秒传失败",
            "tag_completed": "123_已上传",
            "symedia_enabled": False,
            "symedia_url": "",
            "symedia_auth": "",
            "symedia_mount_prefix": "",
            "symedia_device_name": "MoviePilot-Pan123",
            "symedia_user_name": "admin",
            "enable_auto_cleanup": False,
            "min_seed_time": 48.0,
            "min_seed_ratio": 2.0,
            "delete_torrent_file": True,
            "delete_hardlink_file": True,
            "notify_enabled": True,
            "notify_on_upload": True,
            "notify_on_cleanup": True,
            "notify_on_failed": True,
        }
        return form, defaults

    def get_page(self) -> list[dict]:
        """返回插件详情页展示组件"""
        status_text = "已启用 (后台调度运行中)" if self._enabled else "未启用 (请在配置表单中启用并填入凭据)"
        cascade_text = "开启" if self._enable_path_cascade else "关闭"
        symedia_text = "已启用" if self._symedia_enabled else "未启用"
        dl_text = "手动模式" if self._downloader_select == "manual" else f"系统下载器 ({self._qb_host or '已绑定'})"
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info" if self._enabled else "warning",
                    "variant": "tonal",
                    "text": (
                        f"123云盘助手运行状态: {status_text} | "
                        f"目录级联: {cascade_text} (根: {self._local_library_root or '未指定'} -> ID: {self._parent_file_id}) | "
                        f"下载器: {dl_text} | Symedia 联动: {symedia_text}"
                    ),
                },
            }
        ]

    def stop_service(self) -> None:
        """彻底释放插件后台线程及资源"""
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None
        self._enabled = False
        logger.info("Pan123Pilot 后台调度服务已安全停止")


# 注册 MoviePilot 整理刮削完成事件监听（安全适配 TransferComplete / TransferCompleted）
_transfer_event = getattr(EventType, "TransferComplete", getattr(EventType, "TransferCompleted", None))


def pan123_on_transfer_completed(event: Any) -> None:
    """
    当 MoviePilot 刮削整理完成时，自动将新文件录入 123 云盘同步任务表
    """
    event_data = getattr(event, "event_data", None) or {}
    target_path = event_data.get("target_path") or event_data.get("dest")
    source_path = event_data.get("source_path") or event_data.get("src")
    torrent_hash = event_data.get("torrent_hash") or event_data.get("hash")

    if not target_path:
        return

    # 只处理实际存在的视频媒体文件
    if os.path.isdir(target_path):
        return

    filename = os.path.basename(target_path)
    file_size = os.path.getsize(target_path) if os.path.exists(target_path) else 0

    try:
        db = Pan123TaskDatabase()
        db.ensure_table()
        task_id = db.add_or_update_task(
            target_path=target_path,
            file_name=filename,
            file_size=file_size,
            torrent_hash=torrent_hash,
            source_path=source_path,
        )
        logger.info(f"Pan123Pilot 已捕获新整理媒体文件: {filename} (任务ID: {task_id})")
    except Exception as e:
        logger.error(f"Pan123Pilot 录入同步任务失败: {e}")


if _transfer_event and hasattr(eventmanager, "register"):
    try:
        pan123_on_transfer_completed = eventmanager.register(_transfer_event)(pan123_on_transfer_completed)
    except Exception as e:
        logger.warning(f"Pan123Pilot 注册 TransferComplete 事件监听失败: {e}")

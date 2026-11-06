"""
123云盘助手 (Pan123Pilot) - MoviePilot V3 插件入口
支持 123 云盘 SHA1 零流量秒传、断点续传、qBittorrent 状态标签联动与做种清理
"""

import os
import threading
from datetime import datetime
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
from .web_client import Pan123WebClient

_plugin_instance: Optional["Pan123Pilot"] = None

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".ts", ".iso", ".rmvb", ".avi", ".mov", ".flv", ".m2ts", ".wmv"
}


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
    plugin_version = "1.3.4"
    plugin_author = "lxj12328"
    author_url = "https://github.com/lxj12328/MoviePilot-Plugins"
    plugin_config_prefix = "pan123pilot_"
    plugin_order = 20
    auth_level = 1

    _enabled: bool = False
    _auth_mode: str = "web_token"
    _pan123_token: str = ""
    _pan123_username: str = ""
    _pan123_password: str = ""
    _qr_session: dict = {}
    _qr_poll_thread: Optional[threading.Thread] = None
    _qr_stop_event = threading.Event()
    _client_id: str = ""
    _client_secret: str = ""
    _parent_file_id: int = 0
    _local_library_root: str = ""
    _enable_path_cascade: bool = True
    _scan_history_on_save: bool = False
    _auto_scan_history: bool = False
    _retry_interval: int = 30
    _cleanup_interval: int = 60
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

    def _start_qr_polling(self, session_uuid: str) -> None:
        """启动后台异步轮询微信扫码状态线程"""
        import time

        if self._qr_poll_thread and self._qr_poll_thread.is_alive():
            self._qr_stop_event.set()
            try:
                self._qr_poll_thread.join(timeout=1.0)
            except Exception:
                pass

        self._qr_stop_event.clear()

        def _poll_worker():
            logger.info(f"Pan123Pilot 开始监听 123 微信扫码授权 (UUID: {session_uuid[:8]}...)...")
            for _ in range(90):  # 轮询 180 秒 (90 * 2s)
                if self._qr_stop_event.is_set():
                    break
                time.sleep(2)
                if not isinstance(self.client, Pan123WebClient):
                    break
                is_done, tok, msg = self.client.check_qr_login_status(session_uuid)
                if is_done and tok:
                    self._pan123_token = tok
                    try:
                        self.save_data("pan123_token", self._pan123_token)
                    except Exception:
                        pass
                    self._auth_status_msg = "已连接 (微信扫码登录成功，Token 已保存)"
                    logger.info("Pan123Pilot 微信扫码登录成功！已成功持久化保存 Token")
                    self.notify("123云盘登录成功", "微信扫码授权登录成功！已自动持久化保存 Token，后续免扫码免密自动运行！")
                    break
                elif "过期" in msg:
                    self._auth_status_msg = "扫码二维码已过期，请刷新二维码重新扫描"
                    break
                else:
                    self._auth_status_msg = f"等待微信扫码授权 ({msg})"

        self._qr_poll_thread = threading.Thread(target=_poll_worker, daemon=True)
        self._qr_poll_thread.start()

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
        self._auth_mode = str(config.get("auth_mode") or "web_token").strip()
        raw_token = str(config.get("pan123_token") or "").strip()
        if raw_token:
            self._pan123_token = Pan123WebClient._clean_token(raw_token)
            try:
                self.save_data("pan123_token", self._pan123_token)
            except Exception:
                pass
        else:
            try:
                cached_tok = self.get_data("pan123_token")
                if cached_tok:
                    self._pan123_token = Pan123WebClient._clean_token(cached_tok)
            except Exception:
                pass
        self._pan123_username = str(config.get("pan123_username") or "").strip()
        self._pan123_password = str(config.get("pan123_password") or "").strip()
        action_send_sms = bool(config.get("action_send_sms", False))
        pan123_sms_code = str(config.get("pan123_sms_code") or "").strip()
        action_refresh_qrcode = bool(config.get("action_refresh_qrcode", False))
        self._client_id = str(config.get("client_id") or "").strip()
        self._client_secret = str(config.get("client_secret") or "").strip()
        self._parent_file_id = int(config.get("target_parent_file_id") or 0)
        self._local_library_root = str(config.get("local_library_root") or "").strip()
        self._enable_path_cascade = bool(config.get("enable_path_cascade", True))
        self._scan_history_on_save = bool(config.get("scan_history_on_save", False))
        self._auto_scan_history = bool(config.get("auto_scan_history", False))
        try:
            self._retry_interval = max(1, int(config.get("retry_interval") or 30))
        except (ValueError, TypeError):
            self._retry_interval = 30

        try:
            self._cleanup_interval = max(1, int(config.get("cleanup_interval") or 60))
        except (ValueError, TypeError):
            self._cleanup_interval = 60

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
        if self._auth_mode in ("web_token", "web_qr", "web_sms", "web_account"):
            self.client = Pan123WebClient(
                username=self._pan123_username,
                password=self._pan123_password,
                token=self._pan123_token,
                auth_mode=self._auth_mode,
            )
        else:
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
        global _plugin_instance
        _plugin_instance = self
        logger.info("Pan123Pilot (123云盘助手) 初始化完成并已启动后台调度")

        # 4. 严格按照选定的 auth_mode 校验状态与鉴权，彻底杜绝跨模式越权降级
        self._auth_status_msg = "待检测"
        if isinstance(self.client, Pan123WebClient):
            # 场景 A：用户在表单中勾选了「立即发送短信验证码」
            if action_send_sms:
                if not self._pan123_username:
                    self._auth_status_msg = "发送短信失败: 未填写手机号"
                    logger.error("Pan123Pilot 发送短信验证码失败：未填写手机号，请在「123 账号 / 手机号」中输入！")
                else:
                    logger.info(f"Pan123Pilot 正在向手机号 {self._pan123_username} 请求发送短信验证码...")
                    ok_sms, msg_sms = self.client.send_sms_code(self._pan123_username)
                    if ok_sms:
                        self._auth_status_msg = f"短信已发送至 {self._pan123_username}，请查收并在表单中填入验证码后再次点击保存完成登录！"
                        logger.info(f"Pan123Pilot 短信验证码发送成功: {msg_sms}")
                        self.notify("123云盘短信验证码", f"短信验证码已下发至手机 {self._pan123_username}，请在插件表单填入 6 位验证码后点击保存完成登录！")
                    else:
                        self._auth_status_msg = f"发送短信失败: {msg_sms}"
                        logger.error(f"Pan123Pilot 发送短信失败: {msg_sms}")

            # 场景 B：用户填入了短信验证码，触发提交登录
            elif pan123_sms_code:
                logger.info(f"Pan123Pilot 正在使用短信验证码提交登录 (手机号: {self._pan123_username})...")
                ok_sms_login, msg_sms_login = self.client.login_by_sms_code(code=pan123_sms_code, phone=self._pan123_username)
                if ok_sms_login:
                    self._pan123_token = self.client.token
                    try:
                        self.save_data("pan123_token", self._pan123_token)
                    except Exception:
                        pass
                    self._auth_status_msg = "已连接 (短信登录成功，Token 已自动永久保存)"
                    logger.info(f"Pan123Pilot 短信登录成功！已成功持久化保存 Token (前缀: {self._pan123_token[:10]}...)")
                    self.notify("123云盘登录成功", "短信验证码校验成功！已永久保存登录 Token，后续秒传完全免密自动运行！")
                else:
                    self._auth_status_msg = f"短信登录失败: {msg_sms_login}"
                    logger.error(f"Pan123Pilot 短信登录失败: {msg_sms_login}")

            # 场景 C：微信/APP 扫码登录模式 (web_qr) 或勾选了「生成/刷新微信登录二维码」
            elif self._auth_mode == "web_qr" or action_refresh_qrcode:
                if self._pan123_token and not action_refresh_qrcode and self.client.check_token_validity():
                    u_name = self.client.user_info.get("nickname") or self.client.user_info.get("username") or ""
                    user_desc = f" (用户: {u_name})" if u_name else ""
                    self._auth_status_msg = f"已连接 (扫码登录 Token 有效{user_desc})"
                    logger.info(f"Pan123Pilot 123云盘扫码登录 Token 校验有效，连接正常！{user_desc}")
                else:
                    logger.info("Pan123Pilot 正在生成 123 微信/App 扫码登录凭证与二维码...")
                    ok_qr, qr_data, msg_qr = self.client.generate_qr_login()
                    if ok_qr and qr_data:
                        self._qr_session = qr_data
                        self._auth_status_msg = "待扫码 (请进入插件详情页用微信扫码授权)"
                        logger.info(f"Pan123Pilot 微信登录二维码已生成！授权链接: {qr_data.get('url')}")
                        self._start_qr_polling(qr_data["uuid"])
                        self.notify("123云盘扫码登录", "已为您生成 123 微信扫码授权二维码，请进入 MoviePilot 插件详情页用微信扫码授权登录！")
                    else:
                        self._auth_status_msg = f"生成二维码失败: {msg_qr}"
                        logger.error(f"Pan123Pilot 生成二维码失败: {msg_qr}")

            # 场景 D：Web Token 直填模式 (web_token) —— 严格使用填入的 Token，100% 免疫密码登录风控与滑块，绝不自动降级！
            elif self._auth_mode == "web_token":
                if not self._pan123_token:
                    self._auth_status_msg = "未配置 Web Token (请在表单中粘贴 authorToken)"
                    logger.warning("Pan123Pilot 当前选择 Web Token 直填模式，但未填写 Token。请在浏览器按 F12 复制 authorToken 粘贴保存！")
                else:
                    logger.info(f"Pan123Pilot 正在校验 Web Token 有效性 (前缀: {self._pan123_token[:10]}...)...")
                    if self.client.check_token_validity():
                        u_name = self.client.user_info.get("nickname") or self.client.user_info.get("username") or ""
                        user_desc = f" (用户: {u_name})" if u_name else ""
                        self._auth_status_msg = f"已连接 (Web Token 有效{user_desc})"
                        logger.info(f"Pan123Pilot 123云盘 Web Token 校验成功，连接正常！{user_desc}")
                        try:
                            self.save_data("pan123_token", self._pan123_token)
                        except Exception:
                            pass
                    else:
                        self._auth_status_msg = "Web Token 校验未通过: Token 无效或已过期，请在 123 网页端重新复制 authorToken 填入"
                        logger.error("Pan123Pilot 123云盘 Web Token 校验未通过，请检查是否完整复制了 authorToken！")

            # 场景 D：账号密码模式 (web_account)
            elif self._auth_mode == "web_account":
                if not self._pan123_username or not self._pan123_password:
                    self._auth_status_msg = "未配置 123 账号或密码"
                    logger.warning("Pan123Pilot 账号密码模式下未完整配置账号与密码！")
                else:
                    logger.info(f"Pan123Pilot 正在使用 123 账号密码登录 (账号: {self._pan123_username})...")
                    ok, msg = self.client.login_by_password()
                    if ok:
                        self._auth_status_msg = "已连接 (账号密码登录成功)"
                        self._pan123_token = self.client.token
                        logger.info(f"Pan123Pilot 123云盘账号密码登录成功！(前缀: {self.client.token[:10]}...)")
                        try:
                            self.save_data("pan123_token", self._pan123_token)
                        except Exception:
                            pass
                    else:
                        self._auth_status_msg = f"登录失败: {msg}"
                        logger.error(f"Pan123Pilot 123云盘登录失败: {msg}")

            # 场景 E：短信验证码模式 (web_sms) 下无新操作时的探活
            elif self._auth_mode == "web_sms":
                if self._pan123_token and self.client.check_token_validity():
                    self._auth_status_msg = "已连接 (短信登录 Token 有效)"
                else:
                    self._auth_status_msg = "未登录 (请在下方勾选「立即发送短信验证码」并输入验证码完成登录)"

        elif isinstance(self.client, Pan123Client):
            tok = self.client.get_access_token()
            if tok:
                self._auth_status_msg = "已连接 (OpenAPI Token 有效)"
                logger.info("Pan123Pilot OpenAPI Token 获取成功，连接正常！")
            else:
                self._auth_status_msg = "OpenAPI Token 获取失败"
                logger.error("Pan123Pilot OpenAPI Token 获取失败，请检查 Client ID / Secret")

        # 检查是否需要扫描本地历史已整理媒体
        if self._enabled and (self._scan_history_on_save or self._auto_scan_history):
            logger.info("Pan123Pilot 已开启历史媒体扫描，正在启动后台异步补录...")
            if self._scan_history_on_save:
                self._scan_history_on_save = False
            threading.Thread(
                target=self._scan_and_sync_history_library,
                daemon=True,
            ).start()

    def get_state(self) -> bool:
        """返回插件是否启用"""
        return self._enabled

    def get_service(self) -> list[dict[str, Any]]:
        """注册周期性服务任务：调度周期直接基于用户设置的分钟间隔运行"""
        if not self._enabled:
            return []

        interval_min = max(1, int(self._retry_interval or 30))
        cleanup_min = max(1, int(self._cleanup_interval or 60))

        if IntervalTrigger:
            retry_trigger = IntervalTrigger(minutes=interval_min)
            cleanup_trigger = IntervalTrigger(minutes=cleanup_min)
        elif CronTrigger:
            retry_trigger = CronTrigger.from_crontab(f"*/{min(interval_min, 59)} * * * *")
            cleanup_trigger = CronTrigger.from_crontab(f"*/{min(cleanup_min, 59)} * * * *")
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

        interval_min = max(1, int(self._retry_interval or 30))
        due_tasks = []
        for t in retrying_tasks:
            up_val = t.get("updated_at")
            if not up_val:
                due_tasks.append(t)
                continue
            try:
                last_time = up_val if isinstance(up_val, datetime) else datetime.strptime(str(up_val), "%Y-%m-%d %H:%M:%S")
                if (datetime.now() - last_time).total_seconds() >= interval_min * 60:
                    due_tasks.append(t)
            except Exception:
                due_tasks.append(t)

        if not due_tasks:
            logger.debug(f"Pan123Pilot 定时重试检查：当前有 {len(retrying_tasks)} 个待重试任务，但均未达到设定的重试间隔时间 ({interval_min}分钟)")
            return

        logger.info(f"Pan123Pilot 开始执行秒传任务定时重试，达到重试间隔 ({interval_min}分钟) 的任务数: {len(due_tasks)}...")
        self.scheduler.retry_retrying_tasks(enable_real_upload=self._enable_real_upload, retry_interval_minutes=interval_min)

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
            {
                "path": "/scan_library",
                "endpoint": self._api_scan_library,
                "methods": ["POST", "GET"],
                "auth": "bear",
                "summary": "手动扫描本地媒体库并补录待同步媒体文件",
            },
            {
                "path": "/qr_code",
                "endpoint": self._api_get_qr_code,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "获取或刷新 123 微信扫码登录二维码及状态",
            },
            {
                "path": "/send_sms",
                "endpoint": self._api_send_sms,
                "methods": ["POST", "GET"],
                "auth": "bear",
                "summary": "发送 123 云盘登录短信验证码",
            },
            {
                "path": "/verify_sms",
                "endpoint": self._api_verify_sms,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "提交短信验证码登录并自动保存 Token",
            },
            {
                "path": "/test_auth",
                "endpoint": self._api_test_auth,
                "methods": ["POST", "GET"],
                "auth": "bear",
                "summary": "测试当前 123 云盘认证与秒传通道状态",
            },
        ]

    def _api_get_tasks(self) -> dict:
        tasks = self.db_helper.list_all_tasks(limit=50) if self.db_helper else []
        return {"code": 0, "message": "ok", "data": tasks}

    def _api_trigger_retry(self) -> dict:
        if self.scheduler and self._enabled:
            self.scheduler.retry_retrying_tasks(enable_real_upload=self._enable_real_upload, retry_interval_minutes=0)
        return {"code": 0, "message": "已手动触发秒传重试"}

    def _api_trigger_cleanup(self) -> dict:
        self._service_cleanup_tasks()
        return {"code": 0, "message": "已手动触发做种达标清理"}

    def _api_get_qr_code(self, refresh: bool = False) -> dict:
        """获取或刷新当前微信扫码登录信息"""
        if not isinstance(self.client, Pan123WebClient):
            return {"code": 1, "message": "仅在 Web 模式下支持微信扫码登录"}
        if refresh or not self._qr_session or not self._qr_session.get("uuid"):
            ok, data, msg = self.client.generate_qr_login()
            if ok and data:
                self._qr_session = data
                self._start_qr_polling(data["uuid"])
                return {"code": 0, "message": "二维码已生成", "data": self._qr_session}
            return {"code": 1, "message": f"生成二维码失败: {msg}"}
        return {"code": 0, "message": "ok", "data": self._qr_session}

    def _api_send_sms(self, phone: Optional[str] = None) -> dict:
        """向指定手机号发送 123 短信验证码"""
        target = phone or self._pan123_username
        if isinstance(self.client, Pan123WebClient):
            ok, msg = self.client.send_sms_code(target)
            return {"code": 0 if ok else 1, "message": msg}
        return {"code": 1, "message": "仅在 Web 模式下支持短信验证码发送"}

    def _api_verify_sms(self, code: str, phone: Optional[str] = None) -> dict:
        """校验短信验证码并保存 Token"""
        target = phone or self._pan123_username
        if isinstance(self.client, Pan123WebClient):
            ok, msg = self.client.login_by_sms_code(code=code, phone=target)
            if ok:
                self._pan123_token = self.client.token
                try:
                    self.save_data("pan123_token", self._pan123_token)
                except Exception:
                    pass
                return {"code": 0, "message": "短信验证成功！已自动保存 Token", "token": self._pan123_token}
            return {"code": 1, "message": msg}
        return {"code": 1, "message": "仅在 Web 模式下支持短信验证"}

    def _api_test_auth(self) -> dict:
        """测试 123 云盘认证状态"""
        if isinstance(self.client, Pan123WebClient):
            ok, msg = self.client.ensure_authenticated()
            if ok:
                return {"code": 0, "message": f"Web 鉴权成功！当前 Token 有效 (前缀: {self.client.token[:10]}...)"}
            return {"code": 1, "message": f"Web 鉴权失败: {msg}"}
        elif isinstance(self.client, Pan123Client):
            token = self.client.get_access_token()
            if token:
                return {"code": 0, "message": f"OpenAPI Token 获取成功: {token[:10]}..."}
            return {"code": 1, "message": "OpenAPI Token 获取失败，请检查 Client ID / Secret"}
        return {"code": 1, "message": "客户端未初始化"}

    def _scan_and_sync_history_library(self) -> int:
        """
        深度扫描本地已整理好的历史媒体文件，自动关联 MoviePilot 整理历史或 qB 种子，
        补录入库并触发全链路秒传与标签回写。
        """
        try:
            if not self._local_library_root or not os.path.exists(self._local_library_root):
                logger.warning(f"Pan123Pilot 历史媒体库扫描跳过：路径不存在或未配置 ({self._local_library_root})")
                return 0

            logger.info(f"Pan123Pilot 开始扫描本地媒体库历史文件: {self._local_library_root} ...")
            task_ids = _record_transferred_target(self._local_library_root)
            logger.info(f"Pan123Pilot 本地历史媒体扫描完成，共识别与补录 {len(task_ids)} 个媒体文件")
            return len(task_ids)
        except Exception as e:
            logger.error(f"Pan123Pilot 历史媒体库扫描异常: {e}", exc_info=True)
            return 0

    def _api_scan_library(self) -> dict:
        """手动扫描本地媒体库根目录，将未录入的任务录入数据库并立即触发同步"""
        if not self._local_library_root or not os.path.exists(self._local_library_root):
            return {"code": 1, "message": f"本地媒体库路径未配置或在容器内不存在: {self._local_library_root}"}

        count = self._scan_and_sync_history_library()
        return {"code": 0, "message": f"媒体库扫描完成，已录入并检查 {count} 个媒体文件"}

    def on_transfer_completed(self, event: Any) -> None:
        """MoviePilot 插件类反射事件监听入口"""
        pan123_on_transfer_completed(event)

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
                        "text": "123 云盘登录凭证与认证模式 (免开发者包付费)",
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "auth_mode",
                                            "label": "认证方式 (强烈推荐 Web Token 直填模式，100% 免疫滑块风控)",
                                            "items": [
                                                {"title": "微信/APP 扫码登录 (强烈推荐，免输密码防 [7012] 境外风控)", "value": "web_qr"},
                                                {"title": "Web Token 直填模式 (强烈推荐，100% 免疫境外/机房风控与滑块)", "value": "web_token"},
                                                {"title": "手机短信验证码登录 (解决境外/机房 IP [7012] 风险)", "value": "web_sms"},
                                                {"title": "账号密码模式 (模拟网页端自动登录)", "value": "web_account"},
                                                {"title": "OpenAPI 开放平台模式 (需购买官方开发者权益包)", "value": "openapi"},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "pan123_token",
                                            "label": "123 云盘 Web Token (authorToken)",
                                            "placeholder": "浏览器打开 123pan.com 登录后，F12 -> Application -> Local Storage 复制 authorToken",
                                            "hint": "填入后直接免密调用秒传接口，100% 避开任何滑块与验证码拦截 (支持自动去除首尾引号与 Bearer 前缀)",
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
                                            "model": "pan123_username",
                                            "label": "123 账号 / 手机号",
                                            "placeholder": "账号密码模式或短信模式下填写绑定的手机号",
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
                                            "model": "pan123_password",
                                            "label": "123 登录密码",
                                            "type": "password",
                                            "placeholder": "账号密码模式下填写 123 登录密码 (短信与 Token 模式无需填写)",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "action_refresh_qrcode",
                                            "label": "生成/刷新微信登录二维码 (勾选后点保存)",
                                            "hint": "保存后插件后台将生成最新微信授权登录二维码，可在插件详情页用手机扫码完成免密登录",
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "action_send_sms",
                                            "label": "立即发送短信验证码 (勾选后点保存，发送6位验证码到手机)",
                                            "hint": "填好手机号后开启此项并点击保存，插件将向手机发送短信验证码",
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
                                            "model": "pan123_sms_code",
                                            "label": "短信验证码 (收到后填入6位验证码，再次点击保存完成登录)",
                                            "placeholder": "6位数字验证码",
                                            "hint": "填入收到的短信验证码后点击保存，插件将自动完成登录并永久保存 Token",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
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
                                            "label": "OpenAPI Client ID (仅 openapi 模式需填)",
                                            "placeholder": "自建应用 Client ID",
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
                                            "label": "OpenAPI Client Secret (仅 openapi 模式需填)",
                                            "type": "password",
                                            "placeholder": "自建应用 Client Secret",
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
                                            "placeholder": "30",
                                            "hint": "秒传未命中时，每隔多少分钟对排队任务重新发起一次秒传检测（默认 30 分钟）",
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
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "scan_history_on_save",
                                            "label": "保存时扫描补录历史媒体 (自动关联 qB 种子)",
                                            "hint": "保存配置后后台自动遍历本地媒体库，关联种子并把未同步的影视加入秒传队列",
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_scan_history",
                                            "label": "启动时自动扫描补录历史媒体",
                                            "hint": "每次 MoviePilot 启动或重载时，自动检查并补录未同步文件",
                                            "persistent-hint": True,
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
                                            "model": "cleanup_interval",
                                            "label": "清理巡检间隔 (分钟)",
                                            "placeholder": "60",
                                            "hint": "每隔多少分钟巡检一次已完成任务并检查做种是否达标（默认 60 分钟）",
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
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
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
                                "props": {"cols": 12, "md": 6},
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
            "auth_mode": "web_token",
            "pan123_token": "",
            "pan123_username": "",
            "pan123_password": "",
            "action_refresh_qrcode": False,
            "action_send_sms": False,
            "pan123_sms_code": "",
            "client_id": "",
            "client_secret": "",
            "target_parent_file_id": 0,
            "local_library_root": "",
            "enable_path_cascade": True,
            "scan_history_on_save": False,
            "auto_scan_history": False,
            "retry_interval": 30,
            "cleanup_interval": 60,
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
        auth_info = getattr(self, "_auth_status_msg", "待检测")
        is_auth_ok = "已连接" in auth_info
        status_text = "已启用 (后台调度运行中)" if self._enabled else "未启用"
        cascade_text = "开启" if self._enable_path_cascade else "关闭"
        symedia_text = "已启用" if self._symedia_enabled else "未启用"
        dl_text = "手动模式" if self._downloader_select == "manual" else f"系统下载器 ({self._qb_host or '已绑定'})"

        components = [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if is_auth_ok else ("info" if self._enabled else "warning"),
                    "variant": "tonal",
                    "text": (
                        f"123云盘连接状态: {auth_info} | "
                        f"运行状态: {status_text} | "
                        f"秒传重试间隔: {self._retry_interval} 分钟 | 做种清理巡检: {self._cleanup_interval} 分钟 | "
                        f"目录级联: {cascade_text} (根: {self._local_library_root or '未指定'} -> ID: {self._parent_file_id}) | "
                        f"下载器: {dl_text} | Symedia 联动: {symedia_text}"
                    ),
                },
            }
        ]

        # 若未成功连接且已生成扫码会话，在页面直观渲染二维码供用户手机扫码
        if not is_auth_ok and isinstance(self._qr_session, dict) and self._qr_session.get("img_url"):
            components.append({
                "component": "VCard",
                "props": {"class": "mt-3 pa-4 text-center"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-h6 font-weight-bold justify-center"},
                        "text": "📱 123云盘 微信/APP 扫码登录 (免输密码，100% 避开 [7012] 境外风控)",
                    },
                    {
                        "component": "VCardSubtitle",
                        "props": {"class": "text-body-2 mb-2"},
                        "text": "打开手机微信或 123云盘 App 扫描下方二维码，点击「授权登录」后插件将自动保存 Token 永久使用",
                    },
                    {
                        "component": "VImg",
                        "props": {
                            "src": self._qr_session.get("img_url"),
                            "max-width": 220,
                            "class": "mx-auto my-3 elevation-2 rounded",
                        },
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "text-caption text-grey"},
                        "text": f"若手机无法直接扫码，可在微信中打开此授权链接：{self._qr_session.get('url')}",
                    },
                ],
            })

        return components

    def stop_service(self) -> None:
        """彻底释放插件后台线程及资源"""
        global _plugin_instance
        _plugin_instance = None
        self._qr_stop_event.set()
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None
        self._enabled = False
        logger.info("Pan123Pilot 后台调度服务已安全停止")


def _extract_transfer_info(event: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    全面解析 MoviePilot 各版本 TransferComplete 事件携带的数据对象。
    支持从 transferinfo 对象/字典、target_item/target_diritem、root event_data 等全方位提取。
    返回 (target_path, source_path, torrent_hash)
    """
    event_data = getattr(event, "event_data", None)
    if event_data is None:
        if isinstance(event, dict):
            event_data = event
        else:
            event_data = {}

    target_path = None
    source_path = None
    torrent_hash = None

    # 1. 尝试从 transferinfo / transfer_info 中提取（MoviePilot V3 核心传输上下文结构）
    transfer_info = event_data.get("transferinfo") or event_data.get("transfer_info")
    if transfer_info:
        # 目标文件/目录路径提取 (target_item / target_diritem / target_path / dest)
        target_item = getattr(transfer_info, "target_item", None) or (
            transfer_info.get("target_item") if isinstance(transfer_info, dict) else None
        )
        if target_item:
            target_path = getattr(target_item, "path", None) or (
                target_item.get("path") if isinstance(target_item, dict) else str(target_item)
            )

        if not target_path:
            target_diritem = getattr(transfer_info, "target_diritem", None) or (
                transfer_info.get("target_diritem") if isinstance(transfer_info, dict) else None
            )
            if target_diritem:
                target_path = getattr(target_diritem, "path", None) or (
                    target_diritem.get("path") if isinstance(target_diritem, dict) else str(target_diritem)
                )

        if not target_path:
            target_path = getattr(transfer_info, "target_path", None) or (
                transfer_info.get("target_path") if isinstance(transfer_info, dict) else None
            )

        # 源文件路径提取
        file_item = (
            getattr(transfer_info, "fileitem", None)
            or getattr(transfer_info, "source_item", None)
            or (transfer_info.get("fileitem") if isinstance(transfer_info, dict) else None)
        )
        if file_item:
            source_path = getattr(file_item, "path", None) or (
                file_item.get("path") if isinstance(file_item, dict) else str(file_item)
            )

        # Hash 提取
        torrent_hash = (
            getattr(transfer_info, "download_hash", None)
            or getattr(transfer_info, "torrent_hash", None)
            or getattr(transfer_info, "hash", None)
        )
        if not torrent_hash and isinstance(transfer_info, dict):
            torrent_hash = (
                transfer_info.get("download_hash")
                or transfer_info.get("torrent_hash")
                or transfer_info.get("hash")
            )

    # 2. 尝试从根层级直接提取
    if not target_path:
        target_item = event_data.get("target_item")
        if target_item:
            target_path = getattr(target_item, "path", None) or (
                target_item.get("path") if isinstance(target_item, dict) else str(target_item)
            )
    if not target_path:
        target_diritem = event_data.get("target_diritem")
        if target_diritem:
            target_path = getattr(target_diritem, "path", None) or (
                target_diritem.get("path") if isinstance(target_diritem, dict) else str(target_diritem)
            )
    if not target_path:
        target_path = (
            event_data.get("target_path")
            or event_data.get("dest")
            or event_data.get("dest_path")
            or event_data.get("file_path")
            or event_data.get("path")
        )

    if not source_path:
        source_item = event_data.get("fileitem") or event_data.get("source_item")
        if source_item:
            source_path = getattr(source_item, "path", None) or (
                source_item.get("path") if isinstance(source_item, dict) else str(source_item)
            )
        if not source_path:
            source_path = event_data.get("source_path") or event_data.get("src")

    if not torrent_hash:
        torrent_hash = (
            event_data.get("torrent_hash")
            or event_data.get("hash")
            or event_data.get("download_hash")
        )

    target_str = str(target_path).strip() if target_path else None
    source_str = str(source_path).strip() if source_path else None
    hash_str = str(torrent_hash).strip() if torrent_hash else None
    return target_str, source_str, hash_str


def _query_transfer_history(dest_path: str) -> tuple[Optional[str], Optional[str]]:
    """
    从 MoviePilot 整理历史库中查询 (torrent_hash, src)。
    优先通过精确路径匹配，其次通过文件名模糊匹配。
    """
    if not dest_path:
        return None, None
    filename = os.path.basename(dest_path)
    try:
        from app.db import Engine
        if Engine is not None:
            try:
                from sqlalchemy import text
                stmt = text(
                    "SELECT download_hash, src FROM transferhistory WHERE dest = :dest OR dest LIKE :dest_pattern ORDER BY id DESC LIMIT 1"
                )
            except Exception:
                stmt = "SELECT download_hash, src FROM transferhistory WHERE dest = :dest OR dest LIKE :dest_pattern ORDER BY id DESC LIMIT 1"
            with Engine.connect() as conn:
                row = conn.execute(stmt, {"dest": dest_path, "dest_pattern": f"%{filename}%"}).fetchone()
                if row:
                    download_hash = str(row[0] or "").strip() or None
                    src = str(row[1] or "").strip() or None
                    return download_hash, src
    except Exception:
        pass
    return None, None


def _record_transferred_target(
    target_path: str,
    source_path: Optional[str] = None,
    torrent_hash: Optional[str] = None,
) -> list[int]:
    """
    将整理后的目标文件或目录内的媒体录入任务库并触发处理。
    支持单一文件与目录多级遍历，自动关联 MoviePilot 整理历史与 qB 种子 Hash。
    """
    if not target_path or not os.path.exists(target_path):
        return []

    global _plugin_instance
    task_ids = []
    db = getattr(_plugin_instance, "db_helper", None) if _plugin_instance else None
    if not db:
        db = Pan123TaskDatabase()
    db.ensure_table()

    # 待处理的文件清单：[(file_full_path, file_name)]
    target_files: list[tuple[str, str]] = []
    if os.path.isdir(target_path):
        logger.info(f"Pan123Pilot 正在递归检索视频文件: {target_path} ...")
        for root, _, files in os.walk(target_path):
            for f in sorted(files):
                ext = os.path.splitext(f)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    target_files.append((os.path.join(root, f), f))
    else:
        ext = os.path.splitext(target_path)[1].lower()
        if ext in VIDEO_EXTENSIONS:
            target_files.append((target_path, os.path.basename(target_path)))

    logger.info(f"Pan123Pilot 在【{target_path}】下共检索到 {len(target_files)} 个符合条件的视频文件")
    if not target_files:
        logger.warning(
            f"Pan123Pilot 在【{target_path}】下未发现视频文件 (支持格式: {', '.join(sorted(VIDEO_EXTENSIONS))})，若这是媒体库根路径，请检查路径映射是否正确。"
        )
        return []

    # 预获取 qBittorrent 当前所有种子，避免每检查一个文件都发起一次网络请求
    qb_torrents = None
    if _plugin_instance and _plugin_instance.qb_helper and _plugin_instance.qb_helper.is_configured():
        try:
            qb_torrents = _plugin_instance.qb_helper.get_all_torrents()
            logger.info(f"Pan123Pilot 已从 qBittorrent 预获取 {len(qb_torrents)} 个种子用于智能关联")
        except Exception as e:
            logger.warning(f"Pan123Pilot 预获取 qBittorrent 种子列表失败: {e}")

    all_plugin_tags: list[str] = []
    if _plugin_instance:
        all_plugin_tags = [
            _plugin_instance._tag_pending,
            _plugin_instance._tag_uploading,
            _plugin_instance._tag_retrying,
            _plugin_instance._tag_failed,
            _plugin_instance._tag_completed,
        ]

    for file_full, fname in target_files:
        try:
            # 检查是否已存在于数据库且已处理完成
            existing = db.get_task_by_target(target_path=file_full)
            if existing and existing.get("status") in ("COMPLETED", "CLEANED"):
                logger.debug(f"Pan123Pilot 媒体已处理完成，跳过: {fname} (状态: {existing.get('status')})")
                continue

            f_size = os.path.getsize(file_full) if os.path.exists(file_full) else 0
            if f_size == 0:
                continue

            cur_hash = torrent_hash
            cur_src = source_path

            # 1. 若无 hash，尝试从 MoviePilot 整理历史库查询
            if not cur_hash:
                cur_hash, cur_src = _query_transfer_history(file_full)

            # 2. 若仍未匹配到 hash，尝试从 qBittorrent 逆向搜寻
            if not cur_hash and _plugin_instance and _plugin_instance.qb_helper and _plugin_instance.qb_helper.is_configured():
                cur_hash = _plugin_instance.qb_helper.match_torrent_by_path(file_full, torrents=qb_torrents)

            # 3. 录入或更新任务
            tid = db.add_or_update_task(
                target_path=file_full,
                file_name=fname,
                file_size=f_size,
                torrent_hash=cur_hash,
                source_path=cur_src,
            )
            task_ids.append(tid)
            logger.info(
                f"Pan123Pilot 已录入媒体任务: {fname} (任务ID: {tid}, 关联种子: {cur_hash or '未关联'})"
            )

            # 4. 联动 qBittorrent 打上“待处理”标签
            if cur_hash and _plugin_instance and _plugin_instance.qb_helper and _plugin_instance.qb_helper.is_configured():
                try:
                    _plugin_instance.qb_helper.update_tags(
                        cur_hash,
                        _plugin_instance._tag_pending,
                        all_plugin_tags,
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Pan123Pilot 录入同步任务失败 [{fname}]: {e}")

    # 5. 若有新任务入库且调度器正在运行，即刻启动处理
    if task_ids and _plugin_instance and _plugin_instance.scheduler and _plugin_instance.get_state():
        logger.info(f"Pan123Pilot 本次共新增/更新 {len(task_ids)} 个待同步任务，立即启动后台流式处理...")
        try:
            threading.Thread(target=_plugin_instance.scheduler.process_pending_tasks, daemon=True).start()
        except Exception as e:
            logger.error(f"Pan123Pilot 启动上传处理线程失败: {e}")

    return task_ids


def pan123_on_transfer_completed(event: Any) -> None:
    """
    当 MoviePilot 刮削整理完成时，自动将新文件录入 123 云盘同步任务表并即刻触发同步
    """
    try:
        target_path, source_path, torrent_hash = _extract_transfer_info(event)
        if not target_path:
            logger.debug(f"Pan123Pilot TransferComplete 事件中未能解析出目标文件路径: {getattr(event, 'event_data', event)}")
            return
        logger.info(f"Pan123Pilot 监听到整理完成事件: target={target_path}, hash={torrent_hash}")
        _record_transferred_target(target_path, source_path, torrent_hash)
    except Exception as e:
        logger.error(f"Pan123Pilot 处理 TransferComplete 事件异常: {e}")


# 多重事件枚举与名称动态注册（安全适配 TransferComplete / TransferCompleted / 字符串枚举）
_transfer_events: list[Any] = []
for attr in ("TransferComplete", "TransferCompleted", "transfer.complete", "Transfer"):
    val = getattr(EventType, attr, None)
    if val and val not in _transfer_events:
        _transfer_events.append(val)

if hasattr(eventmanager, "register"):
    for ev in _transfer_events:
        try:
            eventmanager.register(ev)(pan123_on_transfer_completed)
        except Exception as e:
            logger.warning(f"Pan123Pilot 注册事件监听 {ev} 失败: {e}")

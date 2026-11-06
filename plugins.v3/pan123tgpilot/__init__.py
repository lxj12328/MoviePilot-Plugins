"""
123云盘TG转存助手 (Pan123TgPilot) - MoviePilot V3 插件入口
支持 Telegram 影视频道消息自动监听、电影与剧集智能分流转存、123秒传/分享链接适配与画质洗版升级
"""

import threading
import time
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

from .client import Pan123TgClient
from .models import Pan123TgDatabase
from .parser import TgMessageParser
from .ranker import calculate_quality_score, evaluate_upgrade
from .tg_watcher import TgChannelWatcher

_plugin_instance: Optional["Pan123TgPilot"] = None


class Pan123TgPilot(_PluginBase):
    """
    123云盘TG转存助手
    """
    # 插件元信息
    plugin_name = "123云盘TG转存助手"
    plugin_desc = "MoviePilot V3 的 123 云盘 Telegram 频道智能转存与画质洗版助手。"
    plugin_version = "1.0.3"
    plugin_author = "lxj12328"

    def __init__(self):
        super().__init__()
        self._enabled: bool = False
        self._auth_token: str = ""
        self._movie_target_dir_id: int = 0
        self._tv_target_dir_id: int = 0
        self._tg_channels: list[str] = []
        self._tg_channels_raw: str = ""
        self._tg_proxy: str = ""
        self._tg_bot_token: str = ""
        self._check_interval: int = 10
        self._auto_upgrade: bool = True
        self._auto_delete_old_version: bool = True
        self._notify_enabled: bool = False

        self._client: Optional[Pan123TgClient] = None
        self._watcher: Optional[TgChannelWatcher] = None
        self._db: Optional[Pan123TgDatabase] = None

        self._lock = threading.Lock()
        self._is_checking: bool = False
        self._last_check_time: Optional[datetime] = None
        self._check_stats: dict[str, int] = {
            "total_checked": 0,
            "transferred_count": 0,
            "upgraded_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
        }

    def notify(self, title: str, text: str) -> None:
        """安全发送 MoviePilot 系统通知"""
        if not self._notify_enabled:
            return
        try:
            if hasattr(self, "post_message"):
                self.post_message(title=title, text=text)
        except TypeError:
            try:
                self.post_message(text=f"【{title}】\n{text}")
            except Exception as e:
                logger.error(f"Pan123TgPilot 发送通知失败: {e}")
        except Exception as e:
            logger.error(f"Pan123TgPilot 发送通知失败: {e}")

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置并初始化插件运行时状态，必须支持重复调用"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._auth_token = str(config.get("pan123_token") or "").strip()

        try:
            self._movie_target_dir_id = int(config.get("movie_target_dir_id") or 0)
        except (ValueError, TypeError):
            self._movie_target_dir_id = 0

        try:
            self._tv_target_dir_id = int(config.get("tv_target_dir_id") or 0)
        except (ValueError, TypeError):
            self._tv_target_dir_id = 0

        self._tg_channels_raw = str(config.get("tg_channels") or "").strip()
        channels = []
        for ch in self._tg_channels_raw.replace("，", ",").replace("\n", ",").split(","):
            c = ch.strip()
            if c:
                channels.append(c)
        self._tg_channels = channels

        # 代理自动探测逻辑：优先读取插件配置，若留空自动继承 MoviePilot 全局代理或系统环境变量
        self._tg_proxy = str(config.get("tg_proxy") or "").strip()
        if not self._tg_proxy:
            try:
                from app.core.config import settings
                sys_p = getattr(settings, "PROXY", None)
                if sys_p:
                    self._tg_proxy = str(sys_p).strip()
            except Exception:
                try:
                    from app.sdk.config import settings
                    sys_p = getattr(settings, "PROXY", None)
                    if sys_p:
                        self._tg_proxy = str(sys_p).strip()
                except Exception:
                    pass

        if not self._tg_proxy:
            import os
            for env_k in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy"):
                val = os.environ.get(env_k)
                if val:
                    self._tg_proxy = val.strip()
                    break

        self._tg_bot_token = str(config.get("tg_bot_token") or "").strip()

        try:
            self._check_interval = max(1, int(config.get("check_interval") or 10))
        except (ValueError, TypeError):
            self._check_interval = 10

        self._auto_upgrade = bool(config.get("auto_upgrade", True))
        self._auto_delete_old_version = bool(config.get("auto_delete_old_version", True))
        self._notify_enabled = bool(config.get("notify_enabled", False))

        # 初始化数据库
        if not self._db:
            self._db = Pan123TgDatabase()
        self._db.ensure_table()

        # 初始化 123 客户端
        if self._auth_token:
            self._client = Pan123TgClient(token=self._auth_token)
        else:
            self._client = None

        # 初始化 TG 监听器
        self._watcher = TgChannelWatcher(
            proxy=self._tg_proxy if self._tg_proxy else "",
            bot_token=self._tg_bot_token if self._tg_bot_token else "",
        )

        global _plugin_instance
        _plugin_instance = self
        proxy_display = f"已配置 ({self._tg_proxy})" if self._tg_proxy else "未配置 (直连模式，境内建议配置代理)"
        logger.info(
            f"Pan123TgPilot 初始化完成: 启用={self._enabled}, "
            f"监控频道={len(self._tg_channels)}个, "
            f"代理={proxy_display}, "
            f"电影目录ID={self._movie_target_dir_id}, 剧集目录ID={self._tv_target_dir_id}, "
            f"智能洗版={self._auto_upgrade}"
        )
        if self._enabled and not self._tg_proxy:
            logger.warning(
                "Pan123TgPilot 提示：当前未检测到 Telegram 代理配置。若您的服务器位于中国大陆境内，"
                "Telegram 官方域名 (t.me) 将被阻断导致 TLS 握手超时。建议在插件设置中填写「Telegram 网络代理」。"
            )

        # 立即运行一次触发逻辑 (方便用户一键测试)
        if self._enabled and config.get("run_now_on_save"):
            logger.info("Pan123TgPilot 检测到勾选「保存配置后立即运行一次」，立即在后台启动全量巡检测试...")
            threading.Thread(target=self._service_check_channels, daemon=True).start()

    @staticmethod
    def get_command() -> list[dict]:
        """注册插件远程命令"""
        return [
            {
                "cmd": "/pan123_tg_run",
                "event": EventType.PluginAction,
                "desc": "立即运行一次 123云盘TG转存巡检",
                "category": "插件命令",
                "data": {"action": "pan123_tg_run"},
            }
        ]

    def run_action(self, action: str) -> None:
        """处理插件动作"""
        if action == "pan123_tg_run":
            logger.info("Pan123TgPilot 收到远程命令 pan123_tg_run，立即在后台启动全量巡检...")
            threading.Thread(target=self._service_check_channels, daemon=True).start()

    def get_state(self) -> bool:
        """返回插件是否启用"""
        return self._enabled

    def stop_service(self) -> None:
        """停止后台服务"""
        logger.info("Pan123TgPilot 服务已停止")

    def get_service(self) -> list[dict[str, Any]]:
        """注册周期性定时监听任务"""
        if not self._enabled:
            return []

        interval_min = max(1, int(self._check_interval or 10))
        if IntervalTrigger:
            trigger = IntervalTrigger(minutes=interval_min)
        elif CronTrigger:
            trigger = CronTrigger.from_crontab(f"*/{min(interval_min, 59)} * * * *")
        else:
            trigger = "interval"

        return [
            {
                "id": "pan123_tg_check_job",
                "name": "123云盘TG频道定时转存与洗版",
                "trigger": trigger,
                "func": self._service_check_channels,
                "kwargs": {},
            }
        ]

    def _service_check_channels(self) -> None:
        """周期执行 TG 频道抓取与转存调度"""
        if not self._enabled:
            return

        if not self._lock.acquire(blocking=False):
            logger.warning("Pan123TgPilot 上一次检查任务仍在进行中，跳过本次调度")
            return

        try:
            self._is_checking = True
            self.execute_check_all_channels()
        except Exception as e:
            logger.error(f"Pan123TgPilot 检查频道过程发生异常: {e}", exc_info=True)
        finally:
            self._is_checking = False
            self._last_check_time = datetime.now()
            self._lock.release()

    def execute_check_all_channels(self) -> dict[str, Any]:
        """执行全量监控频道扫描与转存"""
        if not self._client or not self._watcher or not self._db:
            return {"success": False, "msg": "插件未完成初始化或缺少配置"}

        if not self._tg_channels:
            logger.info("Pan123TgPilot 未配置任何 TG 频道，无需执行抓取")
            return {"success": True, "msg": "未配置监控频道"}

        logger.info(f"Pan123TgPilot 开始巡检 {len(self._tg_channels)} 个 TG 频道...")
        stats = {
            "channels_checked": 0,
            "messages_checked": 0,
            "transferred": 0,
            "upgraded": 0,
            "skipped": 0,
            "failed": 0,
        }

        for channel in self._tg_channels:
            stats["channels_checked"] += 1
            try:
                messages = self._watcher.fetch_channel_messages(channel, limit=30)
                logger.info(f"Pan123TgPilot 成功获取频道 {channel} 的 {len(messages)} 条最近消息")
            except Exception as e:
                logger.error(f"Pan123TgPilot 抓取频道 {channel} 消息失败: {e}")
                continue

            for msg_item in messages:
                # msg_item 为 (msg_id, text) 元组
                if isinstance(msg_item, (tuple, list)) and len(msg_item) >= 2:
                    m_id, m_text = str(msg_item[0]), str(msg_item[1])
                elif isinstance(msg_item, dict):
                    m_id, m_text = str(msg_item.get("post_id", "")), str(msg_item.get("text", ""))
                else:
                    continue

                stats["messages_checked"] += 1
                self._check_stats["total_checked"] += 1
                try:
                    res = self._process_single_message(channel=channel, msg_id=m_id, raw_text=m_text)
                    if res == "transferred":
                        stats["transferred"] += 1
                        self._check_stats["transferred_count"] += 1
                    elif res == "upgraded":
                        stats["upgraded"] += 1
                        self._check_stats["upgraded_count"] += 1
                    elif res == "skipped":
                        stats["skipped"] += 1
                        self._check_stats["skipped_count"] += 1
                    elif res == "failed":
                        stats["failed"] += 1
                        self._check_stats["failed_count"] += 1
                except Exception as e:
                    logger.error(f"Pan123TgPilot 处理消息异常 ({channel}): {e}")
                    stats["failed"] += 1
                    self._check_stats["failed_count"] += 1

        logger.info(
            f"Pan123TgPilot 频道巡检结束: 检查消息 {stats['messages_checked']} 条, "
            f"新转存 {stats['transferred']} 部, 洗版升级 {stats['upgraded']} 部, "
            f"跳过 {stats['skipped']} 部, 失败 {stats['failed']} 部"
        )
        return {"success": True, "stats": stats}

    def _process_single_message(self, channel: str, msg_id: str, raw_text: str) -> str:
        """
        处理单条消息的转存决策与执行
        返回值: "transferred" | "upgraded" | "skipped" | "failed"
        """
        if not raw_text:
            return "skipped"

        resource = TgMessageParser.parse_message(text=raw_text, msg_id=msg_id, channel=channel)
        if not resource:
            # 该消息非影视资源或无法识别 123 链接
            return "skipped"

        # 判断转存目录 ID
        is_tv = (resource.get("media_type") == "tv")
        target_dir_id = self._tv_target_dir_id if is_tv else self._movie_target_dir_id
        media_type_cn = "剧集" if is_tv else "电影"
        title = resource.get("title", "未知影视")

        # 检查本地数据库中该资源的转存记录
        media_key = resource.get("media_key", "")
        existing_record = self._db.get_record_by_media_key(media_key=media_key) if self._db else None

        is_upgrade = False
        decision_reason = ""
        if existing_record:
            if not self._auto_upgrade:
                # 未开启洗版，跳过已转存资源
                return "skipped"

            should_up, decision_reason = evaluate_upgrade(existing_record, resource)
            if not should_up:
                # 质量未超过已保存版本，跳过
                return "skipped"

            is_upgrade = True
            logger.info(
                f"Pan123TgPilot 发现更高画质资源【{title}】: {decision_reason}，准备洗版转存"
            )

        # 执行转存
        save_success = False
        saved_file_id: Optional[int] = None
        error_msg = ""

        # 构建统一的媒体文件夹名称 (例如: "八仙！ (2026)" 或 "死灵法师！我即是天灾")
        year_str = str(resource.get("year") or "").strip()
        folder_display_name = f"{title} ({year_str})" if year_str else title

        try:
            # 1. 优先使用标准分享链接转存：可完整转存该分享下的整个文件夹与全部伴生文件（视频、字幕、nfo、海报等）
            if resource.get("share_key"):
                share_key = resource["share_key"]
                share_pwd = resource.get("share_pwd", "")
                logger.info(
                    f"Pan123TgPilot 发起 123 分享转存 (完整转存文件夹及字幕/nfo刮削伴生文件): 【{title}】 "
                    f"(Key: {share_key}) -> 目标目录ID: {target_dir_id}"
                )
                success, file_id, msg = self._client.save_share_link(
                    share_key=share_key,
                    share_pwd=share_pwd,
                    target_parent_id=target_dir_id,
                    folder_name=folder_display_name,
                )
                if success:
                    save_success = True
                    try:
                        saved_file_id = int(file_id) if file_id else None
                    except (ValueError, TypeError):
                        saved_file_id = None
                else:
                    error_msg = msg
            elif resource.get("fslink"):
                # 2. 仅当没有分享链接时，执行单文件秒传码转存 (自动归集至同名文件夹)
                fslink = resource["fslink"]
                logger.info(
                    f"Pan123TgPilot 发起 123 秒传转存: 【{resource.get('fs_filename') or title}】 -> "
                    f"目标目录ID: {target_dir_id} (归集文件夹: {folder_display_name})"
                )
                success, file_id, msg = self._client.save_fslink(
                    fslink=fslink,
                    parent_file_id=target_dir_id,
                    folder_name=folder_display_name,
                )
                if success:
                    save_success = True
                    try:
                        saved_file_id = int(file_id) if file_id else None
                    except (ValueError, TypeError):
                        saved_file_id = None
                else:
                    error_msg = msg
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Pan123TgPilot 执行转存接口异常: {e}")

        if not save_success:
            logger.warning(
                f"Pan123TgPilot 转存【{title}】失败: {error_msg} (频道: {channel})"
            )
            return "failed"

        # 若是洗版升级且开启了旧版本自动删除
        old_file_deleted = False
        if is_upgrade and self._auto_delete_old_version and existing_record and existing_record.get("pan_file_id"):
            try:
                old_fid = int(existing_record["pan_file_id"])
                logger.info(
                    f"Pan123TgPilot 洗版成功，正在自动清理旧版低画质文件 (FileID: {old_fid})..."
                )
                del_ok, del_msg = self._client.trash_files([old_fid])
                if del_ok:
                    old_file_deleted = True
                    logger.info(f"Pan123TgPilot 旧版文件 {old_fid} 已成功移入回收站")
                else:
                    logger.warning(f"Pan123TgPilot 旧版文件清理未成功: {del_msg}")
            except Exception as e:
                logger.warning(f"Pan123TgPilot 清理旧版文件异常: {e}")

        # 持久化转存记录
        record_data = {
            "media_key": media_key,
            "channel": channel,
            "channel_name": channel,
            "msg_id": msg_id,
            "media_title": title,
            "title": title,
            "year": resource.get("year", ""),
            "media_type": resource.get("media_type", "movie"),
            "season": resource.get("season"),
            "episode": resource.get("episode"),
            "resolution": resource.get("resolution", ""),
            "dovi_hdr": resource.get("dovi_hdr", ""),
            "codec": resource.get("codec", ""),
            "file_size": resource.get("file_size", 0),
            "quality_score": resource.get("quality_score", 0),
            "share_url": resource.get("share_url", ""),
            "fslink": resource.get("fslink", ""),
            "target_dir_id": target_dir_id,
            "pan_dir_id": target_dir_id,
            "cloud_file_id": str(saved_file_id) if saved_file_id else None,
            "pan_file_id": saved_file_id,
            "status": "upgraded" if is_upgrade else "saved",
            "error_msg": decision_reason if is_upgrade else "首发转存",
            "remark": decision_reason if is_upgrade else "首发转存",
        }
        if self._db:
            self._db.save_or_update_record(record_data)

        # 发送 MoviePilot 系统通知
        action_title = "123云盘画质洗版成功" if is_upgrade else "123云盘TG转存成功"
        size_gb = (resource.get("file_size") or 0) / (1024.0 ** 3)
        size_str = f"{size_gb:.2f} GB" if size_gb > 0 else "未知"
        notify_content = (
            f"媒体类型: {media_type_cn}\n"
            f"影视名称: {title}" + (f" ({resource.get('year')})" if resource.get('year') else "") + "\n"
            f"画质规格: {resource.get('resolution') or '未知'} | {resource.get('codec') or '未知'} | {resource.get('dovi_hdr') or 'SDR'}\n"
            f"文件大小: {size_str}\n"
            f"来源频道: {channel}\n"
            f"目标目录ID: {target_dir_id}\n"
        )
        if is_upgrade:
            notify_content += f"洗版策略: 质量评分升级为 {resource.get('quality_score')} 分 (旧版已{'移至回收站' if old_file_deleted else '保留'})\n"

        self.notify(action_title, notify_content)
        logger.info(f"Pan123TgPilot 【{title}】转存完成 ({'洗版升级' if is_upgrade else '首次转存'})")

        return "upgraded" if is_upgrade else "transferred"

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """定义 MoviePilot V3 后台插件配置表单"""
        form = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": (
                        "123云盘TG转存助手：实时监控 Telegram 频道，提取影视名称、TMDB ID 与画质特征，"
                        "精准分流转存至 123 云盘的电影或剧集目录，支持标准分享链接与 123FSLinkV2 秒传链接，"
                        "并内置多维智能洗版升级能力。"
                    ),
                },
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
                                    "model": "enabled",
                                    "label": "启用插件",
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
                                    "model": "notify_enabled",
                                    "label": "发送转存成功通知 (默认关闭)",
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
                                    "model": "run_now_on_save",
                                    "label": "立即运行一次 (保存配置后立即执行)",
                                    "hint": "开启此项并点击保存，插件将立刻在后台执行一次频道巡检与转存测试，无需等待定时周期",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6, "class": "d-flex align-center"},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "elevated",
                                    "prepend-icon": "mdi-play-circle",
                                    "text": "立即执行一次巡检测试",
                                },
                                "events": {
                                    "click": "api.post('/plugin/Pan123TgPilot/manual_check')",
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
                                    "label": "123 云盘 Web Token",
                                    "placeholder": "请输入 123 云盘网页版 Authorization / Token",
                                    "type": "password",
                                    "hint": "在 123pan.com 网页登录后，按 F12 在网络请求 Header 中复制 Authorization (可带或不带 Bearer 前缀)",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    }
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
                                    "model": "movie_target_dir_id",
                                    "label": "电影转存目录 ID",
                                    "placeholder": "例如: 17439195",
                                    "hint": "123云盘中存放电影的文件夹 ID (在网页端打开该文件夹，URL 最后的数字即为目录ID；0为根目录)",
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
                                    "model": "tv_target_dir_id",
                                    "label": "电视剧集转存目录 ID",
                                    "placeholder": "例如: 17439196",
                                    "hint": "123云盘中存放电视剧集的文件夹 ID (识别到剧集、季集SxxExx时转存至此)",
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
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VTextarea",
                                "props": {
                                    "model": "tg_channels",
                                    "label": "监控的 Telegram 频道列表",
                                    "placeholder": "@Pan123Movie, @regeng123, @yun123pan",
                                    "rows": 3,
                                    "hint": "多个频道用逗号或换行分隔，支持频道用户名 (如 @Pan123Movie 或 Pan123Movie)",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    }
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
                                    "model": "check_interval",
                                    "label": "频道抓取检查周期 (分钟)",
                                    "type": "number",
                                    "hint": "默认每 10 分钟自动检查一次 TG 频道的最新消息",
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
                                    "model": "tg_proxy",
                                    "label": "Telegram 网络代理 (可选，国内网络必填)",
                                    "placeholder": "http://192.168.1.100:7890",
                                    "hint": "境内访问 TG 需配置代理。留空则自动继承 MoviePilot 全局代理。⚠️注意：若 MoviePilot 运行在 Docker 容器内，请勿填 127.0.0.1 (指容器本身)，请填写宿主机局域网 IP (如 http://192.168.x.x:7890) 或 http://host.docker.internal:7890，并确认代理软件开启了「允许局域网连接」",
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
                                "component": "VSwitch",
                                "props": {
                                    "model": "auto_upgrade",
                                    "label": "开启智能画质洗版",
                                    "hint": "当检测到同一影视且画质评分更高时 (如 4K 替代 1080p, DoVi 替代 SDR)，自动转存新资源",
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
                                    "model": "auto_delete_old_version",
                                    "label": "洗版成功后自动清理旧版本",
                                    "hint": "洗版转存成功后，自动将网盘中旧版本的低画质文件移入回收站，节省空间",
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
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "tg_bot_token",
                                    "label": "Telegram Bot Token (备用高级模式，可留空)",
                                    "placeholder": "123456789:ABCdefGhIJKlmNoPQRstUVwxYZ",
                                    "hint": "留空时默认通过 t.me/s/ 公开预览流免鉴权解析，完全无需 Bot；如配置则通过 Bot API 获取",
                                },
                            }
                        ],
                    }
                ],
            },
        ]

        model = {
            "enabled": False,
            "run_now_on_save": False,
            "pan123_token": "",
            "movie_target_dir_id": "",
            "tv_target_dir_id": "",
            "tg_channels": "@Pan123Movie, @regeng123, @yun123pan",
            "check_interval": 10,
            "tg_proxy": "",
            "tg_bot_token": "",
            "auto_upgrade": True,
            "auto_delete_old_version": True,
            "notify_enabled": False,
        }

        return form, model

    def get_page(self) -> list[dict]:
        """定义 MoviePilot V3 插件状态页面"""
        status_text = "运行中" if self._enabled else "未启用"
        color = "success" if self._enabled else "grey"
        channels_str = ", ".join(self._tg_channels) if self._tg_channels else "未配置"
        last_check = (
            self._last_check_time.strftime("%Y-%m-%d %H:%M:%S")
            if self._last_check_time
            else "尚未执行"
        )

        return [
            {
                "component": "VCard",
                "props": {"class": "mb-4", "variant": "outlined"},
                "content": [
                    {
                        "component": "VCardItem",
                        "content": [
                            {
                                "component": "VCardTitle",
                                "text": "123云盘TG转存助手运行状态",
                            },
                            {
                                "component": "VCardSubtitle",
                                "text": f"插件状态: {status_text} | 检查周期: 每 {self._check_interval} 分钟 | 上次巡检: {last_check}",
                            },
                        ],
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VRow",
                                "content": [
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 6, "md": 3},
                                        "content": [
                                            {
                                                "component": "VCard",
                                                "props": {"color": "primary", "variant": "tonal"},
                                                "content": [
                                                    {
                                                        "component": "VCardItem",
                                                        "content": [
                                                            {"component": "VCardSubtitle", "text": "累计检查消息"},
                                                            {"component": "VCardTitle", "text": str(self._check_stats["total_checked"])},
                                                        ],
                                                    }
                                                ],
                                            }
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 6, "md": 3},
                                        "content": [
                                            {
                                                "component": "VCard",
                                                "props": {"color": "success", "variant": "tonal"},
                                                "content": [
                                                    {
                                                        "component": "VCardItem",
                                                        "content": [
                                                            {"component": "VCardSubtitle", "text": "成功转存总数"},
                                                            {"component": "VCardTitle", "text": str(self._check_stats["transferred_count"])},
                                                        ],
                                                    }
                                                ],
                                            }
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 6, "md": 3},
                                        "content": [
                                            {
                                                "component": "VCard",
                                                "props": {"color": "info", "variant": "tonal"},
                                                "content": [
                                                    {
                                                        "component": "VCardItem",
                                                        "content": [
                                                            {"component": "VCardSubtitle", "text": "洗版升级总数"},
                                                            {"component": "VCardTitle", "text": str(self._check_stats["upgraded_count"])},
                                                        ],
                                                    }
                                                ],
                                            }
                                        ],
                                    },
                                    {
                                        "component": "VCol",
                                        "props": {"cols": 6, "md": 3},
                                        "content": [
                                            {
                                                "component": "VCard",
                                                "props": {"color": "warning", "variant": "tonal"},
                                                "content": [
                                                    {
                                                        "component": "VCardItem",
                                                        "content": [
                                                            {"component": "VCardSubtitle", "text": "监控频道数量"},
                                                            {"component": "VCardTitle", "text": str(len(self._tg_channels))},
                                                        ],
                                                    }
                                                ],
                                            }
                                        ],
                                    },
                                ],
                            },
                            {
                                "component": "VDivider",
                                "props": {"class": "my-4"},
                            },
                            {
                                "component": "p",
                                "props": {"class": "text-caption text-grey"},
                                "text": f"当前监控的频道: {channels_str}",
                            },
                            {
                                "component": "p",
                                "props": {"class": "text-caption text-grey"},
                                "text": f"电影目录ID: {self._movie_target_dir_id} | 剧集目录ID: {self._tv_target_dir_id} | 智能洗版: {'开启' if self._auto_upgrade else '关闭'} | 旧版清理: {'开启' if self._auto_delete_old_version else '关闭'}",
                            },
                        ],
                    },
                    {
                        "component": "VCardActions",
                        "props": {"class": "px-4 pb-4"},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "elevated",
                                    "prepend-icon": "mdi-play-circle",
                                    "text": "立即运行一次巡检测试",
                                },
                                "events": {
                                    "click": "api.post('/plugin/Pan123TgPilot/manual_check')",
                                },
                            },
                        ],
                    },
                ],
            }
        ]

    def get_api(self) -> list[dict[str, Any]]:
        """注册插件专用 API 路由"""
        return [
            {
                "path": "/manual_check",
                "endpoint": self._api_manual_check,
                "methods": ["POST", "GET"],
                "summary": "立即执行一次 TG 频道抓取与转存巡检",
                "description": "手动触发对所有配置频道的抓取、解析与转存升级流程",
            },
            {
                "path": "/records",
                "endpoint": self._api_get_records,
                "methods": ["GET"],
                "summary": "获取历史转存与洗版记录",
                "description": "查询近期的转存成功与洗版历史记录",
            },
            {
                "path": "/test_tg",
                "endpoint": self._api_test_tg,
                "methods": ["POST", "GET"],
                "summary": "测试 Telegram 网络连通性",
                "description": "测试当前代理或直连环境下能否正常获取指定频道预览",
            },
        ]

    def _api_manual_check(self) -> dict[str, Any]:
        """API: 手动触发频道巡检"""
        threading.Thread(target=self._service_check_channels, daemon=True).start()
        return {"code": 200, "message": "已在后台启动 TG 频道巡检与转存任务"}

    def _api_get_records(self) -> dict[str, Any]:
        """API: 获取转存记录"""
        if not self._db:
            return {"code": 500, "message": "数据库未就绪", "data": []}
        records = self._db.list_records(limit=100)
        return {"code": 200, "message": "success", "data": records}

    def _api_test_tg(self) -> dict[str, Any]:
        """API: 测试 TG 连通性"""
        if not self._watcher:
            return {"code": 500, "message": "监听器未初始化"}
        test_ch = self._tg_channels[0] if self._tg_channels else "Pan123Movie"
        try:
            msgs = self._watcher.fetch_channel_messages(test_ch, limit=5)
            return {
                "code": 200,
                "message": f"成功连通 Telegram 并获取频道 {test_ch} 的 {len(msgs)} 条消息预览",
                "sample_messages": [m[1][:100] for m in msgs[:2]] if msgs else [],
            }
        except Exception as e:
            return {"code": 500, "message": f"连接 Telegram 失败: {e}"}


@eventmanager.register(EventType.PluginAction)
def pan123_tg_on_action(event: Event) -> None:
    """MoviePilot 插件远程命令事件监听器"""
    if not _plugin_instance or not _plugin_instance.get_state():
        return
    event_data = getattr(event, "event_data", None) or {}
    action = event_data.get("action")
    if action == "pan123_tg_run":
        _plugin_instance.run_action(action)

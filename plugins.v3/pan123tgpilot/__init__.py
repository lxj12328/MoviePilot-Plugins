"""
123云盘TG转存助手 (Pan123TgPilot) - MoviePilot V3 插件入口
支持 Telegram 影视频道消息自动监听、电影与剧集智能分流转存、123秒传/分享链接适配与画质洗版升级
"""

import re
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
    plugin_version = "1.1.5"
    plugin_author = "lxj12328"

    # 详情页最多加载的最近记录条数 (客户端分页/排序)
    PAGE_FETCH_LIMIT = 500
    # 详情页每页展示条数
    PAGE_SIZE = 20

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
        self._retention_days: int = 0

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
        try:
            self._retention_days = max(0, int(config.get("retention_days") or 0))
        except (ValueError, TypeError):
            self._retention_days = 0

        # 初始化数据库 (ensure_table 内含可重复执行的结构迁移与空记录清理)
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
        proxy_display = f"已配置 ({self._tg_proxy})" if self._tg_proxy else "未配置 (直连模式)"
        logger.info(
            f"Pan123TgPilot 初始化完成: 启用={self._enabled}, "
            f"监控频道={len(self._tg_channels)}个, "
            f"代理={proxy_display}, "
            f"电影目录ID={self._movie_target_dir_id}, 剧集目录ID={self._tv_target_dir_id}, "
            f"智能洗版={self._auto_upgrade}"
        )

        # 立即运行一次触发逻辑：触发后立即自动将「保存后立即运行一次」开关复位为关闭状态，无需人工再手动关一次
        if config.get("run_now_on_save"):
            config["run_now_on_save"] = False
            if hasattr(self, "update_config"):
                try:
                    self.update_config(dict(config))
                except Exception as e:
                    logger.warning(f"Pan123TgPilot 自动复位 run_now_on_save 开关异常: {e}")
            if self._enabled:
                logger.info("Pan123TgPilot 检测到勾选「保存后立即运行一次」，已自动复位开关并在后台启动全量巡检...")
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

        services = [
            {
                "id": "pan123_tg_check_job",
                "name": "123云盘TG频道定时转存与洗版",
                "trigger": trigger,
                "func": self._service_check_channels,
                "kwargs": {},
            }
        ]
        if self._retention_days > 0 and CronTrigger:
            services.append(
                {
                    "id": "pan123_tg_purge_job",
                    "name": "123云盘TG转存记录过期清理",
                    "trigger": CronTrigger.from_crontab("0 3 * * *"),
                    "func": self._service_purge_records,
                    "kwargs": {},
                }
            )
        return services

    def _service_purge_records(self) -> int:
        """按保留天数清理过期转存记录 (0 表示永久保留，不清理)"""
        if not self._db or self._retention_days <= 0:
            return 0
        try:
            n = self._db.purge_expired(days=self._retention_days)
            logger.info(f"Pan123TgPilot 记录保留清理完成: 保留 {self._retention_days} 天，清除过期记录 {n} 条")
            return int(n or 0)
        except Exception as e:
            logger.error(f"Pan123TgPilot 清理过期记录异常: {e}")
            return 0

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

            ch_valid = 0
            ch_no_link = 0
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
                        ch_valid += 1
                        stats["transferred"] += 1
                        self._check_stats["transferred_count"] += 1
                    elif res == "upgraded":
                        ch_valid += 1
                        stats["upgraded"] += 1
                        self._check_stats["upgraded_count"] += 1
                    elif res == "skipped":
                        ch_valid += 1
                        stats["skipped"] += 1
                        self._check_stats["skipped_count"] += 1
                    elif res == "no_link":
                        ch_no_link += 1
                        stats["skipped"] += 1
                        self._check_stats["skipped_count"] += 1
                    elif res == "failed":
                        ch_valid += 1
                        stats["failed"] += 1
                        self._check_stats["failed_count"] += 1
                except Exception as e:
                    logger.error(f"Pan123TgPilot 处理消息异常 ({channel}): {e}")
                    stats["failed"] += 1
                    self._check_stats["failed_count"] += 1

            if messages and ch_valid == 0:
                logger.info(
                    f"Pan123TgPilot 频道 {channel} 本次检查的 {len(messages)} 条消息中未包含有效 123 云盘分享链接或秒传码 (普通非资源消息 {ch_no_link} 条)"
                )
            elif ch_no_link > 0:
                logger.info(
                    f"Pan123TgPilot 频道 {channel} 解析完成: 有效 123 资源消息 {ch_valid} 条, 普通无链接消息 {ch_no_link} 条"
                )

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
            return "no_link"

        # 判断转存目录 ID
        is_tv = (resource.get("media_type") == "tv")
        target_dir_id = self._tv_target_dir_id if is_tv else self._movie_target_dir_id
        media_type_cn = "剧集" if is_tv else "电影"
        title = resource.get("title", "未知影视")

        # 1. 链接级去重：若该分享链接或秒传码已在历史记录中，直接跳过
        share_url = resource.get("share_url") or ""
        fslink = resource.get("fslink") or ""
        if self._db and self._db.has_link_processed(share_url=share_url, fslink=fslink):
            logger.info(f"Pan123TgPilot 检测到资源链接/秒传码已在本地库中转存过，直接跳过: 【{title}】")
            return "skipped"

        # 2. 媒体级去重与智能洗版对比
        media_key = resource.get("media_key", "")
        existing_record = None
        if self._db:
            existing_record = self._db.get_record_by_media_key(
                media_key=media_key,
                title=title,
                year=resource.get("year", ""),
                tmdb_id=resource.get("tmdb_id"),
                media_type=resource.get("media_type", "movie"),
                ep_tag=resource.get("ep_tag", ""),
            )

        is_upgrade = False
        decision_reason = ""
        if existing_record:
            if not self._auto_upgrade:
                logger.info(f"Pan123TgPilot 本地库已存在【{title}】且未开启自动洗版，跳过转存")
                return "skipped"

            should_up, decision_reason = evaluate_upgrade(existing_record, resource)
            if not should_up:
                logger.info(
                    f"Pan123TgPilot 【{title}】已有同质或更优资源 (当前得分: {existing_record.get('quality_score', 0)} >= 新资源: {resource.get('quality_score', 0)})，跳过转存"
                )
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
            self._db.save_or_update_record(**record_data)

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

    # ------------------------------------------------------------------ 表单构造辅助 (移动端自适应)
    @staticmethod
    def _col(content: list[dict], md: int = 6) -> dict:
        """统一栏位：手机 (cols=12) 单列堆叠，平板/桌面 (md) 才并排"""
        return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": content}

    @staticmethod
    def _switch(model: str, label: str, hint: str = "") -> dict:
        props: dict[str, Any] = {"model": model, "label": label, "color": "primary", "density": "comfortable"}
        if hint:
            props.update({"hint": hint, "persistent-hint": True})
        return {"component": "VSwitch", "props": props}

    @staticmethod
    def _field(model: str, label: str, hint: str = "", **extra: Any) -> dict:
        props: dict[str, Any] = {"model": model, "label": label, "variant": "outlined", "density": "comfortable"}
        if hint:
            props.update({"hint": hint, "persistent-hint": True})
        props.update(extra)
        return {"component": "VTextField", "props": props}

    @staticmethod
    def _section(title: str, icon: str, rows: list[dict]) -> dict:
        """配置分组卡片：标题 + 若干行"""
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "class": "mb-3"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"}, "text": f"{title}"},
                {"component": "VCardText", "content": rows},
            ],
        }

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """定义 MoviePilot V3 后台插件配置表单 (分组卡片 + 手机单列自适应)"""
        col, sw, fld = self._col, self._switch, self._field

        basic = self._section("基础设置", "mdi-cog", [
            {"component": "VRow", "content": [
                col([sw("enabled", "启用插件")], md=4),
                col([sw("notify_enabled", "发送转存成功通知")], md=4),
                col([sw("run_now_on_save", "保存后立即运行一次", "保存后自动触发一次巡检并自动恢复为关闭状态")], md=4),
            ]},
        ])

        pan = self._section("123 云盘", "mdi-cloud", [
            {"component": "VRow", "content": [
                col([fld(
                    "pan123_token", "123 云盘 Web Token", "网页登录后 F12 复制请求头 Authorization (可不带 Bearer)",
                    type="password", placeholder="Authorization / Token",
                )], md=12),
                col([fld(
                    "movie_target_dir_id", "电影目录 ID", "网盘文件夹 URL 末尾的数字，0 为根目录",
                    placeholder="例如 17439195",
                )]),
                col([fld(
                    "tv_target_dir_id", "剧集目录 ID", "识别到剧集 (SxxExx) 时转存至此",
                    placeholder="例如 17439196",
                )]),
            ]},
        ])

        tg = self._section("Telegram 监控", "mdi-send", [
            {"component": "VRow", "content": [
                col([{
                    "component": "VTextarea",
                    "props": {
                        "model": "tg_channels", "label": "监控频道", "variant": "outlined",
                        "rows": 3, "auto-grow": True, "placeholder": "@Pan123Movie, @regeng123, @yun123pan",
                        "hint": "多个频道用逗号或换行分隔", "persistent-hint": True,
                    },
                }], md=12),
                col([fld("check_interval", "检查周期 (分钟)", "默认 10 分钟", type="number", placeholder="10")]),
                col([fld(
                    "tg_proxy", "Telegram 代理 (可选)",
                    "留空则继承 MoviePilot 全局代理；Docker 内请勿填 127.0.0.1",
                    placeholder="http://192.168.1.100:7890",
                )]),
                col([fld(
                    "tg_bot_token", "Bot Token (可选)", "留空使用 t.me/s/ 公开预览，无需 Bot",
                    type="password", placeholder="123456789:ABCdef...",
                )], md=12),
            ]},
        ])

        upgrade = self._section("洗版与数据", "mdi-database", [
            {"component": "VRow", "content": [
                col([sw("auto_upgrade", "智能画质洗版", "同一影视出现更高评分版本时自动转存")]),
                col([sw("auto_delete_old_version", "洗版后清理旧版本", "旧版文件移入网盘回收站")]),
                col([fld(
                    "retention_days", "记录保留天数", "0 = 永久保留；填 N 则每天 03:00 清理 N 天前的记录 (清理后同资源可能被重新转存)",
                    type="number", placeholder="0",
                )], md=12),
            ]},
        ])

        form = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info", "variant": "tonal", "density": "compact", "class": "mb-3",
                    "text": "实时监控 Telegram 频道，按画质评分自动转存/洗版到 123 云盘，支持分享链接与 123FSLinkV2 秒传码。",
                },
            },
            basic, pan, tg, upgrade,
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
            "retention_days": 0,
        }
        return form, model

    # ------------------------------------------------------------------ 详情页
    @staticmethod
    def _stat_card(color: str, label: str, value: Any) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 6, "md": 3},
            "content": [{
                "component": "VCard",
                "props": {"color": color, "variant": "tonal"},
                "content": [{
                    "component": "VCardItem",
                    "content": [
                        {"component": "VCardSubtitle", "text": label},
                        {"component": "VCardTitle", "text": str(value)},
                    ],
                }],
            }],
        }

    @staticmethod
    def _record_to_row(r: dict) -> dict:
        """把数据库记录转成数据表行"""
        size_bytes = r.get("file_size") or 0
        size_str = f"{size_bytes / (1024.0 ** 3):.2f} GB" if size_bytes > 0 else "-"
        spec = " / ".join([x for x in (r.get("resolution"), r.get("codec"), r.get("dovi_hdr")) if x]) or "-"
        status = str(r.get("status") or "").lower()
        status_text = {"saved": "首发转存", "upgraded": "洗版升级", "ignored": "已跳过"}.get(
            status, "首发转存" if "save" in status else (status.upper() or "已转存")
        )
        title_text = str(r.get("media_title") or r.get("title") or "").strip()
        if not title_text and r.get("fslink"):
            m_fn = re.search(r"#([^#]+\.(?:mkv|mp4|ts|avi|mov|iso|rmvb|flv))", r["fslink"], re.IGNORECASE)
            if m_fn:
                title_text = m_fn.group(1)
        return {
            "title": title_text or "-",
            "spec": spec,
            "size": size_str,
            "score": int(r.get("quality_score") or 0),
            "channel": r.get("channel") or "-",
            "status": status_text,
            "time": str(r.get("updated_at") or r.get("created_at") or "-")[:19],
        }

    @staticmethod
    def _records_table(rows_data: list[dict]) -> dict:
        """用 VTable 逐行渲染一页记录 (移动端可横向滚动)"""
        heads = ["影视名称", "画质 (分辨率/编码/HDR)", "大小", "评分", "来源频道", "类型", "转存时间"]
        keys = ["title", "spec", "size", "score", "channel", "status", "time"]
        if rows_data:
            trs = [
                {"component": "tr", "content": [{"component": "td", "text": str(row[k])} for k in keys]}
                for row in rows_data
            ]
        else:
            trs = [{"component": "tr", "content": [{
                "component": "td", "props": {"colspan": len(keys), "class": "text-center text-grey py-4"},
                "text": "暂无转存记录，抓取并转存资源后将在此展示",
            }]}]
        return {
            "component": "VTable",
            "props": {"density": "compact", "hover": True, "class": "text-no-wrap"},
            "content": [
                {"component": "thead", "content": [
                    {"component": "tr", "content": [{"component": "th", "text": h} for h in heads]}
                ]},
                {"component": "tbody", "content": trs},
            ],
        }

    def get_page(self) -> list[dict]:
        """插件详情页：状态统计 + 带分页/排序的已转存资源数据表"""
        status_text = "运行中" if self._enabled else "未启用"
        channels_str = ", ".join(self._tg_channels) if self._tg_channels else "未配置"
        last_check = self._last_check_time.strftime("%Y-%m-%d %H:%M:%S") if self._last_check_time else "尚未执行"
        retention = f"{self._retention_days} 天" if self._retention_days > 0 else "永久"

        status_card = {
            "component": "VCard",
            "props": {"class": "mb-4", "variant": "outlined"},
            "content": [
                {"component": "VCardItem", "content": [
                    {"component": "VCardTitle", "text": "123云盘TG转存助手"},
                    {"component": "VCardSubtitle",
                     "text": f"{status_text} · 每 {self._check_interval} 分钟 · 上次巡检 {last_check}"},
                ]},
                {"component": "VCardText", "content": [
                    {"component": "VRow", "content": [
                        self._stat_card("primary", "累计检查消息", self._check_stats["total_checked"]),
                        self._stat_card("success", "成功转存", self._check_stats["transferred_count"]),
                        self._stat_card("info", "洗版升级", self._check_stats["upgraded_count"]),
                        self._stat_card("warning", "监控频道", len(self._tg_channels)),
                    ]},
                    {"component": "p", "props": {"class": "text-caption text-grey mt-2"},
                     "text": f"频道: {channels_str}"},
                    {"component": "p", "props": {"class": "text-caption text-grey"},
                     "text": (f"电影目录ID {self._movie_target_dir_id} · 剧集目录ID {self._tv_target_dir_id} · "
                              f"洗版 {'开' if self._auto_upgrade else '关'} · "
                              f"清理旧版 {'开' if self._auto_delete_old_version else '关'} · 记录保留 {retention}")},
                ]},
            ],
        }

        total = 0
        items: list[dict] = []
        if self._db:
            try:
                total = self._db.count_records()
                items = [self._record_to_row(r) for r in self._db.list_records(limit=self.PAGE_FETCH_LIMIT)]
            except Exception as e:
                logger.error(f"Pan123TgPilot 读取转存记录失败: {e}")

        # 注意：VDataTable 在 MoviePilot Vuetify JSON 渲染器下只渲染分页条、不渲染行，
        # 因此改用 VTable + tr/td 逐行输出，并把记录按页分组：第 1 页直接展示，其余页放入折叠面板。
        page_size = self.PAGE_SIZE
        pages = [items[k:k + page_size] for k in range(0, len(items), page_size)]

        body: list[dict] = [{
            "component": "VCardItem",
            "content": [
                {"component": "VCardTitle", "text": f"已转存资源清单 (共 {total} 条)"},
                {"component": "VCardSubtitle",
                 "text": f"按时间倒序，每页 {page_size} 条，更早的页请展开下方折叠面板；"
                         "按关键词/状态检索请调用 /records?keyword=&page=&page_size="},
            ],
        }]
        body.append(self._records_table(pages[0] if pages else []))
        if len(pages) > 1:
            panels = []
            for n, chunk in enumerate(pages[1:], start=2):
                start, end = (n - 1) * page_size + 1, (n - 1) * page_size + len(chunk)
                panels.append({
                    "component": "VExpansionPanel",
                    "content": [
                        {"component": "VExpansionPanelTitle", "text": f"第 {n} 页 (第 {start}-{end} 条)"},
                        {"component": "VExpansionPanelText", "content": [self._records_table(chunk)]},
                    ],
                })
            body.append({
                "component": "VExpansionPanels",
                "props": {"variant": "accordion", "class": "mt-2"},
                "content": panels,
            })

        table_card = {"component": "VCard", "props": {"variant": "outlined"}, "content": body}
        return [status_card, table_card]

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
                "summary": "分页查询转存与洗版记录",
                "description": "参数: keyword 关键字, status(saved/upgraded), page 页码(从1起), page_size 每页条数",
            },
            {
                "path": "/purge",
                "endpoint": self._api_purge,
                "methods": ["POST", "GET"],
                "summary": "按保留天数立即清理过期记录",
                "description": "保留天数为 0 (永久) 时不会删除任何记录",
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
        return {"success": True, "code": 200, "message": "已在后台启动 TG 频道巡检与转存任务", "data": None}

    def _api_get_records(
        self, keyword: str = "", status: str = "", page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        """API: 分页获取转存记录，支持关键字与状态检索"""
        if not self._db:
            return {"success": False, "code": 500, "message": "数据库未就绪", "data": []}
        try:
            page = max(1, int(page or 1))
            page_size = min(200, max(1, int(page_size or 20)))
        except (ValueError, TypeError):
            page, page_size = 1, 20
        total = self._db.count_records(keyword=keyword or "", status=status or "")
        records = self._db.list_records(
            limit=page_size, offset=(page - 1) * page_size, keyword=keyword or "", status=status or ""
        )
        return {
            "success": True,
            "code": 200,
            "message": "success",
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
            "data": records,
        }

    def _api_purge(self) -> dict[str, Any]:
        """API: 立即按保留天数清理过期记录"""
        if self._retention_days <= 0:
            return {"success": True, "code": 200, "message": "记录保留天数为 0 (永久保留)，未执行清理", "deleted": 0, "data": {"deleted": 0}}
        n = self._service_purge_records()
        return {"success": True, "code": 200, "message": f"已清理 {n} 条过期记录", "deleted": n, "data": {"deleted": n}}

    def _api_test_tg(self) -> dict[str, Any]:
        """API: 测试 TG 连通性"""
        if not self._watcher:
            return {"success": False, "code": 500, "message": "监听器未初始化", "data": None}
        test_ch = self._tg_channels[0] if self._tg_channels else "Pan123Movie"
        try:
            msgs = self._watcher.fetch_channel_messages(test_ch, limit=5)
            return {
                "success": True,
                "code": 200,
                "message": f"成功连通 Telegram 并获取频道 {test_ch} 的 {len(msgs)} 条消息预览",
                "sample_messages": [m[1][:100] for m in msgs[:2]] if msgs else [],
                "data": {"sample_messages": [m[1][:100] for m in msgs[:2]] if msgs else []},
            }
        except Exception as e:
            return {"success": False, "code": 500, "message": f"连接 Telegram 失败: {e}", "data": None}


@eventmanager.register(EventType.PluginAction)
def pan123_tg_on_action(event: Event) -> None:
    """MoviePilot 插件远程命令事件监听器"""
    if not _plugin_instance or not _plugin_instance.get_state():
        return
    event_data = getattr(event, "event_data", None) or {}
    action = event_data.get("action")
    if action == "pan123_tg_run":
        _plugin_instance.run_action(action)

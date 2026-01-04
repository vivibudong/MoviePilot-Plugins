import json
import threading
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from app.log import logger
from app.schemas.types import NotificationType
import requests


class EmbyPlaybackReport(_PluginBase):
    # 插件名称
    plugin_name = "Emby观影报告推送"
    # 插件描述
    plugin_desc = "定期统计Emby观影数据并推送通知报告"
    # 插件图标
    plugin_icon = "Emby_A.png"
    # 插件版本
    plugin_version = "0.1"
    # 插件作者
    plugin_author = "Vivi"
    # 作者主页
    author_url = "https://github.com/vivibudong"
    # 插件配置项ID前缀
    plugin_config_prefix = "embyplaybackreport_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _onlyonce = False
    _cron = None
    _report_type = "daily"
    _emby_host = None
    _emby_token = None
    _notify = True
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron", "0 9 * * *")
            self._report_type = config.get("report_type", "daily")
            self._emby_host = config.get("emby_host", "")
            self._emby_token = config.get("emby_token", "")
            self._notify = config.get("notify", True)

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._onlyonce:
                logger.info("Emby观影报告服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.report,
                    trigger='date',
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="Emby观影报告"
                )
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "enabled": self._enabled,
                    "onlyonce": False,
                    "cron": self._cron,
                    "report_type": self._report_type,
                    "emby_host": self._emby_host,
                    "emby_token": self._emby_token,
                    "notify": self._notify
                })

            if self._enabled and self._cron:
                try:
                    self._scheduler.add_job(
                        func=self.report,
                        trigger=CronTrigger.from_crontab(self._cron),
                        name="Emby观影报告"
                    )
                except Exception as err:
                    logger.error(f"定时任务配置错误：{err}")
                    self.systemmessage.put(f"Emby观影报告定时任务配置错误：{err}")

            if self._scheduler.get_jobs():
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        """获取插件状态"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        """
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'emby_host',
                                            'label': 'Emby服务器地址',
                                            'placeholder': 'https://emby.example.com',
                                            'hint': '只需填写主域名，如：https://emby.vvapi.de'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'emby_token',
                                            'label': 'Emby API Token',
                                            'placeholder': '输入API密钥',
                                            'hint': '用于访问Emby API的令牌'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'report_type',
                                            'label': '报告类型',
                                            'items': [
                                                {'title': '每日报告', 'value': 'daily'},
                                                {'title': '每周报告', 'value': 'weekly'},
                                                {'title': '每月报告', 'value': 'monthly'}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 9 * * *',
                                            'hint': 'Cron表达式，默认每天9点执行'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '插件通过Emby的Playback Reporting插件统计观影数据。'
                                                    '需要确保Emby服务器已安装并启用该插件。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "0 9 * * *",
            "report_type": "daily",
            "emby_host": "",
            "emby_token": ""
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")

    def report(self):
        """
        生成并推送观影报告
        """
        if not self._emby_host or not self._emby_token:
            logger.error("Emby服务器地址或API Token未配置")
            return

        logger.info("开始生成Emby观影报告...")

        try:
            # 获取时间范围
            end_date = datetime.now()
            if self._report_type == "daily":
                start_date = end_date - timedelta(days=1)
                period_text = "昨日"
            elif self._report_type == "weekly":
                start_date = end_date - timedelta(days=7)
                period_text = "本周"
            else:  # monthly
                start_date = end_date - timedelta(days=30)
                period_text = "本月"

            # 查询观影数据
            stats = self._query_playback_stats(start_date, end_date)

            if stats:
                # 生成报告文本
                report_text = self._generate_report_text(stats, period_text, start_date, end_date)

                # 发送通知
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.MediaServer,
                        title=f"📊 Emby{period_text}观影报告",
                        text=report_text
                    )
                
                logger.info(f"Emby观影报告生成成功：{period_text}")
            else:
                logger.warning("未获取到观影数据")

        except Exception as e:
            logger.error(f"生成观影报告失败：{str(e)}")

    def _query_playback_stats(self, start_date: datetime, end_date: datetime) -> Optional[Dict]:
        """
        查询Emby播放统计数据
        """
        # 构建完整的API URL
        api_url = f"{self._emby_host.rstrip('/')}/emby/user_usage_stats/submit_custom_query"
        
        # 格式化日期
        start_str = start_date.strftime("%Y-%m-%d 00:00:00")
        end_str = end_date.strftime("%Y-%m-%d 23:59:59")

        # SQL查询语句
        query = f"""
        SELECT 
            COUNT(DISTINCT UserId) as user_count,
            COUNT(*) as play_count,
            SUM(PlayDuration) as total_duration,
            ItemType,
            ItemName
        FROM PlaybackActivity 
        WHERE DateCreated >= '{start_str}' 
        AND DateCreated <= '{end_str}'
        GROUP BY ItemType
        ORDER BY play_count DESC
        """

        try:
            # 发送POST请求
            headers = {
                "X-Emby-Token": self._emby_token,
                "Content-Type": "application/json"
            }
            
            response = requests.post(
                api_url,
                headers=headers,
                json={"CustomQueryString": query},
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"成功获取观影数据：{len(result.get('results', []))} 条记录")
                return result
            else:
                logger.error(f"API请求失败：{response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"查询观影数据失败：{str(e)}")
            return None

    def _generate_report_text(self, stats: Dict, period: str, start: datetime, end: datetime) -> str:
        """
        生成报告文本
        """
        results = stats.get("results", [])
        
        if not results:
            return f"{period}暂无观影记录"

        # 统计总数据
        total_plays = 0
        total_duration = 0
        type_stats = {}

        for item in results:
            if len(item) >= 5:
                # 确保类型转换为数值
                try:
                    plays = int(item[1]) if item[1] else 0
                except (ValueError, TypeError):
                    plays = 0
                
                try:
                    duration = float(item[2]) if item[2] else 0
                except (ValueError, TypeError):
                    duration = 0
                
                item_type = str(item[3]) if item[3] else "Unknown"
                
                total_plays += plays
                total_duration += duration
                
                if item_type not in type_stats:
                    type_stats[item_type] = {"count": 0, "duration": 0}
                type_stats[item_type]["count"] += plays
                type_stats[item_type]["duration"] += duration

        # 转换时长为小时
        hours = total_duration / 3600 if total_duration > 0 else 0

        # 构建报告文本
        report = f"📅 统计周期：{start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}\n\n"
        report += f"▶️ 总播放次数：{total_plays} 次\n"
        report += f"⏱️ 总观看时长：{hours:.1f} 小时\n\n"

        if type_stats:
            report += "📺 内容类型统计：\n"
            for item_type, data in sorted(type_stats.items(), key=lambda x: x[1]["count"], reverse=True):
                type_hours = data["duration"] / 3600
                report += f"  · {item_type}：{data['count']} 次 ({type_hours:.1f}小时)\n"

        return report

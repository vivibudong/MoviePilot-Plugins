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
    plugin_desc = "定期统计Emby观影数据并推送通知报告,支持每日/每周/每月多维度统计"
    # 插件图标
    plugin_icon = "Emby_A.png"
    # 插件版本
    plugin_version = "0.5"  # 更新版本号
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
    _notify = False  # 新增：控制是否发送通知
    _onlyonce = False
    _emby_host = None
    _emby_token = None
    
    # 每日报告设置
    _daily_enabled = False
    _daily_cron = None
    _daily_reports = []
    
    # 每周报告设置
    _weekly_enabled = False
    _weekly_cron = None
    _weekly_reports = []
    
    # 每月报告设置
    _monthly_enabled = False
    _monthly_cron = None
    _monthly_reports = []
    
    _scheduler: Optional[BackgroundScheduler] = None

    def _parse_cron_to_trigger(self, cron_str: str, report_type: str) -> Optional[CronTrigger]:
        """
        将 Cron 表达式转换为 CronTrigger,使用明确的参数避免歧义
        """
        try:
            parts = cron_str.strip().split()
            if len(parts) != 5:
                logger.error(f"{report_type} Cron表达式格式错误: {cron_str}")
                return None
            
            minute, hour, day, month, day_of_week = parts
            
            # 构建 CronTrigger 参数
            trigger_args = {
                'timezone': settings.TZ
            }
            
            # 处理分钟
            if minute != '*':
                trigger_args['minute'] = minute
            
            # 处理小时
            if hour != '*':
                trigger_args['hour'] = hour
            
            # 处理日期(每月几号)
            if day != '*':
                trigger_args['day'] = day
            
            # 处理月份
            if month != '*':
                trigger_args['month'] = month
            
            # 处理星期几
            if day_of_week != '*':
                try:
                    dow_num = int(day_of_week)
                    if dow_num == 0:  # Cron的周日
                        trigger_args['day_of_week'] = 6  # APScheduler的周日
                    else:  # Cron的1-6 对应 APScheduler的0-5
                        trigger_args['day_of_week'] = dow_num - 1
                except ValueError:
                    trigger_args['day_of_week'] = day_of_week
            
            logger.info(f"{report_type}报告 Cron解析: {cron_str} -> {trigger_args}")
            return CronTrigger(**trigger_args)
            
        except Exception as e:
            logger.error(f"{report_type}报告 Cron解析失败: {cron_str}, 错误: {e}")
            return None

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", False)  # 读取通知配置
            self._onlyonce = config.get("onlyonce", False)
            self._emby_host = config.get("emby_host", "")
            self._emby_token = config.get("emby_token", "")
            
            # 每日报告配置
            self._daily_enabled = config.get("daily_enabled", False)
            self._daily_cron = config.get("daily_cron", "0 9 * * *")
            self._daily_reports = config.get("daily_reports", [])
            
            # 每周报告配置
            self._weekly_enabled = config.get("weekly_enabled", False)
            self._weekly_cron = config.get("weekly_cron", "0 9 * * 1")
            self._weekly_reports = config.get("weekly_reports", [])
            
            # 每月报告配置
            self._monthly_enabled = config.get("monthly_enabled", False)
            self._monthly_cron = config.get("monthly_cron", "0 9 1 * *")
            self._monthly_reports = config.get("monthly_reports", [])

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._onlyonce:
                logger.info("Emby观影报告服务启动,立即运行一次")
                self._scheduler.add_job(
                    func=self.run_all_reports,
                    trigger='date',
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="Emby观影报告-立即执行"
                )
                # 关闭一次性开关
                self._onlyonce = False
                self._save_config()

            if self._enabled:
                # 添加每日报告任务
                if self._daily_enabled and self._daily_cron:
                    trigger = self._parse_cron_to_trigger(self._daily_cron, "每日")
                    if trigger:
                        try:
                            self._scheduler.add_job(
                                func=self.report,
                                trigger=trigger,
                                args=["daily"],
                                name="Emby观影报告-每日"
                            )
                            logger.info(f"每日报告任务已添加: {self._daily_cron}")
                        except Exception as err:
                            logger.error(f"每日报告任务添加失败: {err}")

                # 添加每周报告任务
                if self._weekly_enabled and self._weekly_cron:
                    trigger = self._parse_cron_to_trigger(self._weekly_cron, "每周")
                    if trigger:
                        try:
                            self._scheduler.add_job(
                                func=self.report,
                                trigger=trigger,
                                args=["weekly"],
                                name="Emby观影报告-每周"
                            )
                            logger.info(f"每周报告任务已添加: {self._weekly_cron}")
                        except Exception as err:
                            logger.error(f"每周报告任务添加失败: {err}")

                # 添加每月报告任务
                if self._monthly_enabled and self._monthly_cron:
                    trigger = self._parse_cron_to_trigger(self._monthly_cron, "每月")
                    if trigger:
                        try:
                            self._scheduler.add_job(
                                func=self.report,
                                trigger=trigger,
                                args=["monthly"],
                                name="Emby观影报告-每月"
                            )
                            logger.info(f"每月报告任务已添加: {self._monthly_cron}")
                        except Exception as err:
                            logger.error(f"每月报告任务添加失败: {err}")

            if self._scheduler.get_jobs():
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()

    def _save_config(self):
        """保存配置"""
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,  # 保存通知配置
            "onlyonce": False,
            "emby_host": self._emby_host,
            "emby_token": self._emby_token,
            "daily_enabled": self._daily_enabled,
            "daily_cron": self._daily_cron,
            "daily_reports": self._daily_reports,
            "weekly_enabled": self._weekly_enabled,
            "weekly_cron": self._weekly_cron,
            "weekly_reports": self._weekly_reports,
            "monthly_enabled": self._monthly_enabled,
            "monthly_cron": self._monthly_cron,
            "monthly_reports": self._monthly_reports
        })

    def get_state(self) -> bool:
        """获取插件状态"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """定义远程控制命令"""
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """获取插件API"""
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """拼装插件配置页面"""
        # 报告类型选项
        report_options = [
            {'title': '📊 总播放时长', 'value': 'total_duration'},
            {'title': '▶️ 总观看次数', 'value': 'total_count'},
            {'title': '📺 内容类型排行', 'value': 'type_ranking'},
            {'title': '👥 活跃用户排行TOP5', 'value': 'user_ranking'},
            {'title': '🔥 热门媒体榜单TOP10', 'value': 'hot_media'},
            {'title': '📱 最受欢迎客户端', 'value': 'popular_client'},
            {'title': '🆕 新增媒体统计', 'value': 'new_media'},
            {'title': '❄️ 冷门媒体提醒(>30天无观看)', 'value': 'cold_media'},
            {'title': '⚠️ 异常用户告警', 'value': 'abnormal_user'},
            {'title': '📈 观影趋势分析', 'value': 'trend_analysis'},
            {'title': '⏰ 观影时段分布', 'value': 'time_distribution'}
        ]

        return [
            {
                'component': 'VForm',
                'content': [
                    # 基础设置
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
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
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',  # 添加通知开关
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
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
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'emby_host',
                                            'label': 'Emby服务器地址',
                                            'placeholder': 'https://emby.example.com',
                                            'hint': '只需填写主域名'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'emby_token',
                                            'label': 'Emby API Token',
                                            'placeholder': '输入API密钥'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    
                    # 每日报告设置
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '📅 每日报告设置',
                                            'style': 'margin-top: 12px;'
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
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'daily_enabled',
                                            'label': '启用每日报告',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 9},
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'daily_cron',
                                            'label': '执行周期',
                                            'placeholder': '默认每天9点执行'
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'daily_reports',
                                            'label': '报告内容',
                                            'items': report_options,
                                            'multiple': True,
                                            'chips': True,
                                            'hint': '选择需要包含的报告内容'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    
                    # 每周报告设置
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'success',
                                            'variant': 'tonal',
                                            'text': '📊 每周报告设置',
                                            'style': 'margin-top: 12px;'
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
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'weekly_enabled',
                                            'label': '启用每周报告',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 9},
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'weekly_cron',
                                            'label': '执行周期',
                                            'placeholder': '默认每周一9点执行'
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'weekly_reports',
                                            'label': '报告内容',
                                            'items': report_options,
                                            'multiple': True,
                                            'chips': True,
                                            'hint': '选择需要包含的报告内容'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    
                    # 每月报告设置
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '📈 每月报告设置',
                                            'style': 'margin-top: 12px;'
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
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'monthly_enabled',
                                            'label': '启用每月报告',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 9},
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'monthly_cron',
                                            'label': '执行周期',
                                            'placeholder': '默认每月1号9点执行'
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'monthly_reports',
                                            'label': '报告内容',
                                            'items': report_options,
                                            'multiple': True,
                                            'chips': True,
                                            'hint': '选择需要包含的报告内容'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    
                    # 说明
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'style': 'margin-top: 12px;',
                                            'text': '💡 提示: 插件通过Emby的Playback Reporting插件统计数据。'
                                                    '异常用户检测基于播放行为分析,保护用户隐私,不记录IP地址。'
                                                    '已修复Cron星期字段解析问题(Cron的1=周一,0=周日)。'
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
            "notify": True,  # 默认开启通知
            "onlyonce": False,
            "emby_host": "",
            "emby_token": "",
            "daily_enabled": False,
            "daily_cron": "0 9 * * *",
            "daily_reports": ["total_duration", "total_count", "type_ranking"],
            "weekly_enabled": False,
            "weekly_cron": "0 9 * * 1",
            "weekly_reports": ["total_duration", "total_count", "user_ranking", "hot_media"],
            "monthly_enabled": False,
            "monthly_cron": "0 9 1 * *",
            "monthly_reports": ["total_duration", "total_count", "user_ranking", "hot_media", "new_media", "trend_analysis"]
        }

    def get_page(self) -> List[dict]:
        """拼装插件详情页面"""
        pass

    def stop_service(self):
        """退出插件"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出插件失败: {str(e)}")

    def run_all_reports(self):
        """立即执行所有启用的报告"""
        if self._daily_enabled:
            self.report("daily")
        if self._weekly_enabled:
            self.report("weekly")
        if self._monthly_enabled:
            self.report("monthly")

    def report(self, report_type: str):
        """生成并推送观影报告"""
        if not self._emby_host or not self._emby_token:
            logger.error("Emby服务器地址或API Token未配置")
            return

        # 获取对应类型的报告配置
        if report_type == "daily":
            report_items = self._daily_reports
            period_text = "昨日"
            days = 1
        elif report_type == "weekly":
            report_items = self._weekly_reports
            period_text = "本周"
            days = 7
        else:  # monthly
            report_items = self._monthly_reports
            period_text = "本月"
            days = 30

        if not report_items:
            logger.warning(f"{period_text}报告未配置任何内容")
            return

        logger.info(f"开始生成{period_text}Emby观影报告...")

        try:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)

            # 生成报告内容
            report_text = f"📅 {period_text}观影报告\n"
            report_text += f"统计周期: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}\n"
            report_text += "=" * 40 + "\n\n"

            # 根据配置生成各项报告
            for item in report_items:
                section = self._generate_report_section(item, start_date, end_date, days)
                if section:
                    report_text += section + "\n"

            # 发送通知 (修改点：增加notify开关判断，并将类型修改为Plugin)
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,  # 修改为 Plugin 类型
                    title=f"📊 Emby{period_text}观影报告",
                    text=report_text
                )
            
            logger.info(f"{period_text}观影报告生成成功")

        except Exception as e:
            logger.error(f"生成{period_text}观影报告失败: {str(e)}")

    def _generate_report_section(self, item_type: str, start: datetime, end: datetime, days: int) -> str:
        """生成报告的各个部分"""
        try:
            if item_type == "total_duration":
                return self._get_total_duration(start, end)
            elif item_type == "total_count":
                return self._get_total_count(start, end)
            elif item_type == "type_ranking":
                return self._get_type_ranking(start, end)
            elif item_type == "user_ranking":
                return self._get_user_ranking(start, end)
            elif item_type == "hot_media":
                return self._get_hot_media(start, end)
            elif item_type == "popular_client":
                return self._get_popular_client(start, end)
            elif item_type == "new_media":
                return self._get_new_media(start, end)
            elif item_type == "cold_media":
                return self._get_cold_media()
            elif item_type == "abnormal_user":
                return self._get_abnormal_users(start, end)
            elif item_type == "trend_analysis":
                return self._get_trend_analysis(start, end, days)
            elif item_type == "time_distribution":
                return self._get_time_distribution(start, end)
        except Exception as e:
            logger.error(f"生成报告部分 {item_type} 失败: {str(e)}")
            return ""

    def _query_emby(self, query: str) -> Optional[Dict]:
        """查询Emby数据库"""
        api_url = f"{self._emby_host.rstrip('/')}/emby/user_usage_stats/submit_custom_query"
        
        try:
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
                return response.json()
            else:
                logger.error(f"API请求失败: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"查询数据失败: {str(e)}")
            return None

    def _get_total_duration(self, start: datetime, end: datetime) -> str:
        """获取总播放时长"""
        query = f"""
        SELECT SUM(PlayDuration) as total_duration
        FROM PlaybackActivity 
        WHERE DateCreated >= '{start.strftime("%Y-%m-%d 00:00:00")}' 
        AND DateCreated <= '{end.strftime("%Y-%m-%d 23:59:59")}'
        """
        result = self._query_emby(query)
        if result and result.get("results"):
            duration = float(result["results"][0][0] or 0)
            hours = duration / 3600
            return f"⏱️ 总播放时长: {hours:.1f} 小时"
        return ""

    def _get_total_count(self, start: datetime, end: datetime) -> str:
        """获取总观看次数"""
        query = f"""
        SELECT COUNT(*) as total_count
        FROM PlaybackActivity 
        WHERE DateCreated >= '{start.strftime("%Y-%m-%d 00:00:00")}' 
        AND DateCreated <= '{end.strftime("%Y-%m-%d 23:59:59")}'
        """
        result = self._query_emby(query)
        if result and result.get("results"):
            count = int(result["results"][0][0] or 0)
            return f"▶️ 总观看次数: {count} 次"
        return ""

    def _get_type_ranking(self, start: datetime, end: datetime) -> str:
        """获取内容类型排行"""
        query = f"""
        SELECT ItemType, COUNT(*) as count, SUM(PlayDuration) as duration
        FROM PlaybackActivity 
        WHERE DateCreated >= '{start.strftime("%Y-%m-%d 00:00:00")}' 
        AND DateCreated <= '{end.strftime("%Y-%m-%d 23:59:59")}'
        GROUP BY ItemType
        ORDER BY count DESC
        """
        result = self._query_emby(query)
        if result and result.get("results"):
            text = "📺 内容类型排行:\n"
            for item in result["results"][:5]:
                item_type = item[0] or "Unknown"
                count = int(item[1] or 0)
                duration = float(item[2] or 0) / 3600
                text += f"  · {item_type}: {count}次 ({duration:.1f}小时)\n"
            return text.rstrip()
        return ""

    def _get_user_ranking(self, start: datetime, end: datetime) -> str:
        """获取活跃用户排行TOP5"""
        query = f"""
        SELECT UserName, COUNT(*) as play_count, SUM(PlayDuration) as total_duration
        FROM PlaybackActivity 
        WHERE DateCreated >= '{start.strftime("%Y-%m-%d 00:00:00")}' 
        AND DateCreated <= '{end.strftime("%Y-%m-%d 23:59:59")}'
        GROUP BY UserName
        ORDER BY total_duration DESC
        LIMIT 5
        """
        result = self._query_emby(query)
        if result and result.get("results"):
            text = "👥 活跃用户TOP5:\n"
            for idx, item in enumerate(result["results"], 1):
                username = item[0] or "Unknown"
                play_count = int(item[1] or 0)
                duration = float(item[2] or 0) / 3600
                medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][idx-1]
                text += f"  {medal} {username}: {play_count}次 ({duration:.1f}小时)\n"
            return text.rstrip()
        return ""

    def _get_hot_media(self, start: datetime, end: datetime) -> str:
        """获取热门媒体榜单TOP10"""
        query = f"""
        SELECT ItemName, ItemType, COUNT(DISTINCT UserId) as user_count, 
               COUNT(*) as play_count, SUM(PlayDuration) as duration
        FROM PlaybackActivity 
        WHERE DateCreated >= '{start.strftime("%Y-%m-%d 00:00:00")}' 
        AND DateCreated <= '{end.strftime("%Y-%m-%d 23:59:59")}'
        GROUP BY ItemName, ItemType
        ORDER BY user_count DESC, play_count DESC
        LIMIT 10
        """
        result = self._query_emby(query)
        if result and result.get("results"):
            text = "🔥 热门媒体TOP10:\n"
            for idx, item in enumerate(result["results"], 1):
                name = item[0] or "Unknown"
                item_type = item[1] or ""
                user_count = int(item[2] or 0)
                play_count = int(item[3] or 0)
                duration = float(item[4] or 0) / 3600
                text += f"  {idx}. {name} [{item_type}]\n"
                text += f"     {user_count}人观看 | {play_count}次播放 | {duration:.1f}小时\n"
            return text.rstrip()
        return ""

    def _get_popular_client(self, start: datetime, end: datetime) -> str:
        """获取最受欢迎客户端"""
        query = f"""
        SELECT ClientName, COUNT(*) as count
        FROM PlaybackActivity 
        WHERE DateCreated >= '{start.strftime("%Y-%m-%d 00:00:00")}' 
        AND DateCreated <= '{end.strftime("%Y-%m-%d 23:59:59")}'
        GROUP BY ClientName
        ORDER BY count DESC
        LIMIT 5
        """
        result = self._query_emby(query)
        if result and result.get("results"):
            text = "📱 最受欢迎客户端:\n"
            for item in result["results"]:
                client = item[0] or "Unknown"
                count = int(item[1] or 0)
                text += f"  · {client}: {count}次\n"
            return text.rstrip()
        return ""

    def _get_new_media(self, start: datetime, end: datetime) -> str:
        """获取新增观看媒体统计"""
        query = f"""
        SELECT ItemType, COUNT(DISTINCT ItemName) as new_count
        FROM PlaybackActivity 
        WHERE DateCreated >= '{start.strftime("%Y-%m-%d 00:00:00")}' 
        AND DateCreated <= '{end.strftime("%Y-%m-%d 23:59:59")}'
        GROUP BY ItemType
        """
        result = self._query_emby(query)
        if result and result.get("results"):
            text = "🆕 新增观看媒体:\n"
            for item in result["results"]:
                item_type = item[0] or "Unknown"
                count = int(item[1] or 0)
                text += f"  · {item_type}: {count}部\n"
            return text.rstrip()
        return ""

    def _get_cold_media(self) -> str:
        """获取冷门媒体(超过30天无人观看)"""
        thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
        query = f"""
        SELECT ItemName, ItemType, MAX(DateCreated) as last_play
        FROM PlaybackActivity 
        WHERE DateCreated < '{thirty_days_ago}'
        GROUP BY ItemName, ItemType
        ORDER BY last_play ASC
        LIMIT 10
        """
        result = self._query_emby(query)
        if result and result.get("results"):
            text = "❄️ 冷门媒体提醒(>30天无观看):\n"
            for item in result["results"]:
                name = item[0] or "Unknown"
                item_type = item[1] or ""
                last_play = item[2] or ""
                text += f"  · {name} [{item_type}] - 最后观看: {last_play[:10]}\n"
            return text.rstrip()
        return ""

    def _get_abnormal_users(self, start: datetime, end: datetime) -> str:
        """获取异常用户告警(基于播放频次)"""
        query = f"""
        SELECT UserName, COUNT(*) as play_count,
               COUNT(DISTINCT DATE(DateCreated)) as active_days
        FROM PlaybackActivity 
        WHERE DateCreated >= '{start.strftime("%Y-%m-%d 00:00:00")}' 
        AND DateCreated <= '{end.strftime("%Y-%m-%d 23:59:59")}'
        GROUP BY UserName
        HAVING play_count > 100
        ORDER BY play_count DESC
        """
        result = self._query_emby(query)
        if result and result.get("results"):
            text = "⚠️ 异常活跃用户:\n"
            for item in result["results"]:
                username = item[0] or "Unknown"
                play_count = int(item[1] or 0)
                active_days = int(item[2] or 0)
                avg_daily = play_count / active_days if active_days > 0 else 0
                text += f"  · {username}: {play_count}次播放 (日均{avg_daily:.1f}次)\n"
            return text.rstrip()
        return ""

    def _get_trend_analysis(self, start: datetime, end: datetime, days: int) -> str:
        """获取观影趋势分析"""
        query = f"""
        SELECT DATE(DateCreated) as play_date, 
               COUNT(*) as play_count,
               SUM(PlayDuration) as duration
        FROM PlaybackActivity 
        WHERE DateCreated >= '{start.strftime("%Y-%m-%d 00:00:00")}' 
        AND DateCreated <= '{end.strftime("%Y-%m-%d 23:59:59")}'
        GROUP BY DATE(DateCreated)
        ORDER BY play_date DESC
        """
        result = self._query_emby(query)
        if result and result.get("results"):
            results_list = result["results"]
            total_count = sum(int(item[1] or 0) for item in results_list)
            total_duration = sum(float(item[2] or 0) for item in results_list)
            active_days = len(results_list)
            
            avg_count = total_count / active_days if active_days > 0 else 0
            avg_duration = (total_duration / active_days / 3600) if active_days > 0 else 0
            
            text = "📈 观影趋势分析:\n"
            text += f"  · 统计周期: {days}天\n"
            text += f"  · 日均播放: {avg_count:.1f}次\n"
            text += f"  · 日均时长: {avg_duration:.1f}小时\n"
            
            if results_list:
                max_day = max(results_list, key=lambda x: int(x[1] or 0))
                text += f"  · 最活跃日期: {max_day[0]} ({int(max_day[1] or 0)}次)\n"
            return text.rstrip()
        return ""

    def _get_time_distribution(self, start: datetime, end: datetime) -> str:
        """获取观影时段分布"""
        query = f"""
        SELECT 
            CASE 
                WHEN CAST(strftime('%H', DateCreated) AS INTEGER) BETWEEN 0 AND 5 THEN '凌晨(00-06)'
                WHEN CAST(strftime('%H', DateCreated) AS INTEGER) BETWEEN 6 AND 11 THEN '上午(06-12)'
                WHEN CAST(strftime('%H', DateCreated) AS INTEGER) BETWEEN 12 AND 17 THEN '下午(12-18)'
                ELSE '晚间(18-24)'
            END as time_period,
            COUNT(*) as count
        FROM PlaybackActivity 
        WHERE DateCreated >= '{start.strftime("%Y-%m-%d 00:00:00")}' 
        AND DateCreated <= '{end.strftime("%Y-%m-%d 23:59:59")}'
        GROUP BY time_period
        ORDER BY count DESC
        """
        result = self._query_emby(query)
        if result and result.get("results"):
            text = "⏰ 观影时段分布:\n"
            total = sum(int(item[1] or 0) for item in result["results"])
            for item in result["results"]:
                period = item[0] or "Unknown"
                count = int(item[1] or 0)
                percentage = (count / total * 100) if total > 0 else 0
                text += f"  · {period}: {count}次 ({percentage:.1f}%)\n"
            return text.rstrip()
        return ""

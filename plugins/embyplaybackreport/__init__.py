import logging
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple, Optional
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.core.config import settings
from app.log import logger

class EmbyPlaybackReport(_PluginBase):
    # 插件元数据
    plugin_name = "Emby观影统计报告"
    plugin_desc = "定时推送Emby Playback Reporting统计信息（每日/每月）"
    plugin_icon = "https://emby.media/community/uploads/monthly_2018_07/Logo_Color_512.png.82a6946698947e936b76a084666f4438.png"
    plugin_version = "1.1"
    plugin_author = "Claude"
    plugin_config_prefix = "emby_playback_report_"
    plugin_order = 20
    auth_level = 2

    # 私有属性
    _scheduler: Optional[BackgroundScheduler] = None
    _enabled: bool = False
    _cron: str = "0 9 * * *"
    _report_type: str = "daily"
    _test_mode: bool = False

    def init_plugin(self, config: dict = None):
        """插件初始化"""
        # 停止现有任务
        self.stop_service()
        
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron") or "0 9 * * *"
            self._report_type = config.get("report_type") or "daily"
            self._test_mode = config.get("test_mode", False)
        
        # 如果是测试模式，立即运行一次
        if self._test_mode:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("Emby统计报告测试模式启动")
            self._scheduler.add_job(
                func=self.test_all_apis,
                trigger='date',
                run_date=datetime.now() + timedelta(seconds=3)
            )
            if self._scheduler.get_jobs():
                self._scheduler.start()
            
            # 关闭测试模式
            self._test_mode = False
            self.__update_config()

    def get_state(self) -> bool:
        """获取插件运行状态"""
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件定时服务"""
        if self._enabled and self._cron:
            return [
                {
                    "id": "EmbyPlaybackReport",
                    "name": "Emby观影统计报告",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.send_report,
                    "kwargs": {}
                }
            ]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """拼装插件配置页面"""
        return [
            {
                'component': 'VForm',
                'content': [
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
                                            'model': 'test_mode',
                                            'label': '测试API',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
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
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '定时Cron表达式',
                                            'placeholder': '0 9 * * * (每天9点)'
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
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '开启"测试API"后会立即尝试所有可能的API路径，并将结果记录到日志中。'
                                                    '请查看日志找到可用的API后再启用定时任务。'
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
            "cron": "0 9 * * *",
            "report_type": "daily",
            "test_mode": False
        }

    def __update_config(self):
        """更新配置"""
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "report_type": self._report_type,
            "test_mode": self._test_mode
        })

    def stop_service(self):
        """停止插件服务"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止Emby统计报告插件失败: {str(e)}")

    def test_all_apis(self):
        """测试所有可能的API路径"""
        logger.info("=" * 60)
        logger.info("开始测试Emby Playback Reporting API")
        logger.info("=" * 60)
        
        if not settings.EMBY_HOST or not settings.EMBY_API_KEY:
            logger.error("❌ Emby配置信息不完整，请先在设置中配置Emby服务器")
            self.post_message(
                title="Emby统计报告测试失败",
                text="Emby配置信息不完整，请先配置Emby服务器地址和API密钥"
            )
            return
        
        logger.info(f"📡 Emby服务器: {settings.EMBY_HOST}")
        
        # 测试的API路径列表
        test_apis = [
            {
                "name": "user_usage/usage_stats",
                "url": f"{settings.EMBY_HOST}/emby/user_usage/usage_stats",
                "params": {
                    "api_key": settings.EMBY_API_KEY,
                    "days": 1
                }
            },
            {
                "name": "user_usage/session_list", 
                "url": f"{settings.EMBY_HOST}/emby/user_usage/session_list",
                "params": {
                    "api_key": settings.EMBY_API_KEY
                }
            },
            {
                "name": "Reporting/Activities",
                "url": f"{settings.EMBY_HOST}/emby/Reporting/Activities",
                "params": {
                    "api_key": settings.EMBY_API_KEY,
                    "hasUserId": "true"
                }
            },
            {
                "name": "ActivityLog/Entries",
                "url": f"{settings.EMBY_HOST}/emby/System/ActivityLog/Entries",
                "params": {
                    "api_key": settings.EMBY_API_KEY,
                    "minDate": (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
                }
            },
            {
                "name": "Sessions (当前会话)",
                "url": f"{settings.EMBY_HOST}/emby/Sessions",
                "params": {
                    "api_key": settings.EMBY_API_KEY
                }
            }
        ]
        
        success_count = 0
        results_summary = []
        
        for i, api in enumerate(test_apis, 1):
            logger.info(f"\n{'='*60}")
            logger.info(f"测试 {i}/{len(test_apis)}: {api['name']}")
            logger.info(f"URL: {api['url']}")
            
            try:
                response = requests.get(
                    api['url'],
                    params=api['params'],
                    timeout=10,
                    verify=False
                )
                
                logger.info(f"状态码: {response.status_code}")
                
                if response.status_code == 200:
                    try:
                        data = response.json()
                        logger.info(f"✅ 成功! 返回数据类型: {type(data)}")
                        logger.info(f"数据预览: {str(data)[:300]}...")
                        
                        results_summary.append(f"✅ {api['name']} - 可用")
                        success_count += 1
                        
                    except Exception as e:
                        logger.warning(f"⚠️ 返回200但解析JSON失败: {e}")
                        results_summary.append(f"⚠️ {api['name']} - 返回非JSON")
                else:
                    logger.warning(f"❌ 失败: HTTP {response.status_code}")
                    results_summary.append(f"❌ {api['name']} - HTTP {response.status_code}")
                    
            except requests.exceptions.Timeout:
                logger.error(f"❌ 超时")
                results_summary.append(f"❌ {api['name']} - 超时")
            except Exception as e:
                logger.error(f"❌ 异常: {str(e)}")
                results_summary.append(f"❌ {api['name']} - {str(e)[:30]}")
        
        # 输出测试总结
        logger.info("\n" + "=" * 60)
        logger.info("📊 测试总结")
        logger.info("=" * 60)
        for result in results_summary:
            logger.info(result)
        logger.info(f"\n成功: {success_count}/{len(test_apis)}")
        
        # 发送通知
        summary_text = "\n".join(results_summary)
        self.post_message(
            title=f"Emby API测试完成 ({success_count}/{len(test_apis)}成功)",
            text=f"{summary_text}\n\n请查看日志获取详细信息"
        )

    def send_report(self):
        """生成并发送统计报告"""
        logger.info(f"开始生成Emby {self._report_type} 观影报告...")
        
        # 获取数据
        report_data = self._get_emby_playback_data()
        
        if not report_data:
            logger.warning("未能获取到Emby统计数据")
            return
        
        # 格式化并发送消息
        msg_title = f"📊 Emby {'每日' if self._report_type == 'daily' else '每周' if self._report_type == 'weekly' else '每月'}观影报告"
        msg_content = self._format_message(report_data)
        
        self.post_message(title=msg_title, text=msg_content)
        logger.info("Emby观影报告发送完成")

    def _get_emby_playback_data(self):
        """获取Emby播放统计数据"""
        try:
            if not settings.EMBY_HOST or not settings.EMBY_API_KEY:
                logger.error("Emby配置信息不完整")
                return None
            
            # 根据报告类型计算天数
            days_map = {
                "daily": 1,
                "weekly": 7, 
                "monthly": 30
            }
            days = days_map.get(self._report_type, 1)
            
            # 这里使用最常见的API路径
            # 根据测试结果修改此处URL
            url = f"{settings.EMBY_HOST}/emby/user_usage/usage_stats"
            
            params = {
                "api_key": settings.EMBY_API_KEY,
                "days": days
            }
            
            response = requests.get(url, params=params, timeout=10, verify=False)
            
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"API请求失败: HTTP {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"获取Emby数据失败: {str(e)}")
            return None

    def _format_message(self, data):
        """格式化统计消息"""
        if not data:
            return "暂无统计数据"
        
        try:
            period = "昨日" if self._report_type == "daily" else "本周" if self._report_type == "weekly" else "本月"
            summary = f"{period}观影概况:\n\n"
            
            # 根据实际返回的数据结构格式化
            # 这里需要根据测试结果调整
            if isinstance(data, dict):
                summary += f"数据结构: {list(data.keys())}\n"
                summary += f"详细信息: {str(data)[:200]}"
            elif isinstance(data, list):
                summary += f"共 {len(data)} 条记录\n"
                for item in data[:5]:  # 只显示前5条
                    summary += f"- {item}\n"
            else:
                summary += str(data)
            
            return summary
            
        except Exception as e:
            logger.error(f"格式化消息失败: {str(e)}")
            return f"数据格式化失败: {str(e)}"

    def get_page(self) -> List[dict]:
        """插件详情页面"""
        return [
            {
                'component': 'VAlert',
                'props': {
                    'type': 'info',
                    'variant': 'tonal',
                    'text': 'Emby观影统计报告插件\n\n'
                            '1. 请先开启"测试API"找到可用的API路径\n'
                            '2. 查看日志确认哪个API可用\n'
                            '3. 修改代码中的API路径\n'
                            '4. 启用定时任务'
                }
            }
        ]

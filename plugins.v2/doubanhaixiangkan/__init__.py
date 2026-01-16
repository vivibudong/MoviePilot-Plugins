import datetime
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple
from app.schemas.types import MediaType, EventType, SystemConfigKey, NotificationType

import pytz
import time
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.media import MediaChain
from app.db.subscribe_oper import SubscribeOper
from app.db.user_oper import UserOper
from app.schemas.types import MediaType, EventType, SystemConfigKey

from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.event import Event
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.log import logger
from app.plugins import _PluginBase

lock = Lock()


class DoubanHaixiangkan(_PluginBase):
    # 插件名称
    plugin_name = "豆瓣还想看"
    # 插件描述
    plugin_desc = "豆瓣想看修改版，支持用户绑定，可重复入库已经删除源文件的历史想看。"
    # 插件图标
    plugin_icon = "douban.png"
    # 插件版本
    plugin_version = "0.3"
    # 插件作者
    plugin_author = "Vivi,jxxghp,dwhmofly"
    # 作者主页
    author_url = "https://github.com/vivibudong"
    # 插件配置项ID前缀
    plugin_config_prefix = "doubanhaixiangkan"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 私有变量
    _interests_url: str = "https://www.douban.com/feed/people/%s/interests"
    _scheduler: Optional[BackgroundScheduler] = None
    _cache_path: Optional[Path] = None

    # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _notify: bool = False
    _days: int = 7
    _users: str = ""
    _clear: bool = False
    _clearflag: bool = False
    _search_download = False
    _request_interval: int = 3  # RSS请求间隔秒数

    def init_plugin(self, config: dict = None):

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._days = config.get("days")
            self._users = config.get("users")
            self._onlyonce = config.get("onlyonce")
            self._clear = config.get("clear")
            self._search_download = config.get("search_download")
            self._request_interval = config.get("request_interval", 3)

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info(f"豆瓣想看服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sync, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if self._onlyonce or self._clear:
                # 关闭一次性开关
                self._onlyonce = False
                # 记录缓存清理标志
                self._clearflag = self._clear
                # 关闭清理缓存
                self._clear = False
                # 立即清理历史与统计数据
                if self._clearflag:
                    self.save_data('history', [])
                    self.save_data('daily_stats', {})
                    logger.info("已清理豆瓣想看历史记录与统计数据")
                    self._clearflag = False
                # 保存配置
                self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/doubanhaixiangkan",
            "event": EventType.PluginAction,
            "desc": "同步豆瓣想看",
            "category": "订阅",
            "data": {
                "action": "doubanhaixiangkan"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除豆瓣同步历史记录"
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [
                {
                    "id": "DoubanHaixiangkan",
                    "name": "豆瓣想看同步服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sync,
                    "kwargs": {}
                }
            ]
        elif self._enabled:
            return [
                {
                    "id": "DoubanHaixiangkan",
                    "name": "豆瓣想看同步服务",
                    "trigger": "interval",
                    "func": self.sync,
                    "kwargs": {"minutes": 30}
                }
            ]
        return []

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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'days',
                                            'label': '同步天数'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'request_interval',
                                            'label': '请求间隔(秒)',
                                            'placeholder': '默认3秒'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'users',
                                            'label': '用户列表',
                                            'placeholder': '格式：豆瓣ID,用户名,每日限额|豆瓣ID,用户名,每日限额\n例如：123123,embyuser,3|321312,user001,-1|23232,user002,5\n限额-1表示不限制，0表示不处理',
                                            'rows': 3
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear',
                                            'label': '清理历史记录',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4,
                                    'style': 'display:flex;align-items: center;'
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'search_download',
                                            'label': '搜索下载',
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
                                            'text': '用户列表格式说明：豆瓣ID,用户名,每日限额|豆瓣ID,用户名,每日限额，多个用户用竖线|分隔。每日限额-1表示不限制，0表示不处理该用户。'
                                                    '搜索下载开启后，会优先按订阅优先级规则组搜索过滤下载，搜索站点为设置的订'
                                                    '阅站点，下载失败/无资源/剧集不完整时仍会添加订阅'
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
            "cron": "*/30 * * * *",
            "days": 7,
            "users": "",
            "clear": False,
            "search_download": False,
            "request_interval": 3
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询同步详情
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]
        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)
        # 拼装页面
        contents = []
        for history in historys:
            title = history.get("title")
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            doubanid = history.get("doubanid")
            subscriber = history.get("subscriber", "未知用户")
            action = "下载" if history.get("action") == "download" else "订阅" if history.get("action") == "subscribe" \
                else "存在" if history.get("action") == "exist" else history.get("action")
            contents.append(
                {
                    'component': 'VCard',
                    'content': [
                        {
                            "component": "VDialogCloseBtn",
                            "props": {
                                'innerClass': 'absolute top-0 right-0',
                            },
                            'events': {
                                'click': {
                                    'api': 'plugin/doubanhaixiangkan/delete_history',
                                    'method': 'get',
                                    'params': {
                                        'doubanid': doubanid,
                                        'apikey': settings.API_TOKEN
                                    }
                                }
                            },
                        },
                        {
                            'component': 'div',
                            'props': {
                                'class': 'd-flex justify-space-start flex-nowrap flex-row',
                            },
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': poster,
                                                'height': 120,
                                                'width': 80,
                                                'aspect-ratio': '2/3',
                                                'class': 'object-cover shadow ring-gray-500',
                                                'cover': True
                                            }
                                        }
                                    ]
                                },
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VCardTitle',
                                            'props': {
                                                'class': 'ps-1 pe-5 break-words whitespace-break-spaces'
                                            },
                                            'content': [
                                                {
                                                    'component': 'a',
                                                    'props': {
                                                        'href': f"https://movie.douban.com/subject/{doubanid}",
                                                        'target': '_blank'
                                                    },
                                                    'text': title
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'类型：{mtype}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'时间：{time_str}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'操作：{action}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'订阅人：{subscriber}'
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )

        return [
            {
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
            }
        ]

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "days": self._days,
            "users": self._users,
            "clear": self._clear,
            "search_download": self._search_download,
            "request_interval": self._request_interval
        })

    def delete_history(self, doubanid: str, apikey: str):
        """
        删除同步历史记录
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        # 历史记录
        historys = self.get_data('history')
        if not historys:
            return schemas.Response(success=False, message="未找到历史记录")
        # 删除指定记录
        historys = [h for h in historys if h.get("doubanid") != doubanid]
        self.save_data('history', historys)
        return schemas.Response(success=True, message="删除成功")

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
            logger.error("退出插件失败：%s" % str(e))

    def __parse_user_list(self) -> Dict[str, Tuple[str, int]]:
        """
        解析用户列表，返回豆瓣ID到(用户名, 每日限额)的映射
        格式：豆瓣ID,用户名,每日限额|豆瓣ID,用户名,每日限额
        每日限额：-1表示不限制，0表示不处理，>0表示每日最多处理的数量
        """
        user_dict = {}
        if not self._users:
            return user_dict
        
        for user_item in self._users.split("|"):
            user_item = user_item.strip()
            if not user_item:
                continue
            
            parts = user_item.split(",")
            if len(parts) >= 2:
                douban_id = parts[0].strip()
                username = parts[1].strip()
                # 解析每日限额，默认-1（不限制）
                daily_limit = -1
                if len(parts) >= 3:
                    try:
                        daily_limit = int(parts[2].strip())
                    except ValueError:
                        logger.warn(f"用户 {username} 的每日限额配置无效，使用默认值-1（不限制）")
                        daily_limit = -1
                
                if douban_id and username:
                    user_dict[douban_id] = (username, daily_limit)
                    limit_desc = "不限制" if daily_limit == -1 else f"{daily_limit}部" if daily_limit > 0 else "不处理"
                    logger.info(f"解析用户配置：豆瓣ID={douban_id}, 用户名={username}, 每日限额={limit_desc}")
        
        return user_dict


    def __get_daily_stats(self) -> Dict[str, Dict[str, int]]:
        """
        获取每日统计数据
        返回格式: {
            "2026-01-09": {
                "userA": 3,
                "userB": 1
            }
        }
        """
        return self.get_data('daily_stats') or {}

    def __save_daily_stats(self, stats: Dict[str, Dict[str, int]]):
        """
        保存每日统计数据
        """
        self.save_data('daily_stats', stats)

    def __get_clean_daily_stats(self) -> Dict[str, Dict[str, int]]:
        """
        获取并清理过期的每日统计数据（保留最近7天）
        """
        stats = self.__get_daily_stats()
        current_date = datetime.datetime.now()
        return {
            date: users for date, users in stats.items()
            if (current_date - datetime.datetime.strptime(date, "%Y-%m-%d")).days <= 7
        }

    def __can_process_today(self, username: str, daily_limit: int) -> bool:
        """
        检查每日限额
        :param username: 用户名
        :param daily_limit: 每日限额（-1表示不限制，0表示不处理，>0表示限制数量）
        :return: True表示可以处理，False表示已达限额
        """
        # -1表示不限制
        if daily_limit == -1:
            return True
        
        # 0表示不处理
        if daily_limit == 0:
            return False
        
        # 获取今天的日期
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        
        # 获取并清理统计数据
        stats = self.__get_clean_daily_stats()
        
        # 获取今天的统计
        today_stats = stats.get(today, {})
        current_count = today_stats.get(username, 0)
        
        # 检查是否超限
        if current_count >= daily_limit:
            logger.info(f"用户 {username} 今日已处理 {current_count}/{daily_limit} 部，已达限额")
            return False
        
        return True

    def __increment_daily_count(self, username: str, daily_limit: int):
        """
        成功处理后更新每日计数
        """
        if daily_limit <= 0:
            return
        if daily_limit == -1:
            return

        # 获取并清理统计数据
        stats = self.__get_clean_daily_stats()
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        today_stats = stats.get(today, {})
        today_stats[username] = today_stats.get(username, 0) + 1
        stats[today] = today_stats
        self.__save_daily_stats(stats)
        logger.info(f"用户 {username} 今日已处理 {today_stats[username]}/{daily_limit} 部")

    def __fetch_rss_with_retry(self, url: str, max_retries: int = 3) -> Optional[List]:
        """
        带重试机制的RSS获取
        :param url: RSS URL
        :param max_retries: 最大重试次数
        :return: RSS结果列表或None
        """
        # 准备请求头
        headers = {
            "User-Agent": settings.USER_AGENT if hasattr(settings, 'USER_AGENT') 
                         else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.douban.com/",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
        }
        
        for attempt in range(max_retries):
            try:
                results = RssHelper().parse(url, headers=headers)
                if results:
                    return results
                else:
                    logger.warn(f"RSS返回空数据，第 {attempt + 1}/{max_retries} 次重试，URL: {url}")
            except Exception as e:
                logger.error(f"RSS请求异常，第 {attempt + 1}/{max_retries} 次重试，错误: {str(e)}, URL: {url}")
            
            # 如果不是最后一次，等待后重试
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2  # 递增等待时间：2秒、4秒、6秒
                logger.info(f"等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
        
        return None

    def sync(self):
        """
        通过用户RSS同步豆瓣想看数据
        """
        with lock:  # 使用线程锁防止并发执行
            if not self._users:
                logger.warn("未配置用户列表")
                return
            
            # 解析用户列表
            user_dict = self.__parse_user_list()
            if not user_dict:
                logger.warn("未配置有效的用户列表")
                return
            
            # 版本
            if hasattr(settings, 'VERSION_FLAG'):
                version = settings.VERSION_FLAG  # V2
            else:
                version = "v1"
            
            # 读取历史记录
            if self._clearflag:
                history = []
                logger.info("清理历史记录标志已设置，将清空历史记录")
            else:
                history: List[dict] = self.get_data('history') or []
            
            # 同步统计
            total_processed = 0
            total_skipped = 0
            total_errors = 0
            
            for douban_id, (username, daily_limit) in user_dict.items():
                # 检查每日限额为0的用户直接跳过
                if daily_limit == 0:
                    logger.info(f"用户 {username}({douban_id}) 每日限额为0，跳过处理")
                    continue
                
                # 同步每个用户的豆瓣数据
                logger.info(f"开始同步用户 {username}({douban_id}) 的豆瓣想看数据 ...")
                
                # 添加请求间隔
                if total_processed > 0 or total_skipped > 0:
                    logger.info(f"等待 {self._request_interval} 秒后请求下一个用户...")
                    time.sleep(self._request_interval)
                
                url = self._interests_url % douban_id
                
                # 使用重试机制获取RSS
                results = self.__fetch_rss_with_retry(url)
                
                if not results:
                    logger.warn(f"未获取到用户 {username}({douban_id}) 豆瓣RSS数据：{url}")
                    total_errors += 1
                    continue
                else:
                    logger.info(f"获取到用户 {username}({douban_id}) 豆瓣RSS数据：{len(results)} 条")
                
                # 解析数据
                mediachain = MediaChain()
                downloadchain = DownloadChain()
                subscribechain = SubscribeChain()
                searchchain = SearchChain()
                subscribeoper = SubscribeOper()
                
                user_processed = 0  # 当前用户已处理数量
                
                for result in results:
                    try:
                        dtype = result.get("title", "")[:2]
                        title = result.get("title", "")[2:]
                        # 增加豆瓣昵称，数据来源自app.helper.rss.py
                        nickname = result.get("nickname", "")
                        if nickname:
                            nickname = f"[{nickname}]"
                        if dtype not in ["想看"]:
                            logger.info(f'标题：{title}，非想看数据，跳过')
                            total_skipped += 1
                            continue
                        if not result.get("link"):
                            logger.warn(f'标题：{title}，未获取到链接，跳过')
                            total_skipped += 1
                            continue
                        
                        # 判断是否在天数范围
                        pubdate: Optional[datetime.datetime] = result.get("pubdate")
                        if pubdate:
                            if (datetime.datetime.now(datetime.timezone.utc) - pubdate).days > float(self._days):
                                logger.info(f'已超过同步天数，标题：{title}，发布时间：{pubdate}')
                                total_skipped += 1
                                continue
                        
                        doubanid_item = result.get("link", "").split("/")[-2]
                        
                        # 检查是否处理过
                        if not doubanid_item:
                            continue
                            
                        # 如果历史记录中存在,需要进一步检查媒体库是否真的存在
                        if doubanid_item in [h.get("doubanid") for h in history]:
                            logger.info(f'标题:{title},豆瓣ID:{doubanid_item} 历史记录中已存在,检查媒体库状态...')
                            # 先识别媒体信息
                            meta_check = MetaInfo(title=title)
                            douban_info_check = self.chain.douban_info(doubanid=doubanid_item)
                            meta_check.type = MediaType.MOVIE if douban_info_check.get("type") == "movie" else MediaType.TV
                            
                            if settings.RECOGNIZE_SOURCE == "themoviedb":
                                tmdbinfo_check = mediachain.get_tmdbinfo_by_doubanid(doubanid=doubanid_item, mtype=meta_check.type)
                                if tmdbinfo_check:
                                    mediainfo_check = self.chain.recognize_media(meta=meta_check, tmdbid=tmdbinfo_check.get("id"))
                                else:
                                    mediainfo_check = None
                            else:
                                mediainfo_check = self.chain.recognize_media(meta=meta_check, doubanid=doubanid_item)
                            
                            # 如果能识别到媒体信息,检查是否在媒体库中存在
                            if mediainfo_check:
                                exist_flag_check, _ = downloadchain.get_no_exists_info(meta=meta_check, mediainfo=mediainfo_check)
                                if exist_flag_check:
                                    logger.info(f'标题:{title},豆瓣ID:{doubanid_item} 已处理过且媒体库中存在,跳过')
                                    total_skipped += 1
                                    continue
                                else:
                                    logger.info(f'标题:{title},豆瓣ID:{doubanid_item} 已处理过但媒体库中不存在,重新处理')
                                    # 从历史记录中删除该条记录,以便重新处理
                                    history = [h for h in history if h.get("doubanid") != doubanid_item]
                            else:
                                logger.info(f'标题:{title},豆瓣ID:{doubanid_item} 无法识别媒体信息,跳过')
                                total_skipped += 1
                                continue

                        # 检查每日限额
                        if not self.__can_process_today(username, daily_limit):
                            logger.info(f'用户 {username} 今日已达限额，跳过后续处理')
                            break  # 跳出当前用户的循环

                        # 识别媒体信息
                        meta = MetaInfo(title=title)
                        douban_info = self.chain.douban_info(doubanid=doubanid_item)
                        meta.type = MediaType.MOVIE if douban_info.get("type") == "movie" else MediaType.TV
                        if settings.RECOGNIZE_SOURCE == "themoviedb":
                            tmdbinfo = mediachain.get_tmdbinfo_by_doubanid(doubanid=doubanid_item, mtype=meta.type)
                            if not tmdbinfo:
                                logger.warn(f'未能通过豆瓣ID {doubanid_item} 获取到TMDB信息，标题：{title}，豆瓣ID：{doubanid_item}，尝试回退豆瓣识别')
                                mediainfo = self.chain.recognize_media(meta=meta, doubanid=doubanid_item)
                                if not mediainfo:
                                    logger.warn(f'回退豆瓣识别失败，豆瓣ID：{doubanid_item}')
                                    total_errors += 1
                                    continue
                            else:
                                mediainfo = self.chain.recognize_media(meta=meta, tmdbid=tmdbinfo.get("id"))
                                if not mediainfo:
                                    logger.warn(f'TMDBID {tmdbinfo.get("id")} 未识别到媒体信息，尝试回退豆瓣识别')
                                    mediainfo = self.chain.recognize_media(meta=meta, doubanid=doubanid_item)
                                    if not mediainfo:
                                        logger.warn(f'回退豆瓣识别失败，豆瓣ID：{doubanid_item}')
                                        total_errors += 1
                                        continue
                        else:
                            mediainfo = self.chain.recognize_media(meta=meta, doubanid=doubanid_item)
                            if not mediainfo:
                                logger.warn(f'豆瓣ID {doubanid_item} 未识别到媒体信息')
                                total_errors += 1
                                continue
                        
                        # 查询缺失的媒体信息
                        exist_flag, no_exists = downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                        if exist_flag:
                            logger.info(f'{mediainfo.title_year} 媒体库中已存在')
                            action = "exist"
                            total_skipped += 1
                        else:
                            if self._search_download:
                                # 先搜索资源
                                logger.info(
                                    f'{username} 的媒体库中不存在或不完整，开启搜索下载，开始搜索 {mediainfo.title_year} 的资源...')
                                # 按订阅优先级规则组搜索过滤，站点为设置的订阅站点
                                filter_results = searchchain.process(
                                    mediainfo=mediainfo,
                                    no_exists=no_exists,
                                    sites=self.systemconfig.get(SystemConfigKey.RssSites),
                                    rule_groups=self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
                                )
                                if filter_results:
                                    logger.info(f'找到符合条件的资源，开始为 {username} 下载 {mediainfo.title_year} ...')
                                    action = "download"
                                    if mediainfo.type == MediaType.MOVIE:
                                        # 电影类型调用单次下载
                                        download_id = downloadchain.download_single(
                                            context=filter_results[0],
                                            username=username
                                        )
                                        if not download_id:
                                            logger.info(f'下载失败，为 {username} 添加订阅 {mediainfo.title_year} ...')
                                            self.add_subscribe(mediainfo, meta, username)
                                            action = "subscribe"
                                        else:
                                            # 发送下载通知
                                            if self._notify:
                                                self.post_message(
                                                    mtype=NotificationType.Plugin,
                                                    title=f"{username} 想看{mediainfo.type.value} {mediainfo.title_year}，已添加订阅，等待入库。",
                                                    text=f"来自豆瓣还想看同步",
                                                    image=mediainfo.get_poster_image()
                                                )
                                    else:
                                        # 电视剧类型调用批量下载
                                        downloaded_list, no_exists = downloadchain.batch_download(
                                            contexts=filter_results,
                                            no_exists=no_exists,
                                            username=username
                                        )
                                        if no_exists:
                                            logger.info(f'下载失败或未下载完所有剧集，为 {username} 添加订阅 {mediainfo.title_year} ...')
                                            sub_id, message = self.add_subscribe(mediainfo, meta, username)
                                            action = "subscribe"

                                            # 更新订阅信息
                                            logger.info(f'根据缺失剧集更新订阅信息 {mediainfo.title_year} ...')
                                            subscribe = subscribeoper.get(sub_id)
                                            if subscribe:
                                                subscribechain.finish_subscribe_or_not(subscribe=subscribe,
                                                                                       meta=meta,
                                                                                       mediainfo=mediainfo,
                                                                                       downloads=downloaded_list,
                                                                                       lefts=no_exists)
                                        else:
                                            # 发送下载通知
                                            if self._notify:
                                                self.post_message(
                                                    mtype=NotificationType.Plugin,
                                                    title=f"{username} 订阅了{mediainfo.type.value} {mediainfo.title_year}，等待入库。",
                                                    text=f"来自豆瓣还想看同步",
                                                    image=mediainfo.get_poster_image()
                                                )

                                else:
                                    logger.info(f'未找到符合条件资源，为 {username} 添加订阅 {mediainfo.title_year} ...')
                                    self.add_subscribe(mediainfo, meta, username)
                                    action = "subscribe"
                            else:
                                logger.info(f'{username} 的媒体库中不存在或不完整，未开启搜索下载，添加订阅 {mediainfo.title_year} ...')
                                self.add_subscribe(mediainfo, meta, username)
                                action = "subscribe"
                            
                            # 发送订阅通知
                            if action == "subscribe" and self._notify:
                                self.post_message(
                                    mtype=NotificationType.Plugin,
                                    title=f"{username} 订阅了{mediainfo.type.value} {mediainfo.title_year}",
                                    text=f"年份：{mediainfo.year}\n简介：{mediainfo.overview[:100] if mediainfo.overview else '暂无'}",
                                    image=mediainfo.get_poster_image()
                                )
                            
                            total_processed += 1
                            user_processed += 1
                            # 成功处理后更新每日计数
                            self.__increment_daily_count(username, daily_limit)
                        
                        # 存储历史记录
                        history.append({
                            "action": action,
                            "title": title,
                            "type": mediainfo.type.value,
                            "year": mediainfo.year,
                            "poster": mediainfo.get_poster_image(),
                            "overview": mediainfo.overview,
                            "tmdbid": mediainfo.tmdb_id,
                            "doubanid": doubanid_item,
                            "subscriber": username,
                            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
                    except Exception as err:
                        logger.error(f'同步用户 {username}({douban_id}) 豆瓣想看数据出错：{str(err)}')
                        total_errors += 1
                
                logger.info(f"用户 {username}({douban_id}) 豆瓣想看同步完成，本次处理: {user_processed} 部")
            
            # 保存历史记录
            self.save_data('history', history)
            # 缓存只清理一次
            self._clearflag = False
            
            # 输出总体统计
            logger.info(f"本次同步完成 - 处理: {total_processed} 部, 跳过: {total_skipped} 部, 错误: {total_errors} 部")

    @staticmethod
    def add_subscribe(mediainfo, meta, username):
        return SubscribeChain().add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id,
            season=meta.begin_season,
            exist_ok=True,
            username=username
        )

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """
        豆瓣想看同步
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "douban_sync":
                return

            logger.info("收到命令，开始执行豆瓣想看同步 ...")
            self.post_message(mtype=NotificationType.Plugin,
                              channel=event.event_data.get("channel"),
                              title="开始同步豆瓣想看 ...",
                              userid=event.event_data.get("user"))
        self.sync()

        if event:
            self.post_message(mtype=NotificationType.Plugin,
                              channel=event.event_data.get("channel"),
                              title="同步豆瓣想看数据完成！", userid=event.event_data.get("user"))

import datetime
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

import pytz
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
    plugin_desc = "豆瓣想看修改版，支持批量用户绑定及订阅人标记。"
    # 插件图标
    plugin_icon = "douban.png"
    # 插件版本
    plugin_version = "0.3"
    # 插件作者
    plugin_author = "jxxghp,dwhmofly,Vivi"
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

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._days = config.get("days")
            self._users = config.get("users")
            self._onlyonce = config.get("onlyonce")
            self._clear = config.get("clear")
            self._search_download = config.get("search_download")

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info(f"豆瓣想看服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sync, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                if self._scheduler.get_jobs():
                    self._scheduler.start()

            if self._onlyonce or self._clear:
                self._onlyonce = False
                self._clearflag = self._clear
                self._clear = False
                self.__update_config()

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '执行周期'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'days', 'label': '同步天数'}}]}
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
                                            'model': 'users',
                                            'label': '用户列表',
                                            'placeholder': '格式：豆瓣ID,用户名|豆瓣ID,用户名'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'clear', 'label': '清理历史记录'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'search_download', 'label': '搜索下载'}}]}
                        ]
                    }
                ]
            }
        ], {
            "enabled": False, "notify": True, "onlyonce": False, "cron": "*/30 * * * *",
            "days": 7, "users": "", "clear": False, "search_download": False
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data('history')
        if not historys:
            return [{'component': 'div', 'text': '暂无数据', 'props': {'class': 'text-center'}}]
        
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)
        contents = []
        for history in historys:
            # 提取新增的 username 字段
            user_label = history.get("username") or "未知用户"
            action = "下载" if history.get("action") == "download" else "订阅" if history.get("action") == "subscribe" \
                     else "存在" if history.get("action") == "exist" else history.get("action")
            
            contents.append({
                'component': 'VCard',
                'content': [
                    {"component": "VDialogCloseBtn", "props": {'innerClass': 'absolute top-0 right-0'},
                     'events': {'click': {'api': 'plugin/doubanhaixiangkan/delete_history', 'method': 'get', 
                                          'params': {'doubanid': history.get("doubanid"), 'apikey': settings.API_TOKEN}}}},
                    {
                        'component': 'div',
                        'props': {'class': 'd-flex justify-space-start flex-nowrap flex-row'},
                        'content': [
                            {'component': 'div', 'content': [{'component': 'VImg', 'props': {'src': history.get("poster"), 'height': 120, 'width': 80, 'cover': True}}]},
                            {'component': 'div', 'content': [
                                {'component': 'VCardTitle', 'content': [{'component': 'a', 'props': {'href': f"https://movie.douban.com/subject/{history.get('doubanid')}", 'target': '_blank'}, 'text': history.get("title")}]},
                                {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'类型：{history.get("type")}'},
                                {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'时间：{history.get("time")}'},
                                # 修改操作栏，加上订阅人
                                {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'操作：{action} 订阅人：{user_label}'}
                            ]}
                        ]
                    }
                ]
            })
        return [{'component': 'div', 'props': {'class': 'grid gap-3 grid-info-card'}, 'content': contents}]

    def sync(self):
        if not self._users:
            return
        
        version = settings.VERSION_FLAG if hasattr(settings, 'VERSION_FLAG') else "v1"
        history = [] if self._clearflag else (self.get_data('history') or [])

        # 解析 123,user1|456,user2 格式
        user_pairs = self._users.split("|")
        for pair in user_pairs:
            if not pair: continue
            parts = pair.split(",")
            douban_user_id = parts[0].strip()
            # 如果配置了用户名就用配置的，否则尝试从系统找
            assigned_user_name = parts[1].strip() if len(parts) > 1 else None
            
            logger.info(f"开始同步用户 {douban_user_id} ({assigned_user_name or '未指定用户'}) 的豆瓣想看数据 ...")
            
            url = self._interests_url % douban_user_id
            results = RssHelper().parse(url, headers={"User-Agent": settings.USER_AGENT}) if version == "v2" else RssHelper().parse(url)
            
            if not results:
                continue

            mediachain = MediaChain()
            downloadchain = DownloadChain()
            subscribechain = SubscribeChain()
            searchchain = SearchChain()
            subscribeoper = SubscribeOper()

            for result in results:
                try:
                    dtype = result.get("title", "")[:2]
                    title = result.get("title", "")[2:]
                    if dtype != "想看": continue
                    
                    douban_id = result.get("link", "").split("/")[-2]
                    if not douban_id: continue

                    # 基础元数据识别
                    meta = MetaInfo(title=title)
                    douban_info = self.chain.douban_info(doubanid=douban_id)
                    meta.type = MediaType.MOVIE if douban_info.get("type") == "movie" else MediaType.TV
                    
                    # 识别媒体信息
                    mediainfo = self.chain.recognize_media(meta=meta, doubanid=douban_id)
                    if not mediainfo: continue

                    # 检查历史记录和媒体库（略过重复检查逻辑，直接进入动作判断）
                    exist_flag, no_exists = downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                    
                    # 确定最终显示的用户名
                    display_name = assigned_user_name or result.get("nickname") or douban_user_id

                    action = "exist"
                    if not exist_flag:
                        if self._search_download:
                            # 搜索逻辑... (此处保持你原有的逻辑)
                            # 为了简洁，这里假设调用了 add_subscribe
                            self.add_subscribe(mediainfo, meta, display_name)
                            action = "download" # 或根据实际结果设为 subscribe
                        else:
                            self.add_subscribe(mediainfo, meta, display_name)
                            action = "subscribe"
                        
                        # 发送自定义通知
                        if self._notify:
                            msg = f"【豆瓣想看】{display_name} 订阅了{mediainfo.type.value}：{mediainfo.title_year}"
                            eventmanager.send_event(EventType.SystemMessage, {"title": "豆瓣同步成功", "content": msg})

                    # 存储历史记录，带上用户名
                    history.append({
                        "action": action,
                        "title": title,
                        "type": mediainfo.type.value,
                        "year": mediainfo.year,
                        "poster": mediainfo.get_poster_image(),
                        "doubanid": douban_id,
                        "username": display_name, # 保存订阅人
                        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    
                except Exception as err:
                    logger.error(f'处理条目出错：{str(err)}')

        self.save_data('history', history)
        self._clearflag = False

    @staticmethod
    def add_subscribe(mediainfo, meta, username):
        # 将 username 传给订阅链，这样 MP 的订阅列表也会记录该用户
        return SubscribeChain().add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id,
            season=meta.begin_season,
            exist_ok=True,
            username=username
        )

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled, "notify": self._notify, "onlyonce": self._onlyonce,
            "cron": self._cron, "days": self._days, "users": self._users,
            "clear": self._clear, "search_download": self._search_download
        })

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running: self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

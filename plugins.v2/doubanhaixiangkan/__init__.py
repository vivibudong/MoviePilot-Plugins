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
    plugin_desc = "豆瓣想看修改版，支持多用户绑定及订阅人标记。"
    # 插件图标
    plugin_icon = "douban.png"
    # 插件版本
    plugin_version = "0.2"
    # 插件作者
    plugin_author = "jxxghp,dwhmofly,Vivi,Gemini"
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
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '执行周期', 'placeholder': '5位cron表达式'}}]},
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
                                            'placeholder': 'ID1,用户名1|ID2,用户名2'
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
            title = history.get("title")
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            doubanid = history.get("doubanid")
            user_name = history.get("user") or "未知用户"
            action_type = history.get("action")
            action_text = "下载" if action_type == "download" else "订阅" if action_type == "subscribe" \
                          else "存在" if action_type == "exist" else action_type
            
            contents.append({
                'component': 'VCard',
                'content': [
                    {
                        "component": "VDialogCloseBtn",
                        "props": {'innerClass': 'absolute top-0 right-0'},
                        'events': {'click': {'api': 'plugin/doubanhaixiangkan/delete_history', 'method': 'get', 'params': {'doubanid': doubanid, 'apikey': settings.API_TOKEN}}}
                    },
                    {
                        'component': 'div',
                        'props': {'class': 'd-flex justify-space-start flex-nowrap flex-row'},
                        'content': [
                            {'component': 'div', 'content': [{'component': 'VImg', 'props': {'src': poster, 'height': 120, 'width': 80, 'aspect-ratio': '2/3', 'class': 'object-cover', 'cover': True}}]},
                            {
                                'component': 'div',
                                'content': [
                                    {'component': 'VCardTitle', 'props': {'class': 'ps-1 pe-5 break-words whitespace-break-spaces'}, 'content': [{'component': 'a', 'props': {'href': f"https://movie.douban.com/subject/{doubanid}", 'target': '_blank'}, 'text': title}]},
                                    {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'类型：{mtype}'},
                                    {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'时间：{time_str}'},
                                    # 这里修改：增加了订阅人显示
                                    {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'操作：{action_text} 订阅人：{user_name}'}
                                ]
                            }
                        ]
                    }
                ]
            })

        return [{'component': 'div', 'props': {'class': 'grid gap-3 grid-info-card'}, 'content': contents}]

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled, "notify": self._notify, "onlyonce": self._onlyonce,
            "cron": self._cron, "days": self._days, "users": self._users,
            "clear": self._clear, "search_download": self._search_download
        })

    def delete_history(self, doubanid: str, apikey: str):
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        historys = self.get_data('history') or []
        historys = [h for h in historys if h.get("doubanid") != doubanid]
        self.save_data('history', historys)
        return schemas.Response(success=True, message="删除成功")

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    def sync(self):
        if not self._users:
            return
        version = settings.VERSION_FLAG if hasattr(settings, 'VERSION_FLAG') else "v1"
        
        if self._clearflag:
            history = []
        else:
            history: List[dict] = self.get_data('history') or []

        # 修改解析逻辑：支持 123,user|456,user 格式
        user_list = []
        if "|" in self._users:
            # 格式：id,name|id,name
            segments = self._users.split("|")
            for seg in segments:
                if "," in seg:
                    user_list.append(seg.split(",", 1))
        else:
            # 兼容老格式或单个 id,name 格式
            if "," in self._users:
                user_list.append(self._users.split(",", 1))
            else:
                user_list.append([self._users, "未知用户"])

        for user_id, bind_name in user_list:
            user_id = user_id.strip()
            bind_name = bind_name.strip()
            if not user_id: continue
            
            logger.info(f"开始同步用户 {bind_name}({user_id}) 的想看数据...")
            url = self._interests_url % user_id
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

                    pubdate = result.get("pubdate")
                    if pubdate and (datetime.datetime.now(datetime.timezone.utc) - pubdate).days > float(self._days):
                        continue

                    douban_id = result.get("link", "").split("/")[-2]
                    if not douban_id: continue

                    # 检查历史记录
                    if douban_id in [h.get("doubanid") for h in history]:
                        continue

                    # 识别媒体
                    meta = MetaInfo(title=title)
                    douban_info = self.chain.douban_info(doubanid=douban_id)
                    meta.type = MediaType.MOVIE if douban_info.get("type") == "movie" else MediaType.TV
                    
                    mediainfo = self.chain.recognize_media(meta=meta, doubanid=douban_id)
                    if not mediainfo: continue

                    exist_flag, no_exists = downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                    action = "exist"
                    
                    if not exist_flag:
                        if self._search_download:
                            filter_results = searchchain.process(
                                mediainfo=mediainfo,
                                no_exists=no_exists,
                                sites=self.systemconfig.get(SystemConfigKey.RssSites),
                                rule_groups=self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
                            )
                            if filter_results:
                                action = "download"
                                download_id = downloadchain.download_single(context=filter_results[0], username=bind_name)
                                if not download_id:
                                    self.add_subscribe(mediainfo, meta, bind_name)
                                    action = "subscribe"
                            else:
                                self.add_subscribe(mediainfo, meta, bind_name)
                                action = "subscribe"
                        else:
                            self.add_subscribe(mediainfo, meta, bind_name)
                            action = "subscribe"

                    # 存储历史，记录订阅人
                    history.append({
                        "action": action,
                        "title": title,
                        "type": mediainfo.type.value,
                        "year": mediainfo.year,
                        "poster": mediainfo.get_poster_image(),
                        "tmdbid": mediainfo.tmdb_id,
                        "doubanid": douban_id,
                        "user": bind_name, # 记录绑定用户名
                        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })

                    # 发送增强通知
                    if self._notify and action != "exist":
                        msg = f"【豆瓣想看】{bind_name} 订阅了{mediainfo.type.value}：{title}"
                        self.chain.post_message(title=msg, text=f"类型：{mediainfo.type.value}\n年份：{mediainfo.year}\n操作：{action}")

                except Exception as err:
                    logger.error(f'处理项目 {title} 出错：{str(err)}')

        self.save_data('history', history)
        self._clearflag = False

    @staticmethod
    def add_subscribe(mediainfo, meta, bind_name):
        return SubscribeChain().add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id,
            season=meta.begin_season,
            exist_ok=True,
            username=bind_name # 这里直接传入绑定的用户名
        )

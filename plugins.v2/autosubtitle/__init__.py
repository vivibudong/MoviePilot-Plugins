import os
import time
import requests
import datetime
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.schemas.types import EventType
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app import schemas

lock = Lock()

class AutoSubtitle(_PluginBase):
    # 插件名称
    plugin_name = "自动字幕下载"
    # 插件描述
    plugin_desc = "自动监控视频库目录，调用伪射手字幕网assrt.net的api自动下载重命名字幕。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/autosubtitle.png"
    # 插件版本
    plugin_version = "0.1"
    # 插件作者
    plugin_author = "Vivi"
    # 作者主页
    author_url = "https://github.com/vivibudong"
    # 插件配置项ID前缀
    plugin_config_prefix = "autosubtitle"
    # 加载顺序
    plugin_order = 10
    # 可使用的用户级别
    auth_level = 2

    # 私有变量
    _scheduler: Optional[BackgroundScheduler] = None
    _enabled: bool = False
    _onlyonce: bool = False
    _clear_history: bool = False
    _monitor_dirs: str = ""
    _api_token: str = ""
    _check_exist: bool = True  # 检测非-mp字幕是否存在
    _cron: str = ""
    _notify: bool = False
    
    # 视频和字幕后缀定义
    _video_exts = ['.mkv', '.mp4', '.avi', '.ts', '.wmv', '.iso', '.mov']
    _sub_exts = ['.srt', '.ass', '.ssa', '.vtt']

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._clear_history = config.get("clear_history")
            self._monitor_dirs = config.get("monitor_dirs")
            self._api_token = config.get("api_token")
            # 这里逻辑稍微反转一下：UI上问"是否检测已存在"，代码里如果为False，则只认-mp，如果为True，则只要有字幕就不下？
            # 根据用户需求：如果检测到视频字幕已经存在（但不是assrt.net的字幕）是否还要继续下载。
            # 我们定义配置项 ignore_other_subs: True(默认，无视其他字幕只看-mp), False(如果有其他字幕则跳过)
            self._ignore_other_subs = config.get("ignore_other_subs", True) 
            self._cron = config.get("cron")

        # 处理一次性运行
        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info(f"自动字幕下载服务启动，立即运行一次")
                self._scheduler.add_job(func=self.scan_task, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=settings.TZ_INFO) + datetime.timedelta(seconds=3)
                                        )
                if self._scheduler.get_jobs():
                    self._scheduler.start()
            
            # 关闭一次性开关并保存
            if self._onlyonce:
                self._onlyonce = False
                self.__update_config()

        # 清理历史记录
        if self._clear_history:
            self.save_data('history', [])
            self._clear_history = False
            self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/autosubtitle",
            "event": EventType.PluginAction,
            "desc": "立即运行自动字幕下载",
            "category": "插件",
            "data": {
                "action": "autosubtitle_run"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
             {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除字幕下载记录"
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled:
            if self._cron:
                return [{
                    "id": "AutoSubtitle",
                    "name": "自动字幕下载监控",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.scan_task,
                    "kwargs": {}
                }]
            else:
                 # 默认每4小时运行一次
                return [{
                    "id": "AutoSubtitle",
                    "name": "自动字幕下载监控",
                    "trigger": "interval",
                    "func": self.scan_task,
                    "kwargs": {"hours": 4}
                }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'clear_history', 'label': '删除历史记录'}}
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
                                    {'component': 'VTextField', 'props': {'model': 'api_token', 'label': 'Assrt.net Token', 'placeholder': '请输入伪射手网API Token'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VCronField', 'props': {'model': 'cron', 'label': '执行周期', 'placeholder': '5位cron表达式，留空默认每4小时'}}
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
                                    {'component': 'VTextarea', 'props': {'model': 'monitor_dirs', 'label': '监控目录', 'rows': 3, 'placeholder': '请输入需要监控的绝对路径，一行一个'}}
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
                                    {'component': 'VSwitch', 'props': {'model': 'ignore_other_subs', 'label': '即使存在其他字幕也下载', 'hint': '开启后，除非已存在 -mp 后缀的字幕，否则都会尝试下载'}}
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "clear_history": False,
            "monitor_dirs": "/Media/Movies",
            "api_token": "",
            "ignore_other_subs": True,
            "cron": ""
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data('history')
        if not historys:
            return [{'component': 'div', 'text': '暂无下载记录', 'props': {'class': 'text-center'}}]

        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)
        contents = []
        
        for h in historys:
            contents.append({
                'component': 'VCard',
                'content': [
                    {
                        "component": "VDialogCloseBtn",
                        "props": {'innerClass': 'absolute top-0 right-0'},
                        'events': {
                            'click': {
                                'api': 'plugin/autosubtitle/delete_history',
                                'method': 'get',
                                'params': {'filename': h.get('filename')}
                            }
                        }
                    },
                    {
                        'component': 'VCardTitle',
                        'text': h.get('filename'),
                        'props': {'class': 'text-subtitle-1'}
                    },
                    {
                        'component': 'VCardText',
                        'text': f"状态: {h.get('status')} | 来源: {h.get('source')} | 时间: {h.get('time')}"
                    }
                ]
            })

        return [{
            'component': 'div',
            'props': {'class': 'grid gap-3 grid-info-card'},
            'content': contents
        }]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "clear_history": self._clear_history,
            "monitor_dirs": self._monitor_dirs,
            "api_token": self._api_token,
            "ignore_other_subs": self._ignore_other_subs,
            "cron": self._cron
        })
    
    def delete_history(self, filename: str):
        historys = self.get_data('history')
        if historys:
            historys = [h for h in historys if h.get("filename") != filename]
            self.save_data('history', historys)
        return schemas.Response(success=True, message="删除成功")

    def scan_task(self):
        """
        主扫描任务
        """
        if not self._monitor_dirs or not self._api_token:
            logger.error("【自动字幕下载】未配置监控目录或API Token，任务跳过")
            return

        logger.info("【自动字幕下载】开始扫描视频目录...")
        dirs = self._monitor_dirs.split('\n')
        
        # 读取历史记录
        history = self.get_data('history') or []
        
        count = 0
        for mon_dir in dirs:
            mon_dir = mon_dir.strip()
            if not mon_dir or not os.path.exists(mon_dir):
                continue
            
            for root, _, files in os.walk(mon_dir):
                for file in files:
                    file_path = Path(root) / file
                    # 检查是否为视频
                    if file_path.suffix.lower() not in self._video_exts:
                        continue
                    
                    # 目标字幕文件名基础 (video-mp)
                    target_mp_base = file_path.parent / f"{file_path.stem}-mp"
                    
                    # 1. 检查是否已经存在 -mp 后缀的字幕 (最高优先级，存在则跳过)
                    mp_exists = False
                    for ext in self._sub_exts:
                        if target_mp_base.with_suffix(ext).exists():
                            mp_exists = True
                            break
                    
                    if mp_exists:
                        continue

                    # 2. 检查是否存在任意字幕 (非 -mp)
                    if not self._ignore_other_subs:
                        # 如果配置为"不忽略其他字幕"，则检测到任意字幕都跳过
                        any_sub_exists = False
                        for ext in self._sub_exts:
                            if (file_path.parent / f"{file_path.stem}{ext}").exists():
                                any_sub_exists = True
                                break
                        if any_sub_exists:
                            logger.info(f"【自动字幕下载】视频 {file} 已存在其他字幕且配置为跳过，不下载")
                            continue

                    # 执行下载逻辑
                    logger.info(f"【自动字幕下载】正在为 {file} 查找字幕...")
                    success, msg = self.download_subtitle(file_path)
                    
                    # 记录日志
                    history.append({
                        "filename": file,
                        "status": "成功" if success else "失败",
                        "source": "Assrt.net",
                        "msg": msg,
                        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    self.save_data('history', history)
                    
                    if success:
                        count += 1
        
        logger.info(f"【自动字幕下载】扫描结束，共下载字幕 {count} 个")

    def download_subtitle(self, video_path: Path) -> Tuple[bool, str]:
        """
        调用Assrt API下载字幕
        API限制：5次/分钟。策略：每次请求后Sleep 13秒。
        """
        keyword = video_path.stem
        api_url = "https://api.assrt.net/v1/sub/search"
        
        try:
            # 1. 搜索字幕
            params = {
                "token": self._api_token,
                "q": keyword,
                "cnt": 1, # 只取一个，通常是最优的
                "pos": 1
            }
            
            resp = requests.get(api_url, params=params, timeout=20)
            
            # 强制频控休眠，防止超限
            time.sleep(13)
            
            if resp.status_code != 200:
                return False, f"API请求失败: {resp.status_code}"
            
            data = resp.json()
            if data.get("status") != 0 or not data.get("sub", {}).get("subs"):
                return False, "未搜索到字幕"
            
            # 获取第一个结果
            sub_info = data["sub"]["subs"][0]
            sub_id = sub_info.get("id")
            sub_filename = sub_info.get("native_name", "sub.srt")
            
            # 确定扩展名
            sub_ext = os.path.splitext(sub_filename)[-1].lower()
            if not sub_ext:
                sub_ext = ".srt" # 默认srt
            
            # 如果是压缩包，暂不支持解压，只记录（V0.1简化逻辑）
            if sub_ext in ['.zip', '.rar', '.7z']:
                return False, f"目标字幕为压缩包({sub_ext})，V0.1暂不支持自动解压"
            
            # 2. 获取详情/下载链接 (伪射手搜索结果通常不包含直接下载链，需要拼接或再次请求，
            # 但API文档显示 search result 包含 url_list 或类似字段，这里使用伪射手通用下载规则)
            # 伪射手下载通常是 api.assrt.net/v1/sub/detail?token=xxx&id=xxx 或直接拼接
            # 观察API返回，通常 file 字段是个列表
            
            # 这里尝试直接通过 search 结果中的 url 下载，如果没有则尝试 detail 接口
            # 为简化，使用详情接口获取确实的下载地址
            detail_url = "https://api.assrt.net/v1/sub/detail"
            d_params = {
                "token": self._api_token,
                "id": sub_id
            }
            d_resp = requests.get(detail_url, params=d_params, timeout=20)
            time.sleep(13) # 再次频控
            
            if d_resp.status_code != 200:
                return False, "详情API请求失败"
            
            d_data = d_resp.json()
            if not d_data.get("sub", {}).get("subs"):
                return False, "无法获取字幕详情"
                
            real_sub_info = d_data["sub"]["subs"][0]
            download_url = real_sub_info.get("url")
            
            if not download_url:
                return False, "未找到下载链接"
            
            # 3. 下载文件
            sub_res = requests.get(download_url, timeout=30)
            if sub_res.status_code != 200:
                return False, "下载失败"
            
            # 4. 保存文件并重命名
            # 格式: 视频名-mp.扩展名
            target_file = video_path.parent / f"{video_path.stem}-mp{sub_ext}"
            
            with open(target_file, "wb") as f:
                f.write(sub_res.content)
            
            logger.info(f"【自动字幕下载】成功下载: {target_file.name}")
            return True, "下载成功"
            
        except Exception as e:
            logger.error(f"【自动字幕下载】处理出错: {str(e)}")
            return False, str(e)

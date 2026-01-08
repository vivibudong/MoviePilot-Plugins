import datetime
import os
import time
import zipfile
import requests
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple, Union

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.schemas.types import EventType, NotificationType
from app.core.config import settings
from app.core.event import Event
from app.core.event import eventmanager
from app.log import logger
from app.plugins import _PluginBase

lock = Lock()

class AutoSubtitle(_PluginBase):
    # 插件名称
    plugin_name = "自动字幕下载"
    # 插件描述
    plugin_desc = "自动监控视频库目录，调用伪射手字幕网assrt.net的api自动下载重命名字幕。"
    # 插件图标
    plugin_icon = "https://www.assrt.net/favicon.ico"
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
    _assrt_api_url = "https://api.assrt.net/v1/sub/search"
    
    # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _notify: bool = False
    _monitor_dirs: str = ""
    _api_token: str = ""
    _download_if_exists: bool = False
    _suffix: str = "-mp" # 强制后缀

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        # 配置加载
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._monitor_dirs = config.get("monitor_dirs")
            self._api_token = config.get("api_token")
            self._download_if_exists = config.get("download_if_exists")
            self._onlyonce = config.get("onlyonce")

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info(f"自动字幕下载服务启动，立即运行一次")
                self._scheduler.add_job(func=self.scan_and_download, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if self._onlyonce:
                # 关闭一次性开关并保存
                self._onlyonce = False
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
                "summary": "删除字幕下载历史记录"
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [
                {
                    "id": "AutoSubtitle",
                    "name": "自动字幕下载服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.scan_and_download,
                    "kwargs": {}
                }
            ]
        elif self._enabled:
            # 默认每小时一次
            return [
                {
                    "id": "AutoSubtitle",
                    "name": "自动字幕下载服务",
                    "trigger": "interval",
                    "func": self.scan_and_download,
                    "kwargs": {"hours": 1}
                }
            ]
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
                                    {'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}
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
                                    {'component': 'VCronField', 'props': {'model': 'cron', 'label': '执行周期', 'placeholder': '5位cron表达式，留空默认每小时'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VTextField', 'props': {'model': 'api_token', 'label': 'Assrt API Token', 'placeholder': '前往 assrt.net 申请'}}
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
                                    {'component': 'VTextarea', 'props': {'model': 'monitor_dirs', 'label': '监控目录列表', 'placeholder': '绝对路径，一行一个', 'rows': 5}}
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
                                    {'component': 'VSwitch', 'props': {'model': 'download_if_exists', 'label': '已存在字幕仍下载', 'hint': '如果开启，即使发现同名视频目录下有字幕文件（不含-mp后缀的），仍会尝试下载'}}
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
            "cron": "",
            "monitor_dirs": "",
            "api_token": "",
            "download_if_exists": False
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data('history')
        if not historys:
            return [{'component': 'div', 'text': '暂无下载记录', 'props': {'class': 'text-center'}}]
        
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)
        contents = []
        for history in historys:
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
                                'params': {'path': history.get("path")}
                            }
                        },
                    },
                    {
                        'component': 'VCardTitle',
                        'text': history.get("filename")
                    },
                    {
                        'component': 'VCardText',
                        'text': f'保存路径：{history.get("saved_path")}'
                    },
                    {
                        'component': 'VCardText',
                        'text': f'下载时间：{history.get("time")}'
                    }
                ]
            })

        return [{'component': 'div', 'props': {'class': 'grid gap-3 grid-info-card'}, 'content': contents}]

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "monitor_dirs": self._monitor_dirs,
            "api_token": self._api_token,
            "download_if_exists": self._download_if_exists
        })

    def delete_history(self, path: str):
        historys = self.get_data('history') or []
        new_historys = [h for h in historys if h.get("path") != path]
        self.save_data('history', new_historys)
        return schemas.Response(success=True, message="删除成功")

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止插件服务失败：{e}")

    def scan_and_download(self):
        """
        核心扫描与下载逻辑
        """
        if not self._monitor_dirs or not self._api_token:
            logger.error("自动字幕下载：未配置监控目录或API Token")
            return

        logger.info("自动字幕下载：开始扫描目录...")
        dirs = [d.strip() for d in self._monitor_dirs.split('\n') if d.strip()]
        video_exts = ['.mkv', '.mp4', '.avi', '.mov', '.iso', '.wmv']
        history: List[dict] = self.get_data('history') or []
        processed_paths = [h.get("path") for h in history]

        count = 0
        for mon_dir in dirs:
            if not os.path.exists(mon_dir):
                logger.warn(f"目录不存在：{mon_dir}")
                continue
            
            for root, _, files in os.walk(mon_dir):
                for file in files:
                    file_path = Path(root) / file
                    if file_path.suffix.lower() not in video_exts:
                        continue
                    
                    # 1. 检查历史记录
                    if str(file_path) in processed_paths:
                        continue

                    # 2. 检查本地是否已存在字幕
                    # 检查带有 -mp 后缀的必然跳过（说明是自己下的）
                    if (file_path.parent / f"{file_path.stem}{self._suffix}.srt").exists() or \
                       (file_path.parent / f"{file_path.stem}{self._suffix}.ass").exists():
                        continue
                    
                    # 检查不带 -mp 后缀的通用字幕
                    if not self._download_if_exists:
                        # 简单检查 .srt, .ass, .zh.srt 等常见组合
                        has_sub = False
                        for ext in ['.srt', '.ass', '.ssa']:
                            if (file_path.parent / f"{file_path.stem}{ext}").exists() or \
                               (file_path.parent / f"{file_path.stem}.zh{ext}").exists() or \
                               (file_path.parent / f"{file_path.stem}.chs{ext}").exists():
                                has_sub = True
                                break
                        if has_sub:
                            logger.info(f"已存在字幕，跳过：{file}")
                            continue

                    # 3. 执行搜索下载
                    success = self.download_subtitle(file_path)
                    if success:
                        count += 1
                        history.append({
                            "path": str(file_path),
                            "filename": file,
                            "saved_path": str(success),
                            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
                        # 避免频繁请求API
                        time.sleep(3) 

        self.save_data('history', history)
        logger.info(f"自动字幕下载：扫描完成，本次共下载 {count} 个字幕")

    def download_subtitle(self, video_path: Path) -> Union[Path, bool]:
        """
        根据视频文件搜索并下载字幕
        :param video_path: 视频文件路径
        :return: 下载成功返回字幕路径，失败返回False
        """
        filename = video_path.stem
        # 去除常见无用后缀优化搜索关键词 (仅作简单处理，assrt本身支持模糊)
        search_kw = filename
        
        params = {
            "token": self._api_token,
            "q": search_kw,
            "cnt": 3, # 获取前3个结果
            "pos": 0
        }
        
        try:
            logger.info(f"正在搜索字幕：{search_kw}")
            resp = requests.get(self._assrt_api_url, params=params, timeout=10)
            if resp.status_code != 200:
                logger.error(f"API请求失败：{resp.status_code}")
                return False
            
            data = resp.json()
            if data.get("status") != 0 or not data.get("sub", {}).get("subs"):
                logger.info(f"未找到字幕：{search_kw}")
                return False

            subs_list = data["sub"]["subs"]
            # 简单策略：取第一个结果（assrt通常按相关性排序）
            # 也可以在这里加入逻辑：优先选 native_name 包含 "简体" / "双语" 的结果
            best_sub = subs_list[0]
            url_list = best_sub.get("url_list", [])
            if not url_list:
                return False
            
            download_url = url_list[0].get("url")
            ext = url_list[0].get("ext", "")
            
            return self.process_download(download_url, ext, video_path)

        except Exception as e:
            logger.error(f"下载字幕出错 {filename}：{e}")
            return False

    def process_download(self, url: str, ext: str, video_path: Path) -> Union[Path, bool]:
        """
        下载并处理文件（支持压缩包解压）
        """
        try:
            logger.info(f"开始下载：{url}")
            resp = requests.get(url, headers={"User-Agent": settings.USER_AGENT}, timeout=20)
            content = resp.content
            
            target_stem = f"{video_path.stem}{self._suffix}"
            
            # 如果是压缩包
            if ext in ['zip', 'rar'] or url.endswith('.zip') or url.endswith('.rar'):
                return self.extract_best_sub(content, video_path.parent, target_stem)
            else:
                # 直接是字幕文件
                # 修正后缀，有时候API不返回ext
                if not ext:
                    if b'Start' in content[:100] or b'00:00' in content[:100]:
                        ext = 'srt'
                    elif b'[Script Info]' in content[:100]:
                        ext = 'ass'
                    else:
                        ext = 'srt' # 默认
                
                # 统一为小写不带点
                ext = ext.replace('.', '').lower()
                save_path = video_path.parent / f"{target_stem}.{ext}"
                with open(save_path, 'wb') as f:
                    f.write(content)
                
                logger.info(f"字幕已保存：{save_path}")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.Manual,
                        title="自动下载字幕成功",
                        text=f"视频：{video_path.name}\n字幕：{save_path.name}"
                    )
                return save_path

        except Exception as e:
            logger.error(f"处理字幕文件失败：{e}")
            return False

    def extract_best_sub(self, content: bytes, dir_path: Path, target_stem: str) -> Union[Path, bool]:
        """
        从压缩包内容中提取最大的 ass/srt 文件
        """
        try:
            with zipfile.ZipFile(BytesIO(content)) as z:
                file_list = z.namelist()
                # 过滤出字幕文件
                sub_files = [f for f in file_list if f.lower().endswith(('.srt', '.ass', '.ssa'))]
                if not sub_files:
                    logger.warn("压缩包内未找到srt/ass字幕文件")
                    return False
                
                # 策略：找最大的文件，通常是完整的
                best_file = max(sub_files, key=lambda x: z.getinfo(x).file_size)
                ext = Path(best_file).suffix.lower() # 包含点 .srt
                
                target_path = dir_path / f"{target_stem}{ext}"
                
                with z.open(best_file) as source, open(target_path, "wb") as target:
                    target.write(source.read())
                
                logger.info(f"已解压并保存字幕：{target_path}")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.Manual,
                        title="自动下载字幕成功",
                        text=f"来源：压缩包解压\n字幕：{target_path.name}"
                    )
                return target_path
        except zipfile.BadZipFile:
            logger.error("无效的ZIP文件")
        except Exception as e:
            logger.error(f"解压字幕失败：{e}")
        return False

    @eventmanager.register(EventType.PluginAction)
    def remote_run(self, event: Event):
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "autosubtitle_run":
                return
            logger.info("收到命令，开始运行自动字幕下载...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始运行自动字幕下载...",
                              userid=event.event_data.get("user"))
            self.scan_and_download()

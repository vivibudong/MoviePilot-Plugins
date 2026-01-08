import datetime
import time
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple
import requests
import zipfile
import io
import re

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

lock = Lock()


class AutoSubtitle(_PluginBase):
    # 插件名称
    plugin_name = "自动字幕下载"
    # 插件描述
    plugin_desc = "自动监控视频库目录，调用伪射手字幕网assrt.net的api自动下载重命名字幕。"
    # 插件图标
    plugin_icon = "subtitle.png"
    # 插件版本
    plugin_version = "0.1"
    # 插件作者
    plugin_author = "Vivi"
    # 作者主页
    author_url = "https://github.com/vivibudong"
    # 插件配置项ID前缀
    plugin_config_prefix = "autosubtitle_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 2

    # 私有变量
    _scheduler: Optional[BackgroundScheduler] = None
    _enabled: bool = False
    _onlyonce: bool = False
    _clear_log: bool = False
    _monitor_dirs: str = ""
    _api_token: str = ""
    _force_download: bool = False
    _cron: str = "0 */6 * * *"  # 默认每6小时运行一次
    _use_proxy: bool = False
    _proxy_url: str = ""
    
    # API相关
    _api_base_url: str = "https://api.assrt.net/v1"
    _last_request_time: float = 0
    _request_interval: float = 3.5  # 20次/分钟 = 3秒/次，留0.5秒余量
    _running: bool = False  # 运行状态标志
    
    # 支持的视频格式
    _video_formats = ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.rmvb', '.m4v', '.ts']
    # 支持的字幕格式
    _subtitle_formats = ['.srt', '.ass', '.ssa']

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        # 先设置运行标志为False，停止任何正在运行的任务
        self._running = False
        
        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._clear_log = config.get("clear_log", False)
            self._monitor_dirs = config.get("monitor_dirs", "")
            self._api_token = config.get("api_token", "")
            self._force_download = config.get("force_download", False)
            self._cron = config.get("cron", "0 */6 * * *")
            self._use_proxy = config.get("use_proxy", False)
            self._proxy_url = config.get("proxy_url", "")

        # 处理清理日志
        if self._clear_log:
            self.save_data('download_log', [])
            logger.info("已清除字幕下载历史记录")
            self._clear_log = False
            self.__update_config()

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("自动字幕下载服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.scan_and_download,
                    trigger='date',
                    run_date=datetime.datetime.now() + datetime.timedelta(seconds=3)
                )
                
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()
                
                # 关闭一次性开关
                self._onlyonce = False
                self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """定义远程控制命令"""
        return [{
            "cmd": "/autosubtitle",
            "event": EventType.PluginAction,
            "desc": "扫描并下载字幕",
            "category": "字幕",
            "data": {
                "action": "autosubtitle"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """获取插件API"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件公共服务"""
        if self._enabled and self._cron:
            return [{
                "id": "AutoSubtitle",
                "name": "自动字幕下载服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.scan_and_download,
                "kwargs": {}
            }]
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
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'enabled',
                                        'label': '启用插件',
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'onlyonce',
                                        'label': '立即运行一次',
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'clear_log',
                                        'label': '删除历史记录',
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'api_token',
                                        'label': 'Assrt API Token',
                                        'placeholder': '请输入assrt.net的API Token'
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'cron',
                                        'label': '执行周期',
                                        'placeholder': '5位cron表达式，如: 0 */6 * * *'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'force_download',
                                        'label': '强制下载（即使已有字幕）',
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {
                                        'model': 'use_proxy',
                                        'label': '使用代理',
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {
                                        'model': 'proxy_url',
                                        'label': '代理地址',
                                        'placeholder': 'http://127.0.0.1:7890'
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'content': [{
                                'component': 'VTextarea',
                                'props': {
                                    'model': 'monitor_dirs',
                                    'label': '监控目录列表',
                                    'placeholder': '每行一个目录路径，例如：\n/media/movies\n/media/tv',
                                    'rows': 5
                                }
                            }]
                        }]
                    },
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12, 'md': 6},
                            'content': [{
                                'component': 'VSwitch',
                                'props': {
                                    'model': 'force_download',
                                    'label': '强制下载（即使已有字幕）',
                                }
                            }]
                        }]
                    },
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{
                                'component': 'VAlert',
                                'props': {
                                    'type': 'info',
                                    'variant': 'tonal',
                                    'text': '说明：\n'
                                            '1. API Token需要在assrt.net注册后获取\n'
                                            '2. 字幕会自动命名为"原文件名-mp.srt"格式\n'
                                            '3. 只有后缀不含"-mp"的视频才会重新下载字幕\n'
                                            '4. API限制5次/分钟，插件会自动控制请求频率\n'
                                            '5. 推荐执行周期设置为6小时或更长'
                                }
                            }]
                        }]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "clear_log": False,
            "monitor_dirs": "",
            "api_token": "",
            "force_download": False,
            "cron": "0 */6 * * *",
            "use_proxy": False,
            "proxy_url": ""
        }

    def get_page(self) -> List[dict]:
        """拼装插件详情页面"""
        download_log = self.get_data('download_log') or []
        
        if not download_log:
            return [{
                'component': 'div',
                'text': '暂无下载记录',
                'props': {'class': 'text-center'}
            }]
        
        # 按时间降序排序
        download_log = sorted(download_log, key=lambda x: x.get('time', ''), reverse=True)
        
        contents = []
        for log in download_log:
            video_path = log.get("video_path", "")
            subtitle_path = log.get("subtitle_path", "")
            status = log.get("status", "")
            time_str = log.get("time", "")
            message = log.get("message", "")
            
            # 状态颜色
            status_color = "success" if status == "成功" else "error" if status == "失败" else "warning"
            
            contents.append({
                'component': 'VCard',
                'props': {'class': 'mb-2'},
                'content': [{
                    'component': 'VCardText',
                    'content': [
                        {
                            'component': 'div',
                            'props': {'class': 'd-flex align-center mb-2'},
                            'content': [
                                {
                                    'component': 'VChip',
                                    'props': {
                                        'color': status_color,
                                        'size': 'small',
                                        'class': 'mr-2'
                                    },
                                    'text': status
                                },
                                {
                                    'component': 'span',
                                    'props': {'class': 'text-caption'},
                                    'text': time_str
                                }
                            ]
                        },
                        {
                            'component': 'div',
                            'props': {'class': 'text-body-2 mb-1'},
                            'text': f'视频: {Path(video_path).name}'
                        },
                        {
                            'component': 'div',
                            'props': {'class': 'text-body-2 mb-1'},
                            'text': f'字幕: {Path(subtitle_path).name if subtitle_path else "无"}'
                        },
                        {
                            'component': 'div',
                            'props': {'class': 'text-caption text-grey'},
                            'text': message
                        }
                    ]
                }]
            })
        
        return [{
            'component': 'div',
            'props': {'class': 'grid gap-3'},
            'content': contents
        }]

    def __update_config(self):
        """更新配置"""
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "clear_log": self._clear_log,
            "monitor_dirs": self._monitor_dirs,
            "api_token": self._api_token,
            "force_download": self._force_download,
            "cron": self._cron,
            "use_proxy": self._use_proxy,
            "proxy_url": self._proxy_url
        })

    def stop_service(self):
        """退出插件"""
        try:
            # 设置运行标志为False
            self._running = False
            
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown(wait=False)
                self._scheduler = None
                logger.info("字幕下载服务已停止")
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")

    def _rate_limit(self):
        """API请求频率限制：5次/分钟"""
        current_time = time.time()
        time_since_last_request = current_time - self._last_request_time
        
        if time_since_last_request < self._request_interval:
            sleep_time = self._request_interval - time_since_last_request
            logger.info(f"API频率限制，等待 {sleep_time:.1f} 秒...")
            time.sleep(sleep_time)
        
        self._last_request_time = time.time()

    def _get_session(self) -> requests.Session:
        """获取配置好的requests session"""
        session = requests.Session()
        
        # 配置代理
        if self._use_proxy and self._proxy_url:
            session.proxies = {
                'http': self._proxy_url,
                'https': self._proxy_url
            }
            logger.info(f"使用代理：{self._proxy_url}")
        
        # 配置重试
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        return session

    def _search_subtitle(self, video_name: str) -> Optional[Dict]:
        """搜索字幕"""
        if not self._api_token:
            logger.error("未配置API Token")
            return None
        
        self._rate_limit()
        
        # 清理文件名，提取关键信息
        clean_name = self._clean_video_name(video_name)
        
        try:
            headers = {
                "User-Agent": "MoviePilot AutoSubtitle Plugin"
            }
            
            # Assrt API的正确搜索endpoint
            params = {
                "token": self._api_token,
                "q": clean_name,
                "is_file": 0  # 按关键词搜索
            }
            
            session = self._get_session()
            response = session.get(
                f"{self._api_base_url}/sub/search",
                params=params,
                headers=headers,
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                # 检查返回的数据结构
                if isinstance(data, dict) and data.get("sub") and data["sub"].get("subs"):
                    subs = data["sub"]["subs"]
                elif isinstance(data, list) and len(data) > 0:
                    subs = data
                else:
                    logger.warning(f"搜索结果为空：{clean_name}")
                    return None
                
                # 返回评分最高的字幕
                if subs:
                    sorted_subs = sorted(subs, 
                                       key=lambda x: float(x.get("vote_score", 0) or 0), 
                                       reverse=True)
                    logger.info(f"找到 {len(subs)} 个字幕，选择评分最高的")
                    return sorted_subs[0]
            else:
                logger.warning(f"搜索字幕失败，状态码：{response.status_code}，响应：{response.text[:200]}")
                
        except Exception as e:
            logger.error(f"搜索字幕异常：{str(e)}")
        
        return None

    def _download_subtitle(self, subtitle_info: Dict, video_path: Path) -> Optional[Path]:
        """下载字幕"""
        if not subtitle_info:
            return None
        
        self._rate_limit()
        
        try:
            sub_id = subtitle_info.get("id")
            if not sub_id:
                logger.error("字幕信息中缺少ID")
                return None
            
            # 第一步：获取字幕详细信息
            params = {
                "token": self._api_token,
                "id": sub_id
            }
            
            headers = {
                "User-Agent": "MoviePilot AutoSubtitle Plugin"
            }
            
            session = self._get_session()
            
            # 调用 sub/detail 获取下载链接
            response = session.get(
                f"{self._api_base_url}/sub/detail",
                params=params,
                headers=headers,
                timeout=30
            )
            
            if response.status_code != 200:
                logger.error(f"获取字幕详情失败，状态码：{response.status_code}，响应：{response.text[:200]}")
                return None
            
            detail_data = response.json()
            
            # 检查返回状态
            if detail_data.get("status") != 0:
                logger.error(f"API返回错误：{detail_data.get('errmsg', '未知错误')}")
                return None
            
            # 提取下载URL
            subs = detail_data.get("sub", {}).get("subs", [])
            if not subs:
                logger.error("字幕详情中没有找到字幕数据")
                return None
            
            download_url = subs[0].get("url")
            if not download_url:
                logger.error("字幕详情中没有找到下载链接")
                return None
            
            logger.info(f"获取到字幕下载链接：{download_url[:100]}...")
            
            # 第二步：下载字幕文件（增加重试和超时处理）
            self._rate_limit()
            
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    # 为下载请求创建新的session
                    download_session = self._get_session()
                    # 设置更长的超时时间
                    sub_response = download_session.get(
                        download_url, 
                        headers=headers, 
                        timeout=120,
                        verify=True  # 验证SSL证书
                    )
                    
                    if sub_response.status_code != 200:
                        logger.error(f"下载字幕文件失败，状态码：{sub_response.status_code}")
                        if attempt < max_retries - 1:
                            logger.info(f"等待5秒后重试... ({attempt + 1}/{max_retries})")
                            time.sleep(5)
                            continue
                        return None
                    
                    # 下载成功，处理内容
                    filename = subs[0].get("filename", "")
                    logger.info(f"字幕文件下载成功，文件名：{filename}")
                    
                    # 检查是否是压缩文件
                    if filename.endswith('.rar') or filename.endswith('.zip') or filename.endswith('.7z'):
                        return self._extract_subtitle_from_archive(sub_response.content, video_path, filename)
                    else:
                        # 直接保存字幕文件
                        return self._save_subtitle(sub_response.content, video_path)
                    
                except requests.exceptions.RequestException as e:
                    logger.error(f"下载字幕时网络错误 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                    if attempt < max_retries - 1:
                        logger.info("等待10秒后重试...")
                        time.sleep(10)
                    else:
                        logger.error("达到最大重试次数，下载失败")
                        return None
                
        except Exception as e:
            logger.error(f"下载字幕异常：{str(e)}")
            import traceback
            logger.error(f"详细错误：{traceback.format_exc()}")
        
        return None

    def _extract_subtitle_from_archive(self, archive_content: bytes, video_path: Path, filename: str) -> Optional[Path]:
        """从压缩包中提取字幕（支持zip和rar）"""
        try:
            # 尝试作为zip处理
            if filename.endswith('.zip'):
                return self._extract_subtitle_from_zip(archive_content, video_path)
            
            # 对于rar和其他格式，尝试先保存临时文件再解压
            import tempfile
            import subprocess
            
            with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tmp_file:
                tmp_file.write(archive_content)
                tmp_path = tmp_file.name
            
            try:
                # 创建临时解压目录
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_dir_path = Path(tmp_dir)
                    
                    # 尝试使用unrar（如果可用）
                    if filename.endswith('.rar'):
                        try:
                            subprocess.run(['unrar', 'x', '-y', tmp_path, str(tmp_dir_path)], 
                                         check=True, capture_output=True)
                        except (subprocess.CalledProcessError, FileNotFoundError):
                            logger.warning("无法解压rar文件（未安装unrar），尝试其他方式")
                            return None
                    
                    # 查找解压后的字幕文件
                    subtitle_files = []
                    for ext in self._subtitle_formats:
                        subtitle_files.extend(tmp_dir_path.rglob(f'*{ext}'))
                    
                    if not subtitle_files:
                        logger.error("压缩包中未找到字幕文件")
                        return None
                    
                    # 优先选择srt格式
                    srt_files = [f for f in subtitle_files if f.suffix.lower() == '.srt']
                    target_file = srt_files[0] if srt_files else subtitle_files[0]
                    
                    # 读取并保存字幕
                    with open(target_file, 'rb') as f:
                        subtitle_content = f.read()
                    
                    return self._save_subtitle(subtitle_content, video_path)
            finally:
                # 删除临时文件
                Path(tmp_path).unlink(missing_ok=True)
                
        except Exception as e:
            logger.error(f"解压字幕文件失败：{str(e)}")
        
        return None
    def _extract_subtitle_from_zip(self, zip_content: bytes, video_path: Path) -> Optional[Path]:
        """从zip压缩包中提取字幕"""
        try:
            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                # 查找srt或ass文件
                subtitle_files = [f for f in zf.namelist() 
                                if any(f.lower().endswith(ext) for ext in self._subtitle_formats)]
                
                if not subtitle_files:
                    logger.error("压缩包中未找到字幕文件")
                    return None
                
                # 优先选择srt格式
                srt_files = [f for f in subtitle_files if f.lower().endswith('.srt')]
                target_file = srt_files[0] if srt_files else subtitle_files[0]
                
                subtitle_content = zf.read(target_file)
                return self._save_subtitle(subtitle_content, video_path)
                
        except Exception as e:
            logger.error(f"解压zip字幕文件失败：{str(e)}")
        
        return None

    def _save_subtitle(self, content: bytes, video_path: Path) -> Optional[Path]:
        """保存字幕文件"""
        try:
            # 生成字幕文件名：原文件名-mp.srt
            subtitle_path = video_path.with_suffix('').with_suffix('.mp.srt')
            
            # 如果原文件名已经包含mp，先去掉
            if subtitle_path.stem.endswith('-mp'):
                subtitle_path = video_path.parent / f"{video_path.stem}-mp.srt"
            else:
                subtitle_path = video_path.parent / f"{video_path.stem}-mp.srt"
            
            with open(subtitle_path, 'wb') as f:
                f.write(content)
            
            logger.info(f"字幕已保存：{subtitle_path}")
            return subtitle_path
            
        except Exception as e:
            logger.error(f"保存字幕文件失败：{str(e)}")
        
        return None

    def _clean_video_name(self, video_name: str) -> str:
        """清理视频文件名，提取关键信息用于搜索"""
        # 移除文件扩展名
        name = Path(video_name).stem
        
        # 移除常见的质量标签和编码信息
        patterns = [
            r'[\.\-\s](1080p|720p|2160p|4k|BluRay|WEB-DL|HDTV|DVDRip|BRRip).*',
            r'[\.\-\s](x264|x265|H264|H265|HEVC|AAC|AC3|DTS).*',
            r'[\.\-\s](PROPER|REPACK|INTERNAL|LIMITED).*',
            r'\[.*?\]',  # 移除方括号内容
            r'\(.*?\)'   # 移除圆括号内容（保留年份除外）
        ]
        
        for pattern in patterns:
            name = re.sub(pattern, '', name, flags=re.IGNORECASE)
        
        # 清理多余的分隔符
        name = re.sub(r'[\.\-_]+', ' ', name)
        name = name.strip()
        
        return name

    def _check_existing_subtitle(self, video_path: Path) -> bool:
        """检查是否已存在字幕"""
        # 检查是否存在-mp后缀的字幕
        mp_subtitle = video_path.parent / f"{video_path.stem}-mp.srt"
        if mp_subtitle.exists():
            return True
        
        # 如果强制下载模式关闭，检查是否存在其他字幕
        if not self._force_download:
            for ext in self._subtitle_formats:
                subtitle_path = video_path.with_suffix(ext)
                if subtitle_path.exists():
                    return False  # 存在非mp字幕，需要重新下载
        
        return False

    def _scan_directory(self, directory: Path) -> List[Path]:
        """扫描目录，获取所有视频文件"""
        video_files = []
        
        if not directory.exists():
            logger.warning(f"目录不存在：{directory}")
            return video_files
        
        try:
            for video_format in self._video_formats:
                video_files.extend(directory.rglob(f"*{video_format}"))
        except Exception as e:
            logger.error(f"扫描目录失败：{directory}，错误：{str(e)}")
        
        return video_files

    def scan_and_download(self):
        """扫描目录并下载字幕"""
        # 检查是否已经在运行
        if self._running:
            logger.warning("字幕下载任务正在运行中，跳过本次执行")
            return
        
        # 设置运行标志
        self._running = True
        
        try:
            if not self._api_token:
                logger.error("未配置API Token，无法下载字幕")
                return
            
            if not self._monitor_dirs:
                logger.warning("未配置监控目录")
                return
            
            # 解析监控目录
            directories = [Path(d.strip()) for d in self._monitor_dirs.split('\n') if d.strip()]
            
            if not directories:
                logger.warning("监控目录列表为空")
                return
            
            logger.info(f"开始扫描 {len(directories)} 个目录...")
            download_log = self.get_data('download_log') or []
            
            total_videos = 0
            success_count = 0
            skip_count = 0
            fail_count = 0
            
            for directory in directories:
                # 检查是否需要停止
                if not self._running:
                    logger.info("检测到停止信号，终止任务")
                    break
                    
                logger.info(f"正在扫描目录：{directory}")
                video_files = self._scan_directory(directory)
                total_videos += len(video_files)
                
                for video_path in video_files:
                    # 检查是否需要停止
                    if not self._running:
                        logger.info("检测到停止信号，终止任务")
                        break
                        
                    try:
                        # 检查是否已有mp字幕
                        if self._check_existing_subtitle(video_path):
                            logger.info(f"跳过（已有字幕）：{video_path.name}")
                            skip_count += 1
                            continue
                        
                        logger.info(f"处理视频：{video_path.name}")
                        
                        # 搜索字幕
                        subtitle_info = self._search_subtitle(video_path.name)
                        
                        if not subtitle_info:
                            logger.warning(f"未找到字幕：{video_path.name}")
                            fail_count += 1
                            download_log.append({
                                "video_path": str(video_path),
                                "subtitle_path": "",
                                "status": "失败",
                                "message": "未找到匹配的字幕",
                                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            })
                            continue
                        
                        # 下载字幕
                        subtitle_path = self._download_subtitle(subtitle_info, video_path)
                        
                        if subtitle_path:
                            logger.info(f"成功下载字幕：{subtitle_path.name}")
                            success_count += 1
                            download_log.append({
                                "video_path": str(video_path),
                                "subtitle_path": str(subtitle_path),
                                "status": "成功",
                                "message": f"字幕评分：{subtitle_info.get('score', 'N/A')}",
                                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            })
                        else:
                            logger.error(f"下载字幕失败：{video_path.name}")
                            fail_count += 1
                            download_log.append({
                                "video_path": str(video_path),
                                "subtitle_path": "",
                                "status": "失败",
                                "message": "字幕下载失败",
                                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            })
                            
                    except Exception as e:
                        logger.error(f"处理视频异常：{video_path.name}，错误：{str(e)}")
                        fail_count += 1
                        download_log.append({
                            "video_path": str(video_path),
                            "subtitle_path": "",
                            "status": "失败",
                            "message": f"异常：{str(e)}",
                            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
            
            # 保存日志
            self.save_data('download_log', download_log)
            
            logger.info(f"字幕下载任务完成！总计：{total_videos}，成功：{success_count}，跳过：{skip_count}，失败：{fail_count}")
            
        finally:
            # 无论如何都要重置运行标志
            self._running = False

    @eventmanager.register(EventType.PluginAction)
    def remote_scan(self, event: Event):
        """远程触发扫描"""
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "autosubtitle":
                return
            
            logger.info("收到命令，开始执行字幕下载任务...")
            self.post_message(
                channel=event.event_data.get("channel"),
                title="开始扫描并下载字幕...",
                userid=event.event_data.get("user")
            )
        
        self.scan_and_download()
        
        if event:
            self.post_message(
                channel=event.event_data.get("channel"),
                title="字幕下载任务完成！",
                userid=event.event_data.get("user")
            )

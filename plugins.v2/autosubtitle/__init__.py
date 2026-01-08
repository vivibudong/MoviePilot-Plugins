from datetime import datetime
import os
import requests
from typing import List, Tuple, Dict, Any
from app.plugins import _PluginBase
from app.core.config import settings
from app.utils.system import SystemUtils

class AutoSubtitle(_PluginBase):
    # 插件基本信息
    name = "自动字幕下载"
    desc = "自动监控视频库目录，调用伪射手字幕网assrt.net的api自动下载重命名字幕。"
    icon = "subtitles"
    author = "Vivi"
    author_url = "https://github.com/vivibudong"
    version = "0.1"

    def init_config(self, config: dict = None):
        """初始化配置"""
        self.enabled = config.get("enabled")
        self.run_now = config.get("run_now")
        self.clear_log = config.get("clear_log")
        self.monitor_dirs = config.get("monitor_dirs", "").splitlines()
        self.api_key = config.get("api_key")
        self.overwrite_existing = config.get("overwrite_existing", False)

    def get_fields(self) -> List[dict]:
        """定义插件配置界面字段"""
        return [
            {
                'component': 'VSwitch',
                'label': '启用插件',
                'prop': 'enabled',
            },
            {
                'component': 'VTextarea',
                'label': '监控目录',
                'prop': 'monitor_dirs',
                'placeholder': '每行一个目录路径',
            },
            {
                'component': 'VTextField',
                'label': 'Assrt API Key',
                'prop': 'api_key',
                'placeholder': '请输入伪射手API密钥',
            },
            {
                'component': 'VSwitch',
                'label': '覆盖现有非MP字幕',
                'prop': 'overwrite_existing',
            },
            {
                'component': 'VSwitch',
                'label': '清除历史日志',
                'prop': 'clear_log',
            },
            {
                'component': 'VSwitch',
                'label': '立即运行一次',
                'prop': 'run_now',
            }
        ]

    def run(self):
        """插件运行主函数"""
        if not self.enabled and not self.run_now:
            return

        if self.clear_log:
            # 清理逻辑由框架处理或此处自定义
            pass

        if not self.api_key:
            self.warn("未设置 Assrt API Key，插件无法运行")
            return

        for monitor_dir in self.monitor_dirs:
            monitor_dir = monitor_dir.strip()
            if not monitor_dir or not os.path.exists(monitor_dir):
                continue
            
            self.info(f"开始扫描目录: {monitor_dir}")
            self._process_directory(monitor_dir)

    def _process_directory(self, directory: str):
        """遍历并处理视频文件"""
        video_exts = ('.mkv', '.mp4', '.ts', '.avi')
        for root, _, files in os.walk(directory):
            for file in files:
                if file.lower().endswith(video_exts):
                    file_path = os.path.join(root, file)
                    self._download_subtitle(file_path)

    def _download_subtitle(self, video_path: str):
        """调用API下载字幕"""
        video_name = os.path.basename(video_path)
        video_base_name = os.path.splitext(video_name)[0]
        video_dir = os.path.dirname(video_path)
        
        # 目标字幕文件名
        target_sub_path = os.path.join(video_dir, f"{video_base_name}-mp.srt")

        # 检查是否已存在
        if os.path.exists(target_sub_path):
            return

        # 检查是否已有普通字幕且不允许覆盖
        if not self.overwrite_existing:
            existing_subs = [f for f in os.listdir(video_dir) if f.startswith(video_base_name) and f.endswith(('.srt', '.ass'))]
            if existing_subs:
                return

        # 调用 Assrt API (伪射手)
        try:
            # 第一步：搜索字幕
            search_url = f"https://api.assrt.net/v1/sub/search?token={self.api_key}&q={video_name}&cnt=1"
            resp = requests.get(search_url, timeout=10).json()

            if resp.get("status") == 0 and resp.get("data", {}).get("subs"):
                # 获取评分最高的第一个结果
                sub_id = resp["data"]["subs"][0]["id"]
                
                # 第二步：获取详情/下载链接
                detail_url = f"https://api.assrt.net/v1/sub/detail?token={self.api_key}&id={sub_id}"
                detail_resp = requests.get(detail_url, timeout=10).json()
                
                if detail_resp.get("status") == 0:
                    sub_file_url = detail_resp["data"]["subs"][0]["url"]
                    
                    # 下载文件
                    sub_content = requests.get(sub_file_url, timeout=15).content
                    with open(target_sub_path, 'wb') as f:
                        f.write(sub_content)
                    
                    self.info(f"成功为视频 {video_name} 下载字幕")
        except Exception as e:
            self.error(f"下载字幕出错 {video_name}: {str(e)}")

    def stop(self):
        pass

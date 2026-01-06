import asyncio
import threading
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)
import requests
import nest_asyncio

nest_asyncio.apply()


class EmbyRegisterBot(_PluginBase):
    plugin_name = "Emby用户管理器"
    plugin_desc = "通过独立TG Bot管理Emby用户,支持注册、续期、查询等功能"
    plugin_version = "0.1.3"
    plugin_author = "Vivi"
    author_url = "https://github.com/vivibudong"
    plugin_config_prefix = "embyregisterbot"
    auth_level = 2

    _enabled = False
    _telegram_token = ""
    _emby_host = ""
    _emby_api_key = ""
    _admin_user_ids = []
    _template_user_id = ""
    _register_codes = {}
    _registered_users = {}
    _expire_warning_days = 3
    _bot_thread = None
    _application = None
    _stop_event = None
    _check_thread = None
    _data_file = None

    def __init__(self):
        super().__init__()
        self._data_file = Path.cwd() / "config" / f"{self.plugin_config_prefix}_data.json"

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        if config:
            self._enabled = config.get("enabled", False)
            self._telegram_token = config.get("telegram_token", "")
            self._emby_host = config.get("emby_host", "").rstrip("/")
            self._emby_api_key = config.get("emby_api_key", "")
            self._admin_user_ids = [
                int(uid.strip()) for uid in config.get("admin_user_ids", "").split(",") 
                if uid.strip()
            ]
            self._template_user_id = config.get("template_user_id", "")
            self._expire_warning_days = int(config.get("expire_warning_days", 3))
            
            # 解析框架配置中的初始数据
            self._parse_register_codes(config.get("register_codes", ""))
            self._parse_registered_users(config.get("registered_users", ""))

        # 加载运行时数据文件（覆盖框架初始值）
        self._load_data()

        # 停止旧的bot
        if self._bot_thread and self._bot_thread.is_alive():
            self._stop_bot()

        if self._enabled and self._telegram_token:
            self._start_bot()
            self._start_check_thread()

    def update_config(self, config: dict):
        """框架配置更新方法 - 处理UI表单保存"""
        # 更新标准配置
        self._enabled = config.get("enabled", False)
        self._telegram_token = config.get("telegram_token", "")
        self._emby_host = config.get("emby_host", "").rstrip("/")
        self._emby_api_key = config.get("emby_api_key", "")
        self._admin_user_ids = [
            int(uid.strip()) for uid in config.get("admin_user_ids", "").split(",") 
            if uid.strip()
        ]
        self._template_user_id = config.get("template_user_id", "")
        self._expire_warning_days = int(config.get("expire_warning_days", 3))
        
        # 解析表单中的自定义数据（作为初始值）
        self._parse_register_codes(config.get("register_codes", ""))
        self._parse_registered_users(config.get("registered_users", ""))
        
        # 合并运行时数据（如果存在，优先运行时）
        self._load_data()
        
        logger.info("配置已更新 (来自UI表单)")
        
        # 重启 bot 如果启用
        if self._enabled and self._telegram_token:
            if self._bot_thread and self._bot_thread.is_alive():
                self._stop_bot()
            self._start_bot()
            self._start_check_thread()

    def _load_data(self):
        """加载运行时数据文件"""
        if self._data_file.exists():
            try:
                with open(self._data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._parse_register_codes(data.get("register_codes", ""))
                self._parse_registered_users(data.get("registered_users", ""))
                logger.info(f"运行时数据加载成功: codes={len(self._register_codes)}, users={len(self._registered_users)}")
            except Exception as e:
                logger.error(f"加载运行时数据失败: {e}")

    def _save_data(self):
        """保存运行时数据到文件"""
        try:
            codes_text, users_text = self._generate_config_text()
            data = {
                "register_codes": codes_text,
                "registered_users": users_text
            }
            self._data_file.parent.mkdir(exist_ok=True)
            with open(self._data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"运行时数据保存成功: codes={len(self._register_codes)}, users={len(self._registered_users)}")
        except Exception as e:
            logger.error(f"保存运行时数据失败: {e}")
            logger.critical("运行时数据保存失败！检查 /config 目录权限")

    def _parse_register_codes(self, codes_text: str):
        """解析注册码配置"""
        self._register_codes = {}
        if not codes_text:
            return
        
        for line in codes_text.strip().split("\n"):
            line = line.strip()
            if not line or "," not in line:
                continue
            try:
                code, days = line.split(",", 1)
                self._register_codes[code.strip()] = int(days.strip())
            except:
                logger.warning(f"无效的注册码配置: {line}")

    def _parse_registered_users(self, users_text: str):
        """解析已注册用户配置"""
        self._registered_users = {}
        if not users_text:
            return
        
        for line in users_text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                # 新格式: @username,tgid,注册时间,expire_time,emby用户名,emby_user_id,状态
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 7:
                    continue
                tg_username = parts[0]
                tg_id = int(parts[1])
                register_time = parts[2]
                expire_time_str = parts[3]  # 绝对时间
                emby_username = parts[4]
                emby_user_id = parts[5]
                status = parts[6]
                
                # 直接用expire_time，不计算
                self._registered_users[tg_id] = {
                    "tg_username": tg_username,
                    "emby_username": emby_username,
                    "emby_user_id": emby_user_id,
                    "register_time": register_time,
                    "expire_time": expire_time_str,
                    "status": status
                }
                
                if status == "disabled" and len(parts) >= 8:
                    self._registered_users[tg_id]["disabled_time"] = parts[7]
                    
            except Exception as e:
                logger.warning(f"解析用户信息失败: {line}, 错误: {e}")

    def _generate_config_text(self) -> Tuple[str, str]:
        """生成配置文本"""
        # 生成注册码文本（不变）
        codes_text = "\n".join([f"{code},{days}" for code, days in self._register_codes.items()])
        
        # 生成已注册用户文本（用绝对expire_time）
        users_lines = []
        for tg_id, info in self._registered_users.items():
            if info["status"] == "deleted":
                continue
            
            line = (
                f"{info['tg_username']},{tg_id},{info['register_time']},"
                f"{info['expire_time']},{info['emby_username']},{info['emby_user_id']},{info['status']}"
            )
            
            if info["status"] == "disabled" and "disabled_time" in info:
                line += f",{info['disabled_time']}"
            
            users_lines.append(line)
        
        users_text = "\n".join(users_lines)
        
        return codes_text, users_text

    def get_config(self):
        """返回当前配置（框架UI使用）"""
        codes_text, users_text = self._generate_config_text()
        return {
            "enabled": self._enabled,
            "telegram_token": self._telegram_token,
            "emby_host": self._emby_host,
            "emby_api_key": self._emby_api_key,
            "admin_user_ids": ",".join(map(str, self._admin_user_ids)),
            "template_user_id": self._template_user_id,
            "expire_warning_days": self._expire_warning_days,
            "register_codes": codes_text,
            "registered_users": users_text
        }

    def _start_check_thread(self):
        """启动定期检查

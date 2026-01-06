import asyncio
import threading
import json
import re
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
    plugin_desc = "通过独立TG Bot管理Emby用户，支持注册码、续期、到期管理等"
    plugin_version = "0.1"
    plugin_author = "Vivi"
    author_url = "<https://github.com/vivibudong>"
    plugin_config_prefix = "embyregisterbot"
    auth_level = 2

    # 私有属性
    _enabled = False
    _telegram_token = ""
    _emby_host = ""
    _emby_api_key = ""
    _admin_user_ids = []
    _template_user_id = "" # 模板用户ID
    _expire_notice_days = 3 # 到期提醒天数
    _bot_thread = None
    _application = None
    _stop_event = None
   
    # 数据存储
    _data_file = None
    _users = {} # {tg_id: {emby_id, emby_username, douban_id, created_at, expire_at, status}}
    _codes = {} # {code: days}
    _douban_plugin_config = "" # 豆瓣插件配置文件路径

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
            self._expire_notice_days = int(config.get("expire_notice_days", 3))
            self._douban_plugin_config = config.get("douban_plugin_config", "")
           
            # 加载注册码
            codes_text = config.get("register_codes", "")
            self._codes = {}
            for line in codes_text.strip().split("\n"):
                line = line.strip()
                if line and "," in line:
                    code, days = line.split(",", 1)
                    self._codes[code.strip()] = int(days.strip())
           
            # 加载用户数据
            users_text = config.get("users_data", "")
            self._users = {}
            for line in users_text.strip().split("\n"):
                line = line.strip()
                if line:
                    self._parse_user_line(line)
        # 数据文件路径
        self._data_file = self.get_data_path() / "users.json"
        self._load_data()
        # 停止旧bot
        if self._bot_thread and self._bot_thread.is_alive():
            self._stop_bot()
        if self._enabled and self._telegram_token:
            self._start_bot()
            # 启动定期检查任务
            self._start_check_task()

    def _parse_user_line(self, line: str):
        """解析用户数据行"""
        try:
            parts = line.split(",")
            if len(parts) >= 6:
                tg_username = parts[0].strip().lstrip("@")
                tg_id = int(parts[1].strip())
                created_at = parts[2].strip()
                days_left = int(parts[3].strip())
                emby_username = parts[4].strip()
                douban_id = parts[5].strip() if parts[5].strip() else None
               
                # 计算到期时间
                expire_at = (datetime.now() + timedelta(days=days_left)).strftime("%Y-%m-%d %H:%M:%S")
               
                self._users[tg_id] = {
                    "tg_username": tg_username,
                    "emby_username": emby_username,
                    "emby_id": "", # 需要从Emby查询
                    "douban_id": douban_id,
                    "created_at": created_at,
                    "expire_at": expire_at,
                    "status": "active"
                }
        except Exception as e:
            logger.error(f"解析用户数据失败: {line}, 错误: {e}")

    def _load_data(self):
        """从文件加载数据"""
        if self._data_file.exists():
            try:
                with open(self._data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 转换键为整数
                    self._users = {int(k): v for k, v in data.get("users", {}).items()}
                    logger.info(f"加载了 {len(self._users)} 个用户数据")
            except Exception as e:
                logger.error(f"加载数据文件失败: {e}")

    def _save_data(self):
        """保存数据到文件"""
        try:
            data = {
                "users": self._users,
                "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            with open(self._data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存数据文件失败: {e}")

    def _start_bot(self):
        """启动独立的Telegram Bot"""
        if self._bot_thread and self._bot_thread.is_alive():
            logger.info("Telegram Bot 已在运行中")
            return
        self._stop_event = threading.Event()

        def run_bot():
            try:
                logger.info("正在初始化 Telegram Bot...")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
               
                self._application = (
                    Application.builder()
                    .token(self._telegram_token)
                    .build()
                )
                # 注册命令处理器
                self._application.add_handler(CommandHandler("start", self._cmd_start))
                self._application.add_handler(CommandHandler("register", self._cmd_register))
                self._application.add_handler(CommandHandler("info", self._cmd_info))
                self._application.add_handler(CommandHandler("renew", self._cmd_renew))
                self._application.add_handler(CommandHandler("setdouban", self._cmd_setdouban))
                self._application.add_handler(CommandHandler("help", self._cmd_help))
               
                # 管理员命令
                self._application.add_handler(CommandHandler("admin", self._cmd_admin))
               
                # 回调查询处理器
                self._application.add_handler(CallbackQueryHandler(self._button_callback))
                logger.info("Telegram Bot 启动成功，开始轮询...")
               
                loop.run_until_complete(self._application.initialize())
                loop.run_until_complete(self._application.start())
               
                bot_info = loop.run_until_complete(self._application.bot.get_me())
                logger.info(f"✅ Bot信息 - @{bot_info.username}, ID: {bot_info.id}")
               
                # 手动轮询
                last_update_id = 0
                while not self._stop_event.is_set():
                    try:
                        updates = loop.run_until_complete(
                            self._application.bot.get_updates(
                                offset=last_update_id,
                                timeout=10,
                                allowed_updates=Update.ALL_TYPES
                            )
                        )
                       
                        if updates:
                            for update in updates:
                                last_update_id = update.update_id + 1
                                loop.run_until_complete(
                                    self._application.process_update(update)
                                )
                       
                        if self._stop_event.wait(timeout=0.1):
                            break
                           
                    except Exception as e:
                        logger.error(f"轮询错误: {e}")
                        self._stop_event.wait(timeout=3)
               
                logger.info("正在停止 Telegram Bot...")
                loop.run_until_complete(self._application.stop())
                loop.run_until_complete(self._application.shutdown())
                loop.close()
               
            except Exception as e:
                logger.error(f"Telegram Bot 运行错误: {str(e)}", exc_info=True)
        self._bot_thread = threading.Thread(target=run_bot, daemon=True, name="EmbyBotThread")
        self._bot_thread.start()
        logger.info("Telegram Bot 线程已启动")

    def _stop_bot(self):
        """停止Telegram Bot"""
        if self._stop_event:
            self._stop_event.set()
        if self._bot_thread and self._bot_thread.is_alive():
            self._bot_thread.join(timeout=5)

    def _start_check_task(self):
        """启动定期检查任务"""
        def check_loop():
            while self._enabled:
                try:
                    self._check_expirations()
                except Exception as e:
                    logger.error(f"检查到期任务错误: {e}")
                # 每小时检查一次
                threading.Event().wait(3600)
       
        threading.Thread(target=check_loop, daemon=True, name="ExpiryCheckThread").start()

    def _check_expirations(self):
        """检查用户到期情况"""
        now = datetime.now()
       
        for tg_id, user in list(self._users.items()):
            expire_at = datetime.strptime(user["expire_at"], "%Y-%m-%d %H:%M:%S")
            days_left = (expire_at - now).days
           
            # 到期提醒
            if days_left == self._expire_notice_days and user["status"] == "active":
                asyncio.run(self._send_expire_notice(tg_id, days_left))
           
            # 到期禁用
            elif days_left <= 0 and user["status"] == "active":
                self._disable_emby_user(user["emby_id"])
                user["status"] = "disabled"
                user["disabled_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                self._save_data()
                asyncio.run(self._send_message(tg_id, "⚠️ 您的账号已到期并被禁用，请尽快续期"))
           
            # 超过7天删除
            elif user["status"] == "disabled":
                disabled_at = datetime.strptime(user["disabled_at"], "%Y-%m-%d %H:%M:%S")
                if (now - disabled_at).days >= 7:
                    self._delete_emby_user(user["emby_id"])
                    del self._users[tg_id]
                    self._save_data()
                    asyncio.run(self._send_message(tg_id, "❌ 您的账号因长期未续期已被删除"))

    async def _send_expire_notice(self, tg_id: int, days: int):
        """发送到期提醒"""
        await self._send_message(tg_id, f"⏰ 提醒：您的账号还有 {days} 天到期，请及时续期！")

    async def _send_message(self, tg_id: int, text: str):
        """发送消息给用户"""
        try:
            if self._application:
                await self._application.bot.send_message(chat_id=tg_id, text=text)
        except Exception as e:
            logger.error(f"发送消息失败: {e}")

    # ===== Telegram 命令处理器 =====
   
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /start 命令"""
        help_text = (
            "🎬 欢迎使用 Emby 用户管理系统！\n\n"
            "📋 可用命令：\n"
            "/register <用户名> <注册码> - 注册新账号\n"
            "/info - 查询账号信息\n"
            "/renew <注册码> - 续期账号\n"
            "/setdouban <豆瓣ID> - 设置豆瓣订阅\n"
            "/help - 显示帮助信息\n\n"
            "💡 示例：/register myname ABC123"
        )
        await update.message.reply_text(help_text)

    async def _cmd_register(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /register 命令"""
        user_id = update.effective_user.id
        username = update.effective_user.username or f"user_{user_id}"
       
        # 检查是否已注册
        if user_id in self._users:
            await update.message.reply_text("❌ 您已经注册过了，请使用 /info 查询信息")
            return
       
        # 检查参数
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ 参数错误\n"
                "用法: /register <Emby用户名> <注册码>\n"
                "示例: /register myname ABC123"
            )
            return
       
        emby_username = context.args[0]
        code = context.args[1]
       
        # 验证注册码
        if code not in self._codes:
            await update.message.reply_text("❌ 注册码无效")
            return
       
        # 创建Emby用户
        success, emby_user_id, message = self._create_emby_user(emby_username)
       
        if success:
            days = self._codes[code]
            expire_date = datetime.now() + timedelta(days=days)
           
            # 保存用户数据
            self._users[user_id] = {
                "tg_username": username,
                "emby_username": emby_username,
                "emby_id": emby_user_id,
                "douban_id": None,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "expire_at": expire_date.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "active"
            }
           
            # 删除已使用的注册码
            del self._codes[code]
            self._save_data()
           
            await update.message.reply_text(
                f"✅ 注册成功！\n\n"
                f"👤 用户名: {emby_username}\n"
                f"📅 到期时间: {expire_date.strftime('%Y-%m-%d')}\n"
                f"⏱️ 有效期: {days}天\n\n"
                f"🔗 Emby服务器: {self._emby_host}\n"
                f"🔑 初始密码: 空（请登录后修改）"
            )
        else:
            await update.message.reply_text(f"❌ 注册失败: {message}")

    async def _cmd_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /info 命令"""
        user_id = update.effective_user.id
       
        if user_id not in self._users:
            await update.message.reply_text("❌ 您还未注册，请使用 /register 注册账号")
            return
       
        user = self._users[user_id]
        expire_at = datetime.strptime(user["expire_at"], "%Y-%m-%d %H:%M:%S")
        days_left = (expire_at - datetime.now()).days
       
        status_emoji = "✅" if user["status"] == "active" else "⚠️"
        status_text = "正常" if user["status"] == "active" else "已禁用"
       
        douban_info = f"🎬 豆瓣ID: {user['douban_id']}" if user['douban_id'] else "🎬 豆瓣ID: 未设置"
       
        await update.message.reply_text(
            f"📊 您的账号信息：\n\n"
            f"👤 Emby用户名: {user['emby_username']}\n"
            f"📅 到期时间: {expire_at.strftime('%Y-%m-%d')}\n"
            f"⏰ 剩余天数: {days_left}天\n"
            f"📌 状态: {status_emoji} {status_text}\n"
            f"{douban_info}\n"
            f"🆔 Emby ID: {user['emby_id']}\n"
            f"📝 注册时间: {user['created_at']}"
        )

    async def _cmd_renew(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /renew 命令"""
        user_id = update.effective_user.id
       
        if user_id not in self._users:
            await update.message.reply_text("❌ 您还未注册，请使用 /register 注册账号")
            return
       
        if not context.args:
            await update.message.reply_text("❌ 请提供注册码\n用法: /renew <注册码>")
            return
       
        code = context.args[0]
       
        if code not in self._codes:
            await update.message.reply_text("❌ 注册码无效")
            return
       
        user = self._users[user_id]
        days = self._codes[code]
       
        # 计算新的到期时间
        current_expire = datetime.strptime(user["expire_at"], "%Y-%m-%d %H:%M:%S")
        if current_expire < datetime.now():
            new_expire = datetime.now() + timedelta(days=days)
        else:
            new_expire = current_expire + timedelta(days=days)
       
        user["expire_at"] = new_expire.strftime("%Y-%m-%d %H:%M:%S")
       
        # 如果是禁用状态，重新启用
        if user["status"] == "disabled":
            self._enable_emby_user(user["emby_id"])
            user["status"] = "active"
       
        # 删除已使用的注册码
        del self._codes[code]
        self._save_data()
       
        await update.message.reply_text(
            f"✅ 续期成功！\n\n"
            f"📅 新到期时间: {new_expire.strftime('%Y-%m-%d')}\n"
            f"➕ 增加天数: {days}天"
        )

    async def _cmd_setdouban(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /setdouban 命令"""
        user_id = update.effective_user.id
       
        if user_id not in self._users:
            await update.message.reply_text("❌ 您还未注册，请使用 /register 注册账号")
            return
       
        user = self._users[user_id]
       
        if user.get("douban_id"):
            await update.message.reply_text("❌ 您已设置过豆瓣ID，无法修改")
            return
       
        if not context.args:
            await update.message.reply_text("❌ 请提供豆瓣ID\n用法: /setdouban <豆瓣ID>")
            return
       
        douban_id = context.args[0]
        user["douban_id"] = douban_id
        self._save_data()
       
        # 更新豆瓣插件配置
        self._update_douban_plugin(douban_id, user["emby_username"])
       
        await update.message.reply_text(f"✅ 豆瓣ID设置成功: {douban_id}")

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /help 命令"""
        await self._cmd_start(update, context)

    async def _cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /admin 命令"""
        if update.effective_user.id not in self._admin_user_ids:
            await update.message.reply_text("❌ 您没有管理员权限")
            return
       
        total = len(self._users)
        active = sum(1 for u in self._users.values() if u["status"] == "active")
        disabled = sum(1 for u in self._users.values() if u["status"] == "disabled")
        codes_count = len(self._codes)
       
        await update.message.reply_text(
            f"🔧 管理员面板\n\n"
            f"👥 总用户数: {total}\n"
            f"✅ 活跃用户: {active}\n"
            f"⚠️ 已禁用: {disabled}\n"
            f"🎟️ 剩余注册码: {codes_count}"
        )

    async def _button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理按钮回调"""
        query = update.callback_query
        await query.answer()
       
        # 这里可以添加按钮交互逻辑

    # ===== Emby API 交互方法 =====
   
    def _create_emby_user(self, username: str) -> Tuple[bool, str, str]:
        """创建Emby用户"""
        try:
            url = f"{self._emby_host}/emby/Users/New"
            headers = {"X-Emby-Token": self._emby_api_key}
            data = {"Name": username}
           
            response = requests.post(url, headers=headers, json=data, timeout=10)
           
            if response.status_code == 200:
                user_data = response.json()
                user_id = user_data["Id"]
               
                # 如果有模板用户，复制配置
                if self._template_user_id:
                    self._copy_user_policy(self._template_user_id, user_id)
               
                return True, user_id, "创建成功"
            else:
                return False, "", f"API返回错误: {response.status_code}"
               
        except Exception as e:
            logger.error(f"创建Emby用户失败: {str(e)}")
            return False, "", str(e)

    def _copy_user_policy(self, template_id: str, target_id: str):
        """复制用户策略"""
        try:
            # 获取模板用户策略
            url = f"{self._emby_host}/emby/Users/{template_id}"
            headers = {"X-Emby-Token": self._emby_api_key}
            response = requests.get(url, headers=headers, timeout=10)
           
            if response.status_code == 200:
                template_data = response.json()
                policy = template_data.get("Policy", {})
               
                # 应用到目标用户
                url = f"{self._emby_host}/emby/Users/{target_id}/Policy"
                requests.post(url, headers=headers, json=policy, timeout=10)
               
        except Exception as e:
            logger.error(f"复制用户策略失败: {e}")

    def _disable_emby_user(self, user_id: str):
        """禁用Emby用户"""
        try:
            url = f"{self._emby_host}/emby/Users/{user_id}/Policy"
            headers = {"X-Emby-Token": self._emby_api_key}
            data = {"IsDisabled": True}
            requests.post(url, headers=headers, json=data, timeout=10)
        except Exception as e:
            logger.error(f"禁用用户失败: {e}")

    def _enable_emby_user(self, user_id: str):
        """启用Emby用户"""
        try:
            url = f"{self._emby_host}/emby/Users/{user_id}/Policy"
            headers = {"X-Emby-Token": self._emby_api_key}
            data = {"IsDisabled": False}
            requests.post(url, headers=headers, json=data, timeout=10)
        except Exception as e:
            logger.error(f"启用用户失败: {e}")

    def _delete_emby_user(self, user_id: str) -> bool:
        """删除Emby用户"""
        try:
            url = f"{self._emby_host}/emby/Users/{user_id}"
            headers = {"X-Emby-Token": self._emby_api_key}
            response = requests.delete(url, headers=headers, timeout=10)
            return response.status_code == 204
        except Exception as e:
            logger.error(f"删除Emby用户失败: {str(e)}")
            return False

    def _update_douban_plugin(self, douban_id: str, emby_username: str):
        """更新豆瓣插件配置"""
        if not self._douban_plugin_config:
            return
       
        try:
            config_path = Path(self._douban_plugin_config)
            if not config_path.exists():
                logger.warning(f"豆瓣插件配置文件不存在: {config_path}")
                return
           
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
           
            # 更新用户列表
            user_list = config.get("user_list", "")
            users = [u.strip() for u in user_list.split("|") if u.strip()]
            new_entry = f"{douban_id},{emby_username}"
           
            if new_entry not in users:
                users.append(new_entry)
                config["user_list"] = "|".join(users)
               
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)
               
                logger.info(f"已更新豆瓣插件配置: {new_entry}")
       
        except Exception as e:
            logger.error(f"更新豆瓣插件配置失败: {e}")

    def get_state(self) -> bool:
        """获取插件状态"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """获取插件命令"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """获取插件API"""
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """获取插件配置表单"""
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'emby_host',
                                            'label': 'Emby服务器地址',
                                            'placeholder': 'http://emby:8096',
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
                                            'model': 'emby_api_key',
                                            'label': 'Emby API Key',
                                            'placeholder': '在Emby控制台生成',
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
                                            'model': 'template_user_id',
                                            'label': '模板用户ID',
                                            'placeholder': '复制此用户的权限配置',
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
                                            'model': 'expire_notice_days',
                                            'label': '到期提醒天数',
                                            'placeholder': '3',
                                            'type': 'number'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'admin_user_ids',
                                            'label': '管理员Telegram User ID',
                                            'placeholder': '多个ID用英文逗号分隔',
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'telegram_token',
                                            'label': 'Telegram Bot Token',
                                            'placeholder': '从 @BotFather 获取',
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'register_codes',
                                            'label': '注册码列表',
                                            'placeholder': '格式: 注册码,天数\n每行一个\n例如:\nABC123,30\nDEF456,90',
                                            'rows': 5
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'users_data',
                                            'label': '用户数据（自动维护，可手动删除）',
                                            'placeholder': '格式: @TG用户名,TGID,注册时间,剩余天数,Emby用户名,豆瓣ID\n删除某行会自动删除对应Emby账户',
                                            'rows': 10
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'douban_plugin_config',
                                            'label': '豆瓣插件配置文件路径',
                                            'placeholder': '/path/to/douban/config.json',
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '✨ 完全独立的Telegram Bot，不依赖MP通知渠道\n🔄 自动管理用户到期、禁用、删除\n📊 用户数据实时同步到配置中'
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
            "telegram_token": "",
            "emby_host": "http://emby:8096",
            "emby_api_key": "",
            "template_user_id": "",
            "admin_user_ids": "",
            "register_codes": "",
            "users_data": "",
            "expire_notice_days": 3,
            "douban_plugin_config": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """停止插件服务"""
        self._stop_bot()

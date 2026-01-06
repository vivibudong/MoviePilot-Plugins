import asyncio
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

# 需要安装: pip install python-telegram-bot requests
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


class EmbyRegisterBot(_PluginBase):
    # 插件名称
    plugin_name = "Emby用户管理器"
    # 插件描述
    plugin_desc = "通过独立TG Bot管理Emby用户，支持注册、续期、查询等功能"
    # 插件版本
    plugin_version = "0.1"
    # 插件作者
    plugin_author = "Vivi"
    # 作者主页
    author_url = "https://github.com/vivibudong"
    # 插件配置项ID前缀
    plugin_config_prefix = "embyregisterbot"
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _telegram_token = ""
    _emby_host = ""
    _emby_api_key = ""
    _admin_user_ids = []
    _default_days = 30
    _bot_thread = None
    _application = None
    _user_data = {}  # 存储用户数据 {tg_user_id: {"emby_user_id": "", "expire_date": ""}}

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
            self._default_days = int(config.get("default_days", 30))

        if self._enabled and self._telegram_token:
            self._start_bot()

    def _start_bot(self):
        """启动独立的Telegram Bot"""
        if self._bot_thread and self._bot_thread.is_alive():
            logger.info("Telegram Bot 已在运行中")
            return

        def run_bot():
            try:
                # 创建新的事件循环
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                # 创建Application
                self._application = Application.builder().token(self._telegram_token).build()

                # 注册命令处理器
                self._application.add_handler(CommandHandler("start", self._cmd_start))
                self._application.add_handler(CommandHandler("register", self._cmd_register))
                self._application.add_handler(CommandHandler("info", self._cmd_info))
                self._application.add_handler(CommandHandler("renew", self._cmd_renew))
                self._application.add_handler(CommandHandler("help", self._cmd_help))
                
                # 管理员命令
                self._application.add_handler(CommandHandler("admin", self._cmd_admin))
                self._application.add_handler(CommandHandler("list", self._cmd_list))
                self._application.add_handler(CommandHandler("delete", self._cmd_delete))
                
                # 回调查询处理器
                self._application.add_handler(CallbackQueryHandler(self._button_callback))

                logger.info("Telegram Bot 启动成功，开始轮询...")
                # 启动轮询
                self._application.run_polling(allowed_updates=Update.ALL_TYPES)
                
            except Exception as e:
                logger.error(f"Telegram Bot 运行错误: {str(e)}")

        # 在新线程中运行bot
        self._bot_thread = threading.Thread(target=run_bot, daemon=True)
        self._bot_thread.start()
        logger.info("Telegram Bot 线程已启动")

    def _stop_bot(self):
        """停止Telegram Bot"""
        if self._application:
            try:
                # 停止轮询
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self._application.stop())
                loop.run_until_complete(self._application.shutdown())
                logger.info("Telegram Bot 已停止")
            except Exception as e:
                logger.error(f"停止 Telegram Bot 错误: {str(e)}")

    # ===== Telegram 命令处理器 =====
    
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /start 命令"""
        keyboard = [
            [InlineKeyboardButton("📝 注册账号", callback_data="register")],
            [InlineKeyboardButton("ℹ️ 查询信息", callback_data="info")],
            [InlineKeyboardButton("🔄 续期账号", callback_data="renew")],
            [InlineKeyboardButton("❓ 帮助", callback_data="help")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"👋 欢迎使用 Emby 用户管理系统！\n\n"
            f"🎬 插件版本: {self.plugin_version}\n"
            f"请选择功能：",
            reply_markup=reply_markup
        )

    async def _cmd_register(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /register 命令 - 注册新用户"""
        user_id = update.effective_user.id
        username = update.effective_user.username or f"user_{user_id}"
        
        # 检查是否已注册
        if user_id in self._user_data:
            await update.message.reply_text("❌ 您已经注册过了，请使用 /info 查询信息")
            return
        
        # 调用Emby API创建用户
        success, emby_user_id, message = self._create_emby_user(username)
        
        if success:
            # 保存用户数据
            expire_date = datetime.now() + timedelta(days=self._default_days)
            self._user_data[user_id] = {
                "emby_user_id": emby_user_id,
                "username": username,
                "expire_date": expire_date.strftime("%Y-%m-%d %H:%M:%S"),
                "created_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            await update.message.reply_text(
                f"✅ 注册成功！\n\n"
                f"👤 用户名: {username}\n"
                f"📅 到期时间: {expire_date.strftime('%Y-%m-%d')}\n"
                f"⏱️ 有效期: {self._default_days}天\n\n"
                f"🔗 Emby服务器: {self._emby_host}\n"
                f"🔑 初始密码: 空（请登录后修改）"
            )
        else:
            await update.message.reply_text(f"❌ 注册失败: {message}")

    async def _cmd_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /info 命令 - 查询用户信息"""
        user_id = update.effective_user.id
        
        if user_id not in self._user_data:
            await update.message.reply_text("❌ 您还未注册，请使用 /register 注册账号")
            return
        
        user_info = self._user_data[user_id]
        expire_date = datetime.strptime(user_info["expire_date"], "%Y-%m-%d %H:%M:%S")
        days_left = (expire_date - datetime.now()).days
        
        status = "✅ 正常" if days_left > 0 else "⚠️ 已过期"
        
        await update.message.reply_text(
            f"📊 您的账号信息：\n\n"
            f"👤 用户名: {user_info['username']}\n"
            f"📅 到期时间: {expire_date.strftime('%Y-%m-%d')}\n"
            f"⏰ 剩余天数: {days_left}天\n"
            f"📌 状态: {status}\n"
            f"🆔 Emby ID: {user_info['emby_user_id']}\n"
            f"📝 注册时间: {user_info['created_date']}"
        )

    async def _cmd_renew(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /renew 命令 - 续期账号"""
        user_id = update.effective_user.id
        
        if user_id not in self._user_data:
            await update.message.reply_text("❌ 您还未注册，请使用 /register 注册账号")
            return
        
        # 这里可以添加支付逻辑，目前直接续期
        user_info = self._user_data[user_id]
        current_expire = datetime.strptime(user_info["expire_date"], "%Y-%m-%d %H:%M:%S")
        
        # 如果已过期，从当前时间开始续期；否则从到期时间续期
        if current_expire < datetime.now():
            new_expire = datetime.now() + timedelta(days=self._default_days)
        else:
            new_expire = current_expire + timedelta(days=self._default_days)
        
        self._user_data[user_id]["expire_date"] = new_expire.strftime("%Y-%m-%d %H:%M:%S")
        
        await update.message.reply_text(
            f"✅ 续期成功！\n\n"
            f"📅 新到期时间: {new_expire.strftime('%Y-%m-%d')}\n"
            f"➕ 增加天数: {self._default_days}天"
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /help 命令"""
        help_text = (
            "📚 命令列表：\n\n"
            "👥 用户命令：\n"
            "/start - 启动机器人\n"
            "/register - 注册新账号\n"
            "/info - 查询账号信息\n"
            "/renew - 续期账号\n"
            "/help - 显示帮助信息\n\n"
        )
        
        if update.effective_user.id in self._admin_user_ids:
            help_text += (
                "🔧 管理员命令：\n"
                "/admin - 管理员面板\n"
                "/list - 列出所有用户\n"
                "/delete <user_id> - 删除用户\n"
            )
        
        await update.message.reply_text(help_text)

    async def _cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /admin 命令 - 管理员面板"""
        if update.effective_user.id not in self._admin_user_ids:
            await update.message.reply_text("❌ 您没有管理员权限")
            return
        
        total_users = len(self._user_data)
        active_users = sum(
            1 for u in self._user_data.values()
            if datetime.strptime(u["expire_date"], "%Y-%m-%d %H:%M:%S") > datetime.now()
        )
        
        keyboard = [
            [InlineKeyboardButton("📋 查看所有用户", callback_data="admin_list")],
            [InlineKeyboardButton("📊 统计信息", callback_data="admin_stats")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"🔧 管理员面板\n\n"
            f"👥 总用户数: {total_users}\n"
            f"✅ 活跃用户: {active_users}\n"
            f"⚠️ 过期用户: {total_users - active_users}",
            reply_markup=reply_markup
        )

    async def _cmd_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /list 命令 - 列出所有用户"""
        if update.effective_user.id not in self._admin_user_ids:
            await update.message.reply_text("❌ 您没有管理员权限")
            return
        
        if not self._user_data:
            await update.message.reply_text("📭 暂无用户")
            return
        
        user_list = "📋 用户列表：\n\n"
        for tg_id, info in self._user_data.items():
            expire_date = datetime.strptime(info["expire_date"], "%Y-%m-%d %H:%M:%S")
            days_left = (expire_date - datetime.now()).days
            status = "✅" if days_left > 0 else "⚠️"
            user_list += f"{status} {info['username']} (TG:{tg_id}) - 剩余{days_left}天\n"
        
        await update.message.reply_text(user_list)

    async def _cmd_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /delete 命令 - 删除用户"""
        if update.effective_user.id not in self._admin_user_ids:
            await update.message.reply_text("❌ 您没有管理员权限")
            return
        
        if not context.args:
            await update.message.reply_text("❌ 请提供用户ID\n用法: /delete <telegram_user_id>")
            return
        
        try:
            target_user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ 无效的用户ID")
            return
        
        if target_user_id not in self._user_data:
            await update.message.reply_text("❌ 用户不存在")
            return
        
        user_info = self._user_data[target_user_id]
        # 删除Emby用户
        success = self._delete_emby_user(user_info["emby_user_id"])
        
        if success:
            del self._user_data[target_user_id]
            await update.message.reply_text(f"✅ 已删除用户: {user_info['username']}")
        else:
            await update.message.reply_text(f"⚠️ 删除Emby用户失败，但已从系统移除")
            del self._user_data[target_user_id]

    async def _button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理按钮回调"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data == "register":
            await self._cmd_register(update, context)
        elif data == "info":
            await self._cmd_info(update, context)
        elif data == "renew":
            await self._cmd_renew(update, context)
        elif data == "help":
            await self._cmd_help(update, context)
        elif data == "admin_list":
            await self._cmd_list(update, context)

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
                return True, user_data["Id"], "创建成功"
            else:
                return False, "", f"API返回错误: {response.status_code}"
                
        except Exception as e:
            logger.error(f"创建Emby用户失败: {str(e)}")
            return False, "", str(e)

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

    def get_state(self) -> bool:
        """获取插件状态"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """获取插件命令"""
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """获取插件API"""
        pass

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
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'emby_host',
                                            'label': 'Emby服务器地址',
                                            'placeholder': 'http://localhost:8096',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'default_days',
                                            'label': '默认有效期(天)',
                                            'placeholder': '30',
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
                                            'model': 'emby_api_key',
                                            'label': 'Emby API Key',
                                            'placeholder': '在Emby控制台-高级-API密钥中生成',
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
                                            'placeholder': '多个ID用英文逗号分隔，如: 123456,789012',
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
                                            'text': '本插件使用独立的Telegram Bot，不会与MP通知渠道冲突'
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
            "emby_host": "http://localhost:8096",
            "emby_api_key": "",
            "admin_user_ids": "",
            "default_days": 30
        }

    def get_page(self) -> List[dict]:
        """获取插件页面"""
        pass

    def stop_service(self):
        """停止插件服务"""
        self._stop_bot()

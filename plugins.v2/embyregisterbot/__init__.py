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
    plugin_version = "0.1"
    plugin_author = "Vivi"
    author_url = "https://github.com/vivibudong"
    plugin_config_prefix = "embyregisterbot"
    auth_level = 2

    _enabled = False
    _telegram_token = ""
    _emby_host = ""
    _emby_api_key = ""
    _admin_user_ids = []
    _template_user_id = ""  # 模板用户ID
    _register_codes = {}  # {code: days}
    _registered_users = {}  # {tg_user_id: {user_info}}
    _expire_warning_days = 3
    _bot_thread = None
    _application = None
    _stop_event = None
    _check_thread = None
    _data_path = None

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
            
            # 解析注册码
            self._parse_register_codes(config.get("register_codes", ""))
            
            # 解析已注册用户
            self._parse_registered_users(config.get("registered_users", ""))

        # 设置数据存储路径
        self._data_path = Path(__file__).parent / "data"
        self._data_path.mkdir(exist_ok=True)

        # 停止旧的bot
        if self._bot_thread and self._bot_thread.is_alive():
            self._stop_bot()

        if self._enabled and self._telegram_token:
            self._start_bot()
            self._start_check_thread()

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
                # 格式: @username,tgid,注册时间,剩余天数,emby用户名
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 5:
                    tg_username = parts[0]
                    tg_id = int(parts[1])
                    register_time = parts[2]
                    days_left = int(parts[3])
                    emby_username = parts[4]
                    
                    # 计算到期时间
                    register_dt = datetime.strptime(register_time, "%Y-%m-%d %H:%M:%S")
                    expire_dt = register_dt + timedelta(days=days_left)
                    
                    self._registered_users[tg_id] = {
                        "tg_username": tg_username,
                        "emby_username": emby_username,
                        "register_time": register_time,
                        "expire_time": expire_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "emby_user_id": "",  # 需要从Emby获取
                        "status": "active"  # active, disabled, deleted
                    }
            except Exception as e:
                logger.warning(f"解析用户信息失败: {line}, 错误: {e}")

    def _save_config(self):
        """保存配置到插件配置"""
        # 生成注册码文本
        codes_text = "\n".join([f"{code},{days}" for code, days in self._register_codes.items()])
        
        # 生成已注册用户文本
        users_lines = []
        for tg_id, info in self._registered_users.items():
            if info["status"] == "deleted":
                continue
            
            register_dt = datetime.strptime(info["register_time"], "%Y-%m-%d %H:%M:%S")
            expire_dt = datetime.strptime(info["expire_time"], "%Y-%m-%d %H:%M:%S")
            days_left = (expire_dt - datetime.now()).days
            
            users_lines.append(
                f"{info['tg_username']},{tg_id},{info['register_time']},{days_left},{info['emby_username']}"
            )
        users_text = "\n".join(users_lines)
        
        # 这里需要调用MoviePilot的配置更新方法
        # 由于无法直接访问配置系统,我们使用文件存储
        config_file = self._data_path / "config.json"
        config_data = {
            "register_codes": self._register_codes,
            "registered_users": self._registered_users
        }
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=2)

    def _start_check_thread(self):
        """启动定期检查线程"""
        def check_loop():
            while not self._stop_event.is_set():
                try:
                    self._check_expiring_users()
                    self._check_expired_users()
                except Exception as e:
                    logger.error(f"检查用户状态失败: {e}")
                
                # 每小时检查一次
                self._stop_event.wait(3600)
        
        self._check_thread = threading.Thread(target=check_loop, daemon=True, name="EmbyCheckThread")
        self._check_thread.start()
        logger.info("用户状态检查线程已启动")

    def _check_expiring_users(self):
        """检查即将到期的用户"""
        now = datetime.now()
        
        for tg_id, info in self._registered_users.items():
            if info["status"] != "active":
                continue
            
            expire_dt = datetime.strptime(info["expire_time"], "%Y-%m-%d %H:%M:%S")
            days_left = (expire_dt - now).days
            
            if 0 < days_left <= self._expire_warning_days:
                # 发送到期提醒
                asyncio.run(self._send_expire_warning(tg_id, days_left))

    def _check_expired_users(self):
        """检查过期用户"""
        now = datetime.now()
        
        for tg_id, info in self._registered_users.items():
            expire_dt = datetime.strptime(info["expire_time"], "%Y-%m-%d %H:%M:%S")
            
            if info["status"] == "active" and expire_dt < now:
                # 禁用账户
                if self._disable_emby_user(info["emby_user_id"]):
                    info["status"] = "disabled"
                    info["disabled_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    self._save_config()
                    asyncio.run(self._send_message(tg_id, "⚠️ 您的Emby账户已到期并被禁用,7天内续费可恢复"))
            
            elif info["status"] == "disabled":
                disabled_dt = datetime.strptime(info.get("disabled_time", info["expire_time"]), "%Y-%m-%d %H:%M:%S")
                if (now - disabled_dt).days >= 7:
                    # 删除账户
                    if self._delete_emby_user(info["emby_user_id"]):
                        info["status"] = "deleted"
                        self._save_config()
                        asyncio.run(self._send_message(tg_id, "❌ 您的Emby账户已被永久删除"))

    async def _send_expire_warning(self, tg_id: int, days_left: int):
        """发送到期提醒"""
        if not self._application:
            return
        
        try:
            await self._application.bot.send_message(
                chat_id=tg_id,
                text=f"⏰ 提醒: 您的Emby账户还有 {days_left} 天到期\n\n请及时使用 /renew 命令续费"
            )
        except Exception as e:
            logger.error(f"发送到期提醒失败: {e}")

    async def _send_message(self, tg_id: int, text: str):
        """发送消息给用户"""
        if not self._application:
            return
        
        try:
            await self._application.bot.send_message(chat_id=tg_id, text=text)
        except Exception as e:
            logger.error(f"发送消息失败: {e}")

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
                self._application.add_handler(CommandHandler("help", self._cmd_help))
                
                # 管理员命令
                self._application.add_handler(CommandHandler("admin", self._cmd_admin))
                self._application.add_handler(CommandHandler("addcode", self._cmd_addcode))
                
                # 回调查询处理器
                self._application.add_handler(CallbackQueryHandler(self._button_callback))

                logger.info("Telegram Bot 启动成功,开始轮询...")
                
                loop.run_until_complete(self._application.initialize())
                loop.run_until_complete(self._application.start())
                
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
                            logger.info(f"📨 收到 {len(updates)} 条更新")
                            for update in updates:
                                last_update_id = update.update_id + 1
                                try:
                                    loop.run_until_complete(
                                        self._application.process_update(update)
                                    )
                                except Exception as process_err:
                                    logger.error(f"处理update失败: {process_err}", exc_info=True)
                        
                        if self._stop_event.wait(timeout=0.1):
                            break
                            
                    except Exception as e:
                        logger.error(f"轮询错误: {e}", exc_info=True)
                        self._stop_event.wait(timeout=3)
                
                # 停止bot
                logger.info("正在停止 Telegram Bot...")
                loop.run_until_complete(self._application.stop())
                loop.run_until_complete(self._application.shutdown())
                loop.close()
                logger.info("Telegram Bot 已停止")
                
            except Exception as e:
                logger.error(f"Telegram Bot 运行错误: {str(e)}", exc_info=True)

        self._bot_thread = threading.Thread(target=run_bot, daemon=True, name="EmbyBotThread")
        self._bot_thread.start()
        logger.info("Telegram Bot 线程已启动")

    def _stop_bot(self):
        """停止Telegram Bot"""
        if self._stop_event:
            logger.info("发送停止信号到 Telegram Bot...")
            self._stop_event.set()
            
        if self._bot_thread and self._bot_thread.is_alive():
            self._bot_thread.join(timeout=5)
            logger.info("Telegram Bot 线程已停止")

    # ===== Telegram 命令处理器 =====
    
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /start 命令"""
        logger.info(f"收到 /start 命令 - 用户ID: {update.effective_user.id}")
        
        help_text = (
            "👋 欢迎使用 Emby 用户管理系统!\n\n"
            "📝 可用命令:\n"
            "/register <用户名> <注册码> - 注册新账户\n"
            "/info - 查询账户信息\n"
            "/renew <注册码> - 续期账户\n"
            "/help - 显示帮助信息\n\n"
            "💡 示例:\n"
            "/register myname ABC123\n"
            "/renew ABC123"
        )
        
        await update.message.reply_text(help_text)

    async def _cmd_register(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /register 命令 - 注册新用户"""
        user_id = update.effective_user.id
        username = update.effective_user.username or f"user_{user_id}"
        
        # 检查是否已注册
        if user_id in self._registered_users and self._registered_users[user_id]["status"] != "deleted":
            await update.message.reply_text("❌ 您已经注册过了,请使用 /info 查询信息")
            return
        
        # 检查参数
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ 参数错误\n\n"
                "用法: /register <Emby用户名> <注册码>\n"
                "示例: /register myname ABC123"
            )
            return
        
        emby_username = context.args[0]
        register_code = context.args[1]
        
        # 验证注册码
        if register_code not in self._register_codes:
            await update.message.reply_text("❌ 无效的注册码")
            return
        
        days = self._register_codes[register_code]
        
        # 创建Emby用户
        success, emby_user_id, message = self._create_emby_user(emby_username)
        
        if success:
            # 保存用户数据
            now = datetime.now()
            expire_dt = now + timedelta(days=days)
            
            self._registered_users[user_id] = {
                "tg_username": f"@{username}",
                "emby_username": emby_username,
                "emby_user_id": emby_user_id,
                "register_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                "expire_time": expire_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "active"
            }
            
            # 删除已使用的注册码
            del self._register_codes[register_code]
            
            # 保存配置
            self._save_config()
            
            await update.message.reply_text(
                f"✅ 注册成功!\n\n"
                f"👤 Emby用户名: {emby_username}\n"
                f"📅 到期时间: {expire_dt.strftime('%Y-%m-%d')}\n"
                f"⏱️ 有效期: {days}天\n\n"
                f"🔗 Emby服务器: {self._emby_host}\n"
                f"🔑 初始密码: 空(请登录后修改)"
            )
        else:
            await update.message.reply_text(f"❌ 注册失败: {message}")

    async def _cmd_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /info 命令 - 查询用户信息"""
        user_id = update.effective_user.id
        
        if user_id not in self._registered_users:
            await update.message.reply_text("❌ 您还未注册,请使用 /register 注册账号")
            return
        
        info = self._registered_users[user_id]
        
        if info["status"] == "deleted":
            await update.message.reply_text("❌ 您的账户已被删除")
            return
        
        expire_dt = datetime.strptime(info["expire_time"], "%Y-%m-%d %H:%M:%S")
        days_left = (expire_dt - datetime.now()).days
        
        status_text = {
            "active": "✅ 正常",
            "disabled": "⚠️ 已禁用(7天内续费可恢复)",
            "deleted": "❌ 已删除"
        }
        
        await update.message.reply_text(
            f"📊 您的账号信息:\n\n"
            f"👤 Emby用户名: {info['emby_username']}\n"
            f"📅 到期时间: {expire_dt.strftime('%Y-%m-%d')}\n"
            f"⏰ 剩余天数: {days_left}天\n"
            f"📌 状态: {status_text.get(info['status'], '未知')}\n"
            f"📝 注册时间: {info['register_time']}"
        )

    async def _cmd_renew(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /renew 命令 - 续期账号"""
        user_id = update.effective_user.id
        
        if user_id not in self._registered_users:
            await update.message.reply_text("❌ 您还未注册,请使用 /register 注册账号")
            return
        
        if not context.args:
            await update.message.reply_text(
                "❌ 参数错误\n\n"
                "用法: /renew <注册码>\n"
                "示例: /renew ABC123"
            )
            return
        
        register_code = context.args[0]
        
        # 验证注册码
        if register_code not in self._register_codes:
            await update.message.reply_text("❌ 无效的注册码")
            return
        
        days = self._register_codes[register_code]
        info = self._registered_users[user_id]
        
        # 如果账户被禁用,先启用
        if info["status"] == "disabled":
            if self._enable_emby_user(info["emby_user_id"]):
                info["status"] = "active"
        
        # 续期
        current_expire = datetime.strptime(info["expire_time"], "%Y-%m-%d %H:%M:%S")
        
        # 如果已过期,从当前时间开始续期;否则从到期时间续期
        if current_expire < datetime.now():
            new_expire = datetime.now() + timedelta(days=days)
        else:
            new_expire = current_expire + timedelta(days=days)
        
        info["expire_time"] = new_expire.strftime("%Y-%m-%d %H:%M:%S")
        
        # 删除已使用的注册码
        del self._register_codes[register_code]
        
        # 保存配置
        self._save_config()
        
        await update.message.reply_text(
            f"✅ 续期成功!\n\n"
            f"📅 新到期时间: {new_expire.strftime('%Y-%m-%d')}\n"
            f"➕ 增加天数: {days}天"
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /help 命令"""
        help_text = (
            "📚 命令列表:\n\n"
            "👥 用户命令:\n"
            "/start - 启动机器人\n"
            "/register <用户名> <注册码> - 注册新账号\n"
            "/info - 查询账号信息\n"
            "/renew <注册码> - 续期账号\n"
            "/help - 显示帮助信息\n\n"
        )
        
        if update.effective_user.id in self._admin_user_ids:
            help_text += (
                "🔧 管理员命令:\n"
                "/admin - 管理面板\n"
                "/addcode <注册码> <天数> - 添加注册码\n"
            )
        
        await update.message.reply_text(help_text)

    async def _cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /admin 命令 - 管理员面板"""
        if update.effective_user.id not in self._admin_user_ids:
            await update.message.reply_text("❌ 您没有管理员权限")
            return
        
        total_users = len([u for u in self._registered_users.values() if u["status"] != "deleted"])
        active_users = len([u for u in self._registered_users.values() if u["status"] == "active"])
        disabled_users = len([u for u in self._registered_users.values() if u["status"] == "disabled"])
        total_codes = len(self._register_codes)
        
        # 列出所有用户
        user_list = "📋 用户列表:\n\n"
        for tg_id, info in self._registered_users.items():
            if info["status"] == "deleted":
                continue
            
            expire_dt = datetime.strptime(info["expire_time"], "%Y-%m-%d %H:%M:%S")
            days_left = (expire_dt - datetime.now()).days
            
            status_emoji = {"active": "✅", "disabled": "⚠️"}
            user_list += (
                f"{status_emoji.get(info['status'], '❓')} {info['tg_username']} "
                f"({info['emby_username']}) - 剩余{days_left}天\n"
            )
        
        # 列出所有注册码
        code_list = "\n\n🎫 可用注册码:\n\n"
        for code, days in self._register_codes.items():
            code_list += f"• {code} - {days}天\n"
        
        await update.message.reply_text(
            f"🔧 管理面板\n\n"
            f"👥 总用户数: {total_users}\n"
            f"✅ 活跃用户: {active_users}\n"
            f"⚠️ 禁用用户: {disabled_users}\n"
            f"🎫 可用注册码: {total_codes}\n\n"
            f"{user_list}"
            f"{code_list}"
        )

    async def _cmd_addcode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /addcode 命令 - 添加注册码"""
        if update.effective_user.id not in self._admin_user_ids:
            await update.message.reply_text("❌ 您没有管理员权限")
            return
        
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ 参数错误\n\n"
                "用法: /addcode <注册码> <天数>\n"
                "示例: /addcode ABC123 30"
            )
            return
        
        code = context.args[0]
        try:
            days = int(context.args[1])
        except:
            await update.message.reply_text("❌ 天数必须是数字")
            return
        
        self._register_codes[code] = days
        self._save_config()
        
        await update.message.reply_text(f"✅ 已添加注册码: {code} ({days}天)")

    async def _button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理按钮回调"""
        query = update.callback_query
        await query.answer()
        
        # 由于改为命令式操作,这里可以移除按钮回调逻辑

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
                
                # 如果有模板用户,复制其配置
                if self._template_user_id:
                    self._copy_user_policy(self._template_user_id, user_id)
                
                return True, user_id, "创建成功"
            else:
                return False, "", f"API返回错误: {response.status_code}"
                
        except Exception as e:
            logger.error(f"创建Emby用户失败: {str(e)}")
            return False, "", str(e)

    def _copy_user_policy(self, template_id: str, target_id: str) -> bool:
        """复制用户策略"""
        try:
            # 获取模板用户策略
            url = f"{self._emby_host}/emby/Users/{template_id}"
            headers = {"X-Emby-Token": self._emby_api_key}
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code != 200:
                return False
            
            template_data = response.json()
            
            # 应用到目标用户
            url = f"{self._emby_host}/emby/Users/{target_id}/Policy"
            policy_data = template_data.get("Policy", {})
            response = requests.post(url, headers=headers, json=policy_data, timeout=10)
            
            return response.status_code == 204
            
        except Exception as e:
            logger.error(f"复制用户策略失败: {str(e)}")
            return False

    def _disable_emby_user(self, user_id: str) -> bool:
        """禁用Emby用户"""
        try:
            url = f"{self._emby_host}/emby/Users/{user_id}/Policy"
            headers = {"X-Emby-Token": self._emby_api_key}
            data = {"IsDisabled": True}
            
            response = requests.post(url, headers=headers, json=data, timeout=10)
            return response.status_code == 204
            
        except Exception as e:
            logger.error(f"禁用Emby用户失败: {str(e)}")
            return False

    def _enable_emby_user(self, user_id: str) -> bool:
        """启用Emby用户"""
        try:
            url = f"{self._emby_host}/emby/Users/{user_id}/Policy"
            headers = {"X-Emby-Token": self._emby_api_key}
            data = {"IsDisabled": False}
            
            response = requests.post(url, headers=headers, json=data, timeout=10)
            return response.status_code == 204
            
        except Exception as e:
            logger.error(f"启用Emby用户失败: {str(e)}")
            return False

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
                                            'placeholder': 'http://emby:8096',
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
                                            'model': 'expire_warning_days',
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
                                'props': {'cols': 12, 'md': 6},
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
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'template_user_id',
                                            'label': 'Emby模板用户ID',
                                            'placeholder': '可选,用于复制权限配置',
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
                                            'label': '注册码配置',
                                            'placeholder': '格式: 注册码,天数\n示例:\nABC123,30\nDEF456,90',
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
                                            'model': 'registered_users',
                                            'label': '已注册用户',
                                            'placeholder': '格式: @TG用户名,TGID,注册时间,剩余天数,Emby用户名\n⚠️ 删除此处的行将同时删除Emby账户!',
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '✨ 本插件使用独立的Telegram Bot,完全不依赖MP通知渠道\n📝 用户通过命令注册: /register <用户名> <注册码>\n⏰ 到期前自动提醒,到期后禁用,7天后删除\n🔧 管理员可通过 /admin 查看所有用户状态'
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
            "admin_user_ids": "",
            "template_user_id": "",
            "expire_warning_days": 3,
            "register_codes": "",
            "registered_users": ""
        }

    def get_page(self) -> List[dict]:
        """获取插件页面"""
        pass

    def stop_service(self):
        """停止插件服务"""
        self._stop_bot()

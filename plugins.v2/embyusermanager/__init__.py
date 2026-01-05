import json
import random
import string
import threading
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.core.event import eventmanager, Event
from app.schemas.types import EventType, NotificationType
from app.log import logger
from app.utils.http import RequestUtils


class EmbyUserManager(_PluginBase):
    # 插件名称
    plugin_name = "Emby用户管理器"
    # 插件描述
    plugin_desc = "通过Telegram Bot实现Emby用户的自动化管理，支持激活码注册、续期等功能。"
    # 插件图标
    plugin_icon = "Emby_A.png"
    # 插件版本
    plugin_version = "0.1"
    # 插件作者
    plugin_author = "Vivi"
    # 作者主页
    author_url = "https://github.com/vivibudong"
    # 插件配置项ID前缀
    plugin_config_prefix = "embyusermanager_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _notify_enabled = False
    _telegram_token = None
    _admin_ids = []
    _emby_host = None
    _emby_apikey = None
    _emby_template_user = None
    _tokens = {}
    _user_bindings = {}
    _scheduler = None
    _lock = threading.Lock()
    _expire_remind_days = [7, 3, 1]
    _auto_delete_expired = False

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        if config:
            self._enabled = config.get("enabled", False)
            self._notify_enabled = config.get("notify_enabled", True)
            self._telegram_token = config.get("telegram_token", "")
            
            admin_ids_str = config.get("admin_ids", "")
            self._admin_ids = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]
            
            self._emby_host = config.get("emby_host", "").rstrip("/")
            self._emby_apikey = config.get("emby_apikey", "")
            self._emby_template_user = config.get("emby_template_user", "")
            
            # 加载tokens和用户绑定
            tokens_str = config.get("tokens", "{}")
            bindings_str = config.get("user_bindings", "{}")
            
            try:
                self._tokens = json.loads(tokens_str) if tokens_str else {}
            except:
                self._tokens = {}
                
            try:
                self._user_bindings = json.loads(bindings_str) if bindings_str else {}
            except:
                self._user_bindings = {}
            
            # 到期提醒天数
            remind_days_str = config.get("expire_remind_days", "7,3,1")
            try:
                self._expire_remind_days = [int(x.strip()) for x in remind_days_str.split(",") if x.strip().isdigit()]
            except:
                self._expire_remind_days = [7, 3, 1]
            
            self._auto_delete_expired = config.get("auto_delete_expired", False)

        # 停止现有任务
        self.stop_service()

        if self._enabled:
            # 启动定时任务
            self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
            
            # 每天检查到期用户
            self._scheduler.add_job(
                func=self._check_expired_users,
                trigger=CronTrigger.from_crontab("0 9 * * *"),
                name="检查到期用户"
            )
            
            self._scheduler.start()
            logger.info(f"Emby用户管理器已启动")

    def get_state(self) -> bool:
        """获取插件状态"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册插件命令"""
        return [
            {
                "cmd": "/user_add",
                "event": EventType.PluginAction,
                "desc": "创建新用户（管理员）",
                "category": "Emby管理",
                "data": {"action": "user_add"}
            },
            {
                "cmd": "/user_del",
                "event": EventType.PluginAction,
                "desc": "删除用户（管理员）",
                "category": "Emby管理",
                "data": {"action": "user_del"}
            },
            {
                "cmd": "/user_list",
                "event": EventType.PluginAction,
                "desc": "查看用户列表（管理员）",
                "category": "Emby管理",
                "data": {"action": "user_list"}
            },
            {
                "cmd": "/token_gen",
                "event": EventType.PluginAction,
                "desc": "生成激活码（管理员）",
                "category": "Emby管理",
                "data": {"action": "token_gen"}
            },
            {
                "cmd": "/token_list",
                "event": EventType.PluginAction,
                "desc": "查看激活码列表（管理员）",
                "category": "Emby管理",
                "data": {"action": "token_list"}
            },
            {
                "cmd": "/renew_gen",
                "event": EventType.PluginAction,
                "desc": "生成续期码（管理员）",
                "category": "Emby管理",
                "data": {"action": "renew_gen"}
            },
            {
                "cmd": "/renew_user",
                "event": EventType.PluginAction,
                "desc": "直接为用户续期（管理员）",
                "category": "Emby管理",
                "data": {"action": "renew_user"}
            },
            {
                "cmd": "/register",
                "event": EventType.PluginAction,
                "desc": "注册账户",
                "category": "Emby用户",
                "data": {"action": "register"}
            },
            {
                "cmd": "/renew",
                "event": EventType.PluginAction,
                "desc": "使用续期码续期",
                "category": "Emby用户",
                "data": {"action": "renew"}
            },
            {
                "cmd": "/my_info",
                "event": EventType.PluginAction,
                "desc": "查看我的信息",
                "category": "Emby用户",
                "data": {"action": "my_info"}
            }
        ]

def get_api(self) -> List[Dict[str, Any]]:
    """注册API"""
    return [
        {
            "path": "/clear_logs",
            "endpoint": self.clear_logs,
            "methods": ["GET"],
            "summary": "清空插件日志",
            "description": "清空Emby用户管理器的所有日志记录"
        }
    ]

    def clear_logs(self):
        """清空插件日志的API接口"""
        try:
            # 这里清空你想清空的数据
            # 例如:清空激活码使用记录、用户操作历史等
            
            with self._lock:
                # 示例1:清空所有已使用的激活码记录
                self._tokens = {k: v for k, v in self._tokens.items() if v.get("status") == "unused"}
                
                # 示例2:清空所有用户的续期历史
                for user_id, info in self._user_bindings.items():
                    if "renew_history" in info:
                        info["renew_history"] = []
                
                self._save_data()
            
            logger.info("插件日志已清空")
            return {
                "code": 0,
                "message": "日志清空成功",
                "data": None
            }
        except Exception as e:
            logger.error(f"清空日志失败: {str(e)}")
            return {
                "code": 1,
                "message": f"清空失败: {str(e)}",
                "data": None
            }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify_enabled',
                                            'label': '发送通知',
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
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'telegram_token',
                                            'label': 'Telegram Bot Token',
                                            'placeholder': '123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11'
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
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'admin_ids',
                                            'label': '管理员Telegram ID',
                                            'placeholder': '123456789,987654321（逗号分隔）'
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
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'emby_host',
                                            'label': 'Emby服务器地址',
                                            'placeholder': 'https://emby.example.com'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'emby_apikey',
                                            'label': 'Emby API Key',
                                            'placeholder': 'xxxxxxxxxxxxxxxx'
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
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'emby_template_user',
                                            'label': 'Emby模板用户ID',
                                            'placeholder': 'xxxxxxxxxxxxxxxx'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'expire_remind_days',
                                            'label': '到期提醒天数',
                                            'placeholder': '7,3,1（逗号分隔）'
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
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'auto_delete_expired',
                                            'label': '自动删除过期用户',
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
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'tokens',
                                            'label': '激活码数据（JSON格式，请勿手动修改）',
                                            'rows': 5,
                                            'readonly': True
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
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'user_bindings',
                                            'label': '用户绑定数据（JSON格式，请勿手动修改）',
                                            'rows': 5,
                                            'readonly': True
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
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '使用说明：\n'
                                                    '1. 在BotFather中创建Bot并获取Token\n'
                                                    '2. 获取你的Telegram ID（可通过 @userinfobot）\n'
                                                    '3. 在Emby中创建一个模板用户，配置好权限和媒体库访问\n'
                                                    '4. 获取模板用户ID（在Emby用户管理页面的URL中）\n'
                                                    '5. 配置完成后，在Telegram中向Bot发送命令即可'
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
            "notify_enabled": True,
            "telegram_token": "",
            "admin_ids": "",
            "emby_host": "",
            "emby_apikey": "",
            "emby_template_user": "",
            "expire_remind_days": "7,3,1",
            "auto_delete_expired": False,
            "tokens": "{}",
            "user_bindings": "{}"
        }

    def get_page(self) -> List[dict]:
        """插件详情页面"""
        return [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VBtn',
                                'props': {
                                    'color': 'error',
                                    'variant': 'outlined',
                                    'text': '清空插件日志'
                                },
                                'events': {
                                    'click': {
                                        'api': 'plugin/EmbyUserManager/clear_logs',
                                        'method': 'get'
                                    }
                                }
                            }
                        ]
                    }
                ]
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event):
        """
        处理插件命令
        """
        if not event or not event.event_data:
            return
            
        event_data = event.event_data
        
        # 添加这行调试日志,查看实际的事件数据
        logger.info(f"事件数据内容: {event_data}")
        
        action = event_data.get("action")
        
        if not action:
            return
        
        # 获取用户信息
        user_id = str(event_data.get("user")) if event_data.get("user") else None
        username = event_data.get("username", "")
        args = event_data.get("args", "")
        
        logger.info(f"收到命令: {action}, 用户: {user_id}, 参数: {args}")
        logger.info(f"事件数据内容: {event_data}")
        
        # 检查是否为管理员
        is_admin = int(user_id) in self._admin_ids if user_id else False
        
        # 处理命令
        if action == "register":
            self._handle_register(user_id, username, args)
        elif action == "renew":
            self._handle_renew(user_id, username, args)
        elif action == "my_info":
            self._handle_my_info(user_id, username)
        elif is_admin:
            if action == "user_add":
                self._handle_user_add(user_id, args)
            elif action == "user_del":
                self._handle_user_del(user_id, args)
            elif action == "user_list":
                self._handle_user_list(user_id)
            elif action == "token_gen":
                self._handle_token_gen(user_id, args)
            elif action == "token_list":
                self._handle_token_list(user_id)
            elif action == "renew_gen":
                self._handle_renew_gen(user_id, args)
            elif action == "renew_user":
                self._handle_renew_user(user_id, args)
        else:
            self._send_message(user_id, "⚠️ 无权限执行此操作")

    def _handle_register(self, user_id: str, username: str, args: str):
        """处理用户注册"""
        if not args:
            self._send_message(user_id, "❌ 请提供激活码\n用法: /register <激活码>")
            return
        
        token = args.strip()
        
        # 检查用户是否已注册
        if user_id in self._user_bindings:
            self._send_message(user_id, "❌ 您已经注册过了，请使用 /my_info 查看信息")
            return
        
        # 验证激活码
        with self._lock:
            if token not in self._tokens:
                self._send_message(user_id, "❌ 激活码不存在")
                return
            
            token_info = self._tokens[token]
            
            if token_info.get("type") != "register":
                self._send_message(user_id, "❌ 这不是注册激活码，请使用注册专用激活码")
                return
            
            if token_info.get("status") != "unused":
                self._send_message(user_id, "❌ 激活码已被使用")
                return
            
            # 生成用户名和密码
            emby_username = f"user_{user_id}"
            emby_password = self._generate_password()
            
            # 计算到期时间
            days = token_info.get("days", 30)
            expire_at = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
            
            # 创建Emby用户
            success, emby_user_id = self._create_emby_user(emby_username, emby_password)
            
            if not success:
                self._send_message(user_id, "❌ 创建Emby用户失败，请联系管理员")
                return
            
            # 保存用户绑定
            self._user_bindings[user_id] = {
                "telegram_id": user_id,
                "telegram_username": username,
                "emby_username": emby_username,
                "emby_user_id": emby_user_id,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "expire_at": expire_at,
                "renew_history": [
                    {
                        "renew_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "days": days,
                        "renew_code": token,
                        "operator": "register"
                    }
                ]
            }
            
            # 标记激活码已使用
            token_info["status"] = "used"
            token_info["used_by_tg_id"] = user_id
            token_info["used_by_emby_username"] = emby_username
            token_info["used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            self._save_data()
        
        # 发送成功消息
        message = (
            f"✅ 激活成功！\n\n"
            f"📝 账户信息：\n"
            f"用户名: {emby_username}\n"
            f"密码: {emby_password}\n"
            f"到期时间: {expire_at}\n\n"
            f"🌐 Emby登录地址:\n{self._emby_host}\n\n"
            f"⚠️ 首次登录后请及时修改密码！\n"
            f"💡 使用 /my_info 查看账户信息"
        )
        self._send_message(user_id, message)
        
        # 通知管理员
        if self._notify_enabled:
            admin_msg = f"📢 新用户注册\n用户: {emby_username}\nTelegram: @{username}"
            for admin_id in self._admin_ids:
                self._send_message(str(admin_id), admin_msg)

    def _handle_renew(self, user_id: str, username: str, args: str):
        """处理用户续期"""
        if not args:
            self._send_message(user_id, "❌ 请提供续期码\n用法: /renew <续期码>")
            return
        
        token = args.strip()
        
        # 检查用户是否已注册
        if user_id not in self._user_bindings:
            self._send_message(user_id, "❌ 您还未注册，请先使用 /register <激活码> 注册")
            return
        
        # 验证续期码
        with self._lock:
            if token not in self._tokens:
                self._send_message(user_id, "❌ 续期码不存在")
                return
            
            token_info = self._tokens[token]
            
            if token_info.get("type") != "renew":
                self._send_message(user_id, "❌ 这不是续期码，请使用续期专用续期码")
                return
            
            if token_info.get("status") != "unused":
                self._send_message(user_id, "❌ 续期码已被使用")
                return
            
            # 获取用户信息
            user_info = self._user_bindings[user_id]
            old_expire = user_info.get("expire_at")
            
            # 计算新的到期时间
            days = token_info.get("days", 30)
            old_expire_dt = datetime.strptime(old_expire, "%Y-%m-%d")
            
            # 如果已过期，从今天开始计算，否则从原到期时间开始计算
            if old_expire_dt < datetime.now():
                new_expire_dt = datetime.now() + timedelta(days=days)
            else:
                new_expire_dt = old_expire_dt + timedelta(days=days)
            
            new_expire = new_expire_dt.strftime("%Y-%m-%d")
            
            # 更新用户信息
            user_info["expire_at"] = new_expire
            
            # 添加续期历史
            if "renew_history" not in user_info:
                user_info["renew_history"] = []
            
            user_info["renew_history"].append({
                "renew_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "days": days,
                "renew_code": token,
                "operator": "self"
            })
            
            # 标记续期码已使用
            token_info["status"] = "used"
            token_info["used_by_tg_id"] = user_id
            token_info["used_by_emby_username"] = user_info.get("emby_username")
            token_info["used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            self._save_data()
        
        # 发送成功消息
        message = (
            f"✅ 续期成功！\n\n"
            f"📝 续期信息：\n"
            f"用户名: {user_info.get('emby_username')}\n"
            f"原到期时间: {old_expire}\n"
            f"新到期时间: {new_expire}\n"
            f"延长天数: {days}天\n\n"
            f"感谢您的续费！"
        )
        self._send_message(user_id, message)

    def _handle_my_info(self, user_id: str, username: str):
        """查看个人信息"""
        if user_id not in self._user_bindings:
            self._send_message(user_id, "❌ 您还未注册，请先使用 /register <激活码> 注册")
            return
        
        user_info = self._user_bindings[user_id]
        expire_at = user_info.get("expire_at")
        expire_dt = datetime.strptime(expire_at, "%Y-%m-%d")
        days_left = (expire_dt - datetime.now()).days
        
        status = "✅ 正常" if days_left > 0 else "❌ 已过期"
        
        message = (
            f"📊 您的账户信息\n\n"
            f"用户名: {user_info.get('emby_username')}\n"
            f"创建时间: {user_info.get('created_at')}\n"
            f"到期时间: {expire_at}\n"
            f"剩余天数: {days_left}天\n"
            f"账户状态: {status}\n"
            f"Telegram绑定: @{username}\n\n"
        )
        
        if days_left <= 7 and days_left > 0:
            message += "⚠️ 账户即将到期，如需续期请联系管理员获取续期码\n使用方式: /renew <续期码>"
        
        self._send_message(user_id, message)

    def _handle_token_gen(self, admin_id: str, args: str):
        """生成激活码（管理员）"""
        try:
            days = int(args.strip()) if args else 30
        except:
            self._send_message(admin_id, "❌ 参数错误\n用法: /token_gen <天数>")
            return
        
        token = self._generate_token()
        
        with self._lock:
            self._tokens[token] = {
                "token": token,
                "type": "register",
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "days": days,
                "status": "unused",
                "used_by_tg_id": None,
                "used_by_emby_username": None,
                "used_at": None
            }
            self._save_data()
        
        message = (
            f"✅ 续期码生成成功！\n\n"
            f"续期码: `{token}`\n"
            f"续期天数: {days}天\n"
            f"状态: 未使用\n\n"
            f"用户使用方式: /renew {token}"
        )
        self._send_message(admin_id, message)

    def _handle_renew_user(self, admin_id: str, args: str):
        """直接为用户续期（管理员）"""
        parts = args.strip().split()
        if len(parts) < 2:
            self._send_message(admin_id, "❌ 参数错误\n用法: /renew_user <用户名> <天数>")
            return
        
        emby_username = parts[0]
        try:
            days = int(parts[1])
        except:
            self._send_message(admin_id, "❌ 天数必须是数字")
            return
        
        # 查找用户
        user_info = None
        user_tg_id = None
        
        with self._lock:
            for tg_id, info in self._user_bindings.items():
                if info.get("emby_username") == emby_username:
                    user_info = info
                    user_tg_id = tg_id
                    break
            
            if not user_info:
                self._send_message(admin_id, f"❌ 未找到用户: {emby_username}")
                return
            
            # 计算新的到期时间
            old_expire = user_info.get("expire_at")
            old_expire_dt = datetime.strptime(old_expire, "%Y-%m-%d")
            
            if old_expire_dt < datetime.now():
                new_expire_dt = datetime.now() + timedelta(days=days)
            else:
                new_expire_dt = old_expire_dt + timedelta(days=days)
            
            new_expire = new_expire_dt.strftime("%Y-%m-%d")
            
            # 更新用户信息
            user_info["expire_at"] = new_expire
            
            if "renew_history" not in user_info:
                user_info["renew_history"] = []
            
            user_info["renew_history"].append({
                "renew_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "days": days,
                "renew_code": "admin_direct",
                "operator": "admin"
            })
            
            self._save_data()
        
        # 通知管理员
        message = (
            f"✅ 续期成功！\n\n"
            f"用户: {emby_username}\n"
            f"原到期时间: {old_expire}\n"
            f"新到期时间: {new_expire}\n"
            f"延长天数: {days}天"
        )
        self._send_message(admin_id, message)
        
        # 通知用户
        if user_tg_id and self._notify_enabled:
            user_message = (
                f"🎉 您的账户已续期！\n\n"
                f"用户名: {emby_username}\n"
                f"新到期时间: {new_expire}\n"
                f"延长天数: {days}天\n\n"
                f"感谢您的支持！"
            )
            self._send_message(user_tg_id, user_message)

    def _handle_user_add(self, admin_id: str, args: str):
        """创建用户（管理员）"""
        parts = args.strip().split()
        if not parts:
            self._send_message(admin_id, "❌ 参数错误\n用法: /user_add <用户名> [天数]")
            return
        
        emby_username = parts[0]
        days = int(parts[1]) if len(parts) > 1 else 30
        
        # 生成密码
        emby_password = self._generate_password()
        
        # 创建Emby用户
        success, emby_user_id = self._create_emby_user(emby_username, emby_password)
        
        if not success:
            self._send_message(admin_id, "❌ 创建Emby用户失败")
            return
        
        # 计算到期时间
        expire_at = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        
        message = (
            f"✅ 用户创建成功！\n\n"
            f"用户名: {emby_username}\n"
            f"密码: {emby_password}\n"
            f"到期时间: {expire_at}\n"
            f"Emby用户ID: {emby_user_id}"
        )
        self._send_message(admin_id, message)

    def _handle_user_del(self, admin_id: str, args: str):
        """删除用户（管理员）"""
        if not args:
            self._send_message(admin_id, "❌ 请提供用户名\n用法: /user_del <用户名>")
            return
        
        emby_username = args.strip()
        
        # 查找用户
        user_tg_id = None
        with self._lock:
            for tg_id, info in self._user_bindings.items():
                if info.get("emby_username") == emby_username:
                    user_tg_id = tg_id
                    emby_user_id = info.get("emby_user_id")
                    break
            
            if not user_tg_id:
                self._send_message(admin_id, f"❌ 未找到用户: {emby_username}")
                return
            
            # 删除Emby用户
            if self._delete_emby_user(emby_user_id):
                del self._user_bindings[user_tg_id]
                self._save_data()
                self._send_message(admin_id, f"✅ 用户 {emby_username} 已删除")
            else:
                self._send_message(admin_id, f"❌ 删除Emby用户失败")

    def _handle_user_list(self, admin_id: str):
        """查看用户列表（管理员）"""
        if not self._user_bindings:
            self._send_message(admin_id, "📋 当前没有用户")
            return
        
        users = []
        for tg_id, info in self._user_bindings.items():
            expire_at = info.get("expire_at")
            expire_dt = datetime.strptime(expire_at, "%Y-%m-%d")
            days_left = (expire_dt - datetime.now()).days
            status = "✅" if days_left > 0 else "❌"
            
            users.append(f"{status} {info.get('emby_username')} (剩余{days_left}天)")
        
        message = "📋 用户列表\n\n" + "\n".join(users[:20])
        self._send_message(admin_id, message)

    def _check_expired_users(self):
        """检查到期用户（定时任务）"""
        logger.info("开始检查到期用户")
        
        with self._lock:
            for tg_id, info in self._user_bindings.items():
                expire_at = info.get("expire_at")
                expire_dt = datetime.strptime(expire_at, "%Y-%m-%d")
                days_left = (expire_dt - datetime.now()).days
                
                # 发送到期提醒
                if days_left in self._expire_remind_days and self._notify_enabled:
                    message = (
                        f"⚠️ 账户到期提醒\n\n"
                        f"您的账户即将到期：\n"
                        f"用户名: {info.get('emby_username')}\n"
                        f"到期时间: {expire_at}\n"
                        f"剩余天数: {days_left}天\n\n"
                        f"💡 续期方式：\n"
                        f"1. 联系管理员获取续期码\n"
                        f"2. 使用 /renew <续期码> 进行续期"
                    )
                    self._send_message(tg_id, message)
                
                # 自动删除过期用户
                if days_left < 0 and self._auto_delete_expired:
                    emby_user_id = info.get("emby_user_id")
                    if self._delete_emby_user(emby_user_id):
                        del self._user_bindings[tg_id]
                        logger.info(f"自动删除过期用户: {info.get('emby_username')}")

            self._save_data()

    # ========== Emby API 相关方法 ==========
    
    def _create_emby_user(self, username: str, password: str) -> Tuple[bool, Optional[str]]:
        """创建Emby用户"""
        if not self._emby_host or not self._emby_apikey:
            logger.error("Emby配置不完整")
            return False, None
        
        url = f"{self._emby_host}/emby/Users/New"
        headers = {"X-Emby-Token": self._emby_apikey}
        
        data = {
            "Name": username,
            "Password": password
        }
        
        try:
            res = RequestUtils(headers=headers).post_res(url, json=data)
            if res and res.status_code == 200:
                user_data = res.json()
                user_id = user_data.get("Id")
                
                # 如果有模板用户，复制权限
                if self._emby_template_user:
                    self._copy_user_policy(user_id, self._emby_template_user)
                
                logger.info(f"Emby用户创建成功: {username}, ID: {user_id}")
                return True, user_id
            else:
                logger.error(f"创建Emby用户失败: {res.status_code if res else 'No response'}")
                return False, None
        except Exception as e:
            logger.error(f"创建Emby用户异常: {str(e)}")
            return False, None

    def _delete_emby_user(self, user_id: str) -> bool:
        """删除Emby用户"""
        if not self._emby_host or not self._emby_apikey:
            return False
        
        url = f"{self._emby_host}/emby/Users/{user_id}"
        headers = {"X-Emby-Token": self._emby_apikey}
        
        try:
            res = RequestUtils(headers=headers).delete_res(url)
            if res and res.status_code in [200, 204]:
                logger.info(f"Emby用户删除成功: {user_id}")
                return True
            else:
                logger.error(f"删除Emby用户失败: {res.status_code if res else 'No response'}")
                return False
        except Exception as e:
            logger.error(f"删除Emby用户异常: {str(e)}")
            return False

    def _copy_user_policy(self, target_user_id: str, template_user_id: str):
        """复制用户权限"""
        if not self._emby_host or not self._emby_apikey:
            return
        
        # 获取模板用户的策略
        url = f"{self._emby_host}/emby/Users/{template_user_id}"
        headers = {"X-Emby-Token": self._emby_apikey}
        
        try:
            res = RequestUtils(headers=headers).get_res(url)
            if res and res.status_code == 200:
                template_data = res.json()
                policy = template_data.get("Policy", {})
                
                # 应用到目标用户
                update_url = f"{self._emby_host}/emby/Users/{target_user_id}/Policy"
                res = RequestUtils(headers=headers).post_res(update_url, json=policy)
                
                if res and res.status_code == 200:
                    logger.info(f"用户权限复制成功: {target_user_id}")
        except Exception as e:
            logger.error(f"复制用户权限异常: {str(e)}")

    # ========== 工具方法 ==========
    
    def _generate_token(self, prefix: str = "TOKEN") -> str:
        """生成激活码"""
        chars = string.ascii_uppercase + string.digits
        return prefix + ''.join(random.choices(chars, k=9))

    def _generate_password(self, length: int = 12) -> str:
        """生成随机密码"""
        chars = string.ascii_letters + string.digits
        return ''.join(random.choices(chars, k=length))

    def _send_message(self, user_id: str, message: str):
        """发送Telegram消息"""
        if not self._telegram_token:
            logger.warning("未配置Telegram Bot Token")
            return
        
        # 直接调用Telegram Bot API发送消息
        url = f"https://api.telegram.org/bot{self._telegram_token}/sendMessage"
        
        data = {
            "chat_id": user_id,
            "text": message,
            "parse_mode": "Markdown"  # 支持Markdown格式
        }
    
    try:
        res = RequestUtils().post_res(url, json=data)
        if res and res.status_code == 200:
            logger.info(f"Telegram消息发送成功: {user_id}")
        else:
            logger.error(f"Telegram消息发送失败: {res.status_code if res else 'No response'}")
    except Exception as e:
        logger.error(f"发送Telegram消息异常: {str(e)}")

    def _save_data(self):
        """保存数据到配置"""
        config = self.get_config()
        config["tokens"] = json.dumps(self._tokens, ensure_ascii=False, indent=2)
        config["user_bindings"] = json.dumps(self._user_bindings, ensure_ascii=False, indent=2)
        self.update_config(config)

    def stop_service(self):
        """停止插件"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
            logger.info("Emby用户管理器已停止")
        except Exception as e:
            logger.error(f"停止服务失败: {str(e)}")

    def _handle_token_list(self, admin_id: str):
        """查看激活码列表（管理员）"""
        if not self._tokens:
            self._send_message(admin_id, "📋 当前没有激活码")
            return
        
        unused_tokens = []
        used_tokens = []
        
        for token, info in self._tokens.items():
            if info.get("status") == "unused":
                unused_tokens.append(f"`{token}` - {info.get('type')} - {info.get('days')}天")
            else:
                used_tokens.append(f"`{token}` - 已使用 - {info.get('used_by_emby_username')}")
        
        message = "📋 激活码列表\n\n"
        
        if unused_tokens:
            message += "🟢 未使用:\n" + "\n".join(unused_tokens[:10]) + "\n\n"
        
        if used_tokens:
            message += "🔴 已使用:\n" + "\n".join(used_tokens[:10])
        
        self._send_message(admin_id, message)

    def _handle_renew_gen(self, admin_id: str, args: str):
        """生成续期码（管理员）"""
        try:
            days = int(args.strip()) if args else 30
        except:
            self._send_message(admin_id, "❌ 参数错误\n用法: /renew_gen <天数>")
            return
        
        token = self._generate_token("RENEW")
        
        with self._lock:
            self._tokens[token] = {
                "token": token,
                "type": "renew",
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "days": days,
                "status": "unused",
                "used_by_tg_id": None,
                "used_by_emby_username": None,
                "used_at": None
            }
            self._save_data()
        
        message = (
            f"✅ 续期码生成成功！\n\n"
            f"续期码: `{token}`\n"
            f"续期天数: {days}天\n"
            f"状态: 未使用\n\n"
            f"用户使用方式: /renew {token}"
        )
        self._send_message(admin_id, message)

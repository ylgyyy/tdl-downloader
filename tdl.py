import re
import json
import subprocess
import os
import threading
import time
import telebot
import pexpect
from time import sleep
from functools import wraps
from telebot.types import BotCommand
from download_queue import DownloadTask, DownloadQueue

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SUPER_ADMIN_RAW = os.environ.get("SUPER_ADMIN", "")
DL_BASE_PATH = os.environ.get("DL_BASE_PATH", "").rstrip("/")
if not DL_BASE_PATH:
    print("❌ 未设置 DL_BASE_PATH 环境变量！请在 docker-compose.yml 中配置下载目录。")
    exit(1)
TDL_PROXY = os.environ.get("TDL_PROXY", "")  # 代理，如 socks5://192.168.31.2:7891
MAX_CONCURRENT_DL = int(os.environ.get("MAX_CONCURRENT_DL", "2") or 2)

if not BOT_TOKEN or not SUPER_ADMIN_RAW:
    print("❌ 未设置 BOT_TOKEN 或 SUPER_ADMIN 环境变量！")
    print("   示例: BOT_TOKEN=123456:ABCdef... SUPER_ADMIN=987654321 docker compose up -d")
    exit(1)

try:
    SUPER_ADMIN = int(SUPER_ADMIN_RAW)
except ValueError:
    print(f"❌ SUPER_ADMIN 必须是纯数字！当前值: {SUPER_ADMIN_RAW}")
    exit(1)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# 持久化文件路径
DATA_DIR = "data"
ADMIN_FILE = os.path.join(DATA_DIR, "admins.json")
TDL_ACCOUNTS_FILE = os.path.join(DATA_DIR, "tdl_accounts.json")
USER_CURRENT_TDL_FILE = os.path.join(DATA_DIR, "user_current_tdl.json")
USER_DL_EXT_FILE = os.path.join(DATA_DIR, "user_dl_ext.json")

# 临时存储用户步骤
user_steps = {}

# 存储正在登录的子进程 {chat_id: pexpect_child}
active_logins = {}

# 当前正在运行、由本程序启动的 tdl 子进程 PID（_kill_stale_tdl 会跳过这些，避免误杀）
_running_tdl_pids = set()

# TDL 账号配置（二维字典，支持多用户私有隔离）
# 结构: { "chat_id_1": ["acc1", "acc2"], "chat_id_2": ["acc3"] }
TDL_ACCOUNTS = {}
# 用户当前使用的 TDL 账号
# 结构: { "chat_id_1": "acc1", "chat_id_2": "acc3" }
user_current_tdl = {}
# 管理员列表
ADMIN_LIST = []
# 用户下载扩展名白名单 {"chat_id": "mp4,jpg"}
user_dl_ext = {}
DEFAULT_DL_EXT = ""  # 空=全部下载

# 类型选项
DL_EXT_OPTIONS = {
    "📷 图片": ["jpg", "png", "gif", "webp", "bmp", "svg", "tiff"],
    "🎬 视频": ["mp4", "mkv", "avi", "mov", "wmv", "flv", "ts", "webm", "m4v"],
    "📄 文档": ["pdf", "doc", "docx", "txt", "zip", "rar"],
}

# 初始化数据（从文件加载）
def load_data():
    """加载持久化数据"""
    # 确保数据目录存在
    os.makedirs(DATA_DIR, exist_ok=True)
    # 加载管理员列表
    global ADMIN_LIST
    if os.path.exists(ADMIN_FILE):
        try:
            with open(ADMIN_FILE, "r", encoding="utf-8") as f:
                ADMIN_LIST = json.load(f)
        except Exception:
            ADMIN_LIST = []
    else:
        ADMIN_LIST = []

    # 加载TDL账号列表
    global TDL_ACCOUNTS
    if os.path.exists(TDL_ACCOUNTS_FILE):
        try:
            with open(TDL_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                TDL_ACCOUNTS = json.load(f)
        except Exception:
            TDL_ACCOUNTS = {}
    else:
        TDL_ACCOUNTS = {}

    # 加载用户当前TDL账号
    global user_current_tdl
    if os.path.exists(USER_CURRENT_TDL_FILE):
        try:
            with open(USER_CURRENT_TDL_FILE, "r", encoding="utf-8") as f:
                user_current_tdl = json.load(f)
        except Exception:
            user_current_tdl = {}
    else:
        user_current_tdl = {}

    # 加载用户下载扩展名
    global user_dl_ext
    if os.path.exists(USER_DL_EXT_FILE):
        try:
            with open(USER_DL_EXT_FILE, "r", encoding="utf-8") as f:
                user_dl_ext = json.load(f)
        except Exception:
            user_dl_ext = {}
    else:
        user_dl_ext = {}

def save_admins():
    """保存管理员列表到文件"""
    try:
        with open(ADMIN_FILE, "w", encoding="utf-8") as f:
            json.dump(ADMIN_LIST, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存管理员列表失败：{e}")

def save_tdl_accounts():
    """保存TDL账号列表到文件"""
    try:
        with open(TDL_ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(TDL_ACCOUNTS, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存TDL账号失败：{e}")

def save_user_current_tdl():
    """保存用户当前TDL账号到文件"""
    try:
        with open(USER_CURRENT_TDL_FILE, "w", encoding="utf-8") as f:
            json.dump(user_current_tdl, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存用户TDL账号失败：{e}")

def save_user_dl_ext():
    """保存用户下载扩展名到文件"""
    try:
        with open(USER_DL_EXT_FILE, "w", encoding="utf-8") as f:
            json.dump(user_dl_ext, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存下载类型失败：{e}")

def get_user_ext(chat_id):
    """获取用户的下载扩展名白名单，空字符串=全部"""
    return user_dl_ext.get(str(chat_id), DEFAULT_DL_EXT)

# 初始化加载数据
load_data()

# ==========================
# 【核心】设置 Telegram 左下角固定菜单
# ==========================
def set_bot_commands():
    commands = [
        BotCommand("start", "🏠 主菜单"),
        BotCommand("single", "📥 单文件下载"),
        BotCommand("multi", "📥 批量下载"),
        BotCommand("cancel", "❌ 取消"),
        BotCommand("user", "👤 用户管理"),
        BotCommand("tdl_list", "📜 查看TDL账号列表"),
    ]
    bot.set_my_commands(commands)
    print("✅ 左下角固定菜单设置完成")

# ==========================
# 工具函数
# ==========================
def _kill_stale_tdl():
    """杀掉孤儿 tdl 进程（本程序当前任务之外的），释放数据库锁，避免误杀进行中的下载/登录"""
    # 收集本程序当前仍存活的 tdl 进程 PID，跳过它们
    protected = set(_running_tdl_pids)
    protected |= queue.running_pids()
    for child in list(active_logins.values()):
        try:
            if child.isalive():
                protected.add(child.pid)
        except Exception:
            pass

    try:
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            pid = int(p)
            if pid in protected:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read()
                if b"tdl" in cmdline:
                    os.kill(pid, 9)
            except Exception:
                pass
    except Exception:
        pass

def extract_channel_and_msg_id_from_link(link):
    link = link.strip()
    pattern = r"https://t\.me/(c/)?(\d+|[a-zA-Z0-9_]+)/(\d+)(/.*)?"
    match = re.match(pattern, link)
    if match:
        channel_id = match.group(2)
        msg_id = match.group(3)
        return channel_id, msg_id
    else:
        return None, None

def run_command(argv, chat_id):
    cid_str = str(chat_id)
    tdl_name = user_current_tdl.get(cid_str)
    user_accounts = TDL_ACCOUNTS.get(cid_str, [])

    if not tdl_name and user_accounts:
        tdl_name = user_accounts[0]

    if not tdl_name:
        return False, "", "❌ 严重错误：未找到您的专属 TDL 账号，请先在【用户管理】中登录！", "无"

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        encoding="utf-8",
    )
    _running_tdl_pids.add(proc.pid)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return False, "", "操作超时（5分钟），请减少消息数量重试", tdl_name
        # 合并 stdout/stderr，tdl 可能把错误打到 stdout
        error_msg = (stderr + stdout).strip()
        if not error_msg:
            error_msg = f"退出码: {proc.returncode}"
        return proc.returncode == 0, stdout, error_msg, tdl_name
    except Exception as e:
        return False, "", str(e), tdl_name
    finally:
        _running_tdl_pids.discard(proc.pid)

def check_export_file(file_path):
    if not os.path.exists(file_path):
        return False, "导出文件不存在"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not data.get("messages") or len(data.get("messages", [])) == 0:
            return False, "消息ID范围无有效消息，请更换ID范围"
        return True, "导出成功"
    except Exception:
        return False, "导出文件解析失败"

def _bind_tdl_account(chat_id, tdl_name, success_msg=None):
    """绑定TDL账号到用户并设为当前账号"""
    cid_str = str(chat_id)
    if cid_str not in TDL_ACCOUNTS:
        TDL_ACCOUNTS[cid_str] = []
    if tdl_name not in TDL_ACCOUNTS[cid_str]:
        TDL_ACCOUNTS[cid_str].append(tdl_name)
    save_tdl_accounts()
    user_current_tdl[cid_str] = tdl_name
    save_user_current_tdl()
    bot.send_message(chat_id, success_msg or f"✅ 登录成功！账号 @{tdl_name} 已绑定并设为您当前的专属账号。")

def _cleanup_login(chat_id):
    """清理登录会话"""
    if chat_id in active_logins:
        active_logins[chat_id].close(force=True)
        del active_logins[chat_id]
    if chat_id in user_steps:
        del user_steps[chat_id]

# ==========================
# 权限装饰器
# ==========================
def super_admin_required(func):
    @wraps(func)
    def wrapper(msg):
        if msg.from_user.id == SUPER_ADMIN:
            return func(msg)
        else:
            bot.send_message(msg.chat.id, "❌ 你无权限执行此操作！")
    return wrapper

def admin_required(func):
    @wraps(func)
    def wrapper(msg):
        user_id = msg.from_user.id
        if user_id == SUPER_ADMIN or user_id in ADMIN_LIST:
            return func(msg)
        else:
            bot.send_message(msg.chat.id, "❌ 你无权限使用此机器人！")
    return wrapper

# ==========================
# 核心业务逻辑抽离
# ==========================
def start_single_download(chat_id):
    user_steps[chat_id] = {"step": "SINGLE_DL_LINK"}
    bot.send_message(chat_id, "✅ 请输入要下载的消息完整链接（如https://t.me/c/123456/789）：")

def start_multi_download(chat_id):
    user_steps[chat_id] = {"step": "MULTI_DL_START_LINK"}
    bot.send_message(chat_id, "✅ 请输入【起始消息完整链接】（如https://t.me/c/123456/38359）：")

def open_user_manager(chat_id):
    cid_str = str(chat_id)
    user_accounts = TDL_ACCOUNTS.get(cid_str, [])

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    account_buttons = [telebot.types.KeyboardButton(f"@{name}") for name in user_accounts]
    if account_buttons:
        markup.add(*account_buttons)
    markup.add("🔐 登录新账号", "🔗 绑定现有账号", "🗑️ 删除TDL账号")
    markup.add("📜 查看账号列表", "🔙 返回主菜单")
    bot.send_message(chat_id, "✅ 用户管理面板（私有隔离模式）", reply_markup=markup)

def open_admin_panel(chat_id):
    admin_markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    admin_markup.add("➕ 添加管理员", "➖ 删除管理员", "📋 查看管理员列表", "🔙 返回主菜单")
    bot.send_message(chat_id, "✅ 进入管理员管理面板，请选择操作：", reply_markup=admin_markup)

# ==========================
# 主菜单
# ==========================
def main_menu(chat_id):
    # 回到主菜单时，强制清理所有进行到一半的任务和进程
    user_steps.pop(chat_id, None)
    if chat_id in active_logins:
        active_logins[chat_id].close(force=True)
        del active_logins[chat_id]

    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn1 = telebot.types.KeyboardButton("📥 单文件下载")
    btn2 = telebot.types.KeyboardButton("📥 批量下载")

    markup.add(btn1, btn2)
    ext = get_user_ext(chat_id)
    ext_label = ext if ext else "全部"
    markup.add(telebot.types.KeyboardButton(f"📎 下载类型: {ext_label}"))

    if chat_id == SUPER_ADMIN or chat_id in ADMIN_LIST:
        btn5 = telebot.types.KeyboardButton("👤 用户管理")
        if chat_id == SUPER_ADMIN:
            btn6 = telebot.types.KeyboardButton("⚙️ 管理面板")
            markup.add(btn5, btn6)
        else:
            markup.add(btn5)

    cid_str = str(chat_id)
    current = user_current_tdl.get(cid_str)
    user_accounts = TDL_ACCOUNTS.get(cid_str, [])

    # 智能回退：如果当前没选中账号，但列表里有账号，默认选第一个
    if not current and user_accounts:
        current = user_accounts[0]
        user_current_tdl[cid_str] = current
        save_user_current_tdl()

    display_account = f"@{current}" if current else "暂无 (请先去用户管理登录)"
    bot.send_message(chat_id, f"✅ 主菜单\n您的当前账号：{display_account}", reply_markup=markup)

# ==========================
# 命令与按钮事件路由
# ==========================
@bot.message_handler(commands=['start', 'menu'])
@admin_required
def cmd_start(msg):
    main_menu(msg.chat.id)

@bot.message_handler(commands=['single', 'single_download'])
@admin_required
def cmd_single_download(msg):
    start_single_download(msg.chat.id)

@bot.message_handler(commands=['multi', 'multi_download'])
@admin_required
def cmd_multi_download(msg):
    start_multi_download(msg.chat.id)

@bot.message_handler(commands=['user', 'user_manager'])
@admin_required
def cmd_user_manager(msg):
    open_user_manager(msg.chat.id)

@bot.message_handler(commands=['admin', 'admin_panel'])
@super_admin_required
def cmd_admin_panel(msg):
    open_admin_panel(msg.chat.id)

@bot.message_handler(commands=['tdl_list', 'list_tdl'])
@admin_required
def cmd_list_tdl(msg):
    cid_str = str(msg.chat.id)
    user_accounts = TDL_ACCOUNTS.get(cid_str, [])
    if not user_accounts:
        bot.send_message(msg.chat.id, "📜 您的名下暂无绑定的TDL账号")
    else:
        text = "\n".join([f"✅ @{k}" for k in user_accounts])
        bot.send_message(msg.chat.id, f"📜 您的私有TDL账号列表：\n{text}")
    open_user_manager(msg.chat.id)

@bot.message_handler(commands=['admin_list', 'list_admin'])
@super_admin_required
def cmd_list_admin(msg):
    if not ADMIN_LIST:
        bot.send_message(msg.chat.id, "📋 当前无普通管理员！")
    else:
        admin_str = "\n".join([f"🆔 {admin_id}" for admin_id in ADMIN_LIST])
        bot.send_message(msg.chat.id, f"📋 当前管理员列表：\n{admin_str}")
    open_admin_panel(msg.chat.id)

@bot.message_handler(func=lambda msg: msg.text == "📥 单文件下载")
@admin_required
def btn_single_download(msg):
    start_single_download(msg.chat.id)

@bot.message_handler(func=lambda msg: msg.text == "📥 批量下载")
@admin_required
def btn_multi_download(msg):
    start_multi_download(msg.chat.id)

@bot.message_handler(func=lambda msg: msg.text == "👤 用户管理")
@admin_required
def btn_user_manager(msg):
    open_user_manager(msg.chat.id)

@bot.message_handler(func=lambda msg: msg.text == "⚙️ 管理面板")
@super_admin_required
def btn_admin_panel(msg):
    open_admin_panel(msg.chat.id)

@bot.message_handler(func=lambda msg: msg.text == "🔐 登录新账号")
@admin_required
def login_new_account(msg):
    user_steps[msg.chat.id] = {"step": "LOGIN_NAME"}
    bot.send_message(msg.chat.id, "✅ 请输入你要创建的 TDL 账号名称 (纯英文/数字，如 user1)：")

@bot.message_handler(func=lambda msg: msg.text == "🔗 绑定现有账号")
@admin_required
def add_tdl_account(msg):
    user_steps[msg.chat.id] = {"step": "ADD_TDL_NAME"}
    bot.send_message(msg.chat.id, "✅ 请输入已经在服务器上登录成功的TDL账号名称：")

@bot.message_handler(func=lambda msg: msg.text == "🗑️ 删除TDL账号")
@admin_required
def del_tdl_account(msg):
    user_steps[msg.chat.id] = {"step": "DEL_TDL_NAME"}
    bot.send_message(msg.chat.id, "✅ 请输入要删除的TDL账号名称：")

@bot.message_handler(func=lambda msg: msg.text == "📜 查看账号列表")
@admin_required
def list_tdl_accounts(msg):
    cid_str = str(msg.chat.id)
    user_accounts = TDL_ACCOUNTS.get(cid_str, [])
    if not user_accounts:
        bot.send_message(msg.chat.id, "📜 您的名下暂无绑定的TDL账号")
    else:
        text = "\n".join([f"✅ @{k}" for k in user_accounts])
        bot.send_message(msg.chat.id, f"📜 您的私有TDL账号列表：\n{text}")
    open_user_manager(msg.chat.id)

@bot.message_handler(func=lambda msg: msg.text == "➕ 添加管理员")
@super_admin_required
def add_admin(msg):
    user_steps[msg.chat.id] = {"step": "ADD_ADMIN_ID"}
    bot.send_message(msg.chat.id, "✅ 请输入要添加的管理员ID（数字）：")

@bot.message_handler(func=lambda msg: msg.text == "➖ 删除管理员")
@super_admin_required
def del_admin(msg):
    user_steps[msg.chat.id] = {"step": "DEL_ADMIN_ID"}
    bot.send_message(msg.chat.id, "✅ 请输入要删除的管理员ID（数字）：")

@bot.message_handler(func=lambda msg: msg.text == "📋 查看管理员列表")
@super_admin_required
def list_admins(msg):
    if not ADMIN_LIST:
        bot.send_message(msg.chat.id, "📋 当前无普通管理员！")
    else:
        admin_str = "\n".join([f"🆔 {admin_id}" for admin_id in ADMIN_LIST])
        bot.send_message(msg.chat.id, f"📋 当前管理员列表：\n{admin_str}")
    open_admin_panel(msg.chat.id)

@bot.message_handler(func=lambda msg: msg.text == "🔙 返回主菜单")
@admin_required
def back_to_main(msg):
    main_menu(msg.chat.id)

def _ext_inline_keyboard(chat_id):
    """内联按钮：仅格式选项"""
    ext = get_user_ext(chat_id)
    selected = set(ext.split(",")) if ext else set()
    markup = telebot.types.InlineKeyboardMarkup()
    for group, exts in DL_EXT_OPTIONS.items():
        markup.add(telebot.types.InlineKeyboardButton(group, callback_data="ext_nop"))
        row = []
        for e in exts:
            prefix = "✅ " if e in selected else ""
            row.append(telebot.types.InlineKeyboardButton(f"{prefix}{e}", callback_data=f"ext_toggle_{e}"))
            if len(row) == 5:
                markup.add(*row)
                row = []
        if row:
            markup.add(*row)
    return markup

def _ext_bottom_keyboard(chat_id):
    """底部按钮：全部 / 手动输入 / 关闭"""
    ext = get_user_ext(chat_id)
    all_text = "✅ 全部" if not ext else "☐ 全部"
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(telebot.types.KeyboardButton(all_text), telebot.types.KeyboardButton("✏️ 手动输入"))
    markup.add(telebot.types.KeyboardButton("🔙 关闭"))
    return markup

@bot.message_handler(func=lambda msg: msg.text and msg.text.startswith("📎 下载类型"))
@admin_required
def btn_dl_ext(msg):
    user_steps[msg.chat.id] = {"step": "SELECTING_EXT"}
    bot.send_message(msg.chat.id, "📎 *选择下载类型*（可多选）\n点击格式切换选择", reply_markup=_ext_inline_keyboard(msg.chat.id), parse_mode="HTML")
    bot.send_message(msg.chat.id, "操作：", reply_markup=_ext_bottom_keyboard(msg.chat.id))

@bot.callback_query_handler(func=lambda call: call.data.startswith("ext_"))
def handle_ext_callback(call):
    chat_id = call.message.chat.id
    data = call.data
    bot.answer_callback_query(call.id)

    if data == "ext_nop":
        return  # 分组标签，无操作

    if data == "ext_close":
        return bot.edit_message_text("📎 下载类型设置已关闭", chat_id, call.message.message_id)

    if data == "ext_all":
        user_dl_ext[str(chat_id)] = ""
        save_user_dl_ext()
        return bot.edit_message_text("📎 已设为全部下载", chat_id, call.message.message_id, reply_markup=_ext_inline_keyboard(chat_id))

    if data == "ext_manual":
        user_steps[chat_id] = {"step": "SET_DL_EXT"}
        return bot.edit_message_text("✏️ 请输入扩展名（逗号分隔，如 mp4,jpg,png）：", chat_id, call.message.message_id)

    if data.startswith("ext_toggle_"):
        e = data[len("ext_toggle_"):]
        ext = get_user_ext(chat_id)
        current = set(ext.split(",")) if ext else set()
        if e in current:
            current.discard(e)
        else:
            current.add(e)
        user_dl_ext[str(chat_id)] = ",".join(sorted(current))
        save_user_dl_ext()
        return bot.edit_message_text("📎 *选择下载类型*（可多选）", chat_id, call.message.message_id, reply_markup=_ext_inline_keyboard(chat_id), parse_mode="HTML")

# ==========================
# 单文件下载重命名确认（内联按钮回调）
# ==========================
@bot.callback_query_handler(func=lambda call: call.data in ("rename_yes", "rename_no"))
def handle_rename_confirm(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    data = user_steps.get(chat_id)
    if not data or data.get("step") != "SINGLE_DL_RENAME_CONFIRM":
        bot.edit_message_text("⚠️ 该操作已失效，请重新开始", chat_id, call.message.message_id)
        return

    if call.data == "rename_yes":
        data["step"] = "SINGLE_DL_RENAME_NAME"
        bot.edit_message_text("✅ 需要重命名\n请输入新的文件名（可带扩展名如 我的电影.mp4；不带扩展名则自动保留原扩展名）：", chat_id, call.message.message_id)
    else:  # rename_no
        bot.edit_message_text("✅ 保留原文件名，开始下载...", chat_id, call.message.message_id)
        do_single_download(chat_id, data["channel_id"], data["msg_id"], data["link"])

# ==========================
# 后台下载监控 & 取消
# ==========================
def _format_size(size_bytes):
    """格式化文件大小"""
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _format_file_list(dl_dir, names):
    """生成带大小的文件列表（最多显示 20 个），返回 (文本, 总大小)"""
    lines = []
    total_size = 0
    for f in names:
        try:
            sz = os.path.getsize(os.path.join(dl_dir, f))
        except Exception:
            sz = 0
        total_size += sz
        if len(lines) < 20:
            lines.append(f"{f}  ({_format_size(sz)})")
    if len(names) > 20:
        lines.append(f"... 还有 {len(names) - 20} 个文件")
    return ("\n".join(lines) if lines else "（无可列出的文件）"), total_size

def _cleanup_tmp(dl_dir):
    """删除目录中的 .tmp 残留文件"""
    try:
        for f in os.listdir(dl_dir):
            if f.endswith(".tmp"):
                os.remove(os.path.join(dl_dir, f))
    except Exception:
        pass

def _get_dl_progress(dl_dir):
    """获取当前正在下载的文件大小（监控 .tmp 文件）"""
    try:
        total_size = 0
        tmp_count = 0
        for f in os.listdir(dl_dir):
            if f.endswith(".tmp"):
                total_size += os.path.getsize(os.path.join(dl_dir, f))
                tmp_count += 1
        done = len([f for f in os.listdir(dl_dir) if not f.startswith('.') and not f.endswith('.tmp')])
        return tmp_count, _format_size(total_size), done
    except Exception:
        return 0, "0 KB", 0


def _run_task(task):
    """在独立线程中执行一个下载任务：export → dl → 轮询进度 → 收尾。"""
    label = "单文件下载" if task.kind == "single" else "批量下载"

    # Step 1/2: 导出（先清理残留进程）
    _kill_stale_tdl()
    step_msg = bot.send_message(task.chat_id, f"📥 *{label}*\n\n📡 Step 1/2: 导出消息...", parse_mode="HTML")
    task.step_msg_id = step_msg.message_id

    if task.kind == "single":
        task.export_file = f"single-export_{task.chat_id}_{task.task_id}.json"
        export_argv = ["tdl", "-n", task.tdl_name, "chat", "export", "-c", task.channel_id,
                       "-i", f"{task.msg_id},{task.msg_id}", "-T", "id", "-o", task.export_file]
        total = 1
    else:
        task.export_file = f"dl-export_{task.chat_id}_{task.task_id}.json"
        export_argv = ["tdl", "-n", task.tdl_name, "chat", "export", "-c", task.source_id,
                       "-i", f"{task.start_id},{task.end_id}", "-T", "id", "-o", task.export_file]
        total = 1
    if TDL_PROXY:
        export_argv += ["--proxy", TDL_PROXY]
    success, _, stderr, _ = run_command(export_argv, task.chat_id)

    if task.canceled:
        queue._finish(task)
        return
    if not success:
        bot.edit_message_text(f"❌ 导出失败：{stderr[:200]}", task.chat_id, step_msg.message_id)
        queue._finish(task)
        return

    if task.kind == "multi":
        file_ok, file_msg = check_export_file(task.export_file)
        if not file_ok:
            bot.edit_message_text(f"❌ {file_msg}", task.chat_id, step_msg.message_id)
            if os.path.exists(task.export_file):
                os.remove(task.export_file)
            queue._finish(task)
            return
        try:
            with open(task.export_file, "r", encoding="utf-8") as f:
                total = len(json.load(f).get("messages", []))
        except Exception:
            total = 1

    task.dl_dir = f"{DL_BASE_PATH}/{task.channel_id if task.kind == 'single' else task.source_id}"
    os.makedirs(task.dl_dir, exist_ok=True)
    try:
        task.before_files = set(os.listdir(task.dl_dir))
    except Exception:
        task.before_files = set()

    bot.edit_message_text(f"📥 *{label}*\n\n📡 Step 1/2: ✅\n⬇️ Step 2/2: 下载文件中...",
                          task.chat_id, step_msg.message_id, parse_mode="HTML")

    dl_argv = ["tdl", "-n", task.tdl_name, "dl", "-f", task.export_file, "-t", "16",
               "--pool", "0", "-d", task.dl_dir, "--rewrite-ext", "--reconnect-timeout", "0"]
    ext = get_user_ext(task.chat_id)
    if ext:
        dl_argv += ["-i", ext]
    dl_argv += ["--template", "{{ .FileName }}"]
    if TDL_PROXY:
        dl_argv += ["--proxy", TDL_PROXY]
    task.process = subprocess.Popen(dl_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)

    last_size = 0
    last_time = time.time()
    while task.process.poll() is None and not task.canceled:
        time.sleep(3)
        tmp_count, _, done = _get_dl_progress(task.dl_dir)
        try:
            cur_size = 0
            for f in os.listdir(task.dl_dir):
                if f.endswith(".tmp"):
                    cur_size += os.path.getsize(os.path.join(task.dl_dir, f))
        except Exception:
            cur_size = 0
        now = time.time()
        elapsed = now - last_time
        speed_str = _format_size((cur_size - last_size) / elapsed) + "/s" if elapsed > 0 and cur_size > last_size else "..."
        last_size = cur_size
        last_time = now
        try:
            if tmp_count > 0:
                tail = f"📦 {_format_size(cur_size)}  ⚡ {speed_str}"
                if task.kind == "multi":
                    tail += f"  ✅ {done}/{total}"
                text = f"📥 *{label}*\n\n📡 Step 1/2: ✅\n⬇️ Step 2/2: 下载文件中...\n\n{tail}"
            else:
                text = f"📥 *{label}*\n\n📡 Step 1/2: ✅\n⬇️ Step 2/2: 等待连接..."
            bot.edit_message_text(text, task.chat_id, step_msg.message_id, parse_mode="HTML")
        except Exception:
            pass

    if task.kind == "multi" and os.path.exists(task.export_file):
        os.remove(task.export_file)

    if task.canceled:
        _cleanup_tmp(task.dl_dir)
        queue._finish(task)
        return

    if task.process.returncode == 0:
        if task.kind == "single":
            renamed = _rename_new_files(task.dl_dir, task.before_files, task.rename_name)
            try:
                if renamed is not None:
                    names = renamed
                else:
                    after_files = set(os.listdir(task.dl_dir))
                    names = sorted(after_files - set(task.before_files or []))
                    names = [f for f in names if not f.startswith('.') and not f.endswith('.tmp')]
                file_list, total_size = _format_file_list(task.dl_dir, names)
            except Exception:
                file_list, total_size = "无法列出文件", 0
            bot.send_message(task.chat_id, f"""✅ 单文件下载完成！
📥 链接：{task.link}
📁 保存目录：{task.dl_dir}/
🔑 使用TDL账号：@{task.tdl_name}
📦 下载大小：{_format_size(total_size)}
📂 下载文件：
{file_list}""")
        else:
            try:
                files = os.listdir(task.dl_dir)
                file_list = "\n".join(files[:20]) if files else "（无可列出的文件）"
                if len(files) > 20:
                    file_list += f"\n... 还有 {len(files) - 20} 个文件"
            except Exception:
                file_list = "无法列出文件"
            bot.send_message(task.chat_id, f"""✅ 批量下载完成！
📥 源链接：{task.source_link} → 源频道ID：{task.source_id}
🆔 消息ID范围：{task.start_id}-{task.end_id}
📁 保存目录：{task.dl_dir}/
🔑 使用TDL账号：@{task.tdl_name}
📂 下载文件：
{file_list}""")
    else:
        _cleanup_tmp(task.dl_dir)
        bot.send_message(task.chat_id, f"❌ 下载失败！\n🔑 使用TDL账号：@{task.tdl_name}")
    queue._finish(task)


def do_single_download(chat_id, channel_id, msg_id, link, rename_name=None):
    """把单文件下载任务提交到队列。"""
    cid_str = str(chat_id)
    tdl_name = user_current_tdl.get(cid_str)
    user_accounts = TDL_ACCOUNTS.get(cid_str, [])
    if not tdl_name and user_accounts:
        tdl_name = user_accounts[0]
    if not tdl_name:
        bot.send_message(chat_id, "❌ 严重错误：未找到您的专属 TDL 账号！")
        user_steps.pop(chat_id, None)
        main_menu(chat_id)
        return
    task = DownloadTask(task_id=0, chat_id=chat_id, tdl_name=tdl_name, kind="single",
                        channel_id=channel_id, msg_id=msg_id, link=link, rename_name=rename_name)
    queue.submit(task)
    bot.send_message(chat_id, f"✅ 已加入下载队列\n{queue.status_text(chat_id)}")
    user_steps.pop(chat_id, None)


def do_multi_download(chat_id, source_id, source_link, start_id, end_id):
    """把批量下载任务提交到队列。"""
    cid_str = str(chat_id)
    tdl_name = user_current_tdl.get(cid_str)
    user_accounts = TDL_ACCOUNTS.get(cid_str, [])
    if not tdl_name and user_accounts:
        tdl_name = user_accounts[0]
    if not tdl_name:
        bot.send_message(chat_id, "❌ 严重错误：未找到您的专属 TDL 账号！")
        user_steps.pop(chat_id, None)
        main_menu(chat_id)
        return
    task = DownloadTask(task_id=0, chat_id=chat_id, tdl_name=tdl_name, kind="multi",
                        source_id=source_id, source_link=source_link, start_id=start_id, end_id=end_id)
    queue.submit(task)
    bot.send_message(chat_id, f"✅ 已加入下载队列\n{queue.status_text(chat_id)}")
    user_steps.pop(chat_id, None)


queue = DownloadQueue(MAX_CONCURRENT_DL, _run_task)

def _rename_new_files(dl_dir, before_files, rename_name):
    """下载完成后识别新增文件；指定 rename_name 时重命名（多文件加序号）。
    返回重命名后的新文件名列表；未指定 rename_name 返回 None。"""
    if not rename_name:
        return None
    try:
        after_files = set(os.listdir(dl_dir))
    except Exception:
        return []
    new_files = sorted(after_files - set(before_files or []))
    new_files = [f for f in new_files if not f.startswith('.') and not f.endswith('.tmp')]
    if not new_files:
        return []

    _, ext = os.path.splitext(rename_name)
    has_ext = bool(re.match(r'^\.[a-zA-Z0-9]{1,5}$', ext))

    result = []
    for i, f in enumerate(new_files):
        if has_ext:
            new_name = rename_name
        else:
            _, orig_ext = os.path.splitext(f)
            new_name = rename_name + orig_ext
        if len(new_files) > 1:
            nb, ne = os.path.splitext(new_name)
            new_name = f"{nb}_{i+1}{ne}"
        try:
            os.rename(os.path.join(dl_dir, f), os.path.join(dl_dir, new_name))
            result.append(new_name)
        except Exception:
            result.append(f)  # 改名失败则保留原名
    return result


@bot.message_handler(commands=['cancel'])
def cmd_cancel(msg):
    """取消当前操作：该用户全部下载任务 + 登录/任何进行中的输入"""
    chat_id = msg.chat.id
    n = queue.cancel_all(chat_id)
    if chat_id in active_logins:
        active_logins[chat_id].close(force=True)
        del active_logins[chat_id]
    user_steps.pop(chat_id, None)
    bot.send_message(chat_id, f"❌ 已取消 {n} 个下载任务")
    main_menu(chat_id)

@bot.message_handler(func=lambda msg: msg.text == "❌ 取消")
def btn_cancel(msg):
    cmd_cancel(msg)

# ==========================
# 步骤处理逻辑 (含新增 Pexpect 登录 + 下载)
# ==========================
@bot.message_handler(func=lambda msg: msg.chat.id in user_steps)
def handle_steps(msg):
    data = user_steps[msg.chat.id]
    step = data["step"]
    chat_id = msg.chat.id

    if step == "ADD_TDL_NAME":
        name = msg.text.strip()
        cid_str = str(chat_id)
        all_accounts = [acc for acc_list in TDL_ACCOUNTS.values() for acc in acc_list]

        if name in all_accounts:
            bot.send_message(chat_id, "❌ 该账号已被系统中其他人占用，为防冲突请换个名字。")
        else:
            if cid_str not in TDL_ACCOUNTS:
                TDL_ACCOUNTS[cid_str] = []
            TDL_ACCOUNTS[cid_str].append(name)
            save_tdl_accounts()
            user_current_tdl[cid_str] = name
            save_user_current_tdl()
            bot.send_message(chat_id, f"✅ 成功将 TDL 账号 @{name} 绑定到您的名下！")
        open_user_manager(chat_id)
        del user_steps[chat_id]
        return

    if step == "DEL_TDL_NAME":
        name = msg.text.strip()
        cid_str = str(chat_id)
        user_accounts = TDL_ACCOUNTS.get(cid_str, [])

        if name not in user_accounts:
            bot.send_message(chat_id, "❌ 该账号不存在或不属于您！")
        else:
            user_accounts.remove(name)
            TDL_ACCOUNTS[cid_str] = user_accounts
            save_tdl_accounts()
            if user_current_tdl.get(cid_str) == name:
                del user_current_tdl[cid_str]
                save_user_current_tdl()
            bot.send_message(chat_id, f"✅ 已从您的名下删除 TDL 账号：@{name}")
        open_user_manager(chat_id)
        del user_steps[chat_id]
        return

    # ================= 交互式登录模块 开始 =================

    if step == "LOGIN_NAME":
        tdl_name = msg.text.strip()
        all_accounts = [acc for acc_list in TDL_ACCOUNTS.values() for acc in acc_list]

        if tdl_name in all_accounts:
            bot.send_message(chat_id, "❌ 该账号名称已被系统内其他成员占用，请换一个名称。")
            del user_steps[chat_id]
            open_user_manager(chat_id)
            return

        data["tdl_name"] = tdl_name
        data["step"] = "LOGIN_PHONE"
        bot.send_message(chat_id, "📱 请输入绑定的手机号\n（必须包含国家代码，例如中国号填 +8613800000000）：")

    # 步骤 B：启动进程，输入手机号
    elif step == "LOGIN_PHONE":
        phone = msg.text.strip()
        tdl_name = data["tdl_name"]

        bot.send_message(chat_id, f"🔄 正在启动 TDL，请求发送验证码到 {phone}...")
        try:
            custom_env = os.environ.copy()
            custom_env['TERM'] = 'dumb'

            # 启动 tdl login 进程
            child = pexpect.spawn(f'tdl -n {tdl_name} login -T code', encoding='utf-8', timeout=45, env=custom_env)
            active_logins[chat_id] = child

            # 终极魔法：拦截并回复终端坐标查询 [6n
            # 它在要求输入手机号时会发出 6n 等待坐标。我们必须等它发出这个请求。
            child.expect(['6n'], timeout=15)

            # 立马回馈一个伪造的终端光标坐标（第24行第1列），喂饱它的 UI 库！
            child.send("\x1b[24;1R")
            sleep(0.5)

            # 喂饱坐标后，再把手机号塞进去
            child.sendline(phone)

            # 接下来等它发短信，它极大概率会再次发出 6n 坐标查询
            child.expect(['6n', '(?i)code'], timeout=45)
            # 为了防止卡在验证码输入前，不管三七二十一，盲补一发坐标
            child.send("\x1b[24;1R")

            data["step"] = "LOGIN_CODE"
            bot.send_message(chat_id, "📩 Telegram 官方已向您的设备发送了验证码。\n👉 **请输入验证码**：")

        except pexpect.TIMEOUT:
            safe_out = str(child.before).replace('<', '[').replace('>', ']')
            bot.send_message(chat_id, f"❌ 请求超时！\n终端截获：\n{safe_out}")
            _cleanup_login(chat_id)
        except Exception as e:
            safe_error = str(e).replace('<', '[').replace('>', ']')
            bot.send_message(chat_id, f"❌ 启动登录失败：\n{safe_error}")
            _cleanup_login(chat_id)

    # 步骤 C：输入验证码
    elif step == "LOGIN_CODE":
        code = msg.text.strip()
        child = active_logins.get(chat_id)
        if not child or not child.isalive():
            bot.send_message(chat_id, "❌ 登录会话已过期或中断，请重试。")
            del user_steps[chat_id]
            return

        bot.send_message(chat_id, "🔄 正在验证您的验证码...")
        child.sendline(code) # 发送验证码

        try:
            # 终极循环：不管它要多少次 6n 坐标，我们都满足它！
            while True:
                index = child.expect(['6n', '(?i)password', '(?i)success|welcome|logged', pexpect.EOF], timeout=15)

                if index == 0:
                    # 如果它又卡坐标了，补喂一次坐标，然后让循环继续等
                    child.send("\x1b[24;1R")

                elif index == 1:
                    # 匹配到需要输入密码
                    data["step"] = "LOGIN_PASSWORD"
                    bot.send_message(chat_id, "🔒 此账号开启了两步验证(2FA)。\n👉 **请输入两步验证密码**：")
                    break # 跳出循环

                elif index == 2 or index == 3:
                    # 匹配到成功或进程结束(EOF)，说明登录完成了！
                    sleep(2)
                    _bind_tdl_account(chat_id, data["tdl_name"])
                    child.close(force=True)
                    del active_logins[chat_id]
                    del user_steps[chat_id]
                    open_user_manager(chat_id)
                    break

        except pexpect.TIMEOUT:
            safe_out = str(child.before).replace('<', '[').replace('>', ']')
            bot.send_message(chat_id, f"❌ 验证超时或错误。\n截图: {safe_out}")
            _cleanup_login(chat_id)

    # 步骤 D：输入两步验证密码
    elif step == "LOGIN_PASSWORD":
        password = msg.text.strip()
        child = active_logins.get(chat_id)

        bot.send_message(chat_id, "🔄 正在验证密码...")
        child.sendline(password)

        try:
            # 密码环节同样使用无限喂食循环
            while True:
                index = child.expect(['6n', '(?i)success|welcome|logged', pexpect.EOF], timeout=15)
                if index == 0:
                    child.send("\x1b[24;1R") # 补喂坐标
                elif index == 1 or index == 2:
                    sleep(2)
                    _bind_tdl_account(chat_id, data["tdl_name"],
                                      f"✅ 两步验证通过！登录成功！账号 @{data['tdl_name']} 已绑定并设为您当前的专属账号。")
                    child.close(force=True)
                    del active_logins[chat_id]
                    del user_steps[chat_id]
                    open_user_manager(chat_id)
                    break
        except Exception:
            bot.send_message(chat_id, "❌ 密码错误或登录失败。")
            _cleanup_login(chat_id)

    # ================= 交互式登录模块 结束 =================

    # 添加管理员
    elif step == "ADD_ADMIN_ID":
        try:
            new_admin_id = int(msg.text.strip())
            if new_admin_id == SUPER_ADMIN:
                bot.send_message(chat_id, "❌ 不能添加超级管理员自身！")
            elif new_admin_id in ADMIN_LIST:
                bot.send_message(chat_id, "❌ 该用户已是管理员！")
            else:
                ADMIN_LIST.append(new_admin_id)
                save_admins()
                bot.send_message(chat_id, f"✅ 成功添加管理员：{new_admin_id}")
        except ValueError:
            bot.send_message(chat_id, "❌ 请输入有效的数字ID！")
        open_admin_panel(chat_id)
        del user_steps[chat_id]

    # 删除管理员
    elif step == "DEL_ADMIN_ID":
        try:
            del_admin_id = int(msg.text.strip())
            if del_admin_id == SUPER_ADMIN:
                bot.send_message(chat_id, "❌ 不能删除超级管理员！")
            elif del_admin_id not in ADMIN_LIST:
                bot.send_message(chat_id, "❌ 该用户不是管理员！")
            else:
                ADMIN_LIST.remove(del_admin_id)
                save_admins()
                bot.send_message(chat_id, f"✅ 成功删除管理员：{del_admin_id}")
        except ValueError:
            bot.send_message(chat_id, "❌ 请输入有效的数字ID！")
        open_admin_panel(chat_id)
        del user_steps[chat_id]

    # ================= 单文件下载步骤 =================
    elif step == "SINGLE_DL_LINK":
        link = msg.text.strip()
        channel_id, msg_id = extract_channel_and_msg_id_from_link(link)
        if not channel_id or not msg_id:
            bot.send_message(chat_id, "❌ 链接格式无效！请输入包含消息ID的完整链接（如https://t.me/c/123456/789）")
            del user_steps[chat_id]
            main_menu(chat_id)
            return

        data["channel_id"] = channel_id
        data["msg_id"] = msg_id
        data["link"] = link
        data["step"] = "SINGLE_DL_RENAME_CONFIRM"

        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("✅ 需要", callback_data="rename_yes"),
            telebot.types.InlineKeyboardButton("❌ 不需要", callback_data="rename_no"),
        )
        bot.send_message(chat_id, "是否需要重命名下载的文件？", reply_markup=markup)

    elif step == "SINGLE_DL_RENAME_CONFIRM":
        bot.send_message(chat_id, "⚠️ 请点击上方消息里的按钮选择")

    elif step == "SINGLE_DL_RENAME_NAME":
        rename_name = msg.text.strip()
        if not rename_name:
            bot.send_message(chat_id, "❌ 文件名不能为空，请重新输入：")
            return
        do_single_download(chat_id, data["channel_id"], data["msg_id"], data["link"], rename_name=rename_name)

    # ================= 批量下载步骤 =================
    elif step == "MULTI_DL_START_LINK":
        start_link = msg.text.strip()
        source_channel_id, start_msg_id = extract_channel_and_msg_id_from_link(start_link)
        if not source_channel_id or not start_msg_id:
            bot.send_message(chat_id, "❌ 链接格式无效！请输入包含消息ID的完整链接（如https://t.me/c/123456/38359）")
            del user_steps[chat_id]
            main_menu(chat_id)
            return
        data["source_id"] = source_channel_id
        data["source_link"] = start_link
        data["start_id"] = start_msg_id
        data["step"] = "MULTI_DL_END_LINK"
        bot.send_message(chat_id, f"🔄 已提取源频道ID：{source_channel_id}，起始消息ID：{start_msg_id}\n✅ 请输入【结束消息完整链接】：")

    elif step == "MULTI_DL_END_LINK":
        end_link = msg.text.strip()
        end_channel_id, end_msg_id = extract_channel_and_msg_id_from_link(end_link)
        if not end_channel_id or not end_msg_id:
            bot.send_message(chat_id, "❌ 链接格式无效！请输入包含消息ID的完整链接")
            del user_steps[chat_id]
            main_menu(chat_id)
            return
        if end_channel_id != data["source_id"]:
            bot.send_message(chat_id, f"❌ 结束链接的频道ID（{end_channel_id}）与起始频道ID（{data['source_id']}）不一致！")
            del user_steps[chat_id]
            main_menu(chat_id)
            return
        do_multi_download(chat_id, data["source_id"], data["source_link"], data["start_id"], end_msg_id)

    # ================= 选择下载类型 =================
    elif step == "SELECTING_EXT":
        text = msg.text.strip()
        if text.startswith("☐ 全部") or text.startswith("✅ 全部"):
            user_dl_ext[str(chat_id)] = ""
            save_user_dl_ext()
            bot.send_message(chat_id, "📎 已设为全部下载")
        elif text == "✏️ 手动输入":
            data["step"] = "SET_DL_EXT"
            bot.send_message(chat_id, "✏️ 请输入扩展名（逗号分隔，如 mp4,jpg,png）：")
            return
        elif text == "🔙 关闭":
            bot.send_message(chat_id, "📎 下载类型设置已关闭")
        else:
            bot.send_message(chat_id, "⚠️ 请使用底部按钮操作")
            return
        del user_steps[chat_id]
        main_menu(chat_id)

    # ================= 设置下载类型（手动输入） =================
    elif step == "SET_DL_EXT":
        ext = msg.text.strip().replace(" ", "")
        if not re.match(r'^[a-z0-9,]+$', ext):
            bot.send_message(chat_id, "❌ 格式无效！请输入纯英文扩展名，逗号分隔（如 mp4,jpg,png）")
            del user_steps[chat_id]
            main_menu(chat_id)
            return
        user_dl_ext[str(chat_id)] = ext
        save_user_dl_ext()
        bot.send_message(chat_id, f"✅ 下载类型已更新: {ext}")
        del user_steps[chat_id]
        main_menu(chat_id)

# ==========================
# 直接发送链接下载（单文件）
# ==========================
@bot.message_handler(func=lambda msg: msg.text and re.match(r'^https://t\.me/', msg.text.strip()))
@admin_required
def direct_link_download(msg):
    """直接发送消息链接即可触发单文件下载（保留原文件名）"""
    link = msg.text.strip()
    channel_id, msg_id = extract_channel_and_msg_id_from_link(link)
    if not channel_id or not msg_id:
        bot.send_message(msg.chat.id, "❌ 链接格式无效！请输入包含消息ID的完整链接（如 https://t.me/JAVDMM/36459）")
        return
    do_single_download(msg.chat.id, channel_id, msg_id, link)

# ==========================
# TDL账号切换
# ==========================
@bot.message_handler(func=lambda msg: msg.text.startswith("@"))
@admin_required
def switch_tdl_account(msg):
    name = msg.text.replace("@", "")
    cid_str = str(msg.chat.id)
    user_accounts = TDL_ACCOUNTS.get(cid_str, [])

    if name not in user_accounts:
        bot.send_message(msg.chat.id, "❌ 切换失败：您名下没有该账号的访问权限，或者该账号不存在！")
        return

    user_current_tdl[cid_str] = name
    save_user_current_tdl()
    bot.send_message(msg.chat.id, f"✅ TDL账号已切换至您的专属账号：@{name}")
    open_user_manager(msg.chat.id)

# ==========================
# 启动机器人
# ==========================
if __name__ == "__main__":
    set_bot_commands()
    print("机器人已启动，等待指令...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            print(f"⚠️ 连接异常，5秒后重试: {e}")
            sleep(5)

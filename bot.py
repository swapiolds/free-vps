import random
import logging
import subprocess
import sys
import os
import re
import time
import asyncio
import sqlite3
import uuid
import urllib.request
from dotenv import load_dotenv
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from telegram.constants import ParseMode

ADMIN_SET_LIMIT, ADMIN_SET_RAM, ADMIN_SET_CPU, ADMIN_SET_DISK, ADMIN_SET_BANNER, ADMIN_ADD_FJ, ADMIN_SET_EXPIRY = range(7)

# Load environment variables
load_dotenv()

# Configuration from .env
TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
BOT_STATUS_NAME = os.getenv('BOT_STATUS_NAME', 'UnixNodes')
WATERMARK = os.getenv('WATERMARK', 'Powered by UnixNodes VPS Bot')

# VPS Defaults
DEFAULT_RAM = os.getenv('DEFAULT_RAM', '2g')
DEFAULT_CPU = os.getenv('DEFAULT_CPU', '1')
DEFAULT_DISK = os.getenv('DEFAULT_DISK', '10G')
VPS_HOSTNAME = os.getenv('VPS_HOSTNAME', 'unix-free')
SERVER_LIMIT = int(os.getenv('SERVER_LIMIT', 1))
TOTAL_SERVER_LIMIT = int(os.getenv('TOTAL_SERVER_LIMIT', 50))
DATABASE_FILE = os.getenv('DATABASE_FILE', 'vps_bot.db')

VPS_DATA_DIR = os.path.join(os.getcwd(), "vps_data")
UBUNTU_ROOTFS_URL = "https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/ubuntu-base-22.04-base-amd64.tar.gz"
UBUNTU_TAR_PATH = os.path.join(os.getcwd(), "ubuntu-base.tar.gz")

os.makedirs(VPS_DATA_DIR, exist_ok=True)

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('vps_bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def download_rootfs():
    if not os.path.exists(UBUNTU_TAR_PATH):
        logger.info("Downloading Ubuntu 22.04 RootFS... (This may take a few minutes)")
        urllib.request.urlretrieve(UBUNTU_ROOTFS_URL, UBUNTU_TAR_PATH)
        logger.info("RootFS downloaded successfully!")

def is_admin(user_id):
    return user_id == ADMIN_ID

# ----------------- Database Setup & Helpers -----------------

def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS vps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            container_id TEXT UNIQUE NOT NULL,
            container_name TEXT NOT NULL,
            os_type TEXT NOT NULL,
            hostname TEXT NOT NULL,
            status TEXT DEFAULT 'stopped',
            ssh_command TEXT,
            ram TEXT DEFAULT '{DEFAULT_RAM}',
            cpu TEXT DEFAULT '{DEFAULT_CPU}',
            disk TEXT DEFAULT '{DEFAULT_DISK}',
            suspended INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    cursor.execute("PRAGMA table_info(vps)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'suspended' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN suspended INTEGER DEFAULT 0")
    if 'expires_at' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN expires_at TIMESTAMP")
    if 'upgraded' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN upgraded INTEGER DEFAULT 0")
    
    cursor.execute("PRAGMA table_info(users)")
    user_columns = [col[1] for col in cursor.fetchall()]
    if 'referred_by' not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
    if 'spent_invites' not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN spent_invites INTEGER DEFAULT 0")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    # Initialize default settings if they don't exist
    default_settings = {
        'TOTAL_SERVER_LIMIT': str(TOTAL_SERVER_LIMIT),
        'DEFAULT_RAM': DEFAULT_RAM,
        'DEFAULT_CPU': DEFAULT_CPU,
        'DEFAULT_DISK': DEFAULT_DISK,
        'BANNER_FILE_ID': '',
        'DEFAULT_EXPIRY_DAYS': '30'
    }
    for k, v in default_settings.items():
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS force_join (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            link TEXT,
            chat_type TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def add_user(user_id, username, referred_by=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM users WHERE user_id = ?', (user_id,))
    if cursor.fetchone():
        cursor.execute('UPDATE users SET username = ? WHERE user_id = ?', (username, user_id))
    else:
        cursor.execute('INSERT INTO users (user_id, username, referred_by) VALUES (?, ?, ?)', (user_id, username, referred_by))
    conn.commit()
    conn.close()

def get_invite_count(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count

def get_spent_invites(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT spent_invites FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def spend_invites(user_id, amount):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET spent_invites = spent_invites + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()
    
def get_user_created_at(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT created_at FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else "Unknown"

def get_leaderboard():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u1.username, (SELECT COUNT(*) FROM users u2 WHERE u2.referred_by = u1.user_id) as invites
        FROM users u1
        ORDER BY invites DESC
        LIMIT 10
    ''')
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_setting(key, default=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0]
    return default

def set_setting(key, value):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_force_join_chats():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, title, link, chat_type FROM force_join")
    chats = cursor.fetchall()
    conn.close()
    return chats

def add_force_join(chat_id, title, link, chat_type):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO force_join (chat_id, title, link, chat_type) VALUES (?, ?, ?, ?)", (chat_id, title, link, chat_type))
    conn.commit()
    conn.close()

def remove_force_join(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM force_join WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM bans WHERE user_id = ?', (user_id,))
    banned = cursor.fetchone() is not None
    conn.close()
    return banned

def add_vps(user_id, vps_id, container_name, os_type, hostname, ssh_line, ram=DEFAULT_RAM, cpu=DEFAULT_CPU, disk=DEFAULT_DISK):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    expiry_days = int(get_setting('DEFAULT_EXPIRY_DAYS', 30))
    if expiry_days > 0:
        cursor.execute('''
            INSERT INTO vps (user_id, container_id, container_name, os_type, hostname, ssh_command, ram, cpu, disk, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+' || ? || ' days'))
        ''', (user_id, vps_id, container_name, os_type, hostname, ssh_line, ram, cpu, disk, expiry_days))
    else:
        cursor.execute('''
            INSERT INTO vps (user_id, container_id, container_name, os_type, hostname, ssh_command, ram, cpu, disk)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, vps_id, container_name, os_type, hostname, ssh_line, ram, cpu, disk))
        
    conn.commit()
    conn.close()

def get_user_vps(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    vps_list = cursor.fetchall()
    conn.close()
    return vps_list

def count_user_vps(user_id):
    return len(get_user_vps(user_id))

def get_vps_by_identifier(user_id, identifier):
    vps_list = get_user_vps(user_id)
    if not identifier:
        return vps_list[0] if vps_list else None
    identifier_lower = identifier.lower()
    for vps in vps_list:
        if (identifier_lower in vps['container_id'].lower() or
            identifier_lower in vps['container_name'].lower()):
            return vps
    return None

def update_vps_status(container_id, status):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET status = ? WHERE container_id = ?', (status, container_id))
    conn.commit()
    conn.close()

def update_vps_ssh(container_id, ssh_command):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET ssh_command = ? WHERE container_id = ?', (ssh_command, container_id))
    conn.commit()
    conn.close()

def delete_vps(container_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM vps WHERE container_id = ?', (container_id,))
    conn.commit()
    conn.close()

def get_total_instances():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM vps WHERE status = "running"')
    count = cursor.fetchone()[0]
    conn.close()
    return count

# ----------------- PRoot Helpers -----------------

def check_proot_status(vps_id):
    try:
        cmd = f"pgrep -f 'proot.*{vps_id}'"
        output = subprocess.check_output(cmd, shell=True).decode().strip()
        return "running" if output else "stopped"
    except Exception:
        return "stopped"

async def async_extract_rootfs(vps_id):
    target_dir = os.path.join(VPS_DATA_DIR, vps_id)
    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)
        logger.info(f"Extracting RootFS for {vps_id}...")
        proc = await asyncio.create_subprocess_exec(
            "tar", "-xf", UBUNTU_TAR_PATH, "-C", target_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        
        # Fix DNS
        resolv_conf = os.path.join(target_dir, "etc", "resolv.conf")
        if os.path.exists(resolv_conf):
            os.remove(resolv_conf)
        with open(resolv_conf, "w") as f:
            f.write("nameserver 8.8.8.8\nnameserver 1.1.1.1\n")
            
        # Generate fake procfs files to spoof hardware
        fake_meminfo = os.path.join(target_dir, ".fake_meminfo")
        with open(fake_meminfo, "w") as f:
            f.write(
                "MemTotal:        2048000 kB\n"
                "MemFree:         1024000 kB\n"
                "MemAvailable:    1536000 kB\n"
                "Buffers:           20480 kB\n"
                "Cached:           307200 kB\n"
                "SwapCached:            0 kB\n"
                "Active:           512000 kB\n"
                "Inactive:         256000 kB\n"
                "SwapTotal:             0 kB\n"
                "SwapFree:              0 kB\n"
            )
            
        fake_cpuinfo = os.path.join(target_dir, ".fake_cpuinfo")
        with open(fake_cpuinfo, "w") as f:
            f.write(
                "processor\t: 0\n"
                "vendor_id\t: GenuineIntel\n"
                "cpu family\t: 6\n"
                "model\t\t: 85\n"
                "model name\t: Intel(R) Xeon(R) Platinum 8259CL CPU @ 2.50GHz\n"
                "stepping\t: 4\n"
                "cpu MHz\t\t: 2500.000\n"
                "cache size\t: 36608 KB\n"
                "physical id\t: 0\n"
                "siblings\t: 2\n"
                "core id\t\t: 0\n"
                "cpu cores\t: 2\n"
                "processor\t: 1\n"
                "vendor_id\t: GenuineIntel\n"
                "cpu family\t: 6\n"
                "model\t\t: 85\n"
                "model name\t: Intel(R) Xeon(R) Platinum 8259CL CPU @ 2.50GHz\n"
                "stepping\t: 4\n"
                "cpu MHz\t\t: 2500.000\n"
                "cache size\t: 36608 KB\n"
                "physical id\t: 0\n"
                "siblings\t: 2\n"
                "core id\t\t: 1\n"
                "cpu cores\t: 2\n"
            )
            
        # Spoof Hostname to SwapiHost
        with open(os.path.join(target_dir, "etc", "hostname"), "w") as f:
            f.write("SwapiHost\n")
        with open(os.path.join(target_dir, "etc", "hosts"), "w") as f:
            f.write("127.0.0.1 localhost\n127.0.1.1 SwapiHost\n")
            
        fake_kernel_hostname = os.path.join(target_dir, ".fake_hostname")
        with open(fake_kernel_hostname, "w") as f:
            f.write("SwapiHost\n")
            
        # Spoof product name for Host in neofetch
        fake_product_name = os.path.join(target_dir, ".fake_product_name")
        with open(fake_product_name, "w") as f:
            f.write("SwapiHost\n")
            
        # Inject custom bash prompt to fake the hostname display
        bashrc = os.path.join(target_dir, "root", ".bashrc")
        if os.path.exists(bashrc):
            with open(bashrc, "a") as f:
                f.write("\nexport HOSTNAME=SwapiHost\n")
                f.write("PS1='\\[\\e[32m\\]root@SwapiHost\\[\\e[m\\]:\\[\\e[34m\\]\\w\\[\\e[m\\]\\$ '\n")
        else:
            os.makedirs(os.path.join(target_dir, "root"), exist_ok=True)
            with open(bashrc, "w") as f:
                f.write("export HOSTNAME=SwapiHost\n")
                f.write("PS1='\\[\\e[32m\\]root@SwapiHost\\[\\e[m\\]:\\[\\e[34m\\]\\w\\[\\e[m\\]\\$ '\n")
                
        # Inject wrapper scripts for hostname and uname
        bin_dir = os.path.join(target_dir, "usr", "local", "bin")
        os.makedirs(bin_dir, exist_ok=True)
        
        hostname_bin = os.path.join(bin_dir, "hostname")
        with open(hostname_bin, "w") as f:
            f.write("#!/bin/sh\necho SwapiHost\n")
        os.chmod(hostname_bin, 0o755)
        
        uname_bin = os.path.join(bin_dir, "uname")
        with open(uname_bin, "w") as f:
            f.write("#!/bin/bash\nif [ \"$1\" = \"-n\" ]; then\n    echo \"SwapiHost\"\nelse\n    /bin/uname \"$@\" | sed 's/-aws//g'\nfi\n")
        os.chmod(uname_bin, 0o755)
            
        lspci_bin = os.path.join(bin_dir, "lspci")
        with open(lspci_bin, "w") as f:
            f.write("#!/bin/bash\nif [ -x /usr/bin/lspci ]; then\n    /usr/bin/lspci \"$@\" | grep -v -i -E \"VGA|3D|Display|Amazon\"\nfi\n")
        os.chmod(lspci_bin, 0o755)
            
        logger.info(f"RootFS ready for {vps_id}")
    return target_dir

async def async_proot_start(vps_id):
    target_dir = os.path.join(VPS_DATA_DIR, vps_id)
    # Install tmate and start it
    cmd = f"proot -0 -r {target_dir} -b /dev -b /proc -b {target_dir}/.fake_meminfo:/proc/meminfo -b {target_dir}/.fake_cpuinfo:/proc/cpuinfo -b {target_dir}/.fake_hostname:/proc/sys/kernel/hostname -b /sys -b {target_dir}/.fake_product_name:/sys/devices/virtual/dmi/id/product_name -b {target_dir}/.fake_product_name:/sys/devices/virtual/dmi/id/sys_vendor -b {target_dir}/.fake_product_name:/sys/class/dmi/id/product_name -b {target_dir}/.fake_product_name:/sys/class/dmi/id/sys_vendor -w /root /bin/bash -c 'apt-get update >/dev/null && apt-get install -y tmate curl wget sudo openssh-client pciutils >/dev/null && tmate -F'"
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    return proc

async def async_proot_stop(vps_id):
    try:
        cmd = f"pkill -f 'proot.*{vps_id}'"
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.communicate()
        return True
    except Exception: return False

async def async_proot_rm(vps_id):
    try:
        target_dir = os.path.join(VPS_DATA_DIR, vps_id)
        cmd = f"rm -rf {target_dir}"
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.communicate()
        return True
    except Exception: return False

async def capture_ssh_session_line(process):
    start_time = time.time()
    while time.time() - start_time < 180: # Wait up to 3 mins for tmate
        try:
            output = await asyncio.wait_for(process.stdout.readline(), timeout=5.0)
            if not output:
                break
            output = output.decode('utf-8').strip()
            if "ssh session:" in output.lower():
                return output.split("ssh session:")[-1].strip()
        except asyncio.TimeoutError:
            continue
    return None

# ----------------- UI / Interactive Handlers -----------------

def get_main_menu_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🚀 ᴅᴇᴘʟᴏʏ ᴠᴘꜱ"), KeyboardButton("🖥 ᴍʏ ᴠᴘꜱ")],
        [KeyboardButton("👤 ᴍʏ ᴘʀᴏꜰɪʟᴇ"), KeyboardButton("🏆 ʟᴇᴀᴅᴇʀʙᴏᴀʀᴅ")],
        [KeyboardButton("🎁 ʀᴇᴡᴀʀᴅꜱ"), KeyboardButton("🛍️ ʙᴜʏ ᴠᴘꜱ")],
        [KeyboardButton("❓ ʜᴇʟᴘ")]
    ], resize_keyboard=True)

async def check_force_join(user_id, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(user_id):
        return True

    chats = get_force_join_chats()
    if not chats:
        return True

    not_joined = []
    for chat_id, title, link, chat_type in chats:
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status in ["left", "kicked"]:
                not_joined.append((title, link, chat_type))
        except Exception as e:
            logger.error(f"Error checking chat {chat_id}: {e}")
            continue

    if not_joined:
        buttons = []
        for title, link, chat_type in not_joined:
            icon = "📢" if chat_type == "channel" else "💬"
            label = "𝐉𝐨𝐢𝐧 𝐂𝐡𝐚𝐧𝐧𝐞𝐥" if chat_type == "channel" else "𝐉𝐨𝐢𝐧 𝐆𝐫𝐨𝐮𝐩"
            buttons.append([InlineKeyboardButton(text=f"{label} {icon}", url=link)])
        
        buttons.append([InlineKeyboardButton(text="𝐉𝐨𝐢𝐧𝐞𝐝 ✅", callback_data="main_menu")])
        markup = InlineKeyboardMarkup(buttons)
        
        banner = get_setting('BANNER_FILE_ID', '')
        caption = "⚠️ <b>Attention!</b>\n━━━━━━━━━━━━━━━━━━━━\nYou must join our official channels and groups to use this bot."
        
        return (False, markup, banner, caption)
    return True

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.first_name or update.effective_user.username or str(user_id)
    
    # Check Force Join first
    fj_check = await check_force_join(user_id, context)
    if fj_check is not True:
        _, markup, banner, caption = fj_check
        if banner:
            try:
                if update.message:
                    await update.message.reply_photo(photo=banner, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
                elif update.callback_query:
                    await update.callback_query.message.reply_photo(photo=banner, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
                return
            except Exception:
                pass
        
        if update.message:
            await update.message.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=markup)
        elif update.callback_query:
            await update.callback_query.message.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    # Handle Referral System
    if update.message and context.args:
        try:
            referred_by = int(context.args[0])
            if referred_by != user_id:
                # add_user will ignore if user already exists, so referred_by is only set on first join
                add_user(user_id, username, referred_by)
            else:
                add_user(user_id, username)
        except ValueError:
            add_user(user_id, username)
    else:
        add_user(user_id, username)

    invites = get_invite_count(user_id)
    bot_username = context.bot.username
    invite_link = f"https://t.me/{bot_username}?start={user_id}"

    msg = (
        "👋 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ꜱᴡᴀᴘɪʜᴏꜱᴛ ᴠᴘꜱ ʙᴏᴛ!\n\n"
        "🎬 ɢᴇᴛ ꜰʀᴇᴇ ᴠᴘꜱ ʙʏ ʀᴇꜰᴇʀʀɪɴɢ ꜰʀɪᴇɴᴅꜱ ᴏʀ ʀᴇᴅᴇᴇᴍɪɴɢ ᴘᴏɪɴᴛꜱ.\n\n"
        "🔥 ꜰᴇᴀᴛᴜʀᴇꜱ:\n"
        "• ɪɴꜱᴛᴀɴᴛ ᴅᴇʟɪᴠᴇʀʏ\n"
        "• ʀᴇꜰᴇʀʀᴀʟ ʀᴇᴡᴀʀᴅꜱ\n"
        "• 24/7 ꜱᴜᴘᴘᴏʀᴛ\n\n"
        "👇 ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ɴᴀᴠɪɢᴀᴛᴇ."
    )
    
    banner = get_setting('BANNER_FILE_ID', '')
    
    if update.message:
        if banner:
            try:
                await update.message.reply_photo(photo=banner, caption=msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
                return
            except Exception:
                pass
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    elif update.callback_query:
        if banner and not update.callback_query.message.photo:
            try:
                await update.callback_query.message.delete()
                await update.callback_query.message.reply_photo(photo=banner, caption=msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
                return
            except Exception:
                pass
        elif banner and update.callback_query.message.photo:
            try:
                await update.callback_query.message.edit_caption(caption=msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
                return
            except Exception:
                pass
        await update.callback_query.message.edit_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())

async def handle_create_vps(update_or_query, context, os_type, user_id, username):
    # Determine if it's a message or callback query
    is_message = hasattr(update_or_query, 'message') and update_or_query.message is not None and not hasattr(update_or_query, 'data')
    message = update_or_query.message if is_message else update_or_query.message
    
    # Enforce force join even on button clicks
    fj_check = await check_force_join(user_id, context)
    if fj_check is not True:
        await cmd_start(update_or_query, context) # Route back to start to show force join
        return

    if is_banned(user_id):
        text_msg = "❌ <b>ʏᴏᴜ ᴀʀᴇ ʙᴀɴɴᴇᴅ ꜰʀᴏᴍ ᴄʀᴇᴀᴛɪɴɢ ᴠᴘꜱ ɪɴꜱᴛᴀɴᴄᴇꜱ.</b>"
        await message.reply_text(text_msg, parse_mode=ParseMode.HTML) if is_message else await update_or_query.message.edit_text(text_msg, parse_mode=ParseMode.HTML)
        return
        
    invites = get_invite_count(user_id)
    if invites < 20 and not is_admin(user_id):
        text_msg = f"❌ <b>ʏᴏᴜ ɴᴇᴇᴅ ᴀᴛ ʟᴇᴀꜱᴛ 20 ɪɴᴠɪᴛᴇꜱ ᴛᴏ ᴅᴇᴘʟᴏʏ ᴀ ᴠᴘꜱ.</b>\n\n👥 <b>ᴄᴜʀʀᴇɴᴛ ɪɴᴠɪᴛᴇꜱ:</b> {invites}/20"
        await message.reply_text(text_msg, parse_mode=ParseMode.HTML) if is_message else await update_or_query.message.edit_text(text_msg, parse_mode=ParseMode.HTML)
        return

    # Strictly 1 VPS per user
    if count_user_vps(user_id) >= 1:
        text_msg = f"❌ <b>ʏᴏᴜ ʜᴀᴠᴇ ʀᴇᴀᴄʜᴇᴅ ᴛʜᴇ ᴍᴀxɪᴍᴜᴍ ʟɪᴍɪᴛ ᴏꜰ 1 ᴠᴘꜱ ɪɴꜱᴛᴀɴᴄᴇ ᴘᴇʀ ᴜꜱᴇʀ.</b>"
        await message.reply_text(text_msg, parse_mode=ParseMode.HTML) if is_message else await update_or_query.message.edit_text(text_msg, parse_mode=ParseMode.HTML)
        return
        
    total_limit = int(get_setting('TOTAL_SERVER_LIMIT', TOTAL_SERVER_LIMIT))
    if get_total_instances() >= total_limit:
        text_msg = "❌ <b>ɢʟᴏʙᴀʟ ꜱᴇʀᴠᴇʀ ʟɪᴍɪᴛ ʀᴇᴀᴄʜᴇᴅ. ᴘʟᴇᴀꜱᴇ ᴛʀʏ ᴀɢᴀɪɴ ʟᴀᴛᴇʀ.</b>"
        await message.reply_text(text_msg, parse_mode=ParseMode.HTML) if is_message else await update_or_query.message.edit_text(text_msg, parse_mode=ParseMode.HTML)
        return

    init_msg = "⏳ <b>ᴄʀᴇᴀᴛɪɴɢ ʏᴏᴜʀ ᴠᴘꜱ ɪɴꜱᴛᴀɴᴄᴇ...</b>\n[□□□□□□□□□□] 0%"
    msg = await message.reply_text(init_msg, parse_mode=ParseMode.HTML) if is_message else await update_or_query.message.edit_text(init_msg, parse_mode=ParseMode.HTML)
    
    async def animate_loading(target_msg):
        frames = [
            "⏳ <b>ᴄʀᴇᴀᴛɪɴɢ ʏᴏᴜʀ ᴠᴘꜱ ɪɴꜱᴛᴀɴᴄᴇ...</b>\n[■□□□□□□□□□] 10%",
            "⏳ <b>ᴇxᴛʀᴀᴄᴛɪɴɢ ʀᴏᴏᴛꜰꜱ...</b>\n[■■■□□□□□□□] 30%",
            "⏳ <b>ɪɴꜱᴛᴀʟʟɪɴɢ ᴘᴀᴄᴋᴀɢᴇꜱ...</b>\n[■■■■■□□□□□] 50%",
            "⏳ <b>ᴄᴏɴꜰɪɢᴜʀɪɴɢ ɴᴇᴛᴡᴏʀᴋ...</b>\n[■■■■■■■□□□] 70%",
            "⏳ <b>ꜱᴇᴛᴛɪɴɢ ᴜᴘ ꜱꜱʜ ᴀᴄᴄᴇꜱꜱ...</b>\n[■■■■■■■■■□] 90%",
            "⏳ <b>ꜰɪɴᴀʟɪᴢɪɴɢ ꜱᴇᴛᴜᴘ...</b>\n[■■■■■■■■■■] 99%"
        ]
        try:
            for frame in frames:
                await asyncio.sleep(4)
                await target_msg.edit_text(frame, parse_mode=ParseMode.HTML)
            while True:
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
            
    loading_task = asyncio.create_task(animate_loading(msg))
    
    vps_id = str(uuid.uuid4())[:8]
    hostname = f"{VPS_HOSTNAME}-{user_id}"
    container_name = f"vps-{user_id}-{vps_id}"
    
    try:
        await async_extract_rootfs(vps_id)
        proc = await async_proot_start(vps_id)
        ssh_line = await capture_ssh_session_line(proc)
    finally:
        loading_task.cancel()
        
    if ssh_line:
        ram = get_setting('DEFAULT_RAM', DEFAULT_RAM)
        cpu = get_setting('DEFAULT_CPU', DEFAULT_CPU)
        disk = get_setting('DEFAULT_DISK', DEFAULT_DISK)
        add_vps(user_id, vps_id, container_name, "ubuntu", hostname, ssh_line, ram=ram, cpu=cpu, disk=disk)
        text = (
            "✅ <b>ʏᴏᴜʀ ᴘʀᴇᴍɪᴜᴍ ᴠᴘꜱ ɪꜱ ʀᴇᴀᴅʏ!</b> 🎉\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🖥️ <b>ꜱᴇʀᴠᴇʀ ꜱᴘᴇᴄɪꜰɪᴄᴀᴛɪᴏɴꜱ:</b>\n"
            f"• <b>ᴏꜱ:</b> ᴜʙᴜɴᴛᴜ 22.04 ʟᴛꜱ\n"
            f"• <b>ʀᴀᴍ:</b> {ram} ʀᴀᴍ\n"
            f"• <b>ᴄᴘᴜ:</b> {cpu} ᴄᴏʀᴇꜱ\n"
            f"• <b>ꜱᴛᴏʀᴀɢᴇ:</b> {disk} ᴅɪꜱᴋ\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔑 <b>ꜱꜱʜ ᴀᴄᴄᴇꜱꜱ ᴄᴏᴍᴍᴀɴᴅ:</b>\n"
            f"<code>{ssh_line}</code>\n\n"
            "<i>(ᴄᴏᴘʏ ᴛʜᴇ ᴀʙᴏᴠᴇ ᴄᴏᴍᴍᴀɴᴅ ᴀɴᴅ ᴘᴀꜱᴛᴇ ɪᴛ ɪɴ ᴛᴇʀᴍᴜx ᴏʀ ᴀɴʏ ꜱꜱʜ ᴄʟɪᴇɴᴛ ᴛᴏ ᴄᴏɴɴᴇᴄᴛ)</i>"
        )
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    else:
        await msg.edit_text("❌ <b>ᴄʀᴇᴀᴛɪᴏɴ ꜰᴀɪʟᴇᴅ:</b> ᴜɴᴀʙʟᴇ ᴛᴏ ɢᴇɴᴇʀᴀᴛᴇ ꜱꜱʜ ꜱᴇꜱꜱɪᴏɴ.", parse_mode=ParseMode.HTML)
        await async_proot_stop(vps_id)


async def handle_keyboard_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    username = update.effective_user.first_name or update.effective_user.username or str(user_id)
    
    fj_check = await check_force_join(user_id, context)
    if fj_check is not True:
        await cmd_start(update, context)
        return

    # Simulate query for functions expecting it, or directly handle
    class FakeQuery:
        def __init__(self, message, from_user):
            self.message = message
            self.from_user = from_user
        async def answer(self, *args, **kwargs): pass

    query = FakeQuery(update.message, update.effective_user)

    if "🚀" in text:
        await handle_create_vps(update, context, "ubuntu", user_id, username)
    elif "🖥" in text:
        vps_list = get_user_vps(user_id)
        if not vps_list:
            await update.message.reply_text("❌ <b>ʏᴏᴜ ʜᴀᴠᴇ ɴᴏ ᴠᴘꜱ ɪɴꜱᴛᴀɴᴄᴇꜱ.</b>", parse_mode=ParseMode.HTML)
            return
        
        keyboard = []
        for v in vps_list[:10]:
            status_emoji = "🟢" if check_proot_status(v['container_id']) == "running" else "🔴"
            upgraded = "💎 " if v['upgraded'] == 1 else ""
            keyboard.append([InlineKeyboardButton(f"{upgraded}{status_emoji} {v['container_name']}", callback_data=f"manage_{v['container_id']}")])
        
        await update.message.reply_text("🖥 <b>ʏᴏᴜʀ ᴠᴘꜱ ɪɴꜱᴛᴀɴᴄᴇꜱ:</b>\nꜱᴇʟᴇᴄᴛ ᴏɴᴇ ᴛᴏ ᴍᴀɴᴀɢᴇ:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif "👤" in text:
        created_at = get_user_created_at(user_id)
        total_invites = get_invite_count(user_id)
        spent = get_spent_invites(user_id)
        vps_count = count_user_vps(user_id)
        bot_username = context.bot.username
        invite_link = f"https://t.me/{bot_username}?start={user_id}"
        
        profile_text = (
            f"👤 <b>ᴜꜱᴇʀ ᴘʀᴏꜰɪʟᴇ:</b> {username}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>ɪᴅ:</b> <code>{user_id}</code>\n"
            f"📅 <b>ᴊᴏɪɴᴇᴅ:</b> {created_at}\n\n"
            f"👥 <b>ᴛᴏᴛᴀʟ ɪɴᴠɪᴛᴇꜱ:</b> {total_invites}\n"
            f"💰 <b>ᴀᴠᴀɪʟᴀʙʟᴇ ɪɴᴠɪᴛᴇꜱ (ᴘᴏɪɴᴛꜱ):</b> {total_invites - spent}\n"
            f"🖥 <b>ᴛᴏᴛᴀʟ ᴠᴘꜱ:</b> {vps_count}\n\n"
            f"🔗 <b>ʏᴏᴜʀ ɪɴᴠɪᴛᴇ ʟɪɴᴋ:</b>\n<code>{invite_link}</code>"
        )
        await update.message.reply_text(profile_text, parse_mode=ParseMode.HTML)
        
    elif "🏆" in text:
        leaders = get_leaderboard()
        if not leaders:
            await update.message.reply_text("🏆 No one is on the leaderboard yet!")
            return
            
        board = "🏆 <b>ᴛᴏᴘ 10 ɪɴᴠɪᴛᴇʀ ʟᴇᴀᴅᴇʀʙᴏᴀʀᴅ:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for idx, (uname, inv) in enumerate(leaders, 1):
            emoji = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else "🏅"
            board += f"{emoji} <b>{uname}</b> — {inv} ɪɴᴠɪᴛᴇꜱ\n"
        
        await update.message.reply_text(board, parse_mode=ParseMode.HTML)
        
    elif "🎁" in text:
        total_invites = get_invite_count(user_id)
        spent = get_spent_invites(user_id)
        available = total_invites - spent
        
        msg = (
            "🎁 <b>ʀᴇᴡᴀʀᴅꜱ ᴄᴇɴᴛᴇʀ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"ʏᴏᴜ ᴄᴜʀʀᴇɴᴛʟʏ ʜᴀᴠᴇ <b>{available}</b> ᴀᴠᴀɪʟᴀʙʟᴇ ɪɴᴠɪᴛᴇ ᴘᴏɪɴᴛꜱ.\n\n"
            "💎 <b>ᴜᴘɢʀᴀᴅᴇ ᴠᴘꜱ ᴛᴏ 8ɢʙ ʀᴀᴍ</b>\n"
            "ᴄᴏꜱᴛ: 50 ɪɴᴠɪᴛᴇꜱ\n"
            "ꜱᴇʟᴇᴄᴛ ᴀ ᴠᴘꜱ ᴛᴏ ᴜᴘɢʀᴀᴅᴇ:"
        )
        
        vps_list = get_user_vps(user_id)
        if not vps_list:
            await update.message.reply_text("❌ <b>ʏᴏᴜ ɴᴇᴇᴅ ᴛᴏ ᴅᴇᴘʟᴏʏ ᴀ ᴠᴘꜱ ꜰɪʀꜱᴛ ʙᴇꜰᴏʀᴇ ᴜᴘɢʀᴀᴅɪɴɢ.</b>", parse_mode=ParseMode.HTML)
            return
            
        keyboard = []
        for v in vps_list[:10]:
            if v['upgraded'] == 0:
                keyboard.append([InlineKeyboardButton(f"ᴜᴘɢʀᴀᴅᴇ {v['container_name']}", callback_data=f"upgrade_{v['container_id']}")])
        
        if not keyboard:
            await update.message.reply_text("✅ <b>ᴀʟʟ ʏᴏᴜʀ ᴠᴘꜱ ɪɴꜱᴛᴀɴᴄᴇꜱ ᴀʀᴇ ᴀʟʀᴇᴀᴅʏ ᴜᴘɢʀᴀᴅᴇᴅ!</b>", parse_mode=ParseMode.HTML)
            return
            
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif "🛍" in text:
        buy_text = (
            "🛍️ <b>ʙᴜʏ ᴘʀᴇᴍɪᴜᴍ ᴠᴘꜱ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "ᴡᴀɴᴛ ᴛᴏ ʙʏᴘᴀꜱꜱ ᴛʜᴇ ɪɴᴠɪᴛᴇ ʟɪᴍɪᴛ ᴀɴᴅ ɢᴇᴛ ᴀ ʜɪɢʜ-ᴘᴇʀꜰᴏʀᴍᴀɴᴄᴇ ᴠᴘꜱ ɪɴꜱᴛᴀɴᴛʟʏ?\n\n"
            "🌐 <b>ᴠɪꜱɪᴛ ᴏᴜʀ ᴡᴇʙꜱɪᴛᴇ:</b> <a href='https://swapihost.in'>swapihost.in</a>\n"
            "💬 <b>ᴄᴏɴᴛᴀᴄᴛ ᴀᴅᴍɪɴ:</b> @swapibhai\n\n"
            "<i>ɢᴇᴛ 24/7 ᴜᴘᴛɪᴍᴇ, ᴅᴇᴅɪᴄᴀᴛᴇᴅ ʀᴇꜱᴏᴜʀᴄᴇꜱ, ᴀɴᴅ ᴘʀᴇᴍɪᴜᴍ ꜱᴜᴘᴘᴏʀᴛ!</i>"
        )
        await update.message.reply_text(buy_text, parse_mode=ParseMode.HTML)
        
    elif "❓" in text:
        help_text = (
            "🤖 <b>ᴠᴘꜱ ʙᴏᴛ ꜱᴜᴘᴘᴏʀᴛ:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "ʜᴀᴠɪɴɢ ᴛʀᴏᴜʙʟᴇ ᴡɪᴛʜ ʏᴏᴜʀ ᴠᴘꜱ ᴏʀ ɴᴇᴇᴅ ᴀꜱꜱɪꜱᴛᴀɴᴄᴇ?\n\n"
            "💬 <b>ᴅɪʀᴇᴄᴛ ꜱᴜᴘᴘᴏʀᴛ:</b> @swapibhai\n"
            "🌐 <b>ᴡᴇʙꜱɪᴛᴇ:</b> <a href='https://swapihost.in'>swapihost.in</a>\n\n"
            "<i>ᴡᴇ ᴀʀᴇ ʜᴇʀᴇ ᴛᴏ ʜᴇʟᴘ!</i>"
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    username = query.from_user.username or str(user_id)
    
    # Check force join for button clicks (unless it's checking join)
    if data != "main_menu":
        fj_check = await check_force_join(user_id, context)
        if fj_check is not True:
            await cmd_start(update, context)
            return

    if data == "main_menu":
        await cmd_start(update, context)
        
    elif data.startswith("deploy_"):
        await handle_create_vps(query, context, "ubuntu", user_id, username)
        
    elif data == "list_vps":
        vps_list = get_user_vps(user_id)
        if not vps_list:
            keyboard = [[InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data="main_menu")]]
            await query.message.edit_text("❌ <b>ʏᴏᴜ ʜᴀᴠᴇ ɴᴏ ᴠᴘꜱ ɪɴꜱᴛᴀɴᴄᴇꜱ.</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        keyboard = []
        for v in vps_list[:10]:
            status_emoji = "🟢" if check_proot_status(v['container_id']) == "running" else "🔴"
            upgraded = "💎 " if v['upgraded'] == 1 else ""
            keyboard.append([InlineKeyboardButton(f"{upgraded}{status_emoji} {v['container_name']}", callback_data=f"manage_{v['container_id']}")])
        
        keyboard.append([InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ᴍᴀɪɴ ᴍᴇɴᴜ", callback_data="main_menu")])
        await query.message.edit_text("🖥 <b>Your VPS Instances:</b>\nSelect one to manage:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data.startswith("manage_"):
        vps_id = data.replace("manage_", "")
        vps = get_vps_by_identifier(user_id, vps_id)
        if not vps:
            await query.message.edit_text("❌ VPS not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="list_vps")]]))
            return
            
        status = check_proot_status(vps_id)
        response = f"ℹ️ <b>ᴠᴘꜱ: {vps['container_name']}</b>\n"
        response += f"ꜱᴛᴀᴛᴜꜱ: {status}\nɪᴅ: <code>{vps_id}</code>\n"
        
        is_upgraded = vps['upgraded'] == 1
        spec_text = "8ɢʙ ʀᴀᴍ | 4 ᴄᴘᴜ" if is_upgraded else f"{vps['ram']} ʀᴀᴍ | {vps['cpu']} ᴄᴘᴜ"
        vip_tag = " 💎 [ᴠɪᴘ]" if is_upgraded else ""
        
        response += f"ᴏꜱ: ᴜʙᴜɴᴛᴜ 22.04 ʟᴛꜱ{vip_tag}\n"
        response += f"ꜱᴘᴇᴄꜱ: {spec_text}\n"
        
        if vps['expires_at']:
            response += f"ᴇxᴘɪʀᴇꜱ ᴀᴛ: {vps['expires_at']}\n"
        
        keyboard = [
            [InlineKeyboardButton("▶️ ꜱᴛᴀʀᴛ", callback_data=f"action_start_{vps_id}"),
             InlineKeyboardButton("⏹ ꜱᴛᴏᴘ", callback_data=f"action_stop_{vps_id}")],
            [InlineKeyboardButton("🔄 ʀᴇꜱᴛᴀʀᴛ", callback_data=f"action_restart_{vps_id}"),
             InlineKeyboardButton("🔑 ɢᴇɴ ꜱꜱʜ", callback_data=f"action_genssh_{vps_id}")],
            [InlineKeyboardButton("❌ ᴅᴇʟᴇᴛᴇ", callback_data=f"action_delete_{vps_id}")],
            [InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ʟɪꜱᴛ", callback_data="list_vps")]
        ]
        await query.message.edit_text(response, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data.startswith("action_"):
        parts = data.split("_")
        action = parts[1]
        vps_id = parts[2]
        
        vps = get_vps_by_identifier(user_id, vps_id)
        if not vps:
            await query.message.edit_text("❌ VPS not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="list_vps")]]))
            return
            
        if action in ["start", "stop", "restart"]:
            await query.message.edit_text(f"⏳ <b>ᴘʀᴏᴄᴇꜱꜱɪɴɢ '{action.upper()}' ᴀᴄᴛɪᴏɴ...</b>", parse_mode=ParseMode.HTML)
            if action == "start": 
                proc = await async_proot_start(vps_id)
                success = True
                ssh_line = await capture_ssh_session_line(proc)
                if ssh_line:
                    update_vps_ssh(vps_id, ssh_line)
                    try:
                        await context.bot.send_message(chat_id=user_id, text=f"✅ <b>ɴᴇᴡ ꜱꜱʜ ꜱᴇꜱꜱɪᴏɴ ɢᴇɴᴇʀᴀᴛᴇᴅ:</b>\n<code>{ssh_line}</code>", parse_mode=ParseMode.HTML)
                    except: pass
            elif action == "stop": 
                success = await async_proot_stop(vps_id)
            elif action == "restart": 
                await async_proot_stop(vps_id)
                proc = await async_proot_start(vps_id)
                success = True
                ssh_line = await capture_ssh_session_line(proc)
                if ssh_line:
                    update_vps_ssh(vps_id, ssh_line)
            
            if success:
                update_vps_status(vps_id, "running" if action in ["start", "restart"] else "stopped")
            
            keyboard = [[InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ᴠᴘꜱ", callback_data=f"manage_{vps_id}")]]
            text = f"✅ <b>ᴀᴄᴛɪᴏɴ '{action.upper()}' ᴄᴏᴍᴘʟᴇᴛᴇᴅ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ.</b>" if success else f"❌ <b>ᴀᴄᴛɪᴏɴ '{action.upper()}' ꜰᴀɪʟᴇᴅ.</b>"
            await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

        elif action == "delete":
            await query.message.edit_text("⏳ <b>ʀᴇᴍᴏᴠɪɴɢ ᴠᴘꜱ...</b>", parse_mode=ParseMode.HTML)
            await async_proot_stop(vps_id)
            await async_proot_rm(vps_id)
            delete_vps(vps_id)
            keyboard = [[InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ʟɪꜱᴛ", callback_data="list_vps")]]
            await query.message.edit_text("✅ <b>ᴠᴘꜱ ʀᴇᴍᴏᴠᴇᴅ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ.</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
            
        elif action == "genssh":
            await query.message.edit_text("⏳ <b>ɢᴇɴᴇʀᴀᴛɪɴɢ ꜱꜱʜ ꜱᴇꜱꜱɪᴏɴ...</b>", parse_mode=ParseMode.HTML)
            import asyncio
            await asyncio.sleep(1)
            ssh_line = vps['ssh_command']
            status = check_proot_status(vps_id)
            if not ssh_line or status != "running":
                await query.message.edit_text("❌ <b>ᴠᴘꜱ ɪꜱ ɴᴏᴛ ʀᴜɴɴɪɴɢ ᴏʀ ɴᴏ ꜱꜱʜ ꜱᴇꜱꜱɪᴏɴ ᴀᴄᴛɪᴠᴇ. ᴘʟᴇᴀꜱᴇ ꜱᴛᴀʀᴛ ᴏʀ ʀᴇꜱᴛᴀʀᴛ ɪᴛ.</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data=f"manage_{vps_id}")]]))
                return
            await query.message.edit_text(f"🔑 <b>ʏᴏᴜʀ ꜱꜱʜ ᴄᴏᴍᴍᴀɴᴅ:</b>\n\n<code>{ssh_line}</code>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data=f"manage_{vps_id}")]]))


# ----------------- Admin Panel -----------------

def get_admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Set Global Limit", callback_data="admin_set_limit"),
         InlineKeyboardButton("⌛ Set Expiry (Days)", callback_data="admin_set_expiry")],
        [InlineKeyboardButton("💻 Set Default RAM", callback_data="admin_set_ram"),
         InlineKeyboardButton("💻 Set Default CPU", callback_data="admin_set_cpu")],
        [InlineKeyboardButton("💾 Set Default Disk", callback_data="admin_set_disk")],
        [InlineKeyboardButton("➕ Add Force Join", callback_data="admin_add_fj"),
         InlineKeyboardButton("📁 Manage FJ", callback_data="admin_manage_fj")],
        [InlineKeyboardButton("🖼️ Set Banner Image", callback_data="admin_set_banner")],
        [InlineKeyboardButton("🔙 Exit Admin", callback_data="admin_exit")]
    ])

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text("⚙️ **Admin Control Panel**\n\nControl everything from here.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_kb())
    return ConversationHandler.END

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Access Denied", show_alert=True)
        return ConversationHandler.END
        
    await query.answer()
    data = query.data
    
    if data == "admin_exit":
        await query.message.edit_text("✅ Exited Admin Panel.")
        return ConversationHandler.END
        
    elif data == "admin_set_limit":
        await query.message.edit_text("Send the new Total Server Limit (Number):")
        return ADMIN_SET_LIMIT
        
    elif data == "admin_set_expiry":
        await query.message.edit_text("Send the new Default Expiry in days (e.g., 30). Send 0 for no expiry:")
        return ADMIN_SET_EXPIRY
        
    elif data == "admin_set_ram":
        await query.message.edit_text("Send the new Default RAM (e.g., 2g, 4g):")
        return ADMIN_SET_RAM
        
    elif data == "admin_set_cpu":
        await query.message.edit_text("Send the new Default CPU (e.g., 1, 2):")
        return ADMIN_SET_CPU
        
    elif data == "admin_set_disk":
        await query.message.edit_text("Send the new Default Disk (e.g., 10G, 20G):")
        return ADMIN_SET_DISK
        
    elif data == "admin_set_banner":
        await query.message.edit_text("Please send the photo you want to set as the bot's banner.")
        return ADMIN_SET_BANNER
        
    elif data == "admin_add_fj":
        await query.message.edit_text("Please forward a message from the channel or group to add it to Force Join.\nEnsure the bot is an admin there.")
        return ADMIN_ADD_FJ
        
    elif data == "admin_manage_fj":
        chats = get_force_join_chats()
        if not chats:
            await query.message.edit_text("No Force Join channels added.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]))
            return ConversationHandler.END
        text = "📁 <b>Currently Required Chats:</b>\n\n"
        buttons = []
        for chat_id, title, url, chat_type in chats:
            text += f"• <b>{title}</b> (<code>{chat_id}</code>)\n"
            buttons.append([InlineKeyboardButton(f"❌ Remove {title}", callback_data=f"remove_fj_{chat_id}")])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_back")])
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return ConversationHandler.END
        
    elif data == "admin_back":
        await query.message.edit_text("⚙️ **Admin Control Panel**\n\nControl everything from here.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_kb())
        return ConversationHandler.END

    elif data.startswith("remove_fj_"):
        chat_id = int(data.split("_")[2])
        remove_force_join(chat_id)
        await query.message.edit_text(f"✅ Removed chat `{chat_id}`.", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_manage_fj")]]))
        return ConversationHandler.END

    return ConversationHandler.END

async def admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE, state: int, key: str, success_msg: str):
    text = update.message.text
    set_setting(key, text)
    await update.message.reply_text(f"✅ {success_msg} {text}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_back")]]))
    return ConversationHandler.END

async def set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_input(update, context, ADMIN_SET_LIMIT, 'TOTAL_SERVER_LIMIT', "Total Server Limit updated to")
async def set_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_input(update, context, ADMIN_SET_EXPIRY, 'DEFAULT_EXPIRY_DAYS', "Default Expiry Days updated to")
async def set_ram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_input(update, context, ADMIN_SET_RAM, 'DEFAULT_RAM', "Default RAM updated to")
async def set_cpu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_input(update, context, ADMIN_SET_CPU, 'DEFAULT_CPU', "Default CPU updated to")
async def set_disk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_input(update, context, ADMIN_SET_DISK, 'DEFAULT_DISK', "Default Disk updated to")

async def set_banner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = update.message.photo[-1].file_id
    set_setting('BANNER_FILE_ID', file_id)
    await update.message.reply_text("✅ Banner Updated Successfully!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_back")]]))
    return ConversationHandler.END

async def add_fj_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.forward_origin:
        await update.message.reply_text("❌ Error: Please forward a message from the actual channel/group.", reply_markup=get_admin_kb())
        return ConversationHandler.END
        
    try:
        if hasattr(update.message.forward_origin, 'chat'):
            chat = update.message.forward_origin.chat
            chat_id = chat.id
            title = chat.title
            chat_type = chat.type
            
            chat_full = await context.bot.get_chat(chat_id)
            invite_link = chat_full.invite_link
            if not invite_link:
                invite_link = f"https://t.me/{chat.username}" if chat.username else None
                
            if not invite_link:
                await update.message.reply_text("❌ Error: Bot cannot find an invite link. Ensure bot is an admin with invite permissions.", reply_markup=get_admin_kb())
                return ConversationHandler.END
                
            add_force_join(chat_id, title, invite_link, chat_type)
            await update.message.reply_text(f"✅ <b>Successfully Added!</b>\nTitle: <code>{title}</code>\nType: <code>{chat_type}</code>\nLink: {invite_link}", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_back")]]))
        else:
            await update.message.reply_text("❌ Could not get chat info from forwarded message.", reply_markup=get_admin_kb())
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=get_admin_kb())
        
    return ConversationHandler.END

# ----------------- Background Tasks -----------------

async def sync_vps_statuses(context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT container_id, status, expires_at FROM vps')
    rows = cursor.fetchall()
    conn.close()
    
    for row in rows:
        cid = row['container_id']
        stat = row['status']
        exp = row['expires_at']
        
        if exp:
            try:
                # Naive parsing since sqlite datetime is naive UTC usually
                expiry_date = datetime.strptime(exp, '%Y-%m-%d %H:%M:%S')
                if datetime.utcnow() > expiry_date:
                    logger.info(f"VPS {cid} expired. Terminating.")
                    await async_proot_stop(cid)
                    await async_proot_rm(cid)
                    delete_vps(cid)
                    continue
            except Exception as e:
                logger.error(f"Error checking expiry for {cid}: {e}")
                
        out = check_proot_status(cid)
        if out != stat:
            update_vps_status(cid, out)

# ----------------- Main -----------------

def main():
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN is missing from environment variables.")
        sys.exit(1)
        
    download_rootfs()
        
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("panel", cmd_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyboard_buttons))
    
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", cmd_admin), CallbackQueryHandler(admin_callback, pattern="^admin_")],
        states={
            ADMIN_SET_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_limit)],
            ADMIN_SET_EXPIRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_expiry)],
            ADMIN_SET_RAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_ram)],
            ADMIN_SET_CPU: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_cpu)],
            ADMIN_SET_DISK: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_disk)],
            ADMIN_SET_BANNER: [MessageHandler(filters.PHOTO, set_banner)],
            ADMIN_ADD_FJ: [MessageHandler(filters.FORWARDED, add_fj_chat)]
        },
        fallbacks=[CommandHandler("admin", cmd_admin), CallbackQueryHandler(admin_callback, pattern="^admin_")],
        allow_reentry=True
    )
    application.add_handler(admin_conv)
    application.add_handler(CallbackQueryHandler(button_handler))
    
    job_queue = application.job_queue
    job_queue.run_repeating(sync_vps_statuses, interval=300, first=10)
    
    logger.info("Telegram Bot (PRoot Edition) started.")
    application.run_polling()

if __name__ == '__main__':
    main()
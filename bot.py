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
import json
from dotenv import load_dotenv
from datetime import datetime, timezone
from aiohttp import web
from pyngrok import ngrok, conf

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from telegram.constants import ParseMode

ADMIN_SET_LIMIT, ADMIN_SET_RAM, ADMIN_SET_CPU, ADMIN_SET_DISK, ADMIN_SET_BANNER, ADMIN_ADD_FJ, ADMIN_SET_EXPIRY, ADMIN_SET_NODE_LIMIT, ADMIN_BROADCAST = range(9)

# Load environment variables
load_dotenv()

# Configuration from .env
TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
BOT_STATUS_NAME = os.getenv('BOT_STATUS_NAME', 'UnixNodes')
WATERMARK = os.getenv('WATERMARK', 'Powered by UnixNodes VPS Bot')
NGROK_AUTHTOKEN = os.getenv('NGROK_AUTHTOKEN', '')
NGROK_DOMAIN = os.getenv('NGROK_DOMAIN', '')

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
    if 'node_id' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN node_id INTEGER DEFAULT 1")
    
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
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nodes (
            node_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            last_ping TIMESTAMP
        )
    ''')
    
    cursor.execute("PRAGMA table_info(nodes)")
    node_columns = [col[1] for col in cursor.fetchall()]
    if 'ram' not in node_columns:
        cursor.execute("ALTER TABLE nodes ADD COLUMN ram TEXT DEFAULT 'Unknown'")
    if 'cpu' not in node_columns:
        cursor.execute("ALTER TABLE nodes ADD COLUMN cpu TEXT DEFAULT 'Unknown'")
    if 'disk' not in node_columns:
        cursor.execute("ALTER TABLE nodes ADD COLUMN disk TEXT DEFAULT 'Unknown'")
    if 'max_vps' not in node_columns:
        cursor.execute("ALTER TABLE nodes ADD COLUMN max_vps INTEGER DEFAULT 5")
        
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            vps_id TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            result TEXT,
            node_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (node_id) REFERENCES nodes (node_id)
        )
    ''')
    
    # Ensure there is at least a local node
    cursor.execute('SELECT 1 FROM nodes WHERE node_id = 1')
    if not cursor.fetchone():
        cursor.execute("INSERT INTO nodes (node_id, name) VALUES (1, 'Local Node')")
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
    is_new = False
    if cursor.fetchone():
        cursor.execute('UPDATE users SET username = ? WHERE user_id = ?', (username, user_id))
    else:
        cursor.execute('INSERT INTO users (user_id, username, referred_by) VALUES (?, ?, ?)', (user_id, username, referred_by))
        is_new = True
    conn.commit()
    conn.close()
    return is_new

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
        
        if update.callback_query:
            try:
                await update.callback_query.message.delete()
            except: pass
            
        chat_id = update.effective_chat.id
        
        if banner:
            try:
                await context.bot.send_photo(chat_id=chat_id, photo=banner, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
                return
            except Exception:
                pass
                
        await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    # Handle Referral System
    is_new = False
    referred_by_id = None
    if update.message and context.args:
        try:
            referred_by = int(context.args[0])
            if referred_by != user_id:
                referred_by_id = referred_by
                is_new = add_user(user_id, username, referred_by)
            else:
                is_new = add_user(user_id, username)
        except ValueError:
            is_new = add_user(user_id, username)
    else:
        is_new = add_user(user_id, username)
        
    if is_new and referred_by_id:
        try:
            await context.bot.send_message(
                chat_id=referred_by_id,
                text=f"🎉 <b>ɴᴇᴡ ʀᴇꜰᴇʀʀᴀʟ!</b>\n━━━━━━━━━━━━━━━━━━━━\n👤 <b>{username}</b> ʜᴀꜱ ᴊᴏɪɴᴇᴅ ᴜꜱɪɴɢ ʏᴏᴜʀ ʟɪɴᴋ!\n🎁 ʏᴏᴜ ᴇᴀʀɴᴇᴅ <b>1 ɪɴᴠɪᴛᴇ ᴘᴏɪɴᴛ</b>.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to notify referrer: {e}")

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
    
    if update.callback_query:
        try:
            await update.callback_query.message.delete()
        except:
            pass
            
    chat_id = update.effective_chat.id
    
    if banner:
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=banner, caption=msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
            return
        except Exception:
            pass
            
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())

async def cmd_nodes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
        
    conn = get_db_connection()
    cursor = conn.cursor()
    # Use left join or group by if needed, but since workers just ping /jobs, we might just look at nodes table
    cursor.execute("SELECT node_id, name, status, last_ping FROM nodes ORDER BY node_id DESC LIMIT 20")
    nodes = cursor.fetchall()
    conn.close()
    
    if not nodes:
        await update.message.reply_text("❌ <b>ɴᴏ ᴡᴏʀᴋᴇʀ ɴᴏᴅᴇꜱ ᴄᴏɴɴᴇᴄᴛᴇᴅ.</b>", parse_mode=ParseMode.HTML)
        return
        
    text = "🖥 <b>ᴄᴏɴɴᴇᴄᴛᴇᴅ ᴡᴏʀᴋᴇʀ ɴᴏᴅᴇꜱ:</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for n in nodes:
        status_emoji = "🟢" if n['status'] == "active" else "🔴"
        last_ping = n['last_ping'] if n['last_ping'] else "Unknown"
        text += f"{status_emoji} <b>{n['name']}</b>\n"
        text += f"• <b>ɪᴅ:</b> <code>{n['node_id']}</code>\n"
        text += f"• <b>ʟᴀꜱᴛ ᴘɪɴɢ:</b> {last_ping}\n\n"
        
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

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
        
    init_msg = "⏳ <b>ᴠᴘꜱ ᴅᴇᴘʟᴏʏᴍᴇɴᴛ ǫᴜᴇᴜᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━━━\nʏᴏᴜʀ ᴠᴘꜱ ɪꜱ ʙᴇɪɴɢ ᴄʀᴇᴀᴛᴇᴅ ᴏɴ ᴀ ᴡᴏʀᴋᴇʀ ɴᴏᴅᴇ. ᴛʜɪꜱ ᴍᴀʏ ᴛᴀᴋᴇ ᴜᴘ ᴛᴏ 60 ꜱᴇᴄᴏɴᴅꜱ.\n\nʏᴏᴜ ᴡɪʟʟ ʀᴇᴄᴇɪᴠᴇ ᴀ ᴍᴇꜱꜱᴀɢᴇ ᴡɪᴛʜ ʏᴏᴜʀ ꜱꜱʜ ᴄᴏᴍᴍᴀɴᴅ ᴏɴᴄᴇ ɪᴛ ɪꜱ ʀᴇᴀᴅʏ."
    msg = await message.reply_text(init_msg, parse_mode=ParseMode.HTML) if is_message else await update_or_query.message.edit_text(init_msg, parse_mode=ParseMode.HTML)
    
    vps_id = str(uuid.uuid4())[:8]
    hostname = f"{VPS_HOSTNAME}-{user_id}"
    container_name = f"vps-{user_id}-{vps_id}"
    
    ram = get_setting('DEFAULT_RAM', DEFAULT_RAM)
    cpu = get_setting('DEFAULT_CPU', DEFAULT_CPU)
    disk = get_setting('DEFAULT_DISK', DEFAULT_DISK)
    
    add_vps(user_id, vps_id, container_name, "ubuntu", hostname, "Pending...", ram=ram, cpu=cpu, disk=disk)
    
    job_id = str(uuid.uuid4())
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO jobs (job_id, vps_id, action) VALUES (?, ?, 'create')", (job_id, vps_id))
    conn.commit()
    conn.close()
    
    try:
        msg = (
            f"🔔 <b>New VPS Queued!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>User:</b> {username} (<code>{user_id}</code>)\n"
            f"🖥 <b>Container:</b> <code>{container_name}</code>\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode=ParseMode.HTML)
    except: pass


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
            
        if action in ["start", "stop", "restart", "delete"]:
            await query.message.edit_text(f"⏳ <b>ǫᴜᴇᴜɪɴɢ '{action.upper()}' ᴀᴄᴛɪᴏɴ...</b>", parse_mode=ParseMode.HTML)
            
            job_id = str(uuid.uuid4())
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO jobs (job_id, vps_id, action, node_id) VALUES (?, ?, ?, ?)", (job_id, vps_id, action, vps['node_id']))
            conn.commit()
            conn.close()
            
            if action == "delete":
                keyboard = [[InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ʟɪꜱᴛ", callback_data="list_vps")]]
                text = f"✅ <b>ᴀᴄᴛɪᴏɴ '{action.upper()}' ʜᴀꜱ ʙᴇᴇɴ ǫᴜᴇᴜᴇᴅ. ᴠᴘꜱ ᴡɪʟʟ ʙᴇ ʀᴇᴍᴏᴠᴇᴅ.</b>"
            else:
                keyboard = [[InlineKeyboardButton("🔙 ʙᴀᴄᴋ ᴛᴏ ᴠᴘꜱ", callback_data=f"manage_{vps_id}")]]
                text = f"✅ <b>ᴀᴄᴛɪᴏɴ '{action.upper()}' ʜᴀꜱ ʙᴇᴇɴ ǫᴜᴇᴜᴇᴅ ᴛᴏ ᴀ ᴡᴏʀᴋᴇʀ ɴᴏᴅᴇ.</b>"
            await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
            
        elif action == "genssh":
            await query.message.edit_text("⏳ <b>ɢᴇɴᴇʀᴀᴛɪɴɢ ꜱꜱʜ ꜱᴇꜱꜱɪᴏɴ...</b>", parse_mode=ParseMode.HTML)
            import asyncio
            await asyncio.sleep(1)
            ssh_line = vps['ssh_command']
            status = check_proot_status(vps_id)
            if not ssh_line or status != "running":
                await query.message.edit_text("❌ <b>ᴠᴘꜱ ɪꜱ ɴᴏᴛ ʀᴜɴɴɪɴɢ ᴏʀ ɴᴏ ꜱꜱʜ ꜱᴇꜱꜱɪᴏɴ ᴀᴄᴛɪᴠᴇ. ᴘʟᴇᴀꜱᴇ ꜱᴛᴀʀᴛ ᴏʀ ʀᴇꜱᴛᴀʀᴛ ɪᴛ.</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data=f"manage_{vps_id}")]]))
                return
            ssh_msg = (
                "✅ <b>ꜱꜱʜ ꜱᴇꜱꜱɪᴏɴ ʀᴇᴛʀɪᴇᴠᴇᴅ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "🔑 <b>ʏᴏᴜʀ ꜱꜱʜ ᴄᴏᴍᴍᴀɴᴅ:</b>\n"
                f"<code>{ssh_line}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<i>(ᴄᴏᴘʏ ᴛʜᴇ ᴀʙᴏᴠᴇ ᴄᴏᴍᴍᴀɴᴅ ᴀɴᴅ ᴘᴀꜱᴛᴇ ɪᴛ ɪɴ ᴛᴇʀᴍᴜx ᴏʀ ᴀɴʏ ꜱꜱʜ ᴄʟɪᴇɴᴛ ᴛᴏ ᴄᴏɴɴᴇᴄᴛ)</i>"
            )
            await query.message.edit_text(ssh_msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data=f"manage_{vps_id}")]]))


# ----------------- Admin Panel -----------------

def get_admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Set Global Limit", callback_data="admin_set_limit"),
         InlineKeyboardButton("⌛ Set Expiry (Days)", callback_data="admin_set_expiry")],
        [InlineKeyboardButton("💻 Set Default RAM", callback_data="admin_set_ram"),
         InlineKeyboardButton("💻 Set Default CPU", callback_data="admin_set_cpu")],
        [InlineKeyboardButton("💾 Set Default Disk", callback_data="admin_set_disk")],
        [InlineKeyboardButton("🖥️ Manage Nodes", callback_data="admin_manage_nodes"),
         InlineKeyboardButton("📊 Bot Status", callback_data="admin_status")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton("➕ Add Force Join", callback_data="admin_add_fj")],
        [InlineKeyboardButton("📁 Manage FJ", callback_data="admin_manage_fj"),
         InlineKeyboardButton("🖼️ Set Banner Image", callback_data="admin_set_banner")],
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
        
    elif data == "admin_broadcast":
        await query.message.edit_text("📢 <b>Send the message you want to broadcast (Text, Photo, Video, etc.):</b>", parse_mode=ParseMode.HTML)
        return ADMIN_BROADCAST
        
    elif data == "admin_status":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM vps")
        vps = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM vps WHERE status='running'")
        vps_running = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM nodes WHERE status='active'")
        nodes = cursor.fetchone()[0]
        conn.close()
        
        text = f"📊 <b>Bot Statistics</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        text += f"👥 <b>Total Users:</b> {users}\n"
        text += f"🖥 <b>Total VPS:</b> {vps} ({vps_running} Running)\n"
        text += f"🌐 <b>Active Worker Nodes:</b> {nodes}\n"
        
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]))
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

    elif data == "admin_manage_nodes":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT node_id, name, status FROM nodes ORDER BY node_id DESC LIMIT 20")
        nodes = cursor.fetchall()
        conn.close()
        
        if not nodes:
            await query.message.edit_text("❌ No Worker Nodes connected.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]))
            return ConversationHandler.END
            
        buttons = []
        for n in nodes:
            emoji = "🟢" if n['status'] == "active" else "🔴"
            buttons.append([InlineKeyboardButton(f"{emoji} {n['name']} (ID:{n['node_id']})", callback_data=f"admin_node_{n['node_id']}")])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_back")])
        await query.message.edit_text("🖥 **Worker Nodes Management**\nSelect a node to view stats and manage limits:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
        return ConversationHandler.END
        
    elif data.startswith("admin_node_"):
        node_id = int(data.split("_")[2])
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,))
        node = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM vps WHERE node_id = ?", (node_id,))
        vps_count = cursor.fetchone()[0]
        conn.close()
        
        if not node:
            await query.message.edit_text("❌ Node not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_manage_nodes")]]))
            return ConversationHandler.END
            
        text = f"🖥 <b>Node:</b> {node['name']}\n"
        text += f"━━━━━━━━━━━━━━━━━━━━\n"
        text += f"<b>Status:</b> {node['status'].upper()}\n"
        text += f"<b>RAM:</b> {node['ram'] or 'Unknown'}\n"
        text += f"<b>CPU:</b> {node['cpu'] or 'Unknown'}\n"
        text += f"<b>Disk:</b> {node['disk'] or 'Unknown'}\n"
        text += f"<b>VPS Hosted:</b> {vps_count} / {node['max_vps'] or 5}\n"
        text += f"<b>Last Ping:</b> {node['last_ping']}\n"
        
        buttons = [
            [InlineKeyboardButton("📊 Set VPS Limit", callback_data=f"admin_set_node_limit_{node_id}")],
            [InlineKeyboardButton("🔙 Back to Nodes", callback_data="admin_manage_nodes")]
        ]
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return ConversationHandler.END
        
    elif data.startswith("admin_set_node_limit_"):
        node_id = int(data.split("_")[4])
        context.user_data['edit_node_id'] = node_id
        await query.message.edit_text(f"Send the new maximum VPS limit for Node {node_id} (Number):")
        return ADMIN_SET_NODE_LIMIT

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

async def set_node_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    node_id = context.user_data.get('edit_node_id')
    try:
        limit = int(update.message.text)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE nodes SET max_vps = ? WHERE node_id = ?", (limit, node_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ Node {node_id} maximum VPS limit updated to {limit}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Nodes", callback_data="admin_manage_nodes")]]))
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Nodes", callback_data="admin_manage_nodes")]]))
    return ConversationHandler.END

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

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    await msg.reply_text("⏳ <b>Broadcasting message...</b>", parse_mode=ParseMode.HTML)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    conn.close()
    
    success = 0
    failed = 0
    for u in users:
        try:
            await msg.copy(chat_id=u['user_id'])
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
            
    await msg.reply_text(f"✅ <b>Broadcast Complete!</b>\nSuccess: {success}\nFailed: {failed}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_back")]]), parse_mode=ParseMode.HTML)
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

# ----------------- Master API -----------------

async def api_register(request):
    data = await request.json()
    name = data.get('name', 'Unknown Node')
    ram = data.get('ram', 'Unknown')
    cpu = data.get('cpu', 'Unknown')
    disk = data.get('disk', 'Unknown')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO nodes (name, status, last_ping, ram, cpu, disk) VALUES (?, 'active', CURRENT_TIMESTAMP, ?, ?, ?)", (name, ram, cpu, disk))
    node_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    try:
        bot_app = request.app.get('bot_app')
        if bot_app:
            msg = (
                f"🔔 <b>New Worker Node Connected!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🖥 <b>Name:</b> <code>{name}</code>\n"
                f"⚙️ <b>RAM:</b> {ram}\n"
                f"⚡️ <b>CPU:</b> {cpu}\n"
                f"💾 <b>Disk:</b> {disk}\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
            asyncio.create_task(bot_app.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode='HTML'))
    except Exception as e:
        logger.error(f"Failed to send admin notification: {e}")
    
    return web.json_response({'status': 'ok', 'node_id': node_id})

async def api_get_jobs(request):
    node_id = request.query.get('node_id')
    if not node_id:
        return web.json_response({'error': 'Missing node_id'}, status=400)
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE nodes SET last_ping = CURRENT_TIMESTAMP WHERE node_id = ?", (node_id,))
    
    # Check node capacity
    cursor.execute("SELECT COUNT(*) FROM vps WHERE node_id = ?", (node_id,))
    vps_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT max_vps FROM nodes WHERE node_id = ?", (node_id,))
    node_data = cursor.fetchone()
    max_vps = node_data[0] if node_data else 5
    
    # Fetch job logic:
    # 1. First, always try to get a job specifically assigned to this node (like start, stop, restart for its own VPSs)
    cursor.execute("SELECT * FROM jobs WHERE status = 'pending' AND node_id = ? ORDER BY created_at ASC LIMIT 1", (node_id,))
    job = cursor.fetchone()
    
    # 2. If no specific job, and we have capacity, look for a new unassigned 'create' job
    if not job and vps_count < max_vps:
        cursor.execute("SELECT * FROM jobs WHERE status = 'pending' AND action = 'create' AND (node_id IS NULL OR node_id = ?) ORDER BY created_at ASC LIMIT 1", (node_id,))
        job = cursor.fetchone()
    
    if job:
        cursor.execute("UPDATE jobs SET status = 'running', node_id = ? WHERE job_id = ?", (node_id, job['job_id']))
        conn.commit()
        conn.close()
        return web.json_response({'job': dict(job)})
    
    conn.commit()
    conn.close()
    return web.json_response({'job': None})

async def api_post_result(request):
    data = await request.json()
    job_id = data.get('job_id')
    status = data.get('status')
    result = data.get('result', '')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE jobs SET status = ?, result = ? WHERE job_id = ?", (status, result, job_id))
    conn.commit()
    conn.close()
    
    return web.json_response({'status': 'ok'})

async def start_master_api(application: Application):
    webapp = web.Application()
    webapp['bot_app'] = application
    webapp.add_routes([
        web.post('/register', api_register),
        web.get('/jobs', api_get_jobs),
        web.post('/jobs/result', api_post_result)
    ])
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 5000)
    await site.start()
    logger.info("Master API Server started on port 5000")
    
    if NGROK_AUTHTOKEN and NGROK_DOMAIN:
        try:
            conf.get_default().auth_token = NGROK_AUTHTOKEN
            url = ngrok.connect(5000, domain=NGROK_DOMAIN).public_url
            logger.info(f"Ngrok tunnel established at: {url}")
        except Exception as e:
            logger.error(f"Failed to start Ngrok tunnel: {e}")

async def monitor_completed_jobs(application: Application):
    while True:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM jobs WHERE status IN ('completed', 'failed')")
            finished_jobs = cursor.fetchall()
            
            for job in finished_jobs:
                job_id = job['job_id']
                vps_id = job['vps_id']
                action = job['action']
                status = job['status']
                result = job['result']
                
                cursor.execute("SELECT user_id FROM vps WHERE container_id = ?", (vps_id,))
                vps_row = cursor.fetchone()
                if not vps_row:
                    cursor.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
                    continue
                user_id = vps_row['user_id']
                
                if action == "start":
                    if status == "completed":
                        update_vps_ssh(vps_id, result)
                        update_vps_status(vps_id, "running")
                        try:
                            ssh_msg = (
                                "✅ <b>ɴᴇᴡ ꜱꜱʜ ꜱᴇꜱꜱɪᴏɴ ɢᴇɴᴇʀᴀᴛᴇᴅ!</b> 🎉\n"
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                "🔑 <b>ꜱꜱʜ ᴀᴄᴄᴇꜱꜱ ᴄᴏᴍᴍᴀɴᴅ:</b>\n"
                                f"<code>{result}</code>\n"
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                "<i>(ᴄᴏᴘʏ ᴛʜᴇ ᴀʙᴏᴠᴇ ᴄᴏᴍᴍᴀɴᴅ ᴀɴᴅ ᴘᴀꜱᴛᴇ ɪᴛ ɪɴ ᴛᴇʀᴍᴜx ᴏʀ ᴀɴʏ ꜱꜱʜ ᴄʟɪᴇɴᴛ ᴛᴏ ᴄᴏɴɴᴇᴄᴛ)</i>"
                            )
                            await application.bot.send_message(chat_id=user_id, text=ssh_msg, parse_mode=ParseMode.HTML)
                        except: pass
                    else:
                        try: await application.bot.send_message(chat_id=user_id, text=f"❌ Failed to start VPS: {result}")
                        except: pass
                        
                elif action == "stop":
                    update_vps_status(vps_id, "stopped")
                elif action == "delete":
                    delete_vps(vps_id)
                elif action == "restart":
                    if status == "completed":
                        update_vps_ssh(vps_id, result)
                        update_vps_status(vps_id, "running")
                elif action == "create":
                    if status == "completed":
                        update_vps_ssh(vps_id, result)
                        update_vps_status(vps_id, "running")
                        cursor.execute("UPDATE vps SET node_id = ? WHERE container_id = ?", (job['node_id'], vps_id))
                        try:
                            ssh_msg = (
                                "✅ <b>ɴᴇᴡ ꜱꜱʜ ꜱᴇꜱꜱɪᴏɴ ɢᴇɴᴇʀᴀᴛᴇᴅ!</b> 🎉\n"
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                "🔑 <b>ꜱꜱʜ ᴀᴄᴄᴇꜱꜱ ᴄᴏᴍᴍᴀɴᴅ:</b>\n"
                                f"<code>{result}</code>\n"
                                "━━━━━━━━━━━━━━━━━━━━\n"
                                "<i>(ᴄᴏᴘʏ ᴛʜᴇ ᴀʙᴏᴠᴇ ᴄᴏᴍᴍᴀɴᴅ ᴀɴᴅ ᴘᴀꜱᴛᴇ ɪᴛ ɪɴ ᴛᴇʀᴍᴜx ᴏʀ ᴀɴʏ ꜱꜱʜ ᴄʟɪᴇɴᴛ ᴛᴏ ᴄᴏɴɴᴇᴄᴛ)</i>"
                            )
                            await application.bot.send_message(chat_id=user_id, text=ssh_msg, parse_mode=ParseMode.HTML)
                        except: pass
                
                cursor.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
                
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error monitoring jobs: {e}")
            
        await asyncio.sleep(5)

async def post_init(application: Application):
    await start_master_api(application)
    asyncio.create_task(monitor_completed_jobs(application))

# ----------------- Main -----------------

def main():
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN is missing from environment variables.")
        sys.exit(1)
        
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("panel", cmd_start))
    application.add_handler(CommandHandler("nodes", cmd_nodes))
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
            ADMIN_ADD_FJ: [MessageHandler(filters.FORWARDED, add_fj_chat)],
            ADMIN_SET_NODE_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_node_limit)],
            ADMIN_BROADCAST: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_message)]
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
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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

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

def add_user(user_id, username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM bans WHERE user_id = ?', (user_id,))
    banned = cursor.fetchone() is not None
    conn.close()
    return banned

def add_vps(user_id, container_id, container_name, os_type, hostname, ssh_command, ram=DEFAULT_RAM, cpu=DEFAULT_CPU, disk=DEFAULT_DISK):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO vps (user_id, container_id, container_name, os_type, hostname, status, ssh_command, ram, cpu, disk, suspended)
        VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, 0)
    ''', (user_id, container_id, container_name, os_type, hostname, ssh_command, ram, cpu, disk))
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
            
        logger.info(f"RootFS ready for {vps_id}")
    return target_dir

async def async_proot_start(vps_id):
    target_dir = os.path.join(VPS_DATA_DIR, vps_id)
    # Install tmate and start it
    cmd = f"proot -0 -r {target_dir} -b /dev -b /proc -b /sys -w /root /bin/bash -c 'apt-get update >/dev/null && apt-get install -y tmate curl wget sudo openssh-client >/dev/null && tmate -F'"
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Deploy VPS (Ubuntu)", callback_data="deploy_ubuntu")],
        [InlineKeyboardButton("🖥 My VPS Instances", callback_data="list_vps")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "👋 <b>Welcome to UnixNodes VPS Bot! (PRoot Edition)</b>\n\nUse the buttons below to deploy and manage your VPS instances."
    if update.message:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.message.edit_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())

async def handle_create_vps(query, context, os_type, user_id, username):
    add_user(user_id, username)
    if is_banned(user_id):
        await query.message.edit_text("❌ You are banned from creating VPS instances.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))
        return
    if count_user_vps(user_id) >= SERVER_LIMIT:
        await query.message.edit_text(f"❌ Limit of {SERVER_LIMIT} VPS instances reached.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))
        return
    if get_total_instances() >= TOTAL_SERVER_LIMIT:
        await query.message.edit_text("❌ Global server limit reached.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))
        return

    msg = await query.message.edit_text("⏳ Creating your VPS instance... This takes about 30-60 seconds (Extracting RootFS and Installing Packages).")
    
    vps_id = str(uuid.uuid4())[:8]
    hostname = f"{VPS_HOSTNAME}-{user_id}"
    container_name = f"vps-{user_id}-{vps_id}"
    
    await async_extract_rootfs(vps_id)
    
    proc = await async_proot_start(vps_id)
    
    ssh_line = await capture_ssh_session_line(proc)
    
    keyboard = [[InlineKeyboardButton("🖥 Go to My VPS", callback_data="list_vps")]]
    
    if ssh_line:
        add_vps(user_id, vps_id, container_name, "ubuntu", hostname, ssh_line)
        text = f"✅ <b>VPS Instance Created (PRoot)</b>\nOS: Ubuntu 22.04\n<code>{ssh_line}</code>"
        try:
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
            await msg.edit_text("✅ VPS created! Check your DMs for SSH details.", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            await msg.edit_text(f"✅ VPS created! Here are the details:\n\n{text}", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await msg.edit_text("❌ Creation failed: Unable to generate SSH session.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))
        await async_proot_stop(vps_id)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    username = query.from_user.username or str(user_id)
    
    if data == "main_menu":
        await cmd_start(update, context)
        
    elif data == "help":
        help_text = (
            "🤖 <b>VPS Bot Help:</b>\n\n"
            "Deploy VPS instances up to your limits. These are PRoot environments running within a container."
        )
        keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")]]
        await query.message.edit_text(help_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data.startswith("deploy_"):
        await handle_create_vps(query, context, "ubuntu", user_id, username)
        
    elif data == "list_vps":
        vps_list = get_user_vps(user_id)
        if not vps_list:
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
            await query.message.edit_text("❌ You have no VPS instances.", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        keyboard = []
        for v in vps_list[:10]:
            status_emoji = "🟢" if check_proot_status(v['container_id']) == "running" else "🔴"
            keyboard.append([InlineKeyboardButton(f"{status_emoji} {v['container_name']}", callback_data=f"manage_{v['container_id']}")])
        
        keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")])
        await query.message.edit_text("🖥 <b>Your VPS Instances:</b>\nSelect one to manage:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data.startswith("manage_"):
        vps_id = data.replace("manage_", "")
        vps = get_vps_by_identifier(user_id, vps_id)
        if not vps:
            await query.message.edit_text("❌ VPS not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="list_vps")]]))
            return
            
        status = check_proot_status(vps_id)
        response = f"ℹ️ <b>VPS: {vps['container_name']}</b>\n"
        response += f"Status: {status}\nID: <code>{vps_id}</code>\n"
        response += f"OS: Ubuntu 22.04 (PRoot)\n"
        
        keyboard = [
            [InlineKeyboardButton("▶️ Start", callback_data=f"action_start_{vps_id}"),
             InlineKeyboardButton("⏹ Stop", callback_data=f"action_stop_{vps_id}")],
            [InlineKeyboardButton("🔄 Restart", callback_data=f"action_restart_{vps_id}")],
            [InlineKeyboardButton("❌ Delete", callback_data=f"action_delete_{vps_id}")],
            [InlineKeyboardButton("🔙 Back to List", callback_data="list_vps")]
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
            await query.message.edit_text(f"⏳ Processing `{action}` action...", parse_mode=ParseMode.MARKDOWN)
            if action == "start": 
                proc = await async_proot_start(vps_id)
                success = True
                ssh_line = await capture_ssh_session_line(proc)
                if ssh_line:
                    update_vps_ssh(vps_id, ssh_line)
                    try:
                        await context.bot.send_message(chat_id=user_id, text=f"✅ <b>New SSH Session Generated:</b>\n<code>{ssh_line}</code>", parse_mode=ParseMode.HTML)
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
            
            keyboard = [[InlineKeyboardButton("🔙 Back to VPS", callback_data=f"manage_{vps_id}")]]
            text = f"✅ Action '{action.title()}' completed successfully." if success else f"❌ Action '{action.title()}' failed."
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

        elif action == "delete":
            await query.message.edit_text("⏳ Removing VPS...")
            await async_proot_stop(vps_id)
            await async_proot_rm(vps_id)
            delete_vps(vps_id)
            keyboard = [[InlineKeyboardButton("🔙 Back to List", callback_data="list_vps")]]
            await query.message.edit_text("✅ VPS Removed Successfully.", reply_markup=InlineKeyboardMarkup(keyboard))


# ----------------- Background Tasks -----------------

async def sync_vps_statuses(context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT container_id, status FROM vps')
    rows = cursor.fetchall()
    conn.close()
    
    for row in rows:
        cid = row['container_id']
        stat = row['status']
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
    application.add_handler(CallbackQueryHandler(button_handler))
    
    job_queue = application.job_queue
    job_queue.run_repeating(sync_vps_statuses, interval=300, first=10)
    
    logger.info("Telegram Bot (PRoot Edition) started.")
    application.run_polling()

if __name__ == '__main__':
    main()
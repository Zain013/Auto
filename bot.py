import os
import asyncio
import logging
import sqlite3
from contextlib import suppress

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

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
REMOVE = "🗑️ Remove Child"
WELCOME = "✏️ Edit Welcome"
ON = "🟢 AutoReq ON"
OFF = "🔴 AutoReq OFF"
PENDING = "📊 Pending Requests"
REFRESH = "🔄 Refresh"
CANCEL = "❌ Cancel"

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
db.commit()

db_lock = asyncio.Lock()
states = {}          # (bot_token, user_id) -> state
child_tasks = {}     # token -> asyncio.Task
father = Bot(FATHER_TOKEN)
dp = Dispatcher()


def menu(owner=False):
    rows = [
        [KeyboardButton(text=WELCOME)],
        [KeyboardButton(text=ON), KeyboardButton(text=OFF)],
        [KeyboardButton(text=PENDING), KeyboardButton(text=REFRESH)],
    ]
    if owner:
        rows = [
            [KeyboardButton(text=ADD)],
            [KeyboardButton(text=LIST), KeyboardButton(text=REMOVE)],
            *rows,
        ]
    rows.append([KeyboardButton(text=CANCEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def child_row(token):
    return db.execute("SELECT * FROM children WHERE token=?", (token,)).fetchone()


def children_rows():
    return db.execute("SELECT * FROM children ORDER BY username").fetchall()


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


async def pending_count(token):
    async with db_lock:
        return db.execute(
            "SELECT COUNT(*) FROM pending WHERE token=?", (token,)
        ).fetchone()[0]


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


async def approve_saved(token):
    async with db_lock:
        rows = db.execute(
            "SELECT chat_id, user_id FROM pending WHERE token=? ORDER BY rowid",
            (token,)
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
        if uid == OWNER_ID:
            await send(token, chat_id, "Child Bot Panel", menu())
        else:
            await send(token, chat_id, child["welcome"])
        return

    # All child controls are owner-only.
    if uid != OWNER_ID:
        return

    state_key = (token, uid)
    state = states.get(state_key)

    if text == CANCEL:
        states.pop(state_key, None)
        await send(token, chat_id, "Cancelled.", menu())
        return

    if state == "welcome":
        if not text:
            await send(token, chat_id, "Please send a text welcome message.")
            return
        async with db_lock:
            db.execute("UPDATE children SET welcome=? WHERE token=?", (text, token))
            db.commit()
        states.pop(state_key, None)
        await send(token, chat_id, "Welcome message updated.", menu())
        return

    if text == WELCOME:
        states[state_key] = "welcome"
        await send(token, chat_id, "Send the new welcome message. Use ❌ Cancel to stop.")
    elif text == ON:
        async with db_lock:
            db.execute("UPDATE children SET autoreq=1 WHERE token=?", (token,))
            db.commit()
        await send(token, chat_id, "AutoReq ON. Approving saved requests…", menu())
        await approve_saved(token)
        await send(
            token, chat_id,
            f"Finished. Saved requests still pending: {await pending_count(token)}",
            menu()
        )
    elif text == OFF:
        async with db_lock:
            db.execute("UPDATE children SET autoreq=0 WHERE token=?", (token,))
            db.commit()
        await send(token, chat_id, "AutoReq OFF. Requests received from now on will be saved, not approved.", menu())
    elif text in (PENDING, REFRESH):
        count = await pending_count(token)
        await send(token, chat_id, f"Saved pending requests: {count}", menu())


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
                    allowed_updates=["message", "chat_join_request"]
                )
                if not result.get("ok"):
                    log.warning("getUpdates failed for child: %s", result)
                    await asyncio.sleep(5)
                    continue

                for update in result.get("result", []):
                    offset = update["update_id"] + 1

                    request = update.get("chat_join_request")
                    if request:
                        chat_id = request["chat"]["id"]
                        user_id = request["from"]["id"]
                        async with db_lock:
                            db.execute("""
                                INSERT OR IGNORE INTO pending(token, chat_id, user_id)
                                VALUES (?, ?, ?)
                            """, (token, chat_id, user_id))
                            row = db.execute(
                                "SELECT autoreq FROM children WHERE token=?", (token,)
                            ).fetchone()
                            db.commit()

                        if row and row["autoreq"]:
                            await approve_request(session, token, chat_id, user_id)

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

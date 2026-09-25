import os
import re
import asyncio
import logging
import sqlite3
from contextlib import closing

import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ChatJoinRequest,
)
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError


# =========================================================
# CONFIG
# =========================================================

FATHER_TOKEN = os.getenv("FATHER_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "botdata.db").strip()

if not FATHER_TOKEN:
    raise RuntimeError("FATHER_TOKEN environment variable is missing.")

if not OWNER_ID_RAW:
    raise RuntimeError("OWNER_ID environment variable is missing.")

try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError:
    raise RuntimeError("OWNER_ID must be a numeric Telegram user ID.")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("AUTU")


# =========================================================
# DATABASE
# =========================================================

db_lock = asyncio.Lock()


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS children (
                token TEXT PRIMARY KEY,
                bot_id INTEGER UNIQUE NOT NULL,
                username TEXT DEFAULT '',
                owner_id INTEGER NOT NULL,
                welcome TEXT NOT NULL DEFAULT 'Welcome!'
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                token TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                username TEXT DEFAULT '',
                autoreq INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (token, chat_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending (
                token TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                requested_at INTEGER DEFAULT (strftime('%s','now')),
                PRIMARY KEY (token, chat_id, user_id)
            )
        """)

        conn.commit()


async def db_execute(query, params=(), fetch=False, fetchall=False):
    async with db_lock:
        def run():
            with closing(sqlite3.connect(DB_PATH)) as conn:
                cur = conn.cursor()
                cur.execute(query, params)

                if fetchall:
                    result = cur.fetchall()
                elif fetch:
                    result = cur.fetchone()
                else:
                    result = None

                conn.commit()
                return result

        return await asyncio.to_thread(run)


# =========================================================
# DATABASE HELPERS
# =========================================================

async def add_child_db(token, bot_id, username):
    await db_execute(
        """
        INSERT INTO children(token, bot_id, username, owner_id, welcome)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            token,
            bot_id,
            username or "",
            OWNER_ID,
            "Welcome!"
        )
    )


async def get_children():
    return await db_execute(
        """
        SELECT token, bot_id, username, owner_id, welcome
        FROM children
        ORDER BY bot_id
        """,
        fetchall=True
    )


async def get_child(token):
    return await db_execute(
        """
        SELECT token, bot_id, username, owner_id, welcome
        FROM children
        WHERE token = ?
        """,
        (token,),
        fetch=True
    )


async def remove_child_db(token):
    await db_execute(
        "DELETE FROM pending WHERE token = ?",
        (token,)
    )

    await db_execute(
        "DELETE FROM channels WHERE token = ?",
        (token,)
    )

    await db_execute(
        "DELETE FROM children WHERE token = ?",
        (token,)
    )


async def add_channel_db(token, chat_id, title, username):
    await db_execute(
        """
        INSERT OR REPLACE INTO channels
        (token, chat_id, title, username, autoreq)
        VALUES (
            ?,
            ?,
            ?,
            ?,
            COALESCE(
                (
                    SELECT autoreq
                    FROM channels
                    WHERE token = ? AND chat_id = ?
                ),
                0
            )
        )
        """,
        (
            token,
            chat_id,
            title,
            username or "",
            token,
            chat_id
        )
    )


async def get_channels(token):
    return await db_execute(
        """
        SELECT chat_id, title, username, autoreq
        FROM channels
        WHERE token = ?
        ORDER BY title
        """,
        (token,),
        fetchall=True
    )


async def get_channel(token, chat_id):
    return await db_execute(
        """
        SELECT chat_id, title, username, autoreq
        FROM channels
        WHERE token = ? AND chat_id = ?
        """,
        (token, chat_id),
        fetch=True
    )


async def set_autoreq(token, chat_id, enabled):
    await db_execute(
        """
        UPDATE channels
        SET autoreq = ?
        WHERE token = ? AND chat_id = ?
        """,
        (
            1 if enabled else 0,
            token,
            chat_id
        )
    )


async def save_pending(token, chat_id, user_id):
    await db_execute(
        """
        INSERT OR IGNORE INTO pending(token, chat_id, user_id)
        VALUES (?, ?, ?)
        """,
        (
            token,
            chat_id,
            user_id
        )
    )


async def delete_pending(token, chat_id, user_id):
    await db_execute(
        """
        DELETE FROM pending
        WHERE token = ? AND chat_id = ? AND user_id = ?
        """,
        (
            token,
            chat_id,
            user_id
        )
    )


async def get_pending(token, chat_id):
    return await db_execute(
        """
        SELECT user_id
        FROM pending
        WHERE token = ? AND chat_id = ?
        ORDER BY requested_at
        """,
        (
            token,
            chat_id
        ),
        fetchall=True
    )


async def pending_count(token, chat_id):
    row = await db_execute(
        """
        SELECT COUNT(*)
        FROM pending
        WHERE token = ? AND chat_id = ?
        """,
        (
            token,
            chat_id
        ),
        fetch=True
    )

    return int(row[0]) if row else 0


async def total_pending(token):
    row = await db_execute(
        """
        SELECT COUNT(*)
        FROM pending
        WHERE token = ?
        """,
        (token,),
        fetch=True
    )

    return int(row[0]) if row else 0


async def update_welcome(token, welcome):
    await db_execute(
        """
        UPDATE children
        SET welcome = ?
        WHERE token = ?
        """,
        (
            welcome,
            token
        )
    )


# =========================================================
# KEYBOARDS
# =========================================================

def father_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Add Child"),
                KeyboardButton(text="📋 Child List")
            ],
            [
                KeyboardButton(text="➖ Remove Child"),
                KeyboardButton(text="🔄 Refresh")
            ]
        ],
        resize_keyboard=True
    )


def child_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Add Channel"),
                KeyboardButton(text="📺 My Channels")
            ],
            [
                KeyboardButton(text="✏️ Edit Welcome"),
                KeyboardButton(text="🟢 AutoReq ON")
            ],
            [
                KeyboardButton(text="🔴 AutoReq OFF"),
                KeyboardButton(text="📥 Pending Requests")
            ],
            [
                KeyboardButton(text="🔄 Refresh")
            ]
        ],
        resize_keyboard=True
    )


def cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❌ Cancel")]
        ],
        resize_keyboard=True
    )


def channel_keyboard(channels, action):
    buttons = []

    for chat_id, title, username, autoreq in channels:
        state = "ON" if autoreq else "OFF"

        if action == "on":
            text = f"🟢 {title}"
        elif action == "off":
            text = f"🔴 {title}"
        else:
            text = f"📺 {title} [{state}]"

        buttons.append([
            KeyboardButton(text=text)
        ])

    buttons.append([
        KeyboardButton(text="❌ Cancel")
    ])

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True
    )


# =========================================================
# GLOBAL STATES
# =========================================================

father_states = {}

# token -> state
child_states = {}


def set_father_state(user_id, state):
    father_states[user_id] = state


def get_father_state(user_id):
    return father_states.get(user_id)


def clear_father_state(user_id):
    father_states.pop(user_id, None)


def set_child_state(token, state):
    child_states[token] = state


def get_child_state(token):
    return child_states.get(token)


def clear_child_state(token):
    child_states.pop(token, None)


# =========================================================
# FATHER BOT
# =========================================================

father_router = Router()


@father_router.message(CommandStart())
async def father_start(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ You are not authorized.")
        return

    clear_father_state(message.from_user.id)

    children = await get_children()

    await message.answer(
        f"👑 <b>Father Bot Control Panel</b>\n\n"
        f"Child Bots: <b>{len(children)}</b>\n\n"
        f"Choose an option below.",
        parse_mode="HTML",
        reply_markup=father_keyboard()
    )


@father_router.message(F.text == "➕ Add Child")
async def father_add_child(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    set_father_state(message.from_user.id, "waiting_child_token")

    await message.answer(
        "🤖 <b>Add Child Bot</b>\n\n"
        "BotFather se child bot ka token copy karke yahan send karo.\n\n"
        "Example:\n"
        "<code>123456789:AAxxxxxxxxxxxxxxxx</code>\n\n"
        "⚠️ Token kisi aur ko mat dena.",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )


@father_router.message(F.text == "📋 Child List")
async def father_child_list(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    children = await get_children()

    if not children:
        await message.answer(
            "📋 <b>Child Bots</b>\n\n"
            "No child bots added.",
            parse_mode="HTML",
            reply_markup=father_keyboard()
        )
        return

    lines = ["📋 <b>Child Bots</b>\n"]

    for i, row in enumerate(children, start=1):
        token, bot_id, username, owner_id, welcome = row

        name = f"@{username}" if username else f"ID: {bot_id}"

        lines.append(
            f"{i}. 🤖 <b>{name}</b>\n"
            f"   ID: <code>{bot_id}</code>"
        )

    await message.answer(
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=father_keyboard()
    )


@father_router.message(F.text == "➖ Remove Child")
async def father_remove_child(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    children = await get_children()

    if not children:
        await message.answer(
            "No child bots available.",
            reply_markup=father_keyboard()
        )
        return

    set_father_state(message.from_user.id, "waiting_remove_child")

    lines = [
        "➖ <b>Remove Child Bot</b>\n",
        "Send the child bot username or bot ID.\n"
    ]

    for token, bot_id, username, owner_id, welcome in children:
        name = f"@{username}" if username else str(bot_id)
        lines.append(f"• {name}")

    lines.append("\n⚠️ This will also remove its channels and saved requests.")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )


@father_router.message(F.text == "🔄 Refresh")
async def father_refresh(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    children = await get_children()

    await message.answer(
        f"🔄 <b>Refreshed</b>\n\n"
        f"Child Bots: <b>{len(children)}</b>",
        parse_mode="HTML",
        reply_markup=father_keyboard()
    )


@father_router.message(F.text == "❌ Cancel")
async def father_cancel(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    clear_father_state(message.from_user.id)

    await message.answer(
        "❌ Cancelled.",
        reply_markup=father_keyboard()
    )


@father_router.message()
async def father_text_handler(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    state = get_father_state(message.from_user.id)

    if state == "waiting_child_token":
        token = (message.text or "").strip()

        if ":" not in token:
            await message.answer(
                "❌ Invalid bot token.\n\n"
                "BotFather se complete token copy karo.",
                reply_markup=cancel_keyboard()
            )
            return

        try:
            test_bot = Bot(token)

            me = await test_bot.get_me()

            await test_bot.session.close()

        except Exception as e:
            logger.error("Invalid child token: %s", e)

            try:
                await test_bot.session.close()
            except Exception:
                pass

            await message.answer(
                "❌ Token invalid hai ya Telegram API se verify nahi hua.\n\n"
                "Token dobara check karo.",
                reply_markup=cancel_keyboard()
            )
            return

        existing = await get_child(token)

        if existing:
            await message.answer(
                "⚠️ Ye child bot already added hai.",
                reply_markup=father_keyboard()
            )
            clear_father_state(message.from_user.id)
            return

        try:
            await add_child_db(
                token,
                me.id,
                me.username or ""
            )

        except Exception as e:
            logger.error("Could not add child: %s", e)

            await message.answer(
                "❌ Child bot database me add nahi hua.",
                reply_markup=father_keyboard()
            )

            clear_father_state(message.from_user.id)
            return

        clear_father_state(message.from_user.id)

        await message.answer(
            f"✅ <b>Child Bot Added</b>\n\n"
            f"🤖 @{me.username or me.first_name}\n"
            f"🆔 <code>{me.id}</code>\n\n"
            f"Ab is child bot ko channel ka admin banao.",
            parse_mode="HTML",
            reply_markup=father_keyboard()
        )

        await start_child_bot(token)

        return

    if state == "waiting_remove_child":
        value = (message.text or "").strip().lower()

        children = await get_children()

        found_token = None
        found_name = None

        for token, bot_id, username, owner_id, welcome in children:

            if value == str(bot_id):
                found_token = token
                found_name = f"@{username}" if username else str(bot_id)
                break

            if username and value.lstrip("@") == username.lower():
                found_token = token
                found_name = f"@{username}"
                break

        if not found_token:
            await message.answer(
                "❌ Child bot nahi mila.",
                reply_markup=cancel_keyboard()
            )
            return

        await remove_child_db(found_token)

        await stop_child_bot(found_token)

        clear_father_state(message.from_user.id)

        await message.answer(
            f"✅ Removed: <b>{found_name}</b>",
            parse_mode="HTML",
            reply_markup=father_keyboard()
        )

        return


# =========================================================
# CHILD BOT MANAGEMENT
# =========================================================

child_bots = {}
child_tasks = {}
child_locks = {}


async def create_child_bot(token):
    if token in child_bots:
        return child_bots[token]

    bot = Bot(token)

    child_bots[token] = bot

    return bot


async def close_child_bot(token):
    bot = child_bots.pop(token, None)

    if bot:
        try:
            await bot.session.close()
        except Exception:
            pass


async def stop_child_bot(token):
    task = child_tasks.pop(token, None)

    if task:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    await close_child_bot(token)
    clear_child_state(token)


# =========================================================
# CHILD BOT API HELPERS
# =========================================================

async def telegram_delete_webhook(bot):
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        logger.warning(
            "deleteWebhook failed for %s: %s",
            bot.token[:10],
            e
        )


async def get_channel_from_input(bot, value):
    value = value.strip()

    # t.me/channelname
    value = re.sub(
        r"^https?://t\.me/",
        "",
        value,
        flags=re.IGNORECASE
    )

    value = value.strip("/")

    # Remove ? parameters
    value = value.split("?")[0]

    # @username
    if value.startswith("@"):
        chat_ref = value

    # Numeric ID
    elif re.fullmatch(r"-?\d+", value):
        chat_ref = int(value)

    # Plain username
    elif re.fullmatch(r"[A-Za-z0-9_]{4,}", value):
        chat_ref = "@" + value

    else:
        return None

    try:
        chat = await bot.get_chat(chat_ref)
        return chat

    except Exception as e:
        logger.error("getChat failed: %s", e)
        return None


async def verify_channel_admin(bot, chat_id):
    try:
        me = await bot.get_me()

        member = await bot.get_chat_member(
            chat_id,
            me.id
        )

        if member.status == "creator":
            return True, "creator"

        if member.status != "administrator":
            return False, "Bot is not an administrator."

        # For ChatJoinRequest updates the bot needs invite-user permission.
        can_invite = getattr(
            member,
            "can_invite_users",
            None
        )

        if can_invite is False:
            return (
                False,
                "Bot admin hai, lekin Invite Users / Invite via Link permission OFF hai."
            )

        return True, "administrator"

    except Exception as e:
        logger.error(
            "Channel admin verification failed: %s",
            e
        )

        return False, str(e)


async def approve_request(bot, chat_id, user_id):
    try:
        await bot.approve_chat_join_request(
            chat_id=chat_id,
            user_id=user_id
        )

        return True

    except TelegramBadRequest as e:
        logger.warning(
            "Approve failed for %s/%s: %s",
            chat_id,
            user_id,
            e
        )
        return False

    except TelegramForbiddenError as e:
        logger.warning(
            "Approve forbidden for %s/%s: %s",
            chat_id,
            user_id,
            e
        )
        return False

    except Exception as e:
        logger.error(
            "Approve exception: %s",
            e
        )
        return False


# =========================================================
# CHILD BOT ROUTER
# =========================================================

def create_child_router(token):
    router = Router()

    @router.message(CommandStart())
    async def child_start(message: Message):
        if message.from_user.id != OWNER_ID:
            child = await get_child(token)

            welcome = (
                child[4]
                if child and child[4]
                else "Welcome!"
            )

            await message.answer(welcome)
            return

        clear_child_state(token)

        channels = await get_channels(token)
        total = await total_pending(token)

        await message.answer(
            f"🤖 <b>Child Bot Panel</b>\n\n"
            f"📺 Channels: <b>{len(channels)}</b>\n"
            f"📥 Pending Requests: <b>{total}</b>\n\n"
            f"Choose an option below.",
            parse_mode="HTML",
            reply_markup=child_keyboard()
        )

    @router.message(F.text == "➕ Add Channel")
    async def add_channel(message: Message):
        if message.from_user.id != OWNER_ID:
            return

        set_child_state(token, "waiting_channel")

        await message.answer(
            "➕ <b>Add Channel</b>\n\n"
            "Channel ka @username ya numeric chat ID bhejo.\n\n"
            "Examples:\n"
            "<code>@mychannel</code>\n"
            "<code>-1001234567890</code>\n\n"
            "Public channel ke liye @username easiest hai.\n\n"
            "⚠️ Child bot ko channel ka admin hona chahiye.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )

    @router.message(F.text == "📺 My Channels")
    async def my_channels(message: Message):
        if message.from_user.id != OWNER_ID:
            return

        channels = await get_channels(token)

        if not channels:
            await message.answer(
                "📺 <b>My Channels</b>\n\n"
                "Abhi koi channel added nahi hai.",
                parse_mode="HTML",
                reply_markup=child_keyboard()
            )
            return

        lines = ["📺 <b>My Channels</b>\n"]

        for i, (chat_id, title, username, autoreq) in enumerate(
            channels,
            start=1
        ):
            state = "🟢 ON" if autoreq else "🔴 OFF"

            name = (
                f"@{username}"
                if username
                else title
            )

            count = await pending_count(
                token,
                chat_id
            )

            lines.append(
                f"{i}. <b>{name}</b>\n"
                f"   AutoReq: {state}\n"
                f"   Pending: <b>{count}</b>\n"
                f"   ID: <code>{chat_id}</code>"
            )

        await message.answer(
            "\n\n".join(lines),
            parse_mode="HTML",
            reply_markup=child_keyboard()
        )

    @router.message(F.text == "✏️ Edit Welcome")
    async def edit_welcome(message: Message):
        if message.from_user.id != OWNER_ID:
            return

        set_child_state(token, "waiting_welcome")

        child = await get_child(token)

        current = child[4] if child else "Welcome!"

        await message.answer(
            "✏️ <b>Edit Welcome Message</b>\n\n"
            f"Current:\n<blockquote>{current}</blockquote>\n\n"
            "Ab naya welcome message send karo.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )

    @router.message(F.text == "🟢 AutoReq ON")
    async def autoreq_on(message: Message):
        if message.from_user.id != OWNER_ID:
            return

        channels = await get_channels(token)

        if not channels:
            await message.answer(
                "❌ Pehle channel add karo.",
                reply_markup=child_keyboard()
            )
            return

        if len(channels) == 1:
            chat_id = channels[0][0]

            await enable_autoreq_for_channel(
                message,
                token,
                chat_id
            )

            return

        set_child_state(token, "waiting_autoreq_on")

        await message.answer(
            "🟢 <b>AutoReq ON</b>\n\n"
            "Kis channel ke liye ON karna hai?",
            parse_mode="HTML",
            reply_markup=channel_keyboard(
                channels,
                "on"
            )
        )

    @router.message(F.text == "🔴 AutoReq OFF")
    async def autoreq_off(message: Message):
        if message.from_user.id != OWNER_ID:
            return

        channels = await get_channels(token)

        if not channels:
            await message.answer(
                "❌ Pehle channel add karo.",
                reply_markup=child_keyboard()
            )
            return

        if len(channels) == 1:
            chat_id = channels[0][0]

            await disable_autoreq_for_channel(
                message,
                token,
                chat_id
            )

            return

        set_child_state(token, "waiting_autoreq_off")

        await message.answer(
            "🔴 <b>AutoReq OFF</b>\n\n"
            "Kis channel ke liye OFF karna hai?",
            parse_mode="HTML",
            reply_markup=channel_keyboard(
                channels,
                "off"
            )
        )

    @router.message(F.text == "📥 Pending Requests")
    async def pending_requests(message: Message):
        if message.from_user.id != OWNER_ID:
            return

        channels = await get_channels(token)

        if not channels:
            await message.answer(
                "❌ Koi channel added nahi hai.",
                reply_markup=child_keyboard()
            )
            return

        lines = ["📥 <b>Pending Requests</b>\n"]

        total = 0

        for chat_id, title, username, autoreq in channels:
            count = await pending_count(
                token,
                chat_id
            )

            total += count

            lines.append(
                f"📺 <b>{title}</b>: {count}"
            )

        lines.append(
            f"\n<b>Total: {total}</b>"
        )

        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=child_keyboard()
        )

    @router.message(F.text == "🔄 Refresh")
    async def refresh(message: Message):
        if message.from_user.id != OWNER_ID:
            return

        channels = await get_channels(token)
        total = await total_pending(token)

        await message.answer(
            f"🔄 <b>Refreshed</b>\n\n"
            f"📺 Channels: <b>{len(channels)}</b>\n"
            f"📥 Pending: <b>{total}</b>",
            parse_mode="HTML",
            reply_markup=child_keyboard()
        )

    @router.message(F.text == "❌ Cancel")
    async def cancel(message: Message):
        if message.from_user.id != OWNER_ID:
            return

        clear_child_state(token)

        await message.answer(
            "❌ Cancelled.",
            reply_markup=child_keyboard()
        )

    @router.chat_join_request()
    async def join_request(request: ChatJoinRequest):
        await process_join_request(
            token,
            request
        )

    @router.message()
    async def child_text_handler(message: Message):
        if message.from_user.id != OWNER_ID:
            return

        state = get_child_state(token)

        if state == "waiting_channel":
            value = (message.text or "").strip()

            bot = child_bots.get(token)

            if not bot:
                await message.answer(
                    "❌ Child bot connection unavailable.",
                    reply_markup=child_keyboard()
                )

                clear_child_state(token)
                return

            chat = await get_channel_from_input(
                bot,
                value
            )

            if not chat:
                await message.answer(
                    "❌ Channel nahi mila.\n\n"
                    "Public channel ke liye @username use karo, "
                    "ya numeric chat ID bhejo.",
                    reply_markup=cancel_keyboard()
                )
                return

            if chat.type != "channel":
                await message.answer(
                    "❌ Ye Telegram channel nahi hai.",
                    reply_markup=cancel_keyboard()
                )
                return

            ok, reason = await verify_channel_admin(
                bot,
                chat.id
            )

            if not ok:
                await message.answer(
                    "❌ <b>Channel permission problem</b>\n\n"
                    f"{reason}\n\n"
                    "Child bot ko channel ka admin banao "
                    "aur join-request/invite management permission do.",
                    parse_mode="HTML",
                    reply_markup=cancel_keyboard()
                )
                return

            await add_channel_db(
                token,
                chat.id,
                chat.title or "Untitled Channel",
                getattr(chat, "username", "") or ""
            )

            clear_child_state(token)

            username_text = (
                f"@{chat.username}"
                if getattr(chat, "username", None)
                else "Private Channel"
            )

            await message.answer(
                f"✅ <b>Channel Added</b>\n\n"
                f"📺 {chat.title}\n"
                f"🔗 {username_text}\n"
                f"🆔 <code>{chat.id}</code>\n\n"
                f"AutoReq: 🔴 OFF",
                parse_mode="HTML",
                reply_markup=child_keyboard()
            )

            return

        if state == "waiting_welcome":
            welcome = (message.text or "").strip()

            if not welcome:
                await message.answer(
                    "❌ Welcome message empty nahi ho sakta.",
                    reply_markup=cancel_keyboard()
                )
                return

            if len(welcome) > 4000:
                await message.answer(
                    "❌ Welcome message maximum 4000 characters.",
                    reply_markup=cancel_keyboard()
                )
                return

            await update_welcome(
                token,
                welcome
            )

            clear_child_state(token)

            await message.answer(
                "✅ Welcome message updated.",
                reply_markup=child_keyboard()
            )

            return

        if state == "waiting_autoreq_on":
            chat_id = await find_channel_from_button(
                token,
                message.text
            )

            if chat_id is None:
                await message.answer(
                    "❌ Channel select nahi hua.",
                    reply_markup=child_keyboard()
                )
                clear_child_state(token)
                return

            await enable_autoreq_for_channel(
                message,
                token,
                chat_id
            )

            return

        if state == "waiting_autoreq_off":
            chat_id = await find_channel_from_button(
                token,
                message.text
            )

            if chat_id is None:
                await message.answer(
                    "❌ Channel select nahi hua.",
                    reply_markup=child_keyboard()
                )
                clear_child_state(token)
                return

            await disable_autoreq_for_channel(
                message,
                token,
                chat_id
            )

            return

    return router


# =========================================================
# CHANNEL BUTTON HELPERS
# =========================================================

async def find_channel_from_button(token, text):
    if not text:
        return None

    clean = text

    for prefix in [
        "🟢 ",
        "🔴 ",
        "📺 "
    ]:
        if clean.startswith(prefix):
            clean = clean[len(prefix):]

    # Remove [ON]/[OFF]
    clean = re.sub(
        r"\s*\[(ON|OFF)\]\s*$",
        "",
        clean,
        flags=re.IGNORECASE
    )

    channels = await get_channels(token)

    for chat_id, title, username, autoreq in channels:
        if clean == title:
            return chat_id

        if username:
            if clean.lstrip("@").lower() == username.lower():
                return chat_id

    return None


# =========================================================
# AUTO REQUEST FUNCTIONS
# =========================================================

async def enable_autoreq_for_channel(
    message,
    token,
    chat_id
):
    channel = await get_channel(
        token,
        chat_id
    )

    if not channel:
        await message.answer(
            "❌ Channel not found.",
            reply_markup=child_keyboard()
        )
        clear_child_state(token)
        return

    bot = child_bots.get(token)

    if not bot:
        await message.answer(
            "❌ Child bot unavailable.",
            reply_markup=child_keyboard()
        )
        clear_child_state(token)
        return

    await set_autoreq(
        token,
        chat_id,
        True
    )

    pending = await get_pending(
        token,
        chat_id
    )

    approved = 0
    failed = 0

    for row in pending:
        user_id = row[0]

        success = await approve_request(
            bot,
            chat_id,
            user_id
        )

        if success:
            await delete_pending(
                token,
                chat_id,
                user_id
            )

            approved += 1

        else:
            failed += 1

    clear_child_state(token)

    await message.answer(
        f"🟢 <b>AutoReq ON</b>\n\n"
        f"📺 {channel[1]}\n\n"
        f"Existing saved requests approved: <b>{approved}</b>\n"
        f"Failed/remaining: <b>{failed}</b>\n\n"
        f"New join requests ab automatically approve honge.",
        parse_mode="HTML",
        reply_markup=child_keyboard()
    )


async def disable_autoreq_for_channel(
    message,
    token,
    chat_id
):
    channel = await get_channel(
        token,
        chat_id
    )

    if not channel:
        await message.answer(
            "❌ Channel not found.",
            reply_markup=child_keyboard()
        )
        clear_child_state(token)
        return

    await set_autoreq(
        token,
        chat_id,
        False
    )

    clear_child_state(token)

    count = await pending_count(
        token,
        chat_id
    )

    await message.answer(
        f"🔴 <b>AutoReq OFF</b>\n\n"
        f"📺 {channel[1]}\n\n"
        f"Existing saved pending: <b>{count}</b>\n\n"
        f"New requests save honge, "
        f"automatically approve nahi honge.",
        parse_mode="HTML",
        reply_markup=child_keyboard()
    )


# =========================================================
# JOIN REQUEST PROCESSOR
# =========================================================

async def process_join_request(
    token,
    request: ChatJoinRequest
):
    chat_id = request.chat.id
    user_id = request.from_user.id

    channel = await get_channel(
        token,
        chat_id
    )

    # Channel not registered in child bot
    if not channel:
        logger.info(
            "Ignoring unregistered channel %s",
            chat_id
        )
        return

    bot = child_bots.get(token)

    if not bot:
        logger.warning(
            "Child bot not available for join request."
        )
        return

    # Always save first
    await save_pending(
        token,
        chat_id,
        user_id
    )

    autoreq = bool(channel[3])

    if not autoreq:
        logger.info(
            "Saved pending request: bot=%s channel=%s user=%s",
            token[:10],
            chat_id,
            user_id
        )
        return

    # Auto approve
    success = await approve_request(
        bot,
        chat_id,
        user_id
    )

    if success:
        await delete_pending(
            token,
            chat_id,
            user_id
        )

        logger.info(
            "Auto approved: channel=%s user=%s",
            chat_id,
            user_id
        )

    else:
        logger.warning(
            "Auto approve failed; keeping pending: channel=%s user=%s",
            chat_id,
            user_id
        )


# =========================================================
# CHILD BOT POLLING
# =========================================================

async def child_polling(token):
    bot = child_bots.get(token)

    if not bot:
        return

    router = create_child_router(token)

    dp = Dispatcher()

    dp.include_router(router)

    try:
        me = await bot.get_me()

        logger.info(
            "Starting child bot: @%s (%s)",
            me.username,
            me.id
        )

        await telegram_delete_webhook(bot)

        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "chat_join_request"
            ],
            polling_timeout=25
        )

    except asyncio.CancelledError:
        logger.info(
            "Child bot stopped: %s",
            token[:10]
        )

        raise

    except Exception as e:
        logger.exception(
            "Child bot polling crashed: %s",
            e
        )

    finally:
        logger.info(
            "Polling ended: %s",
            token[:10]
        )


# =========================================================
# START / STOP CHILD BOTS
# =========================================================

async def start_child_bot(token):
    if token in child_tasks:
        task = child_tasks[token]

        if not task.done():
            return

    try:
        bot = await create_child_bot(token)

        # Verify token
        await bot.get_me()

    except Exception as e:
        logger.error(
            "Cannot start child bot: %s",
            e
        )

        await close_child_bot(token)
        return

    task = asyncio.create_task(
        child_polling(token)
    )

    child_tasks[token] = task

    logger.info(
        "Child task started: %s",
        token[:10]
    )


async def start_all_children():
    children = await get_children()

    logger.info(
        "Loading %d child bots...",
        len(children)
    )

    for row in children:
        token = row[0]

        await start_child_bot(token)

        # Small delay avoids API burst
        await asyncio.sleep(0.3)


# =========================================================
# MONITOR
# =========================================================

async def child_monitor():
    while True:
        try:
            children = await get_children()

            db_tokens = {
                row[0]
                for row in children
            }

            # Start missing child bots
            for token in db_tokens:
                task = child_tasks.get(token)

                if task is None or task.done():
                    await start_child_bot(token)

            # Stop deleted child bots
            running_tokens = list(child_tasks.keys())

            for token in running_tokens:
                if token not in db_tokens:
                    await stop_child_bot(token)

        except Exception as e:
            logger.exception(
                "Child monitor error: %s",
                e
            )

        await asyncio.sleep(10)


# =========================================================
# MAIN
# =========================================================

async def main():
    init_db()

    logger.info("================================")
    logger.info("AUTU Telegram Bot Starting")
    logger.info("Database: %s", DB_PATH)
    logger.info("Owner ID: %s", OWNER_ID)
    logger.info("================================")

    father_bot = Bot(FATHER_TOKEN)

    father_dp = Dispatcher()

    father_dp.include_router(
        father_router
    )

    # Father bot should also use polling only
    await telegram_delete_webhook(
        father_bot
    )

    # Start existing child bots
    await start_all_children()

    # Monitor child bots
    monitor_task = asyncio.create_task(
        child_monitor()
    )

    try:
        logger.info(
            "Father bot polling started."
        )

        await father_dp.start_polling(
            father_bot,
            allowed_updates=[
                "message"
            ],
            polling_timeout=25
        )

    finally:
        monitor_task.cancel()

        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        # Stop children
        tokens = list(child_tasks.keys())

        for token in tokens:
            await stop_child_bot(token)

        try:
            await father_bot.session.close()
        except Exception:
            pass


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")

    except Exception as e:
        logger.exception(
            "Fatal error: %s",
            e
        )
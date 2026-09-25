"""
AUTO REQUEST ACCEPT BOT — Multi-Channel / Multi-Bot Manager
=============================================================
Features:
1. Per-channel Auto-Accept toggle (ON/OFF button) + live Pending
   Requests counter (auto-refreshes).
2. Instant custom welcome message to every user jo join request bheje
   (owner ise button se edit kar sakta hai).
3. Add Channel (forward a message from the channel, ya @username bhejo)
   + Delete Channel button.
4. Owner-only: Add Child Bot (BotFather token dalo) / Remove Child Bot.
   Child bots ko full access milta hai (channels, auto-accept, welcome
   message) — sirf "Add/Remove Child" sirf OWNER ke paas rehta hai,
   master bot me hi.

SETUP
-----
1. pip install -r requirements.txt
2. Neeche MASTER_BOT_TOKEN me apna BotFather token daalo.
3. python bot.py
4. Master bot ko /start karo — jo pehla user /start karega wahi OWNER
   ban jayega (permanently, data.json me save hota hai).
5. Bot(s) ko apne channel/group me ADMIN banao, with "Add Members" /
   "Invite via link" permission, aur channel me "Approve New Members"
   (join request mode) ON karo.
6. Master bot ke menu se "➕ Add Channel" dabao, phir channel se koi
   message forward karo (sabse reliable tarika) ya @username bhejo.
"""

import asyncio
import json
import logging
import os
import time

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Bot,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

# ================= CONFIG =================
MASTER_BOT_TOKEN = os.environ.get("MASTER_BOT_TOKEN", "YOUR_MASTER_BOT_TOKEN_HERE")
DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")
PENDING_REFRESH_SECONDS = 10
PENDING_AUTOSTOP_AFTER = 30  # ~5 min (30 * 10s) ke baad auto-refresh ruk jayega
# ============================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DEFAULT_WELCOME = "👋 Namaste {name}!\n\nAapki join request mil gayi hai, jald hi review ki jayegi."

# ---------------- Storage ----------------
_lock = asyncio.Lock()


def _default_data():
    return {
        "owner_id": None,
        "children": {},  # bot_key -> {"token": str, "added_at": float}
        "bots": {
            "master": {"channels": {}, "welcome_message": DEFAULT_WELCOME}
        },
    }


def _load():
    if not os.path.exists(DATA_FILE):
        return _default_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return _default_data()


def _save(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


DATA = _load()


async def persist():
    async with _lock:
        _save(DATA)


def ensure_bot_entry(bot_key: str):
    if bot_key not in DATA["bots"]:
        DATA["bots"][bot_key] = {"channels": {}, "welcome_message": DEFAULT_WELCOME}
    return DATA["bots"][bot_key]


def is_owner(user_id: int) -> bool:
    return DATA["owner_id"] is not None and int(DATA["owner_id"]) == int(user_id)


# ============================================================
# Keyboard builders
# ============================================================

def kb_main_menu(is_master: bool):
    rows = [[InlineKeyboardButton("📡 My Channels", callback_data="channels")],
            [InlineKeyboardButton("✏️ Welcome Message", callback_data="welcome_edit")]]
    if is_master:
        rows.append([InlineKeyboardButton("➕ Add Child Bot", callback_data="child_add")])
        rows.append([InlineKeyboardButton("🤖 Child Bots", callback_data="child_list")])
    return InlineKeyboardMarkup(rows)


def kb_channels(bot_key: str):
    channels = DATA["bots"][bot_key]["channels"]
    rows = []
    for chat_id, info in channels.items():
        rows.append([InlineKeyboardButton(f"📢 {info['title']}", callback_data=f"chan_view:{chat_id}")])
    rows.append([InlineKeyboardButton("➕ Add Channel", callback_data="chan_add")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def kb_channel_detail(bot_key: str, chat_id: str):
    info = DATA["bots"][bot_key]["channels"][chat_id]
    auto = info.get("auto_accept", False)
    toggle_label = "✅ Auto Accept: ON (tap to turn OFF)" if auto else "⛔ Auto Accept: OFF (tap to turn ON)"
    pending_count = len(info.get("pending", {}))
    rows = [
        [InlineKeyboardButton(toggle_label, callback_data=f"chan_toggle:{chat_id}")],
        [InlineKeyboardButton(f"⏳ Pending Requests ({pending_count})", callback_data=f"chan_pending:{chat_id}")],
        [InlineKeyboardButton("🗑 Delete Channel", callback_data=f"chan_delete:{chat_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="channels")],
    ]
    return InlineKeyboardMarkup(rows)


def kb_pending(chat_id: str):
    rows = [
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"chan_pending:{chat_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"chan_view:{chat_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def kb_confirm_delete(chat_id: str):
    rows = [
        [InlineKeyboardButton("✅ Haan, delete karo", callback_data=f"chan_delete_confirm:{chat_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"chan_view:{chat_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def kb_child_list():
    rows = []
    for bot_key, info in DATA["children"].items():
        label = info.get("label", bot_key)
        rows.append([InlineKeyboardButton(f"🗑 Remove: {label}", callback_data=f"child_remove:{bot_key}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def kb_confirm_child_remove(bot_key: str):
    rows = [
        [InlineKeyboardButton("✅ Haan, remove karo", callback_data=f"child_remove_confirm:{bot_key}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="child_list")],
    ]
    return InlineKeyboardMarkup(rows)


# ============================================================
# Handler factory — same handlers reused for master + every child bot.
# `bot_key` + `is_master` are captured via closure.
# ============================================================

def build_application(token: str, bot_key: str, is_master: bool) -> Application:
    ensure_bot_entry(bot_key)

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if DATA["owner_id"] is None and is_master:
            DATA["owner_id"] = user.id
            await persist()
            await update.message.reply_text(
                f"👑 Aap ab is bot system ke OWNER ho, {user.first_name}!\n\n"
                "Neeche menu se channels add karo aur auto-accept setup karo."
            )
        if not is_owner(user.id):
            await update.message.reply_text("🚫 Yeh bot sirf owner control kar sakta hai.")
            return
        await update.message.reply_text(
            "🤖 Control Panel", reply_markup=kb_main_menu(is_master)
        )

    async def guard_owner_cb(update: Update) -> bool:
        q = update.callback_query
        if not is_owner(q.from_user.id):
            await q.answer("🚫 Sirf owner ke liye.", show_alert=True)
            return False
        return True

    async def cb_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not await guard_owner_cb(update):
            return
        await q.answer()
        data = q.data

        if data == "menu":
            await q.edit_message_text("🤖 Control Panel", reply_markup=kb_main_menu(is_master))

        elif data == "channels":
            await q.edit_message_text("📡 Aapke Channels:", reply_markup=kb_channels(bot_key))

        elif data == "chan_add":
            context.user_data["awaiting"] = "add_channel"
            await q.edit_message_text(
                "➕ Channel add karne ke liye:\n"
                "• Channel se koi message FORWARD karo (best way), YA\n"
                "• Channel ka @username bhejo\n\n"
                "Bot uss channel me ADMIN hona chahiye.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Cancel", callback_data="channels")]]
                ),
            )

        elif data.startswith("chan_view:"):
            chat_id = data.split(":", 1)[1]
            info = DATA["bots"][bot_key]["channels"].get(chat_id)
            if not info:
                await q.edit_message_text("Channel nahi mila.", reply_markup=kb_channels(bot_key))
                return
            await q.edit_message_text(
                f"📢 {info['title']}\nChat ID: {chat_id}",
                reply_markup=kb_channel_detail(bot_key, chat_id),
            )

        elif data.startswith("chan_toggle:"):
            chat_id = data.split(":", 1)[1]
            info = DATA["bots"][bot_key]["channels"].get(chat_id)
            if not info:
                return
            info["auto_accept"] = not info.get("auto_accept", False)
            if info["auto_accept"] and info.get("pending"):
                # turning ON -> accept everyone already pending
                for uid in list(info["pending"].keys()):
                    try:
                        await context.bot.approve_chat_join_request(chat_id=int(chat_id), user_id=int(uid))
                    except TelegramError as e:
                        logger.warning(f"Approve fail {uid}: {e}")
                    info["pending"].pop(uid, None)
            await persist()
            await q.edit_message_text(
                f"📢 {info['title']}\nChat ID: {chat_id}",
                reply_markup=kb_channel_detail(bot_key, chat_id),
            )

        elif data.startswith("chan_pending:"):
            chat_id = data.split(":", 1)[1]
            info = DATA["bots"][bot_key]["channels"].get(chat_id)
            if not info:
                return
            count = len(info.get("pending", {}))
            await q.edit_message_text(
                f"⏳ Pending Join Requests: {count}\n(har {PENDING_REFRESH_SECONDS}s me auto-refresh hoga)",
                reply_markup=kb_pending(chat_id),
            )
            job_name = f"pending_{bot_key}_{chat_id}_{q.message.chat_id}"
            for j in context.job_queue.get_jobs_by_name(job_name):
                j.schedule_removal()
            context.job_queue.run_repeating(
                pending_refresh_job,
                interval=PENDING_REFRESH_SECONDS,
                first=PENDING_REFRESH_SECONDS,
                data={
                    "chat_id": q.message.chat_id,
                    "message_id": q.message.message_id,
                    "channel_id": chat_id,
                    "bot_key": bot_key,
                    "count": 0,
                },
                name=job_name,
            )

        elif data.startswith("chan_delete:"):
            chat_id = data.split(":", 1)[1]
            await q.edit_message_text("Pakka delete karna hai? (sirf tracking se hatega, bot admin hi rahega)",
                                       reply_markup=kb_confirm_delete(chat_id))

        elif data.startswith("chan_delete_confirm:"):
            chat_id = data.split(":", 1)[1]
            DATA["bots"][bot_key]["channels"].pop(chat_id, None)
            await persist()
            await q.edit_message_text("🗑 Channel remove ho gaya.", reply_markup=kb_channels(bot_key))

        elif data == "welcome_edit":
            context.user_data["awaiting"] = "welcome_message"
            current = DATA["bots"][bot_key]["welcome_message"]
            await q.edit_message_text(
                f"✏️ Current message:\n\n{current}\n\n"
                "Naya welcome message bhejo. Use {name} for user's first name.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Cancel", callback_data="menu")]]
                ),
            )

        elif data == "child_add" and is_master:
            context.user_data["awaiting"] = "child_token"
            await q.edit_message_text(
                "➕ BotFather se mila naya bot token bhejo.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Cancel", callback_data="menu")]]
                ),
            )

        elif data == "child_list" and is_master:
            if not DATA["children"]:
                await q.edit_message_text("Koi child bot nahi hai abhi.", reply_markup=kb_main_menu(is_master))
            else:
                await q.edit_message_text("🤖 Child Bots:", reply_markup=kb_child_list())

        elif data.startswith("child_remove:") and is_master:
            child_key = data.split(":", 1)[1]
            await q.edit_message_text("Pakka is child bot ko remove karna hai? (uske channels data bhi delete hoga)",
                                       reply_markup=kb_confirm_child_remove(child_key))

        elif data.startswith("child_remove_confirm:") and is_master:
            child_key = data.split(":", 1)[1]
            await stop_child_bot(child_key)
            DATA["children"].pop(child_key, None)
            DATA["bots"].pop(child_key, None)
            await persist()
            await q.edit_message_text("🗑 Child bot remove ho gaya.", reply_markup=kb_main_menu(is_master))

    async def pending_refresh_job(context: ContextTypes.DEFAULT_TYPE):
        j = context.job
        d = j.data
        d["count"] += 1
        info = DATA["bots"].get(d["bot_key"], {}).get("channels", {}).get(d["channel_id"])
        if info is None:
            j.schedule_removal()
            return
        count = len(info.get("pending", {}))
        text = f"⏳ Pending Join Requests: {count}\n(har {PENDING_REFRESH_SECONDS}s me auto-refresh hoga)"
        if d["count"] >= PENDING_AUTOSTOP_AFTER:
            text += "\n\n⏸ Auto-refresh ruk gaya, 🔄 Refresh dabao dobara chalane ke liye."
        try:
            await context.bot.edit_message_text(
                chat_id=d["chat_id"], message_id=d["message_id"], text=text,
                reply_markup=kb_pending(d["channel_id"]),
            )
        except TelegramError:
            pass  # message unchanged ya user ne chat delete kar diya
        if d["count"] >= PENDING_AUTOSTOP_AFTER:
            j.schedule_removal()

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not is_owner(user.id):
            return
        awaiting = context.user_data.get("awaiting")
        if not awaiting:
            return

        if awaiting == "welcome_message":
            DATA["bots"][bot_key]["welcome_message"] = update.message.text
            await persist()
            context.user_data["awaiting"] = None
            await update.message.reply_text("✅ Welcome message update ho gaya.", reply_markup=kb_main_menu(is_master))

        elif awaiting == "child_token" and is_master:
            token = update.message.text.strip()
            try:
                test_bot = Bot(token)
                me = await test_bot.get_me()
            except TelegramError:
                await update.message.reply_text("❌ Invalid token, dobara try karo.")
                return
            child_key = str(me.id)
            if child_key in DATA["children"]:
                await update.message.reply_text("Yeh bot pehle se added hai.")
                return
            DATA["children"][child_key] = {"token": token, "label": f"@{me.username}", "added_at": time.time()}
            ensure_bot_entry(child_key)
            await persist()
            context.user_data["awaiting"] = None
            await update.message.reply_text(f"✅ @{me.username} child bot add ho gaya aur start ho raha hai...")
            await start_child_bot(token, child_key)

        elif awaiting == "add_channel":
            chat = None
            origin = getattr(update.message, "forward_origin", None)
            if origin is not None and getattr(origin, "chat", None) is not None:
                chat = origin.chat  # naya Telegram Bot API tarika
            elif update.message.forward_from_chat:
                chat = update.message.forward_from_chat  # purana/fallback tarika
            elif update.message.text and update.message.text.strip().startswith("@"):
                try:
                    chat = await context.bot.get_chat(update.message.text.strip())
                except TelegramError as e:
                    await update.message.reply_text(f"❌ Channel nahi mila: {e}")
                    return
            else:
                await update.message.reply_text(
                    "❌ Yeh samajh nahi aaya. Channel se message FORWARD karo ya @username bhejo."
                )
                return

            try:
                member = await context.bot.get_chat_member(chat.id, context.bot.id)
                if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                    await update.message.reply_text("❌ Bot is channel me admin nahi hai. Pehle admin banao.")
                    return
            except TelegramError as e:
                await update.message.reply_text(f"❌ Check nahi kar paya: {e}\nBot admin hai confirm karo.")
                return

            DATA["bots"][bot_key]["channels"][str(chat.id)] = {
                "title": chat.title or chat.username or str(chat.id),
                "auto_accept": False,
                "pending": {},
            }
            await persist()
            context.user_data["awaiting"] = None
            await update.message.reply_text(
                f"✅ '{chat.title}' add ho gaya!", reply_markup=kb_channels(bot_key)
            )

    async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
        req = update.chat_join_request
        chat_key = str(req.chat.id)
        bot_data = DATA["bots"].get(bot_key)
        if bot_data is None:
            return
        info = bot_data["channels"].get(chat_key)
        if info is None:
            return  # yeh channel track nahi ho raha

        # instant message
        try:
            text = bot_data["welcome_message"].format(name=req.from_user.first_name)
        except Exception:
            text = bot_data["welcome_message"]
        try:
            await context.bot.send_message(chat_id=req.from_user.id, text=text)
        except TelegramError as e:
            logger.warning(f"Welcome msg fail {req.from_user.id}: {e}")

        if info.get("auto_accept", False):
            try:
                await context.bot.approve_chat_join_request(chat_id=req.chat.id, user_id=req.from_user.id)
            except TelegramError as e:
                logger.warning(f"Auto-approve fail: {e}")
        else:
            info.setdefault("pending", {})[str(req.from_user.id)] = {
                "name": req.from_user.first_name,
                "username": req.from_user.username,
                "ts": time.time(),
            }
        await persist()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND | filters.FORWARDED, on_text))
    from telegram.ext import ChatJoinRequestHandler
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    return app


# ============================================================
# Multi-bot lifecycle management
# ============================================================
running_apps: dict[str, Application] = {}
running_tasks: dict[str, asyncio.Task] = {}


async def start_child_bot(token: str, bot_key: str):
    app = build_application(token, bot_key, is_master=False)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    running_apps[bot_key] = app
    logger.info(f"Child bot {bot_key} started.")


async def stop_child_bot(bot_key: str):
    app = running_apps.pop(bot_key, None)
    if app is None:
        return
    try:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception as e:
        logger.warning(f"Error stopping {bot_key}: {e}")
    logger.info(f"Child bot {bot_key} stopped.")


async def main():
    if MASTER_BOT_TOKEN == "YOUR_MASTER_BOT_TOKEN_HERE":
        raise SystemExit("Pehle MASTER_BOT_TOKEN environment variable set karo!")

    master_app = build_application(MASTER_BOT_TOKEN, "master", is_master=True)
    await master_app.initialize()
    await master_app.start()
    await master_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    running_apps["master"] = master_app
    logger.info("Master bot started.")

    # saved child bots ko bhi start karo
    for bot_key, info in list(DATA["children"].items()):
        try:
            await start_child_bot(info["token"], bot_key)
        except Exception as e:
            logger.error(f"Child bot {bot_key} start nahi ho paya: {e}")

    logger.info("Sab bots chalu hain. Ctrl+C se rokoge.")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for key in list(running_apps.keys()):
            await stop_child_bot(key)


if __name__ == "__main__":
    asyncio.run(main())

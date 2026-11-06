import logging
import os
import re
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -------------------- CONFIG --------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
MONGODB_URI = os.environ["MONGODB_URI"]
DB_NAME = os.getenv("DB_NAME", "number_info_bot")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("number-info-bot")

mongo_client = AsyncIOMotorClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=10000,
)
db = mongo_client[DB_NAME]

users = db["users"]
force_channels = db["force_channels"]
settings_col = db["settings"]

DEFAULT_SETTINGS = {
    "signup_bonus": 0,
    "referral_reward": 0,
    "minimum_redeem": 0,
    "referral_enabled": False,
    "redeem_enabled": False,
}


# -------------------- DATABASE --------------------

async def get_settings():
    await settings_col.update_one(
        {"_id": "main"},
        {"$setOnInsert": DEFAULT_SETTINGS},
        upsert=True,
    )
    return await settings_col.find_one({"_id": "main"})


async def ensure_user(update: Update):
    """Create a user once, then update last_seen without conflicting operators."""
    user = update.effective_user
    if user is None:
        return

    now = datetime.now(timezone.utc)

    # IMPORTANT: last_seen is deliberately present ONLY in $set.
    # Putting it in both $set and $setOnInsert causes MongoDB error code 40.
    await users.update_one(
        {"_id": user.id},
        {
            "$setOnInsert": {
                "joined_at": now,
                "balance": 0,
                "blocked": False,
            },
            "$set": {
                "name": user.full_name,
                "username": user.username,
                "last_seen": now,
            },
        },
        upsert=True,
    )


async def is_admin_user(user_id: int) -> bool:
    return user_id == ADMIN_ID


# -------------------- FORCE JOIN --------------------

async def all_required_channels_joined(bot, user_id: int) -> bool:
    async for channel in force_channels.find({}):
        try:
            member = await bot.get_chat_member(channel["chat_id"], user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return False
            if member.status == ChatMemberStatus.RESTRICTED and not member.is_member:
                return False
        except TelegramError as exc:
            logger.warning(
                "Membership check failed for channel %s: %s",
                channel.get("chat_id"),
                exc,
            )
            # Fail closed if the bot cannot verify membership.
            return False
    return True


async def force_join_keyboard():
    rows = []
    async for channel in force_channels.find({}):
        rows.append([
            InlineKeyboardButton(
                f"Join {channel.get('title', 'Channel')}",
                url=channel["invite"],
            )
        ])
    rows.append([
        InlineKeyboardButton("✅ Joined — Verify", callback_data="verify_join")
    ])
    return InlineKeyboardMarkup(rows)


async def require_membership(query, context) -> bool:
    if await all_required_channels_joined(context.bot, query.from_user.id):
        return True

    await query.message.reply_text(
        "🔒 Bot use karne ke liye pehle required channels join karein.",
        reply_markup=await force_join_keyboard(),
    )
    return False


# -------------------- KEYBOARDS --------------------

def user_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Number Info", callback_data="number_info")],
        [
            InlineKeyboardButton("💰 Balance", callback_data="balance"),
            InlineKeyboardButton("👥 Refer & Earn", callback_data="referral"),
        ],
        [InlineKeyboardButton("🎁 Redeem", callback_data="redeem")],
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Force-join Channels", callback_data="adm_channels")],
        [InlineKeyboardButton("👥 User Statistics", callback_data="adm_stats")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="adm_broadcast")],
        [InlineKeyboardButton("⚙️ Bot Settings", callback_data="adm_settings")],
    ])


def admin_channel_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Channel", callback_data="adm_add_channel")],
        [InlineKeyboardButton("➖ Remove Channel", callback_data="adm_remove_channel")],
        [InlineKeyboardButton("📋 List Channels", callback_data="adm_list_channels")],
        [InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_home")],
    ])


# -------------------- COMMANDS --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)

    if not await all_required_channels_joined(
        context.bot, update.effective_user.id
    ):
        await update.effective_message.reply_text(
            "Welcome! 🔒 Bot use karne ke liye neeche diye gaye channel(s) join karein.",
            reply_markup=await force_join_keyboard(),
        )
        return

    await update.effective_message.reply_text(
        "Welcome! Choose an option:",
        reply_markup=user_menu(),
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin_user(update.effective_user.id):
        await update.effective_message.reply_text("⛔ Unauthorized.")
        return

    await update.effective_message.reply_text(
        "🛡 ADMIN CONTROL PANEL",
        reply_markup=admin_menu(),
    )


# -------------------- CALLBACKS --------------------

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    uid = query.from_user.id

    # Admin callbacks are always owner-only.
    if data.startswith("adm_") or data == "admin_home":
        if not await is_admin_user(uid):
            await query.answer("Unauthorized", show_alert=True)
            return

    if data == "verify_join":
        if await all_required_channels_joined(context.bot, uid):
            await query.message.reply_text(
                "✅ Membership verified. Welcome!",
                reply_markup=user_menu(),
            )
        else:
            await query.answer(
                "Aapne abhi sabhi required channels join nahi kiye.",
                show_alert=True,
            )
        return

    if data == "admin_home":
        await query.message.reply_text(
            "🛡 ADMIN CONTROL PANEL",
            reply_markup=admin_menu(),
        )
        return

    # Every user-facing feature is gated, not just /start.
    if data in {"number_info", "balance", "referral", "redeem"}:
        if not await require_membership(query, context):
            return

    if data == "number_info":
        context.user_data["await_number"] = True
        await query.message.reply_text(
            "Number country code ke saath bhejein, jaise +919876543210.\n"
            "Is version mein sirf number format validate hota hai."
        )

    elif data == "balance":
        user_doc = await users.find_one({"_id": uid}) or {}
        await query.message.reply_text(
            f"💰 Your balance: ₹{user_doc.get('balance', 0)}"
        )

    elif data == "referral":
        settings = await get_settings()
        if not settings.get("referral_enabled", False):
            await query.message.reply_text("Referral system abhi disabled hai.")
        else:
            me = await context.bot.get_me()
            link = f"https://t.me/{me.username}?start=ref{uid}"
            await query.message.reply_text(f"Your referral link:\n{link}")

    elif data == "redeem":
        settings = await get_settings()
        if not settings.get("redeem_enabled", False):
            await query.message.reply_text("Redeem system abhi disabled hai.")
        else:
            await query.message.reply_text(
                "Redeem request ke liye admin se contact karein.\n"
                f"Minimum redeem: ₹{settings.get('minimum_redeem', 0)}"
            )

    # Admin panel sections
    elif data == "adm_channels":
        await query.message.reply_text(
            "Force-join channel management:",
            reply_markup=admin_channel_menu(),
        )

    elif data == "adm_add_channel":
        context.user_data["admin_action"] = "add_channel"
        await query.message.reply_text(
            "Is format mein bhejein:\n"
            "@channelusername | https://t.me/channelusername\n\n"
            "Bot ko channel ka administrator banana zaroori hai."
        )

    elif data == "adm_remove_channel":
        buttons = []
        async for channel in force_channels.find({}):
            buttons.append([
                InlineKeyboardButton(
                    f"❌ {channel.get('title', channel['chat_id'])}",
                    callback_data=f"adm_delch_{channel['_id']}",
                )
            ])
        buttons.append([
            InlineKeyboardButton("⬅️ Back", callback_data="adm_channels")
        ])
        await query.message.reply_text(
            "Remove karne ke liye channel select karein:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("adm_delch_"):
        try:
            channel_id = ObjectId(data.removeprefix("adm_delch_"))
            await force_channels.delete_one({"_id": channel_id})
            await query.message.reply_text(
                "✅ Channel removed.",
                reply_markup=admin_menu(),
            )
        except Exception:
            await query.message.reply_text("Channel remove nahi ho paya.")

    elif data == "adm_list_channels":
        lines = []
        async for channel in force_channels.find({}):
            lines.append(
                f"• {channel.get('title', 'Channel')} ({channel['chat_id']})"
            )
        await query.message.reply_text(
            "\n".join(lines) if lines else "Abhi koi force-join channel set nahi hai."
        )

    elif data == "adm_stats":
        now = datetime.now(timezone.utc)
        total = await users.count_documents({})
        active_24h = await users.count_documents({
            "last_seen": {"$gte": now - timedelta(hours=24)}
        })
        active_7d = await users.count_documents({
            "last_seen": {"$gte": now - timedelta(days=7)}
        })
        await query.message.reply_text(
            "👥 USER STATISTICS\n\n"
            f"Total users: {total}\n"
            f"Active in 24 hours: {active_24h}\n"
            f"Active in 7 days: {active_7d}"
        )

    elif data == "adm_broadcast":
        context.user_data["admin_action"] = "broadcast"
        await query.message.reply_text(
            "Ab woh message bhejein jise broadcast karna hai. "
            "Text, photo, video ya document bhej sakte hain."
        )

    elif data == "adm_settings":
        settings = await get_settings()
        text = (
            "⚙️ BOT SETTINGS\n\n"
            f"Signup bonus: ₹{settings.get('signup_bonus', 0)}\n"
            f"Referral reward: ₹{settings.get('referral_reward', 0)}\n"
            f"Minimum redeem: ₹{settings.get('minimum_redeem', 0)}\n"
            f"Referral enabled: {settings.get('referral_enabled', False)}\n"
            f"Redeem enabled: {settings.get('redeem_enabled', False)}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Set signup bonus", callback_data="adm_set_signup")],
            [InlineKeyboardButton("Set referral reward", callback_data="adm_set_referral")],
            [InlineKeyboardButton("Set minimum redeem", callback_data="adm_set_min")],
            [InlineKeyboardButton("Toggle referral", callback_data="adm_toggle_ref")],
            [InlineKeyboardButton("Toggle redeem", callback_data="adm_toggle_redeem")],
            [InlineKeyboardButton("⬅️ Admin Home", callback_data="admin_home")],
        ])
        await query.message.reply_text(text, reply_markup=keyboard)

    elif data in {"adm_set_signup", "adm_set_referral", "adm_set_min"}:
        context.user_data["admin_action"] = data
        await query.message.reply_text("Amount rupees mein bhejein (0 ya positive whole number).")

    elif data in {"adm_toggle_ref", "adm_toggle_redeem"}:
        current = await get_settings()
        key = "referral_enabled" if data == "adm_toggle_ref" else "redeem_enabled"
        await settings_col.update_one(
            {"_id": "main"},
            {"$set": {key: not current.get(key, False)}},
            upsert=True,
        )
        await query.message.reply_text(
            "✅ Setting updated.",
            reply_markup=admin_menu(),
        )


# -------------------- TEXT / BROADCAST HANDLER --------------------

async def handle_admin_action(update: Update, context):
    message = update.effective_message
    action = context.user_data.get("admin_action")

    if action == "broadcast":
        sent = 0
        failed = 0
        async for user_doc in users.find(
            {"blocked": {"$ne": True}},
            {"_id": 1},
        ):
            try:
                await context.bot.copy_message(
                    chat_id=user_doc["_id"],
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                )
                sent += 1
            except TelegramError as exc:
                failed += 1
                logger.info("Broadcast failed for %s: %s", user_doc["_id"], exc)
                if "blocked" in str(exc).lower() or "deactivated" in str(exc).lower():
                    await users.update_one(
                        {"_id": user_doc["_id"]},
                        {"$set": {"blocked": True}},
                    )

        context.user_data.pop("admin_action", None)
        await message.reply_text(
            f"📢 Broadcast complete.\nSent: {sent}\nFailed: {failed}"
        )
        return True

    if action == "add_channel":
        try:
            raw = (message.text or "").strip()
            chat_id, invite = [part.strip() for part in raw.split("|", 1)]
            if not invite.startswith(("https://t.me/", "http://t.me/")):
                raise ValueError("Invalid Telegram invite URL")

            chat = await context.bot.get_chat(chat_id)
            bot_member = await context.bot.get_chat_member(
                chat.id,
                context.bot.id,
            )
            if bot_member.status not in (
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            ):
                await message.reply_text(
                    "Bot ko pehle channel ka administrator banayein."
                )
                return True

            await force_channels.update_one(
                {"chat_id": chat.id},
                {"$set": {
                    "chat_id": chat.id,
                    "title": chat.title or chat_id,
                    "invite": invite,
                }},
                upsert=True,
            )
            await message.reply_text(f"✅ Channel added: {chat.title or chat_id}")
        except Exception as exc:
            logger.warning("Add channel failed: %s", exc)
            await message.reply_text(
                "Channel add nahi hua. Format check karein:\n"
                "@channelusername | https://t.me/channelusername\n"
                "Bot ko channel admin bhi banayein."
            )
        finally:
            context.user_data.pop("admin_action", None)
        return True

    if action in {"adm_set_signup", "adm_set_referral", "adm_set_min"}:
        try:
            amount = int((message.text or "").strip())
            if amount < 0:
                raise ValueError
            key = {
                "adm_set_signup": "signup_bonus",
                "adm_set_referral": "referral_reward",
                "adm_set_min": "minimum_redeem",
            }[action]
            await settings_col.update_one(
                {"_id": "main"},
                {"$set": {key: amount}},
                upsert=True,
            )
            await message.reply_text(f"✅ {key} updated to ₹{amount}.")
        except ValueError:
            await message.reply_text("Valid non-negative whole number bhejein.")
            return True
        finally:
            context.user_data.pop("admin_action", None)
        return True

    return False


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return

    # Admin's pending action is handled separately.
    if context.user_data.get("admin_action") and await is_admin_user(user.id):
        handled = await handle_admin_action(update, context)
        if handled:
            return

    # Enforce force-join before processing normal user input.
    if not await all_required_channels_joined(context.bot, user.id):
        await update.effective_message.reply_text(
            "🔒 Pehle required channel(s) join karein.",
            reply_markup=await force_join_keyboard(),
        )
        return

    if context.user_data.pop("await_number", False):
        number = (update.effective_message.text or "").strip()
        if not re.fullmatch(r"\+?[1-9]\d{7,14}", number):
            await update.effective_message.reply_text(
                "Invalid number format. Example: +919876543210"
            )
        else:
            await update.effective_message.reply_text(
                "✅ Number format valid hai. Is bot version mein private "
                "subscriber identity ya address lookup/display nahi hota."
            )


# -------------------- ERROR HANDLING / STARTUP --------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled bot exception", exc_info=context.error)


async def post_init(application: Application):
    # Verify MongoDB connectivity and create default settings before polling.
    await mongo_client.admin.command("ping")
    await get_settings()
    logger.info("MongoDB connected; bot initialized.")


async def post_shutdown(application: Application):
    mongo_client.close()


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, text_handler)
    )
    application.add_error_handler(error_handler)

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

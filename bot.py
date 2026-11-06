import os, re, logging
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("number-info-bot")

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
MONGO_URI = os.environ["MONGODB_URI"]
DB_NAME = os.getenv("DB_NAME", "number_info_bot")

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]
users = db.users
channels = db.force_channels
settings = db.settings
redeems = db.redeems

DEFAULTS = {
    "signup_bonus": 0,
    "referral_reward": 0,
    "minimum_redeem": 0,
    "referral_enabled": False,
    "redeem_enabled": False,
}

def is_admin(uid): return uid == ADMIN_ID

async def get_settings():
    doc = await settings.find_one({"_id": "main"})
    if not doc:
        doc = {"_id": "main", **DEFAULTS}
        await settings.insert_one(doc)
    return doc

async def joined_all(bot, uid):
    async for ch in channels.find({}):
        try:
            member = await bot.get_chat_member(ch["chat_id"], uid)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return False
        except TelegramError:
            # Fail closed: bot must be admin in each force-join channel.
            return False
    return True

async def force_markup():
    rows = []
    async for ch in channels.find({}):
        rows.append([InlineKeyboardButton("Join " + ch.get("title", ch["chat_id"]), url=ch["invite"])])
    rows.append([InlineKeyboardButton("✅ I Joined — Verify", callback_data="verify_join")])
    return InlineKeyboardMarkup(rows)

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Number Info", callback_data="number_info")],
        [InlineKeyboardButton("💰 Balance", callback_data="balance"),
         InlineKeyboardButton("👥 Refer & Earn", callback_data="referral")],
        [InlineKeyboardButton("🎁 Redeem", callback_data="redeem")]
    ])

async def ensure_user(update):
    u = update.effective_user
    now = datetime.now(timezone.utc)
    await users.update_one(
        {"_id": u.id},
        {"$setOnInsert": {"name": u.full_name, "username": u.username, "balance": 0,
                          "joined_at": now, "last_seen": now, "blocked": False},
         "$set": {"last_seen": now, "name": u.full_name, "username": u.username}},
        upsert=True
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    if context.args and context.args[0].startswith("ref"):
        pass  # Referral crediting can be enabled/configured in the admin settings.
    if not await joined_all(context.bot, update.effective_user.id):
        await update.message.reply_text(
            "🔒 To use this bot, join all required channels and then verify.",
            reply_markup=await force_markup()
        )
        return
    await update.message.reply_text("Welcome! Choose an option:", reply_markup=main_menu())

async def gate_callback(q, context):
    if not await joined_all(context.bot, q.from_user.id):
        await q.edit_message_text("Please join every required channel first.", reply_markup=await force_markup())
        return False
    return True

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "verify_join":
        if await joined_all(context.bot, q.from_user.id):
            await q.edit_message_text("✅ Membership verified. Welcome!", reply_markup=main_menu())
        else:
            await q.answer("You have not joined all required channels.", show_alert=True)
        return
    if data.startswith("adm_") and not is_admin(q.from_user.id):
        await q.answer("Unauthorized", show_alert=True); return
    if data not in ("admin_home",) and not data.startswith("adm_"):
        if not await gate_callback(q, context): return

    if data == "number_info":
        context.user_data["await_number"] = True
        await q.message.reply_text("Send a phone number with country code (e.g. +919876543210). This bot currently performs format validation only.")
    elif data == "balance":
        doc = await users.find_one({"_id": q.from_user.id}) or {}
        await q.message.reply_text(f"Your balance: ₹{doc.get('balance', 0)}")
    elif data == "referral":
        s = await get_settings()
        if not s.get("referral_enabled"):
            await q.message.reply_text("Referral system is currently disabled.")
        else:
            me = await context.bot.get_me()
            await q.message.reply_text(f"Your referral link:\nhttps://t.me/{me.username}?start=ref{q.from_user.id}")
    elif data == "redeem":
        s = await get_settings()
        if not s.get("redeem_enabled"):
            await q.message.reply_text("Redeem is currently disabled.")
        else:
            await q.message.reply_text(f"To request redemption, contact the bot administrator. Minimum: ₹{s.get('minimum_redeem', 0)}")
    elif data == "admin_home":
        await admin_home(q.message, q.from_user.id)
    elif data == "adm_channels":
        await q.message.reply_text("Force-join channels:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add channel", callback_data="adm_add_channel")],
            [InlineKeyboardButton("➖ Remove channel", callback_data="adm_remove_channel")],
            [InlineKeyboardButton("📋 List channels", callback_data="adm_list_channels")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_home")]
        ]))
    elif data == "adm_add_channel":
        context.user_data["admin_action"] = "add_channel"
        await q.message.reply_text("Send channel details in this format:\n@channelusername | https://t.me/channelusername\n\nMake the bot an administrator in that channel.")
    elif data == "adm_remove_channel":
        items = []
        async for ch in channels.find({}):
            items.append([InlineKeyboardButton(f"Remove {ch.get('title', ch['chat_id'])}", callback_data="adm_delch_"+str(ch["_id"]))])
        items.append([InlineKeyboardButton("⬅️ Back", callback_data="adm_channels")])
        await q.message.reply_text("Choose a channel to remove:", reply_markup=InlineKeyboardMarkup(items))
    elif data.startswith("adm_delch_"):
        from bson import ObjectId
        await channels.delete_one({"_id": ObjectId(data.replace("adm_delch_", ""))})
        await q.message.reply_text("Channel removed.", reply_markup=admin_keyboard())
    elif data == "adm_list_channels":
        out = []
        async for ch in channels.find({}): out.append(f"• {ch.get('title')} — {ch.get('chat_id')}")
        await q.message.reply_text("\n".join(out) if out else "No force-join channels configured.")
    elif data == "adm_stats":
        total = await users.count_documents({})
        day = await users.count_documents({"last_seen": {"$gte": datetime.now(timezone.utc)-timedelta(days=1)}})
        week = await users.count_documents({"last_seen": {"$gte": datetime.now(timezone.utc)-timedelta(days=7)}})
        await q.message.reply_text(f"👥 Total users: {total}\n🟢 Active 24h: {day}\n🟢 Active 7d: {week}")
    elif data == "adm_broadcast":
        context.user_data["admin_action"] = "broadcast"
        await q.message.reply_text("Send the text you want to broadcast. It will be delivered to registered users.")
    elif data == "adm_settings":
        s = await get_settings()
        await q.message.reply_text(
            f"Signup bonus: ₹{s['signup_bonus']}\nReferral reward: ₹{s['referral_reward']}\n"
            f"Minimum redeem: ₹{s['minimum_redeem']}\nReferral enabled: {s['referral_enabled']}\n"
            f"Redeem enabled: {s['redeem_enabled']}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Set signup bonus", callback_data="adm_set_signup")],
                [InlineKeyboardButton("Set referral reward", callback_data="adm_set_referral")],
                [InlineKeyboardButton("Set minimum redeem", callback_data="adm_set_min")],
                [InlineKeyboardButton("Toggle referral", callback_data="adm_toggle_ref")],
                [InlineKeyboardButton("Toggle redeem", callback_data="adm_toggle_redeem")],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin_home")]
            ])
        )
    elif data in ("adm_set_signup","adm_set_referral","adm_set_min"):
        context.user_data["admin_action"] = data
        await q.message.reply_text("Send the new amount in rupees (number only).")
    elif data in ("adm_toggle_ref","adm_toggle_redeem"):
        key = "referral_enabled" if data == "adm_toggle_ref" else "redeem_enabled"
        s = await get_settings()
        await settings.update_one({"_id":"main"}, {"$set": {key: not s.get(key, False)}})
        await q.message.reply_text("Setting updated.", reply_markup=admin_keyboard())

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 Force-join channels", callback_data="adm_channels")],
        [InlineKeyboardButton("👥 User statistics", callback_data="adm_stats")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast")],
        [InlineKeyboardButton("⚙️ Bot settings", callback_data="adm_settings")]
    ])

async def admin_home(message, uid):
    if not is_admin(uid): return
    await message.reply_text("🛡 Admin Panel", reply_markup=admin_keyboard())

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    await admin_home(update.message, update.effective_user.id)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    action = context.user_data.get("admin_action")
    if action and is_admin(uid):
        msg = update.message
        if action == "broadcast":
            sent = failed = 0
            async for u in users.find({"blocked": {"$ne": True}}, {"_id": 1}):
                try:
                    await context.bot.copy_message(u["_id"], msg.chat_id, msg.message_id); sent += 1
                except TelegramError:
                    failed += 1
                    await users.update_one({"_id":u["_id"]},{"$set":{"blocked":True}})
            context.user_data.pop("admin_action", None)
            await msg.reply_text(f"Broadcast finished. Sent: {sent}, failed: {failed}")
            return
        if action == "add_channel":
            try:
                raw = msg.text.strip()
                chat_id, invite = [x.strip() for x in raw.split("|", 1)]
                chat = await context.bot.get_chat(chat_id)
                await context.bot.get_chat_member(chat.id, context.bot.id)
                await channels.update_one({"chat_id": chat.id}, {"$set":{"chat_id":chat.id,"title":chat.title or chat_id,"invite":invite}}, upsert=True)
                await msg.reply_text("Channel added. Ensure the bot is an administrator there.")
            except Exception:
                await msg.reply_text("Could not add channel. Use @channelusername | https://t.me/channelusername and make the bot an admin.")
            context.user_data.pop("admin_action", None); return
        if action in ("adm_set_signup","adm_set_referral","adm_set_min"):
            try:
                amount = int(msg.text.strip())
                if amount < 0: raise ValueError()
                key = {"adm_set_signup":"signup_bonus","adm_set_referral":"referral_reward","adm_set_min":"minimum_redeem"}[action]
                await settings.update_one({"_id":"main"},{"$set":{key:amount}},upsert=True)
                await msg.reply_text(f"Updated {key} to ₹{amount}.")
            except ValueError:
                await msg.reply_text("Enter a valid non-negative whole number.")
            context.user_data.pop("admin_action", None); return

    if context.user_data.pop("await_number", False):
        raw = update.message.text.strip()
        if not re.fullmatch(r"\+?[1-9]\d{7,14}", raw):
            await update.message.reply_text("Invalid number format. Include country code, e.g. +919876543210.")
        else:
            await update.message.reply_text("✅ Number format is valid. This bot does not retrieve or disclose private subscriber information.")
        return

async def post_init(app):
    await get_settings()
    log.info("Bot initialized")

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()

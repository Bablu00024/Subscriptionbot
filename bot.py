import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from pymongo import MongoClient
import qrcode

# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
client = MongoClient(os.getenv("MONGO_URI"))
db = client["subscription_bot"]
channels = db["channels"]
subs = db["subscribers"]
payments = db["payments"]

# --- Utility: check admin ---
def is_channel_admin(channel_name: str, user_id: int) -> bool:
    channel = channels.find_one({"name": channel_name})
    if not channel:
        return False
    return user_id in channel["admin_ids"]

# --- Add channel ---
async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /add_channel <channel_name> <channel_id>")
        return

    channel_name = " ".join(context.args[:-1])
    channel_id = int(context.args[-1])

    # Only allow the user who creates the channel to be its first admin
    channels.insert_one({
        "name": channel_name,
        "channel_id": channel_id,
        "admin_ids": [user_id],
        "plans": [],
        "upi_id": "your-upi@bank"
    })
    await update.message.reply_text(f"✅ Channel '{channel_name}' added. You are the admin.")

# --- Add admin ---
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /add_admin <channel_name> <new_admin_id>")
        return

    channel_name = " ".join(context.args[:-1])
    new_admin_id = int(context.args[-1])

    if not is_channel_admin(channel_name, user_id):
        await update.message.reply_text("❌ You are not authorized to add admins for this channel.")
        return

    channels.update_one(
        {"name": channel_name},
        {"$addToSet": {"admin_ids": new_admin_id}}
    )
    await update.message.reply_text(f"✅ User {new_admin_id} added as admin for {channel_name}.")

# --- Remove admin ---
async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /remove_admin <channel_name> <admin_id>")
        return

    channel_name = " ".join(context.args[:-1])
    admin_id = int(context.args[-1])

    if not is_channel_admin(channel_name, user_id):
        await update.message.reply_text("❌ You are not authorized to remove admins for this channel.")
        return

    channels.update_one(
        {"name": channel_name},
        {"$pull": {"admin_ids": admin_id}}
    )
    await update.message.reply_text(f"✅ User {admin_id} removed as admin for {channel_name}.")

# --- Set plan ---
async def set_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 4:
        await update.message.reply_text("Usage: /set_plan <channel_name> <plan_name> <price> <days>")
        return

    channel_name = " ".join(context.args[:-3])
    plan_name = context.args[-3]
    price = int(context.args[-2])
    days = int(context.args[-1])

    if not is_channel_admin(channel_name, user_id):
        await update.message.reply_text("❌ You are not authorized to set plans for this channel.")
        return

    channels.update_one(
        {"name": channel_name},
        {"$push": {"plans": {"name": plan_name, "price": price, "days": days}}}
    )
    await update.message.reply_text(f"✅ Plan '{plan_name}' added for {channel_name}.")

# --- Start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        channel_name = context.args[0]
        channel = channels.find_one({"name": channel_name})
        if not channel:
            await update.message.reply_text("Channel not found.")
            return
        buttons = [[InlineKeyboardButton(f"{p['name']} - ₹{p['price']} ({p['days']} days)", callback_data=f"plan_{channel_name}_{p['name']}")] for p in channel["plans"]]
        await update.message.reply_text(f"Choose a plan for {channel_name}:", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text("Welcome! Use /add_channel or /set_plan if you're an admin.")

# --- Plan selected ---
async def plan_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, channel_name, plan_name = query.data.split("_")
    channel = channels.find_one({"name": channel_name})
    plan = next(p for p in channel["plans"] if p["name"] == plan_name)
    upi_id = channel.get("upi_id", "your-upi@bank")
    amount = plan["price"]

    qr = qrcode.make(f"upi://pay?pa={upi_id}&am={amount}&cu=INR")
    qr.save("payment_qr.png")

    payments.insert_one({"user_id": query.from_user.id, "channel_name": channel_name, "plan_name": plan_name, "amount": amount, "status": "pending"})
    buttons = [[InlineKeyboardButton("I have paid", callback_data=f"paid_{channel_name}_{query.from_user.id}")]]
    await query.message.reply_photo(photo=open("payment_qr.png", "rb"), caption=f"Pay ₹{amount} to {upi_id}\nAfter payment, click 'I have paid'.", reply_markup=InlineKeyboardMarkup(buttons))

# --- Expiry cleanup ---
def remove_expired_subs(app):
    now = datetime.utcnow()
    expired = subs.find({"valid_until": {"$lt": now}, "status": "active"})
    for user in expired:
        channel = channels.find_one({"name": user["channel_name"]})
        try:
            app.bot.ban_chat_member(chat_id=channel["channel_id"], user_id=user["user_id"])
            app.bot.unban_chat_member(chat_id=channel["channel_id"], user_id=user["user_id"])
            subs.update_one({"_id": user["_id"]}, {"$set": {"status": "expired"}})
            app.bot.send_message(chat_id=user["user_id"], text=f"⚠️ Your subscription to {channel['name']} has expired.")
        except Exception as e:
            print(f"Failed to remove {user['user_id']}: {e}")

# --- Reminder job ---
def send_expiry_reminders(app):
    now = datetime.utcnow()
    tomorrow = now + timedelta(days=1)
    expiring = subs.find({"valid_until": {"$lte": tomorrow, "$gte": now}, "status": "active"})
    for user in expiring:
        channel = channels.find_one({"name": user["channel_name"]})
        try:
            buttons = [[InlineKeyboardButton("🔄 Renew Now", callback_data=f"renew_{user['channel_name']}_{user['user_id']}")]]
            app.bot.send_message(
                chat_id=user["user_id"],
                text=f"⚠️ Your subscription to {channel['name']} will expire on {user['valid_until'].date()}.\nClick below to renew.",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            print(f"Reminder failed: {e}")

# --- Main ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("add_channel", add_channel))
    app.add_handler(CommandHandler("add_admin", add_admin))
    app.add_handler(CommandHandler("remove_admin", remove_admin))
    app.add_handler(CommandHandler("set_plan", set_plan))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(plan_selected, pattern="^plan_"))

    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: remove_expired_subs(app), "interval", hours=1)
    scheduler.add_job(lambda: send_expiry_reminders(app), "interval", hours=24)
    scheduler.start()

    app.run_polling()

if __name__ == "__main__":
    main()

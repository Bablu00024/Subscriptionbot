import os
import qrcode
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)
from pymongo import MongoClient

# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
client = MongoClient(os.getenv("MONGO_URI"))
db = client["subscription_bot"]
channels = db["channels"]
payments = db["payments"]

# Conversation states
PLAN_NAME, PLAN_PRICE, PLAN_DAYS, ADD_ANOTHER = range(4)

# --- Forward channel message to add channel ---
async def forward_add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fwd_chat = update.message.forward_from_chat
    if not fwd_chat or fwd_chat.type != "channel":
        await update.message.reply_text("❌ Please forward a message from the channel you want to add.")
        return ConversationHandler.END

    channel_name = fwd_chat.title
    channel_id = fwd_chat.id
    user_id = update.effective_user.id

    channels.insert_one({
        "name": channel_name,
        "channel_id": channel_id,
        "admin_ids": [user_id],
        "plans": [],
        "upi_id": "your-upi@bank"   # default placeholder
    })

    context.user_data["channel_id"] = channel_id
    context.user_data["channel_name"] = channel_name
    context.user_data["plans"] = []
    await update.message.reply_text(f"✅ Channel '{channel_name}' added.\n\nEnter the first plan name:")
    return PLAN_NAME

# --- Ask plan name ---
async def ask_plan_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["plan_name"] = update.message.text
    await update.message.reply_text("Enter the plan price (₹):")
    return PLAN_PRICE

# --- Ask plan price ---
async def ask_plan_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["plan_price"] = int(update.message.text)
    await update.message.reply_text("Enter the plan duration (days):")
    return PLAN_DAYS

# --- Ask plan days and save plan ---
async def ask_plan_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id = context.user_data["channel_id"]
    channel_name = context.user_data["channel_name"]
    plan_name = context.user_data["plan_name"]
    price = context.user_data["plan_price"]
    days = int(update.message.text)

    channels.update_one(
        {"channel_id": channel_id},
        {"$push": {"plans": {"name": plan_name, "price": price, "days": days}}}
    )
    context.user_data["plans"].append({"name": plan_name, "price": price, "days": days})

    keyboard = [
        [InlineKeyboardButton("➕ Add Another Plan", callback_data="add_more")],
        [InlineKeyboardButton("✅ Finish Setup", callback_data="finish_setup")]
    ]
    await update.message.reply_text(
        f"✅ Plan '{plan_name}' added for {channel_name}.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADD_ANOTHER

# --- Handle buttons ---
async def add_another(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "add_more":
        await query.message.reply_text("Enter the next plan name:")
        return PLAN_NAME
    elif query.data == "finish_setup":
        channel_id = context.user_data["channel_id"]
        channel_name = context.user_data["channel_name"]
        bot_username = (await context.bot.get_me()).username
        start_link = f"https://t.me/{bot_username}?start={channel_id}"

        plans = context.user_data.get("plans", [])
        summary = "\n".join([f"- {p['name']}: ₹{p['price']} ({p['days']} days)" for p in plans])

        await query.message.reply_text(
            f"🎉 Setup complete for {channel_name}!\n\n"
            f"📋 Plans configured:\n{summary}\n\n"
            f"🔗 Share this link with users:\n{start_link}"
        )
        return ConversationHandler.END

# --- Cancel ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Setup cancelled.")
    return ConversationHandler.END

# --- Start command (for users clicking the link) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        channel_id = int(context.args[0])
        channel = channels.find_one({"channel_id": channel_id})
        if not channel:
            await update.message.reply_text("❌ Channel not found.")
            return

        buttons = [
            [InlineKeyboardButton(
                f"{p['name']} - ₹{p['price']} ({p['days']} days)",
                callback_data=f"plan|{channel_id}|{p['name']}"
            )]
            for p in channel.get("plans", [])
        ]

        if not buttons:
            await update.message.reply_text(f"No plans configured yet for {channel['name']}.")
            return

        await update.message.reply_text(
            f"Choose a plan for {channel['name']}:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.message.reply_text("👋 Forward a message from your channel to begin setup.")

# --- Plan selected (with payment) ---
async def plan_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, channel_id, plan_name = query.data.split("|")
    channel_id = int(channel_id)
    channel = channels.find_one({"channel_id": channel_id})
    plan = next((p for p in channel["plans"] if p["name"] == plan_name), None)

    if not plan:
        await query.message.reply_text("❌ Plan not found.")
        return

    upi_id = channel.get("upi_id", "your-upi@bank")
    amount = plan["price"]

    # Generate QR code for UPI payment
    qr = qrcode.make(f"upi://pay?pa={upi_id}&am={amount}&cu=INR")
    qr.save("payment_qr.png")

    # Save payment record
    payments.insert_one({
        "user_id": query.from_user.id,
        "channel_id": channel_id,
        "plan_name": plan_name,
        "amount": amount,
        "status": "pending"
    })

    buttons = [[InlineKeyboardButton("✅ I have paid", callback_data=f"paid|{channel_id}|{query.from_user.id}")]]
    await query.message.reply_photo(
        photo=open("payment_qr.png", "rb"),
        caption=f"Pay ₹{amount} to {upi_id}\nAfter payment, click 'I have paid'.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# --- Payment confirmed ---
async def payment_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, channel_id, user_id = query.data.split("|")
    channel_id = int(channel_id)
    user_id = int(user_id)

    channel = channels.find_one({"channel_id": channel_id})
    if not channel:
        await query.message.reply_text("❌ Channel not found.")
        return

    payments.update_one(
        {"user_id": user_id, "channel_id": channel_id, "status": "pending"},
        {"$set": {"status": "awaiting_approval"}}
    )

    # Notify admins
    for admin_id in channel["admin_ids"]:
        buttons = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve|{channel_id}|{user_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject|{channel_id}|{user_id}")
            ]
        ]
        await context.bot.send_message(
            chat_id=admin_id,
            text=f"💳 Payment confirmation received for {channel['name']}.\n"
                 f"User ID: {user_id}\nPlan: awaiting approval.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    await query.message.reply_text("✅ Payment submitted. Waiting for admin approval.")

# --- Approve payment ---
async def approve_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, channel_id, user_id = query.data.split("|")
    channel_id = int(channel_id)
    user_id = int(user_id)

    channel = channels.find_one({"channel_id": channel_id})
    payments.update_one(
        {"user_id": user_id, "channel_id": channel_id},
        {"$set": {"status": "approved"}}
    )

    await context.bot.send_message(user_id, f"🎉 Your payment for {channel['name']} has been approved! You now have access.")
    await query.message.reply_text("✅ Approved.")

# --- Reject payment ---
async def reject_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, channel_id, user_id = query.data.split("|")
    channel_id

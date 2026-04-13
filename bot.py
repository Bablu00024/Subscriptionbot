import os
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
        "upi_id": "your-upi@bank"
    })

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
    channel_name = context.user_data["channel_name"]
    plan_name = context.user_data["plan_name"]
    price = context.user_data["plan_price"]
    days = int(update.message.text)

    channels.update_one(
        {"name": channel_name},
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
        channel_name = context.user_data["channel_name"]
        bot_username = (await context.bot.get_me()).username
        start_link = f"https://t.me/{bot_username}?start={channel_name}"

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

# --- Start command (for testing) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Forward a message from your channel to begin setup.")

# --- Main ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.FORWARDED, forward_add_channel)],  # ✅ fixed
        states={
            PLAN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_plan_name)],
            PLAN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_plan_price)],
            PLAN_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_plan_days)],
            ADD_ANOTHER: [CallbackQueryHandler(add_another)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.run_polling()

if __name__ == "__main__":
    main()

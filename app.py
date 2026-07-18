import os
import asyncio
import sqlite3
import threading
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ========== FLASK ДЛЯ RENDER ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "🤖 Бот работает!"

@flask_app.route('/health')
def health():
    return "OK", 200

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8632640394:AAE0RQffAqNutiCS2Je1BUwdQBYybhbO1D0")
ADMIN_ID = 8559381302
TEST_CHANNEL_ID = os.environ.get("CHANNEL_ID", "-1003773134695")

# ========== БАЗА ДАННЫХ ==========
DB_PATH = "test_users.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 10
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS paid_tariffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            tariff_name TEXT,
            paid_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_balance(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result:
        return result[0]
    else:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO users (user_id, balance) VALUES (?, 10)", (user_id,))
        conn.commit()
        conn.close()
        return 10

def deduct_balance(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?", (amount, user_id, amount))
    conn.commit()
    conn.close()

def add_paid_tariff(user_id, tariff_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO paid_tariffs (user_id, tariff_name) VALUES (?, ?)", (user_id, tariff_name))
    conn.commit()
    conn.close()

def is_tariff_paid(user_id, tariff_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM paid_tariffs WHERE user_id = ? AND tariff_name = ?", (user_id, tariff_name))
    result = c.fetchone()
    conn.close()
    return result is not None

# ========== ФУНКЦИИ БОТА ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = get_balance(user_id)
    
    text = f"""🎯 <b>Тестовый бот для одноразовых ссылок</b>

👤 Пользователь: {update.effective_user.first_name}
💰 Баланс: {balance} RUB

📌 <b>Как работает:</b>
1. Тариф стоит 1 RUB (виртуальный)
2. После оплаты ты получишь ссылку в канал
3. Ссылка работает ДЛЯ ОДНОГО ЧЕЛОВЕКА
4. После перехода ссылка ДЕАКТИВИРУЕТСЯ"""
    
    keyboard = [
        [InlineKeyboardButton("💎 Купить доступ (1 рубль)", callback_data="buy_access")],
        [InlineKeyboardButton("💰 Мой баланс", callback_data="show_balance")],
        [InlineKeyboardButton("📋 Мои покупки", callback_data="show_purchases")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "buy_access":
        balance = get_balance(user_id)
        if balance < 1:
            await query.edit_message_text("❌ Недостаточно средств! Используй команду /addmoney (для админа)")
            return
        
        if is_tariff_paid(user_id, "test_access"):
            await query.edit_message_text("❌ Ты уже покупал этот тариф!")
            return
        
        deduct_balance(user_id, 1)
        add_paid_tariff(user_id, "test_access")
        
        # Создаём одноразовую ссылку
        try:
            from telegram import Bot
            bot = Bot(token=BOT_TOKEN)
            invite_link = await bot.create_chat_invite_link(
                chat_id=TEST_CHANNEL_ID,
                member_limit=1,
                creates_join_request=False
            )
            link = invite_link.invite_link
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка создания ссылки: {e}")
            return
        
        new_balance = get_balance(user_id)
        text = f"""✅ <b>Оплата прошла успешно!</b>

💰 Снято: 1 RUB
💰 Остаток: {new_balance} RUB

🔗 <b>ТВОЯ ОДНОРАЗОВАЯ ССЫЛКА:</b>
{link}

⚠️ <b>ВАЖНО!</b>
• Ссылка ТОЛЬКО ДЛЯ 1 ЧЕЛОВЕКА
• После перехода ссылка ДЕАКТИВИРУЕТСЯ"""
        await query.edit_message_text(text, parse_mode="HTML")
    
    elif query.data == "show_balance":
        balance = get_balance(user_id)
        await query.edit_message_text(f"💰 <b>Твой баланс:</b> {balance} RUB", parse_mode="HTML")
    
    elif query.data == "show_purchases":
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT tariff_name, paid_date FROM paid_tariffs WHERE user_id = ?", (user_id,))
        purchases = c.fetchall()
        conn.close()
        
        if not purchases:
            text = "📋 <b>У тебя пока нет покупок</b>"
        else:
            text = "📋 <b>Твои покупки:</b>\n\n"
            for tariff, date in purchases:
                text += f"• {tariff} — {date[:10]}\n"
        await query.edit_message_text(text, parse_mode="HTML")

async def addmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет прав!")
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, balance) VALUES (?, 10)", (ADMIN_ID,))
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ Баланс пополнен до 10 RUB!")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет прав!")
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM users")
    c.execute("DELETE FROM paid_tariffs")
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ Все данные сброшены!")

# ========== ЗАПУСК БОТА ==========
def run_bot():
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addmoney", addmoney))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("🚀 ТЕСТОВЫЙ БОТ ЗАПУЩЕН (Web Service)")
    print(f"📢 Канал: {TEST_CHANNEL_ID}")
    print("💰 Баланс: 10 RUB | Тариф: 1 RUB")
    print("🔗 Ссылки НА 1 ЧЕЛОВЕКА")
    print("=" * 60)
    
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

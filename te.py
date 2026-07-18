from datetime import datetime
from flask import Flask, request, jsonify
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

# ========== FLASK ДЛЯ RENDER ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "🤖 Бот работает!"

@flask_app.route('/health')
def health():
    return "OK", 200

@flask_app.route('/ping')
def ping():
    return "pong", 200

# ========== НАСТРОЙКИ БОТА ==========
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
    print("✅ База данных создана")

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
    return True

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

# ========== ИНИЦИАЛИЗАЦИЯ БОТА ==========
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ========== ФУНКЦИЯ СОЗДАНИЯ ССЫЛКИ ==========
async def create_one_time_link(chat_id: str) -> str:
    try:
        invite_link = await bot.create_chat_invite_link(
            chat_id=chat_id,
            member_limit=1,
            creates_join_request=False
        )
        return invite_link.invite_link
    except Exception as e:
        print(f"Ошибка создания ссылки: {e}")
        return None

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    buttons = [
        [InlineKeyboardButton(text="💎 Купить доступ (1 рубль)", callback_data="buy_access")],
        [InlineKeyboardButton(text="💰 Мой баланс", callback_data="show_balance")],
        [InlineKeyboardButton(text="📋 Мои покупки", callback_data="show_purchases")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ========== ХЭНДЛЕРЫ ==========

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    balance = get_balance(user_id)
    
    text = f"""🎯 <b>Тестовый бот для одноразовых ссылок</b>

👤 Пользователь: {message.from_user.first_name}
💰 Баланс: {balance} RUB

📌 <b>Как работает:</b>
1. Тариф стоит 1 RUB (виртуальный)
2. После оплаты ты получишь ссылку в канал
3. Ссылка работает ДЛЯ ОДНОГО ЧЕЛОВЕКА
4. После перехода ссылка ДЕАКТИВИРУЕТСЯ

💡 Тестовый режим!
"""
    
    await message.answer(text, reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "buy_access")
async def buy_access(callback: CallbackQuery):
    user_id = callback.from_user.id
    balance = get_balance(user_id)
    
    if balance < 1:
        await callback.answer("❌ Недостаточно средств!", show_alert=True)
        return
    
    if is_tariff_paid(user_id, "test_access"):
        await callback.answer("❌ Ты уже покупал этот тариф!", show_alert=True)
        return
    
    deduct_balance(user_id, 1)
    add_paid_tariff(user_id, "test_access")
    
    link = await create_one_time_link(TEST_CHANNEL_ID)
    
    if not link:
        await callback.message.edit_text(
            "❌ Ошибка создания ссылки!\n"
            "Проверь CHANNEL_ID и права бота."
        )
        return
    
    new_balance = get_balance(user_id)
    
    text = f"""✅ <b>Оплата прошла успешно!</b>

💰 Снято: 1 RUB
💰 Остаток: {new_balance} RUB

🔗 <b>ТВОЯ ОДНОРАЗОВАЯ ССЫЛКА:</b>
{link}

⚠️ <b>ВАЖНО!</b>
• Ссылка ТОЛЬКО ДЛЯ 1 ЧЕЛОВЕКА
• После перехода ссылка ДЕАКТИВИРУЕТСЯ
"""
    
    await callback.message.edit_text(text, reply_markup=get_main_keyboard())
    await callback.answer("✅ Доступ куплен!")

@dp.callback_query(F.data == "show_balance")
async def show_balance(callback: CallbackQuery):
    user_id = callback.from_user.id
    balance = get_balance(user_id)
    
    await callback.message.edit_text(
        f"💰 <b>Твой баланс:</b> {balance} RUB",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "show_purchases")
async def show_purchases(callback: CallbackQuery):
    user_id = callback.from_user.id
    
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
    
    await callback.message.edit_text(text, reply_markup=get_main_keyboard())
    await callback.answer()

@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет прав!")
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM users")
    c.execute("DELETE FROM paid_tariffs")
    conn.commit()
    conn.close()
    await message.answer("✅ Все пользователи и покупки сброшены!")

@dp.message(Command("addmoney"))
async def cmd_add_money(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет прав!")
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, balance) VALUES (?, 10)", (ADMIN_ID,))
    conn.commit()
    conn.close()
    await message.answer("✅ Баланс пополнен до 10 RUB!")

# ========== ЗАПУСК БОТА В ОТДЕЛЬНОМ ПОТОКЕ ==========
def run_bot():
    asyncio.run(dp.start_polling(bot))

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("🚀 ТЕСТОВЫЙ БОТ ЗАПУЩЕН (Web Service)")
    print(f"📢 Канал: {TEST_CHANNEL_ID}")
    print("💰 Баланс: 10 RUB | Тариф: 1 RUB")
    print("🔗 Ссылки НА 1 ЧЕЛОВЕКА")
    print("=" * 60)
    
    # Запускаем бота в фоновом потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Запускаем Flask для Render
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

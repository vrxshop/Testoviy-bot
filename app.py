import logging
import asyncio
import os
import json
import uuid
import sqlite3
import threading
import re
import aiohttp
from datetime import datetime, timedelta
from flask import Flask, request
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import create_engine, text

# ==================================================
# FLASK
# ==================================================
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "🤖 Бот работает!"

@flask_app.route('/health')
def health():
    return "OK", 200

@flask_app.route('/crypto_webhook', methods=['POST'])
def crypto_webhook():
    """Обработчик вебхука от CryptoBot"""
    try:
        data = request.get_json()
        logging.info(f"Получен вебхук: {data}")
        
        if data.get("update_type") == "invoice_paid":
            invoice_id = data.get("invoice_id", "")
            
            invoice = get_crypto_invoice(invoice_id)
            if invoice:
                user_id = invoice[1]
                tariff_key = invoice[2]
                mark_invoice_paid(invoice_id)
                
                asyncio.create_task(send_crypto_success(user_id, tariff_key))
        
        return "OK", 200
    except Exception as e:
        logging.error(f"Ошибка вебхука: {e}")
        return "Error", 500

# ==================================================
# SUPABASE
# ==================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")

engine = create_engine(SUPABASE_URL, echo=False, pool_pre_ping=True)

def get_all_users():
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT user_id FROM users"))
            return [row[0] for row in result]
    except Exception as e:
        logging.error(f"Ошибка получения пользователей: {e}")
        return []

def get_user_count():
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(*) FROM users"))
            return result.fetchone()[0] or 0
    except Exception as e:
        logging.error(f"Ошибка получения количества пользователей: {e}")
        return 0

def add_user(user_id: int, first_name: str, username: str = None):
    try:
        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO users (user_id, first_name, username) VALUES (:id, :name, :uname) ON CONFLICT (user_id) DO NOTHING"),
                {"id": user_id, "name": first_name, "uname": username}
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка добавления пользователя: {e}")
        return False

def add_user_discount(user_id: int, discount_code: str, discount_percent: int):
    try:
        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO user_discounts (user_id, discount_code, discount_percent) VALUES (:id, :code, :percent) ON CONFLICT (user_id, discount_code) DO NOTHING"),
                {"id": user_id, "code": discount_code, "percent": discount_percent}
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка сохранения скидки: {e}")
        return False

def get_user_discounts(user_id: int):
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT discount_code, discount_percent, used FROM user_discounts WHERE user_id = :id AND used = 0"), {"id": user_id})
            return result.fetchall()
    except Exception as e:
        logging.error(f"Ошибка получения скидок: {e}")
        return []

def mark_discount_used(user_id: int, discount_code: str):
    try:
        with engine.connect() as conn:
            conn.execute(text("UPDATE user_discounts SET used = 1 WHERE user_id = :id AND discount_code = :code"), {"id": user_id, "code": discount_code})
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка отметки скидки: {e}")
        return False

# ==================================================
# КОНФИГУРАЦИЯ
# ==================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROJECT_NAME = "VIP"
SUPPORT_CONTACT_RU = "https://t.me/kasgd"
SUPPORT_CONTACT_EN = "https://t.me/kasgd"
ADMIN_IDS = [8370080332, 8559381302, 8924977674]

# CRYPTOBOT
CRYPTOBOT_API_KEY = os.getenv("CRYPTO_TOKEN")
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api/"

# КУРСЫ
USDT_RATE = 80
TON_RATE = 150
BTC_RATE = 4000000

# ==================================================
# ID КАНАЛОВ
# ==================================================
CHANNEL_IDS = {
    "2": "-1004478645537",
    "3": "-1004325704012",
    "4": "-1004362010819",
    "5": "-1004303957771",
    "6": "-1004429510738",
    "7": "-1003748125426",
    "9": "-1004331987176",
    "10": "-1001234567899",
    "11": "-1003862973415",
    "14": "-1004345678901",
    "15": "-1004267025056",
    "test": "-1003875225035",
}

# ==================================================
# БАЗА ДАННЫХ (SQLite)
# ==================================================
DB_PATH = "users.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS paid_tariffs (
            user_id INTEGER,
            tariff_key TEXT,
            paid_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, tariff_key)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payment_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            tariff_key TEXT,
            amount INTEGER,
            message_text TEXT,
            media_file_id TEXT,
            media_type TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS crypto_invoices (
            invoice_id TEXT PRIMARY KEY,
            user_id INTEGER,
            tariff_key TEXT,
            amount REAL,
            asset TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    logging.info("✅ База данных инициализирована")

def add_paid_tariff(user_id: int, tariff_key: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO paid_tariffs (user_id, tariff_key) VALUES (?, ?)', (user_id, tariff_key))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"Ошибка добавления оплаты: {e}")
        return False

def get_paid_tariffs(user_id: int):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT tariff_key FROM paid_tariffs WHERE user_id = ?', (user_id,))
        result = [row[0] for row in cursor.fetchall()]
        conn.close()
        return result
    except Exception as e:
        logging.error(f"Ошибка получения оплаченных тарифов: {e}")
        return []

def is_tariff_paid(user_id: int, tariff_key: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM paid_tariffs WHERE user_id = ? AND tariff_key = ?', (user_id, tariff_key))
        result = cursor.fetchone() is not None
        conn.close()
        return result
    except Exception as e:
        logging.error(f"Ошибка проверки оплаты: {e}")
        return False

def add_payment_request(user_id: int, username: str, tariff_key: str, amount: int, message_text: str = None, media_file_id: str = None, media_type: str = None):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO payment_requests (user_id, username, tariff_key, amount, message_text, media_file_id, media_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, username, tariff_key, amount, message_text, media_file_id, media_type))
        conn.commit()
        request_id = cursor.lastrowid
        conn.close()
        return request_id
    except Exception as e:
        logging.error(f"Ошибка добавления заявки: {e}")
        return None

def get_payment_request(request_id: int):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM payment_requests WHERE id = ?', (request_id,))
        result = cursor.fetchone()
        conn.close()
        return result
    except Exception as e:
        logging.error(f"Ошибка получения заявки: {e}")
        return None

def save_crypto_invoice(invoice_id: str, user_id: int, tariff_key: str, amount: float, asset: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO crypto_invoices (invoice_id, user_id, tariff_key, amount, asset)
            VALUES (?, ?, ?, ?, ?)
        ''', (invoice_id, user_id, tariff_key, amount, asset))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"Ошибка сохранения инвойса: {e}")
        return False

def get_crypto_invoice(invoice_id: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM crypto_invoices WHERE invoice_id = ?', (invoice_id,))
        result = cursor.fetchone()
        conn.close()
        return result
    except Exception as e:
        logging.error(f"Ошибка получения инвойса: {e}")
        return None

def mark_invoice_paid(invoice_id: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('UPDATE crypto_invoices SET status = "paid" WHERE invoice_id = ?', (invoice_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"Ошибка отметки инвойса: {e}")
        return False

# ==================================================
# ТЕКСТЫ
# ==================================================
LANG = {
    "ru": {
        "start_promo": "🎉 <b>Промокод {code} активирован! Скидка {discount}%!</b>",
        "start_welcome": "👋 Привет, {name}!\n\n<a href=\"{offer}\">Пользовательское соглашение</a>\n<a href=\"{policy}\">Политика конфиденциальности</a>",
        "prices_menu": "📋 <b>Прайс</b>\n\nВыберите тариф, чтобы узнать подробности и оформить покупку.",
        "subs_menu": "📋 <b>Ваши активные подписки</b>\n\n{list}",
        "no_subs": "⌛️ <b>У Вас нет действующих подписок.</b>\n\nВыберите тариф, чтобы оформить доступ.",
        "tariff_desc": "📋 <b>{name}</b>\n\n💰 Цена: {price_text}\nСрок доступа: {duration}\n\n{desc}",
        "tariff_desc_paid": "📋 <b>{name}</b>\n\n💰 Цена: {price_text}\nСрок доступа: {duration}\n\n{desc}\n\n✅ <b>ТАРИФ ОПЛАЧЕН</b>\n\n🔑 Для получения ссылки напишите в поддержку @kasgd",
        "enter_promo": "🏷️ <b>Введите код промокода</b>\n\nНапишите промокод в чат.",
        "promo_success": "✅ Промокод <b>{code}</b> активирован! Скидка {discount}% 🔥\n\n📋 <b>{name}</b>\n💰 Цена: <s>{old_rub} RUB</s> → {new_rub} RUB <b>(-{discount}%)</b>\n\nВыберите валюту для оплаты.",
        "promo_fail": "❌ Промокод не найден. Попробуйте еще раз (или нажмите ◀️ Отмена).",
        "choose_pay": "📋 <b>{name}</b>\nСрок доступа: {duration}\n💰 Цена: {price_text}\n\n🔒 Будет получен доступ к:\n• {project} (внешняя ссылка)\n\nВыберите способ оплаты",
        "pay_card": "Способ оплаты: Перевод на карту\n\n💰 К оплате: {final} RUB\n🆔 Ваш ID: {user_id}\n\n📌 <b>Реквизиты для оплаты:</b>\n\n💳 2200190284092510\n\n🏧 Банк: Уралсиб\nПолучатель: Кирилл\n\n❗️ Проверка ботом может занимать какое-то время (ручная проверка)\n❕ Если вы оплатили, нажмите обязательно кнопку «Я оплатил»\n❕ Если вы ждете больше 12 часов, напишите администратору",
        "pay_stars": "📋 <b>{name}</b>\nСрок доступа: {duration}\n{price_line}💳 Способ оплаты: ЗА ЗВЕЗДЫ ⭐\n\n💰 Итоговая стоимость: {final} STARS\n\nℹ️ <b>Информация по оплате</b>\nПодарить звезды или подарки на этот аккаунт - <a href=\"{support}\">@kasgd</a>\n\nкурс:\n1 ⭐ = 1 рубль",
        "pay_crypto_choose": "🪙 <b>Выберите монету:</b>",
        "pay_crypto_invoice": "✅ <b>Счёт на оплату сформирован.</b>\n\nДоступы к закрытым сообществам будут открыты, как только вы оплатите его.\n\n📋 <b>{name}</b>\n💰 Сумма: <b>{amount} {asset}</b>\n\nНажмите кнопку ниже для оплаты:",
        "crypto_payment_success": "✅ <b>Оплата прошла успешно!</b>\n\nДля получения доступа пишите @kasgd\n❗️ Пишите сразу тариф, который брали, и чек оплаты",
        "refresh_link": "♻️ <i>Ссылка обновлена!</i>",
        "btn_prices": "💵 Тарифы",
        "btn_subs": "⏳ Мои подписки",
        "btn_promo": "🏷️ Ввести промокод",
        "btn_pay": "💳 Способы оплаты",
        "btn_back": "👈 НАЗАД",
        "btn_pay_card": "💳 На карту",
        "btn_pay_card_disc": "💳 На карту 🏷️(-{disc}%)",
        "btn_pay_stars": "⭐ Звезды",
        "btn_pay_stars_disc": "⭐ Звезды 🏷️(-{disc}%)",
        "btn_pay_crypto": "🪙 Криптовалюта",
        "btn_pay_crypto_disc": "🪙 Криптовалюта 🏷️(-{disc}%)",
        "btn_crypto_usdt": "💵 USDT",
        "btn_crypto_ton": "💎 TON",
        "btn_crypto_btc": "₿ BTC",
        "btn_crypto_direct": "📤 Прямой перевод",
        "btn_pay_now": "💳 ОПЛАТИТЬ",
        "btn_i_paid": "✅ Я ОПЛАТИЛ",
        "btn_to_prices": "✅ КУПИТЬ ПОДПИСКУ",
        "btn_cancel": "🚫 ОТМЕНА",
        "btn_pay_for_friend": "🎁 ОПЛАТИТЬ ДЛЯ ДРУГА",
        "btn_stars_go": "⭐ Stars со скидкой до 42%",
        "btn_lang": "🇷🇺 Язык",
        "btn_write_user": "✍️ Написать лично",
        "btn_write_via_bot": "🤖 Написать через бота",
        "btn_back_to_admin": "◀️ Назад",
        "payment_success": "✅ <b>Оплата прошла!</b>\n\n🔗 <b>Ваша ссылка доступа (действует 30 секунд):</b>\n{link}\n\n⚠️ <b>Внимание!</b> Ссылка действительна только 30 секунд!\n\nСпасибо за покупку! ❤️\n\n📞 Поддержка: @kasgd",
        "payment_success_test": "✅ <b>Доступ открыт!</b>\n\n🔗 <b>Ваша ссылка доступа (действует 30 секунд):</b>\n{link}\n\n⚠️ <b>Внимание!</b> Ссылка действительна только 30 секунд!\n\nСпасибо за использование бота! ❤️\n\n📞 Поддержка: @kasgd",
        "subs_list_item": "• {name} (оплачен ✅)",
        "main_menu_text": "После выбора и оплаты тарифа бот автоматически тебе выдаст доступ на вход в группу. На случай потери ссылки на нашу випку, ты сможешь всегда её запросить повторно у бота, это бесплатно.\n\nНажми на тариф чтобы прочесть описание.\n\nКаждый канал отличается",
        "i_paid_confirm": "💁🏻‍♂️ Оплатили?\n\n👌🏻 Тогда отправьте сюда картинкой (не документом!) квитанцию платежа: скриншот или фото. Иначе бот не узнает что вы оплатили\n\n📌 На квитанции должны быть четко видны: дата, время и сумма платежа. Проверка может занимать до дня.\n🔒 Никто ваши чеки не увидит, Telegram не хранит их.\n\n⚠️ За спам вы можете быть заблокированы!",
        "payment_receipt_received": "✅ Ваш чек получен! Администратор проверит его в ближайшее время.",
        "new_payment_request": "🆕 <b>Новая заявка на оплату!</b>\n\n👤 Пользователь: {user_link}\n🆔 ID: <code>{user_id}</code>\n📋 Тариф: {tariff_name}\n💰 Сумма: {amount} RUB\n📝 Сообщение: {message_text}\n\n{media_info}",
        "admin_panel": "⚙️ <b>Админ-панель</b>\n\n👥 Всего пользователей: {user_count}\n⏳ Ожидают проверки: {pending_count}\n\nВыберите действие:",
        "crypto_error": "❌ Ошибка создания счета. Проверьте API ключ или попробуйте позже.",
        "crypto_direct": "🪙 <b>Прямой перевод</b>\n\nДля получения реквизитов либо по другому вопросу - @kasgd"
    },
    "en": {
        "start_promo": "🎉 <b>Promo code {code} activated! {discount}% discount!</b>",
        "start_welcome": "👋 Hello, {name}!\n\n<a href=\"{offer}\">Terms of Service</a>\n<a href=\"{policy}\">Privacy Policy</a>",
        "prices_menu": "📋 <b>Prices</b>\n\nSelect a tariff to view details and make a purchase.",
        "subs_menu": "📋 <b>Your active subscriptions</b>\n\n{list}",
        "no_subs": "⌛️ <b>You don't have any active subscriptions.</b>\n\nSelect a tariff to get access.",
        "tariff_desc": "📋 <b>{name}</b>\n\n💰 Price: {price_text}\nAccess duration: {duration}\n\n{desc}",
        "tariff_desc_paid": "📋 <b>{name}</b>\n\n💰 Price: {price_text}\nAccess duration: {duration}\n\n{desc}\n\n✅ <b>TARIFF PAID</b>\n\n🔑 To get the link contact support @kasgd",
        "enter_promo": "🏷️ <b>Enter promo code</b>\n\nType the promo code in the chat.",
        "promo_success": "✅ Promo code <b>{code}</b> activated! {discount}% discount 🔥\n\n📋 <b>{name}</b>\n💰 Price: <s>{old_rub} RUB</s> → {new_rub} RUB <b>(-{discount}%)</b>\n\nChoose a currency for payment.",
        "promo_fail": "❌ Promo code not found. Try again (or press ◀️ Cancel).",
        "choose_pay": "📋 <b>{name}</b>\nAccess duration: {duration}\n💰 Price: {price_text}\n\n🔒 You will get access to:\n• {project} (external link)\n\nChoose payment method",
        "pay_card": "Payment method: Bank card\n\n💰 Amount: {final} RUB\n🆔 Your ID: {user_id}\n\n📌 <b>Payment details:</b>\n\n💳 2200190284092510\n\n🏧 Bank: Uralsib\nRecipient: Kirill\n\n❗️ Verification may take some time (manual check)\n❕ After payment, press <b>«I Paid»</b> button\n❕ If waiting more than 12 hours, contact admin",
        "pay_stars": "📋 <b>{name}</b>\nAccess duration: {duration}\n{price_line}💳 Payment method: FOR STARS ⭐\n\n💰 Total cost: {final} STARS\n\nℹ️ <b>Payment info</b>\nSend stars or gifts to this account - <a href=\"{support}\">@kasgd</a>\n\nRate:\n1 ⭐ = 1 ruble",
        "pay_crypto_choose": "🪙 <b>Choose coin:</b>",
        "pay_crypto_invoice": "✅ <b>Invoice created.</b>\n\nAccess to closed communities will be opened as soon as you pay it.\n\n📋 <b>{name}</b>\n💰 Amount: <b>{amount} {asset}</b>\n\nClick the button below to pay:",
        "crypto_payment_success": "✅ <b>Payment successful!</b>\n\nFor access write @kasgd\n❗️ Write the tariff you purchased and payment receipt",
        "refresh_link": "♻️ <i>Link refreshed!</i>",
        "btn_prices": "💵 Prices",
        "btn_subs": "⏳ My subscriptions",
        "btn_promo": "🏷️ Enter promo code",
        "btn_pay": "💳 Payment methods",
        "btn_back": "👈 Back",
        "btn_pay_card": "💳 Card",
        "btn_pay_card_disc": "💳 Card 🏷️(-{disc}%)",
        "btn_pay_stars": "⭐ Stars",
        "btn_pay_stars_disc": "⭐ Stars 🏷️(-{disc}%)",
        "btn_pay_crypto": "🪙 Crypto",
        "btn_pay_crypto_disc": "🪙 Crypto 🏷️(-{disc}%)",
        "btn_crypto_usdt": "💵 USDT",
        "btn_crypto_ton": "💎 TON",
        "btn_crypto_btc": "₿ BTC",
        "btn_crypto_direct": "📤 Direct transfer",
        "btn_pay_now": "💳 PAY NOW",
        "btn_i_paid": "✅ I PAID",
        "btn_to_prices": "✅ BUY SUBSCRIPTION",
        "btn_cancel": "🚫 CANCEL",
        "btn_pay_for_friend": "🎁 PAY FOR FRIEND",
        "btn_stars_go": "⭐ Stars up to 42% off",
        "btn_lang": "🇬🇧 Language",
        "btn_write_user": "✍️ Write personally",
        "btn_write_via_bot": "🤖 Write via bot",
        "btn_back_to_admin": "◀️ Back",
        "payment_success": "✅ <b>Payment successful!</b>\n\n🔗 <b>Your access link (valid 30 seconds):</b>\n{link}\n\n⚠️ <b>Warning!</b> The link is valid only 30 seconds!\n\nThank you for your purchase! ❤️\n\n📞 Support: @kasgd",
        "payment_success_test": "✅ <b>Access granted!</b>\n\n🔗 <b>Your access link (valid 30 seconds):</b>\n{link}\n\n⚠️ <b>Warning!</b> The link is valid only 30 seconds!\n\nThank you for using the bot! ❤️\n\n📞 Support: @kasgd",
        "subs_list_item": "• {name} (paid ✅)",
        "main_menu_text": "After selecting and paying for the tariff, the bot will automatically give you access to the group. If you lose the link to our VIP, you can always request it again from the bot, it's free.\n\nClick on the tariff to read the description.\n\nEach channel is different",
        "i_paid_confirm": "💁🏻‍♂️ Paid?\n\n👌🏻 Then send a payment receipt as an image (not document!): screenshot or photo. Otherwise the bot won't know you paid.\n\n📌 The receipt must clearly show: date, time and payment amount. Verification may take up to a day.\n🔒 No one will see your receipts, Telegram doesn't store them.\n\n⚠️ You may be blocked for spam!",
        "payment_receipt_received": "✅ Your receipt has been received! Administrator will check it shortly.",
        "new_payment_request": "🆕 <b>New payment request!</b>\n\n👤 User: {user_link}\n🆔 ID: <code>{user_id}</code>\n📋 Tariff: {tariff_name}\n💰 Amount: {amount} RUB\n📝 Message: {message_text}\n\n{media_info}",
        "admin_panel": "⚙️ <b>Admin panel</b>\n\n👥 Total users: {user_count}\n⏳ Pending: {pending_count}\n\nSelect action:",
        "crypto_error": "❌ Error creating invoice. Check API key or try again later.",
        "crypto_direct": "🪙 <b>Direct transfer</b>\n\nFor details or other questions - @kasgd"
    }
}

# ==================================================
# ТАРИФЫ
# ==================================================
TARIFFS = {
    "2": {
        "name_ru": "🖤 Сливы шкyp 🖤",
        "name_en": "🖤 Skin Leaks 🖤",
        "price_rub": 349,
        "price_stars": 349,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "main",
        "desc_ru": "Вы получите доступ к следующим ресурсам:\n• H2 (канал)\n\n❗️ После покупки вы попадете в приватный канал со сливом девушек\n\n✅ Что в канале? П0pнo девок 13-19, а так-же слив и их разводом на фото, видео и \"беседы\" в скайпе, иногда ссылками на соц сети и Некоторых особых шкур есть номера и страницы вк\n\n❓Уровень? В основном 14-20, но встречаются и до 14 Вo3pacT\n\n✅ Помимо канала прилагается еще немного архивов с шкурками"
    },
    "3": {
        "name_ru": "❕Mini Deтск. До 12 🌐-Хит",
        "name_en": "❕Mini Child. Up to 12 🌐-Hit",
        "price_rub": 499,
        "price_stars": 499,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "main",
        "desc_ru": "Это мини пак с огромным количеством небольших видео\n\n❗️ После покyпки вы попадете в привaтный kaнал с de**ским пopno довольно таки жectkиm.\n\n✅ Уровень? i1-i12 вo3PacT, ceks, изnocuловаnие, инцceT, ласкает себя и т.д.\n\n✅ Помимо видео прилагается еще архивы с множеством гб"
    },
    "4": {
        "name_ru": "🔥💙ШкоDницЫ👧🏼🔥 (13-17 Jleт)",
        "name_en": "🔥💙Schoolgirls👧🏼🔥 (13-17 Years)",
        "price_rub": 799,
        "price_stars": 799,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "main",
        "desc_ru": "❗️ После покупки вы попадете в приватный канал с цe**льным пpоцe**poм пopno\n\n✅ Большой сборник из мега подборки пopно ваших любимых шкoльниц возрастом от 12 до 17 🔥 , есть изnocuлование, инцceT, много сливов с впиcoк и просто cлив шkyp, скрытые камеры шkoльниц/стyдeнток и ceксoм, ласкает себя и т.д.\n\n✅ Помимо видео прилагается еще архивы с множеством гб этой категории.\n\nКонтента очень много"
    },
    "5": {
        "name_ru": "❗️Premium Deтск. До 12 ✅",
        "name_en": "❗️Premium Child. Up to 12 ✅",
        "price_rub": 899,
        "price_stars": 899,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "main",
        "desc_ru": "❗️ После покyпки вы попадете в привaтный kaнал с de**ским пopno довольно таки жectkиm.\n\n✅ Уровень? i1-i12 вo3PacT, ceks, изnocuловаnие, инцceT, ласкает себя и т.д.\n\n✅ Помимо видео прилагается еще архивы с множеством гб\n\nКонтента очень много"
    },
    "6": {
        "name_ru": "Канал 3оo🐕",
        "name_en": "Zoo Channel🐕",
        "price_rub": 239,
        "price_stars": 239,
        "duration_ru": "2 месяца",
        "duration_en": "2 months",
        "category": "main",
        "desc_ru": "Канал с зоо контентом"
    },
    "7": {
        "name_ru": "Гeи",
        "name_en": "Gay",
        "price_rub": 299,
        "price_stars": 299,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "main",
        "desc_ru": "Вы получите доступ к следующим ресурсам:\n• Gg (канал)\n\n❗️ После покупки вы попадете в приватный канал с м+м\n\n✅ Уровень? Есть до 12, но в основном видео 12-17, есть немного изnocuлование, инцceT, скрытые камеры шkoльнов/стyдeнтов и конечно основное же ceкс и минет\n\n✅ Помимо видео прилагается еще дополнительный архив."
    },
    "9": {
        "name_ru": "🩵Всё включено 2026💚",
        "name_en": "🩵All inclusive 2026💚",
        "price_rub": 1499,
        "price_stars": 1499,
        "duration_ru": "Бессрочно",
        "duration_en": "Forever",
        "category": "main",
        "desc_ru": "❗️Вы получите доступ сразу в 10 наших каналов при этом их подписка останется у вас НАВСЕГДА! А выйдет гораздо дешевле чем покупать по отдельности.\n\n🔥 Кoнтeнтa у вас выйдет очень МНОГО\n\n+ Бонусные каналы к тарифу"
    },
    "10": {
        "name_ru": "Vpn 7 дней",
        "name_en": "Vpn 7 days",
        "price_rub": 10000,
        "price_stars": 10000,
        "duration_ru": "1 день",
        "duration_en": "1 day",
        "category": "main",
        "desc_ru": "Не покупать, читайте описание.\n\n✅ Хороший VPN для обхода белых списков.\n\nПереходим по ссылке:\nhttps://t.me/velvet_vpn_bot?start=sYzcRbjU\n\nВам дают 2 дня бесплатного доступа, а также вводим ещё 2 секретных промокода на 7 дней:\n\nWELCOME_BACK\nJUSTTRY"
    },
    "11": {
        "name_ru": "✅Пак - Обновление ссылок",
        "name_en": "✅Pack - Link Update",
        "price_rub": 699,
        "price_stars": 699,
        "duration_ru": "21 дней",
        "duration_en": "21 days",
        "category": "paki",
        "desc_ru": "Cливaeм ccлыky дpyгиx кaнaлoв, peкoмeндyeм пoкyпaть пocлe пpocмoтpa дpyгиx тapифoв\n\nЕдинственный пак который не входит во всё включено"
    },
    "14": {
        "name_ru": "💯Жêçть (2-17 Jlet)🩸",
        "name_en": "💯Extreme (2-17 Years)🩸",
        "price_rub": 599,
        "price_stars": 599,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "paki",
        "desc_ru": "Bы пoлyчитe дocтyп k cлeдyющим pecypcaм:\n• Жecть (kaнaл)\n\n❗️ Пocлe пoкyпkи вы пoпaдeтe в пpивaтный kaнaл c caмым жecтkим koнтeнтoм, чтo ecть в интepнeтe.\n\n❓Уpoвeнь? 14-20 лeт, кpoвь, yнижeния, бoль, экcтpим, мясo, гpyппoвyшkи, инцecT — вce caмoe жecтkoe."
    },
    "15": {
        "name_ru": "💫рabыни + слivы + kpyжки✨",
        "name_en": "💫Slaves + Leaks + Mugs✨",
        "price_rub": 250,
        "price_stars": 250,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "main",
        "desc_ru": ""
    }
}

# --- ТЕСТОВЫЙ ТАРИФ ---
TEST_TARIFF = {
    "name_ru": "🧪 ТЕСТОВЫЙ тариф (Бесплатно)",
    "name_en": "🧪 TEST tariff (Free)",
    "price_rub": 0,
    "price_stars": 0,
    "duration_ru": "Тестовый",
    "duration_en": "Test",
    "desc_ru": "🧪 Это тестовый тариф. Он полностью БЕСПЛАТНЫЙ!\n\nПросто выберите его и получите ссылку для тестирования."
}

# --- ПРОМОКОДЫ ---
PROMO_CODES = {
    "VIP10": 10,
    "SUPER25": 25,
    "HOMAKE40": 40,
    "BANK50": 50,
    "LOLIPOP80": 80,
    "newpopolnenie": 60
}

# --- ИНИЦИАЛИЗАЦИЯ ---
storage = MemoryStorage()
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=session)
dp = Dispatcher(storage=storage)

# --- FSM STATES ---
class PromoStates(StatesGroup):
    waiting_for_promo = State()

class MailingStates(StatesGroup):
    waiting_for_content = State()
    waiting_for_mail_type = State()

class PaymentStates(StatesGroup):
    waiting_for_receipt = State()

class AdminReplyStates(StatesGroup):
    waiting_for_reply = State()

# --- ФУНКЦИИ ---
async def create_one_time_link(chat_id: str) -> str:
    try:
        expire_date = datetime.now() + timedelta(seconds=30)
        invite_link = await bot.create_chat_invite_link(
            chat_id=chat_id,
            member_limit=1,
            expire_date=expire_date,
            creates_join_request=False
        )
        return invite_link.invite_link
    except Exception as e:
        logging.error(f"Ошибка создания ссылки: {e}")
        return None

async def get_lang(state: FSMContext):
    data = await state.get_data()
    return data.get("lang", "ru")

async def save_payment_and_send_link(message: Message, tariff_key: str, lang: str, user_id: int):
    if tariff_key not in CHANNEL_IDS:
        await message.answer("❌ Ошибка: канал для этого тарифа не настроен.")
        return
    
    chat_id = CHANNEL_IDS[tariff_key]
    link = await create_one_time_link(chat_id)
    
    if not link:
        await message.answer("❌ Ошибка создания ссылки.")
        return
    
    add_paid_tariff(user_id, tariff_key)
    
    if tariff_key == "test":
        text = LANG[lang]["payment_success_test"].format(link=link)
    else:
        text = LANG[lang]["payment_success"].format(link=link)
    
    await message.answer(text, disable_web_page_preview=False)

async def create_crypto_invoice(amount: float, user_id: int, tariff_key: str, asset: str = "USDT") -> dict:
    """Создает счет в CryptoBot и возвращает данные"""
    if not CRYPTOBOT_API_KEY:
        logging.error("CRYPTOBOT_API_KEY не задан!")
        return None
    
    url = CRYPTOBOT_API_URL + "createInvoice"
    headers = {
        "Crypto-Pay-API-Token": CRYPTOBOT_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "asset": asset,
        "amount": str(amount),
        "description": f"Оплата тарифа {tariff_key} для пользователя {user_id}",
        "paid_btn_name": "openChannel",
        "paid_btn_url": "https://t.me/kasgd",
        "payload": f"{user_id}_{tariff_key}"
    }
    
    logging.info(f"Создание инвойса: {payload}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as response:
                response_text = await response.text()
                logging.info(f"Ответ CryptoBot: {response_text}")
                
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok"):
                        result = data["result"]
                        return {
                            "invoice_id": result["invoice_id"],
                            "pay_url": result["pay_url"]
                        }
                    else:
                        logging.error(f"Ошибка CryptoBot: {data}")
                        return None
                else:
                    logging.error(f"Ошибка HTTP: {response.status}, {response_text}")
                    return None
    except Exception as e:
        logging.error(f"Ошибка при создании счета: {e}")
        return None

def round_to_half(value: float) -> float:
    """Округляет до ближайшего 0.5"""
    return round(value * 2) / 2

# ==================================================
# ОБЩАЯ ФУНКЦИЯ ДЛЯ ВЫБОРА ОПЛАТЫ
# ==================================================

async def choose_payment_logic(callback: CallbackQuery, state: FSMContext, tariff_key: str):
    tariff = TARIFFS[tariff_key]
    
    if tariff['price_rub'] == 0:
        lang = await get_lang(state)
        user_id = callback.from_user.id
        await callback.message.delete()
        await save_payment_and_send_link(callback.message, tariff_key, lang, user_id)
        await callback.answer("✅ Доступ открыт!")
        return
    
    lang = await get_lang(state)
    data = await state.get_data()
    discount = data.get("discount", 0)
    
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    duration = tariff['duration_ru'] if lang == "ru" else tariff['duration_en']
    
    if discount > 0:
        show_rub = int(tariff['price_rub'] * (1 - discount / 100))
        price_text = f"<s>{tariff['price_rub']} RUB</s> → {show_rub} RUB (-{discount}%)"
    else:
        show_rub = tariff['price_rub']
        price_text = f"{show_rub} RUB"
    
    text = LANG[lang]["choose_pay"].format(name=name, duration=duration, price_text=price_text, project=PROJECT_NAME)
    await callback.message.edit_text(text, reply_markup=get_payment_method_keyboard(tariff_key, discount, lang))

# --- КЛАВИАТУРЫ ---
def get_main_keyboard(lang):
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=LANG[lang]["btn_prices"]), KeyboardButton(text=LANG[lang]["btn_subs"])]
    ], resize_keyboard=True)

def get_tariff_keyboard(lang):
    buttons = []
    for key, data in TARIFFS.items():
        if data.get("category") == "main":
            name = data['name_ru'] if lang == 'ru' else data['name_en']
            buttons.append([InlineKeyboardButton(text=name, callback_data=f"tariff_{key}")])
    buttons.append([InlineKeyboardButton(text="👈🏻 Паки", callback_data="show_paki")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_paki_keyboard(lang):
    buttons = []
    for key, data in TARIFFS.items():
        if data.get("category") == "paki":
            name = data['name_ru'] if lang == 'ru' else data['name_en']
            buttons.append([InlineKeyboardButton(text=name, callback_data=f"tariff_{key}")])
    buttons.append([InlineKeyboardButton(text="👈 НАЗАД", callback_data="back_to_prices")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_test_tariff_keyboard(lang):
    buttons = [
        [InlineKeyboardButton(text="💳 ОПЛАТИТЬ", callback_data="pay_test")],
        [InlineKeyboardButton(text="👈 НАЗАД", callback_data="back_to_prices")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_tariff_details_keyboard(tariff_key, lang, user_id):
    buttons = []
    buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_promo"], callback_data=f"enter_promo_{tariff_key}")])
    
    is_paid = is_tariff_paid(user_id, tariff_key)
    
    if not is_paid:
        buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_pay"], callback_data=f"choose_pay_{tariff_key}")])
        buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_pay_for_friend"], callback_data=f"pay_for_friend_{tariff_key}")])
    
    buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_payment_method_keyboard(tariff_key, discount_percent=0, lang="ru"):
    tariff = TARIFFS[tariff_key]
    
    if discount_percent > 0:
        btn_card = LANG[lang]["btn_pay_card_disc"].format(disc=discount_percent)
        btn_stars = LANG[lang]["btn_pay_stars_disc"].format(disc=discount_percent)
        btn_crypto = LANG[lang]["btn_pay_crypto_disc"].format(disc=discount_percent)
    else:
        btn_card = LANG[lang]["btn_pay_card"]
        btn_stars = LANG[lang]["btn_pay_stars"]
        btn_crypto = LANG[lang]["btn_pay_crypto"]

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_card, callback_data=f"pay_card_{tariff_key}")],
        [InlineKeyboardButton(text=btn_stars, callback_data=f"pay_stars_{tariff_key}")],
        [InlineKeyboardButton(text=btn_crypto, callback_data=f"pay_crypto_{tariff_key}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
    ])

def get_crypto_currency_keyboard(tariff_key, discount_percent=0, lang="ru"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=LANG[lang]["btn_crypto_usdt"], callback_data=f"crypto_usdt_{tariff_key}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_crypto_ton"], callback_data=f"crypto_ton_{tariff_key}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_crypto_btc"], callback_data=f"crypto_btc_{tariff_key}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_crypto_direct"], callback_data=f"crypto_direct_{tariff_key}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
    ])

def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_mailing")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📋 Заявки на оплату", callback_data="admin_payment_requests")]
    ])

def get_payment_request_keyboard(request_id, lang="ru"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать лично", callback_data=f"write_user_{request_id}")],
        [InlineKeyboardButton(text="🤖 Написать через бота", callback_data=f"write_via_bot_{request_id}")],
        [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"confirm_payment_{request_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_admin")]
    ])

# ==================================================
# ХЭНДЛЕРЫ
# ==================================================

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "Пользователь"
    username = message.from_user.username
    
    add_user(user_id, first_name, username)
    
    lang = await get_lang(state)
    
    welcome_text = f"""👋 Привет, {first_name}!
Ты попал в наш бот✅

Нажимая на каждый тариф ты видишь краткое описание.

Если бот не доступен пиши мне

Тех.поддержка: @kasgd"""
    
    await message.answer(welcome_text, disable_web_page_preview=True)
    
    menu_text = LANG[lang]["main_menu_text"]
    await message.answer(menu_text, reply_markup=get_tariff_keyboard(lang))

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    user_count = get_user_count()
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM payment_requests WHERE status = "pending"')
    pending_count = cursor.fetchone()[0] or 0
    conn.close()
    
    text = LANG["ru"]["admin_panel"].format(user_count=user_count, pending_count=pending_count)
    
    await message.answer(text, reply_markup=get_admin_keyboard())

@dp.callback_query(F.data == "admin_mailing")
async def admin_mailing_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.message.delete()
    await callback.message.answer(
        "📨 <b>Рассылка</b>\n\n"
        "Отправь мне сообщение (текст, фото, видео, GIF, документ), "
        "и я разошлю его ВСЕМ пользователям бота.\n\n"
        "⚠️ <b>Внимание:</b> Рассылка пойдёт всем пользователям, которые "
        "когда-либо взаимодействовали с ботом.\n\n"
        "🔄 Чтобы отменить, отправь /cancel"
    )
    await state.set_state(MailingStates.waiting_for_content)

@dp.message(Command("mail"))
async def cmd_mail(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏷️ Скидка 25%", callback_data="mail_promo25")],
        [InlineKeyboardButton(text="🏷️ Скидка 40%", callback_data="mail_promo40")],
        [InlineKeyboardButton(text="🏷️ Скидка 60%", callback_data="mail_promo60")],
        [InlineKeyboardButton(text="📨 Обычная рассылка", callback_data="mail_normal")]
    ])
    
    await message.answer(
        "📨 <b>Выбери тип рассылки:</b>\n\n"
        "• Скидка 25% — пользователь получит скидку 25%\n"
        "• Скидка 40% — пользователь получит скидку 40%\n"
        "• Скидка 60% — пользователь получит скидку 60%\n"
        "• Обычная — просто текст",
        reply_markup=keyboard
    )
    await state.set_state(MailingStates.waiting_for_mail_type)

@dp.callback_query(MailingStates.waiting_for_mail_type)
async def process_mail_type(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    mail_type = callback.data.replace("mail_", "")
    await state.update_data(mail_type=mail_type)
    
    await callback.message.delete()
    await callback.message.answer(
        "📝 <b>Отправь текст сообщения</b>\n\n"
        "Этот текст увидят все пользователи. Ты можешь отправить:\n"
        "• Текст\n"
        "• Фото\n"
        "• Видео\n"
        "• GIF\n\n"
        "🔄 Чтобы отменить, отправь /cancel"
    )
    await state.set_state(MailingStates.waiting_for_content)
    await callback.answer()

@dp.message(MailingStates.waiting_for_content)
async def process_mailing_content(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    data = await state.get_data()
    mail_type = data.get("mail_type", "normal")
    
    await message.answer("⏳ Начинаю рассылку...")
    
    users = get_all_users()
    
    if not users:
        await message.answer("❌ Нет пользователей для рассылки!")
        await state.clear()
        return
    
    if mail_type == "promo25":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏷️ АКТИВИРОВАТЬ СКИДКУ", callback_data="mail_discount_25")]
        ])
        footer = "\n\n🔥 Нажми кнопку, чтобы активировать скидку 25% на любой тариф!"
    elif mail_type == "promo40":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏷️ АКТИВИРОВАТЬ СКИДКУ", callback_data="mail_discount_40")]
        ])
        footer = "\n\n🔥 Нажми кнопку, чтобы активировать скидку 40% на любой тариф!"
    elif mail_type == "promo60":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏷️ АКТИВИРОВАТЬ СКИДКУ", callback_data="mail_discount_60")]
        ])
        footer = "\n\n🔥 Нажми кнопку, чтобы активировать скидку 60% на любой тариф!"
    else:
        keyboard = None
        footer = ""
    
    success = 0
    failed = 0
    
    for user_id in users:
        try:
            if message.text:
                text = message.text + footer
                await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=keyboard)
            elif message.photo:
                await bot.send_photo(user_id, message.photo[-1].file_id, caption=message.caption + footer, reply_markup=keyboard)
            elif message.video:
                await bot.send_video(user_id, message.video.file_id, caption=message.caption + footer, reply_markup=keyboard)
            elif message.animation:
                await bot.send_animation(user_id, message.animation.file_id, caption=message.caption + footer, reply_markup=keyboard)
            elif message.document:
                await bot.send_document(user_id, message.document.file_id, caption=message.caption + footer, reply_markup=keyboard)
            else:
                await message.answer("❌ Неподдерживаемый тип сообщения!")
                await state.clear()
                return
            
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
    
    await message.answer(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📤 Отправлено: {success}\n"
        f"❌ Не доставлено: {failed}\n"
        f"👥 Всего пользователей: {len(users)}\n"
        f"📌 Тип: {mail_type}"
    )
    await state.clear()

@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Отменено.")

@dp.callback_query(F.data == "mail_discount_25")
async def mail_discount_25(callback: CallbackQuery):
    user_id = callback.from_user.id
    add_user_discount(user_id, "SUPER25", 25)
    
    await callback.message.edit_text(
        "🏷️ <b>Скидка 25% активирована!</b>\n\n"
        "Ты получил скидку 25% на любой тариф 🎉\n\n"
        "Скидка будет применена автоматически при покупке."
    )
    await callback.answer("✅ Скидка 25% активирована!", show_alert=True)

@dp.callback_query(F.data == "mail_discount_40")
async def mail_discount_40(callback: CallbackQuery):
    user_id = callback.from_user.id
    add_user_discount(user_id, "HOMAKE40", 40)
    
    await callback.message.edit_text(
        "🏷️ <b>Скидка 40% активирована!</b>\n\n"
        "Ты получил скидку 40% на любой тариф 🎉\n\n"
        "Скидка будет применена автоматически при покупке."
    )
    await callback.answer("✅ Скидка 40% активирована!", show_alert=True)

@dp.callback_query(F.data == "mail_discount_60")
async def mail_discount_60(callback: CallbackQuery):
    user_id = callback.from_user.id
    add_user_discount(user_id, "newpopolnenie", 60)
    
    await callback.message.edit_text(
        "🏷️ <b>Скидка 60% активирована!</b>\n\n"
        "Ты получил скидку 60% на любой тариф 🎉\n\n"
        "Скидка будет применена автоматически при покупке."
    )
    await callback.answer("✅ Скидка 60% активирована!", show_alert=True)

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    user_count = get_user_count()
    
    await callback.message.edit_text(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: {user_count}",
        reply_markup=get_admin_keyboard()
    )
    await callback.answer()

@dp.message(Command("test67"))
async def cmd_test67(message: Message, state: FSMContext):
    lang = await get_lang(state)
    user_id = message.from_user.id
    
    is_paid = is_tariff_paid(user_id, "test")
    
    if is_paid:
        text = f"""📋 <b>{TEST_TARIFF['name_ru'] if lang == 'ru' else TEST_TARIFF['name_en']}</b>

💰 Цена: БЕСПЛАТНО 🎉
Срок доступа: {TEST_TARIFF['duration_ru'] if lang == 'ru' else TEST_TARIFF['duration_en']}

{TEST_TARIFF['desc_ru'] if lang == 'ru' else TEST_TARIFF['desc_en']}

✅ <b>ТАРИФ ОПЛАЧЕН</b>

🔑 Для получения ссылки напишите в поддержку @kasgd"""
        await message.answer(text)
        return
    
    text = f"""📋 <b>{TEST_TARIFF['name_ru'] if lang == 'ru' else TEST_TARIFF['name_en']}</b>

💰 Цена: БЕСПЛАТНО 🎉
Срок доступа: {TEST_TARIFF['duration_ru'] if lang == 'ru' else TEST_TARIFF['duration_en']}

{TEST_TARIFF['desc_ru'] if lang == 'ru' else TEST_TARIFF['desc_en']}"""
    
    await message.answer(text, reply_markup=get_test_tariff_keyboard(lang))

@dp.callback_query(F.data == "pay_test")
async def pay_test_tariff(callback: CallbackQuery, state: FSMContext):
    lang = await get_lang(state)
    user_id = callback.from_user.id
    
    if is_tariff_paid(user_id, "test"):
        await callback.answer("❌ Вы уже активировали тестовый тариф!", show_alert=True)
        return
    
    await callback.message.delete()
    await save_payment_and_send_link(callback.message, "test", lang, user_id)
    await callback.answer("✅ Доступ открыт!")

@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для этой команды!")
        return
    await message.answer("🔄 Выполняю сброс...")
    await message.answer("✅ Бот сброшен!")

@dp.message(Command("language"))
async def cmd_language(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="set_lang_ru")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="set_lang_en")]
    ])
    await message.answer("🌍 Выберите язык:", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_lang_"))
async def process_lang_change(callback: CallbackQuery, state: FSMContext):
    lang = callback.data.replace("set_lang_", "")
    await state.update_data(lang=lang)
    await callback.answer()
    await callback.message.delete()
    await callback.message.answer(f"✅ Язык установлен на {'Русский' if lang == 'ru' else 'English'}! Нажмите /start")

@dp.message(F.text.in_([LANG["ru"]["btn_prices"], LANG["en"]["btn_prices"]]))
async def show_prices(message: Message, state: FSMContext):
    lang = await get_lang(state)
    await message.answer(LANG[lang]["main_menu_text"], reply_markup=get_tariff_keyboard(lang))

@dp.message(F.text.in_([LANG["ru"]["btn_subs"], LANG["en"]["btn_subs"]]))
async def show_subscriptions(message: Message, state: FSMContext):
    lang = await get_lang(state)
    user_id = message.from_user.id
    
    paid_list = get_paid_tariffs(user_id)
    
    if paid_list:
        subs_list = []
        for tariff_key in paid_list:
            if tariff_key == "test":
                name = TEST_TARIFF['name_ru'] if lang == "ru" else TEST_TARIFF['name_en']
                subs_list.append(LANG[lang]["subs_list_item"].format(name=name))
            elif tariff_key in TARIFFS:
                name = TARIFFS[tariff_key]['name_ru'] if lang == "ru" else TARIFFS[tariff_key]['name_en']
                subs_list.append(LANG[lang]["subs_list_item"].format(name=name))
        
        if subs_list:
            text = LANG[lang]["subs_menu"].format(list="\n".join(subs_list))
            await message.answer(text)
            return
    
    await message.answer(LANG[lang]["no_subs"])

# ==================================================
# ОБРАБОТЧИКИ ТАРИФОВ И ОПЛАТЫ
# ==================================================

@dp.callback_query(F.data == "back_to_prices")
async def back_to_prices(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    lang = await get_lang(state)
    await callback.message.edit_text(LANG[lang]["main_menu_text"], reply_markup=get_tariff_keyboard(lang))

@dp.callback_query(F.data == "show_paki")
async def show_paki(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    lang = await get_lang(state)
    await callback.message.edit_text(LANG[lang]["main_menu_text"], reply_markup=get_paki_keyboard(lang))

@dp.callback_query(F.data.startswith("tariff_"))
async def show_tariff_details(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("tariff_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    lang = await get_lang(state)
    data = await state.get_data()
    discount = data.get("discount", 0)
    user_id = callback.from_user.id
    
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    duration = tariff['duration_ru'] if lang == "ru" else tariff['duration_en']
    desc = tariff['desc_ru'] if lang == "ru" else tariff['desc_en']
    
    if tariff['price_rub'] == 0:
        price_text = "БЕСПЛАТНО 🎉"
    elif discount > 0:
        new_price = int(tariff['price_rub'] * (1 - discount / 100))
        price_text = f"<s>{tariff['price_rub']} 🇷🇺RUB</s> → {new_price} 🇷🇺RUB <b>(-{discount}%)</b>"
    else:
        price_text = f"{tariff['price_rub']} 🇷🇺RUB"
    
    is_paid = is_tariff_paid(user_id, tariff_key)
    
    if is_paid:
        text = LANG[lang]["tariff_desc_paid"].format(
            name=name,
            price_text=price_text,
            duration=duration,
            desc=desc
        )
    else:
        text = LANG[lang]["tariff_desc"].format(
            name=name,
            price_text=price_text,
            duration=duration,
            desc=desc
        )
    
    await callback.message.edit_text(text, reply_markup=get_tariff_details_keyboard(tariff_key, lang, user_id))

@dp.callback_query(F.data.startswith("enter_promo_"))
async def enter_promo(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("enter_promo_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    lang = await get_lang(state)
    await state.update_data(current_tariff=tariff_key)
    await callback.message.edit_text(
        LANG[lang]["enter_promo"],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=LANG[lang]["btn_cancel"], callback_data=f"cancel_promo_{tariff_key}")]])
    )
    await state.set_state(PromoStates.waiting_for_promo)

@dp.message(PromoStates.waiting_for_promo)
async def process_promo(message: Message, state: FSMContext):
    promo_code = message.text.strip().upper()
    data = await state.get_data()
    tariff_key = data.get("current_tariff")
    lang = await get_lang(state)
    
    if not tariff_key or tariff_key not in TARIFFS:
        await state.clear()
        await message.answer("❌ Ошибка. Попробуйте выбрать тариф заново.")
        return

    if promo_code in PROMO_CODES:
        discount = PROMO_CODES[promo_code]
        await state.update_data(discount=discount)
        
        tariff = TARIFFS[tariff_key]
        name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
        new_rub = int(tariff['price_rub'] * (1 - discount / 100))
        
        text = LANG[lang]["promo_success"].format(
            code=promo_code, 
            discount=discount, 
            name=name, 
            old_rub=tariff['price_rub'], 
            new_rub=new_rub
        )
        await message.answer(text, reply_markup=get_payment_method_keyboard(tariff_key, discount, lang))
        await state.clear()
    else:
        await message.answer(LANG[lang]["promo_fail"])

@dp.callback_query(F.data.startswith("cancel_promo_"))
async def cancel_promo(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("cancel_promo_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    lang = await get_lang(state)
    await state.clear()
    await callback.message.delete()
    tariff = TARIFFS[tariff_key]
    data = await state.get_data()
    discount = data.get("discount", 0)
    user_id = callback.from_user.id
    
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    duration = tariff['duration_ru'] if lang == "ru" else tariff['duration_en']
    desc = tariff['desc_ru'] if lang == "ru" else tariff['desc_en']

    if tariff['price_rub'] == 0:
        price_text = "БЕСПЛАТНО 🎉"
    elif discount > 0:
        new_price = int(tariff['price_rub'] * (1 - discount / 100))
        price_text = f"<s>{tariff['price_rub']} RUB</s> -> {new_price} RUB <b>(-{discount}%)</b>"
    else:
        price_text = f"{tariff['price_rub']} RUB"

    is_paid = is_tariff_paid(user_id, tariff_key)
    
    if is_paid:
        text = LANG[lang]["tariff_desc_paid"].format(
            name=name,
            price_text=price_text,
            duration=duration,
            desc=desc
        )
    else:
        text = LANG[lang]["tariff_desc"].format(
            name=name,
            price_text=price_text,
            duration=duration,
            desc=desc
        )
    
    await callback.message.answer(text, reply_markup=get_tariff_details_keyboard(tariff_key, lang, user_id))

@dp.callback_query(F.data.startswith("choose_pay_"))
async def choose_payment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("choose_pay_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    await choose_payment_logic(callback, state, tariff_key)

# ==================================================
# ОПЛАТА ДЛЯ ДРУГА (РАБОТАЕТ ТАК ЖЕ)
# ==================================================

@dp.callback_query(F.data.startswith("pay_for_friend_"))
async def pay_for_friend(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("pay_for_friend_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    await choose_payment_logic(callback, state, tariff_key)

# ==================================================
# ОПЛАТА НА КАРТУ
# ==================================================

@dp.callback_query(F.data.startswith("pay_card_"))
async def process_card_payment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("pay_card_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    lang = await get_lang(state)
    data = await state.get_data()
    discount = data.get("discount", 0)
    user_id = callback.from_user.id
    
    final_price = int(TARIFFS[tariff_key]['price_rub'] * (1 - discount / 100))
    
    text = LANG[lang]["pay_card"].format(
        final=final_price,
        user_id=user_id
    )
    
    copy_button = InlineKeyboardButton(text="📋 Скопировать номер карты", callback_data=f"copy_card_{tariff_key}")
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [copy_button],
            [InlineKeyboardButton(text=LANG[lang]["btn_i_paid"], callback_data=f"i_paid_{tariff_key}")],
            [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
        ]),
        disable_web_page_preview=True
    )

@dp.callback_query(F.data.startswith("copy_card_"))
async def copy_card(callback: CallbackQuery):
    await callback.answer("💳 Номер карты скопирован!\n\n2200190284092510", show_alert=True)

@dp.callback_query(F.data.startswith("i_paid_"))
async def i_paid(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("i_paid_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    lang = await get_lang(state)
    await state.update_data(current_tariff=tariff_key)
    
    text = LANG[lang]["i_paid_confirm"]
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=LANG[lang]["btn_cancel"], callback_data=f"cancel_payment_{tariff_key}")]
        ])
    )
    await state.set_state(PaymentStates.waiting_for_receipt)

@dp.message(PaymentStates.waiting_for_receipt, F.photo | F.document | F.video)
async def process_receipt(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    data = await state.get_data()
    tariff_key = data.get("current_tariff")
    lang = await get_lang(state)
    
    if not tariff_key or tariff_key not in TARIFFS:
        await state.clear()
        await message.answer("❌ Ошибка. Попробуйте выбрать тариф заново.")
        return
    
    tariff = TARIFFS[tariff_key]
    discount = data.get("discount", 0)
    final_price = int(tariff['price_rub'] * (1 - discount / 100))
    
    media_file_id = None
    media_type = None
    message_text = message.caption or ""
    
    if message.photo:
        media_file_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        media_file_id = message.video.file_id
        media_type = "video"
    elif message.document:
        media_file_id = message.document.file_id
        media_type = "document"
    
    request_id = add_payment_request(user_id, username, tariff_key, final_price, message_text, media_file_id, media_type)
    
    if not request_id:
        await message.answer("❌ Ошибка сохранения заявки. Попробуйте позже.")
        await state.clear()
        return
    
    user_link = f"<a href='tg://user?id={user_id}'>{username}</a>"
    tariff_name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    
    media_info = ""
    if media_file_id:
        media_info = f"📎 <b>Есть вложение:</b> {media_type}"
    
    admin_text = LANG[lang]["new_payment_request"].format(
        user_link=user_link,
        user_id=user_id,
        tariff_name=tariff_name,
        amount=final_price,
        message_text=message_text or "Нет сообщения",
        media_info=media_info
    )
    
    for admin_id in ADMIN_IDS:
        try:
            if media_file_id and media_type == "photo":
                await bot.send_photo(admin_id, media_file_id, caption=admin_text, reply_markup=get_payment_request_keyboard(request_id, lang))
            elif media_file_id and media_type == "video":
                await bot.send_video(admin_id, media_file_id, caption=admin_text, reply_markup=get_payment_request_keyboard(request_id, lang))
            elif media_file_id and media_type == "document":
                await bot.send_document(admin_id, media_file_id, caption=admin_text, reply_markup=get_payment_request_keyboard(request_id, lang))
            else:
                await bot.send_message(admin_id, admin_text, reply_markup=get_payment_request_keyboard(request_id, lang))
        except Exception as e:
            logging.error(f"Ошибка отправки заявки админу {admin_id}: {e}")
    
    await message.answer(LANG[lang]["payment_receipt_received"])
    await state.clear()

@dp.message(PaymentStates.waiting_for_receipt)
async def process_receipt_invalid(message: Message, state: FSMContext):
    lang = await get_lang(state)
    await message.answer("❌ Пожалуйста, отправьте ЧЕК в виде фото или скриншота (не документом!).")

@dp.callback_query(F.data.startswith("cancel_payment_"))
async def cancel_payment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("cancel_payment_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    lang = await get_lang(state)
    await state.clear()
    await callback.message.delete()
    
    tariff = TARIFFS[tariff_key]
    data = await state.get_data()
    discount = data.get("discount", 0)
    user_id = callback.from_user.id
    
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    duration = tariff['duration_ru'] if lang == "ru" else tariff['duration_en']
    desc = tariff['desc_ru'] if lang == "ru" else tariff['desc_en']

    if tariff['price_rub'] == 0:
        price_text = "БЕСПЛАТНО 🎉"
    elif discount > 0:
        new_price = int(tariff['price_rub'] * (1 - discount / 100))
        price_text = f"<s>{tariff['price_rub']} RUB</s> -> {new_price} RUB <b>(-{discount}%)</b>"
    else:
        price_text = f"{tariff['price_rub']} RUB"

    is_paid = is_tariff_paid(user_id, tariff_key)
    
    if is_paid:
        text = LANG[lang]["tariff_desc_paid"].format(
            name=name,
            price_text=price_text,
            duration=duration,
            desc=desc
        )
    else:
        text = LANG[lang]["tariff_desc"].format(
            name=name,
            price_text=price_text,
            duration=duration,
            desc=desc
        )
    
    await callback.message.answer(text, reply_markup=get_tariff_details_keyboard(tariff_key, lang, user_id))

@dp.callback_query(F.data.startswith("pay_stars_"))
async def process_stars_payment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("pay_stars_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    lang = await get_lang(state)
    data = await state.get_data()
    discount = data.get("discount", 0)
    
    final_price = int(tariff['price_stars'] * (1 - discount / 100))
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    duration = tariff['duration_ru'] if lang == "ru" else tariff['duration_en']
    
    if discount > 0:
        price_line = f"💰 Цена: <s>{tariff['price_stars']} STARS</s> → {final_price} STARS (-{discount}%)\n"
    else:
        price_line = f"💰 Цена: {final_price} STARS\n"
    
    support = SUPPORT_CONTACT_RU if lang == "ru" else SUPPORT_CONTACT_EN
    
    text = LANG[lang]["pay_stars"].format(
        name=name,
        duration=duration,
        price_line=price_line,
        final=final_price,
        support=support
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨‍💼 Написать админу", url="https://t.me/kasgd")],
            [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
        ]),
        disable_web_page_preview=True
    )

# ==================================================
# КРИПТОВАЛЮТА
# ==================================================

@dp.callback_query(F.data.startswith("pay_crypto_"))
async def process_crypto_payment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("pay_crypto_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    lang = await get_lang(state)
    
    text = LANG[lang]["pay_crypto_choose"]
    
    await callback.message.edit_text(
        text,
        reply_markup=get_crypto_currency_keyboard(tariff_key, 0, lang)
    )

@dp.callback_query(F.data.startswith("crypto_usdt_"))
async def crypto_usdt_payment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("crypto_usdt_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    lang = await get_lang(state)
    data = await state.get_data()
    discount = data.get("discount", 0)
    user_id = callback.from_user.id
    
    final_rub = int(tariff['price_rub'] * (1 - discount / 100))
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    
    final_usdt = round_to_half(final_rub / USDT_RATE)
    
    invoice_data = await create_crypto_invoice(final_usdt, user_id, tariff_key, "USDT")
    
    if invoice_data:
        invoice_id = invoice_data["invoice_id"]
        pay_url = invoice_data["pay_url"]
        
        save_crypto_invoice(invoice_id, user_id, tariff_key, final_usdt, "USDT")
        
        text = LANG[lang]["pay_crypto_invoice"].format(
            name=name,
            amount=final_usdt,
            asset="USDT"
        )
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=LANG[lang]["btn_pay_now"], url=pay_url)],
                [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
            ])
        )
    else:
        await callback.message.edit_text(
            LANG[lang]["crypto_error"],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
            ])
        )

@dp.callback_query(F.data.startswith("crypto_ton_"))
async def crypto_ton_payment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("crypto_ton_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    lang = await get_lang(state)
    data = await state.get_data()
    discount = data.get("discount", 0)
    user_id = callback.from_user.id
    
    final_rub = int(tariff['price_rub'] * (1 - discount / 100))
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    
    final_ton = round_to_half(final_rub / TON_RATE)
    
    invoice_data = await create_crypto_invoice(final_ton, user_id, tariff_key, "TON")
    
    if invoice_data:
        invoice_id = invoice_data["invoice_id"]
        pay_url = invoice_data["pay_url"]
        
        save_crypto_invoice(invoice_id, user_id, tariff_key, final_ton, "TON")
        
        text = LANG[lang]["pay_crypto_invoice"].format(
            name=name,
            amount=final_ton,
            asset="TON"
        )
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=LANG[lang]["btn_pay_now"], url=pay_url)],
                [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
            ])
        )
    else:
        await callback.message.edit_text(
            LANG[lang]["crypto_error"],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
            ])
        )

@dp.callback_query(F.data.startswith("crypto_btc_"))
async def crypto_btc_payment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("crypto_btc_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    lang = await get_lang(state)
    data = await state.get_data()
    discount = data.get("discount", 0)
    user_id = callback.from_user.id
    
    final_rub = int(tariff['price_rub'] * (1 - discount / 100))
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    
    final_btc = round(final_rub / BTC_RATE, 8)
    
    invoice_data = await create_crypto_invoice(final_btc, user_id, tariff_key, "BTC")
    
    if invoice_data:
        invoice_id = invoice_data["invoice_id"]
        pay_url = invoice_data["pay_url"]
        
        save_crypto_invoice(invoice_id, user_id, tariff_key, final_btc, "BTC")
        
        text = LANG[lang]["pay_crypto_invoice"].format(
            name=name,
            amount=final_btc,
            asset="BTC"
        )
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=LANG[lang]["btn_pay_now"], url=pay_url)],
                [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
            ])
        )
    else:
        await callback.message.edit_text(
            LANG[lang]["crypto_error"],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
            ])
        )

@dp.callback_query(F.data.startswith("crypto_direct_"))
async def crypto_direct_payment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("crypto_direct_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    lang = await get_lang(state)
    
    text = LANG[lang]["crypto_direct"]
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨‍💼 Написать админу", url="https://t.me/kasgd")],
            [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
        ])
    )

# ==================================================
# ВЕБХУК ДЛЯ CRYPTOBOT
# ==================================================

async def send_crypto_success(user_id: int, tariff_key: str):
    try:
        lang = "ru"
        text = LANG[lang]["crypto_payment_success"]
        await bot.send_message(user_id, text)
    except Exception as e:
        logging.error(f"Ошибка отправки сообщения об успешной оплате: {e}")

# ==================================================
# АДМИН: ЗАЯВКИ НА ОПЛАТУ
# ==================================================

@dp.callback_query(F.data == "admin_payment_requests")
async def admin_payment_requests(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, user_id, username, tariff_key, amount, created_at, status FROM payment_requests ORDER BY created_at DESC LIMIT 20')
    requests = cursor.fetchall()
    conn.close()
    
    if not requests:
        await callback.message.edit_text("📋 <b>Заявки на оплату</b>\n\nНет заявок на проверку.", reply_markup=get_admin_keyboard())
        await callback.answer()
        return
    
    text = "📋 <b>Последние заявки на оплату</b>\n\n"
    for req in requests:
        status_emoji = "⏳" if req[6] == "pending" else "✅" if req[6] == "confirmed" else "❌"
        text += f"{status_emoji} #{req[0]} | Пользователь: {req[2]} | {req[4]} RUB | {req[5]}\n"
    
    text += "\nДля просмотра заявки нажмите /view_request <номер>"
    
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())
    await callback.answer()

@dp.message(Command("view_request"))
async def view_request(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    try:
        request_id = int(message.text.split()[1])
    except:
        await message.answer("❌ Использование: /view_request <id>")
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM payment_requests WHERE id = ?', (request_id,))
    req = cursor.fetchone()
    conn.close()
    
    if not req:
        await message.answer("❌ Заявка не найдена.")
        return
    
    lang = await get_lang(message)
    tariff_name = TARIFFS.get(req[3], {}).get('name_ru', req[3])
    
    text = f"""
📋 <b>Заявка #{req[0]}</b>

👤 Пользователь: <a href='tg://user?id={req[1]}'>{req[2]}</a>
🆔 ID: <code>{req[1]}</code>
📋 Тариф: {tariff_name}
💰 Сумма: {req[4]} RUB
📝 Сообщение: {req[5] or "Нет"}
📎 Медиа: {req[7] or "Нет"}
📅 Создана: {req[8]}
📊 Статус: {req[6]}
"""
    
    if req[7]:
        try:
            if req[7] == "photo":
                await bot.send_photo(message.chat.id, req[6], caption=text, reply_markup=get_payment_request_keyboard(request_id, lang))
            elif req[7] == "video":
                await bot.send_video(message.chat.id, req[6], caption=text, reply_markup=get_payment_request_keyboard(request_id, lang))
            elif req[7] == "document":
                await bot.send_document(message.chat.id, req[6], caption=text, reply_markup=get_payment_request_keyboard(request_id, lang))
            else:
                await message.answer(text, reply_markup=get_payment_request_keyboard(request_id, lang))
        except Exception as e:
            await message.answer(text, reply_markup=get_payment_request_keyboard(request_id, lang))
    else:
        await message.answer(text, reply_markup=get_payment_request_keyboard(request_id, lang))

@dp.callback_query(F.data.startswith("write_user_"))
async def write_user(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    request_id = int(callback.data.replace("write_user_", ""))
    req = get_payment_request(request_id)
    
    if not req:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return
    
    user_id = req[1]
    username = req[2]
    
    try:
        await bot.send_message(user_id, "👋 Администратор свяжется с вами лично в ближайшее время.")
        await callback.answer(f"✅ Открыт чат с пользователем {username}", show_alert=True)
        await callback.message.answer(f"✍️ Напишите пользователю: <a href='tg://user?id={user_id}'>{username}</a>")
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)

@dp.callback_query(F.data.startswith("write_via_bot_"))
async def write_via_bot(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    request_id = int(callback.data.replace("write_via_bot_", ""))
    req = get_payment_request(request_id)
    
    if not req:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return
    
    await state.update_data(reply_request_id=request_id)
    await callback.message.answer("✍️ Напишите сообщение, которое будет отправлено пользователю от имени бота:")
    await state.set_state(AdminReplyStates.waiting_for_reply)
    await callback.answer()

@dp.message(AdminReplyStates.waiting_for_reply)
async def process_admin_reply(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    data = await state.get_data()
    request_id = data.get("reply_request_id")
    req = get_payment_request(request_id)
    
    if not req:
        await message.answer("❌ Заявка не найдена.")
        await state.clear()
        return
    
    user_id = req[1]
    
    try:
        await bot.send_message(user_id, f"📨 <b>Сообщение от администратора:</b>\n\n{message.text}")
        await message.answer("✅ Сообщение отправлено пользователю!")
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки: {e}")
    
    await state.clear()

@dp.callback_query(F.data.startswith("confirm_payment_"))
async def confirm_payment(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    request_id = int(callback.data.replace("confirm_payment_", ""))
    req = get_payment_request(request_id)
    
    if not req:
        await callback.answer("❌ Заявка не найдена", show_alert=True)
        return
    
    user_id = req[1]
    tariff_key = req[3]
    
    add_paid_tariff(user_id, tariff_key)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE payment_requests SET status = "confirmed" WHERE id = ?', (request_id,))
    conn.commit()
    conn.close()
    
    lang = "ru"
    chat_id = CHANNEL_IDS.get(tariff_key)
    if chat_id:
        link = await create_one_time_link(chat_id)
        if link:
            text = LANG[lang]["payment_success"].format(link=link)
            await bot.send_message(user_id, text, disable_web_page_preview=False)
        else:
            await bot.send_message(user_id, "✅ Оплата подтверждена! Напишите @kasgd для получения ссылки.")
    else:
        await bot.send_message(user_id, "✅ Оплата подтверждена! Напишите @kasgd для получения ссылки.")
    
    await callback.message.delete()
    await callback.message.answer(f"✅ Оплата по заявке #{request_id} подтверждена! Пользователь уведомлен.")
    await callback.answer()

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    user_count = get_user_count()
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM payment_requests WHERE status = "pending"')
    pending_count = cursor.fetchone()[0] or 0
    conn.close()
    
    text = LANG["ru"]["admin_panel"].format(user_count=user_count, pending_count=pending_count)
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())
    await callback.answer()

# ==================================================
# ЗАПУСК
# ==================================================
async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    
    if not BOT_TOKEN:
        logging.error("❌ BOT_TOKEN не задан в переменных окружения!")
        return
    
    print("=" * 60)
    print("🚀 ОСНОВНОЙ БОТ ЗАПУЩЕН!")
    print("📦 База данных: Supabase + SQLite")
    print(f"🪙 CRYPTO_TOKEN: {CRYPTOBOT_API_KEY[:10]}..." if CRYPTOBOT_API_KEY else "🪙 CRYPTO_TOKEN: НЕ ЗАДАН!")
    print(f"💵 Курс USDT: {USDT_RATE} RUB")
    print(f"💎 Курс TON: {TON_RATE} RUB")
    print(f"₿ Курс BTC: {BTC_RATE} RUB")
    print("📞 Поддержка: @kasgd")
    print("👥 Админы: " + ", ".join(str(admin) for admin in ADMIN_IDS))
    print("=" * 60)
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("✅ Flask запущен в фоновом потоке!")
    asyncio.run(main())

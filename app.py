import logging
import asyncio
import os
import json
import uuid
import sqlite3
import threading
import re
import aiohttp
import random
import string
import socket
from datetime import datetime, timedelta
from flask import Flask, request
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ChatJoinRequest
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from supabase import create_client, Client

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
# SUPABASE (REST API)
# ==================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    logging.error("❌ SUPABASE_URL или SUPABASE_KEY не заданы!")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logging.info("✅ Supabase REST API подключен")

# ==================================================
# ФУНКЦИИ РАБОТЫ С БАЗОЙ (REST API)
# ==================================================

def get_all_users():
    try:
        response = supabase.table('users').select('user_id').execute()
        return [row['user_id'] for row in response.data]
    except Exception as e:
        logging.error(f"Ошибка получения пользователей: {e}")
        return []

def get_user_count():
    try:
        response = supabase.table('users').select('*', count='exact').execute()
        return response.count or 0
    except Exception as e:
        logging.error(f"Ошибка получения количества пользователей: {e}")
        return 0

def add_user(user_id: int, first_name: str, username: str = None):
    try:
        logging.info(f"🔍 add_user вызван: ID={user_id}, Name={first_name}, Username={username}")
        
        response = supabase.table('users').select('user_id').eq('user_id', user_id).execute()
        logging.info(f"📊 Результат проверки: {response.data}")
        
        if response.data:
            logging.info(f"🔄 Обновление пользователя {user_id}")
            update_response = supabase.table('users').update({
                'first_name': first_name,
                'username': username
            }).eq('user_id', user_id).execute()
            logging.info(f"📊 Обновлено: {update_response.data}")
            return True
        else:
            logging.info(f"➕ Создание пользователя {user_id}")
            insert_response = supabase.table('users').insert({
                'user_id': user_id,
                'first_name': first_name,
                'username': username
            }).execute()
            logging.info(f"📊 Создано: {insert_response.data}")
            return True
    except Exception as e:
        logging.error(f"❌ Ошибка добавления пользователя {user_id}: {e}")
        return False

def get_active_subscriptions(user_id: int):
    try:
        response = supabase.table('subscriptions')\
            .select('*')\
            .eq('user_id', user_id)\
            .eq('status', 'active')\
            .execute()
        return response.data
    except Exception as e:
        logging.error(f"Ошибка получения подписок: {e}")
        return []

def get_subscription_by_tariff(user_id: int, tariff_key: str):
    try:
        response = supabase.table('subscriptions')\
            .select('*')\
            .eq('user_id', user_id)\
            .eq('tariff_key', tariff_key)\
            .eq('status', 'active')\
            .execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logging.error(f"Ошибка получения подписки: {e}")
        return None

def add_subscription(user_id: int, tariff_key: str, duration_days: int = None):
    try:
        expires_at = None
        if duration_days is not None:
            expires_at = (datetime.now() + timedelta(days=duration_days)).isoformat()
        
        supabase.table('subscriptions').insert({
            'user_id': user_id,
            'tariff_key': tariff_key,
            'expires_at': expires_at,
            'status': 'active'
        }).execute()
        return True
    except Exception as e:
        logging.error(f"Ошибка добавления подписки: {e}")
        return False

def extend_subscription(user_id: int, tariff_key: str, duration_days: int):
    try:
        sub = get_subscription_by_tariff(user_id, tariff_key)
        if sub and sub.get('expires_at'):
            expires_at = datetime.fromisoformat(sub['expires_at']) + timedelta(days=duration_days)
            supabase.table('subscriptions')\
                .update({'expires_at': expires_at.isoformat()})\
                .eq('user_id', user_id)\
                .eq('tariff_key', tariff_key)\
                .execute()
        else:
            add_subscription(user_id, tariff_key, duration_days)
        return True
    except Exception as e:
        logging.error(f"Ошибка продления подписки: {e}")
        return False

def expire_subscription(user_id: int, tariff_key: str):
    try:
        supabase.table('subscriptions')\
            .update({'status': 'expired'})\
            .eq('user_id', user_id)\
            .eq('tariff_key', tariff_key)\
            .execute()
        return True
    except Exception as e:
        logging.error(f"Ошибка отметки подписки: {e}")
        return False

def get_tariff_channel(tariff_key: str):
    try:
        response = supabase.table('tariff_channels')\
            .select('*')\
            .eq('tariff_key', tariff_key)\
            .execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logging.error(f"Ошибка получения канала: {e}")
        return None

def set_tariff_channel(tariff_key: str, channel_id: str, invite_link: str):
    try:
        supabase.table('tariff_channels').upsert({
            'tariff_key': tariff_key,
            'channel_id': channel_id,
            'invite_link': invite_link
        }).execute()
        return True
    except Exception as e:
        logging.error(f"Ошибка сохранения канала: {e}")
        return False

def create_subscription_key(tariff_key: str, duration_days: int = None, created_by: int = None) -> str:
    try:
        key = ''.join(random.choices(string.ascii_lowercase + string.digits, k=32))
        supabase.table('subscription_keys').insert({
            'key': key,
            'tariff_key': tariff_key,
            'duration_days': duration_days,
            'created_by': created_by
        }).execute()
        return key
    except Exception as e:
        logging.error(f"Ошибка создания ключа: {e}")
        return None

def get_subscription_key(key: str):
    try:
        response = supabase.table('subscription_keys')\
            .select('*')\
            .eq('key', key)\
            .execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logging.error(f"Ошибка получения ключа: {e}")
        return None

def delete_subscription_key(key: str):
    try:
        supabase.table('subscription_keys')\
            .delete()\
            .eq('key', key)\
            .execute()
        return True
    except Exception as e:
        logging.error(f"Ошибка удаления ключа: {e}")
        return False

def create_promo_code(code: str, discount_percent: int, expires_minutes: int = None, created_by: int = None):
    try:
        expires_at = None
        if expires_minutes is not None:
            expires_at = (datetime.now() + timedelta(minutes=expires_minutes)).isoformat()
        
        supabase.table('promo_codes').upsert({
            'code': code.upper(),
            'discount_percent': discount_percent,
            'expires_at': expires_at,
            'created_by': created_by
        }).execute()
        return True
    except Exception as e:
        logging.error(f"Ошибка создания промокода: {e}")
        return False

def get_promo_code(code: str):
    try:
        response = supabase.table('promo_codes')\
            .select('*')\
            .eq('code', code.upper())\
            .execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logging.error(f"Ошибка получения промокода: {e}")
        return None

def delete_promo_code(code: str):
    try:
        supabase.table('promo_codes')\
            .delete()\
            .eq('code', code.upper())\
            .execute()
        return True
    except Exception as e:
        logging.error(f"Ошибка удаления промокода: {e}")
        return False

def get_all_promo_codes():
    try:
        response = supabase.table('promo_codes')\
            .select('*')\
            .order('created_at', desc=True)\
            .execute()
        return response.data
    except Exception as e:
        logging.error(f"Ошибка получения промокодов: {e}")
        return []

def get_expired_subscriptions():
    try:
        now = datetime.now().isoformat()
        response = supabase.table('subscriptions')\
            .select('*')\
            .eq('status', 'active')\
            .lt('expires_at', now)\
            .execute()
        return response.data
    except Exception as e:
        logging.error(f"Ошибка получения истекших подписок: {e}")
        return []

def get_expiring_soon_subscriptions(days=3):
    try:
        now = datetime.now().isoformat()
        future = (datetime.now() + timedelta(days=days)).isoformat()
        response = supabase.table('subscriptions')\
            .select('*')\
            .eq('status', 'active')\
            .gte('expires_at', now)\
            .lte('expires_at', future)\
            .execute()
        return response.data
    except Exception as e:
        logging.error(f"Ошибка получения подписок: {e}")
        return []

def get_all_active_subscriptions():
    try:
        response = supabase.table('subscriptions')\
            .select('*')\
            .eq('status', 'active')\
            .execute()
        return response.data
    except Exception as e:
        logging.error(f"Ошибка получения подписок: {e}")
        return []

def get_subscription_stats():
    try:
        response = supabase.table('subscriptions')\
            .select('*', count='exact')\
            .eq('status', 'active')\
            .execute()
        total = response.count or 0
        
        tomorrow = (datetime.now() + timedelta(days=1)).isoformat()
        response2 = supabase.table('subscriptions')\
            .select('*', count='exact')\
            .eq('status', 'active')\
            .lte('expires_at', tomorrow)\
            .execute()
        expiring_tomorrow = response2.count or 0
        
        return {"total": total, "expiring_tomorrow": expiring_tomorrow}
    except Exception as e:
        logging.error(f"Ошибка статистики: {e}")
        return {"total": 0, "expiring_tomorrow": 0}

def get_all_channels():
    try:
        response = supabase.table('tariff_channels')\
            .select('*')\
            .execute()
        return response.data
    except Exception as e:
        logging.error(f"Ошибка получения каналов: {e}")
        return []

def get_all_subscription_keys():
    try:
        response = supabase.table('subscription_keys')\
            .select('*')\
            .order('created_at', desc=True)\
            .execute()
        return response.data
    except Exception as e:
        logging.error(f"Ошибка получения ключей: {e}")
        return []

# ==================================================
# SQLite (для совместимости)
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
    logging.info("✅ SQLite база инициализирована")

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
USD_RATE = 80
GRAM_RATE = 1.34
BTC_RATE = 65000

# ==================================================
# ID КАНАЛОВ
# ==================================================
CHANNEL_IDS = {
    "test": "-1003875225035",
}

# ==================================================
# ТЕКСТЫ
# ==================================================
LANG = {
    "ru": {
        "channel_unavailable": "❌ <b>Канал временно не доступен либо забанен.</b>\n\nДля уточнения сроков восстановления, напишите админу @kasgd\n\n❕ Важно: когда админ починит доступ, у вас он автоматически появится.",
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
        "promo_expired": "❌ Промокод истек. Попробуйте другой.",
        "choose_pay": "📋 <b>{name}</b>\nСрок доступа: {duration}\n💰 Цена: {price_text}\n\n🔒 Будет получен доступ к:\n• {project} (внешняя ссылка)\n\nВыберите способ оплаты",
        "pay_card": "Способ оплаты: Перевод на карту\n\n💰 К оплате: {final} RUB\n🆔 Ваш ID: {user_id}\n\n📌 <b>Реквизиты для оплаты:</b>\n\n💳 2200190284092510\n\n🏧 Банк: Уралсиб\nПолучатель: Кирилл\n\n❗️ Проверка ботом может занимать какое-то время (ручная проверка)\n❕ Если вы оплатили, нажмите обязательно кнопку «Я оплатил»\n❕ Если вы ждете больше 12 часов, напишите администратору",
        "pay_stars": "📋 <b>{name}</b>\nСрок доступа: {duration}\n{price_line}💳 Способ оплаты: ЗА ЗВЕЗДЫ ⭐\n\n💰 Итоговая стоимость: {final} STARS\n\nℹ️ <b>Информация по оплате</b>\nПодарить звезды или подарки на этот аккаунт - <a href=\"{support}\">@kasgd</a>\n\nкурс:\n1 ⭐ = 1 рубль",
        "pay_crypto_choose": "🪙 <b>Выберите монету:</b>",
        "pay_crypto_invoice": "✅ <b>Счёт на оплату сформирован.</b>\n\nДоступы к закрытым сообществам будут открыты, как только вы оплатите его.",
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
        "btn_join": "🔗 ВСТУПИТЬ",
        "btn_buy_other": "💳 КУПИТЬ ДРУГОЙ ДОСТУП",
        "payment_success": "✅ <b>Оплата прошла!</b>\n\n🔗 <b>Ваша ссылка доступа (действует 30 секунд):</b>\n{link}\n\n⚠️ <b>Внимание!</b> Ссылка действительна только 30 секунд!\n\nСпасибо за покупку! ❤️\n\n📞 Поддержка: @kasgd",
        "payment_success_test": "✅ <b>Доступ открыт!</b>\n\n🔗 <b>Ваша ссылка доступа (действует 30 секунд):</b>\n{link}\n\n⚠️ <b>Внимание!</b> Ссылка действительна только 30 секунд!\n\nСпасибо за использование бота! ❤️\n\n📞 Поддержка: @kasgd",
        "subs_list_item": "• {name} (оплачен ✅)",
        "main_menu_text": "После выбора и оплаты тарифа бот автоматически тебе выдаст доступ на вход в группу. На случай потери ссылки на нашу випку, ты сможешь всегда её запросить повторно у бота, это бесплатно.\n\nНажми на тариф чтобы прочесть описание.\n\nКаждый канал отличается",
        "i_paid_confirm": "💁🏻‍♂️ Оплатили?\n\n👌🏻 Тогда отправьте сюда картинкой (не документом!) квитанцию платежа: скриншот или фото. Иначе бот не узнает что вы оплатили\n\n📌 На квитанции должны быть четко видны: дата, время и сумма платежа. Проверка может занимать до дня.\n🔒 Никто ваши чеки не увидит, Telegram не хранит их.\n\n⚠️ За спам вы можете быть заблокированы!",
        "payment_receipt_received": "✅ Ваш чек получен! Администратор проверит его в ближайшее время.",
        "new_payment_request": "🆕 <b>Новая заявка на оплату!</b>\n\n👤 Пользователь: {user_link}\n🆔 ID: <code>{user_id}</code>\n📋 Тариф: {tariff_name}\n💰 Сумма: {amount} RUB\n📝 Сообщение: {message_text}\n\n{media_info}",
        "admin_panel": "⚙️ <b>Админ-панель</b>\n\n👥 Всего пользователей: {user_count}\n📋 Активных подписок: {subscriptions_count}\n⏳ Истекают завтра: {expiring_tomorrow}\n\nВыберите действие:",
        "crypto_error": "❌ Ошибка создания счета. Проверьте API ключ или попробуйте позже.",
        "crypto_direct": "🪙 <b>Прямой перевод</b>\n\nДля получения реквизитов либо по другому вопросу - @kasgd",
        "subscription_activated": "✅ <b>Название тарифа на срок</b> активирован! Доступ уже появился в разделе \"⌛ Мои подписки\" в меню кнопок.",
        "subscription_extended": "✅ <b>Подписка продлена!</b>\n\n📋 Тариф: {tariff_name}\n📅 Новый срок до: {expires_at}\n\nДоступ уже появился в разделе \"⌛ Мои подписки\".",
        "subscription_expired": "⚠️ <b>Ваша подписка на \"{tariff_name}\" истекла!</b>\n\nДоступ к сообществу закрыт. Для продления доступа оплатите тариф заново.",
        "subscription_expiring_soon": "⏰ <b>Напоминание!</b>\n\nВаша подписка на \"{tariff_name}\" истекает через {days} дня(ей).\nДля продления доступа оплатите тариф заново.",
        "access_info": "✅ <b>Вход открыт.</b>\n\nНажмите на кнопку ВСТУПИТЬ, затем Подать заявку и снова ВСТУПИТЬ:",
        "no_channel_configured": "❌ Для этого тарифа еще не настроена ссылка на канал. Обратитесь к администратору.",
        "key_activated": "✅ <b>Ключ активирован!</b>\n\n📋 Тариф: {tariff_name}\n📅 Действует до: {expires_at}\n\nДоступ уже появился в разделе \"⌛ Мои подписки\".",
        "key_not_found": "❌ Такого ключа не существует или он истек.",
        "key_activated_admin": "🔑 <b>Активирован ключ!</b>\n\n👤 Пользователь: {user_link}\n🆔 ID: <code>{user_id}</code>\n📋 Тариф: {tariff_name}\n📅 Действует до: {expires_at}"
    },
    "en": {
        "channel_unavailable": "❌ <b>The channel is temporarily unavailable or banned.</b>\n\nTo clarify the recovery time, contact admin @kasgd\n\n❕ Important: when the admin fixes access, it will appear automatically.",
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
        "promo_expired": "❌ Promo code expired. Try another one.",
        "choose_pay": "📋 <b>{name}</b>\nAccess duration: {duration}\n💰 Price: {price_text}\n\n🔒 You will get access to:\n• {project} (external link)\n\nChoose payment method",
        "pay_card": "Payment method: Bank card\n\n💰 Amount: {final} RUB\n🆔 Your ID: {user_id}\n\n📌 <b>Payment details:</b>\n\n💳 2200190284092510\n\n🏧 Bank: Uralsib\nRecipient: Kirill\n\n❗️ Verification may take some time (manual check)\n❕ After payment, press <b>«I Paid»</b> button\n❕ If waiting more than 12 hours, contact admin",
        "pay_stars": "📋 <b>{name}</b>\nAccess duration: {duration}\n{price_line}💳 Payment method: FOR STARS ⭐\n\n💰 Total cost: {final} STARS\n\nℹ️ <b>Payment info</b>\nSend stars or gifts to this account - <a href=\"{support}\">@kasgd</a>\n\nRate:\n1 ⭐ = 1 ruble",
        "pay_crypto_choose": "🪙 <b>Choose coin:</b>",
        "pay_crypto_invoice": "✅ <b>Invoice created.</b>\n\nAccess to closed communities will be opened as soon as you pay it.",
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
        "btn_join": "🔗 JOIN",
        "btn_buy_other": "💳 BUY OTHER ACCESS",
        "payment_success": "✅ <b>Payment successful!</b>\n\n🔗 <b>Your access link (valid 30 seconds):</b>\n{link}\n\n⚠️ <b>Warning!</b> The link is valid only 30 seconds!\n\nThank you for your purchase! ❤️\n\n📞 Support: @kasgd",
        "payment_success_test": "✅ <b>Access granted!</b>\n\n🔗 <b>Your access link (valid 30 seconds):</b>\n{link}\n\n⚠️ <b>Warning!</b> The link is valid only 30 seconds!\n\nThank you for using the bot! ❤️\n\n📞 Support: @kasgd",
        "subs_list_item": "• {name} (paid ✅)",
        "main_menu_text": "After selecting and paying for the tariff, the bot will automatically give you access to the group. If you lose the link to our VIP, you can always request it again from the bot, it's free.\n\nClick on the tariff to read the description.\n\nEach channel is different",
        "i_paid_confirm": "💁🏻‍♂️ Paid?\n\n👌🏻 Then send a payment receipt as an image (not document!): screenshot or photo. Otherwise the bot won't know you paid.\n\n📌 The receipt must clearly show: date, time and payment amount. Verification may take up to a day.\n🔒 No one will see your receipts, Telegram doesn't store them.\n\n⚠️ You may be blocked for spam!",
        "payment_receipt_received": "✅ Your receipt has been received! Administrator will check it shortly.",
        "new_payment_request": "🆕 <b>New payment request!</b>\n\n👤 User: {user_link}\n🆔 ID: <code>{user_id}</code>\n📋 Tariff: {tariff_name}\n💰 Amount: {amount} RUB\n📝 Message: {message_text}\n\n{media_info}",
        "admin_panel": "⚙️ <b>Admin panel</b>\n\n👥 Total users: {user_count}\n📋 Active subscriptions: {subscriptions_count}\n⏳ Expiring tomorrow: {expiring_tomorrow}\n\nSelect action:",
        "crypto_error": "❌ Error creating invoice. Check API key or try again later.",
        "crypto_direct": "🪙 <b>Direct transfer</b>\n\nFor details or other questions - @kasgd",
        "subscription_activated": "✅ <b>Tariff name for period</b> activated! Access already appeared in \"⌛ My subscriptions\".",
        "subscription_extended": "✅ <b>Subscription extended!</b>\n\n📋 Tariff: {tariff_name}\n📅 New expiry: {expires_at}\n\nAccess already appeared in \"⌛ My subscriptions\".",
        "subscription_expired": "⚠️ <b>Your subscription to \"{tariff_name}\" has expired!</b>\n\nAccess to the community is closed. Pay the tariff again to renew your access.",
        "subscription_expiring_soon": "⏰ <b>Reminder!</b>\n\nYour subscription to \"{tariff_name}\" expires in {days} day(s).\nPay the tariff again to renew your access.",
        "access_info": "✅ <b>Access open.</b>\n\nClick the JOIN button, then Submit request and JOIN again:",
        "no_channel_configured": "❌ No channel link configured for this tariff yet. Contact administrator.",
        "key_activated": "✅ <b>Key activated!</b>\n\n📋 Tariff: {tariff_name}\n📅 Valid until: {expires_at}\n\nAccess already appeared in \"⌛ My subscriptions\".",
        "key_not_found": "❌ This key does not exist or has expired.",
        "key_activated_admin": "🔑 <b>Key activated!</b>\n\n👤 User: {user_link}\n🆔 ID: <code>{user_id}</code>\n📋 Tariff: {tariff_name}\n📅 Valid until: {expires_at}"
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
        "duration_days": 30,
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
        "duration_days": 30,
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
        "duration_days": 30,
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
        "duration_days": 30,
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
        "duration_days": 60,
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
        "duration_days": 30,
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
        "duration_days": None,
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
        "duration_days": 1,
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
        "duration_days": 21,
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
        "duration_days": 30,
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
        "duration_days": 30,
        "category": "main",
        "desc_ru": "Описание будет позже"
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

class AdminStates(StatesGroup):
    waiting_for_tariff_key = State()
    waiting_for_channel_id = State()
    waiting_for_invite_link = State()
    waiting_for_key_tariff = State()
    waiting_for_key_duration = State()
    waiting_for_promo_code = State()
    waiting_for_promo_discount = State()
    waiting_for_promo_minutes = State()
    waiting_for_custom_days = State()  # НОВОЕ СОСТОЯНИЕ

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
    
    tariff = TARIFFS.get(tariff_key)
    if tariff and tariff.get('duration_days') is not None:
        extend_subscription(user_id, tariff_key, tariff['duration_days'])
    else:
        add_subscription(user_id, tariff_key, None)
    
    if tariff_key == "test":
        text = LANG[lang]["payment_success_test"].format(link=link)
    else:
        text = LANG[lang]["payment_success"].format(link=link)
    
    await message.answer(text, disable_web_page_preview=False)

async def create_crypto_invoice_usd(amount_usd: float, user_id: int, tariff_key: str, asset: str = "USDT") -> dict:
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
        "amount": str(amount_usd),
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
    return round(value * 2) / 2

def get_tariff_name(tariff_key: str, lang: str = "ru"):
    tariff = TARIFFS.get(tariff_key)
    if tariff:
        return tariff['name_ru'] if lang == "ru" else tariff['name_en']
    return tariff_key

def format_date(date):
    if date is None:
        return "Бессрочно"
    if isinstance(date, str):
        date = datetime.fromisoformat(date)
    return date.strftime("%d.%m.%Y")

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
        [KeyboardButton(text=LANG[lang]["btn_prices"]), 
         KeyboardButton(text=LANG[lang]["btn_subs"])]
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
    
    sub = get_subscription_by_tariff(user_id, tariff_key)
    
    if not sub:
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
        [InlineKeyboardButton(text=btn_card, callback_data=f"pay_card_{tariff_key}_{discount_percent}")],
        [InlineKeyboardButton(text=btn_stars, callback_data=f"pay_stars_{tariff_key}_{discount_percent}")],
        [InlineKeyboardButton(text=btn_crypto, callback_data=f"pay_crypto_{tariff_key}_{discount_percent}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
    ])

def get_crypto_currency_keyboard(tariff_key, discount_percent=0, lang="ru"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=LANG[lang]["btn_crypto_usdt"], callback_data=f"crypto_usdt_{tariff_key}_{discount_percent}"),
         InlineKeyboardButton(text=LANG[lang]["btn_crypto_ton"], callback_data=f"crypto_ton_{tariff_key}_{discount_percent}"),
         InlineKeyboardButton(text=LANG[lang]["btn_crypto_btc"], callback_data=f"crypto_btc_{tariff_key}_{discount_percent}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_crypto_direct"], callback_data=f"crypto_direct_{tariff_key}_{discount_percent}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
    ])

def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Управление тарифами", callback_data="admin_channels")],
        [InlineKeyboardButton(text="🔑 Создать ключ", callback_data="admin_create_key")],
        [InlineKeyboardButton(text="🏷️ Управление промокодами", callback_data="admin_promocodes")],
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

def get_subscription_keyboard(subscriptions, lang="ru"):
    buttons = []
    for sub in subscriptions:
        tariff_key = sub['tariff_key']
        name = get_tariff_name(tariff_key, lang)
        buttons.append([InlineKeyboardButton(text=name, callback_data=f"access_{tariff_key}")])
    buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_access_keyboard(tariff_key, lang="ru"):
    tariff_channel = get_tariff_channel(tariff_key)
    invite_link = tariff_channel['invite_link'] if tariff_channel else None
    
    buttons = []
    if invite_link:
        buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_join"], url=invite_link)])
    buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_buy_other"], callback_data="back_to_prices")])
    buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_subs")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================================================
# ОБРАБОТЧИК ЗАЯВОК В КАНАЛ
# ==================================================

@dp.chat_join_request()
async def handle_join_request(update: ChatJoinRequest):
    user_id = update.from_user.id
    chat_id = update.chat.id
    
    logging.info(f"📥 Заявка от {user_id} в канал {chat_id}")
    
    try:
        response = supabase.table('tariff_channels')\
            .select('tariff_key')\
            .eq('channel_id', str(chat_id))\
            .execute()
        
        if response.data:
            tariff_key = response.data[0]['tariff_key']
            sub = get_subscription_by_tariff(user_id, tariff_key)
            if sub:
                await update.approve()
                logging.info(f"✅ Заявка от {user_id} одобрена для {tariff_key}")
                return
            else:
                logging.info(f"❌ Отказ для {user_id} - нет подписки на {tariff_key}")
        else:
            logging.info(f"⚠️ Канал {chat_id} не найден в настройках")
    except Exception as e:
        logging.error(f"Ошибка обработки заявки: {e}")

# ==================================================
# ХЭНДЛЕРЫ
# ==================================================

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "Пользователь"
    username = message.from_user.username
    
    logging.info(f"🚀 Получена команда /start от {user_id} ({first_name})")
    
    add_user(user_id, first_name, username)
    await state.update_data(discount=0)
    
    if message.text and " " in message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            key_param = parts[1]
            key_data = get_subscription_key(key_param)
            if key_data:
                await process_key_activation(message, key_param, state)
                return
            else:
                await message.answer("❌ Такого ключа не существует или он истек.")
    
    lang = await get_lang(state)
    
    welcome_text = f"""👋 Привет, {first_name}!
Ты попал в наш бот✅

Нажимая на каждый тариф ты видишь краткое описание.

Если бот не доступен пиши мне

Тех.поддержка: @kasgd"""
    
    # ПЕРВОЕ сообщение - приветствие + кнопки внизу
    await message.answer(
        welcome_text,
        reply_markup=get_main_keyboard(lang),
        disable_web_page_preview=True
    )
    
    # ВТОРОЕ сообщение - меню + тарифы (кнопки внизу уже есть)
    menu_text = LANG[lang]["main_menu_text"]
    await message.answer(
        menu_text,
        reply_markup=get_tariff_keyboard(lang)
    )

async def process_key_activation(message: Message, key_param: str, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    lang = await get_lang(state)
    
    logging.info(f"🔑 Активация ключа: {key_param} от {user_id}")
    
    key_data = get_subscription_key(key_param)
    if not key_data:
        logging.warning(f"❌ Ключ не найден: {key_param}")
        await message.answer("❌ Такого ключа не существует или он истек.")
        return
    
    logging.info(f"✅ Ключ найден: {key_data}")
    
    tariff_key = key_data['tariff_key']
    duration_days = key_data['duration_days']
    tariff = TARIFFS.get(tariff_key)
    
    if not tariff:
        await message.answer("❌ Ошибка: тариф не найден.")
        return
    
    tariff_name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    
    # Проверяем есть ли уже подписка на этот тариф
    existing_sub = get_subscription_by_tariff(user_id, tariff_key)
    
    if existing_sub:
        # Продлеваем существующую подписку
        if duration_days is not None:
            extend_subscription(user_id, tariff_key, duration_days)
            expires_at = datetime.now() + timedelta(days=duration_days)
        else:
            expires_at = None
        action = "продлена"
    else:
        # Создаем новую подписку
        if duration_days is not None:
            expires_at = datetime.now() + timedelta(days=duration_days)
        else:
            expires_at = None
        add_subscription(user_id, tariff_key, duration_days)
        action = "активирован"
    
    # Удаляем использованный ключ
    delete_subscription_key(key_param)
    
    # Отправляем сообщение пользователю
    if duration_days is not None:
        days_text = f"{duration_days} дней"
        if duration_days == 1:
            days_text = "1 день"
        elif duration_days in [2, 3, 4]:
            days_text = f"{duration_days} дня"
        expires_text = format_date(expires_at)
    else:
        days_text = "бессрочно"
        expires_text = "Бессрочно"
    
    text = f"✅ <b>Ваш ключ активирован!</b>\n\n"
    text += f"📋 Вы получили <b>«{tariff_name}»</b>\n"
    text += f"📅 Срок: <b>{days_text}</b>\n\n"
    text += f"Доступ уже появился в разделе <b>\"Мои подписки\"</b>."
    
    await message.answer(text)
    
    # Уведомляем админов
    user_link = f"<a href='tg://user?id={user_id}'>{username}</a>"
    admin_text = f"🔑 <b>Активирован ключ!</b>\n\n"
    admin_text += f"👤 Пользователь: {user_link}\n"
    admin_text += f"🆔 ID: <code>{user_id}</code>\n"
    admin_text += f"📋 Тариф: {tariff_name}\n"
    admin_text += f"📅 Действует до: {expires_text}"
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text)
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления админу: {e}")

@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    user_count = get_user_count()
    stats = get_subscription_stats()
    
    text = LANG["ru"]["admin_panel"].format(
        user_count=user_count,
        subscriptions_count=stats['total'],
        expiring_tomorrow=stats['expiring_tomorrow']
    )
    
    await message.answer(text, reply_markup=get_admin_keyboard())

# ==================================================
# АДМИН: УПРАВЛЕНИЕ ТАРИФАМИ
# ==================================================

@dp.callback_query(F.data == "admin_channels")
async def admin_channels(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    text = "📋 <b>Управление ссылками каналов</b>\n\n"
    text += "Для каждого тарифа можно настроить ссылку на канал с заявками.\n\n"
    
    for key, tariff in TARIFFS.items():
        channel = get_tariff_channel(key)
        if channel and channel.get('invite_link'):
            status = "✅ настроен"
            link_preview = channel['invite_link'][:30] + "..." if len(channel['invite_link']) > 30 else channel['invite_link']
            text += f"• {tariff['name_ru']}: {status}\n  🔗 {link_preview}\n\n"
        else:
            text += f"• {tariff['name_ru']}: ❌ не настроен\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Настроить тариф", callback_data="admin_edit_channel")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_admin")]
        ])
    )

@dp.callback_query(F.data == "admin_edit_channel")
async def admin_edit_channel(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    buttons = []
    for key, tariff in TARIFFS.items():
        channel = get_tariff_channel(key)
        status = "✅" if channel and channel.get('invite_link') else "❌"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {tariff['name_ru']}", 
            callback_data=f"admin_channel_{key}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_channels")])
    
    await callback.message.edit_text(
        "📋 <b>Выберите тариф для настройки:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("admin_channel_"))
async def admin_channel_set(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    tariff_key = callback.data.replace("admin_channel_", "")
    tariff = TARIFFS[tariff_key]
    channel = get_tariff_channel(tariff_key)
    
    await state.update_data(admin_tariff_key=tariff_key)
    
    text = f"📋 <b>Настройка: {tariff['name_ru']}</b>\n\n"
    
    if channel and channel.get('channel_id'):
        text += f"🆔 ID канала: <code>{channel['channel_id']}</code>\n"
    else:
        text += "🆔 ID канала: ❌ не задан\n"
    
    if channel and channel.get('invite_link'):
        text += f"🔗 Ссылка-приглашение: {channel['invite_link']}\n"
    else:
        text += "🔗 Ссылка-приглашение: ❌ не задана\n"
    
    text += "\nВведите ID канала (например -1001234567890):"
    
    await callback.message.edit_text(text)
    await state.set_state(AdminStates.waiting_for_channel_id)

@dp.message(AdminStates.waiting_for_channel_id)
async def admin_set_channel_id(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    data = await state.get_data()
    tariff_key = data.get("admin_tariff_key")
    channel_id = message.text.strip()
    
    await state.update_data(admin_channel_id=channel_id)
    
    await message.answer(
        "📋 Теперь введите ссылку-приглашение на канал (с заявками):\n\n"
        "Пример: https://t.me/joinchat/XXXXX"
    )
    await state.set_state(AdminStates.waiting_for_invite_link)

@dp.message(AdminStates.waiting_for_invite_link)
async def admin_set_invite_link(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    data = await state.get_data()
    tariff_key = data.get("admin_tariff_key")
    channel_id = data.get("admin_channel_id")
    invite_link = message.text.strip()
    
    set_tariff_channel(tariff_key, channel_id, invite_link)
    
    tariff = TARIFFS[tariff_key]
    
    await message.answer(
        f"✅ Настройки для тарифа <b>{tariff['name_ru']}</b> сохранены!\n\n"
        f"🆔 ID канала: <code>{channel_id}</code>\n"
        f"🔗 Ссылка: {invite_link}\n\n"
        f"Пользователи с активной подпиской теперь увидят эту ссылку в разделе 'Мои подписки'."
    )
    await state.clear()

# ==================================================
# АДМИН: СОЗДАНИЕ КЛЮЧЕЙ
# ==================================================

@dp.callback_query(F.data == "admin_create_key")
async def admin_create_key(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    buttons = []
    for key, tariff in TARIFFS.items():
        buttons.append([InlineKeyboardButton(
            text=f"{tariff['name_ru']} ({tariff['duration_ru']})", 
            callback_data=f"admin_key_tariff_{key}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_admin")])
    
    await callback.message.edit_text(
        "🔑 <b>Создание одноразового ключа</b>\n\n"
        "Выберите тариф для ключа:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("admin_key_tariff_"))
async def admin_key_tariff(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    tariff_key = callback.data.replace("admin_key_tariff_", "")
    tariff = TARIFFS[tariff_key]
    
    await state.update_data(admin_key_tariff=tariff_key)
    
    duration_options = [
        ("30 дней", 30),
        ("60 дней", 60),
        ("90 дней", 90),
        ("Бессрочно", None),
        ("✏️ Свой срок", "custom")  # <-- НОВАЯ КНОПКА
    ]
    
    buttons = []
    for label, days in duration_options:
        callback_data = f"admin_key_days_{days if days is not None else '0'}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=callback_data)])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_create_key")])
    
    await callback.message.edit_text(
        f"📋 <b>Тариф: {tariff['name_ru']}</b>\n\n"
        "Выберите срок действия подписки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("admin_key_days_"))
async def admin_key_days(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    days_str = callback.data.replace("admin_key_days_", "")
    
    # Если выбрано "Свой срок"
    if days_str == "custom":
        await callback.message.edit_text(
            "📝 <b>Введите срок в днях</b>\n\n"
            "Напишите число (например: 45, 100, 365):"
        )
        await state.set_state(AdminStates.waiting_for_custom_days)  # <-- ПРАВИЛЬНО!
        return
    
    duration_days = None if days_str == "0" else int(days_str)
    
    data = await state.get_data()
    tariff_key = data.get("admin_key_tariff")
    
    if not tariff_key:
        await callback.message.edit_text("❌ Ошибка: тариф не выбран.")
        return
    
    # Создаем ключ
    key = create_subscription_key(tariff_key, duration_days, callback.from_user.id)
    
    if key:
        tariff = TARIFFS[tariff_key]
        bot_info = await bot.get_me()
        link = f"https://t.me/{bot_info.username}?start={key}"
        
        text = f"✅ <b>Ключ создан!</b>\n\n"
        text += f"📋 Тариф: {tariff['name_ru']}\n"
        text += f"📅 Срок: {'Бессрочно' if duration_days is None else f'{duration_days} дней'}\n"
        text += f"🔑 Ключ: <code>{key}</code>\n"
        text += f"🔗 Ссылка: {link}\n\n"
        text += "⚠️ Ключ одноразовый. После активации будет удален."
        
        await callback.message.edit_text(text)
    else:
        await callback.message.edit_text("❌ Ошибка создания ключа. Проверьте логи.")
    
    await state.clear()

@dp.message(AdminStates.waiting_for_custom_days)
async def process_custom_days(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    try:
        days = int(message.text.strip())
        if days < 1:
            await message.answer("❌ Введите положительное число (минимум 1 день).")
            return
    except:
        await message.answer("❌ Введите число. Попробуйте еще раз.")
        return
    
    data = await state.get_data()
    tariff_key = data.get("admin_key_tariff")
    
    if not tariff_key:
        await message.answer("❌ Ошибка: тариф не выбран.")
        await state.clear()
        return
    
    key = create_subscription_key(tariff_key, days, message.from_user.id)
    
    if key:
        tariff = TARIFFS[tariff_key]
        bot_info = await bot.get_me()
        link = f"https://t.me/{bot_info.username}?start={key}"
        
        text = f"✅ <b>Ключ создан!</b>\n\n"
        text += f"📋 Тариф: {tariff['name_ru']}\n"
        text += f"📅 Срок: {days} дней\n"
        text += f"🔑 Ключ: <code>{key}</code>\n"
        text += f"🔗 Ссылка: {link}\n\n"
        text += "⚠️ Ключ одноразовый. После активации будет удален."
        
        await message.answer(text)
    else:
        await message.answer("❌ Ошибка создания ключа. Проверьте логи.")
    
    await state.clear()

# ==================================================
# АДМИН: УПРАВЛЕНИЕ ПРОМОКОДАМИ
# ==================================================

@dp.callback_query(F.data == "admin_promocodes")
async def admin_promocodes(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    promocodes = get_all_promo_codes()
    
    text = "🏷️ <b>Управление промокодами</b>\n\n"
    
    if promocodes:
        for pc in promocodes:
            code = pc['code']
            discount = pc['discount_percent']
            expires = pc.get('expires_at')
            status = "✅ активен" if (expires is None or datetime.fromisoformat(expires) > datetime.now()) else "❌ истек"
            text += f"• <b>{code}</b> - {discount}% ({status})\n"
            if expires:
                text += f"  До: {datetime.fromisoformat(expires).strftime('%d.%m.%Y %H:%M')}\n"
            text += "\n"
    else:
        text += "❌ Нет созданных промокодов\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin_create_promo")],
            [InlineKeyboardButton(text="🗑️ Удалить промокод", callback_data="admin_delete_promo")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_admin")]
        ])
    )

@dp.callback_query(F.data == "admin_create_promo")
async def admin_create_promo(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    await callback.message.edit_text(
        "🏷️ <b>Создание промокода</b>\n\n"
        "Введите название промокода (только буквы и цифры, без пробелов):\n\n"
        "Пример: BLACKFRIDAY"
    )
    await state.set_state(AdminStates.waiting_for_promo_code)

@dp.message(AdminStates.waiting_for_promo_code)
async def admin_promo_code(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    code = message.text.strip().upper()
    if not code or " " in code:
        await message.answer("❌ Название не должно содержать пробелов. Попробуйте еще раз.")
        return
    
    await state.update_data(admin_promo_code=code)
    
    await message.answer(
        "🏷️ <b>Создание промокода</b>\n\n"
        "Введите процент скидки (число от 1 до 100):\n\n"
        "Пример: 25"
    )
    await state.set_state(AdminStates.waiting_for_promo_discount)

@dp.message(AdminStates.waiting_for_promo_discount)
async def admin_promo_discount(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    try:
        discount = int(message.text.strip())
        if discount < 1 or discount > 100:
            await message.answer("❌ Скидка должна быть от 1 до 100. Попробуйте еще раз.")
            return
    except:
        await message.answer("❌ Введите число. Попробуйте еще раз.")
        return
    
    await state.update_data(admin_promo_discount=discount)
    
    await message.answer(
        "🏷️ <b>Создание промокода</b>\n\n"
        "Введите срок действия в минутах:\n\n"
        "Пример: 1440 (1 день), 43200 (30 дней)\n"
        "Или 0 для бессрочного"
    )
    await state.set_state(AdminStates.waiting_for_promo_minutes)

@dp.message(AdminStates.waiting_for_promo_minutes)
async def admin_promo_minutes(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    try:
        minutes = int(message.text.strip())
        if minutes < 0:
            await message.answer("❌ Введите положительное число или 0.")
            return
        expires_minutes = None if minutes == 0 else minutes
    except:
        await message.answer("❌ Введите число. Попробуйте еще раз.")
        return
    
    data = await state.get_data()
    code = data.get("admin_promo_code")
    discount = data.get("admin_promo_discount")
    
    create_promo_code(code, discount, expires_minutes, message.from_user.id)
    
    expires_text = "Бессрочно" if expires_minutes is None else f"{expires_minutes} минут"
    
    await message.answer(
        f"✅ <b>Промокод создан!</b>\n\n"
        f"🏷️ Код: <b>{code}</b>\n"
        f"📉 Скидка: {discount}%\n"
        f"⏰ Срок: {expires_text}\n\n"
        f"Пользователи могут использовать его при оформлении заказа."
    )
    await state.clear()

@dp.callback_query(F.data == "admin_delete_promo")
async def admin_delete_promo(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    promocodes = get_all_promo_codes()
    
    if not promocodes:
        await callback.message.edit_text(
            "❌ Нет созданных промокодов для удаления.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_promocodes")]
            ])
        )
        return
    
    buttons = []
    for pc in promocodes:
        code = pc['code']
        buttons.append([InlineKeyboardButton(
            text=f"🗑️ {code} ({pc['discount_percent']}%)", 
            callback_data=f"admin_del_promo_{code}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_promocodes")])
    
    await callback.message.edit_text(
        "🏷️ <b>Удаление промокода</b>\n\n"
        "Выберите промокод для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("admin_del_promo_"))
async def admin_del_promo(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    code = callback.data.replace("admin_del_promo_", "")
    delete_promo_code(code)
    
    await callback.message.edit_text(
        f"✅ Промокод <b>{code}</b> удален.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_promocodes")]
        ])
    )

# ==================================================
# АДМИН: РАССЫЛКА
# ==================================================

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
    await callback.answer()

@dp.message(MailingStates.waiting_for_content)
async def process_mailing_content(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    await message.answer("⏳ Начинаю рассылку...")
    
    users = get_all_users()
    
    if not users:
        await message.answer("❌ Нет пользователей для рассылки!")
        await state.clear()
        return
    
    success = 0
    failed = 0
    
    for user_id in users:
        try:
            if message.text:
                await bot.send_message(user_id, message.text, parse_mode="HTML")
            elif message.photo:
                await bot.send_photo(user_id, message.photo[-1].file_id, caption=message.caption)
            elif message.video:
                await bot.send_video(user_id, message.video.file_id, caption=message.caption)
            elif message.animation:
                await bot.send_animation(user_id, message.animation.file_id, caption=message.caption)
            elif message.document:
                await bot.send_document(user_id, message.document.file_id, caption=message.caption)
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
        f"👥 Всего пользователей: {len(users)}"
    )
    await state.clear()

# ==================================================
# АДМИН: СТАТИСТИКА
# ==================================================

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    user_count = get_user_count()
    stats = get_subscription_stats()
    
    keys = get_all_subscription_keys()
    keys_count = len(keys)
    
    promos = get_all_promo_codes()
    promo_count = len(promos)
    
    text = f"""📊 <b>Статистика бота</b>

👥 Всего пользователей: {user_count}
📋 Активных подписок: {stats['total']}
⏳ Истекают завтра: {stats['expiring_tomorrow']}
🔑 Создано ключей: {keys_count}
🏷️ Активных промокодов: {promo_count}

📌 <b>Статус бота:</b>
✅ Supabase REST API подключена
✅ SQLite работает
✅ CryptoBot {'✅' if CRYPTOBOT_API_KEY else '❌'}"""

    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())

# ==================================================
# ОБРАБОТЧИКИ ОПЛАТ
# ==================================================

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    user_count = get_user_count()
    stats = get_subscription_stats()
    
    text = LANG["ru"]["admin_panel"].format(
        user_count=user_count,
        subscriptions_count=stats['total'],
        expiring_tomorrow=stats['expiring_tomorrow']
    )
    
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())

@dp.callback_query(F.data == "back_to_subs")
async def back_to_subs(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    # СБРАСЫВАЕМ СКИДКУ
    await state.update_data(discount=0)
    
    lang = await get_lang(state)
    user_id = callback.from_user.id
    
    subscriptions = get_active_subscriptions(user_id)
    
    if subscriptions:
        text = "📋 <b>Ваши активные подписки</b>\n\nВыберите доступ:"
        await callback.message.edit_text(text, reply_markup=get_subscription_keyboard(subscriptions, lang))
    else:
        await callback.message.edit_text(LANG[lang]["no_subs"])
        await callback.message.answer(LANG[lang]["main_menu_text"], reply_markup=get_tariff_keyboard(lang))

@dp.callback_query(F.data == "back_to_prices")
async def back_to_prices(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    # СБРАСЫВАЕМ СКИДКУ
    await state.update_data(discount=0)
    
    lang = await get_lang(state)
    await callback.message.edit_text(LANG[lang]["main_menu_text"], reply_markup=get_tariff_keyboard(lang))

@dp.message(F.text.in_([LANG["ru"]["btn_prices"], LANG["en"]["btn_prices"]]))
async def show_prices(message: Message, state: FSMContext):
    lang = await get_lang(state)
    await state.update_data(discount=0)
    
    await message.answer(
        LANG[lang]["prices_menu"],
        reply_markup=get_tariff_keyboard(lang)
    )

@dp.message(F.text.in_([LANG["ru"]["btn_subs"], LANG["en"]["btn_subs"]]))
async def show_subscriptions(message: Message, state: FSMContext):
    lang = await get_lang(state)
    await state.update_data(discount=0)
    user_id = message.from_user.id
    
    subscriptions = get_active_subscriptions(user_id)
    
    if subscriptions:
        text = "📋 <b>Ваши активные подписки</b>\n\nВыберите доступ:"
        await message.answer(
            text,
            reply_markup=get_subscription_keyboard(subscriptions, lang)
        )
    else:
        await message.answer(
            LANG[lang]["no_subs"],
            reply_markup=get_main_keyboard(lang)
        )

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
    
    is_paid = get_subscription_by_tariff(user_id, tariff_key) is not None
    
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

    promo = get_promo_code(promo_code)
    
    if not promo:
        if promo_code in PROMO_CODES:
            discount = PROMO_CODES[promo_code]
            await state.update_data(discount=discount, current_tariff=tariff_key)
            
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
            return
        else:
            await message.answer(LANG[lang]["promo_fail"])
            return
    
    expires_at = promo.get('expires_at')
    if expires_at and datetime.fromisoformat(expires_at) < datetime.now():
        await message.answer(LANG[lang]["promo_expired"])
        return
    
    discount = promo['discount_percent']
    await state.update_data(discount=discount, current_tariff=tariff_key)
    
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

    is_paid = get_subscription_by_tariff(user_id, tariff_key) is not None
    
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

@dp.callback_query(F.data.startswith("pay_for_friend_"))
async def pay_for_friend(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("pay_for_friend_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    await choose_payment_logic(callback, state, tariff_key)

@dp.callback_query(F.data.startswith("pay_card_"))
async def process_card_payment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    parts = callback.data.split("_")
    tariff_key = parts[2]
    discount = int(parts[3]) if len(parts) > 3 else 0
    
    lang = await get_lang(state)
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
            [InlineKeyboardButton(text=LANG[lang]["btn_i_paid"], callback_data=f"i_paid_{tariff_key}_{discount}")],
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
    
    parts = callback.data.split("_")
    tariff_key = parts[2]
    discount = int(parts[3]) if len(parts) > 3 else 0
    
    lang = await get_lang(state)
    await state.update_data(current_tariff=tariff_key, discount=discount)
    
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

    is_paid = get_subscription_by_tariff(user_id, tariff_key) is not None
    
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
    
    parts = callback.data.split("_")
    tariff_key = parts[2]
    discount = int(parts[3]) if len(parts) > 3 else 0
    
    tariff = TARIFFS[tariff_key]
    lang = await get_lang(state)
    
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
    
    parts = callback.data.split("_")
    tariff_key = parts[2]
    discount = int(parts[3]) if len(parts) > 3 else 0
    
    # Сохраняем скидку в состояние для следующих шагов
    await state.update_data(discount=discount, current_tariff=tariff_key)
    
    lang = await get_lang(state)
    
    text = LANG[lang]["pay_crypto_choose"]
    
    await callback.message.edit_text(
        text,
        reply_markup=get_crypto_currency_keyboard(tariff_key, discount, lang)
    )

@dp.callback_query(F.data.startswith("crypto_usdt_"))
async def crypto_usdt_payment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    parts = callback.data.split("_")
    tariff_key = parts[2]
    discount = int(parts[3]) if len(parts) > 3 else 0
    
    tariff = TARIFFS[tariff_key]
    lang = await get_lang(state)
    user_id = callback.from_user.id
    
    final_rub = int(tariff['price_rub'] * (1 - discount / 100))
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    
    final_usdt = round_to_half(final_rub / USDT_RATE)
    
    invoice_data = await create_crypto_invoice_usd(final_usdt, user_id, tariff_key, "USDT")
    
    if invoice_data:
        invoice_id = invoice_data["invoice_id"]
        pay_url = invoice_data["pay_url"]
        
        save_crypto_invoice(invoice_id, user_id, tariff_key, final_usdt, "USDT")
        
        text = LANG[lang]["pay_crypto_invoice"]
        
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
    
    parts = callback.data.split("_")
    tariff_key = parts[2]
    discount = int(parts[3]) if len(parts) > 3 else 0
    
    tariff = TARIFFS[tariff_key]
    lang = await get_lang(state)
    user_id = callback.from_user.id
    
    final_rub = int(tariff['price_rub'] * (1 - discount / 100))
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    
    final_usd = round_to_half(final_rub / USD_RATE)
    final_gram = round_to_half(final_usd / GRAM_RATE)
    
    invoice_data = await create_crypto_invoice_usd(final_gram, user_id, tariff_key, "TON")
    
    if invoice_data:
        invoice_id = invoice_data["invoice_id"]
        pay_url = invoice_data["pay_url"]
        
        save_crypto_invoice(invoice_id, user_id, tariff_key, final_gram, "TON")
        
        text = LANG[lang]["pay_crypto_invoice"]
        
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
    
    parts = callback.data.split("_")
    tariff_key = parts[2]
    discount = int(parts[3]) if len(parts) > 3 else 0
    
    tariff = TARIFFS[tariff_key]
    lang = await get_lang(state)
    user_id = callback.from_user.id
    
    final_rub = int(tariff['price_rub'] * (1 - discount / 100))
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    
    final_usd = round_to_half(final_rub / USD_RATE)
    final_btc = round(final_usd / BTC_RATE, 8)
    
    invoice_data = await create_crypto_invoice_usd(final_btc, user_id, tariff_key, "BTC")
    
    if invoice_data:
        invoice_id = invoice_data["invoice_id"]
        pay_url = invoice_data["pay_url"]
        
        save_crypto_invoice(invoice_id, user_id, tariff_key, final_btc, "BTC")
        
        text = LANG[lang]["pay_crypto_invoice"]
        
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
    
    parts = callback.data.split("_")
    tariff_key = parts[2]
    discount = int(parts[3]) if len(parts) > 3 else 0
    
    lang = await get_lang(state)
    
    text = LANG[lang]["crypto_direct"]
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨‍💼 Написать админу", url="https://t.me/kasgd")],
            [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
        ])
    )

@dp.callback_query(F.data == "show_paki")
async def show_paki(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    lang = await get_lang(state)
    
    text = "📋 <b>Паки</b>\n\nВыберите пак для подробностей:"
    await callback.message.edit_text(
        text,
        reply_markup=get_paki_keyboard(lang)
    )
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
    
    tariff = TARIFFS.get(tariff_key)
    if tariff and tariff.get('duration_days') is not None:
        extend_subscription(user_id, tariff_key, tariff['duration_days'])
    else:
        add_subscription(user_id, tariff_key, None)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE payment_requests SET status = "confirmed" WHERE id = ?', (request_id,))
    conn.commit()
    conn.close()
    
    # НОВЫЙ ТЕКСТ
    await bot.send_message(
        user_id, 
        "✅ <b>Оплата подтверждена!</b>\n\n"
        "Ваша подписка уже появилась в разделе \"Мои подписки\".\n\n"
        "Если канал заблокирован, либо доступ не выдается, пишите админу."
    )
    
    await callback.message.delete()
    await callback.message.answer(f"✅ Оплата по заявке #{request_id} подтверждена! Пользователь уведомлен.")
    await callback.answer()

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    user_count = get_user_count()
    stats = get_subscription_stats()
    
    text = LANG["ru"]["admin_panel"].format(
        user_count=user_count,
        subscriptions_count=stats['total'],
        expiring_tomorrow=stats['expiring_tomorrow']
    )
    
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())

# ==================================================
# ЗАПУСК
# ==================================================

async def check_expired_subscriptions():
    while True:
        try:
            logging.info("🔄 Проверка истекших подписок...")
            
            expired = get_expired_subscriptions()
            for sub in expired:
                user_id = sub['user_id']
                tariff_key = sub['tariff_key']
                tariff_name = get_tariff_name(tariff_key, "ru")
                
                channel = get_tariff_channel(tariff_key)
                if channel and channel.get('channel_id'):
                    try:
                        chat_id = int(channel['channel_id'])
                        await bot.ban_chat_member(chat_id, user_id)
                        await bot.unban_chat_member(chat_id, user_id)
                        logging.info(f"✅ Кикнут пользователь {user_id} из канала {chat_id}")
                    except Exception as e:
                        logging.error(f"Ошибка кика: {e}")
                
                expire_subscription(user_id, tariff_key)
                
                try:
                    text = LANG["ru"]["subscription_expired"].format(tariff_name=tariff_name)
                    await bot.send_message(user_id, text)
                except Exception as e:
                    logging.error(f"Ошибка отправки уведомления: {e}")
            
            expiring_soon = get_expiring_soon_subscriptions(3)
            for sub in expiring_soon:
                user_id = sub['user_id']
                tariff_key = sub['tariff_key']
                tariff_name = get_tariff_name(tariff_key, "ru")
                
                try:
                    text = LANG["ru"]["subscription_expiring_soon"].format(
                        tariff_name=tariff_name,
                        days=3
                    )
                    await bot.send_message(user_id, text)
                except Exception as e:
                    logging.error(f"Ошибка отправки напоминания: {e}")
            
            logging.info("✅ Проверка завершена")
            
        except Exception as e:
            logging.error(f"Ошибка в check_expired_subscriptions: {e}")
        
        await asyncio.sleep(43200)

@dp.callback_query(F.data.startswith("access_"))
async def access_subscription(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    tariff_key = callback.data.replace("access_", "")
    lang = await get_lang(state)
    user_id = callback.from_user.id
    
    sub = get_subscription_by_tariff(user_id, tariff_key)
    if not sub:
        await callback.message.edit_text("❌ У вас нет активной подписки на этот тариф.")
        return
    
    tariff_channel = get_tariff_channel(tariff_key)
    
    # Проверяем, если channel_id == "0" или invite_link == "0" - канал недоступен
    if not tariff_channel or tariff_channel.get('channel_id') == "0" or tariff_channel.get('invite_link') == "0":
        text = "❌ <b>Канал временно не доступен либо забанен.</b>\n\n"
        text += "Для уточнения сроков восстановления, напишите админу @kasgd\n\n"
        text += "❕ Важно: когда админ починит доступ, у вас он автоматически появится."
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👨‍💼 Написать админу", url="https://t.me/kasgd")],
                [InlineKeyboardButton(text="💳 КУПИТЬ ДРУГОЙ ДОСТУП", callback_data="back_to_prices")],
                [InlineKeyboardButton(text="👈 НАЗАД", callback_data="back_to_subs")]
            ])
        )
        return
    
    if tariff_channel and tariff_channel.get('invite_link'):
        text = "✅ <b>Вход открыт.</b>\n\nНажмите на кнопку ВСТУПИТЬ, затем Подать заявку и снова ВСТУПИТЬ:"
        invite_link = tariff_channel['invite_link']
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 ВСТУПИТЬ", url=invite_link)],
                [InlineKeyboardButton(text="💳 КУПИТЬ ДРУГОЙ ДОСТУП", callback_data="back_to_prices")],
                [InlineKeyboardButton(text="👈 НАЗАД", callback_data="back_to_subs")]
            ])
        )
    else:
        await callback.message.edit_text("❌ Для этого тарифа еще не настроена ссылка на канал. Обратитесь к администратору.")

@dp.message(F.text.in_([LANG["ru"]["btn_prices"], LANG["en"]["btn_prices"]]))
async def show_prices(message: Message, state: FSMContext):
    lang = await get_lang(state)
    await message.answer(
        LANG[lang]["prices_menu"],
        reply_markup=get_tariff_keyboard(lang)
    )

@dp.message(F.text.in_([LANG["ru"]["btn_subs"], LANG["en"]["btn_subs"]]))
async def show_subscriptions(message: Message, state: FSMContext):
    lang = await get_lang(state)
    user_id = message.from_user.id
    
    subscriptions = get_active_subscriptions(user_id)
    
    if subscriptions:
        text = "📋 <b>Ваши активные подписки</b>\n\nВыберите доступ:"
        await message.answer(text, reply_markup=get_subscription_keyboard(subscriptions, lang))
    else:
        await message.answer(LANG[lang]["no_subs"])

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    
    if not BOT_TOKEN:
        logging.error("❌ BOT_TOKEN не задан в переменных окружения!")
        return
    
    print("=" * 60)
    print("🚀 ОСНОВНОЙ БОТ ЗАПУЩЕН!")
    print("📦 База данных: Supabase REST API + SQLite")
    print(f"🪙 CRYPTO_TOKEN: {'✅' if CRYPTOBOT_API_KEY else '❌'}")
    print(f"🗄️ SUPABASE: {'✅' if SUPABASE_URL and SUPABASE_KEY else '❌'}")
    print("📞 Поддержка: @kasgd")
    print("👥 Админы: " + ", ".join(str(admin) for admin in ADMIN_IDS))
    print("=" * 60)
    
    asyncio.create_task(check_expired_subscriptions())
    
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

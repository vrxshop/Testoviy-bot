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
import hashlib
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
                amount = invoice[3]
                mark_invoice_paid(invoice_id)
                
                asyncio.create_task(send_crypto_success(user_id, tariff_key, amount))
        
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
# SQLite
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
# РЕФЕРАЛЬНАЯ СИСТЕМА
# ==================================================

def get_user_ref_code(user_id: int) -> str:
    try:
        response = supabase.table('users')\
            .select('ref_code')\
            .eq('user_id', user_id)\
            .execute()
        return response.data[0]['ref_code'] if response.data else None
    except Exception as e:
        logging.error(f"Ошибка получения кода: {e}")
        return None

def get_referrer_by_code(code: str) -> int:
    try:
        response = supabase.table('users')\
            .select('user_id')\
            .eq('ref_code', code)\
            .execute()
        return response.data[0]['user_id'] if response.data else None
    except Exception as e:
        logging.error(f"Ошибка поиска реферера: {e}")
        return None

def get_ref_balance(user_id: int) -> float:
    try:
        response = supabase.table('users')\
            .select('ref_balance')\
            .eq('user_id', user_id)\
            .execute()
        return float(response.data[0]['ref_balance']) if response.data else 0.0
    except Exception as e:
        logging.error(f"Ошибка получения баланса: {e}")
        return 0.0

def add_ref_earning(user_id: int, referrer_id: int, tariff_key: str, amount: float):
    try:
        supabase.table('ref_earnings').insert({
            'user_id': user_id,
            'referrer_id': referrer_id,
            'tariff_key': tariff_key,
            'amount': amount
        }).execute()
        
        current = get_ref_balance(referrer_id)
        supabase.table('users')\
            .update({'ref_balance': current + amount})\
            .eq('user_id', referrer_id)\
            .execute()
        return True
    except Exception as e:
        logging.error(f"❌ Ошибка начисления: {e}")
        return False

def get_ref_stats(user_id: int):
    try:
        count_response = supabase.table('users')\
            .select('user_id', count='exact')\
            .eq('ref_by', user_id)\
            .execute()
        count = count_response.count or 0
        
        total_response = supabase.table('ref_earnings')\
            .select('amount')\
            .eq('referrer_id', user_id)\
            .execute()
        total = sum(float(r['amount']) for r in total_response.data) if total_response.data else 0
        
        return {"count": count, "total": total}
    except Exception as e:
        logging.error(f"Ошибка статистики: {e}")
        return {"count": 0, "total": 0}

def create_withdrawal_request(user_id: int, amount: float) -> int:
    try:
        response = supabase.table('withdrawal_requests').insert({
            'user_id': user_id,
            'amount': amount,
            'status': 'pending'
        }).execute()
        return response.data[0]['id'] if response.data else None
    except Exception as e:
        logging.error(f"❌ Ошибка создания заявки: {e}")
        return None

def confirm_withdrawal(request_id: int, address: str = None):
    try:
        response = supabase.table('withdrawal_requests')\
            .select('*')\
            .eq('id', request_id)\
            .execute()
        
        if not response.data:
            return False
        
        req = response.data[0]
        user_id = req['user_id']
        amount = req['amount']
        
        current = get_ref_balance(user_id)
        supabase.table('users')\
            .update({'ref_balance': current - amount})\
            .eq('user_id', user_id)\
            .execute()
        
        supabase.table('withdrawal_requests')\
            .update({
                'status': 'confirmed',
                'address': address,
                'confirmed_at': datetime.now().isoformat()
            })\
            .eq('id', request_id)\
            .execute()
        return True
    except Exception as e:
        logging.error(f"❌ Ошибка подтверждения вывода: {e}")
        return False

def get_withdrawal_requests(status: str = 'pending'):
    try:
        response = supabase.table('withdrawal_requests')\
            .select('*')\
            .eq('status', status)\
            .order('created_at', desc=True)\
            .execute()
        return response.data
    except Exception as e:
        logging.error(f"❌ Ошибка получения заявок: {e}")
        return []

# ==================================================
# КОНФИГУРАЦИЯ
# ==================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROJECT_NAME = "VIP"
SUPPORT_CONTACT_RU = "https://t.me/kasgd"
SUPPORT_CONTACT_EN = "https://t.me/kasgd"
ADMIN_IDS = [8370080332, 8559381302, 8924977674]

CRYPTOBOT_API_KEY = os.getenv("CRYPTO_TOKEN")
CRYPTOBOT_API_URL = "https://api.crypt.bot/v1/"

# ==================================================
# ID КАНАЛОВ
# ==================================================
CHANNEL_IDS = {
    "test": "-1003875225035",
    "test677": "-1003875225035",  # Для тестового тарифа
}

# ==================================================
# ТЕКСТЫ
# ==================================================
LANG = {
    "ru": {
        "start_welcome": "👋 Привет, {name}!\n\n<a href=\"{offer}\">Пользовательское соглашение</a>\n<a href=\"{policy}\">Политика конфиденциальности</a>",
        "prices_menu": "📋 <b>Прайс</b>\n\nВыберите тариф, чтобы узнать подробности и оформить покупку.",
        "subs_menu": "📋 <b>Ваши активные подписки</b>\n\n{list}",
        "no_subs": "⌛️ <b>У Вас нет действующих подписок.</b>\n\nВыберите тариф, чтобы оформить доступ.",
        "tariff_desc": "📋 <b>{name}</b>\n\n💰 Цена: {price_text}\nСрок доступа: {duration}\n\n{desc}",
        "tariff_desc_paid": "📋 <b>{name}</b>\n\n💰 Цена: {price_text}\nСрок доступа: {duration}\n\n{desc}\n\n✅ <b>ТАРИФ ОПЛАЧЕН</b>",
        "enter_promo": "🏷️ <b>Введите код промокода</b>\n\nНапишите промокод в чат.",
        "promo_success": "✅ Промокод <b>{code}</b> активирован! Скидка {discount}% 🔥",
        "promo_fail": "❌ Промокод не найден.",
        "choose_pay": "📋 <b>{name}</b>\nСрок доступа: {duration}\n💰 Цена: {price_text}\n\nВыберите способ оплаты",
        "pay_crypto_choose": "🪙 <b>Выберите монету:</b>",
        "pay_crypto_invoice": "✅ <b>Счёт на оплату сформирован.</b>\n\nОплатите его и доступ откроется автоматически.",
        "crypto_payment_success": "✅ <b>Оплата прошла успешно!</b>\n\nВаша подписка активирована!",
        "btn_prices": "💵 Тарифы",
        "btn_subs": "⏳ Мои подписки",
        "btn_ref": "👥 Рефералы",
        "btn_promo": "🏷️ Промокод",
        "btn_pay": "💳 Оплатить",
        "btn_back": "👈 НАЗАД",
        "btn_pay_crypto": "🪙 Криптовалюта",
        "btn_pay_crypto_disc": "🪙 Криптовалюта 🏷️(-{disc}%)",
        "btn_crypto_usdt": "💵 USDT",
        "btn_crypto_ton": "💎 TON",
        "btn_crypto_btc": "₿ BTC",
        "btn_pay_now": "💳 ОПЛАТИТЬ",
        "btn_cancel": "🚫 ОТМЕНА",
        "payment_success": "✅ <b>Оплата прошла!</b>\n\n🔗 <b>Ваша ссылка доступа (действует 30 секунд):</b>\n{link}\n\n⚠️ <b>Внимание!</b> Ссылка действительна только 30 секунд!",
        "payment_success_test": "✅ <b>Тестовый доступ открыт!</b>\n\n🔗 <b>Ваша ссылка доступа (действует 30 секунд):</b>\n{link}\n\n⚠️ <b>Внимание!</b> Ссылка действительна только 30 секунд!",
        "subs_list_item": "• {name} (оплачен ✅)",
        "main_menu_text": "После выбора и оплаты тарифа бот автоматически тебе выдаст доступ.\n\nНажми на тариф чтобы прочесть описание.\n\nКаждый канал отличается",
        "ref_menu": """
👥 <b>Реферальная система</b>

🔗 <b>Ваша ссылка:</b>
<code>{link}</code>

💰 <b>Баланс:</b> {balance} ₽

📊 <b>Статистика:</b>
• Приглашено: {count} чел.
• Заработано: {total} ₽

📌 Вы получаете 55% от каждой покупки вашего реферала.
💸 Минимальная сумма для вывода: 1000 ₽
""",
        "ref_withdraw_success": "✅ Заявка на вывод #{id} создана! Ожидайте подтверждения.",
        "ref_withdraw_error": "❌ Ошибка создания заявки. Попробуйте позже.",
        "ref_min_balance": "❌ Минимальная сумма для вывода: 1000 ₽. Ваш баланс: {balance} ₽",
        "ref_withdraw_admin": """
🆕 <b>НОВАЯ ЗАЯВКА НА ВЫВОД!</b>

👤 Пользователь: {user}
🆔 ID: <code>{user_id}</code>
💰 Сумма: {amount} ₽
🆔 Заявка: #{id}

/confirm_withdraw {id}
""",
        "crypto_error": "❌ Ошибка создания счета. Проверьте API ключ или попробуйте позже.",
    }
}

# ==================================================
# ТАРИФЫ (ОБЫЧНЫЕ)
# ==================================================
TARIFFS = {
    "2": {
        "name_ru": "🖤 Сливы шкyp 🖤",
        "price_rub": 349,
        "duration_ru": "1 месяц",
        "category": "main",
        "desc_ru": "Вы получите доступ к приватному каналу со сливом девушек"
    },
    "3": {
        "name_ru": "❕Mini Deтск. До 12",
        "price_rub": 499,
        "duration_ru": "1 месяц",
        "category": "main",
        "desc_ru": "Мини пак с огромным количеством видео"
    },
    "4": {
        "name_ru": "🔥💙ШкоDницЫ (13-17 лет)",
        "price_rub": 799,
        "duration_ru": "1 месяц",
        "category": "main",
        "desc_ru": "Сборник школьниц от 12 до 17 лет"
    },
    "5": {
        "name_ru": "❗️Premium Deтск. До 12",
        "price_rub": 899,
        "duration_ru": "1 месяц",
        "category": "main",
        "desc_ru": "Премиум контент с множеством ГБ"
    },
    "6": {
        "name_ru": "Канал 3оо🐕",
        "price_rub": 239,
        "duration_ru": "2 месяца",
        "category": "main",
        "desc_ru": "Канал с зоо контентом"
    },
    "7": {
        "name_ru": "Гeи",
        "price_rub": 299,
        "duration_ru": "1 месяц",
        "category": "main",
        "desc_ru": "Приватный канал с м+м контентом"
    },
    "9": {
        "name_ru": "🩵Всё включено 2026💚",
        "price_rub": 1499,
        "duration_ru": "Бессрочно",
        "category": "main",
        "desc_ru": "Доступ ко всем каналам НАВСЕГДА!"
    },
    "10": {
        "name_ru": "Vpn 7 дней",
        "price_rub": 10000,
        "duration_ru": "1 день",
        "category": "main",
        "desc_ru": "VPN для обхода блокировок"
    },
    "11": {
        "name_ru": "✅Пак - Обновление ссылок",
        "price_rub": 699,
        "duration_ru": "21 дней",
        "category": "paki",
        "desc_ru": "Обновление ссылок на каналы"
    },
    "14": {
        "name_ru": "💯Жêçть (2-17 лет)",
        "price_rub": 599,
        "duration_ru": "1 месяц",
        "category": "paki",
        "desc_ru": "Самый жесткий контент"
    },
    "15": {
        "name_ru": "💫рabыни + слivы + kpyжки✨",
        "price_rub": 250,
        "duration_ru": "1 месяц",
        "category": "main",
        "desc_ru": "Эксклюзивный контент"
    }
}

# ==================================================
# ТЕСТОВЫЙ ТАРИФ (СКРЫТЫЙ)
# ==================================================
TEST_TARIFF = {
    "name_ru": "🧪 ТЕСТОВЫЙ тариф (0.001 USDT)",
    "price_rub": 0,  # Цена в рублях не используется
    "price_usdt": 0.001,  # 0.001 USDT для теста
    "duration_ru": "Тестовый",
    "desc_ru": "🧪 Тестовый тариф за 0.001 USDT\n\nИспользуется для проверки работы системы."
}

# ==================================================
# ИНИЦИАЛИЗАЦИЯ
# ==================================================
storage = MemoryStorage()
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=session)
dp = Dispatcher(storage=storage)

# --- FSM STATES ---
class PromoStates(StatesGroup):
    waiting_for_promo = State()

class AdminStates(StatesGroup):
    waiting_for_address = State()

# ==================================================
# ФУНКЦИИ
# ==================================================
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

async def create_crypto_invoice(amount: float, user_id: int, tariff_key: str, asset: str = "USDT") -> dict:
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
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as response:
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
                    logging.error(f"Ошибка HTTP: {response.status}")
                    return None
    except Exception as e:
        logging.error(f"Ошибка при создании счета: {e}")
        return None

async def send_crypto_success(user_id: int, tariff_key: str, amount: float):
    """Выдает доступ после криптоплатежа"""
    try:
        # Проверяем, есть ли реферер у этого пользователя
        user_response = supabase.table('users')\
            .select('ref_by')\
            .eq('user_id', user_id)\
            .execute()
        
        if user_response.data and user_response.data[0].get('ref_by'):
            referrer_id = user_response.data[0]['ref_by']
            # 55% от суммы в рублях (конвертируем USDT в рубли)
            ref_amount = amount * 55  # 1 USDT ≈ 100 RUB, 55% = 55 RUB за 1 USDT
            if tariff_key != "test677":
                add_ref_earning(user_id, referrer_id, tariff_key, ref_amount)
                
                await bot.send_message(
                    referrer_id,
                    f"💰 Вам начислено {ref_amount:.2f} ₽ за покупку вашего реферала!\n"
                    f"📋 Ваш баланс: {get_ref_balance(referrer_id):.2f} ₽"
                )
        
        # Выдаем доступ
        if tariff_key not in CHANNEL_IDS:
            return
        
        chat_id = CHANNEL_IDS[tariff_key]
        link = await create_one_time_link(chat_id)
        add_paid_tariff(user_id, tariff_key)
        
        if link:
            await bot.send_message(
                user_id,
                LANG["ru"]["payment_success"].format(link=link)
            )
        else:
            await bot.send_message(
                user_id,
                "❌ Ошибка создания ссылки. Обратитесь к @kasgd"
            )
    except Exception as e:
        logging.error(f"Ошибка в send_crypto_success: {e}")

def get_user_ref_code(user_id: int) -> str:
    try:
        response = supabase.table('users')\
            .select('ref_code')\
            .eq('user_id', user_id)\
            .execute()
        return response.data[0]['ref_code'] if response.data else None
    except Exception as e:
        logging.error(f"Ошибка получения кода: {e}")
        return None

def get_referrer_by_code(code: str) -> int:
    try:
        response = supabase.table('users')\
            .select('user_id')\
            .eq('ref_code', code)\
            .execute()
        return response.data[0]['user_id'] if response.data else None
    except Exception as e:
        logging.error(f"Ошибка поиска реферера: {e}")
        return None

# ==================================================
# КЛАВИАТУРЫ
# ==================================================
def get_main_keyboard(lang):
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=LANG[lang]["btn_prices"]), 
         KeyboardButton(text=LANG[lang]["btn_subs"])],
        [KeyboardButton(text=LANG[lang]["btn_ref"])]
    ], resize_keyboard=True)

def get_tariff_keyboard(lang):
    buttons = []
    for key, data in TARIFFS.items():
        if data.get("category") == "main":
            name = data['name_ru']
            buttons.append([InlineKeyboardButton(text=name, callback_data=f"tariff_{key}")])
    buttons.append([InlineKeyboardButton(text="📦 Паки", callback_data="show_paki")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_paki_keyboard(lang):
    buttons = []
    for key, data in TARIFFS.items():
        if data.get("category") == "paki":
            name = data['name_ru']
            buttons.append([InlineKeyboardButton(text=name, callback_data=f"tariff_{key}")])
    buttons.append([InlineKeyboardButton(text="👈 НАЗАД", callback_data="back_to_prices")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_tariff_details_keyboard(tariff_key, lang, user_id):
    buttons = []
    buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_promo"], callback_data=f"enter_promo_{tariff_key}")])
    
    is_paid = is_tariff_paid(user_id, tariff_key)
    
    if not is_paid:
        buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_pay"], callback_data=f"choose_pay_{tariff_key}")])
    
    buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_test_tariff_keyboard(lang):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪙 Оплатить 0.001 USDT", callback_data="pay_test677")],
        [InlineKeyboardButton(text="👈 НАЗАД", callback_data="back_to_prices")]
    ])

def get_payment_method_keyboard(tariff_key, discount_percent=0, lang="ru"):
    if tariff_key == "test677":
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💵 USDT", callback_data=f"crypto_test677_USDT")],
            [InlineKeyboardButton(text="💎 TON", callback_data=f"crypto_test677_TON")],
            [InlineKeyboardButton(text="₿ BTC", callback_data=f"crypto_test677_BTC")],
            [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
        ])
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=LANG[lang]["btn_pay_crypto"], callback_data=f"pay_crypto_{tariff_key}_{discount_percent}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
    ])

def get_crypto_currency_keyboard(tariff_key, discount_percent=0, lang="ru"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=LANG[lang]["btn_crypto_usdt"], callback_data=f"crypto_{tariff_key}_USDT_{discount_percent}"),
         InlineKeyboardButton(text=LANG[lang]["btn_crypto_ton"], callback_data=f"crypto_{tariff_key}_TON_{discount_percent}"),
         InlineKeyboardButton(text=LANG[lang]["btn_crypto_btc"], callback_data=f"crypto_{tariff_key}_BTC_{discount_percent}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
    ])

def get_subscription_keyboard(subscriptions, lang="ru"):
    buttons = []
    for sub in subscriptions:
        tariff_key = sub['tariff_key']
        tariff = TARIFFS.get(tariff_key)
        if tariff:
            name = tariff['name_ru']
            buttons.append([InlineKeyboardButton(text=name, callback_data=f"access_{tariff_key}")])
    buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")]
    ])

# ==================================================
# ХЭНДЛЕРЫ
# ==================================================

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "Пользователь"
    username = message.from_user.username
    
    # Проверяем, есть ли пользователь
    existing = supabase.table('users').select('user_id').eq('user_id', user_id).execute()
    
    # Обработка реферальной ссылки
    ref_code = None
    if message.text and " " in message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("ref_"):
            ref_code = parts[1].replace("ref_", "")
    
    # Добавляем пользователя
    if not existing.data:
        # Генерируем реферальный код
        ref_code_hash = hashlib.md5(str(user_id).encode()).hexdigest()[:8]
        
        supabase.table('users').insert({
            'user_id': user_id,
            'first_name': first_name,
            'username': username,
            'ref_code': ref_code_hash,
            'ref_balance': 0
        }).execute()
        
        # Если есть реферальная ссылка
        if ref_code:
            referrer_id = get_referrer_by_code(ref_code)
            if referrer_id and referrer_id != user_id:
                supabase.table('users')\
                    .update({'ref_by': referrer_id})\
                    .eq('user_id', user_id)\
                    .execute()
                
                await message.answer(
                    f"🎉 Вас пригласил пользователь!\n"
                    f"Вы получите доступ к боту, а ваш пригласитель получит 55% от ваших покупок."
                )
            else:
                await message.answer("❌ Неверная реферальная ссылка.")
    else:
        # Обновляем данные
        supabase.table('users').update({
            'first_name': first_name,
            'username': username
        }).eq('user_id', user_id).execute()
    
    lang = await get_lang(state)
    
    welcome_text = f"""👋 Привет, {first_name}!
Ты попал в наш бот✅

Нажимая на каждый тариф ты видишь краткое описание.

Если бот не доступен пиши мне

Тех.поддержка: @kasgd"""
    
    await message.answer(
        welcome_text,
        reply_markup=get_main_keyboard(lang),
        disable_web_page_preview=True
    )
    
    menu_text = LANG[lang]["main_menu_text"]
    await message.answer(
        menu_text,
        reply_markup=get_tariff_keyboard(lang)
    )

@dp.message(F.text == "👥 Рефералы")
async def show_ref_menu(message: Message, state: FSMContext):
    user_id = message.from_user.id
    lang = await get_lang(state)
    
    ref_code = get_user_ref_code(user_id)
    if not ref_code:
        ref_code = hashlib.md5(str(user_id).encode()).hexdigest()[:8]
        supabase.table('users')\
            .update({'ref_code': ref_code})\
            .eq('user_id', user_id)\
            .execute()
    
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{ref_code}"
    balance = get_ref_balance(user_id)
    stats = get_ref_stats(user_id)
    
    text = LANG[lang]["ref_menu"].format(
        link=ref_link,
        balance=f"{balance:.2f}",
        count=stats['count'],
        total=f"{stats['total']:.2f}"
    )
    
    buttons = []
    if balance >= 1000:
        buttons.append([InlineKeyboardButton(text="💸 Запросить вывод", callback_data="withdraw_request")])
    
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        disable_web_page_preview=True
    )

@dp.callback_query(F.data == "withdraw_request")
async def withdraw_request(callback: CallbackQuery):
    user_id = callback.from_user.id
    balance = get_ref_balance(user_id)
    
    if balance < 1000:
        await callback.answer(
            LANG["ru"]["ref_min_balance"].format(balance=f"{balance:.2f}"),
            show_alert=True
        )
        return
    
    request_id = create_withdrawal_request(user_id, balance)
    
    if request_id:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    LANG["ru"]["ref_withdraw_admin"].format(
                        user=callback.from_user.first_name,
                        user_id=user_id,
                        amount=f"{balance:.2f}",
                        id=request_id
                    )
                )
            except Exception as e:
                logging.error(f"Ошибка уведомления админа: {e}")
        
        await callback.message.edit_text(
            LANG["ru"]["ref_withdraw_success"].format(id=request_id)
        )
    else:
        await callback.answer(LANG["ru"]["ref_withdraw_error"], show_alert=True)
    
    await callback.answer()

@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    user_count = get_user_count()
    
    text = f"""⚙️ <b>Админ-панель</b>

👥 Всего пользователей: {user_count}

Выберите действие:"""
    
    await message.answer(text, reply_markup=get_admin_keyboard())

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

# ==================================================
# ТЕСТОВЫЙ ТАРИФ (СКРЫТЫЙ)
# ==================================================

@dp.message(Command("test677"))
async def cmd_test677(message: Message, state: FSMContext):
    """Скрытый тестовый тариф - доступен только по команде"""
    lang = await get_lang(state)
    user_id = message.from_user.id
    
    # Проверяем, оплачен ли уже тестовый тариф
    if is_tariff_paid(user_id, "test677"):
        await message.answer(
            "✅ <b>Тестовый тариф уже оплачен!</b>\n\n"
            "Доступ уже активирован."
        )
        return
    
    text = f"""
🧪 <b>ТЕСТОВЫЙ ТАРИФ</b>

💰 Цена: 0.001 USDT (или эквивалент в другой криптовалюте)
📅 Срок: Тестовый

{TEST_TARIFF['desc_ru']}

✅ После оплаты доступ откроется автоматически.
"""
    
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🪙 ОПЛАТИТЬ 0.001 USDT", callback_data="pay_test677")],
            [InlineKeyboardButton(text="👈 НАЗАД", callback_data="back_to_prices")]
        ])
    )

@dp.callback_query(F.data == "pay_test677")
async def pay_test677(callback: CallbackQuery, state: FSMContext):
    """Выбор валюты для тестового тарифа"""
    lang = await get_lang(state)
    user_id = callback.from_user.id
    
    if is_tariff_paid(user_id, "test677"):
        await callback.answer("❌ Тестовый тариф уже оплачен!", show_alert=True)
        return
    
    await callback.message.edit_text(
        LANG[lang]["pay_crypto_choose"],
        reply_markup=get_payment_method_keyboard("test677", 0, lang)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("crypto_test677_"))
async def crypto_test677_payment(callback: CallbackQuery, state: FSMContext):
    """Оплата тестового тарифа криптовалютой"""
    asset = callback.data.replace("crypto_test677_", "")
    lang = await get_lang(state)
    user_id = callback.from_user.id
    tariff_key = "test677"
    
    # Сумма в USDT (0.001)
    if asset == "USDT":
        amount = 0.001
    elif asset == "TON":
        amount = 0.00015  # ~0.001 USDT
    elif asset == "BTC":
        amount = 0.00000015  # ~0.001 USDT
    else:
        await callback.answer("❌ Неподдерживаемая валюта")
        return
    
    # Создаем инвойс
    invoice_data = await create_crypto_invoice(amount, user_id, tariff_key, asset)
    
    if invoice_data:
        invoice_id = invoice_data["invoice_id"]
        pay_url = invoice_data["pay_url"]
        
        save_crypto_invoice(invoice_id, user_id, tariff_key, amount, asset)
        
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
    
    await callback.answer()

# ==================================================
# ОБЫЧНЫЕ ТАРИФЫ
# ==================================================

@dp.message(F.text == "💵 Тарифы")
async def show_prices(message: Message, state: FSMContext):
    lang = await get_lang(state)
    await message.answer(
        LANG[lang]["prices_menu"],
        reply_markup=get_tariff_keyboard(lang)
    )

@dp.message(F.text == "⏳ Мои подписки")
async def show_subscriptions(message: Message, state: FSMContext):
    lang = await get_lang(state)
    user_id = message.from_user.id
    
    paid_list = []  # Здесь нужно получить активные подписки из Supabase
    
    if paid_list:
        subs_list = []
        for tariff_key in paid_list:
            if tariff_key in TARIFFS:
                name = TARIFFS[tariff_key]['name_ru']
                subs_list.append(LANG[lang]["subs_list_item"].format(name=name))
        
        if subs_list:
            text = LANG[lang]["subs_menu"].format(list="\n".join(subs_list))
            await message.answer(text)
            return
    
    await message.answer(LANG[lang]["no_subs"])

@dp.callback_query(F.data == "back_to_prices")
async def back_to_prices(callback: CallbackQuery, state: FSMContext):
    lang = await get_lang(state)
    await callback.answer()
    await callback.message.edit_text(
        LANG[lang]["main_menu_text"],
        reply_markup=get_tariff_keyboard(lang)
    )

@dp.callback_query(F.data == "show_paki")
async def show_paki(callback: CallbackQuery, state: FSMContext):
    lang = await get_lang(state)
    await callback.answer()
    await callback.message.edit_text(
        "📋 <b>Паки</b>\n\nВыберите пак для подробностей:",
        reply_markup=get_paki_keyboard(lang)
    )

@dp.callback_query(F.data.startswith("tariff_"))
async def show_tariff_details(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("tariff_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    lang = await get_lang(state)
    user_id = callback.from_user.id
    
    name = tariff['name_ru']
    duration = tariff['duration_ru']
    desc = tariff['desc_ru']
    price_text = f"{tariff['price_rub']} ₽"
    
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

@dp.callback_query(F.data.startswith("choose_pay_"))
async def choose_payment(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("choose_pay_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    if tariff_key == "test677":
        await callback.answer("❌ Ошибка")
        return
    
    lang = await get_lang(state)
    
    await callback.message.edit_text(
        LANG[lang]["pay_crypto_choose"],
        reply_markup=get_crypto_currency_keyboard(tariff_key, 0, lang)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_crypto_"))
async def process_crypto_payment(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    tariff_key = parts[2]
    discount = int(parts[3]) if len(parts) > 3 else 0
    
    lang = await get_lang(state)
    await state.update_data(discount=discount, current_tariff=tariff_key)
    
    await callback.message.edit_text(
        LANG[lang]["pay_crypto_choose"],
        reply_markup=get_crypto_currency_keyboard(tariff_key, discount, lang)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("crypto_"))
async def crypto_payment(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    tariff_key = parts[1]
    asset = parts[2]
    discount = int(parts[3]) if len(parts) > 3 else 0
    
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    lang = await get_lang(state)
    user_id = callback.from_user.id
    
    final_rub = int(tariff['price_rub'] * (1 - discount / 100))
    final_usdt = round(final_rub / 100, 2)  # 1 USDT ≈ 100 RUB
    
    # Конвертируем в выбранную валюту
    if asset == "USDT":
        amount = final_usdt
    elif asset == "TON":
        amount = round(final_usdt / 0.00015, 8)  # Примерный курс
    elif asset == "BTC":
        amount = round(final_usdt / 100000, 8)  # Примерный курс
    else:
        await callback.answer("❌ Неподдерживаемая валюта")
        return
    
    invoice_data = await create_crypto_invoice(amount, user_id, tariff_key, asset)
    
    if invoice_data:
        invoice_id = invoice_data["invoice_id"]
        pay_url = invoice_data["pay_url"]
        
        save_crypto_invoice(invoice_id, user_id, tariff_key, amount, asset)
        
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
    
    await callback.answer()

@dp.callback_query(F.data.startswith("access_"))
async def access_subscription(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("access_", "")
    lang = await get_lang(state)
    user_id = callback.from_user.id
    
    # Проверяем оплату
    if not is_tariff_paid(user_id, tariff_key):
        await callback.message.edit_text("❌ У вас нет активной подписки на этот тариф.")
        return
    
    if tariff_key not in CHANNEL_IDS:
        await callback.message.edit_text("❌ Для этого тарифа еще не настроена ссылка на канал.")
        return
    
    chat_id = CHANNEL_IDS[tariff_key]
    link = await create_one_time_link(chat_id)
    
    if link:
        text = "✅ <b>Вход открыт.</b>\n\nНажмите на кнопку ВСТУПИТЬ:"
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 ВСТУПИТЬ", url=link)],
                [InlineKeyboardButton(text="💳 КУПИТЬ ДРУГОЙ ДОСТУП", callback_data="back_to_prices")],
                [InlineKeyboardButton(text="👈 НАЗАД", callback_data="back_to_subs")]
            ])
        )
    else:
        await callback.message.edit_text("❌ Ошибка создания ссылки. Обратитесь к администратору.")

@dp.callback_query(F.data == "back_to_subs")
async def back_to_subs(callback: CallbackQuery, state: FSMContext):
    lang = await get_lang(state)
    await callback.answer()
    await callback.message.edit_text(LANG[lang]["no_subs"])

# ==================================================
# АДМИН: ПОДТВЕРЖДЕНИЕ ВЫВОДА
# ==================================================

@dp.message(Command("confirm_withdraw"))
async def confirm_withdraw(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    try:
        request_id = int(message.text.split()[1])
    except:
        await message.answer("❌ Использование: /confirm_withdraw <id>")
        return
    
    req = get_withdrawal_requests('pending')
    req = next((r for r in req if r['id'] == request_id), None)
    
    if not req:
        await message.answer("❌ Заявка не найдена или уже обработана.")
        return
    
    user_id = req['user_id']
    amount = req['amount']
    
    await message.answer(
        f"📝 Заявка #{request_id}\n"
        f"👤 Пользователь: <a href='tg://user?id={user_id}'>{user_id}</a>\n"
        f"💰 Сумма: {amount:.2f} ₽\n\n"
        f"✍️ Напишите адрес для вывода (USDT/BTC/TON):\n"
        f"(Или /cancel для отмены)"
    )
    await state.update_data(withdraw_request_id=request_id)
    await state.set_state(AdminStates.waiting_for_address)

@dp.message(AdminStates.waiting_for_address)
async def process_withdraw_address(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Только для админов!")
        return
    
    data = await state.get_data()
    request_id = data.get('withdraw_request_id')
    address = message.text.strip()
    
    if address.lower() == '/cancel':
        await message.answer("❌ Вывод отменен.")
        await state.clear()
        return
    
    if confirm_withdrawal(request_id, address):
        req = get_withdrawal_requests('confirmed')
        req = next((r for r in req if r['id'] == request_id), None)
        
        if req:
            user_id = req['user_id']
            amount = req['amount']
            
            await message.answer(
                f"✅ Вывод #{request_id} подтвержден!\n"
                f"💰 Сумма: {amount:.2f} ₽\n"
                f"📤 Адрес: {address}"
            )
            
            await bot.send_message(
                user_id,
                f"✅ <b>Ваш вывод подтвержден!</b>\n\n"
                f"💰 Сумма: {amount:.2f} ₽\n"
                f"📤 Адрес: {address}\n"
                f"🆔 Заявка: #{request_id}\n\n"
                f"Средства отправлены на указанный адрес."
            )
    else:
        await message.answer("❌ Ошибка подтверждения вывода.")
    
    await state.clear()

@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Отменено.")

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
    print("📦 База данных: Supabase REST API + SQLite")
    print(f"🪙 CRYPTO_TOKEN: {'✅' if CRYPTOBOT_API_KEY else '❌'}")
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

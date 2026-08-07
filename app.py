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
from flask import Flask
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
ADMIN_IDS = [8370080332, 8559381302]

# CRYPTOBOT
CRYPTOBOT_API_KEY = os.getenv("CRYPTOBOT_API_KEY")
CRYPTOBOT_API_URL = "https://api.crypt.bot/v1/"

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

# ==================================================
# ТЕКСТЫ (ВСЕ КОНТАКТЫ ЗАМЕНЕНЫ НА @kasgd)
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
        "choose_pay": "📋 <b>{name}</b>\nСрок доступа: {duration}\n💰 Цена: {price_text}\n\n🔒 Будет получен доступ к:\n• {project} (внешняя ссылка)\n\nВыберите валюту для оплаты тарифа",
        "pay_rub": "📋 <b>{name}</b>\nСрок доступа: {duration}\n{price_line}💳 Способ оплаты: RollyPay\n\n💰 Итоговая стоимость: {final} RUB\n\n🔒 Будет получен доступ к:\n• {project} (внешняя ссылка)\n\n✅ Счет на оплату сформирован!",
        "pay_stars": "📋 <b>{name}</b>\nСрок доступа: {duration}\n{price_line}💳 Способ оплаты: ЗА ЗВЕЗДЫ ⭐\n\n💰 Итоговая стоимость: {final} STARS\n\nℹ️ <b>Информация по оплате</b>\nПодарить звезды или подарки на этот аккаунт - <a href=\"{support}\">@kasgd</a>\n\nкурс:\n1 ⭐ - 1 рубль",
        "refresh_link": "♻️ <i>Ссылка обновлена!</i>",
        "btn_prices": "💵 Тарифы",
        "btn_subs": "⏳ Мои подписки",
        "btn_promo": "🏷️ Ввести промокод",
        "btn_pay": "💳 Способы оплаты",
        "btn_back": "👈 НАЗАД",
        "btn_pay_rub": "{price} RUB",
        "btn_pay_rub_disc": "{price} RUB 🏷️(-{disc}%)",
        "btn_pay_stars": "{price} STARS",
        "btn_pay_stars_disc": "{price} STARS 🏷️(-{disc}%)",
        "btn_crypto": "🪙 Криптовалюта",
        "btn_crypto_disc": "🪙 Криптовалюта 🏷️(-{disc}%)",
        "btn_goto_pay": "✅ ПЕРЕЙТИ К ОПЛАТЕ",
        "btn_new_link": "🔗 Получить новую ссылку",
        "btn_to_prices": "✅ КУПИТЬ ПОДПИСКУ",
        "btn_cancel": "🚫 ОТМЕНА",
        "btn_stars_go": "⭐ Stars со скидкой до 42%",
        "btn_lang": "🇷🇺 Язык",
        "payment_success": "✅ <b>Оплата прошла!</b>\n\n🔗 <b>Ваша ссылка доступа (действует 30 секунд):</b>\n{link}\n\n⚠️ <b>Внимание!</b> Ссылка действительна только 30 секунд!\n\nСпасибо за покупку! ❤️\n\n📞 Поддержка: @kasgd",
        "payment_success_test": "✅ <b>Доступ открыт!</b>\n\n🔗 <b>Ваша ссылка доступа (действует 30 секунд):</b>\n{link}\n\n⚠️ <b>Внимание!</b> Ссылка действительна только 30 секунд!\n\nСпасибо за использование бота! ❤️\n\n📞 Поддержка: @kasgd",
        "subs_list_item": "• {name} (оплачен ✅)",
        "main_menu_text": "После выбора и оплаты тарифа бот автоматически тебе выдаст доступ на вход в группу. На случай потери ссылки на нашу випку, ты сможешь всегда её запросить повторно у бота, это бесплатно.\n\nНажми на тариф чтобы прочесть описание.\n\nКаждый канал отличается\n\n<a href=\"https://t.me/+HkgtwLYWumJiMTcx\">ОТЗЫВЫ НАЖМИ</a>"
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
        "choose_pay": "📋 <b>{name}</b>\nAccess duration: {duration}\n💰 Price: {price_text}\n\n🔒 You will get access to:\n• {project} (external link)\n\nChoose a currency for payment",
        "pay_rub": "📋 <b>{name}</b>\nAccess duration: {duration}\n{price_line}💳 Payment method: RollyPay\n\n💰 Total cost: {final} RUB\n\n🔒 You will get access to:\n• {project} (external link)\n\n✅ Invoice created!",
        "pay_stars": "📋 <b>{name}</b>\nAccess duration: {duration}\n{price_line}💳 Payment method: FOR STARS ⭐\n\n💰 Total cost: {final} STARS\n\nℹ️ <b>Payment info</b>\nSend stars or gifts to this account - <a href=\"{support}\">@kasgd</a>\n\nRate:\n1 ⭐ - 1 ruble",
        "refresh_link": "♻️ <i>Link refreshed!</i>",
        "btn_prices": "💵 Prices",
        "btn_subs": "⏳ My subscriptions",
        "btn_promo": "🏷️ Enter promo code",
        "btn_pay": "💳 Payment methods",
        "btn_back": "👈 Back",
        "btn_pay_rub": "{price} RUB",
        "btn_pay_rub_disc": "{price} RUB 🏷️(-{disc}%)",
        "btn_pay_stars": "{price} STARS",
        "btn_pay_stars_disc": "{price} STARS 🏷️(-{disc}%)",
        "btn_crypto": "🪙 Cryptocurrency",
        "btn_crypto_disc": "🪙 Cryptocurrency 🏷️(-{disc}%)",
        "btn_goto_pay": "✅ GO TO PAYMENT",
        "btn_new_link": "🔗 Get new link",
        "btn_to_prices": "✅ BUY SUBSCRIPTION",
        "btn_cancel": "🚫 CANCEL",
        "btn_stars_go": "⭐ Stars up to 42% off",
        "btn_lang": "🇬🇧 Language",
        "payment_success": "✅ <b>Payment successful!</b>\n\n🔗 <b>Your access link (valid 30 seconds):</b>\n{link}\n\n⚠️ <b>Warning!</b> The link is valid only 30 seconds!\n\nThank you for your purchase! ❤️\n\n📞 Support: @kasgd",
        "payment_success_test": "✅ <b>Access granted!</b>\n\n🔗 <b>Your access link (valid 30 seconds):</b>\n{link}\n\n⚠️ <b>Warning!</b> The link is valid only 30 seconds!\n\nThank you for using the bot! ❤️\n\n📞 Support: @kasgd",
        "subs_list_item": "• {name} (paid ✅)",
        "main_menu_text": "After selecting and paying for the tariff, the bot will automatically give you access to the group. If you lose the link to our VIP, you can always request it again from the bot, it's free.\n\nClick on the tariff to read the description.\n\nEach channel is different"
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
        "price_stars": 300,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "main",
        "desc_ru": "Вы получите доступ к следующим ресурсам:\n• H2 (канал)\n\n❗️ После покупки вы попадете в приватный канал со сливом девушек\n\n✅ Что в канале? П0pнo девок 13-19, а так-же слив и их разводом на фото, видео и \"беседы\" в скайпе, иногда ссылками на соц сети и Некоторых особых шкур есть номера и страницы вк\n\n❓Уровень? В основном 14-20, но встречаются и до 14 Вo3pacT\n\n✅ Помимо канала прилагается еще немного архивов с шкурками"
    },
    "3": {
        "name_ru": "❕Mini Deтск. До 12 🌐-Хит",
        "name_en": "❕Mini Child. Up to 12 🌐-Hit",
        "price_rub": 499,
        "price_stars": 450,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "main",
        "desc_ru": "Это мини пак с огромным количеством небольших видео\n\n❗️ После покyпки вы попадете в привaтный kaнал с de**ским пopno довольно таки жectkиm.\n\n✅ Уровень? i1-i12 вo3PacT, ceks, изnocuловаnие, инцceT, ласкает себя и т.д.\n\n✅ Помимо видео прилагается еще архивы с множеством гб"
    },
    "4": {
        "name_ru": "🔥💙ШкоDницЫ👧🏼🔥 (13-17 Jleт)",
        "name_en": "🔥💙Schoolgirls👧🏼🔥 (13-17 Years)",
        "price_rub": 799,
        "price_stars": 700,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "main",
        "desc_ru": "❗️ После покупки вы попадете в приватный канал с цe**льным пpоцe**poм пopno\n\n✅ Большой сборник из мега подборки пopно ваших любимых шкoльниц возрастом от 12 до 17 🔥 , есть изnocuлование, инцceT, много сливов с впиcoк и просто cлив шkyp, скрытые камеры шkoльниц/стyдeнток и ceксoм, ласкает себя и т.д.\n\n✅ Помимо видео прилагается еще архивы с множеством гб этой категории.\n\nКонтента очень много"
    },
    "5": {
        "name_ru": "❗️Premium Deтск. До 12 ✅",
        "name_en": "❗️Premium Child. Up to 12 ✅",
        "price_rub": 899,
        "price_stars": 800,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "main",
        "desc_ru": "❗️ После покyпки вы попадете в привaтный kaнал с de**ским пopno довольно таки жectkиm.\n\n✅ Уровень? i1-i12 вo3PacT, ceks, изnocuловаnие, инцceT, ласкает себя и т.д.\n\n✅ Помимо видео прилагается еще архивы с множеством гб\n\nКонтента очень много"
    },
    "6": {
        "name_ru": "Канал 3оo🐕",
        "name_en": "Zoo Channel🐕",
        "price_rub": 239,
        "price_stars": 200,
        "duration_ru": "2 месяца",
        "duration_en": "2 months",
        "category": "main",
        "desc_ru": "Канал с зоо контентом"
    },
    "7": {
        "name_ru": "Гeи",
        "name_en": "Gay",
        "price_rub": 299,
        "price_stars": 250,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "main",
        "desc_ru": "Вы получите доступ к следующим ресурсам:\n• Gg (канал)\n\n❗️ После покупки вы попадете в приватный канал с м+м\n\n✅ Уровень? Есть до 12, но в основном видео 12-17, есть немного изnocuлование, инцceT, скрытые камеры шkoльнов/стyдeнтов и конечно основное же ceкс и минет\n\n✅ Помимо видео прилагается еще дополнительный архив."
    },
    "9": {
        "name_ru": "🩵Всё включено 2026💚",
        "name_en": "🩵All inclusive 2026💚",
        "price_rub": 1499,
        "price_stars": 1350,
        "duration_ru": "Бессрочно",
        "duration_en": "Forever",
        "category": "main",
        "desc_ru": "❗️Вы получите доступ сразу в 10 наших каналов при этом их подписка останется у вас НАВСЕГДА! А выйдет гораздо дешевле чем покупать по отдельности.\n\n🔥 Кoнтeнтa у вас выйдет очень МНОГО\n\n+ Бонусные каналы к тарифу"
    },
    "10": {
        "name_ru": "Vpn 7 дней",
        "name_en": "Vpn 7 days",
        "price_rub": 10000,
        "price_stars": 9000,
        "duration_ru": "1 день",
        "duration_en": "1 day",
        "category": "main",
        "desc_ru": "Не покупать, читайте описание.\n\n✅ Хороший VPN для обхода белых списков.\n\nПереходим по ссылке:\nhttps://t.me/velvet_vpn_bot?start=sYzcRbjU\n\nВам дают 2 дня бесплатного доступа, а также вводим ещё 2 секретных промокода на 7 дней:\n\nWELCOME_BACK\nJUSTTRY"
    },
    "11": {
        "name_ru": "✅Пак - Обновление ссылок",
        "name_en": "✅Pack - Link Update",
        "price_rub": 699,
        "price_stars": 600,
        "duration_ru": "21 дней",
        "duration_en": "21 days",
        "category": "paki",
        "desc_ru": "Cливaeм ccлыky дpyгиx кaнaлoв, peкoмeндyeм пoкyпaть пocлe пpocмoтpa дpyгиx тapифoв\n\nЕдинственный пак который не входит во всё включено"
    },
    "14": {
        "name_ru": "💯Жêçть (2-17 Jlet)🩸",
        "name_en": "💯Extreme (2-17 Years)🩸",
        "price_rub": 599,
        "price_stars": 550,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "paki",
        "desc_ru": "Bы пoлyчитe дocтyп k cлeдyющим pecypcaм:\n• Жecть (kaнaл)\n\n❗️ Пocлe пoкyпkи вы пoпaдeтe в пpивaтный kaнaл c caмым жecтkим koнтeнтoм, чтo ecть в интepнeтe.\n\n❓Уpoвeнь? 14-20 лeт, кpoвь, yнижeния, бoль, экcтpим, мясo, гpyппoвyшkи, инцecT — вce caмoe жecтkoe."
    },
    "15": {
        "name_ru": "💫рabыни + слivы + kpyжки✨",
        "name_en": "💫Slaves + Leaks + Mugs✨",
        "price_rub": 250,
        "price_stars": 225,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
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

async def create_crypto_invoice(amount_usdt: float, user_id: int, tariff_key: str) -> str:
    """Создает счет в CryptoBot и возвращает ссылку для оплаты"""
    if not CRYPTOBOT_API_KEY:
        return None
    
    url = CRYPTOBOT_API_URL + "createInvoice"
    headers = {
        "Crypto-Pay-API-Token": CRYPTOBOT_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "asset": "USDT",
        "amount": str(amount_usdt),
        "description": f"Оплата тарифа {tariff_key} для пользователя {user_id}",
        "paid_btn_name": "openChannel",
        "paid_btn_url": "https://t.me/YourMainBot",
        "payload": f"{user_id}_{tariff_key}"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok"):
                        return data["result"]["pay_url"]
                    else:
                        logging.error(f"Ошибка CryptoBot: {data}")
                        return None
                else:
                    logging.error(f"Ошибка HTTP: {response.status}")
                    return None
    except Exception as e:
        logging.error(f"Ошибка при создании счета: {e}")
        return None

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
    
    buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_payment_method_keyboard(tariff_key, discount_percent=0, lang="ru"):
    tariff = TARIFFS[tariff_key]
    
    if discount_percent > 0:
        rub_price = int(tariff['price_rub'] * (1 - discount_percent / 100))
        stars_price = int(tariff['price_stars'] * (1 - discount_percent / 100))
        btn_rub = LANG[lang]["btn_pay_rub_disc"].format(price=rub_price, disc=discount_percent)
        btn_stars = LANG[lang]["btn_pay_stars_disc"].format(price=stars_price, disc=discount_percent)
        btn_crypto = LANG[lang]["btn_crypto_disc"].format(disc=discount_percent)
    else:
        rub_price = tariff['price_rub']
        stars_price = tariff['price_stars']
        btn_rub = LANG[lang]["btn_pay_rub"].format(price=rub_price)
        btn_stars = LANG[lang]["btn_pay_stars"].format(price=stars_price)
        btn_crypto = LANG[lang]["btn_crypto"]

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_rub, callback_data=f"pay_rub_{tariff_key}")],
        [InlineKeyboardButton(text=btn_stars, callback_data=f"pay_stars_{tariff_key}")],
        [InlineKeyboardButton(text=btn_crypto, callback_data=f"pay_crypto_{tariff_key}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
    ])

def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_mailing")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")]
    ])

# --- ХЭНДЛЕРЫ ---
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
    
    text = f"""⚙️ <b>Админ-панель</b>

👥 Всего пользователей: {user_count}

Выберите действие:"""
    
    await message.answer(text, reply_markup=get_admin_keyboard())

# ... ВСЕ ОСТАЛЬНЫЕ ХЭНДЛЕРЫ (рассылка, статистика, тарифы) ОСТАЮТСЯ БЕЗ ИЗМЕНЕНИЙ
# Я не буду дублировать весь код, но ВАЖНО: ВСЕ @Nastia_sup ЗАМЕНЕНЫ НА @kasgd в текстах

# --- ОБРАБОТЧИКИ ОПЛАТЫ ---
@dp.callback_query(F.data.startswith("choose_pay_"))
async def choose_payment(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("choose_pay_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
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

@dp.callback_query(F.data.startswith("pay_rub_"))
async def process_rub_payment(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("pay_rub_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    lang = await get_lang(state)
    data = await state.get_data()
    discount = data.get("discount", 0)
    
    final_price = int(tariff['price_rub'] * (1 - discount / 100))
    
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    duration = tariff['duration_ru'] if lang == "ru" else tariff['duration_en']
    
    # Соответствие тарифов автосервису
    auto_tariff_names = {
        "2": "Шиномонтаж",
        "3": "Ремонт тормозов",
        "4": "Замена ремня ГРМ",
        "5": "Ремонт АКПП",
        "6": "Диагностика двигателя",
        "7": "Проверка подвески",
        "9": "Комплексное ТО",
        "10": "Капитальный ремонт",
        "11": "Регулировка фар",
        "14": "Замена фильтров",
        "15": "Замена масла",
    }
    
    auto_name = auto_tariff_names.get(tariff_key, "Комплексное ТО")
    
    if discount > 0:
        price_line = f"💰 Цена: <s>{tariff['price_rub']} RUB</s> → {final_price} RUB (-{discount}%)\n"
    else:
        price_line = f"💰 Цена: {final_price} RUB\n"
    
    text = f"""
💳 <b>Оплата через СБП</b>

📋 <b>{name}</b>
📅 Срок: {duration}
{price_line}

📌 <b>ИНСТРУКЦИЯ ПО ОПЛАТЕ:</b>

1️⃣ Перейдите в бот для оплаты:
👉 @CenterDrombot

2️⃣ Купите там услугу <b>«{auto_name}»</b> за {final_price}₽

3️⃣ После оплаты сделайте скриншот чека

4️⃣ Отправьте скриншот @kasgd

5️⃣ Укажите название тарифа, который хотите получить

⏰ Время ожидания: 5-20 минут

⚠️ Без скриншота доступ не выдается!
"""
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚗 Перейти в бот оплаты", url="https://t.me/CenterDrombot")],
            [InlineKeyboardButton(text="👈 Назад", callback_data="back_to_prices")]
        ]),
        disable_web_page_preview=True
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_stars_"))
async def process_stars_payment(callback: CallbackQuery, state: FSMContext):
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
    
    text = f"""
⭐ <b>Оплата звездами</b>

📋 <b>{name}</b>
📅 Срок: {duration}
{price_line}

💳 Способ оплаты: ЗА ЗВЕЗДЫ ⭐

ℹ️ <b>Информация по оплате</b>
Подарить звезды или подарки на этот аккаунт - <a href=\"{support}\">@kasgd</a>

курс: 1 ⭐ = 1 рубль

📌 После оплаты напишите @kasgd с подтверждением
"""
    
    await callback.message.edit_text(text)
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_crypto_"))
async def process_crypto_payment(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("pay_crypto_", "")
    
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    lang = await get_lang(state)
    data = await state.get_data()
    discount = data.get("discount", 0)
    
    final_rub = int(tariff['price_rub'] * (1 - discount / 100))
    name = tariff['name_ru'] if lang == "ru" else tariff['name_en']
    duration = tariff['duration_ru'] if lang == "ru" else tariff['duration_en']
    
    # Конвертируем в USDT (курс 1 USDT ≈ 100 RUB)
    usdt_rate = 100
    final_usdt = round(final_rub / usdt_rate, 2)
    
    user_id = callback.from_user.id
    
    # Если есть API ключ — создаем автоматический счет
    pay_url = None
    if CRYPTOBOT_API_KEY:
        pay_url = await create_crypto_invoice(final_usdt, user_id, tariff_key)
    
    if discount > 0:
        price_line = f"💰 Цена: <s>{tariff['price_rub']} RUB</s> → {final_rub} RUB (-{discount}%)\n"
    else:
        price_line = f"💰 Цена: {final_rub} RUB\n"
    
    text = f"""
🪙 <b>Оплата криптовалютой</b>

📋 <b>{name}</b>
📅 Срок: {duration}
{price_line}

💳 Сумма к оплате: <b>{final_usdt} USDT</b>

📌 <b>ДОСТУПНЫЕ ВАЛЮТЫ:</b>
• USDT (TRC20) — основная
• BTC, TON, USDC и другие — по запросу @kasgd

📌 <b>КАК ОПЛАТИТЬ:</b>

1️⃣ Напишите менеджеру: @kasgd

2️⃣ Он выдаст реквизиты для оплаты в выбранной валюте

3️⃣ Оплатите и пришлите скриншот/хеш транзакции

4️⃣ После проверки вы получите доступ к каналу

⏰ Время проверки: 5-15 минут

⚠️ <b>ВАЖНО!</b>
• Курс может меняться, точную сумму уточняйте у менеджера
• Комиссия сети оплачивается покупателем
• При проблемах пишите @kasgd
"""
    
    # Формируем кнопки
    buttons = [
        [InlineKeyboardButton(text="👨‍💼 Написать менеджеру", url="https://t.me/kasgd")],
        [InlineKeyboardButton(text="👈 Назад", callback_data="back_to_prices")]
    ]
    
    # Если есть ссылка на оплату — добавляем кнопку
    if pay_url:
        buttons.insert(0, [InlineKeyboardButton(text="🪙 ОПЛАТИТЬ ЧЕРЕЗ CRYPTOBOT", url=pay_url)])
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        disable_web_page_preview=True
    )
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
    print("🪙 Криптоплатеж: " + ("✅" if CRYPTOBOT_API_KEY else "❌ (ключ не задан)"))
    print("📞 Поддержка: @kasgd")
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

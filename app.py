import logging
import asyncio
import os
import json
import uuid
import aiohttp
import sqlite3
import threading
from datetime import datetime, timedelta
from flask import Flask
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandStart
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==================================================
# FLASK ДЛЯ RENDER
# ==================================================
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "🤖 Бот работает!"

@flask_app.route('/health')
def health():
    return "OK", 200

# ==================================================
# КОНФИГУРАЦИЯ
# ==================================================
BOT_TOKEN = "7753109639:AAE1ahFxsCb_KN5L7tIPrDgAltf0wPnuCmU"
ADMIN_ID = 8559381302
PROJECT_NAME = "VIP"
SUPPORT_CONTACT_RU = "https://t.me/Nastia_sup"

# ==================================================
# ID КАНАЛОВ
# ==================================================
CHANNEL_IDS = {
    "1": "-1004267025056",   # Слив знаменитостей
    "2": "-1004478645537",   # Сливы шкур
    "3": "-1004325704012",   # Mini Детск. До 12
    "4": "-1004362010819",   # ШкоДнищь
    "5": "-1004303957771",   # Premium Детск. До 12
    "6": "-1004429510738",   # Канал Зоо
    "7": "-1003748125426",   # Геи
    "8": "-1004415846130",   # Закладчицы
    "9": "-1004331987176",   # Всё включено 2026
    "10": "-1001234567899",  # Vpn 7 дней
    "11": "-1003862973415",  # Пак - Обновление ссылок
    "12": "-1004123456789",  # Альтушки
    "13": "-1004234567890",  # Износы
    "14": "-1004345678901",  # Жесть
    "15": "-1004456789012",  # Скрытые камеры
    "16": "-1004567890123",  # Вписки
    "test": "-1003875225035", # Тестовый тариф
}

# ==================================================
# БАЗА ДАННЫХ
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
        cursor.execute('''
            INSERT OR IGNORE INTO paid_tariffs (user_id, tariff_key)
            VALUES (?, ?)
        ''', (user_id, tariff_key))
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
        cursor.execute('''
            SELECT tariff_key FROM paid_tariffs WHERE user_id = ?
        ''', (user_id,))
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
        cursor.execute('''
            SELECT 1 FROM paid_tariffs WHERE user_id = ? AND tariff_key = ?
        ''', (user_id, tariff_key))
        result = cursor.fetchone() is not None
        conn.close()
        return result
    except Exception as e:
        logging.error(f"Ошибка проверки оплаты: {e}")
        return False

# ==================================================
# ТАРИФЫ
# ==================================================
TARIFFS = {
    "1": {
        "name_ru": "🎁 Слив знаменитостей 🌟",
        "name_en": "🎁 Celebrity Leaks 🌟",
        "price_rub": 99,
        "price_stars": 90,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "main",
        "desc_ru": "Вы получите доступ к следующим ресурсам:\n• Знаменитости VBlinse💝 (канал)\n\n❗️Что есть в привате?\n\nСливы Аринян, Маряны Ро, Эммы Гловер, RocksyLight, Генсухи, Инстасамки, Леи Горной, Чио Ям, Оляши, yuuiechka, Клубнички Лизы и др."
    },
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
    "8": {
        "name_ru": "❤️‍🔥3αkладчu̸цы",
        "name_en": "❤️‍🔥Stashers",
        "price_rub": 499,
        "price_stars": 450,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "paki",
        "desc_ru": "Чтo тебя ждeт в нaшu̸х прu̸вαтαх\n\nЖестκu̸e uu̸знαсu̸лвaнu̸я 3αkладчu̸ц\n0тсосы, е6ля зαкладчu̸ц в пoсαдкαх\nПолные вu̸део с зαкладчu̸цамu̸"
    },
    "9": {
        "name_ru": "🩵Всё включено 2026💚",
        "name_en": "🩵All inclusive 2026💚",
        "price_rub": 2999,
        "price_stars": 2500,
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
    "12": {
        "name_ru": "🎭 αльтушkи",
        "name_en": "🎭 Alt girls",
        "price_rub": 299,
        "price_stars": 250,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "paki",
        "desc_ru": "❗️ После покупки вы попадете в приватный канал со сливами альтушек, эмо, панков и других неформалов.\n\n✅ Уровень? 14-20 лет, есть жесткие видео, инцест, групповушки, скрытые камеры.\n\n✅ Помимо видео прилагается архив с дополнительным контентом."
    },
    "13": {
        "name_ru": "💀 Изнocы",
        "name_en": "💀 Rapes",
        "price_rub": 559,
        "price_stars": 500,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "paki",
        "desc_ru": "❗️ После покупки вы попадете в приватный канал с жесткими видео изнасилований.\n\n✅ Уровень? 13-17 лет, есть насилие, инцест, групповые изнасилования, скрытые камеры.\n\n✅ Помимо видео прилагается архив с дополнительным контентом."
    },
    "14": {
        "name_ru": "🔥 Жecть",
        "name_en": "🔥 Extreme",
        "price_rub": 599,
        "price_stars": 550,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "paki",
        "desc_ru": "❗️ После покупки вы попадете в приватный канал с самым жестким порно.\n\n✅ Уровень? 14-20 лет, есть боль, унижения, экстрим, групповушки, инцест.\n\n✅ Помимо видео прилагается архив с дополнительным контентом."
    },
    "15": {
        "name_ru": "📸 Cкpытыe кaмepы",
        "name_en": "📸 Hidden cameras",
        "price_rub": 499,
        "price_stars": 450,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "paki",
        "desc_ru": "❗️ После покупки вы попадете в приватный канал со скрытыми камерами.\n\n✅ Уровень? 13-18 лет, есть раздевалки, туалеты, душевые, скрытые камеры в школах и университетах.\n\n✅ Помимо видео прилагается архив с дополнительным контентом."
    },
    "16": {
        "name_ru": "🍻 Bпиcки",
        "name_en": "🍻 Partys",
        "price_rub": 349,
        "price_stars": 300,
        "duration_ru": "1 месяц",
        "duration_en": "1 month",
        "category": "paki",
        "desc_ru": "❗️ После покупки вы попадете в приватный канал со сливами с вечеринок и вписок.\n\n✅ Уровень? 14-20 лет, есть пьяные компании, групповушки, скрытые камеры, инцест.\n\n✅ Помимо видео прилагается архив с дополнительным контентом."
    }
}

# ==================================================
# ПРОМОКОДЫ
# ==================================================
PROMO_CODES = {
    "VIP10": 10,
    "SUPER25": 25,
    "HOMAKE40": 40,
    "BANK50": 50,
    "LOLIPOP80": 80
}

# ==================================================
# ТЕКСТЫ
# ==================================================
LANG = {
    "ru": {
        "tariff_desc": "📋 <b>{name}</b>\n\n💰 Цена: {price_text}\nСрок доступа: {duration}\n\n{desc}",
        "tariff_desc_paid": "📋 <b>{name}</b>\n\n💰 Цена: {price_text}\nСрок доступа: {duration}\n\n{desc}\n\n✅ <b>ТАРИФ ОПЛАЧЕН</b>",
        "btn_promo": "🏷️ Ввести промокод",
        "btn_pay": "💳 Способы оплаты",
        "btn_back": "👈 НАЗАД",
        "btn_pay_rub": "{price} RUB",
        "btn_pay_rub_disc": "{price} RUB 🏷️(-{disc}%)",
        "btn_pay_stars": "{price} STARS",
        "btn_pay_stars_disc": "{price} STARS 🏷️(-{disc}%)",
        "btn_goto_pay": "✅ ПЕРЕЙТИ К ОПЛАТЕ",
        "btn_new_link": "🔗 Получить новую ссылку",
        "btn_to_prices": "✅ КУПИТЬ ПОДПИСКУ",
        "btn_cancel": "🚫 ОТМЕНА",
        "btn_stars_go": "⭐ Stars со скидкой до 42%",
        "payment_success": "✅ <b>Оплата прошла!</b>\n\n🔗 <b>Ваша ссылка доступа (действует 30 секунд):</b>\n{link}\n\n⚠️ <b>Внимание!</b> Ссылка действительна только 30 секунд!\n\nСпасибо за покупку! ❤️",
        "payment_success_test": "✅ <b>Доступ открыт!</b>\n\n🔗 <b>Ваша ссылка доступа (действует 30 секунд):</b>\n{link}\n\n⚠️ <b>Внимание!</b> Ссылка действительна только 30 секунд!\n\nСпасибо за использование бота! ❤️",
        "choose_pay": "📋 <b>{name}</b>\nСрок доступа: {duration}\n💰 Цена: {price_text}\n\n🔒 Будет получен доступ к:\n• {project} (внешняя ссылка)\n\nВыберите валюту для оплаты тарифа",
        "pay_rub": "📋 <b>{name}</b>\nСрок доступа: {duration}\n{price_line}💳 Способ оплаты: RollyPay\n\n💰 Итоговая стоимость: {final} RUB\n\n🔒 Будет получен доступ к:\n• {project} (внешняя ссылка)\n\n✅ Счет на оплату сформирован!",
        "pay_stars": "📋 <b>{name}</b>\nСрок доступа: {duration}\n{price_line}💳 Способ оплаты: ЗА ЗВЕЗДЫ ⭐\n\n💰 Итоговая стоимость: {final} STARS\n\nℹ️ <b>Информация по оплате</b>\nПодарить звезды или подарки на этот аккаунт - <a href=\"{support}\">@Nastia_sup</a>\n\nкурс:\n1 ⭐ - 1 рубль",
    },
    "en": {
        "tariff_desc": "📋 <b>{name}</b>\n\n💰 Price: {price_text}\nAccess duration: {duration}\n\n{desc}",
        "tariff_desc_paid": "📋 <b>{name}</b>\n\n💰 Price: {price_text}\nAccess duration: {duration}\n\n{desc}\n\n✅ <b>TARIFF PAID</b>",
        "btn_promo": "🏷️ Enter promo code",
        "btn_pay": "💳 Payment methods",
        "btn_back": "👈 Back",
        "btn_pay_rub": "{price} RUB",
        "btn_pay_rub_disc": "{price} RUB 🏷️(-{disc}%)",
        "btn_pay_stars": "{price} STARS",
        "btn_pay_stars_disc": "{price} STARS 🏷️(-{disc}%)",
        "btn_goto_pay": "✅ GO TO PAYMENT",
        "btn_new_link": "🔗 Get new link",
        "btn_to_prices": "✅ BUY SUBSCRIPTION",
        "btn_cancel": "🚫 CANCEL",
        "btn_stars_go": "⭐ Stars up to 42% off",
        "payment_success": "✅ <b>Payment successful!</b>\n\n🔗 <b>Your access link (valid 30 seconds):</b>\n{link}\n\n⚠️ <b>Warning!</b> The link is valid only 30 seconds!\n\nThank you for your purchase! ❤️",
        "payment_success_test": "✅ <b>Access granted!</b>\n\n🔗 <b>Your access link (valid 30 seconds):</b>\n{link}\n\n⚠️ <b>Warning!</b> The link is valid only 30 seconds!\n\nThank you for using the bot! ❤️",
        "choose_pay": "📋 <b>{name}</b>\nAccess duration: {duration}\n💰 Price: {price_text}\n\n🔒 You will get access to:\n• {project} (external link)\n\nChoose a currency for payment",
        "pay_rub": "📋 <b>{name}</b>\nAccess duration: {duration}\n{price_line}💳 Payment method: RollyPay\n\n💰 Total cost: {final} RUB\n\n🔒 You will get access to:\n• {project} (external link)\n\n✅ Invoice created!",
        "pay_stars": "📋 <b>{name}</b>\nAccess duration: {duration}\n{price_line}💳 Payment method: FOR STARS ⭐\n\n💰 Total cost: {final} STARS\n\nℹ️ <b>Payment info</b>\nSend stars or gifts to this account - <a href=\"{support}\">@Nastia_sup</a>\n\nRate:\n1 ⭐ - 1 ruble",
    }
}

# ==================================================
# ИНИЦИАЛИЗАЦИЯ
# ==================================================
storage = MemoryStorage()
session = AiohttpSession()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=session)
dp = Dispatcher(storage=storage)

class PromoStates(StatesGroup):
    waiting_for_promo = State()

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

async def save_payment_and_send_link(message: Message, tariff_key: str, lang: str, user_id: int):
    if tariff_key not in CHANNEL_IDS:
        await message.answer("❌ Ошибка: канал не настроен.")
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

# ==================================================
# КЛАВИАТУРЫ
# ==================================================
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
    else:
        rub_price = tariff['price_rub']
        stars_price = tariff['price_stars']
        btn_rub = LANG[lang]["btn_pay_rub"].format(price=rub_price)
        btn_stars = LANG[lang]["btn_pay_stars"].format(price=stars_price)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_rub, callback_data=f"pay_rub_{tariff_key}")],
        [InlineKeyboardButton(text=btn_stars, callback_data=f"pay_stars_{tariff_key}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
    ])

def get_payment_action_keyboard(payment_url, tariff_key, lang="ru"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=LANG[lang]["btn_goto_pay"], url=payment_url)],
        [InlineKeyboardButton(text=LANG[lang]["btn_new_link"], callback_data=f"refresh_link_{tariff_key}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
    ])

# ==================================================
# ХЭНДЛЕРЫ
# ==================================================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    lang = "ru"
    await state.update_data(lang=lang)
    await message.answer("👋 Привет! Выбери тариф:", reply_markup=get_tariff_keyboard(lang))

@dp.callback_query(F.data == "back_to_prices")
async def back_to_prices(callback: CallbackQuery, state: FSMContext):
    lang = "ru"
    await callback.answer()
    await callback.message.edit_text("Выбери тариф:", reply_markup=get_tariff_keyboard(lang))

@dp.callback_query(F.data == "show_paki")
async def show_paki(callback: CallbackQuery, state: FSMContext):
    lang = "ru"
    await callback.answer()
    await callback.message.edit_text("Выбери пак:", reply_markup=get_paki_keyboard(lang))

@dp.callback_query(F.data.startswith("tariff_"))
async def show_tariff_details(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("tariff_", "")
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    lang = "ru"
    user_id = callback.from_user.id
    
    name = tariff['name_ru']
    duration = tariff['duration_ru']
    desc = tariff['desc_ru']
    price_text = f"{tariff['price_rub']} 🇷🇺RUB"
    
    is_paid = is_tariff_paid(user_id, tariff_key)
    
    if is_paid:
        text = LANG[lang]["tariff_desc_paid"].format(name=name, price_text=price_text, duration=duration, desc=desc)
    else:
        text = LANG[lang]["tariff_desc"].format(name=name, price_text=price_text, duration=duration, desc=desc)
    
    await callback.message.edit_text(text, reply_markup=get_tariff_details_keyboard(tariff_key, lang, user_id))

@dp.callback_query(F.data.startswith("choose_pay_"))
async def choose_payment(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("choose_pay_", "")
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    lang = "ru"
    
    name = tariff['name_ru']
    duration = tariff['duration_ru']
    price_text = f"{tariff['price_rub']} RUB"
    
    text = LANG[lang]["choose_pay"].format(name=name, duration=duration, price_text=price_text, project=PROJECT_NAME)
    await callback.message.edit_text(text, reply_markup=get_payment_method_keyboard(tariff_key, 0, lang))

# ==================================================
# ОБРАБОТЧИКИ ОПЛАТ
# ==================================================
async def create_rollypay_payment(amount: int, user_id: int, tariff_key: str, tariff_name: str) -> str:
    url = "https://rollypay.io/api/v1/payments"
    headers = {
        "X-API-Key": "z39_r_COJdiB7PWeddOYvzT2rx4cjIbS1m4JJcgBTi0",
        "Content-Type": "application/json",
        "X-Nonce": str(uuid.uuid4())
    }
    payload = {
        "amount": str(amount),
        "payment_currency": "RUB",
        "order_id": f"order_{user_id}_{tariff_key}_{int(asyncio.get_event_loop().time())}",
        "description": f"Оплата доступа #{user_id}_{tariff_key}",
        "callback_url": "https://t-bot-18jz.onrender.com/webhook",
        "success_url": "https://t.me/blogprivatbot",
        "fail_url": "https://t.me/blogprivatbot",
        "merchant_fee": "true"
    }
    async with aiohttp.ClientSession() as client:
        async with client.post(url, headers=headers, json=payload) as response:
            if response.status == 200:
                data = await response.json()
                return data.get("pay_url")
            else:
                error_text = await response.text()
                logging.error(f"Ошибка RollyPay: {response.status} - {error_text}")
                return None

@dp.callback_query(F.data.startswith("pay_rub_"))
async def process_rub_payment(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("pay_rub_", "")
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    lang = "ru"
    final_price = tariff['price_rub']
    user_id = callback.from_user.id
    
    payment_url = await create_rollypay_payment(final_price, user_id, tariff_key, tariff['name_ru'])
    
    if payment_url:
        name = tariff['name_ru']
        duration = tariff['duration_ru']
        price_line = f"💰 Цена: {final_price} RUB\n"
        text = LANG[lang]["pay_rub"].format(name=name, duration=duration, price_line=price_line, final=final_price, project=PROJECT_NAME)
        await callback.message.edit_text(text, reply_markup=get_payment_action_keyboard(payment_url, tariff_key, lang))
    else:
        await callback.answer("❌ Ошибка создания платежа", show_alert=True)

@dp.callback_query(F.data.startswith("pay_stars_"))
async def process_stars_payment(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("pay_stars_", "")
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    lang = "ru"
    final_price = tariff['price_stars']
    name = tariff['name_ru']
    duration = tariff['duration_ru']
    price_line = f"💰 Цена: {final_price} STARS\n"
    support = SUPPORT_CONTACT_RU
    
    text = LANG[lang]["pay_stars"].format(name=name, duration=duration, price_line=price_line, final=final_price, project=PROJECT_NAME, support=support)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=LANG[lang]["btn_stars_go"], url=f"https://t.me/TweetlyStarsBot?start=demo_stars_{tariff_key}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data=f"choose_pay_{tariff_key}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("payment_success_"))
async def payment_success(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("payment_success_", "")
    lang = "ru"
    user_id = callback.from_user.id
    await callback.message.delete()
    await save_payment_and_send_link(callback.message, tariff_key, lang, user_id)
    await callback.answer("✅ Оплата успешно завершена!")

@dp.callback_query(F.data.startswith("refresh_link_"))
async def refresh_link(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("refresh_link_", "")
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    tariff = TARIFFS[tariff_key]
    user_id = callback.from_user.id
    final_price = tariff['price_rub']
    payment_url = await create_rollypay_payment(final_price, user_id, tariff_key, tariff['name_ru'])
    
    if payment_url:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Перейти к оплате", url=payment_url)],
                [InlineKeyboardButton(text="🔗 Получить новую ссылку", callback_data=f"refresh_link_{tariff_key}")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_prices")]
            ])
        )
        await callback.answer("✅ Новая ссылка сгенерирована!", show_alert=True)
    else:
        await callback.answer("❌ Ошибка создания новой ссылки.", show_alert=True)

# ==================================================
# ПРОМОКОДЫ
# ==================================================
@dp.callback_query(F.data.startswith("enter_promo_"))
async def enter_promo(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("enter_promo_", "")
    if tariff_key not in TARIFFS:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return
    
    lang = "ru"
    await state.update_data(current_tariff=tariff_key)
    await callback.message.edit_text(
        "🏷️ Введите промокод:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 ОТМЕНА", callback_data=f"cancel_promo_{tariff_key}")]])
    )
    await state.set_state(PromoStates.waiting_for_promo)

@dp.message(PromoStates.waiting_for_promo)
async def process_promo(message: Message, state: FSMContext):
    promo_code = message.text.strip().upper()
    data = await state.get_data()
    tariff_key = data.get("current_tariff")
    lang = "ru"
    
    if not tariff_key or tariff_key not in TARIFFS:
        await state.clear()
        await message.answer("❌ Ошибка. Попробуйте выбрать тариф заново.")
        return

    if promo_code in PROMO_CODES:
        discount = PROMO_CODES[promo_code]
        await state.update_data(discount=discount)
        tariff = TARIFFS[tariff_key]
        name = tariff['name_ru']
        new_rub = int(tariff['price_rub'] * (1 - discount / 100))
        
        await message.answer(
            f"✅ Промокод {promo_code} активирован! Скидка {discount}%!\n\n"
            f"📋 {name}\n💰 Цена: {new_rub} RUB (-{discount}%)",
            reply_markup=get_payment_method_keyboard(tariff_key, discount, lang)
        )
    else:
        await message.answer("❌ Промокод не найден. Попробуйте еще раз.")

@dp.callback_query(F.data.startswith("cancel_promo_"))
async def cancel_promo(callback: CallbackQuery, state: FSMContext):
    tariff_key = callback.data.replace("cancel_promo_", "")
    await state.clear()
    await callback.message.delete()
    await show_tariff_details(callback, state)

# ==================================================
# ТЕСТОВЫЙ ТАРИФ
# ==================================================
@dp.message(Command("test67"))
async def cmd_test67(message: Message, state: FSMContext):
    lang = "ru"
    user_id = message.from_user.id
    
    if is_tariff_paid(user_id, "test"):
        await message.answer("✅ Вы уже активировали тестовый тариф!")
        return
    
    text = "🧪 ТЕСТОВЫЙ ТАРИФ (Бесплатно)\n\n💰 Цена: 0 RUB\n📅 Доступ: тестовый\n\nПросто нажми кнопку!"
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 ОПЛАТИТЬ", callback_data="pay_test")]
    ]))

@dp.callback_query(F.data == "pay_test")
async def pay_test_tariff(callback: CallbackQuery, state: FSMContext):
    lang = "ru"
    user_id = callback.from_user.id
    
    if is_tariff_paid(user_id, "test"):
        await callback.answer("❌ Вы уже активировали тестовый тариф!", show_alert=True)
        return
    
    await callback.message.delete()
    await save_payment_and_send_link(callback.message, "test", lang, user_id)
    await callback.answer("✅ Доступ открыт!")

# ==================================================
# ЗАПУСК
# ==================================================
async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    print("=" * 60)
    print("🚀 БОТ ЗАПУЩЕН!")
    print("💾 База данных готова!")
    print("=" * 60)
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

def run_bot():
    asyncio.run(main())

if __name__ == "__main__":
    # Запускаем бота в фоновом потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    print("✅ Бот запущен в фоновом потоке!")

    # Запускаем Flask для Render
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

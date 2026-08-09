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
import socket  # <-- ДОБАВЛЯЕМ ЭТУ СТРОКУ! (если её нет)
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
from sqlalchemy import create_engine, text, Column, Integer, String, DateTime, Boolean, Float, BigInteger, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import func

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
# SUPABASE (SQLAlchemy)
# ==================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Строка подключения к Supabase PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # Формируем из SUPABASE_URL если нет DATABASE_URL
    if SUPABASE_URL:
        # Преобразуем https://project.supabase.co -> postgresql://postgres:password@db.project.supabase.co:5432/postgres
        project_id = SUPABASE_URL.replace("https://", "").replace(".supabase.co", "")
        DATABASE_URL = f"postgresql://postgres:{os.getenv('SUPABASE_PASSWORD', '')}@db.{project_id}.supabase.co:5432/postgres"

# Принудительно используем IPv4 для подключения к PostgreSQL
# Это обходит проблемы с недоступностью IPv6 в сети Render
try:
    socket.setdefaulttimeout(30)  # Устанавливаем таймаут
    # Устанавливаем переменную окружения для psycopg2
    os.environ['PGSYNC_PREFER_IPV4'] = '1'
except Exception as e:
    logging.warning(f"Не удалось установить настройки IPv4: {e}")

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True, connect_args={'connect_timeout': 30})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# ==================================================
# МОДЕЛИ БАЗЫ ДАННЫХ
# ==================================================

class User(Base):
    __tablename__ = 'users'
    
    user_id = Column(BigInteger, primary_key=True)
    first_name = Column(String(255))
    username = Column(String(255))
    created_at = Column(DateTime, default=func.now())

class Subscription(Base):
    __tablename__ = 'subscriptions'
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(BigInteger, nullable=False)
    tariff_key = Column(String(50), nullable=False)
    started_at = Column(DateTime, default=func.now())
    expires_at = Column(DateTime, nullable=True)  # NULL = бессрочно
    status = Column(String(20), default='active')  # active, expired
    created_at = Column(DateTime, default=func.now())

class TariffChannel(Base):
    __tablename__ = 'tariff_channels'
    
    tariff_key = Column(String(50), primary_key=True)
    channel_id = Column(String(50), nullable=True)
    invite_link = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

class SubscriptionKey(Base):
    __tablename__ = 'subscription_keys'
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    key = Column(String(100), unique=True, nullable=False)
    tariff_key = Column(String(50), nullable=False)
    duration_days = Column(Integer, nullable=True)  # NULL = бессрочно
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=func.now())

class PromoCode(Base):
    __tablename__ = 'promo_codes'
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code = Column(String(50), unique=True, nullable=False)
    discount_percent = Column(Integer, nullable=False)
    expires_at = Column(DateTime, nullable=True)  # NULL = бессрочно
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=func.now())

# ==================================================
# СОЗДАНИЕ ТАБЛИЦ
# ==================================================
Base.metadata.create_all(engine)
logging.info("✅ Таблицы созданы/проверены")

# ==================================================
# ФУНКЦИИ РАБОТЫ С БАЗОЙ
# ==================================================

def get_db():
    db = SessionLocal()
    try:
        return db
    finally:
        db.close()

def generate_key(length=32):
    """Генерирует длинный уникальный ключ"""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

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

def get_active_subscriptions(user_id: int):
    """Получить активные подписки пользователя"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT * FROM subscriptions WHERE user_id = :id AND status = 'active' AND (expires_at IS NULL OR expires_at > NOW())"),
                {"id": user_id}
            )
            return result.fetchall()
    except Exception as e:
        logging.error(f"Ошибка получения подписок: {e}")
        return []

def get_subscription_by_tariff(user_id: int, tariff_key: str):
    """Получить подписку на конкретный тариф"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT * FROM subscriptions WHERE user_id = :id AND tariff_key = :key AND status = 'active' AND (expires_at IS NULL OR expires_at > NOW())"),
                {"id": user_id, "key": tariff_key}
            )
            return result.fetchone()
    except Exception as e:
        logging.error(f"Ошибка получения подписки: {e}")
        return None

def add_subscription(user_id: int, tariff_key: str, duration_days: int = None):
    """Добавить подписку пользователю"""
    try:
        expires_at = None
        if duration_days is not None:
            expires_at = datetime.now() + timedelta(days=duration_days)
        
        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO subscriptions (user_id, tariff_key, expires_at) VALUES (:id, :key, :exp)"),
                {"id": user_id, "key": tariff_key, "exp": expires_at}
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка добавления подписки: {e}")
        return False

def extend_subscription(user_id: int, tariff_key: str, duration_days: int):
    """Продлить подписку (прибавить дни)"""
    try:
        with engine.connect() as conn:
            # Проверяем есть ли подписка
            sub = conn.execute(
                text("SELECT expires_at FROM subscriptions WHERE user_id = :id AND tariff_key = :key AND status = 'active'"),
                {"id": user_id, "key": tariff_key}
            ).fetchone()
            
            if sub and sub[0] is not None:
                # Если есть дата истечения - прибавляем дни
                new_expires = sub[0] + timedelta(days=duration_days)
                conn.execute(
                    text("UPDATE subscriptions SET expires_at = :exp WHERE user_id = :id AND tariff_key = :key AND status = 'active'"),
                    {"exp": new_expires, "id": user_id, "key": tariff_key}
                )
            else:
                # Если бессрочная или нет подписки - создаем новую
                expires_at = None if sub and sub[0] is None else datetime.now() + timedelta(days=duration_days)
                conn.execute(
                    text("INSERT INTO subscriptions (user_id, tariff_key, expires_at) VALUES (:id, :key, :exp) ON CONFLICT (user_id, tariff_key) DO UPDATE SET expires_at = :exp"),
                    {"id": user_id, "key": tariff_key, "exp": expires_at}
                )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка продления подписки: {e}")
        return False

def expire_subscription(user_id: int, tariff_key: str):
    """Пометить подписку как истекшую"""
    try:
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE subscriptions SET status = 'expired' WHERE user_id = :id AND tariff_key = :key"),
                {"id": user_id, "key": tariff_key}
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка отметки подписки: {e}")
        return False

def get_tariff_channel(tariff_key: str):
    """Получить настройки канала для тарифа"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT * FROM tariff_channels WHERE tariff_key = :key"),
                {"key": tariff_key}
            )
            return result.fetchone()
    except Exception as e:
        logging.error(f"Ошибка получения канала: {e}")
        return None

def set_tariff_channel(tariff_key: str, channel_id: str, invite_link: str):
    """Установить настройки канала для тарифа"""
    try:
        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO tariff_channels (tariff_key, channel_id, invite_link) VALUES (:key, :cid, :link) ON CONFLICT (tariff_key) DO UPDATE SET channel_id = :cid, invite_link = :link, updated_at = NOW()"),
                {"key": tariff_key, "cid": channel_id, "link": invite_link}
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка сохранения канала: {e}")
        return False

def create_subscription_key(tariff_key: str, duration_days: int = None, created_by: int = None) -> str:
    """Создать одноразовый ключ"""
    try:
        key = generate_key(32)
        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO subscription_keys (key, tariff_key, duration_days, created_by) VALUES (:key, :t, :dur, :creator)"),
                {"key": key, "t": tariff_key, "dur": duration_days, "creator": created_by}
            )
            conn.commit()
        return key
    except Exception as e:
        logging.error(f"Ошибка создания ключа: {e}")
        return None

def get_subscription_key(key: str):
    """Получить ключ по значению"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT * FROM subscription_keys WHERE key = :key"),
                {"key": key}
            )
            return result.fetchone()
    except Exception as e:
        logging.error(f"Ошибка получения ключа: {e}")
        return None

def delete_subscription_key(key: str):
    """Удалить ключ (после активации)"""
    try:
        with engine.connect() as conn:
            conn.execute(
                text("DELETE FROM subscription_keys WHERE key = :key"),
                {"key": key}
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка удаления ключа: {e}")
        return False

def create_promo_code(code: str, discount_percent: int, expires_minutes: int = None, created_by: int = None):
    """Создать промокод"""
    try:
        expires_at = None
        if expires_minutes is not None:
            expires_at = datetime.now() + timedelta(minutes=expires_minutes)
        
        with engine.connect() as conn:
            conn.execute(
                text("INSERT INTO promo_codes (code, discount_percent, expires_at, created_by) VALUES (:code, :disc, :exp, :creator) ON CONFLICT (code) DO UPDATE SET discount_percent = :disc, expires_at = :exp"),
                {"code": code.upper(), "disc": discount_percent, "exp": expires_at, "creator": created_by}
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка создания промокода: {e}")
        return False

def get_promo_code(code: str):
    """Получить промокод по названию"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT * FROM promo_codes WHERE code = :code"),
                {"code": code.upper()}
            )
            return result.fetchone()
    except Exception as e:
        logging.error(f"Ошибка получения промокода: {e}")
        return None

def delete_promo_code(code: str):
    """Удалить промокод"""
    try:
        with engine.connect() as conn:
            conn.execute(
                text("DELETE FROM promo_codes WHERE code = :code"),
                {"code": code.upper()}
            )
            conn.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка удаления промокода: {e}")
        return False

def get_all_promo_codes():
    """Получить все промокоды"""
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT * FROM promo_codes ORDER BY created_at DESC"))
            return result.fetchall()
    except Exception as e:
        logging.error(f"Ошибка получения промокодов: {e}")
        return []

def get_expired_subscriptions():
    """Получить все истекшие подписки"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT * FROM subscriptions WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < NOW()")
            )
            return result.fetchall()
    except Exception as e:
        logging.error(f"Ошибка получения истекших подписок: {e}")
        return []

def get_expiring_soon_subscriptions(days=3):
    """Получить подписки, истекающие через N дней"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT * FROM subscriptions WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at BETWEEN NOW() AND NOW() + INTERVAL :days DAY"),
                {"days": days}
            )
            return result.fetchall()
    except Exception as e:
        logging.error(f"Ошибка получения подписок: {e}")
        return []

def get_all_active_subscriptions():
    """Получить все активные подписки"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT * FROM subscriptions WHERE status = 'active' AND (expires_at IS NULL OR expires_at > NOW())")
            )
            return result.fetchall()
    except Exception as e:
        logging.error(f"Ошибка получения подписок: {e}")
        return []

def get_subscription_stats():
    """Получить статистику по подпискам"""
    try:
        with engine.connect() as conn:
            # Всего активных
            total = conn.execute(text("SELECT COUNT(*) FROM subscriptions WHERE status = 'active' AND (expires_at IS NULL OR expires_at > NOW())")).fetchone()[0] or 0
            
            # Истекают завтра
            tomorrow = datetime.now() + timedelta(days=1)
            expiring_tomorrow = conn.execute(
                text("SELECT COUNT(*) FROM subscriptions WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at BETWEEN NOW() AND :tomorrow"),
                {"tomorrow": tomorrow}
            ).fetchone()[0] or 0
            
            return {"total": total, "expiring_tomorrow": expiring_tomorrow}
    except Exception as e:
        logging.error(f"Ошибка статистики: {e}")
        return {"total": 0, "expiring_tomorrow": 0}

def get_all_channels():
    """Получить все настройки каналов"""
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT * FROM tariff_channels"))
            return result.fetchall()
    except Exception as e:
        logging.error(f"Ошибка получения каналов: {e}")
        return []

def get_all_subscription_keys():
    """Получить все ключи"""
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT * FROM subscription_keys ORDER BY created_at DESC"))
            return result.fetchall()
    except Exception as e:
        logging.error(f"Ошибка получения ключей: {e}")
        return []

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
# ID КАНАЛОВ (устаревшие, теперь хранятся в БД)
# ==================================================
CHANNEL_IDS = {
    "test": "-1003875225035",
}

# ==================================================
# БАЗА ДАННЫХ (SQLite - резервная, для совместимости)
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
        "price_rub": 1999,
        "price_stars": 1999,
        "duration_ru": "Бессрочно",
        "duration_en": "Forever",
        "duration_days": None,  # Бессрочно
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

# --- ПРОМОКОДЫ (захардкоженные, для совместимости) ---
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
    
    # Добавляем подписку
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
    """Создает счет в CryptoBot с суммой в USD"""
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
    """Округляет до ближайшего 0.5"""
    return round(value * 2) / 2

def get_tariff_name(tariff_key: str, lang: str = "ru"):
    tariff = TARIFFS.get(tariff_key)
    if tariff:
        return tariff['name_ru'] if lang == "ru" else tariff['name_en']
    return tariff_key

def format_date(date):
    if date is None:
        return "Бессрочно"
    return date.strftime("%d.%m.%Y")

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
    
    # Проверяем подписку в Supabase
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
        [InlineKeyboardButton(text=btn_card, callback_data=f"pay_card_{tariff_key}")],
        [InlineKeyboardButton(text=btn_stars, callback_data=f"pay_stars_{tariff_key}")],
        [InlineKeyboardButton(text=btn_crypto, callback_data=f"pay_crypto_{tariff_key}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")]
    ])

def get_crypto_currency_keyboard(tariff_key, discount_percent=0, lang="ru"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=LANG[lang]["btn_crypto_usdt"], callback_data=f"crypto_usdt_{tariff_key}"),
         InlineKeyboardButton(text=LANG[lang]["btn_crypto_ton"], callback_data=f"crypto_ton_{tariff_key}"),
         InlineKeyboardButton(text=LANG[lang]["btn_crypto_btc"], callback_data=f"crypto_btc_{tariff_key}")],
        [InlineKeyboardButton(text=LANG[lang]["btn_crypto_direct"], callback_data=f"crypto_direct_{tariff_key}")],
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
    """Клавиатура для раздела Мои подписки"""
    buttons = []
    for sub in subscriptions:
        tariff_key = sub[2]  # tariff_key
        name = get_tariff_name(tariff_key, lang)
        buttons.append([InlineKeyboardButton(text=name, callback_data=f"access_{tariff_key}")])
    buttons.append([InlineKeyboardButton(text=LANG[lang]["btn_back"], callback_data="back_to_prices")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_access_keyboard(tariff_key, lang="ru"):
    """Клавиатура для доступа к каналу"""
    tariff_channel = get_tariff_channel(tariff_key)
    invite_link = tariff_channel[2] if tariff_channel else None
    
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
    """Автоматическое принятие заявок в канал"""
    user_id = update.from_user.id
    chat_id = update.chat.id
    
    logging.info(f"📥 Заявка от {user_id} в канал {chat_id}")
    
    # Проверяем есть ли подписка у пользователя на этот канал
    # Находим tariff_key по channel_id
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT tariff_key FROM tariff_channels WHERE channel_id = :cid"),
                {"cid": str(chat_id)}
            ).fetchone()
            
            if result:
                tariff_key = result[0]
                
                # Проверяем подписку
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
    
    add_user(user_id, first_name, username)
    
    # Проверяем, может это активация ключа
    if message.text and " " in message.text:
        parts = message.text.split()
        if len(parts) > 1 and parts[0] == "/start":
            key_param = parts[1]
            if key_param.startswith("key_"):
                await process_key_activation(message, key_param, state)
                return
    
    lang = await get_lang(state)
    
    welcome_text = f"""👋 Привет, {first_name}!
Ты попал в наш бот✅

Нажимая на каждый тариф ты видишь краткое описание.

Если бот не доступен пиши мне

Тех.поддержка: @kasgd"""
    
    await message.answer(welcome_text, disable_web_page_preview=True)
    
    menu_text = LANG[lang]["main_menu_text"]
    await message.answer(menu_text, reply_markup=get_tariff_keyboard(lang))

async def process_key_activation(message: Message, key_param: str, state: FSMContext):
    """Обработка активации ключа"""
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    lang = await get_lang(state)
    
    # Проверяем ключ в базе
    key_data = get_subscription_key(key_param)
    
    if not key_data:
        await message.answer(LANG[lang]["key_not_found"])
        return
    
    # Получаем данные ключа
    tariff_key = key_data[2]  # tariff_key
    duration_days = key_data[3]  # duration_days
    
    tariff = TARIFFS.get(tariff_key)
    tariff_name = get_tariff_name(tariff_key, lang)
    
    # Проверяем есть ли уже подписка на этот тариф
    existing_sub = get_subscription_by_tariff(user_id, tariff_key)
    
    if existing_sub:
        # Продлеваем
        if duration_days is not None:
            extend_subscription(user_id, tariff_key, duration_days)
            expires_at = datetime.now() + timedelta(days=duration_days)
        else:
            # Бессрочно - ничего не меняем, но обновим статус
            pass
    else:
        # Создаем новую подписку
        add_subscription(user_id, tariff_key, duration_days)
    
    # Удаляем ключ
    delete_subscription_key(key_param)
    
    # Получаем обновленную подписку для отображения
    sub = get_subscription_by_tariff(user_id, tariff_key)
    expires_at = sub[4] if sub else None
    
    # Отправляем сообщение пользователю
    text = LANG[lang]["key_activated"].format(
        tariff_name=tariff_name,
        expires_at=format_date(expires_at)
    )
    await message.answer(text)
    
    # Уведомляем админов
    user_link = f"<a href='tg://user?id={user_id}'>{username}</a>"
    admin_text = LANG[lang]["key_activated_admin"].format(
        user_link=user_link,
        user_id=user_id,
        tariff_name=tariff_name,
        expires_at=format_date(expires_at)
    )
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
# АДМИН: УПРАВЛЕНИЕ ТАРИФАМИ (ССЫЛКИ КАНАЛОВ)
# ==================================================

@dp.callback_query(F.data == "admin_channels")
async def admin_channels(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Только для админов!", show_alert=True)
        return
    
    await callback.answer()
    
    channels = get_all_channels()
    tariff_list = TARIFFS
    
    text = "📋 <b>Управление ссылками каналов</b>\n\n"
    text += "Для каждого тарифа можно настроить ссылку на канал с заявками.\n\n"
    
    for key, tariff in tariff_list.items():
        channel = get_tariff_channel(key)
        if channel and channel[2]:
            status = "✅ настроен"
            link_preview = channel[2][:30] + "..." if len(channel[2]) > 30 else channel[2]
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
    
    # Создаем клавиатуру с выбором тарифа
    buttons = []
    for key, tariff in TARIFFS.items():
        channel = get_tariff_channel(key)
        status = "✅" if channel and channel[2] else "❌"
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
    
    if channel and channel[1]:
        text += f"🆔 ID канала: <code>{channel[1]}</code>\n"
    else:
        text += "🆔 ID канала: ❌ не задан\n"
    
    if channel and channel[2]:
        text += f"🔗 Ссылка-приглашение: {channel[2]}\n"
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
    
    # Сохраняем в базу
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
    
    # Создаем клавиатуру с выбором тарифа
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
        ("Бессрочно", None)
    ]
    
    buttons = []
    for label, days in duration_options:
        buttons.append([InlineKeyboardButton(
            text=label, 
            callback_data=f"admin_key_days_{days if days is not None else '0'}"
        )])
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
    duration_days = None if days_str == "0" else int(days_str)
    
    data = await state.get_data()
    tariff_key = data.get("admin_key_tariff")
    
    # Создаем ключ
    key = create_subscription_key(tariff_key, duration_days, callback.from_user.id)
    
    if key:
        tariff = TARIFFS[tariff_key]
        link = f"https://t.me/{bot.username}?start={key}"
        
        text = f"✅ <b>Ключ создан!</b>\n\n"
        text += f"📋 Тариф: {tariff['name_ru']}\n"
        text += f"📅 Срок: {'Бессрочно' if duration_days is None else f'{duration_days} дней'}\n"
        text += f"🔑 Ключ: <code>{key}</code>\n"
        text += f"🔗 Ссылка: {link}\n\n"
        text += "⚠️ Ключ одноразовый. После активации будет удален."
        
        await callback.message.edit_text(text)
    else:
        await callback.message.edit_text("❌ Ошибка создания ключа. Попробуйте позже.")
    
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
            code = pc[2]
            discount = pc[3]
            expires = pc[4]
            status = "✅ активен" if (expires is None or expires > datetime.now()) else "❌ истек"
            text += f"• <b>{code}</b> - {discount}% ({status})\n"
            if expires:
                text += f"  До: {expires.strftime('%d.%m.%Y %H:%M')}\n"
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
    
    # Создаем промокод
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
        code = pc[2]
        buttons.append([InlineKeyboardButton(
            text=f"🗑️ {code} ({pc[3]}%)", 
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
# АДМИН: РАССЫЛКА (УЖЕ ЕСТЬ)
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
    
    # Подсчет ключей
    with engine.connect() as conn:
        keys_count = conn.execute(text("SELECT COUNT(*) FROM subscription_keys")).fetchone()[0] or 0
    
    # Подсчет промокодов
    with engine.connect() as conn:
        promo_count = conn.execute(text("SELECT COUNT(*) FROM promo_codes")).fetchone()[0] or 0
    
    text = f"""📊 <b>Статистика бота</b>

👥 Всего пользователей: {user_count}
📋 Активных подписок: {stats['total']}
⏳ Истекают завтра: {stats['expiring_tomorrow']}
🔑 Создано ключей: {keys_count}
🏷️ Активных промокодов: {promo_count}

📌 <b>Статус бота:</b>
✅ Supabase подключена
✅ SQLite работает
✅ CryptoBot {'✅' if CRYPTOBOT_API_KEY else '❌'}"""

    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())

# ==================================================
# ОБРАБОТЧИКИ ОПЛАТ (ОСТАЛИСЬ БЕЗ ИЗМЕНЕНИЙ)
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
    lang = await get_lang(state)
    user_id = callback.from_user.id
    
    subscriptions = get_active_subscriptions(user_id)
    
    if subscriptions:
        text = "📋 <b>Ваши активные подписки</b>\n\nВыберите доступ:"
        await callback.message.edit_text(text, reply_markup=get_subscription_keyboard(subscriptions, lang))
    else:
        await callback.message.edit_text(LANG[lang]["no_subs"])
        await callback.message.answer(LANG[lang]["main_menu_text"], reply_markup=get_tariff_keyboard(lang))

# ==================================================
# ПРОМОКОДЫ (ОБНОВЛЕННЫЕ)
# ==================================================

@dp.message(Command("promo"))
async def cmd_promo(message: Message, state: FSMContext):
    """Ввод промокода"""
    lang = await get_lang(state)
    await state.update_data(waiting_for_promo=True)
    await message.answer(LANG[lang]["enter_promo"])
    await state.set_state(PromoStates.waiting_for_promo)

@dp.message(PromoStates.waiting_for_promo)
async def process_promo(message: Message, state: FSMContext):
    promo_code = message.text.strip().upper()
    data = await state.get_data()
    tariff_key = data.get("current_tariff")
    lang = await get_lang(state)
    
    # Проверяем в базе данных
    promo = get_promo_code(promo_code)
    
    if not promo:
        # Проверяем в захардкоженных
        if promo_code in PROMO_CODES:
            discount = PROMO_CODES[promo_code]
            await state.update_data(discount=discount, current_tariff=tariff_key)
            
            if tariff_key and tariff_key in TARIFFS:
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
                await message.answer("❌ Сначала выберите тариф.")
                await state.clear()
            return
        else:
            await message.answer(LANG[lang]["promo_fail"])
            return
    
    # Проверяем срок действия
    expires_at = promo[4]
    if expires_at and expires_at < datetime.now():
        await message.answer(LANG[lang]["promo_expired"])
        return
    
    discount = promo[3]
    await state.update_data(discount=discount, current_tariff=tariff_key)
    
    if tariff_key and tariff_key in TARIFFS:
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
        await message.answer("❌ Сначала выберите тариф.")
        await state.clear()

# ==================================================
# ЗАПУСК (С ПЕРИОДИЧЕСКИМИ ЗАДАЧАМИ)
# ==================================================

async def check_expired_subscriptions():
    """Проверка истекших подписок (каждые 12 часов)"""
    while True:
        try:
            logging.info("🔄 Проверка истекших подписок...")
            
            # Проверяем истекшие
            expired = get_expired_subscriptions()
            for sub in expired:
                user_id = sub[1]
                tariff_key = sub[2]
                tariff_name = get_tariff_name(tariff_key, "ru")
                
                # Получаем канал
                channel = get_tariff_channel(tariff_key)
                if channel and channel[1]:
                    try:
                        chat_id = int(channel[1])
                        await bot.ban_chat_member(chat_id, user_id)
                        # Разбаниваем чтобы мог перезайти если оплатит
                        await bot.unban_chat_member(chat_id, user_id)
                        logging.info(f"✅ Кикнут пользователь {user_id} из канала {chat_id}")
                    except Exception as e:
                        logging.error(f"Ошибка кика: {e}")
                
                # Помечаем подписку как истекшую
                expire_subscription(user_id, tariff_key)
                
                # Отправляем уведомление
                try:
                    text = LANG["ru"]["subscription_expired"].format(tariff_name=tariff_name)
                    await bot.send_message(user_id, text)
                except Exception as e:
                    logging.error(f"Ошибка отправки уведомления: {e}")
            
            # Проверяем подписки, истекающие через 3 дня
            expiring_soon = get_expiring_soon_subscriptions(3)
            for sub in expiring_soon:
                user_id = sub[1]
                tariff_key = sub[2]
                tariff_name = get_tariff_name(tariff_key, "ru")
                
                # Отправляем напоминание
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
        
        # Ждем 12 часов
        await asyncio.sleep(43200)  # 12 часов

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    
    if not BOT_TOKEN:
        logging.error("❌ BOT_TOKEN не задан в переменных окружения!")
        return
    
    print("=" * 60)
    print("🚀 ОСНОВНОЙ БОТ ЗАПУЩЕН!")
    print("📦 База данных: Supabase + SQLite")
    print(f"🪙 CRYPTO_TOKEN: {'✅' if CRYPTOBOT_API_KEY else '❌'}")
    print(f"🗄️ SUPABASE: {'✅' if SUPABASE_URL else '❌'}")
    print("📞 Поддержка: @kasgd")
    print("👥 Админы: " + ", ".join(str(admin) for admin in ADMIN_IDS))
    print("=" * 60)
    
    # Запускаем периодическую проверку подписок
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

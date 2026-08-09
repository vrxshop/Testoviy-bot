import asyncio
import aiohttp
import os

# Берем ключ из переменных окружения
CRYPTOBOT_API_KEY = os.getenv("CRYPTO_TOKEN")
# ПРАВИЛЬНЫЙ URL для CryptoBot API
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api/"

async def test_crypto_api():
    """Тест API ключа из переменных"""
    print("🔍 Тестируем CryptoBot API...")
    print(f"🔑 Ключ из переменных: {CRYPTOBOT_API_KEY[:10]}..." if CRYPTOBOT_API_KEY else "❌ Ключ НЕ НАЙДЕН!")
    
    if not CRYPTOBOT_API_KEY:
        print("❌ Переменная CRYPTO_TOKEN не найдена!")
        return False
    
    # ПРАВИЛЬНЫЙ эндпоинт - getMe
    url = CRYPTOBOT_API_URL + "getMe"
    headers = {
        "Crypto-Pay-API-Token": CRYPTOBOT_API_KEY,
        "Content-Type": "application/json"
    }
    
    try:
        print(f"📤 Запрос к: {url}")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers) as response:
                text = await response.text()
                print(f"📥 Ответ (первые 200 символов): {text[:200]}")
                
                try:
                    data = await response.json()
                    if data.get("ok"):
                        print("✅ API ключ РАБОТАЕТ!")
                        print(f"   Бот: @{data['result']['username']}")
                        return True
                    else:
                        print(f"❌ API ключ НЕ РАБОТАЕТ!")
                        print(f"   Ошибка: {data}")
                        return False
                except:
                    print("❌ Ответ не в формате JSON!")
                    print(f"   Текст ответа: {text[:500]}")
                    return False
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False

async def create_test_invoice():
    """Создает тестовый счет на 1 USDT"""
    print("\n🔄 Создаем тестовый счет...")
    
    url = CRYPTOBOT_API_URL + "createInvoice"
    headers = {
        "Crypto-Pay-API-Token": CRYPTOBOT_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "asset": "USDT",
        "amount": "1",
        "description": "Тестовый платеж",
        "paid_btn_name": "openChannel",
        "paid_btn_url": "https://t.me/kasgd",
        "payload": "test_123"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as response:
                data = await response.json()
                print(f"📥 Ответ: {data}")
                
                if data.get("ok"):
                    result = data["result"]
                    print(f"✅ Счет создан!")
                    print(f"   ID: {result['invoice_id']}")
                    print(f"   Ссылка: {result['pay_url']}")
                    return True
                else:
                    print(f"❌ Ошибка: {data}")
                    return False
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False

async def main():
    print("=" * 50)
    print("🧪 ТЕСТ CRYPTOBOT API (ПРАВИЛЬНЫЙ URL)")
    print(f"📌 Переменная: CRYPTO_TOKEN")
    print(f"📌 URL: {CRYPTOBOT_API_URL}")
    print("=" * 50)
    
    # Тест 1: проверка ключа
    api_ok = await test_crypto_api()
    
    if api_ok:
        # Тест 2: создание счета
        await create_test_invoice()
    else:
        print("\n❌ Невозможно создать счет - API ключ не работает!")
        print("   Проверь:")
        print("   1. Переменная CRYPTO_TOKEN добавлена в Render")
        print("   2. Значение ключа правильное (без лишних пробелов)")

if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import os
import random
import string
import logging
import html
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
import httpx

# Токен теперь безопасно берется из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_URL = "https://api.mail.tm"

# Временная "база данных" в оперативной памяти (user_id -> {"address": "...", "token": "..."})
users_db = {}

# Инициализация бота и диспетчера
if not BOT_TOKEN:
    logging.error("Переменная окружения BOT_TOKEN не задана!")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# --- Вспомогательные функции для работы с API Mail.tm ---

async def get_domain():
    """Получает доступный домен для регистрации"""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_URL}/domains")
        data = response.json()
        return data["hydra:member"][0]["domain"]


async def create_account(address, password):
    """Создает почтовый ящик"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_URL}/accounts",
            json={"address": address, "password": password}
        )
        return response.status_code in [200, 201]


async def get_token(address, password):
    """Получает JWT токен для авторизации"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_URL}/token",
            json={"address": address, "password": password}
        )
        if response.status_code == 200:
            return response.json()["token"]
        return None


async def get_messages(token):
    """Получает список входящих писем"""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{API_URL}/messages",
            headers={"Authorization": f"Bearer {token}"}
        )
        return response.json().get("hydra:member", [])


async def get_message(token, msg_id):
    """Получает содержимое конкретного письма"""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{API_URL}/messages/{msg_id}",
            headers={"Authorization": f"Bearer {token}"}
        )
        return response.json()


def generate_random_string(length=10):
    """Генератор случайной строки для логина и пароля"""
    letters = string.ascii_lowercase + string.digits
    return ''.join(random.choice(letters) for _ in range(length))


# --- Клавиатуры ---

def main_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="🆕 Создать ящик", callback_data="create_mail")
    builder.button(text="📥 Проверить входящие", callback_data="check_mail")
    builder.adjust(1)
    return builder.as_markup()


# --- Обработчики команд и событий Telegram ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот для работы с временной почтой.\n\n"
        "Выберите нужное действие:",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data == "create_mail")
async def process_create_mail(callback: types.CallbackQuery):
    await callback.message.edit_text("⏳ Генерирую адрес и регистрирую ящик...")

    try:
        domain = await get_domain()
        username = generate_random_string(8)
        password = generate_random_string(12)
        address = f"{username}@{domain}"

        if await create_account(address, password):
            token = await get_token(address, password)
            # Сохраняем данные пользователя в словарь
            users_db[callback.from_user.id] = {"address": address, "token": token}

            await callback.message.edit_text(
                f"✅ <b>Ваш новый почтовый ящик готов!</b>\n\n"
                f"📧 <b>Адрес:</b> <code>{address}</code>\n"
                f"🔑 <b>Пароль:</b> <code>{password}</code>\n\n"
                f"Ящик привязан к вашему аккаунту.",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
        else:
            await callback.message.edit_text("❌ Ошибка при создании ящика.", reply_markup=main_menu())
    except Exception as e:
        await callback.message.edit_text(f"❌ Произошла ошибка: {e}", reply_markup=main_menu())


@dp.callback_query(F.data == "check_mail")
async def process_check_mail(callback: types.CallbackQuery):
    user_data = users_db.get(callback.from_user.id)
    if not user_data:
        await callback.answer("У вас нет активного ящика. Сначала создайте его!", show_alert=True)
        return

    await callback.message.edit_text("⏳ Проверяю входящие...")
    token = user_data["token"]

    try:
        messages = await get_messages(token)
        if not messages:
            await callback.message.edit_text(
                f"📬 Входящих писем пока нет.\n\nТекущий ящик: <code>{user_data['address']}</code>",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
            return

        builder = InlineKeyboardBuilder()
        text = f"📬 <b>Входящие письма</b> (последние 5):\n\n"

        # Показываем только последние 5 писем
        for idx, msg in enumerate(messages[:5]):
            sender = html.escape(msg['from']['address'])
            subject = html.escape(msg.get('subject', 'Без темы'))
            text += f"{idx + 1}. <b>От:</b> <code>{sender}</code>\n<b>Тема:</b> {subject}\n\n"
            builder.button(text=f"Читать письмо {idx + 1}", callback_data=f"read_{msg['id']}")

        builder.button(text="🔙 Назад", callback_data="back_to_main")
        builder.adjust(1)

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    except Exception as e:
        await callback.message.edit_text(f"❌ Произошла ошибка при получении писем: {e}", reply_markup=main_menu())


@dp.callback_query(F.data.startswith("read_"))
async def process_read_mail(callback: types.CallbackQuery):
    msg_id = callback.data.split("_")[1]
    user_data = users_db.get(callback.from_user.id)

    if not user_data:
        await callback.answer("Сессия истекла. Создайте новый ящик.", show_alert=True)
        return

    await callback.message.edit_text("⏳ Загружаю письмо...")
    try:
        msg = await get_message(user_data["token"], msg_id)

        subject = html.escape(msg.get("subject", "Без темы"))
        from_email = html.escape(msg.get("from", {}).get("address", "Неизвестен"))

        raw_body = msg.get("text", "")
        body = html.escape(
            raw_body) if raw_body else "<i>Текст отсутствует (возможно, письмо только в HTML-формате).</i>"

        # Ограничение Telegram на длину сообщения
        if len(body) > 3000:
            body = body[:3000] + "\n\n...[ТЕКСТ ОБРЕЗАН]..."

        text = (
            f"📨 <b>От:</b> <code>{from_email}</code>\n"
            f"📝 <b>Тема:</b> {subject}\n"
            f"〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
            f"{body}"
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 К списку писем", callback_data="check_mail")

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка загрузки письма: {e}", reply_markup=main_menu())


@dp.callback_query(F.data == "back_to_main")
async def process_back(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "👋 Вы вернулись в главное меню.\n\nВыберите действие ниже:",
        reply_markup=main_menu()
    )


# --- Интеграция с веб-сервером для Render (Health Check) ---

async def health_check(request):
    """Ответ для пингеров, чтобы Render не усыплял сервис"""
    return web.Response(text="Bot is running 24/7!")


async def main():
    # Настройка логирования
    logging.basicConfig(level=logging.INFO)

    # 1. Настройка и запуск веб-сервера aiohttp
    app = web.Application()
    app.router.add_get('/', health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    # Render автоматически передает порт в переменную окружения PORT
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Web server successfully started on port {port}")

    # 2. Запуск долгого опроса (polling) бота Telegram
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
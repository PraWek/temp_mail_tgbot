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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import httpx

# Токен безопасно берется из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_URL = "https://api.mail.tm"

# Временная "база данных" в оперативной памяти (user_id -> {"address": "...", "token": "..."})
users_db = {}

if not BOT_TOKEN:
    logging.error("Переменная окружения BOT_TOKEN не задана!")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# --- FSM Состояния для входа ---
class LoginState(StatesGroup):
    waiting_for_address = State()
    waiting_for_password = State()


# --- Вспомогательные функции для работы с API Mail.tm ---

async def get_domain():
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_URL}/domains")
        data = response.json()
        return data["hydra:member"][0]["domain"]


async def create_account(address, password):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_URL}/accounts",
            json={"address": address, "password": password}
        )
        return response.status_code in [200, 201]


async def get_token(address, password):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_URL}/token",
            json={"address": address, "password": password}
        )
        if response.status_code == 200:
            return response.json()["token"]
        return None


async def get_messages(token):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{API_URL}/messages",
            headers={"Authorization": f"Bearer {token}"}
        )
        return response.json().get("hydra:member", [])


async def get_message(token, msg_id):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{API_URL}/messages/{msg_id}",
            headers={"Authorization": f"Bearer {token}"}
        )
        return response.json()


def generate_random_string(length=10):
    letters = string.ascii_lowercase + string.digits
    return ''.join(random.choice(letters) for _ in range(length))


# --- Клавиатуры ---

def main_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="🆕 Создать ящик", callback_data="create_mail")
    builder.button(text="🔑 Войти в ящик", callback_data="login_mail")
    builder.button(text="📥 Проверить входящие", callback_data="check_mail")
    builder.adjust(1)
    return builder.as_markup()


def cancel_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="cancel_action")
    return builder.as_markup()


# --- Обработчики команд и событий Telegram ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот для работы с временной почтой.\n\n"
        "Выберите нужное действие:",
        reply_markup=main_menu()
    )


# Обработчик отмены любого действия (FSM)
@dp.callback_query(F.data == "cancel_action")
async def process_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🚫 Действие отменено.",
        reply_markup=main_menu()
    )


# === БЛОК СОЗДАНИЯ ЯЩИКА ===

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
            users_db[callback.from_user.id] = {"address": address, "token": token}

            await callback.message.edit_text(
                f"✅ <b>Ваш новый почтовый ящик готов!</b>\n\n"
                f"📧 <b>Адрес:</b> <code>{address}</code>\n"
                f"🔑 <b>Пароль:</b> <code>{password}</code>\n\n"
                f"<i>⚠️ Обязательно сохраните пароль, чтобы войти в ящик позже!</i>",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
        else:
            await callback.message.edit_text("❌ Ошибка при создании ящика.", reply_markup=main_menu())
    except Exception as e:
        await callback.message.edit_text(f"❌ Произошла ошибка: {e}", reply_markup=main_menu())


# === БЛОК АВТОРИЗАЦИИ (ВХОДА) ===

@dp.callback_query(F.data == "login_mail")
async def process_login_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Введите адрес вашей почты (например, <code>example@domain.com</code>):",
        parse_mode="HTML",
        reply_markup=cancel_menu()
    )
    await state.set_state(LoginState.waiting_for_address)


@dp.message(LoginState.waiting_for_address)
async def process_login_address(message: types.Message, state: FSMContext):
    address = message.text.strip()
    await state.update_data(address=address)

    await message.answer(
        f"📧 Адрес: <b>{address}</b>\n\nТеперь введите пароль от ящика:",
        parse_mode="HTML",
        reply_markup=cancel_menu()
    )
    await state.set_state(LoginState.waiting_for_password)


@dp.message(LoginState.waiting_for_password)
async def process_login_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    user_data = await state.get_data()
    address = user_data["address"]

    # Удаляем сообщение с паролем в целях безопасности (если бот имеет права)
    try:
        await message.delete()
    except Exception:
        pass

    msg = await message.answer("⏳ Авторизация...")

    token = await get_token(address, password)

    if token:
        # Успешный вход
        users_db[message.from_user.id] = {"address": address, "token": token}
        await msg.edit_text(
            f"✅ <b>Вы успешно вошли в почту!</b>\n\n"
            f"Текущий ящик: <code>{address}</code>",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    else:
        # Ошибка входа
        await msg.edit_text(
            "❌ <b>Неверный адрес или пароль.</b>\nПожалуйста, попробуйте снова.",
            parse_mode="HTML",
            reply_markup=main_menu()
        )

    await state.clear()


# === БЛОК ПРОВЕРКИ И ЧТЕНИЯ ПИСЕМ ===

@dp.callback_query(F.data == "check_mail")
async def process_check_mail(callback: types.CallbackQuery):
    user_data = users_db.get(callback.from_user.id)
    if not user_data:
        await callback.answer("У вас нет активного ящика. Создайте его или войдите!", show_alert=True)
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
        await callback.answer("Сессия истекла. Войдите в ящик заново.", show_alert=True)
        return

    await callback.message.edit_text("⏳ Загружаю письмо...")
    try:
        msg = await get_message(user_data["token"], msg_id)

        subject = html.escape(msg.get("subject", "Без темы"))
        from_email = html.escape(msg.get("from", {}).get("address", "Неизвестен"))

        raw_body = msg.get("text", "")
        body = html.escape(
            raw_body) if raw_body else "<i>Текст отсутствует (возможно, письмо только в HTML-формате).</i>"

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


# --- Интеграция с веб-сервером для Render ---

async def health_check(request):
    return web.Response(text="Bot is running 24/7!")


async def main():
    logging.basicConfig(level=logging.INFO)

    app = web.Application()
    app.router.add_get('/', health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Web server successfully started on port {port}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
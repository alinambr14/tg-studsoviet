import os
import io
import re
import json
import logging
import asyncio
import requests

from PIL import Image, ImageDraw, ImageFont
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
# httpx в INFO-режиме печатает в лог полный адрес запроса вместе с токеном бота — отключаем
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

load_dotenv()

# Состояния диалога
QUEST_NAME, PARTICIPANTS_COUNT, REQUIRED_COUNT = range(3)

# Константы для генерации обложки
IMAGE_WIDTH = 1400
IMAGE_HEIGHT = 350
BACKGROUND_COLOR = (16, 53, 71)  # Тёмно-синий
TEXT_COLOR = (248, 241, 222)  # Бежевый / Белый
ACCENT_COLOR = (253, 188, 58)  # Золотой / Жёлтый

# Файл для сохранения разрешённых пользователей.
# На Railway его нужно хранить на подключённом Volume (постоянный диск),
# иначе файл стирается при каждом новом деплое.
DATA_DIR = os.environ.get('DATA_DIR') or os.environ.get('RAILWAY_VOLUME_MOUNT_PATH') or '.'
os.makedirs(DATA_DIR, exist_ok=True)
USERS_FILE = os.path.join(DATA_DIR, 'allowed_users.json')
ADMIN_USER_ID = int(os.environ.get('ADMIN_USER_ID', 0))  # ID главного администратора


def load_allowed_users() -> set:
    """Загружает список разрешенных пользователей из файла или .env"""
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                users = json.load(f)
                return set(int(u) for u in users)
        except Exception as e:
            logger.error(f"Ошибка при чтении {USERS_FILE}: {e}")

    # Резервный вариант: из переменной окружения
    env_users = os.environ.get('ALLOWED_USERS', '').split(',')
    parsed_users = {int(u.strip()) for u in env_users if u.strip().isdigit()}
    if ADMIN_USER_ID:
        parsed_users.add(ADMIN_USER_ID)

    # Сохраняем исходный список во внешнем файле
    save_allowed_users(parsed_users)
    return parsed_users


def save_allowed_users(users: set) -> None:
    """Сохраняет список разрешенных пользователей в файл"""
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(users), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка при сохранении {USERS_FILE}: {e}")


# Глобальный сет авторизованных пользователей
ALLOWED_USERS = load_allowed_users()


def check_access(user_id: int) -> bool:
    """Проверка прав доступа к функции создания формы"""
    return user_id in ALLOWED_USERS or user_id == ADMIN_USER_ID


async def access_denied(update: Update) -> None:
    """Сообщение об отказе в доступе"""
    await update.message.reply_text(
        "Доступ запрещен.\n\n"
        "Обратитесь к администратору для получения доступа."
    )


MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [["📝 Создать форму"]],
    resize_keyboard=True,
    is_persistent=True
)


def split_text_to_lines(text: str) -> tuple:
    """Форматирование и разделение текста для картинки"""
    text = text.replace('"', '«', 1)
    text = text.replace('"', '»', 1)

    if '«' in text and '»' in text:
        before_quotes = text[:text.index('«')].strip().upper()
        quoted_part = text[text.index('«'):text.index('»') + 1].upper()
        return before_quotes, quoted_part
    else:
        words = text.split()
        if len(words) <= 1:
            return "КВЕСТ", text.upper()

        mid = len(words) // 2
        line1 = " ".join(words[:mid]).upper()
        line2 = " ".join(words[mid:]).upper()
        return line1, line2


async def send_image_as_both_photo_and_file(update: Update, image_buffer: io.BytesIO, quest_name: str,
                                            caption: str = ""):
    """Отправка сгенерированного изображения файлом и картинкой"""
    safe_name = re.sub(r'[^\w\s-]', '', quest_name)
    safe_name = re.sub(r'[-\s]+', '_', safe_name).strip('-_')

    if len(safe_name) > 50:
        safe_name = safe_name[:50]

    filename = f"{safe_name}.png"

    image_buffer.seek(0)
    await update.message.reply_photo(
        photo=image_buffer,
        caption=caption
    )

    image_buffer.seek(0)
    await update.message.reply_document(
        document=image_buffer,
        filename=filename,
        caption="📎 А это тот же файл в формате PNG для скачивания"
    )


def generate_quest_image(quest_name: str) -> io.BytesIO:
    """Генерация баннера формы"""
    img = Image.new('RGB', (IMAGE_WIDTH, IMAGE_HEIGHT), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(img)

    try:
        font_subtitle = ImageFont.truetype("Montserrat-Black.ttf", 58.3)
        font_main = ImageFont.truetype("Montserrat-Black.ttf", 95)
        font_footer = ImageFont.truetype("Montserrat-Bold.ttf", 37.23)
    except Exception:
        font_subtitle = ImageFont.truetype("arialbd.ttf", 50)
        font_main = ImageFont.truetype("arialbd.ttf", 95)
        font_footer = ImageFont.truetype("arial.ttf", 28)

    draw.text(
        (IMAGE_WIDTH / 2, 25),
        "РЕГИСТРАЦИЯ",
        font=font_subtitle,
        fill=TEXT_COLOR,
        anchor="mt"
    )

    line1, line2 = split_text_to_lines(quest_name)
    draw.text(
        (IMAGE_WIDTH / 2, 120),
        f'НА {line1.upper()}',
        font=font_main,
        fill=TEXT_COLOR,
        anchor="mt"
    )

    curr_size = 95
    while True:
        try:
            q_font = ImageFont.truetype("Montserrat-Black.ttf", curr_size)
        except Exception:
            q_font = ImageFont.truetype("arialbd.ttf", curr_size)

        bbox = draw.textbbox((0, 0), line2.upper(), font=q_font)
        text_width = bbox[2] - bbox[0]

        if text_width < IMAGE_WIDTH - 100 or curr_size <= 30:
            break
        curr_size -= 2.5

    draw.text(
        (IMAGE_WIDTH / 2, 225),
        line2.upper(),
        font=q_font,
        fill=ACCENT_COLOR,
        anchor="mt"
    )

    # Подпись по краям
    draw.text((30.5, 30), "bsuirhostel.5", font=font_footer, fill=(76, 107, 125, 80), anchor="lt")
    draw.text((1118.33, 30), "bsuirhostel.5", font=font_footer, fill=(76, 107, 125, 80), anchor="lt")

    img_io = io.BytesIO()
    img.save(img_io, format='PNG')
    img_io.seek(0)
    return img_io


def call_google_script(quest_name: str, count: int, req_count: int) -> dict:
    """Вызов скрипта Google Apps Script"""
    script_url = os.environ.get('GOOGLE_SCRIPT_URL')
    folder_id = os.environ.get('GOOGLE_DRIVE_FOLDER_ID')

    if not script_url:
        raise ValueError("Нет URL скрипта GOOGLE_SCRIPT_URL")

    payload = {
        "questName": quest_name,
        "count": count,
        "required_count": req_count,
        "folderId": folder_id
    }

    response = requests.post(script_url, json=payload)
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"HTTP {response.status_code}: {response.text}")


# --- Команды администратора ---

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню команд администратора"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда только для главного администратора.")
        return

    user_list = "\n".join([f"• <code>{u}</code>" for u in ALLOWED_USERS]) if ALLOWED_USERS else "Список пуст"

    await update.message.reply_text(
        f"🛠️ <b>Административные команды:</b>\n\n"
        f"<b>Текущие пользователи:</b>\n{user_list}\n\n"
        f"<b>Управление:</b>\n"
        f"/users — Показать список пользователей\n"
        f"/add_user <code>&lt;id&gt;</code> — Добавить доступ по Telegram ID\n"
        f"/del_user <code>&lt;id&gt;</code> — Удалить доступ",
        parse_mode='HTML',
        reply_markup=MAIN_MENU_KEYBOARD
    )


async def show_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вывод всех разрешенных пользователей"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда только для главного администратора.")
        return

    if not ALLOWED_USERS:
        await update.message.reply_text("Список пользователей пуст.", reply_markup=MAIN_MENU_KEYBOARD)
        return

    user_list = "\n".join([f"• <code>{user_id}</code>" for user_id in ALLOWED_USERS])
    await update.message.reply_text(
        f"📋 <b>Разрешенные пользователи ({len(ALLOWED_USERS)}):</b>\n\n{user_list}",
        parse_mode='HTML',
        reply_markup=MAIN_MENU_KEYBOARD
    )


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавление нового пользователя в список доступа"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда только для главного администратора.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Укажите Telegram ID.\nПример: <code>/add_user 123456789</code>",
                                        parse_mode='HTML')
        return

    try:
        new_id = int(context.args[0])
        if new_id in ALLOWED_USERS:
            await update.message.reply_text(f"Пользователь <code>{new_id}</code> уже в списке.", parse_mode='HTML')
            return

        ALLOWED_USERS.add(new_id)
        save_allowed_users(ALLOWED_USERS)
        await update.message.reply_text(f"✅ Пользователь <code>{new_id}</code> успешно добавлен!", parse_mode='HTML')
    except ValueError:
        await update.message.reply_text("❌ Ошибка: ID должен быть числом.")


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление пользователя из списка доступа"""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда только для главного администратора.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Укажите Telegram ID.\nПример: <code>/del_user 123456789</code>",
                                        parse_mode='HTML')
        return

    try:
        target_id = int(context.args[0])
        if target_id == ADMIN_USER_ID:
            await update.message.reply_text("❌ Нельзя удалить самого главного администратора.")
            return

        if target_id in ALLOWED_USERS:
            ALLOWED_USERS.remove(target_id)
            save_allowed_users(ALLOWED_USERS)
            await update.message.reply_text(f"🗑 Пользователь <code>{target_id}</code> удалён из списка.",
                                            parse_mode='HTML')
        else:
            await update.message.reply_text("Пользователь не найден в списке.")
    except ValueError:
        await update.message.reply_text("❌ Ошибка: ID должен быть числом.")


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает Telegram ID пользователя (доступно всем) — чтобы админ мог выдать доступ"""
    await update.message.reply_text(
        f"Твой Telegram ID: <code>{update.effective_user.id}</code>\n"
        "Отправь его администратору, чтобы получить доступ к боту.",
        parse_mode='HTML'
    )


# --- Основные функции и ConversationHandler ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update.effective_user.id):
        await access_denied(update)
        return ConversationHandler.END

    await update.message.reply_text(
        "Привет! Я бот для создания форм регистрации на мероприятия.\n\n"
        "Используй кнопку ниже, чтобы начать создание формы:",
        reply_markup=MAIN_MENU_KEYBOARD
    )
    return ConversationHandler.END


async def create_form_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update.effective_user.id):
        await access_denied(update)
        return ConversationHandler.END

    await update.message.reply_text(
        "Отлично! Введи название мероприятия:",
        reply_markup=ReplyKeyboardRemove()
    )
    return QUEST_NAME


async def get_quest_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update.effective_user.id):
        await access_denied(update)
        return ConversationHandler.END

    context.user_data['quest_name'] = update.message.text.upper()
    await update.message.reply_text("Введи МАКСИМАЛЬНОЕ количество участников в команде:")
    return PARTICIPANTS_COUNT


async def get_participants_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update.effective_user.id):
        await access_denied(update)
        return ConversationHandler.END

    try:
        count = int(update.message.text)
        if count <= 0:
            await update.message.reply_text("Количество должно быть положительным числом.")
            return PARTICIPANTS_COUNT

        context.user_data['count'] = count
        await update.message.reply_text(
            f"Теперь введи МИНИМАЛЬНОЕ количество участников в команде (от 1 до {count}):"
        )
        return REQUIRED_COUNT

    except ValueError:
        await update.message.reply_text("Нужно ввести число.")
        return PARTICIPANTS_COUNT


async def get_required_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update.effective_user.id):
        await access_denied(update)
        return ConversationHandler.END

    try:
        req_count = int(update.message.text)
        max_count = context.user_data['count']

        if req_count <= 0 or req_count > max_count:
            await update.message.reply_text(f"Минимальное количество должно быть числом от 1 до {max_count}.")
            return REQUIRED_COUNT

        quest_name = context.user_data['quest_name']

        status_msg = await update.message.reply_text("🎨 Генерирую картинку...")
        img_io = generate_quest_image(quest_name)

        await send_image_as_both_photo_and_file(
            update=update,
            image_buffer=img_io,
            quest_name=quest_name,
            caption="✅ Картинка готова! Сохрани ее и загрузи в форму вручную в Шапку."
        )

        await status_msg.edit_text("🚀 Отправляю задачу в Google (создание + связка)...")

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, call_google_script, quest_name, max_count, req_count)

            if result.get('status') == 'success':
                text = (
                    f"✅ Автоматизация завершена!\n\n"
                    f"Форма (для заполнения): {result['formUrl']}\n"
                    f"Форма (редактирование): {result['editFormUrl']}\n"
                    f"Таблица: {result['sheetUrl']}\n\n"
                    f"Важно! Форма настроена так, что {req_count} первых участника ОБЯЗАТЕЛЬНЫ.\n"
                    f"Следующие шаги:\n"
                    f"1. Открой форму и установи шрифт Montserrat.\n"
                    f"2. Загрузи картинку, которую я прислал, в Шапку темы."
                )
                await status_msg.edit_text(text, disable_web_page_preview=True)
                await update.message.reply_text(
                    "🎉 Форма готова! Хочешь создать еще одну?",
                    reply_markup=MAIN_MENU_KEYBOARD
                )
            else:
                await status_msg.edit_text(f"⚠️ Ошибка скрипта: {result.get('message')}")
                await update.message.reply_text("Попробуй еще раз:", reply_markup=MAIN_MENU_KEYBOARD)

        except Exception as e:
            logger.error(f"Error: {e}")
            await status_msg.edit_text("❌ Ошибка соединения при вызове Google Script.")
            await update.message.reply_text("Произошла ошибка. Попробуй еще раз:", reply_markup=MAIN_MENU_KEYBOARD)

        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("Нужно ввести число.")
        return REQUIRED_COUNT


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update.effective_user.id):
        await access_denied(update)
        return ConversationHandler.END

    await update.message.reply_text("Отмена. Возвращаю в главное меню.", reply_markup=MAIN_MENU_KEYBOARD)
    return ConversationHandler.END


async def handle_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update.effective_user.id):
        await access_denied(update)
        return

    await update.message.reply_text(
        "🤔 Не понял вашего сообщения. Используйте кнопку '📝 Создать форму' для начала работы.",
        reply_markup=MAIN_MENU_KEYBOARD
    )


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    webhook_url = os.environ.get("WEBHOOK_URL")
    port = int(os.environ.get("PORT", 8000))

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN не установлен в переменных окружения")

    app = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('createForm', create_form_start),
            MessageHandler(filters.Regex('^📝 Создать форму$'), create_form_start)
        ],
        states={
            QUEST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_quest_name)],
            PARTICIPANTS_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_participants_count)],
            REQUIRED_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_required_count)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("admin", admin_help))
    app.add_handler(CommandHandler("users", show_users))
    app.add_handler(CommandHandler("add_user", add_user))
    app.add_handler(CommandHandler("del_user", remove_user))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown_message))

    if webhook_url:
        logger.info(f"Запуск Webhook: {webhook_url} на порту {port}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,
            webhook_url=f"{webhook_url}/{token}"
        )
    else:
        logger.info("Запуск Polling (локально)...")
        app.run_polling()


if __name__ == '__main__':
    main()
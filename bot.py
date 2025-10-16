import os
import asyncio
import base64
import re
from io import BytesIO
from dotenv import load_dotenv

import aiohttp
from telegram import Update, BotCommand, BotCommandScopeAllPrivateChats
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, ContextTypes, filters
)

# === КОНСТАНТЫ ===
MAX_HISTORY_MESSAGES = 4
MAX_RETRIES = 3
# Увеличен таймаут для долгих ответов Gemini
GEMINI_TIMEOUT_SECONDS = 60
# ===============================

# === Настройки окружения ===
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
GAS_PROXY_URL = os.getenv("GAS_PROXY_URL")

TOKEN = TOKEN.strip() if TOKEN else None
GAS_PROXY_URL = GAS_PROXY_URL.strip() if GAS_PROXY_URL else None

if not TOKEN or not GAS_PROXY_URL:
    print("Ошибка: Убедитесь, что TELEGRAM_TOKEN и GAS_PROXY_URL установлены в файле .env")
    exit(1)


# === УТИЛИТА: ЭКРАНИРОВАНИЕ MARKDOWNV2 ===
# Эта функция остается, так как она экранирует специальные символы ВНЕ кода,
# что необходимо для безопасной отправки любого текста, даже без блоков кода.
def escape_markdown_v2(text: str) -> str:
    """
    Экранирует специальные символы MarkdownV2, кроме тех, что внутри блоков кода.

    Специальные символы: _, *, [, ], (, ), ~, `, >, #, +, -, =, |, {, }, ., !
    """

    # Символы, которые НУЖНО экранировать, за исключением тех, что внутри блоков кода.
    escape_chars = r'_*[]()~`>#+-=|{}.!'

    # 1. Находим и временно заменяем блоки кода (```...```)
    code_blocks = re.findall(r"```.*?```", text, re.DOTALL)
    placeholder = "___CODE_BLOCK___"

    # Заменяем блоки кода временным плейсхолдером
    text_processed = re.sub(r"```.*?```", placeholder, text, flags=re.DOTALL)

    # 2. Экранируем специальные символы в оставшемся тексте
    # Заменяем каждый спецсимвол на обратный слэш и сам символ
    text_escaped = re.sub(f"([{re.escape(escape_chars)}])", r'\\\1', text_processed)

    # 3. Восстанавливаем блоки кода (на случай, если модель все-таки их сгенерировала)
    for block in code_blocks:
        text_escaped = text_escaped.replace(placeholder, block, 1)

    return text_escaped


# === Gemini-прокси (ИСПОЛЬЗУЕТ gemini-2.5-flash) ===
async def query_gemini(prompt: str, file_data: str = None, mime_type: str = None, history: list = None) -> str:
    """
    Отправляет запрос к Gemini через прокси.
    Включает логику повторных попыток для 5xx ошибок.
    """

    # --- СИСТЕМНАЯ ИНСТРУКЦИЯ (УДАЛЕНО ТРЕБОВАНИЕ ФОРМАТИРОВАНИЯ КОДА) ---
    system_instruction_text = (
        "Отвечай всегда на русском языке, если вопрос не содержит другого указания. "
        "Если есть прикрепленный файл, внимательно его проанализируй. "
        "**НИКОГДА не используй блоки кода Markdown (тройные обратные кавычки ` ``` `) в ответе**, "
        "даже если ты отвечаешь программным кодом. Просто выводи текст."
    )
    # ----------------------------

    # 1. Инициализируем список Contents.
    contents = history if history else []

    # 2. Убеждаемся, что системная инструкция присутствует, если это новая сессия (нет истории)
    if not history:
        contents.append({
            "role": "user",
            "parts": [{"text": system_instruction_text}]
        })

    # 3. Формируем список Part'ов для текущего запроса пользователя
    current_user_parts = []

    if file_data and mime_type:
        current_user_parts.append({
            "inlineData": {
                "mimeType": mime_type,
                "data": file_data
            }
        })

    current_user_parts.append({"text": prompt})

    # 4. Добавляем Content текущего пользователя
    contents.append({
        "role": "user",
        "parts": current_user_parts
    })

    payload = {
        "model": "gemini-2.5-flash",
        "args": {
            "contents": contents
        }
    }

    for attempt in range(MAX_RETRIES):
        try:
            async with aiohttp.ClientSession() as s:
                # Увеличенный таймаут для устойчивости
                async with s.post(GAS_PROXY_URL, json=payload, timeout=GEMINI_TIMEOUT_SECONDS) as r:

                    # 1. Проверяем ошибки сервера (5xx)
                    if r.status >= 500:
                        if attempt < MAX_RETRIES - 1:
                            delay = 1
                            print(f"Попытка {attempt + 1} завершилась ошибкой 5xx. Пауза {delay}s...")
                            await asyncio.sleep(delay)
                            continue
                        else:
                            r.raise_for_status()

                    # 2. Если код 4xx, сразу выбрасываем ошибку
                    r.raise_for_status()

                    # 3. Если код 200, обрабатываем ответ
                    data = await r.json()

                    text = (
                        data.get("candidates", [{}])[0]
                        .get("content", {})
                        .get("parts", [{}])[0]
                        .get("text", "")
                    )
                    return text or data.get("error", "Нет текста в ответе.")

        # 4. Обработка сетевых ошибок (aiohttp) и таймаутов
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < MAX_RETRIES - 1:
                delay = 1
                print(f"Попытка {attempt + 1} завершилась сетевой ошибкой/таймаутом. Пауза {delay}s...")
                await asyncio.sleep(delay)
                continue
            else:
                return f"Ошибка сетевого запроса к прокси после {MAX_RETRIES} попыток: {e}"

        # 5. Обработка всех остальных ошибок
        except Exception as e:
            return f"Общая ошибка при запросе к Gemini: {e}"

    return "Не удалось получить ответ от модели после всех повторных попыток."


# === Утилита для загрузки файла и кодирования в Base64 ===
async def _download_file_as_base64(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> str:
    """Загружает файл из Telegram и возвращает его содержимое в Base64."""
    try:
        file_obj = await context.bot.get_file(file_id)
        download_url = file_obj.file_path

        if not download_url:
            raise ValueError("Не удалось получить URL для скачивания файла.")

        # Используем aiohttp для загрузки файла
        async with aiohttp.ClientSession() as s:
            async with s.get(download_url, timeout=GEMINI_TIMEOUT_SECONDS) as r:
                r.raise_for_status()
                file_bytes = await r.read()

        return base64.b64encode(file_bytes).decode('utf-8')
    except Exception as e:
        raise Exception(f"Ошибка при загрузке или кодировании файла: {e}")


# === Команды ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет приветственное сообщение."""
    await update.message.reply_text(
        "👋 Привет! Я — Gemini Proxy Bot, сфокусированный на анализе текста и файлов.\n"
        "Задавай вопросы или прикрепи фото/документ (PDF, TXT) с вопросом для анализа!"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет список команд."""
    await update.message.reply_text(
        "📘 Команды:\n"
        "/start — начать\n"
        "/help — список команд\n"
        "/reset — очистить историю диалога\n\n"
        "💬 В группе используй **@**, чтобы бот ответил. Бот помнит контекст последних сообщений.\n"
        "🖼️ Анализ: Отправьте фото или документ (PDF, TXT) с подписью, **упомянув бота (@ваш_бот)**, для анализа."
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очищает историю диалога для текущего чата."""
    chat_id = update.message.chat_id
    if 'history' in context.chat_data and chat_id in context.chat_data['history']:
        context.chat_data['history'][chat_id] = []
        await update.message.reply_text("✅ История диалога была очищена. Начните новый разговор.")
    else:
        await update.message.reply_text("⚠️ История диалога уже пуста.")


# === Обработка обычных сообщений ===
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовые сообщения и запросы в чатах/группах."""
    if not update.message or not update.message.text:
        return

    chat_id = update.message.chat_id

    if 'history' not in context.chat_data:
        context.chat_data['history'] = {}

    chat_history = context.chat_data['history'].get(chat_id, [])

    bot_username = (await context.bot.get_me()).username.lower()
    text = update.message.text

    # В группе отвечаем только по упоминанию
    if update.message.chat.type in ("group", "supergroup"):
        if f"@{bot_username}" not in text.lower():
            return
        # Удаляем упоминание из текста, чтобы Gemini не путался
        text = text.replace(f"@{bot_username}", "").strip()

    if not text:
        if update.message.chat.type in ("group", "supergroup"):
            await update.message.reply_text(
                "💬 Задайте свой вопрос сразу после упоминания меня!"
            )
        return

    # Отправка действия "Печатает..." и временного сообщения
    await update.message.chat.send_action(action="TYPING")
    status_message = await update.message.reply_text("⌛ Думаю...")

    # Используем query_gemini с историей
    answer = await query_gemini(text, history=chat_history)

    # --- ЛОГИКА ОБНОВЛЕНИЯ ИСТОРИИ ---
    chat_history.append({
        "role": "user",
        "parts": [{"text": text}]
    })
    chat_history.append({
        "role": "model",
        "parts": [{"text": answer}]
    })

    # Обрезаем историю: сохраняем последние MAX_HISTORY_MESSAGES ходов.
    chat_history = chat_history[-(MAX_HISTORY_MESSAGES):]
    context.chat_data['history'][chat_id] = chat_history
    # ------------------------------------

    # Экранируем ответ перед отправкой в MarkdownV2
    escaped_answer = escape_markdown_v2(answer)

    try:
        # Пытаемся изменить временное сообщение
        # Даже если код не форматируется блоками, мы используем MarkdownV2
        # для безопасного отображения текста, экранируя спецсимволы.
        await status_message.edit_text(escaped_answer, parse_mode='MarkdownV2')
    except Exception as e:
        # Обработка ошибки форматирования
        print(f"Ошибка при edit_text (MarkdownV2). Попытка отправить чанками: {e}")

        # Разбиваем на чанки
        for chunk in [escaped_answer[i:i + 4000] for i in range(0, len(escaped_answer), 4000)]:
            try:
                await update.message.reply_text(chunk, parse_mode='MarkdownV2')
            except Exception as e_reply:
                # Если даже reply_text с экранированием не работает,
                # отправляем как обычный текст, чтобы пользователь получил ответ.
                print(f"Критическая ошибка при reply_text (MarkdownV2): {e_reply}")
                await update.message.reply_text(
                    "❌ Извините, произошла ошибка форматирования. Вот текст без форматирования:\n\n" + answer)


# === Обработчик файлов (Фото и Документы) ===
async def handle_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает фото, документы и их подписи с помощью Gemini."""
    if not update.message:
        return

    is_group = update.message.chat.type in ("group", "supergroup")
    bot_username = (await context.bot.get_me()).username
    user_prompt = update.message.caption

    # 1. Проверка в группе
    if is_group:
        if not user_prompt or f"@{bot_username}" not in user_prompt:
            return
        # Удаляем упоминание из текста
        user_prompt = user_prompt.replace(f"@{bot_username}", "").strip()

    file_id = None
    mime_type = None

    # 2. Определяем файл, его ID и MIME тип
    if update.message.photo:
        largest_photo = update.message.photo[-1]
        file_id = largest_photo.file_id
        mime_type = "image/jpeg"
    elif update.message.document:
        document = update.message.document

        supported_mimes = [
            "image/jpeg",
            "image/png",
            "application/pdf",
            "text/plain",
        ]

        if document.mime_type in supported_mimes:
            file_id = document.file_id
            mime_type = document.mime_type
        else:
            await update.message.reply_text(
                f"Извините, я не могу обработать файл типа: `{document.mime_type}`. "
                f"Поддерживаются только изображения, PDF и TXT."
            )
            return
    else:
        return

    # 3. Получаем текст: если подписи нет, ставим запрос по умолчанию
    if not user_prompt:
        user_prompt = "Опиши этот файл и ответь, что на нём изображено, или что в нём содержится."

    # 4. Начинаем процесс
    await update.message.chat.send_action(action="TYPING")
    status_message = await update.message.reply_text(f"1️⃣ Загружаю и анализирую ваш файл ({mime_type})...")

    try:
        # 5. Загрузка файла и кодирование в base64
        base64_data = await _download_file_as_base64(context, file_id)

        # 6. Анализ Gemini
        await update.message.chat.send_action(action="TYPING")
        await status_message.edit_text("2️⃣ Анализирую файл с помощью Gemini...")

        answer = await query_gemini(user_prompt, base64_data, mime_type, history=[])

        # 7. Ответ
        escaped_answer = escape_markdown_v2(answer)
        await status_message.edit_text(escaped_answer, parse_mode='MarkdownV2')

    except Exception as e:
        error_msg = f"❌ Произошла ошибка при обработке файла. Подробнее: {str(e)}"
        print(f"File handling error: {e}")
        try:
            await status_message.edit_text(error_msg)
        except Exception:
            await update.message.reply_text(error_msg)


# === Устанавливаем список команд ===
async def set_bot_commands(app):
    """Устанавливает подсказки для команд в меню бота."""
    commands = [
        BotCommand("start", "Начать работу с ботом"),
        BotCommand("help", "Показать список команд"),
        BotCommand("reset", "Очистить историю диалога"),
    ]

    await app.bot.set_my_commands(
        commands,
        scope=BotCommandScopeAllPrivateChats()
    )


# === Запуск ===
def main():
    """Основная функция запуска бота."""
    # Увеличиваем read_timeout для устойчивости к сетевым сбоям Telegram
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).read_timeout(30).build()

    # === Регистрация обработчиков ===
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset_command))

    # Обработчик для фотографий и документов
    file_handler = MessageHandler(
        (filters.PHOTO | filters.Document.ALL) & filters.UpdateType.MESSAGE,
        handle_files
    )
    app.add_handler(file_handler)

    # Обработчик для текстовых сообщений
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.post_init = set_bot_commands

    print("✅ Бот запущен. Работает в чатах и группах.")
    app.run_polling()


if __name__ == "__main__":
    main()
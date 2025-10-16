# api/chat.py
import os
import asyncio
import re
import logging
from dotenv import load_dotenv

from aiohttp import web
# Используем aiohttp_session, который должен быть установлен в вашем окружении
from aiohttp_session import get_session, SimpleCookieStorage

# === КОНСТАНТЫ (КОПИЯ ИЗ bot.py) ===
MAX_HISTORY_MESSAGES = 4
MAX_RETRIES = 3
GEMINI_TIMEOUT_SECONDS = 60
# ===============================

# --- Настройки окружения (Vercel использует ENV VARS) ---
# Vercel автоматически загрузит переменные окружения, установленные в его настройках.
load_dotenv()  # Загружаем локально для тестирования
GAS_PROXY_URL = os.getenv("GAS_PROXY_URL")
# --------------------------------------------------------

# Настройка логирования
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# =======================================================
# === СКОПИРОВАННАЯ ЛОГИКА ИЗ web_server.py И bot.py ===
# =======================================================

# УТИЛИТА: ЭКРАНИРОВАНИЕ MARKDOWNV2 (Копия)
def escape_markdown_v2(text: str) -> str:
    # ... (Код функции escape_markdown_v2) ...
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    code_blocks = re.findall(r"```.*?```", text, re.DOTALL)
    placeholder = "___CODE_BLOCK___"
    text_processed = re.sub(r"```.*?```", placeholder, text, flags=re.DOTALL)
    text_escaped = re.sub(f"([{re.escape(escape_chars)}])", r'\\\1', text_processed)
    for block in code_blocks:
        text_escaped = text_escaped.replace(placeholder, block, 1)
    return text_escaped


# Gemini-прокси (Копия)
async def query_gemini(prompt: str, file_data: str = None, mime_type: str = None, history: list = None) -> str:
    # ... (Код функции query_gemini) ...
    # --- СИСТЕМНАЯ ИНСТРУКЦИЯ ---
    system_instruction_text = (
        "Отвечай всегда на русском языке, если вопрос не содержит другого указания. "
        "Если есть прикрепленный файл, внимательно его проанализируй. "
        "**НИКОГДА не используй блоки кода Markdown (тройные обратные кавычки ` ``` `) в ответе**, "
        "даже если ты отвечаешь программным кодом. Просто выводи текст."
    )
    # ----------------------------

    # 1. Инициализируем список Contents.
    contents = history if history else []

    if not history:
        contents.append({
            "role": "user",
            "parts": [{"text": system_instruction_text}]
        })

    current_user_parts = []
    if file_data and mime_type:
        current_user_parts.append({
            "inlineData": {
                "mimeType": mime_type,
                "data": file_data
            }
        })

    current_user_parts.append({"text": prompt})
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

    # 2. Используем aiohttp.ClientSession для запроса
    # В Vercel лучше создавать сессию внутри функции, т.к. окружение бессерверное
    import aiohttp  # Импортируем внутри для чистоты, если не используется в других местах
    for attempt in range(MAX_RETRIES):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(GAS_PROXY_URL, json=payload, timeout=GEMINI_TIMEOUT_SECONDS) as r:
                    if r.status >= 500 and attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(1)
                        continue

                    r.raise_for_status()
                    data = await r.json()

                    text = (
                        data.get("candidates", [{}])[0]
                        .get("content", {})
                        .get("parts", [{}])[0]
                        .get("text", "")
                    )
                    return text or data.get("error", "Нет текста в ответе.")

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(1)
                continue
            else:
                return f"Ошибка сетевого запроса к прокси после {MAX_RETRIES} попыток: {e}"

        except Exception as e:
            return f"Общая ошибка при запросе к Gemini: {e}"

    return "Не удалось получить ответ от модели после всех повторных попыток."


# =======================================================


# --- ASGI/AIOHTTP ХЕНДЛЕРЫ ---
async def chat_handler(request):
    """Обрабатывает запросы /chat от TWA."""
    if not GAS_PROXY_URL:
        return web.json_response({"error": "GAS_PROXY_URL не настроен"}, status=500)

    try:
        data = await request.json()
        prompt = data.get('prompt')
        file_data = data.get('file_data')
        mime_type = data.get('mime_type')

        session = await get_session(request)
        history = session.get('history', [])

        if not prompt and not file_data:
            return web.json_response({"error": "Пустой запрос"}, status=400)

        is_multimodal = file_data is not None and mime_type is not None

        current_history = [] if is_multimodal else history

        answer = await query_gemini(prompt, file_data, mime_type, history=current_history)

        if not is_multimodal:
            history.append({"role": "user", "parts": [{"text": prompt}]})
            history.append({"role": "model", "parts": [{"text": answer}]})

            history = history[-(MAX_HISTORY_MESSAGES):]
            session['history'] = history

        # TWA самостоятельно отобразит Markdown, переводы строк
        return web.json_response({"text": answer})

    except Exception as e:
        logger.error(f"Ошибка в chat_handler: {e}")
        return web.json_response({"error": f"Произошла ошибка сервера: {e}"}, status=500)


async def reset_handler(request):
    """Обрабатывает команду /reset от TWA."""
    session = await get_session(request)
    session['history'] = []
    logger.info("История TWA сброшена.")
    return web.json_response({"status": "История очищена"})


# Создаем приложение (будет вызвано Vercel)
def create_app():
    app = web.Application()

    # Vercel не сохраняет состояние между вызовами, но сессия aiohttp_session
    # будет использовать куки, которые хранятся в браузере (TWA), что нам и нужно.
    # Используем SimpleCookieStorage, так как у нас нет Redis/Memcached.
    from aiohttp_session import setup as setup_session
    setup_session(app, SimpleCookieStorage())

    app.router.add_post('/chat', chat_handler)
    app.router.add_post('/reset', reset_handler)

    return app


# Vercel ожидает хендлер ASGI или WSGI. aiohttp.web.run_app не нужен.
app = create_app()

# Экспортируем ASGI-совместимый обработчик для Vercel
# Vercel автоматически найдет этот объект и использует его как ASGI-приложение.
handler = app
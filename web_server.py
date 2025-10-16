import os
import asyncio
import re
import base64
import logging
from dotenv import load_dotenv

import aiohttp
from aiohttp import web
from aiohttp_session import setup as setup_session, get_session, SimpleCookieStorage

# Настраиваем логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === КОНСТАНТЫ (КОПИЯ ИЗ bot.py) ===
MAX_HISTORY_MESSAGES = 4
MAX_RETRIES = 3
GEMINI_TIMEOUT_SECONDS = 60
# ===============================

# === Настройки окружения (КОПИЯ ИЗ bot.py) ===
load_dotenv()
GAS_PROXY_URL = os.getenv("GAS_PROXY_URL").strip() if os.getenv("GAS_PROXY_URL") else None

if not GAS_PROXY_URL:
    logger.error("Ошибка: Убедитесь, что GAS_PROXY_URL установлен в файле .env")
    exit(1)


# === УТИЛИТА: ЭКРАНИРОВАНИЕ MARKDOWNV2 (КОПИЯ ИЗ bot.py) ===
def escape_markdown_v2(text: str) -> str:
    """
    Экранирует специальные символы MarkdownV2, кроме тех, что внутри блоков кода.
    """
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    code_blocks = re.findall(r"```.*?```", text, re.DOTALL)
    placeholder = "___CODE_BLOCK___"
    text_processed = re.sub(r"```.*?```", placeholder, text, flags=re.DOTALL)
    text_escaped = re.sub(f"([{re.escape(escape_chars)}])", r'\\\1', text_processed)
    for block in code_blocks:
        text_escaped = text_escaped.replace(placeholder, block, 1)
    return text_escaped


# === Gemini-прокси (КОПИЯ ИЗ bot.py) ===
async def query_gemini(prompt: str, file_data: str = None, mime_type: str = None, history: list = None) -> str:
    """
    Отправляет запрос к Gemini через прокси.
    (Весь код функции скопирован из bot.py)
    """
    system_instruction_text = (
        "Отвечай всегда на русском языке, если вопрос не содержит другого указания. "
        "Если есть прикрепленный файл, внимательно его проанализируй. "
        "**НИКОГДА не используй блоки кода Markdown (тройные обратные кавычки ` ``` `) в ответе**, "
        "даже если ты отвечаешь программным кодом. Просто выводи текст."
    )

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

    for attempt in range(MAX_RETRIES):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(GAS_PROXY_URL, json=payload, timeout=GEMINI_TIMEOUT_SECONDS) as r:

                    if r.status >= 500:
                        if attempt < MAX_RETRIES - 1:
                            delay = 1
                            logger.info(f"Попытка {attempt + 1} завершилась ошибкой 5xx. Пауза {delay}s...")
                            await asyncio.sleep(delay)
                            continue
                        else:
                            r.raise_for_status()

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
                delay = 1
                logger.info(f"Попытка {attempt + 1} завершилась сетевой ошибкой/таймаутом. Пауза {delay}s...")
                await asyncio.sleep(delay)
                continue
            else:
                return f"Ошибка сетевого запроса к прокси после {MAX_RETRIES} попыток: {e}"

        except Exception as e:
            return f"Общая ошибка при запросе к Gemini: {e}"

    return "Не удалось получить ответ от модели после всех повторных попыток."


# === HTTP-ОБРАБОТЧИКИ ДЛЯ TWA ===

async def chat_handler(request):
    """Обрабатывает текстовые и мультимодальные запросы от TWA."""
    try:
        data = await request.json()
        prompt = data.get('prompt')
        file_data = data.get('file_data')
        mime_type = data.get('mime_type')

        session = await get_session(request)
        # TWA использует сессию для хранения истории
        history = session.get('history', [])

        if not prompt:
            return web.json_response({"error": "Пустой запрос"}, status=400)

        is_multimodal = file_data is not None and mime_type is not None

        # Для мультимодальных запросов, как и в боте, сбрасываем историю
        current_history = [] if is_multimodal else history

        answer = await query_gemini(prompt, file_data, mime_type, history=current_history)

        # Обновление истории только для обычных текстовых запросов (как в bot.py)
        if not is_multimodal:
            history.append({
                "role": "user",
                "parts": [{"text": prompt}]
            })
            history.append({
                "role": "model",
                "parts": [{"text": answer}]
            })

            history = history[-(MAX_HISTORY_MESSAGES):]
            session['history'] = history
        else:
            # Для мультимодальных запросов история сбрасывается для следующего запроса
            session['history'] = []

        # TWA самостоятельно отобразит Markdown
        return web.json_response({
            "text": answer,
        })

    except Exception as e:
        logger.error(f"Ошибка в chat_handler: {e}")
        return web.json_response({"error": f"Произошла ошибка сервера: {e}"}, status=500)


async def reset_handler(request):
    """Обрабатывает команду сброса истории TWA."""
    session = await get_session(request)
    session['history'] = []
    logger.info("История TWA сброшена.")
    return web.json_response({"status": "История очищена"})


def create_app():
    app = web.Application()

    # Настройка сессий для хранения истории (ВНИМАНИЕ: для продакшена нужен Memcached/Redis)
    setup_session(app, SimpleCookieStorage(max_age=3600))  # Сессия на 1 час

    # Добавляем маршруты API
    app.router.add_post('/chat', chat_handler)
    app.router.add_post('/reset', reset_handler)

    # Маршрут для отдачи HTML/JS/CSS (статические файлы TWA)
    # Создайте папку 'static' и поместите туда index.html
    app.router.add_static('/', path='static', name='static')

    return app


if __name__ == '__main__':
    # ВАЖНО: Укажите здесь реальный порт (например, 8080)
    PORT = 8080

    logger.info(f"🚀 Запуск TWA сервера на http://0.0.0.0:{PORT}")
    # Используем aiohttp.web.run_app
    web.run_app(create_app(), host='0.0.0.0', port=PORT)
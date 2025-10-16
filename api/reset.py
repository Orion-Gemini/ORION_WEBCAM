# api/reset.py
import os
import logging
from aiohttp import web
from aiohttp_session import get_session, SimpleCookieStorage
from aiohttp_session import setup as setup_session

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

async def reset_handler(request):
    """Обрабатывает команду /reset от TWA."""
    try:
        session = await get_session(request)
        session['history'] = []
        logger.info("История TWA сброшена.")
        return web.json_response({"status": "История очищена"})
    except Exception as e:
        logger.error(f"Ошибка в reset_handler: {e}")
        return web.json_response({"error": f"Произошла ошибка сервера: {e}"}, status=500)

def create_app():
    app = web.Application()
    setup_session(app, SimpleCookieStorage())
    app.router.add_post('/reset', reset_handler)
    return app

app = create_app()
handler = app
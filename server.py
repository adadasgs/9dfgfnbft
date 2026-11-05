import asyncio
import logging
import os

from aiohttp import web
from bot import main

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("radar-server")


async def health(request):
    return web.json_response({"status": "ok", "service": "nft-profile-radar"})


def bot_task_finished(task):
    if task.cancelled():
        log.info("Telegram bot task was cancelled")
        return
    error = task.exception()
    if error is not None:
        log.exception(
            "Telegram bot stopped with an error",
            exc_info=(type(error), error, error.__traceback__),
        )
    else:
        log.error("Telegram bot task stopped unexpectedly without an exception")


async def start_bot(app):
    log.info("Starting Telegram bot background task")
    task = asyncio.create_task(main(), name="telegram-bot")
    task.add_done_callback(bot_task_finished)
    app["bot_task"] = task


async def stop_bot(app):
    task = app.get("bot_task")
    if task and not task.done():
        log.info("Stopping Telegram bot")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = web.Application()
app.router.add_get("/", health)
app.router.add_get("/health", health)
app.on_startup.append(start_bot)
app.on_cleanup.append(stop_bot)


if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

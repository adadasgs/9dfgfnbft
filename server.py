import asyncio
import os
from aiohttp import web
from bot import main

async def health(request):
    return web.json_response({"status": "ok", "service": "nft-profile-radar"})

async def start_bot(app):
    app["bot_task"] = asyncio.create_task(main())

async def stop_bot(app):
    task = app.get("bot_task")
    if task:
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

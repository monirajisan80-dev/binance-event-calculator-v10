import asyncio
import logging
import signal
from contextlib import AsyncExitStack
from aiohttp import web
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from config import BOT_TOKEN, SCRAPE_INTERVAL_SEC, PORT, HEALTH_SERVER
from db import db
from scraper import scraper
from monitor import Monitor
import handlers as h

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("main")


async def safe_cleanup(close):
    try:
        await close()
    except Exception:
        log.exception("Service cleanup failed")


async def health_server():
    app = web.Application()
    async def health(request):
        try:
            ok = await asyncio.wait_for(db.ping(), timeout=5)
        except Exception:
            ok = False
        return web.Response(text="ok" if ok else "db unavailable", status=200 if ok else 503)
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
    except BaseException:
        await safe_cleanup(runner.cleanup)
        raise
    return runner


async def main(stop_event=None):
    stopped = stop_event if stop_event is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = []
    if stop_event is None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stopped.set)
                signals.append(sig)
            except (NotImplementedError, RuntimeError):
                pass
    try:
        async with AsyncExitStack() as resources:
            resources.push_async_callback(safe_cleanup, db.close)
            await db.init()
            resources.push_async_callback(safe_cleanup, scraper.stop)
            await scraper.start()
            app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()
            app.add_handler(CommandHandler("start", h.start))
            app.add_handler(CommandHandler("admin", h.admin))
            app.add_handler(CommandHandler("cancel", h.cancel))
            app.add_handler(CommandHandler("help", h.help_cmd))
            app.add_handler(CallbackQueryHandler(h.on_callback))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_text))
            app.add_error_handler(h.on_error)
            from jobs import job_scrape, job_cleanup
            app.job_queue.run_repeating(job_scrape, interval=SCRAPE_INTERVAL_SEC, first=20,
                                        job_kwargs={"max_instances": 1, "coalesce": True})
            app.job_queue.run_repeating(job_cleanup, interval=3600, first=60)
            async def notify(text):
                for aid in await db.notification_ids():
                    try:
                        await app.bot.send_message(aid, text, disable_web_page_preview=True)
                    except Exception:
                        log.warning("notify failed for %s", aid)
            if HEALTH_SERVER:
                runner = await health_server()
                resources.push_async_callback(safe_cleanup, runner.cleanup)
                log.info("Health server listening on 0.0.0.0:%s", PORT)
            resources.push_async_callback(safe_cleanup, app.shutdown)
            await app.initialize()
            async def stop_app():
                if app.running:
                    await app.stop()
            resources.push_async_callback(safe_cleanup, stop_app)
            await app.start()
            if app.updater is None:
                raise RuntimeError("Telegram updater unavailable")
            async def stop_polling():
                if app.updater.running:
                    await app.updater.stop()
            resources.push_async_callback(safe_cleanup, stop_polling)
            await app.updater.start_polling(drop_pending_updates=True)
            monitor = Monitor(notify)
            resources.push_async_callback(safe_cleanup, monitor.stop)
            await monitor.start()
            log.info("Bot running")
            await stopped.wait()
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

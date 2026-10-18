import asyncio
import fcntl
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import BOT_TOKEN
from handlers.photo import photo_router

LOCK_PATH = Path("/tmp/tonometer-bot.lock")
lock_file = None


def acquire_lock() -> None:
    global lock_file
    lock_file = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
    except OSError:
        print(
            "Bot already running. Only one instance can poll Telegram updates at a time.",
            file=sys.stderr,
        )
        sys.exit(1)


def release_lock() -> None:
    global lock_file
    if lock_file is None:
        return
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    except OSError:
        pass
    lock_file.close()
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass

logging.basicConfig(level=logging.INFO, stream=sys.stdout)


async def main() -> None:

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.include_router(photo_router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    acquire_lock()
    try:
        asyncio.run(main())
    finally:
        release_lock()

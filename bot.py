import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from max_sender import send_to_max


load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_IDS = frozenset(
    int(x) for x in os.environ["CHANNEL_IDS"].split(",") if x.strip()
)
LOG_FILE = str(Path(__file__).parent / "bridge.log")

MAX_SEND_TIMEOUT = 240
QUEUE_MAX_SIZE = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

log = logging.getLogger(__name__)

send_queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)


@dataclass
class BridgeJob:
    job_id: str
    chat_id: int
    message_id: int
    text: str
    photo_path: Optional[str]
    created_at: float


def describe_update(update: Update) -> str:
    msg = update.channel_post or update.message or update.edited_channel_post or update.edited_message

    if not msg:
        return "unknown update without message"

    return (
        f"chat_id={msg.chat.id}, "
        f"message_id={msg.message_id}, "
        f"date={msg.date}, "
        f"text={bool(msg.text)}, "
        f"caption={bool(msg.caption)}, "
        f"photo={bool(msg.photo)}, "
        f"video={bool(msg.video)}, "
        f"document={bool(msg.document)}, "
        f"media_group_id={msg.media_group_id}"
    )


async def handle_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("Получен update: %s", describe_update(update))

    msg = update.channel_post or update.message

    if not msg:
        log.warning("Нет channel_post/message в update, пропускаем")
        return

    chat_id = msg.chat.id

    if chat_id not in CHANNEL_IDS:
        log.warning("Пост из чужого чата %s, разрешены %s, пропускаем", chat_id, sorted(CHANNEL_IDS))
        return

    text = msg.text or msg.caption or ""
    photo_path = None

    job_id = str(uuid.uuid4())[:8]

    log.info(
        "[%s] Новый пост из канала %s, message_id=%s, text='%s', photo=%s",
        job_id,
        chat_id,
        msg.message_id,
        text[:120],
        bool(msg.photo)
    )

    if msg.photo:
        try:
            photo = msg.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            photo_path = f"/tmp/tg_photo_{job_id}_{photo.file_id}.jpg"
            await file.download_to_drive(photo_path)

            if os.path.exists(photo_path):
                log.info("[%s] Фото скачано: %s, размер=%s байт", job_id, photo_path, os.path.getsize(photo_path))
            else:
                log.error("[%s] После скачивания фото файла нет: %s", job_id, photo_path)
                photo_path = None

        except Exception as exc:
            log.exception("[%s] Ошибка скачивания фото: %s", job_id, exc)
            photo_path = None

    if not text and not photo_path:
        log.warning("[%s] Нет текста и фото, пропускаем", job_id)
        return

    job = BridgeJob(
        job_id=job_id,
        chat_id=chat_id,
        message_id=msg.message_id,
        text=text,
        photo_path=photo_path,
        created_at=time.time()
    )

    try:
        send_queue.put_nowait(job)
        log.info("[%s] Задача добавлена в очередь. Размер очереди: %s", job_id, send_queue.qsize())
    except asyncio.QueueFull:
        log.error("[%s] Очередь переполнена, задача потеряна", job_id)

        if photo_path and os.path.exists(photo_path):
            try:
                os.remove(photo_path)
            except Exception:
                pass


async def max_worker():
    log.info("Max worker запущен")

    while True:
        job: BridgeJob = await send_queue.get()

        wait_sec = round(time.time() - job.created_at, 1)

        log.info(
            "[%s] Worker взял задачу. Ждала в очереди %s сек. text=%s, photo=%s",
            job.job_id,
            wait_sec,
            bool(job.text),
            bool(job.photo_path)
        )

        try:
            ok = await asyncio.wait_for(
                send_to_max(text=job.text, photo_path=job.photo_path),
                timeout=MAX_SEND_TIMEOUT
            )

            if ok:
                log.info("[%s] Успешно отправлено в Max", job.job_id)
            else:
                log.error("[%s] send_to_max вернул False", job.job_id)

        except asyncio.TimeoutError:
            log.error("[%s] Таймаут отправки в Max: %s сек", job.job_id, MAX_SEND_TIMEOUT)

        except Exception as exc:
            log.exception("[%s] Ошибка отправки в Max: %s", job.job_id, exc)

        finally:
            if job.photo_path and os.path.exists(job.photo_path):
                try:
                    os.remove(job.photo_path)
                    log.info("[%s] Временное фото удалено: %s", job.job_id, job.photo_path)
                except Exception as exc:
                    log.warning("[%s] Не удалось удалить временное фото: %s", job.job_id, exc)

            send_queue.task_done()
            log.info("[%s] Задача завершена. Очередь: %s", job.job_id, send_queue.qsize())


async def post_init(app: Application):
    app.create_task(max_worker())


def main():
    log.info("=== Запуск моста Telegram → Max с очередью ===")
    log.info("Каналы: %s", sorted(CHANNEL_IDS))

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(MessageHandler(filters.ALL, handle_post))

    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()

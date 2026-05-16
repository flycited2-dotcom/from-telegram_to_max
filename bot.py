import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from max_sender import send_to_max


load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_IDS = frozenset(
    int(x) for x in os.environ["CHANNEL_IDS"].split(",") if x.strip()
)
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"]) if os.environ.get("ADMIN_CHAT_ID") else None
LOG_FILE = str(Path(__file__).parent / "bridge.log")

MAX_SEND_TIMEOUT = 900
QUEUE_MAX_SIZE = 100
# Telegram Bot API лимит на скачивание файлов через get_file — 20 МБ.
DOC_MAX_SIZE = 20 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

log = logging.getLogger(__name__)


@dataclass
class BridgeJob:
    job_id: str
    chat_id: int
    message_id: int
    text: str
    photo_path: Optional[str]
    document_path: Optional[str]
    document_name: Optional[str]
    created_at: float


class JobQueue:
    """deque + asyncio.Event. Поверх FIFO даёт pop_last/pop_n/clear, чтобы
    /cancel_last /cancel_5 /cancel_all могли удалять задачи из хвоста очереди."""

    def __init__(self, maxsize: int) -> None:
        self._items: deque[BridgeJob] = deque()
        self._maxsize = maxsize
        self._event = asyncio.Event()

    def put_nowait(self, job: BridgeJob) -> None:
        if len(self._items) >= self._maxsize:
            raise asyncio.QueueFull
        self._items.append(job)
        self._event.set()

    async def get(self) -> BridgeJob:
        while not self._items:
            self._event.clear()
            await self._event.wait()
        job = self._items.popleft()
        if not self._items:
            self._event.clear()
        return job

    def qsize(self) -> int:
        return len(self._items)

    def snapshot(self) -> List[BridgeJob]:
        return list(self._items)

    def pop_last(self) -> Optional[BridgeJob]:
        if not self._items:
            return None
        return self._items.pop()

    def pop_n_last(self, n: int) -> List[BridgeJob]:
        removed: List[BridgeJob] = []
        for _ in range(min(n, len(self._items))):
            removed.append(self._items.pop())
        return removed

    def clear_all(self) -> List[BridgeJob]:
        removed = list(self._items)
        self._items.clear()
        self._event.clear()
        return removed


send_queue = JobQueue(maxsize=QUEUE_MAX_SIZE)

# Последние N завершённых задач — для /status. Сбрасывается при рестарте.
recent_jobs: deque = deque(maxlen=50)

# Заполняется в post_init из app.bot, чтобы _notify_admin_failure мог писать
# в личку администратора через тот же Bot-инстанс, что обслуживает polling.
_admin_bot = None


def _safe_filename(raw: Optional[str], fallback: str) -> str:
    cleaned = re.sub(r"[\\/\x00]", "_", raw or "").strip()
    return cleaned or fallback


def _cleanup_job_files(job: BridgeJob) -> None:
    """Снести временные файлы задачи. Зовём из worker'а после отправки
    и из /cancel_* после удаления из очереди."""
    if job.photo_path and os.path.exists(job.photo_path):
        try:
            os.remove(job.photo_path)
        except Exception as exc:
            log.warning("[%s] Не удалось удалить временное фото: %s", job.job_id, exc)

    if job.document_path:
        doc_dir = os.path.dirname(job.document_path)
        try:
            shutil.rmtree(doc_dir, ignore_errors=True)
        except Exception as exc:
            log.warning("[%s] Не удалось удалить временный документ: %s", job.job_id, exc)


async def _fetch_with_retry(
    bot,
    file_id: str,
    target_path: str,
    job_id: str,
    kind: str,
    max_attempts: int = 3,
    delay: float = 3.0,
) -> bool:
    """Скачать file_id в target_path с N повторными попытками. Telegram API
    периодически отдаёт `Timed out` на больших фото/документах — без retry
    мы это проглатывали и теряли вложение."""
    for attempt in range(1, max_attempts + 1):
        try:
            file = await bot.get_file(file_id)
            await file.download_to_drive(target_path)
            if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                return True
            log.warning("[%s] %s: попытка %s — файла нет/пустой", job_id, kind, attempt)
        except Exception as exc:
            log.warning("[%s] %s: попытка %s упала: %s", job_id, kind, attempt, exc)

        if attempt < max_attempts:
            await asyncio.sleep(delay)

    log.error("[%s] %s: все %s попыток скачивания провалились", job_id, kind, max_attempts)
    return False


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
    document_path = None
    document_name = None

    job_id = str(uuid.uuid4())[:8]

    log.info(
        "[%s] Новый пост из канала %s, message_id=%s, text='%s', photo=%s, document=%s",
        job_id,
        chat_id,
        msg.message_id,
        text[:120],
        bool(msg.photo),
        bool(msg.document)
    )

    if msg.photo:
        photo = msg.photo[-1]
        photo_path = f"/tmp/tg_photo_{job_id}_{photo.file_id}.jpg"
        ok = await _fetch_with_retry(context.bot, photo.file_id, photo_path, job_id, "Фото")
        if ok:
            log.info(
                "[%s] Фото скачано: %s, размер=%s байт",
                job_id,
                photo_path,
                os.path.getsize(photo_path),
            )
        else:
            photo_path = None

    if msg.document:
        doc = msg.document
        if doc.file_size and doc.file_size > DOC_MAX_SIZE:
            log.warning(
                "[%s] Документ '%s' слишком большой (%s байт), Bot API лимит 20 МБ. Пропускаем документ",
                job_id,
                doc.file_name,
                doc.file_size,
            )
        else:
            doc_dir = tempfile.mkdtemp(prefix=f"tg_doc_{job_id}_")
            document_name = _safe_filename(doc.file_name, f"file_{doc.file_id}")
            document_path = os.path.join(doc_dir, document_name)
            ok = await _fetch_with_retry(
                context.bot, doc.file_id, document_path, job_id, "Документ"
            )
            if ok:
                log.info(
                    "[%s] Документ скачан: %s, размер=%s байт",
                    job_id,
                    document_path,
                    os.path.getsize(document_path),
                )
            else:
                shutil.rmtree(doc_dir, ignore_errors=True)
                document_path = None
                document_name = None

    if not text and not photo_path and not document_path:
        log.warning("[%s] Нет текста, фото и документа, пропускаем", job_id)
        return

    job = BridgeJob(
        job_id=job_id,
        chat_id=chat_id,
        message_id=msg.message_id,
        text=text,
        photo_path=photo_path,
        document_path=document_path,
        document_name=document_name,
        created_at=time.time()
    )

    try:
        send_queue.put_nowait(job)
        log.info("[%s] Задача добавлена в очередь. Размер очереди: %s", job_id, send_queue.qsize())
    except asyncio.QueueFull:
        log.error("[%s] Очередь переполнена, задача потеряна", job_id)
        _cleanup_job_files(job)


async def _notify_admin_failure(job: "BridgeJob") -> None:
    """Сообщить администратору в личку, что пост не доставлен. Тихо
    проглатываем 403 / прочие ошибки send_message — администратор мог
    ещё не нажать /start у бота, в этом случае Telegram не позволит
    инициировать диалог."""
    if not ADMIN_CHAT_ID or _admin_bot is None:
        return

    preview = (job.text or "(без текста)")[:200]
    if job.text and len(job.text) > 200:
        preview += "…"

    text = (
        "⚠️ Пост не доставлен в Max\n"
        f"Канал: {job.chat_id}\n"
        f"message_id: {job.message_id}\n"
        f"Вложения: photo={bool(job.photo_path)}, document={bool(job.document_path)}"
    )
    if job.document_name:
        text += f" ({job.document_name})"
    text += f"\nТекст: {preview}"

    try:
        await _admin_bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
        log.info("[%s] Уведомление администратору отправлено", job.job_id)
    except Exception as exc:
        log.warning("[%s] Не удалось отправить уведомление администратору: %s", job.job_id, exc)


async def _send_job_with_retry(job: "BridgeJob", max_attempts: int = 2, retry_delay: float = 10.0) -> bool:
    """Зовёт send_to_max до max_attempts раз. На транзиентных сбоях
    (Playwright timeout, отвалившаяся сессия) вторая попытка через
    retry_delay сек обычно проходит — open browser в send_to_max
    создаётся заново для каждой попытки."""
    for attempt in range(1, max_attempts + 1):
        try:
            ok = await asyncio.wait_for(
                send_to_max(
                    text=job.text,
                    photo_path=job.photo_path,
                    document_path=job.document_path,
                    document_name=job.document_name,
                ),
                timeout=MAX_SEND_TIMEOUT,
            )
            if ok:
                return True
            log.warning("[%s] Попытка отправки %s вернула False", job.job_id, attempt)
        except asyncio.TimeoutError:
            log.warning("[%s] Попытка %s: таймаут %s сек", job.job_id, attempt, MAX_SEND_TIMEOUT)
        except Exception as exc:
            log.exception("[%s] Попытка %s: ошибка отправки: %s", job.job_id, attempt, exc)

        if attempt < max_attempts:
            await asyncio.sleep(retry_delay)

    return False


async def max_worker():
    log.info("Max worker запущен")

    while True:
        job: BridgeJob = await send_queue.get()

        wait_sec = round(time.time() - job.created_at, 1)

        log.info(
            "[%s] Worker взял задачу. Ждала в очереди %s сек. text=%s, photo=%s, document=%s",
            job.job_id,
            wait_sec,
            bool(job.text),
            bool(job.photo_path),
            bool(job.document_path)
        )

        ok = False
        try:
            ok = await _send_job_with_retry(job)

            if ok:
                log.info("[%s] Успешно отправлено в Max", job.job_id)
            else:
                log.error("[%s] Все попытки отправки провалились", job.job_id)
                await _notify_admin_failure(job)

        finally:
            _cleanup_job_files(job)
            recent_jobs.append({
                "job_id": job.job_id,
                "chat_id": job.chat_id,
                "message_id": job.message_id,
                "status": "done" if ok else "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "has_photo": bool(job.photo_path),
                "has_document": bool(job.document_path),
            })
            log.info("[%s] Задача завершена. Очередь: %s", job.job_id, send_queue.qsize())


def _is_admin(update: Update) -> bool:
    if ADMIN_CHAT_ID is None:
        return False
    user = update.effective_user
    return user is not None and user.id == ADMIN_CHAT_ID


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статус", callback_data="status")],
        [InlineKeyboardButton("⏪ Отменить последнюю", callback_data="cancel_last")],
        [InlineKeyboardButton("5️⃣ Отменить 5 последних", callback_data="cancel_5")],
        [InlineKeyboardButton("🗑 Очистить очередь", callback_data="cancel_all")],
        [InlineKeyboardButton("🔄 Рестарт сервиса", callback_data="restart")],
    ])


def _format_status() -> str:
    pending = send_queue.snapshot()
    done = sum(1 for j in recent_jobs if j["status"] == "done")
    failed = sum(1 for j in recent_jobs if j["status"] == "failed")

    lines = [
        "📊 *Статус моста*",
        f"В очереди: {len(pending)}",
        f"За последние {len(recent_jobs)} задач: ✅ {done} / ❌ {failed}",
    ]

    if pending:
        lines.append("")
        lines.append("Ожидают:")
        for j in pending[:10]:
            wait = round(time.time() - j.created_at)
            kind = []
            if j.text:
                kind.append("text")
            if j.photo_path:
                kind.append("photo")
            if j.document_path:
                kind.append("doc")
            lines.append(f"  • {j.job_id} (msg {j.message_id}, {wait}с, {','.join(kind) or '—'})")
        if len(pending) > 10:
            lines.append(f"  … и ещё {len(pending) - 10}")

    if recent_jobs:
        lines.append("")
        lines.append("Последние:")
        for j in list(recent_jobs)[-5:]:
            mark = "✅" if j["status"] == "done" else "❌"
            lines.append(f"  {mark} {j['job_id']} (msg {j['message_id']})")

    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    await update.effective_message.reply_text(
        "Привет! Я мост Telegram → MAX.\n"
        "Управление — кнопками ниже или командами /menu, /status.",
        reply_markup=_menu_keyboard(),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    await update.effective_message.reply_text("Меню:", reply_markup=_menu_keyboard())


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    await update.effective_message.reply_text(_format_status(), parse_mode="Markdown")


async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return
    if not _is_admin(update):
        await query.answer("Нет доступа", show_alert=False)
        return

    action = query.data

    if action == "status":
        await query.answer()
        await query.message.reply_text(_format_status(), parse_mode="Markdown")
        return

    if action == "cancel_last":
        job = send_queue.pop_last()
        await query.answer()
        if job:
            _cleanup_job_files(job)
            await query.message.reply_text(
                f"⏪ Отменена последняя в очереди: {job.job_id} (msg {job.message_id}). "
                f"Осталось: {send_queue.qsize()}"
            )
            log.info("[admin] Отменена последняя: %s", job.job_id)
        else:
            await query.message.reply_text("Очередь пуста — нечего отменять.")
        return

    if action == "cancel_5":
        removed = send_queue.pop_n_last(5)
        await query.answer()
        for j in removed:
            _cleanup_job_files(j)
            log.info("[admin] Отменена: %s", j.job_id)
        await query.message.reply_text(
            f"5️⃣ Удалено из очереди: {len(removed)}. Осталось: {send_queue.qsize()}"
        )
        return

    if action == "cancel_all":
        removed = send_queue.clear_all()
        await query.answer()
        for j in removed:
            _cleanup_job_files(j)
        log.info("[admin] Очередь очищена, удалено %s задач", len(removed))
        await query.message.reply_text(
            f"🗑 Очередь очищена ({len(removed)} задач удалено). "
            "Текущая (если есть) добивает доставку."
        )
        return

    if action == "restart":
        await query.answer()
        await query.message.reply_text("🔄 Перезапускаю сервис, вернусь через ~10 сек.")
        log.info("[admin] Запрошен рестарт сервиса")
        # systemd сам поднимет сервис после `Restart=always`. Используем
        # Popen, чтобы не блокироваться на ожидании systemctl.
        try:
            subprocess.Popen(
                ["systemctl", "restart", "tg-max-bridge.service"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            log.exception("Ошибка systemctl restart: %s", exc)
            await query.message.reply_text(f"Не удалось рестартовать: {exc}")
            return
        # Дадим сообщению уйти, потом завершимся.
        await asyncio.sleep(1)
        sys.exit(0)

    await query.answer("Неизвестное действие")


async def post_init(app: Application):
    global _admin_bot
    _admin_bot = app.bot
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.ALL, handle_post))

    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()

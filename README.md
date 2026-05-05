# tg-max-bridge

Мост, который пересылает посты из Telegram-канала в чат VK MAX.

Текст, фото и документы из `channel_post`/`message` забираются через Telegram Bot API,
кладутся в asyncio-очередь и по одному отправляются в MAX через Playwright
(headless Chromium, переиспользование `storage_state`). Документы — до 20 МБ
(лимит Telegram Bot API на скачивание через `getFile`).

## Файлы

- `bot.py` — Telegram poller + очередь + worker
- `max_sender.py` — Playwright-логика отправки (composer, прикрепление фото, send)
- `save_session.py` — однократно: войти в MAX вручную и сохранить `max_session.json`
- `tg-max-bridge.service` — systemd unit для деплоя
- `requirements.txt`, `.env.example`, `.gitignore`

## Переменные окружения (`.env`)

| Имя | Что это |
|---|---|
| `BOT_TOKEN` | токен Telegram-бота из `@BotFather` |
| `CHANNEL_IDS` | список ID каналов-источников через запятую, каждый — отрицательное число с `-100` (можно один или несколько) |
| `MAX_CHAT_URL` | полный URL чата MAX, напр. `https://web.max.ru/-12345` |

## Подготовка сессии MAX (нужно один раз)

Требуется машина с GUI (Windows/Linux с X11). На сервере без графики не запустится:

```
python -m venv .venv
. .venv/bin/activate           # на Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python save_session.py         # откроет браузер, залогинься в MAX, нажми Enter
```

Получится `max_session.json` рядом со скриптами. Скопируй его на сервер
в `/opt/tg-max-bridge/max_session.json` (`scp ./max_session.json root@host:/opt/tg-max-bridge/`).

Файл сессии — секрет, в git не попадает (`.gitignore`).

## Деплой на сервер (Ubuntu)

```
apt-get update
apt-get install -y python3-venv git
git clone https://github.com/flycited2-dotcom/from-telegram_to_max /opt/tg-max-bridge
cd /opt/tg-max-bridge
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium

cp .env.example .env
# отредактировать .env: BOT_TOKEN, CHANNEL_IDS, MAX_CHAT_URL
chmod 600 .env

# положить max_session.json (см. выше)
chmod 600 max_session.json

cp tg-max-bridge.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tg-max-bridge
systemctl status tg-max-bridge
journalctl -u tg-max-bridge -f
```

## Поведение

- Принимаются `channel_post`, `message`, `edited_*` из любого канала, чей ID есть в `CHANNEL_IDS`. Остальное игнорируется.
- Фото скачивается во временный `/tmp/tg_photo_<job>_<file_id>.jpg` и удаляется после отправки/таймаута.
- При сбое отправки делаются `debug/<step>.png|.html` рядом со скриптами.
- Очередь `QUEUE_MAX_SIZE=100`, таймаут отправки `MAX_SEND_TIMEOUT=240` сек.

## Перевыпуск сессии MAX

Если в логах появляются ошибки логина/composer'а — сессия истекла.
Запусти `save_session.py` локально, скопируй свежий `max_session.json` на сервер,
`systemctl restart tg-max-bridge`.

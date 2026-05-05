import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

load_dotenv(Path(__file__).parent / ".env")

MAX_CHAT_URL = os.environ["MAX_CHAT_URL"]
SESSION_FILE = str(Path(__file__).parent / "max_session.json")
DEBUG_DIR = Path(__file__).parent / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


async def _save_debug(page, name: str):
    try:
        png = DEBUG_DIR / f"{name}.png"
        html = DEBUG_DIR / f"{name}.html"
        await page.screenshot(path=str(png), full_page=True)
        html.write_text(await page.content(), encoding="utf-8")
        log.info("Диагностика сохранена: %s и %s", png, html)
    except Exception as exc:
        log.warning("Не удалось сохранить диагностику %s: %s", name, exc)


async def _wait_composer(page):
    composer = page.locator('[data-testid="composer"]').first
    await composer.wait_for(state="visible", timeout=30000)
    log.info("Composer найден")
    return composer


async def _composer_textbox(page):
    composer = await _wait_composer(page)

    textbox = composer.locator('div[role="textbox"][data-lexical-editor="true"]').first

    if await textbox.count() <= 0:
        textbox = composer.locator('div[role="textbox"]').first

    await textbox.wait_for(state="visible", timeout=15000)

    box = await textbox.bounding_box()
    log.info("Поле сообщения найдено внутри composer: %s", box)

    return textbox


async def _type_text(page, text: str) -> bool:
    text = text or ""

    if not text.strip():
        return True

    try:
        textbox = await _composer_textbox(page)

        await textbox.click(force=True)
        await page.wait_for_timeout(300)

        await page.keyboard.press("Control+A")
        await page.wait_for_timeout(150)

        await page.keyboard.type(text, delay=10)
        await page.wait_for_timeout(800)

        confirmed = await page.evaluate(
            """
            (expected) => {
                const composer = document.querySelector('[data-testid="composer"]');
                if (!composer) return false;

                const box =
                    composer.querySelector('div[role="textbox"][data-lexical-editor="true"]') ||
                    composer.querySelector('div[role="textbox"]');

                if (!box) return false;

                const value = (box.innerText || box.textContent || '').trim();
                return value.includes(expected.trim());
            }
            """,
            text[:80],
        )

        if confirmed:
            log.info("Текст подтверждён в composer")
            return True

        log.error("Текст не подтвердился в composer")
        await _save_debug(page, "text_not_confirmed")
        return False

    except Exception as exc:
        log.exception("Ошибка ввода текста в composer: %s", exc)
        await _save_debug(page, "text_input_error")
        return False


async def _composer_media_count(page) -> int:
    try:
        return await page.evaluate(
            """
            () => {
                const composer = document.querySelector('[data-testid="composer"]');
                if (!composer) return 0;

                const items = composer.querySelectorAll(
                    'img, canvas, video, [class*="preview"], [class*="attach"], [class*="media"], [class*="photo"], [class*="image"], [class*="file"]'
                );

                let count = 0;

                for (const el of items) {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);

                    const visible =
                        r.width > 0 &&
                        r.height > 0 &&
                        s.display !== 'none' &&
                        s.visibility !== 'hidden' &&
                        s.opacity !== '0';

                    if (visible && r.width >= 20 && r.height >= 20) {
                        count++;
                    }
                }

                return count;
            }
            """
        )
    except Exception:
        return 0


async def _wait_photo_preview(page, before_count: int, timeout_ms: int = 25000) -> bool:
    log.info("Ждём превью фото внутри composer. Было media=%s", before_count)

    attempts = max(1, timeout_ms // 500)

    for _ in range(attempts):
        count = await _composer_media_count(page)

        if count > before_count:
            log.info("Превью фото найдено внутри composer: media было %s, стало %s", before_count, count)
            return True

        direct_found = await page.evaluate(
            """
            () => {
                const composer = document.querySelector('[data-testid="composer"]');
                if (!composer) return false;

                const selectors = [
                    'img[src^="data:"]',
                    'img[src^="blob:"]',
                    'canvas',
                    'video',
                    '[class*="preview"]',
                    '[class*="attach"]',
                    '[class*="media"]',
                    '[class*="photo"]',
                    '[class*="image"]',
                    '[class*="file"]'
                ];

                for (const selector of selectors) {
                    const items = composer.querySelectorAll(selector);

                    for (const el of items) {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);

                        const visible =
                            r.width > 0 &&
                            r.height > 0 &&
                            s.display !== 'none' &&
                            s.visibility !== 'hidden' &&
                            s.opacity !== '0';

                        if (visible && r.width >= 20 && r.height >= 20) {
                            return true;
                        }
                    }
                }

                return false;
            }
            """
        )

        if direct_found:
            log.info("Превью фото найдено прямой проверкой внутри composer")
            return True

        await page.wait_for_timeout(500)

    log.error("Превью фото внутри composer не появилось")
    await _save_debug(page, "photo_preview_not_found")
    return False


async def _attach_photo(page, photo_path: str) -> bool:
    path = Path(photo_path)

    if not path.exists():
        log.error("Фото не найдено: %s", photo_path)
        return False

    if path.stat().st_size <= 0:
        log.error("Фото пустое: %s", photo_path)
        return False

    try:
        composer = await _wait_composer(page)

        before_count = await _composer_media_count(page)

        log.info("Прикрепляем фото через меню Max: скрепка -> Фото или видео")

        upload_button = composer.locator('button[aria-label="Загрузить файл"]').first
        await upload_button.wait_for(state="visible", timeout=15000)

        # 1. Открываем меню скрепки
        await upload_button.click(force=True)
        await page.wait_for_timeout(1000)

        # 2. Кликаем пункт "Фото или видео"
        photo_menu_selectors = [
            'text="Фото или видео"',
            'button:has-text("Фото или видео")',
            '[role="button"]:has-text("Фото или видео")',
            'div:has-text("Фото или видео")',
        ]

        clicked_menu = False

        for selector in photo_menu_selectors:
            try:
                item = page.locator(selector).first
                await item.wait_for(state="visible", timeout=3000)

                log.info("Найден пункт меню фото: %s", selector)

                async with page.expect_file_chooser(timeout=10000) as fc_info:
                    await item.click(force=True)

                file_chooser = await fc_info.value
                await file_chooser.set_files(str(path))

                clicked_menu = True
                log.info("Файл передан через file chooser после пункта 'Фото или видео'")
                break

            except Exception as exc:
                log.warning("Пункт меню/chooser не сработал %s: %s", selector, exc)

        # 3. Если file chooser не поймался, пробуем второй вариант:
        # пункт меню мог активировать скрытый input после клика
        if not clicked_menu:
            log.warning("File chooser через пункт меню не пойман, пробуем input[type=file] после открытия меню")

            try:
                file_inputs = page.locator('input[type="file"]')
                count = await file_inputs.count()
                log.info("input[type=file] после меню: %s", count)

                for i in range(count):
                    try:
                        file_input = file_inputs.nth(i)
                        await file_input.set_input_files(str(path))
                        await page.wait_for_timeout(1000)

                        handle = await file_input.element_handle()

                        if handle:
                            await page.evaluate(
                                """
                                (el) => {
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                                }
                                """,
                                handle,
                            )

                        clicked_menu = True
                        log.info("Файл передан через input[type=file] #%s после меню", i)
                        break

                    except Exception as exc:
                        log.warning("input[type=file] #%s после меню не сработал: %s", i, exc)

            except Exception as exc:
                log.warning("Ошибка fallback input после меню: %s", exc)

        if not clicked_menu:
            log.error("Не удалось передать файл в Max")
            await _save_debug(page, "photo_menu_file_not_passed")
            return False

        await page.wait_for_timeout(7000)

        ok = await _wait_photo_preview(page, before_count=before_count, timeout_ms=30000)

        if ok:
            log.info("Фото прикреплено успешно через меню 'Фото или видео'")
            return True

        log.error("Фото не прикрепилось: превью после меню не появилось")
        await _save_debug(page, "photo_attach_menu_failed")
        return False

    except Exception as exc:
        log.exception("Ошибка прикрепления фото через меню: %s", exc)
        await _save_debug(page, "photo_attach_menu_exception")
        return False


async def _composer_has_text(page) -> bool:
    try:
        return await page.evaluate(
            """
            () => {
                const composer = document.querySelector('[data-testid="composer"]');
                if (!composer) return false;

                const box =
                    composer.querySelector('div[role="textbox"][data-lexical-editor="true"]') ||
                    composer.querySelector('div[role="textbox"]');

                if (!box) return false;

                const value = (box.innerText || box.textContent || '').trim();
                return value.length > 0;
            }
            """
        )
    except Exception:
        return False


async def _composer_has_media(page) -> bool:
    try:
        return (await _composer_media_count(page)) > 0
    except Exception:
        return False


async def _click_send(page) -> bool:
    try:
        composer = await _wait_composer(page)

        button = composer.locator('button[aria-label="Отправить сообщение"]').first

        if await button.count() <= 0:
            button = composer.locator('button[aria-label*="Отправить"]').first

        await button.wait_for(state="visible", timeout=15000)

        box = await button.bounding_box()
        log.info("Кнопка отправки внутри composer найдена: %s", box)

        for attempt in range(1, 4):
            log.info("Клик отправки, попытка %s", attempt)

            await button.click(force=True)
            await page.wait_for_timeout(5000)

            has_text = await _composer_has_text(page)
            has_media = await _composer_has_media(page)

            log.info("После клика composer: has_text=%s, has_media=%s", has_text, has_media)

            if not has_text and not has_media:
                log.info("Composer очистился — отправка подтверждена")
                await page.wait_for_timeout(5000)
                return True

        log.warning("Composer не очистился после кликов, пробуем Enter")

        textbox = await _composer_textbox(page)
        await textbox.click(force=True)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(5000)

        has_text = await _composer_has_text(page)
        has_media = await _composer_has_media(page)

        log.info("После Enter composer: has_text=%s, has_media=%s", has_text, has_media)

        if not has_text and not has_media:
            log.info("Composer очистился после Enter — отправка подтверждена")
            await page.wait_for_timeout(5000)
            return True

        log.error("Отправка не подтверждена: composer не очистился")
        await _save_debug(page, "send_not_confirmed")
        return False

    except Exception as exc:
        log.exception("Ошибка клика отправки: %s", exc)
        await _save_debug(page, "send_click_exception")
        return False


async def send_to_max(text: str = "", photo_path: Optional[str] = None) -> bool:
    text = text or ""

    async with async_playwright() as p:
        browser = None

        try:
            if not os.path.exists(SESSION_FILE):
                raise FileNotFoundError(f"Файл сессии не найден: {SESSION_FILE}")

            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            context = await browser.new_context(
                storage_state=SESSION_FILE,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1600, "height": 900},
            )

            page = await context.new_page()

            log.info("Открываем Max: %s", MAX_CHAT_URL)
            await page.goto(MAX_CHAT_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(7000)

            await _wait_composer(page)
            await _composer_textbox(page)

            photo_attached = False

            if photo_path and os.path.exists(photo_path):
                photo_attached = await _attach_photo(page, photo_path)

                if not photo_attached:
                    log.error("Фото не прикрепилось. Если есть текст — отправим только текст.")

            if text.strip():
                text_ok = await _type_text(page, text)

                if not text_ok and not photo_attached:
                    log.error("Текст не введён, фото нет — отправлять нечего")
                    return False

            if not text.strip() and not photo_attached:
                log.error("Нет текста и нет фото")
                return False

            send_ok = await _click_send(page)

            await context.storage_state(path=SESSION_FILE)

            if send_ok:
                log.info("Сообщение отправлено в Max")
            else:
                log.error("Сообщение не отправлено")

            return send_ok

        except Exception as exc:
            log.exception("Ошибка send_to_max: %s", exc)

            try:
                if "page" in locals():
                    await _save_debug(page, "send_to_max_exception")
            except Exception:
                pass

            return False

        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass

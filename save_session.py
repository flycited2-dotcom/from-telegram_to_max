import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

SESSION_FILE = str(Path(__file__).parent / "max_session.json")

async def save_session():
    print("Запускаем браузер... Нужен GUI/VNC!")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()
        await page.goto("https://web.max.ru", wait_until="domcontentloaded")
        print(">>> Залогинься в браузере, потом нажми Enter...")
        input()
        await context.storage_state(path=SESSION_FILE)
        print(f"Сессия сохранена: {SESSION_FILE}")
        await browser.close()

asyncio.run(save_session())

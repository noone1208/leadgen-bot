import asyncio
import logging
import json
import os
import re
import random
import subprocess
import sys
from typing import Optional
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def ensure_playwright_browser():
    """Install Playwright Chromium browser if not already present."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
            logger.info("Playwright browser not found. Installing Chromium...")
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                logger.info("Playwright Chromium installed successfully.")
            else:
                logger.error(f"Failed to install Playwright Chromium: {result.stderr}")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "mode": "notify",
    "keywords": [],
    "min_score": 5,
    "your_product": "",
    "your_name": "",
    "language": "uk",
}

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            return {**DEFAULT_SETTINGS, **json.load(f)}
    return DEFAULT_SETTINGS.copy()

def save_settings(s: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)

settings = load_settings()
seen_posts = set()
monitoring_task: Optional[asyncio.Task] = None

# ── Threads Scraper ───────────────────────────────────────────────────────────
async def scrape_threads(keyword: str) -> list:
    posts = []
    url = f"https://www.threads.net/search?q={keyword.replace(' ', '+')}&serp_type=default"
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = await context.new_page()
            
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(3, 5))
            
            # Scroll to load more posts
            for _ in range(3):
                await page.keyboard.press("End")
                await asyncio.sleep(1.5)
            
            # Extract posts from page
            content = await page.content()
            await browser.close()
            
            # Parse post data from HTML
            posts = parse_threads_html(content, keyword)
            logger.info(f"Scraped {len(posts)} posts for '{keyword}'")
            
    except Exception as e:
        logger.error(f"Scraper error for '{keyword}': {e}")
    
    return posts

def parse_threads_html(html: str, keyword: str) -> list:
    posts = []
    
    # Extract JSON data embedded in page
    json_matches = re.findall(r'"text_post_app_thread":\{[^}]+\}', html)
    
    # Fallback: extract text blocks that look like posts
    # Look for aria-label patterns and text content
    text_pattern = re.findall(
        r'"caption":\{"text":"([^"]{20,500})"[^}]*\}.*?"user":\{"pk":"(\d+)".*?"username":"([^"]+)"',
        html
    )
    
    seen_texts = set()
    for match in text_pattern[:20]:
        text, user_id, username = match
        text = text.encode().decode('unicode_escape', errors='ignore')
        
        if text in seen_texts:
            continue
        seen_texts.add(text)
        
        posts.append({
            "text": text,
            "author": username,
            "author_id": user_id,
            "url": f"https://www.threads.net/@{username}",
            "keyword": keyword,
        })
    
    # If regex didn't work, try another pattern
    if not posts:
        alt_pattern = re.findall(
            r'"username":"([^"]+)"[^}]*"full_name":"([^"]*)".*?"text":"([^"]{20,500})"',
            html
        )
        for match in alt_pattern[:20]:
            username, full_name, text = match
            try:
                text = text.encode().decode('unicode_escape', errors='ignore')
            except Exception:
                pass
            
            if text in seen_texts:
                continue
            seen_texts.add(text)
            
            posts.append({
                "text": text,
                "author": username,
                "author_name": full_name,
                "url": f"https://www.threads.net/@{username}",
                "keyword": keyword,
            })
    
    return posts

# ── AI Analysis ──────────────────────────────────────────────────────────────
def analyze_post(text: str, author: str, author_bio: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are a B2B sales intelligence analyst. Analyze this Threads post.

POST: {text[:1500]}
AUTHOR: @{author}
BIO: {author_bio or 'no bio'}
WE SELL: {settings.get('your_product', 'not specified')}
SELLER: {settings.get('your_name', 'not specified')}

Respond ONLY with valid JSON:
{{
  "relevance_score": <0-10>,
  "pain_points": ["pain1", "pain2"],
  "author_insights": {{
    "likely_role": "role",
    "company_stage": "startup/smb/enterprise/individual",
    "buying_intent": "low/medium/high",
    "personality": "one sentence"
  }},
  "opportunity_summary": "2-3 sentences why good lead",
  "outreach_message": "personalized DM 3-4 sentences warm not salesy in {settings.get('language', 'uk')}"
}}"""

    msg = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    try:
        return json.loads(msg.content[0].text)
    except Exception:
        return {
            "relevance_score": 5,
            "pain_points": [],
            "author_insights": {"likely_role": "?", "company_stage": "?", "buying_intent": "medium", "personality": "?"},
            "opportunity_summary": msg.content[0].text[:300],
            "outreach_message": "Привіт! Бачив твій пост і подумав що можу допомогти."
        }

# ── Format ────────────────────────────────────────────────────────────────────
def clean(s: str) -> str:
    for ch in ['_', '*', '[', ']', '`', '~']:
        s = str(s).replace(ch, '')
    return s

def format_lead(post: dict, analysis: dict) -> str:
    score = analysis.get("relevance_score", 0)
    score_emoji = "🔥" if score >= 8 else "⚡" if score >= 6 else "📌"
    intent_emoji = {"high": "🎯", "medium": "👀", "low": "💤"}.get(
        analysis.get("author_insights", {}).get("buying_intent", "low"), "💤"
    )
    ai = analysis.get("author_insights", {})
    pain_points = "\n".join(f"  • {clean(p)}" for p in analysis.get("pain_points", []))
    author = clean(post.get("author") or "unknown")
    text = clean(post.get("text") or "")
    post_url = post.get("url") or f"https://www.threads.net/@{author}"

    return f"""{score_emoji} Новий лід з Threads! [{score}/10]

👤 @{author} {intent_emoji}
{post_url}

📝 {text[:300]}

━━━━━━━━━━━━━━
🧠 Інсайти:
  • Роль: {clean(ai.get('likely_role', '?'))}
  • Компанія: {clean(ai.get('company_stage', '?'))}
  • Інтент: {clean(ai.get('buying_intent', '?'))}
  • {clean(ai.get('personality', ''))}

💥 Болі:
{clean(pain_points)}

💡 Чому лід:
{clean(analysis.get('opportunity_summary', ''))}

━━━━━━━━━━━━━━
✉️ Готове повідомлення:
{clean(analysis.get('outreach_message', ''))}"""

# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""🤖 LeadGen Monitor Bot — Threads

Моніторю Threads і знаходжу B2B лідів.

Команди:
/setup — налаштування
/status — поточний стан
/start_monitor — запустити
/stop_monitor — зупинити
/mode — notify / auto_send
/test — тестовий аналіз""")

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""/set_product веб-дизайн послуги
/set_name Олександр
/set_keywords web designer ux designer
/set_score 5
/set_language uk""")

async def set_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    if not context.args:
        await update.message.reply_text("Використання: /set_keywords слово1 слово2")
        return
    # Deduplicate keywords
    keywords = list(dict.fromkeys(context.args))
    settings["keywords"] = keywords
    save_settings(settings)
    await update.message.reply_text(f"Ключові слова: {', '.join(keywords)}")

async def set_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    settings["your_product"] = " ".join(context.args)
    save_settings(settings)
    await update.message.reply_text(f"Продукт: {settings['your_product']}")

async def set_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    settings["your_name"] = " ".join(context.args)
    save_settings(settings)
    await update.message.reply_text(f"Ім'я: {settings['your_name']}")

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    settings["language"] = context.args[0] if context.args else "uk"
    save_settings(settings)
    await update.message.reply_text(f"Мова: {settings['language']}")

async def set_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    try:
        settings["min_score"] = max(0, min(10, int(context.args[0])))
        save_settings(settings)
        await update.message.reply_text(f"Мінімальний скор: {settings['min_score']}")
    except (IndexError, ValueError):
        await update.message.reply_text("Використання: /set_score 5")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_task
    is_running = monitoring_task and not monitoring_task.done()
    await update.message.reply_text(f"""Статус

{'🟢 Активний' if is_running else '🔴 Зупинений'}
Режим: {'Авто' if settings['mode'] == 'auto_send' else 'Notify'}
Ключові слова: {', '.join(settings['keywords']) or 'не задані'}
Продукт: {settings.get('your_product') or 'не задано'}
Мін. скор: {settings['min_score']}/10
Мова: {settings.get('language', 'uk')}""")

async def toggle_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    settings["mode"] = "auto_send" if settings["mode"] == "notify" else "notify"
    save_settings(settings)
    await update.message.reply_text(f"Режим: {'Авто' if settings['mode'] == 'auto_send' else 'Notify'}")

async def test_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Запускаю тестовий аналіз...")
    fake_post = {
        "author": "startup_ceo_ua",
        "text": "Шукаю веб дизайнера для редизайну сайту. Є бюджет, потрібен хтось хто розуміє B2B і може зробити лендінг що конвертує.",
        "url": "https://www.threads.net/@startup_ceo_ua"
    }
    loop = asyncio.get_event_loop()
    analysis = await loop.run_in_executor(None, analyze_post, fake_post["text"], fake_post["author"], "CEO at B2B startup")
    msg = format_lead(fake_post, analysis)
    keyboard = [[InlineKeyboardButton("Профіль", url=fake_post["url"])]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)

async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_task
    if not settings["keywords"]:
        await update.message.reply_text("Спочатку задай /set_keywords")
        return
    if monitoring_task and not monitoring_task.done():
        await update.message.reply_text("Вже запущено!")
        return
    monitoring_task = asyncio.create_task(monitor_loop(context.application))
    await update.message.reply_text(f"🟢 Моніторинг запущено!\nКлючові слова: {', '.join(settings['keywords'])}\nПеревірка кожні 30 хвилин.")

async def stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_task
    if monitoring_task and not monitoring_task.done():
        monitoring_task.cancel()
        await update.message.reply_text("🔴 Зупинено.")
    else:
        await update.message.reply_text("Не запущено.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

# ── Monitor Loop ──────────────────────────────────────────────────────────────
async def monitor_loop(app: Application):
    logger.info(f"Monitor started: {settings['keywords']}")
    while True:
        try:
            for keyword in settings["keywords"]:
                posts = await scrape_threads(keyword)
                logger.info(f"'{keyword}': {len(posts)} posts")

                for post in posts:
                    post_id = post.get("url", "") + (post.get("text") or "")[:50]
                    if post_id in seen_posts:
                        continue
                    seen_posts.add(post_id)

                    text = post.get("text") or ""
                    if not text or len(text) < 20:
                        continue

                    author = post.get("author") or "unknown"
                    bio = post.get("bio") or ""

                    loop = asyncio.get_event_loop()
                    analysis = await loop.run_in_executor(None, analyze_post, text, author, bio)

                    if analysis["relevance_score"] < settings["min_score"]:
                        continue

                    msg = format_lead(post, analysis)
                    post_url = post.get("url") or f"https://www.threads.net/@{author}"
                    keyboard = [[
                        InlineKeyboardButton("Пост", url=post_url),
                        InlineKeyboardButton("Профіль", url=f"https://www.threads.net/@{author}")
                    ]]

                    await app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=msg,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        disable_web_page_preview=True
                    )

                    await asyncio.sleep(2)

                await asyncio.sleep(10)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Monitor error: {e}")

        await asyncio.sleep(1800)  # 30 хвилин

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ensure_playwright_browser()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setup", setup))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("mode", toggle_mode))
    app.add_handler(CommandHandler("start_monitor", start_monitor))
    app.add_handler(CommandHandler("stop_monitor", stop_monitor))
    app.add_handler(CommandHandler("test", test_analysis))
    app.add_handler(CommandHandler("set_keywords", set_keywords))
    app.add_handler(CommandHandler("set_product", set_product))
    app.add_handler(CommandHandler("set_name", set_name))
    app.add_handler(CommandHandler("set_language", set_language))
    app.add_handler(CommandHandler("set_score", set_score))
    app.add_handler(CallbackQueryHandler(button_callback))
    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

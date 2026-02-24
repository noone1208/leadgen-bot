import asyncio
import logging
import json
import os
import httpx
from typing import Optional
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
APIFY_TOKEN = os.getenv("APIFY_TOKEN")

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

def save_settings(settings: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

settings = load_settings()
seen_posts = set()
monitoring_task: Optional[asyncio.Task] = None

# ── Apify Search ──────────────────────────────────────────────────────────────
async def search_threads(keywords: list) -> list:
    # Step 1: Start the run
    run_url = "https://api.apify.com/v2/acts/watcher.data~search-threads-by-keywords/runs"
    payload = {
        "keywords": keywords,
        "maxItemsPerKeyword": 10,
        "sortByRecent": True,
    }
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            # Start run
            resp = await client.post(
                run_url,
                params={"token": APIFY_TOKEN},
                json=payload
            )
            if resp.status_code not in (200, 201):
                logger.error(f"Apify start error {resp.status_code}: {resp.text[:300]}")
                return []
            
            run_id = resp.json()["data"]["id"]
            logger.info(f"Apify run started: {run_id}")
            
            # Step 2: Wait for run to finish
            for _ in range(30):  # max 60 seconds
                await asyncio.sleep(2)
                status_resp = await client.get(
                    f"https://api.apify.com/v2/acts/watcher.data~search-threads-by-keywords/runs/{run_id}",
                    params={"token": APIFY_TOKEN}
                )
                status = status_resp.json()["data"]["status"]
                logger.info(f"Apify run status: {status}")
                if status == "SUCCEEDED":
                    break
                elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                    logger.error(f"Apify run failed: {status}")
                    return []
            
            # Step 3: Get results
            dataset_id = status_resp.json()["data"]["defaultDatasetId"]
            results_resp = await client.get(
                f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                params={"token": APIFY_TOKEN, "format": "json"}
            )
            if results_resp.status_code == 200:
                return results_resp.json()
            else:
                logger.error(f"Apify results error: {results_resp.status_code}")
                return []
    except Exception as e:
        logger.error(f"Apify request failed: {e}")
        return []

# ── AI Analysis ──────────────────────────────────────────────────────────────
def analyze_post(text: str, author: str, author_bio: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are a B2B sales intelligence analyst. Analyze this Threads post to determine if it's a good lead.

POST TEXT: {text[:1500]}
AUTHOR USERNAME: @{author}
AUTHOR BIO: {author_bio or 'no bio'}

WHAT WE SELL: {settings.get('your_product', 'not specified')}
SELLER NAME: {settings.get('your_name', 'not specified')}

Respond ONLY with valid JSON, no markdown:
{{
  "relevance_score": <0-10, how likely this person needs our service>,
  "pain_points": ["<specific pain 1>", "<specific pain 2>"],
  "author_insights": {{
    "likely_role": "<guessed role>",
    "company_stage": "<startup/smb/enterprise/individual/unknown>",
    "buying_intent": "<low/medium/high>",
    "personality": "<1 sentence personality read>"
  }},
  "opportunity_summary": "<2-3 sentences: why this is a good lead>",
  "outreach_message": "<personalized DM, 3-4 sentences, warm and human, NOT salesy, reference their specific situation. Write in: {settings.get('language', 'uk')}>"
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
            "author_insights": {
                "likely_role": "?", "company_stage": "?",
                "buying_intent": "medium", "personality": "?"
            },
            "opportunity_summary": msg.content[0].text[:300],
            "outreach_message": "Привіт! Бачив твій пост і подумав що можу допомогти."
        }

# ── Format ────────────────────────────────────────────────────────────────────
def clean_text(s: str) -> str:
    """Remove markdown special chars to avoid Telegram parse errors."""
    for ch in ['_', '*', '[', ']', '`', '~']:
        s = s.replace(ch, '')
    return s

def format_lead(post: dict, analysis: dict) -> str:
    score = analysis.get("relevance_score", 0)
    score_emoji = "🔥" if score >= 8 else "⚡" if score >= 6 else "📌"
    intent_emoji = {"high": "🎯", "medium": "👀", "low": "💤"}.get(
        analysis.get("author_insights", {}).get("buying_intent", "low"), "💤"
    )
    ai = analysis.get("author_insights", {})
    pain_points = "\n".join(f"  • {clean_text(p)}" for p in analysis.get("pain_points", []))
    author = clean_text(post.get("author") or post.get("author_name") or "unknown")
    text = clean_text(post.get("text") or "")
    post_url = post.get("url") or f"https://www.threads.net/@{author}"
    summary = clean_text(analysis.get("opportunity_summary", ""))
    outreach = clean_text(analysis.get("outreach_message", ""))
    personality = clean_text(ai.get("personality", ""))

    return f"""{score_emoji} Новий лід з Threads! [{score}/10]

👤 @{author} {intent_emoji}
{post_url}

📝 {text[:300]}

━━━━━━━━━━━━━━
🧠 Інсайти:
  • Роль: {ai.get("likely_role", "?")}
  • Компанія: {ai.get("company_stage", "?")}
  • Інтент: {ai.get("buying_intent", "?")}
  • {personality}

💥 Болі:
{pain_points}

💡 Чому лід:
{summary}

━━━━━━━━━━━━━━
✉️ Готове повідомлення:
{outreach}"""

# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""🤖 *LeadGen Monitor Bot — Threads*

Моніторю Threads і знаходжу B2B лідів по твоїх ключових словах.

*Команди:*
/setup — як налаштувати
/status — поточний стан
/start\\_monitor — запустити моніторинг
/stop\\_monitor — зупинити
/mode — notify / auto\\_send
/test — тестовий аналіз""", parse_mode="Markdown")

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""`/set_product веб-дизайн послуги`
`/set_name Олександр`
`/set_keywords web designer ux designer`
`/set_score 5`
`/set_language uk`""", parse_mode="Markdown")

async def set_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    if not context.args:
        await update.message.reply_text("Використання: /set\\_keywords слово1 слово2", parse_mode="Markdown")
        return
    settings["keywords"] = context.args
    save_settings(settings)
    await update.message.reply_text(f"✅ Ключові слова: {', '.join(context.args)}")

async def set_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    settings["your_product"] = " ".join(context.args)
    save_settings(settings)
    await update.message.reply_text(f"✅ Продукт: {settings['your_product']}")

async def set_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    settings["your_name"] = " ".join(context.args)
    save_settings(settings)
    await update.message.reply_text(f"✅ Ім'я: {settings['your_name']}")

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    settings["language"] = context.args[0] if context.args else "uk"
    save_settings(settings)
    await update.message.reply_text(f"✅ Мова: {settings['language']}")

async def set_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    try:
        settings["min_score"] = max(0, min(10, int(context.args[0])))
        save_settings(settings)
        await update.message.reply_text(f"✅ Мінімальний скор: {settings['min_score']}")
    except (IndexError, ValueError):
        await update.message.reply_text("Використання: /set\\_score 5", parse_mode="Markdown")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_task
    is_running = monitoring_task and not monitoring_task.done()
    await update.message.reply_text(f"""📊 *Статус*

{'🟢 Активний' if is_running else '🔴 Зупинений'}
Режим: {'📤 Авто-надсилання' if settings['mode'] == 'auto_send' else '🔔 Тільки сповіщення'}
Ключові слова: {', '.join(settings['keywords']) or 'не задані'}
Продукт: {settings.get('your_product') or 'не задано'}
Мін. скор: {settings['min_score']}/10
Мова: {settings.get('language', 'uk')}""", parse_mode="Markdown")

async def toggle_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    settings["mode"] = "auto_send" if settings["mode"] == "notify" else "notify"
    save_settings(settings)
    mode_text = "📤 Авто-надсилання" if settings["mode"] == "auto_send" else "🔔 Тільки сповіщення"
    await update.message.reply_text(f"✅ Режим: *{mode_text}*", parse_mode="Markdown")

async def test_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Запускаю тестовий аналіз...")
    fake_post = {
        "author": "startup_ceo_ua",
        "author_name": "Олег | CEO",
        "text": "Шукаю веб дизайнера для редизайну нашого сайту. Є бюджет, потрібен хтось хто розуміє B2B і може зробити лендінг що конвертує. DM якщо є досвід.",
        "url": "https://www.threads.net/@startup_ceo_ua"
    }
    loop = asyncio.get_event_loop()
    analysis = await loop.run_in_executor(
        None, analyze_post,
        fake_post["text"], fake_post["author"], "CEO at B2B startup"
    )
    msg = format_lead(fake_post, analysis)
    keyboard = [[InlineKeyboardButton("👤 Профіль", url=f"https://www.threads.net/@{fake_post['author']}")]]
    await update.message.reply_text(
        msg, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )

async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_task
    if not settings["keywords"]:
        await update.message.reply_text("⚠️ Спочатку задай /set\\_keywords", parse_mode="Markdown")
        return
    if monitoring_task and not monitoring_task.done():
        await update.message.reply_text("⚠️ Вже запущено!")
        return
    monitoring_task = asyncio.create_task(monitor_loop(context.application))
    await update.message.reply_text(
        f"🟢 Моніторинг запущено!\nКлючові слова: {', '.join(settings['keywords'])}\nПеревірка кожні 10 хвилин."
    )

async def stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_task
    if monitoring_task and not monitoring_task.done():
        monitoring_task.cancel()
        await update.message.reply_text("🔴 Моніторинг зупинено.")
    else:
        await update.message.reply_text("⚠️ Моніторинг не запущений.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

# ── Monitor Loop ──────────────────────────────────────────────────────────────
async def monitor_loop(app: Application):
    logger.info(f"Monitor started: {settings['keywords']}")
    while True:
        try:
            posts = await search_threads(settings["keywords"])
            logger.info(f"Found {len(posts)} posts")

            for post in posts:
                post_id = post.get("id") or post.get("url") or str(post.get("text", ""))[:80]
                if post_id in seen_posts:
                    continue
                seen_posts.add(post_id)

                text = post.get("text") or ""
                if not text or len(text) < 20:
                    continue

                author = post.get("author") or post.get("author_name") or "unknown"
                bio = post.get("biography") or post.get("bio") or ""

                loop = asyncio.get_event_loop()
                analysis = await loop.run_in_executor(
                    None, analyze_post, text, author, bio
                )

                if analysis["relevance_score"] < settings["min_score"]:
                    logger.info(f"Skip post by @{author}: score {analysis['relevance_score']}")
                    continue

                msg = format_lead(post, analysis)
                post_url = post.get("url") or f"https://www.threads.net/@{author}"
                keyboard = [[
                    InlineKeyboardButton("🔗 Пост", url=post_url),
                    InlineKeyboardButton("👤 Профіль", url=f"https://www.threads.net/@{author}")
                ]]

                await app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=msg,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    disable_web_page_preview=True
                )

                if settings["mode"] == "auto_send":
                    await app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"📤 Повідомлення для @{author}:\n\n`{analysis.get('outreach_message', '')}`",
                        parse_mode="Markdown"
                    )

                await asyncio.sleep(2)

        except asyncio.CancelledError:
            logger.info("Monitor cancelled")
            break
        except Exception as e:
            logger.error(f"Monitor error: {e}")

        await asyncio.sleep(600)  # 10 хвилин

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
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

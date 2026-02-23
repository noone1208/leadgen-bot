import asyncio
import logging
import json
import os
from datetime import datetime
from typing import Optional
import praw
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# User settings (persisted to settings.json)
SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "mode": "notify",           # "notify" | "auto_send"
    "keywords": [],             # list of keywords to monitor
    "subreddits": [],           # list of subreddits to monitor
    "min_score": 0,             # minimum relevance score (0-10)
    "your_product": "",         # what you're selling (for message generation)
    "your_name": "",            # your name for personalization
    "language": "en",           # language for generated messages
}

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
            return {**DEFAULT_SETTINGS, **s}
    return DEFAULT_SETTINGS.copy()

def save_settings(settings: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

# Global state
settings = load_settings()
seen_posts = set()
monitoring_task: Optional[asyncio.Task] = None
app_instance = None

# ── AI Analysis ──────────────────────────────────────────────────────────────
def analyze_post(post_title: str, post_body: str, post_url: str,
                 author_name: str, subreddit: str) -> dict:
    """Analyze post + author with Claude, return structured insights."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are a B2B sales intelligence analyst. Analyze this social media post and provide insights.

POST DETAILS:
- Subreddit: r/{subreddit}
- Author: u/{author_name}
- Title: {post_title}
- Body: {post_body[:2000] if post_body else "(no body)"}
- URL: {post_url}

PRODUCT/SERVICE BEING SOLD: {settings.get('your_product', 'not specified')}
SELLER NAME: {settings.get('your_name', 'not specified')}

Respond ONLY with valid JSON (no markdown, no explanation):
{{
  "relevance_score": <0-10, how relevant this lead is>,
  "pain_points": ["<pain point 1>", "<pain point 2>"],
  "author_insights": {{
    "likely_role": "<guessed job title/role>",
    "company_stage": "<startup/smb/enterprise/unknown>",
    "tech_savvy": "<low/medium/high>",
    "buying_intent": "<low/medium/high>",
    "personality": "<brief 1-sentence personality read>"
  }},
  "opportunity_summary": "<2-3 sentences: why this is a good lead and what they need>",
  "outreach_message": "<personalized DM message, 3-5 sentences, human and warm, NOT salesy, reference their specific situation, offer value first. Language: {settings.get('language', 'en')}>"
}}"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        return json.loads(message.content[0].text)
    except json.JSONDecodeError:
        # Fallback if JSON parsing fails
        return {
            "relevance_score": 5,
            "pain_points": ["Unable to parse"],
            "author_insights": {"likely_role": "unknown", "company_stage": "unknown",
                                 "tech_savvy": "unknown", "buying_intent": "medium",
                                 "personality": "unknown"},
            "opportunity_summary": message.content[0].text[:300],
            "outreach_message": "Hi! I saw your post and thought I could help..."
        }

# ── Telegram Message Formatting ───────────────────────────────────────────────
def format_lead_message(post, analysis: dict) -> str:
    score = analysis.get("relevance_score", 0)
    score_emoji = "🔥" if score >= 8 else "⚡" if score >= 6 else "📌"
    intent_emoji = {"high": "🎯", "medium": "👀", "low": "💤"}.get(
        analysis.get("author_insights", {}).get("buying_intent", "low"), "💤"
    )

    pain_points = "\n".join(f"  • {p}" for p in analysis.get("pain_points", []))
    ai = analysis.get("author_insights", {})

    msg = f"""{score_emoji} *Новий лід знайдено!* [{score}/10]

📍 *r/{post.subreddit}* | [Відкрити пост]({post.url})
👤 *u/{post.author}* {intent_emoji}

📝 *{post.title[:100]}*

━━━━━━━━━━━━━━
🧠 *Інсайти про автора:*
  • Роль: {ai.get('likely_role', '?')}
  • Компанія: {ai.get('company_stage', '?')}
  • Tech: {ai.get('tech_savvy', '?')}
  • Інтент: {ai.get('buying_intent', '?')}
  • {ai.get('personality', '')}

💥 *Болі:*
{pain_points}

💡 *Чому це лід:*
{analysis.get('opportunity_summary', '')}

━━━━━━━━━━━━━━
✉️ *Згенероване повідомлення:*
_{analysis.get('outreach_message', '')}_"""
    return msg

# ── Telegram Handlers ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """🤖 *LeadGen Monitor Bot*

Я моніторю Reddit і знаходжу потенційних клієнтів для тебе.

*Команди:*
/setup — налаштувати моніторинг
/status — поточні налаштування
/start\\_monitor — запустити моніторинг
/stop\\_monitor — зупинити моніторинг
/mode — перемкнути режим (notify/auto\\_send)
/test — тестовий аналіз"""
    await update.message.reply_text(text, parse_mode="Markdown")

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """⚙️ *Налаштування*

Надішли команди для конфігурації:

`/set_keywords saas crm автоматизація` — ключові слова
`/set_subreddits entrepreneur startups SaaS` — сабредіти
`/set_product CRM для малого бізнесу` — що продаєш
`/set_name Олексій` — твоє ім'я
`/set_language uk` — мова повідомлень (uk/en)
`/set_score 6` — мінімальний скор (0-10)"""
    await update.message.reply_text(text, parse_mode="Markdown")

async def set_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    if not context.args:
        await update.message.reply_text("Використання: /set_keywords keyword1 keyword2")
        return
    settings["keywords"] = context.args
    save_settings(settings)
    await update.message.reply_text(f"✅ Ключові слова: {', '.join(context.args)}")

async def set_subreddits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    if not context.args:
        await update.message.reply_text("Використання: /set_subreddits sub1 sub2")
        return
    settings["subreddits"] = context.args
    save_settings(settings)
    await update.message.reply_text(f"✅ Сабредіти: {', '.join(context.args)}")

async def set_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    if not context.args:
        await update.message.reply_text("Використання: /set_product назва продукту")
        return
    settings["your_product"] = " ".join(context.args)
    save_settings(settings)
    await update.message.reply_text(f"✅ Продукт: {settings['your_product']}")

async def set_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    if not context.args:
        await update.message.reply_text("Використання: /set_name Олексій")
        return
    settings["your_name"] = " ".join(context.args)
    save_settings(settings)
    await update.message.reply_text(f"✅ Ім'я: {settings['your_name']}")

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    lang = context.args[0] if context.args else "en"
    settings["language"] = lang
    save_settings(settings)
    await update.message.reply_text(f"✅ Мова: {lang}")

async def set_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    try:
        score = int(context.args[0])
        settings["min_score"] = max(0, min(10, score))
        save_settings(settings)
        await update.message.reply_text(f"✅ Мінімальний скор: {settings['min_score']}")
    except (IndexError, ValueError):
        await update.message.reply_text("Використання: /set_score 6")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_task
    is_running = monitoring_task and not monitoring_task.done()
    mode_text = "📤 Авто-надсилання" if settings["mode"] == "auto_send" else "🔔 Тільки сповіщення"
    status_text = "🟢 Активний" if is_running else "🔴 Зупинений"

    text = f"""📊 *Статус моніторингу*

{status_text}
Режим: {mode_text}
Ключові слова: {', '.join(settings['keywords']) or 'не задані'}
Сабредіти: {', '.join(settings['subreddits']) or 'не задані'}
Продукт: {settings.get('your_product') or 'не задано'}
Мінімальний скор: {settings['min_score']}/10
Мова: {settings.get('language', 'en')}"""
    await update.message.reply_text(text, parse_mode="Markdown")

async def toggle_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global settings
    if settings["mode"] == "notify":
        settings["mode"] = "auto_send"
        text = "✅ Режим: *📤 Авто-надсилання*\nПовідомлення надсилатимуться автоматично в DM."
    else:
        settings["mode"] = "notify"
        text = "✅ Режим: *🔔 Тільки сповіщення*\nТи будеш отримувати сповіщення і вирішувати самостійно."
    save_settings(settings)
    await update.message.reply_text(text, parse_mode="Markdown")

async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_task
    if not settings["keywords"] or not settings["subreddits"]:
        await update.message.reply_text("⚠️ Спочатку налаштуй /set_keywords та /set_subreddits")
        return
    if monitoring_task and not monitoring_task.done():
        await update.message.reply_text("⚠️ Моніторинг вже запущений!")
        return

    monitoring_task = asyncio.create_task(monitor_reddit(context.application))
    await update.message.reply_text(
        f"🟢 Моніторинг запущено!\n"
        f"Слідкую за: {', '.join(settings['subreddits'])}\n"
        f"Ключові слова: {', '.join(settings['keywords'])}"
    )

async def stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitoring_task
    if monitoring_task and not monitoring_task.done():
        monitoring_task.cancel()
        await update.message.reply_text("🔴 Моніторинг зупинено.")
    else:
        await update.message.reply_text("⚠️ Моніторинг не запущений.")

async def test_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Аналізую тестовий пост...")

    class FakePost:
        title = "Looking for a CRM solution for our 5-person sales team - budget around $500/mo"
        selftext = "We're a B2B SaaS startup, currently using spreadsheets but it's getting messy. Need something with email tracking and pipeline management."
        url = "https://reddit.com/r/entrepreneur/test"
        author = "startup_founder_99"
        subreddit = "entrepreneur"

    post = FakePost()
    analysis = analyze_post(post.title, post.selftext, post.url, str(post.author), str(post.subreddit))

    if analysis["relevance_score"] >= settings["min_score"]:
        msg = format_lead_message(post, analysis)
        keyboard = [
            [InlineKeyboardButton("✉️ Скопіювати повідомлення", callback_data="copy_msg"),
             InlineKeyboardButton("🔗 Відкрити профіль", url=f"https://reddit.com/u/{post.author}")]
        ]
        await update.message.reply_text(msg, parse_mode="Markdown",
                                        reply_markup=InlineKeyboardMarkup(keyboard),
                                        disable_web_page_preview=True)
    else:
        await update.message.reply_text(f"📉 Тестовий пост: скор {analysis['relevance_score']}/10 — нижче порогу {settings['min_score']}.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "copy_msg":
        await query.message.reply_text("ℹ️ Повідомлення вже є в картці вище — просто скопіюй текст після ✉️")

# ── Reddit Monitoring Loop ────────────────────────────────────────────────────
async def monitor_reddit(app: Application):
    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent="LeadGenBot/1.0"
    )

    logger.info(f"Starting Reddit monitor: {settings['subreddits']} | {settings['keywords']}")

    while True:
        try:
            subreddit_str = "+".join(settings["subreddits"])
            subreddit = reddit.subreddit(subreddit_str)

            for post in subreddit.new(limit=25):
                if post.id in seen_posts:
                    continue
                seen_posts.add(post.id)

                # Check keyword match
                text_to_search = (post.title + " " + (post.selftext or "")).lower()
                if not any(kw.lower() in text_to_search for kw in settings["keywords"]):
                    continue

                logger.info(f"Found matching post: {post.id} - {post.title[:60]}")

                # Run AI analysis in thread pool (praw is sync)
                loop = asyncio.get_event_loop()
                analysis = await loop.run_in_executor(
                    None, analyze_post,
                    post.title, post.selftext or "", post.url,
                    str(post.author), str(post.subreddit)
                )

                if analysis["relevance_score"] < settings["min_score"]:
                    logger.info(f"Post score {analysis['relevance_score']} below threshold, skipping")
                    continue

                msg = format_lead_message(post, analysis)
                keyboard = [
                    [InlineKeyboardButton("🔗 Відкрити пост", url=post.url),
                     InlineKeyboardButton("👤 Профіль", url=f"https://reddit.com/u/{post.author}")]
                ]

                await app.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=msg,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    disable_web_page_preview=True
                )

                if settings["mode"] == "auto_send":
                    await app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"📤 *Авто-надсилання*\nПовідомлення готове для відправки u/{post.author}:\n\n`{analysis.get('outreach_message', '')}`",
                        parse_mode="Markdown"
                    )

        except asyncio.CancelledError:
            logger.info("Monitor cancelled")
            break
        except Exception as e:
            logger.error(f"Monitor error: {e}")

        await asyncio.sleep(60)  # Check every minute

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
    app.add_handler(CommandHandler("set_subreddits", set_subreddits))
    app.add_handler(CommandHandler("set_product", set_product))
    app.add_handler(CommandHandler("set_name", set_name))
    app.add_handler(CommandHandler("set_language", set_language))
    app.add_handler(CommandHandler("set_score", set_score))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

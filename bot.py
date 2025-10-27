import os
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
import feedparser

# -------------------------
# Load environment variables
# -------------------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
FETCH_INTERVAL_SECONDS = int(os.getenv("FETCH_INTERVAL_SECONDS", 900))  # default 15 min

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is not set in .env")

# -------------------------
# MongoDB setup (optional)
# -------------------------
mongo = None
db = None
if MONGODB_URI:
    from pymongo import MongoClient
    mongo = MongoClient(MONGODB_URI)
    db = mongo.get_database("newsbot")  # default database name

# -------------------------
# Telegram Bot setup
# -------------------------
bot = Bot(token=TELEGRAM_TOKEN)

# -------------------------
# Feeds list (you can add dynamically)
# -------------------------
feeds = []

# -------------------------
# Helper functions
# -------------------------
async def fetch_html(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.text()

async def fetch_news_from_url(url):
    """Scrape news from a URL (like newsonair.gov.in)"""
    html = await fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    # Example: extract title, summary, images, and video
    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "No title"

    summary_tag = soup.find("p")
    summary = summary_tag.get_text(strip=True) if summary_tag else ""

    # Fetch up to 3 images (no banners/logos)
    images = []
    for img in soup.find_all("img", limit=3):
        src = img.get("src")
        if src and "logo" not in src and "banner" not in src:
            images.append(src)

    # Fetch first video if exists
    video_tag = soup.find("video")
    video_url = None
    if video_tag and video_tag.get("src"):
        video_url = video_tag.get("src")

    return {"title": title, "summary": summary, "images": images, "video": video_url}

async def post_news(chat_id, news):
    media = []
    for img_url in news["images"]:
        media.append(InputMediaPhoto(media=img_url))
    if news["video"]:
        media.append(InputMediaVideo(media=news["video"]))

    caption = f"*{news['title']}*\n\n{news['summary']}"
    if media:
        await bot.send_media_group(chat_id=chat_id, media=media)
        if not news["video"]:  # if video already sent, avoid sending text separately
            await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.MARKDOWN)
    else:
        await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.MARKDOWN)

# -------------------------
# Commands
# -------------------------
async def addfeeds(update: "ContextTypes.DEFAULT_TYPE", context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addfeeds <feed_url>")
        return
    url = context.args[0]
    feeds.append(url)
    await update.message.reply_text(f"Feed added: {url}")

async def removefeeds(update: "ContextTypes.DEFAULT_TYPE", context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removefeeds <feed_url>")
        return
    url = context.args[0]
    if url in feeds:
        feeds.remove(url)
        await update.message.reply_text(f"Feed removed: {url}")
    else:
        await update.message.reply_text("Feed not found.")

async def clearfeeds(update: "ContextTypes.DEFAULT_TYPE", context: ContextTypes.DEFAULT_TYPE):
    feeds.clear()
    await update.message.reply_text("All feeds cleared.")

async def listfeeds(update: "ContextTypes.DEFAULT_TYPE", context: ContextTypes.DEFAULT_TYPE):
    if not feeds:
        await update.message.reply_text("No feeds added.")
        return
    feed_list = "\n".join(feeds)
    await update.message.reply_text(f"Current feeds:\n{feed_list}")

async def start(update: "ContextTypes.DEFAULT_TYPE", context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to News Bot! Use /addfeeds to add news feeds.")

# -------------------------
# Periodic news fetcher
# -------------------------
async def fetch_and_post_news(application: Application):
    while True:
        for feed_url in feeds:
            news = await fetch_news_from_url(feed_url)
            # Example: you can use multiple channels
            target_chats = [update.effective_chat.id for update in application.bot_data.get("chats", [])]
            for chat_id in target_chats:
                await post_news(chat_id, news)
        await asyncio.sleep(FETCH_INTERVAL_SECONDS)

# -------------------------
# Main bot setup
# -------------------------
def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register commands
    application.add_handler(CommandHandler("addfeeds", addfeeds))
    application.add_handler(CommandHandler("removefeeds", removefeeds))
    application.add_handler(CommandHandler("clearfeeds", clearfeeds))
    application.add_handler(CommandHandler("listfeeds", listfeeds))
    application.add_handler(CommandHandler("start", start))

    # Start the periodic fetcher
    application.job_queue.run_repeating(lambda ctx: asyncio.create_task(fetch_and_post_news(application)), interval=FETCH_INTERVAL_SECONDS, first=5)

    # Run bot
    application.run_polling()

if __name__ == "__main__":
    main()

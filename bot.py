import os
import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio
from pymongo import MongoClient

# -------------------------
# Load environment variables
# -------------------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
FETCH_INTERVAL_SECONDS = int(os.getenv("FETCH_INTERVAL_SECONDS", 900))  # 15 min default

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is not set in .env")

# -------------------------
# MongoDB setup
# -------------------------
if not MONGODB_URI:
    raise ValueError("MONGODB_URI is not set in .env")

mongo = MongoClient(MONGODB_URI)
db = mongo.get_database("newsbot")
feeds_collection = db.feeds
chats_collection = db.chats

# -------------------------
# Helper functions
# -------------------------
async def fetch_html(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.text()

async def fetch_news_from_url(url):
    html = await fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "No title"

    summary_tag = soup.find("p")
    summary = summary_tag.get_text(strip=True) if summary_tag else ""

    images = []
    for img in soup.find_all("img", limit=3):
        src = img.get("src")
        if src and "logo" not in src.lower() and "banner" not in src.lower():
            images.append(src)

    video_tag = soup.find("video")
    video_url = video_tag.get("src") if video_tag and video_tag.get("src") else None

    return {"title": title, "summary": summary, "images": images, "video": video_url}

async def post_news(chat_id, news, bot_instance):
    media = []
    for img_url in news["images"]:
        media.append(InputMediaPhoto(media=img_url))
    if news["video"]:
        media.append(InputMediaVideo(media=news["video"]))

    caption = f"*{news['title']}*\n\n{news['summary']}"
    if media:
        await bot_instance.send_media_group(chat_id=chat_id, media=media)
        if not news["video"]:
            await bot_instance.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.MARKDOWN)
    else:
        await bot_instance.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.MARKDOWN)

# -------------------------
# Bot commands
# -------------------------
async def addfeeds(update: ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /addfeeds <feed_url>")
        return
    url = context.args[0]
    if feeds_collection.find_one({"url": url}):
        await update.message.reply_text("Feed already exists.")
    else:
        feeds_collection.insert_one({"url": url})
        await update.message.reply_text(f"Feed added: {url}")

async def removefeeds(update: ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /removefeeds <feed_url>")
        return
    url = context.args[0]
    if feeds_collection.delete_one({"url": url}).deleted_count:
        await update.message.reply_text(f"Feed removed: {url}")
    else:
        await update.message.reply_text("Feed not found.")

async def clearfeeds(update: ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE):
    feeds_collection.delete_many({})
    await update.message.reply_text("All feeds cleared.")

async def listfeeds(update: ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE):
    all_feeds = [f["url"] for f in feeds_collection.find()]
    if not all_feeds:
        await update.message.reply_text("No feeds added.")
        return
    feed_list = "\n".join(all_feeds)
    await update.message.reply_text(f"Current feeds:\n{feed_list}")

async def start(update: ContextTypes.DEFAULT_TYPE, context: ContextTypes.DEFAULT_TYPE):
    # save chat_id for posting
    chat_id = update.effective_chat.id
    if not chats_collection.find_one({"chat_id": chat_id}):
        chats_collection.insert_one({"chat_id": chat_id})
    await update.message.reply_text("Welcome to News Bot! Use /addfeeds to add news feeds.")

# -------------------------
# Background task
# -------------------------
async def periodic_fetch(application: Application):
    while True:
        try:
            all_feeds = [f["url"] for f in feeds_collection.find()]
            all_chats = [c["chat_id"] for c in chats_collection.find()]
            for feed_url in all_feeds:
                news = await fetch_news_from_url(feed_url)
                for chat_id in all_chats:
                    await post_news(chat_id, news, application.bot)
        except Exception as e:
            print(f"Error fetching/posting feed {feed_url}: {e}")
        await asyncio.sleep(FETCH_INTERVAL_SECONDS)

# -------------------------
# Main bot
# -------------------------
def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register commands
    application.add_handler(CommandHandler("addfeeds", addfeeds))
    application.add_handler(CommandHandler("removefeeds", removefeeds))
    application.add_handler(CommandHandler("clearfeeds", clearfeeds))
    application.add_handler(CommandHandler("listfeeds", listfeeds))
    application.add_handler(CommandHandler("start", start))

    # Start background task after bot initializes
    async def on_startup(app: Application):
        app.create_task(periodic_fetch(app))

    application.post_init = on_startup

    # Run polling (PTB manages event loop)
    application.run_polling()

if __name__ == "__main__":
    main()

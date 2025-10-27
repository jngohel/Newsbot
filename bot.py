#!/usr/bin/env python3
import os
import re
import asyncio
import logging
from datetime import datetime
from io import BytesIO

import aiohttp
import feedparser
from bs4 import BeautifulSoup
from pymongo import MongoClient
from telegram import Update, InputMediaPhoto, InputMediaVideo
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

# --- Load environment variables ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
FETCH_INTERVAL = int(os.getenv("FETCH_INTERVAL_SECONDS", "900"))

if not TELEGRAM_TOKEN or not MONGODB_URI:
    raise RuntimeError("Missing TELEGRAM_TOKEN or MONGODB_URI in environment variables")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("NewsBot")

mongo = MongoClient(MONGODB_URI)
db = mongo.get_database()
feeds_col = db["feeds"]
seen_col = db["seen"]

HEADERS = {"User-Agent": "Mozilla/5.0 TelegramNewsBot/Light"}
FILTER_IMG = re.compile(r"(logo|banner|ads?|advert)", re.I)


async def fetch_html(session, url):
    try:
        async with session.get(url, headers=HEADERS, timeout=15) as r:
            if r.status == 200:
                return await r.text()
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
    return None


def clean_summary(text, max_len=500):
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:max_len]


def extract_article_data(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title else ""
    paragraphs = [p.get_text() for p in soup.find_all("p")]
    summary = clean_summary(" ".join(paragraphs))
    imgs = []
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if not src.startswith("http"):
            if src.startswith("/"):
                src = base_url.rstrip("/") + src
        if not FILTER_IMG.search(src):
            imgs.append(src)
    imgs = list(dict.fromkeys(imgs))[:3]
    video = None
    vtag = soup.find("video")
    if vtag and vtag.get("src"):
        video = vtag["src"]
        if not video.startswith("http"):
            video = base_url.rstrip("/") + video
    return title, summary, imgs, video


async def get_article_links(session, site_url):
    html = await fetch_html(session, site_url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            if href.startswith("/"):
                href = site_url.rstrip("/") + href
        if re.search(r"(news|article|story|details|show)", href, re.I):
            links.append(href)
    return list(dict.fromkeys(links))[:5]


async def send_article(app, chat_id, url):
    if seen_col.find_one({"url": url, "chat_id": str(chat_id)}):
        return False
    async with aiohttp.ClientSession() as session:
        html = await fetch_html(session, url)
        if not html:
            return False
        title, summary, imgs, video = extract_article_data(html, url)
        caption = f"*{title}*\n\n{summary}\n\n[Source]({url})"
        media = []
        files = []
        if video:
            try:
                async with session.get(video) as v:
                    if v.status == 200:
                        vb = BytesIO(await v.read())
                        vb.name = "video.mp4"
                        files.append(vb)
                        media.append(InputMediaVideo(vb, caption=caption, parse_mode="Markdown"))
            except Exception:
                video = None
        for i, img_url in enumerate(imgs):
            try:
                async with session.get(img_url) as im:
                    if im.status == 200:
                        ib = BytesIO(await im.read())
                        ib.name = f"img_{i}.jpg"
                        files.append(ib)
                        if not media:
                            media.append(InputMediaPhoto(ib, caption=caption, parse_mode="Markdown"))
                        else:
                            media.append(InputMediaPhoto(ib))
            except Exception:
                continue
        try:
            if media:
                await app.bot.send_media_group(chat_id=chat_id, media=media)
            else:
                await app.bot.send_message(chat_id=chat_id, text=caption, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Send failed for {url}: {e}")
        seen_col.insert_one({"url": url, "chat_id": str(chat_id), "time": datetime.utcnow()})
        return True


async def fetch_news_loop(app):
    while True:
        feeds = list(feeds_col.find({}))
        logger.info(f"Checking {len(feeds)} feeds...")
        async with aiohttp.ClientSession() as session:
            for f in feeds:
                site = f["url"]
                chat_id = int(f["chat_id"])
                links = await get_article_links(session, site)
                for link in links:
                    await send_article(app, chat_id, link)
        logger.info(f"Sleeping for {FETCH_INTERVAL} seconds...")
        await asyncio.sleep(FETCH_INTERVAL)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Welcome to NewsBot!*\n\n"
        "Use these commands in any channel where I'm admin:\n"
        "/addfeeds <url> — add site\n"
        "/removefeeds <url> — remove site\n"
        "/listfeeds — show feeds\n"
        "/clearfeeds — clear feeds\n\n"
        "I’ll auto-post news every 15 minutes!"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def addfeeds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /addfeeds <url>")
        return
    url = context.args[0]
    if feeds_col.find_one({"url": url, "chat_id": str(chat_id)}):
        await update.message.reply_text("Feed already added.")
        return
    feeds_col.insert_one({"url": url, "chat_id": str(chat_id), "added_at": datetime.utcnow()})
    await update.message.reply_text(f"✅ Added feed: {url}")


async def listfeeds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    feeds = list(feeds_col.find({"chat_id": str(chat_id)}))
    if not feeds:
        await update.message.reply_text("No feeds added.")
    else:
        text = "\n".join(f"- {f['url']}" for f in feeds)
        await update.message.reply_text(f"Feeds:\n{text}")


async def removefeeds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /removefeeds <url>")
        return
    url = context.args[0]
    res = feeds_col.delete_one({"url": url, "chat_id": str(chat_id)})
    if res.deleted_count:
        await update.message.reply_text(f"❌ Removed feed: {url}")
    else:
        await update.message.reply_text("Feed not found.")


async def clearfeeds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    res = feeds_col.delete_many({"chat_id": str(chat_id)})
    await update.message.reply_text(f"🧹 Cleared {res.deleted_count} feeds.")


async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addfeeds", addfeeds))
    app.add_handler(CommandHandler("listfeeds", listfeeds))
    app.add_handler(CommandHandler("removefeeds", removefeeds))
    app.add_handler(CommandHandler("clearfeeds", clearfeeds))
    app.create_task(fetch_news_loop(app))
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())

import os
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Bot, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
import feedparser
import requests
from pymongo import MongoClient

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
FETCH_INTERVAL_HOURS = float(os.getenv("FETCH_INTERVAL_HOURS", "0.25"))
MONGO_URI = os.getenv("MONGO_URI")

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client["newsbot"]
posted_col = db["posted_urls"]
feeds_col = db["feeds"]

bot = Bot(token=TELEGRAM_TOKEN)

# Default Hindi news sources + newsonair custom site
default_feeds = [
    {"url": "https://www.republicbharat.com/rss/technology/gadgets.xml", "name": "R Bharat Gadgets"},
    {"url": "https://www.republicbharat.com/rss/entertainment/movie-review.xml", "name": "R Bharat Movie Reviews"},
    {"url": "https://www.republicbharat.com/rss/breaking.xml", "name": "R Bharat Breaking"},
    {"url": "http://www.amarujala.com/rss/breaking-news.xml", "name": "Breaking News"},
    {"url": "https://www.republicbharat.com/rss/videos/sports/cricket.xml", "name": "Cricket"},
    {"url": "https://www.republicbharat.com/rss/latest-news.xml", "name": "R Bharat Latest"},
    {"url": "https://www.bbc.com/hindi/index.xml", "name": "BBC Hindi"},
    {"url": "https://www.amarujala.com/rss/world-news.xml", "name": "Amar Ujala"},
    {"url": "https://www.newsonair.gov.in/hi/", "name": "News On Air Hindi"}  # Added manual site
]

# Ensure default feeds exist
for f in default_feeds:
    if feeds_col.find_one({"url": f["url"]}) is None:
        feeds_col.insert_one(f)

HEADERS = {"User-Agent": "Mozilla/5.0"}

async def download_file(url):
    """Download file for Telegram upload"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.read()
    except Exception as e:
        print(f"[ERROR] Download failed for {url}: {e}")
    return None


def is_valid_image(url: str) -> bool:
    """Filter out ad banners, logos, and non-content images"""
    if not url:
        return False
    url_lower = url.lower()
    bad_keywords = ["logo", "banner", "icon", "sprite", "ads", "advert", "favicon", "placeholder", "gif"]
    if any(k in url_lower for k in bad_keywords):
        return False
    if url_lower.endswith((".gif", ".svg")):
        return False
    return True


def scrape_article(url):
    """Scrape up to 3 main images (excluding ads/logos), plus title and summary"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        html = r.text
        soup = BeautifulSoup(html, "html.parser")

        # Title
        title_tag = soup.find("meta", property="og:title")
        title = title_tag["content"].strip() if title_tag and title_tag.get("content") else soup.title.get_text(strip=True)

        # Description
        desc_tag = soup.find("meta", property="og:description")
        summary = desc_tag["content"].strip() if desc_tag and desc_tag.get("content") else ""

        if not summary:
            paragraphs = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 40]
            if paragraphs:
                summary = "\n\n".join(paragraphs[:2])

        if len(summary) > 900:
            summary = summary[:900].rsplit(" ", 1)[0] + "..."

        # Collect up to 3 valid images
        all_imgs = [img.get("src") for img in soup.find_all("img") if img.get("src")]
        valid_imgs = [u for u in all_imgs if is_valid_image(u)]
        main_images = valid_imgs[:3] if valid_imgs else []

        # Video
        vid_tag = soup.find("meta", property="og:video")
        video_url = vid_tag["content"] if vid_tag and vid_tag.get("content") else None

        return title, summary, main_images, video_url

    except Exception as e:
        print(f"[ERROR] Failed scraping article {url}: {e}")
        return None, None, [], None


def fetch_newsonair_articles():
    """Custom scraper for NewsOnAir Hindi site"""
    try:
        r = requests.get("https://www.newsonair.gov.in/hi/", headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")

        articles = []
        news_cards = soup.select("div.news-item, div.card, li")  # flexible match
        for card in news_cards:
            link_tag = card.find("a", href=True)
            title_tag = card.find("h2") or card.find("h3") or card.find("a")
            if not link_tag or not title_tag:
                continue

            link = link_tag["href"]
            if not link.startswith("http"):
                link = "https://www.newsonair.gov.in" + link

            title = title_tag.get_text(strip=True)
            if not title or posted_col.find_one({"url": link}):
                continue

            articles.append({"url": link, "source_name": "News On Air Hindi"})
        return articles[:8]  # limit to few new articles
    except Exception as e:
        print(f"[ERROR] Failed to fetch newsonair articles: {e}")
        return []


async def fetch_feed_entries(feed):
    """Fetch the latest articles from RSS feed or custom source"""
    if "newsonair.gov.in" in feed["url"]:
        return fetch_newsonair_articles()

    try:
        feed_data = feedparser.parse(feed["url"], request_headers=HEADERS)
        entries = []
        for e in feed_data.entries:
            link = e.get("link")
            if link and posted_col.find_one({"url": link}) is None:
                entries.append({"url": link, "source_name": feed["name"]})
        return entries
    except Exception as e:
        print(f"[ERROR] Failed to fetch feed {feed['url']}: {e}")
        return []


async def post_news():
    feeds = list(feeds_col.find({}))
    for feed in feeds:
        try:
            articles = await fetch_feed_entries(feed)
            print(f"[INFO] {feed['name']} -> {len(articles)} new articles")

            for article in articles:
                title, summary, image_urls, video_url = scrape_article(article["url"])
                if not title:
                    continue

                text = f"*{title}*\n\n_{summary}_\n\n[Source: {article['source_name']}]({article['url']})"

                if video_url:
                    vid_data = await download_file(video_url)
                    if vid_data:
                        await bot.send_video(
                            chat_id=TARGET_CHAT_ID,
                            video=vid_data,
                            caption=text,
                            parse_mode=ParseMode.MARKDOWN,
                            supports_streaming=True
                        )
                        print(f"[INFO] Sent video post as media: {article['url']}")
                elif image_urls:
                    images = []
                    for idx, img_url in enumerate(image_urls):
                        img_data = await download_file(img_url)
                        if img_data:
                            if idx == 0:
                                images.append(InputMediaPhoto(media=img_data, caption=text, parse_mode=ParseMode.MARKDOWN))
                            else:
                                images.append(InputMediaPhoto(media=img_data))
                    if images:
                        await bot.send_media_group(chat_id=TARGET_CHAT_ID, media=images)
                        print(f"[INFO] Sent multi-image post: {article['url']}")
                else:
                    await bot.send_message(chat_id=TARGET_CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN)
                    print(f"[INFO] Sent text-only post: {article['url']}")

                posted_col.insert_one({"url": article["url"]})

        except Exception as e:
            print(f"[ERROR] Processing feed {feed['url']} failed: {e}")


# Telegram commands
async def add_feed(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        url = context.args[0]
        name = " ".join(context.args[1:]).strip()
        
        if not name:
            feed_data = feedparser.parse(url)
            if feed_data.feed.get("title"):
                name = feed_data.feed.title
            else:
                name = url

        if feeds_col.find_one({"url": url}) is None:
            feeds_col.insert_one({"url": url, "name": name})
            await update.message.reply_text(f"✅ Feed '{name}' added successfully!")
        else:
            await update.message.reply_text(f"⚠️ Feed '{name}' already exists.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error adding feed: {e}")


async def remove_feed(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        url = context.args[0]
        result = feeds_col.delete_one({"url": url})
        if result.deleted_count > 0:
            await update.message.reply_text(f"🗑 Feed removed successfully!")
        else:
            await update.message.reply_text(f"⚠️ Feed not found.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error removing feed: {e}")


async def list_feeds(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        feeds = list(feeds_col.find({}))
        if not feeds:
            await update.message.reply_text("⚠️ No feeds found.")
            return
        feed_list = "\n".join([f"📰 [{f.get('name', 'Unnamed')}]({f['url']})" for f in feeds])
        await update.message.reply_text(f"🗞 *Active Feeds:*\n\n{feed_list}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ Error listing feeds: {e}")


async def main_loop():
    while True:
        await post_news()
        print(f"[INFO] Sleeping for {FETCH_INTERVAL_HOURS} hours")
        await asyncio.sleep(FETCH_INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("addfeed", add_feed))
    app.add_handler(CommandHandler("removefeed", remove_feed))
    app.add_handler(CommandHandler("listfeeds", list_feeds))

    print("[INFO] Bot started.")
    asyncio.get_event_loop().create_task(main_loop())
    app.run_polling()

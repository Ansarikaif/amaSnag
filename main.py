import os

import re

import random

import logging

import sqlite3

import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

from telegram.ext import (

    ApplicationBuilder,

    CommandHandler,

    ContextTypes,

    CallbackQueryHandler,

    MessageHandler,

    filters,

    ExtBot,

)

from telegram.request import HTTPXRequest

from playwright.async_api import async_playwright

from bs4 import BeautifulSoup

from apscheduler.schedulers.asyncio import AsyncIOScheduler



# === CONFIG (Using Environment Variables for Safety) ===

TOKEN = os.getenv("TOKEN")

CHANNEL_ID = os.getenv("CHANNEL_ID")

AFFILIATE_TAG = os.getenv("AFFILIATE_TAG")

SCRAPE_URL = 'https://www.amazon.in/deals'

POST_INTERVAL = 1800  # 30 minutes

ADMIN_IDS = [672417973]



# === NEW: Set DB_PATH to a persistent volume mount path ===

# This tells the bot to save the database in the '/data' directory,

# which we will link to a persistent Railway Volume.

DB_PATH = '/data/deals.db'

TRACKED_EMOJI = '🔍'



# === LOGGER (FIXED for Windows Emoji Support) ===

logging.basicConfig(

    level=logging.INFO,

    format='%(asctime)s - %(levelname)s - %(message)s',

    handlers=[

        logging.FileHandler("bot.log", encoding='utf-8'),

        logging.StreamHandler()

    ]

)

logger = logging.getLogger(__name__)



USER_AGENTS = [

    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",

    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",

    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36"

]

# === DATABASE (No changes needed in functions) ===

def init_db():

    # Ensure the directory for the database exists

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute("CREATE TABLE IF NOT EXISTS deals (asin TEXT PRIMARY KEY, discount INTEGER)")

    c.execute("CREATE TABLE IF NOT EXISTS user_tracking (user_id INTEGER, asin TEXT, PRIMARY KEY (user_id, asin))")

    c.execute("CREATE TABLE IF NOT EXISTS user_preferences (user_id INTEGER PRIMARY KEY, min_discount INTEGER DEFAULT 5)")

    c.execute("CREATE TABLE IF NOT EXISTS user_notified (user_id INTEGER, asin TEXT, PRIMARY KEY(user_id, asin))")

    conn.commit()

    conn.close()



def is_new_or_updated_deal(asin, discount):

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute("SELECT discount FROM deals WHERE asin=?", (asin,))

    row = c.fetchone()

    notify_users = False

    if row is None:

        c.execute("INSERT INTO deals VALUES (?, ?)", (asin, discount))

        notify_users = True

    elif discount > row[0]:

        c.execute("UPDATE deals SET discount=? WHERE asin=?", (discount, asin))

        notify_users = True

    conn.commit()

    conn.close()

    return notify_users



def get_users_tracking_asin(asin):

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute("SELECT user_id FROM user_tracking WHERE asin=?", (asin,))

    users = c.fetchall()

    conn.close()

    return [u[0] for u in users]



def get_user_min_discount(user_id):

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute("SELECT min_discount FROM user_preferences WHERE user_id=?", (user_id,))

    row = c.fetchone()

    conn.close()

    return row[0] if row else 5



def set_user_min_discount(user_id, discount):

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute("INSERT OR REPLACE INTO user_preferences (user_id, min_discount) VALUES (?, ?)", (user_id, discount))

    conn.commit()

    conn.close()



def add_user_track(user_id, asin):

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute("SELECT 1 FROM user_tracking WHERE user_id=? AND asin=?", (user_id, asin))

    exists = c.fetchone()

    if exists:

        conn.close()

        return False

    c.execute("INSERT INTO user_tracking (user_id, asin) VALUES (?, ?)", (user_id, asin))

    conn.commit()

    conn.close()

    return True



def remove_user_track(user_id, asin):

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute("DELETE FROM user_tracking WHERE user_id=? AND asin=?", (user_id, asin))

    conn.commit()

    conn.close()



def mark_user_notified(user_id, asin):

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    try:

        c.execute("INSERT INTO user_notified (user_id, asin) VALUES (?, ?)", (user_id, asin))

        conn.commit()

    except sqlite3.IntegrityError:

        pass

    conn.close()



def has_user_been_notified(user_id, asin):

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute("SELECT 1 FROM user_notified WHERE user_id=? AND asin=?", (user_id, asin))

    row = c.fetchone()

    conn.close()

    return bool(row)



def clear_user_notifications(asin):

    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute("DELETE FROM user_notified WHERE asin=?", (asin,))

    conn.commit()

    conn.close()





# === CATEGORY (No changes) ===

def get_category(title):

    title = title.lower()

    if any(word in title for word in ["laptop", "notebook", "macbook"]): return "Laptops"

    if any(word in title for word in ["phone", "smartphone", "galaxy", "iphone"]): return "Smartphones"

    if any(word in title for word in ["headphone", "earbuds", "airpods"]): return "Audio"

    if any(word in title for word in ["shoes", "sneaker", "sandals"]): return "Footwear"

    if any(word in title for word in ["watch", "smartwatch"]): return "Watches"

    return "Deals"



# === SCRAPER (IMPROVED with Anti-Scraping Evasion) ===

async def scrape_deals():

    """

    Scrapes Amazon deals with anti-scraping measures.

    """

    logger.info("Starting scrape using Playwright...")

    deals = []

    async with async_playwright() as p:

        browser = None

        try:

            # Note: On Railway, you might need to specify chromium path if issues arise

            # but their base images are usually well-configured.

            browser = await p.chromium.launch(headless=True)

            context = await browser.new_context(

                user_agent=random.choice(USER_AGENTS),

                viewport={'width': 1920, 'height': 1080},

                locale='en-US',

            )

            page = await context.new_page()

            await page.goto(SCRAPE_URL, timeout=60000, wait_until='domcontentloaded')

            await page.wait_for_selector('div[data-testid="product-card"]', timeout=20000)

            await page.wait_for_timeout(random.randint(3000, 6000))

            content = await page.content()

            await context.close()

            await browser.close()

        except Exception as e:

            logger.error(f"Playwright scrape failed during page load: {e}")

            if browser:

                await browser.close()

            return []



    soup = BeautifulSoup(content, 'html.parser')

    product_cards = soup.find_all('div', {'data-testid': 'product-card'})



    for card in product_cards:

        asin = card.get('data-asin')

        if not asin: continue



        title = 'No Title Found'

        discount_percent = 0



        title_p_tag = card.find('p', {'id': f'title-{asin}'})

        if title_p_tag:

            title_span = title_p_tag.find('span', class_='a-truncate-full')

            if title_span:

                title = title_span.get_text(strip=True)



        badge_container = card.find('div', {'data-component': 'dui-badge'})

        if badge_container:

            discount_tag = badge_container.find('span', string=re.compile(r'(\d+%\s*off)'))

            if discount_tag:

                match = re.search(r'(\d+)', discount_tag.get_text())

                if match:

                    discount_percent = int(match.group(1))



        coupon_text = 'No Coupon'

        coupon_tag = card.find(['div', 'span'], string=re.compile(r'coupon', re.IGNORECASE))

        if coupon_tag and "coupon" in coupon_tag.get_text(strip=True).lower():

            coupon_text = coupon_tag.get_text(strip=True)



        image_tag = card.find('img')

        image = image_tag['src'] if image_tag else ''

        link = f"https://www.amazon.in/dp/{asin}/?tag={AFFILIATE_TAG}"



        if title == 'No Title Found' or discount_percent == 0:

            logger.warning(f"Skipping ASIN {asin}. Title Found: '{title}', Discount Found: {discount_percent}%")

            continue



        if not is_new_or_updated_deal(asin, discount_percent):

            continue



        clear_user_notifications(asin)

        deals.append({

            'asin': asin, 'title': title, 'discount': f"{discount_percent}% off", 'coupon': coupon_text,

            'image': image, 'link': link, 'category': get_category(title), 'discount_val': discount_percent

        })



    if not deals and product_cards:

        logger.error("CRITICAL: Scraper found product cards but failed to extract details from ANY. Selectors may be outdated.")

    else:

        logger.info(f"Scrape complete. Found {len(deals)} new/updated deals.")

        

    return deals





# === TELEGRAM FUNCTIONS ===

async def post_deals(context: ContextTypes.DEFAULT_TYPE = None):

    bot = context.bot if context else ApplicationBuilder().token(TOKEN).build().bot

    deals = await scrape_deals()

    for deal in deals:

        msg = f"✨ <b>{deal['title']}</b>\n\n"

        msg += f"🔹 {deal['discount']}\n"

        if deal['coupon'] != 'No Coupon':

             msg += f"💳 {deal['coupon']}\n"

        msg += f"🌂 {deal['category']}\n"

        msg += f"🔗 <a href='{deal['link']}'>Check The Deal</a>"

        msg += "\n\nFor more deals and features, join our bot: <a href='https://t.me/AmaSnag_Bot'>@AmaSnag_Bot</a>"



        keyboard = InlineKeyboardMarkup([

            [

                InlineKeyboardButton("🔍 Track Deal", callback_data=f"track_{deal['asin']}"),

                InlineKeyboardButton("❌ Untrack", callback_data=f"untrack_{deal['asin']}")

            ],

            [InlineKeyboardButton("📣 Share", url=f"https://t.me/share/url?url={deal['link']}")]

        ])



        try:

            if deal['image']:

                await bot.send_photo(chat_id=CHANNEL_ID, photo=deal['image'], caption=msg, parse_mode='HTML', reply_markup=keyboard)

            else:

                await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode='HTML', reply_markup=keyboard)



            users = get_users_tracking_asin(deal['asin'])

            for user in users:

                if has_user_been_notified(user, deal['asin']):

                    continue

                min_disc = get_user_min_discount(user)

                if deal['discount_val'] >= min_disc:

                    await bot.send_message(chat_id=user, text=f"🔔 New discount on tracked item: {deal['title']} - {deal['discount']}\n<a href='{deal['link']}'>Check Deal</a>", parse_mode='HTML')

                    mark_user_notified(user, deal['asin'])

        except Exception as e:

            logger.warning(f"Failed to send deal for ASIN {deal.get('asin', 'N/A')}: {e}")

        await asyncio.sleep(2)





async def my_deals(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    page = 1

    user = None

    message_to_reply = None



    if query:

        await query.answer()

        user = query.from_user

        message_to_reply = query.message

        match = re.match(r'^mydeals_page_(\d+)$', query.data)

        if match:

            page = int(match.group(1))

    else:

        user = update.message.from_user

        message_to_reply = update.message



    user_id = user.id



    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()

    c.execute("SELECT asin FROM user_tracking WHERE user_id=?", (user_id,))

    rows = c.fetchall()

    conn.close()



    if not rows:

        await message_to_reply.reply_text("❌ You are not tracking any deals.")

        return



    per_page = 5

    start = (page - 1) * per_page

    total_pages = (len(rows) + per_page - 1) // per_page

    page_data = rows[start:start + per_page]



    if not page_data:

        await message_to_reply.reply_text(f"❓ No deals found on page {page}.")

        return



    if query:

        await context.bot.send_message(chat_id=user_id, text=f"📖 Here is Page {page}/{total_pages} of your tracked deals:")

    else:

        await message_to_reply.reply_text(f"📖 Here are your tracked deals:")



    for asin_tuple in page_data:

        asin = asin_tuple[0]

        title_link = f"https://www.amazon.in/dp/{asin}?tag={AFFILIATE_TAG}"

        image = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.jpg"

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Untrack", callback_data=f"untrack_{asin}")]])

        try:

            await context.bot.send_photo(

                chat_id=user_id,

                photo=image,

                caption=f"<b><a href='{title_link}'>View Product</a></b>",

                parse_mode='HTML',

                reply_markup=keyboard

            )

        except Exception as e:

            logger.warning(f"Could not send photo for ASIN {asin}: {e}. Sending text fallback.")

            await context.bot.send_message(

                chat_id=user_id,

                text=f"<b><a href='{title_link}'>View Product</a></b> (Image unavailable)",

                parse_mode='HTML',

                reply_markup=keyboard

            )

        await asyncio.sleep(0.5)



    nav_buttons = []

    if page > 1:

        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"mydeals_page_{page - 1}"))

    if page < total_pages:

        nav_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"mydeals_page_{page + 1}"))



    if nav_buttons:

        await context.bot.send_message(

            chat_id=user_id,

            text=f"📄 Page {page}/{total_pages}",

            reply_markup=InlineKeyboardMarkup([nav_buttons])

        )



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    welcome_msg = (

        "👋 <b>Welcome to <a href='https://t.me/AmaSnag'>AmaSnag Deals Bot</a>!</b>\n\n"

        "🛍️ Find hot Amazon India deals with big discounts, coupons, and easy tracking.\n\n"

        "Use /help to see all available commands."

    )

    await update.message.reply_text(welcome_msg, parse_mode='HTML', disable_web_page_preview=True)



async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    help_text = (

        "<b>🛠 Commands Available:</b>\n"

        "━━━━━━━━━━━━━━━\n"

        "🔎 /mydeals – See your tracked deals.\n"

        "📉 /setdiscount 30 – Get alerts only for deals with ≥ 30% off.\n"

        "ℹ️ /help – View this help message.\n\n"

    )



    if user_id in ADMIN_IDS:

        help_text += (

            "<b>👑 Admin Commands:</b>\n"

            "📤 /post – Post the latest scraped deals to the channel.\n"

            "📂 /getdb – Receive the `deals.db` database file.\n\n"

        )



    help_text += (

        "📌 <b>Inline Buttons:</b>\n"

        "🔍 Track – Get alerts when discounts increase\n"

        "❌ Untrack – Stop alerts for a deal\n"

        "📣 Share – Send the deal to friends\n\n"

        "❤️ Powered by @AmaSnag"

    )

    await update.message.reply_text(help_text, parse_mode='HTML', disable_web_page_preview=True)





async def set_discount(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if not context.args:

        current_discount = get_user_min_discount(user_id)

        await update.message.reply_text(

            f"Your current alert is set for deals with {current_discount}% or more discount.\n\n"

            "To change it, use the command with a number (1-99).\n"

            "<b>Example:</b> /setdiscount 50",

            parse_mode='HTML'

        )

        return

    try:

        discount_value = int(context.args[0])

        if not 1 <= discount_value <= 99:

            raise ValueError("Discount must be between 1 and 99.")

        set_user_min_discount(user_id, discount_value)

        await update.message.reply_text(

            f"✅ Success! You will now be notified for deals with <b>{discount_value}%</b> or more discount.",

            parse_mode='HTML'

        )

    except (IndexError, ValueError):

        await update.message.reply_text(

            "❌ <b>Invalid format.</b> Please provide a number between 1 and 99.\n"

            "<b>Example:</b> /setdiscount 50",

            parse_mode='HTML'

        )



async def handle_untrack_button(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    asin = query.data.replace('untrack_', '')

    remove_user_track(user_id, asin)

    await context.bot.send_message(chat_id=user_id, text=f"❌ Deal with ASIN `{asin}` has been untracked.", parse_mode='Markdown')

    await query.edit_message_reply_markup(reply_markup=None)



async def handle_track_button(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    user_id = query.from_user.id

    asin = query.data.replace('track_', '')

    added = add_user_track(user_id, asin)

    if added:

        await query.answer(text=f"{TRACKED_EMOJI} Deal tracked!")

    else:

        await query.answer(text=f"{TRACKED_EMOJI} Already tracking.")



async def manual_post(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id not in ADMIN_IDS:

        await update.message.reply_text("🚫 Unauthorized.")

        return

    await update.message.reply_text("📤 Posting latest deals to the channel...")

    await post_deals(context)

    await update.message.reply_text("✅ Posting complete.")



async def get_db(update: Update, context: ContextTypes.DEFAULT_TYPE):

    """Sends the database file to an admin."""

    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:

        logger.warning(f"Unauthorized /getdb attempt by user ID: {user_id}")

        return



    logger.info(f"Admin user {user_id} requested the database.")

    try:

        with open(DB_PATH, 'rb') as db_file:

            await context.bot.send_document(

                chat_id=user_id,

                document=db_file,

                filename=os.path.basename(DB_PATH)

            )

        logger.info(f"Database file sent successfully to admin {user_id}.")

    except FileNotFoundError:

        logger.error(f"Database file not found at path: {DB_PATH}")

        await update.message.reply_text("❌ Error: The database file could not be found.")

    except Exception as e:

        logger.error(f"Failed to send database file to admin {user_id}: {e}")

        await update.message.reply_text("❌ An unexpected error occurred while sending the database file.")





# === MAIN ===

async def main() -> None:

    """Start the bot and the scheduler."""

    init_db()



    request = HTTPXRequest(

        connect_timeout=10.0,

        read_timeout=20.0,

    )

    bot = ExtBot(token=TOKEN, request=request)

    app = ApplicationBuilder().bot(bot).build()



    # Add all your handlers

    app.add_handler(CommandHandler('start', start))

    app.add_handler(CommandHandler('help', help_command))

    app.add_handler(CommandHandler('mydeals', my_deals))

    app.add_handler(CommandHandler('post', manual_post))

    app.add_handler(CommandHandler('getdb', get_db))

    app.add_handler(CommandHandler('setdiscount', set_discount))

    app.add_handler(CallbackQueryHandler(handle_track_button, pattern=r'^track_'))

    app.add_handler(CallbackQueryHandler(handle_untrack_button, pattern=r'^untrack_'))

    app.add_handler(CallbackQueryHandler(my_deals, pattern=r'^mydeals_page_'))



    # Initialize the scheduler

    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

    scheduler.add_job(post_deals, 'interval', seconds=POST_INTERVAL, args=[app])



    async with app:

        scheduler.start()

        logger.info("Scheduler has started.")

        await app.initialize()

        await app.updater.start_polling()

        await app.start()

        logger.info("Bot has started successfully and is polling for updates.")

        await asyncio.Event().wait()



if __name__ == '__main__':

    try:

        asyncio.run(main())

    except (KeyboardInterrupt, SystemExit):

        logger.info("Bot stopped by user.")

    except Exception as e:

        logger.error(f"An unexpected error occurred in main: {e}", exc_info=True)

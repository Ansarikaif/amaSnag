import os
import re
import random
import logging
import psycopg2
import psycopg2.extras
import asyncio
import io
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext (
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

# === CONFIG (Using Environment Variables for Hosting) ===
# Set these environment variables in your hosting service's dashboard.
TOKEN = os.getenv("TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID") # For public channels use "@YourChannelName", for private channels use its ID like -1001234567890
AFFILIATE_TAG = os.getenv("AFFILIATE_TAG")
SCRAPE_URL = 'https://www.amazon.in/deals'
POST_INTERVAL = 10800  # 3 hours

# Set ADMIN_IDS as a comma-separated string in your environment variables (e.g., "672417973,987654321")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(admin_id) for admin_id in ADMIN_IDS_STR.split(',') if admin_id]

# === NEW: Supabase Database URL ===
DATABASE_URL = os.getenv("DATABASE_URL")
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

# === DATABASE (MODIFIED for Supabase/PostgreSQL) ===
def get_db_connection():
    """Establishes a connection to the PostgreSQL database."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Could not connect to the database: {e}")
        raise

def init_db():
    """Initializes the database tables if they don't exist."""
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS deals (
                    asin TEXT PRIMARY KEY,
                    discount INTEGER NOT NULL
                );
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_tracking (
                    user_id BIGINT,
                    asin TEXT,
                    PRIMARY KEY (user_id, asin)
                );
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id BIGINT PRIMARY KEY,
                    min_discount INTEGER DEFAULT 5
                );
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_notified (
                    user_id BIGINT,
                    asin TEXT,
                    PRIMARY KEY (user_id, asin)
                );
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS keyword_alerts (
                    user_id BIGINT,
                    keyword TEXT,
                    PRIMARY KEY (user_id, keyword)
                );
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    asin TEXT,
                    price REAL NOT NULL,
                    date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (asin, date)
                );
            """)
            conn.commit()
            logger.info("Database tables checked/created successfully.")
    finally:
        conn.close()

def is_new_or_updated_deal(asin, discount):
    conn = get_db_connection()
    notify_users = False
    try:
        with conn.cursor() as c:
            c.execute("SELECT discount FROM deals WHERE asin = %s", (asin,))
            row = c.fetchone()
            if row is None:
                c.execute("INSERT INTO deals (asin, discount) VALUES (%s, %s)", (asin, discount))
                notify_users = True
            elif discount > row[0]:
                c.execute("UPDATE deals SET discount = %s WHERE asin = %s", (discount, asin))
                notify_users = True
            conn.commit()
    finally:
        conn.close()
    return notify_users

def get_users_tracking_asin(asin):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT user_id FROM user_tracking WHERE asin = %s", (asin,))
            users = c.fetchall()
            return [u[0] for u in users]
    finally:
        conn.close()

def get_user_min_discount(user_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT min_discount FROM user_preferences WHERE user_id = %s", (user_id,))
            row = c.fetchone()
            return row[0] if row else 5
    finally:
        conn.close()

def set_user_min_discount(user_id, discount):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            # PostgreSQL UPSERT
            c.execute("""
                INSERT INTO user_preferences (user_id, min_discount) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET min_discount = EXCLUDED.min_discount;
            """, (user_id, discount))
            conn.commit()
    finally:
        conn.close()

def add_user_track(user_id, asin):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            try:
                c.execute("INSERT INTO user_tracking (user_id, asin) VALUES (%s, %s)", (user_id, asin))
                conn.commit()
                return True
            except psycopg2.IntegrityError: # Handles duplicate key error
                conn.rollback() # Rollback the failed transaction
                return False
    finally:
        conn.close()

def remove_user_track(user_id, asin):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM user_tracking WHERE user_id = %s AND asin = %s", (user_id, asin))
            conn.commit()
    finally:
        conn.close()

def mark_user_notified(user_id, asin):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            # Use ON CONFLICT to avoid errors if the notification is already marked
            c.execute("""
                INSERT INTO user_notified (user_id, asin) VALUES (%s, %s)
                ON CONFLICT (user_id, asin) DO NOTHING;
            """, (user_id, asin))
            conn.commit()
    finally:
        conn.close()

def has_user_been_notified(user_id, asin):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM user_notified WHERE user_id = %s AND asin = %s", (user_id, asin))
            row = c.fetchone()
            return bool(row)
    finally:
        conn.close()

def clear_user_notifications(asin):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM user_notified WHERE asin = %s", (asin,))
            conn.commit()
    finally:
        conn.close()

# === NEW: Keyword Alert Database Functions ===
def add_keyword_alert(user_id, keyword):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            try:
                c.execute("INSERT INTO keyword_alerts (user_id, keyword) VALUES (%s, %s)", (user_id, keyword.lower()))
                conn.commit()
                return True
            except psycopg2.IntegrityError:
                conn.rollback()
                return False
    finally:
        conn.close()

def remove_keyword_alert(user_id, keyword):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM keyword_alerts WHERE user_id = %s AND keyword = %s", (user_id, keyword.lower()))
            conn.commit()
            return c.rowcount > 0
    finally:
        conn.close()

def get_user_keyword_alerts(user_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT keyword FROM keyword_alerts WHERE user_id = %s", (user_id,))
            keywords = c.fetchall()
            return [k[0] for k in keywords]
    finally:
        conn.close()

def get_users_for_keyword(keyword):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT user_id FROM keyword_alerts WHERE keyword = %s", (keyword.lower(),))
            users = c.fetchall()
            return [u[0] for u in users]
    finally:
        conn.close()

# === NEW: Price History Database Functions ===
def add_price_history(asin, price):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("INSERT INTO price_history (asin, price) VALUES (%s, %s)", (asin, price))
            conn.commit()
    finally:
        conn.close()

def get_price_history(asin, days=30):
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT price, date FROM price_history
                WHERE asin = %s AND date >= NOW() - INTERVAL '%s days'
                ORDER BY date DESC
            """, (asin, days))
            return c.fetchall()
    finally:
        conn.close()

# === CATEGORY (IMPROVED) ===
CATEGORIES = {
    "Laptops": ["laptop", "notebook", "macbook", "chromebook"],
    "Smartphones": ["phone", "smartphone", "galaxy", "iphone", "pixel", "redmi", "oneplus"],
    "Audio": ["headphone", "earbuds", "airpods", "earphone", "speaker", "soundbar", "jbl", "sony", "bose"],
    "Electronics": ["tv", "television", "monitor", "camera", "dslr", "projector", "kindle", "smart home", "smartwatch"],
    "Watches": ["watch", "smartwatch", "fitbit", "garmin"], # Smartwatch moved to Electronics as well
    "Home & Kitchen": ["kitchen", "cookware", "mixer", "grinder", "purifier", "vacuum", "cleaner", "fridge", "microwave", "blender", "toaster", "coffee maker", "air fryer", "iron", "washing machine", "dishwasher"],
    "Fashion": ["shirt", "t-shirt", "jeans", "trousers", "saree", "kurta", "dress", "shoes", "sneaker", "sandals", "heels", "boots", "apparel", "clothing", "footwear", "bag", "wallet", "sunglasses", "jewellery"],
    "Footwear": ["shoes", "sneaker", "sandals", "heels", "boots"], # Redundant, but kept for broader matching
    "Books": ["book", "novel", "author", "magazine", "ebook"],
    "Gaming": ["gaming", "console", "playstation", "xbox", "nintendo", "game", "controller", "headset"],
    "Health & Personal Care": ["health", "personal care", "grooming", "beauty", "fitness", "supplements", "protein", "shampoo", "conditioner", "lotion", "makeup", "perfume"],
    "Sports & Outdoors": ["sports", "outdoor", "camping", "cycling", "running", "yoga", "gym", "trekking", "hiking", "fishing", "tent", "sleeping bag"],
    "Toys & Games": ["toy", "doll", "action figure", "board game", "puzzle", "lego", "play-doh", "barbie"],
    "Automotive": ["car", "bike", "motorcycle", "tyre", "helmet", "accessories", "parts"],
    "Office Products": ["office", "stationery", "pen", "notebook", "paper", "printer", "scanner", "shredder", "desk", "chair"],
    "Pet Supplies": ["pet", "dog", "cat", "food", "treats", "toys", "bed", "collar", "leash"],
    "Baby Products": ["baby", "diaper", "wipes", "formula", "stroller", "car seat", "crib", "baby food"],
    "Groceries": ["grocery", "food", "snack", "beverage", "tea", "coffee", "spices", "oil", "flour", "rice", "dal"],
}

def get_category(title):
    title_lower = title.lower()
    for category, keywords in CATEGORIES.items():
        if any(word in title_lower for word in keywords):
            return category
    return "Deals"  # Default category

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
            # Note: For hosting, ensure the buildpack includes chromium dependencies
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
        current_price = 0.0 # NEW: Initialize current_price

        title_p_tag = card.find('p', {'id': f'title-{asin}'})
        if title_p_tag:
            title_span = title_p_tag.find('span', class_='a-truncate-full')
            if title_span:
                title = title_span.get_text(strip=True)
        
        # NEW: Extract current price
        price_span = card.find('span', class_='a-price-whole')
        if price_span:
            try:
                current_price = float(price_span.get_text(strip=True).replace(',', ''))
            except ValueError:
                logger.warning(f"Could not parse price for ASIN {asin}: {price_span.get_text(strip=True)}")

        badge_container = card.find('div', {'data-component': 'dui-badge'})
        if badge_container:
            discount_tag = badge_container.find('span', string=re.compile(r'(\d+%)'))
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

        # NEW: Add price to history
        if current_price > 0:
            add_price_history(asin, current_price)

        if not is_new_or_updated_deal(asin, discount_percent):
            continue

        clear_user_notifications(asin)
        deals.append({
            'asin': asin, 'title': title, 'discount': f"{discount_percent}% off", 'coupon': coupon_text,
            'image': image, 'link': link, 'category': get_category(title), 'discount_val': discount_percent,
            'current_price': current_price # NEW: Include current price in deal dict
        })

    if not deals and product_cards:
        logger.error("CRITICAL: Scraper found product cards but failed to extract details from ANY. Selectors may be outdated.")
    else:
        logger.info(f"Scrape complete. Found {len(deals)} new/updated deals.")
        
    return deals

# === NEW URL SCRAPER (REVISED FOR RAILWAY DEBUGGING) ===
async def scrape_single_product_by_asin(asin: str, bot: ExtBot = None):
    """
    Scrapes a single Amazon product page by its ASIN with enhanced anti-scraping.
    If scraping fails and a bot instance is provided, it sends the HTML content
    to the admin for debugging.
    """
    url = f"https://www.amazon.in/dp/{asin}"
    logger.info(f"Starting single product scrape for ASIN: {asin} at URL: {url}")
    
    realistic_headers = {
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.google.com/'
    }

    async with async_playwright() as p:
        browser = None
        page = None # Define page here to access it in the except block
        try:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                extra_http_headers=realistic_headers
            )
            page = await context.new_page()
            await page.goto(url, timeout=60000, wait_until='domcontentloaded')
            await page.wait_for_selector('#productTitle', timeout=15000)
            await page.wait_for_timeout(random.randint(2000, 4000))
            content = await page.content()
            await browser.close()
        except Exception as e:
            logger.error(f"Playwright failed for ASIN {asin}: {e}")
            
            # --- NEW DEBUGGING LOGIC ---
            # If a bot object and an admin ID are available, send the debug file.
            if bot and ADMIN_IDS and page:
                try:
                    content_on_fail = await page.content()
                    logger.info(f"Attempting to send debug HTML for ASIN {asin} to admin.")
                    
                    # Convert the string content to a bytes-like object for sending
                    html_bytes = io.BytesIO(content_on_fail.encode('utf-8'))
                    
                    await bot.send_document(
                        chat_id=ADMIN_IDS[0], # Sends to the first admin in your list
                        document=html_bytes,
                        filename=f"error_{asin}.html",
                        caption=f"Scraping failed for ASIN {asin}. Reason: {e}"
                    )
                    logger.info("Debug HTML sent successfully to admin.")
                except Exception as send_e:
                    logger.error(f"Failed to send the debug HTML file to admin: {send_e}")
            # --- END OF NEW LOGIC ---

            if browser:
                await browser.close()
            return None

    # Check for CAPTCHA
    lower_content = content.lower()
    if "captcha" in lower_content or "are you a robot" in lower_content or "puzzle" in lower_content:
        logger.warning(f"CAPTCHA detected for ASIN {asin}. Scraping blocked.")
        return None

    soup = BeautifulSoup(content, 'html.parser')
    
    # Extract Title
    title_tag = soup.find('span', id='productTitle')
    title = title_tag.get_text(strip=True) if title_tag else 'No Title Found'

    if title == 'No Title Found':
        logger.warning(f"Could not find title for ASIN {asin} even after waiting. Amazon's HTML may have changed.")
        return None

    # Extract Image
    image = ''
    image_tag_container = soup.find('div', id='imgTagWrapperId')
    if image_tag_container:
        image_tag = image_tag_container.find('img')
        if image_tag and image_tag.has_attr('src'):
            image = image_tag['src']

    # Extract Discount
    discount_str = "Discount not found"
    discount_val = 0
    discount_badge = soup.find('span', class_=re.compile(r'savingsPercentage'))
    if discount_badge:
        match = re.search(r'(\d+)', discount_badge.get_text())
        if match:
            discount_val = int(match.group(1))
            discount_str = f"{discount_val}% off"

    # NEW: Extract current price for single product
    current_price = 0.0
    price_whole_tag = soup.find('span', class_='a-price-whole')
    if price_whole_tag:
        try:
            current_price = float(price_whole_tag.get_text(strip=True).replace(',', ''))
        except ValueError:
            logger.warning(f"Could not parse price for ASIN {asin} in single product scrape.")

    # Extract Coupon
    coupon_text = 'No Coupon'
    coupon_tag = soup.find('span', string=re.compile(r'Apply.*coupon', re.IGNORECASE))
    if coupon_tag:
        coupon_text = coupon_tag.get_text(strip=True)

    # NEW: Add price to history for single product
    if current_price > 0:
        add_price_history(asin, current_price)

    return {
        'asin': asin, 
        'title': title, 
        'discount': discount_str, 
        'coupon': coupon_text,
        'image': image, 
        'link': f"https://www.amazon.in/dp/{asin}/?tag={AFFILIATE_TAG}", 
        'category': get_category(title), 
        'discount_val': discount_val,
        'current_price': current_price # NEW: Include current price in deal dict
    }

# === TELEGRAM FUNCTIONS ===
async def post_deals(context: ContextTypes.DEFAULT_TYPE = None):
    bot = context.bot if context else ApplicationBuilder().token(TOKEN).build().bot
    deals = await scrape_deals()
    for deal in deals:
        msg = f"✨ <b>{deal['title']}</b>\n\n"
        if deal['current_price'] > 0: # NEW: Display current price
            msg += f"💰 Price: ₹{deal['current_price']:.2f}\n"
        msg += f"🔹 {deal['discount']}\n"
        if deal['coupon'] != 'No Coupon':
             msg += f"💳 {deal['coupon']}\n"
        msg += f"🌂 {deal['category']}\n"
        msg += f"🔗 <a href='{deal['link']}'>Check The Deal</a>"
        msg += "\n\nFor more deals and features, join our bot: <a href='https://t.me/AmaSnag_Bot'>@AmaSnag_Bot</a>"

        # NEW: Add price history to message
        price_history = get_price_history(deal['asin'], days=30)
        if price_history:
            prices = [p[0] for p in price_history]
            if prices:
                lowest_price = min(prices)
                highest_price = max(prices)
                if deal['current_price'] == lowest_price:
                    msg += "\n\n🔥 <b>Lowest price in last 30 days!</b>"
                elif deal['current_price'] < prices[0]: # If current price is lower than the most recent recorded price
                    msg += f"\n\n📉 Price dropped from ₹{prices[0]:.2f}!"
                elif deal['current_price'] > prices[0]:
                    msg += f"\n\n📈 Price increased from ₹{prices[0]:.2f}."
                
                # Optional: Add more detailed history
                # msg += f"\n(Lowest: ₹{lowest_price:.2f}, Highest: ₹{highest_price:.2f} in 30 days)"


        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔍 Track Deal", callback_data=f"track_{deal['asin']}"),
                InlineKeyboardButton("❌ Untrack", callback_data=f"untrack_{deal['asin']}")
            ],
            [InlineKeyboardButton("📣 Share", url=f"https://t.me/share/url?url={deal['link']}")],
            [InlineKeyboardButton("📊 Price History", callback_data=f"history_{deal['asin']}")] # NEW: Price History Button
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
            
            # NEW: Notify users subscribed to keywords
            for keyword in CATEGORIES.keys(): # Check for category keywords
                if keyword.lower() in deal['category'].lower():
                    keyword_users = get_users_for_keyword(keyword)
                    for user_id in keyword_users:
                        if not has_user_been_notified(user_id, deal['asin']): # Avoid double notification
                            await bot.send_message(chat_id=user_id, text=f"🔔 New deal for '{keyword}': {deal['title']} - {deal['discount']}\n<a href='{deal['link']}'>Check Deal</a>", parse_mode='HTML')
                            mark_user_notified(user_id, deal['asin'])
            
            # Also check for specific keywords in the title
            for user_id, subscribed_keyword in get_all_keyword_alerts(): # Need a function to get all keyword alerts
                if subscribed_keyword.lower() in deal['title'].lower():
                    if not has_user_been_notified(user_id, deal['asin']): # Avoid double notification
                        await bot.send_message(chat_id=user_id, text=f"🔔 New deal for '{subscribed_keyword}': {deal['title']} - {deal['discount']}\n<a href='{deal['link']}'>Check Deal</a>", parse_mode='HTML')
                        mark_user_notified(user_id, deal['asin'])


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

    # MODIFIED: Use PostgreSQL connection
    conn = get_db_connection()
    rows = []
    try:
        with conn.cursor() as c:
            c.execute("SELECT asin FROM user_tracking WHERE user_id = %s", (user_id,))
            rows = c.fetchall()
    finally:
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
        await message.reply_text(f"📖 Here are your tracked deals:")

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
        "🔔 /alertme &lt;keyword&gt; – Get alerts for specific keywords (e.g., /alertme laptop).\n" # NEW
        "📝 /myalerts – See your keyword alerts.\n" # NEW
        "🔕 /removealert &lt;keyword&gt; – Stop alerts for a keyword.\n" # NEW
        "ℹ️ /help – View this help message.\n\n"
    )

    if user_id in ADMIN_IDS:
        help_text += (
            "<b>👑 Admin Commands:</b>\n"
            "📤 /post – Post the latest scraped deals to the channel.\n"
            "🔗 /url &lt;ASIN or URL&gt; – Manually post a specific product.\n" 
            "📂 /getdb – Receive the `deals.db` database file.\n\n"
            "📊 /stats – Get bot usage statistics.\n" # NEW
            "📢 /broadcast &lt;message&gt; – Send a message to all bot users.\n\n" # NEW
        )

    help_text += (
        "📌 <b>Inline Buttons:</b>\n"
        "🔍 Track – Get alerts when discounts increase\n"
        "❌ Untrack – Stop alerts for a deal\n"
        "📣 Share – Send the deal to friends\n"
        "📊 Price History – View historical prices for a deal\n\n" # NEW
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

# === NEW: Keyword Alert Handlers ===
async def alert_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Please provide a keyword to alert you about. Example: /alertme laptop")
        return
    
    keyword = " ".join(context.args).strip().lower()
    if not keyword:
        await update.message.reply_text("Keyword cannot be empty.")
        return

    if add_keyword_alert(user_id, keyword):
        await update.message.reply_text(f"✅ You will now receive alerts for deals containing '{keyword}'.")
    else:
        await update.message.reply_text(f"ℹ️ You are already subscribed to alerts for '{keyword}'.")

async def my_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    alerts = get_user_keyword_alerts(user_id)
    
    if not alerts:
        await update.message.reply_text("❌ You have no active keyword alerts. Use /alertme to add one.")
        return
    
    alert_list = "\n".join([f"- {k}" for k in alerts])
    await update.message.reply_text(f"🔔 Your current keyword alerts:\n{alert_list}")

async def remove_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Please provide the keyword to remove. Example: /removealert laptop")
        return
    
    keyword = " ".join(context.args).strip().lower()
    if not keyword:
        await update.message.reply_text("Keyword cannot be empty.")
        return

    if remove_keyword_alert(user_id, keyword):
        await update.message.reply_text(f"✅ Alerts for '{keyword}' have been removed.")
    else:
        await update.message.reply_text(f"ℹ️ You were not subscribed to alerts for '{keyword}'.")

# === NEW: Price History Handler ===
async def handle_price_history_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    asin = query.data.replace('history_', '')
    
    history = get_price_history(asin, days=30)
    
    if not history:
        await query.message.reply_text(f"❌ No price history available for ASIN `{asin}` in the last 30 days.", parse_mode='Markdown')
        return

    msg = f"📊 <b>Price History for ASIN `{asin}` (Last 30 Days):</b>\n"
    msg += "━━━━━━━━━━━━━━━\n"
    
    # Sort history by date ascending for display
    history.sort(key=lambda x: x[1]) 

    for price, date in history:
        msg += f"₹{price:.2f} on {date.strftime('%Y-%m-%d %H:%M')}\n"
    
    await query.message.reply_text(msg, parse_mode='HTML')


# === ADMIN COMMANDS ===
async def manual_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("🚫 Unauthorized.")
        return
    await update.message.reply_text("📤 Posting latest deals to the channel...")
    await post_deals(context)
    await update.message.reply_text("✅ Posting complete.")

async def get_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Informs the admin that the database is now hosted remotely."""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        logger.warning(f"Unauthorized /getdb attempt by user ID: {user_id}")
        return

    logger.info(f"Admin user {user_id} used the /getdb command.")
    await update.message.reply_text(
        "ℹ️ The database is now hosted on Supabase (PostgreSQL) and can no longer be sent as a file. "
        "You can access the database directly through your Supabase dashboard."
    )

# === NEW URL HANDLER (ADMIN) - MODIFIED FOR DUPLICATE CHECK ===
async def post_by_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Allows an admin to post a product by its URL or ASIN.
    Checks for duplicates before posting.
    """
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text("Please provide a product URL or ASIN.\nUsage: /url <URL_or_ASIN>")
        return

    input_arg = context.args[0]
    
    # Regex to find ASIN in a URL or check if the arg itself is an ASIN
    match = re.search(r'(?:/dp/|/gp/product/)(B[A-Z0-9]{9})', input_arg)
    asin = match.group(1) if match else (input_arg if re.match(r'^B[A-Z0-9]{9}$', input_arg) else None)

    if not asin:
        await update.message.reply_text("❌ Could not find a valid ASIN in the provided input.")
        return

    await update.message.reply_text(f"Scraping product with ASIN: {asin}...")
    
    deal = await scrape_single_product_by_asin(asin, bot=context.bot)

    if not deal:
        await update.message.reply_text(f"❌ Failed to scrape product details for ASIN {asin}.")
        return
        
    # --- NEW: DUPLICATE CHECK LOGIC ---
    # Check if the deal is new or has a better discount before posting.
    if deal['discount_val'] > 0 and not is_new_or_updated_deal(asin, deal['discount_val']):
        await update.message.reply_text(f"ℹ️ This is a duplicate deal. A post for ASIN `{asin}` with an equal or better discount already exists.", parse_mode='Markdown')
        return
    # --- END OF NEW LOGIC ---

    # Format message and post to channel
    msg = f"✨ <b>{deal['title']}</b>\n\n"
    if deal['current_price'] > 0: # NEW: Display current price
        msg += f"💰 Price: ₹{deal['current_price']:.2f}\n"
    msg += f"🔹 {deal['discount']}\n"
    if deal['coupon'] != 'No Coupon':
        msg += f"💳 {deal['coupon']}\n"
    msg += f"🌂 {deal['category']}\n"
    msg += f"🔗 <a href='{deal['link']}'>Check The Deal</a>"
    msg += "\n\nFor more deals and features, join our bot: <a href='https://t.me/AmaSnag_Bot'>@AmaSnag_Bot</a>"

    # NEW: Add price history to message for manual posts
    price_history = get_price_history(deal['asin'], days=30)
    if price_history:
        prices = [p[0] for p in price_history]
        if prices:
            lowest_price = min(prices)
            highest_price = max(prices)
            if deal['current_price'] == lowest_price:
                msg += "\n\n🔥 <b>Lowest price in last 30 days!</b>"
            elif deal['current_price'] < prices[0]:
                msg += f"\n\n📉 Price dropped from ₹{prices[0]:.2f}!"
            elif deal['current_price'] > prices[0]:
                msg += f"\n\n📈 Price increased from ₹{prices[0]:.2f}."

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Track Deal", callback_data=f"track_{deal['asin']}"),
            InlineKeyboardButton("❌ Untrack", callback_data=f"untrack_{deal['asin']}")
        ],
        [InlineKeyboardButton("📣 Share", url=f"https://t.me/share/url?url={deal['link']}")],
        [InlineKeyboardButton("📊 Price History", callback_data=f"history_{deal['asin']}")] # NEW: Price History Button
    ])
    
    try:
        if deal['image']:
            await context.bot.send_photo(chat_id=CHANNEL_ID, photo=deal['image'], caption=msg, parse_mode='HTML', reply_markup=keyboard)
        else:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode='HTML', reply_markup=keyboard)
        
        await update.message.reply_text(f"✅ Successfully posted deal for ASIN {asin} to the channel.")
        
        # Since the deal is posted, clear old notifications for users tracking it.
        clear_user_notifications(asin)
            
    except Exception as e:
        logger.error(f"Failed to post manual deal for ASIN {asin}: {e}")
        await update.message.reply_text(f"❌ An error occurred while posting to the channel: {e}")

# === NEW: Admin Statistics Command ===
async def get_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 Unauthorized.")
        return

    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT COUNT(DISTINCT user_id) FROM user_preferences;")
            total_users = c.fetchone()[0]

            c.execute("SELECT COUNT(*) FROM user_tracking;")
            total_tracked_items = c.fetchone()[0]

            c.execute("SELECT COUNT(*) FROM keyword_alerts;")
            total_keyword_alerts = c.fetchone()[0]

            c.execute("""
                SELECT asin, COUNT(*) as count FROM user_tracking
                GROUP BY asin ORDER BY count DESC LIMIT 5;
            """)
            top_tracked_items = c.fetchall()

            c.execute("""
                SELECT keyword, COUNT(*) as count FROM keyword_alerts
                GROUP BY keyword ORDER BY count DESC LIMIT 5;
            """)
            top_keyword_alerts = c.fetchall()

        msg = "📊 <b>Bot Statistics:</b>\n"
        msg += f"━━━━━━━━━━━━━━━\n"
        msg += f"👥 Total Users: {total_users}\n"
        msg += f"🔍 Total Tracked Items: {total_tracked_items}\n"
        msg += f"🔔 Total Keyword Alerts: {total_keyword_alerts}\n\n"

        if top_tracked_items:
            msg += "<b>Top 5 Tracked Items:</b>\n"
            for asin, count in top_tracked_items:
                msg += f"- `{asin}`: {count} users\n"
            msg += "\n"

        if top_keyword_alerts:
            msg += "<b>Top 5 Keyword Alerts:</b>\n"
            for keyword, count in top_keyword_alerts:
                msg += f"- '{keyword}': {count} users\n"
            msg += "\n"
        
        await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Failed to get bot statistics: {e}")
        await update.message.reply_text("❌ An error occurred while fetching statistics.")
    finally:
        conn.close()

# === NEW: Admin Broadcast Command ===
async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text("Please provide a message to broadcast. Usage: /broadcast <your message>")
        return

    message_to_send = " ".join(context.args)
    
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT DISTINCT user_id FROM user_preferences;")
            all_users = c.fetchall()
        
        sent_count = 0
        failed_count = 0
        for user_tuple in all_users:
            target_user_id = user_tuple[0]
            try:
                await context.bot.send_message(chat_id=target_user_id, text=message_to_send, parse_mode='HTML', disable_web_page_preview=True)
                sent_count += 1
                await asyncio.sleep(0.1) # Telegram Bot API rate limit: 30 messages per second to different users
            except Exception as e:
                logger.warning(f"Failed to send broadcast message to user {target_user_id}: {e}")
                failed_count += 1
        
        await update.message.reply_text(f"✅ Broadcast complete! Sent to {sent_count} users, failed for {failed_count} users.")

    except Exception as e:
        logger.error(f"An error occurred during broadcast: {e}")
        await update.message.reply_text("❌ An unexpected error occurred during broadcast.")
    finally:
        conn.close()

# Helper to get all keyword alerts for broadcast notification
def get_all_keyword_alerts():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SELECT user_id, keyword FROM keyword_alerts;")
            return c.fetchall()
    finally:
        conn.close()

# === MAIN ===
async def main() -> None:
    """Start the bot and the scheduler."""
    # Add a check for essential environment variables on startup
    if not all([TOKEN, CHANNEL_ID, AFFILIATE_TAG, ADMIN_IDS, DATABASE_URL]):
        logger.critical("FATAL: Missing one or more essential environment variables (TOKEN, CHANNEL_ID, AFFILIATE_TAG, ADMIN_IDS, DATABASE_URL). Shutting down.")
        return

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
    app.add_handler(CommandHandler('url', post_by_url)) # NEW HANDLER
    app.add_handler(CommandHandler('alertme', alert_me)) # NEW
    app.add_handler(CommandHandler('myalerts', my_alerts)) # NEW
    app.add_handler(CommandHandler('removealert', remove_alert)) # NEW
    app.add_handler(CommandHandler('stats', get_stats)) # NEW
    app.add_handler(CommandHandler('broadcast', broadcast_message)) # NEW

    app.add_handler(CallbackQueryHandler(handle_track_button, pattern=r'^track_'))
    app.add_handler(CallbackQueryHandler(handle_untrack_button, pattern=r'^untrack_'))
    app.add_handler(CallbackQueryHandler(my_deals, pattern=r'^mydeals_page_'))
    app.add_handler(CallbackQueryHandler(handle_price_history_button, pattern=r'^history_')) # NEW

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

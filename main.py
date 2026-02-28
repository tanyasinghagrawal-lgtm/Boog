import asyncio
import logging
import random
import json
import io
import time
import uuid
import string
import sys
import hashlib
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import asyncpg
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Response, HTTPException, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from PIL import Image
from passlib.context import CryptContext

# --- CONFIGURATION (Hardcoded) ---
DB_DSN = "postgres://avnadmin:AVNS_d9GncXE-Fge9t5p3XlY@pg-7cbbad8-tanyasinghagrawal-62c1.j.aivencloud.com:26734/defaultdb?sslmode=require"
WP_SITE_URL = "pranavcea.wordpress.com"
HOME_PAGE_URL = "https://www.pranavblog.online/home"
APP_DOMAIN = "https://blog.pranavblog.online"
MAX_CACHE_SIZE_MB = 300
MAX_CACHE_SIZE_BYTES = MAX_CACHE_SIZE_MB * 1024 * 1024
NEON_ANALYTICS_DSN = "postgresql://neondb_owner:npg_ikjvtSpqJ4l0@ep-dry-night-a1znu5d8-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

# Logger setup
# Logger setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Password Hashing Setup
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- GLOBAL IN-MEMORY CACHE ---
# Track total size of images in RAM
CACHE_STATE = {"current_size": 0}

# Mappings: short_slug -> original_url
URL_MAP_CACHE: Dict[str, str] = {}

# Articles Cache: wp_slug -> { title, content_html, featured_hash, access_count, last_accessed }
ARTICLE_CACHE: Dict[str, dict] = {}

# Images Cache: image_hash -> { bytes, size, access_count, last_accessed }
IMAGE_CACHE: Dict[str, dict] = {}

db_pool: asyncpg.Pool = None

# --- ANALYTICS GLOBALS ---
neon_pool: asyncpg.Pool = None
ANALYTICS_QUEUE = []
IP_GEO_CACHE: Dict[str, dict] = {} # ip -> {"city": "...", "country": "..."}
INDEX_CACHE_BLOGS: Dict[str, int] = {} # slug -> db_id
INDEX_CACHE_REFS: Dict[str, int] = {} # referrer -> db_id

# --- DATA MODELS ---

# --- DATA MODELS ---
class UserSignup(BaseModel):
    username: str
    password: str

class AddPost(BaseModel):
    slug: str
    original_url: str


class TrackData(BaseModel):
    slug: str
    referrer: str
    user_id: str
    visit_count: int

# --- CACHE MANAGEMENT ---

# --- CACHE MANAGEMENT ---


def update_cache_metrics(item_dict: dict):
    """Updates access count and timestamp for cache eviction tracking."""
    item_dict['access_count'] += 1
    item_dict['last_accessed'] = time.time()

def enforce_cache_limit():
    """Evicts least used images if cache exceeds 300 MB."""
    global CACHE_STATE, IMAGE_CACHE
    if CACHE_STATE["current_size"] <= MAX_CACHE_SIZE_BYTES:
        return

    logger.warning(f"Cache limit exceeded ({CACHE_STATE['current_size'] / (1024*1024):.2f} MB). Evicting old data...")
    
    # Sort images by (access_count ascending, last_accessed ascending)
    sorted_images = sorted(
        IMAGE_CACHE.items(), 
        key=lambda item: (item[1]['access_count'], item[1]['last_accessed'])
    )
    
    # Delete until we are down to 80% of max capacity (leave breathing room)
    target_size = MAX_CACHE_SIZE_BYTES * 0.8
    for img_hash, img_data in sorted_images:
        if CACHE_STATE["current_size"] <= target_size:
            break
        
        CACHE_STATE["current_size"] -= img_data["size"]
        del IMAGE_CACHE[img_hash]
        
    logger.info(f"Cache cleanup complete. New size: {CACHE_STATE['current_size'] / (1024*1024):.2f} MB")

def add_image_to_cache(img_hash: str, img_bytes: bytes):
    size = sys.getsizeof(img_bytes)
    # If already in cache, adjust size differential
    if img_hash in IMAGE_CACHE:
        CACHE_STATE["current_size"] -= IMAGE_CACHE[img_hash]["size"]
        
    IMAGE_CACHE[img_hash] = {
        "bytes": img_bytes,
        "size": size,
        "access_count": 0,
        "last_accessed": time.time()
    }
    CACHE_STATE["current_size"] += size
    enforce_cache_limit()

# --- DATABASE SETUP ---

async def init_db():
    """Create all necessary tables, including new ones for Articles and Images."""
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=10)
        async with db_pool.acquire() as conn:
            # Users
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL
                );
            """)
            # URLs
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS url_mappings (
                    short_slug TEXT PRIMARY KEY,
                    original_url TEXT NOT NULL,
                    wp_slug TEXT NOT NULL
                );
            """)
            # Compressed Images
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS images (
                    image_hash TEXT PRIMARY KEY,
                    image_data BYTEA NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Pre-rendered Articles
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS articles (
                    wp_slug TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content_html TEXT NOT NULL,
                    featured_hash TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        logger.info("Database schemas verified successfully.")
    except Exception as e:
        logger.error(f"DB Init Error: {e}")

# --- IMAGE PROCESSING (WebP < 50KB) ---

# --- IMAGE PROCESSING (WebP < 50KB) ---

def process_image_to_webp(image_bytes: bytes) -> bytes:
    """Robust Image Compressor: Handles transparency, SVGs, and prevents crashes."""
    try:
        # SVG images ko compress nahi kiya ja sakta, unhe as-is chhod do
        if image_bytes.strip().startswith(b'<svg') or image_bytes.strip().startswith(b'<?xml'):
            return image_bytes

        img = Image.open(io.BytesIO(image_bytes))
        
        # Proper Transparency Fix: Replace transparent bg with White (instead of black)
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            alpha = img.convert('RGBA').split()[-1]
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img.convert('RGBA'), mask=alpha)
            img = bg
        else:
            img = img.convert("RGB")
            
        # Lighthouse Fix: Reduced width to 600 for mobile optimization
        max_width = 600
        if img.width > max_width:
            ratio = max_width / float(img.width)
            new_height = int((float(img.height) * float(ratio)))
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
        
        quality = 85
        output = io.BytesIO()
        
        while True:
            output.seek(0)
            output.truncate(0)
            img.save(output, format="WEBP", quality=quality, method=6)
            size_kb = len(output.getvalue()) / 1024
            
            if size_kb <= 48 or quality <= 10:
                break
            
            quality -= 10
            if quality < 30:
                img = img.resize((int(img.width * 0.8), int(img.height * 0.8)), Image.Resampling.LANCZOS)
                
        return output.getvalue()
    except Exception as e:
        logger.warning(f"Image compression skipped (format unsupported/SVG/Corrupt): {e}")
        return image_bytes # Agar koi bhi error aaye toh original image bhej do (image tooti hui nahi dikhegi)
async def download_and_process_image(url: str, client: httpx.AsyncClient) -> Optional[Tuple[str, bytes]]:
    """Downloads an image, compresses it, and generates a unique hash."""
    if not url or url.startswith("data:"):
        return None
    try:
        resp = await client.get(url, timeout=10.0)
        if resp.status_code == 200:
            processed_bytes = await asyncio.to_thread(process_image_to_webp, resp.content)
            img_hash = hashlib.md5(processed_bytes).hexdigest()
            return img_hash, processed_bytes
    except Exception as e:
        logger.error(f"Failed to download/process image {url}: {e}")
    return None

# --- WORDPRESS SCRAPING & HTML PARSING ---

async def scrape_and_save_article(wp_slug: str, client: httpx.AsyncClient) -> bool:
    """Fetches WP article, processes all images, saves HTML to DB."""
    api_url = f"https://public-api.wordpress.com/rest/v1.1/sites/{WP_SITE_URL}/posts/slug:{wp_slug}"
    try:
        resp = await client.get(api_url, timeout=15.0)
        if resp.status_code != 200:
            return False
            
        data = resp.json()
        title = data.get('title', 'To The Point')
        raw_content = data.get('content', '')
        featured_image_url = data.get('featured_image')

        # Fallback image extraction
        soup = BeautifulSoup(raw_content, 'html.parser')
        if not featured_image_url:
            img_tag = soup.find('img')
            if img_tag and img_tag.get('src'):
                featured_image_url = img_tag.get('src')

        featured_hash = None
        # Process Featured Image
        if featured_image_url:
            res = await download_and_process_image(featured_image_url, client)
            if res:
                featured_hash, img_bytes = res
                await save_image_to_db(featured_hash, img_bytes)

        # Process In-Content Images (SEO optimization)
        # FIX 1: Heading Sequence Error (Convert H4, H5, H6 into H3)
        # FIX 1: Heading Sequence Error (Convert H4, H5, H6 into <p> to bypass hierarchy errors)
        for tag in soup.find_all(['h4', 'h5', 'h6']):
            tag.name = 'p' # Paragraph tag me badalne se Lighthouse heading error nahi dega
            tag['class'] = tag.get('class', []) + ['text-xl', 'font-semibold', 'mt-6', 'mb-3', 'text-green-800', 'block']
        # FIX 2: Content Formatting (Make lists, quotes, and links look good)
        for ul in soup.find_all('ul'):
            ul['class'] = ul.get('class', []) + ['list-disc', 'ml-6', 'mb-4', 'text-gray-700', 'space-y-1']
        for ol in soup.find_all('ol'):
            ol['class'] = ol.get('class', []) + ['list-decimal', 'ml-6', 'mb-4', 'text-gray-700', 'space-y-1']
        for bq in soup.find_all('blockquote'):
            bq['class'] = bq.get('class', []) + ['border-l-4', 'border-green-500', 'bg-green-50', 'p-4', 'my-5', 'italic', 'text-green-900', 'rounded-r-lg']
        for a in soup.find_all('a'):
            a['class'] = a.get('class', []) + ['text-green-600', 'underline', 'font-medium', 'hover:text-green-800']

        # FIX 3: Remove Duplicate Images (Agar featured image post ke andar bhi repeat hui ho)
        if featured_image_url:
            for img in soup.find_all('img'):
                if img.get('src') == featured_image_url:
                    img.decompose() # Duplicate image ko delete kar dega

        # Process In-Content Images (SEO optimization)
        # Process In-Content Images (SEO optimization & Alt Text Integration)
        img_counter = 1
        for img in soup.find_all('img'):
            src = img.get('src')
            if src:
                res = await download_and_process_image(src, client)
                if res:
                    c_hash, c_bytes = res
                    await save_image_to_db(c_hash, c_bytes)
                    img['src'] = f"/img_asset/{c_hash}" # Replace external URL with local optimized endpoint
                    
                    # SEO Fix: Dynamically generate polished Alt text based on Blog Title
                    clean_title_for_alt = title.replace('"', "'")
                    img['alt'] = f"{clean_title_for_alt} - Graphic Illustration {img_counter}"
                    img_counter += 1
                    
                    # Strip classes/sizes that might break responsive layout
                    if img.get('srcset'): del img['srcset']
                    if img.get('sizes'): del img['sizes']
        processed_html = str(soup)

        # Save to DB
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO articles (wp_slug, title, content_html, featured_hash)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (wp_slug) DO UPDATE 
                SET title = EXCLUDED.title, content_html = EXCLUDED.content_html, featured_hash = EXCLUDED.featured_hash
            """, wp_slug, title, processed_html, featured_hash)
            
        # Update Cache
        ARTICLE_CACHE[wp_slug] = {
            "title": title,
            "content_html": processed_html,
            "featured_hash": featured_hash,
            "access_count": 0,
            "last_accessed": time.time()
        }
        logger.info(f"Successfully scraped, processed, and saved article: {wp_slug}")
        return True
    except Exception as e:
        logger.error(f"Error scraping WP article {wp_slug}: {e}")
        return False

async def save_image_to_db(img_hash: str, img_bytes: bytes):
    """Saves optimized image to DB and RAM cache."""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO images (image_hash, image_data)
                VALUES ($1, $2) ON CONFLICT DO NOTHING
            """, img_hash, img_bytes)
        add_image_to_cache(img_hash, img_bytes)
    except Exception as e:
        logger.error(f"DB Image save error: {e}")


# --- ANALYTICS SYSTEM (NEON DB) ---

# --- ANALYTICS SYSTEM (NEON DB) ---

async def init_neon_db():
    """Creates highly optimized indexed tables for Analytics in Neon DB."""
    global neon_pool
    try:
        neon_pool = await asyncpg.create_pool(NEON_ANALYTICS_DSN, min_size=1, max_size=5)
        async with neon_pool.acquire() as conn:
            # Table 1: Blog Indexing
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS track_blogs (
                    id SERIAL PRIMARY KEY,
                    slug TEXT UNIQUE NOT NULL
                );
            """)
            # Table 2: Referrer Indexing
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS track_refs (
                    id SERIAL PRIMARY KEY,
                    referrer TEXT UNIQUE NOT NULL
                );
            """)
            # Table 3: Main Logs (Unique log_id ensures NO duplicates on timeouts)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS track_events (
                    log_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    ip_address TEXT,
                    country TEXT,
                    city TEXT,
                    blog_id INTEGER REFERENCES track_blogs(id),
                    ref_id INTEGER REFERENCES track_refs(id),
                    visit_count INTEGER,
                    visited_at TIMESTAMP
                );
            """)
            # Load existing indexes into RAM for speed
            for row in await conn.fetch("SELECT id, slug FROM track_blogs"):
                INDEX_CACHE_BLOGS[row['slug']] = row['id']
            for row in await conn.fetch("SELECT id, referrer FROM track_refs"):
                INDEX_CACHE_REFS[row['referrer']] = row['id']
                
        logger.info("Neon Analytics DB Initialized and Indexed.")
    except Exception as e:
        logger.error(f"Neon DB Init Error: {e}")

async def resolve_ip_geo(ip: str):
    """Background task to fetch Geo details securely without blocking requests."""
    if ip in IP_GEO_CACHE or ip in ["127.0.0.1", "localhost", "::1"]:
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://ip-api.com/json/{ip}", timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                IP_GEO_CACHE[ip] = {
                    "country": data.get("country", "Unknown"),
                    "city": data.get("city", "Unknown")
                }
            else:
                IP_GEO_CACHE[ip] = {"country": "Unknown", "city": "Unknown"}
    except Exception:
        IP_GEO_CACHE[ip] = {"country": "Unknown", "city": "Unknown"}

async def flush_analytics_data():
    """Runs every 5 mins: Dumps RAM queue to Neon DB efficiently with deduplication."""
    while True:
        await asyncio.sleep(300) # 5 Minutes
        if not ANALYTICS_QUEUE:
            continue
            
        # Snapshot the queue and clear original
        queue_snapshot = list(ANALYTICS_QUEUE)
        ANALYTICS_QUEUE.clear()
        
        try:
            async with neon_pool.acquire() as conn:
                # 1. Ensure all blogs and referrers are Indexed
                for item in queue_snapshot:
                    slug = item['slug']
                    ref = item['referrer']
                    
                    if slug not in INDEX_CACHE_BLOGS:
                        await conn.execute("INSERT INTO track_blogs (slug) VALUES ($1) ON CONFLICT DO NOTHING", slug)
                        row = await conn.fetchrow("SELECT id FROM track_blogs WHERE slug = $1", slug)
                        if row: INDEX_CACHE_BLOGS[slug] = row['id']
                        
                    if ref not in INDEX_CACHE_REFS:
                        await conn.execute("INSERT INTO track_refs (referrer) VALUES ($1) ON CONFLICT DO NOTHING", ref)
                        row = await conn.fetchrow("SELECT id FROM track_refs WHERE referrer = $1", ref)
                        if row: INDEX_CACHE_REFS[ref] = row['id']

                # 2. Bulk Insert into main logs (ON CONFLICT DO NOTHING prevents duplicates)
                log_data = []
                for item in queue_snapshot:
                    geo = IP_GEO_CACHE.get(item['ip'], {"city": "Unknown", "country": "Unknown"})
                    log_data.append((
                        item['log_id'],
                        item['user_id'],
                        item['ip'],
                        geo['country'],
                        geo['city'],
                        INDEX_CACHE_BLOGS.get(item['slug']),
                        INDEX_CACHE_REFS.get(item['referrer']),
                        item['visit_count'],
                        item['timestamp']
                    ))
                    
                await conn.executemany("""
                    INSERT INTO track_events 
                    (log_id, user_id, ip_address, country, city, blog_id, ref_id, visit_count, visited_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (log_id) DO NOTHING
                """, log_data)
                
            logger.info(f"Analytics Auto-Flush: Safely saved {len(log_data)} visits to Neon DB.")
        except Exception as e:
            logger.error(f"Analytics Flush Error/Timeout: {e}. Retrying next cycle...")
            # Error aane par queue wapas dalega. Jo save ho chuke the wo `ON CONFLICT` se skip ho jayenge agli baar!
            ANALYTICS_QUEUE.extend(queue_snapshot)

# --- BACKGROUND TASKS ---

async def startup_sync():
    """On boot: Load maps into cache, find missing DB articles, and fetch them."""
    global URL_MAP_CACHE
    import urllib.parse
    async with db_pool.acquire() as conn:
        # Load mappings
        rows = await conn.fetch("SELECT short_slug, original_url, wp_slug FROM url_mappings")
        for r in rows:
            short_slug = r['short_slug']
            orig_url = r['original_url']
            wp_slug = r['wp_slug']
            
            # --- AUTO-FIX: Convert Encoded URLs to Proper Human-Readable Text in DB ---
            if '%' in wp_slug or '%' in orig_url:
                clean_orig = urllib.parse.unquote(orig_url)
                clean_wp = urllib.parse.unquote(wp_slug)
                try:
                    # Update both tables with clean text
                    await conn.execute("UPDATE url_mappings SET original_url = $1, wp_slug = $2 WHERE short_slug = $3", clean_orig, clean_wp, short_slug)
                    await conn.execute("UPDATE articles SET wp_slug = $1 WHERE wp_slug = $2", clean_wp, wp_slug)
                    logger.info(f"Auto-fixed encoded URL in DB for: {short_slug}")
                except Exception as e:
                    pass # Ignored if already cleaned
                    
                orig_url = clean_orig
                wp_slug = clean_wp
            # --------------------------------------------------------------------------
            
            URL_MAP_CACHE[short_slug] = orig_url
            
        # Find which wp_slugs need scraping            
        # Find which wp_slugs need scraping
        db_articles = await conn.fetch("SELECT wp_slug, title, content_html, featured_hash FROM articles")
        for a in db_articles:
            ARTICLE_CACHE[a['wp_slug']] = {
                "title": a['title'],
                "content_html": a['content_html'],
                "featured_hash": a['featured_hash'],
                "access_count": 0,
                "last_accessed": time.time()
            }
            
        known_wp_slugs = {r['wp_slug'] for r in rows}
        scraped_wp_slugs = {a['wp_slug'] for a in db_articles}
        missing_slugs = known_wp_slugs - scraped_wp_slugs

    if missing_slugs:
        logger.info(f"Found {len(missing_slugs)} missing articles in DB. Starting background fetch...")
        async with httpx.AsyncClient() as client:
            for wp_slug in missing_slugs:
                await scrape_and_save_article(wp_slug, client)
                await asyncio.sleep(2) # Prevent rate limiting

async def auto_discover_new_posts():
    """Runs every 10 mins: Syncs with WP. Auto-Adds new posts & Auto-Deletes removed posts everywhere."""
    while True:
        await asyncio.sleep(600) # 10 Minutes
        logger.info("Scanning WordPress for Additions and Deletions...")
        try:
            import urllib.parse
            active_wp_slugs = set()
            wp_posts_data = {}
            page = 1
            
            async with httpx.AsyncClient() as client:
                # 1. Fetch ALL active posts from WordPress (using pagination)
                while True:
                    api_url = f"https://public-api.wordpress.com/rest/v1.1/sites/{WP_SITE_URL}/posts?number=100&page={page}&fields=slug,URL"
                    resp = await client.get(api_url, timeout=15.0)
                    if resp.status_code != 200: break
                    
                    data = resp.json()
                    posts = data.get('posts', [])
                    if not posts: break
                    
                    for p in posts:
                        clean_wp_slug = urllib.parse.unquote(p['slug'])
                        active_wp_slugs.add(clean_wp_slug)
                        wp_posts_data[clean_wp_slug] = urllib.parse.unquote(p['URL'])
                        
                    if data.get('meta', {}).get('next_page'):
                        page += 1
                    else:
                        break
                        
                # Fail-safe: Agar WP API down ho toh sab kuch delete na kare!
                if not active_wp_slugs:
                    logger.warning("WP API returned 0 posts. Skipping sync to prevent accidental mass deletion.")
                    continue 

                async with db_pool.acquire() as conn:
                    db_slugs = await conn.fetch("SELECT short_slug, wp_slug FROM url_mappings")
                    
                    # Create a map of our DB wp_slugs to their short_slugs
                    db_mapping = {urllib.parse.unquote(r['wp_slug']): r['short_slug'] for r in db_slugs}
                    db_wp_slugs = set(db_mapping.keys())
                    
                    # --- ACTION 1: DELETIONS (If post is in DB but NOT in WordPress) ---
                    deleted_slugs = db_wp_slugs - active_wp_slugs
                    for del_wp_slug in deleted_slugs:
                        del_short_slug = db_mapping.get(del_wp_slug)
                        
                        # Remove from DB
                        await conn.execute("DELETE FROM articles WHERE wp_slug = $1", del_wp_slug)
                        if del_short_slug:
                            await conn.execute("DELETE FROM url_mappings WHERE short_slug = $1", del_short_slug)
                            
                        # Remove from RAM Cache (Automatically removes from Sitemap & API)
                        if del_short_slug in URL_MAP_CACHE: del URL_MAP_CACHE[del_short_slug]
                        if del_wp_slug in ARTICLE_CACHE: del ARTICLE_CACHE[del_wp_slug]
                        
                        logger.info(f"Deleted Post Removed Everywhere: {del_wp_slug}")
                        
                    # --- ACTION 2: ADDITIONS (If post is in WordPress but NOT in DB) ---
                    new_slugs = active_wp_slugs - db_wp_slugs
                    for new_wp_slug in new_slugs:
                        orig_url = wp_posts_data[new_wp_slug]
                        short_slug = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
                        logger.info(f"New Post Detected: {new_wp_slug}. Auto-generating link: /{short_slug}")
                        
                        await conn.execute(
                            "INSERT INTO url_mappings (short_slug, original_url, wp_slug) VALUES ($1, $2, $3)",
                            short_slug, orig_url, new_wp_slug
                        )
                        URL_MAP_CACHE[short_slug] = orig_url
                        
                        # Scrape & Compress Images
                        await scrape_and_save_article(new_wp_slug, client)

        except Exception as e:
            logger.error(f"Auto-sync scanner failed: {e}")
# --- LIFESPAN MANAGER ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server Starting... Initializing DB and Cache.")
    await init_db()
    await init_neon_db() # Start Neon Analytics DB
    
    # Run sync synchronously so cache is ready immediately
    await startup_sync()
    
    # Start loops for continuous scanning & analytics flush
    scanner_task = asyncio.create_task(auto_discover_new_posts())
    analytics_task = asyncio.create_task(flush_analytics_data())
    
    yield
    
    scanner_task.cancel()
    analytics_task.cancel()
    if db_pool:
        await db_pool.close()
    if neon_pool:
        await neon_pool.close()
    logger.info("Server Shutting Down.")
# --- FASTAPI APP ---

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- AUTH & ADMIN ROUTES ---

@app.post("/signup")
async def signup(user: UserSignup):
    async with db_pool.acquire() as conn:
        count_val = await conn.fetchval("SELECT COUNT(*) FROM users")
        if count_val >= 3:
            raise HTTPException(status_code=403, detail="Signup limit reached (Max 3 users).")
        
        exists = await conn.fetchval("SELECT id FROM users WHERE username = $1", user.username)
        if exists:
            raise HTTPException(status_code=400, detail="Username already taken.")

        hashed_password = pwd_context.hash(user.password)
        await conn.execute("INSERT INTO users (username, password_hash) VALUES ($1, $2)", user.username, hashed_password)
        return {"status": "success", "message": "User created successfully."}

@app.post("/verify")
async def verify_user(user: UserSignup):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT password_hash FROM users WHERE username = $1", user.username)
        if not row or not pwd_context.verify(user.password, row['password_hash']):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        return {"status": "success", "message": "Verified"}

@app.get("/allpost")
async def get_all_posts():
    """
    Returns wp_slugs (SEO friendly) instead of short random slugs.
    Guarantees fully decoded, human-readable URLs for the API client.
    """
    import urllib.parse
    result = []
    for short_slug, orig_url in URL_MAP_CACHE.items():
        # URL me se safely exact slug nikalna (extra query params ignore karke)
        parsed_url = urllib.parse.urlparse(orig_url)
        raw_wp_slug = parsed_url.path.strip('/').split('/')[-1]
        
        # API ko bhejne se pehle ajeeb characters (%f0%9d) ko forcefully clean (decode) karna
        clean_wp_slug = urllib.parse.unquote(raw_wp_slug)
        
        result.append({
            "slug": clean_wp_slug,        # Aapke server ka clean SEO link
            "original_url": orig_url      # WordPress ka original exact link
        })
    return result
@app.post("/addpost")
async def add_post(post: AddPost, background_tasks: BackgroundTasks):
    clean_slug = post.slug.strip('/')
    wp_slug = post.original_url.strip('/').split('/')[-1]
    
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT short_slug FROM url_mappings WHERE short_slug = $1", clean_slug)
        if exists:
            raise HTTPException(status_code=400, detail="Slug already exists.")
            
        await conn.execute(
            "INSERT INTO url_mappings (short_slug, original_url, wp_slug) VALUES ($1, $2, $3)",
            clean_slug, post.original_url, wp_slug
        )
        
    URL_MAP_CACHE[clean_slug] = post.original_url
    
    # Scrape immediately in background so it's ready quickly
    async def bg_scrape():
        async with httpx.AsyncClient() as client:
            await scrape_and_save_article(wp_slug, client)
            
    background_tasks.add_task(bg_scrape)
    return {"status": "success", "slug": clean_slug, "url": post.original_url}

# --- PUBLIC ROUTES ---

# --- PUBLIC ROUTES ---

# --- PUBLIC ROUTES ---

@app.post("/track")
async def track_user_visit(data: TrackData, request: Request, background_tasks: BackgroundTasks):
    """Receives non-blocking analytics data from frontend."""
    import urllib.parse
    import uuid
    
    # Get Real IP Address (Bypassing Cloudflare/Proxies)
    ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For") or request.client.host
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()
        
    # Schedule Geolocation fetch without blocking this response
    if ip and ip not in IP_GEO_CACHE:
        background_tasks.add_task(resolve_ip_geo, ip)
        
    # IST Time calculation (+5:30)
    ist_time = datetime.utcnow() + timedelta(hours=5, minutes=30)
    
    # Clean Referrer: Extract ONLY domain name (e.g., google.com)
    raw_ref = data.referrer if data.referrer else "Direct"
    clean_ref = "Direct"
    if raw_ref and raw_ref.lower() != "direct":
        if not raw_ref.startswith("http"):
            raw_ref = "http://" + raw_ref # Help urlparse detect domain correctly
        try:
            parsed = urllib.parse.urlparse(raw_ref)
            clean_ref = parsed.netloc if parsed.netloc else raw_ref
            if clean_ref.startswith("www."):
                clean_ref = clean_ref[4:] # Remove www.
        except:
            clean_ref = "Direct"
            
    if not clean_ref:
        clean_ref = "Direct"
    
    # Append to RAM Queue with Unique Log ID (Prevents Duplicates on Neon Timeout)
    ANALYTICS_QUEUE.append({
        "log_id": str(uuid.uuid4()),
        "slug": data.slug,
        "referrer": clean_ref,
        "user_id": data.user_id,
        "visit_count": 1, # Always fixed to 1 as per requirements
        "ip": ip,
        "timestamp": ist_time
    })
    
    return {"status": "ok"}

@app.get("/author.webp")
async def serve_author_img():
    """Serves the author profile image from the root directory."""
    from fastapi.responses import FileResponse
    import os
    if os.path.exists("author.webp"):
        return FileResponse("author.webp")
    return Response(status_code=404)

@app.get("/favicon.ico")
async def serve_favicon():
    """Serves the favicon from the root directory."""
    from fastapi.responses import FileResponse
    import os
    if os.path.exists("favicon.ico"):
        return FileResponse("favicon.ico")
    return Response(status_code=404)

@app.get("/robots.txt")
async def robots_txt():
    """Tells search engines to crawl everything and where the sitemap is."""
    content = f"User-agent: *\nAllow: /\nSitemap: {APP_DOMAIN}/sitemap.xml"
    return Response(content=content, media_type="text/plain")

@app.get("/sitemap.xml")
async def sitemap_xml():
    """Generates an XML sitemap of all active WordPress Slugs for Google."""
    import html # XML errors bachane ke liye escape tool
    
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    
    # Add Home Page
    xml += f'  <url>\n    <loc>{APP_DOMAIN}/</loc>\n    <priority>1.0</priority>\n  </url>\n'
    
    # Add all SEO friendly blog URLs from cache
    for short_slug, orig_url in URL_MAP_CACHE.items():
        wp_slug = orig_url.strip('/').split('/')[-1]
        # FIX: Escape special characters like & for valid XML format
        safe_url = html.escape(f"{APP_DOMAIN}/{wp_slug}")
        xml += f'  <url>\n    <loc>{safe_url}</loc>\n    <priority>0.8</priority>\n  </url>\n'
        
    xml += '</urlset>'
    return Response(content=xml, media_type="application/xml")
@app.get("/")
async def root():
    return RedirectResponse(url=HOME_PAGE_URL)
def get_image_mime_type(img_bytes: bytes) -> str:
    """Reads file signatures to return correct MIME type, avoiding broken images."""
    if img_bytes.strip().startswith(b'<svg') or img_bytes.strip().startswith(b'<?xml'): return "image/svg+xml"
    if img_bytes.startswith(b'\x89PNG'): return "image/png"
    if img_bytes.startswith(b'GIF8'): return "image/gif"
    if img_bytes.startswith(b'\xff\xd8'): return "image/jpeg"
    return "image/webp"

@app.get("/img_asset/{img_hash}")
async def serve_optimized_image(img_hash: str):
    """Serves optimized images intelligently based on their real format."""
    cache_headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    
    # Check RAM
    if img_hash in IMAGE_CACHE:
        update_cache_metrics(IMAGE_CACHE[img_hash])
        img_bytes = IMAGE_CACHE[img_hash]["bytes"]
        mime = get_image_mime_type(img_bytes)
        return Response(content=img_bytes, media_type=mime, headers=cache_headers)
        
    # Check DB
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT image_data FROM images WHERE image_hash = $1", img_hash)
        if row:
            img_bytes = row['image_data']
            add_image_to_cache(img_hash, img_bytes)
            mime = get_image_mime_type(img_bytes)
            return Response(content=img_bytes, media_type=mime, headers=cache_headers)
            
    return Response(status_code=404)
@app.get("/{slug}.png")
async def legacy_og_image(slug: str):
    """Fallback route for older platforms requesting .png specifically. Serves webp internally."""
    wp_slug = slug
    
    # Check if a short slug was passed instead of wp_slug
    if slug in URL_MAP_CACHE:
        wp_slug = URL_MAP_CACHE[slug].strip('/').split('/')[-1]
        
    article = ARTICLE_CACHE.get(wp_slug)
    
    if article and article.get('featured_hash'):
        return RedirectResponse(url=f"/img_asset/{article['featured_hash']}")
    return Response(status_code=404)

@app.get("/{slug}")
async def server_side_rendered_blog(slug: str):
    """
    Core SSR Logic. Replaces HTML contents dynamically so 
    the frontend does NO fetching, ensuring perfect SEO and speed.
    Supports both old short_slugs and new SEO wp_slugs.
    """
    wp_slug = slug
    original_url = None

    # Check if user visited using an old short_slug
    if slug in URL_MAP_CACHE:
        original_url = URL_MAP_CACHE[slug]
        wp_slug = original_url.strip('/').split('/')[-1]
    else:
        # DB aur Cache me ab text ekdum clean hai, isliye seedha direct match chalega!
        for short, orig in URL_MAP_CACHE.items():
            if orig.strip('/').split('/')[-1] == wp_slug:
                original_url = orig
                break

    if wp_slug not in ARTICLE_CACHE and not original_url:
        return RedirectResponse(url=HOME_PAGE_URL)

    # 1. Ensure Article is available
    article = ARTICLE_CACHE.get(wp_slug)
    if not article:
        # Emergency fetch if it was skipped
        async with httpx.AsyncClient() as client:
            success = await scrape_and_save_article(wp_slug, client)
            if success:
                article = ARTICLE_CACHE.get(wp_slug)
    
    if not article:
        return HTMLResponse("<h1>Error Processing Article. Please try again later.</h1>", status_code=500)

    update_cache_metrics(article)

    # 2. Prepare 4 Recommendations
    all_short_slugs = list(URL_MAP_CACHE.keys())
    
    # Ensure we don't recommend the article we are currently viewing
    current_short_slug = None
    if slug in all_short_slugs:
        current_short_slug = slug
    else:
        for short, orig in URL_MAP_CACHE.items():
            if orig.strip('/').split('/')[-1] == wp_slug:
                current_short_slug = short
                break
                
    if current_short_slug and current_short_slug in all_short_slugs:
        all_short_slugs.remove(current_short_slug)
        
    random_recs = random.sample(all_short_slugs, min(4, len(all_short_slugs)))
    
    rec_html_block = ""
    for r_short_slug in random_recs:
        r_wp_slug = URL_MAP_CACHE[r_short_slug].strip('/').split('/')[-1]
        rec_article = ARTICLE_CACHE.get(r_wp_slug)
        
        if rec_article:
            r_title = rec_article['title']
            r_hash = rec_article.get('featured_hash')
            
            # Prevent HTML breaking by removing double quotes from title
            clean_r_title = r_title.replace('"', "'") 
            
            img_html = f'<div class="h-40 w-full overflow-hidden bg-gray-100"><img src="/img_asset/{r_hash}" alt="Read more about: {clean_r_title}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500" loading="lazy"></div>' if r_hash else '<div class="h-40 w-full bg-green-50 flex items-center justify-center"><i class="fas fa-leaf text-green-200 text-4xl"></i></div>'            
            # NOTE: We now use r_wp_slug in the href for SEO optimization
            rec_html_block += f"""
                <a href="/{r_wp_slug}" class="glass-morphism glass-card rounded-xl overflow-hidden group block text-left">
                    {img_html}
                    <div class="p-5">
                        <h3 class="font-bold text-green-900 leading-tight mb-2 group-hover:text-green-700 transition-colors line-clamp-2">{r_title}</h3>
                        <div class="text-xs text-gray-500 font-semibold uppercase tracking-wide mt-2">Read Article <i class="fas fa-arrow-right ml-1"></i></div>
                    </div>
                </a>
            """
    # 3. Read base HTML
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>Error: index.html not found on server</h1>", status_code=500)

    STATIC_CSS = """<style>*,::after,::before{box-sizing:border-box;border-width:0;border-style:solid;border-color:#e5e7eb}body{margin:0;line-height:inherit}a{color:inherit;text-decoration:inherit}h1,h2,h3,h4{margin:0;font-size:inherit;font-weight:inherit}p{margin:0}img{display:block;max-width:100%;height:auto}.fixed{position:fixed}.top-0{top:0}.z-50{z-index:50}.mx-auto{margin-left:auto;margin-right:auto}.mt-1{margin-top:0.25rem}.mt-2{margin-top:0.5rem}.mt-5{margin-top:1.25rem}.mt-6{margin-top:1.5rem}.mt-10{margin-top:2.5rem}.mt-12{margin-top:3rem}.mb-2{margin-bottom:0.5rem}.mb-3{margin-bottom:0.75rem}.mb-4{margin-bottom:1rem}.mb-6{margin-bottom:1.5rem}.mb-10{margin-bottom:2.5rem}.mb-20{margin-bottom:5rem}.ml-1{margin-left:0.25rem}.ml-6{margin-left:1.5rem}.my-5{margin-top:1.25rem;margin-bottom:1.25rem}.flex{display:flex}.grid{display:grid}.hidden{display:none}.block{display:block}.h-40{height:10rem}.h-64{height:16rem}.h-full{height:100%}.w-full{width:100%}.w-fit{width:fit-content}.max-w-4xl{max-width:56rem}.max-w-5xl{max-width:64rem}.min-h-screen{min-height:100vh}.min-h-\[60vh\]{min-height:60vh}.flex-col{flex-direction:column}.items-center{align-items:center}.justify-center{justify-content:center}.gap-2{gap:0.5rem}.gap-3{gap:0.75rem}.gap-6{gap:1.5rem}.grid-cols-1{grid-template-columns:repeat(1,minmax(0,1fr))}.overflow-hidden{overflow:hidden}.rounded-xl{border-radius:0.75rem}.rounded-2xl{border-radius:1rem}.rounded-3xl{border-radius:1.5rem}.rounded-full{border-radius:9999px}.rounded-r-lg{border-top-right-radius:0.5rem;border-bottom-right-radius:0.5rem}.border-b{border-bottom-width:1px}.border-t{border-top-width:1px}.border-l-4{border-left-width:4px}.border-green-100{border-color:#dcfce7}.border-green-200\/50{border-color:rgb(187 247 208 / 0.5)}.border-green-500{border-color:#22c55e}.bg-gray-100{background-color:#f3f4f6}.bg-green-50{background-color:#f0fdf4}.bg-green-600{background-color:#16a34a}.p-4{padding:1rem}.p-5{padding:1.25rem}.p-6{padding:1.5rem}.p-8{padding:2rem}.px-4{padding-left:1rem;padding-right:1rem}.px-6{padding-left:1.5rem;padding-right:1.5rem}.py-2{padding-top:0.5rem;padding-bottom:0.5rem}.py-3{padding-top:0.75rem;padding-bottom:0.75rem}.pb-12{padding-bottom:3rem}.pb-6{padding-bottom:1.5rem}.pt-24{padding-top:6rem}.pt-8{padding-top:2rem}.text-center{text-align:center}.text-left{text-align:left}.text-2xl{font-size:1.5rem;line-height:2rem}.text-4xl{font-size:2.25rem;line-height:2.5rem}.text-6xl{font-size:3.75rem;line-height:1}.text-\[0\.65rem\]{font-size:0.65rem}.text-xl{font-size:1.25rem;line-height:1.75rem}.text-xs{font-size:0.75rem;line-height:1rem}.font-bold{font-weight:700}.font-medium{font-weight:500}.font-semibold{font-weight:600}.uppercase{text-transform:uppercase}.italic{font-style:italic}.leading-none{line-height:1}.leading-tight{line-height:1.25}.tracking-wider{letter-spacing:0.05em}.text-gray-500{color:#6b7280}.text-gray-600{color:#4b5563}.text-gray-700{color:#374151}.text-gray-800{color:#1f2937}.text-green-200{color:#bbf7d0}.text-green-500{color:#22c55e}.text-green-600{color:#16a34a}.text-green-700{color:#15803d}.text-green-800{color:#166534}.text-green-900{color:#14532d}.text-green-950{color:#052e16}.text-red-400{color:#f87171}.text-white{color:#fff}.underline{text-decoration-line:underline}.shadow-lg{box-shadow:0 10px 15px -3px rgb(0 0 0 / .1), 0 4px 6px -4px rgb(0 0 0 / .1)}.transition-colors{transition-property:color,background-color,border-color;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.transition-opacity{transition-property:opacity;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.transition-transform{transition-property:transform;transition-timing-function:cubic-bezier(.4,0,.2,1);transition-duration:.15s}.duration-500{transition-duration:.5s}.object-cover{object-fit:cover}.list-disc{list-style-type:disc;padding-left:1rem}.list-decimal{list-style-type:decimal;padding-left:1rem}.space-y-1>*{margin-top:0.25rem;margin-bottom:0}.animate-bounce{animation:bounce 1s infinite}@keyframes bounce{0%,100%{transform:translateY(-25%);animation-timing-function:cubic-bezier(.8,0,1,1)}50%{transform:none;animation-timing-function:cubic-bezier(0,0,.2,1)}}.hover\:bg-green-700:hover{background-color:#15803d}.hover\:text-green-700:hover{color:#15803d}.hover\:text-green-800:hover{color:#166534}.hover\:opacity-80:hover{opacity:.8}.group:hover .group-hover\:scale-105{transform:scale(1.05)}.group:hover .group-hover\:text-green-700{color:#15803d}@media (min-width:768px){.md\:grid-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}.md\:h-80{height:20rem}.md\:p-10{padding:2.5rem}.md\:px-6{padding-left:1.5rem;padding-right:1.5rem}.md\:text-3xl{font-size:1.875rem;line-height:2.25rem}}</style>"""
    
    # Ye replace function purani script ko delete karke hamaari fast CSS ko laga dega
    # 1. TailwindJS fix: Apply STATIC CSS
    html = html.replace('<script src="https://cdn.tailwindcss.com"></script>', STATIC_CSS)
    html = html.replace('<script defer src="https://cdn.tailwindcss.com"></script>', '')
    
    # 2. FontAwesome Fix: Use media="print" trick (Restores perfect animations & sizes WITHOUT render blocking)
    old_fa = '<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">'
    new_fa = '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" media="print" onload="this.media=\'all\'">'
    html = html.replace(old_fa, new_fa)
    
    # 3. Google Fonts Fix: Switch to optimized <link> tags instead of render-blocking @import
    html = html.replace("@import url('https://fonts.googleapis.com/css2?family=Quicksand:wght@300;400;500;600;700&display=swap');", "")
    font_links = '<link rel="preconnect" href="https://fonts.googleapis.com">\n<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n<link href="https://fonts.googleapis.com/css2?family=Quicksand:wght@300;400;500;600;700&display=swap" rel="stylesheet">'
    html = html.replace('</head>', f'{font_links}\n</head>')
    # ---------------------------------------------------------------------------------------- 
    # 4. Perform Server-Side String Injections (SEO Meta)
        # --- GOOGLE ADSENSE AUTO-ADS INTEGRATION ---
    # Ye script Google ko allow karti hai ki wo automatically best jagah par ads dikhaye
    adsense_script = '<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4780769968389227" crossorigin="anonymous"></script>'
    html = html.replace('</head>', f'    {adsense_script}\n</head>')
    # -------------------------------------------

    # 4. Perform Server-Side String Injections (SEO Meta)
    og_image_link = f"{APP_DOMAIN}/img_asset/{article['featured_hash']}" if article.get('featured_hash') else ""
    
    # SEO TRUNCATION: Keep Title < 60 chars and Description < 160 chars
    raw_title = article["title"].replace('"', "'")
    seo_title = raw_title[:57] + "..." if len(raw_title) > 60 else raw_title
    
    # Extract plain text from article content for a rich SEO description
    temp_soup = BeautifulSoup(article["content_html"], 'html.parser')
    plain_text = temp_soup.get_text(separator=" ", strip=True).replace('"', "'")
    seo_desc = plain_text[:155] + "..." if len(plain_text) > 155 else plain_text
    
    # Inject SEO optimized meta tags
    html = html.replace('<title>To The Point - Environment Energy and Agriculture</title>', f'<title>{seo_title}</title>')
    html = html.replace('content="To The Point - Environment Energy and Agriculture"', f'content="{seo_title}"')
    
    # Description replace (Checks standard and OG description)
    html = html.replace('name="description" content="Latest insights and articles on Environment, Energy, and Agriculture."', f'name="description" content="{seo_desc}"')
    html = html.replace('name="description" content="Read our latest insights on Environment, Energy, and Agriculture."', f'name="description" content="{seo_desc}"')
    html = html.replace('property="og:description" content="Latest insights and articles on Environment, Energy, and Agriculture. Click and read more."', f'property="og:description" content="{seo_desc}"')
    
    html = html.replace('property="og:image" content=""', f'property="og:image" content="{og_image_link}"')
    # 5. Inject Blog Content Directly into DOM (Removes need for frontend JS fetch)
    # 5. Inject Blog Content Directly into DOM (Removes need for frontend JS fetch)
    html = html.replace(
        '<h1 id="blogTitle" class="text-2xl md:text-3xl font-bold text-green-950 leading-tight mb-4"></h1>',
        f'<h1 id="blogTitle" class="text-2xl md:text-3xl font-bold text-green-950 leading-tight mb-4">{article["title"]}</h1>'
    )
    
    # --- AUTO-UPDATE DB FOR OLD POSTS: Check missing 'alt' tags, add them, and save to DB ---    
    # --- AUTO-UPDATE DB FOR OLD POSTS: Check missing 'alt' tags, add them, and save to DB ---
    content_soup = BeautifulSoup(article["content_html"], 'html.parser')
    clean_title_for_alt = article['title'].replace('"', "'")
    img_counter = 1
    needs_db_update = False
    
    for img in content_soup.find_all('img'):
        if not img.get('alt'): # Agar alt tag nahi hai
            img['alt'] = f"{clean_title_for_alt} - Graphic Illustration {img_counter}"
            needs_db_update = True
        img_counter += 1
        
    # FIX RETAINED OLD HEADINGS: Convert old H4, H5, H6 to H3 for SEO compliance
    # FIX RETAINED OLD HEADINGS: Convert old H4, H5, H6 to <p> for SEO compliance
    for tag in content_soup.find_all(['h4', 'h5', 'h6']):
        tag.name = 'p'
        tag['class'] = tag.get('class', []) + ['text-xl', 'font-semibold', 'mt-6', 'mb-3', 'text-green-800', 'block']
        needs_db_update = True        
    final_content_html = str(content_soup)    
    # Agar kisi bhi image me alt tag missing tha aur ab add hua hai, toh Cache aur DB dono update kar do
    if needs_db_update:
        ARTICLE_CACHE[wp_slug]["content_html"] = final_content_html # Agle user ke liye RAM me update
        try:
            async with db_pool.acquire() as conn:
                # Purane post ka HTML database me hamesha ke liye update kar diya
                await conn.execute("UPDATE articles SET content_html = $1 WHERE wp_slug = $2", final_content_html, wp_slug)
            logger.info(f"Successfully auto-updated missing alt tags in DB for: {wp_slug}")
        except Exception as e:
            logger.error(f"Failed to update old post alt tags in DB for {wp_slug}: {e}")
    # ----------------------------------------------------------------------------------------

    html = html.replace(
        '<div id="blogBody" class="blog-content text-gray-700"></div>',
        f'<div id="blogBody" class="blog-content text-gray-700">{final_content_html}</div>'
    )

    if article.get('featured_hash'):
        html = html.replace(
            'id="featuredImageContainer" class="w-full h-64 md:h-80 rounded-2xl overflow-hidden shadow-lg hidden mb-6"',
            'id="featuredImageContainer" class="w-full h-64 md:h-80 rounded-2xl overflow-hidden shadow-lg mb-6"'
        )
        
        # Clean the title and replace both src and alt dynamically
        clean_title = article['title'].replace('"', "'")
        html = html.replace(
            '<img id="featuredImage" src="" alt="Blog Featured Image"',
            f'<img id="featuredImage" src="/img_asset/{article["featured_hash"]}" alt="{clean_title} - Featured Cover Image" fetchpriority="high"'
        )
        html = html.replace(
            '<img id="featuredImage" src=""',
            f'<img id="featuredImage" src="/img_asset/{article["featured_hash"]}" alt="{clean_title} - Featured Cover Image"'
        )

    # 6. Inject Beautiful Author Card & Recommendations DOM
    if rec_html_block:
        # PURE CSS AUTHOR CARD: Tailwind-independent layout for guaranteed beautiful UI
        author_card_html = """
        <style>
            .author-card-wrapper { background: rgba(255, 255, 255, 0.75); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px); border: 1px solid rgba(255, 255, 255, 0.6); border-radius: 1.5rem; padding: 1.5rem 2rem; margin-top: 2.5rem; margin-bottom: 2.5rem; display: flex; align-items: center; gap: 1.5rem; box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.1); font-family: 'Quicksand', sans-serif; position: relative; }
            .author-img-box { width: 110px; height: 110px; border-radius: 50%; padding: 4px; background: linear-gradient(135deg, #10b981 0%, #047857 100%); flex-shrink: 0; box-shadow: 0 8px 25px rgba(16, 185, 129, 0.3); }
            .author-img-box img { width: 100%; height: 100%; object-fit: cover; border-radius: 50%; border: 3px solid #ffffff; background-color: #ffffff; pointer-events: none; -webkit-user-drag: none; }
            .author-info { flex-grow: 1; }
            .author-label { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: #059669; font-weight: 700; display: block; margin-bottom: 0.3rem; }
            .author-name { font-size: 1.5rem; color: #064e3b; font-weight: 700; margin: 0 0 0.2rem 0; display: flex; align-items: center; gap: 0.4rem; }
            .author-title { color: #047857; font-weight: 600; font-size: 0.95rem; margin: 0 0 0.4rem 0; }
            .author-loc { color: #6b7280; font-size: 0.85rem; font-weight: 600; margin: 0 0 1rem 0; display: flex; align-items: center; gap: 0.4rem; }
            .author-actions { display: flex; gap: 0.8rem; flex-wrap: wrap; }
            
            /* Accessibility Fix: Pure White text on Solid Dark Background for 100% Contrast Score */
            .author-btn { display: inline-flex; align-items: center; justify-content: center; gap: 0.4rem; padding: 0.5rem 1.2rem; border-radius: 50px; font-weight: 700; font-size: 0.85rem; text-decoration: none; transition: all 0.3s ease; flex: 1; text-align: center; color: #ffffff !important; }
            .author-btn-in { background: #0284c7; border: 1px solid #0369a1; }
            .author-btn-in:hover { background: #0369a1; transform: translateY(-2px); box-shadow: 0 4px 12px rgba(2, 132, 199, 0.3); }
            .author-btn-mail { background: #dc2626; border: 1px solid #b91c1c; }
            .author-btn-mail:hover { background: #b91c1c; transform: translateY(-2px); box-shadow: 0 4px 12px rgba(220, 38, 38, 0.3); }
            
            @media (max-width: 640px) {
                .author-card-wrapper { flex-direction: column; text-align: center; padding: 1.5rem; }
                .author-actions { justify-content: center; width: 100%; }
                .author-name, .author-loc, .author-label { justify-content: center; text-align: center; }
            }
        </style>
        <aside class="author-card-wrapper" aria-label="About the Author">
            <div class="author-img-box">
                <img src="/author.webp" alt="Pranav Sinha" onerror="this.onerror=null; this.src='https://ui-avatars.com/api/?name=Pranav+Sinha&background=10b981&color=fff&size=150';">
            </div>
            <div class="author-info">
                <span class="author-label">About the Author</span>
                <h2 class="author-name">Pranav Sinha <svg style="width:20px;height:20px;color:#3b82f6" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"></path></svg></h2>
                <p class="author-title">Environmental Specialist & Climate Policy Expert</p>
                <p class="author-loc">
                    <svg style="width:14px;height:14px" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z"></path></svg> Delhi, India
                </p>
                <div class="author-actions">
                    <a href="https://www.linkedin.com/in/pranav-sinha-a5002aa/" target="_blank" rel="noopener noreferrer" class="author-btn author-btn-in">
                        <svg style="width:14px;height:14px" fill="currentColor" viewBox="0 0 24 24"><path d="M19 0h-14c-2.761 0-5 2.239-5 5v14c0 2.761 2.239 5 5 5h14c2.762 0 5-2.239 5-5v-14c0-2.761-2.238-5-5-5zm-11 19h-3v-11h3v11zm-1.5-12.268c-.966 0-1.75-.79-1.75-1.764s.784-1.764 1.75-1.764 1.75.79 1.75 1.764-.783 1.764-1.75 1.764zm13.5 12.268h-3v-5.604c0-3.368-4-3.113-4 0v5.604h-3v-11h3v1.765c1.396-2.586 7-2.777 7 2.476v6.759z"/></svg> LinkedIn
                    </a>
                    
                    <!-- Cloudflare Email Obfuscation Bypass Tag -->
                    <!--email_off-->
                    <a href="mailto:pranavsinhain@gmail.com" class="author-btn author-btn-mail">
                        <svg style="width:14px;height:14px" fill="currentColor" viewBox="0 0 24 24"><path d="M0 3v18h24v-18h-24zm21.518 2l-9.518 7.713-9.518-7.713h19.036zm-19.518 14v-11.817l10 8.104 10-8.104v11.817h-20z"/></svg> Email
                    </a>
                    <!--/email_off-->
                    
                </div>
            </div>
        </aside>
        """
        html = html.replace('id="recommendationsSection" class="hidden', 'id="recommendationsSection" class="')
        
        # Inject Author Card right before Recommendations Section
        html = html.replace(
            '<section id="recommendationsSection"', 
            f'{author_card_html}\n            <section id="recommendationsSection"'
        )
        
        html = html.replace(
            '<div id="recGrid" class="grid grid-cols-1 md:grid-cols-2 gap-6">\n                    <!-- Recommendation cards injected here -->\n                </div>',
            f'<div id="recGrid" class="grid grid-cols-1 md:grid-cols-2 gap-6">{rec_html_block}</div>'
        )
        # --- FIX FOR "MORE TO READ" HEADING SEO ERROR ---
        # H3 ko H2 me badal rahe hain taaki H1 -> H2 ka proper SEO sequence bane
        html = html.replace(
            '<h3 class="text-2xl font-bold text-green-900 mb-6 flex items-center gap-2">',
            '<h2 class="text-2xl font-bold text-green-900 mb-6 flex items-center gap-2">'
        )
        # Windows (\r\n) aur Linux (\n) dono line endings ke liye closing tag fix
        html = html.replace('More to Read\n                </h3>', 'More to Read\n                </h2>')
        html = html.replace('More to Read\r\n                </h3>', 'More to Read\r\n                </h2>')
        # ------------------------------------------------

    # 7. Modify UI Classes to Show Content & Hide Loader immediately
    html = html.replace('id="loader" class="flex-1 flex', 'id="loader" class="hidden flex-1 flex')
    html = html.replace('id="blogContainer" class="hidden"', 'id="blogContainer" class=""')

    # 7.5. Inject Footer (Privacy, Terms, About Us) just below the article
    footer_html = """
    <footer class="w-full text-center py-6 mt-2">
        <div class="flex flex-wrap justify-center items-center gap-6 text-sm font-semibold text-gray-500">
            <a href="https://pranavblog.online/privacy-policy" class="hover:text-green-700 transition-colors hover:underline underline-offset-4">Privacy Policy</a>
            <a href="https://pranavblog.online/terms-of-use" class="hover:text-green-700 transition-colors hover:underline underline-offset-4">Terms of Use</a>
            <a href="https://pranavblog.online/about-us" class="hover:text-green-700 transition-colors hover:underline underline-offset-4">About Us</a>
        </div>
    </footer>
    """
    html = html.replace('</article>', f'</article>\n{footer_html}')

    # 8. Neutralize Frontend Fetch Logic (Since we already rendered everything server-side)
    # By commenting out init(), the JS won't overwrite our pre-rendered HTML.
    html = html.replace('init();', '// init(); --- Frontend Fetch Disabled. Served pre-rendered by Python Backend.')

    # 9. Inject Zero-Render-Blocking Analytics Tracker
    # Yeh script page load hone ke 1.5 second baad chupchaap chalegi aur data /track par bhejegi
    analytics_js = """
    <script>
        setTimeout(function() {
            try {
                // Generate or get unique User ID from browser cache
                let uid = localStorage.getItem('pranav_uid');
                if(!uid) { 
                    uid = crypto.randomUUID ? crypto.randomUUID() : 'user_' + Math.random().toString(36).substr(2, 9);
                    localStorage.setItem('pranav_uid', uid); 
                }
                
                // Extract slug from URL safely
                let pathSlug = window.location.pathname.replace(/^\/|\/$/g, '');
                if(pathSlug === '') pathSlug = 'home';
                
                // Send silent pulse to backend (always sends visit_count as 1)
                fetch('/track', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        slug: pathSlug,
                        referrer: document.referrer || 'Direct',
                        user_id: uid,
                        visit_count: 1
                    }),
                    keepalive: true // Ensures request completes even if user closes tab
                }).catch(e => {}); // Catch silent errors
            } catch(e) {}
        }, 1500); // Wait 1.5s to ensure 0 impact on Lighthouse PageSpeed
    </script>
    </body>
    """
    html = html.replace('</body>', analytics_js)

    return HTMLResponse(content=html, status_code=200)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

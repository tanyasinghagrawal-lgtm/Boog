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
from datetime import datetime

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

# --- DATA MODELS ---
class UserSignup(BaseModel):
    username: str
    password: str

class AddPost(BaseModel):
    slug: str
    original_url: str

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

async def process_image_to_webp(image_bytes: bytes) -> bytes:
    """Compresses image to WebP format targeting < 50KB."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        max_width = 800
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
        logger.error(f"Image processing error: {e}")
        return image_bytes

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
        for img in soup.find_all('img'):
            src = img.get('src')
            if src:
                res = await download_and_process_image(src, client)
                if res:
                    c_hash, c_bytes = res
                    await save_image_to_db(c_hash, c_bytes)
                    img['src'] = f"/img_asset/{c_hash}" # Replace external URL with local optimized endpoint
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

# --- BACKGROUND TASKS ---

async def startup_sync():
    """On boot: Load maps into cache, find missing DB articles, and fetch them."""
    global URL_MAP_CACHE
    async with db_pool.acquire() as conn:
        # Load mappings
        rows = await conn.fetch("SELECT short_slug, original_url, wp_slug FROM url_mappings")
        for r in rows:
            URL_MAP_CACHE[r['short_slug']] = r['original_url']
            
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
    """Runs every 10 mins: Checks WP for new posts, generates short link, downloads and processes."""
    while True:
        await asyncio.sleep(600) # 10 Minutes
        logger.info("Scanning WordPress for new posts...")
        try:
            api_url = f"https://public-api.wordpress.com/rest/v1.1/sites/{WP_SITE_URL}/posts?number=5&fields=slug,URL"
            async with httpx.AsyncClient() as client:
                resp = await client.get(api_url, timeout=10.0)
                if resp.status_code == 200:
                    posts = resp.json().get('posts', [])
                    
                    async with db_pool.acquire() as conn:
                        existing_slugs = await conn.fetch("SELECT wp_slug FROM url_mappings")
                        existing_set = {r['wp_slug'] for r in existing_slugs}
                        
                        for p in posts:
                            wp_slug = p['slug']
                            orig_url = p['URL']
                            
                            if wp_slug not in existing_set:
                                # New post found! Auto-generate short slug
                                short_slug = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
                                logger.info(f"New Post Detected: {wp_slug}. Auto-generating link: /{short_slug}")
                                
                                # Insert Mapping
                                await conn.execute(
                                    "INSERT INTO url_mappings (short_slug, original_url, wp_slug) VALUES ($1, $2, $3)",
                                    short_slug, orig_url, wp_slug
                                )
                                URL_MAP_CACHE[short_slug] = orig_url
                                
                                # Scrape, Compress Images, Save to DB
                                await scrape_and_save_article(wp_slug, client)
        except Exception as e:
            logger.error(f"Auto-discover scanner failed: {e}")

# --- LIFESPAN MANAGER ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server Starting... Initializing DB and Cache.")
    await init_db()
    
    # Run sync synchronously so cache is ready immediately
    await startup_sync()
    
    # Start loop for continuous scanning
    scanner_task = asyncio.create_task(auto_discover_new_posts())
    yield
    scanner_task.cancel()
    if db_pool:
        await db_pool.close()
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
    return [{"slug": k, "original_url": v} for k, v in URL_MAP_CACHE.items()]

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

@app.get("/")
async def root():
    return RedirectResponse(url=HOME_PAGE_URL)

@app.get("/img_asset/{img_hash}")
async def serve_optimized_image(img_hash: str):
    """Serves compressed images completely from DB/RAM."""
    # Check RAM
    if img_hash in IMAGE_CACHE:
        update_cache_metrics(IMAGE_CACHE[img_hash])
        return Response(content=IMAGE_CACHE[img_hash]["bytes"], media_type="image/webp")
        
    # Check DB
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT image_data FROM images WHERE image_hash = $1", img_hash)
        if row:
            img_bytes = row['image_data']
            add_image_to_cache(img_hash, img_bytes)
            return Response(content=img_bytes, media_type="image/webp")
            
    # Fallback missing image
    return Response(status_code=404)

@app.get("/{slug}.png")
async def legacy_og_image(slug: str):
    """Fallback route for older platforms requesting .png specifically. Serves webp internally."""
    original_url = URL_MAP_CACHE.get(slug)
    if not original_url:
        return Response(status_code=404)
        
    wp_slug = original_url.strip('/').split('/')[-1]
    article = ARTICLE_CACHE.get(wp_slug)
    
    if article and article.get('featured_hash'):
        return RedirectResponse(url=f"/img_asset/{article['featured_hash']}")
    return Response(status_code=404)

@app.get("/{slug}")
async def server_side_rendered_blog(slug: str):
    """
    Core SSR Logic. Replaces HTML contents dynamically so 
    the frontend does NO fetching, ensuring perfect SEO and speed.
    """
    original_url = URL_MAP_CACHE.get(slug)
    if not original_url:
        return RedirectResponse(url=HOME_PAGE_URL)

    wp_slug = original_url.strip('/').split('/')[-1]
    
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
    all_slugs = list(URL_MAP_CACHE.keys())
    if slug in all_slugs:
        all_slugs.remove(slug)
    random_recs = random.sample(all_slugs, min(4, len(all_slugs)))
    
    rec_html_block = ""
    for r_slug in random_recs:
        r_wp_slug = URL_MAP_CACHE[r_slug].strip('/').split('/')[-1]
        rec_article = ARTICLE_CACHE.get(r_wp_slug)
        
        if rec_article:
            r_title = rec_article['title']
            r_hash = rec_article.get('featured_hash')
            
            img_html = f'<div class="h-40 w-full overflow-hidden bg-gray-100"><img src="/img_asset/{r_hash}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"></div>' if r_hash else '<div class="h-40 w-full bg-green-50 flex items-center justify-center"><i class="fas fa-leaf text-green-200 text-4xl"></i></div>'
            
            rec_html_block += f"""
                <a href="/{r_slug}" class="glass-morphism glass-card rounded-xl overflow-hidden group block text-left">
                    {img_html}
                    <div class="p-5">
                        <h4 class="font-bold text-green-900 leading-tight mb-2 group-hover:text-green-700 transition-colors line-clamp-2">{r_title}</h4>
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

    # 4. Perform Server-Side String Injections (SEO Meta)
    og_image_link = f"{APP_DOMAIN}/img_asset/{article['featured_hash']}" if article.get('featured_hash') else ""
    
    html = html.replace('<title>To The Point - Environment Energy and Agriculture</title>', f'<title>{article["title"]} - To The Point</title>')
    html = html.replace('content="To The Point - Environment Energy and Agriculture"', f'content="{article["title"]}"')
    html = html.replace('name="description" content="Latest insights and articles on Environment, Energy, and Agriculture."', 'name="description" content="Read our latest insights on Environment, Energy, and Agriculture."')
    html = html.replace('property="og:image" content=""', f'property="og:image" content="{og_image_link}"')

    # 5. Inject Blog Content Directly into DOM (Removes need for frontend JS fetch)
    html = html.replace(
        '<h1 id="blogTitle" class="text-2xl md:text-3xl font-bold text-green-950 leading-tight mb-4"></h1>',
        f'<h1 id="blogTitle" class="text-2xl md:text-3xl font-bold text-green-950 leading-tight mb-4">{article["title"]}</h1>'
    )
    
    html = html.replace(
        '<div id="blogBody" class="blog-content text-gray-700"></div>',
        f'<div id="blogBody" class="blog-content text-gray-700">{article["content_html"]}</div>'
    )

    if article.get('featured_hash'):
        html = html.replace(
            'id="featuredImageContainer" class="w-full h-64 md:h-80 rounded-2xl overflow-hidden shadow-lg hidden mb-6"',
            'id="featuredImageContainer" class="w-full h-64 md:h-80 rounded-2xl overflow-hidden shadow-lg mb-6"'
        )
        html = html.replace(
            '<img id="featuredImage" src=""',
            f'<img id="featuredImage" src="/img_asset/{article["featured_hash"]}"'
        )

    # 6. Inject Recommendations DOM
    if rec_html_block:
        html = html.replace('id="recommendationsSection" class="hidden', 'id="recommendationsSection" class="')
        html = html.replace(
            '<div id="recGrid" class="grid grid-cols-1 md:grid-cols-2 gap-6">\n                    <!-- Recommendation cards injected here -->\n                </div>',
            f'<div id="recGrid" class="grid grid-cols-1 md:grid-cols-2 gap-6">{rec_html_block}</div>'
        )

    # 7. Modify UI Classes to Show Content & Hide Loader immediately
    html = html.replace('id="loader" class="flex-1 flex', 'id="loader" class="hidden flex-1 flex')
    html = html.replace('id="blogContainer" class="hidden"', 'id="blogContainer" class=""')

    # 8. Neutralize Frontend Fetch Logic (Since we already rendered everything server-side)
    # By commenting out init(), the JS won't overwrite our pre-rendered HTML.
    html = html.replace('init();', '// init(); --- Frontend Fetch Disabled. Served pre-rendered by Python Backend.')

    return HTMLResponse(content=html, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

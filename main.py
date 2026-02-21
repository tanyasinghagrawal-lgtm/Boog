import asyncio
import logging
import random
import json
import io
import time
import sys
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import asyncpg
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from PIL import Image
from passlib.context import CryptContext

# --- CONFIGURATION (Hardcoded) ---
DB_DSN = "postgres://avnadmin:AVNS_d9GncXE-Fge9t5p3XlY@pg-7cbbad8-tanyasinghagrawal-62c1.j.aivencloud.com:26734/defaultdb?sslmode=require"
WP_SITE_URL = "pranavcea.wordpress.com"
HOME_PAGE_URL = "https://www.pranavblog.online/home"
APP_DOMAIN = "https://blog.pranavblog.online"

# --- GLOBAL IN-MEMORY CACHE ---
URL_CACHE: Dict[str, str] = {}
HTML_CACHE: Dict[str, str] = {}
CACHE_ACCESS_TIMES: Dict[str, float] = {}
MAX_CACHE_SIZE = 300 * 1024 * 1024  # 300 MB

# Logger setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Password Hashing Setup
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- DATA MODELS ---
class UserSignup(BaseModel):
    username: str
    password: str

class AddPost(BaseModel):
    slug: str
    original_url: str

# --- CACHE MANAGEMENT (LRU 300MB LIMIT) ---

def get_cache_size():
    """Returns the total size of cached HTML strings in bytes."""
    return sum(len(v.encode('utf-8')) for v in HTML_CACHE.values())

def update_html_cache(slug: str, html_content: str):
    """Adds HTML to cache and evicts least recently used if size > 300MB."""
    size = len(html_content.encode('utf-8'))
    
    # Evict oldest pages if memory limit is exceeded
    while get_cache_size() + size > MAX_CACHE_SIZE and HTML_CACHE:
        oldest_slug = min(CACHE_ACCESS_TIMES, key=CACHE_ACCESS_TIMES.get)
        logger.info(f"Cache limit reaching 300MB. Evicting inactive cache for: {oldest_slug}")
        del HTML_CACHE[oldest_slug]
        del CACHE_ACCESS_TIMES[oldest_slug]
        
    HTML_CACHE[slug] = html_content
    CACHE_ACCESS_TIMES[slug] = time.time()

def get_html_cache(slug: str) -> Optional[str]:
    """Retrieves cached HTML and updates last access time."""
    if slug in HTML_CACHE:
        CACHE_ACCESS_TIMES[slug] = time.time()
        return HTML_CACHE[slug]
    return None

# --- DATABASE FUNCTIONS ---

async def init_db():
    """Create necessary tables if they don't exist."""
    try:
        conn = await asyncpg.connect(DB_DSN)
        # Create users table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            );
        """)
        # Create mappings table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS url_mappings (
                id SERIAL PRIMARY KEY,
                short_slug TEXT UNIQUE NOT NULL,
                original_url TEXT NOT NULL
            );
        """)
        # Create articles table (Stores full content and webp image)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                wp_slug TEXT PRIMARY KEY,
                title TEXT,
                content TEXT,
                image_webp BYTEA
            );
        """)
        await conn.close()
        logger.info("Database tables verified.")
    except Exception as e:
        logger.error(f"DB Init Error: {e}")

async def fetch_all_mappings():
    """Fetches all URL mappings from Aiven DB."""
    try:
        conn = await asyncpg.connect(DB_DSN)
        rows = await conn.fetch("SELECT short_slug, original_url FROM url_mappings")
        await conn.close()
        
        new_cache = {}
        for row in rows:
            slug = row['short_slug'].strip('/')
            new_cache[slug] = row['original_url']
        
        global URL_CACHE
        URL_CACHE = new_cache
        logger.info(f"URL Cache updated. Total links: {len(URL_CACHE)}")
    except Exception as e:
        logger.error(f"DB Error: {e}")

async def resolve_slug_fallback(slug: str) -> Optional[str]:
    """Fallback: Check DB directly if not in RAM."""
    try:
        conn = await asyncpg.connect(DB_DSN)
        row = await conn.fetchrow("SELECT original_url FROM url_mappings WHERE short_slug = $1", slug)
        await conn.close()
        if row:
            URL_CACHE[slug] = row['original_url']
            return row['original_url']
    except Exception as e:
        logger.error(f"DB Fallback Error: {e}")
    return None

# --- IMAGE OPTIMIZATION (WEBP) ---

def compress_to_webp(image_bytes: bytes) -> bytes:
    """Resizes and compresses image to WebP (10-50KB) maintaining aspect ratio."""
    try:
        if not image_bytes: return b""
        
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        max_width = 800
        if img.width > max_width:
            ratio = max_width / float(img.width)
            new_height = int((float(img.height) * float(ratio)))
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
        
        output = io.BytesIO()
        quality = 85
        
        # Initial Save as WEBP
        img.save(output, format="WEBP", quality=quality, method=4)
        
        # Compress loop to target <= 50KB
        while output.tell() > 50 * 1024 and quality > 10:
            output.seek(0)
            output.truncate(0)
            quality -= 10
            # If quality alone isn't enough, reduce dimension
            if quality < 50:
                img = img.resize((int(img.width * 0.8), int(img.height * 0.8)), Image.Resampling.LANCZOS)
            
            img.save(output, format="WEBP", quality=quality, method=4)
            
        return output.getvalue()
    except Exception as e:
        logger.error(f"WebP Compression error: {e}")
        return image_bytes

# --- WP FETCHING LOGIC ---

async def fetch_wp_and_save(client: httpx.AsyncClient, conn, wp_slug: str):
    """Fetches single article data from WP API, compresses image, and saves to DB."""
    try:
        api_url = f"https://public-api.wordpress.com/rest/v1.1/sites/{WP_SITE_URL}/posts/slug:{wp_slug}?fields=title,featured_image,content"
        resp = await client.get(api_url, timeout=10.0)
        
        if resp.status_code == 200:
            data = resp.json()
            title = data.get('title', 'To The Point')
            content = data.get('content', '')
            img_url = data.get('featured_image')

            # Fallback for image in HTML body
            if not img_url and content:
                soup = BeautifulSoup(content, 'html.parser')
                img_tag = soup.find('img')
                if img_tag and img_tag.get('src'):
                    img_url = img_tag.get('src')
            
            webp_bytes = b""
            if img_url:
                try:
                    img_resp = await client.get(img_url, timeout=10.0)
                    if img_resp.status_code == 200:
                        webp_bytes = compress_to_webp(img_resp.content)
                except Exception as e:
                    logger.error(f"Error fetching image for {wp_slug}: {e}")
            
            # Save into DB
            await conn.execute("""
                INSERT INTO articles (wp_slug, title, content, image_webp) 
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (wp_slug) DO NOTHING
            """, wp_slug, title, content, webp_bytes)
            
            return {"title": title, "content": content, "image_webp": webp_bytes}
    except Exception as e:
        logger.error(f"WP fetch error for {wp_slug}: {e}")
    return None

async def get_or_fetch_article(wp_slug: str):
    """Gets article from DB. If not present, fetches from WP and saves."""
    conn = await asyncpg.connect(DB_DSN)
    try:
        row = await conn.fetchrow("SELECT title, content FROM articles WHERE wp_slug = $1", wp_slug)
        if row:
            return dict(row)
        
        # Not in DB, fetch on demand
        logger.info(f"Article {wp_slug} missing in DB. Fetching synchronously.")
        async with httpx.AsyncClient() as client:
            return await fetch_wp_and_save(client, conn, wp_slug)
    finally:
        await conn.close()

# --- BACKGROUND TASKS ---

async def background_sync_task():
    """Checks missing articles from URL cache and saves them to DB in background."""
    while True:
        logger.info("Running background sync & mapping task...")
        await fetch_all_mappings()
        
        try:
            conn = await asyncpg.connect(DB_DSN)
            existing_records = await conn.fetch("SELECT wp_slug FROM articles")
            existing_slugs = {r['wp_slug'] for r in existing_records}
            
            async with httpx.AsyncClient() as client:
                for slug, orig_url in URL_CACHE.items():
                    wp_slug = orig_url.strip('/').split('/')[-1]
                    if wp_slug not in existing_slugs:
                        logger.info(f"Background Sync: Fetching missing article {wp_slug}")
                        await fetch_wp_and_save(client, conn, wp_slug)
                        existing_slugs.add(wp_slug)
                        await asyncio.sleep(2)  # Avoid rate limiting
                        
            await conn.close()
        except Exception as e:
            logger.error(f"Background Task Error: {e}")
            
        await asyncio.sleep(180) # Run every 3 Minutes

# --- LIFESPAN MANAGER ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server Starting... Initializing DB and Cache.")
    await init_db()
    await fetch_all_mappings()
    task = asyncio.create_task(background_sync_task())
    yield
    task.cancel()
    logger.info("Server Shutting Down.")

# --- FASTAPI APP ---

app = FastAPI(lifespan=lifespan)

# --- CORS MIDDLEWARE ---
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
    conn = await asyncpg.connect(DB_DSN)
    try:
        count_val = await conn.fetchval("SELECT COUNT(*) FROM users")
        if count_val >= 3:
            raise HTTPException(status_code=403, detail="Signup limit reached (Max 3 users).")
        
        exists = await conn.fetchval("SELECT id FROM users WHERE username = $1", user.username)
        if exists:
            raise HTTPException(status_code=400, detail="Username already taken.")

        hashed_password = pwd_context.hash(user.password)
        await conn.execute("INSERT INTO users (username, password_hash) VALUES ($1, $2)", user.username, hashed_password)
        
        return {"status": "success", "message": "User created successfully."}
    finally:
        await conn.close()

@app.post("/verify")
async def verify_user(user: UserSignup):
    conn = await asyncpg.connect(DB_DSN)
    try:
        row = await conn.fetchrow("SELECT password_hash FROM users WHERE username = $1", user.username)
        if not row or not pwd_context.verify(user.password, row['password_hash']):
            raise HTTPException(status_code=401, detail="Invalid username or password")
            
        return {"status": "success", "message": "Verified"}
    finally:
        await conn.close()

@app.get("/allpost")
async def get_all_posts():
    return [{"slug": k, "original_url": v} for k, v in URL_CACHE.items()]

@app.post("/addpost")
async def add_post(post: AddPost):
    clean_slug = post.slug.strip('/')
    conn = await asyncpg.connect(DB_DSN)
    try:
        exists = await conn.fetchval("SELECT id FROM url_mappings WHERE short_slug = $1", clean_slug)
        if exists:
            raise HTTPException(status_code=400, detail="Slug already exists.")
            
        await conn.execute(
            "INSERT INTO url_mappings (short_slug, original_url) VALUES ($1, $2)",
            clean_slug, post.original_url
        )
        URL_CACHE[clean_slug] = post.original_url
        return {"status": "success", "slug": clean_slug, "url": post.original_url}
    except Exception as e:
        logger.error(f"Add Post Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()

# --- PUBLIC ROUTES ---

@app.get("/")
async def root():
    return RedirectResponse(url=HOME_PAGE_URL)

@app.get("/{slug}.png")
@app.get("/image/{slug}.webp")
async def serve_compressed_image(slug: str):
    """
    Serves WebP compressed image from PostgreSQL DB.
    Also handles legacy .png fallback requests for social media previews.
    """
    wp_slug = slug
    
    # If a short slug was passed as .png, resolve to wp_slug first
    if slug in URL_CACHE:
        wp_slug = URL_CACHE[slug].strip('/').split('/')[-1]
    
    conn = await asyncpg.connect(DB_DSN)
    try:
        row = await conn.fetchrow("SELECT image_webp FROM articles WHERE wp_slug = $1", wp_slug)
        if row and row['image_webp']:
            # We return image/webp even for .png extension to enforce high compression size saving
            return Response(content=row['image_webp'], media_type="image/webp")
    finally:
        await conn.close()
        
    return Response(status_code=404)

@app.get("/{slug}")
async def blog_viewer(slug: str):
    # 1. Check RAM Cache First (Fast SSR Serve)
    cached_html = get_html_cache(slug)
    if cached_html:
        return HTMLResponse(content=cached_html, status_code=200)

    # 2. Get URL Mapping
    original_url = URL_CACHE.get(slug)
    if not original_url:
        original_url = await resolve_slug_fallback(slug)
    
    if not original_url:
        return RedirectResponse(url=HOME_PAGE_URL)

    wp_slug = original_url.strip('/').split('/')[-1]

    # 3. Get Complete Article (DB / Sync Fetch)
    article_data = await get_or_fetch_article(wp_slug)
    if not article_data:
        return HTMLResponse("<h1>Error: Content not found</h1>", status_code=404)

    # 4. Generate Random Recommendations (Get from DB)
    all_slugs = list(URL_CACHE.keys())
    if slug in all_slugs:
        all_slugs.remove(slug)
    random_recs = random.sample(all_slugs, min(4, len(all_slugs)))
    
    rec_data = []
    conn = await asyncpg.connect(DB_DSN)
    for r_slug in random_recs:
        r_wp_slug = URL_CACHE[r_slug].strip('/').split('/')[-1]
        rec_row = await conn.fetchrow("SELECT title FROM articles WHERE wp_slug = $1", r_wp_slug)
        rec_title = rec_row['title'] if rec_row else "Read More"
        rec_data.append({
            "link": f"/{r_slug}",
            "wp_slug": r_wp_slug,
            "title": rec_title
        })
    await conn.close()

    # 5. Read HTML and Inject via BeautifulSoup (No index.html file edits needed)
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html_content = f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>Error: index.html not found</h1>", status_code=500)

    soup = BeautifulSoup(html_content, 'html.parser')
    
    # --- SEO Metadata Updates ---
    page_title = f"{article_data['title']} - To The Point"
    if soup.title:
        soup.title.string = page_title
    else:
        new_title = soup.new_tag("title")
        new_title.string = page_title
        soup.head.append(new_title)
        
    og_image_link = f"{APP_DOMAIN}/image/{wp_slug}.webp"
    raw_text = BeautifulSoup(article_data['content'], "html.parser").get_text()
    desc_text = raw_text[:160] + "..." if raw_text else page_title

    for meta in soup.find_all('meta'):
        if meta.get('property') in ['og:title', 'twitter:title'] or meta.get('name') == 'title':
            meta['content'] = article_data["title"]
        elif meta.get('property') in ['og:image', 'twitter:image']:
            meta['content'] = og_image_link
        elif meta.get('name') == 'description' or meta.get('property') == 'og:description':
            meta['content'] = desc_text

    # --- Construct Full Server-Side Rendered (SSR) HTML ---
    ssr_container = soup.new_tag("div", id="ssr-seo-container")
    
    # Article Body
    article_html = f"""
    <article style="max-width: 800px; margin: 2rem auto; padding: 0 1rem; font-family: system-ui, -apple-system, sans-serif;">
        <h1 style="font-size: 2.5rem; font-weight: bold; margin-bottom: 1.5rem; color: #1a202c; line-height: 1.2;">{article_data['title']}</h1>
        <img src="/image/{wp_slug}.webp" alt="{article_data['title']}" style="width: 100%; height: auto; border-radius: 12px; margin-bottom: 2rem; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);" />
        <div class="ssr-content" style="line-height: 1.8; font-size: 1.125rem; color: #4a5568;">
            {article_data['content']}
        </div>
    </article>
    """
    ssr_container.append(BeautifulSoup(article_html, "html.parser"))

    # Recommendations Section
    recs_html = "<div style='max-width: 800px; margin: 4rem auto; padding: 0 1rem; font-family: system-ui, -apple-system, sans-serif;'>"
    recs_html += "<h2 style='font-size: 1.8rem; font-weight: bold; margin-bottom: 1.5rem; color: #2d3748; border-bottom: 2px solid #e2e8f0; padding-bottom: 0.5rem;'>Recommended Articles</h2>"
    recs_html += "<div style='display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 1.5rem;'>"
    
    for rec in rec_data:
        recs_html += f"""
        <a href="{rec['link']}" style="text-decoration: none; color: inherit; display: flex; flex-direction: column; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; background: #fff; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
            <img src="/image/{rec['wp_slug']}.webp" alt="{rec['title']}" style="width: 100%; height: 180px; object-fit: cover; border-bottom: 1px solid #e2e8f0;" />
            <div style="padding: 1rem;">
                <h3 style="font-size: 1.1rem; font-weight: 600; margin: 0; color: #1a202c; line-height: 1.4;">{rec['title']}</h3>
            </div>
        </a>
        """
    recs_html += "</div></div>"
    ssr_container.append(BeautifulSoup(recs_html, "html.parser"))

    # Add Inline Base Styles for SSR Content
    style_tag = soup.new_tag("style")
    style_tag.string = """
        .ssr-content img { max-width: 100%; height: auto; border-radius: 8px; margin: 15px 0; }
        .ssr-content iframe { max-width: 100%; }
        .ssr-content a { color: #3182ce; text-decoration: none; }
        .ssr-content p { margin-bottom: 1.5em; }
    """
    soup.head.append(style_tag)

    # --- INJECT INTO DOM ---
    # Smart injection: Overwrite "#root" or "#app" if exists (bypassing client-side execution) 
    # Or append to the body as fallback.
    target_div = soup.find(id="root") or soup.find(id="app")
    if target_div:
        target_div.clear()
        target_div.append(ssr_container)
    else:
        soup.body.insert(0, ssr_container)

    final_html = str(soup)

    # Cache the final fully rendered HTML before returning
    update_html_cache(slug, final_html)

    return HTMLResponse(content=final_html, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

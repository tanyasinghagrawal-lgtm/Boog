import asyncio
import logging
import random
import json
import io
import time
import sys
import string
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import asyncpg
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from PIL import Image
from passlib.context import CryptContext

# Import the new template renderer module
from indextemp import render_template

# --- CONFIGURATION (Hardcoded as requested) ---
DB_DSN = "postgres://avnadmin:AVNS_d9GncXE-Fge9t5p3XlY@pg-7cbbad8-tanyasinghagrawal-62c1.j.aivencloud.com:26734/defaultdb?sslmode=require"
WP_SITE_URL = "pranavcea.wordpress.com"
HOME_PAGE_URL = "https://www.pranavblog.online/home"
APP_DOMAIN = "https://blog.pranavblog.online"

# --- GLOBAL VARIABLES & CACHE ---
URL_CACHE: Dict[str, str] = {}  # short_slug -> original_url

# HTML Cache: slug -> { html: str, access_count: int, last_accessed: float, size: int }
HTML_CACHE: Dict[str, dict] = {}
CACHE_MAX_SIZE_BYTES = 300 * 1024 * 1024  # 300 MB
CURRENT_CACHE_SIZE = 0

# Logger setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Password Hashing Setup
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Global DB Pool
DB_POOL = None

# --- DATA MODELS ---
class UserSignup(BaseModel):
    username: str
    password: str

class AddPost(BaseModel):
    slug: str
    original_url: str

# --- DATABASE FUNCTIONS ---
async def init_db():
    """Creates necessary tables if they don't exist and initializes the connection pool."""
    global DB_POOL
    try:
        DB_POOL = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=10)
        async with DB_POOL.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS url_mappings (
                    id SERIAL PRIMARY KEY,
                    short_slug TEXT UNIQUE NOT NULL,
                    original_url TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS articles (
                    slug TEXT PRIMARY KEY,
                    title TEXT,
                    content TEXT,
                    featured_image_url TEXT
                );
                CREATE TABLE IF NOT EXISTS images (
                    url TEXT PRIMARY KEY,
                    image_data BYTEA
                );
            """)
        logger.info("Database tables verified.")
    except Exception as e:
        logger.error(f"DB Init Error: {e}")

async def fetch_all_mappings():
    """Fetches all URL mappings from DB into memory."""
    try:
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch("SELECT short_slug, original_url FROM url_mappings")
            
            new_cache = {}
            for row in rows:
                slug = row['short_slug'].strip('/')
                new_cache[slug] = row['original_url']
            
            global URL_CACHE
            URL_CACHE = new_cache
            logger.info(f"URL Cache updated. Total mappings: {len(URL_CACHE)}")
    except Exception as e:
        logger.error(f"DB Mapping Fetch Error: {e}")

# --- CACHE MANAGEMENT ---
def enforce_cache_limit():
    """Removes least accessed & oldest accessed HTML from cache to keep it under 300MB limit."""
    global CURRENT_CACHE_SIZE, HTML_CACHE
    if CURRENT_CACHE_SIZE > CACHE_MAX_SIZE_BYTES:
        logger.warning(f"Cache size exceeded 300MB (Current: {CURRENT_CACHE_SIZE / (1024*1024):.2f} MB). Evicting old items...")
        
        # Sort items: Least accessed first, then oldest accessed
        sorted_items = sorted(HTML_CACHE.items(), key=lambda x: (x[1]['access_count'], x[1]['last_accessed']))
        
        for slug, data in sorted_items:
            if CURRENT_CACHE_SIZE <= CACHE_MAX_SIZE_BYTES * 0.9:  # Evict until 90% full to prevent thrashing
                break
            CURRENT_CACHE_SIZE -= data['size']
            del HTML_CACHE[slug]
            logger.info(f"Evicted /{slug} from cache.")
        
        logger.info(f"Cache size after eviction: {CURRENT_CACHE_SIZE / (1024*1024):.2f} MB.")

def add_to_html_cache(slug: str, html_content: str):
    """Adds HTML to cache and updates cache size."""
    global CURRENT_CACHE_SIZE, HTML_CACHE
    
    html_bytes_size = len(html_content.encode('utf-8'))
    
    if slug in HTML_CACHE:
        CURRENT_CACHE_SIZE -= HTML_CACHE[slug]['size']
    
    HTML_CACHE[slug] = {
        "html": html_content,
        "access_count": 1,
        "last_accessed": time.time(),
        "size": html_bytes_size
    }
    CURRENT_CACHE_SIZE += html_bytes_size
    
    enforce_cache_limit()

# --- IMAGE & CONTENT PROCESSING ---
def process_image_to_webp(image_bytes: bytes) -> bytes:
    """Resizes and compresses image to WEBP target 10-30KB while keeping aspect ratio."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")
            
        max_width = 800
        if img.width > max_width:
            ratio = max_width / float(img.width)
            new_height = int(float(img.height) * ratio)
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
        
        output = io.BytesIO()
        quality = 85
        
        img.save(output, format="WEBP", quality=quality, method=4)
        
        # Try to compress below 30KB
        target_size = 30 * 1024
        while output.tell() > target_size and quality > 10:
            output.seek(0)
            output.truncate(0)
            quality -= 5
            
            if quality < 30: # If quality is too low, resize it smaller
                img = img.resize((int(img.width * 0.8), int(img.height * 0.8)), Image.Resampling.LANCZOS)
                
            img.save(output, format="WEBP", quality=quality, method=4)
            
        logger.info(f"Image compressed to {output.tell() / 1024:.2f} KB (WebP)")
        return output.getvalue()
    except Exception as e:
        logger.error(f"Image compression error: {e}")
        return image_bytes

async def fetch_and_save_image(image_url: str) -> bool:
    """Fetches image from URL, compresses to WebP, and saves to DB."""
    if not image_url: return False
    
    try:
        async with DB_POOL.acquire() as conn:
            exists = await conn.fetchval("SELECT url FROM images WHERE url = $1", image_url)
            if exists: return True
            
        async with httpx.AsyncClient() as client:
            resp = await client.get(image_url, timeout=10.0)
            if resp.status_code == 200:
                compressed_bytes = process_image_to_webp(resp.content)
                async with DB_POOL.acquire() as conn:
                    await conn.execute("INSERT INTO images (url, image_data) VALUES ($1, $2) ON CONFLICT DO NOTHING", 
                                       image_url, compressed_bytes)
                return True
    except Exception as e:
        logger.error(f"Error fetching/saving image {image_url}: {e}")
    return False

async def sync_single_article(slug: str, original_url: str):
    """Fetches WP post data, saves content and image to DB."""
    wp_slug = original_url.strip('/').split('/')[-1]
    api_url = f"https://public-api.wordpress.com/rest/v1.1/sites/{WP_SITE_URL}/posts/slug:{wp_slug}?fields=title,featured_image,content"
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(api_url, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                title = data.get('title', 'To The Point')
                content = data.get('content', '')
                featured_image = data.get('featured_image', '')

                # Fallback: Find image inside content
                if not featured_image and content:
                    soup_tmp = BeautifulSoup(content, 'html.parser')
                    img_tag = soup_tmp.find('img')
                    if img_tag and img_tag.get('src'):
                        featured_image = img_tag.get('src')
                
                # Save to DB
                async with DB_POOL.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO articles (slug, title, content, featured_image_url) 
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (slug) DO UPDATE SET title = EXCLUDED.title, content = EXCLUDED.content
                    """, slug, title, content, featured_image)
                
                # Download & Compress Image
                if featured_image:
                    await fetch_and_save_image(featured_image)
                    
                logger.info(f"Successfully synced article to DB: /{slug}")
                return True
    except Exception as e:
        logger.error(f"Error syncing article {slug}: {e}")
    return False

# --- BACKGROUND TASKS ---
async def startup_db_sync():
    """Runs on startup. Checks which mappings lack articles in DB, fetches and saves them."""
    logger.info("Starting background Database Sync...")
    try:
        async with DB_POOL.acquire() as conn:
            mappings = await conn.fetch("SELECT short_slug, original_url FROM url_mappings")
            
            for row in mappings:
                slug = row['short_slug'].strip('/')
                orig_url = row['original_url']
                
                # Check if article exists
                exists = await conn.fetchval("SELECT slug FROM articles WHERE slug = $1", slug)
                if not exists:
                    logger.info(f"Article missing in DB for /{slug}. Fetching...")
                    await sync_single_article(slug, orig_url)
                    
        logger.info("Startup DB Sync Complete.")
    except Exception as e:
        logger.error(f"Startup Sync Error: {e}")

async def auto_fetch_new_blogs_task():
    """Runs every 10 mins. Checks for new blogs on WP, generates slug, and saves completely to DB."""
    while True:
        await asyncio.sleep(600)  # 10 Minutes
        logger.info("Checking for new WordPress articles (10 min interval)...")
        try:
            api_url = f"https://public-api.wordpress.com/rest/v1.1/sites/{WP_SITE_URL}/posts?number=5&fields=slug,URL"
            async with httpx.AsyncClient() as client:
                resp = await client.get(api_url, timeout=10.0)
                if resp.status_code == 200:
                    posts = resp.json().get('posts', [])
                    
                    async with DB_POOL.acquire() as conn:
                        for post in posts:
                            wp_slug = post.get('slug')
                            orig_url = post.get('URL')
                            
                            # Check if original_url ends with this wp_slug in mappings
                            exists = await conn.fetchval(
                                "SELECT short_slug FROM url_mappings WHERE original_url LIKE $1", 
                                f"%{wp_slug}%"
                            )
                            
                            if not exists:
                                # New post found! Generate 5 char slug
                                new_slug = ''.join(random.choices(string.ascii_lowercase, k=5))
                                logger.info(f"New WP Post found: {wp_slug}. Generating slug: /{new_slug}")
                                
                                # Insert Mapping
                                await conn.execute(
                                    "INSERT INTO url_mappings (short_slug, original_url) VALUES ($1, $2)",
                                    new_slug, orig_url
                                )
                                URL_CACHE[new_slug] = orig_url
                                
                                # Sync the actual post data & compress image
                                await sync_single_article(new_slug, orig_url)
        except Exception as e:
            logger.error(f"Auto Fetch New Blogs Error: {e}")

# --- LIFESPAN MANAGER ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server Starting...")
    await init_db()
    await fetch_all_mappings()
    
    # Start background tasks
    task_startup_sync = asyncio.create_task(startup_db_sync())
    task_auto_fetch = asyncio.create_task(auto_fetch_new_blogs_task())
    
    yield
    
    # Cleanup
    task_startup_sync.cancel()
    task_auto_fetch.cancel()
    if DB_POOL:
        await DB_POOL.close()
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
    async with DB_POOL.acquire() as conn:
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
    async with DB_POOL.acquire() as conn:
        row = await conn.fetchrow("SELECT password_hash FROM users WHERE username = $1", user.username)
        if not row or not pwd_context.verify(user.password, row['password_hash']):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        return {"status": "success", "message": "Verified"}

@app.get("/allpost")
async def get_all_posts():
    return [{"slug": k, "original_url": v} for k, v in URL_CACHE.items()]

@app.post("/addpost")
async def add_post(post: AddPost):
    clean_slug = post.slug.strip('/')
    try:
        async with DB_POOL.acquire() as conn:
            exists = await conn.fetchval("SELECT id FROM url_mappings WHERE short_slug = $1", clean_slug)
            if exists:
                raise HTTPException(status_code=400, detail="Slug already exists.")
                
            await conn.execute("INSERT INTO url_mappings (short_slug, original_url) VALUES ($1, $2)", clean_slug, post.original_url)
            
        URL_CACHE[clean_slug] = post.original_url
        
        # Trigger background sync for this specific new post immediately
        asyncio.create_task(sync_single_article(clean_slug, post.original_url))
        
        return {"status": "success", "slug": clean_slug, "url": post.original_url}
    except Exception as e:
        logger.error(f"Add Post Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- PUBLIC ROUTES ---
@app.get("/")
async def root():
    return RedirectResponse(url=HOME_PAGE_URL)

@app.get("/robots.txt", response_class=Response)
async def get_robots_txt():
    """Generates robots.txt dynamically."""
    content = f"""User-agent: *
Allow: /

Sitemap: {APP_DOMAIN}/sitemap.xml
"""
    return Response(content=content, media_type="text/plain")

@app.get("/sitemap.xml", response_class=Response)
async def get_sitemap_xml():
    """Generates a dynamic XML sitemap based on all slugs in the database/cache."""
    xml_content = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml_content.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    
    # Add home page entry
    xml_content.append(f"""  <url>
    <loc>{APP_DOMAIN}/</loc>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>""")
    
    # Iterate through all the slugs mapping we have in the DB via URL_CACHE
    for slug in URL_CACHE.keys():
        loc_url = f"{APP_DOMAIN}/{slug}"
        xml_content.append(f"""  <url>
    <loc>{loc_url}</loc>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>""")
        
    xml_content.append('</urlset>')
    
    return Response(content="\n".join(xml_content), media_type="application/xml")

@app.get("/{slug}/image.webp")
async def serve_compressed_image(slug: str):
    """Serves the WebP compressed image directly from PostgreSQL."""
    try:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT i.image_data 
                FROM articles a 
                JOIN images i ON a.featured_image_url = i.url 
                WHERE a.slug = $1
            """, slug)
            
            if row and row['image_data']:
                return Response(content=row['image_data'], media_type="image/webp")
    except Exception as e:
        logger.error(f"Image Serve Error: {e}")
        
    return Response(status_code=404)

@app.get("/{slug}")
async def blog_viewer(slug: str):
    """
    1. Checks RAM Cache.
    2. If not found, fetches fully processed data from DB.
    3. Builds HTML completely using the indextemp module.
    4. Caches and returns it.
    """
    # 1. Check RAM Cache
    if slug in HTML_CACHE:
        logger.info(f"Serving /{slug} from RAM Cache.")
        HTML_CACHE[slug]['access_count'] += 1
        HTML_CACHE[slug]['last_accessed'] = time.time()
        return HTMLResponse(content=HTML_CACHE[slug]['html'], status_code=200)

    original_url = URL_CACHE.get(slug)
    if not original_url:
        async with DB_POOL.acquire() as conn:
            row = await conn.fetchrow("SELECT original_url FROM url_mappings WHERE short_slug = $1", slug)
            if row:
                original_url = row['original_url']
                URL_CACHE[slug] = original_url
            else:
                return RedirectResponse(url=HOME_PAGE_URL)

    # 2. Fetch Article from DB
    async with DB_POOL.acquire() as conn:
        article = await conn.fetchrow("SELECT title, content, featured_image_url FROM articles WHERE slug = $1", slug)
        
        if not article:
            # If not in DB yet (maybe new), sync it synchronously first to serve the user
            logger.info(f"Article /{slug} not found in DB. Doing immediate sync...")
            success = await sync_single_article(slug, original_url)
            if success:
                article = await conn.fetchrow("SELECT title, content, featured_image_url FROM articles WHERE slug = $1", slug)
            
        if not article:
             return HTMLResponse("<h1>Error processing blog data. Please try again.</h1>", status_code=500)

    title = article['title']
    content = article['content']
    featured_image_url = article['featured_image_url']
    local_image_path = f"{APP_DOMAIN}/{slug}/image.webp" if featured_image_url else ""

    # Generate Recommendations (Get 4 random slugs from Cache that exist in DB)
    all_slugs = list(URL_CACHE.keys())
    if slug in all_slugs: all_slugs.remove(slug)
    
    random_recs = random.sample(all_slugs, min(4, len(all_slugs)))
    recs_data = []
    
    async with DB_POOL.acquire() as conn:
        for r_slug in random_recs:
            r_art = await conn.fetchrow("SELECT title, featured_image_url FROM articles WHERE slug = $1", r_slug)
            if r_art:
                recs_data.append({
                    "slug": r_slug,
                    "title": r_art['title'],
                    "image": f"/{r_slug}/image.webp" if r_art['featured_image_url'] else ""
                })

    # 3. Server-Side HTML Rendering via indextemp.py
    final_html = render_template(title, content, local_image_path, recs_data)
    
    if final_html.startswith("<h1>Error"):
        return HTMLResponse(content=final_html, status_code=500)

    # 4. Save to Cache & Return
    add_to_html_cache(slug, final_html)
    logger.info(f"Page generated, optimized, and added to cache: /{slug}")
    
    return HTMLResponse(content=final_html, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

import asyncio
import logging
import random
import json
import io
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
WP_META_CACHE: Dict[str, dict] = {}

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
        # Create mappings table (assuming it might strictly need setup)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS url_mappings (
                id SERIAL PRIMARY KEY,
                short_slug TEXT UNIQUE NOT NULL,
                original_url TEXT NOT NULL
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
        logger.info(f"Cache updated. Total links: {len(URL_CACHE)}")
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

# --- IMAGE OPTIMIZATION ---

def process_image(image_bytes: bytes) -> bytes:
    """Resizes and compresses image to be < 50KB while keeping aspect ratio."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        
        # Convert to RGB (in case of PNG with transparency) for JPEG conversion
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        # 1. Resize logic: Max width 800px (keeps aspect ratio)
        max_width = 800
        if img.width > max_width:
            ratio = max_width / float(img.width)
            new_height = int((float(img.height) * float(ratio)))
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
        
        # 2. Compress loop to target < 50KB
        output = io.BytesIO()
        quality = 85
        
        # First attempt
        img.save(output, format="JPEG", quality=quality, optimize=True)
        
        # If larger than 50KB (51200 bytes), lower quality and/or resize
        while output.tell() > 50 * 1024 and quality > 20:
            output.seek(0)
            output.truncate(0)
            quality -= 10
            # If quality gets too low, resize further
            if quality < 50:
                img = img.resize((int(img.width * 0.8), int(img.height * 0.8)), Image.Resampling.LANCZOS)
            
            img.save(output, format="JPEG", quality=quality, optimize=True)
            
        return output.getvalue()
    except Exception as e:
        logger.error(f"Image processing error: {e}")
        return image_bytes # Return original if processing fails

# --- BACKGROUND TASKS ---

async def background_cache_updater():
    """Updates cache every 3 minutes."""
    while True:
        await asyncio.sleep(180) # 3 Minutes
        logger.info("Running background cache update...")
        await fetch_all_mappings()

# --- LIFESPAN MANAGER ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server Starting... Initializing DB and Cache.")
    await init_db()
    await fetch_all_mappings()
    task = asyncio.create_task(background_cache_updater())
    yield
    task.cancel()
    logger.info("Server Shutting Down.")

# --- FASTAPI APP ---

app = FastAPI(lifespan=lifespan)

# --- CORS MIDDLEWARE (Allow All Origins) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allow any site to access
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- HELPER: FETCH WP METADATA ---

async def get_wp_metadata(wp_slug: str):
    if wp_slug in WP_META_CACHE:
        return WP_META_CACHE[wp_slug]

    api_url = f"https://public-api.wordpress.com/rest/v1.1/sites/{WP_SITE_URL}/posts/slug:{wp_slug}?fields=title,featured_image,content"
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(api_url, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                title = data.get('title', 'To The Point')
                featured_image = data.get('featured_image')
                content = data.get('content', '')

                image_url = featured_image
                if not image_url and content:
                    soup = BeautifulSoup(content, 'html.parser')
                    img_tag = soup.find('img')
                    if img_tag and img_tag.get('src'):
                        image_url = img_tag.get('src')
                
                if not image_url:
                    image_url = "" 

                meta_data = {"title": title, "image": image_url}
                WP_META_CACHE[wp_slug] = meta_data
                return meta_data
        except Exception as e:
            logger.error(f"WP API Error for {wp_slug}: {e}")
    
    return {"title": "To The Point", "image": ""}

# --- AUTH & ADMIN ROUTES ---

@app.post("/signup")
async def signup(user: UserSignup):
    """
    Registers a new user. 
    Restricts registration if 3 or more users already exist.
    """
    conn = await asyncpg.connect(DB_DSN)
    try:
        # Check user count
        count_val = await conn.fetchval("SELECT COUNT(*) FROM users")
        if count_val >= 3:
            raise HTTPException(status_code=403, detail="Signup limit reached (Max 3 users).")
        
        # Check if username exists
        exists = await conn.fetchval("SELECT id FROM users WHERE username = $1", user.username)
        if exists:
            raise HTTPException(status_code=400, detail="Username already taken.")

        # Hash password and save
        hashed_password = pwd_context.hash(user.password)
        await conn.execute("INSERT INTO users (username, password_hash) VALUES ($1, $2)", user.username, hashed_password)
        
        return {"status": "success", "message": "User created successfully."}
    finally:
        await conn.close()

@app.post("/verify")
async def verify_user(user: UserSignup):
    """
    Verifies username and password against DB.
    """
    conn = await asyncpg.connect(DB_DSN)
    try:
        row = await conn.fetchrow("SELECT password_hash FROM users WHERE username = $1", user.username)
        if not row:
            raise HTTPException(status_code=401, detail="Invalid username or password")
        
        if not pwd_context.verify(user.password, row['password_hash']):
            raise HTTPException(status_code=401, detail="Invalid username or password")
            
        return {"status": "success", "message": "Verified"}
    finally:
        await conn.close()

@app.get("/allpost")
async def get_all_posts():
    """
    Returns all slugs and original URLs.
    """
    # Prefer returning from cache for speed, but ensure it's up to date list
    return [{"slug": k, "original_url": v} for k, v in URL_CACHE.items()]

@app.post("/addpost")
async def add_post(post: AddPost):
    """
    Adds a new post mapping to the DB and updates cache.
    """
    clean_slug = post.slug.strip('/')
    
    conn = await asyncpg.connect(DB_DSN)
    try:
        # Check if slug exists
        exists = await conn.fetchval("SELECT id FROM url_mappings WHERE short_slug = $1", clean_slug)
        if exists:
            raise HTTPException(status_code=400, detail="Slug already exists.")
            
        await conn.execute(
            "INSERT INTO url_mappings (short_slug, original_url) VALUES ($1, $2)",
            clean_slug, post.original_url
        )
        
        # Update local cache immediately
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
async def dynamic_og_image(slug: str):
    """
    Fetches WP image, resizes/compresses to <50KB, returns as image/jpeg.
    Note: Extension kept as .png in URL for compatibility, but serving JPEG for size.
    """
    original_url = URL_CACHE.get(slug)
    if not original_url:
        original_url = await resolve_slug_fallback(slug)
    
    if not original_url:
        return Response(status_code=404)

    wp_slug = original_url.strip('/').split('/')[-1]
    meta = await get_wp_metadata(wp_slug)
    image_url = meta.get('image')

    if not image_url:
        # Fallback 1x1 pixel or similar could go here, or 404
        return Response(status_code=404)

    async with httpx.AsyncClient() as client:
        try:
            req = client.build_request("GET", image_url)
            r = await client.send(req) # Download full image
            
            if r.status_code == 200:
                # Process Image (Resize & Compress)
                processed_image_bytes = process_image(r.content)
                
                return Response(content=processed_image_bytes, media_type="image/jpeg")
            else:
                return Response(status_code=404)
        except Exception as e:
            logger.error(f"Image fetch error: {e}")
            return Response(status_code=404)

@app.get("/{slug}")
async def blog_viewer(slug: str):
    # 1. Check Cache
    original_url = URL_CACHE.get(slug)
    if not original_url:
        original_url = await resolve_slug_fallback(slug)
    
    if not original_url:
        return RedirectResponse(url=HOME_PAGE_URL)

    # 2. Prepare Data
    wp_slug = original_url.strip('/').split('/')[-1]
    meta = await get_wp_metadata(wp_slug)
    
    # Recommendations
    all_slugs = list(URL_CACHE.keys())
    if slug in all_slugs:
        all_slugs.remove(slug)
    random_recs = random.sample(all_slugs, min(4, len(all_slugs)))
    
    rec_data = []
    for r_slug in random_recs:
        r_orig = URL_CACHE[r_slug]
        r_wp_slug = r_orig.strip('/').split('/')[-1]
        rec_data.append({
            "link": f"/{r_slug}",
            "wp_slug": r_wp_slug
        })

    # 3. Read HTML
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html_content = f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>Error: index.html not found</h1>", status_code=500)

    # 4. Inject Meta Tags
    # Update OG Image to point to the .png endpoint which is now compressed
    og_image_link = f"{APP_DOMAIN}/{slug}.png"
    
    html_content = html_content.replace(
        '<title>To The Point - Environment Energy and Agriculture</title>',
        f'<title>{meta["title"]} - To The Point</title>'
    )
    html_content = html_content.replace(
        'content="To The Point - Environment Energy and Agriculture"',
        f'content="{meta["title"]}"'
    )
    html_content = html_content.replace(
        'name="description" content="Latest insights and articles on Environment, Energy, and Agriculture."',
        'name="description" content=""'
    )
    html_content = html_content.replace(
        'property="og:description" content="Latest insights and articles on Environment, Energy, and Agriculture. Click and read more."',
        'property="og:description" content=""'
    )
    
    # Target the empty og:image tag specifically
    html_content = html_content.replace(
        'property="og:image" content=""', 
        f'property="og:image" content="{og_image_link}"'
    )

    # 5. Inject Data Script
    server_data_json = json.dumps({
        "wp_slug": wp_slug,
        "recommendations": rec_data
    })

    script_injection = f"""
    <script>
        window.SERVER_DATA = {server_data_json};
    </script>
    """
    
    if "</head>" in html_content:
        html_content = html_content.replace("</head>", f"{script_injection}</head>")
    else:
        html_content = script_injection + html_content

    return HTMLResponse(content=html_content, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

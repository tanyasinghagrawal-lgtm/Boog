import asyncio
import logging
import random
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import asyncpg
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# --- CONFIGURATION (Hardcoded as requested) ---
DB_DSN = "postgres://avnadmin:AVNS_d9GncXE-Fge9t5p3XlY@pg-7cbbad8-tanyasinghagrawal-62c1.j.aivencloud.com:26734/defaultdb?sslmode=require"
WP_SITE_URL = "pranavcea.wordpress.com"
HOME_PAGE_URL = "https://www.pranavblogs.online/home"
APP_DOMAIN = "https://blog.pranavblogs.online"

# --- GLOBAL IN-MEMORY CACHE ---
# Structure: { "short_slug": "original_wp_url" }
URL_CACHE: Dict[str, str] = {}

# Secondary Cache for WP Metadata to avoid hitting WP API repeatedly for titles/images
# Structure: { "wp_slug": { "title": "...", "image": "...", "excerpt": "..." } }
WP_META_CACHE: Dict[str, dict] = {}

# Logger setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- DATABASE FUNCTIONS ---

async def fetch_all_mappings():
    """Fetches all URL mappings from Aiven DB."""
    try:
        conn = await asyncpg.connect(DB_DSN)
        rows = await conn.fetch("SELECT short_slug, original_url FROM url_mappings")
        await conn.close()
        
        new_cache = {}
        for row in rows:
            # Strip slashes just in case
            slug = row['short_slug'].strip('/')
            new_cache[slug] = row['original_url']
        
        global URL_CACHE
        URL_CACHE = new_cache
        logger.info(f"Cache updated. Total links: {len(URL_CACHE)}")
    except Exception as e:
        logger.error(f"DB Error: {e}")

async def resolve_slug_fallback(slug: str) -> Optional[str]:
    """
    Fallback: If slug is not in RAM, check DB directly (for very new links).
    Returns original_url or None.
    """
    try:
        conn = await asyncpg.connect(DB_DSN)
        row = await conn.fetchrow("SELECT original_url FROM url_mappings WHERE short_slug = $1", slug)
        await conn.close()
        if row:
            # Add to cache immediately
            URL_CACHE[slug] = row['original_url']
            return row['original_url']
    except Exception as e:
        logger.error(f"DB Fallback Error: {e}")
    return None

# --- BACKGROUND TASKS ---

async def background_cache_updater():
    """Updates cache every 3 minutes."""
    while True:
        await asyncio.sleep(180) # 3 Minutes
        logger.info("Running background cache update...")
        await fetch_all_mappings()

# --- LIFESPAN MANAGER (Startup/Shutdown) ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Load Cache immediately
    logger.info("Server Starting... Fetching initial data.")
    await fetch_all_mappings()
    
    # Start background task
    asyncio.create_task(background_cache_updater())
    
    yield
    # Shutdown logic (if any)
    logger.info("Server Shutting Down.")

# --- FASTAPI APP ---

app = FastAPI(lifespan=lifespan)

# --- HELPER: FETCH WP METADATA ---

async def get_wp_metadata(wp_slug: str):
    """
    Fetches Title and Image URL from WordPress. 
    Uses internal cache to be super fast.
    """
    if wp_slug in WP_META_CACHE:
        return WP_META_CACHE[wp_slug]

    api_url = f"https://public-api.wordpress.com/rest/v1.1/sites/{WP_SITE_URL}/posts/slug:{wp_slug}?fields=title,featured_image,content"
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(api_url, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                title = data.get('title', 'Blog Post')
                featured_image = data.get('featured_image')
                content = data.get('content', '')

                # If no featured image, find first img in content
                image_url = featured_image
                if not image_url and content:
                    soup = BeautifulSoup(content, 'html.parser')
                    img_tag = soup.find('img')
                    if img_tag and img_tag.get('src'):
                        image_url = img_tag.get('src')
                
                # Fallback image if nothing found
                if not image_url:
                    image_url = f"{APP_DOMAIN}/static/default-leaf.png" # You can add a static fallback later

                meta_data = {
                    "title": title,
                    "image": image_url
                }
                
                # Cache it (Simple cache, in production maybe use LRU)
                WP_META_CACHE[wp_slug] = meta_data
                return meta_data
        except Exception as e:
            logger.error(f"WP API Error for {wp_slug}: {e}")
    
    return {"title": "To The Point - Environment Energy", "image": ""}

# --- ROUTES ---

@app.get("/")
async def root():
    return RedirectResponse(url=HOME_PAGE_URL)

@app.get("/{slug}.png")
async def dynamic_og_image(slug: str):
    """
    Serves the blog's main image as a local PNG proxy.
    Example: blog.pranavblogs.online/xyz.png
    """
    # 1. Identify Blog
    original_url = URL_CACHE.get(slug)
    if not original_url:
        original_url = await resolve_slug_fallback(slug)
    
    if not original_url:
        # Return a 1x1 transparent pixel or default image if 404
        return Response(status_code=404)

    # 2. Get WP Metadata
    wp_slug = original_url.strip('/').split('/')[-1]
    meta = await get_wp_metadata(wp_slug)
    image_url = meta.get('image')

    if not image_url:
        return Response(status_code=404)

    # 3. Stream the Image (Proxy)
    # We fetch the image from WP and stream it to the user so it looks like it's coming from us
    async with httpx.AsyncClient() as client:
        try:
            req = client.build_request("GET", image_url)
            r = await client.send(req, stream=True)
            return StreamingResponse(
                r.aiter_bytes(), 
                media_type=r.headers.get("content-type", "image/png")
            )
        except Exception:
            return Response(status_code=404)

@app.get("/{slug}")
async def blog_viewer(slug: str):
    """
    Main Blog Viewer Endpoint.
    Server-Side Renders (SSR) the Meta tags, then serves HTML.
    """
    # 1. Check Cache
    original_url = URL_CACHE.get(slug)
    
    # 2. Fallback to DB if not in cache
    if not original_url:
        original_url = await resolve_slug_fallback(slug)
    
    # 3. If still not found -> Redirect Home
    if not original_url:
        return RedirectResponse(url=HOME_PAGE_URL)

    # 4. Extract WP Slug
    wp_slug = original_url.strip('/').split('/')[-1]

    # 5. Fetch Metadata (Title & Image) for SEO
    meta = await get_wp_metadata(wp_slug)
    
    # 6. Generate Recommendations (4 Random links from Cache)
    # Filter out current slug, pick 4
    all_slugs = list(URL_CACHE.keys())
    if slug in all_slugs:
        all_slugs.remove(slug)
    
    random_recs = random.sample(all_slugs, min(4, len(all_slugs)))
    
    # Prepare recommendation data for Frontend (to avoid frontend DB calls)
    rec_data = []
    for r_slug in random_recs:
        r_orig = URL_CACHE[r_slug]
        r_wp_slug = r_orig.strip('/').split('/')[-1]
        rec_data.append({
            "link": f"/{r_slug}",
            "wp_slug": r_wp_slug
        })

    # 7. Read HTML Template
    # We assume index.html is in the same directory. 
    # NOTE: You will provide the HTML later, I will assume a standard read here.
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html_content = f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>Error: index.html not found on server</h1>", status_code=500)

    # 8. Server-Side Injection (Replacing placeholders)
    # We are modifying the meta tags dynamically before sending to user.
    
    og_image_link = f"{APP_DOMAIN}/{slug}.png"
    
    # Update Title
    html_content = html_content.replace(
        '<title>To The Point - Environment Energy and Agriculture</title>',
        f'<title>{meta["title"]} - To The Point</title>'
    )
    html_content = html_content.replace(
        'content="To The Point - Environment Energy and Agriculture"',
        f'content="{meta["title"]}"'
    )
    
    # Remove Description content (as requested)
    html_content = html_content.replace(
        'name="description" content="Latest insights and articles on Environment, Energy, and Agriculture."',
        'name="description" content=""'
    )
    html_content = html_content.replace(
        'property="og:description" content="Latest insights and articles on Environment, Energy, and Agriculture. Click and read more."',
        'property="og:description" content=""'
    )

    # Update Image
    html_content = html_content.replace(
        'content=""',  # Targeted replacement for the empty og:image tag in your HTML
        f'content="{og_image_link}"'
    )
    
    # Inject Data for Client-Side Script (To avoid 2nd DB call from frontend)
    # We inject a script tag with variables that your frontend JS can read
    script_injection = f"""
    <script>
        window.SERVER_DATA = {{
            "wp_slug": "{wp_slug}",
            "recommendations": {str(rec_data).replace("'", '"')} 
        }};
    </script>
    """
    
    # Insert script before closing body
    html_content = html_content.replace('</body>', f'{script_injection}</body>')

    return HTMLResponse(content=html_content, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

import asyncio
import logging
import random
import json
import ssl
from contextlib import asynccontextmanager
from typing import Dict, List, Optional
from urllib.parse import urlparse

import asyncpg
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

# --- CONFIGURATION ---
# Aiven DB String
AIVEN_DB_URL = "postgres://avnadmin:AVNS_d9GncXE-Fge9t5p3XlY@pg-7cbbad8-tanyasinghagrawal-62c1.j.aivencloud.com:26734/defaultdb?sslmode=require"

WP_SITE_URL = "pranavcea.wordpress.com"
HOME_PAGE_URL = "https://www.pranavblog.online/home"
APP_DOMAIN = "https://blog.pranavblog.online"

# --- GLOBAL IN-MEMORY CACHE ---
URL_CACHE: Dict[str, str] = {}
WP_META_CACHE: Dict[str, dict] = {}

# Logger setup - Enhanced for Debugging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ProxyServer")

# --- DATABASE FUNCTIONS ---

async def fetch_all_mappings():
    """Fetches all URL mappings from Aiven DB."""
    try:
        conn = await asyncpg.connect(AIVEN_DB_URL)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS url_mappings (
                id SERIAL PRIMARY KEY,
                original_url TEXT UNIQUE NOT NULL,
                short_slug TEXT UNIQUE NOT NULL,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
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
        conn = await asyncpg.connect(AIVEN_DB_URL)
        row = await conn.fetchrow("SELECT original_url FROM url_mappings WHERE short_slug = $1", slug)
        await conn.close()
        if row:
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

# --- LIFESPAN MANAGER ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server Starting... Fetching initial data.")
    try:
        await fetch_all_mappings()
    except Exception as e:
        logger.error(f"Initial DB Connection failed: {e}")
        
    asyncio.create_task(background_cache_updater())
    yield
    logger.info("Server Shutting Down.")

# --- FASTAPI APP ---

app = FastAPI(lifespan=lifespan)

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

# --- OPTIMIZED PROXY LOGIC (NEON -> WEBSOCKET -> AIVEN) ---

@app.websocket("/{path:path}")
async def db_proxy(websocket: WebSocket, path: str):
    """
    Optimized WebSocket Proxy for Neon Serverless Driver.
    Directly pipes binary data to Aiven with robust error handling.
    """
    # 1. Parse Aiven Details
    db_url = urlparse(AIVEN_DB_URL)
    db_host = db_url.hostname
    db_port = db_url.port or 5432

    # 2. Handle Subprotocols (CRITICAL for Neon Driver)
    protocols = websocket.headers.get("sec-websocket-protocol")
    selected_protocol = protocols.split(',')[0].strip() if protocols else None
    
    logger.info(f"New WS Connection. Path: {path}, Protocol: {selected_protocol}")
    await websocket.accept(subprotocol=selected_protocol)

    writer = None
    try:
        # 3. Create SSL Context for Aiven
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        
        logger.info(f"Connecting to Aiven DB at {db_host}:{db_port}...")
        
        # 4. Open TCP Connection to Aiven
        # We increase the limit to prevent buffer issues
        reader, writer = await asyncio.open_connection(
            db_host, db_port, ssl=ssl_ctx, limit=1024*1024
        )
        logger.info("Connected to Aiven DB successfully.")

        # 5. Define Pipe Functions
        async def ws_to_tcp():
            """Reads from Browser (WebSocket) -> Writes to Aiven (TCP)"""
            try:
                while True:
                    data = await websocket.receive_bytes()
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except WebSocketDisconnect:
                logger.info("Client disconnected WS.")
            except Exception as e:
                logger.error(f"WS->TCP Error: {e}")

        async def tcp_to_ws():
            """Reads from Aiven (TCP) -> Writes to Browser (WebSocket)"""
            try:
                while True:
                    # Read larger chunks for performance
                    data = await reader.read(65536) 
                    if not data:
                        logger.info("Aiven closed connection.")
                        break
                    await websocket.send_bytes(data)
            except Exception as e:
                logger.error(f"TCP->WS Error: {e}")

        # 6. Run pipes concurrently and wait for EITHER to finish
        # This prevents one side hanging if the other closes
        done, pending = await asyncio.wait(
            [asyncio.create_task(ws_to_tcp()), asyncio.create_task(tcp_to_ws())],
            return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel pending tasks to free resources
        for task in pending:
            task.cancel()

    except Exception as e:
        logger.error(f"Proxy Setup Error: {e}")
        try:
            await websocket.close(code=1011)
        except:
            pass
    finally:
        logger.info("Cleaning up connection...")
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        try:
            await websocket.close()
        except:
            pass

# --- STANDARD ROUTES ---

@app.get("/")
async def root():
    return RedirectResponse(url=HOME_PAGE_URL)

@app.get("/{slug}.png")
async def dynamic_og_image(slug: str):
    original_url = URL_CACHE.get(slug)
    if not original_url:
        original_url = await resolve_slug_fallback(slug)
    
    if not original_url:
        return Response(status_code=404)

    wp_slug = original_url.strip('/').split('/')[-1]
    meta = await get_wp_metadata(wp_slug)
    image_url = meta.get('image')

    if not image_url:
        return Response(status_code=404)

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
    # Check if slug exists
    original_url = URL_CACHE.get(slug)
    if not original_url:
        original_url = await resolve_slug_fallback(slug)
    
    if not original_url:
        return RedirectResponse(url=HOME_PAGE_URL)

    # Prepare Data
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

    # Read HTML
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html_content = f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>Error: index.html not found</h1>", status_code=500)

    # Inject Meta Tags
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
    html_content = html_content.replace(
        'property="og:image" content=""', 
        f'property="og:image" content="{og_image_link}"'
    )

    # Inject Data Script
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

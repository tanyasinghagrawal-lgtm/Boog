from bs4 import BeautifulSoup

def render_template(title: str, content: str, local_image_path: str, recs_data: list) -> str:
    """
    Reads the index.html template and injects blog data, SEO tags, 
    and performance optimizations using BeautifulSoup.
    """
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html_content = f.read()
    except FileNotFoundError:
        return "<h1>Error: index.html not found on server</h1>"

    soup = BeautifulSoup(html_content, "html.parser")

    # ==========================================
    # --- PERFORMANCE & SEO OPTIMIZATIONS ---
    # ==========================================
    
    # 1. Inject Preconnects and Preloads into <head> for faster font loading
    # ==========================================
    # --- PERFORMANCE, SEO & FOUC FIXES ---
    # ==========================================
    
    head_tag = soup.head
    if head_tag:
        # 1. Google Fonts Render-Blocking Fix
        # Remove slow @import from style and inject fast Async <link> tags
        css_import_str = "@import url('https://fonts.googleapis.com/css2?family=Quicksand:wght@300;400;500;600;700&display=swap');"
        for style_tag in soup.find_all("style"):
            if style_tag.string and css_import_str in style_tag.string:
                style_tag.string = style_tag.string.replace(css_import_str, "")
                
                # Add async Google Fonts links
                gf_link = soup.new_tag("link", rel="stylesheet", href="https://fonts.googleapis.com/css2?family=Quicksand:wght@300;400;500;600;700&display=swap", media="print", onload="this.media='all'")
                head_tag.insert(0, gf_link)
                head_tag.insert(0, soup.new_tag("link", rel="preconnect", href="https://fonts.gstatic.com", crossorigin="anonymous"))
                head_tag.insert(0, soup.new_tag("link", rel="preconnect", href="https://fonts.googleapis.com"))
                break

        # 2. FontAwesome Render-Blocking Fix
        # Make it async using media="print" trick
        fa_link = soup.find("link", href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css")
        if fa_link:
            fa_link["media"] = "print"
            fa_link["onload"] = "this.media='all'"
            # Preload the actual font file to avoid delay when icons show
            preload_fa = soup.new_tag("link", rel="preload", href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-solid-900.woff2", **{"as": "font", "type": "font/woff2", "crossorigin": "anonymous"})
            head_tag.insert(0, preload_fa)

        # 3. FOUC Fix for Tailwind CDN (Ajeeb formatting hatane ke liye)
        # Page ko initial load par hide rakhenge (taaki tuta hua text na dikhe)
        fouc_style = soup.new_tag("style")
        fouc_style.string = "html { visibility: hidden; opacity: 0; transition: opacity 0.3s ease; }"
        head_tag.append(fouc_style)
        
        # Jaise hi DOM load hoga aur Tailwind apna kaam karega, hum smooth fade-in se page dikha denge
        fouc_script = soup.new_tag("script")
        fouc_script.string = "window.addEventListener('DOMContentLoaded', () => { setTimeout(() => { document.documentElement.style.visibility = 'visible'; document.documentElement.style.opacity = '1'; }, 50); });"
        head_tag.append(fouc_script)
        
        # Ensure Tailwind runs synchronously to paint the layout faster
        tw_script = soup.find("script", src="https://cdn.tailwindcss.com")
        if tw_script and "defer" in tw_script.attrs:
            del tw_script["defer"]

    # 2. Update Meta Tags
    # 2. Update Meta Tags
    if soup.title:
        soup.title.string = f"{title} - To The Point"
    
    for meta in soup.find_all("meta"):
        if meta.get("property") == "og:title":
            meta["content"] = title
        elif meta.get("name") == "description" or meta.get("property") == "og:description":
            meta["content"] = f"Read our latest insights on: {title}. Environment, Energy, and Agriculture."
        elif meta.get("property") == "og:image":
            meta["content"] = local_image_path

    # Hide Loader & Show Container
    loader = soup.find(id="loader")
    if loader: loader["class"] = loader.get("class", []) + ["hidden"]
    
    blog_container = soup.find(id="blogContainer")
    if blog_container and "hidden" in blog_container.get("class", []):
        blog_container["class"].remove("hidden")

    # Inject Title
    title_tag = soup.find(id="blogTitle")
    if title_tag: title_tag.string = title

    # 3. Inject Featured Image & Add SEO `alt` tags
    if local_image_path:
        img_container = soup.find(id="featuredImageContainer")
        if img_container and "hidden" in img_container.get("class", []):
            img_container["class"].remove("hidden")
            
        img_tag = soup.find(id="featuredImage")
        if img_tag: 
            img_tag["src"] = local_image_path
            # Setting dynamic ALT data
            img_tag["alt"] = f"Blog title image of {title}"
            img_tag["loading"] = "eager"  # LCP element should load eager

    # 4. Inject Content & Update Content Images with `alt` tags
    # 4. Inject Content & Update Content Images with `alt` tags (SAFE METHOD)
    body_tag = soup.find(id="blogBody")
    if body_tag:
        body_tag.clear()
        
        # 1. Parse ONLY the WordPress content isolated from the main page
        # Using a more forgiving approach for messy WP HTML
        temp_soup = BeautifulSoup(content, "html.parser")
        
        # 2. Modify tags safely without changing the structure
        for i, img in enumerate(temp_soup.find_all("img")):
            existing_alt = img.get("alt", "")
            if not existing_alt or existing_alt.strip() == "":
                img["alt"] = f"Image {i+1} illustrating {title}"
            # Ensure native lazy loading for performance
            if not img.get("loading"):
                img["loading"] = "lazy"
        
        # 3. CRITICAL FIX: Convert the modified isolated soup BACK to a raw HTML string.
        # This prevents the main page's parser from getting confused and duplicating tags 
        # when we try to append a soup object into another soup object.
        safe_html_content = str(temp_soup)
        
        # 4. Inject the raw HTML string directly into the main document
        # We append a parsed version of the STRING, which is much safer than appending a modified nested soup.
        body_tag.append(BeautifulSoup(safe_html_content, "html.parser"))
    # 5. Inject Recommendations & Add SEO `alt` tags for Recommendation cards
    rec_section = soup.find(id="recommendationsSection")
    if rec_section and "hidden" in rec_section.get("class", []):
        rec_section["class"].remove("hidden")
        
    rec_grid = soup.find(id="recGrid")
    if rec_grid:
        rec_grid.clear()
        for rec in recs_data:
            rec_title = rec["title"]
            rec_image = rec["image"]
            rec_alt_text = f"Image of {rec_title}"
            
            if rec_image:
                # Adding alt tag and lazy loading
                img_html = f'<div class="h-40 w-full overflow-hidden bg-gray-100"><img src="{rec_image}" alt="{rec_alt_text}" loading="lazy" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"></div>'
            else:
                img_html = '<div class="h-40 w-full bg-green-50 flex items-center justify-center"><i class="fas fa-leaf text-green-200 text-4xl" aria-label="No image available"></i></div>'
            
            card_html = f"""
            <a href="/{rec['slug']}" class="glass-morphism glass-card rounded-xl overflow-hidden group block text-left">
                {img_html}
                <div class="p-5">
                    <h4 class="font-bold text-green-900 leading-tight mb-2 group-hover:text-green-700 transition-colors line-clamp-2">{rec_title}</h4>
                    <div class="text-xs text-gray-500 font-semibold uppercase tracking-wide mt-2">Read Article <i class="fas fa-arrow-right ml-1"></i></div>
                </div>
            </a>
            """
            rec_grid.append(BeautifulSoup(card_html, "html.parser"))

    # Clean up Javascript blocks that do frontend fetching
    for script in soup.find_all("script"):
        if script.string and ("const WP_SITE" in script.string or "async function init" in script.string):
            script.decompose()

    # 6. Ads Script Optimization (Added 'async' and 'defer')
    # Ensures the ad network script does not block page rendering
    ad_script = soup.new_tag("script", src="https://quge5.com/88/tag.min.js")
    ad_script["data-zone"] = "209738"
    ad_script["async"] = ""  # Let it load asynchronously
    ad_script["defer"] = ""  # Let it execute after parsing
    if soup.body:
        soup.body.append(ad_script)

    # Convert final optimized soup to string
    return str(soup)

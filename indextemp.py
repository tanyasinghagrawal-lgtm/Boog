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
    head_tag = soup.head
    if head_tag:
        # Google Fonts Preconnect
        preconnect_1 = soup.new_tag("link", rel="preconnect", href="https://fonts.googleapis.com")
        preconnect_2 = soup.new_tag("link", rel="preconnect", href="https://fonts.gstatic.com", crossorigin="anonymous")
        
        # FontAwesome Preload
        preload_fa = soup.new_tag("link", rel="preload", href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/webfonts/fa-solid-900.woff2")
        preload_fa["as"] = "font"
        preload_fa["type"] = "font/woff2"
        preload_fa["crossorigin"] = "anonymous"
        
        # Insert them right after the meta tags
        head_tag.insert(0, preload_fa)
        head_tag.insert(0, preconnect_2)
        head_tag.insert(0, preconnect_1)

        # Defer Tailwind CDN if it exists to unblock rendering slightly (Optional but helpful)
        tw_script = soup.find("script", src="https://cdn.tailwindcss.com")
        if tw_script:
            tw_script["defer"] = ""

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
    body_tag = soup.find(id="blogBody")
    if body_tag:
        body_tag.clear()
        
        # Parse content html specifically to modify inner tags
        content_soup = BeautifulSoup(content, "html.parser")
        
        # Give every image inside the blog content an alt text if missing
        for i, img in enumerate(content_soup.find_all("img")):
            existing_alt = img.get("alt", "").strip()
            if not existing_alt:
                img["alt"] = f"Image {i+1} illustrating {title}"
            img["loading"] = "lazy" # Native lazy loading for lower images
            
        body_tag.append(content_soup)

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

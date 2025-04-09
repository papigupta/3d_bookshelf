# app.py - FINAL: Fixed Invalid Regex Error
# --- Imports ---
from flask import Flask, request, render_template_string, jsonify
import requests
from bs4 import BeautifulSoup
import os
import threading
import re
from urllib.parse import quote_plus
import io
import traceback
import math

# --- Pillow Check ---
try:
    from PIL import Image, ImageStat
    print("Pillow loaded.")
except ImportError:
    print("WARNING: Pillow not found...")
    Image = None; ImageStat = None

# --- Colorgram Import ---
try:
    import colorgram
    print("colorgram.py loaded.")
except ImportError:
     print("WARNING: colorgram.py not found. pip install colorgram.py")
     colorgram = None

# --- Global Data ---
progress_data = { "total_books": 0, "books_processed": 0, "complete": False, "error": None }

# --- Helper Function: get_edge_color (Copied from User) ---
def get_edge_color(image_url, edge_width_percent=10):
    if not Image or not ImageStat or not io:
        return "#808080"
    try:
        response = requests.get(image_url, stream=True, timeout=10)
        response.raise_for_status()
        img = Image.open(io.BytesIO(response.content))
    except Exception as e:
        print(f"Warn: Img process fail (request/open) {image_url.split('/')[-1]} {e}")
        return "#808080"

    try:
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        img_width, img_height = img.size
        if img_width <= 1 or img_height <= 0:
            return "#808080"

        width = max(1, int(img.width * (edge_width_percent / 100)))
        width = min(width, img_width)

        edge = img.crop((0, 0, width, img.height))
        stat = ImageStat.Stat(edge)
        avg_color = (128, 128, 128) # Default gray

        if hasattr(stat, 'mean') and stat.mean:
             avg_color_float = stat.mean
             if isinstance(avg_color_float, (list, tuple)) and len(avg_color_float) >= 1:
                 avg_color_int = tuple(int(c) for c in avg_color_float[:3])
                 if len(avg_color_int) == 1:
                     avg_color = (avg_color_int[0], avg_color_int[0], avg_color_int[0])
                 elif len(avg_color_int) >= 3:
                     avg_color = avg_color_int[:3]
             elif isinstance(avg_color_float, (int, float)):
                 gray_val = int(avg_color_float)
                 avg_color = (gray_val, gray_val, gray_val)

        if len(avg_color) != 3:
             avg_color = (128, 128, 128)

        avg_color = tuple(max(0, min(255, c)) for c in avg_color)
        hex_color = "#{:02x}{:02x}{:02x}".format(*avg_color)
        return hex_color
    except Exception as e:
        print(f"Warn: Img process fail (analysis) {image_url.split('/')[-1]} {e}")
        return "#808080"

# --- Contrast Calculation Helpers (Copied from User) ---
def hex_to_rgb(hex_color):
    hex_color = str(hex_color or '').lstrip('#')
    if len(hex_color) != 6:
        return (128, 128, 128)
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        return (r, g, b)
    except ValueError:
        return (128, 128, 128)

def get_luminance(r, g, b):
    if not all(isinstance(c, (int, float)) for c in [r, g, b]):
         return 0.5
    try:
        rgb_norm = []
        for val in [r, g, b]:
            c = val / 255.0
            if c <= 0.03928:
                rgb_norm.append(c / 12.92)
            else:
                rgb_norm.append(math.pow(((c + 0.055) / 1.055), 2.4))
        return 0.2126 * rgb_norm[0] + 0.7152 * rgb_norm[1] + 0.0722 * rgb_norm[2]
    except Exception as e:
        print(f"Error calculating luminance for ({r},{g},{b}): {e}")
        return 0.5 # Default on error

def get_contrast_ratio(lum1, lum2):
    if not isinstance(lum1, (int, float)) or not isinstance(lum2, (int, float)):
        return 1.0
    L1, L2 = sorted([lum1, lum2], reverse=True)
    denominator = L2 + 0.05
    if abs(denominator) < 1e-9:
        return 1.0
    return (L1 + 0.05) / denominator

# --- Contrasting Text Color Helper (Copied from User) ---
def get_contrasting_text_color(image_url, spine_bg_hex, min_contrast=4.5, fallback_light='#FFFFFF', fallback_dark='#000000'):
    r_bg, g_bg, b_bg = hex_to_rgb(spine_bg_hex)
    bg_lum = get_luminance(r_bg, g_bg, b_bg)
    default_color = fallback_dark if bg_lum > 0.5 else fallback_light

    if not Image or not io or not colorgram or not image_url:
        return default_color

    best_color_hex = None
    highest_contrast = 0.0

    try:
        response = requests.get(image_url, stream=True, timeout=10)
        response.raise_for_status()
        if not response.content: raise ValueError("Empty image content")

        img_buffer = io.BytesIO(response.content)
        img_buffer.seek(0)
        colors = colorgram.extract(img_buffer, 6)

        if not colors: raise ValueError("Could not extract colors")

        for color in colors:
            c_r, c_g, c_b = color.rgb.r, color.rgb.g, color.rgb.b
            c_lum = get_luminance(c_r, c_g, c_b)
            contrast = get_contrast_ratio(bg_lum, c_lum)

            if contrast > highest_contrast:
                highest_contrast = contrast
                best_color_hex = "#{:02x}{:02x}{:02x}".format(c_r, c_g, c_b)

        if highest_contrast >= min_contrast:
            return best_color_hex
        else:
            return default_color

    except Exception as e:
        # print(f"Warn: Failed palette/contrast for {image_url[-30:]}: {e}. Using default.") # Less verbose
        return default_color

# --- Helper Function: get_books_from_shelf (Rating/Review Included) ---
def get_books_from_shelf(url):
    global progress_data
    progress_data = {"total_books":0, "books_processed":0, "complete":False, "error":None}
    books=[]
    headers={"User-Agent": "Mozilla/5.0"}
    PAGE_COUNT_SELECTOR = 'td.field.num_pages .value'
    DEFAULT_PAGE_COUNT = 350
    RATING_SELECTOR = 'td.field.rating .value span.staticStars'
    REVIEW_SELECTOR = 'td.field.review .value span[id^="freeTextContainer"]'

    try:
        page = 1
        initial_url = f"{url}&page=1"
        initial_response = requests.get(initial_url, headers=headers, timeout=15)
        initial_response.raise_for_status()
        initial_soup = BeautifulSoup(initial_response.text, "html.parser")

        total_books = 0
        count_elem = initial_soup.select_one('#shelfHeader .greyText')
        if count_elem and 'books)' in count_elem.text:
            match = re.search(r'of (\d{1,3}(?:,\d{3})*|\d+) books', count_elem.text.replace(',',''))
            if match: total_books = int(match.group(1))
        if total_books == 0:
             count_elem_fallback = initial_soup.select_one('.selectedShelf')
             if count_elem_fallback: total_books = int(''.join(filter(str.isdigit, count_elem_fallback.text)))

        progress_data["total_books"] = max(1, total_books)

        while True:
            current_url = f"{url}&page={page}"

            if page == 1:
                soup = initial_soup
            else:
                try:
                    response = requests.get(current_url, headers=headers, timeout=10)
                    response.raise_for_status()
                    soup = BeautifulSoup(response.text, "html.parser")
                except requests.exceptions.RequestException as page_err:
                    print(f"Error fetching page {page}: {page_err}.")
                    progress_data["error"] = f"Warn: Failed page {page}."
                    break

            rows = soup.select('tr[id^="review_"]')
            if not rows:
                break

            for row in rows:
                title_elem = row.select_one('td.field.title .value a')
                author_elem = row.select_one('td.field.author .value a')

                if title_elem and author_elem:
                    book_title = title_elem.text.strip()
                    author_text = author_elem.text.strip()
                    if ', ' in author_text:
                        parts = author_text.split(', ', 1)
                        author_name = f"{parts[1]} {parts[0]}" if len(parts) == 2 else author_text
                    else:
                        author_name = author_text

                    image_elem = row.select_one('td.field.cover img')
                    publisher_elem = row.select_one('td.field.publisher .value')
                    page_count_elem = row.select_one(PAGE_COUNT_SELECTOR)
                    rating_elem = row.select_one(RATING_SELECTOR)
                    review_container_elem = row.select_one(REVIEW_SELECTOR)
                    review_text = ""
                    if review_container_elem:
                        review_parts = [elem.text for elem in review_container_elem.find_all(string=True, recursive=False)]
                        review_text = ' '.join(review_parts).strip()
                        review_text = re.sub(r'\s*\.\.\.\(more\)$', '', review_text)

                    publisher_name = publisher_elem.text.strip() if publisher_elem else ""
                    page_count = DEFAULT_PAGE_COUNT
                    if page_count_elem:
                        page_text_raw = page_count_elem.text.strip()
                        match = re.search(r'(\d{1,3}(?:,\d{3})*|\d+)\s*(?:pages?)?', page_text_raw.replace(',',''))
                        if match:
                            try: page_count = max(1, int(match.group(1)))
                            except ValueError: pass

                    rating_value = None
                    if rating_elem and rating_elem.has_attr("title"):
                        rating_value = rating_elem["title"].strip()

                    raw_image_url = image_elem.get("src") if image_elem else ""
                    high_res_image_url = raw_image_url
                    if raw_image_url:
                        high_res_image_url = re.sub(r'\._S[XY]?\d+(_?\.|\.jpg)', '.', raw_image_url, count=1)

                    spine_color = get_edge_color(high_res_image_url) if high_res_image_url and Image else "#808080"
                    spine_text_color = get_contrasting_text_color(high_res_image_url, spine_color) if high_res_image_url and Image and colorgram else ('#000000' if get_luminance(*hex_to_rgb(spine_color)) > 0.5 else '#FFFFFF')

                    books.append({
                        "title": book_title,
                        "author": author_name,
                        "publisher": publisher_name,
                        "image": high_res_image_url,
                        "spine_color": spine_color,
                        "spine_text_color": spine_text_color,
                        "page_count": page_count,
                        "rating": rating_value,
                        "review": review_text
                    })
                    progress_data["books_processed"] = len(books)

            page += 1

        if not books and not progress_data.get("error"):
            progress_data["error"] = "No valid books found."
        progress_data["total_books"] = max(progress_data["total_books"], progress_data["books_processed"])
        progress_data["complete"] = True
        print(f"\nScraping finished. Found: {len(books)} books.")
        return books

    except requests.exceptions.RequestException as req_err:
        print(f"Initial request failed: {req_err}")
        progress_data["error"] = f"Error connecting to Goodreads: {req_err}"
        progress_data["complete"] = True
        return None
    except Exception as e:
        print(f"Unexpected scraping error in get_books_from_shelf: {e}")
        traceback.print_exc()
        progress_data["error"] = str(e)
        progress_data["complete"] = True
        return None


# --- Flask App ---
app = Flask(__name__)

print("Test change - Version 1.2 (Fixed Regex Error)") # Version Bump

# --- HTML Template ---
THREE_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"> <meta name="viewport" content="width=device-width, initial-scale=1.0"> <title>My 3D Bookshelf</title>
    <style>
        /* Base Styles */
        body { margin: 0; background-color: #090909; color: #eee; font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; overflow-y: scroll; }
        #info { display: none; }
        /* Input and Loading States */
        #loading-message, #input-container { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); background: #fff; color: #333; padding: 30px; border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.2); text-align: center; z-index: 200; min-width: 280px; }
        #input-container h2 { margin-top: 0; color: #333; font-size: 1.5em; margin-bottom: 1.5em; }
        #input-container input[type=text] { width: calc(100% - 24px); padding: 12px; margin-bottom: 20px; border: 2px solid #ddd; border-radius: 6px; font-size: 1em; transition: border-color 0.2s ease; }
        #input-container input[type=text]:focus { border-color: #4CAF50; outline: none; }
        #input-container button { padding: 12px 24px; background-color: #4CAF50; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 1em; transition: background-color 0.2s ease; }
        #input-container button:hover { background-color: #45a049; }
        /* Canvas Container */
        #canvas-container { width: 100%; height: 100%; position: fixed; top: 0; left: 0; z-index: 1; }
        canvas { display: block; }
        /* Progress Bar */
        #progress-bar { width: 85%; background-color: #e0e0e0; border-radius: 6px; overflow: hidden; margin: 15px auto 10px auto; height: 8px; }
        #progress-fill { height: 100%; background-color: #4CAF50; width: 0%; transition: width 0.3s ease; font-size: 0.8em; color: transparent; }
        #status-text { font-family: monospace; margin-top: 12px; font-size: 0.9em; color: #666; }
        /* Detail View */
        #detail-view { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(9, 9, 9, 0.95); display: flex; align-items: center; justify-content: center; z-index: 150; opacity: 0; pointer-events: none; transition: opacity 0.5s ease-in-out; color: #eee; backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); }
        #detail-view.visible { opacity: 1; pointer-events: auto; }
        #detail-left { flex: 1.2; position: relative; display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 40px; box-sizing: border-box; }
        #detail-right { flex: 1; padding: 60px 50px; max-width: 560px; height: 100vh; box-sizing: border-box; overflow-y: auto; background: rgba(255,255,255,0.03); border-left: 1px solid rgba(255,255,255,0.1); }
        #detail-right::-webkit-scrollbar { width: 8px; }
        #detail-right::-webkit-scrollbar-track { background: rgba(255,255,255,0.05); }
        #detail-right::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.2); border-radius: 4px; }
        #detail-right h2 { margin-top: 0; font-size: 2.2em; color: #fff; line-height: 1.2; font-weight: 700; margin-bottom: 0.5em; }
        #detail-right p { margin-bottom: 1.5em; line-height: 1.6; font-size: 1.1em; color: rgba(255,255,255,0.9); }
        #detail-right p.book-meta { margin-bottom: 1em; font-size: 1em; color: rgba(255,255,255,0.8); }
        #detail-right p.book-meta strong { color: rgba(255,255,255,0.9); margin-right: 5px; }
        #detail-right #detail-rating-value, #detail-review-value { font-style: normal; display: block; margin-top: 5px; line-height: 1.5; color: rgba(255,255,255,0.75); }
        #detail-right #detail-rating-value.not-available, #detail-right #detail-review-value.not-available { font-style: italic; color: #aaa; }
        #detail-rating-stars { color: #f0e442; font-size: 1.2em; margin-left: 5px; }
        #detail-right #detail-author { font-style: italic; margin-bottom: 2em; font-size: 1.3em; color: rgba(255,255,255,0.8); }
        #detail-right #detail-publisher, #detail-right #detail-page-count { font-size: 1em; color: rgba(255,255,255,0.6); }
        #detail-right hr { border: none; border-top: 1px solid rgba(255,255,255,0.1); margin: 2em 0; }
        #close-detail-button { position: fixed; top: 30px; right: 40px; background: none; border: none; color: rgba(255,255,255,0.6); font-size: 2.5em; cursor: pointer; padding: 10px; line-height: 0.8; transition: all 0.2s ease; z-index: 160; width: 48px; height: 48px; border-radius: 50%; }
        #close-detail-button:hover { color: #fff; background: rgba(255,255,255,0.1); }
        /* Responsive Design */
        @media (max-width: 1024px) { #detail-view { flex-direction: column; } #detail-left { flex: none; height: 40vh; min-height: auto; width: 100%; padding: 20px; } #detail-right { flex: none; height: 60vh; width: 100%; max-width: none; padding: 30px; border-left: none; border-top: 1px solid rgba(255,255,255,0.1); } #detail-right h2 { font-size: 1.8em; } #close-detail-button { top: 20px; right: 20px; } }
        /* Animation Classes */
        .fade-in { animation: fadeIn 0.5s ease forwards; } .fade-out { animation: fadeOut 0.5s ease forwards; } @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } } @keyframes fadeOut { from { opacity: 1; } to { opacity: 0; } }
    </style>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"></script>
</head>
<body>
    <div id="info"> <h1>My 3D Reading Journey</h1> <div id="stats"></div> </div>
    <div id="input-container"> <h2>Enter Goodreads Shelf URL</h2> <form id="shelfForm"> <input type="text" id="shelfUrl" name="url" required placeholder="https://www.goodreads.com/review/list/..."> <button type="submit">Load Bookshelf</button> </form> </div>
    <div id="loading-message" style="display:none"> <div id="status-text">Fetching data...</div> <div id="progress-bar"><div id="progress-fill">0%</div></div> </div>
    <div id="canvas-container"></div>

    <div id="detail-view">
        <div id="detail-left"></div>
        <div id="detail-right">
            <h2 id="detail-title">Book Title</h2>
            <p id="detail-author">Author Name</p>
            <p id="detail-publisher">Publisher</p>
            <p id="detail-page-count">XXX pages</p>
            <hr>
            <p class="book-meta"><strong>My Rating:</strong> <span id="detail-rating-value" class="not-available">(Not Available)</span></p>
            <p class="book-meta"><strong>My Review:</strong> <span id="detail-review-value" class="not-available">(Not Available)</span></p>
        </div>
        <button id="close-detail-button" title="Close Details">&times;</button>
    </div>

    <script type="importmap">{% raw %}
        { "imports": { "three": "https://unpkg.com/three@0.163.0/build/three.module.js", "three/addons/": "https://unpkg.com/three@0.163.0/examples/jsm/" } }
    {% endraw %}</script>

    <script type="module">{% raw %}
        import * as THREE from 'three';

        // --- Constants ---
        const BOOK_DEFAULTS = { HEIGHT: 9.0, WIDTH: 6.075, THICKNESS: 1.125 }; const PAGE_COLOR = 0xf5f5dc; const BOOK_SPACING = -6; const TARGET_ROTATION_X = THREE.MathUtils.degToRad(-90); const TARGET_ROTATION_Y = THREE.MathUtils.degToRad(88); const CAMERA_Z = 30; const CAMERA_Y = 6; const CAMERA_FOV = 35; const ANIM_START_X = 40; const ANIM_DURATION = 0.7; const ANIM_STAGGER = 0.03; const ANIM_EASE = "back.out(0.5)"; const SPINE_TEXTURE_HEIGHT = 450; const SPINE_FONT_FAMILY = 'Arial, sans-serif'; const SPINE_TITLE_SIZE_PX = 14; const SPINE_AUTHOR_SIZE_PX = 9; const SPINE_PUBLISHER_SIZE_PX = 6; const SPINE_PADDING_PX = 15; const SPINE_AUTHOR_PADDING_PX = 5; const SPINE_TEXT_OPACITY = 0.75; const SPINE_TITLE_LINE_HEIGHT = SPINE_TITLE_SIZE_PX * 1.1; const SPINE_AUTHOR_LINE_HEIGHT = SPINE_AUTHOR_SIZE_PX * 1.1; const SPINE_AUTHOR_MAX_LINES = 4; const SPINE_TITLE_MAX_LINES = 2; const SPINE_EDGE_WIDTH = 8; const SPINE_HIGHLIGHT_OPACITY = 0.4; const SPINE_SHADOW_OPACITY = 0.3; const MIN_THICKNESS = 0.2; const MAX_THICKNESS = 4.0; const AVG_PAGE_COUNT = 350; const BACK_TEXTURE_WIDTH = 300; const BACK_SCRIBBLE_OPACITY = 0.75; const BACK_SCRIBBLE_BLUR = 'blur(0.5px)'; const BACK_SCRIBBLE_LINES = 50; const BACK_SCRIBBLE_LINE_HEIGHT = 5; const MATTE_ROUGHNESS = 0.98; const MATTE_METALNESS = 0.01; const HOVER_SCALE = 1.1; const HOVER_Z_OFFSET = 1.0; const HOVER_DURATION = 0.3; const DETAIL_ANIM_DURATION = 0.7; const DETAIL_BOOK_TARGET_POS = new THREE.Vector3(-10, 0, 15); const DETAIL_BOOK_TARGET_ROT = new THREE.Euler( THREE.MathUtils.degToRad(-10), THREE.MathUtils.degToRad(20), 0, 'YXZ' ); const DETAIL_BOOK_TARGET_SCALE = 1.5; const OTHER_BOOKS_FLY_X_OFFSET = 60; const OTHER_BOOKS_FADE_SCALE = 0.01; const DETAIL_VIEW = { BOOK: { POSITION: new THREE.Vector3(-8, 0, 10), ROTATION: new THREE.Euler( THREE.MathUtils.degToRad(-5), THREE.MathUtils.degToRad(25), 0, 'YXZ' ), SCALE: 2.0, ANIMATION_DURATION: 1.2, EASE: "power3.inOut" }, OTHER_BOOKS: { FADE_DURATION: 0.4, STAGGER: 0.02, EXIT_X: 40 } };

        // --- DOM Elements ---
        const inputContainer=document.getElementById('input-container'); const loadingMessage=document.getElementById('loading-message'); const shelfForm=document.getElementById('shelfForm'); const shelfUrlInput=document.getElementById('shelfUrl'); const statsDiv=document.getElementById('stats'); const canvasContainer=document.getElementById('canvas-container'); const progressBarFill=document.getElementById('progress-fill'); const statusText=document.getElementById('status-text'); const detailViewDiv = document.getElementById('detail-view'); const detailTitle = document.getElementById('detail-title'); const detailAuthor = document.getElementById('detail-author'); const detailPublisher = document.getElementById('detail-publisher'); const detailPageCount = document.getElementById('detail-page-count'); const closeDetailButton = document.getElementById('close-detail-button');
        const detailRatingValue = document.getElementById('detail-rating-value'); const detailReviewValue = document.getElementById('detail-review-value');


        // --- Three.js Variables ---
        let scene, camera, renderer; let bookData=[]; const textureLoader=new THREE.TextureLoader(); const booksGroup=new THREE.Group(); let currentScrollY=window.scrollY; let targetGroupY=0; let animationFrameId=null; let progressIntervalId=null; const raycaster = new THREE.Raycaster(); const mouse = new THREE.Vector2(); let currentlyHovered = null; let isDetailView = false; let selectedBookIndex = -1; let selectedBookMesh = null; let isTransitioning = false;

        // --- Helper Functions ---
        function hexToRgba(hex, alpha) { hex = String(hex || '').replace('#', ''); const r = parseInt(hex.substring(0, 2), 16); const g = parseInt(hex.substring(2, 4), 16); const b = parseInt(hex.substring(4, 6), 16); if (isNaN(r) || isNaN(g) || isNaN(b)) return `rgba(128, 128, 128, ${alpha})`; return `rgba(${r}, ${g}, ${b}, ${alpha})`; }
        function wrapText(context, text, maxWidth) { const words = String(text || '').split(' '); let line = ''; const lines = []; for(let n = 0; n < words.length; n++) { const testLine = line + words[n] + ' '; const metrics = context.measureText(testLine); const testWidth = metrics.width; if (testWidth > maxWidth && n > 0) { lines.push(line.trim()); line = words[n] + ' '; } else { line = testLine; } } lines.push(line.trim()); return lines; }
        function createSpineTexture(book, widthPx, heightPx) { widthPx = Math.max(1, Math.round(widthPx)); heightPx = Math.max(1, Math.round(heightPx)); const canvas = document.createElement('canvas'); canvas.width = widthPx; canvas.height = heightPx; const ctx = canvas.getContext('2d'); if (!ctx) return null; try { ctx.fillStyle = book.spine_color || '#808080'; ctx.fillRect(0, 0, widthPx, heightPx); const shadowGradient = ctx.createLinearGradient(0, 0, SPINE_EDGE_WIDTH, 0); shadowGradient.addColorStop(0, `rgba(0, 0, 0, ${SPINE_SHADOW_OPACITY})`); shadowGradient.addColorStop(1, 'rgba(0, 0, 0, 0)'); ctx.fillStyle = shadowGradient; ctx.fillRect(0, 0, SPINE_EDGE_WIDTH, heightPx); const highlightGradient = ctx.createLinearGradient(widthPx - SPINE_EDGE_WIDTH, 0, widthPx, 0); highlightGradient.addColorStop(0, 'rgba(255, 255, 255, 0)'); highlightGradient.addColorStop(1, `rgba(255, 255, 255, ${SPINE_HIGHLIGHT_OPACITY})`); ctx.fillStyle = highlightGradient; ctx.fillRect(widthPx - SPINE_EDGE_WIDTH, 0, SPINE_EDGE_WIDTH, heightPx); ctx.fillStyle = book.spine_text_color || '#FFFFFF'; ctx.save(); ctx.translate(widthPx / 2, heightPx / 2); ctx.rotate(Math.PI / 2); ctx.translate(-heightPx / 2, -widthPx / 2); const drawWidth = heightPx; const drawHeight = widthPx; if (book.publisher) { ctx.font = `${SPINE_PUBLISHER_SIZE_PX}px ${SPINE_FONT_FAMILY}`; ctx.textAlign = 'left'; ctx.textBaseline = 'top'; ctx.globalAlpha = SPINE_TEXT_OPACITY; let pubText = book.publisher; const maxPubWidth = drawWidth - (SPINE_PADDING_PX * 2); if (ctx.measureText(pubText).width > maxPubWidth) { pubText = pubText.substring(0, Math.floor(maxPubWidth / (SPINE_PUBLISHER_SIZE_PX * 0.6))) + "..."; } ctx.fillText(pubText, SPINE_PADDING_PX, SPINE_PADDING_PX); ctx.globalAlpha = 1.0; } ctx.font = `bold ${SPINE_TITLE_SIZE_PX}px ${SPINE_FONT_FAMILY}`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; const maxTitleWidth = drawWidth * 0.75; const titleText = book.title || ''; let titleLines = wrapText(ctx, titleText, maxTitleWidth); const requiredHeightForTwoLines = 2 * SPINE_TITLE_LINE_HEIGHT; const availableDrawHeight = drawHeight - (SPINE_PADDING_PX * 2); if (titleLines.length > 1 && requiredHeightForTwoLines > availableDrawHeight) { titleLines = [ wrapText(ctx, titleText, maxTitleWidth)[0] ]; if (ctx.measureText(titleLines[0]).width > maxTitleWidth) { let line = titleLines[0]; const ellipsis = "..."; const ellipsisWidth = ctx.measureText(ellipsis).width; while (ctx.measureText(line).width + ellipsisWidth > maxTitleWidth && line.length > 0) { line = line.slice(0, -1); } titleLines[0] = line.trim() + ellipsis; } } else if (titleLines.length > SPINE_TITLE_MAX_LINES) { titleLines = titleLines.slice(0, SPINE_TITLE_MAX_LINES); let lastLine = titleLines[SPINE_TITLE_MAX_LINES - 1]; const ellipsis = "..."; const ellipsisWidth = ctx.measureText(ellipsis).width; while (ctx.measureText(lastLine).width + ellipsisWidth > maxTitleWidth && lastLine.length > 0) { lastLine = lastLine.slice(0, -1); } titleLines[SPINE_TITLE_MAX_LINES - 1] = lastLine.trim() + ellipsis; } const totalTitleHeight = titleLines.length * SPINE_TITLE_LINE_HEIGHT; let currentTitleY = (drawHeight / 2) - (totalTitleHeight / 2) + (SPINE_TITLE_LINE_HEIGHT / 2); titleLines.forEach(line => { ctx.fillText(line, drawWidth / 2, currentTitleY); currentTitleY += SPINE_TITLE_LINE_HEIGHT; }); ctx.font = `${SPINE_AUTHOR_SIZE_PX}px ${SPINE_FONT_FAMILY}`; ctx.globalAlpha = SPINE_TEXT_OPACITY; const authorText = book.author || ''; const maxAuthorDrawWidth = drawHeight - (SPINE_AUTHOR_PADDING_PX * 2); let authorLines = wrapText(ctx, authorText, maxAuthorDrawWidth); if (authorLines.length > SPINE_AUTHOR_MAX_LINES) { authorLines = authorLines.slice(0, SPINE_AUTHOR_MAX_LINES); let lastLine = authorLines[SPINE_AUTHOR_MAX_LINES - 1]; const ellipsis = "..."; const ellipsisWidth = ctx.measureText(ellipsis).width; while (ctx.measureText(lastLine).width + ellipsisWidth > maxAuthorDrawWidth && lastLine.length > 0) { lastLine = lastLine.slice(0, -1); } authorLines[SPINE_AUTHOR_MAX_LINES - 1] = lastLine.trim() + ellipsis; } ctx.save(); ctx.translate(drawWidth - SPINE_PADDING_PX, drawHeight / 2); ctx.rotate(-Math.PI / 2); ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'; let currentAuthorY = (authorLines.length -1) * SPINE_AUTHOR_LINE_HEIGHT / 2; for (let i = authorLines.length - 1; i >= 0; i--) { ctx.fillText(authorLines[i], 0, currentAuthorY); currentAuthorY -= SPINE_AUTHOR_LINE_HEIGHT; } ctx.restore(); ctx.restore(); ctx.globalAlpha = 1.0; } catch (error) { console.error("Error creating spine texture:", error); ctx.fillStyle = 'red'; ctx.fillRect(0, 0, widthPx, heightPx); } const texture = new THREE.CanvasTexture(canvas); texture.colorSpace = THREE.SRGBColorSpace; texture.needsUpdate = true; return texture; }
        function createBackTexture(book, widthPx, heightPx) { widthPx = Math.max(1, Math.round(widthPx)); heightPx = Math.max(1, Math.round(heightPx)); const canvas = document.createElement('canvas'); canvas.width = widthPx; canvas.height = heightPx; const ctx = canvas.getContext('2d'); if (!ctx) return null; try { ctx.fillStyle = book.spine_color || '#DDDDDD'; ctx.fillRect(0, 0, widthPx, heightPx); ctx.strokeStyle = hexToRgba(book.spine_text_color || '#000000', BACK_SCRIBBLE_OPACITY); ctx.lineWidth = 0.7; ctx.filter = BACK_SCRIBBLE_BLUR; const padding = widthPx * 0.1; const lineLengthVariation = widthPx * 0.2; const lineStartXVariation = widthPx * 0.05; let currentY = padding; while (currentY < heightPx - padding) { const startX = padding + Math.random() * lineStartXVariation; const endX = widthPx - padding - Math.random() * lineLengthVariation; ctx.beginPath(); ctx.moveTo(startX, currentY); ctx.lineTo(endX, currentY); ctx.stroke(); currentY += BACK_SCRIBBLE_LINE_HEIGHT * (0.8 + Math.random() * 0.4); if (Math.random() < 0.1) { currentY += BACK_SCRIBBLE_LINE_HEIGHT * 1.5; } } ctx.filter = 'none'; } catch (error) { console.error("Error creating back texture:", error); ctx.fillStyle = 'red'; ctx.fillRect(0, 0, widthPx, heightPx); } const texture = new THREE.CanvasTexture(canvas); texture.colorSpace = THREE.SRGBColorSpace; texture.needsUpdate = true; return texture; }
        function calculateThickness(pageCount) { pageCount = Number(pageCount); if (!pageCount || isNaN(pageCount) || pageCount <= 0) { return BOOK_DEFAULTS.THICKNESS; } const ratio = pageCount / AVG_PAGE_COUNT; const thickness = BOOK_DEFAULTS.THICKNESS * ratio; const clampedThickness = Math.max(MIN_THICKNESS, Math.min(MAX_THICKNESS, thickness)); return clampedThickness; }
        function initThreeJS() { console.log("Initializing Three.js scene..."); scene = new THREE.Scene(); scene.background = new THREE.Color(0x090909); const aspect = window.innerWidth / window.innerHeight; camera = new THREE.PerspectiveCamera(CAMERA_FOV, aspect, 0.1, 1000); camera.position.set(0, CAMERA_Y, CAMERA_Z); camera.lookAt(0, 0, 0); renderer = new THREE.WebGLRenderer({ antialias: true }); renderer.setSize(window.innerWidth, window.innerHeight); renderer.setPixelRatio(window.devicePixelRatio); canvasContainer.appendChild(renderer.domElement); const ambientLight = new THREE.AmbientLight(0xffffff, 0.7); scene.add(ambientLight); const keyLight = new THREE.DirectionalLight(0xffffff, 0.8); keyLight.position.set(-8, 10, 8); scene.add(keyLight); const fillLight = new THREE.DirectionalLight(0xffffff, 0.3); fillLight.position.set(8, 2, 6); scene.add(fillLight); scene.add(booksGroup); window.addEventListener('resize', onWindowResize); if (!animationFrameId) { animate(); console.log("Animation loop started."); } }
        function createBookMesh(book) { const dynamicThickness = calculateThickness(book.page_count); const dynamicSpineTextureWidth = Math.max(1, Math.round(SPINE_TEXTURE_HEIGHT * (dynamicThickness / BOOK_DEFAULTS.HEIGHT))); const geometry = new THREE.BoxGeometry( BOOK_DEFAULTS.WIDTH, BOOK_DEFAULTS.HEIGHT, dynamicThickness ); const pageMaterial = new THREE.MeshStandardMaterial({ color: PAGE_COLOR, roughness: MATTE_ROUGHNESS, metalness: MATTE_METALNESS }); const spineTexture = createSpineTexture(book, dynamicSpineTextureWidth, SPINE_TEXTURE_HEIGHT); if (!spineTexture) return null; const spineMaterial = new THREE.MeshStandardMaterial({ map: spineTexture, color: 0xffffff, roughness: MATTE_ROUGHNESS, metalness: MATTE_METALNESS }); const coverMaterial = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: MATTE_ROUGHNESS, metalness: MATTE_METALNESS }); const backTextureHeight = Math.round(BACK_TEXTURE_WIDTH * (BOOK_DEFAULTS.HEIGHT / BOOK_DEFAULTS.WIDTH)); const backTexture = createBackTexture(book, BACK_TEXTURE_WIDTH, backTextureHeight); if (!backTexture) return null; const backMaterial = new THREE.MeshStandardMaterial({ map: backTexture, color: 0xffffff, roughness: MATTE_ROUGHNESS, metalness: MATTE_METALNESS }); if (book.image) { textureLoader.load( book.image, (texture) => { texture.colorSpace = THREE.SRGBColorSpace; const imgAspect = texture.image.naturalWidth / texture.image.naturalHeight; const geomAspect = BOOK_DEFAULTS.WIDTH / BOOK_DEFAULTS.HEIGHT; texture.repeat.set(1, geomAspect / imgAspect); texture.offset.set(0, (1 - texture.repeat.y) / 2); coverMaterial.map = texture; coverMaterial.needsUpdate = true; }, undefined, (err) => { console.error(`Err loading texture ${book.title}:`, err); } ); } const materials = [ pageMaterial, spineMaterial, pageMaterial, pageMaterial, coverMaterial, backMaterial ]; const mesh = new THREE.Mesh(geometry, materials); mesh.rotation.order = 'YXZ'; mesh.rotation.x = TARGET_ROTATION_X; mesh.rotation.y = TARGET_ROTATION_Y; mesh.userData.bookInfo = book; return mesh; }
        function populateScene() { if (currentlyHovered) { animateHover(currentlyHovered, false); currentlyHovered = null; } while(booksGroup.children.length > 0){ booksGroup.remove(booksGroup.children[0]); } console.log(`Creating ${bookData.length} book meshes...`); let currentY = 0; const bookMeshes = []; bookData.forEach((book, index) => { try { const bookMesh = createBookMesh(book); if (bookMesh) { bookMesh.userData.bookIndex = index; bookMeshes.push(bookMesh); } else { console.error(`Failed to create mesh for book index ${index}`, book); } } catch (meshError) { console.error(`Error creating mesh for book index ${index}:`, meshError, book); } }); if (bookMeshes.length === 0) { console.error("No valid book meshes were created."); statusText.textContent = "Error creating book visuals."; loadingMessage.style.display = 'block'; return; } const actualTotalStackHeight = bookMeshes.reduce((sum, mesh) => sum + mesh.geometry.parameters.height + BOOK_SPACING, 0) - BOOK_SPACING; const startY = actualTotalStackHeight / 2; currentY = startY; bookMeshes.forEach((bookMesh, index) => { const bookHeight = bookMesh.geometry.parameters.height; bookMesh.position.y = currentY - (bookHeight / 2); bookMesh.userData.stackY = bookMesh.position.y; /* STORE STACK Y */ currentY -= (bookHeight + BOOK_SPACING); const startX = (index % 2 === 0) ? -ANIM_START_X : ANIM_START_X; bookMesh.position.x = startX; booksGroup.add(bookMesh); gsap.to(bookMesh.position, { x: 0, duration: ANIM_DURATION, delay: 0.05 + index * ANIM_STAGGER, ease: ANIM_EASE }); }); console.log("Finished adding meshes."); loadingMessage.style.display = 'none'; document.body.style.height = `${actualTotalStackHeight * 50}px`; targetGroupY = -startY + (bookMeshes[0]?.geometry.parameters.height / 2 || 0); booksGroup.position.y = targetGroupY; window.addEventListener('scroll', onWindowScroll); onWindowScroll(); }
        async function updateProgress() { try { const response = await fetch('/progress'); if (!response.ok) { console.warn("Progress check failed:", response.status); return; } const data = await response.json(); const percent = data.progress || 0; if(progressBarFill){ progressBarFill.style.width = percent + '%'; progressBarFill.textContent = percent + '%'; } if(statusText){ if (!data.complete && !data.error) { statusText.textContent = `Processing... (${data.books_processed}/${data.total_books})`; } } if (data.complete || data.error) { console.log("Progress poll end."); if (progressIntervalId) clearInterval(progressIntervalId); progressIntervalId = null; if(progressBarFill){ progressBarFill.style.width = '100%'; progressBarFill.textContent = '100%';} if(statusText && data.error){ statusText.textContent = `Error: ${data.error}`; } else if(statusText && data.complete && bookData.length > 0) { statusText.textContent = `✓ ${bookData.length} books loaded.`; } else if (statusText && data.complete && bookData.length == 0) { statusText.textContent = `No books found or error during process.`;} } } catch (error) { console.warn("Error fetching progress:", error); } }
        async function handleUrlSubmit(event) { event.preventDefault(); const shelfUrl = shelfUrlInput.value.trim(); if (!shelfUrl || !shelfUrl.includes('goodreads.com/review/list/')) { alert('Error: Invalid URL.'); return; } console.log("Shelf URL:", shelfUrl); inputContainer.style.display = 'none'; loadingMessage.style.display = 'block'; statusText.textContent = "Fetching data..."; progressBarFill.style.width = '0%'; progressBarFill.textContent = '0%'; if (progressIntervalId) clearInterval(progressIntervalId); progressIntervalId = setInterval(updateProgress, 1000); try { const apiUrl = `/get_books?url=${encodeURIComponent(shelfUrl)}`; console.log("Fetching from:", apiUrl); const response = await fetch(apiUrl); let errorMsg = `HTTP error ${response.status}`; if (!response.ok) { try { const d=await response.json(); errorMsg = d.error||errorMsg; } catch (e) {} throw new Error(errorMsg); } const data = await response.json(); console.log("Received data:", data); if (progressIntervalId) clearInterval(progressIntervalId); progressIntervalId = null; if (data.error && (!data.books || data.books.length === 0)) { throw new Error(data.error); } bookData = data.books || []; statusText.textContent = `${bookData.length} books found. Building scene...`; progressBarFill.style.width = '100%'; progressBarFill.textContent = '100%'; if (!scene) { initThreeJS(); } populateScene(); } catch (error) { console.error("Fetch error:", error); alert(`Error: ${error.message}`); statusText.textContent = `Error: ${error.message}`; if (progressIntervalId) clearInterval(progressIntervalId); progressIntervalId = null; loadingMessage.style.display = 'block'; } }
        function onWindowScroll() { if (isDetailView || isTransitioning) { return; } const actualTotalStackHeight = booksGroup.children.reduce((sum, mesh) => sum + mesh.geometry.parameters.height + BOOK_SPACING, 0) - BOOK_SPACING; const scrollableHeight = document.documentElement.scrollHeight - window.innerHeight; if (scrollableHeight <= 0) return; currentScrollY = window.scrollY; const scrollRatio = Math.max(0, Math.min(1, currentScrollY / scrollableHeight)); const startY = actualTotalStackHeight / 2; const initialGroupY = -startY + (booksGroup.children[0]?.geometry.parameters.height / 2 || 0); const maxTravel = actualTotalStackHeight - (booksGroup.children[0]?.geometry.parameters.height || BOOK_DEFAULTS.HEIGHT); targetGroupY = initialGroupY + (scrollRatio * maxTravel); }
        function onMouseMove(event) { if (isTransitioning || !camera || !booksGroup || booksGroup.children.length === 0 || isDetailView) return; mouse.x = (event.clientX / window.innerWidth) * 2 - 1; mouse.y = - (event.clientY / window.innerHeight) * 2 + 1; raycaster.setFromCamera(mouse, camera); const intersects = raycaster.intersectObjects(booksGroup.children); if (intersects.length > 0) { const intersectedObject = intersects[0].object; if (intersectedObject.visible && currentlyHovered !== intersectedObject) { if (currentlyHovered) { animateHover(currentlyHovered, false); } currentlyHovered = intersectedObject; animateHover(currentlyHovered, true); } } else { if (currentlyHovered) { animateHover(currentlyHovered, false); currentlyHovered = null; } } }
        function onClick(event) { if (isTransitioning) return; if (event.target === closeDetailButton || detailViewDiv.contains(event.target) && !canvasContainer.contains(event.target)) { return; } if (isDetailView) return; if (!camera || !booksGroup || booksGroup.children.length === 0) return; mouse.x = (event.clientX / window.innerWidth) * 2 - 1; mouse.y = - (event.clientY / window.innerHeight) * 2 + 1; raycaster.setFromCamera(mouse, camera); const intersects = raycaster.intersectObjects(booksGroup.children); if (intersects.length > 0) { const clickedObject = intersects[0].object; if (!clickedObject.visible) return; const index = clickedObject.userData.bookIndex; if (index !== undefined && index !== -1) { if (currentlyHovered === clickedObject) { animateHover(currentlyHovered, false); currentlyHovered = null; } showDetailView(index); } } }
        function onDetailScroll(event) { /* SCROLL BETWEEN DETAILS DISABLED */ if (true) { if (isDetailView) event.preventDefault(); return; } }
        function onWindowResize() { if (!camera || !renderer) return; camera.aspect = window.innerWidth / window.innerHeight; camera.updateProjectionMatrix(); renderer.setSize(window.innerWidth, window.innerHeight); if (!isDetailView) { onWindowScroll(); } }
        function animateHover(object, hoverIn) { if (!object || (object === selectedBookMesh && isDetailView)) return; const targetScale = hoverIn ? HOVER_SCALE : 1.0; const originalZ = object.userData.originalZ !== undefined ? object.userData.originalZ : 0; const targetZ = hoverIn ? originalZ + HOVER_Z_OFFSET : originalZ; gsap.to(object.scale, { x: targetScale, y: targetScale, z: targetScale, duration: HOVER_DURATION, ease: "power2.out", overwrite: true }); gsap.to(object.position, { z: targetZ, duration: HOVER_DURATION, ease: "power2.out", overwrite: true }); }

        // --- Show Detail View (Rating/Review Logic + FIXED REGEX) ---
        function showDetailView(index) {
            if (index < 0 || index >= bookData.length || isTransitioning) return;
            if (isDetailView && index === selectedBookIndex) return;

            isTransitioning = true;
            console.log(`Showing detail view for book ${index}`);

            const meshToShow = booksGroup.children[index];
            const bookInfo = bookData[index]; // Get book data

            if (!meshToShow || !bookInfo) {
                console.error("Invalid book mesh or data for detail view");
                isTransitioning = false;
                return;
            }

            meshToShow.userData.originalState = { position: meshToShow.position.clone(), rotation: meshToShow.rotation.clone(), scale: meshToShow.scale.clone() };

            detailTitle.textContent = bookInfo.title || 'N/A';
            detailAuthor.textContent = bookInfo.author || 'N/A';
            detailPublisher.textContent = bookInfo.publisher ? `Publisher: ${bookInfo.publisher}` : '';
            detailPageCount.textContent = bookInfo.page_count ? `${bookInfo.page_count} pages` : '';

            // Populate Rating
            if (bookInfo.rating) {
                 detailRatingValue.textContent = '';
                 detailRatingValue.classList.remove('not-available');
                 let stars = 0;
                 // *** FIXED REGEX with escaped backslashes \\d and \\s ***
                 const match = bookInfo.rating.match(/(\\d+)\\s*star/);
                 if (match) {
                     stars = parseInt(match[1]);
                 } else if (bookInfo.rating === "it was amazing") stars = 5;
                 else if (bookInfo.rating === "really liked it") stars = 4;
                 else if (bookInfo.rating === "liked it") stars = 3;
                 else if (bookInfo.rating === "it was ok") stars = 2;
                 else if (bookInfo.rating === "did not like it") stars = 1;

                 if (stars > 0) {
                      const starSpan = document.createElement('span'); starSpan.id = 'detail-rating-stars'; starSpan.textContent = '⭐'.repeat(stars); detailRatingValue.appendChild(starSpan);
                      const textSpan = document.createElement('span'); textSpan.textContent = ` (${bookInfo.rating})`; textSpan.style.fontSize = '0.8em'; textSpan.style.marginLeft = '5px'; detailRatingValue.appendChild(textSpan);
                 } else { detailRatingValue.textContent = bookInfo.rating; }
            } else { detailRatingValue.textContent = '(Not Rated)'; detailRatingValue.classList.add('not-available'); }

            // Populate Review
            if (bookInfo.review && bookInfo.review.trim() !== "") {
                 detailReviewValue.innerHTML = bookInfo.review.replace(/\\n/g, '<br>'); // Use escaped \\n here too just in case
                 detailReviewValue.classList.remove('not-available');
            } else { detailReviewValue.textContent = '(No Review)'; detailReviewValue.classList.add('not-available'); }

            detailViewDiv.scrollTop = 0;

            // Animation (GSAP)
            const tl = gsap.timeline({ onStart: () => { console.log("Detail animation starting"); detailViewDiv.style.pointerEvents = 'auto'; detailViewDiv.classList.add('visible'); meshToShow.visible = true; meshToShow.renderOrder = 1; }, onComplete: () => { console.log("Detail animation complete"); isDetailView = true; selectedBookIndex = index; selectedBookMesh = meshToShow; isTransitioning = false; } });
            booksGroup.children.forEach((mesh, i) => { if (i !== index) { tl.to(mesh, { x: (i % 2 === 0 ? -1 : 1) * DETAIL_VIEW.OTHER_BOOKS.EXIT_X, opacity: 0, visible: false, duration: DETAIL_VIEW.OTHER_BOOKS.FADE_DURATION, ease: "power2.inOut" }, 0); } });
            tl.to(meshToShow.position, { x: DETAIL_VIEW.BOOK.POSITION.x, y: DETAIL_VIEW.BOOK.POSITION.y, z: DETAIL_VIEW.BOOK.POSITION.z, duration: DETAIL_VIEW.BOOK.ANIMATION_DURATION, ease: DETAIL_VIEW.BOOK.EASE }, 0);
            tl.to(meshToShow.rotation, { x: DETAIL_VIEW.BOOK.ROTATION.x, y: DETAIL_VIEW.BOOK.ROTATION.y, z: DETAIL_VIEW.BOOK.ROTATION.z, duration: DETAIL_VIEW.BOOK.ANIMATION_DURATION, ease: DETAIL_VIEW.BOOK.EASE }, 0);
            tl.to(meshToShow.scale, { x: DETAIL_VIEW.BOOK.SCALE, y: DETAIL_VIEW.BOOK.SCALE, z: DETAIL_VIEW.BOOK.SCALE, duration: DETAIL_VIEW.BOOK.ANIMATION_DURATION, ease: DETAIL_VIEW.BOOK.EASE }, 0);
            tl.to(detailViewDiv, { opacity: 1, duration: DETAIL_VIEW.OTHER_BOOKS.FADE_DURATION, ease: "power2.inOut" }, 0.2);
        }
        // --- Hide Detail View ---
        function hideDetailView() {
            if (!isDetailView || isTransitioning || !selectedBookMesh) return;
            isTransitioning = true; console.log("Hiding detail view"); const meshToRestore = selectedBookMesh; const originalState = meshToRestore.userData.originalState;
            if (!originalState) { console.error("Missing original state"); isTransitioning = false; return; }
            const tl = gsap.timeline({ onStart: () => { console.log("Return animation starting"); detailViewDiv.style.pointerEvents = 'none'; booksGroup.children.forEach(mesh => { mesh.visible = true; mesh.renderOrder = 0; }); }, onComplete: () => { console.log("Return animation complete"); isDetailView = false; selectedBookIndex = -1; selectedBookMesh = null; isTransitioning = false; detailViewDiv.classList.remove('visible'); booksGroup.children.forEach(mesh => { if (mesh.userData.stackY !== undefined) { mesh.position.set(0, mesh.userData.stackY, 0); mesh.rotation.set(TARGET_ROTATION_X, TARGET_ROTATION_Y, 0); mesh.scale.set(1, 1, 1); } }); onWindowScroll(); } });
            tl.to(detailViewDiv, { opacity: 0, duration: DETAIL_VIEW.OTHER_BOOKS.FADE_DURATION, ease: "power2.inOut" }, 0);
            tl.to(meshToRestore.position, { x: originalState.position.x, y: originalState.position.y, z: originalState.position.z, duration: DETAIL_VIEW.BOOK.ANIMATION_DURATION, ease: DETAIL_VIEW.BOOK.EASE }, 0);
            tl.to(meshToRestore.rotation, { x: originalState.rotation.x, y: originalState.rotation.y, z: originalState.rotation.z, duration: DETAIL_VIEW.BOOK.ANIMATION_DURATION, ease: DETAIL_VIEW.BOOK.EASE }, 0);
            tl.to(meshToRestore.scale, { x: originalState.scale.x, y: originalState.scale.y, z: originalState.scale.z, duration: DETAIL_VIEW.BOOK.ANIMATION_DURATION, ease: DETAIL_VIEW.BOOK.EASE }, 0);
            booksGroup.children.forEach((mesh, i) => { if (mesh !== meshToRestore) { tl.to(mesh, { x: 0, opacity: 1, visible: true, duration: DETAIL_VIEW.OTHER_BOOKS.FADE_DURATION, ease: "power2.inOut" }, DETAIL_VIEW.BOOK.ANIMATION_DURATION * 0.5); } });
        }
        // --- Animate Loop ---
        function animate() { animationFrameId = requestAnimationFrame(animate); if (!isDetailView && !isTransitioning) { const diff = targetGroupY - booksGroup.position.y; if (Math.abs(diff) > 0.01) { booksGroup.position.y += diff * 0.15; } } renderer.render(scene, camera); }
        function showInputForm(){ loadingMessage.style.display = 'none'; inputContainer.style.display = 'block'; }

        // --- Core Setup ---
        function setupEventHandlers() { shelfForm.addEventListener('submit', handleUrlSubmit); window.addEventListener('resize', onWindowResize); window.addEventListener('mousemove', onMouseMove); window.addEventListener('click', onClick); window.addEventListener('wheel', onDetailScroll, { passive: false }); closeDetailButton.addEventListener('click', hideDetailView); }

        // --- Initialization ---
        document.addEventListener('DOMContentLoaded', () => { console.log("DOM Loaded. Initializing."); if (!scene) { initThreeJS(); } else { console.warn("Scene already exists on DOMContentLoaded?"); } setupEventHandlers(); showInputForm(); });

    {% endraw %}</script>
</body>
</html>'''

# --- Flask Routes ---
@app.route("/")
def index():
    global progress_data; progress_data = {"total_books":0,"books_processed":0,"complete":False,"error":None}
    return render_template_string(THREE_TEMPLATE)

@app.route("/get_books")
def get_books_api():
    url = request.args.get("url", "").strip();
    if not url: return jsonify({"error": "Missing URL parameter"}), 400
    books_data = get_books_from_shelf(url); error_message = progress_data.get("error")
    if error_message and not books_data: status_code = 500 if "page 1" in error_message or "fetch" in error_message else 404; return jsonify({"error": error_message, "books": []}), status_code
    elif not books_data and not error_message: return jsonify({"error": "No books found on shelf.", "books": []}), 404
    elif error_message and books_data: return jsonify({"error": f"Warning: {error_message}", "books": books_data or [], "total_found": len(books_data or [])}), 200
    else: return jsonify({"books": books_data, "total_found": len(books_data)})

@app.route("/progress")
def get_progress():
    total = progress_data.get("total_books", 0); processed = progress_data.get("books_processed", 0)
    percent = min(100, int((processed / total) * 100)) if total > 0 else 0
    return jsonify({ "progress": percent, "books_processed": processed, "total_books": total, "complete": progress_data.get("complete", False), "error": progress_data.get("error") })


# --- Main Execution ---
if __name__ == "__main__":
    if Image is None: print("\nERROR: Pillow library is required. Run 'pip install Pillow'\n")
    if colorgram is None: print("\nERROR: colorgram.py library is required. Run 'pip install colorgram.py'\n")
    app.run(debug=True, port=5000)
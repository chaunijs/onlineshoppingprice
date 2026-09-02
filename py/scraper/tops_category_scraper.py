# -*- coding: utf-8 -*-
"""
Tops Entire Category Scraper - Patchright Edition
Version: 3.0 (Subcategory Discovery + Recommendation Filtering + Full Inventory)
Date: 2026-08-26
Description:
    Scrapes entire Tops categories across all sub-categories and paginated pages
    to capture the complete catalog inventory (all 700+ items in Laundry).
    Prevents cross-category contamination by filtering out recommendation carousels.
    
    Imitates Cloudflare anti-bot bypass techniques from tops_scraper.py:
        - Patchright async driver with system Chrome fallback
        - Warm-up homepage session
        - Cloudflare challenge detection & mouse movement / turnstile simulation
        - Infinite retry queue with progressive backoff
        - Automatic subcategory extraction & deep pagination (?page=1, 2, 3...)
        - Recommendation & carousel filtering (eliminates unrelated items like Ice Cream, Water, etc.)
        - Comprehensive DOM + __NEXT_DATA__ parsing for prices & condition badges (e.g. "Buy 2 Pay 1")
        - Data cleaning & regex parsing with Polars (Brand, Volume, Unit, Pack, Retailer)
        - Deduplication across overlapping multi-tag categories
        - Excel export to output directory
"""

import os
import sys
import re
import json
import math
import asyncio
import random
import datetime
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import polars as pl
from bs4 import BeautifulSoup
from patchright.async_api import async_playwright


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = SCRIPT_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

today_date = datetime.datetime.now().strftime("%Y-%m-%d")
print(f"Today is {today_date}")
print(f"Output directory: {OUTPUT_DIR}")

# Use system Chrome to avoid Playwright browser download SSL issues
USE_SYSTEM_CHROME = os.getenv("USE_SYSTEM_CHROME", "true").lower() == "true"

# Cloudflare & Rate-limit Settings
CF_COOLDOWN_BASE = 30      # Base cooldown between failed retries (seconds)
CF_COOLDOWN_STEP = 15      # Escalation per failed attempt
CF_COOLDOWN_MAX = 120      # Max cooldown cap

PRE_REQUEST_DELAY_MIN = 5  # Polite delay before opening URL
PRE_REQUEST_DELAY_MAX = 10

CF_BYPASS_TICKS = 90       # Seconds to wait/simulate bypass for CF
CF_INITIAL_WAIT = 8        # Wait after navigate before checking CF
CF_POST_RESOLVE_WAIT = 5   # Wait after CF resolved for React rendering


def calc_cooldown(attempt: int) -> int:
    """Progressive backoff: 30s -> 45s -> 60s -> ... capped at 120s."""
    return min(CF_COOLDOWN_BASE + (attempt - 1) * CF_COOLDOWN_STEP, CF_COOLDOWN_MAX)


# ---------------------------------------------------------------------
# Browser Launch Helper
# ---------------------------------------------------------------------
async def launch_browser(p):
    """Launches Chromium with fallback to system Chrome for corporate networks."""
    launch_kwargs = {
        "headless": True,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ]
    }
    if USE_SYSTEM_CHROME:
        launch_kwargs["channel"] = "chrome"
    return await p.chromium.launch(**launch_kwargs)


# ---------------------------------------------------------------------
# Cloudflare Bypass Helper
# ---------------------------------------------------------------------
async def handle_cloudflare_challenge(page, max_ticks: int = CF_BYPASS_TICKS) -> bool:
    """
    Detects and handles Cloudflare interstitial or Turnstile challenges
    via human-like cursor movements, clicks, and reload fallbacks.
    """
    try:
        title = await page.title()
    except Exception:
        title = ""

    try:
        h1_elems = await page.query_selector_all("h1")
        h1_text = " ".join([await h.inner_text() for h in h1_elems])
    except Exception:
        h1_text = ""

    combined_text = f"{title} {h1_text}".lower()
    is_blocked = any(k in combined_text for k in [
        "tops.co.th", "just a moment", "blocked", "checking",
        "error 1015", "access denied", "attention required",
        "challenge", "security verification", "verify you are human"
    ])

    if not is_blocked:
        return True

    print(f"  -> 🛡️ Cloudflare challenge detected! Running active bypass ({max_ticks}s)...")

    for tick in range(1, max_ticks + 1):
        await asyncio.sleep(1)

        try:
            await page.mouse.move(
                random.randint(200, 1200),
                random.randint(200, 800)
            )
        except Exception:
            pass

        if tick % 7 == 0:
            try:
                await page.mouse.click(
                    random.randint(900, 1000),
                    random.randint(500, 580)
                )
            except Exception:
                pass

        if tick % 3 == 0:
            try:
                cur_title = await page.title()
                cur_h1_elems = await page.query_selector_all("h1")
                cur_h1_text = " ".join([await h.inner_text() for h in cur_h1_elems])
                cur_text = f"{cur_title} {cur_h1_text}".lower()

                if not any(k in cur_text for k in [
                    "tops.co.th", "just a moment", "blocked", "checking",
                    "error 1015", "access denied", "attention required",
                    "challenge", "security verification", "verify you are human"
                ]):
                    print(f"  -> ✅ Cloudflare resolved after {tick}s!")
                    return True
            except Exception:
                pass

    print("  -> 🔄 Bypass ticks exhausted. Attempting page reload...")
    try:
        await page.reload(wait_until="domcontentloaded", timeout=90000)
        await asyncio.sleep(10)

        cur_title = await page.title()
        if not any(k in cur_title.lower() for k in ["access denied", "error 1015", "just a moment"]):
            print("  -> ✅ Cloudflare resolved after reload!")
            return True
    except Exception as e:
        print(f"  -> ⚠️ Reload error: {e}")

    return False


# ---------------------------------------------------------------------
# Price & DOM Parsing Helpers
# ---------------------------------------------------------------------
IGNORED_CONDITION_TERMS = {
    "usa", "japan", "united kingdom", "uk", "thailand", "korea", "germany",
    "france", "italy", "australia", "new zealand", "china", "malaysia",
    "singapore", "vietnam", "indonesia", "taiwan", "imported",
    "best seller", "new", "recommended", "top pick", "halal", "organic",
    "vegan", "eco friendly", "exclusive", "tops online", "express"
}


def clean_promo_condition(cond: str) -> str | None:
    """Filters out country names, generic metadata tags, and non-promotional text."""
    if not cond:
        return None
    cond_str = str(cond).strip()
    if not cond_str or len(cond_str) < 2:
        return None
    if cond_str.lower() in IGNORED_CONDITION_TERMS:
        return None
    if re.match(r"^\s*฿?\s*\d+(?:\.\d+)?\s*$", cond_str):
        return None
    return cond_str


def parse_price(val):
    """Parses numeric price from float, int, or currency formatted string."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    val_str = str(val).replace(',', '').replace('฿', '').strip()
    match = re.search(r'(\d+(?:\.\d+)?)', val_str)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def extract_subcategories_from_html(html_str: str, base_category_url: str) -> list[str]:
    """
    Extracts all subcategory URLs under the given category from Next.js state
    and category badge/pill links in the DOM.
    """
    soup = BeautifulSoup(html_str, "html.parser")
    found_urls = set()

    clean_base_path = urlparse(base_category_url).path.rstrip('/')

    # 1. From __NEXT_DATA__
    next_tag = soup.find("script", id="__NEXT_DATA__")
    if next_tag and next_tag.string:
        try:
            data = json.loads(next_tag.string)
            cat_data = data.get("props", {}).get("pageProps", {}).get("categoryData", {})
            
            # Subcategories from categoryData.categories or catBars
            categories_list = cat_data.get("categories", []) or []
            cat_bars = cat_data.get("catBars", []) or []
            
            for item in categories_list + cat_bars:
                url_key = item.get("url_key") or item.get("urlKey") or item.get("url") or item.get("path")
                if url_key:
                    if not url_key.startswith("http"):
                        full_u = f"https://www.tops.co.th/en/{url_key.lstrip('/')}"
                    else:
                        full_u = url_key
                    if clean_base_path in urlparse(full_u).path:
                        found_urls.add(full_u.split("?")[0])
        except Exception as e:
            print(f"  -> Subcategory extraction note: {e}")

    # 2. From DOM category badge / pill links
    badge_links = soup.find_all("a", href=lambda h: h and clean_base_path in h)
    for a in badge_links:
        href = a.get("href")
        if href:
            full_url = urljoin("https://www.tops.co.th", href).split("?")[0].rstrip('/')
            # Ensure it's a direct sub-category (longer path than base)
            if urlparse(full_url).path != clean_base_path and clean_base_path in urlparse(full_url).path:
                found_urls.add(full_url)

    sorted_subcats = sorted(list(found_urls))
    return sorted_subcats


def extract_products_from_soup(html_or_soup) -> tuple[list, int, int]:
    """
    Extracts products, prices, and promotional conditions for the category.
    Strictly filters out non-category recommendation carousels and sliders.
    Returns: (list_of_products, total_products_in_catalog, page_limit)
    """
    if isinstance(html_or_soup, str):
        soup = BeautifulSoup(html_or_soup, "html.parser")
    else:
        soup = html_or_soup

    products_map = {}
    total_products = 0
    page_limit = 40

    # 1. Parse __NEXT_DATA__ (Strictly from categoryData.products)
    next_tag = soup.find("script", id="__NEXT_DATA__")
    if next_tag and next_tag.string:
        try:
            data = json.loads(next_tag.string)
            page_props = data.get("props", {}).get("pageProps", {})
            cat_data = page_props.get("categoryData", {})
            total_products = cat_data.get("totalProducts", 0)
            page_limit = cat_data.get("pageLimit", 40)
            
            # STRICT: only read category products, never generic productData or recommendations
            raw_products = cat_data.get("products", []) or []

            for item in raw_products:
                name = item.get("name") or item.get("title")
                if not name or "tops.co.th" in str(name).lower() or len(str(name)) < 3:
                    continue
                name = str(name).strip()

                badges = []
                for b_key in [
                    "badge", "badges", "promotion", "promotions",
                    "promotion_badge", "promotion_badges", "promotion_label",
                    "promotion_tag", "tag", "tags"
                ]:
                    b_val = item.get(b_key)
                    if isinstance(b_val, str) and b_val.strip():
                        badges.append(b_val.strip())
                    elif isinstance(b_val, list):
                        for sub_b in b_val:
                            if isinstance(sub_b, str) and sub_b.strip():
                                badges.append(sub_b.strip())
                            elif isinstance(sub_b, dict):
                                lbl = sub_b.get("label") or sub_b.get("text") or sub_b.get("title")
                                if lbl:
                                    badges.append(str(lbl).strip())
                    elif isinstance(b_val, dict):
                        lbl = b_val.get("label") or b_val.get("text") or b_val.get("title")
                        if lbl:
                            badges.append(str(lbl).strip())

                clean_badges = [clean_promo_condition(b) for b in badges]
                condition = " | ".join([b for b in clean_badges if b]) or None

                price = item.get("price") or item.get("final_price") or item.get("sale_price")
                special_price = (
                    item.get("special_price")
                    or item.get("promotion_price")
                    or item.get("specialPrice")
                    or item.get("promo_price")
                )
                original_price = (
                    item.get("original_price")
                    or item.get("originalPrice")
                    or item.get("regular_price")
                    or item.get("regularPrice")
                )

                p_price = parse_price(special_price if special_price is not None else price)
                o_price = parse_price(original_price if original_price is not None else price)

                products_map[name] = {
                    "name": name,
                    "promotion_price": p_price,
                    "original_price": o_price,
                    "condition": condition
                }
        except Exception as e:
            print(f"  -> __NEXT_DATA__ parse note: {e}")

    # 2. DOM Parsing Fallback & Attribute Enrichment
    # Remove recommendation / cross-sell carousels before DOM extraction to avoid unrelated items
    for rec in soup.find_all(lambda tag: tag.name in ["div", "section", "aside"] and any(
        kw in str(tag.get("class", [])).lower() or kw in str(tag.get("id", "")).lower() or kw in str(tag.get("data-testid", "")).lower()
        for kw in ["recommend", "carousel", "slider", "recently-viewed", "cross-sell", "upsell", "swiper", "you-may-also-like", "popular-item"]
    )):
        rec.decompose()

    name_divs = soup.find_all(
        "div",
        class_=lambda c: c and "text-textblack" in c
    )

    for nd in name_divs:
        name = nd.get_text(strip=True)
        if not name or len(name) < 3 or "tops.co.th" in name.lower():
            continue

        # Climb to the immediate product card ancestor
        card = nd
        curr = nd
        for _ in range(6):
            parent = curr.parent
            if not parent or parent.name in ["body", "html", "main"]:
                break
            curr = parent
            has_price_elem = bool(
                curr.find("span", class_=lambda c: c and ("inline" in c or "hidden" in c or "line-through" in c or "text-confirmgreen" in c or "underline" in c))
                or re.search(r"\b\d{2,}\b", curr.get_text())
            )
            if has_price_elem:
                card = curr
                break

        # Extract price spans (Tops uses both inline lg:hidden and hidden lg:inline)
        price_spans = card.find_all("span", class_=lambda c: c and ("inline" in c or "hidden" in c))
        raw_prices = []
        for s in price_spans:
            txt = s.get_text(strip=True)
            p = parse_price(txt)
            if p is not None and p > 0:
                is_strike = "line-through" in str(s.get("class", []))
                raw_prices.append((p, is_strike))

        # Fallback leaf check for standalone numbers
        if not raw_prices:
            for elem in card.find_all(["span", "div", "p"]):
                if elem.find(["span", "div", "p"]):
                    continue
                txt = elem.get_text(strip=True)
                if re.search(r"^\s*฿?\s*\d+(?:,\d+)*(?:\.\d+)?\s*$", txt):
                    p = parse_price(txt)
                    if p is not None and p > 0:
                        is_strike = "line-through" in str(elem.get("class", []))
                        raw_prices.append((p, is_strike))

        # Deduplicate price readings
        unique_prices = []
        for p, is_strike in raw_prices:
            if not any(abs(u[0] - p) < 0.01 and u[1] == is_strike for u in unique_prices):
                unique_prices.append((p, is_strike))

        promo_price = None
        orig_price = None

        strike_prices = [u[0] for u in unique_prices if u[1]]
        normal_prices = [u[0] for u in unique_prices if not u[1]]

        if strike_prices:
            orig_price = max(strike_prices)
            if normal_prices:
                promo_price = min(normal_prices)
            else:
                promo_price = min(strike_prices)
        elif len(normal_prices) >= 2:
            promo_price = min(normal_prices)
            orig_price = max(normal_prices)
        elif len(normal_prices) == 1:
            orig_price = normal_prices[0]
            promo_price = None

        # Condition / Promotion tag in DOM (e.g. "Buy 2 Pay 1", "Buy 1 Get 1", "1 แถม 1")
        cond_elem = card.find(
            lambda tag: tag.name in ["span", "div", "p", "a"] and (
                "text-confirmgreen" in str(tag.get("class", [])) or
                "text-alertred" in str(tag.get("class", [])) or
                "underline" in str(tag.get("class", [])) or
                "promo" in str(tag.get("class", []))
            ) and len(tag.get_text(strip=True)) > 2 and clean_promo_condition(tag.get_text(strip=True)) is not None
        )
        dom_condition = clean_promo_condition(cond_elem.get_text(strip=True)) if cond_elem else None

        if name not in products_map:
            products_map[name] = {
                "name": name,
                "promotion_price": promo_price,
                "original_price": orig_price,
                "condition": dom_condition
            }
        else:
            if dom_condition and not products_map[name]["condition"]:
                products_map[name]["condition"] = dom_condition
            if promo_price is not None and products_map[name]["promotion_price"] is None:
                products_map[name]["promotion_price"] = promo_price
            if orig_price is not None and products_map[name]["original_price"] is None:
                products_map[name]["original_price"] = orig_price

    return list(products_map.values()), total_products, page_limit


# ---------------------------------------------------------------------
# Subcategory & Category Page Scraper (Infinite Retry Queue)
# ---------------------------------------------------------------------
async def scrape_single_category_url(
    target_url: str,
    max_scrolls: int = 15,
    max_pages: int = 50
) -> tuple[list, list[str]]:
    """
    Scrapes all pages of a specific category/subcategory URL.
    Returns: (list_of_extracted_products, list_of_discovered_subcategories)
    """
    extracted_items = []
    discovered_subcategories = []
    seen_in_scope = set()

    clean_base_url = target_url.split("?")[0].rstrip('/')
    page_num = 1
    total_products_expected = None
    page_limit = 40
    empty_page_streak = 0

    while page_num <= max_pages:
        page_url = f"{clean_base_url}?page={page_num}" if page_num > 1 else clean_base_url
        page_success = False
        attempt = 0

        while not page_success:
            attempt += 1
            pre_wait = random.uniform(PRE_REQUEST_DELAY_MIN, PRE_REQUEST_DELAY_MAX)
            print(f"\n[Page {page_num}] Opening: {page_url} (Attempt {attempt})")
            print(f"  -> Human-like pause: {pre_wait:.1f}s...")
            await asyncio.sleep(pre_wait)

            async with async_playwright() as p:
                browser = await launch_browser(p)
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="Asia/Bangkok",
                )
                await context.add_cookies([
                    {'name': 'language', 'value': 'en', 'domain': '.tops.co.th', 'path': '/'},
                    {'name': 'NEXT_LOCALE', 'value': 'en', 'domain': '.tops.co.th', 'path': '/'}
                ])
                page = await context.new_page()

                # Homepage warm-up on initial request
                if page_num == 1 and attempt == 1:
                    try:
                        print("  -> Session warm-up on Tops homepage...")
                        await page.goto("https://www.tops.co.th/en/", wait_until="domcontentloaded", timeout=60000)
                        await asyncio.sleep(4)
                    except Exception as e:
                        print(f"  -> Warm-up note: {e}")

                try:
                    print(f"  -> Navigating to {page_url}...")
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=90000)
                    await asyncio.sleep(CF_INITIAL_WAIT)

                    resolved = await handle_cloudflare_challenge(page)
                    if not resolved:
                        print("  -> ⚠️ Cloudflare challenge unresolved.")
                    else:
                        await asyncio.sleep(CF_POST_RESOLVE_WAIT)

                        # Scroll down to hydrate product cards
                        for s in range(1, max_scrolls + 1):
                            await page.keyboard.press("PageDown")
                            await asyncio.sleep(0.8)
                            await page.evaluate("window.scrollBy(0, 1200)")
                            await asyncio.sleep(1.2)

                        html_content = await page.content()

                        # On page 1, discover all subcategories under this parent category
                        if page_num == 1:
                            discovered_subcategories = extract_subcategories_from_html(html_content, clean_base_url)
                            if discovered_subcategories:
                                print(f"  🔍 Discovered {len(discovered_subcategories)} subcategories:")
                                for sc in discovered_subcategories:
                                    print(f"     - {sc}")

                        # Extract products
                        page_items, total_count, p_limit = extract_products_from_soup(html_content)

                        if total_count > 0 and total_products_expected is None:
                            total_products_expected = total_count
                            page_limit = p_limit or 40
                            est_pages = math.ceil(total_products_expected / page_limit)
                            print(f"  📊 Inventory: {total_products_expected} items in this category/subcategory across ~{est_pages} pages")

                        new_count = 0
                        for prod in page_items:
                            if prod["name"] not in seen_in_scope:
                                seen_in_scope.add(prod["name"])
                                extracted_items.append(prod)
                                new_count += 1

                        print(f"  ✅ Page {page_num} finished: {new_count} new items (Scope total: {len(extracted_items)})")

                        if new_count == 0:
                            empty_page_streak += 1
                        else:
                            empty_page_streak = 0

                        page_success = True

                except Exception as e:
                    print(f"  -> ❌ Error on page {page_num}: {e}")

                finally:
                    if browser.is_connected():
                        await browser.close()

            if not page_success:
                cooldown = calc_cooldown(attempt)
                print(f"  [!] Retrying page {page_num}. Cooldown wait: {cooldown}s...\n")
                await asyncio.sleep(cooldown)

        if empty_page_streak >= 2:
            print(f"  🏁 End of pages reached for {clean_base_url} at page {page_num}.")
            break

        if total_products_expected and len(extracted_items) >= total_products_expected:
            print(f"  🏁 All {total_products_expected} items collected for {clean_base_url}.")
            break

        page_num += 1

    return extracted_items, discovered_subcategories


# ---------------------------------------------------------------------
# Full Catalog Scraper (With Subcategory Expansion & Deduplication)
# ---------------------------------------------------------------------
async def scrape_tops_entire_catalog(root_urls: list) -> pl.DataFrame:
    """
    Scrapes the entire Tops catalog:
    1. Opens each root category URL.
    2. Auto-discovers all subcategories under it (e.g. Fabric Softener, Liquid Detergent, etc.).
    3. Scrapes every subcategory through all paginated pages.
    4. Deduplicates across overlapping subcategories.
    """
    all_unique_products = []
    global_seen_names = set()

    for root_idx, root_url in enumerate(root_urls, start=1):
        print("\n" + "=" * 70)
        print(f"[{root_idx}/{len(root_urls)}] Initiating Full Category Scrape: {root_url}")
        print("=" * 70)

        # Step 1: Scrape the root category and extract its subcategories
        root_items, subcats = await scrape_single_category_url(root_url)

        for it in root_items:
            if it["name"] not in global_seen_names:
                global_seen_names.add(it["name"])
                all_unique_products.append(it)

        print(f"\nRoot Category '{root_url}' scraped: {len(all_unique_products)} items so far.")

        # Step 2: If subcategories exist, scrape each subcategory to guarantee full inventory
        if subcats:
            print(f"\n--- Expanding {len(subcats)} Subcategories for 100% Inventory ---")
            for s_idx, sub_url in enumerate(subcats, start=1):
                print(f"\n>> [{s_idx}/{len(subcats)}] Scraping Subcategory: {sub_url}")
                sub_items, _ = await scrape_single_category_url(sub_url)

                added = 0
                for it in sub_items:
                    if it["name"] not in global_seen_names:
                        global_seen_names.add(it["name"])
                        all_unique_products.append(it)
                        added += 1

                print(f"  ↳ Subcategory finished: {added} new unique items added (Running Total: {len(all_unique_products)})")

    df_raw = pl.DataFrame(all_unique_products)
    return df_raw


# ---------------------------------------------------------------------
# Data Cleaning & Feature Extraction
# ---------------------------------------------------------------------
def re_evaluate_price(df: pl.DataFrame) -> pl.DataFrame:
    """
    1. Casts prices to float.
    2. Swaps original & promotion if original < promotion.
    3. Fills missing original with promotion.
    4. Nullifies redundant promotion when equal to original.
    """
    if df.is_empty():
        return df

    df = (
        df.with_columns([
            pl.col("promotion_price").cast(pl.Float64, strict=False),
            pl.col("original_price").cast(pl.Float64, strict=False)
        ])
        .with_columns(
            pl.when(
                pl.col("original_price").is_null()
                & pl.col("promotion_price").is_not_null()
            )
            .then(pl.col("promotion_price"))
            .otherwise(pl.col("original_price"))
            .alias("original_price")
        )
        .with_columns(
            pl.max_horizontal("original_price", "promotion_price").alias("original_price"),
            pl.min_horizontal("original_price", "promotion_price").alias("temp_promo")
        )
        .with_columns(
            pl.when(pl.col("temp_promo") == pl.col("original_price"))
            .then(None)
            .otherwise(pl.col("temp_promo"))
            .alias("promotion_price")
        )
        .drop("temp_promo")
    )
    return df


def parse_product_names(df: pl.DataFrame, shop_name: str = "Tops") -> pl.DataFrame:
    """Extracts Date, Brand, Volume, Unit, Pack, and Retailer from product name."""
    if df.is_empty():
        return df

    today_str = date.today().strftime("%Y-%m-%d")

    quant_unit_pattern = r"(?i)([\d.]+)\s*(ML|G|KG|L|LTR|LITERS?|GRAMS?)\b"

    pack_pattern = (
        r"(?i)(PACK\s*\d*\s*FREE\s*\d+|PACK\s*\d*\s*\+\s*\d+|"
        r"PACK\s*\d+|TWINPACK|\bX\s*\d+\s*\+\s*\d+|\bX\s*\d+\b|"
        r"P?\d+\s*\+\s*\d+|\(\d+\+\d+\)|\d+\s*FREE\s*\d+|\bPACK\b)"
    )

    return df.with_columns([
        pl.lit(today_str).alias("Date"),
        pl.col("name").str.split(" ").list.first().alias("Brand"),
        pl.col("name")
          .str.extract(quant_unit_pattern, 1)
          .cast(pl.Float64, strict=False)
          .alias("Volume"),
        pl.col("name")
          .str.extract(quant_unit_pattern, 2)
          .str.to_uppercase()
          .alias("Unit"),
        pl.col("name")
          .str.extract(pack_pattern, 1)
          .str.to_uppercase()
          .alias("Pack"),
        pl.lit(shop_name).alias("Retailer")
    ])


# ---------------------------------------------------------------------
# Target Category URLs
# ---------------------------------------------------------------------
default_category_urls = [
    "https://www.tops.co.th/en/household-and-pet/laundry",
]


# ---------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------
async def run_pipeline(urls: list = None):
    print("=" * 70)
    print("Tops Entire Category Scraper (Full Subcategory Expansion & Inventory)")
    print("=" * 70)

    target_urls = urls or default_category_urls

    print(f"\nStarting entire category scrape for:")
    for u in target_urls:
        print(f"  - {u}")

    raw_df = await scrape_tops_entire_catalog(target_urls)

    if raw_df.is_empty():
        print("\n⚠️ No data collected from category scraping. Exiting.")
        return

    print(f"\n--- Total Scraped: {len(raw_df)} unique records ---")

    # ---------- Transform & Clean ----------
    df_prep = re_evaluate_price(raw_df)
    df_transformed = parse_product_names(df_prep, "Tops")
    df_final = df_transformed.unique(subset=["name"], maintain_order=True)

    print("\n--- Sample Scraped Records (Head 15) ---")
    print(df_final.head(15))

    # ---------- Export to Excel ----------
    output_filename = OUTPUT_DIR / f"tops_category_{today_date}.xlsx"
    df_final.write_excel(str(output_filename))
    print(f"\n✅ Successfully saved {len(df_final)} items to: {output_filename}")

    print("\n" + "=" * 70)
    print("Entire category scraping pipeline completed.")
    print("=" * 70)


def main():
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()

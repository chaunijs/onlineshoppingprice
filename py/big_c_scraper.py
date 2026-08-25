# -*- coding: utf-8 -*-
"""
Big C (Cloudflare) Scraper - Scrapling + Patchright Edition
Version: 2.0
Date: 2026-07-03
Changelog:
    - v2.0: Refactored for local + GitHub Actions
        * Removed Colab/IPython dependencies (display, subprocess installer)
        * Removed apt-get/pip subprocess calls
        * Removed top-level `await` (moved into async main)
        * Removed nest_asyncio (not needed outside Jupyter)
        * Uses system Chrome (channel="chrome") to bypass corporate SSL
        * Added output directory + proper main() entry point
        * Added fresh browser + fast reload strategy for watchlist
        * OCR is optional (fallback if easyocr unavailable)
"""

import os
import io
import re
import asyncio
import random
import datetime
from datetime import date
from pathlib import Path

import polars as pl
import requests
import numpy as np
from PIL import Image
from bs4 import BeautifulSoup

from scrapling.fetchers import StealthyFetcher
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

USE_SYSTEM_CHROME = os.getenv("USE_SYSTEM_CHROME", "true").lower() == "true"
ENABLE_OCR = os.getenv("ENABLE_OCR", "true").lower() == "true"


# ---------------------------------------------------------------------
# Browser Launch Helper
# ---------------------------------------------------------------------
async def launch_browser(p):
    """Launches Chromium with fallback to system Chrome for corporate networks."""
    launch_kwargs = {
        "headless": True,
        "args": ["--disable-blink-features=AutomationControlled"]
    }
    if USE_SYSTEM_CHROME:
        launch_kwargs["channel"] = "chrome"
    return await p.chromium.launch(**launch_kwargs)


# ---------------------------------------------------------------------
# OCR Setup (Lazy)
# ---------------------------------------------------------------------
_ocr_reader = None


def get_ocr_reader():
    """Lazy-load easyocr only when needed."""
    global _ocr_reader
    if _ocr_reader is None and ENABLE_OCR:
        try:
            import easyocr
            print("🔄 Initializing OCR Engine (first-time load)...")
            _ocr_reader = easyocr.Reader(['en', 'th'])
        except ImportError:
            print("⚠️ easyocr not installed. Skipping OCR badge detection.")
            return None
    return _ocr_reader


def get_condition_from_text(raw_text: str):
    """
    Handles dynamic patterns: Buy 2/3/4/5 Get 1 and Buy 2/3/4 Cheaper.
    Also normalizes Thai keywords and common OCR typos.
    """
    t = raw_text.upper().replace(" ", "")
    t = t.replace("BUV", "BUY")  # Fix common OCR typo

    digits = re.findall(r'\d', t)
    n = digits[0] if digits else ""

    # Buy N Get 1 (Priority over general save)
    if any(k in t for k in ["GET", "แถม"]):
        if n:
            if n == "1" or "1แถม1" in t or "1GET1" in t:
                return "Buy 1 Get 1"
            return f"Buy {n} Get 1"
        return "Buy 1 Get 1"

    # Buy N Cheaper
    if "CHEAPER" in t:
        if n:
            return f"Buy {n} Cheaper"
        return "Buy 2 Cheaper"

    # Supersave
    if any(k in t for k in ["SUPERSAVE", "SAVE", "ประหยัด"]):
        return "Super Save"

    return raw_text.strip() if raw_text.strip() else None


def run_ocr_on_badges(badge_urls: list) -> dict:
    """Downloads badge images, upscales, OCRs and maps to condition labels."""
    reader = get_ocr_reader()
    badge_map = {}

    if reader is None or not badge_urls:
        return badge_map

    print(f"🔍 Processing {len(badge_urls)} unique badges via OCR...")

    for url in badge_urls:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                img = Image.open(io.BytesIO(response.content)).convert('RGB')
                # Upscale for OCR accuracy
                img = img.resize(
                    (img.width * 4, img.height * 4),
                    resample=Image.LANCZOS
                )
                img_array = np.array(img)

                results = reader.readtext(img_array)
                raw_text = " ".join([res[1] for res in results])

                label = get_condition_from_text(raw_text)
                badge_map[url] = label
                print(f"  ✅ {url[-30:]} -> {label}")
            else:
                badge_map[url] = None
        except Exception as e:
            print(f"  ❌ Error on {url[-30:]}: {e}")
            badge_map[url] = None

    return badge_map


# ---------------------------------------------------------------------
# Data Prep & Transformation
# ---------------------------------------------------------------------
def re_evaluate_price(df: pl.DataFrame) -> pl.DataFrame:
    """
    1. If original_price is missing, fill from promotion_price.
    2. If promotion_price == original_price, nullify promotion.
    """
    return (
        df.with_columns(
            pl.when(
                pl.col("original_price").is_null()
                & pl.col("promotion_price").is_not_null()
            )
            .then(pl.col("promotion_price"))
            .otherwise(pl.col("original_price"))
            .alias("original_price")
        )
        .with_columns(
            pl.when(pl.col("promotion_price") == pl.col("original_price"))
            .then(None)
            .otherwise(pl.col("promotion_price"))
            .alias("promotion_price")
        )
    )


def parse_product_names(df: pl.DataFrame, shop_name: str) -> pl.DataFrame:
    """Standardize columns: Brand, Volume, Unit, Pack, Retailer."""
    today_str = date.today().strftime("%Y-%m-%d")

    quant_unit_pattern = r"(?i)([\d.]+)\s*(ML|G|KG|L|GRAMS?)"

    pack_pattern = (
        r"(?i)(PACK\s*\d*\s*FREE\s*\d+|PACK\s*\d*\s*\+\s*\d+|"
        r"PACK\s*\d+|TWINPACK|\bX\s*\d+\b|"
        r"P?\d+\s*\+\s*\d+|\(\d+\+\d+\)|\d+\s*FREE\s*\d+|\bPACK\b)"
    )

    return df.with_columns(
        pl.lit(today_str).alias("Date"),
        pl.col("name").str.split(" ").list.first().alias("Brand"),
        pl.col("name")
          .str.extract(quant_unit_pattern, 1)
          .str.replace_all(",", "")
          .cast(pl.Int64, strict=False)
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
    )


# ---------------------------------------------------------------------
# Category Scraper (Multi-Page via Scrapling)
# ---------------------------------------------------------------------
async def scrape_bigc_multi_pages(base_url_list: list) -> pl.DataFrame:
    """
    Scrapes Big C category pages using Scrapling's StealthyFetcher.
    Auto-solves Cloudflare on each request.
    """
    all_data = []

    en_cookies = [
        {'name': 'language', 'value': 'en',
         'domain': '.bigc.co.th', 'path': '/'},
        {'name': 'NEXT_LOCALE', 'value': 'en',
         'domain': '.bigc.co.th', 'path': '/'}
    ]
    en_headers = {'Accept-Language': 'en-US,en;q=0.9'}

    print(f"🚀 Multi-Category Scrape ({len(base_url_list)} categories)...")

    for base_url in base_url_list:
        current_page = 1
        print(f"\n📂 Category: {base_url}")

        while True:
            separator = "&" if "?" in base_url else "?"
            clean_url = base_url.split(f"{separator}page=")[0]
            target_url = f"{clean_url}{separator}page={current_page}"

            print(f"  📄 Page {current_page}...")

            try:
                page = await StealthyFetcher.async_fetch(
                    target_url,
                    headless=True,
                    solve_cloudflare=True,
                    cookies=en_cookies,
                    headers=en_headers,
                    timeout=60000,
                    network_idle=True
                )

                if page.status != 200:
                    print(f"  🛑 Stopped (Status: {page.status})")
                    break

                containers = page.css('div[class*="productCard_container"]')
                if not containers:
                    print(f"  ✅ Category complete.")
                    break

                for item in containers:
                    name = item.css(
                        'div[class*="productCard_title"] a::text'
                    ).get()
                    sale_price = item.css(
                        'span[class*="productCard_sale_price"]::text'
                    ).get()
                    original_price = item.css(
                        'div[class*="productCard_base_price"]::text'
                    ).get()
                    badge_url = item.css(
                        'div[class*="productCard_badge"] img::attr(src)'
                    ).get()

                    all_data.append({
                        "product_name": name.strip() if name else "N/A",
                        "sale_price": (
                            sale_price.strip() if sale_price else "N/A"
                        ),
                        "original_price": (
                            original_price.strip().replace('฿', '')
                            if original_price else "N/A"
                        ),
                        "condition": None,
                        "badge_url": badge_url if badge_url else "null"
                    })

                current_page += 1
                await asyncio.sleep(1)

            except Exception as e:
                print(f"  ❌ Error at page {current_page}: {e}")
                break

    return pl.DataFrame(all_data) if all_data else pl.DataFrame()


# ---------------------------------------------------------------------
# Watchlist Scraper (Cloudflare-Aware, Serial Optimized)
# ---------------------------------------------------------------------
async def scrape_bigc_watchlist_optimized(urls: list) -> pl.DataFrame:
    """
    Optimized watchlist scraper.
    - Fresh browser per URL (prevents CF flag accumulation)
    - 2 fast reload attempts per URL
    - Adaptive cooldowns
    """
    extracted_data = []
    queue = [(url, 1) for url in urls]
    successful_urls = set()
    start_time = datetime.datetime.now()

    print(f"\n🚀 Watchlist scrape for {len(urls)} products...")
    print("Strategy: Fresh browser + Fast reload + Adaptive cooldowns.\n")

    total_attempts_cap = len(urls) * 6
    total_attempts = 0

    while queue and total_attempts < total_attempts_cap:
        total_attempts += 1
        current_url, attempt = queue.pop(0)

        # Guard against merged URLs (missing comma bug)
        if current_url.count("https://") > 1:
            print(f"  ❌ SYNTAX: Merged URL detected. Skipping.")
            continue

        url_short = current_url.split('/')[-1][:50]
        print(f"[{len(successful_urls)}/{len(urls)}] {url_short}")
        print(f"  → Attempt {attempt} | Queue: {len(queue) + 1}")

        success = False

        # FRESH BROWSER PER URL - prevents CF flag accumulation
        async with async_playwright() as p:
            browser = await launch_browser(p)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="Asia/Bangkok",
            )
            await context.add_cookies([
                {'name': 'language', 'value': 'en',
                 'domain': '.bigc.co.th', 'path': '/'},
                {'name': 'NEXT_LOCALE', 'value': 'en',
                 'domain': '.bigc.co.th', 'path': '/'}
            ])
            page = await context.new_page()

            try:
                # ---------- INITIAL LOAD ----------
                await page.goto(
                    current_url,
                    wait_until="domcontentloaded",
                    timeout=60000
                )
                try:
                    await page.wait_for_selector("h1", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(3)

                html_content = await page.content()
                soup = BeautifulSoup(html_content, "html.parser")

                name_elem = soup.find("h1")
                name = (
                    name_elem.text.strip()
                    if name_elem else "Unknown Product"
                )

                is_blocked = (
                    "Just a moment" in name
                    or "Cloudflare" in html_content
                    or "Attention Required" in html_content
                )

                # ---------- FAST RELOAD BYPASS ----------
                if is_blocked:
                    for reload_try in range(2):
                        print(f"  → CF detected. Reload {reload_try + 1}/2...")
                        await asyncio.sleep(5)
                        try:
                            await page.reload(
                                wait_until="domcontentloaded",
                                timeout=60000
                            )
                            await asyncio.sleep(5)

                            html_content = await page.content()
                            soup = BeautifulSoup(html_content, "html.parser")
                            name_elem = soup.find("h1")
                            name = (
                                name_elem.text.strip()
                                if name_elem else "Unknown Product"
                            )

                            is_blocked = (
                                "Just a moment" in name
                                or "Cloudflare" in html_content
                                or "Attention Required" in html_content
                            )
                            if not is_blocked:
                                print("  → Cloudflare resolved!")
                                break
                        except Exception as e:
                            print(f"  → Reload error: {str(e)[:60]}")

                if is_blocked:
                    raise Exception("Still blocked after 2 reloads")

                if name == "Unknown Product":
                    raise Exception("Product title not found (out of stock?)")

                # ---------- PARSE ----------
                promo_price = None
                orig_price = None

                price_div = soup.find(
                    "div",
                    class_=lambda x: (
                        x and "productDetail_product_price_new" in x
                    )
                )
                if price_div:
                    base_div = price_div.find(
                        "div",
                        class_=lambda x: (
                            x and "productDetail_product_baseprice" in x
                        )
                    )
                    if base_div:
                        orig_price = (
                            base_div.text.replace('฿', '')
                            .replace(',', '').strip()
                        )
                        base_div.extract()

                    unit_span = price_div.find(
                        "span",
                        class_=lambda x: x and "productDetail_unit" in x
                    )
                    if unit_span:
                        unit_span.extract()

                    promo_price = (
                        price_div.text.replace('฿', '')
                        .replace(',', '').strip()
                    )

                badge_url = "null"
                badge_div = soup.find(
                    "div",
                    class_=lambda x: x and "imageSlider_badge_warpper" in x
                )
                if badge_div:
                    badge_img = badge_div.find("img")
                    if badge_img and badge_img.has_attr('src'):
                        badge_url = badge_img['src']

                extracted_data.append({
                    "url": current_url,
                    "name": name,
                    "promotion_price": promo_price,
                    "original_price": orig_price,
                    "condition": None,
                    "badge_url": badge_url
                })
                successful_urls.add(current_url)
                print(f"  ✅ ฿{promo_price} | {name[:50]}")
                success = True

            except Exception as e:
                print(f"  ❌ Error: {str(e)[:80]}")

            finally:
                if browser.is_connected():
                    await browser.close()

        # ---------- ADAPTIVE QUEUE MANAGEMENT ----------
        if not success:
            queue.append((current_url, attempt + 1))
            if attempt >= 5:
                cooldown = 25
                print(f"  💤 High-retry. {cooldown}s cooldown...\n")
            elif attempt >= 3:
                cooldown = 15
                print(f"  💤 Moderate retry. {cooldown}s cooldown...\n")
            elif len(queue) <= 2:
                cooldown = 10
                print(f"  💤 Small queue. {cooldown}s cooldown...\n")
            else:
                cooldown = random.uniform(4, 6)
                print(f"  💤 {cooldown:.1f}s cooldown...\n")
            await asyncio.sleep(cooldown)
        else:
            if queue:
                await asyncio.sleep(random.uniform(3, 5))

    # ---------- SUMMARY ----------
    elapsed = (datetime.datetime.now() - start_time).total_seconds()
    print(f"\n{'=' * 60}")
    print(f"📊 Watchlist Summary")
    print(f"{'=' * 60}")
    print(f"  Success: {len(successful_urls)}/{len(urls)} URLs")
    print(f"  Total attempts: {total_attempts}")
    print(f"  Elapsed: {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"{'=' * 60}\n")

    if not extracted_data:
        print("⚠️ No data collected.")
        return pl.DataFrame()

    df = pl.DataFrame(extracted_data)
    df = df.with_columns([
        pl.col("url").cast(pl.String),
        pl.col("name").cast(pl.String),
        pl.col("promotion_price").cast(pl.Float64, strict=False),
        pl.col("original_price").cast(pl.Float64, strict=False),
        pl.col("condition").cast(pl.String, strict=False),
        pl.col("badge_url").cast(pl.String)
    ])
    return df


# ---------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------
category_list = [
    "https://www.bigc.co.th/en/category/laundry?brand=187%2C188%2C249%2C186%2C256&limit=100",
    "https://www.bigc.co.th/en/category/dishwashing-liquid?limit=100",
]

watchlist_urls = [
    "https://www.bigc.co.th/en/product/fineline-liquid-laundry-detergent-sunny-gold-scent-550-ml.3791984",
    "https://www.bigc.co.th/en/product/fineline-plus-liquid-laundry-detergent-sunny-gold-scent-1250-ml.2155497",
    "https://www.bigc.co.th/en/product/hygiene-expert-wash-concentrate-liquid-detergent-milky-touch-600-ml.6394917",
    "https://www.bigc.co.th/en/product/hygiene-expert-wash-concentrate-liquid-detergent-milky-touch-1400-ml.6394919",
    "https://www.bigc.co.th/en/product/pao-win-wash-liquid-detergent-620-ml.12782",
    "https://www.bigc.co.th/en/product/pao-win-wash-concentrated-liquid-detergent-formula-1300-ml.34065",
    "https://www.bigc.co.th/en/product/pao-super-white-laundry-detergent-1800-g.5977",
    "https://www.bigc.co.th/en/product/pao-super-white-detergent-2400-g.3520",
    "https://www.bigc.co.th/en/product/attack-easy-detergent-happy-sweet-2500-g.47463",
    "https://www.bigc.co.th/en/product/hygiene-fabric-softener-expert-care-milky-touch-480-ml.12003",
    "https://www.bigc.co.th/en/product/hygiene-expert-care-concentrated-fabric-softener-milky-touch-scent-1000-ml-pack-2.1953035",
    "https://www.bigc.co.th/en/product/lipon-f-dishwashing-liquid-hygienic-formula-500-ml-pack-3.3639",
    "https://www.bigc.co.th/en/product/lipon-f-dishwashing-liquid-hygienic-formula-3200-ml.673",
    "https://www.bigc.co.th/en/product/pro-blue-plus-powder-laundry-detergent-standard-formula-2400-g.501",
    "https://www.bigc.co.th/en/product/lipon-f-sanitary-formula-dish-washing-liquid-refill-750-ml-pack-of-2.78902",
    "https://www.bigc.co.th/en/product/hygiene-fabric-softener-expert-care-tender-touch-480-ml-pack-2-free-1.32428",
    "https://www.bigc.co.th/en/product/attack-ez-conventional-detergent-happy-sweet-scent-1700-g.47863",
]

list_to_search = [
    'FINELINE Liquid Laundry Detergent Sunny Gold Scent 550 ml.',
    'FINELINE Plus Liquid Laundry Detergent Sunny Gold Scent 1250 ml.',
    'HYGIENE Expert Wash Concentrate Liquid Detergent Milky Touch 600 ml.',
    'HYGIENE Expert Wash Concentrate Liquid Detergent Milky Touch 1400 ml.',
    'PAO Win Wash Liquid Laundry Detergent 620 ml.',
    'PAO Win Wash Liquid Laundry Detergent 1300 ml.',
    'PAO Super White Laundry Detergent 1800 g.',
    'PAO Super White Powder Laundry Detergent 2400 g.',
    'ATTACK EASY DETERGENT HAPPY SWEET 2500 G',
    'HYGIENE Expert Care Concentrated Fabric Softener Milky Touch Scent 480 ml.',
    'HYGIENE Expert Care Concentrated Fabric Softener Milky Touch Scent 1000 ml. Pack 2',
    'LIPON F Dishwashing Liquid Hygienic Formula 500 ml. Pack 3',
    'LIPON F Dishwashing Liquid Hygienic Formula 3200 ml.',
    'PRO Blue Plus Powder Laundry Detergent 2400 g.',
    'LIPON F Sanitary Formula Dish Washing Liquid Refill 750 ml. Pack of 2',
    'HYGIENE Expert Care Concentrated Fabric Softener Milky Touch Scent 480 ml. Pack 2+1',
    'ATTACK Easy Conventional Detergent Happy Sweet Pink 1.7 kg x 1+1',
    'ATTACK EZ Conventional Detergent Happy Sweet Scent 1700 g.',
]


# ---------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------
async def run_pipeline():
    print("=" * 60)
    print("Big C Scraper (Cloudflare-Aware)")
    print("=" * 60)

    # ---------- CATEGORY SCRAPING ----------
    print("\n--- Scraping Category Catalog ---")
    final_df = await scrape_bigc_multi_pages(category_list)
    print(f"🏁 Category scrape done: {len(final_df)} products")

    # ---------- OCR BADGES (CATEGORY) ----------
    if not final_df.is_empty() and "badge_url" in final_df.columns:
        unique_urls = [
            u for u in final_df["badge_url"].unique().to_list()
            if u and u != "null"
        ]
        if unique_urls:
            badge_map = run_ocr_on_badges(unique_urls)
            badge_map["null"] = None
            if badge_map:
                final_df = final_df.with_columns(
                    pl.col("badge_url")
                      .replace(badge_map, default=None)
                      .alias("condition")
                )

    # ---------- TRANSFORM + SAVE CATEGORY ----------
    if not final_df.is_empty():
        df_big_c_sel = final_df.select([
            pl.col("product_name").alias("name"),
            pl.col("sale_price")
              .cast(pl.Float64, strict=False)
              .alias("promotion_price"),
            pl.col("original_price")
              .cast(pl.Float64, strict=False)
              .alias("original_price"),
            pl.col("condition")
        ])
        df_prep_big_c = re_evaluate_price(df_big_c_sel)
        df_trans_big_c = parse_product_names(df_prep_big_c, "BigC")

        cat_file = OUTPUT_DIR / f"big_c_result_{today_date}.xlsx"
        df_trans_big_c.write_excel(str(cat_file))
        print(f"✅ Saved category output: {cat_file}")

    # ---------- WATCHLIST SCRAPING ----------
    print("\n--- Scraping Watchlist ---")
    df_watchlist_final = await scrape_bigc_watchlist_optimized(watchlist_urls)

    if df_watchlist_final.is_empty():
        print("\n⚠️ No watchlist data. Skipping watchlist output.")
        return

    # ---------- OCR BADGES (WATCHLIST) ----------
    if "badge_url" in df_watchlist_final.columns:
        unique_urls = [
            u for u in df_watchlist_final["badge_url"].unique().to_list()
            if u and u != "null"
        ]
        if unique_urls:
            badge_map = run_ocr_on_badges(unique_urls)
            badge_map["null"] = None
            if badge_map:
                df_watchlist_final = df_watchlist_final.with_columns(
                    pl.col("badge_url")
                      .replace(badge_map, default=None)
                      .alias("condition")
                )

    # ---------- SEARCH RESULTS ----------
    search_df = pl.DataFrame({"product_name": list_to_search})
    search_results_df = search_df.join(
        df_watchlist_final.select(
            ["name", "original_price", "promotion_price"]
        ),
        left_on="product_name",
        right_on="name",
        how="left"
    )
    watchlist_names_set = set(df_watchlist_final["name"].to_list())
    search_results_df = search_results_df.with_columns(
        pl.col("product_name").is_in(watchlist_names_set).alias("Found")
    ).unique()

    print("\nSearch Results:")
    print(search_results_df)

    search_file = OUTPUT_DIR / f"search_result_big_c_{today_date}.xlsx"
    search_results_df.write_excel(str(search_file))
    print(f"✅ Saved search output: {search_file}")

    # ---------- TRANSFORM + SAVE WATCHLIST ----------
    df_watchlist_final = df_watchlist_final.select([
        pl.col("name"),
        pl.col("promotion_price").cast(pl.Float64, strict=False),
        pl.col("original_price").cast(pl.Float64, strict=False),
        pl.col("condition")
    ])
    df_prep_watchlist = re_evaluate_price(df_watchlist_final)
    df_trans_watchlist = parse_product_names(df_prep_watchlist, "BigC")

    watchlist_file = OUTPUT_DIR / f"big_c_watchlist_{today_date}.xlsx"
    df_trans_watchlist.write_excel(str(watchlist_file))
    print(f"✅ Saved watchlist output: {watchlist_file}")

    print("\n" + "=" * 60)
    print("Scraping completed.")
    print("=" * 60)


def main():
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()
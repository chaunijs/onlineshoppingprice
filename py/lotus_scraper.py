# -*- coding: utf-8 -*-
"""
Lotus's Online Scraper (Playwright)
Version: 2.0
Date: 2026-07-03
Changelog:
    - v2.0: Refactored for local + GitHub Actions
        * Removed Colab/IPython dependencies (display, subprocess installer)
        * Removed apt-get/pip subprocess calls
        * Removed top-level `await` (moved into async main)
        * Removed nest_asyncio (not needed outside Jupyter)
        * Removed @register_cell_magic
        * Uses system Chrome (channel="chrome") to bypass corporate SSL
        * Added output directory + proper main() entry point
        * Added adaptive cooldowns to watchlist scraper
"""

import os
import sys
import asyncio
import random
import datetime
from collections import deque
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import polars as pl
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


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
# Catalog Scraper (Infinite Scroll)
# ---------------------------------------------------------------------
async def scrape_lotuss_scroller(shop_url_list: list) -> pl.DataFrame:
    """
    Scrapes Lotus's category pages using infinite scroll.
    Uses a queue with retry for failed URLs.
    """
    all_extracted_data = []
    url_queue = deque([(url, 0) for url in shop_url_list])
    MAX_RETRIES = 3

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
        page = await context.new_page()

        while url_queue:
            shop_url, attempts = url_queue.popleft()
            print(f"\n🚀 Opening catalog: {shop_url} (Attempt {attempts + 1})")

            try:
                await page.goto(
                    shop_url,
                    wait_until="domcontentloaded",
                    timeout=60000
                )
                await page.wait_for_selector(
                    ".mui-style-17swnep",
                    timeout=15000
                )
            except Exception:
                print("⚠️ Failed to load products.")
                if attempts < MAX_RETRIES:
                    print("🔄 Re-queueing to try again later...")
                    url_queue.append((shop_url, attempts + 1))
                    await asyncio.sleep(2)
                else:
                    print(f"❌ Max retries reached. Skipping.")
                continue

            # ---------- INFINITE SCROLLER ----------
            previous_item_count = 0
            scroll_attempts = 0
            max_scrolls = 30

            while scroll_attempts < max_scrolls:
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                await asyncio.sleep(4)

                current_items = await page.query_selector_all(".MuiCard-root")
                current_count = len(current_items)

                print(f"  ↳ Scroll {scroll_attempts + 1}: {current_count} products")

                if current_count == previous_item_count:
                    print("  ↳ Reached bottom of catalog!")
                    break

                previous_item_count = current_count
                scroll_attempts += 1

            # ---------- EXTRACT ----------
            html_content = await page.content()
            soup = BeautifulSoup(html_content, "html.parser")

            products = soup.find_all("div", class_="MuiCard-root")

            for prod in products:
                name_elem = prod.find(class_="mui-style-17swnep")
                name = name_elem.text.strip() if name_elem else None
                if not name:
                    continue

                promo_elem = prod.find("div", class_="mui-style-18s6ztp")
                if promo_elem:
                    raw_promo = (
                        promo_elem.text.replace(',', '')
                        .replace('฿', '').strip()
                    )
                    promotion_price = raw_promo if raw_promo else None
                else:
                    promotion_price = None

                orig_elem = prod.find(
                    "div", class_=lambda c: c and ("mui-style-f59hd5" in c or "mui-style-4bdmr1" in c or "mui-style-1opqjiq" in c)
                ) or prod.find("div", class_="mui-style-f59hd5") or prod.find("div", class_="mui-style-4bdmr1")
                if orig_elem:
                    raw_orig = (
                        orig_elem.text.replace(',', '')
                        .replace('฿', '').strip()
                    )
                    original_price = raw_orig if raw_orig else promotion_price
                else:
                    original_price = promotion_price

                condition_elem = prod.find("div", class_="mui-style-axryyp")
                if condition_elem:
                    condition = " ".join(
                        condition_elem.get_text(" ").split()
                    )
                else:
                    condition = None

                all_extracted_data.append({
                    "name": name,
                    "promotion_price": promotion_price,
                    "original_price": original_price,
                    "condition": condition
                })

            await asyncio.sleep(3)

        await browser.close()

    df = pl.DataFrame(all_extracted_data)

    if not df.is_empty():
        df = df.with_columns([
            pl.col("name").cast(pl.String),
            pl.col("promotion_price").cast(pl.Float64, strict=False),
            pl.col("original_price").cast(pl.Float64, strict=False),
            pl.col("condition").cast(pl.String)
        ])
        df = df.unique(subset=["name"], maintain_order=True)

    return df


# ---------------------------------------------------------------------
# Watchlist Scraper (Sequential with Retry Queue)
# ---------------------------------------------------------------------
async def scrape_lotuss_watchlist_sequential(urls: list) -> pl.DataFrame:
    """
    Sequential watchlist scraper using a single persistent context
    (which works for Lotus since it doesn't have Cloudflare aggression).
    Includes retry queue for failed URLs.
    """
    final_data = []
    retry_queue = []
    successful_urls = set()
    start_time = datetime.datetime.now()

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
        page = await context.new_page()

        async def process_url(url: str):
            url_short = url.split('/')[-1][:50]
            print(f"🚀 Processing: {url_short}")

            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60000
                )
                await asyncio.sleep(4)

                html_content = await page.content()
                soup = BeautifulSoup(html_content, "html.parser")

                name_elem = (
                    soup.find("h1", class_="mui-style-1wc9qn4")
                    or soup.find("h1")
                )
                name = name_elem.text.strip() if name_elem else "Unknown Product"

                if "Privacy Preference Center" in name or name == "Unknown Product":
                    return None, False

                current_price_elem = soup.find(
                    "div", class_=lambda c: c and "mui-style-kbur42" in c
                ) or soup.find("div", class_="mui-style-kbur42")
                crossed_price_elem = soup.find(
                    "div", class_=lambda c: c and ("mui-style-4bdmr1" in c or "mui-style-1opqjiq" in c)
                ) or soup.find("div", class_="mui-style-4bdmr1") or soup.find("div", class_="mui-style-1opqjiq")

                raw_current = (
                    current_price_elem.get_text()
                    .replace(',', '').replace('฿', '')
                    .split('/')[0].strip()
                    if current_price_elem else None
                )
                raw_crossed = (
                    crossed_price_elem.text
                    .replace(',', '').replace('฿', '').strip()
                    if crossed_price_elem else None
                )

                if raw_crossed:
                    promo_price, orig_price = raw_current, raw_crossed
                else:
                    promo_price, orig_price = None, raw_current

                condition_elem = soup.find("div", class_="mui-style-abwb4l")
                condition = (
                    condition_elem.get_text(separator=" ").strip()
                    if condition_elem else None
                )

                return {
                    "url": url,
                    "name": name,
                    "promotion_price": promo_price,
                    "original_price": orig_price,
                    "condition": condition
                }, True

            except Exception as e:
                print(f"  ❌ Error: {str(e)[:80]}")
                return None, False

        # ---------- 1st Pass ----------
        print(f"\n--- 1st Pass: {len(urls)} URLs ---")
        for url in urls:
            data, success = await process_url(url)
            if success:
                final_data.append(data)
                successful_urls.add(url)
                print(f"  ✅ {data['name'][:50]}")
            else:
                print(f"  ⚠️ Blocked/failed. Re-queueing.")
                retry_queue.append(url)
            await asyncio.sleep(random.uniform(2, 3))

        # ---------- Retry Pass ----------
        if retry_queue:
            print(f"\n--- Retry Pass ({len(retry_queue)} URLs) ---")
            await asyncio.sleep(8)

            for url in retry_queue:
                data, success = await process_url(url)
                if success:
                    final_data.append(data)
                    successful_urls.add(url)
                    print(f"  ✅ Recovered: {data['name'][:50]}")
                else:
                    print(f"  ❌ Permanently failed")
                    final_data.append({
                        "url": url,
                        "name": "Blocked or Not Found",
                        "promotion_price": None,
                        "original_price": None,
                        "condition": None
                    })
                await asyncio.sleep(random.uniform(3, 5))

        await browser.close()

    # ---------- SUMMARY ----------
    elapsed = (datetime.datetime.now() - start_time).total_seconds()
    print(f"\n{'=' * 60}")
    print(f"📊 Watchlist Summary")
    print(f"{'=' * 60}")
    print(f"  Success: {len(successful_urls)}/{len(urls)} URLs")
    print(f"  Elapsed: {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"{'=' * 60}\n")

    df = pl.DataFrame(final_data)

    if not df.is_empty():
        df = df.with_columns([
            pl.col("url").cast(pl.String),
            pl.col("name").cast(pl.String),
            pl.col("promotion_price").cast(pl.Float64, strict=False),
            pl.col("original_price").cast(pl.Float64, strict=False),
            pl.col("condition").cast(pl.String)
        ])

    return df


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
    """Extract Brand, Volume, Unit, Pack, Retailer from product name."""
    quant_unit_pattern = r"(?i)([\d.]+)\s*(ML|G|KG|L|กรัม|GRAMS?)"

    pack_pattern = (
        r"(?i)(PACK\s*\d*\s*FREE\s*\d+|PACK\s*\d*\s*\+\s*\d+|"
        r"PACK\s*\d+|TWINPACK|\bX\s*\d+\b|"
        r"P?\d+\s*\+\s*\d+|\(\d+\+\d+\)|\d+\s*FREE\s*\d+|\bPACK\b)"
    )

    return df.with_columns(
        pl.lit(date.today()).alias("Date"),
        pl.col("name").str.split(" ").list.first().alias("Brand"),
        pl.col("name")
          .str.extract(quant_unit_pattern, 1)
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
# URLs
# ---------------------------------------------------------------------
catalog_urls = [
    "https://www.lotuss.com/en/category/household-and-merits/86590-laundry-supplies"
    "?sort=relevance:DESC&filter.brandId=21490,21430,21829,22054,23485",

    "https://www.lotuss.com/en/category/household-and-merits/86590-cleaning-chemical"
    "?sort=relevance:DESC&filter.categoryId=87100&filter.brandId=22327,24049",
]

watchlist_urls = [
    "https://www.lotuss.com/en/product/75552978",
    "https://www.lotuss.com/en/product/52564595",
    "https://www.lotuss.com/en/product/75605072",
    "https://www.lotuss.com/en/product/75605074",
    "https://www.lotuss.com/en/product/pao-win-wash-liquid-refill-concentrated-liquid-detergent-700ml-27074455",
    "https://www.lotuss.com/en/product/pao-win-wash-liquid-refill-concentrated-liquid-detergent-1500ml-74515209",
    "https://www.lotuss.com/en/product/51625738",
    "https://www.lotuss.com/en/product/51625744",
    "https://www.lotuss.com/en/product/pao-super-white-standard-formula-for-hand-wash-top-loading-washing-machine-2700g-176389",
    "https://www.lotuss.com/en/product/75659870",
    "https://www.lotuss.com/en/product/attack-easy-happy-sweet-conventional-detergent-2700g-75278235",
    "https://www.lotuss.com/en/product/75551723",
    "https://www.lotuss.com/en/product/pro-powder-detergent-2700-g-5314917",
    "https://www.lotuss.com/en/product/hygiene-expert-care-milky-touch-refill-concentrate-fabric-softener-540ml-71833501",
    "https://www.lotuss.com/en/product/hygiene-expert-care-milky-touch-refill-concentrate-fabric-softener-540ml-x-21pcs-72884576",
    "https://www.lotuss.com/en/product/hygiene-expert-care-milky-touch-refill-concentrate-fabric-softener-1300ml-50625999",
    "https://www.lotuss.com/en/product/hygiene-e-pert-milky-white-1300ml-p1-1-51586608",
    "https://www.lotuss.com/en/product/lipon-f-refill-concentrated-dishwashing-liquid-550ml-x-3pcs-12942545",
    "https://www.lotuss.com/en/product/75645272",
    "https://www.lotuss.com/en/product/75645274",
    "https://www.lotuss.com/en/product/lipon-f-xtra-clean-kaffir-lime-formula-dishwashing-liquid-3600ml-73289973",
]

list_to_search = [
    'FINELINE LIQUID PLUS GOLD 550 ML.',
    'FINELINE LIQUID PLUS GOLD 1250 ML.',
    'HYGIENE LAUNDRY EXPERT WASH MILKY TOUCH 600 ML.',
    'HYGIENE LAUNDRY EXPERT WASH MILKY TOUCH 1400 ML.',
    'PAO WIN WASH LIQUID REFILL 620 ML',
    'PAO WIN WASH LIQUID 1300 ML',
    'PAO SUPER WHITE POWDER DETERGENT1800G.P2',
    'PAO SUPER WHITE POWDER DETERGENT 2400 G',
    'ATTACK EASY POWDER HAPPY SWEET 1800G. TWIN PACK',
    'ATTACK EASY HAPPY SWEET DETERGENT 2500G.',
    'PRO POWER DETERGENT BLUE PLUS 1700 G. PACK 1+1',
    'PRO POWDER DETERGENT BLUE PLUS 2400 G.',
    'HYGIENE FABRIC SOFTENER EXPERT CARE MILKY TOUCH 480 ML.',
    'HYGIENE FABRIC SOFTENER EXPERT CARE MILKY WHITE 480 ML 2+1',
    'HYGIENE FABRIC SOFT EXPERTCARE MILKY WHITE 1000 ML',
    'HYGIENE FABRIC SOFTENER EXPERTCARE MILKY TOUCH 1000 ML 1+1',
    'LIPON F DISHWASHING HYGIENE 500 ML PACK 3',
    'LIPON F DISH WASHER XTRA HYGENIC 750 ML. PACK 2',
    'LIPON-F DISHWASH BERGAMOT GALLON 3200 ML',
]


# ---------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------
async def run_pipeline():
    print("=" * 60)
    print("Lotus's Online Scraper")
    print("=" * 60)

    # ---------- CATALOG SCRAPING ----------
    print("\n--- Scraping Catalog ---")
    df_lotuss_catalog = await scrape_lotuss_scroller(
        shop_url_list=catalog_urls
    )
    print(f"\n🏁 Catalog: {len(df_lotuss_catalog)} unique items")

    if not df_lotuss_catalog.is_empty():
        df_prep_lotuss = re_evaluate_price(df_lotuss_catalog)
        df_trans_lotuss = parse_product_names(df_prep_lotuss, "Lotus's")

        catalog_file = OUTPUT_DIR / f"lotus_{today_date}.xlsx"
        df_trans_lotuss.write_excel(str(catalog_file))
        print(f"✅ Saved catalog output: {catalog_file}")

    # ---------- WATCHLIST SCRAPING ----------
    print("\n--- Scraping Watchlist ---")
    df_watchlist_final = await scrape_lotuss_watchlist_sequential(
        watchlist_urls
    )
    print("\n--- Watchlist Results ---")
    print(df_watchlist_final)

    if df_watchlist_final.is_empty():
        print("\n⚠️ No watchlist data. Exiting.")
        return

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

    search_file = OUTPUT_DIR / f"lotus_search_result_{today_date}.xlsx"
    search_results_df.write_excel(str(search_file))
    print(f"✅ Saved search output: {search_file}")

    # ---------- TRANSFORM + SAVE WATCHLIST ----------
    df_prep_watchlist = re_evaluate_price(df_watchlist_final)
    df_trans_watchlist = parse_product_names(df_prep_watchlist, "Lotus's")

    watchlist_file = OUTPUT_DIR / f"lotus_watchlist_{today_date}.xlsx"
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
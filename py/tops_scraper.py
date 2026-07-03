# -*- coding: utf-8 -*-
"""
Tops (Cloudflare) Scraper - Patchright Edition
Version: 3.0 (Serial Optimized)
Date: 2026-07-03
Changelog:
    - v3.0: Serial optimized for speed + reliability
        * Persistent browser + context (CF cookie reuse)
        * Warm-up homepage visit to establish CF cookies
        * Skip slow 30s active bypass; use fast reload strategy
        * 3 quick reload attempts per URL
        * Adaptive cooldowns based on queue state
        * Locale/timezone spoofing for better CF evasion
    - v2.0: Refactored for local + GitHub Actions
        * Removed Colab/IPython dependencies
        * Uses system Chrome (channel="chrome") for corporate SSL
"""

import os
import asyncio
import random
import datetime
from datetime import date
from pathlib import Path

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


# ---------------------------------------------------------------------
# Browser Launch Helper
# ---------------------------------------------------------------------
async def launch_browser(p):
    """Launches Chromium with fallback to system Chrome for corporate networks."""
    launch_kwargs = {"headless": True}
    if USE_SYSTEM_CHROME:
        launch_kwargs["channel"] = "chrome"
    return await p.chromium.launch(**launch_kwargs)


# ---------------------------------------------------------------------
# Data Prep & Transformation
# ---------------------------------------------------------------------
def process_tops_data(df: pl.DataFrame) -> pl.DataFrame:
    """Base cleanup for Tops raw scraped data."""
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
            pl.when(pl.col("promotion_price") == pl.col("original_price"))
            .then(None)
            .otherwise(pl.col("promotion_price"))
            .alias("promotion_price")
        )
    )

    today = date.today().strftime("%Y-%m-%d")
    quant_pattern = r"(?i)(\d+)\s*(ML|G|KG|L)\b"

    return df.with_columns([
        pl.lit(today).alias("Date"),
        pl.col("name").str.split(" ").list.first().alias("Brand"),
        pl.col("name")
          .str.extract(quant_pattern, 1)
          .cast(pl.Int64, strict=False)
          .alias("Volume"),
        pl.col("name")
          .str.extract(quant_pattern, 2)
          .str.to_uppercase()
          .alias("Unit"),
        pl.lit("Tops").alias("Retailer")
    ])


# ---------------------------------------------------------------------
# Full Catalog Scraper (Single URL)
# ---------------------------------------------------------------------
async def scrape_tops_full_catalog(url: str) -> pl.DataFrame:
    """Scrapes a Tops category page, scrolling to load all items."""
    extracted_data = []

    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Bangkok",
        )
        await context.add_cookies([
            {'name': 'language', 'value': 'en',
             'domain': '.tops.co.th', 'path': '/'},
            {'name': 'NEXT_LOCALE', 'value': 'en',
             'domain': '.tops.co.th', 'path': '/'}
        ])
        page = await context.new_page()

        print(f"Opening Tops: {url}")
        try:
            await page.goto(url, wait_until="load", timeout=90000)
            await asyncio.sleep(5)
        except Exception as e:
            print(f"Initial load error: {e}")
            await browser.close()
            return pl.DataFrame()

        previous_count = 0
        retries = 0

        for _ in range(60):
            await page.keyboard.press("PageDown")
            await asyncio.sleep(2)
            await page.evaluate("window.scrollBy(0, 1000)")
            await asyncio.sleep(3)

            current_items = await page.query_selector_all(
                'div[class*="text-textblack"]'
            )
            current_count = len(current_items)

            if current_count > previous_count:
                print(f"Items detected: {current_count}...")
                previous_count = current_count
                retries = 0
            else:
                retries += 1
                await page.evaluate("window.scrollBy(0, -1500)")
                await asyncio.sleep(2)
                await page.keyboard.press("End")
                await asyncio.sleep(2)

            if retries >= 5:
                print("End of catalog or scroll limit reached.")
                break

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        name_tags = soup.find_all(
            "div",
            class_=lambda x: x and "text-textblack" in x
        )

        for nt in name_tags:
            name = nt.get_text(strip=True)
            parent = (
                nt.find_parent(
                    "div",
                    class_=lambda x: x and ("relative" in x or "item" in x)
                )
                or nt.parent
            )
            price_spans = parent.find_all(
                "span", class_="hidden lg:inline"
            )

            if len(price_spans) >= 2:
                promo = price_spans[0].get_text(strip=True).replace(',', '')
                orig = price_spans[1].get_text(strip=True).replace(',', '')
            elif len(price_spans) == 1:
                promo = None
                orig = price_spans[0].get_text(strip=True).replace(',', '')
            else:
                promo = orig = None

            extracted_data.append({
                "name": name,
                "promotion_price": promo,
                "original_price": orig
            })

        await browser.close()

    df_raw = pl.DataFrame(extracted_data)
    return process_tops_data(df_raw)


# ---------------------------------------------------------------------
# Multi URL Scraper (Category)
# ---------------------------------------------------------------------
async def scrape_tops_multi_url(urls: list) -> pl.DataFrame:
    """Chain multiple category URLs sequentially."""
    temp_df = pl.DataFrame()
    for url in urls:
        try:
            df = await scrape_tops_full_catalog(url)
            if not df.is_empty():
                temp_df = pl.concat([temp_df, df])
        except Exception as e:
            print(f"Error with {url}: {e}")
    return temp_df



# ---------------------------------------------------------------------
# Cloudflare Retry Cooldown Settings (Success-Optimized)
# ---------------------------------------------------------------------
# Philosophy: Prefer succeeding on the FIRST attempt over speed.
# Manager wants data captured — time spent is not a concern.

CF_COOLDOWN_BASE = 30      # Longer base cooldown between failed retries
CF_COOLDOWN_STEP = 15      # Aggressive escalation per failed attempt
CF_COOLDOWN_MAX = 120      # Cap at 2 minutes

# Preventive delays (BEFORE the request, not after failure)
PRE_REQUEST_DELAY_MIN = 8    # Min wait before opening each URL
PRE_REQUEST_DELAY_MAX = 15   # Max wait before opening each URL

# Cloudflare bypass patience
CF_BYPASS_TICKS = 90         # 90 seconds (was 30) — wait longer for CF to clear
CF_INITIAL_WAIT = 8          # Wait 8s AFTER navigate before checking CF
CF_POST_RESOLVE_WAIT = 5     # After CF resolved, wait 5s for content to render

def calc_cooldown(attempt: int) -> int:
    """Progressive backoff: 30s -> 45s -> 60s -> ... capped at 120s."""
    return min(CF_COOLDOWN_BASE + (attempt - 1) * CF_COOLDOWN_STEP, CF_COOLDOWN_MAX)

# ---------------------------------------------------------------------
# Watchlist Scraper (v3.1 - Fresh Browser + Fast Reload)
# ---------------------------------------------------------------------
async def scrape_tops_watchlist_unlimited(urls: list) -> pl.DataFrame:
    """
    Unlimited Queue Scraper — Success-Optimized.
    Re-queues failed URLs until every URL succeeds.
    Manager preference: prioritize successful data capture over speed.
    """
    extracted_data = []
    queue = [(url, 1) for url in urls]

    print(f"Starting UNLIMITED scrape for {len(urls)} products...")
    print("Strategy: Success-First. Long waits, patient Cloudflare bypass.\n")

    # Safety cap higher because we're more patient now
    total_attempts_cap = len(urls) * 15
    total_attempts = 0

    while queue and total_attempts < total_attempts_cap:
        total_attempts += 1
        current_url, attempt = queue.pop(0)

        # Preventive polite delay BEFORE opening URL
        pre_wait = random.uniform(PRE_REQUEST_DELAY_MIN, PRE_REQUEST_DELAY_MAX)
        print(f"Preparing next request in {pre_wait:.1f}s (human-like pause)...")
        await asyncio.sleep(pre_wait)

        print(f"Fetching: {current_url}")
        print(f"  -> (Attempt {attempt}) | Items remaining: {len(queue) + 1}")

        success = False

        async with async_playwright() as p:
            browser = await launch_browser(p)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080}
            )
            await context.add_cookies([
                {'name': 'language', 'value': 'en',
                 'domain': '.tops.co.th', 'path': '/'},
                {'name': 'NEXT_LOCALE', 'value': 'en',
                 'domain': '.tops.co.th', 'path': '/'}
            ])
            page = await context.new_page()

            try:
                await page.goto(
                    current_url,
                    wait_until="domcontentloaded",
                    timeout=90000    # 90s (was 60s)
                )

                # Longer initial wait for React + Cloudflare hydration
                await asyncio.sleep(CF_INITIAL_WAIT)

                await page.wait_for_selector("h1", timeout=30000)  # 30s (was 15s)

                h1_locator = page.locator("h1").first
                h1_text = await h1_locator.inner_text()

                # CLOUDFLARE CHECK
                is_blocked = any(k in h1_text.lower() for k in [
                    "tops.co.th", "just a moment", "blocked", "checking"
                ])

                if is_blocked:
                    print(f"  -> Cloudflare detected. Extended bypass ({CF_BYPASS_TICKS}s)...")
                    resolved = False

                    for tick in range(CF_BYPASS_TICKS):
                        await asyncio.sleep(1)

                        # Human-like mouse movement
                        await page.mouse.move(
                            random.randint(200, 1200),
                            random.randint(200, 800)
                        )

                        # Periodic Turnstile clicks
                        if tick % 7 == 0:
                            await page.mouse.click(
                                random.randint(900, 1000),
                                random.randint(500, 580)
                            )

                        # Check every 3 seconds
                        if tick % 3 == 0:
                            try:
                                h1_text = await h1_locator.inner_text(timeout=3000)
                                if not any(k in h1_text.lower() for k in [
                                    "tops.co.th", "just a moment", "blocked", "checking"
                                ]):
                                    print(f"  -> Cloudflare resolved after {tick}s!")
                                    resolved = True
                                    break
                            except Exception:
                                pass

                    if not resolved:
                        print("  -> Extended bypass exhausted. Emergency reload...")
                        try:
                            await page.reload(
                                wait_until="domcontentloaded",
                                timeout=90000
                            )
                            await asyncio.sleep(10)  # Give more time after reload
                            h1_text = await h1_locator.inner_text()
                            if not any(k in h1_text.lower() for k in [
                                "tops.co.th", "just a moment", "blocked", "checking"
                            ]):
                                resolved = True
                                print("  -> Cloudflare resolved after reload!")
                        except Exception as e:
                            print(f"  -> Reload failed: {str(e)[:80]}")
                else:
                    resolved = True

                if resolved:
                    # Extra wait for React to fully populate prices
                    await asyncio.sleep(CF_POST_RESOLVE_WAIT)

                    html = await page.content()
                    soup = BeautifulSoup(html, "html.parser")

                    name_tag = soup.find("h1")
                    name = name_tag.get_text(strip=True) if name_tag else None

                    price_spans = soup.find_all(
                        "span", class_="hidden lg:inline"
                    )
                    promo = orig = None
                    if len(price_spans) >= 2:
                        promo = (
                            price_spans[0].get_text(strip=True)
                            .replace(',', '').replace('฿', '')
                        )
                        orig = (
                            price_spans[1].get_text(strip=True)
                            .replace(',', '').replace('฿', '')
                        )
                    elif len(price_spans) == 1:
                        orig = (
                            price_spans[0].get_text(strip=True)
                            .replace(',', '').replace('฿', '')
                        )

                    condition_tag = soup.find(
                        "div",
                        class_=lambda c: (
                            c and "text-textsecondary" in c
                            and "text-sm" in c and "mt-2" in c
                        )
                    )
                    condition = (
                        condition_tag.get_text(strip=True)
                        if condition_tag else None
                    )

                    if name and "tops.co.th" not in name.lower():
                        extracted_data.append({
                            "name": name,
                            "promotion_price": promo,
                            "original_price": orig,
                            "condition": condition
                        })
                        promo_display = (
                            f" | Promo: {condition}" if condition else ""
                        )
                        print(f"  ✅ Success: {name}{promo_display}")
                        success = True
                    else:
                        print("  -> Invalid name. Still blocked or content missing.")

            except Exception as e:
                print(f"  Error: {str(e)[:100]}...")

            finally:
                if browser.is_connected():
                    await browser.close()

        # Progressive Cloudflare Backoff (Success-Optimized)
        if not success:
            queue.append((current_url, attempt + 1))
            cf_wait = calc_cooldown(attempt)

            # Extra penalty when queue is nearly empty
            if len(queue) == 1:
                cf_wait = max(cf_wait, 90)
                print(f"  [!] Failed. 1 item left. Extra heavy cooldown: {cf_wait}s\n")
            elif len(queue) <= 3:
                cf_wait = max(cf_wait, 60)
                print(f"  [!] Failed. Small queue. Cooldown: {cf_wait}s\n")
            else:
                jitter = random.uniform(0, 5)
                cf_wait = cf_wait + jitter
                print(f"  [!] Failed. Attempt {attempt} cooldown: {cf_wait:.1f}s\n")

            await asyncio.sleep(cf_wait)

        else:
            # Successful — still take a polite pause
            wait_time = random.uniform(5, 8)   # Was 3-5
            if queue:
                print(f"  Waiting {wait_time:.1f}s before next item...\n")
                await asyncio.sleep(wait_time)

    if not extracted_data:
        print("\nNo data collected.")
        return pl.DataFrame()

    df_raw = pl.DataFrame(extracted_data)
    return process_tops_data(df_raw)
# ---------------------------------------------------------------------
# Final Transformation
# ---------------------------------------------------------------------
def re_evaluate_price(df: pl.DataFrame) -> pl.DataFrame:
    """
    1. Swap original & promotion if original < promotion.
    2. Fill missing original with promotion.
    3. Nullify redundant promotion when equal to original.
    """
    return (
        df.with_columns(
            pl.max_horizontal("original_price", "promotion_price")
              .alias("original_price"),
            pl.min_horizontal("original_price", "promotion_price")
              .alias("temp_promo")
        )
        .with_columns(
            pl.when(pl.col("temp_promo") == pl.col("original_price"))
            .then(None)
            .otherwise(pl.col("temp_promo"))
            .alias("promotion_price")
        )
        .drop("temp_promo")
    )


def parse_product_names(df: pl.DataFrame, shop_name: str) -> pl.DataFrame:
    """Extract Brand, Volume, Unit, Pack, Retailer from product name."""
    today_str = date.today().strftime("%Y-%m-%d")

    quant_unit_pattern = r"(?i)([\d.]+)\s*(ML|G|KG|L|LTR|LITERS?|GRAMS?)"

    pack_pattern = (
        r"(?i)(PACK\s*\d*\s*FREE\s*\d+|PACK\s*\d*\s*\+\s*\d+|"
        r"PACK\s*\d+|TWINPACK|\bX\s*\d+\s*\+\s*\d+|\bX\s*\d+\b|"
        r"P?\d+\s*\+\s*\d+|\(\d+\+\d+\)|\d+\s*FREE\s*\d+|\bPACK\b)"
    )

    return df.with_columns(
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
    )


# ---------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------
urls_to_scrape = [
    "https://www.tops.co.th/en/household-and-pet/laundry/fabric-softener?brand=HYGIENE",
    "https://www.tops.co.th/en/household-and-pet/laundry/fabric-softener?brand=FINELINE",
    "https://www.tops.co.th/en/household-and-pet/laundry/liquid-detergent?brand=FINELINE",
    "https://www.tops.co.th/en/household-and-pet/laundry/liquid-detergent?brand=ATTACK%2CHYGIENE",
    "https://www.tops.co.th/en/household-and-pet/laundry/liquid-detergent?brand=PAO",
    "https://www.tops.co.th/en/household-and-pet/laundry/concentrated-fabric-softener?brand=HYGIENE",
    "https://www.tops.co.th/en/household-and-pet/laundry/concentrated-fabric-softener?brand=FINELINE",
    "https://www.tops.co.th/en/household-and-pet/laundry/powder-detergent?brand=ATTACK%2CPAO%2CPRO",
    "https://www.tops.co.th/en/household-and-pet/laundry/regular-fabric-softener?brand=HYGIENE%2CFINELINE",
    "https://www.tops.co.th/en/household-and-pet/laundry/gel-ball-detergent?brand=PAO",
    "https://www.tops.co.th/en/household-and-pet/dish-cleaner/dish-detergent?brand=LIPON+F",
]

watchlist = [
    "https://www.tops.co.th/en/fineline-liquid-detergent-sunny-gold-550ml-8851989033365",
    "https://www.tops.co.th/en/fineline-liquid-detergent-sunny-gold-1250ml-8851989034737",
    "https://www.tops.co.th/en/hygiene-expert-wash-concentrated-liquid-detergent-milky-touch-scent-600ml-8850092254155",
    "https://www.tops.co.th/en/hygiene-expert-wash-concentrated-liquid-detergent-milky-touch-scent-1400ml-8850092254216",
    "https://www.tops.co.th/en/pao-win-wash-liquid-concentrated-detergent-620ml-refill-8850002024823",
    "https://www.tops.co.th/en/pao-win-wash-liquid-concentrated-detergent-1300ml-refill-8850002031739",
    "https://www.tops.co.th/en/hygiene-expert-care-concentrate-fabric-softener-milky-touch-white-480ml-8850092280604",
    "https://www.tops.co.th/en/hygiene-expert-care-concentrate-fabric-softener-milky-touch-480ml-pack-3-8850092280819",
    "https://www.tops.co.th/en/hygiene-expert-care-concentrate-fabric-softener-milky-touch-white-1000ml-8850092280901",
    "https://www.tops.co.th/en/hygiene-expert-care-concentrate-fabric-softener-milky-touch-white-1000ml-pack-2-8850092280925",
    "https://www.tops.co.th/en/lipon-f-x-tra-hygienic-dish-wash-500ml-pack-3-8850002042643",
    "https://www.tops.co.th/en/lipon-f-dish-wash-3-2ltr-8850002010772",
]


# ---------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------
async def run_pipeline():
    print("=" * 60)
    print("Tops Scraper (Cloudflare-Aware, Serial Optimized)")
    print("=" * 60)

    # ---------- Watchlist Scraping ----------
    print("\n--- Scraping Watchlist ---")
    df_watchlist_results = await scrape_tops_watchlist_unlimited(watchlist)

    if df_watchlist_results.is_empty():
        print("\n⚠️ No data collected from watchlist. Exiting.")
        return

    print("\n--- Watchlist Results ---")
    print(df_watchlist_results)

    # ---------- Transform + Save ----------
    df_prep_watchlist = re_evaluate_price(df_watchlist_results)
    df_trans_watchlist = parse_product_names(df_prep_watchlist, "Tops")

    watchlist_file = OUTPUT_DIR / f"tops_watchlists_{today_date}.xlsx"
    df_trans_watchlist.unique().write_excel(str(watchlist_file))
    print(f"\n✅ Saved watchlist output: {watchlist_file}")

    # ---------- Optional: Full Catalog (uncomment to enable) ----------
    # print("\n--- Scraping Category Catalog ---")
    # tops_df = await scrape_tops_multi_url(urls_to_scrape)
    # if not tops_df.is_empty():
    #     df_prep_cat = re_evaluate_price(tops_df)
    #     df_trans_cat = parse_product_names(df_prep_cat, "Tops")
    #     catalog_file = OUTPUT_DIR / f"tops_catalog_{today_date}.xlsx"
    #     df_trans_cat.unique().write_excel(str(catalog_file))
    #     print(f"✅ Saved catalog output: {catalog_file}")

    print("\n" + "=" * 60)
    print("Scraping completed.")
    print("=" * 60)


def main():
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()
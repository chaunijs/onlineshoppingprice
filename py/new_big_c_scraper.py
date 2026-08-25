# -*- coding: utf-8 -*-
"""
BigC Online Product Scraper
Automated scraper designed to run in standalone Python environments and GitHub Actions.
Fetches product catalog and watchlist data, processes the data with Polars, and saves to Excel.
"""

import os
import sys
import re
import time
import json
import random
import datetime
from datetime import date
from pathlib import Path
import concurrent.futures
import polars as pl
from scrapling.fetchers import StealthyFetcher

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ----------------------------------------------------------------------
# CONFIGURATION & OUTPUT DIRECTORY
# ----------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = SCRIPT_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

today_date = datetime.datetime.now().strftime("%Y-%m-%d")
print(f"Today is {today_date}")
print(f"Output directory: {OUTPUT_DIR}")

URLS = [
    "https://www.bigc.co.th/en/category/laundry?limit=90&page={page}",
]

DELAY_SECONDS = 2
PRODUCT_CARD_SELECTOR = "main ul > li"

WATCHLIST_URLS = [
    # -- BIG C
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
    "https://www.bigc.co.th/en/product/attack-ez-conventional-detergent-happy-sweet-scent-1700-g.47863"
]

LIST_TO_SEARCH = [
    # -- BIG C
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
    'ATTACK EZ Conventional Detergent Happy Sweet Scent 1700 g.'
]

# ----------------------------------------------------------------------
# HELPER FUNCTIONS
# ----------------------------------------------------------------------
def clean_price(value: str) -> str:
    """Strip currency symbol, commas and whitespace; return clean string."""
    if not value:
        return None
    return value.replace("฿", "").replace(",", "").strip()

def clean_condition(candidates: list[str] | str) -> str | None:
    """
    Extracts and normalizes the single highest-priority promotional condition.
    Filters out boilerplate disclaimers, delivery tags, units, and noise.
    Priority:
      1. Buy X Get Y (e.g., Buy 1 Get 1)
      2. Buy X Cheaper (e.g., Buy 2 Cheaper)
      3. Super Save
      4. Red Hot
      5. Online Exclusive
    """
    if not candidates:
        return None

    if isinstance(candidates, str):
        candidates = [s.strip() for s in candidates.split("|")]

    cleaned_candidates = []
    for c in candidates:
        if not c:
            continue
        s = c.strip()
        if not s or s == '฿' or s.startswith('-') or s.startswith('*'):
            continue
        if len(s) > 60:
            continue
        if any(noise in s.lower() for noise in [
            "the company reserves", "prior notice", "packaging", "illustration",
            "nextday", "next day", "pickup", "express", "donation", "shipping",
            "previous slide", "next slide", "add to cart", "share", "promotions",
            "expire", "id:", "brand", "category", "fda", "coupon", "disc",
            "shop over", "t&c", "collect", "readmore", "related products",
            "/ piece", "/ pack", "/ bag", "/ bottle", "/ box", "/ can", "/ unit"
        ]):
            continue
        cleaned_candidates.append(s)

    # 1. Buy X Get Y (Highest priority)
    for c in cleaned_candidates:
        match = re.search(r'(?i)buy\s*(\d+)\s*(?:piece|item|bottle|pack|bag)?\s*get\s*(\d+)', c)
        if match:
            return f"Buy {match.group(1)} Get {match.group(2)}"
        thai_match = re.search(r'(\d+)\s*แถม\s*(\d+)', c)
        if thai_match:
            return f"Buy {thai_match.group(1)} Get {thai_match.group(2)}"
        if any(k in c.lower() for k in ["buy 1 get 1", "1 แถม 1", "1 get 1", "buy 1 get"]):
            return "Buy 1 Get 1"

    # 2. Buy X Cheaper
    for c in cleaned_candidates:
        match = re.search(r'(?i)buy\s*(\d+)\s*cheaper', c)
        if match:
            return f"Buy {match.group(1)} Cheaper"
        if "cheaper" in c.lower():
            return "Buy 2 Cheaper"

    # 3. Super Save
    for c in cleaned_candidates:
        if any(k in c.lower() for k in ["super save", "supersave", "ประหยัด"]):
            return "Super Save"

    # 4. Red Hot
    for c in cleaned_candidates:
        if "red hot" in c.lower():
            return "Red Hot"

    # 5. Online Exclusive
    for c in cleaned_candidates:
        if "online exclusive" in c.lower():
            return "Online Exclusive"

    return None

def extract_product(item) -> dict:
    """Extract one product's fields from a single card element."""
    name = item.css('p[class*="line-clamp-2"]::text').get()
    
    # 1. Promotion Price
    promotion_price = item.css('p[class*="text-red-500"] span[class*="text-xl"]::text').get()
    if not promotion_price:
        red_spans = item.css('p[class*="text-red-500"] span::text').getall()
        promotion_price = "".join([s for s in red_spans if '฿' not in s]).strip() or None

    # 2. Original Price
    original_price = item.css('div[class*="line-through"]::text').get()

    # 3. Badges / Conditions (Excludes Nextday / delivery tags / disclaimers)
    condition_spans = item.css('a div span::text').getall()
    condition = clean_condition(condition_spans)

    return {
        "product_name": name.strip() if name else None,
        "promotion_price": clean_price(promotion_price),
        "original_price": clean_price(original_price),
        "condition": condition,
    }

def extract_watchlist_item(page_result, url: str) -> dict:
    """Extracts product data by first inspecting Next.js JSON, falling back to HTML."""
    name, promo_price, orig_price, condition = None, None, None, None
    
    # 1. Try extracting from Next.js embedded JSON
    try:
        raw_json = page_result.css('script#__NEXT_DATA__::text').get()
        if raw_json:
            json_data = json.loads(raw_json)
            page_props = json_data.get("props", {}).get("pageProps", {})
            product = page_props.get("product") or page_props.get("initialData", {}).get("product")
            
            if product:
                name = product.get("name") or product.get("title")
                promo_price = product.get("final_price") or product.get("special_price")
                orig_price = product.get("price") or product.get("original_price")
                if promo_price == orig_price:
                    promo_price = None
                
                badges = product.get("badges") or product.get("promotions") or []
                if isinstance(badges, list):
                    labels = [b.get("label", "") for b in badges if isinstance(b, dict) and b.get("label")]
                    condition = clean_condition(labels)
    except Exception:
        pass

    # 2. Fallback to HTML selectors if JSON was missing or incomplete
    if not name:
        name_elem = page_result.css('h1::text').get() or page_result.css('title::text').get()
        if name_elem:
            clean_title = name_elem.split(" - Big C")[0].strip()
            # Filter out domain names or bot/challenge pages
            if clean_title.lower() not in [
                "www.bigc.co.th", "big c online", "just a moment...",
                "attention required", "access denied", "robot", "security check"
            ]:
                name = clean_title

    # Collect spans only in the main product section (stop before description/brand/related products)
    all_spans = page_result.css('main span::text').getall()
    header_spans = []
    for s in all_spans:
        s_clean = s.strip()
        if s_clean.startswith('*') or s_clean in ['Related products', 'Product description', 'Product Description', 'Readmore', 'Brand', 'Category']:
            break
        header_spans.append(s_clean)

    if not promo_price and not orig_price:
        prices = []
        for s in header_spans:
            clean_s = s.replace("฿", "").replace(",", "").strip()
            if re.match(r'^\d+(?:\.\d+)?$', clean_s):
                if not s.startswith('-') and not s.endswith('%'):
                    prices.append(clean_s)

        if len(prices) == 1:
            orig_price = prices[0]
            promo_price = None
        elif len(prices) >= 2:
            promo_price = prices[0]
            orig_price = prices[1]

    if not condition:
        condition = clean_condition(header_spans)

    return {
        "product_name": name.strip() if name else None,
        "promotion_price": str(promo_price) if promo_price is not None else None,
        "original_price": str(orig_price) if orig_price is not None else None,
        "condition": condition
    }

# ----------------------------------------------------------------------
# DATA TRANSFORMATION UDFs
# ----------------------------------------------------------------------
def re_evaluate_price(df: pl.DataFrame) -> pl.DataFrame:
    """Standardizes pricing logic."""
    return (
        df.with_columns(
            pl.when(pl.col("original_price").is_null() & pl.col("promotion_price").is_not_null())
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
    """Standardizes columns, extracts Brand, Volume, Unit, Pack size, and Retailer."""
    quant_unit_pattern = r"(?i)([\d.]+)\s*(ML|G|KG|L|GRAMS?)"
    pack_pattern = r"(?i)(PACK\s*\d*\s*FREE\s*\d+|PACK\s*\d*\s*\+\s*\d+|PACK\s*\d+|TWINPACK|\bX\s*\d+\b|P?\d+\s*\+\s*\d+|\(\d+\+\d+\)|\d+\s*FREE\s*\d+|\bPACK\b)"
    
    return df.with_columns(
        pl.lit(today_date).alias("Date"),
        pl.col("name").str.split(" ").list.first().alias("Brand"),
        pl.col("name")
            .str.extract(quant_unit_pattern, 1)
            .str.replace_all(",", "")
            .cast(pl.Int64, strict=False)
            .alias("Volume"),
        pl.col("name").str.extract(quant_unit_pattern, 2).str.to_uppercase().alias("Unit"),
        pl.col("name").str.extract(pack_pattern, 1).str.to_uppercase().alias("Pack"),
        pl.lit(shop_name).alias("Retailer")
    )

# ----------------------------------------------------------------------
# SCRAPING ROUTINES
# ----------------------------------------------------------------------
def scrape_catalog(max_pages: int = None) -> list[dict]:
    """Scrapes product listing pages."""
    all_data = []
    en_cookies = [
        {'name': 'language', 'value': 'en', 'domain': '.bigc.co.th', 'path': '/'},
        {'name': 'NEXT_LOCALE', 'value': 'en', 'domain': '.bigc.co.th', 'path': '/'}
    ]
    en_headers = {
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    for base_url in URLS:
        page = 1
        while True:
            if max_pages and page > max_pages:
                print(f"[*] Reached maximum configured page limit ({max_pages}).")
                break
            url = base_url.format(page=page) if "{page}" in base_url else base_url
            print(f"[*] Fetching: {url}")
            try:
                page_result = StealthyFetcher.fetch(
                    url,
                    headless=True,
                    network_idle=True,
                    timeout=60000,
                    cookies=en_cookies,
                    headers=en_headers
                )
                containers = page_result.css(PRODUCT_CARD_SELECTOR)
                if not containers:
                    print(f"    -> No product cards found on page {page}. Category complete.")
                    break
                print(f"    -> Found {len(containers)} products on page {page}")
                for item in containers:
                    data = extract_product(item)
                    if data["product_name"]:
                        all_data.append(data)
            except Exception as e:
                print(f"    [!] Error fetching catalog page {page}: {e}")
                break
            time.sleep(DELAY_SECONDS)
            if "{page}" not in base_url:
                break
            page += 1
    return all_data

# ----------------------------------------------------------------------
# RETRY & COOLDOWN SETTINGS (Infinite Queue / Tops Scraper Style)
# ----------------------------------------------------------------------
CF_COOLDOWN_BASE = 15      # Base cooldown between failed retries (seconds)
CF_COOLDOWN_STEP = 10      # Escalation per failed attempt
CF_COOLDOWN_MAX = 60       # Maximum cooldown cap

PRE_REQUEST_DELAY_MIN = 2  # Min wait before opening each URL
PRE_REQUEST_DELAY_MAX = 5  # Max wait before opening each URL

def calc_cooldown(attempt: int) -> int:
    """Progressive backoff: 15s -> 25s -> 35s -> ... capped at 60s."""
    return min(CF_COOLDOWN_BASE + (attempt - 1) * CF_COOLDOWN_STEP, CF_COOLDOWN_MAX)

def scrape_watchlist(urls: list[str]) -> list[dict]:
    """
    Unlimited Queue Scraper (matching tops_scraper style).
    Continuously re-queues failed URLs until all items succeed.
    Prioritizes complete data capture over speed.
    """
    scraped_data = []
    queue = [(url, 1) for url in urls]
    successful_urls = set()
    total = len(urls)

    print(f"\nStarting UNLIMITED Watchlist scrape for {total} products...")
    print("Strategy: Success-First. Infinite retry queue with adaptive backoff.\n")

    total_attempts_cap = len(urls) * 15
    total_attempts = 0

    en_cookies = [
        {'name': 'language', 'value': 'en', 'domain': '.bigc.co.th', 'path': '/'},
        {'name': 'NEXT_LOCALE', 'value': 'en', 'domain': '.bigc.co.th', 'path': '/'}
    ]
    en_headers = {
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }

    while queue and total_attempts < total_attempts_cap:
        total_attempts += 1
        current_url, attempt = queue.pop(0)

        # Preventive polite delay BEFORE opening URL
        pre_wait = random.uniform(PRE_REQUEST_DELAY_MIN, PRE_REQUEST_DELAY_MAX)
        print(f"Preparing next request in {pre_wait:.1f}s (human-like pause)...")
        time.sleep(pre_wait)

        url_short = current_url.split('/product/')[-1]
        print(f"Fetching [{len(successful_urls)}/{total}]: {url_short} (Attempt {attempt}) | Remaining in queue: {len(queue) + 1}")

        success = False
        try:
            page_result = StealthyFetcher.fetch(
                current_url,
                headless=True,
                network_idle=True,
                timeout=60000,
                cookies=en_cookies,
                headers=en_headers
            )
            item_data = extract_watchlist_item(page_result, current_url)
            if item_data and item_data.get("product_name") and (item_data.get("original_price") or item_data.get("promotion_price")):
                scraped_data.append(item_data)
                successful_urls.add(current_url)
                promo_display = f" | Promo: {item_data['condition']}" if item_data.get("condition") else ""
                print(f"  [+] Success: {item_data['product_name'][:50]}{promo_display}")
                success = True
            else:
                print(f"  [!] Blocked or incomplete response for {url_short}")
        except Exception as e:
            print(f"  [!] Error on attempt {attempt}: {str(e)[:80]}")

        # Progressive backoff on failure
        if not success:
            queue.append((current_url, attempt + 1))
            cf_wait = calc_cooldown(attempt)
            if len(queue) == 1:
                cf_wait = max(cf_wait, 30)
                print(f"  [!] Failed. 1 item left in queue. Heavy cooldown: {cf_wait}s\n")
            elif len(queue) <= 3:
                cf_wait = max(cf_wait, 20)
                print(f"  [!] Failed. Small queue. Cooldown: {cf_wait}s\n")
            else:
                jitter = random.uniform(0, 3)
                cf_wait = cf_wait + jitter
                print(f"  [!] Failed. Attempt {attempt} cooldown: {cf_wait:.1f}s\n")
            time.sleep(cf_wait)
        else:
            wait_time = random.uniform(2, 4)
            if queue:
                print(f"  [+] Waiting {wait_time:.1f}s before next item...\n")
                time.sleep(wait_time)

    print(f"\n[✓] Watchlist complete: {len(successful_urls)}/{total} successful.")
    return scraped_data

# ----------------------------------------------------------------------
# MAIN EXECUTION
# ----------------------------------------------------------------------
def main():
    print(f"=== Starting BigC Scraper ({today_date}) ===")

    # 1. Scrape Catalog
    print("\n--- 1. Scraping Catalog ---")
    with concurrent.futures.ThreadPoolExecutor() as executor:
        catalog_future = executor.submit(scrape_catalog)
        catalog_rows = catalog_future.result()

    if catalog_rows:
        df_catalog_raw = pl.DataFrame(catalog_rows).unique()
        print(f"[+] Scraped {len(df_catalog_raw)} unique catalog items.")
        df_catalog_clean = df_catalog_raw.select([
            pl.col("product_name").alias("name"),
            pl.col("promotion_price").cast(pl.Float64, strict=False),
            pl.col("original_price").cast(pl.Float64, strict=False),
            pl.col("condition")
        ])
        df_prep_big_c = re_evaluate_price(df_catalog_clean)
        df_trans_big_c = parse_product_names(df_prep_big_c, "BigC")
    else:
        print("[!] No catalog rows retrieved. Initializing empty DataFrame.")
        df_trans_big_c = pl.DataFrame()

    # 2. Scrape Watchlist
    print("\n--- 2. Scraping Watchlist ---")
    with concurrent.futures.ThreadPoolExecutor() as executor:
        watchlist_future = executor.submit(scrape_watchlist, WATCHLIST_URLS)
        watchlist_rows = watchlist_future.result()

    if watchlist_rows:
        df_watchlist_raw = pl.DataFrame(watchlist_rows).unique()
        print(f"[+] Scraped {len(df_watchlist_raw)} unique watchlist items.")
        df_watchlist_clean = df_watchlist_raw.select([
            pl.col("product_name").alias("name"),
            pl.col("promotion_price").cast(pl.Float64, strict=False),
            pl.col("original_price").cast(pl.Float64, strict=False),
            pl.col("condition")
        ])
        df_prep_watchlist = re_evaluate_price(df_watchlist_clean)
        df_trans_watchlist = parse_product_names(df_prep_watchlist, "BigC")

        # 3. Match against search list
        print("\n--- 3. Matching Search List ---")
        search_df = pl.DataFrame({"product_name": LIST_TO_SEARCH})
        search_results_df = search_df.join(
            df_watchlist_clean.select(["name", "original_price", "promotion_price"]),
            left_on="product_name",
            right_on="name",
            how="left"
        )
        watchlist_names_set = set(df_watchlist_clean["name"].to_list())
        search_results_df = search_results_df.with_columns(
            pl.col("product_name").is_in(watchlist_names_set).alias("Found")
        ).unique()
    else:
        print("[!] No watchlist rows retrieved. Initializing empty DataFrames.")
        df_trans_watchlist = pl.DataFrame()
        search_results_df = pl.DataFrame()

    # 4. Save Excel Files (Matched to Lotus Scraper format)
    print("\n--- 4. Exporting Excel Files ---")
    catalog_file = OUTPUT_DIR / f"big_c_{today_date}.xlsx"
    df_trans_big_c.write_excel(str(catalog_file))
    print(f"[+] Saved catalog output: {catalog_file}")

    search_file = OUTPUT_DIR / f"big_c_search_result_{today_date}.xlsx"
    search_results_df.write_excel(str(search_file))
    print(f"[+] Saved search output: {search_file}")

    watchlist_file = OUTPUT_DIR / f"big_c_watchlist_{today_date}.xlsx"
    df_trans_watchlist.write_excel(str(watchlist_file))
    print(f"[+] Saved watchlist output: {watchlist_file}")

    print("\n=== Scraper completed successfully ===")

if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
BigC Online Product Scraper
Automated scraper designed to run in standalone Python environments and GitHub Actions.
Fetches product catalog and watchlist data, processes the data with Polars, and saves to Excel and Zip.
"""

import os
import re
import time
import json
import zipfile
import datetime
from datetime import date
import concurrent.futures

import polars as pl
from scrapling.fetchers import StealthyFetcher

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------
URLS = [
    "https://www.bigc.co.th/en/category/laundry?page={page}&limit=90",
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


def extract_product(item) -> dict:
    """Extract one product's fields from a single card element."""
    # Product Name
    name = item.css('p[class*="line-clamp-2"]::text').get()

    # 1. Promotion Price
    promotion_price = item.css('p[class*="text-red-500"] span[class*="text-xl"]::text').get()
    if not promotion_price:
        red_spans = item.css('p[class*="text-red-500"] span::text').getall()
        promotion_price = "".join([s for s in red_spans if '฿' not in s]).strip() or None

    # 2. Original Price
    original_price = item.css('div[class*="line-through"]::text').get()

    # 3. Badges / Conditions (Excludes Nextday / delivery tags)
    condition_spans = item.css('a div span::text').getall()
    conditions = [
        s.strip() for s in condition_spans
        if s.strip()
        and s.strip() != '฿'
        and not s.strip().startswith('-')
        and s.strip().lower() not in ["nextday", "next day"]
    ]
    condition = " | ".join(list(dict.fromkeys(conditions))) if conditions else None

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
                promo_price = product.get("final_price") or product.get("special_price") or product.get("price")
                orig_price = product.get("price") or product.get("original_price")
                if promo_price == orig_price:
                    promo_price = None

                # Check for badges / conditions in JSON
                badges = product.get("badges") or product.get("promotions") or []
                if isinstance(badges, list):
                    condition = " | ".join([b.get("label", "") for b in badges if isinstance(b, dict) and b.get("label")])
    except Exception:
        pass

    # 2. Fallback to HTML selectors if JSON was missing or incomplete
    if not name:
        name_elem = page_result.css('h1::text').get() or page_result.css('title::text').get()
        name = name_elem.split(" - Big C")[0].strip() if name_elem else None

    if not promo_price:
        all_text = " ".join(page_result.css('body ::text').getall())
        price_matches = re.findall(r'฿\s*([\d,]+(?:\.\d+)?)', all_text)
        if price_matches:
            promo_price = price_matches[0].replace(",", "").strip()
            if len(price_matches) > 1 and float(price_matches[1].replace(",", "")) > float(promo_price):
                orig_price = price_matches[1].replace(",", "").strip()

    if not condition:
        badge_spans = page_result.css('main span::text').getall()
        valid_badges = [
            b.strip() for b in badge_spans
            if b.strip() and b.strip() != '฿'
            and not b.strip().startswith('-')
            and b.strip().lower() not in ["nextday", "next day", "pickup", "express", "add to cart", "share"]
        ]
        promo_words = [b for b in valid_badges if any(k in b.lower() for k in ["save", "get", "free", "pack", "disc", "off", "deal"])]
        condition = " | ".join(list(dict.fromkeys(promo_words))) if promo_words else None

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
    """
    Standardizes pricing logic:
    1. If original_price is missing, move the promotion_price to it.
    2. If promotion_price matches the original_price, set it to Null.
    """
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
    """
    Standardizes columns, extracts Brand, Volume, Unit, Pack size, and Retailer.
    """
    today_date = date.today().strftime("%Y-%m-%d")
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
    for base_url in URLS:
        page = 1
        while True:
            if max_pages and page > max_pages:
                print(f"[*] Reached maximum configured page limit ({max_pages}).")
                break

            url = base_url.format(page=page) if "{page}" in base_url else base_url
            print(f"[*] Fetching: {url}")

            try:
                page_result = StealthyFetcher.fetch(url, headless=True, network_idle=True)
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


def scrape_watchlist(urls: list[str], delay: int = 1) -> list[dict]:
    """Iterates through a list of individual product URLs and scrapes their details."""
    scraped_data = []
    total = len(urls)

    for i, url in enumerate(urls, 1):
        print(f"[*] [{i}/{total}] Fetching Watchlist Item: {url.split('/product/')[-1]}")
        try:
            page_result = StealthyFetcher.fetch(url, headless=True, network_idle=True)
            item_data = extract_watchlist_item(page_result, url)
            if item_data["product_name"]:
                scraped_data.append(item_data)
        except Exception as e:
            print(f"    [!] Error fetching {url}: {e}")

        time.sleep(delay)

    return scraped_data


# ----------------------------------------------------------------------
# MAIN EXECUTION
# ----------------------------------------------------------------------
def main():
    today_date = datetime.datetime.now().strftime("%Y-%m-%d")
    print(f"=== Starting BigC Scraper ({today_date}) ===")

    # 1. Scrape Catalog
    print("\n--- 1. Scraping Catalog ---")
    with concurrent.futures.ThreadPoolExecutor() as executor:
        catalog_future = executor.submit(scrape_catalog)
        catalog_rows = catalog_future.result()

    if catalog_rows:
        df_catalog_raw = pl.DataFrame(catalog_rows).unique()
        print(f"[✓] Scraped {len(df_catalog_raw)} unique catalog items.")

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
        print(f"[✓] Scraped {len(df_watchlist_raw)} unique watchlist items.")

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

    # 4. Save Excel Files
    print("\n--- 4. Exporting Excel Files ---")
    catalog_excel = f"big_c_catalog_{today_date}.xlsx"
    watchlist_excel = f"big_c_watchlist_{today_date}.xlsx"
    search_excel = f"search_result_big_c_{today_date}.xlsx"

    print("\n=== Scraper completed successfully ===")


if __name__ == "__main__":
    main()

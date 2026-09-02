# -*- coding: utf-8 -*-
"""
BigC Online Product Extractor (product_id & product_name)
Extracts product IDs and product names across all pages of specified BigC category pages
and stores them in a Polars DataFrame with the extraction date.
"""

import os
import sys
import re
import asyncio
from datetime import datetime
from pathlib import Path

import polars as pl
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------
# Target Category URLs
# ---------------------------------------------------------------------
CATEGORY_URLS = [
    "https://www.bigc.co.th/en/category/dishwashing-liquid?limit=100",
    "https://www.bigc.co.th/en/category/laundry?limit=100",
]

# Use system Chrome if available to handle corporate/network environments
USE_SYSTEM_CHROME = os.getenv("USE_SYSTEM_CHROME", "true").lower() == "true"


async def launch_browser(p):
    """Launches Chromium with fallback to system Chrome."""
    launch_kwargs = {
        "headless": True,
        "args": ["--disable-blink-features=AutomationControlled"]
    }
    if USE_SYSTEM_CHROME:
        launch_kwargs["channel"] = "chrome"
    return await p.chromium.launch(**launch_kwargs)


def extract_product_id_from_url(href: str) -> str | None:
    """
    Extracts the product ID from a BigC product URL or href.
    Examples:
      '/en/v2/product/pro-dishwashing-400-ml-refill.3650' -> '3650'
      'https://www.bigc.co.th/en/product/pao-win-wash-liquid-detergent-620-ml.12782' -> '12782'
    """
    if not href:
        return None

    # BigC product URLs end with .<product_id> before query params
    match = re.search(r'\.(\d+)(?:\?|$)', href)
    if match:
        return match.group(1)

    # Fallback match for numeric suffix after dash or slash
    match_fallback = re.search(r'[-/](\d+)(?:\?|$)', href)
    return match_fallback.group(1) if match_fallback else None


def extract_category_from_url(url: str) -> str:
    """
    Extracts and normalizes the category name from a BigC category URL.
    Examples:
      'https://www.bigc.co.th/en/category/dishwashing-liquid?limit=100' -> 'Dishwashing Liquid'
      'https://www.bigc.co.th/en/category/laundry?limit=100'            -> 'Laundry'
    """
    path = url.split("?")[0].rstrip("/")
    slug = path.split("/")[-1]
    clean_slug = re.sub(r'^\d+-', '', slug)
    return clean_slug.replace("-", " ").title()


def build_page_url(base_url: str, page: int) -> str:
    """Appends or updates the page query parameter in the category URL."""
    if "page=" in base_url:
        return re.sub(r'page=\d+', f'page={page}', base_url)
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}page={page}"


async def scrape_bigc_product_names(category_urls: list[str]) -> pl.DataFrame:
    """
    Scrapes product_id and product_name from BigC categories, automatically paginating
    through all pages (Page 1, 2, 3, ...) until all items are extracted.
    Returns a Polars DataFrame with columns: ['product_id', 'product_name', 'category', 'retailer', 'date'].
    """
    today_date = datetime.now().strftime("%Y-%m-%d")
    extracted_records = []

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
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

        # Set English language cookies
        await context.add_cookies([
            {"name": "language", "value": "en", "domain": ".bigc.co.th", "path": "/"},
            {"name": "NEXT_LOCALE", "value": "en", "domain": ".bigc.co.th", "path": "/"}
        ])

        page = await context.new_page()

        for cat_url in category_urls:
            cat_name = extract_category_from_url(cat_url)
            print("\n" + "=" * 60)
            print(f"🚀 Category: {cat_name} ({cat_url})")
            print("=" * 60)

            page_num = 1
            max_pages_limit = 50  # Safeguard upper limit

            while page_num <= max_pages_limit:
                page_url = build_page_url(cat_url, page_num)
                print(f"\n📄 Fetching Page {page_num}: {page_url}")

                try:
                    await page.goto(
                        page_url,
                        wait_until="domcontentloaded",
                        timeout=60000
                    )
                    # Wait for product card containers
                    await page.wait_for_selector(
                        'main ul > li, a[href*="/product/"]',
                        timeout=15000
                    )
                except Exception as e:
                    print(f"⚠️ Page load notice (Page {page_num}): {e}")

                # Scroll down to trigger dynamic loading of images and cards
                for _ in range(3):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(1.5)

                html_content = await page.content()
                soup = BeautifulSoup(html_content, "html.parser")

                # Find product card containers (each product is in a <li> inside main <ul>)
                cards = soup.select("main ul > li")
                if not cards:
                    # Fallback: select cards that have product links
                    cards = [
                        a.find_parent("li") or a
                        for a in soup.find_all("a", href=lambda h: h and "/product/" in h)
                    ]
                    # Unique containers
                    seen_objs = set()
                    unique_cards = []
                    for c in cards:
                        if id(c) not in seen_objs:
                            seen_objs.add(id(c))
                            unique_cards.append(c)
                    cards = unique_cards

                page_extracted = 0
                for card in cards:
                    # 1. Find product link inside this specific card container
                    link_elem = card if card.name == "a" else card.find("a", href=lambda h: h and "/product/" in h)
                    if not link_elem:
                        continue

                    href = link_elem.get("href", "")
                    product_id = extract_product_id_from_url(href)
                    if not product_id:
                        continue

                    # 2. Extract Product Name
                    product_name = None
                    p_elem = card.find("p", class_=lambda c: c and "line-clamp-2" in c)
                    if not p_elem:
                        p_elem = card.find("p")

                    if p_elem and p_elem.get_text(strip=True):
                        product_name = p_elem.get_text(strip=True)
                    elif link_elem.get("aria-label"):
                        product_name = link_elem.get("aria-label").strip()
                    elif card.find("img") and card.find("img").get("alt"):
                        product_name = card.find("img")["alt"].strip()

                    if product_id and product_name:
                        extracted_records.append({
                            "product_id": str(product_id),
                            "product_name": str(product_name),
                            "category": cat_name,
                            "retailer": "BigC",
                            "date": today_date,
                        })
                        page_extracted += 1

                print(f"   ↳ Extracted {page_extracted} products from Page {page_num} (Total so far: {len(extracted_records)})")

                # If no products were found on this page, the category is complete
                if page_extracted == 0:
                    print(f"🏁 Finished all pages for this category.")
                    break

                page_num += 1
                await asyncio.sleep(2)

        await browser.close()

    # Create Polars DataFrame
    if extracted_records:
        df = pl.DataFrame(extracted_records)
        df = df.with_columns([
            pl.col("product_id").cast(pl.String),
            pl.col("product_name").cast(pl.String),
            pl.col("category").cast(pl.String),
            pl.col("retailer").cast(pl.String),
            pl.col("date").cast(pl.String),
        ])
    else:
        df = pl.DataFrame(
            schema={
                "product_id": pl.String,
                "product_name": pl.String,
                "category": pl.String,
                "retailer": pl.String,
                "date": pl.String,
            }
        )

    return df


async def main():
    print("=" * 60)
    print("🛒 Starting BigC Product ID & Name Extraction")
    print("=" * 60)

    df = await scrape_bigc_product_names(CATEGORY_URLS)

    print("\n" + "=" * 60)
    print("📊 Extraction Completed!")
    print(f"Total Products Extracted: {df.height}")
    print("=" * 60)
    print("\nPreview:")
    print(df)

    output_dir = Path(__file__).resolve().parents[2] / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / f"bigc_products_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    try:
        df.write_excel(output_path)
        print(f"\n💾 Saved result to: {output_path}")
    except Exception:
        csv_path = output_dir / f"bigc_products_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        df.write_csv(csv_path)
        print(f"\n💾 Saved result to: {csv_path}")

    return df


if __name__ == "__main__":
    asyncio.run(main())

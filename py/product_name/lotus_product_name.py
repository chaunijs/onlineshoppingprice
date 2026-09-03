# -*- coding: utf-8 -*-
"""
Lotus's Online Product Extractor (product_id & product_name)
Extracts product IDs and product names from specified Lotus category pages
and stores them in a Polars DataFrame.
"""

import os
import sys
import re
import asyncio
from datetime import datetime
from pathlib import Path
from collections import deque

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
    "https://www.lotuss.com/en/category/household-and-merits/86590-cleaning-chemical/86671-dish-washing-liquid?sort=relevance:DESC",
    "https://www.lotuss.com/en/category/household-and-merits/86590-laundry-supplies?sort=relevance:DESC",
]

# Use system Chrome if available to handle network / corporate environments
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
    Extracts the product ID from a Lotus product URL or href.
    Example:
      '.../pro-refill-concentrated-formula-dishwashing-liquid-400ml-71100814?utm_track=...' -> '71100814'
    """
    if not href:
        return None
    
    # Matches the numeric ID at the end of the URL slug before query parameters
    match = re.search(r'-(\d+)(?:\?|$)', href)
    if match:
        return match.group(1)
    
    # Fallback pattern for any /product/.../(\d+)
    match_fallback = re.search(r'/(\d+)(?:\?|$)', href)
    return match_fallback.group(1) if match_fallback else None


def extract_category_from_url(url: str) -> str:
    """
    Extracts and normalizes the category name from a Lotus category URL.
    Examples:
      '.../86671-dish-washing-liquid?sort=...' -> 'Dish Washing Liquid'
      '.../86590-laundry-supplies?sort=...'   -> 'Laundry Supplies'
    """
    path = url.split("?")[0].rstrip("/")
    slug = path.split("/")[-1]
    clean_slug = re.sub(r'^\d+-', '', slug)
    return clean_slug.replace("-", " ").title()


async def scrape_lotus_product_names(category_urls: list[str]) -> pl.DataFrame:
    """
    Scrapes product_id and product_name from Lotus category URLs using infinite scrolling.
    Returns a Polars DataFrame with columns: ['product_id', 'product_name', 'category', 'retailer', 'date'].
    """
    today_date = datetime.now().strftime("%Y-%m-%d")
    extracted_records = []
    url_queue = deque([(url, 0) for url in category_urls])
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
            cat_url, attempts = url_queue.popleft()
            cat_name = extract_category_from_url(cat_url)
            print(f"\n🚀 Opening Category: {cat_name} ({cat_url})")
            print(f"   Attempt: {attempts + 1}/{MAX_RETRIES + 1}")

            try:
                await page.goto(
                    cat_url,
                    wait_until="domcontentloaded",
                    timeout=60000
                )
                # Wait for title or product cards to be mounted in DOM
                await page.wait_for_selector(
                    '[data-testid="common-product__title"], .mui-style-17swnep, .MuiCard-root',
                    timeout=15000
                )
            except Exception as e:
                print(f"⚠️ Page load warning: {e}")
                if attempts < MAX_RETRIES:
                    print("🔄 Re-queueing URL for retry...")
                    url_queue.append((cat_url, attempts + 1))
                    await asyncio.sleep(2)
                    continue
                else:
                    print("❌ Max retries reached for this URL. Skipping.")
                    continue

            # Infinite scroll loop to load all dynamic products
            previous_item_count = 0
            scroll_attempts = 0
            max_scrolls = 40

            while scroll_attempts < max_scrolls:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(3.5)

                current_items = await page.query_selector_all(".MuiCard-root")
                current_count = len(current_items)
                print(f"  ↳ Scroll #{scroll_attempts + 1}: Found {current_count} products")

                if current_count == previous_item_count and current_count > 0:
                    print("  ↳ Reached the bottom of the category!")
                    break

                previous_item_count = current_count
                scroll_attempts += 1

            # Parse DOM content with BeautifulSoup
            html_content = await page.content()
            soup = BeautifulSoup(html_content, "html.parser")

            # Look for card containers
            cards = soup.find_all("div", class_="MuiCard-root")
            if not cards:
                # Fallback: search parent containers of product links
                cards = soup.find_all("a", href=lambda h: h and "/product/" in h)

            for card in cards:
                # 1. Extract Product ID
                product_id = None
                link_elem = card if card.name == "a" else card.find("a", href=lambda h: h and "/product/" in h)
                if link_elem and link_elem.get("href"):
                    product_id = extract_product_id_from_url(link_elem["href"])

                # Fallback: inspect image src if link wasn't found
                if not product_id:
                    img_elem = card.find("img")
                    if img_elem and img_elem.get("src"):
                        img_match = re.search(r'/(\d+)\.(?:jpg|png|webp)', img_elem["src"])
                        if img_match:
                            product_id = img_match.group(1)

                # 2. Extract Product Name
                product_name = None
                title_elem = card.find(attrs={"data-testid": "common-product__title"})
                if not title_elem:
                    title_elem = card.find(class_="mui-style-17swnep")
                
                if title_elem:
                    product_name = title_elem.get_text(strip=True)
                elif card.find("img") and card.find("img").get("alt"):
                    product_name = card.find("img")["alt"].strip()

                # Validate and record
                if product_id and product_name:
                    extracted_records.append({
                        "product_id": str(product_id),
                        "product_name": str(product_name),
                        "category": cat_name,
                        "retailer": "Lotus",
                        "date": today_date,
                    })

            print(f"✅ Extracted {len(extracted_records)} products so far.")
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
    print("🛒 Starting Lotus's Product ID & Name Extraction")
    print("=" * 60)

    df = await scrape_lotus_product_names(CATEGORY_URLS)

    print("\n" + "=" * 60)
    print("📊 Extraction Completed!")
    print(f"Total Products Extracted: {df.height}")
    print("=" * 60)
    print("\nPreview:")
    print(df)

    # Optional: Save to Excel / CSV in output directory
    output_dir = Path(__file__).resolve().parents[2] / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / f"lotus_products_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    
    try:
        df.write_excel(output_path)
        print(f"\n💾 Saved result to: {output_path}")
    except Exception as e:
        csv_path = output_dir / f"lotus_products_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        df.write_csv(csv_path)
        print(f"\n💾 Saved result to: {csv_path}")

    return df


if __name__ == "__main__":
    asyncio.run(main())

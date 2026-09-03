# -*- coding: utf-8 -*-
"""
7-Eleven AllOnline Product Extractor (product_id & product_name)
Extracts product IDs and product names across all pages of specified 7-Eleven category URLs
and stores them in a Polars DataFrame with category, retailer, and extraction date.
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
    "https://www.allonline.7eleven.co.th/supermarket/household-items/laundry/",
    "https://www.allonline.7eleven.co.th/supermarket/household-items/household-cleaner/",
    "https://www.allonline.7eleven.co.th/supermarket/household-items/dish-detergent/",
]

# Use system Chrome if available to handle corporate/proxy environments
USE_SYSTEM_CHROME = os.getenv("USE_SYSTEM_CHROME", "true").lower() == "true"


async def launch_browser(p):
    """Launches Chromium with fallback to system Chrome."""
    launch_kwargs = {
        "headless": True,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--ignore-certificate-errors",
            "--ignore-ssl-errors",
        ]
    }
    if USE_SYSTEM_CHROME:
        launch_kwargs["channel"] = "chrome"
    return await p.chromium.launch(**launch_kwargs)


def extract_product_id_from_url(href: str) -> str | None:
    """
    Extracts the product ID from a 7-Eleven product URL.
    Examples:
      '/p/ไฟน์ไลน์-พลัส-น้ำยาซักผ้า-ซันนี่-โกลด์-แกลลอน-2800-มล/462770/' -> '462770'
    """
    if not href:
        return None

    # Matches numeric product ID at end of URL path
    match = re.search(r'/(\d+)/?(?:\?|#|$)', href)
    if match:
        return match.group(1)

    match_fallback = re.search(r'[-/](\d+)(?:\?|#|$)', href)
    return match_fallback.group(1) if match_fallback else None


def extract_category_from_url(url: str) -> str:
    """
    Extracts and normalizes the category name from a 7-Eleven category URL.
    Examples:
      '.../household-items/laundry/'           -> 'Laundry'
      '.../household-items/household-cleaner/' -> 'Household Cleaner'
      '.../household-items/dish-detergent/'   -> 'Dish Detergent'
    """
    path = url.split("?")[0].rstrip("/")
    slug = path.split("/")[-1]
    return slug.replace("-", " ").title()


def build_page_url(base_url: str, page_index: int) -> str:
    """Constructs 7-Eleven pagination URL using 0-indexed parameter (p=0, 1, 2...)."""
    base = base_url.split("?")[0].rstrip("/") + "/"
    return f"{base}?p={page_index}"


async def scrape_7eleven_product_names(category_urls: list[str]) -> pl.DataFrame:
    """
    Scrapes product_id and product_name from 7-Eleven category URLs,
    paginating until all products across all pages are collected.
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
            locale="th-TH",
            timezone_id="Asia/Bangkok",
        )
        page = await context.new_page()

        for cat_url in category_urls:
            cat_name = extract_category_from_url(cat_url)
            print("\n" + "=" * 60)
            print(f"🚀 Category: {cat_name} ({cat_url})")
            print("=" * 60)

            p_index = 0
            max_pages_limit = 50
            previous_page_ids = []

            while p_index <= max_pages_limit:
                page_url = build_page_url(cat_url, p_index)
                print(f"\n📄 Fetching Page {p_index + 1} (p={p_index}): {page_url}")

                try:
                    await page.goto(
                        page_url,
                        wait_until="domcontentloaded",
                        timeout=60000
                    )
                    await page.wait_for_selector(
                        '.item-list-wrapper, .product-item, a.productlink',
                        timeout=15000
                    )
                except Exception as e:
                    print(f"⚠️ Page load notice (Page {p_index + 1}): {e}")

                # Scroll down to hydrate lazy-loaded elements
                for _ in range(2):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(1.5)

                html_content = await page.content()
                soup = BeautifulSoup(html_content, "html.parser")

                # Product card containers
                product_cards = soup.select(".item-list-wrapper, .product-item")
                if not product_cards:
                    # Fallback to direct product links
                    product_cards = soup.find_all("a", class_="productlink")

                if not product_cards:
                    print(f"🏁 No product cards found. Category complete.")
                    break

                current_page_ids = []
                page_extracted = 0

                for card in product_cards:
                    # Find link element
                    link_elem = card if card.name == "a" else card.select_one("a.productlink") or card.find("a", href=lambda h: h and "/p/" in h)
                    if not link_elem:
                        continue

                    href = link_elem.get("href", "")
                    product_id = extract_product_id_from_url(href)
                    if not product_id:
                        continue

                    current_page_ids.append(product_id)

                    # Extract Product Name
                    product_name = None
                    desc_elem = card.select_one(".item-description-cls-mobile, .item.description")
                    if desc_elem and desc_elem.get_text(strip=True):
                        product_name = desc_elem.get_text(strip=True)
                    elif link_elem.get("title"):
                        # Clean title if it contains brand/category suffix
                        raw_title = link_elem.get("title").strip()
                        product_name = raw_title.split(" - ")[0].strip() if " - " in raw_title else raw_title
                    elif link_elem.find("img") and link_elem.find("img").get("alt"):
                        product_name = link_elem.find("img")["alt"].strip()

                    if product_id and product_name:
                        extracted_records.append({
                            "product_id": str(product_id),
                            "product_name": str(product_name),
                            "category": cat_name,
                            "retailer": "7-Eleven",
                            "date": today_date,
                        })
                        page_extracted += 1

                # Check if we looped back or hit duplicate final page
                if current_page_ids and current_page_ids == previous_page_ids:
                    print("🏁 Duplicate page detected (end of listing reached).")
                    break

                previous_page_ids = current_page_ids.copy()
                print(f"   ↳ Extracted {page_extracted} products from Page {p_index + 1} (Total so far: {len(extracted_records)})")

                if page_extracted == 0:
                    print("🏁 Finished all pages for this category.")
                    break

                p_index += 1
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
    print("🛒 Starting 7-Eleven AllOnline Product ID & Name Extraction")
    print("=" * 60)

    df = await scrape_7eleven_product_names(CATEGORY_URLS)

    print("\n" + "=" * 60)
    print("📊 Extraction Completed!")
    print(f"Total Products Extracted: {df.height}")
    print("=" * 60)
    print("\nPreview:")
    print(df)

    output_dir = Path(__file__).resolve().parents[2] / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / f"7eleven_products_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    try:
        df.write_excel(output_path)
        print(f"\n💾 Saved result to: {output_path}")
    except Exception:
        csv_path = output_dir / f"7eleven_products_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        df.write_csv(csv_path)
        print(f"\n💾 Saved result to: {csv_path}")

    return df


if __name__ == "__main__":
    asyncio.run(main())

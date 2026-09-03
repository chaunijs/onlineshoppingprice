# -*- coding: utf-8 -*-
"""
Makro PRO Online Product Extractor (product_id & product_name)
Extracts product IDs and product names across all pages of specified Makro collection URLs
and stores them in a Polars DataFrame with category, retailer, and extraction date.
"""

import os
import sys
import re
import asyncio
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

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
    "https://www.makro.pro/en/c/collections/Home%20Care%20%7C%20Laundry%20Detergent/35527?info=JTdCJTIyc291cmNlRXZlbnQlMjIlM0ElMjJtYXJrZXRpbmdDYXJvdXNlbCUyMiUyQyUyMmJhbm5lck5hbWUlMjIlM0ElMjJIb21lJTIwQ2FyZSUyMCU3QyUyMExhdW5kcnklMjBEZXRlcmdlbnQlMjIlN0Q",
    "https://www.makro.pro/en/c/collections/Home%20Care%20%7C%20Fabric%20Softener/35524?info=JTdCJTIyc291cmNlRXZlbnQlMjIlM0ElMjJtYXJrZXRpbmdDYXJvdXNlbCUyMiUyQyUyMmJhbm5lck5hbWUlMjIlM0ElMjJIb21lJTIwQ2FyZSUyMCU3QyUyMEZhYnJpYyUyMFNvZnRlbmVyJTIyJTdE",
    "https://www.makro.pro/en/c/collections/Home%20Care%20%7C%20Dishwashing/35523?info=JTdCJTIyc291cmNlRXZlbnQlMjIlM0ElMjJtYXJrZXRpbmdDYXJvdXNlbCUyMiUyQyUyMmJhbm5lck5hbWUlMjIlM0ElMjJIb21lJTIwQ2FyZSUyMCU3QyUyMERpc2h3YXNoaW5nJTIyJTdE",
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
    Extracts the product ID from a Makro product URL or href.
    Examples:
      '/en/p/660z7os-7115236737219?info=...' -> '7115236737219'
    """
    if not href:
        return None

    # Matches numeric product ID suffix after dash before query params
    match = re.search(r'-(\d+)(?:\?|$)', href)
    if match:
        return match.group(1)

    match_fallback = re.search(r'[-/](\d+)(?:\?|$)', href)
    return match_fallback.group(1) if match_fallback else None


def extract_category_from_url(url: str) -> str:
    """
    Extracts and normalizes the category name from a Makro collection URL.
    Examples:
      '.../Home%20Care%20%7C%20Laundry%20Detergent/35527' -> 'Laundry Detergent'
      '.../Home%20Care%20%7C%20Fabric%20Softener/35524'   -> 'Fabric Softener'
      '.../Home%20Care%20%7C%20Dishwashing/35523'       -> 'Dishwashing'
    """
    path = url.split("?")[0].rstrip("/")
    parts = path.split("/")
    cat_part = parts[-2] if len(parts) >= 2 and parts[-1].isdigit() else parts[-1]
    decoded = unquote(cat_part)
    if "|" in decoded:
        decoded = decoded.split("|")[-1].strip()
    return decoded.replace("-", " ").title()


async def scrape_makro_product_names(category_urls: list[str]) -> pl.DataFrame:
    """
    Scrapes product_id and product_name from Makro collection URLs using SPA navigation.
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
        )
        page = await context.new_page()

        for cat_url in category_urls:
            cat_name = extract_category_from_url(cat_url)
            print("\n" + "=" * 60)
            print(f"🚀 Category: {cat_name} ({cat_url})")
            print("=" * 60)

            try:
                await page.goto(
                    cat_url,
                    wait_until="domcontentloaded",
                    timeout=60000
                )
                await page.wait_for_selector(
                    'a[href*="/p/"], [data-test-id="href-url"], div[data-testid="product-card"]',
                    timeout=20000
                )
            except Exception as e:
                print(f"⚠️ Page load warning: {e}")

            await asyncio.sleep(4)

            max_spa_pages = 25
            for page_num in range(1, max_spa_pages + 1):
                print(f"\n📄 Scraping Page {page_num}...")

                # Scroll down to hydrate all cards in the grid
                for _ in range(3):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(1.5)

                html_content = await page.content()
                soup = BeautifulSoup(html_content, "html.parser")

                # Locate all product card anchor links
                product_cards = soup.find_all(
                    "a",
                    href=lambda href: href and "/p/" in href
                )

                page_extracted = 0
                for card in product_cards:
                    href = card.get("href", "")
                    product_id = extract_product_id_from_url(href)
                    if not product_id:
                        # Fallback: check data-test-id attributes inside card
                        title_div = card.find(lambda t: t.has_attr("data-test-id") and "_title" in t["data-test-id"])
                        if title_div:
                            id_match = re.search(r'Product/(\d+)_', title_div["data-test-id"])
                            if id_match:
                                product_id = id_match.group(1)

                    if not product_id:
                        continue

                    # Extract Product Name
                    product_name = None
                    title_elem = card.find(lambda t: t.has_attr("data-test-id") and "_title" in t["data-test-id"])
                    if title_elem and title_elem.get_text(strip=True):
                        product_name = title_elem.get_text(strip=True)
                    elif card.find("span", class_="css-1n8x1y2"):
                        product_name = card.find("span", class_="css-1n8x1y2").get_text(strip=True)
                    elif card.find("img") and card.find("img").get("alt"):
                        product_name = card.find("img")["alt"].strip()
                    else:
                        # Fallback to stripped strings heuristic
                        for text in card.stripped_strings:
                            if len(text) > 8 and "฿" not in text and "points" not in text.lower() and "Today" not in text:
                                product_name = text
                                break

                    if product_id and product_name:
                        extracted_records.append({
                            "product_id": str(product_id),
                            "product_name": str(product_name),
                            "category": cat_name,
                            "retailer": "Makro",
                            "date": today_date,
                        })
                        page_extracted += 1

                print(f"   ↳ Extracted {page_extracted} products from Page {page_num} (Total so far: {len(extracted_records)})")

                # Try clicking Next button for SPA pagination
                if page_num < max_spa_pages:
                    next_clicked = False
                    try:
                        next_selectors = [
                            "button:has-text('Next')",
                            "[aria-label='Go to next page']",
                            "[aria-label='next page']",
                            "button.MuiPaginationItem-next",
                            "li.MuiPaginationItem-next button",
                            "a:has-text('Next')",
                        ]

                        for sel in next_selectors:
                            btn = page.locator(sel).first
                            if await btn.is_visible() and await btn.is_enabled():
                                await btn.scroll_into_view_if_needed()
                                await btn.click()
                                print("   ↳ Clicked 'Next' button. Waiting for page transition...")
                                await asyncio.sleep(4)
                                next_clicked = True
                                break
                    except Exception as e:
                        print(f"   ↳ Pagination note: {e}")

                    if not next_clicked:
                        print("   ↳ No more 'Next' pages available. Category complete.")
                        break

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
    print("🛒 Starting Makro PRO Product ID & Name Extraction")
    print("=" * 60)

    df = await scrape_makro_product_names(CATEGORY_URLS)

    print("\n" + "=" * 60)
    print("📊 Extraction Completed!")
    print(f"Total Products Extracted: {df.height}")
    print("=" * 60)
    print("\nPreview:")
    print(df)

    output_dir = Path(__file__).resolve().parents[2] / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / f"makro_products_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    try:
        df.write_excel(output_path)
        print(f"\n💾 Saved result to: {output_path}")
    except Exception:
        csv_path = output_dir / f"makro_products_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        df.write_csv(csv_path)
        print(f"\n💾 Saved result to: {csv_path}")

    return df


if __name__ == "__main__":
    asyncio.run(main())

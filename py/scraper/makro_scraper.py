# -*- coding: utf-8 -*-
"""
Makro Scraper (Playwright)
Version: 2.0
Date: 2026-07-02
Changelog:
    - v2.0: Refactored for GitHub Actions + local execution
    - Removed Colab/IPython dependencies (data_table, subprocess installer)
    - Removed apt-get/pip subprocess calls
    - Removed top-level `await` (moved into async main)
    - Removed @register_cell_magic
    - Added output directory + proper main() entry point
"""

import os
import json
import asyncio
import re
import datetime
from datetime import date
from pathlib import Path

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


# ---------------------------------------------------------------------
# Category Scraper (SPA Clicker)
# ---------------------------------------------------------------------
async def scrape_makro_spa_clicker(start_url: str) -> pl.DataFrame:
    extracted_data = []
    seen_names = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        print("Walking through Makro's front door...")
        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)

        try:
            await page.wait_for_selector("div[data-testid='product-card']", timeout=15000)
        except Exception:
            print(f"Warning: Products took too long to load for {start_url}")

        await asyncio.sleep(6)

        assume_MAX_page = 16
        for page_num in range(1, assume_MAX_page + 1):
            print(f"Scraping page {page_num}...")

            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

            html_content = await page.content()
            soup = BeautifulSoup(html_content, "html.parser")

            product_cards = soup.find_all(
                "a",
                href=lambda href: href and "/p/" in href
            )

            new_items_found = 0
            for card in product_cards:
                texts = list(card.stripped_strings)
                if len(texts) < 3:
                    continue

                # 1. Product Name
                name = None
                for text in texts:
                    if (
                        len(text) > 10
                        and "฿" not in text
                        and "points" not in text.lower()
                        and "Today" not in text
                    ):
                        name = text
                        break

                if not name or name in seen_names:
                    continue

                # 2. Prices
                prices = []
                for text in texts:
                    if any(k in text.lower() for k in ["buy", "get", "point"]):
                        continue
                    clean_text = text.replace("฿", "").replace(",", "").strip()
                    try:
                        val = float(clean_text)
                        if 5 < val < 10000:
                            prices.append(val)
                    except ValueError:
                        pass

                if not prices:
                    continue

                promo_price = min(prices)
                original_price = max(prices)

                # 3. Condition
                condition = None
                condition_tag = card.find(
                    lambda tag: tag.has_attr("data-test-id")
                    and "_lbl_buy_more" in tag["data-test-id"]
                )
                if condition_tag:
                    condition = condition_tag.get_text(strip=True)
                else:
                    for text in texts:
                        if "unit" in text.lower() and any(c.isdigit() for c in text):
                            condition = text
                            break

                extracted_data.append({
                    "name": name,
                    "promotion_price": promo_price,
                    "original_price": original_price,
                    "condition": condition
                })
                seen_names.add(name)
                new_items_found += 1

            print(f"  -> Extracted {new_items_found} new products.")

            # SPA Clicker
            if page_num < assume_MAX_page:
                try:
                    next_button = page.locator("text=Next").first
                    if await next_button.is_visible():
                        await next_button.click()
                        print("  -> Clicked 'Next'. Waiting for SPA to load...")
                        await asyncio.sleep(4)
                    else:
                        print("  -> 'Next' button not visible. Reached the end!")
                        break
                except Exception as e:
                    print(f"  -> Pagination stopped: {e}")
                    break

        await browser.close()

    df = pl.DataFrame(
        extracted_data,
        schema={
            "name": str,
            "promotion_price": float,
            "original_price": float,
            "condition": str
        }
    )

    if not df.is_empty():
        df = df.unique(subset=["name"], maintain_order=True)

        df = df.with_columns(
            pl.when(pl.col("promotion_price") == pl.col("original_price"))
            .then(None)
            .otherwise(pl.col("original_price"))
            .alias("original_price")
        )

        df = df.with_columns(
            pl.when(pl.col("original_price").is_not_null())
            .then(
                (
                    (pl.col("original_price") - pl.col("promotion_price"))
                    / pl.col("original_price") * 100
                ).round(1)
            )
            .otherwise(None)
            .alias("discount_pct")
        ).sort("name")

    return df


# ---------------------------------------------------------------------
# Watchlist Conditions Scraper
# ---------------------------------------------------------------------
async def scrape_watchlist_with_conditions(urls: list) -> pl.DataFrame:
    scraped_data = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        for url in urls:
            print(f"Scraping conditions: {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                
                # Wait for title to ensure page load, then give a short buffer for SPA pricing blocks
                try:
                    await page.wait_for_selector(
                        '[data-test-id$="_product_title"], h1',
                        timeout=10000
                    )
                except Exception:
                    pass
                
                await asyncio.sleep(2) 

                content = await page.content()
                soup = BeautifulSoup(content, "html.parser")

                name_div = soup.find(
                    'div',
                    attrs={
                        'data-test-id': lambda x: x and x.endswith('_product_title')
                    }
                )
                product_name = name_div.text.strip() if name_div else "Name not found"

                condition_dict = {}
                text_nodes = list(soup.stripped_strings)
                
                # Find the starting index of the promotional block
                idx = -1
                for i, text in enumerate(text_nodes):
                    if "buy more" in text.lower() and "save more" in text.lower():
                        idx = i
                        break
                
                # Extract headers (units) and values (prices) directly from the text sequence
                if idx != -1:
                    subset = text_nodes[idx + 1 : idx + 20]
                    units = []
                    prices = []
                    
                    for i, text in enumerate(subset):
                        # Match headers like "1 - 1 units", "2+ units"
                        if "unit" in text.lower() and any(c.isdigit() for c in text):
                            units.append(text)
                        # Match prices and handle potential HTML splitting between '฿' and the number
                        elif "฿" in text:
                            if text.strip() == "฿" and i + 1 < len(subset):
                                prices.append(f"฿ {subset[i+1].strip()}")
                            else:
                                prices.append(text)
                    
                    # Map them together 1:1
                    if units and prices:
                        pair_count = min(len(units), len(prices))
                        condition_dict = dict(zip(units[:pair_count], prices[:pair_count]))

                # Keep conditions nested cleanly as a JSON string
                condition_json = json.dumps(condition_dict, ensure_ascii=False) if condition_dict else "{}"

                scraped_data.append({
                    "url": url,
                    "name": product_name,
                    "condition": condition_json
                })

            except Exception as e:
                print(f"  -> Error scraping {url}: {e}")

        await browser.close()

    df = pl.DataFrame(scraped_data)
    if not df.is_empty():
        df = df.with_columns(
            pl.col("condition").replace("{}", None)
        )
    return df

# ---------------------------------------------------------------------
# Watchlist Price Scraper (Concurrent)
# ---------------------------------------------------------------------
async def scrape_makro_product(url, browser_instance, semaphore):
    async with semaphore:
        context = await browser_instance.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()

        data = {
            "url": url,
            "name": None,
            "promotion_price": None,
            "original_price": None
        }

        try:
            print(f"Scraping price: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            title_selector = '[data-test-id$="_product_title"], h1'
            try:
                await page.wait_for_selector(
                    title_selector,
                    state="visible",
                    timeout=20000
                )
            except Exception:
                print(f"  -> Warning: Title selector timed out for {url}")

            await page.wait_for_timeout(1500)

            page_data = await page.evaluate('''() => {
                let titleEl = document.querySelector('[data-test-id$="_product_title"]');
                if (!titleEl) {
                    titleEl = document.querySelector('h1');
                }
                if (!titleEl) return null;
                return {
                    name: titleEl.innerText.trim(),
                    rawText: document.body.innerText
                };
            }''')

            promo_price = None
            original_price = None

            if page_data and page_data['rawText']:
                lines = [
                    line.strip()
                    for line in page_data['rawText'].split('\n')
                    if line.strip()
                ]
                start_idx = -1
                for i, line in enumerate(lines):
                    if "Code :" in line or "Code:" in line:
                        start_idx = i
                        break

                if start_idx == -1 and page_data['name']:
                    for i, line in enumerate(lines):
                        if page_data['name'] in line:
                            start_idx = i

                if start_idx != -1:
                    prices = []
                    for line in lines[start_idx+1 : start_idx+16]:
                        clean_line = line.replace("฿", "").replace(",", "").strip()
                        if re.match(r'^\d+(\.\d+)?$', clean_line):
                            prices.append(clean_line)

                    if len(prices) >= 2:
                        promo_price = prices[0]
                        original_price = prices[1]
                    elif len(prices) == 1:
                        promo_price = prices[0]
                        original_price = prices[0]

            data["name"] = page_data['name'] if page_data else None
            data["promotion_price"] = promo_price
            data["original_price"] = original_price

        except Exception as e:
            print(f"  -> Failed to scrape {url}: {e}")
        finally:
            await context.close()

        return data


async def scrape_watchlist_prices(urls: list) -> pl.DataFrame:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        semaphore = asyncio.Semaphore(3)
        tasks = [
            scrape_makro_product(url, browser, semaphore) for url in urls
        ]
        results = await asyncio.gather(*tasks)
        await browser.close()

    return pl.DataFrame(
        results,
        schema={
            "url": str,
            "name": str,
            "promotion_price": str,
            "original_price": str
        }
    )


# ---------------------------------------------------------------------
# Data Prep Functions
# ---------------------------------------------------------------------
def ensure_consistent_schema(df: pl.DataFrame) -> pl.DataFrame:
    df = df.with_columns(
        pl.col("name").cast(pl.String, strict=False).alias("name"),
        pl.col("promotion_price").cast(pl.Float64, strict=False).alias("promotion_price"),
        pl.col("original_price").cast(pl.Float64, strict=False).alias("original_price"),
        pl.col("condition").cast(pl.String, strict=False).alias("condition"),
    )
    return df.select(["name", "promotion_price", "original_price", "condition"])


def re_evaluate_price(df: pl.DataFrame) -> pl.DataFrame:
    """
    Standardizes pricing:
    1. If original_price missing, use promotion_price.
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
    """
    Standardizes column extraction for supermarket product data.
    """
    today_str = date.today().strftime("%Y-%m-%d")

    quant_unit_pattern = r"(?i)([\d.]+)\s*(ML|G|KG|L|GRAMS?)"

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
        pl.lit(shop_name).alias("Retailer"),
    )


# ---------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------
scrape_list = [
    "https://www.makro.pro/en/c/collections/Shop%20by%20Brand:%20FINELINE/22199?info=JTdCJTIyc291cmNlRXZlbnQlMjIlM0ElMjJtYXJrZXRpbmdDYXJvdXNlbCUyMiUyQyUyMmJhbm5lck5hbWUlMjIlM0ElMjJTaG9wJTIwYnklMjBCcmFuZCUzQSUyMEZJTkVMSU5FJTIyJTdE",
    "https://www.makro.pro/en/c/household-supplies/laundry-supplies",
    "https://www.makro.pro/en/c/collections/Home%20Care%20%7C%20Fresh%20and%20Soft/17985?info=JTdCJTIyc291cmNlRXZlbnQlMjIlM0ElMjJmbGV4aXBhZ2VDYXJvdXNlbCUyMiUyQyUyMmNhcm91c2VsTmFtZSUyMiUzQSUyMkhvbWUlMjBDYXJlJTIwJTdDJTIwRnJlc2glMjBhbmQlMjBTb2Z0JTIyJTJDJTIyY2Fyb3VzZWxUaXRsZSUyMiUzQSUyMkhvbWUlMjBDYXJlJTIwJTdDJTIwRnJlc2glMjBhbmQlMjBTb2Z0JTIyJTdE",
    "https://www.makro.pro/en/c/collections/Home%20Care%20%7C%20Dishwash%20Care/17988?info=JTdCJTIyc291cmNlRXZlbnQlMjIlM0ElMjJmbGV4aXBhZ2VDYXJvdXNlbCUyMiUyQyUyMmNhcm91c2VsTmFtZSUyMiUzQSUyMkhvbWUlMjBDYXJlJTIwJTdDJTIwRGlzaHdhc2glMjBDYXJlJTIyJTJDJTIyY2Fyb3VzZWxUaXRsZSUyMiUzQSUyMkhvbWUlMjBDYXJlJTIwJTdDJTIwRGlzaHdhc2glMjBDYXJlJTIyJTdE",
]

urls_watchlist = [
    "https://www.makro.pro/en/p/BxZe5f5-174668393051076",
    "https://www.makro.pro/en/p/z0icJlu-272067985587301",
    "https://www.makro.pro/en/p/ex0iyeb-7078772015299",
    "https://www.makro.pro/en/p/gka-ofe-7352769970371",
    "https://www.makro.pro/en/p/piHDQ_qh-227017132155382",
    "https://www.makro.pro/en/p/ru-ickh-6761207398595",
    "https://www.makro.pro/en/p/vzcfnxp-7078770016451",
    "https://www.makro.pro/en/p/4_Yu5hcU-829783173153864",
    "https://www.makro.pro/en/p/05CJD5Oy-126255086281148",
    "https://www.makro.pro/en/p/hojuwx2-6761206448323",
    "https://www.makro.pro/en/p/iuia8gd-7416044290243",
    "https://www.makro.pro/en/p/ampyy_n-7275653693635",
    "https://www.makro.pro/en/p/854707-7248018604227",
    "https://www.makro.pro/en/p/8vSXFyA-869126234793421",
    "https://www.makro.pro/en/p/8lbyz1p-6761190785219",
    "https://www.makro.pro/en/p/bpm7o_y-6761191080131",
    "https://www.makro.pro/en/p/LNFCtR5-980397089042734",
    "https://www.makro.pro/en/p/wUmyk9Pz-896561474964112",
]

list_to_search = [
    "Fineline Liquid Detergent Plus Sunny Gold 550 ML. Gold",
    "Fineline Liquid Detergent Plus Sunny Gold 1250 ML.",
    "HYGIENE Expert Wash Liauid Detergent Milky Touch 1.4 ml",
    "PAO Win Wash Concentrated Liquid Detergent Orange 620 ml",
    "PAO Win Wash Concentrated Liquid Detergent Orange 1.3 l",
    "PAO Super White Laundry Detergent 1.8 kg x 1+1",
    "PAO Super White Standard Formula Powder Detergent 2.4 kg",
    "ATTACK Easy Regular Detergent Happy Sweet Pink 2.3/2.5 kg",
    "ATTACK Easy Conventional Detergent Happy Sweet Pink 1.7 kg x 1+1",
    "PRO Regular Powder Detergent Blue Plus Red 1.7 kg x 1+1",
    "PRO Regular Powder Detergent Blue Plus Red 2.4 kg",
    "HYGIENE Fabric Softener Expert Care Milky Touch White 480 ml",
    "HYGIENE EXPERT CARE CONCENTRATE FABRIC SOFTENER MILKY TOUCH 480 ML X 2+1 BAGS",
    "HYGIENE Expert Care Concentrated Fabric Softener Milky Touch 1 l",
    "HYGIENE Expert Care Concentrate Softener Duo Milky Touch White 1 l x 1+1",
    "LIPON F Dishwash 500 ml x 3",
    "LIPON F DISHWASHING LIQUID 3.2 L.",
]


# ---------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------
async def run_pipeline():
    print("=" * 60)
    print("Makro Scraper")
    print("=" * 60)

    # ---------- Category Scraping ----------
    scraped_dfs = []
    for url in scrape_list:
        print(f"\nStarting scrape for URL: {url[:80]}...")
        try:
            df_result = await scrape_makro_spa_clicker(start_url=url)
            print("=" * 60)
            print(f"Extracted {len(df_result)} products.")
            print("=" * 60)
            if not df_result.is_empty():
                scraped_dfs.append(df_result)
        except Exception as e:
            print(f"Failed to scrape {url}: {e}")

    if scraped_dfs:
        df_combined_makro = pl.concat(scraped_dfs)
        df_combined_makro = df_combined_makro.unique(subset=["name"], maintain_order=True)
    else:
        df_combined_makro = pl.DataFrame()

    print(f"\nMakro Category Scraping Complete - {len(df_combined_makro)} unique products.")

    # ---------- Watchlist Conditions ----------
    print("\n--- Scraping Watchlist Conditions ---")
    df_watchlist_cond = await scrape_watchlist_with_conditions(urls_watchlist)

    # ---------- Watchlist Prices ----------
    print("\n--- Scraping Watchlist Prices ---")
    df_watchlist_price = await scrape_watchlist_prices(urls_watchlist)

    # ---------- Merge Watchlist ----------
    if not df_watchlist_price.is_empty() and not df_watchlist_cond.is_empty():
        df_merge_watchlist = df_watchlist_price.join(
            df_watchlist_cond, on="url", how="left"
        )
    else:
        df_merge_watchlist = df_watchlist_price

    # ---------- Combine Category + Watchlist ----------
    if "discount_pct" in df_combined_makro.columns:
        df_combined_makro = df_combined_makro.drop("discount_pct")

    dfs_to_combine = []
    if not df_combined_makro.is_empty():
        dfs_to_combine.append(ensure_consistent_schema(df_combined_makro))
    if not df_merge_watchlist.is_empty():
        df_watchlist_fixed = ensure_consistent_schema(df_merge_watchlist)
        dfs_to_combine.append(df_watchlist_fixed)
    else:
        df_watchlist_fixed = pl.DataFrame()

    if dfs_to_combine:
        df_makro = pl.concat(dfs_to_combine)
    else:
        print("No data was scraped.")
        return

    # ---------- Transform ----------
    df_prep_makro = re_evaluate_price(df_makro)
    df_trans_makro = parse_product_names(df_prep_makro, "Makro")

    main_file = OUTPUT_DIR / f"makro_{today_date}.xlsx"
    df_trans_makro.write_excel(str(main_file))
    print(f"\n✅ Saved main output: {main_file}")

    # ---------- Watchlist Output ----------
    if not df_watchlist_fixed.is_empty():
        df_prep_watchlist = re_evaluate_price(df_watchlist_fixed)
        df_trans_watchlist = parse_product_names(df_prep_watchlist, "Makro")

        watchlist_file = OUTPUT_DIR / f"makro_watchlist_{today_date}.xlsx"
        df_trans_watchlist.write_excel(str(watchlist_file))
        print(f"✅ Saved watchlist output: {watchlist_file}")

    # ---------- Search Results ----------
    search_df = pl.DataFrame({"product_name": list_to_search})
    search_results_df = search_df.join(
        df_trans_makro.select(["name", "original_price", "promotion_price"]),
        left_on="product_name",
        right_on="name",
        how="left"
    )
    makro_names_set = set(df_trans_makro["name"].to_list())
    search_results_df = search_results_df.with_columns(
        pl.col("product_name").is_in(makro_names_set).alias("Found")
    ).unique()

    print("\nSearch Results with Prices:")
    print(search_results_df)

    search_file = OUTPUT_DIR / f"search_result_makro_{today_date}.xlsx"
    search_results_df.write_excel(str(search_file))
    print(f"✅ Saved search output: {search_file}")

    print("\n" + "=" * 60)
    print("Scraping completed.")
    print("=" * 60)


def main():
    # Windows requires ProactorEventLoop for subprocess (Playwright)
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()
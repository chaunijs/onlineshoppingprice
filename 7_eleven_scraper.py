# -*- coding: utf-8 -*-
"""
7-Eleven AllOnline Scraper
Version: 2.1
Date: 2026-07-02
Changelog:
    - v2.1: Switched to Selenium Manager (built-in) instead of webdriver-manager
            - Resolves SSL cert issues on corporate networks (Unilever proxy)
            - Added SSL error tolerance flags to Chrome
    - v2.0: Refactored for GitHub Actions schedule trigger
            - Removed Colab/IPython dependencies
            - Added output directory handling
"""

import os
import time
import re
import datetime
from datetime import date
from pathlib import Path

# Optional: workaround for corporate SSL inspection (safe to keep)
os.environ["WDM_SSL_VERIFY"] = "0"

import polars as pl
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

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
# Driver Setup (Selenium Manager - built-in since Selenium 4.6)
# ---------------------------------------------------------------------
def get_driver():
    """
    Initialize a headless Chrome driver using Selenium Manager.
    Selenium 4.6+ auto-downloads matching ChromeDriver without webdriver-manager.
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--window-size=1920,1080")

    # SSL error tolerance for corporate proxy environments
    chrome_options.add_argument("--ignore-certificate-errors")
    chrome_options.add_argument("--ignore-ssl-errors")
    chrome_options.add_argument("--allow-insecure-localhost")

    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    )

    # Selenium Manager handles ChromeDriver automatically
    driver = webdriver.Chrome(options=chrome_options)
    driver.set_window_size(1920, 1080)
    return driver


# ---------------------------------------------------------------------
# Category Scraper
# ---------------------------------------------------------------------
def scrape_7eleven_data(base_url: str) -> pl.DataFrame:
    """
    Scrapes product data from 7-Eleven's online store
    using strict container boundaries.
    """
    print("Starting browser...")
    driver = get_driver()

    data = []
    p_index = 0
    previous_page_names = []

    try:
        while True:
            print(f"\nScraping Page {p_index + 1} (URL parameter p={p_index})...")
            current_page_url = base_url + f"&p={p_index}"
            driver.get(current_page_url)

            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "item-list-wrapper"))
                )
            except Exception:
                print("No products found on this page. Reached the end!")
                break

            soup = BeautifulSoup(driver.page_source, "html.parser")

            product_cards = soup.select(".item-list-wrapper, .product-item")
            if not product_cards:
                print("Page is empty. Reached the end!")
                break

            current_page_names = []
            for card in product_cards:
                a_tag = card.select_one("a.productlink")
                name = a_tag.get("title") if a_tag and a_tag.has_attr("title") else None

                if not name:
                    desc_elem = card.select_one(".item-description-cls-mobile")
                    name = desc_elem.text.strip() if desc_elem else "Unknown"

                current_page_names.append(name)

            if current_page_names == previous_page_names:
                print("Duplicate items detected! Reached the final page.")
                break

            previous_page_names = current_page_names.copy()

            for card, name in zip(product_cards, current_page_names):
                promo_elem = card.select_one("strong")
                orig_elem = card.select_one("s, strike, del")

                promotion_price = promo_elem.text.strip() if promo_elem else None
                original_price = orig_elem.text.strip() if orig_elem else None

                condition = None
                flag_elem = card.select_one(".flag")
                if flag_elem and flag_elem.has_attr("style"):
                    match = re.search(
                        r'\/([A-Za-z0-9_]+)-th\.svg',
                        flag_elem["style"]
                    )
                    if match:
                        condition = match.group(1).upper()

                data.append({
                    "name": name,
                    "promotion_price": promotion_price,
                    "original_price": original_price,
                    "condition": condition
                })

            print(f"Successfully grabbed {len(product_cards)} items from Page {p_index + 1}.")
            p_index += 1

    finally:
        driver.quit()

    df = pl.DataFrame(
        data,
        schema={
            "name": str,
            "promotion_price": str,
            "original_price": str,
            "condition": str
        }
    )

    if not df.is_empty():
        df = df.with_columns(
            pl.col("promotion_price")
              .str.replace_all(r"[^\d.]", "")
              .cast(pl.Float64, strict=False)
              .alias("promotion_price"),
            pl.col("original_price")
              .str.replace_all(r"[^\d.]", "")
              .cast(pl.Float64, strict=False)
              .alias("original_price")
        )
        df = df.unique(subset=["name"], keep="first")

        print("\n--- Final Scraping Results ---")
        print(df.tail(10))
        print(f"\nFinal clean item count: {df.height}")
        return df

    print("No data was scraped.")
    return pl.DataFrame()


# ---------------------------------------------------------------------
# Watchlist Scraper
# ---------------------------------------------------------------------
def scrape_specific_products(urls: list) -> pl.DataFrame:
    """
    Scrapes 7-Eleven product detail pages using targeted CSS selectors.
    """
    driver = get_driver()
    data = []

    try:
        for url in urls:
            print(f"Scraping product: {url[:80]}...")
            try:
                driver.get(url)

                try:
                    WebDriverWait(driver, 15).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, ".currentPrice, h1")
                        )
                    )
                except Exception:
                    time.sleep(3)

                soup = BeautifulSoup(driver.page_source, "html.parser")

                name_elem = soup.find("h1")
                name = name_elem.text.strip() if name_elem else "Unknown Name"

                promo_elem = soup.select_one(".currentPrice")
                original_elem = soup.select_one("strike")

                promo_text = promo_elem.text.strip() if promo_elem else None
                orig_text = original_elem.text.strip() if original_elem else None

                condition = None
                gallery_area = soup.select_one(".gallery, .art-detail-page-top")
                search_area = gallery_area if gallery_area else soup

                flag_elems = search_area.select(".flag")
                for flag in flag_elems:
                    style_attr = flag.get("style", "").upper()
                    if "1GET1" in style_attr:
                        condition = "1GET1"
                        break
                    elif "2GET1" in style_attr:
                        condition = "2GET1"
                        break

                data.append({
                    "name": name,
                    "promotion_price": promo_text,
                    "original_price": orig_text,
                    "url": url,
                    "condition": condition
                })

                print(f"  -> Promo: {promo_text} | Orig: {orig_text} | Flag: {condition}")

            except Exception as e:
                print(f"  Error scraping URL: {str(e)[:100]}...")

            time.sleep(2)

    finally:
        driver.quit()

    df = pl.DataFrame(
        data,
        schema={
            "name": str,
            "promotion_price": str,
            "original_price": str,
            "url": str,
            "condition": str
        }
    )

    if not df.is_empty():
        df = df.with_columns(
            pl.col("promotion_price")
              .str.replace_all(r"[^\d.]", "")
              .cast(pl.Float64, strict=False),
            pl.col("original_price")
              .str.replace_all(r"[^\d.]", "")
              .cast(pl.Float64, strict=False)
        )

    return df


# ---------------------------------------------------------------------
# Data Prep Functions
# ---------------------------------------------------------------------
def re_evaluate_price(df: pl.DataFrame) -> pl.DataFrame:
    """
    Standardizes pricing logic:
    1. If original_price is missing, move promotion_price to it.
    2. If promotion_price == original_price, set promotion_price to Null.
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


def parse_product_names_TH(df: pl.DataFrame, shop_name: str) -> pl.DataFrame:
    """
    Standardizes product name parsing for Thai supermarket data.
    Extracts Brand, Volume, Unit, Pack size, and Retailer.
    """
    today_str = date.today().strftime("%Y-%m-%d")

    quant_unit_pattern = r"(?i)([\d,.]+)\s*(มล\.|ลิตร|ก\.ก\.|กรัม|ML|G|KG|L)"

    pack_pattern = (
        r"(?i)(PACK\s*\d*\s*FREE\s*\d+|PACK\s*\d*\s*\+\s*\d+|"
        r"PACK\s*\d+|TWINPACK|\bX\s*\d+\b|P?\d+\s*\+\s*\d+|"
        r"\(\d+\+\d+\)|\d+\s*FREE\s*\d+|\bPACK\b|แพ็ก\s*\d+\s*ชิ้น)"
    )

    thai_brands = ["ไฟน์ไลน์", "ไฮยีน", "เปา", "แอทแทค", "ไลปอนเอฟ"]
    brand_pattern = r"^(" + "|".join(re.escape(b) for b in thai_brands) + r")"

    return df.with_columns([
        pl.lit(None).alias("condition"),
        pl.lit(today_str).alias("Date"),

        pl.col("name")
          .str.extract(brand_pattern, 1)
          .fill_null(pl.col("name").str.split(" ").list.first())
          .alias("Brand"),

        pl.col("name")
          .str.extract(quant_unit_pattern, 1)
          .str.replace_all(",", "")
          .cast(pl.Float64, strict=False)
          .alias("Volume"),

        pl.col("name")
          .str.extract(quant_unit_pattern, 2)
          .alias("Unit"),

        pl.col("name")
          .str.extract(pack_pattern, 0)
          .str.to_uppercase()
          .alias("Pack"),

        pl.lit(shop_name).alias("Retailer"),
    ])


# ---------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------
list_of_7eleven_urls = [
    "https://allonline.7eleven.co.th/supermarket/household-items/laundry/liquid-detergent/"
    "?filter.BRAND=Fineline&filter.BRAND=Hygiene"
    "&filter.BRAND=%E0%B9%80%E0%B8%9B%E0%B8%B2"
    "&filter.BRAND=%E0%B9%81%E0%B8%AD%E0%B8%97%E0%B9%81%E0%B8%97%E0%B8%84"
    "&filter.PRICE=49-470&filter.initial_from_PRICE=49"
    "&filter.initial_to_PRICE=470&landing=true&pageSize=90&sortBy=si&view=0",

    "https://allonline.7eleven.co.th/supermarket/household-items/dish-detergent/"
    "?show=all&filter.from_PRICE=53&filter.to_PRICE=899"
    "&brands=on&sortBy=si"
    "&filter.BRAND=%E0%B9%84%E0%B8%A5%E0%B8%9B%E0%B8%AD%E0%B8%99%E0%B9%80%E0%B8%AD%E0%B8%9F"
    "&filter.initial_from_PRICE=53&filter.initial_to_PRICE=899&view=6",
]

specific_product_urls = [
    # ไลปอนเอฟ น้ำยาล้างจาน สูตรอนามัย 3200 มล.
    "https://www.allonline.7eleven.co.th/p/%E0%B9%84%E0%B8%A5%E0%B8%9B%E0%B8%AD%E0%B8%99%E0%B9%80%E0%B8%AD%E0%B8%9F-%E0%B8%99%E0%B9%89%E0%B8%B3%E0%B8%A2%E0%B8%B2%E0%B8%A5%E0%B9%89%E0%B8%B2%E0%B8%87%E0%B8%88%E0%B8%B2%E0%B8%99-%E0%B8%AA%E0%B8%B9%E0%B8%95%E0%B8%A3%E0%B8%AD%E0%B8%99%E0%B8%B2%E0%B8%A1%E0%B8%B1%E0%B8%A2-3200-%E0%B8%A1%E0%B8%A5/473248/",
    # ไลปอนเอฟ น้ำยาล้างจาน สูตรอนามัย 500 มล. (แพ็ก 3 ชิ้น)
    "https://www.allonline.7eleven.co.th/p/%E0%B9%84%E0%B8%A5%E0%B8%9B%E0%B8%AD%E0%B8%99%E0%B9%80%E0%B8%AD%E0%B8%9F-%E0%B8%99%E0%B9%89%E0%B8%B3%E0%B8%A2%E0%B8%B2%E0%B8%A5%E0%B9%89%E0%B8%B2%E0%B8%87%E0%B8%88%E0%B8%B2%E0%B8%99-%E0%B8%AA%E0%B8%B9%E0%B8%95%E0%B8%A3%E0%B8%AD%E0%B8%99%E0%B8%B2%E0%B8%A1%E0%B8%B1%E0%B8%A2-500-%E0%B8%A1%E0%B8%A5-%E0%B9%81%E0%B8%9E%E0%B9%87%E0%B8%81-3-%E0%B8%8A%E0%B8%B4%E0%B9%89%E0%B8%99/456259/",
    # ไฟน์ไลน์ พลัส น้ำยาซักผ้า ซันนี่ โกลด์ 1250 มล.
    "https://allonline.7eleven.co.th/p/%E0%B9%84%E0%B8%9F%E0%B8%99%E0%B9%8C%E0%B9%84%E0%B8%A5%E0%B8%99%E0%B9%8C-%E0%B8%9E%E0%B8%A5%E0%B8%B1%E0%B8%AA-%E0%B8%99%E0%B9%89%E0%B8%B3%E0%B8%A2%E0%B8%B2%E0%B8%8B%E0%B8%B1%E0%B8%81%E0%B8%9C%E0%B9%89%E0%B8%B2-%E0%B8%8B%E0%B8%B1%E0%B8%99%E0%B8%99%E0%B8%B5%E0%B9%88-%E0%B9%82%E0%B8%81%E0%B8%A5%E0%B8%94%E0%B9%8C-1250-%E0%B8%A1%E0%B8%A5/346148/",
    # ไฟน์ไลน์พลัสซักผ้าชนิดน้ำสีทอง 550 มล.
    "https://www.allonline.7eleven.co.th/p/%E0%B9%84%E0%B8%9F%E0%B8%99%E0%B9%8C%E0%B9%84%E0%B8%A5%E0%B8%99%E0%B9%8C%E0%B8%9E%E0%B8%A5%E0%B8%B1%E0%B8%AA%E0%B8%8B%E0%B8%B1%E0%B8%81%E0%B8%9C%E0%B9%89%E0%B8%B2%E0%B8%8A%E0%B8%99%E0%B8%B4%E0%B8%94%E0%B8%99%E0%B9%89%E0%B8%B3%E0%B8%AA%E0%B8%B5%E0%B8%97%E0%B8%AD%E0%B8%87-550-%E0%B8%A1%E0%B8%A5/462765",
    # ไฮยีน น้ำยาซักผ้า มิลค์กี้ ทัช 600 มล.
    "https://www.allonline.7eleven.co.th/p/%E0%B9%84%E0%B8%AE%E0%B8%A2%E0%B8%B5%E0%B8%99-%E0%B8%99%E0%B9%89%E0%B8%B3%E0%B8%A2%E0%B8%B2%E0%B8%8B%E0%B8%B1%E0%B8%81%E0%B8%9C%E0%B9%89%E0%B8%B2-%E0%B8%A1%E0%B8%B4%E0%B8%A5%E0%B8%84%E0%B9%8C%E0%B8%81%E0%B8%B5%E0%B9%89-%E0%B8%97%E0%B8%B1%E0%B8%8A-600-%E0%B8%A1%E0%B8%A5/351927",
    # ไฮยีน เอ็กซ์เพิร์ท วอช 1400 มล.
    "https://www.allonline.7eleven.co.th/p/%E0%B9%84%E0%B8%AE%E0%B8%A2%E0%B8%B5%E0%B8%99-%E0%B9%80%E0%B8%AD%E0%B9%87%E0%B8%81%E0%B8%8B%E0%B9%8C%E0%B9%80%E0%B8%9E%E0%B8%B4%E0%B8%A3%E0%B9%8C%E0%B8%97-%E0%B8%A7%E0%B8%AD%E0%B8%8A-%E0%B8%99%E0%B9%89%E0%B8%B3%E0%B8%A2%E0%B8%B2%E0%B8%8B%E0%B8%B1%E0%B8%81%E0%B8%9C%E0%B9%89%E0%B8%B2-%E0%B8%A1%E0%B8%B4%E0%B8%A5%E0%B8%84%E0%B9%8C%E0%B8%81%E0%B8%B5%E0%B9%89-%E0%B8%97%E0%B8%B1%E0%B8%8A-1400-%E0%B8%A1%E0%B8%A5/376426",
    # ไฮยีน น้ำยาปรับผ้านุ่ม 480 มล.
    "https://allonline.7eleven.co.th/p/%E0%B9%84%E0%B8%AE%E0%B8%A2%E0%B8%B5%E0%B8%99-%E0%B8%99%E0%B9%89%E0%B8%B3%E0%B8%A2%E0%B8%B2%E0%B8%9B%E0%B8%A3%E0%B8%B1%E0%B8%9A%E0%B8%9C%E0%B9%89%E0%B8%B2%E0%B8%99%E0%B8%B8%E0%B9%88%E0%B8%A1-%E0%B8%A1%E0%B8%B4%E0%B8%A5%E0%B8%84%E0%B9%8C%E0%B8%81%E0%B8%B5%E0%B9%89%E0%B8%97%E0%B8%B1%E0%B8%8A-480-%E0%B8%A1%E0%B8%A5/328226/",
    # ไฮยีน เอ็กซ์เพิร์ทแคร์ 1000 มล.
    "https://allonline.7eleven.co.th/p/%E0%B9%84%E0%B8%AE%E0%B8%A2%E0%B8%B5%E0%B8%99-%E0%B9%80%E0%B8%AD%E0%B9%87%E0%B8%81%E0%B8%8B%E0%B9%8C%E0%B9%80%E0%B8%9E%E0%B8%B4%E0%B8%A3%E0%B9%8C%E0%B8%97%E0%B9%81%E0%B8%84%E0%B8%A3%E0%B9%8C-%E0%B8%99%E0%B9%89%E0%B8%B3%E0%B8%A2%E0%B8%B2%E0%B8%9B%E0%B8%A3%E0%B8%B1%E0%B8%9A%E0%B8%9C%E0%B9%89%E0%B8%B2%E0%B8%99%E0%B8%B8%E0%B9%88%E0%B8%A1-%E0%B8%82%E0%B8%B2%E0%B8%A7%E0%B8%A1%E0%B8%B4%E0%B8%A5%E0%B8%84%E0%B9%8C%E0%B8%81%E0%B8%B5%E0%B9%89-1000-%E0%B8%A1%E0%B8%A5/467880",
    # เปาวินวอช น้ำยาซักผ้าลิควิด 620 มล.
    "https://www.allonline.7eleven.co.th/p/%E0%B9%80%E0%B8%9B%E0%B8%B2%E0%B8%A7%E0%B8%B4%E0%B8%99%E0%B8%A7%E0%B8%AD%E0%B8%8A-%E0%B8%99%E0%B9%89%E0%B8%B3%E0%B8%A2%E0%B8%B2%E0%B8%8B%E0%B8%B1%E0%B8%81%E0%B8%9C%E0%B9%89%E0%B8%B2%E0%B8%A5%E0%B8%B4%E0%B8%84%E0%B8%A7%E0%B8%B4%E0%B8%94-620-%E0%B8%A1%E0%B8%A5/450704",
    # เปา ไวท์นาโนเทค 2400 กรัม
    "https://allonline.7eleven.co.th/p/%E0%B9%80%E0%B8%9B%E0%B8%B2-%E0%B9%84%E0%B8%A7%E0%B8%97%E0%B9%8C%E0%B8%99%E0%B8%B2%E0%B9%82%E0%B8%99%E0%B9%80%E0%B8%97%E0%B8%84%E0%B8%9C%E0%B8%87%E0%B8%8B%E0%B8%B1%E0%B8%81%E0%B8%9F%E0%B8%AD%E0%B8%812400-%E0%B8%81%E0%B8%A3%E0%B8%B1%E0%B8%A1/462743",
    # แอทแทค แฮปปี้ สวีท 1800 กรัม
    "https://allonline.7eleven.co.th/p/%E0%B9%81%E0%B8%AD%E0%B8%97%E0%B9%81%E0%B8%97%E0%B8%84-%E0%B8%9C%E0%B8%87%E0%B8%8B%E0%B8%B1%E0%B8%81%E0%B8%9F%E0%B8%AD%E0%B8%81-%E0%B9%81%E0%B8%AE%E0%B8%9B%E0%B8%9B%E0%B8%B5%E0%B9%89-%E0%B8%AA%E0%B8%A7%E0%B8%B5%E0%B8%97-1800-%E0%B8%81%E0%B8%A3%E0%B8%B1%E0%B8%A1-%E0%B9%81%E0%B8%9E%E0%B9%87%E0%B8%81%E0%B8%84%E0%B8%B9%E0%B9%88/346501",
    # แอทแทค แฮปปี้สวีท 2500 กรัม
    "https://allonline.7eleven.co.th/p/%E0%B9%81%E0%B8%AD%E0%B8%97%E0%B9%81%E0%B8%97%E0%B8%84-%E0%B8%9C%E0%B8%87%E0%B8%8B%E0%B8%B1%E0%B8%81%E0%B8%9F%E0%B8%AD%E0%B8%81-%E0%B9%81%E0%B8%AE%E0%B8%9B%E0%B8%9B%E0%B8%B5%E0%B9%89%E0%B8%AA%E0%B8%A7%E0%B8%B5%E0%B8%97-2500-%E0%B8%81%E0%B8%A3%E0%B8%B1%E0%B8%A1/334941",
    # โปร บลูพลัส 2400 กรัม
    "https://allonline.7eleven.co.th/p/%E0%B9%82%E0%B8%9B%E0%B8%A3-%E0%B8%9A%E0%B8%A5%E0%B8%B9%E0%B8%9E%E0%B8%A5%E0%B8%B1%E0%B8%AA-%E0%B8%9C%E0%B8%87%E0%B8%8B%E0%B8%B1%E0%B8%81%E0%B8%9F%E0%B8%AD%E0%B8%81-2400-%E0%B8%81%E0%B8%A3%E0%B8%B1%E0%B8%A1/462744",
    # เปา วินวอชลิควิด ถุงเติม 1500 มล.
    "https://www.allonline.7eleven.co.th/p/%E0%B9%80%E0%B8%9B%E0%B8%B2-%E0%B8%A7%E0%B8%B4%E0%B8%99%E0%B8%A7%E0%B8%AD%E0%B8%8A%E0%B8%A5%E0%B8%B4%E0%B8%84%E0%B8%A7%E0%B8%B4%E0%B8%94-%E0%B8%96%E0%B8%B8%E0%B8%87%E0%B9%80%E0%B8%95%E0%B8%B4%E0%B8%A1%E0%B8%99%E0%B9%89%E0%B8%B3%E0%B8%A2%E0%B8%B2%E0%B8%8B%E0%B8%B1%E0%B8%81%E0%B8%9C%E0%B9%89%E0%B8%B21500-%E0%B8%A1%E0%B8%A5/509381/",
    # ไฟน์ไลน์ ดีลักซ์ เพอร์ฟูม 490 มล.
    "https://www.allonline.7eleven.co.th/p/%E0%B9%84%E0%B8%9F%E0%B8%99%E0%B9%8C%E0%B9%84%E0%B8%A5%E0%B8%99%E0%B9%8C-%E0%B8%94%E0%B8%B5%E0%B8%A5%E0%B8%B1%E0%B8%81%E0%B8%8B%E0%B9%8C-%E0%B9%80%E0%B8%9E%E0%B8%AD%E0%B8%A3%E0%B9%8C%E0%B8%9F%E0%B8%B9%E0%B8%A1-%E0%B8%9B%E0%B8%A3%E0%B8%B1%E0%B8%9A%E0%B8%9C%E0%B9%89%E0%B8%B2%E0%B8%99%E0%B8%B8%E0%B9%88%E0%B8%A1%E0%B8%AA%E0%B8%B9%E0%B8%95%E0%B8%A3%E0%B9%80%E0%B8%82%E0%B9%89%E0%B8%A1%E0%B8%82%E0%B9%89%E0%B8%99%E0%B8%9E%E0%B8%B4%E0%B9%80%E0%B8%A8%E0%B8%A9-%E0%B9%80%E0%B8%97%E0%B8%99%E0%B9%80%E0%B8%94%E0%B8%AD%E0%B8%A3%E0%B9%8C-%E0%B9%80%E0%B8%8B%E0%B8%99%E0%B8%97%E0%B9%8C-%E0%B8%97%E0%B8%AD%E0%B8%87-490-%E0%B8%A1%E0%B8%A5/343673/",
]


# ---------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------
def main():
    print("=" * 60)
    print("7-Eleven AllOnline Scraper")
    print("=" * 60)

    # ---------- Category Scraping ----------
    all_scraped_dataframes = []
    for url in list_of_7eleven_urls:
        print(f"\n--- Starting scraping for URL: {url[:80]}... ---")
        try:
            df_current_url = scrape_7eleven_data(url)
            if not df_current_url.is_empty():
                all_scraped_dataframes.append(df_current_url)
        except Exception as e:
            print(f"Failed to scrape category URL: {e}")

    if all_scraped_dataframes:
        df_combined_7eleven = pl.concat(all_scraped_dataframes, how="vertical")
        df_combined_7eleven = df_combined_7eleven.unique(subset=["name"], keep="first")
        print(f"\nCombined data. Final unique items: {df_combined_7eleven.height}")
    else:
        print("No data was scraped from category URLs.")
        df_combined_7eleven = pl.DataFrame()

    # ---------- Watchlist Scraping ----------
    print("\n--- Scraping Watchlist Products ---")
    df_watchlist = scrape_specific_products(specific_product_urls)
    print("\n--- Watchlist Results ---")
    print(df_watchlist)

    cols_sel = ["name", "promotion_price", "original_price", "condition"]
    df_watchlist_sel = df_watchlist.select(cols_sel) if not df_watchlist.is_empty() else pl.DataFrame()

    # ---------- Combine (toggle here) ----------
    just_watchlist = True

    if just_watchlist:
        df_combined_sel = df_watchlist_sel
    else:
        df_combined_sel = df_combined_7eleven.select(cols_sel)
        df_combined_sel = pl.concat([df_combined_sel, df_watchlist_sel])

    # ---------- Data Prep ----------
    if not df_combined_sel.is_empty():
        df_prep = re_evaluate_price(df_combined_sel)
        df_trans = parse_product_names_TH(df_prep.unique(), "7-Eleven")

        output_file = OUTPUT_DIR / f"7_eleven_laundry_dish_{today_date}.xlsx"
        df_trans.write_excel(str(output_file))
        print(f"\n✅ Saved main output: {output_file}")

    # ---------- Watchlist Output ----------
    if not df_watchlist_sel.is_empty():
        df_prep_watchlist = re_evaluate_price(df_watchlist_sel)
        df_trans_watchlist = parse_product_names_TH(df_prep_watchlist.unique(), "7-Eleven")

        watchlist_file = OUTPUT_DIR / f"7_eleven_watchlist_{today_date}.xlsx"
        df_trans_watchlist.write_excel(str(watchlist_file))
        print(f"✅ Saved watchlist output: {watchlist_file}")

    print("\n" + "=" * 60)
    print("Scraping completed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
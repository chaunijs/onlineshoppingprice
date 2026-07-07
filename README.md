# 🛒 Online Shopping Price Tracker

> Track and analyze product prices across online retailers 📉📊  

This project helps you **track product prices from online shopping websites** for analysis purposes. Since **each retailer has a different web page structure**, the scraping logic must be adjusted per site.

---

## ✨ Features

✅ Track prices by **product** or **category** ✅ Automated tracking via **GitHub Actions** (Runs Monday, Wednesday, Friday)   
✅ Centralized execution using an `orchestrator.py` script  
✅ Bypasses bot detection using **Playwright** & **Patchright** ✅ Built mainly for **data analysis & learning**

### 🛍️ Supported Retailers
Currently, the orchestrator handles sequential scraping for:
* **7-Eleven**
* **Makro**
* **Lotus's**
* **Tops**
* *Big C (Currently WIP / Requires OCR)*

---

## 🧠 How It Works

Because every retailer structures their website differently:

### 🔍 Technical Approach
1. **Web Scraping Automation:** Scripts are written in Python using Playwright/Patchright to handle headless browser scraping and bypass Cloudflare/bot protections.
2. **Orchestrator (`orchestrator.py`):** Runs the individual store scrapers sequentially. If one fails, it logs the error and continues to the next.
3. **Artifact Generation:** The scraped data is exported to Excel/CSV files in the `py/output/` directory.

### 🙋 For Users
If you want data from your **interested product or category**:
1. Clone the repository
2. Modify the **URL** inside the specific retailer's scraper script in the `py/` folder.
3. Run the pipeline:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   python orchestrator.py
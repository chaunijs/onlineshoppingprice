# -*- coding: utf-8 -*-
"""
Supabase Database Sink Utility
Efficiently sinks Polars DataFrames into Supabase tables:
- 'price_catalog'
- 'watchlist'
- 'product_name'

Supports two connection modes:
1. Supabase REST API (via SUPABASE_URL + SUPABASE_SECRET_KEY / SUPABASE_KEY)
2. Direct PostgreSQL connection (via DATABASE_URL or SUPABASE_DB_PASSWORD)
"""

import os
import sys
from pathlib import Path
from typing import Optional
import datetime

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import polars as pl
import requests

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None
    execute_values = None

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

# Load .env from project root if present
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

try:
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH)
    else:
        load_dotenv()
except ImportError:
    if ENV_PATH.exists():
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip("'\""))
        except Exception:
            pass


CATALOG_COLUMNS = [
    "date", "retailer", "brand", "name", "volume",
    "unit", "pack", "original_price", "promotion_price", "condition"
]

WATCHLIST_COLUMNS = [
    "date", "retailer", "brand", "name", "volume",
    "unit", "pack", "original_price", "promotion_price", "condition", "url"
]

PRODUCT_NAME_COLUMNS = [
    "product_id", "product_name", "category", "retailer", "date"
]


def get_connection():
    """
    Establishes and returns a direct connection to Supabase PostgreSQL.
    Tries pooler / connection string parameters from environment.
    """
    if psycopg2 is None:
        raise ImportError("psycopg2 is not installed.")

    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return psycopg2.connect(db_url, connect_timeout=10)

    host = os.getenv("SUPABASE_DB_HOST", "aws-0-ap-northeast-1.pooler.supabase.com")
    port = int(os.getenv("SUPABASE_DB_PORT", "6543"))
    user = os.getenv("SUPABASE_DB_USER", "postgres.poctuwzyarqoycksmxte")
    password = os.getenv("SUPABASE_DB_PASSWORD", "")
    database = os.getenv("SUPABASE_DB_NAME", "postgres")

    if not password:
        raise ValueError("SUPABASE_DB_PASSWORD environment variable is not set.")

    return psycopg2.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        connect_timeout=10
    )


def _prepare_records(
    df: pl.DataFrame,
    table_name: str,
    available_cols: list[str]
) -> list[dict]:
    """
    Prepares and cleans records from DataFrame according to the target table schema.
    """
    raw_dicts = df.select(available_cols).to_dicts()
    cleaned_records = []
    skipped_count = 0

    for r in raw_dicts:
        item = {}
        for col in available_cols:
            val = r.get(col)

            # Date formatting
            if hasattr(val, "isoformat"):
                val = val.isoformat()

            # Numeric cleaning for price catalog / watchlist
            if col in ("volume", "original_price", "promotion_price"):
                if val is None or (isinstance(val, float) and val != val) or val == "":
                    val = None
                else:
                    try:
                        val = float(val)
                    except (ValueError, TypeError):
                        val = None

            # BigInt conversion for product_name table
            elif col == "product_id":
                try:
                    val = int(val)
                except (ValueError, TypeError):
                    val = None

            # Generic string/NaN cleaning
            elif isinstance(val, float) and val != val:
                val = None
            elif val == "":
                val = None

            item[col] = val

        # Skip product_name rows where product_id is invalid (NOT NULL column)
        if table_name == "product_name" and item.get("product_id") is None:
            skipped_count += 1
            continue

        cleaned_records.append(item)

    if skipped_count > 0:
        print(f"⚠️ [Supabase Warning] Skipped {skipped_count} row(s) in '{table_name}' due to invalid/missing product_id.")

    return cleaned_records


def _sink_via_rest(
    records: list[dict],
    table_name: str,
    batch_size: int = 500
) -> bool:
    """
    Inserts records into Supabase using PostgREST API with SUPABASE_URL and SUPABASE_SECRET_KEY.
    """
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        print("❌ [Supabase REST] SUPABASE_URL or SUPABASE_SECRET_KEY is missing from environment.")
        return False

    endpoint = f"{supabase_url}/rest/v1/{table_name}"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }

    total = len(records)
    for i in range(0, total, batch_size):
        batch = records[i:i + batch_size]
        batch_num = (i // batch_size) + 1
        resp = None

        try:
            resp = requests.post(endpoint, headers=headers, json=batch, timeout=30)
        except requests.exceptions.SSLError:
            resp = requests.post(endpoint, headers=headers, json=batch, timeout=30, verify=False)
        except Exception as req_ex:
            print(f"❌ [Supabase REST Connection Error] Batch {batch_num} for table '{table_name}': {req_ex}")
            raise req_ex

        if not resp.ok:
            error_msg = resp.text.strip()
            print(f"❌ [Supabase REST HTTP ERROR] Status {resp.status_code} on batch {batch_num} for table '{table_name}':")
            print(f"   Response Payload: {error_msg}")
            resp.raise_for_status()

    print(f"✅ [Supabase REST] Successfully inserted {total} rows into '{table_name}'.")
    return True


def _sink_via_postgres(
    records: list[dict],
    table_name: str,
    available_cols: list[str],
    page_size: int = 1000
) -> bool:
    """
    Inserts records into Supabase using direct psycopg2 connection.
    """
    tuple_data = []
    for r in records:
        tuple_data.append(tuple(r.get(col) for col in available_cols))

    cols_str = ", ".join(f'"{col}"' for col in available_cols)
    insert_sql = f'INSERT INTO public."{table_name}" ({cols_str}) VALUES %s'

    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            execute_values(cur, insert_sql, tuple_data, page_size=page_size)
        conn.commit()
        print(f"✅ [Supabase Postgres] Successfully inserted {len(tuple_data)} rows into public.{table_name}.")
        return True
    finally:
        if conn:
            conn.close()


def sink_to_supabase(
    df: pl.DataFrame,
    table_name: str,
    page_size: int = 1000
) -> bool:
    """
    Inserts a Polars DataFrame into the specified Supabase table:
    - 'price_catalog'
    - 'watchlist'
    - 'product_name'

    Tries Supabase REST API first (SUPABASE_URL + SUPABASE_SECRET_KEY).
    Falls back to direct PostgreSQL (DATABASE_URL / SUPABASE_DB_PASSWORD) if REST is unavailable.
    Prints prominent error messages if the sink fails for any reason.
    """
    if df is None or df.is_empty():
        print(f"⚠️ [Supabase Warning] DataFrame is empty (0 rows). Skipping sink to table '{table_name}'.")
        return False

    table_name = table_name.lower().strip()
    if table_name == "price_catalog":
        target_columns = CATALOG_COLUMNS
    elif table_name == "watchlist":
        target_columns = WATCHLIST_COLUMNS
    elif table_name == "product_name":
        target_columns = PRODUCT_NAME_COLUMNS
    else:
        target_columns = [col for col in df.columns if col not in ("id", "created_at")]

    available_cols = [c for c in target_columns if c in df.columns]
    if not available_cols:
        print(f"❌ [Supabase ERROR] No matching target columns found for table '{table_name}'!")
        print(f"   Target schema expected: {target_columns}")
        print(f"   Available DataFrame columns: {df.columns}")
        return False

    records = _prepare_records(df, table_name, available_cols)
    if not records:
        print(f"❌ [Supabase ERROR] No valid records prepared after cleaning for table '{table_name}'. Skipping.")
        return False

    # Check available credentials
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_KEY")
    has_rest = bool(supabase_url and supabase_key)
    has_pg = bool(os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_PASSWORD"))

    if not has_rest and not has_pg:
        print(f"\n" + "!" * 70)
        print(f"❌ [Supabase CRITICAL ERROR] Cannot sink to '{table_name}': No credentials configured!")
        print(f"   - SUPABASE_URL: {'[SET]' if supabase_url else '[MISSING]'}")
        print(f"   - SUPABASE_SECRET_KEY: {'[SET]' if supabase_key else '[MISSING]'}")
        print(f"   - SUPABASE_DB_PASSWORD: {'[SET]' if os.getenv('SUPABASE_DB_PASSWORD') else '[MISSING]'}")
        print(f"   Please check your GitHub Actions Repository Secrets or .env file.")
        print("!" * 70 + "\n")
        return False

    # 1. Attempt REST API
    if has_rest:
        try:
            return _sink_via_rest(records, table_name, batch_size=500)
        except Exception as rest_err:
            print(f"⚠️ [Supabase REST Failed] Table '{table_name}' REST insert error: {rest_err}")
            if has_pg:
                print(f"   Attempting direct Postgres fallback...")
            else:
                print(f"❌ [Supabase ERROR] No Postgres credentials available for fallback. Sinking to '{table_name}' failed.")
                return False

    # 2. Fallback to direct Postgres
    if has_pg:
        try:
            return _sink_via_postgres(records, table_name, available_cols, page_size=page_size)
        except Exception as pg_err:
            print(f"❌ [Supabase Postgres ERROR] Direct connection failed for table '{table_name}': {pg_err}")
            return False

    return False


if __name__ == "__main__":
    print("Testing Supabase connection and sink utility...")
    # 1. Test REST API
    s_url = os.getenv("SUPABASE_URL")
    s_key = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_KEY")
    if s_url and s_key:
        print(f"Testing Supabase REST API at: {s_url}")
        headers = {"apikey": s_key, "Authorization": f"Bearer {s_key}"}
        for tbl in ("price_catalog", "watchlist", "product_name"):
            try:
                endpoint = f"{s_url.rstrip('/')}/rest/v1/{tbl}?limit=1"
                try:
                    r = requests.get(endpoint, headers=headers, timeout=10)
                except requests.exceptions.SSLError:
                    r = requests.get(endpoint, headers=headers, timeout=10, verify=False)
                if r.status_code in (200, 206):
                    print(f"✅ REST API: Table '{tbl}' accessible.")
                else:
                    print(f"❌ REST API: Table '{tbl}' returned status {r.status_code}: {r.text[:150]}")
            except Exception as ex:
                print(f"❌ REST API test failed for '{tbl}': {ex}")
    else:
        print("⚠️ No REST API credentials found (SUPABASE_URL / SUPABASE_SECRET_KEY).")

    # 2. Test Direct Postgres (if configured)
    if os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_PASSWORD"):
        print("\nTesting direct Postgres connection...")
        try:
            c = get_connection()
            with c.cursor() as cur:
                cur.execute("SELECT current_database(), current_user, version();")
                info = cur.fetchone()
                print(f"✅ Connected to: DB='{info[0]}', USER='{info[1]}'")
            c.close()
        except Exception as err:
            print(f"❌ Postgres connection failed: {err}")

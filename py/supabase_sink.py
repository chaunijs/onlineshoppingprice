# -*- coding: utf-8 -*-
"""
Supabase Database Sink Utility
Efficiently sinks Polars DataFrames into Supabase PostgreSQL tables:
- 'price_catalog'
- 'watchlist'

Reads connection credentials securely from .env or environment variables.
Uses psycopg2.extras.execute_values for high-speed batch insertion.
"""

import os
import sys
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import polars as pl
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

# Load .env from project root if present
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
else:
    load_dotenv()


CATALOG_COLUMNS = [
    "date", "retailer", "brand", "name", "volume",
    "unit", "pack", "original_price", "promotion_price", "condition"
]

WATCHLIST_COLUMNS = [
    "date", "retailer", "brand", "name", "volume",
    "unit", "pack", "original_price", "promotion_price", "condition", "url"
]


def get_connection():
    """
    Establishes and returns a connection to Supabase PostgreSQL.
    Tries pooler / connection string parameters from environment.
    """
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


def sink_to_supabase(
    df: pl.DataFrame,
    table_name: str,
    page_size: int = 1000
) -> bool:
    """
    Inserts a Polars DataFrame into the specified Supabase table ('price_catalog' or 'watchlist').
    Automatically validates column alignment and converts types to Python native.
    Returns True if successful, False otherwise.
    """
    if df is None or df.is_empty():
        print(f"[Supabase] DataFrame is empty. Skipping sink to '{table_name}'.")
        return False

    table_name = table_name.lower().strip()
    if table_name == "price_catalog":
        target_columns = CATALOG_COLUMNS
    elif table_name == "watchlist":
        target_columns = WATCHLIST_COLUMNS
    else:
        target_columns = [col for col in df.columns if col != "id" and col != "created_at"]

    # Filter to only the target columns present
    available_cols = [c for c in target_columns if c in df.columns]
    if not available_cols:
        print(f"[Supabase] [!] No matching target columns found for table '{table_name}'. Columns: {df.columns}")
        return False

    df_subset = df.select(available_cols)
    rows = df_subset.to_dicts()

    # Convert rows to list of tuples in exact column order, replacing NaNs/invalids with None
    tuple_data = []
    for r in rows:
        row_tuple = []
        for col in available_cols:
            val = r.get(col)
            # Normalize float NaN or empty string in numeric fields to None
            if col in ["volume", "original_price", "promotion_price"] and (val is None or val != val):
                val = None
            row_tuple.append(val)
        tuple_data.append(tuple(row_tuple))

    cols_str = ", ".join(f'"{col}"' for col in available_cols)
    insert_sql = f'INSERT INTO public."{table_name}" ({cols_str}) VALUES %s'

    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            execute_values(cur, insert_sql, tuple_data, page_size=page_size)
        conn.commit()
        print(f"[Supabase] [OK] Successfully inserted {len(tuple_data)} rows into public.{table_name}.")
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[Supabase] [!] Error sinking data to table '{table_name}': {e}")
        return False
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    print("Testing Supabase connection and sink utility...")
    # Test connection
    try:
        c = get_connection()
        with c.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, version();")
            info = cur.fetchone()
            print(f"[+] Connected to: DB='{info[0]}', USER='{info[1]}'")
        c.close()
        print("[+] Supabase sink utility is configured properly.")
    except Exception as err:
        print(f"[!] Connection test failed: {err}")

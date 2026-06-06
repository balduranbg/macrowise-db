#!/usr/bin/env python3
"""
build_db.py — Macrowise food database builder

Downloads Open Food Facts CSV dump and USDA FoodData Central JSON,
strips to Macrowise-relevant fields, builds a SQLite database with
FTS5 full-text search across all available languages, and compresses
the result.

Output: macrowise-foods.db.gz

Usage:
    python3 build_db.py [--off-only] [--usda-only] [--limit N] [--no-compress]

Dependencies:
    pip install requests tqdm

Environment variables (optional):
    USDA_API_KEY   — USDA FoodData Central API key (falls back to DEMO_KEY)
"""

import argparse
import csv
import sys

# OFF CSV has some very large fields — increase the limit
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from tqdm import tqdm

from categories import normalise_category, normalise_usda_category

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OFF_CSV_URL = "https://static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz"
USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/list"

DB_NAME = "macrowise-foods.db"
GZ_NAME = "macrowise-foods.db.gz"
VERSION_FILE = "version.json"

# Minimum nutrient requirements — products missing any of these are excluded
REQUIRED_NUTRIENTS = {"calories", "protein", "fat", "carbs"}

# USDA nutrient ID -> Macrowise field name
USDA_NUTRIENT_MAP = {
    2047: "calories",       # Energy (Atwater General) — preferred
    2048: "calories_alt",   # Energy (Atwater Specific) — fallback
    1008: "calories_leg",   # Energy legacy — second fallback
    1003: "protein",
    1004: "fat",
    1005: "carbs",
    1079: "fiber",
    1063: "sugar",
    2000: "sugar_alt",      # Sugars (SR Legacy / Branded)
    1093: "sodium",         # grams — convert to mg
    1092: "potassium",      # grams — convert to mg
    1258: "saturated_fat",
    1253: "cholesterol",
}

# OFF nutrient field suffixes (per 100g)
OFF_NUTRIENT_MAP = {
    "energy-kcal_100g":     "calories",
    "proteins_100g":        "protein",
    "fat_100g":             "fat",
    "carbohydrates_100g":   "carbs",
    "fiber_100g":           "fiber",
    "sugars_100g":          "sugar",
    "sodium_100g":          "sodium",      # grams in OFF — convert to mg
    "potassium_100g":       "potassium",   # grams in OFF — convert to mg
    "saturated-fat_100g":   "saturated_fat",
    "cholesterol_100g":     "cholesterol",
}

# Languages to extract from OFF (all available)
# The build script dynamically discovers language columns, but we define
# the preferred display language order for the fallback display name.
DISPLAY_LANGUAGE_PRIORITY = [
    "en", "fr", "de", "es", "it", "nl", "pl", "pt", "ru", "bg",
    "ro", "hu", "cs", "sk", "sv", "da", "fi", "no", "el", "hr",
    "sl", "lt", "lv", "et", "uk", "tr", "ar", "ja", "zh", "ko",
]

# Batch sizes
OFF_CHUNK_SIZE = 10_000     # rows processed per commit
USDA_PAGE_SIZE = 200        # items per USDA API page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = -64000;

CREATE TABLE IF NOT EXISTS foods (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode             TEXT,
    name                TEXT NOT NULL,
    brand               TEXT,
    source              TEXT NOT NULL,      -- 'OFF' or 'USDA'
    image_url           TEXT,
    food_category       TEXT NOT NULL DEFAULT 'generic',
    calories            REAL NOT NULL,
    protein             REAL NOT NULL,
    fat                 REAL NOT NULL,
    carbs               REAL NOT NULL,
    fiber               REAL,
    sugar               REAL,
    sodium              REAL,               -- mg per 100g
    potassium           REAL,               -- mg per 100g
    saturated_fat       REAL,
    cholesterol         REAL,
    serving_description TEXT,
    serving_grams       REAL,
    popularity          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS food_names (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    food_id     INTEGER NOT NULL,
    language    TEXT NOT NULL,              -- ISO 639-1 code, e.g. 'en', 'bg'
    name        TEXT NOT NULL,
    FOREIGN KEY (food_id) REFERENCES foods(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_foods_barcode   ON foods(barcode);
CREATE INDEX IF NOT EXISTS idx_foods_source    ON foods(source);
CREATE INDEX IF NOT EXISTS idx_foods_category  ON foods(food_category);
CREATE INDEX IF NOT EXISTS idx_food_names_food ON food_names(food_id);
CREATE INDEX IF NOT EXISTS idx_food_names_lang ON food_names(language);

CREATE VIRTUAL TABLE IF NOT EXISTS food_names_fts USING fts5(
    name,
    language UNINDEXED,
    content='food_names',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS food_names_ai
AFTER INSERT ON food_names BEGIN
    INSERT INTO food_names_fts(rowid, name, language)
    VALUES (new.id, new.name, new.language);
END;

CREATE TRIGGER IF NOT EXISTS food_names_ad
AFTER DELETE ON food_names BEGIN
    INSERT INTO food_names_fts(food_names_fts, rowid, name, language)
    VALUES ('delete', old.id, old.name, old.language);
END;
"""


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def safe_float(value) -> float | None:
    """Convert a value to float, returning None on failure."""
    if value is None or value == "" or value == "unknown":
        return None
    try:
        f = float(value)
        return None if f < 0 else f
    except (ValueError, TypeError):
        return None


def build_off_image_url(barcode: str) -> str | None:
    """Construct the OFF front image thumbnail URL from a barcode."""
    if not barcode or not barcode.isdigit():
        return None
    # OFF stores images in barcode-based paths
    # e.g. barcode 3017620422003 -> 301/762/042/2003/
    b = barcode.zfill(13)
    path = f"{b[0:3]}/{b[3:6]}/{b[6:9]}/{b[9:]}"
    return f"https://images.openfoodfacts.org/images/products/{path}/front_en.100.jpg"


def pick_display_name(names: dict[str, str]) -> str | None:
    """
    Pick the best display name from a language->name dict.
    Prefers 'en' first, then languages in DISPLAY_LANGUAGE_PRIORITY order,
    then falls back to any non-empty name.
    """
    # Try English first (most common in OFF CSV)
    if names.get("en", "").strip():
        return names["en"].strip()
    # Try priority languages
    for lang in DISPLAY_LANGUAGE_PRIORITY:
        if names.get(lang, "").strip():
            return names[lang].strip()
    # Take any non-empty name
    for name in names.values():
        if name and name.strip():
            return name.strip()
    return None


def insert_food(conn: sqlite3.Connection, food: dict, names: dict[str, str]) -> int | None:
    """
    Insert one food record and its multilingual names.
    Returns the new food id, or None if the record was skipped.
    """
    # Validate required nutrients
    for field in REQUIRED_NUTRIENTS:
        if food.get(field) is None:
            return None

    # Round all numeric nutrient fields to 2 decimal places
    nutrient_fields = ["calories", "protein", "fat", "carbs", "fiber",
                       "sugar", "sodium", "potassium", "saturated_fat", "cholesterol"]
    for field in nutrient_fields:
        if food.get(field) is not None:
            food[field] = round(float(food[field]), 2)

    # Need at least one name
    display_name = pick_display_name(names)
    if not display_name:
        return None

    cur = conn.execute("""
        INSERT INTO foods (
            barcode, name, brand, source, image_url, food_category,
            calories, protein, fat, carbs, fiber, sugar,
            sodium, potassium, saturated_fat, cholesterol,
            serving_description, serving_grams, popularity
        ) VALUES (
            :barcode, :name, :brand, :source, :image_url, :food_category,
            :calories, :protein, :fat, :carbs, :fiber, :sugar,
            :sodium, :potassium, :saturated_fat, :cholesterol,
            :serving_description, :serving_grams, :popularity
        )
    """, {
        "barcode":             food.get("barcode"),
        "name":                display_name,
        "brand":               food.get("brand"),
        "source":              food["source"],
        "image_url":           food.get("image_url"),
        "food_category":       food.get("food_category", "generic"),
        "calories":            food["calories"],
        "protein":             food["protein"],
        "fat":                 food["fat"],
        "carbs":               food["carbs"],
        "fiber":               food.get("fiber"),
        "sugar":               food.get("sugar"),
        "sodium":              food.get("sodium"),
        "potassium":           food.get("potassium"),
        "saturated_fat":       food.get("saturated_fat"),
        "cholesterol":         food.get("cholesterol"),
        "serving_description": food.get("serving_description"),
        "serving_grams":       food.get("serving_grams"),
        "popularity":          food.get("popularity", 0),
    })
    food_id = cur.lastrowid

    # Insert all available language names (deduplicated)
    seen_names: set[tuple[str, str]] = set()
    for lang, name in names.items():
        name = name.strip()
        if not name:
            continue
        key = (lang, name.lower())
        if key in seen_names:
            continue
        seen_names.add(key)
        conn.execute(
            "INSERT INTO food_names (food_id, language, name) VALUES (?, ?, ?)",
            (food_id, lang, name)
        )

    return food_id


# ---------------------------------------------------------------------------
# Open Food Facts processing
# ---------------------------------------------------------------------------

def download_off_csv(dest_path: str) -> None:
    """Download the OFF CSV gz to dest_path, showing a progress bar."""
    log.info(f"Downloading OFF CSV dump from {OFF_CSV_URL}")
    log.info("This is a large file (~900MB). Please be patient.")

    headers = {
        "User-Agent": "Macrowise/1.0 (contact@macrowise.app)",
        "Accept-Encoding": "identity",  # Prevent double-compression issues
    }

    response = requests.get(OFF_CSV_URL, stream=True, headers=headers)
    response.raise_for_status()

    total = int(response.headers.get("content-length", 0))
    with open(dest_path, "wb") as f, tqdm(
        total=total, unit="B", unit_scale=True, desc="OFF download"
    ) as bar:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            bar.update(len(chunk))

    log.info(f"OFF CSV downloaded to {dest_path}")


def process_off_csv(conn: sqlite3.Connection, csv_gz_path: str,
                    limit: int | None = None) -> dict:
    """
    Stream-process the OFF CSV, inserting qualifying products into the DB.
    Returns stats dict.
    """
    stats = {"processed": 0, "inserted": 0, "skipped_nutrients": 0,
             "skipped_noname": 0, "errors": 0}

    log.info("Processing OFF CSV dump...")

    with gzip.open(csv_gz_path, "rt", encoding="utf-8", errors="replace") as gz:
        reader = csv.DictReader(gz, delimiter="\t")
        headers = reader.fieldnames or []

        # OFF CSV has a single product_name column — no per-language variants
        # Multilingual names are only available via the API, not the CSV dump
        # We store product_name as 'en' by default (most OFF names are in the
        # product's local language, but 'en' is the most common single language)
        # The API-based search (via Worker) handles multilingual queries separately
        log.info("OFF CSV uses single product_name column — storing as primary name")

        batch_count = 0

        for row in tqdm(reader, desc="OFF products", unit=" products"):
            if limit and stats["processed"] >= limit:
                break

            stats["processed"] += 1

            try:
                # --- Extract nutrients ---
                nutrients = {}
                for off_field, mw_field in OFF_NUTRIENT_MAP.items():
                    val = safe_float(row.get(off_field))
                    if val is not None:
                        nutrients[mw_field] = val

                # Convert sodium and potassium from g to mg
                if "sodium" in nutrients:
                    nutrients["sodium"] = round(nutrients["sodium"] * 1000, 2)
                if "potassium" in nutrients:
                    nutrients["potassium"] = round(nutrients["potassium"] * 1000, 2)

                # Round calories to nearest integer
                if "calories" in nutrients:
                    nutrients["calories"] = round(nutrients["calories"])

                # Check required nutrients present
                missing = REQUIRED_NUTRIENTS - set(nutrients.keys())
                if missing:
                    stats["skipped_nutrients"] += 1
                    continue

                # --- Extract name ---
                # Use product_name as primary; fall back to generic_name
                name = (row.get("product_name", "") or
                        row.get("generic_name", "") or "").strip()
                if not name:
                    stats["skipped_noname"] += 1
                    continue

                # Store under 'en' as the primary language
                # Additional language names can be enriched later via API
                names = {"en": name}

                # Also store abbreviated name if different
                abbrev = row.get("abbreviated_product_name", "").strip()
                if abbrev and abbrev.lower() != name.lower():
                    names["en_abbrev"] = abbrev

                # --- Extract metadata ---
                barcode = row.get("code", "").strip() or None
                brand = row.get("brands", "").strip() or None
                if brand:
                    # OFF sometimes has multiple brands comma-separated; take first
                    brand = brand.split(",")[0].strip()

                # Category
                cats_raw = row.get("categories_tags", "")
                cats = [c.strip() for c in cats_raw.split(",") if c.strip()] if cats_raw else []
                food_category = normalise_category(cats)

                # Image URL
                image_url = None
                if barcode:
                    # Check if OFF has a front image for this product
                    image_field = row.get("image_front_small_url", "").strip()
                    if image_field:
                        image_url = image_field
                    else:
                        image_url = build_off_image_url(barcode)

                # Serving size
                serving_desc = row.get("serving_size", "").strip() or None
                serving_grams = safe_float(row.get("serving_quantity"))

                # Popularity
                popularity = safe_float(row.get("popularity_key")) or 0

                food = {
                    **nutrients,
                    "barcode":             barcode,
                    "brand":               brand,
                    "source":              "OFF",
                    "image_url":           image_url,
                    "food_category":       food_category,
                    "serving_description": serving_desc,
                    "serving_grams":       serving_grams,
                    "popularity":          int(popularity),
                }

                result = insert_food(conn, food, names)
                if result is not None:
                    stats["inserted"] += 1
                else:
                    stats["skipped_noname"] += 1

            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 10:  # Log first 10 errors only
                    log.warning(f"Error processing OFF row: {e}")

            # Commit in batches
            batch_count += 1
            if batch_count >= OFF_CHUNK_SIZE:
                conn.commit()
                batch_count = 0

    conn.commit()
    log.info(f"OFF processing complete: {stats}")
    return stats


# ---------------------------------------------------------------------------
# USDA FoodData Central processing
# ---------------------------------------------------------------------------

def process_usda(conn: sqlite3.Connection, api_key: str,
                 limit: int | None = None) -> dict:
    """
    Fetch USDA foods via the FoodData Central API and insert into DB.
    Fetches Foundation Foods and SR Legacy data types.
    Returns stats dict.
    """
    stats = {"processed": 0, "inserted": 0, "skipped_nutrients": 0,
             "skipped_noname": 0, "errors": 0, "api_errors": 0}

    log.info("Fetching USDA FoodData Central foods...")

    data_types = ["Foundation", "SR Legacy"]
    page_number = 1
    total_fetched = 0

    while True:
        if limit and total_fetched >= limit:
            break

        params = {
            "api_key":    api_key,
            "dataType":   data_types,
            "pageSize":   USDA_PAGE_SIZE,
            "pageNumber": page_number,
            "nutrients":  list(USDA_NUTRIENT_MAP.keys()),
        }

        try:
            response = requests.get(USDA_SEARCH_URL, params=params, timeout=30)
            response.raise_for_status()
            foods = response.json()
        except requests.RequestException as e:
            stats["api_errors"] += 1
            log.warning(f"USDA API error on page {page_number}: {e}")
            if stats["api_errors"] > 5:
                log.error("Too many USDA API errors, stopping USDA processing")
                break
            time.sleep(5)
            continue

        if not foods:
            break  # No more pages

        for item in foods:
            if limit and total_fetched >= limit:
                break

            stats["processed"] += 1
            total_fetched += 1

            try:
                # --- Extract nutrients ---
                raw_nutrients: dict[str, float] = {}
                for n in item.get("foodNutrients", []):
                    nid = n.get("nutrientId")
                    val = safe_float(n.get("value") or n.get("amount"))
                    if nid in USDA_NUTRIENT_MAP and val is not None:
                        field = USDA_NUTRIENT_MAP[nid]
                        raw_nutrients[field] = val

                nutrients: dict[str, float] = {}

                # Resolve calories with fallback chain
                calories = (raw_nutrients.get("calories") or
                            raw_nutrients.get("calories_alt") or
                            raw_nutrients.get("calories_leg"))
                if calories is not None:
                    nutrients["calories"] = round(calories)

                # Resolve sugar with fallback
                sugar = raw_nutrients.get("sugar") or raw_nutrients.get("sugar_alt")
                if sugar is not None:
                    nutrients["sugar"] = sugar

                # Copy remaining fields
                for field in ("protein", "fat", "carbs", "fiber",
                              "saturated_fat", "cholesterol"):
                    if field in raw_nutrients:
                        nutrients[field] = raw_nutrients[field]

                # Convert sodium and potassium g -> mg
                for field in ("sodium", "potassium"):
                    if field in raw_nutrients:
                        nutrients[field] = round(raw_nutrients[field] * 1000, 2)

                # Check required nutrients
                missing = REQUIRED_NUTRIENTS - set(nutrients.keys())
                if missing:
                    stats["skipped_nutrients"] += 1
                    continue

                # --- Name ---
                name_en = item.get("description", "").strip()
                if not name_en:
                    stats["skipped_noname"] += 1
                    continue

                names = {"en": name_en}

                # Some USDA items have a commonNames field
                common = item.get("commonNames", "")
                if common and common.strip():
                    # Store as additional English name variant
                    names["en_common"] = common.strip()

                # --- Category ---
                food_group = item.get("foodCategory", "")
                food_category = normalise_usda_category(food_group)

                food = {
                    **nutrients,
                    "barcode":             None,   # USDA has no barcodes
                    "brand":               None,   # USDA is generic
                    "source":              "USDA",
                    "image_url":           None,   # No product images in USDA
                    "food_category":       food_category,
                    "serving_description": None,
                    "serving_grams":       None,
                    "popularity":          0,
                }

                result = insert_food(conn, food, names)
                if result is not None:
                    stats["inserted"] += 1
                else:
                    stats["skipped_noname"] += 1

            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 10:
                    log.warning(f"Error processing USDA item: {e}")

        # Commit after each page
        conn.commit()
        log.info(f"USDA page {page_number}: fetched {len(foods)} items, "
                 f"total inserted so far: {stats['inserted']}")

        if len(foods) < USDA_PAGE_SIZE:
            break  # Last page

        page_number += 1
        time.sleep(0.1)  # Be polite to the USDA API

    conn.commit()
    log.info(f"USDA processing complete: {stats}")
    return stats


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

def create_indexes(conn: sqlite3.Connection) -> None:
    """Create additional indexes after bulk insert for better query performance."""
    log.info("Creating additional indexes...")
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_foods_popularity
            ON foods(popularity DESC);
        CREATE INDEX IF NOT EXISTS idx_foods_source_category
            ON foods(source, food_category);
        ANALYZE;
    """)
    conn.commit()
    log.info("Indexes created")


def compress_db(db_path: str, gz_path: str) -> str:
    """Compress the SQLite database with gzip. Returns SHA256 of the gz file."""
    log.info(f"Compressing {db_path} -> {gz_path}")

    db_size = os.path.getsize(db_path)
    log.info(f"Uncompressed size: {db_size / 1024 / 1024:.1f} MB")

    sha256 = hashlib.sha256()

    with open(db_path, "rb") as f_in, \
         gzip.open(gz_path, "wb", compresslevel=9) as f_out, \
         tqdm(total=db_size, unit="B", unit_scale=True, desc="Compressing") as bar:
        while True:
            chunk = f_in.read(1024 * 1024)
            if not chunk:
                break
            f_out.write(chunk)
            sha256.update(chunk)
            bar.update(len(chunk))

    gz_size = os.path.getsize(gz_path)
    checksum = sha256.hexdigest()

    log.info(f"Compressed size: {gz_size / 1024 / 1024:.1f} MB "
             f"(ratio: {gz_size/db_size:.1%})")
    log.info(f"SHA256: {checksum}")

    return checksum


def write_version_file(version: str, checksum: str, output_path: str,
                       gz_path: str, stats: dict) -> None:
    """Write a version.json file alongside the database."""
    output_size = os.path.getsize(output_path)

    version_data = {
        "version":          version,
        "built_at":         datetime.now(timezone.utc).isoformat(),
        "checksum_sha256":  checksum,
        "file_bytes":       output_size,
        "stats":            stats,
    }

    with open(VERSION_FILE, "w") as f:
        json.dump(version_data, f, indent=2)

    log.info(f"Version file written: {VERSION_FILE}")
    log.info(json.dumps(version_data, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build macrowise-foods.db from OFF and USDA sources"
    )
    parser.add_argument("--off-only",    action="store_true",
                        help="Process only Open Food Facts")
    parser.add_argument("--usda-only",   action="store_true",
                        help="Process only USDA FoodData Central")
    parser.add_argument("--limit",       type=int, default=None,
                        help="Limit rows per source (for testing)")
    parser.add_argument("--no-compress", action="store_true",
                        help="Skip gzip compression (for testing)")
    parser.add_argument("--keep-csv",    action="store_true",
                        help="Keep downloaded OFF CSV after processing")
    args = parser.parse_args()

    # Version string: YYYY-MM
    version = datetime.now(timezone.utc).strftime("%Y-%m")

    log.info(f"=== Macrowise Food Database Builder ===")
    log.info(f"Version: {version}")
    log.info(f"Sources: {'OFF only' if args.off_only else 'USDA only' if args.usda_only else 'OFF + USDA'}")
    if args.limit:
        log.info(f"Limit: {args.limit} rows per source (TEST MODE)")

    # USDA API key
    usda_key = os.environ.get("USDA_API_KEY", "DEMO_KEY")
    if usda_key == "DEMO_KEY":
        log.warning("Using DEMO_KEY for USDA — rate limits apply. "
                    "Set USDA_API_KEY env var for production.")

    # Work directory — use persistent cache dir if --keep-csv, otherwise temp
    if args.keep_csv:
        work_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache")
        os.makedirs(work_dir, exist_ok=True)
        log.info(f"Using persistent cache directory: {os.path.abspath(work_dir)}")
    else:
        work_dir = tempfile.mkdtemp(prefix="macrowise_build_")
        log.info(f"Working directory: {work_dir}")

    db_path  = os.path.join(work_dir, DB_NAME)
    gz_path  = os.path.join(work_dir, GZ_NAME)

    try:
        # --- Open database ---
        conn = open_db(db_path)
        all_stats = {}

        # --- Process Open Food Facts ---
        if not args.usda_only:
            csv_gz_path = os.path.join(work_dir, "off.csv.gz")

            # Check if we already have a cached download (useful for reruns)
            if not os.path.exists(csv_gz_path):
                download_off_csv(csv_gz_path)
            else:
                log.info(f"Using cached OFF CSV at {csv_gz_path}")

            off_stats = process_off_csv(conn, csv_gz_path, limit=args.limit)
            all_stats["off"] = off_stats

            if not args.keep_csv:
                os.remove(csv_gz_path)
                log.info("OFF CSV deleted")

        # --- Process USDA ---
        if not args.off_only:
            usda_stats = process_usda(conn, usda_key, limit=args.limit)
            all_stats["usda"] = usda_stats

        # --- Post-processing ---
        create_indexes(conn)

        # Log totals
        total_foods = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
        total_names = conn.execute("SELECT COUNT(*) FROM food_names").fetchone()[0]
        log.info(f"Database totals: {total_foods:,} foods, {total_names:,} name entries")

        conn.close()

        # --- Compress ---
        checksum = ""
        actual_output_path = ""
        if not args.no_compress:
            checksum = compress_db(db_path, gz_path)
            output_gz = os.path.join(".", GZ_NAME)
            shutil.move(gz_path, output_gz)
            actual_output_path = output_gz
            log.info(f"Output: {output_gz}")
        else:
            output_db = os.path.join(".", DB_NAME)
            shutil.move(db_path, output_db)
            actual_output_path = output_db
            log.info(f"Output (uncompressed): {output_db}")

        # --- Write version file ---
        final_gz = GZ_NAME if not args.no_compress else DB_NAME
        write_version_file(version, checksum, actual_output_path, final_gz, {
            **all_stats,
            "total_foods": total_foods,
            "total_names": total_names,
        })

        log.info("=== Build complete ===")

    except KeyboardInterrupt:
        log.info("Build interrupted by user")
        sys.exit(1)

    except Exception as e:
        log.error(f"Build failed: {e}", exc_info=True)
        sys.exit(1)

    finally:
        if not args.keep_csv:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()

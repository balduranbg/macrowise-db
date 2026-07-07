#!/usr/bin/env python3
"""
build_db.py — Macrowise food database builder

Downloads Open Food Facts CSV dump, USDA FoodData Central JSON, and DSLD
(Dietary Supplement Label Database) supplement labels, strips to
Macrowise-relevant fields, builds a SQLite database with FTS5 full-text
search across all available languages, and compresses the result.

Output: macrowise-foods.db.gz

Usage:
    python3 build_db.py [--off-only] [--usda-only] [--dsld-only] [--limit N] [--no-compress]

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

from categories import normalise_category, normalise_dsld_category, normalise_usda_category

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OFF_CSV_URL = "https://static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz"
USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/list"
DSLD_SEARCH_URL = "https://api.ods.od.nih.gov/dsld/v9/search-filter"
DSLD_LABEL_URL = "https://api.ods.od.nih.gov/dsld/v9/label/{id}"

DB_NAME = "macrowise-foods.db"
GZ_NAME = "macrowise-foods.db.zst"  # zstd compressed (falls back to gzip if zstd unavailable)
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

# DSLD physicalState.langualCode values in scope: Powders and Liquids only.
# Capsules/tablets/gummies/etc. are out of scope — they belong to a separate,
# dedicated supplement-tracking app. See docs/DSLD_Integration_Brief_1.md §1.
DSLD_INCLUDED_FORMS = {"E0162", "E0165"}  # Powders, Liquids

# DSLD serving-size volume units -> millilitres (liquids only; see brief §6)
DSLD_VOLUME_TO_ML = {
    "ml": 1.0, "milliliter": 1.0,
    "l": 1000.0, "liter": 1000.0,
    "tsp": 4.92892, "teaspoon": 4.92892,
    "tbsp": 14.7868, "tablespoon": 14.7868,
    "fl oz": 29.5735, "fl. oz.": 29.5735, "fluid ounce": 29.5735,
    "cup": 236.588,
}

# DSLD nutrient name keyword matching (case-insensitive substring match).
# ingredientRows[].ingredientGroup is unreliable (see brief §5) — match on
# the free-text name instead. Order within each list matters (most specific
# first, e.g. "total fat" before the bare "fat").
DSLD_NUTRIENT_KEYWORDS = {
    "calories":      ["calorie"],
    "protein":       ["protein"],
    "fat":           ["total fat", "fat"],       # check "total fat" first
    "carbs":         ["total carbohydrate", "carbohydrate"],
    "fiber":         ["dietary fiber", "fiber"],
    "sugar":         ["total sugar", "sugars", "sugar"],
    "sodium":        ["sodium"],
    "potassium":     ["potassium"],
    "saturated_fat": ["saturated fat"],
    "cholesterol":   ["cholesterol"],
}

# Per brief §5: saturated fat, fiber, and sugar are consistently nested under
# a parent row (Total Fat / Total Carbohydrate) rather than top-level: the two
# groups must be matched separately to avoid double-counting or misattribution.
DSLD_TOP_LEVEL_FIELDS = {"calories", "protein", "fat", "carbs", "sodium", "potassium", "cholesterol"}
DSLD_NESTED_FIELDS = {"saturated_fat", "fiber", "sugar"}

# search-filter's supplement_form param filters server-side on physicalState
# (verified live — codes documented at the API's own HTML docs page)
DSLD_SUPPLEMENT_FORM_PARAM = "e0162,e0165"

# search-filter's product_type codes — used to split oversized year buckets
# (see _enumerate_dsld_ids). Documented at https://api.ods.od.nih.gov/dsld/v9/
DSLD_PRODUCT_TYPES = [
    "a1305", "a1306", "a1326", "a1310", "a1302",
    "a1299", "a1316", "a1315", "a1317", "a1309", "a1325",
]

DSLD_SEARCH_WINDOW_LIMIT = 9800  # observed ES pagination window caps ~10,000
DSLD_PAGE_SIZE = 200
DSLD_MIN_YEAR = 1994  # DSLD program inception

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
    source              TEXT NOT NULL,      -- 'OFF', 'USDA', or 'DSLD'
    image_url           TEXT,
    food_category       TEXT NOT NULL DEFAULT 'generic',
    quantity_basis      TEXT NOT NULL DEFAULT 'per_100g',  -- 'per_100g' or 'per_100ml'
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
            barcode, name, brand, source, image_url, food_category, quantity_basis,
            calories, protein, fat, carbs, fiber, sugar,
            sodium, potassium, saturated_fat, cholesterol,
            serving_description, serving_grams, popularity
        ) VALUES (
            :barcode, :name, :brand, :source, :image_url, :food_category, :quantity_basis,
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
        "quantity_basis":      food.get("quantity_basis", "per_100g"),
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
# DSLD (Dietary Supplement Label Database) processing
# ---------------------------------------------------------------------------

DSLD_MAX_BACKOFF_SECONDS = 900  # cap a single wait at 15 minutes


def _dsld_get(url: str, params: dict | None = None) -> requests.Response:
    """
    GET with retry/backoff on 429 (rate limit) and 5xx (transient server
    errors). Honours the Retry-After header when DSLD sends one, otherwise
    backs off exponentially (2, 4, 8... capped at DSLD_MAX_BACKOFF_SECONDS).
    Retries indefinitely — an unattended multi-hour/multi-day run should
    wait out a rate limit rather than give up and lose all progress.
    """
    attempt = 0
    while True:
        try:
            response = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            attempt += 1
            wait = min(2 ** attempt, DSLD_MAX_BACKOFF_SECONDS)
            log.warning(f"DSLD network error ({e}) — retrying in {wait}s")
            time.sleep(wait)
            continue

        if response.status_code == 429 or response.status_code >= 500:
            attempt += 1
            retry_after = response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    wait = min(float(retry_after), DSLD_MAX_BACKOFF_SECONDS)
                except ValueError:
                    wait = min(2 ** attempt, DSLD_MAX_BACKOFF_SECONDS)
            else:
                wait = min(2 ** attempt, DSLD_MAX_BACKOFF_SECONDS)
            log.warning(
                f"DSLD API returned {response.status_code} — backing off {wait:.0f}s "
                f"(attempt {attempt})"
            )
            time.sleep(wait)
            continue

        response.raise_for_status()
        return response


def _dsld_search_page(params: dict, from_: int, size: int) -> dict:
    """One page of the DSLD search-filter endpoint."""
    query = {**params, "from": from_, "size": size}
    response = _dsld_get(DSLD_SEARCH_URL, params=query)
    return response.json()


def _dsld_bucket_count(params: dict) -> int:
    """Total hit count for a search-filter query, without fetching results."""
    page = _dsld_search_page(params, 0, 1)
    return page.get("stats", {}).get("count", 0)


def _dsld_paginate_ids(params: dict):
    """Yield label IDs for one search-filter query, staying inside the ES window."""
    from_ = 0
    while from_ < DSLD_SEARCH_WINDOW_LIMIT:
        page = _dsld_search_page(params, from_, DSLD_PAGE_SIZE)
        hits = page.get("hits", [])
        if not hits:
            return
        for hit in hits:
            yield hit["_id"]
        if len(hits) < DSLD_PAGE_SIZE:
            return
        from_ += DSLD_PAGE_SIZE
        time.sleep(0.1)  # Be polite to the DSLD API


def _enumerate_dsld_ids():
    """
    Yield distinct DSLD label IDs for Powders + Liquids.

    search-filter's supplement_form param filters server-side on
    physicalState, but the underlying Elasticsearch query window caps at
    ~10,000 results — well under the ~69k matching products. We bucket by
    entry year (and, for oversized years, further by product_type) to keep
    each query under that window.

    This is verified, not assumed: each final bucket's own count is checked
    against the window (logged if still oversized), and the number of IDs
    actually paginated out of each bucket is compared against that bucket's
    reported count (logged if fewer came back than expected — a sign of
    silent truncation). For oversized years, the product_type sub-buckets'
    counts are also summed and compared to the year's total, since a product
    with no product_type match would fall through the split uncounted.
    """
    seen: set[str] = set()
    base_params = {"q": "*", "supplement_form": DSLD_SUPPLEMENT_FORM_PARAM}
    current_year = datetime.now(timezone.utc).year

    for year in range(DSLD_MIN_YEAR, current_year + 1):
        year_params = {**base_params, "date_start": year, "date_end": year}
        year_count = _dsld_bucket_count(year_params)
        if year_count == 0:
            continue

        if year_count <= DSLD_SEARCH_WINDOW_LIMIT:
            buckets = [(year_params, year_count)]
        else:
            buckets = []
            for pt in DSLD_PRODUCT_TYPES:
                pt_params = {**year_params, "product_type": pt}
                pt_count = _dsld_bucket_count(pt_params)
                if pt_count > 0:
                    buckets.append((pt_params, pt_count))
            covered = sum(c for _, c in buckets)
            if covered < year_count:
                log.warning(
                    f"DSLD year {year}: product_type split covers {covered}/{year_count} "
                    f"products — {year_count - covered} product(s) with no matching "
                    f"product_type will be missed"
                )

        for bucket_params, bucket_count in buckets:
            if bucket_count > DSLD_SEARCH_WINDOW_LIMIT:
                log.warning(
                    f"DSLD bucket {bucket_params} has {bucket_count} hits, exceeding the "
                    f"{DSLD_SEARCH_WINDOW_LIMIT}-hit pagination window — this bucket will "
                    f"be truncated; consider finer-grained bucketing"
                )
            yielded = 0
            for label_id in _dsld_paginate_ids(bucket_params):
                yielded += 1
                if label_id not in seen:
                    seen.add(label_id)
                    yield label_id
            if yielded < bucket_count:
                log.warning(
                    f"DSLD bucket {bucket_params}: expected {bucket_count} hits, only "
                    f"retrieved {yielded} — pagination was truncated by the ES window"
                )


def _dsld_isclose(a, b, tol: float = 1e-6) -> bool:
    return a is not None and b is not None and abs(a - b) < tol


def _dsld_pick_quantity(quantity_entries: list, serving_size: dict):
    """
    Pick the quantity[] entry matching this label's declared serving size.

    Per brief §5, match explicitly against servingSizes[0].minQuantity rather
    than assuming array position. Some labels (observed on real data, e.g.
    multi-scoop mass gainers with a serving *range*) instead express
    servingSizeQuantity as a serving-multiple count — e.g. "5" meaning
    "5 scoops" — that matches maxQuantity/minQuantity rather than a raw
    quantity. We fall back to that interpretation before giving up.

    Most labels carry >1 matching entry per row as a matter of course — one
    per daily-value target group (e.g. adults vs. children) — and those
    duplicates almost always agree on quantity, so that alone isn't worth
    flagging. When multiple matching entries disagree on quantity, resolution
    is NOT by array position — verified against a real disagreement (label
    6472, "Calories": 170 vs. 80) that the correct entry (80, confirmed
    against the label's own Atwater-derived macros) was the one carrying a
    populated dailyValueTargetGroup naming the primary adult group, while the
    wrong one (170) had an empty dailyValueTargetGroup — i.e. it looks like
    an incomplete/malformed duplicate row, not a legitimate alternate
    reading. So: prefer the entry tagged for the primary adult group; if
    that's not decisive, prefer any entry with a populated
    dailyValueTargetGroup over one with none; only if still tied (e.g. two
    differently-populated entries, such as adult vs. child, that actually
    disagree — not observed in any label checked so far, but plausible) do
    we fall back to the last array entry, flagged as ambiguous so it can be
    audited.

    Returns (entry, denominator, ambiguous) where ambiguous is True only if
    disagreeing entries couldn't be resolved by target group and the
    last-match fallback had to be used, or (None, None, False) if no entry
    matches.
    """
    min_q = serving_size.get("minQuantity")
    max_q = serving_size.get("maxQuantity")

    def is_primary_adult_group(entry) -> bool:
        return any("adult" in (g.get("name") or "").lower()
                   for g in (entry.get("dailyValueTargetGroup") or []))

    def pick(matches):
        values = {m.get("quantity") for m in matches}
        if len(values) == 1:
            return matches[-1], False  # duplicates agree — no ambiguity

        primary = [m for m in matches if is_primary_adult_group(m)]
        if len(primary) == 1:
            return primary[0], False  # resolved via target group, not position

        non_empty = [m for m in matches if m.get("dailyValueTargetGroup")]
        if len(non_empty) == 1:
            return non_empty[0], False  # resolved via target group, not position

        # Still tied (e.g. multiple adult-tagged entries disagree, or none
        # are tagged at all) — no principled signal left, fall back and flag.
        return matches[-1], True

    matches = [e for e in quantity_entries if _dsld_isclose(e.get("servingSizeQuantity"), min_q)]
    if matches:
        entry, ambiguous = pick(matches)
        return entry, min_q, ambiguous

    if min_q and max_q and min_q != max_q:
        multiple = max_q / min_q
        matches = [e for e in quantity_entries if _dsld_isclose(e.get("servingSizeQuantity"), multiple)]
        if matches:
            entry, ambiguous = pick(matches)
            return entry, max_q, ambiguous

    return None, None, False


def _dsld_match_field(name: str, allowed_fields: set) -> str | None:
    """Match an ingredientRows[].name string to a Macrowise nutrient field."""
    name_lower = name.lower()
    for field, keywords in DSLD_NUTRIENT_KEYWORDS.items():
        if field not in allowed_fields:
            continue
        for kw in keywords:
            if kw in name_lower:
                return field
    return None


def _extract_dsld_nutrients(label: dict) -> dict | None:
    """
    Walk a DSLD label's ingredientRows and convert to per-100g/per-100ml
    nutrient values. Returns None if the label has no usable serving size.

    The returned dict includes a "_quantity_basis" key ('per_100g' or
    'per_100ml') and an "_ambiguous_fields" key (list of field names where
    _dsld_pick_quantity's last-match tie-break fired) that the caller must
    pop before further use.
    """
    serving_sizes = label.get("servingSizes") or []
    if not serving_sizes:
        return None
    serving_size = serving_sizes[0]
    min_q = serving_size.get("minQuantity")
    if not min_q or min_q <= 0:
        return None

    physical_state = label.get("physicalState") or {}
    langual = physical_state.get("langualCode")

    ml_per_unit = None
    if langual == "E0162":
        quantity_basis = "per_100g"
    elif langual == "E0165":
        quantity_basis = "per_100ml"
        unit = (serving_size.get("unit") or "").strip().lower()
        ml_per_unit = DSLD_VOLUME_TO_ML.get(unit)
        if ml_per_unit is None:
            return None
    else:
        return None  # caller already filters on physicalState; defensive only

    nutrients: dict[str, float] = {}
    ambiguous_fields: list[str] = []

    def resolve(row: dict, allowed_fields: set) -> None:
        field = _dsld_match_field(row.get("name", ""), allowed_fields)
        if field is None or field in nutrients:
            return
        entry, denom, ambiguous = _dsld_pick_quantity(row.get("quantity", []), serving_size)
        if entry is None:
            return
        value = safe_float(entry.get("quantity"))
        if value is None:
            return
        if ml_per_unit is not None:
            serving_ml = denom * ml_per_unit
            if serving_ml <= 0:
                return
            nutrients[field] = (value / serving_ml) * 100
        else:
            nutrients[field] = (value / denom) * 100
        if ambiguous:
            ambiguous_fields.append(field)

    for row in label.get("ingredientRows", []):
        resolve(row, DSLD_TOP_LEVEL_FIELDS)
        for nested in row.get("nestedRows", []):
            resolve(nested, DSLD_NESTED_FIELDS)

    nutrients["_quantity_basis"] = quantity_basis
    nutrients["_ambiguous_fields"] = ambiguous_fields
    return nutrients


def _load_dsld_checkpoint(checkpoint_path: str | None) -> set[str]:
    """Load the set of label IDs already attempted in a prior run, if any."""
    if not checkpoint_path or not os.path.exists(checkpoint_path):
        return set()
    with open(checkpoint_path, "r") as f:
        return {line.strip() for line in f if line.strip()}


def process_dsld(conn: sqlite3.Connection, limit: int | None = None,
                  checkpoint_path: str | None = None) -> dict:
    """
    Fetch DSLD supplement labels (Powders + Liquids only) and insert into DB.

    Two-step fetch: search-filter to enumerate candidate label IDs (filtered
    server-side via the supplement_form param), then /label/{id} for full
    serving-size and nutrient detail. Returns stats dict.

    If checkpoint_path is given, every attempted label ID is appended there
    (flushed immediately), and any ID already present on startup is skipped —
    this lets a killed/crashed run resume without refetching work already
    done or double-inserting rows on restart. Pass None to disable (default
    OFF/USDA/combined runs are unaffected either way).
    """
    stats = {"processed": 0, "inserted": 0, "skipped_nutrients": 0,
             "skipped_form": 0, "skipped_noname": 0, "errors": 0, "api_errors": 0,
             "ambiguous_quantity_labels": 0, "ambiguous_quantity_fields": 0,
             "ambiguous_label_ids": []}

    done = _load_dsld_checkpoint(checkpoint_path)
    if done:
        log.info(f"Resuming DSLD run: {len(done)} label(s) already attempted previously, skipping them")

    checkpoint_file = open(checkpoint_path, "a") if checkpoint_path else None

    log.info("Fetching DSLD supplement labels (Powders + Liquids)...")

    try:
        _process_dsld_labels(conn, stats, limit, done, checkpoint_file)
    finally:
        if checkpoint_file:
            checkpoint_file.close()

    conn.commit()
    log.info(f"DSLD processing complete: {stats}")
    return stats


def _process_dsld_labels(conn, stats, limit, done, checkpoint_file):
    for label_id in tqdm(_enumerate_dsld_ids(), desc="DSLD labels", unit=" labels"):
        if limit and stats["processed"] >= limit:
            break

        if str(label_id) in done:
            continue

        stats["processed"] += 1

        try:
            _process_one_dsld_label(conn, stats, label_id)
        finally:
            if checkpoint_file:
                checkpoint_file.write(f"{label_id}\n")
                checkpoint_file.flush()
                os.fsync(checkpoint_file.fileno())

        if stats["processed"] % 500 == 0:
            conn.commit()


def _process_one_dsld_label(conn, stats, label_id):
    try:
        response = _dsld_get(DSLD_LABEL_URL.format(id=label_id))
        label = response.json()
    except requests.RequestException as e:
        stats["api_errors"] += 1
        log.warning(f"DSLD API error fetching label {label_id}: {e}")
        return
    finally:
        time.sleep(0.1)  # Be polite to the DSLD API

    try:
        # --- Filter to in-scope physical forms (defensive re-check) ---
        physical_state = label.get("physicalState") or {}
        if physical_state.get("langualCode") not in DSLD_INCLUDED_FORMS:
            stats["skipped_form"] += 1
            return

        # --- Extract and convert nutrients ---
        nutrients = _extract_dsld_nutrients(label)
        if nutrients is None:
            stats["skipped_nutrients"] += 1
            return

        quantity_basis = nutrients.pop("_quantity_basis")
        ambiguous_fields = nutrients.pop("_ambiguous_fields")
        if ambiguous_fields:
            stats["ambiguous_quantity_labels"] += 1
            stats["ambiguous_quantity_fields"] += len(ambiguous_fields)
            stats["ambiguous_label_ids"].append({"id": label_id, "fields": ambiguous_fields})
            log.info(
                f"DSLD label {label_id}: last-match tie-break used for "
                f"quantity field(s) {ambiguous_fields} (multiple quantity[] "
                f"entries shared the same servingSizeQuantity)"
            )

        if "calories" in nutrients:
            nutrients["calories"] = round(nutrients["calories"])

        missing = REQUIRED_NUTRIENTS - set(nutrients.keys())
        if missing:
            stats["skipped_nutrients"] += 1
            return

        # --- Name ---
        name = (label.get("fullName") or "").strip()
        if not name:
            stats["skipped_noname"] += 1
            return
        names = {"en": name}

        # --- Metadata ---
        brand = (label.get("brandName") or "").strip() or None

        food = {
            **nutrients,
            "barcode":             None,   # DSLD has no barcodes
            "brand":               brand,
            "source":              "DSLD",
            "image_url":           None,   # No product images in DSLD
            "food_category":       normalise_dsld_category(),
            "serving_description": None,
            "serving_grams":       None,
            "popularity":          0,
            "quantity_basis":      quantity_basis,
        }

        result = insert_food(conn, food, names)
        if result is not None:
            stats["inserted"] += 1
        else:
            stats["skipped_noname"] += 1

    except Exception as e:
        stats["errors"] += 1
        if stats["errors"] <= 10:
            log.warning(f"Error processing DSLD label {label_id}: {e}")


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


def compress_db(db_path: str, out_path: str) -> str:
    """
    Compress the SQLite database.
    Uses zstd if available (better compression), falls back to gzip level 9.
    Returns SHA256 of the compressed file.
    """
    log.info(f"Compressing {db_path} -> {out_path}")

    db_size = os.path.getsize(db_path)
    log.info(f"Uncompressed size: {db_size / 1024 / 1024:.1f} MB")

    sha256 = hashlib.sha256()

    # Try zstd first (better compression ratio than gzip)
    try:
        import zstandard as zstd
        log.info("Using zstd compression (level 19)")
        cctx = zstd.ZstdCompressor(level=19, threads=-1)
        with open(db_path, "rb") as f_in, \
             open(out_path, "wb") as f_out, \
             tqdm(total=db_size, unit="B", unit_scale=True, desc="Compressing") as bar:
            with cctx.stream_writer(f_out) as compressor:
                while True:
                    chunk = f_in.read(1024 * 1024)
                    if not chunk:
                        break
                    compressor.write(chunk)
                    sha256.update(chunk)
                    bar.update(len(chunk))

    except ImportError:
        log.info("zstd not available — falling back to gzip level 9")
        with open(db_path, "rb") as f_in, \
             gzip.open(out_path, "wb", compresslevel=9) as f_out, \
             tqdm(total=db_size, unit="B", unit_scale=True, desc="Compressing") as bar:
            while True:
                chunk = f_in.read(1024 * 1024)
                if not chunk:
                    break
                f_out.write(chunk)
                sha256.update(chunk)
                bar.update(len(chunk))

    out_size = os.path.getsize(out_path)
    checksum = sha256.hexdigest()

    log.info(f"Compressed size: {out_size / 1024 / 1024:.1f} MB "
             f"(ratio: {out_size/db_size:.1%})")
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
        description="Build macrowise-foods.db from OFF, USDA, and DSLD sources"
    )
    parser.add_argument("--off-only",    action="store_true",
                        help="Process only Open Food Facts")
    parser.add_argument("--usda-only",   action="store_true",
                        help="Process only USDA FoodData Central")
    parser.add_argument("--dsld-only",   action="store_true",
                        help="Process only DSLD (supplement powders/liquids)")
    parser.add_argument("--limit",       type=int, default=None,
                        help="Limit rows per source (for testing)")
    parser.add_argument("--no-compress", action="store_true",
                        help="Skip gzip compression (for testing)")
    parser.add_argument("--keep-csv",    action="store_true",
                        help="Keep downloaded OFF CSV after processing")
    args = parser.parse_args()

    # Version string: YYYY-MM
    version = datetime.now(timezone.utc).strftime("%Y-%m")

    # Which sources to run: if any single-source-only flag is set, run only
    # that source; if none are set, run all three (default).
    any_only = args.off_only or args.usda_only or args.dsld_only
    run_off  = args.off_only  or not any_only
    run_usda = args.usda_only or not any_only
    run_dsld = args.dsld_only or not any_only

    log.info(f"=== Macrowise Food Database Builder ===")
    log.info(f"Version: {version}")
    sources = [name for name, run in (("OFF", run_off), ("USDA", run_usda), ("DSLD", run_dsld)) if run]
    log.info(f"Sources: {' + '.join(sources)}")
    if args.limit:
        log.info(f"Limit: {args.limit} rows per source (TEST MODE)")

    # USDA API key
    usda_key = os.environ.get("USDA_API_KEY", "DEMO_KEY")
    if usda_key == "DEMO_KEY":
        log.warning("Using DEMO_KEY for USDA — rate limits apply. "
                    "Set USDA_API_KEY env var for production.")

    # Work directory — use persistent cache dir if --keep-csv, otherwise temp.
    # --dsld-only always uses a persistent directory (DSLD_CACHE_DIR env var,
    # or a local fallback) regardless of --keep-csv, since a multi-hour/
    # multi-day DSLD run needs its db + checkpoint file to survive a crash
    # or restart — this has no effect on OFF/USDA-only or combined runs.
    dsld_checkpoint_path = None
    if args.dsld_only:
        work_dir = os.environ.get(
            "DSLD_CACHE_DIR",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache", "dsld"),
        )
        os.makedirs(work_dir, exist_ok=True)
        dsld_checkpoint_path = os.path.join(work_dir, "dsld_checkpoint.txt")
        log.info(f"Using persistent DSLD working directory: {os.path.abspath(work_dir)}")
    elif args.keep_csv:
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
        if run_off:
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
        if run_usda:
            usda_stats = process_usda(conn, usda_key, limit=args.limit)
            all_stats["usda"] = usda_stats

        # --- Process DSLD ---
        if run_dsld:
            dsld_stats = process_dsld(conn, limit=args.limit, checkpoint_path=dsld_checkpoint_path)
            all_stats["dsld"] = dsld_stats

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
        # Never clean up the persistent DSLD working directory — its whole
        # point is to survive across runs so a crash/restart can resume
        # instead of refetching ~69k labels from scratch.
        if not args.keep_csv and not args.dsld_only:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()

# macrowise-db

Monthly food database builder for the [Macrowise](https://github.com/balduranbg/macrowise) nutrition tracking app.

## What this produces

`macrowise-foods.db.gz` — a compressed SQLite database containing:
- ~1.5 million foods from Open Food Facts (global packaged products)
- ~300,000 foods from USDA FoodData Central (whole/generic foods)
- Full-text search across all available languages (40+ languages)
- Nutrient data: calories, protein, fat, carbs, fiber, sugar, sodium, potassium, saturated fat, cholesterol
- Product images (OFF image URLs where available)
- Food category for icon fallback

## Schedule

Rebuilt automatically on the **first Sunday of each month** via GitHub Actions.

## Database schema

```sql
foods (
    id, barcode, name, brand, source,
    image_url, food_category,
    calories, protein, fat, carbs, fiber, sugar,
    sodium, potassium, saturated_fat, cholesterol,
    serving_description, serving_grams, popularity
)

food_names (
    id, food_id, language, name
)

food_names_fts  -- FTS5 virtual table, searches food_names across all languages
```

## Documentation

Detailed specs for individual data-source integrations live in `docs/`:
- `docs/DSLD_Integration_Brief.md` — DSLD (NIH Dietary Supplement Label Database) integration: scope, schema changes, conversion logic

## Quality filter

Only products with **calories + protein + carbs + fat** all present are included.

## Building locally

```bash
# Install dependencies
pip install requests tqdm

# Full build (downloads ~900MB OFF CSV)
cd scripts
USDA_API_KEY=your_key python3 build_db.py

# Test build (100 rows per source, no compression)
USDA_API_KEY=your_key python3 build_db.py --limit 100 --no-compress

# OFF only
USDA_API_KEY=your_key python3 build_db.py --off-only

# Keep downloaded CSV for faster reruns
USDA_API_KEY=your_key python3 build_db.py --keep-csv
```

## Required GitHub secrets

| Secret | Description |
|---|---|
| `USDA_API_KEY` | USDA FoodData Central API key — register free at api.nal.usda.gov |
| `CLOUDFLARE_API_TOKEN` | Cloudflare API token with Worker edit permissions |
| `CLOUDFLARE_ACCOUNT_ID` | Your Cloudflare account ID |

## Releases

Each monthly build creates a GitHub Release tagged `YYYY-MM` containing:
- `macrowise-foods.db.gz` — the database
- `version.json` — build metadata (version, checksum, size, stats)

The app checks the Worker's `/api/db/version` endpoint on launch to determine
if a new database is available for download.

## License

Food data is sourced from:
- [Open Food Facts](https://world.openfoodfacts.org) — Open Database License (ODbL)
- [USDA FoodData Central](https://fdc.nal.usda.gov) — Public domain (CC0)

Build scripts in this repository are MIT licensed.

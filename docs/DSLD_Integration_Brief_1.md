# DSLD Integration Brief — for Claude Code

**Purpose:** Add the NIH Dietary Supplement Label Database (DSLD) as a third data source in `macrowise-db`'s `build_db.py`, alongside the existing Open Food Facts and USDA FoodData Central sources.

**Status:** Design and API verification complete (via live `curl` testing against the real DSLD API). This brief describes the verified scope and required changes. `process_dsld()` itself has not yet been written — implement it following the structure below.

---

## 1. Why DSLD, and scope boundary

DSLD fills a real gap: neither OFF nor USDA cover dietary supplements (protein powders, creatine, etc.) well. Victor has a separate, dedicated supplement-tracking app (brand-agnostic, capsule/tablet-focused) already covering full supplement tracking — so **Macrowise's DSLD integration is deliberately scoped to supplement forms that behave like food in a nutrition log**: things measured by weight or volume (powders, liquids), not capsules/tablets/gummies which belong in the other app's domain.

**In scope:** `physicalState.langualCode` values:
- `E0162` — Powders (whey, casein, creatine, mass gainers, etc.)
- `E0165` — Liquids (liquid aminos, liquid MCT/fish oil, liquid vitamins, etc.)

**Out of scope:** everything else (E0155 Tablets, E0159 Capsules, E0161 Softgel Capsules, E0176 Gummies, E0174 Lozenges, E0164 Bars, E0172 Other, E0177 Unknown).

**Verified:** `physicalState` is a reliable, name-independent field — tested a product literally called "Men's Living Green Liquid-Gel Multi" and confirmed it correctly returns `E0161` (Softgel), not `E0165`, despite "Liquid" in the product name. Filtering on `physicalState.langualCode` alone is safe; no secondary cross-check against product name or serving-unit shape is needed.

---

## 2. API details

- Base: `https://api.ods.od.nih.gov/dsld/v9/`
- Search: `GET /search-filter?q={query}&size={n}` — index/summary view, does NOT include full `servingSizes` or per-serving `ingredientRows` quantities. Use only to discover `_id`s.
- Detail: `GET /label/{id}` — full label detail, including `servingSizes`, `ingredientRows` with nested `quantity[]` arrays, `physicalState`, `netContents`.
- No API key required (unlike USDA).
- Rate limits: not documented; be conservative, similar politeness delay to the existing USDA loop (`time.sleep(0.1)` between calls) is a reasonable default.
- **Practical ingestion approach:** since `search-filter` doesn't return full serving/nutrient detail, ingestion likely needs to (a) enumerate candidate `_id`s via filtered search (e.g. by `physicalState`/`productType` query params — check API guide for a direct filter-by-physicalState param before resorting to brute pagination), then (b) fetch full detail per `_id` via `/label/{id}`. This is a two-step fetch per product, unlike OFF (single CSV pass) or USDA (single paginated list call already returns full nutrient detail). Confirm whether `search-filter` supports a `physicalState` query parameter directly to avoid fetching irrelevant forms' detail records.

---

## 3. Schema changes required

### `food_cache.db` — `foods` table
Add one column:
```sql
quantity_basis TEXT NOT NULL DEFAULT 'per_100g'  -- 'per_100g' or 'per_100ml'
```
- All existing OFF/USDA rows implicitly `'per_100g'` (default) — zero migration impact on the 2.1M existing foods.
- DSLD Powder rows (`E0162`) → `'per_100g'`.
- DSLD Liquid rows (`E0165`) → `'per_100ml'`.
- Update the stale `source TEXT NOT NULL, -- 'OFF' or 'USDA'` comment to include `'DSLD'`.

### `user_data.db` — `custom_foods` table (versioned table)
Add the same `quantity_basis` column, for users creating custom liquid foods (e.g. homemade shakes) via the Custom Food Creation form (spec §11). Ride-along on the existing `valid_from`/`valid_to` versioning — no special handling needed, versioning mechanics are unaffected by this column.

**No other schema impact.** Confirmed: no interaction with Phase-2 sync fields (`device_id`, `created_at`, `updated_at`, `is_synced`) — those apply to `user_data.db` tables only, and `quantity_basis` just rides along like any other column on `custom_foods`. `food_cache.db` was never in scope for sync fields (it's reference/cache data, not user-owned state). Snapshot tables (`food_log`, `favourites`, `recent_foods`) need no changes — they copy whatever basis the source row declares at log time.

---

## 4. App-side implications (Flutter, not just build_db.py)

These are downstream of the schema change and should be flagged to whoever picks up the Flutter-side work (likely a later Claude Code session, once data models exist):

- **Food Detail screen (spec §10):** live serving-size calculation needs a basis-aware branch — grams-entered × (value/100) for `per_100g`, mL-entered × (value/100) for `per_100ml`. The serving size selector already supports `ml` as a unit (spec §10), so no new UI control is needed, just the calculation branch.
- **Custom Food Creation (spec §11):** the fixed label "Calories per 100g" needs to become basis-aware, with a toggle or unit selector for liquid custom foods.
- **Display copy (spec §8, §16, §17):** any UI showing "per 100g" as fixed text needs to become basis-aware, with new i18n strings for "per 100mL" phrasing.
- **Macro/target math, cascade logic, nudges, history:** confirmed unaffected — once a serving's absolute macro grams are computed, everything downstream is basis-agnostic.

---

## 5. Nutrient extraction — matching strategy (verified via 5 real labels)

**Do NOT match on `ingredientRows[].ingredientGroup`.** It was hypothesized as more stable than the free-text `name` field, but real-data testing disproved this: the same conceptual field ("Calories") returned `ingredientGroup: "Calories"` on 3 of 5 tested labels and `ingredientGroup: "Header"` on 2 of 5 (Precision Engineered whey, Groovy Bee MCT oil) — no discernible pattern predicting which, not correlated with brand tier or product category.

**Use keyword/substring matching on `ingredientRows[].name` instead** (case-insensitive `contains`, not exact `==`), since `name` consistently contains the recognizable root word across all tested labels despite plural/phrasing variance ("Total Fat" vs "Fat", "Total Carbohydrate" vs "Total Carbohydrates").

```python
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
```

### Nested-row handling (important — verified across multiple labels)
`ingredientRows` entries can have `nestedRows[]`. Observed nesting patterns:
- "Calories from Fat" nested under "Calories"
- "Saturated Fat" and "Trans Fat" nested under "Total Fat"
- "Dietary Fiber" and "Sugar" nested under "Total Carbohydrates"
- Sub-fatty-acids (e.g. "C8:0 Caprylic Acid") nested under "Medium Chain Triglycerides" — not nutrients you track, safe to ignore

**The parser must walk top-level `ingredientRows` for primary fields** (calories, protein, total fat, total carbs, sodium, potassium, cholesterol) **and separately reach into `nestedRows` only for saturated fat, fiber, and sugar**, which are consistently nested rather than top-level in the labels tested. A flat/naive traversal will double-count or misattribute values.

### Multiple `servingSizeQuantity` entries
Each ingredient's `quantity[]` array can contain multiple entries for different serving multiples (e.g. 25g and 50g, i.e. 1 and 2 scoops). **Match explicitly against `servingSizes[0].minQuantity`** — do not assume array position or a single-entry array.

---

## 6. Serving size → per-100g / per-100mL conversion

### Powders (E0162) — verified clean, no conversion ambiguity
`servingSizes[0].unit` is reliably `"Gram(s)"` for powders (confirmed across whey isolate, creatine, mass gainer labels from 4 different brands). Conversion is exact:
```
per_100_value = (ingredient_quantity_at_servingSizeQuantity / servingSizeQuantity) * 100
```

### Liquids (E0165) — verified, uses volume not mass
`servingSizes[0].unit` varies — observed both `"Tbsp"` and `"mL"` directly across different labels. **Normalize to per-100mL, not per-100g** — this was a deliberate design decision to avoid density-guessing (oil vs. water-based liquids have meaningfully different densities; verified via Carlson MCT oil label showing an implied density of ~0.93 g/mL, not the ~1.0 g/mL a naive water-based assumption would use). Conversion:
```
serving_ml = servingSizeQuantity_in_native_unit * DSLD_VOLUME_TO_ML[unit]
per_100ml_value = (ingredient_quantity / serving_ml) * 100
```

```python
DSLD_VOLUME_TO_ML = {
    "ml": 1.0, "milliliter": 1.0,
    "l": 1000.0, "liter": 1000.0,
    "tsp": 4.92892, "teaspoon": 4.92892,
    "tbsp": 14.7868, "tablespoon": 14.7868,
    "fl oz": 29.5735, "fl. oz.": 29.5735, "fluid ounce": 29.5735,
    "cup": 236.588,
}
```

---

## 7. Required-field filtering (inherited, no change needed)

The existing `REQUIRED_NUTRIENTS = {"calories", "protein", "fat", "carbs"}` check in `insert_food()` already correctly excludes:
- Creatine-only products (no calorie/macro fields at all — verified via NOW Sports Creatine Monohydrate label)
- Vitamin/mineral/botanical-only products (verified via a multivitamin softgel label, which also happens to be excluded by the `physicalState` filter independently)

No changes needed to this logic — DSLD rows flow through the same `insert_food()` validation as OFF/USDA rows.

---

## 8. `source` field and category

- `source = 'DSLD'` — third valid value alongside `'OFF'`/`'USDA'` (currently only a comment, not a DB constraint, so no schema migration needed for this specific value).
- `food_category` should map to `'supplements'` (already a valid value in the existing category list) — needs a `normalise_dsld_category()` function or an extension of the existing category normalizer.

---

## 9. Suggested `process_dsld()` structure (mirrors `process_off_csv()` / `process_usda()`)

```python
def process_dsld(conn: sqlite3.Connection, limit: int | None = None) -> dict:
    """
    Fetch DSLD supplement labels (Powders + Liquids only) and insert into DB.
    Two-step fetch: search-filter to enumerate candidates, then /label/{id}
    for full serving/nutrient detail.
    """
    stats = {"processed": 0, "inserted": 0, "skipped_nutrients": 0,
              "skipped_form": 0, "skipped_noname": 0, "errors": 0}
    # 1. Enumerate candidate label IDs (check if search-filter supports
    #    a physicalState query param to filter server-side; else fetch
    #    broadly and filter client-side on physicalState.langualCode)
    # 2. For each ID: GET /label/{id}
    # 3. Filter: physicalState.langualCode not in DSLD_INCLUDED_FORMS -> skip, stats["skipped_form"] += 1
    # 4. Extract servingSizes[0] -> determine basis (per_100g or per_100ml) + conversion factor
    # 5. Walk ingredientRows top-level + targeted nestedRows per section 5 above
    # 6. Convert each nutrient to per-100 basis per section 6 above
    # 7. Check REQUIRED_NUTRIENTS (inherited, unchanged)
    # 8. Call insert_food() with source="DSLD", food_category via normalise_dsld_category(),
    #    quantity_basis set accordingly
    ...
    return stats
```

Wire into `main()` alongside the existing `--off-only`/`--usda-only` flags — e.g. a `--dsld-only` flag and inclusion in the default (no-flag) run alongside OFF + USDA.

---

## 10. Real verified label examples (for testing/reference)

| Label ID | Product | Form | Basis | Notes |
|---|---|---|---|---|
| 28403 | Whey Protein Deluxe Chocolate (Precision Engineered) | Powder | per_100g | `ingredientGroup` inconsistency case ("Header") |
| 205180 | Creatine Monohydrate (NOW Sports) | Powder | — | Correctly excluded by REQUIRED_NUTRIENTS (no macros) |
| 269280 | Mass Pro Mass Gainer (G6 Sports) | Powder | per_100g | Dense carb profile, many micronutrient rows |
| 6472 | Whey Protein Powder Vanilla (VitaCeutical Labs) | Powder | per_100g | `ingredientGroup: "Calories"` (consistent case) |
| 2456 | Liquid Aminos (Precision Engineered) | Liquid | per_100ml | `servingSizes.unit = "Tbsp"`, dilute (mostly water) |
| 299084 | Organic MCT Oil (Groovy Bee) | Liquid | per_100ml | `servingSizes.unit = "mL"` directly, oil density case |
| 241650 | MCT Oil 14,000mg (Carlson) | Liquid | per_100ml | Implied density ~0.93 g/mL — confirms oil ≠ water assumption |
| 56490 | Men's Living Green Liquid-Gel Multi (Irwin Naturals) | Softgel (E0161) | excluded | Confirms `physicalState` is name-independent and reliable |

---

*Prepared from a design/verification conversation — all API responses referenced above were pulled live from the production DSLD API on 2026-07-06.*

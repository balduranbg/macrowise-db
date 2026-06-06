"""
categories.py — Food category normalisation for macrowise-foods.db

Maps Open Food Facts category tags and USDA food categories
to a consistent set of ~20 Macrowise categories used for
icon display when no product image is available.
"""

# ---------------------------------------------------------------------------
# Macrowise category definitions
# Each category maps to a Phosphor icon name in the Flutter app.
# Order matters: first match wins when a product has multiple category tags.
# ---------------------------------------------------------------------------

# Map: macrowise_category -> list of OFF/USDA substrings to match against
# Matching is case-insensitive substring match on the category tag.
CATEGORY_RULES = [
    # High-specificity categories first
    ("supplements",  ["protein-powder", "dietary-supplement", "sport-nutrition",
                      "whey", "creatine", "bcaa", "protein-bar", "energy-bar",
                      "nutrition-bar"]),

    ("eggs",         ["en:eggs", "egg-products"]),

    ("fish",         ["en:fish", "seafood", "salmon", "tuna", "cod", "shrimp",
                      "prawns", "shellfish", "molluscs", "canned-fish",
                      "smoked-fish", "fish-products"]),

    ("meats",        ["en:meats", "poultry", "chicken", "beef", "pork", "lamb",
                      "turkey", "veal", "duck", "sausage", "deli-meat",
                      "cured-meat", "processed-meat", "offal"]),

    ("dairy",        ["en:dairy", "milk", "cheese", "yogurt", "yoghurt",
                      "butter", "cream", "kefir", "skyr", "cottage-cheese",
                      "whipped-cream", "dairy-products"]),

    ("vegetables",   ["en:vegetables", "fresh-vegetables", "frozen-vegetables",
                      "canned-vegetables", "tomato", "lettuce", "spinach",
                      "broccoli", "carrot", "pepper", "onion", "garlic",
                      "cucumber", "zucchini", "mushroom"]),

    ("fruits",       ["en:fruits", "fresh-fruits", "frozen-fruits",
                      "canned-fruits", "dried-fruits", "apple", "banana",
                      "orange", "berry", "grape", "mango", "citrus"]),

    ("grains",       ["en:cereals", "bread", "pasta", "rice", "oat",
                      "wheat", "flour", "noodle", "tortilla", "wrap",
                      "cereal", "granola", "muesli", "quinoa", "barley",
                      "couscous", "grain-products"]),

    ("legumes",      ["en:legumes", "bean", "lentil", "chickpea", "pea",
                      "soy", "tofu", "tempeh", "hummus", "pulse"]),

    ("nuts",         ["en:nuts", "seed", "almond", "walnut", "cashew",
                      "peanut", "pistachio", "hazelnut", "pecan",
                      "sunflower-seed", "nut-butter", "tahini"]),

    ("oils",         ["en:oils", "oil", "en:fats", "margarine", "lard",
                      "ghee", "cooking-fat"]),

    ("sauces",       ["sauce", "condiment", "dressing", "ketchup",
                      "mayonnaise", "mustard", "vinegar", "salsa",
                      "hot-sauce", "soy-sauce", "pesto"]),

    ("soups",        ["soup", "broth", "stock", "bouillon", "stew",
                      "bisque", "chowder"]),

    ("sweets",       ["chocolate", "candy", "sweet", "confection",
                      "gummy", "marshmallow", "lollipop", "caramel",
                      "toffee", "nougat"]),

    ("desserts",     ["dessert", "ice-cream", "sorbet", "gelato",
                      "pudding", "cake", "cookie", "biscuit", "pastry",
                      "waffle", "pancake", "muffin", "brownie", "pie",
                      "tart", "cheesecake"]),

    ("bakery",       ["bread", "bagel", "croissant", "roll", "bun",
                      "sourdough", "flatbread", "pita", "naan",
                      "bakery", "baked-goods"]),

    ("snacks",       ["snack", "chip", "crisp", "popcorn", "pretzel",
                      "cracker", "rice-cake", "trail-mix"]),

    ("beverages",    ["beverage", "drink", "juice", "water", "soda",
                      "coffee", "tea", "smoothie", "milkshake",
                      "energy-drink", "sports-drink", "alcohol",
                      "wine", "beer", "spirit", "cocktail"]),

    ("spices",       ["spice", "herb", "seasoning", "salt", "pepper",
                      "cinnamon", "cumin", "paprika", "curry",
                      "dried-herb"]),

    ("prepared",     ["prepared-meal", "ready-meal", "frozen-meal",
                      "microwave-meal", "pizza", "sandwich", "burger",
                      "wrap", "ready-to-eat", "meal-kit"]),

    # Catch-all — always matches, placed last
    ("generic",      []),
]


def normalise_category(categories_tags: list[str] | None,
                       food_group: str | None = None) -> str:
    """
    Return the best Macrowise category string for a product.

    Args:
        categories_tags: list of OFF category tag strings, e.g.
                         ["en:foods", "en:dairy", "en:yogurts"]
                         Pass None or [] if unavailable.
        food_group:      USDA food group string, e.g. "Dairy and Egg Products"
                         Used as a fallback when categories_tags is empty.

    Returns:
        One of the ~20 Macrowise category strings, or "generic".
    """
    # Build a single searchable string from all available signals
    search_str = ""

    if categories_tags:
        search_str += " ".join(categories_tags).lower()

    if food_group:
        search_str += " " + food_group.lower()

    if not search_str.strip():
        return "generic"

    for category, keywords in CATEGORY_RULES:
        if not keywords:
            continue  # Skip catch-all during keyword matching
        for kw in keywords:
            if kw.lower() in search_str:
                return category

    return "generic"


# ---------------------------------------------------------------------------
# USDA food group -> Macrowise category direct mapping
# Used when USDA food group is available and OFF categories are absent
# ---------------------------------------------------------------------------
USDA_GROUP_MAP = {
    "Dairy and Egg Products":           "dairy",
    "Spices and Herbs":                 "spices",
    "Baby Foods":                       "prepared",
    "Fats and Oils":                    "oils",
    "Poultry Products":                 "meats",
    "Soups, Sauces, and Gravies":       "soups",
    "Sausages and Luncheon Meats":      "meats",
    "Breakfast Cereals":                "grains",
    "Fruits and Fruit Juices":          "fruits",
    "Pork Products":                    "meats",
    "Vegetables and Vegetable Products":"vegetables",
    "Nut and Seed Products":            "nuts",
    "Beef Products":                    "meats",
    "Beverages":                        "beverages",
    "Finfish and Shellfish Products":   "fish",
    "Legumes and Legume Products":      "legumes",
    "Lamb, Veal, and Game Products":    "meats",
    "Baked Products":                   "bakery",
    "Sweets":                           "sweets",
    "Cereal Grains and Pasta":          "grains",
    "Fast Foods":                       "prepared",
    "Meals, Entrees, and Side Dishes":  "prepared",
    "Snacks":                           "snacks",
    "American Indian/Alaska Native Foods": "generic",
    "Restaurant Foods":                 "prepared",
    "Ethnic Foods":                     "prepared",
}


def normalise_usda_category(food_group: str | None) -> str:
    """
    Return Macrowise category for a USDA food group string.
    Falls back to keyword matching if the group isn't in the direct map.
    """
    if not food_group:
        return "generic"

    # Try direct map first
    direct = USDA_GROUP_MAP.get(food_group)
    if direct:
        return direct

    # Fall back to keyword matching
    return normalise_category(None, food_group)


# ---------------------------------------------------------------------------
# Icon name mapping — used by the Flutter app
# Maps Macrowise category -> Phosphor icon name
# ---------------------------------------------------------------------------
CATEGORY_ICONS = {
    "meats":       "meat",
    "fish":        "fish",
    "dairy":       "drop",
    "eggs":        "egg",
    "vegetables":  "leaf",
    "fruits":      "apple-logo",
    "grains":      "bread",
    "legumes":     "plant",
    "nuts":        "nut",
    "oils":        "drop-half",
    "sauces":      "bottle",
    "soups":       "bowl-food",
    "sweets":      "candy",
    "desserts":    "cake",
    "bakery":      "bread",
    "snacks":      "popcorn",
    "beverages":   "coffee",
    "spices":      "pepper",
    "prepared":    "cooking-pot",
    "supplements": "pill",
    "generic":     "fork-knife",
}

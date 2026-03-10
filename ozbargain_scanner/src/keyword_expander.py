"""
Smart keyword expander.

For each shopping list item, this module generates a set of search terms that
broadens the OzBargain search beyond the literal item name — catching synonyms,
brand variants, common abbreviations, and category-level terms.
"""

from __future__ import annotations

import re
from typing import Dict, List, Set

# ---------------------------------------------------------------------------
# Expansion dictionary
# Key: canonical lower-case term (or substring that triggers the rule)
# Value: list of additional search terms to add
# ---------------------------------------------------------------------------
EXPANSION_MAP: Dict[str, List[str]] = {
    # ── Audio ──────────────────────────────────────────────────────────────
    "headphones": ["headphones", "headset", "earphones", "earbuds", "ANC headphones", "noise cancelling headphones"],
    "earbuds": ["earbuds", "earphones", "TWS", "true wireless", "AirPods"],
    "speaker": ["speaker", "bluetooth speaker", "soundbar", "portable speaker"],
    "soundbar": ["soundbar", "sound bar", "home theatre", "home theater"],

    # ── TVs & Displays ─────────────────────────────────────────────────────
    "tv": ["TV", "television", "OLED TV", "QLED TV", "4K TV", "smart TV", "LED TV"],
    "television": ["television", "TV", "OLED", "QLED", "4K", "smart TV"],
    "monitor": ["monitor", "display", "IPS monitor", "gaming monitor", "4K monitor"],
    "projector": ["projector", "home cinema", "home theater projector"],

    # ── Phones ─────────────────────────────────────────────────────────────
    "iphone": ["iPhone", "Apple iPhone", "iOS"],
    "samsung phone": ["Samsung phone", "Samsung Galaxy", "Galaxy S", "Galaxy A"],
    "pixel": ["Google Pixel", "Pixel phone"],
    "phone": ["phone", "smartphone", "mobile phone", "handset"],

    # ── Computers & Laptops ────────────────────────────────────────────────
    "laptop": ["laptop", "notebook", "ultrabook", "MacBook", "Chromebook"],
    "macbook": ["MacBook", "Apple laptop", "Mac laptop"],
    "desktop": ["desktop", "PC", "desktop computer", "tower PC"],
    "tablet": ["tablet", "iPad", "Android tablet", "iPad Pro"],
    "ipad": ["iPad", "Apple tablet"],

    # ── Gaming ─────────────────────────────────────────────────────────────
    "ps5": ["PS5", "PlayStation 5", "Sony PlayStation"],
    "playstation": ["PlayStation", "PS5", "PS4", "Sony gaming"],
    "xbox": ["Xbox", "Xbox Series X", "Xbox Series S", "Microsoft Xbox"],
    "nintendo": ["Nintendo", "Switch", "Nintendo Switch", "OLED Switch"],
    "gaming chair": ["gaming chair", "office chair", "ergonomic chair"],
    "gaming mouse": ["gaming mouse", "mouse", "wireless mouse"],
    "gaming keyboard": ["gaming keyboard", "mechanical keyboard", "keyboard"],
    "gpu": ["GPU", "graphics card", "RTX", "RX", "video card"],
    "graphics card": ["graphics card", "GPU", "RTX", "Radeon", "GeForce"],

    # ── Coffee & Kitchen ───────────────────────────────────────────────────
    "coffee machine": ["coffee machine", "coffee maker", "espresso machine", "pod machine", "Nespresso", "Breville", "De'Longhi"],
    "coffee maker": ["coffee maker", "coffee machine", "espresso", "Nespresso"],
    "air fryer": ["air fryer", "airfryer", "convection oven"],
    "instant pot": ["Instant Pot", "pressure cooker", "multicooker", "slow cooker"],
    "blender": ["blender", "smoothie maker", "NutriBullet", "Vitamix"],
    "toaster": ["toaster", "toaster oven", "sandwich press"],
    "microwave": ["microwave", "microwave oven"],
    "dishwasher": ["dishwasher", "dish washer"],
    "fridge": ["fridge", "refrigerator", "freezer combo"],
    "washing machine": ["washing machine", "washer", "front loader", "top loader"],
    "dryer": ["dryer", "clothes dryer", "tumble dryer", "heat pump dryer"],

    # ── Tools & Home Improvement ───────────────────────────────────────────
    "drill": ["drill", "power drill", "cordless drill", "impact driver"],
    "vacuum": ["vacuum", "vacuum cleaner", "robot vacuum", "Roomba", "Dyson"],
    "robot vacuum": ["robot vacuum", "robovac", "Roomba", "Roborock", "iRobot"],
    "lawn mower": ["lawn mower", "lawnmower", "robot mower", "grass cutter"],
    "pressure washer": ["pressure washer", "high pressure cleaner", "karcher"],

    # ── Fitness & Health ───────────────────────────────────────────────────
    "treadmill": ["treadmill", "running machine", "home gym"],
    "bike": ["bike", "bicycle", "e-bike", "electric bike", "mountain bike"],
    "smartwatch": ["smartwatch", "smart watch", "fitness tracker", "Apple Watch", "Garmin", "Fitbit"],
    "apple watch": ["Apple Watch", "smartwatch", "fitness tracker"],
    "massage gun": ["massage gun", "percussive massager", "muscle gun"],

    # ── Cameras ────────────────────────────────────────────────────────────
    "camera": ["camera", "DSLR", "mirrorless camera", "action camera", "GoPro"],
    "gopro": ["GoPro", "action camera", "action cam"],
    "dash cam": ["dash cam", "dashcam", "car camera", "dash camera"],

    # ── Networking ─────────────────────────────────────────────────────────
    "router": ["router", "wifi router", "mesh router", "NBN router", "modem router"],
    "mesh wifi": ["mesh wifi", "wifi mesh", "mesh network", "Eero", "Google Nest WiFi", "TP-Link Deco"],
    "nas": ["NAS", "network attached storage", "Synology", "QNAP"],

    # ── Smart Home ─────────────────────────────────────────────────────────
    "smart bulb": ["smart bulb", "smart light", "LED smart bulb", "Philips Hue", "LIFX"],
    "smart plug": ["smart plug", "wifi plug", "smart switch", "smart outlet"],
    "security camera": ["security camera", "CCTV", "IP camera", "wifi camera", "doorbell camera"],

    # ── Baby & Kids ────────────────────────────────────────────────────────
    "pram": ["pram", "stroller", "baby pram", "baby stroller", "pushchair"],
    "car seat": ["car seat", "baby car seat", "child car seat", "booster seat"],

    # ── Clothing & Fashion ─────────────────────────────────────────────────
    "sneakers": ["sneakers", "trainers", "running shoes", "athletic shoes"],
    "running shoes": ["running shoes", "sneakers", "trainers", "jogging shoes"],
    "jeans": ["jeans", "denim", "pants"],
    "hoodie": ["hoodie", "sweatshirt", "jumper"],

    # ── Travel & Luggage ──────────────────────────────────────────────────
    "luggage": ["luggage", "suitcase", "travel bag", "carry-on"],
    "backpack": ["backpack", "rucksack", "daypack", "travel backpack"],
}

# Abbreviation normalisation map (expand short forms before lookup)
ABBREVIATIONS: Dict[str, str] = {
    r"\btv\b": "tv",
    r"\bpcs?\b": "PC",
    r"\bps5\b": "ps5",
    r"\bps4\b": "playstation",
    r"\bnintendo\b": "nintendo",
    r"\bssd\b": "SSD",
    r"\bhdd\b": "hard drive",
    r"\bram\b": "RAM",
}


def expand_keywords(item: str, smart: bool = True) -> List[str]:
    """
    Return a deduplicated list of search terms for *item*.

    Always includes the original item.  When *smart* is True, also adds
    synonyms and brand variants based on EXPANSION_MAP.
    """
    terms: Set[str] = {item.strip()}

    if not smart:
        return list(terms)

    normalised = item.lower().strip()

    # Check every rule whose key appears as a substring of the item
    for trigger, expansions in EXPANSION_MAP.items():
        if trigger in normalised:
            terms.update(expansions)

    # Also check if the full item matches a key exactly (handles short items)
    for trigger, expansions in EXPANSION_MAP.items():
        if re.search(r"\b" + re.escape(trigger) + r"\b", normalised):
            terms.update(expansions)

    # Deduplicate while preserving insertion order (Python 3.7+)
    seen: Set[str] = set()
    result: List[str] = []
    for t in terms:
        lower = t.lower()
        if lower not in seen:
            seen.add(lower)
            result.append(t)

    return result


def build_search_queries(shopping_list: List[str], smart: bool = True) -> Dict[str, List[str]]:
    """
    Map each shopping list item to its expanded list of search queries.

    Returns a dict: { original_item: [query1, query2, ...] }
    """
    return {item: expand_keywords(item, smart) for item in shopping_list}

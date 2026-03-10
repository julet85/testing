"""
OzBargain scanner.

Strategy:
  1. Fetch the global "new deals" RSS feed for a broad sweep.
  2. For each unique expanded keyword, also fetch OzBargain's per-search RSS
     feed so we catch deals that might have scrolled off the global feed.
  3. Score every deal by upvotes and recency, filter by configured thresholds,
     and return matches grouped by the shopping-list item that triggered them.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

OZBARGAIN_BASE = "https://www.ozbargain.com.au"
GLOBAL_FEED_URL = f"{OZBARGAIN_BASE}/deals/feed"
SEARCH_FEED_URL = f"{OZBARGAIN_BASE}/search/node/{{query}}%20type%3Adeals/feed"
SEARCH_PAGE_URL = f"{OZBARGAIN_BASE}/search/node/{{query}}?type=deals"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; OzBargainScanner/1.0; HomeAssistant Addon)"
    )
}

REQUEST_TIMEOUT = 15  # seconds
MAX_SEARCH_RESULTS = 20  # results to examine per keyword search


@dataclass
class Deal:
    id: str                          # stable hash of URL
    title: str
    url: str
    description: str
    price: Optional[str]
    discount_percent: Optional[float]
    upvotes: int
    published: datetime
    category: str
    thumbnail: Optional[str]
    matched_items: List[str] = field(default_factory=list)  # shopping list items
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "description": self.description,
            "price": self.price,
            "discount_percent": self.discount_percent,
            "upvotes": self.upvotes,
            "published": self.published.isoformat(),
            "category": self.category,
            "thumbnail": self.thumbnail,
            "matched_items": self.matched_items,
            "score": self.score,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deal_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:16]


def _parse_price(text: str) -> Optional[str]:
    m = re.search(r"\$[\d,]+(?:\.\d{1,2})?", text)
    return m.group() if m else None


def _parse_discount(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*off", text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _parse_votes(entry) -> int:
    """Extract vote count from feedparser entry."""
    # OzBargain puts votes in <ozb:meta votes="..."> or summary text
    try:
        meta = entry.get("ozb_meta", {})
        if isinstance(meta, dict):
            return int(meta.get("votes", 0))
    except (TypeError, ValueError):
        pass

    # Fallback: scrape vote count from summary HTML
    summary = entry.get("summary", "")
    m = re.search(r"(\d+)\s+votes?", summary, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return 0


def _parse_published(entry) -> datetime:
    try:
        t = entry.get("published_parsed") or entry.get("updated_parsed")
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    except Exception:
        pass
    return datetime.now(timezone.utc)


def _recency_weight(published: datetime) -> float:
    """
    Returns a weight 0–1 that decays with deal age.
    Deals < 1 h old → 1.0; deals > 48 h old → ~0.
    """
    age_hours = (datetime.now(timezone.utc) - published).total_seconds() / 3600
    return max(0.0, 1.0 - age_hours / 48)


def _compute_score(deal: Deal) -> float:
    return deal.upvotes * (1 + _recency_weight(deal.published))


def _fetch_feed(url: str) -> List[feedparser.FeedParserDict]:
    """Fetch and parse an RSS/Atom feed, returning entries."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        return parsed.get("entries", [])
    except Exception as exc:
        logger.warning("Failed to fetch feed %s: %s", url, exc)
        return []


def _entry_to_deal(entry) -> Optional[Deal]:
    """Convert a feedparser entry into a Deal object."""
    url = entry.get("link", "")
    if not url:
        return None

    title = entry.get("title", "").strip()
    summary = entry.get("summary", "")
    soup = BeautifulSoup(summary, "lxml")
    description = soup.get_text(separator=" ", strip=True)

    # Try thumbnail
    thumbnail = None
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image"):
            thumbnail = enc.get("href")
            break
    if not thumbnail:
        img = soup.find("img")
        if img:
            thumbnail = img.get("src")

    category = ""
    tags = entry.get("tags", [])
    if tags:
        category = tags[0].get("term", "")

    published = _parse_published(entry)
    upvotes = _parse_votes(entry)
    price = _parse_price(title + " " + description)
    discount = _parse_discount(title + " " + description)

    deal = Deal(
        id=_deal_id(url),
        title=title,
        url=url,
        description=description[:500],
        price=price,
        discount_percent=discount,
        upvotes=upvotes,
        published=published,
        category=category,
        thumbnail=thumbnail,
    )
    deal.score = _compute_score(deal)
    return deal


# ---------------------------------------------------------------------------
# Search via OzBargain search page (HTML scraping)
# ---------------------------------------------------------------------------

def _scrape_search_page(keyword: str) -> List[Deal]:
    """Scrape OzBargain search results page for a keyword."""
    url = SEARCH_PAGE_URL.format(query=quote_plus(keyword))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Search page fetch failed for '%s': %s", keyword, exc)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    deals: List[Deal] = []

    for node in soup.select(".node-ozbdeal")[:MAX_SEARCH_RESULTS]:
        try:
            title_el = node.select_one("h2.title a")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            if href.startswith("/"):
                href = OZBARGAIN_BASE + href

            desc_el = node.select_one(".content p")
            description = desc_el.get_text(strip=True)[:500] if desc_el else ""

            # Votes
            vote_el = node.select_one(".voteup .vote-count, .vote-count")
            upvotes = 0
            if vote_el:
                try:
                    upvotes = int(vote_el.get_text(strip=True).replace("+", ""))
                except ValueError:
                    pass

            # Category
            cat_el = node.select_one(".taxonomy-links a")
            category = cat_el.get_text(strip=True) if cat_el else ""

            # Thumbnail
            img_el = node.select_one("img")
            thumbnail = img_el.get("src") if img_el else None

            # Time
            time_el = node.select_one("time")
            published = datetime.now(timezone.utc)
            if time_el and time_el.get("datetime"):
                try:
                    published = datetime.fromisoformat(
                        time_el["datetime"].replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

            price = _parse_price(title + " " + description)
            discount = _parse_discount(title + " " + description)

            deal = Deal(
                id=_deal_id(href),
                title=title,
                url=href,
                description=description,
                price=price,
                discount_percent=discount,
                upvotes=upvotes,
                published=published,
                category=category,
                thumbnail=thumbnail,
            )
            deal.score = _compute_score(deal)
            deals.append(deal)

        except Exception as exc:
            logger.debug("Error parsing deal node: %s", exc)
            continue

    return deals


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _matches_any_query(deal: Deal, queries: List[str], excluded: List[str]) -> bool:
    """True if deal title/description matches any query and has no exclusions."""
    haystack = (deal.title + " " + deal.description).lower()

    for excl in excluded:
        if excl.lower() in haystack:
            return False

    for q in queries:
        if q.lower() in haystack:
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan(
    keyword_map: Dict[str, List[str]],
    min_upvotes: int = 5,
    min_discount: float = 0,
    min_score: float = 0,
    excluded_keywords: Optional[List[str]] = None,
) -> List[Deal]:
    """
    Scan OzBargain for deals matching *keyword_map*.

    Parameters
    ----------
    keyword_map:
        { shopping_list_item: [expanded_query1, ...] }
    min_upvotes:
        Discard deals with fewer upvotes.
    min_discount:
        Discard deals with a lower discount percentage (0 = no filter).
    min_score:
        Discard deals below this computed score.
    excluded_keywords:
        Words that disqualify a deal.

    Returns
    -------
    List of matching Deal objects (deduplicated by deal ID).
    """
    excluded_keywords = excluded_keywords or []
    all_deals: Dict[str, Deal] = {}  # id → Deal

    # ── Step 1: Global "new deals" RSS sweep ────────────────────────────────
    logger.info("Fetching global OzBargain feed…")
    global_entries = _fetch_feed(GLOBAL_FEED_URL)
    for entry in global_entries:
        deal = _entry_to_deal(entry)
        if deal:
            all_deals[deal.id] = deal

    # ── Step 2: Per-keyword search RSS + page scrape ────────────────────────
    unique_queries: Set[str] = set()
    for queries in keyword_map.values():
        unique_queries.update(queries)

    for query in unique_queries:
        logger.info("Searching OzBargain for: %s", query)

        # Try RSS search feed first
        feed_url = SEARCH_FEED_URL.format(query=quote_plus(query))
        for entry in _fetch_feed(feed_url):
            deal = _entry_to_deal(entry)
            if deal and deal.id not in all_deals:
                all_deals[deal.id] = deal

        # Also scrape the HTML search page (catches more results)
        for deal in _scrape_search_page(query):
            if deal.id not in all_deals:
                all_deals[deal.id] = deal

        time.sleep(0.5)  # be polite to OzBargain

    # ── Step 3: Match deals back to shopping list items ─────────────────────
    matched: Dict[str, Deal] = {}
    for deal in all_deals.values():
        for item, queries in keyword_map.items():
            if _matches_any_query(deal, queries, excluded_keywords):
                if deal.id not in matched:
                    matched[deal.id] = deal
                if item not in matched[deal.id].matched_items:
                    matched[deal.id].matched_items.append(item)

    # ── Step 4: Apply filters ───────────────────────────────────────────────
    results = []
    for deal in matched.values():
        if deal.upvotes < min_upvotes:
            continue
        if min_discount > 0 and (deal.discount_percent or 0) < min_discount:
            continue
        if deal.score < min_score:
            continue
        results.append(deal)

    results.sort(key=lambda d: d.score, reverse=True)
    logger.info("Scan complete: %d matching deals found", len(results))
    return results

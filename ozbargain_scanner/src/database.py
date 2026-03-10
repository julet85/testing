"""
SQLite persistence layer.

Tables:
  shopping_items  – the user's shopping list (source: HA todo / manual / UI)
  seen_deals      – deals already notified, with expiry timestamp
  deal_history    – log of every deal ever matched (for the UI history view)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/ozbargain_scanner.db")


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db() -> None:
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS shopping_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL UNIQUE,
                source      TEXT    NOT NULL DEFAULT 'manual',
                added_at    TEXT    NOT NULL,
                active      INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS seen_deals (
                deal_id     TEXT    PRIMARY KEY,
                title       TEXT    NOT NULL,
                url         TEXT    NOT NULL,
                notified_at TEXT    NOT NULL,
                expires_at  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deal_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id         TEXT    NOT NULL,
                title           TEXT    NOT NULL,
                url             TEXT    NOT NULL,
                price           TEXT,
                discount        REAL,
                upvotes         INTEGER,
                score           REAL,
                category        TEXT,
                thumbnail       TEXT,
                matched_items   TEXT,   -- JSON list
                published_at    TEXT,
                discovered_at   TEXT    NOT NULL,
                notified        INTEGER NOT NULL DEFAULT 0
            );
        """)
    logger.info("Database initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Shopping list
# ---------------------------------------------------------------------------

def get_shopping_items(active_only: bool = True) -> List[Dict[str, Any]]:
    query = "SELECT * FROM shopping_items"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY name"
    with _conn() as con:
        rows = con.execute(query).fetchall()
    return [dict(r) for r in rows]


def add_shopping_item(name: str, source: str = "manual") -> bool:
    """Add item; returns True if inserted, False if already existed."""
    try:
        with _conn() as con:
            con.execute(
                "INSERT INTO shopping_items (name, source, added_at) VALUES (?, ?, ?)",
                (name.strip(), source, _now()),
            )
        return True
    except sqlite3.IntegrityError:
        # UNIQUE constraint: item already present → just reactivate it
        with _conn() as con:
            con.execute(
                "UPDATE shopping_items SET active = 1, source = ? WHERE name = ?",
                (source, name.strip()),
            )
        return False


def remove_shopping_item(item_id: int) -> None:
    with _conn() as con:
        con.execute("UPDATE shopping_items SET active = 0 WHERE id = ?", (item_id,))


def sync_shopping_items(items: List[str], source: str) -> None:
    """
    Sync items from an external source (e.g. HA shopping list).
    Items present in *items* are activated; items from the same source that
    are absent are deactivated.
    """
    normalised = [i.strip() for i in items if i.strip()]
    with _conn() as con:
        # Deactivate items from this source that are no longer in the list
        con.execute(
            "UPDATE shopping_items SET active = 0 WHERE source = ? AND name NOT IN ({})".format(
                ",".join("?" * len(normalised)) if normalised else "SELECT NULL"
            ),
            [source] + normalised,
        )
        # Upsert each item
        for name in normalised:
            try:
                con.execute(
                    "INSERT INTO shopping_items (name, source, added_at) VALUES (?, ?, ?)",
                    (name, source, _now()),
                )
            except sqlite3.IntegrityError:
                con.execute(
                    "UPDATE shopping_items SET active = 1, source = ? WHERE name = ?",
                    (source, name),
                )


# ---------------------------------------------------------------------------
# Seen deals (deduplication / cooldown)
# ---------------------------------------------------------------------------

def is_deal_seen(deal_id: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT expires_at FROM seen_deals WHERE deal_id = ?", (deal_id,)
        ).fetchone()
    if not row:
        return False
    expires = datetime.fromisoformat(row["expires_at"])
    if datetime.now(timezone.utc) > expires:
        # Expired — treat as unseen
        with _conn() as con:
            con.execute("DELETE FROM seen_deals WHERE deal_id = ?", (deal_id,))
        return False
    return True


def mark_deal_seen(deal_id: str, title: str, url: str, cooldown_hours: int) -> None:
    expires = datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)
    with _conn() as con:
        con.execute(
            """INSERT INTO seen_deals (deal_id, title, url, notified_at, expires_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(deal_id) DO UPDATE SET notified_at=excluded.notified_at,
               expires_at=excluded.expires_at""",
            (deal_id, title, url, _now(), expires.isoformat()),
        )


def purge_expired_seen() -> int:
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM seen_deals WHERE expires_at < ?", (_now(),)
        )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Deal history
# ---------------------------------------------------------------------------

def save_deal_to_history(deal: Any, notified: bool) -> None:
    with _conn() as con:
        con.execute(
            """INSERT OR IGNORE INTO deal_history
               (deal_id, title, url, price, discount, upvotes, score, category,
                thumbnail, matched_items, published_at, discovered_at, notified)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                deal.id, deal.title, deal.url, deal.price,
                deal.discount_percent, deal.upvotes, deal.score,
                deal.category, deal.thumbnail,
                json.dumps(deal.matched_items),
                deal.published.isoformat(),
                _now(), 1 if notified else 0,
            ),
        )


def get_deal_history(limit: int = 100) -> List[Dict[str, Any]]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM deal_history ORDER BY discovered_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["matched_items"] = json.loads(d.get("matched_items") or "[]")
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Scanner status
# ---------------------------------------------------------------------------

STATUS_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS scanner_status (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
"""


def set_status(key: str, value: str) -> None:
    with _conn() as con:
        con.execute(STATUS_TABLE_SQL)
        con.execute(
            "INSERT INTO scanner_status (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_status(key: str, default: str = "") -> str:
    with _conn() as con:
        con.execute(STATUS_TABLE_SQL)
        row = con.execute(
            "SELECT value FROM scanner_status WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

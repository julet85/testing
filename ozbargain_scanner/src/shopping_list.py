"""
Shopping list manager.

Sources (merged, highest priority last wins for deduplication):
  1. Config-file custom list  (from addon options)
  2. HA todo entity           (polled each scan cycle)
  3. Webhook / UI additions   (stored in SQLite)

All sources are normalised and stored in SQLite so the web UI shows a
unified, editable list.
"""

from __future__ import annotations

import json
import logging
import os
from typing import List

import database as db
import notifier

logger = logging.getLogger(__name__)

USE_HA_LIST = os.environ.get("USE_HA_SHOPPING_LIST", "true").lower() == "true"
HA_TODO_ENTITY = os.environ.get("HA_TODO_ENTITY", "todo.shopping_list")
CUSTOM_LIST_ENV = os.environ.get("CUSTOM_SHOPPING_LIST", "[]")


def _parse_custom_list() -> List[str]:
    """Parse the CUSTOM_SHOPPING_LIST env var (JSON array or newline-separated)."""
    raw = CUSTOM_LIST_ENV.strip()
    if not raw:
        return []
    try:
        items = json.loads(raw)
        return [str(i).strip() for i in items if str(i).strip()]
    except json.JSONDecodeError:
        return [line.strip() for line in raw.splitlines() if line.strip()]


def sync_from_config() -> None:
    """Sync the custom list from addon options into the DB."""
    items = _parse_custom_list()
    if items:
        db.sync_shopping_items(items, source="config")
        logger.info("Synced %d item(s) from addon config", len(items))


def sync_from_ha() -> None:
    """Pull unchecked items from the HA todo entity and sync to DB."""
    if not USE_HA_LIST:
        return
    items = notifier.fetch_ha_todo_items(HA_TODO_ENTITY)
    if items:
        db.sync_shopping_items(items, source="ha_todo")
        logger.info("Synced %d item(s) from HA todo '%s'", len(items), HA_TODO_ENTITY)
    else:
        logger.debug("No items returned from HA todo entity '%s'", HA_TODO_ENTITY)


def get_active_items() -> List[str]:
    """Return all active shopping list item names."""
    rows = db.get_shopping_items(active_only=True)
    return [r["name"] for r in rows]


def add_item(name: str, source: str = "manual") -> bool:
    name = name.strip()
    if not name:
        return False
    added = db.add_shopping_item(name, source)
    logger.info("%s shopping item: '%s' (source=%s)", "Added" if added else "Reactivated", name, source)
    return True


def remove_item(item_id: int) -> None:
    db.remove_shopping_item(item_id)
    logger.info("Deactivated shopping item id=%d", item_id)


def full_sync() -> List[str]:
    """
    Run a full sync from all sources and return the unified active list.
    Called at the start of every scan cycle.
    """
    sync_from_config()
    sync_from_ha()
    return get_active_items()

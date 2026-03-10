"""
Home Assistant notification sender.

Uses the Supervisor proxy (http://supervisor/core/api) with the
SUPERVISOR_TOKEN so no user-supplied token is needed.
"""

from __future__ import annotations

import logging
import os
from typing import List

import requests

from scanner import Deal

logger = logging.getLogger(__name__)

HA_URL = os.environ.get("HA_URL", "http://supervisor/core")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
NOTIFY_SERVICE = os.environ.get("NOTIFY_SERVICE", "notify.notify")
MAX_PER_NOTIFICATION = int(os.environ.get("MAX_DEALS_PER_NOTIFICATION", "5"))

REQUEST_TIMEOUT = 10


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }


def _call_service(domain: str, service: str, data: dict) -> bool:
    url = f"{HA_URL}/api/services/{domain}/{service}"
    try:
        resp = requests.post(
            url, json=data, headers=_headers(), timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error("HA service call %s.%s failed: %s", domain, service, exc)
        return False


# ---------------------------------------------------------------------------
# Shopping list sync
# ---------------------------------------------------------------------------

def fetch_ha_todo_items(entity_id: str) -> List[str]:
    """
    Retrieve unchecked items from a HA todo/shopping-list entity.

    Returns a list of item names (strings).
    """
    url = f"{HA_URL}/api/states/{entity_id}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        state = resp.json()

        # HA 2023.11+ stores todo items in attributes
        attributes = state.get("attributes", {})
        items = attributes.get("items", [])
        if items:
            return [
                i.get("summary", i.get("name", ""))
                for i in items
                if i.get("status", "needs_action") == "needs_action"
            ]
    except Exception as exc:
        logger.warning("Could not fetch HA todo entity '%s': %s", entity_id, exc)

    # Fallback: try the todo/get_items service (returns items via event)
    # Since this is async we cannot easily retrieve the response here;
    # callers should rely on the states endpoint only.
    return []


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _format_deal(deal: Deal) -> str:
    parts = [f"• **{deal.title}**"]
    if deal.price:
        parts.append(f"  Price: {deal.price}")
    if deal.discount_percent:
        parts.append(f"  Discount: {deal.discount_percent:.0f}% off")
    parts.append(f"  Votes: {deal.upvotes}  |  {deal.url}")
    return "\n".join(parts)


def send_deal_notification(deals: List[Deal], service_name: str | None = None) -> bool:
    """
    Send a single notification bundling up to MAX_PER_NOTIFICATION deals.

    Returns True if the notification was successfully sent.
    """
    service_name = service_name or NOTIFY_SERVICE
    if not deals:
        return False

    chunk = deals[:MAX_PER_NOTIFICATION]
    overflow = len(deals) - len(chunk)

    items_found = set()
    for d in chunk:
        items_found.update(d.matched_items)

    if len(items_found) == 1:
        title = f"OzBargain Deal: {next(iter(items_found))}"
    else:
        title = f"OzBargain: {len(chunk)} deal{'s' if len(chunk) > 1 else ''} found"

    lines = [f"Found {len(chunk)} deal{'s' if len(chunk) > 1 else ''} on your shopping list!\n"]
    for deal in chunk:
        lines.append(_format_deal(deal))
        lines.append("")

    if overflow > 0:
        lines.append(f"…and {overflow} more deal{'s' if overflow > 1 else ''}.")
        lines.append("Open the OzBargain Scanner panel to see all deals.")

    message = "\n".join(lines)

    # Determine service domain and name
    if "." in service_name:
        domain, svc = service_name.split(".", 1)
    else:
        domain, svc = "notify", service_name

    data: dict = {
        "title": title,
        "message": message,
    }

    # Add action button for mobile app notifications
    if domain == "notify":
        data["data"] = {
            "actions": [
                {"action": "URI", "title": "View Deal", "uri": chunk[0].url}
            ]
        }

    ok = _call_service(domain, svc, data)
    if ok:
        logger.info("Notification sent via %s.%s: %d deals", domain, svc, len(chunk))
    return ok


def send_persistent_notification(message: str, title: str = "OzBargain Scanner") -> None:
    """Create a persistent notification in the HA dashboard."""
    _call_service(
        "persistent_notification",
        "create",
        {"title": title, "message": message, "notification_id": "ozbargain_scanner"},
    )

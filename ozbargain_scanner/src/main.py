"""
OzBargain Deal Scanner – main entry point.

Runs two things in the same process:
  • APScheduler job  – periodically scans OzBargain and fires HA notifications.
  • Flask web server – serves the ingress UI and a small REST API.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, redirect, render_template, request, url_for

import database as db
import keyword_expander
import notifier
import scanner
import shopping_list

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))
MIN_UPVOTES = int(os.environ.get("MIN_UPVOTES", "5"))
MIN_DISCOUNT = float(os.environ.get("MIN_DISCOUNT_PERCENT", "0"))
NOTIFY_SERVICE = os.environ.get("NOTIFY_SERVICE", "notify.notify")
SMART_EXPANSION = os.environ.get("SMART_KEYWORD_EXPANSION", "true").lower() == "true"
COOLDOWN_HOURS = int(os.environ.get("NOTIFICATION_COOLDOWN_HOURS", "24"))
DEAL_SCORE_THRESHOLD = float(os.environ.get("DEAL_SCORE_THRESHOLD", "0"))
EXCLUDED_KEYWORDS_RAW = os.environ.get("EXCLUDED_KEYWORDS", "[]")
INGRESS_PATH = os.environ.get("INGRESS_PATH", "")

try:
    EXCLUDED_KEYWORDS: List[str] = json.loads(EXCLUDED_KEYWORDS_RAW)
except json.JSONDecodeError:
    EXCLUDED_KEYWORDS = []

# ---------------------------------------------------------------------------
# Scanner job
# ---------------------------------------------------------------------------

_scan_lock = threading.Lock()


def run_scan(manual: bool = False) -> Dict[str, Any]:
    """
    Execute one full scan cycle.  Thread-safe via _scan_lock.
    Returns a summary dict suitable for the API.
    """
    if not _scan_lock.acquire(blocking=False):
        logger.info("Scan already in progress, skipping.")
        return {"status": "skipped", "reason": "scan already running"}

    result: Dict[str, Any] = {}
    try:
        db.set_status("last_scan_start", datetime.now(timezone.utc).isoformat())
        db.set_status("scan_status", "running")

        # 1. Sync shopping list from all sources
        items = shopping_list.full_sync()
        if not items:
            logger.warning("Shopping list is empty – nothing to scan for.")
            db.set_status("scan_status", "idle")
            result = {"status": "ok", "deals_found": 0, "message": "Shopping list is empty."}
            return result

        logger.info("Scanning for %d shopping list item(s): %s", len(items), items)

        # 2. Build keyword map (with optional smart expansion)
        keyword_map = keyword_expander.build_search_queries(items, smart=SMART_EXPANSION)

        # 3. Scan OzBargain
        deals = scanner.scan(
            keyword_map=keyword_map,
            min_upvotes=MIN_UPVOTES,
            min_discount=MIN_DISCOUNT,
            min_score=DEAL_SCORE_THRESHOLD,
            excluded_keywords=EXCLUDED_KEYWORDS,
        )

        # 4. Filter out already-notified deals
        new_deals = [d for d in deals if not db.is_deal_seen(d.id)]

        # 5. Save all to history; mark new ones as seen
        for deal in deals:
            notified = deal in new_deals
            db.save_deal_to_history(deal, notified=notified)

        # 6. Send notification for new deals
        if new_deals:
            sent = notifier.send_deal_notification(new_deals, service_name=NOTIFY_SERVICE)
            if sent:
                for deal in new_deals:
                    db.mark_deal_seen(deal.id, deal.title, deal.url, COOLDOWN_HOURS)
                logger.info("Notified about %d new deal(s).", len(new_deals))
            else:
                logger.warning("Notification failed – will retry next cycle.")
        else:
            logger.info("No new deals to notify about.")

        # 7. Purge expired seen-deals entries
        purged = db.purge_expired_seen()
        if purged:
            logger.debug("Purged %d expired seen-deal entries.", purged)

        db.set_status("last_scan_end", datetime.now(timezone.utc).isoformat())
        db.set_status("last_scan_deals_found", str(len(new_deals)))
        db.set_status("scan_status", "idle")

        result = {
            "status": "ok",
            "items_scanned": len(items),
            "deals_found": len(deals),
            "new_deals": len(new_deals),
            "manual": manual,
        }
        return result

    except Exception as exc:
        logger.exception("Scan failed: %s", exc)
        db.set_status("scan_status", "error")
        db.set_status("last_error", str(exc))
        result = {"status": "error", "message": str(exc)}
        return result
    finally:
        _scan_lock.release()


# ---------------------------------------------------------------------------
# Flask web app
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="/app/webapp/templates")


def _base_url() -> str:
    return INGRESS_PATH.rstrip("/")


@app.route("/")
def index():
    items = db.get_shopping_items(active_only=False)
    history = db.get_deal_history(limit=50)
    status = {
        "scan_status": db.get_status("scan_status", "idle"),
        "last_scan_start": db.get_status("last_scan_start", "Never"),
        "last_scan_end": db.get_status("last_scan_end", "Never"),
        "last_scan_deals_found": db.get_status("last_scan_deals_found", "0"),
        "last_error": db.get_status("last_error", ""),
        "check_interval": CHECK_INTERVAL,
        "notify_service": NOTIFY_SERVICE,
        "smart_expansion": SMART_EXPANSION,
    }
    return render_template(
        "index.html",
        items=items,
        history=history,
        status=status,
        base_url=_base_url(),
    )


# ── API: Shopping list ──────────────────────────────────────────────────────

@app.route("/api/items", methods=["GET"])
def api_get_items():
    items = db.get_shopping_items(active_only=False)
    return jsonify(items)


@app.route("/api/items", methods=["POST"])
def api_add_item():
    data = request.get_json(force=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    shopping_list.add_item(name, source="ui")
    items = db.get_shopping_items(active_only=False)
    return jsonify({"ok": True, "items": items}), 201


@app.route("/api/items/<int:item_id>", methods=["DELETE"])
def api_delete_item(item_id: int):
    shopping_list.remove_item(item_id)
    return jsonify({"ok": True})


@app.route("/api/items/<int:item_id>/toggle", methods=["POST"])
def api_toggle_item(item_id: int):
    """Toggle the active state of an item."""
    with db._conn() as con:
        row = con.execute(
            "SELECT active FROM shopping_items WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        new_state = 0 if row["active"] else 1
        con.execute(
            "UPDATE shopping_items SET active = ? WHERE id = ?", (new_state, item_id)
        )
    return jsonify({"ok": True, "active": bool(new_state)})


# ── API: Webhook (for HA automations / voice assistant) ────────────────────

@app.route("/api/webhook/add", methods=["POST"])
def api_webhook_add():
    """
    Webhook endpoint for adding items via HA automations.

    Accepts:
      { "items": ["item1", "item2"] }
    or plain text body (one item per line).
    """
    if request.content_type and "json" in request.content_type:
        data = request.get_json(force=True) or {}
        raw_items = data.get("items", [])
        if isinstance(raw_items, str):
            raw_items = [raw_items]
    else:
        raw_items = [
            line.strip()
            for line in (request.data.decode("utf-8", errors="ignore")).splitlines()
            if line.strip()
        ]

    added = 0
    for item in raw_items:
        if shopping_list.add_item(item, source="webhook"):
            added += 1

    return jsonify({"ok": True, "added": added, "total_received": len(raw_items)})


# ── API: Scanner control ────────────────────────────────────────────────────

@app.route("/api/scan", methods=["POST"])
def api_trigger_scan():
    """Trigger an immediate manual scan (runs in background thread)."""
    t = threading.Thread(target=run_scan, kwargs={"manual": True}, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Scan started"})


@app.route("/api/status", methods=["GET"])
def api_status():
    active_items = shopping_list.get_active_items()
    return jsonify({
        "scan_status": db.get_status("scan_status", "idle"),
        "last_scan_start": db.get_status("last_scan_start", ""),
        "last_scan_end": db.get_status("last_scan_end", ""),
        "last_scan_deals_found": db.get_status("last_scan_deals_found", "0"),
        "last_error": db.get_status("last_error", ""),
        "check_interval_minutes": CHECK_INTERVAL,
        "active_items_count": len(active_items),
        "smart_expansion": SMART_EXPANSION,
    })


# ── API: Deal history ───────────────────────────────────────────────────────

@app.route("/api/history", methods=["GET"])
def api_history():
    limit = int(request.args.get("limit", 100))
    history = db.get_deal_history(limit=limit)
    return jsonify(history)


@app.route("/api/keywords/<path:item_name>", methods=["GET"])
def api_expanded_keywords(item_name: str):
    """Preview the expanded keywords for a shopping list item."""
    terms = keyword_expander.expand_keywords(item_name, smart=SMART_EXPANSION)
    return jsonify({"item": item_name, "keywords": terms})


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(
        func=run_scan,
        trigger="interval",
        minutes=CHECK_INTERVAL,
        id="ozbargain_scan",
        name="OzBargain periodic scan",
        replace_existing=True,
    )
    sched.start()
    logger.info(
        "Scheduler started – scanning every %d minute(s). Next run: %s",
        CHECK_INTERVAL,
        sched.get_job("ozbargain_scan").next_run_time,
    )
    return sched


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def main() -> None:
    # Initialise database
    db.init_db()

    # Sync config items on startup
    shopping_list.sync_from_config()

    # Run an initial scan shortly after boot
    t = threading.Timer(10.0, run_scan)
    t.daemon = True
    t.start()

    # Start periodic scheduler
    scheduler = start_scheduler()

    # Start Flask (blocks)
    try:
        app.run(host="0.0.0.0", port=8099, debug=False, use_reloader=False)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    main()

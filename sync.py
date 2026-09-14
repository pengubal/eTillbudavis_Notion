"""
etilbudsavis-notion sync
=========================

Reads ingredient rows from a Notion database, checks each one against
eTilbudsavis/Tjek for current grocery discounts, and writes the result
back onto the Notion row (on_sale checkbox + store/price/expiry text).

Two moving parts:

1. Notion API  -> fully documented, stable, implemented for real below.
2. Tjek API    -> UNOFFICIAL, undocumented. The function `search_offers()`
   below is a best-effort implementation. If it doesn't return real data,
   see README.md "Fixing the Tjek request" section: capture the real
   request from your browser's devtools and we'll patch this function
   to match exactly.

Environment variables (see .env.example):
    NOTION_TOKEN          Notion internal integration token
    NOTION_DATABASE_ID    Database with your ingredient list
    NOTION_NAME_PROPERTY  Title property holding the ingredient name (default "Name")
    TJEK_LAT / TJEK_LNG   Your location, for store radius filtering
    TJEK_RADIUS_M         Search radius in metres (default 25000)
    TJEK_STORES           Comma-separated store names to restrict to (optional)
    RUN_INTERVAL_HOURS    How often to loop when run as a long-lived container (default 6)
"""

from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass

import requests

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("etilbudsavis-notion")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
NOTION_NAME_PROPERTY = os.environ.get("NOTION_NAME_PROPERTY", "Name")
NOTION_VERSION = "2022-06-28"

TJEK_LAT = float(os.environ.get("TJEK_LAT", "55.6761"))   # default: Copenhagen
TJEK_LNG = float(os.environ.get("TJEK_LNG", "12.5683"))
TJEK_RADIUS_M = int(os.environ.get("TJEK_RADIUS_M", "25000"))
TJEK_STORES = [s.strip() for s in os.environ.get("TJEK_STORES", "").split(",") if s.strip()]

RUN_INTERVAL_HOURS = float(os.environ.get("RUN_INTERVAL_HOURS", "6"))

NOTION_API_BASE = "https://api.notion.com/v1"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Offer:
    heading: str
    store: str
    price: float | None
    currency: str
    run_till: str | None  # ISO date string, if available


@dataclass
class NotionRow:
    page_id: str
    name: str


# --------------------------------------------------------------------------
# Notion side (documented, stable: https://developers.notion.com)
# --------------------------------------------------------------------------

def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def fetch_ingredient_rows() -> list[NotionRow]:
    """Paginate through the Notion database and return (page_id, name) pairs."""
    rows: list[NotionRow] = []
    url = f"{NOTION_API_BASE}/databases/{NOTION_DATABASE_ID}/query"
    payload: dict = {"page_size": 100}

    while True:
        resp = requests.post(url, headers=_notion_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        for page in data.get("results", []):
            title_prop = page["properties"].get(NOTION_NAME_PROPERTY)
            if not title_prop or title_prop["type"] != "title":
                log.warning(
                    "Row %s has no '%s' title property, skipping",
                    page["id"], NOTION_NAME_PROPERTY,
                )
                continue
            title_fragments = title_prop["title"]
            name = "".join(fragment["plain_text"] for fragment in title_fragments).strip()
            if name:
                rows.append(NotionRow(page_id=page["id"], name=name))

        if data.get("has_more"):
            payload["start_cursor"] = data["next_cursor"]
        else:
            break

    log.info("Fetched %d ingredient rows from Notion", len(rows))
    return rows


def update_row_with_offer(page_id: str, offer: Offer | None) -> None:
    """Set the On Sale / Store / Price / Sale Ends properties on a Notion page.

    Assumes the database has these properties (create them once in Notion):
        On Sale    -> checkbox
        Store      -> rich_text
        Sale Price -> number
        Sale Ends  -> date
    Adjust the property names below if yours differ.
    """
    url = f"{NOTION_API_BASE}/pages/{page_id}"

    if offer is None:
        properties = {
            "On Sale": {"checkbox": False},
            "Store": {"rich_text": []},
            "Sale Price": {"number": None},
            "Sale Ends": {"date": None},
        }
    else:
        properties = {
            "On Sale": {"checkbox": True},
            "Store": {"rich_text": [{"text": {"content": offer.store}}]},
            "Sale Price": {"number": offer.price},
            "Sale Ends": (
                {"date": {"start": offer.run_till}} if offer.run_till else {"date": None}
            ),
        }

    resp = requests.patch(url, headers=_notion_headers(), json={"properties": properties}, timeout=30)
    resp.raise_for_status()


# --------------------------------------------------------------------------
# Tjek / eTilbudsavis side (UNOFFICIAL — verify against real traffic)
# --------------------------------------------------------------------------
#
# Best-effort implementation. The eTilbudsavis "Tjek" backend has served
# offer search under names like api.etilbudsavis.dk/v2 and, more recently,
# squid-api.tjek.com/v2 — this has changed over the product's history and
# I can't promise the exact current shape from static knowledge. If this
# function returns no results even for a term you know is on sale, follow
# README.md's "Fixing the Tjek request" steps and send me the real request;
# I'll patch this function to match.

TJEK_API_BASE = os.environ.get("TJEK_API_BASE", "https://squid-api.tjek.com/v2")


def search_offers(term: str) -> list[Offer]:
    """Search current offers for a search term near TJEK_LAT/TJEK_LNG."""
    params = {
        "query": term,
        "r_lat": TJEK_LAT,
        "r_lng": TJEK_LNG,
        "r_radius": TJEK_RADIUS_M,
        "offset": 0,
        "limit": 24,
        "order_by": "-relevance",
    }

    try:
        resp = requests.get(f"{TJEK_API_BASE}/offers/search", params=params, timeout=15)
        resp.raise_for_status()
        raw_offers = resp.json()
    except requests.RequestException as exc:
        log.error("Tjek search failed for %r: %s", term, exc)
        return []

    offers: list[Offer] = []
    for item in raw_offers:
        try:
            store_name = (
                item.get("branding", {}).get("name")
                or item.get("dealer", {}).get("name")
                or "Unknown store"
            )
            if TJEK_STORES and store_name not in TJEK_STORES:
                continue

            pricing = item.get("pricing", {})
            price = pricing.get("price")
            currency = pricing.get("currency", "DKK")

            run_till = item.get("run_till")

            offers.append(
                Offer(
                    heading=item.get("heading", term),
                    store=store_name,
                    price=price,
                    currency=currency,
                    run_till=run_till,
                )
            )
        except Exception:  # noqa: BLE001 - keep going even if one offer is malformed
            log.exception("Could not parse offer for term %r: %r", term, item)

    return offers


def pick_best_offer(offers: list[Offer]) -> Offer | None:
    priced = [o for o in offers if o.price is not None]
    if not priced:
        return offers[0] if offers else None
    return min(priced, key=lambda o: o.price)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_once() -> None:
    rows = fetch_ingredient_rows()
    for row in rows:
        offers = search_offers(row.name)
        best = pick_best_offer(offers)
        if best:
            log.info("MATCH  %-25s -> %s @ %s (%s %s)", row.name, best.heading, best.store, best.price, best.currency)
        else:
            log.debug("no offer for %s", row.name)
        update_row_with_offer(row.page_id, best)
        time.sleep(0.5)  # be polite to the unofficial API


def main() -> None:
    interval_seconds = RUN_INTERVAL_HOURS * 3600
    while True:
        started = time.time()
        try:
            run_once()
        except Exception:  # noqa: BLE001 - one bad run shouldn't kill the container
            log.exception("Sync run failed")
        elapsed = time.time() - started
        sleep_for = max(interval_seconds - elapsed, 60)
        log.info("Sync run finished in %.1fs, sleeping %.1fh", elapsed, sleep_for / 3600)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()

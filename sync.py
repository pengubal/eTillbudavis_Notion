"""
etilbudsavis-notion sync
=========================

Reads ingredient rows from a Notion database, looks each one up on
eTilbudsavis/Tjek, and writes the top relevant offers back to the row.

Notion properties expected on the database (create once):
    On Sale       checkbox
    Store         rich_text   (store of the most relevant offer)
    Sale Price    number      (price of the most relevant offer)
    Sale Ends     date
    Product Link  url
    Alternatives  rich_text   (up to MAX_OFFERS offers, one per line, each linked)

Environment variables (put them in .env, never in the source):
    NOTION_TOKEN, NOTION_DATABASE_ID          required
    NOTION_NAME_PROPERTY                      default "Name"
    TJEK_LAT / TJEK_LNG / TJEK_RADIUS_M       default Copenhagen / 25000
    TJEK_STORES                               comma-separated store names (optional)
    TJEK_API_BASE                             default https://squid-api.tjek.com/v2
    TJEK_SEARCH_URL                           fallback link template, {query} is replaced
    MAX_OFFERS                                default 3
    RUN_INTERVAL_HOURS                        default 6 (loop mode)
    DRY_RUN=1                                 log only, don't write to Notion

Usage:
    python sync.py          # one run
    python sync.py --loop   # repeat every RUN_INTERVAL_HOURS
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:  # optional: load .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("etilbudsavis-notion")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

# NOTION_TOKEN = os.environ["NOTION_TOKEN"]
# NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
# NOTION_NAME_PROPERTY = os.environ.get("NOTION_NAME_PROPERTY", "Name")

NOTION_TOKEN="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"
NOTION_DATABASE_ID="3db8d59fe8068059ba06f10868d6a1f6"
NOTION_NAME_PROPERTY="Name"

NOTION_VERSION = "2022-06-28"
NOTION_API_BASE = "https://api.notion.com/v1"

# TJEK_LAT = float(os.environ.get("TJEK_LAT", "55.6761"))
# TJEK_LNG = float(os.environ.get("TJEK_LNG", "12.5683"))
# TJEK_RADIUS_M = int(os.environ.get("TJEK_RADIUS_M", "25000"))

TJEK_LAT=55.6761
TJEK_LNG=12.5683
TJEK_RADIUS_M=1000

TJEK_STORES = {
    s.strip().lower() for s in os.environ.get("TJEK_STORES", "").split(",") if s.strip()
}
TJEK_API_BASE = os.environ.get("TJEK_API_BASE", "https://squid-api.tjek.com/v2")
# Used only when an offer has no direct link. VERIFY this URL in your browser.
TJEK_SEARCH_URL = os.environ.get("TJEK_SEARCH_URL", "https://etilbudsavis.dk/soeg/{query}")

MAX_OFFERS = int(os.environ.get("MAX_OFFERS", "3"))
RUN_INTERVAL_HOURS = float(os.environ.get("RUN_INTERVAL_HOURS", "6"))
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")
LOCAL_TZ = None  # ZoneInfo("Europe/Copenhagen")

# One session with automatic retry/backoff (handles Notion's 429 rate limit).
session = requests.Session()
session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=None,  # retry POST/PATCH too
        )
    ),
)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Offer:
    heading: str
    store: str
    price: float | None
    currency: str
    run_till: str | None  # YYYY-MM-DD in local time
    link: str | None


@dataclass
class NotionRow:
    page_id: str
    name: str


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _parse_date(value: str | None) -> datetime | None:
    """Parse Tjek timestamps like 2026-10-11T21:59:59+0000 / ...Z / ...+00:00."""
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)  # +0000 -> +00:00
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", text.lower()) if len(t) > 1]


def _is_relevant(term: str, heading: str) -> bool:
    """All words of the search term must appear in the heading (substring match,
    so 'mælk' matches 'letmælk' and 'tomat' matches 'tomater')."""
    head = heading.lower()
    return all(tok in head for tok in _tokens(term))


# --------------------------------------------------------------------------
# Notion side
# --------------------------------------------------------------------------

def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def fetch_ingredient_rows() -> list[NotionRow]:
    rows: list[NotionRow] = []
    url = f"{NOTION_API_BASE}/databases/{NOTION_DATABASE_ID}/query"
    payload: dict = {"page_size": 100}

    while True:
        resp = session.post(url, headers=_notion_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        for page in data.get("results", []):
            title_prop = page["properties"].get(NOTION_NAME_PROPERTY)
            if not title_prop or title_prop["type"] != "title":
                log.warning("Row %s has no '%s' title property, skipping",
                            page["id"], NOTION_NAME_PROPERTY)
                continue
            name = "".join(f["plain_text"] for f in title_prop["title"]).strip()
            if name:
                rows.append(NotionRow(page_id=page["id"], name=name))

        if data.get("has_more"):
            payload["start_cursor"] = data["next_cursor"]
        else:
            break

    log.info("Fetched %d ingredient rows from Notion", len(rows))
    return rows


def _alternatives_rich_text(offers: list[Offer]) -> list[dict]:
    """One line per offer; the line is a clickable link when we have a URL."""
    cheapest = min((o.price for o in offers if o.price is not None), default=None)
    segments: list[dict] = []
    for i, o in enumerate(offers):
        price = f"{o.price:g} {o.currency}" if o.price is not None else "pris ukendt"
        until = f" · til {o.run_till}" if o.run_till else ""
        star = " ★billigst" if cheapest is not None and o.price == cheapest and len(offers) > 1 else ""
        text = f"{i + 1}. {o.heading} – {o.store} – {price}{until}{star}"
        if i < len(offers) - 1:
            text += "\n"
        content: dict = {"content": text[:2000]}
        if o.link:
            content["link"] = {"url": o.link}
        segments.append({"text": content})
    return segments


def update_row_with_offers(page_id: str, offers: list[Offer]) -> None:
    if offers:
        top = offers[0]  # most relevant
        properties = {
            "On Sale": {"checkbox": True},
            "Store": {"rich_text": [{"text": {"content": top.store}}]},
            "Sale Price": {"number": top.price},
            "Sale Ends": {"date": {"start": top.run_till} if top.run_till else None},
            "Product Link": {"url": top.link},
            "Alternatives": {"rich_text": _alternatives_rich_text(offers)},
        }
    else:
        properties = {
            "On Sale": {"checkbox": False},
            "Store": {"rich_text": []},
            "Sale Price": {"number": None},
            "Sale Ends": {"date": None},
            "Product Link": {"url": None},
            "Alternatives": {"rich_text": []},
        }

    if DRY_RUN:
        log.info("DRY_RUN: would update %s", page_id)
        return
    resp = session.patch(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=_notion_headers(),
        json={"properties": properties},
        timeout=30,
    )

    resp.raise_for_status()


# --------------------------------------------------------------------------
# Tjek / eTilbudsavis side (UNOFFICIAL, verify against real traffic)
# --------------------------------------------------------------------------

def _offer_link(item: dict, term: str) -> str | None:
    links = item.get("links") if isinstance(item.get("links"), dict) else {}
    direct = links.get("webshop") or item.get("webshop_url") or item.get("url")
    if direct:
        return direct
    # No direct product page exists for most offers: fall back to a search
    # for the product, which at least lands on the right deals.
    return TJEK_SEARCH_URL.format(query=quote(item.get("heading") or term))


def search_offers(term: str) -> list[Offer]:
    """Return up to MAX_OFFERS relevant, currently valid offers, most relevant first."""
    params = {
        "query": term,
        "r_lat": TJEK_LAT,
        "r_lng": TJEK_LNG,
        "r_radius": TJEK_RADIUS_M,
        "offset": 0,
        "limit": 48,
        "order_by": "-relevance",
    }
    try:
        resp = session.get(f"{TJEK_API_BASE}/offers/search", params=params, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.error("Tjek search failed for %r: %s", term, exc)
        return []

    if isinstance(raw, dict):  # some versions wrap results
        raw = raw.get("offers") or raw.get("results") or []

    now = datetime.now(timezone.utc)
    offers: list[Offer] = []
    seen: set[tuple[str, str]] = set()

    for item in raw:
        try:
            heading = item.get("heading") or term
            store = (
                (item.get("branding") or {}).get("name")
                or (item.get("dealer") or {}).get("name")
                or "Unknown store"
            )
            if TJEK_STORES and store.lower() not in TJEK_STORES:
                continue
            if not _is_relevant(term, heading):
                continue

            start = _parse_date(item.get("run_from"))
            end = _parse_date(item.get("run_till"))
            if (start and start > now) or (end and end < now):
                continue  # not valid right now

            key = (store.lower(), heading.lower())
            if key in seen:
                continue
            seen.add(key)

            pricing = item.get("pricing") or {}
            offers.append(
                Offer(
                    heading=heading,
                    store=store,
                    price=pricing.get("price"),
                    currency=pricing.get("currency", "DKK"),
                    run_till=end.astimezone().date().isoformat() if end else None,
                    link=_offer_link(item, term),
                )
            )
        except Exception:  # noqa: BLE001
            log.exception("Could not parse offer for %r: %r", term, item)

        if len(offers) >= MAX_OFFERS:
            break

    return offers  # API relevance order is preserved


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_once() -> None:
    rows = fetch_ingredient_rows()
    for row in rows:
        try:
            offers = search_offers(row.name)
            if offers:
                log.info("%-25s -> %d offer(s), top: %s @ %s (%s)",
                         row.name, len(offers), offers[0].heading, offers[0].store, offers[0].price)
            else:
                log.debug("no offer for %s", row.name)
            update_row_with_offers(row.page_id, offers)
        except requests.RequestException:
            log.exception("Failed to process %r, continuing", row.name)
        time.sleep(0.4)  # Notion allows ~3 req/s; stay under it


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="repeat every RUN_INTERVAL_HOURS")
    args = parser.parse_args()

    if not args.loop:
        run_once()
        return

    interval = RUN_INTERVAL_HOURS * 3600
    while True:
        started = time.time()
        try:
            run_once()
        except Exception:  # noqa: BLE001
            log.exception("Sync run failed")
        sleep_for = max(interval - (time.time() - started), 60)
        log.info("Sleeping %.1fh", sleep_for / 3600)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
Skyscanner Daily Tracker
------------------------
Tracks: price drops, new destinations, and cheap weekend deals.

Uses the sky-scrapper RapidAPI wrapper of Skyscanner.
Stores history in a local SQLite DB so it can detect *changes* day over day.

Usage:
  python tracker.py                # run a normal daily check
  python tracker.py --init         # build/refresh the DB schema and exit
  python tracker.py --dry-run      # show what it WOULD fetch, no writes

Schedule via cron (example, 8:00 AM every day):
  0 8 * * * /usr/bin/env -S bash -lc 'cd ~/skyscanner-tracker && ./venv/bin/python tracker.py >> tracker.log 2>&1'
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv

# ---------- Config ----------------------------------------------------------

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = "sky-scrapper.p.rapidapi.com"
BASE = f"https://{RAPIDAPI_HOST}"
DB_PATH = ROOT / "history.db"
REPORT_DIR = ROOT / "reports"
REPORT_DIR.mkdir(exist_ok=True)

CONFIG_PATH = ROOT / "config.json"

HEADERS = {
    "x-rapidapi-host": RAPIDAPI_HOST,
    "x-rapidapi-key": RAPIDAPI_KEY,
    "Content-Type": "application/json",
}

# Price-drop alert threshold (percent)
PRICE_DROP_PCT = 10.0
# Anything cheaper than this for a weekend is flagged
WEEKEND_DEAL_USD = 400


# ---------- Helpers ---------------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    print(f"[tracker] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def api_get(path: str, params: dict[str, Any], *, retries: int = 3) -> dict[str, Any]:
    url = f"{BASE}{path}"
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if r.status_code == 429:
                wait = 2 ** attempt
                log(f"rate limited, sleeping {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries:
                raise
            log(f"request failed ({e}), retrying...")
            time.sleep(2 ** attempt)
    return {}


# ---------- DB --------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS price_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at   TEXT NOT NULL,
    origin        TEXT NOT NULL,
    destination   TEXT NOT NULL,
    depart_date   TEXT,
    return_date   TEXT,
    price_usd     REAL NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'USD',
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_route_date
  ON price_snapshots(origin, destination, depart_date, return_date);

CREATE TABLE IF NOT EXISTS seen_destinations (
    origin        TEXT NOT NULL,
    destination   TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (origin, destination)
);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(DDL)
    log(f"DB initialized at {DB_PATH}")


# ---------- Config ----------------------------------------------------------

DEFAULT_CONFIG = {
    "origins": ["JFK", "LGA", "EWR"],
    "trip_lengths_days": [3, 5, 7],
    "look_ahead_days": [14, 30, 60, 90],
    "price_drop_pct": PRICE_DROP_PCT,
    "weekend_deal_usd": WEEKEND_DEAL_USD
}


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        log(f"wrote default config to {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text())


# ---------- Sky-scrapper calls ---------------------------------------------

def get_airport_entity_id(iata: str) -> str | None:
    """Resolve an IATA code to a Skyscanner entityId (skyId)."""
    data = api_get(
        "/api/v1/flights/searchAirport",
        {"query": iata, "locale": "en-US"},
    )
    items = (data or {}).get("data") or []
    for it in items:
        if it.get("skyId", "").upper() == iata.upper():
            return it.get("entityId")
    return items[0].get("entityId") if items else None


def search_everywhere(origin_sky: str, origin_entity: str, depart: str, ret: str) -> list[dict[str, Any]]:
    """Use the 'flights everywhere' endpoint to discover cheap destinations."""
    data = api_get(
        "/api/v1/flights/searchFlightEverywhere",
        {
            "originSkyId": origin_sky,
            "originEntityId": origin_entity,
            "cabinClass": "economy",
            "journeyType": "round-trip",
            "currency": "USD",
            "market": "US",
            "countryCode": "US",
            "date": depart,
            "returnDate": ret,
        },
    )
    return (data or {}).get("data", {}).get("everywhereDestination", {}).get("results", []) or []


# ---------- Core logic ------------------------------------------------------

@dataclass
class Quote:
    origin: str
    destination: str
    depart_date: str
    return_date: str
    price_usd: float
    raw: dict[str, Any]


def weekend_pairs(start: date, end: date) -> Iterable[tuple[str, str]]:
    """Yield (Fri, Sun) pairs between start and end."""
    d = start
    while d <= end:
        if d.weekday() == 4:  # Friday
            yield d.isoformat(), (d + timedelta(days=2)).isoformat()
        d += timedelta(days=1)


def last_price(conn: sqlite3.Connection, origin: str, dest: str, depart: str, ret: str) -> float | None:
    row = conn.execute(
        """SELECT price_usd FROM price_snapshots
           WHERE origin=? AND destination=? AND depart_date=? AND return_date=?
           ORDER BY captured_at DESC LIMIT 1""",
        (origin, dest, depart, ret),
    ).fetchone()
    return float(row["price_usd"]) if row else None


def record_destination(conn: sqlite3.Connection, origin: str, dest: str) -> bool:
    """Returns True if this destination is new for the origin."""
    cur = conn.execute(
        "SELECT 1 FROM seen_destinations WHERE origin=? AND destination=?",
        (origin, dest),
    )
    if cur.fetchone():
        return False
    conn.execute(
        "INSERT INTO seen_destinations(origin, destination, first_seen_at) VALUES (?,?,?)",
        (origin, dest, datetime.utcnow().isoformat()),
    )
    return True


def save_snapshot(conn: sqlite3.Connection, q: Quote) -> None:
    conn.execute(
        """INSERT INTO price_snapshots
           (captured_at, origin, destination, depart_date, return_date, price_usd, currency, raw)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            datetime.utcnow().isoformat(),
            q.origin,
            q.destination,
            q.depart_date,
            q.return_date,
            q.price_usd,
            "USD",
            json.dumps(q.raw)[:5000],
        ),
    )


def parse_result(origin: str, depart: str, ret: str, item: dict[str, Any]) -> Quote | None:
    try:
        dest = (
            item.get("content", {}).get("location", {}).get("skyCode")
            or item.get("content", {}).get("location", {}).get("id")
            or item.get("skyId")
        )
        price = (
            item.get("content", {}).get("flightQuotes", {}).get("cheapest", {}).get("price")
            or item.get("price", {}).get("raw")
            or item.get("price")
        )
        if isinstance(price, str):
            price = float("".join(c for c in price if c.isdigit() or c == "."))
        if not dest or price is None:
            return None
        return Quote(origin, str(dest), depart, ret, float(price), item)
    except Exception:
        return None


# ---------- Report ----------------------------------------------------------

def build_report(
    new_dests: list[Quote],
    price_drops: list[tuple[Quote, float, float]],  # quote, old, pct_drop
    weekend_deals: list[Quote],
) -> str:
    today = date.today().isoformat()
    lines = [f"# Skyscanner Daily Tracker - {today}", ""]

    lines.append("## Price drops")
    if price_drops:
        for q, old, pct in sorted(price_drops, key=lambda x: x[2], reverse=True):
            lines.append(
                f"- {q.origin} to {q.destination} | {q.depart_date} to {q.return_date} | "
                f"${old:.0f} to ${q.price_usd:.0f} ({pct:.1f}% off)"
            )
    else:
        lines.append("_No notable drops today._")
    lines.append("")

    lines.append("## New destinations discovered")
    if new_dests:
        for q in new_dests:
            lines.append(
                f"- {q.origin} to {q.destination} | from ${q.price_usd:.0f} | "
                f"{q.depart_date} to {q.return_date}"
            )
    else:
        lines.append("_No new destinations today._")
    lines.append("")

    lines.append(f"## Cheap weekend deals (under ${WEEKEND_DEAL_USD})")
    if weekend_deals:
        for q in sorted(weekend_deals, key=lambda x: x.price_usd):
            lines.append(
                f"- {q.origin} to {q.destination} | {q.depart_date} to {q.return_date} | ${q.price_usd:.0f}"
            )
    else:
        lines.append("_No weekend deals under threshold today._")
    lines.append("")

    return "\n".join(lines)


# ---------- Main ------------------------------------------------------------

def run(dry_run: bool = False) -> None:
    if not RAPIDAPI_KEY:
        die("RAPIDAPI_KEY missing. Put it in .env (RAPIDAPI_KEY=...).")

    cfg = load_config()
    init_db()

    today = date.today()
    horizon_end = today + timedelta(days=max(cfg["look_ahead_days"]) + 7)

    new_dests: list[Quote] = []
    price_drops: list[tuple[Quote, float, float]] = []
    weekend_deals: list[Quote] = []

    with db() as conn:
        for origin in cfg["origins"]:
            log(f"resolving {origin}...")
            entity = get_airport_entity_id(origin) if not dry_run else "DRYRUN"
            if not entity:
                log(f"  could not resolve {origin}, skipping")
                continue

            # 1) Discover destinations at several look-ahead windows
            for offset in cfg["look_ahead_days"]:
                for trip_len in cfg["trip_lengths_days"]:
                    depart = (today + timedelta(days=offset)).isoformat()
                    ret = (today + timedelta(days=offset + trip_len)).isoformat()
                    log(f"  {origin} | depart {depart} | return {ret}")
                    if dry_run:
                        continue
                    try:
                        results = search_everywhere(origin, entity, depart, ret)
                    except Exception as e:
                        log(f"  search failed: {e}")
                        continue
                    for item in results:
                        q = parse_result(origin, depart, ret, item)
                        if not q:
                            continue
                        is_new = record_destination(conn, q.origin, q.destination)
                        prev = last_price(conn, q.origin, q.destination, q.depart_date, q.return_date)
                        save_snapshot(conn, q)
                        if is_new:
                            new_dests.append(q)
                        if prev and prev > 0:
                            pct = (prev - q.price_usd) / prev * 100
                            if pct >= cfg["price_drop_pct"]:
                                price_drops.append((q, prev, pct))
                    time.sleep(0.5)  # be gentle on the API

            # 2) Weekend-deal scan (Fri to Sun within the next ~10 weeks)
            log(f"  weekend scan for {origin}")
            for depart, ret in weekend_pairs(today + timedelta(days=3), today + timedelta(days=70)):
                if dry_run:
                    continue
                try:
                    results = search_everywhere(origin, entity, depart, ret)
                except Exception as e:
                    log(f"  weekend search failed: {e}")
                    continue
                for item in results:
                    q = parse_result(origin, depart, ret, item)
                    if not q:
                        continue
                    save_snapshot(conn, q)
                    if q.price_usd <= cfg["weekend_deal_usd"]:
                        weekend_deals.append(q)
                time.sleep(0.5)

        conn.commit()

    report = build_report(new_dests, price_drops, weekend_deals)
    report_path = REPORT_DIR / f"{today.isoformat()}.md"
    report_path.write_text(report)
    log(f"report written to {report_path}")
    print("\n" + report)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="create DB and exit")
    ap.add_argument("--dry-run", action="store_true", help="plan calls without hitting the API")
    args = ap.parse_args()

    if args.init:
        init_db()
        return
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

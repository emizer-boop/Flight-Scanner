#!/usr/bin/env python3
# Flight price tracker using "Flights Scraper Data" (Google Flights) on RapidAPI.
# - Pulls a price graph per configured route
# - Compares each date's price vs. what we last saw -> flags price drops
# - Flags cheap weekend deals under a configurable cap

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = "flights-scraper-data.p.rapidapi.com"
BASE = "https://" + RAPIDAPI_HOST

DB_PATH = ROOT / "history.db"
REPORT_DIR = ROOT / "reports"
REPORT_DIR.mkdir(exist_ok=True)
CONFIG_PATH = ROOT / "config.json"

HEADERS = {
    "x-rapidapi-host": RAPIDAPI_HOST,
    "x-rapidapi-key": RAPIDAPI_KEY,
    "Content-Type": "application/json",
}


def log(msg: str) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    print("[" + stamp + "] " + msg, flush=True)


def die(msg: str) -> None:
    print("[tracker] ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def api_get(path: str, params: dict, retries: int = 3) -> dict:
    url = BASE + path
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if r.status_code == 429:
                wait = 2 ** attempt
                log("rate limited, sleeping " + str(wait) + "s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries:
                raise
            log("request failed (" + str(e) + "), retrying")
            time.sleep(2 ** attempt)
    return {}


DDL = (
    "CREATE TABLE IF NOT EXISTS price_snapshots ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  captured_at TEXT NOT NULL,"
    "  origin TEXT NOT NULL,"
    "  destination TEXT NOT NULL,"
    "  depart_date TEXT NOT NULL,"
    "  return_date TEXT,"
    "  price_usd REAL NOT NULL"
    ");"
    "CREATE INDEX IF NOT EXISTS idx_route_date "
    "ON price_snapshots(origin, destination, depart_date, return_date);"
)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(DDL)
    log("DB initialized at " + str(DB_PATH))


DEFAULT_CONFIG = {
    "routes": [
        {"origin": "JFK", "destination": "LAX"},
        {"origin": "JFK", "destination": "LHR"},
        {"origin": "JFK", "destination": "CDG"},
        {"origin": "JFK", "destination": "BCN"},
        {"origin": "JFK", "destination": "MEX"},
        {"origin": "LGA", "destination": "MIA"},
        {"origin": "EWR", "destination": "LAS"}
    ],
    "trip_length_days": 7,
    "price_drop_pct": 10.0,
    "weekend_deal_usd": 400
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        log("wrote default config to " + str(CONFIG_PATH))
    return json.loads(CONFIG_PATH.read_text())


def get_price_graph(origin: str, dest: str, trip_length_days: int) -> list:
    # Endpoint: GET /price-graph/for-roundtrip?departureId=JFK&arrivalId=LHR&tripLength=7
    params = {
        "departureId": origin,
        "arrivalId": dest,
        "tripLength": trip_length_days,
        "currency": "USD",
    }
    try:
        data = api_get("/price-graph/for-roundtrip", params)
    except Exception as e:
        log("  graph request failed: " + str(e))
        return []
    # The API has changed shapes before; try common paths
    candidates = []
    if isinstance(data, dict):
        for key in ("data", "prices", "graph", "results"):
            v = data.get(key)
            if isinstance(v, list):
                candidates = v
                break
            if isinstance(v, dict):
                for k2 in ("prices", "graph", "items", "data"):
                    if isinstance(v.get(k2), list):
                        candidates = v[k2]
                        break
                if candidates:
                    break
    out = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        depart = (
            item.get("departureDate")
            or item.get("date")
            or item.get("outboundDate")
            or item.get("start")
            or item.get("departure_date")
        )
        ret = (
            item.get("returnDate")
            or item.get("inboundDate")
            or item.get("end")
            or item.get("return_date")
        )
        price = (
            item.get("price")
            or item.get("amount")
            or item.get("value")
        )
        if isinstance(price, dict):
            price = price.get("amount") or price.get("raw") or price.get("value")
        if isinstance(price, str):
            digits = "".join(c for c in price if c.isdigit() or c == ".")
            price = float(digits) if digits else None
        if not depart or price is None:
            continue
        try:
            p = float(price)
        except (TypeError, ValueError):
            continue
        out.append((str(depart)[:10], str(ret)[:10] if ret else None, p))
    return out


def last_price(conn, origin, dest, depart, ret):
    row = conn.execute(
        "SELECT price_usd FROM price_snapshots "
        "WHERE origin=? AND destination=? AND depart_date=? "
        "  AND ((return_date IS NULL AND ? IS NULL) OR return_date=?) "
        "ORDER BY captured_at DESC LIMIT 1",
        (origin, dest, depart, ret, ret),
    ).fetchone()
    return float(row["price_usd"]) if row else None


def save_snapshot(conn, origin, dest, depart, ret, price):
    conn.execute(
        "INSERT INTO price_snapshots(captured_at, origin, destination, "
        "depart_date, return_date, price_usd) VALUES (?,?,?,?,?,?)",
        (datetime.utcnow().isoformat(), origin, dest, depart, ret, price),
    )


def is_weekend_trip(depart: str, ret: str) -> bool:
    try:
        d = datetime.fromisoformat(depart).date()
        r = datetime.fromisoformat(ret).date() if ret else None
    except Exception:
        return False
    if r is None:
        return False
    return d.weekday() == 4 and (r - d).days in (2, 3)


def build_report(price_drops, weekend_deals, cheapest_per_route, cfg) -> str:
    today = date.today().isoformat()
    lines = ["# Daily Flight Tracker - " + today, ""]

    lines.append("## Price drops")
    if price_drops:
        for o, d, dep, ret, prev, now, pct in sorted(price_drops, key=lambda x: x[6], reverse=True):
            tail = (" return " + ret) if ret else ""
            lines.append("- " + o + " to " + d + " | depart " + dep + tail +
                         " | $" + str(int(round(prev))) + " -> $" + str(int(round(now))) +
                         " (" + str(round(pct, 1)) + "% off)")
    else:
        lines.append("_No notable drops today._")
    lines.append("")

    cap = int(cfg["weekend_deal_usd"])
    lines.append("## Cheap weekend deals (under $" + str(cap) + ")")
    if weekend_deals:
        for o, d, dep, ret, price in sorted(weekend_deals, key=lambda x: x[4]):
            lines.append("- " + o + " to " + d + " | " + dep + " to " + ret + " | $" + str(int(round(price))))
    else:
        lines.append("_No weekend deals under threshold today._")
    lines.append("")

    lines.append("## Cheapest date per route")
    if cheapest_per_route:
        for o, d, dep, ret, price in sorted(cheapest_per_route, key=lambda x: x[4]):
            tail = (" to " + ret) if ret else ""
            lines.append("- " + o + " to " + d + " | best: " + dep + tail + " | $" + str(int(round(price))))
    else:
        lines.append("_No prices returned today._")
    lines.append("")

    return "\n".join(lines)


def run():
    if not RAPIDAPI_KEY:
        die("RAPIDAPI_KEY missing. Set as GitHub Actions secret or in .env")
    cfg = load_config()
    init_db()

    trip_len = int(cfg.get("trip_length_days", 7))
    drop_pct = float(cfg.get("price_drop_pct", 10.0))
    weekend_cap = float(cfg.get("weekend_deal_usd", 400))

    price_drops = []
    weekend_deals = []
    cheapest_per_route = []

    with db() as conn:
        for route in cfg.get("routes", []):
            o, d = route["origin"], route["destination"]
            log("price graph " + o + " -> " + d + " (trip " + str(trip_len) + "d)")
            graph = get_price_graph(o, d, trip_len)
            if not graph:
                log("  no data")
                time.sleep(0.5)
                continue

            best = None
            for depart, ret, price in graph:
                prev = last_price(conn, o, d, depart, ret)
                save_snapshot(conn, o, d, depart, ret, price)
                if best is None or price < best[2]:
                    best = (depart, ret, price)
                if prev and prev > 0:
                    pct = (prev - price) / prev * 100
                    if pct >= drop_pct:
                        price_drops.append((o, d, depart, ret, prev, price, pct))
                if ret and is_weekend_trip(depart, ret) and price <= weekend_cap:
                    weekend_deals.append((o, d, depart, ret, price))

            if best:
                cheapest_per_route.append((o, d, best[0], best[1], best[2]))

            time.sleep(1.0)

        conn.commit()

    report = build_report(price_drops, weekend_deals, cheapest_per_route, cfg)
    out = REPORT_DIR / (date.today().isoformat() + ".md")
    out.write_text(report)
    log("report written to " + str(out))
    print("\n" + report)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    args = ap.parse_args()
    if args.init:
        init_db()
        return
    run()


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
# Flight price tracker using "Flights Scraper Data" (Google Flights) on RapidAPI.
# - Pulls a price graph per configured route
# - Compares each date's price vs. what we last saw -> flags price drops
# - Flags cheap weekend deals under a configurable cap

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = "flights-scraper-data.p.rapidapi.com"
BASE = "https://" + RAPIDAPI_HOST

DB_PATH = ROOT / "history.db"
REPORT_DIR = ROOT / "reports"
REPORT_DIR.mkdir(exist_ok=True)
CONFIG_PATH = ROOT / "config.json"

HEADERS = {
    "x-rapidapi-host": RAPIDAPI_HOST,
    "x-rapidapi-key": RAPIDAPI_KEY,
    "Content-Type": "application/json",
}


def log(msg: str) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    print("[" + stamp + "] " + msg, flush=True)


def die(msg: str) -> None:
    print("[tracker] ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def api_get(path: str, params: dict, retries: int = 3) -> dict:
    url = BASE + path
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if r.status_code == 429:
                wait = 2 ** attempt
                log("rate limited, sleeping " + str(wait) + "s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries:
                raise
            log("request failed (" + str(e) + "), retrying")
            time.sleep(2 ** attempt)
    return {}


DDL = (
    "CREATE TABLE IF NOT EXISTS price_snapshots ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  captured_at TEXT NOT NULL,"
    "  origin TEXT NOT NULL,"
    "  destination TEXT NOT NULL,"
    "  depart_date TEXT NOT NULL,"
    "  return_date TEXT,"
    "  price_usd REAL NOT NULL"
    ");"
    "CREATE INDEX IF NOT EXISTS idx_route_date "
    "ON price_snapshots(origin, destination, depart_date, return_date);"
)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(DDL)
    log("DB initialized at " + str(DB_PATH))


DEFAULT_CONFIG = {
    "routes": [
        {"origin": "JFK", "destination": "LAX"},
        {"origin": "JFK", "destination": "LHR"},
        {"origin": "JFK", "destination": "CDG"},
        {"origin": "JFK", "destination": "BCN"},
        {"origin": "JFK", "destination": "MEX"},
        {"origin": "LGA", "destination": "MIA"},
        {"origin": "EWR", "destination": "LAS"}
    ],
    "trip_length_days": 7,
    "price_drop_pct": 10.0,
    "weekend_deal_usd": 400
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        log("wrote default config to " + str(CONFIG_PATH))
    return json.loads(CONFIG_PATH.read_text())


def get_price_graph(origin: str, dest: str, trip_length_days: int) -> list:
    # Endpoint: GET /price-graph/for-roundtrip?departureId=JFK&arrivalId=LHR&tripLength=7
    params = {
        "departureId": origin,
        "arrivalId": dest,
        "tripLength": trip_length_days,
        "currency": "USD",
    }
    try:
        data = api_get("/price-graph/for-roundtrip", params)
    except Exception as e:
        log("  graph request failed: " + str(e))
        return []
    # The API has changed shapes before; try common paths
    candidates = []
    if isinstance(data, dict):
        for key in ("data", "prices", "graph", "results"):
            v = data.get(key)
            if isinstance(v, list):
                candidates = v
                break
            if isinstance(v, dict):
                for k2 in ("prices", "graph", "items", "data"):
                    if isinstance(v.get(k2), list):
                        candidates = v[k2]
                        break
                if candidates:
                    break
    out = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        depart = (
            item.get("departureDate")
            or item.get("date")
            or item.get("outboundDate")
            or item.get("start")
            or item.get("departure_date")
        )
        ret = (
            item.get("returnDate")
            or item.get("inboundDate")
            or item.get("end")
            or item.get("return_date")
        )
        price = (
            item.get("price")
            or item.get("amount")
            or item.get("value")
        )
        if isinstance(price, dict):
            price = price.get("amount") or price.get("raw") or price.get("value")
        if isinstance(price, str):
            digits = "".join(c for c in price if c.isdigit() or c == ".")
            price = float(digits) if digits else None
        if not depart or price is None:
            continue
        try:
            p = float(price)
        except (TypeError, ValueError):
            continue
        out.append((str(depart)[:10], str(ret)[:10] if ret else None, p))
    return out


def last_price(conn, origin, dest, depart, ret):
    row = conn.execute(
        "SELECT price_usd FROM price_snapshots "
        "WHERE origin=? AND destination=? AND depart_date=? "
        "  AND ((return_date IS NULL AND ? IS NULL) OR return_date=?) "
        "ORDER BY captured_at DESC LIMIT 1",
        (origin, dest, depart, ret, ret),
    ).fetchone()
    return float(row["price_usd"]) if row else None


def save_snapshot(conn, origin, dest, depart, ret, price):
    conn.execute(
        "INSERT INTO price_snapshots(captured_at, origin, destination, "
        "depart_date, return_date, price_usd) VALUES (?,?,?,?,?,?)",
        (datetime.utcnow().isoformat(), origin, dest, depart, ret, price),
    )


def is_weekend_trip(depart: str, ret: str) -> bool:
    try:
        d = datetime.fromisoformat(depart).date()
        r = datetime.fromisoformat(ret).date() if ret else None
    except Exception:
        return False
    if r is None:
        return False
    return d.weekday() == 4 and (r - d).days in (2, 3)


def build_report(price_drops, weekend_deals, cheapest_per_route, cfg) -> str:
    today = date.today().isoformat()
    lines = ["# Daily Flight Tracker - " + today, ""]

    lines.append("## Price drops")
    if price_drops:
        for o, d, dep, ret, prev, now, pct in sorted(price_drops, key=lambda x: x[6], reverse=True):
            tail = (" return " + ret) if ret else ""
            lines.append("- " + o + " to " + d + " | depart " + dep + tail +
                         " | $" + str(int(round(prev))) + " -> $" + str(int(round(now))) +
                         " (" + str(round(pct, 1)) + "% off)")
    else:
        lines.append("_No notable drops today._")
    lines.append("")

    cap = int(cfg["weekend_deal_usd"])
    lines.append("## Cheap weekend deals (under $" + str(cap) + ")")
    if weekend_deals:
        for o, d, dep, ret, price in sorted(weekend_deals, key=lambda x: x[4]):
            lines.append("- " + o + " to " + d + " | " + dep + " to " + ret + " | $" + str(int(round(price))))
    else:
        lines.append("_No weekend deals under threshold today._")
    lines.append("")

    lines.append("## Cheapest date per route")
    if cheapest_per_route:
        for o, d, dep, ret, price in sorted(cheapest_per_route, key=lambda x: x[4]):
            tail = (" to " + ret) if ret else ""
            lines.append("- " + o + " to " + d + " | best: " + dep + tail + " | $" + str(int(round(price))))
    else:
        lines.append("_No prices returned today._")
    lines.append("")

    return "\n".join(lines)


def run():
    if not RAPIDAPI_KEY:
        die("RAPIDAPI_KEY missing. Set as GitHub Actions secret or in .env")
    cfg = load_config()
    init_db()

    trip_len = int(cfg.get("trip_length_days", 7))
    drop_pct = float(cfg.get("price_drop_pct", 10.0))
    weekend_cap = float(cfg.get("weekend_deal_usd", 400))

    price_drops = []
    weekend_deals = []
    cheapest_per_route = []

    with db() as conn:
        for route in cfg.get("routes", []):
            o, d = route["origin"], route["destination"]
            log("price graph " + o + " -> " + d + " (trip " + str(trip_len) + "d)")
            graph = get_price_graph(o, d, trip_len)
            if not graph:
                log("  no data")
                time.sleep(0.5)
                continue

            best = None
            for depart, ret, price in graph:
                prev = last_price(conn, o, d, depart, ret)
                save_snapshot(conn, o, d, depart, ret, price)
                if best is None or price < best[2]:
                    best = (depart, ret, price)
                if prev and prev > 0:
                    pct = (prev - price) / prev * 100
                    if pct >= drop_pct:
                        price_drops.append((o, d, depart, ret, prev, price, pct))
                if ret and is_weekend_trip(depart, ret) and price <= weekend_cap:
                    weekend_deals.append((o, d, depart, ret, price))

            if best:
                cheapest_per_route.append((o, d, best[0], best[1], best[2]))

            time.sleep(1.0)

        conn.commit()

    report = build_report(price_drops, weekend_deals, cheapest_per_route, cfg)
    out = REPORT_DIR / (date.today().isoformat() + ".md")
    out.write_text(report)
    log("report written to " + str(out))
    print("\n" + report)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    args = ap.parse_args()
    if args.init:
        init_db()
        return
    run()


if __name__ == "__main__":
    main()

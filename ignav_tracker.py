"""Ignav flight-price tracker: a second tracker that runs alongside tracker.py.

Mirrors the SerpApi tracker's role (persist live fares, enrich the cheapest
results with verified detail and a booking link) but sources data from the
Ignav API (https://ignav.com) into a separate SQLite database. Uses only the
standard library.

Fare coverage is complete: for every origin/destination/stay-length it queries
Ignav's cheapest-dates endpoint, which returns a price for every departure day
in the window. The cheapest observations from each run are then enriched with a
round-trip search (verified price, airline, stops) and a booking-link lookup.

Requires IGNAT_API_KEY in the environment. Does not read or write the SerpApi
database (data/prices.db).
"""
import json
import os
import sqlite3
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

from tracker import (
    ORIGINS,
    DEPART_FROM,
    DEPART_TO,
    MIN_NIGHTS,
    MAX_NIGHTS,
    CURRENCY,
)

BASE_URL = "https://ignav.com/api"
DB_PATH = Path(os.environ.get("IGNAT_DB_PATH", Path(__file__).parent / "data" / "ignav.db"))

# Ignav needs explicit destination airport codes (it has no "country" filter).
DESTINATIONS = tuple(
    d.strip().upper() for d in os.environ.get(
        "IGNAT_DESTINATIONS", "HND,NRT,KIX,NGO,FUK,OKA"
    ).split(",") if d.strip()
)
DEPART_FROM = date.fromisoformat(
    os.environ.get("IGNAT_DEPART_FROM", DEPART_FROM.isoformat())
)
DEPART_TO = date.fromisoformat(
    os.environ.get("IGNAT_DEPART_TO", DEPART_TO.isoformat())
)
STAY_MIN = int(os.environ.get("IGNAT_MIN_NIGHTS", MIN_NIGHTS))
STAY_MAX = int(os.environ.get("IGNAT_MAX_NIGHTS", MAX_NIGHTS))
ENRICH_COUNT = int(os.environ.get("IGNAT_ENRICH_COUNT", 5))
MAX_STOPS = int(os.environ.get("IGNAT_MAX_STOPS", 2))
MARKET = os.environ.get("IGNAT_MARKET", "US")
ADULTS = int(os.environ.get("IGNAT_ADULTS", 1))
# Reject "bookable" fares this far below the calendar fare: extreme lowball fares
# from third-party OTAs (e.g. HolidayBreakz) are bait pricing, not real deals.
MAX_BOOKABLE_DISCOUNT = float(os.environ.get("IGNAT_MAX_BOOKABLE_DISCOUNT", 0.15))
# Also drop "bookable" fares this far above the calendar fare: the calendar price is
# then the better deal signal, so the row stays calendar-only.
MAX_BOOKABLE_PREMIUM = float(os.environ.get("IGNAT_MAX_BOOKABLE_PREMIUM", 0.25))

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    nights INTEGER NOT NULL,
    price REAL,
    currency TEXT,
    price_status TEXT,
    verified_price REAL,
    verified_airline TEXT,
    verified_stops INTEGER,
    verified_cabin_class TEXT,
    verified_duration_minutes INTEGER,
    ignav_id TEXT,
    booking_link TEXT,
    booking_provider TEXT,
    booking_provider_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_ignav_itin ON observations (origin, dest, start_date, end_date);
CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY,
    scan_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    raw_results INTEGER NOT NULL DEFAULT 0,
    valid_results INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
"""


def post(path: str, body: dict) -> dict:
    """POST JSON to the Ignav API and return the parsed response."""
    key = os.environ.get("IGNAT_API_KEY")
    if not key:
        sys.exit("Set IGNAT_API_KEY in the environment before running.")
    request = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


MIGRATIONS = {
    "observations": {
        "booking_provider": "TEXT",
        "booking_provider_type": "TEXT",
    },
}


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, kind in columns.items():
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    db.commit()


def cheapest_dates(origin: str, dest: str, stay_days: int) -> list[dict]:
    data = post("/fares/cheapest-dates", {
        "origin": origin,
        "destination": dest,
        "departure_date_range": {
            "start": DEPART_FROM.isoformat(),
            "end": DEPART_TO.isoformat(),
        },
        "stay_days": stay_days,
        "adults": ADULTS,
        "max_stops": MAX_STOPS,
        "market": MARKET,
    })
    return data.get("dates", [])


def round_trip(origin: str, dest: str, depart: str, ret: str) -> list[dict]:
    data = post("/fares/round-trip", {
        "origin": origin,
        "destination": dest,
        "departure_date": depart,
        "return_date": ret,
        "adults": ADULTS,
        "max_stops": MAX_STOPS,
        "market": MARKET,
    })
    return data.get("itineraries", [])


def booking_links(ignav_id: str) -> list[dict]:
    data = post("/fares/booking-links", {"ignav_id": ignav_id})
    links = []
    for option in data.get("booking_options", []):
        for link in option.get("links", []):
            links.append(link)
    return links


def choose_booking_link(links: list[dict]) -> tuple[str | None, str | None, str | None]:
    """Prefer an airline-direct link over third-party OTAs."""
    if not links:
        return None, None, None
    for link in links:
        if link.get("provider_type") == "airline":
            return link.get("url"), link.get("provider_name"), link.get("provider_type")
    link = links[0]
    return link.get("url"), link.get("provider_name"), link.get("provider_type")


def bookable_discount(calendar_price: float, verified_price: float) -> float:
    """Fractional drop of the bookable fare below the calendar fare (0 if none)."""
    if calendar_price:
        return max(0.0, (calendar_price - verified_price) / calendar_price)
    return 0.0


def bookable_premium(calendar_price: float, verified_price: float) -> float:
    """Fractional excess of the bookable fare above the calendar fare (0 if none)."""
    if calendar_price:
        return max(0.0, (verified_price - calendar_price) / calendar_price)
    return 0.0


def bookable_within_band(calendar_price: float, verified_price: float) -> bool:
    """True when a bookable fare is close enough to the calendar fare to trust."""
    return (bookable_discount(calendar_price, verified_price) <= MAX_BOOKABLE_DISCOUNT
            and bookable_premium(calendar_price, verified_price) <= MAX_BOOKABLE_PREMIUM)


def summarize(itinerary: dict) -> dict:
    """Flatten an Ignav itinerary into the fields the tracker stores."""
    price = itinerary.get("price") or {}
    legs = itinerary.get("legs") or [itinerary]
    outbound = itinerary.get("outbound") or {}
    inbound = itinerary.get("inbound") or {}

    segments = []
    for leg in (outbound, inbound):
        segments.extend(leg.get("segments", []) or [])
    for leg in legs:
        if isinstance(leg, dict):
            segments.extend(leg.get("segments", []) or [])

    carriers = []
    for segment in segments:
        name = segment.get("operating_carrier_name") or segment.get("marketing_carrier_code")
        if name and name not in carriers:
            carriers.append(name)

    def leg_stops(leg: dict) -> int:
        return max(0, len(leg.get("segments", []) or []) - 1)

    duration = (outbound.get("duration_minutes") or 0) + (inbound.get("duration_minutes") or 0)
    return {
        "price": price.get("amount"),
        "currency": price.get("currency") or CURRENCY,
        "status": price.get("status"),
        "airline": ", ".join(carriers) or outbound.get("carrier") or None,
        "stops": leg_stops(outbound) + leg_stops(inbound),
        "cabin_class": itinerary.get("cabin_class"),
        "duration_minutes": duration or None,
        "ignav_id": itinerary.get("ignav_id"),
    }


def parse_calendar(origin: str, dest: str, stay_days: int, dates: list[dict]) -> list[dict]:
    """Turn a cheapest-dates response into observation rows (null prices skipped)."""
    rows = []
    for entry in dates:
        price = entry.get("price") or {}
        amount = price.get("amount")
        if not isinstance(amount, (int, float)):
            continue
        rows.append({
            "origin": origin,
            "dest": dest,
            "start_date": entry.get("departure_date"),
            "end_date": entry.get("return_date"),
            "nights": stay_days,
            "price": amount,
            "currency": price.get("currency") or CURRENCY,
            "price_status": price.get("status"),
        })
    return rows


def fetch_calendar(origin: str, dest: str, stay_days: int) -> tuple[tuple, list[dict] | Exception]:
    try:
        rows = parse_calendar(origin, dest, stay_days, cheapest_dates(origin, dest, stay_days))
        return (origin, dest, stay_days), rows
    except Exception as exc:  # noqa: BLE001 - record and continue with other routes
        return (origin, dest, stay_days), exc


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    init_db(db)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scan_id = now
    tasks = [
        (origin, dest, stay)
        for origin in ORIGINS
        for dest in DESTINATIONS
        for stay in range(STAY_MIN, STAY_MAX + 1)
    ]

    saved = []
    errors: dict[str, int] = {}
    raw_total: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_calendar, *task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            (origin, dest, stay), rows = future.result()
            if isinstance(rows, Exception):
                errors[origin] = errors.get(origin, 0) + 1
                print(f"{origin}->{dest} ({stay}n) failed: {rows}", file=sys.stderr)
                continue
            raw_total[origin] = raw_total.get(origin, 0) + len(rows)
            for row in rows:
                db.execute(
                    """INSERT INTO observations
                       (observed_at, origin, dest, start_date, end_date, nights,
                        price, currency, price_status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (now, row["origin"], row["dest"], row["start_date"], row["end_date"],
                     row["nights"], row["price"], row["currency"], row["price_status"]),
                )
                row["id"] = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                saved.append(row)
    db.commit()

    # Record one scan_run row per origin.
    for origin in ORIGINS:
        status = "error" if errors.get(origin) else "success"
        db.execute(
            """INSERT INTO scan_runs
               (scan_id, observed_at, origin, status, raw_results, valid_results, error)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (scan_id, now, origin, status,
             raw_total.get(origin, 0), len([r for r in saved if r["origin"] == origin]),
             f"{errors.get(origin, 0)} route(s) failed" if errors.get(origin) else None),
        )
    db.commit()

    # Enrich the cheapest observations this run with verified detail + booking links.
    saved.sort(key=lambda row: row["price"] if row["price"] is not None else float("inf"))
    enriched = 0
    for row in saved[:ENRICH_COUNT]:
        try:
            itineraries = round_trip(row["origin"], row["dest"], row["start_date"], row["end_date"])
            if not itineraries:
                continue
            top = itineraries[0]
            summary = summarize(top)
            verified_price = summary["price"]
            if not isinstance(verified_price, (int, float)):
                continue
            discount = bookable_discount(row["price"] or 0, verified_price)
            premium = bookable_premium(row["price"] or 0, verified_price)
            if not bookable_within_band(row["price"] or 0, verified_price):
                if discount > MAX_BOOKABLE_DISCOUNT:
                    print(f"Skipping {row['origin']}->{row['dest']} {row['start_date']}: "
                          f"bookable ${verified_price:,.0f} is {discount:.0%} below the calendar "
                          f"${row['price']:,.0f} (possible OTA bait fare)", file=sys.stderr)
                else:
                    print(f"Skipping {row['origin']}->{row['dest']} {row['start_date']}: "
                          f"bookable ${verified_price:,.0f} is {premium:.0%} above the calendar "
                          f"${row['price']:,.0f} (calendar fare is the better deal)", file=sys.stderr)
                continue
            booking_link, provider, provider_type = None, None, None
            if summary.get("ignav_id"):
                booking_link, provider, provider_type = choose_booking_link(
                    booking_links(summary["ignav_id"])
                )
            db.execute(
                """UPDATE observations SET
                   verified_price=?, verified_airline=?, verified_stops=?,
                   verified_cabin_class=?, verified_duration_minutes=?, ignav_id=?,
                   booking_link=?, booking_provider=?, booking_provider_type=?
                   WHERE id=?""",
                (verified_price, summary["airline"], summary["stops"],
                 summary["cabin_class"], summary["duration_minutes"], summary["ignav_id"],
                 booking_link, provider, provider_type, row["id"]),
            )
            enriched += 1
        except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
            print(f"Enrichment failed for {row['origin']}->{row['dest']} "
                  f"{row['start_date']}: {exc}", file=sys.stderr)
    db.commit()

    count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    print(f"{len(saved)} observations saved this run "
          f"({len(tasks)} route queries); {enriched} enriched with verified detail.")
    print(f"Ignav DB total observations: {count}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

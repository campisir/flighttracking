"""Japan flight deal tracker: SerpApi Google Flights Deals (scout) -> Google Flights (verify) -> SQLite -> ntfy."""
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

SERPAPI_URL = "https://serpapi.com/search.json"

# ---- Search config ----
ORIGINS = ["PNS", "TPA", "MCO"]
DEPART_FROM = date(2027, 3, 20)
DEPART_TO = date(2027, 4, 10)
MIN_NIGHTS, MAX_NIGHTS = 12, 16
ADULTS = 2
MAX_STOPS = 2  # set to 1 to require at most one stop (cheapest Japan fares usually have 2)
CURRENCY = "USD"

# ---- Alert rules (any one triggers) ----
ABS_THRESHOLD = 900            # per-person fare at or under this
DROP_AMOUNT = 100              # drop vs. previous lowest price seen for the same itinerary
DISCOUNT_PCT = 20              # % below Google's typical price
MAX_ALERTS_PER_RUN = 3         # each alert candidate costs one verification search

DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "data" / "prices.db"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    dest_name TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    nights INTEGER NOT NULL,
    price REAL NOT NULL,
    average_price REAL,
    discount_pct REAL,
    stops INTEGER,
    airline TEXT,
    link TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_itin ON observations (origin, dest, start_date, end_date);
CREATE TABLE IF NOT EXISTS alerts (
    origin TEXT, dest TEXT, start_date TEXT, end_date TEXT, price REAL, alerted_at TEXT,
    PRIMARY KEY (origin, dest, start_date, end_date, price)
);
"""


def serpapi(params: dict) -> dict:
    params = {**params, "api_key": os.environ["SERPAPI_KEY"], "currency": CURRENCY, "hl": "en", "gl": "us"}
    r = requests.get(SERPAPI_URL, params=params, timeout=120)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        # Deals returns "Empty results" intermittently and for multi-airport departure_id.
        if "mpty results" in data["error"] or "hasn't returned any results" in data["error"]:
            return {}
        raise RuntimeError(data["error"])
    return data


def fetch_deals() -> list[dict]:
    deals = []
    # Multi-airport departure_id returns empty, so query each origin. Dates must be in the query text:
    # `query` can't be combined with trip_length and it overrides the outbound_date window.
    query = (f"Japan {MIN_NIGHTS} to {MAX_NIGHTS} days departing "
             f"{DEPART_FROM:%B} {DEPART_FROM.day} to {DEPART_TO:%B} {DEPART_TO.day} {DEPART_TO.year}")
    for origin in ORIGINS:
        data = serpapi({
            "engine": "google_flights_deals",
            "departure_id": origin,
            "query": query,
            "travel_class": 1,
            "stops": MAX_STOPS + 1,  # 2 = 1 stop or fewer, 3 = 2 stops or fewer
            "adults": ADULTS,
        })
        deals += parse_deals(data.get("deals", []), origin)
    return deals


def parse_deals(raw: list[dict], origin: str) -> list[dict]:
    deals = []
    for d in raw:
        try:
            start = date.fromisoformat(d["outbound_date"])
            end = date.fromisoformat(d["return_date"])
        except (KeyError, ValueError):
            continue
        nights = (end - start).days
        if (
            d.get("country") == "Japan"
            and DEPART_FROM <= start <= DEPART_TO
            and MIN_NIGHTS <= nights <= MAX_NIGHTS
            and d.get("stops", 0) <= MAX_STOPS
            and d.get("price")
        ):
            price = float(d["price"])
            deals.append({
                "origin": origin,
                "dest": d.get("arrival_airport_code") or "",
                "dest_name": d.get("name"),
                "start_date": d["outbound_date"],
                "end_date": d["return_date"],
                "nights": nights,
                "price": price,
                "average_price": d.get("average_price"),
                "discount_pct": d.get("discount_percentage"),
                "stops": d.get("stops"),
                "airline": d.get("airline"),
                "link": d.get("flight_link"),
            })
    return deals


def verify(deal: dict) -> float | None:
    """Cheapest per-person round trip from a detailed Google Flights search, or None if nothing is bookable."""
    data = serpapi({
        "engine": "google_flights",
        "departure_id": deal["origin"],
        "arrival_id": deal["dest"],
        "outbound_date": deal["start_date"],
        "return_date": deal["end_date"],
        "travel_class": 1,
        "stops": MAX_STOPS + 1,
        "adults": ADULTS,
        "type": 1,
    })
    prices = [f["price"] for f in data.get("best_flights", []) + data.get("other_flights", []) if f.get("price")]
    # google_flights returns the total for all adults
    return float(min(prices)) / ADULTS if prices else None


def alert_reasons(db: sqlite3.Connection, deal: dict, global_low: float | None) -> list[str]:
    reasons = []
    if deal["price"] <= ABS_THRESHOLD:
        reasons.append(f"under ${ABS_THRESHOLD}")
    if deal["discount_pct"] and deal["discount_pct"] >= DISCOUNT_PCT:
        reasons.append(f"{deal['discount_pct']:.0f}% below typical")
    prev = db.execute(
        "SELECT MIN(price) FROM observations WHERE origin=? AND dest=? AND start_date=? AND end_date=?",
        (deal["origin"], deal["dest"], deal["start_date"], deal["end_date"]),
    ).fetchone()[0]
    if prev is not None and prev - deal["price"] >= DROP_AMOUNT:
        reasons.append(f"down ${prev - deal['price']:.0f} from ${prev:.0f}")
    if global_low is not None and deal["price"] < global_low:
        reasons.append("new personal low")
    return reasons


def notify(title: str, body: str, link: str | None) -> None:
    print(f"\n== {title}\n{body}\n{link or ''}")
    topic = os.environ.get("NTFY_TOPIC")
    if DRY_RUN or not topic:
        return
    headers = {"Title": title, "Priority": "high", "Tags": "airplane"}
    if link:
        headers["Click"] = link
    requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, timeout=30).raise_for_status()


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)

    deals = fetch_deals()
    print(f"{len(deals)} matching Japan deals")
    if not deals:
        return 0

    # Must be read before inserting this run's observations.
    global_low = db.execute("SELECT MIN(price) FROM observations").fetchone()[0]

    candidates = []
    for d in deals:
        reasons = alert_reasons(db, d, global_low)
        already = db.execute(
            "SELECT 1 FROM alerts WHERE origin=? AND dest=? AND start_date=? AND end_date=? AND price=?",
            (d["origin"], d["dest"], d["start_date"], d["end_date"], d["price"]),
        ).fetchone()
        if reasons and not already:
            candidates.append((d, reasons))

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cols = ["origin", "dest", "dest_name", "start_date", "end_date", "nights", "price",
            "average_price", "discount_pct", "stops", "airline", "link"]
    db.executemany(
        f"INSERT INTO observations (observed_at, {','.join(cols)}) VALUES (?, {','.join('?' * len(cols))})",
        [(now, *(d[c] for c in cols)) for d in deals],
    )
    db.commit()

    best = min(deals, key=lambda d: d["price"])
    print(f"Best: {best['origin']}->{best['dest']} {best['start_date']}..{best['end_date']} ${best['price']:.0f}")

    candidates.sort(key=lambda c: c[0]["price"])
    for d, reasons in candidates[:MAX_ALERTS_PER_RUN]:
        try:
            verified = verify(d)
        except Exception as e:  # still worth alerting; just unverified
            print(f"verify failed for {d['origin']}->{d['dest']}: {e}", file=sys.stderr)
            verified, note = d["price"], " (unverified)"
        else:
            if verified is None:
                print(f"skip {d['origin']}->{d['dest']} {d['start_date']}: no bookable flights found")
                continue
            note = ""
        body = (
            f"{d['origin']} -> {d['dest']} ({d['dest_name']}), {d['start_date']} to {d['end_date']} ({d['nights']} nights)\n"
            f"Deal ${d['price']:.0f}/person, verified ${verified:.0f}/person{note}\n"
            f"{d['airline'] or 'multiple airlines'}, {d['stops']} stop(s); Google's typical ${d['average_price'] or '?'}\n"
            f"Why: {', '.join(reasons)}"
        )
        notify(f"Japan ${verified:.0f}: {d['origin']}-{d['dest']}", body, d["link"])
        db.execute(
            "INSERT OR IGNORE INTO alerts VALUES (?, ?, ?, ?, ?, ?)",
            (d["origin"], d["dest"], d["start_date"], d["end_date"], d["price"], now),
        )
        db.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())

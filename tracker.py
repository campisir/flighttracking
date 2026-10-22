"""Japan flight deal tracker: SerpApi -> SQLite -> ntfy."""
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

SERPAPI_URL = "https://serpapi.com/search.json"
ORIGINS = ["PNS", "TPA", "MCO", "MIA", "ATL", "IAH"]
JAPAN_HUBS = ("HND", "NRT", "KIX", "NGO", "FUK")
DEPART_FROM = date(2027, 3, 20)
DEPART_TO = date(2027, 4, 10)
MIN_NIGHTS, MAX_NIGHTS = 12, 16
CURRENCY = "USD"
DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "data" / "prices.db"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
PRICE_DROP_AMOUNT = 100

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
CREATE INDEX IF NOT EXISTS idx_scan_runs_time ON scan_runs (observed_at);
CREATE TABLE IF NOT EXISTS api_requests (
    id INTEGER PRIMARY KEY,
    requested_at TEXT NOT NULL,
    purpose TEXT NOT NULL,
    origin TEXT,
    status TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_requests_time ON api_requests (requested_at);
CREATE TABLE IF NOT EXISTS verification_events (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL,
    scout_price REAL,
    verified_price REAL,
    error TEXT
);
"""

MIGRATIONS = {
    "observations": {
        "verified_price": "REAL",
        "verified_airline": "TEXT",
        "verified_stops": "INTEGER",
        "verified_details": "TEXT",
        "verified_link": "TEXT",
        "pto_weekdays": "INTEGER",
        "pto_weekdays_max": "INTEGER",
        "pto_approximate": "INTEGER",
        "deal_score": "REAL",
        "itinerary_type": "TEXT NOT NULL DEFAULT 'round_trip'",
    },
    "alerts": {
        "verified_price": "REAL",
        "reasons": "TEXT",
        "deal_score": "REAL",
        "verified_airline": "TEXT",
        "verified_stops": "INTEGER",
        "verified_link": "TEXT",
    },
}


class BudgetExceeded(RuntimeError):
    """Raised when a SerpApi request is blocked by the configured monthly budget."""


def env_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value < 0:
        raise ValueError(f"{name} must be zero or greater")
    return value


def sanitized_error(error: Exception | str) -> str:
    message = str(error)
    api_key = os.environ.get("SERPAPI_KEY")
    return message.replace(api_key, "[REDACTED]") if api_key else message


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    for table, columns in MIGRATIONS.items():
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, kind in columns.items():
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    db.commit()


def estimate_pto(start: date, end: date) -> dict:
    """Count weekdays over the itinerary dates; flight times/date-line shifts are unknown."""
    days = (end - start).days
    if days < 0:
        raise ValueError("end date must not precede start date")
    weekdays = sum((start.fromordinal(start.toordinal() + offset).weekday() < 5)
                   for offset in range(days + 1))
    return {
        "pto_weekdays": weekdays,
        "pto_weekdays_max": weekdays + 1,
        "pto_approximate": True,
    }


def monthly_usage(db: sqlite3.Connection, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    month = now.astimezone(timezone.utc).strftime("%Y-%m")
    return db.execute(
        "SELECT COUNT(*) FROM api_requests WHERE requested_at LIKE ?", (f"{month}%",)
    ).fetchone()[0]


def serpapi(db: sqlite3.Connection, params: dict, purpose: str, origin: str | None = None) -> dict:
    budget = env_int("SERPAPI_MONTHLY_BUDGET", 250)
    reserve = min(env_int("SERPAPI_VERIFY_RESERVE", 40), budget)
    used = monthly_usage(db)
    if purpose == "scout":
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        scouts = db.execute(
            "SELECT COUNT(*) FROM api_requests WHERE purpose='scout' AND requested_at LIKE ?",
            (f"{month}%",),
        ).fetchone()[0]
        if scouts >= budget - reserve:
            raise BudgetExceeded(
                f"scout request blocked: scout allocation exhausted ({scouts}/{budget - reserve})"
            )
    if used >= budget:
        raise BudgetExceeded(f"{purpose} request blocked: monthly budget exhausted ({used}/{budget})")

    api_key = os.environ["SERPAPI_KEY"]
    requested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = db.execute(
        "INSERT INTO api_requests (requested_at, purpose, origin, status) VALUES (?, ?, ?, 'pending')",
        (requested_at, purpose, origin),
    )
    request_id = cur.lastrowid
    db.commit()
    request_params = {**params, "api_key": api_key, "currency": CURRENCY, "hl": "en", "gl": "us"}
    try:
        response = requests.get(SERPAPI_URL, params=request_params, timeout=120)
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            message = str(data["error"])
            if "empty results" in message.casefold() or "hasn't returned any results" in message.casefold():
                data = {}
            else:
                raise RuntimeError(message)
    except Exception as exc:
        error = sanitized_error(exc)
        db.execute("UPDATE api_requests SET status='error', error=? WHERE id=?", (error, request_id))
        db.commit()
        raise
    db.execute("UPDATE api_requests SET status='success' WHERE id=?", (request_id,))
    db.commit()
    return data


def parse_deals(raw: list[dict], origin: str) -> list[dict]:
    deals = []
    for item in raw:
        try:
            start = date.fromisoformat(item["outbound_date"])
            end = date.fromisoformat(item["return_date"])
            price = float(item["price"])
            stops = int(item["stops"]) if item.get("stops") is not None else None
        except (KeyError, TypeError, ValueError):
            continue
        nights = (end - start).days
        if (
            item.get("country") == "Japan"
            and DEPART_FROM <= start <= DEPART_TO
            and MIN_NIGHTS <= nights <= MAX_NIGHTS
            and (stops is None or 0 <= stops <= 2)
            and price > 0
        ):
            average = item.get("average_price")
            try:
                average = float(average) if average is not None else None
            except (TypeError, ValueError):
                average = None
            discount = item.get("discount_percentage")
            try:
                discount = float(discount) if discount is not None else None
            except (TypeError, ValueError):
                discount = None
            deals.append({
                "origin": origin,
                "dest": item.get("arrival_airport_code") or "",
                "dest_name": item.get("name"),
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "nights": nights,
                "price": price,
                "average_price": average,
                "discount_pct": discount,
                "stops": stops,
                "airline": item.get("airline"),
                "link": item.get("flight_link"),
                "itinerary_type": "round_trip",
            })
    return deals


def fetch_origin_deals(db: sqlite3.Connection, origin: str) -> tuple[list[dict], str, str | None, int]:
    query = (f"Japan {MIN_NIGHTS} to {MAX_NIGHTS} days departing "
             f"{DEPART_FROM:%B} {DEPART_FROM.day} to {DEPART_TO:%B} {DEPART_TO.day} {DEPART_TO.year}")
    scan_id = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        data = serpapi(db, {
            "engine": "google_flights_deals",
            "departure_id": origin,
            "query": query,
            "travel_class": 1,
            # SerpApi's request filter values are 1=nonstop, 2=<=1 stop, 3=<=2 stops.
            "stops": 3,
            "adults": 1,
        }, "scout", origin)
    except BudgetExceeded as exc:
        db.execute(
            "INSERT INTO scan_runs (scan_id, observed_at, origin, status, error) VALUES (?, ?, ?, 'budget_skipped', ?)",
            (scan_id, scan_id, origin, str(exc)),
        )
        db.commit()
        print(f"{origin}: {exc}", file=sys.stderr)
        return [], "budget_skipped", str(exc), 0
    except Exception as exc:
        error = sanitized_error(exc)
        db.execute(
            "INSERT INTO scan_runs (scan_id, observed_at, origin, status, error) VALUES (?, ?, ?, 'error', ?)",
            (scan_id, scan_id, origin, error),
        )
        db.commit()
        print(f"{origin} scout failed: {error}", file=sys.stderr)
        return [], "error", error, 0

    raw = data.get("deals", [])
    if not isinstance(raw, list):
        raw = []
    deals = parse_deals(raw, origin)
    db.execute(
        """INSERT INTO scan_runs
           (scan_id, observed_at, origin, status, raw_results, valid_results)
           VALUES (?, ?, ?, 'success', ?, ?)""",
        (scan_id, scan_id, origin, len(raw), len(deals)),
    )
    db.commit()
    return deals, "success", None, len(raw)


def itinerary_history(db: sqlite3.Connection, deal: dict) -> tuple[float | None, float | None]:
    row = db.execute(
        """SELECT price FROM observations
           WHERE origin=? AND dest=? AND start_date=? AND end_date=?
           ORDER BY observed_at DESC, id DESC LIMIT 1""",
        (deal["origin"], deal["dest"], deal["start_date"], deal["end_date"]),
    ).fetchone()
    low = db.execute(
        """SELECT MIN(price) FROM observations
           WHERE origin=? AND dest=? AND start_date=? AND end_date=?""",
        (deal["origin"], deal["dest"], deal["start_date"], deal["end_date"]),
    ).fetchone()[0]
    return (row[0] if row else None, low)


def alert_reasons(deal: dict, price: float, previous: float | None,
                  historical_low: float | None) -> tuple[list[str], bool, float, bool]:
    threshold = 900 if deal["origin"] == "PNS" else 850
    average = deal.get("average_price")
    discount = ((average - price) / average * 100) if average and average > 0 else None
    if discount is None and price == deal["price"]:
        discount = deal.get("discount_pct")
    drop = previous - price if previous is not None else 0
    new_low = historical_low is None or price < historical_low
    max_premium = env_int("EXCEPTIONAL_PRICE_PREMIUM", 75)
    drop_threshold = env_int("PRICE_DROP_AMOUNT", PRICE_DROP_AMOUNT)
    exceptional = price <= threshold + max_premium and (
        drop >= drop_threshold or (discount is not None and discount >= 30)
    )
    qualifies = price <= threshold or exceptional
    reasons = []
    if price <= threshold:
        reasons.append(f"at or below the {deal['origin']} target of ${threshold}/person")
    if previous is not None and drop >= drop_threshold:
        reasons.append(f"down ${drop:.0f} from the previous ${previous:.0f} observation")
    if discount is not None and discount >= 20:
        reasons.append(f"{discount:.0f}% below typical")
    if new_low:
        reasons.append("new itinerary historical low")
    if price > threshold and exceptional:
        reasons.append("exceptional deal slightly above the airport target")
    preferred_return = date.fromisoformat(os.environ.get("PREFERRED_RETURN_BY", "2027-04-15"))
    if date.fromisoformat(deal["end_date"]) > preferred_return:
        reasons.append(f"returns after preferred date {preferred_return.isoformat()}")
    return reasons, qualifies, discount or 0, new_low


def deal_score(deal: dict, price: float, previous: float | None,
               discount: float, new_low: bool, stops: int | None = None) -> float:
    threshold = 900 if deal["origin"] == "PNS" else 850
    pto = estimate_pto(date.fromisoformat(deal["start_date"]), date.fromisoformat(deal["end_date"]))
    pto_budget = env_int("PTO_MAX_WEEKDAYS", 10)
    stops = deal.get("stops") if stops is None else stops
    stop_penalty = env_int("TWO_STOP_PENALTY", 15)
    score = (threshold - price) / 5
    score += {0: 20, 1: 10, 2: -stop_penalty}.get(stops, -stop_penalty)
    score += min(discount, 30) * 0.4
    if previous is not None:
        score += min(max(previous - price, 0), 300) * 0.04
    score += 3 if new_low else 0
    conservative_pto = pto["pto_weekdays_max"]
    score += max(0, pto_budget - conservative_pto) * 0.5
    score -= max(0, conservative_pto - pto_budget) * 4
    preferred_return = date.fromisoformat(os.environ.get("PREFERRED_RETURN_BY", "2027-04-15"))
    if date.fromisoformat(deal["end_date"]) > preferred_return:
        score -= env_int("LATE_RETURN_PENALTY", 10)
    return score


def result_link(data: dict) -> str | None:
    metadata = data.get("search_metadata") or {}
    link = metadata.get("google_flights_url") or data.get("google_flights_url")
    return link if isinstance(link, str) and link.startswith("https://") else None


def summarize_flight(result: dict, origin: str, destination: str) -> dict:
    segments = result.get("flights") or []
    airlines = list(dict.fromkeys(
        str(segment.get("airline")) for segment in segments if segment.get("airline")
    ))
    details = []
    for segment in segments:
        dep = segment.get("departure_airport") or {}
        arr = segment.get("arrival_airport") or {}
        number = segment.get("flight_number") or ""
        details.append(
            f"{number} {segment.get('airline') or ''}: "
            f"{dep.get('id') or dep.get('name') or '?'} {dep.get('time') or ''} -> "
            f"{arr.get('id') or arr.get('name') or '?'} {arr.get('time') or ''}".strip()
        )

    # Some responses include both legs in `flights`; split at each destination arrival.
    route_stops = None
    leg_segments = 0
    stop_total = 0
    current_airport = origin
    for segment in segments:
        dep = (segment.get("departure_airport") or {}).get("id")
        arr = (segment.get("arrival_airport") or {}).get("id")
        if dep and current_airport and dep != current_airport:
            leg_segments = 0
        leg_segments += 1
        current_airport = arr or current_airport
        if arr == destination or arr == origin:
            stop_total += max(0, leg_segments - 1)
            leg_segments = 0
            current_airport = arr
    if segments and leg_segments == 0:
        route_stops = stop_total
    if route_stops is None:
        layovers = result.get("layovers")
        route_stops = len(layovers) if isinstance(layovers, list) else None
    return {
        "airline": ", ".join(airlines) or None,
        "stops": route_stops,
        "details": "\n".join(details) if details else None,
    }


def verify(deal: dict, db: sqlite3.Connection) -> dict | None:
    """Return a one-adult round-trip search result, not a booking guarantee."""
    data = serpapi(db, {
        "engine": "google_flights",
        "departure_id": deal["origin"],
        "arrival_id": deal["dest"],
        "outbound_date": deal["start_date"],
        "return_date": deal["end_date"],
        "travel_class": 1,
        "stops": 3,
        "adults": 1,
        "type": 1,
    }, "verification", deal["origin"])
    results = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    priced = []
    for result in results:
        result_type = str(result.get("type", "")).casefold().replace("-", " ")
        if result_type not in ("round trip",) and not result.get("departure_token"):
            continue
        try:
            price = float(result["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if price > 0:
            priced.append((price, result))
    if not priced:
        return None
    _, outbound = min(priced, key=lambda pair: pair[0])
    result = outbound
    link = result_link(data)
    if outbound.get("departure_token"):
        return_data = serpapi(db, {
            "engine": "google_flights",
            "departure_token": outbound["departure_token"],
            "travel_class": 1,
            "adults": 1,
        }, "verification", deal["origin"])
        return_results = (return_data.get("best_flights") or []) + (return_data.get("other_flights") or [])
        return_priced = []
        for option in return_results:
            try:
                option_price = float(option["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if option_price > 0:
                return_priced.append((option_price, option))
        if not return_priced:
            return None
        price, return_result = min(return_priced, key=lambda pair: pair[0])
        outbound_segments = outbound.get("flights") or []
        return_segments = return_result.get("flights") or []
        first_return_departure = (
            (return_segments[0].get("departure_airport") or {}).get("id")
            if return_segments else None
        )
        combined_segments = (
            outbound_segments + return_segments
            if first_return_departure == deal["dest"] else return_segments or outbound_segments
        )
        result = {
            **return_result,
            "flights": combined_segments,
            "layovers": (outbound.get("layovers") or []) + (return_result.get("layovers") or []),
        }
        link = result_link(return_data) or link
    else:
        price = float(outbound["price"])
    details = summarize_flight(result, deal["origin"], deal["dest"])
    return {"price": price, **details, "link": link}


def open_jaw_params(deal: dict) -> tuple[dict, str] | None:
    dest = deal.get("dest", "")
    if dest not in JAPAN_HUBS:
        return None
    if dest in ("HND", "NRT"):
        alternate = "KIX"
    elif dest == "KIX":
        alternate = "HND"
    else:
        alternate = "HND" if dest != "HND" else "KIX"
    legs = [
        {"departure_id": deal["origin"], "arrival_id": dest, "date": deal["start_date"]},
        {"departure_id": alternate, "arrival_id": deal["origin"], "date": deal["end_date"]},
    ]
    return {
        "engine": "google_flights",
        "type": 3,
        "multi_city_json": json.dumps(legs, separators=(",", ":")),
        "adults": 1,
        "travel_class": 1,
        "stops": 3,
    }, alternate


def discover_open_jaw(deal: dict, db: sqlite3.Connection) -> tuple[dict | None, str | None, str | None]:
    params_and_alt = open_jaw_params(deal)
    if not params_and_alt:
        return None, None, None
    params, alternate = params_and_alt
    try:
        data = serpapi(db, params, "verification", deal["origin"])
        results = (data.get("best_flights") or []) + (data.get("other_flights") or [])
        priced = []
        for result in results:
            if str(result.get("type", "")).casefold() == "one way":
                continue
            try:
                price = float(result["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if price > 0:
                priced.append((price, result))
        if not priced:
            return None, alternate, "no open-jaw search results"
        price, result = min(priced, key=lambda pair: pair[0])
        summary = summarize_flight(result, deal["origin"], deal["dest"])
        summary["link"] = result_link(data)
        summary["price"] = price
        return summary, alternate, None
    except Exception as exc:
        error = sanitized_error(exc)
        return None, alternate, error


def has_alerted(db: sqlite3.Connection, deal: dict, verified_price: float) -> bool:
    return db.execute(
        """SELECT 1 FROM alerts WHERE origin=? AND dest=? AND start_date=? AND end_date=?
           AND (price=? OR verified_price=?) LIMIT 1""",
        (deal["origin"], deal["dest"], deal["start_date"], deal["end_date"],
         deal["price"], verified_price),
    ).fetchone() is not None


def verification_within_tolerance(scout_price: float, verified_price: float,
                                  tolerance: float) -> bool:
    return verified_price <= scout_price + tolerance


def notify(title: str, body: str, link: str | None) -> None:
    print(f"\n== {title}\n{body}\n{link or ''}")
    topic = os.environ.get("NTFY_TOPIC")
    if DRY_RUN or not topic:
        return
    headers = {"Title": title, "Priority": "high", "Tags": "airplane"}
    if link:
        headers["Click"] = link
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                      headers=headers, timeout=30).raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"ntfy notification failed: {str(exc).replace(topic, '[REDACTED]')}") from None


def log_verification(db: sqlite3.Connection, deal: dict, status: str,
                     verified_price: float | None = None, error: str | None = None) -> None:
    db.execute(
        """INSERT INTO verification_events
           (observed_at, origin, dest, start_date, end_date, status, scout_price, verified_price, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), deal["origin"], deal["dest"],
         deal["start_date"], deal["end_date"], status, deal.get("price"), verified_price, error),
    )
    db.commit()


def make_notification(deal: dict, verified: dict, reasons: list[str], pto: dict) -> tuple[str, str]:
    route = (f"{deal['origin']} -> {deal['dest']}" if deal["itinerary_type"] == "round_trip"
             else f"{deal['origin']} -> {deal['dest'].replace('/', ' / ')} -> {deal['origin']}")
    stops = verified.get("stops")
    stop_label = f"{stops} stop(s)" if stops is not None else "stops unavailable"
    pto_label = (
        f"~{pto['pto_weekdays']}-{pto['pto_weekdays_max']} weekdays (approximate; "
        "flight times/date-line may shift the estimate)"
    )
    body = (
        f"{route}; {deal['start_date']} to {deal['end_date']} ({deal['nights']} nights)\n"
        f"Verified search result: ${verified['price']:.0f}/person; scout: ${deal['price']:.0f}/person\n"
        f"{verified.get('airline') or 'Airline unavailable'}; {stop_label}\n"
        f"PTO estimate: {pto_label}\n"
        f"Why: {', '.join(reasons)}"
    )
    if verified.get("details"):
        body += f"\nFlights:\n{verified['details']}"
    if deal.get("average_price"):
        body += f"\nProvider typical: ${deal['average_price']:.0f}/person"
    return f"Japan ${verified['price']:.0f}: {route}", body


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    init_db(db)
    try:
        deals = []
        statuses = []
        for origin in ORIGINS:
            found, status, _, raw_count = fetch_origin_deals(db, origin)
            statuses.append(status)
            deals.extend(found)
            print(f"{origin}: {raw_count} raw results, {len(found)} valid Japan itineraries ({status})")
        api_failures = statuses.count("error")
        successful_scans = statuses.count("success")
        if api_failures and not successful_scans:
            print("No origin search succeeded; all remaining origins failed or were skipped.", file=sys.stderr)
            return 1
        if api_failures:
            print(f"Partial scan failure: {api_failures}/{len(ORIGINS)} origin searches failed.", file=sys.stderr)
        if not deals:
            print("No matching deals found in successful origin searches.")
            return 0

        candidates = []
        candidate_keys = set()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        histories = [itinerary_history(db, deal) for deal in deals]
        for deal, (previous, low) in zip(deals, histories):
            reasons, qualifies, discount, new_low = alert_reasons(deal, deal["price"], previous, low)
            pto = estimate_pto(date.fromisoformat(deal["start_date"]), date.fromisoformat(deal["end_date"]))
            cur = db.execute(
                """INSERT INTO observations
                   (observed_at, origin, dest, dest_name, start_date, end_date, nights, price,
                    average_price, discount_pct, stops, airline, link, pto_weekdays,
                    pto_weekdays_max, pto_approximate,
                    itinerary_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, deal["origin"], deal["dest"], deal["dest_name"], deal["start_date"],
                 deal["end_date"], deal["nights"], deal["price"], deal["average_price"],
                 deal["discount_pct"], deal["stops"], deal["airline"], deal["link"],
                 pto["pto_weekdays"], pto["pto_weekdays_max"],
                 int(pto["pto_approximate"]), "round_trip"),
            )
            observation_id = cur.lastrowid
            key = (deal["origin"], deal["dest"], deal["start_date"], deal["end_date"], deal["price"])
            if qualifies and key not in candidate_keys and not has_alerted(db, deal, deal["price"]):
                candidate_keys.add(key)
                candidates.append({
                    "deal": deal, "previous": previous, "low": low,
                    "scout_reasons": reasons, "score": deal_score(
                        deal, deal["price"], previous, discount, new_low),
                    "observation_id": observation_id,
                })
        db.commit()

        candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
        max_verifications = env_int("MAX_VERIFICATIONS_PER_RUN", 6)
        price_tolerance = env_int("VERIFY_PRICE_TOLERANCE", 50)
        verified_candidates = []
        verified_candidate_keys = set()
        for candidate in candidates[:max_verifications]:
            deal = candidate["deal"]
            try:
                verified = verify(deal, db)
            except BudgetExceeded as exc:
                log_verification(db, deal, "budget_skipped", error=str(exc))
                print(f"Verification skipped for {deal['origin']}->{deal['dest']}: {exc}")
                continue
            except Exception as exc:
                error = sanitized_error(exc)
                log_verification(db, deal, "error", error=error)
                print(f"Verification failed for {deal['origin']}->{deal['dest']}: {error}", file=sys.stderr)
                continue
            if verified is None:
                log_verification(db, deal, "no_results")
                print(f"No verified flight results for {deal['origin']}->{deal['dest']} {deal['start_date']}")
                continue

            verified_price = verified["price"]
            log_verification(db, deal, "success", verified_price)
            if not verification_within_tolerance(deal["price"], verified_price, price_tolerance):
                print(
                    f"Skipping {deal['origin']}->{deal['dest']}: verified ${verified_price:.0f} "
                    f"is over scout ${deal['price']:.0f} by more than ${price_tolerance}."
                )
                continue
            reasons, qualifies, discount, new_low = alert_reasons(
                deal, verified_price, candidate["previous"], candidate["low"])
            if not qualifies or has_alerted(db, deal, verified_price):
                continue
            verified_key = (
                deal["origin"], deal["dest"], deal["start_date"], deal["end_date"], verified_price
            )
            if verified_key in verified_candidate_keys:
                continue
            verified_candidate_keys.add(verified_key)
            if verified.get("stops") is not None:
                deal["stops"] = verified["stops"]
            pto = estimate_pto(date.fromisoformat(deal["start_date"]), date.fromisoformat(deal["end_date"]))
            score = deal_score(deal, verified_price, candidate["previous"], discount,
                               new_low, verified.get("stops"))
            db.execute(
                """UPDATE observations SET verified_price=?, verified_airline=?, verified_stops=?,
                   verified_details=?, verified_link=?, deal_score=? WHERE id=?""",
                (verified_price, verified.get("airline"), verified.get("stops"),
                 verified.get("details"), verified.get("link"), score, candidate["observation_id"]),
            )
            verified_candidates.append({
                **candidate, "verified": verified, "reasons": reasons, "pto": pto, "score": score,
            })
            db.commit()

        # A single additional multi-city search tests the most promising round trip per run.
        open_jaw_limit = env_int("OPEN_JAW_SEARCHES_PER_RUN", 1)
        if open_jaw_limit not in (0, 1):
            raise ValueError("OPEN_JAW_SEARCHES_PER_RUN must be 0 or 1")
        if open_jaw_limit and verified_candidates:
            parent = max(verified_candidates, key=lambda item: item["score"])
            deal = parent["deal"]
            alternate = open_jaw_params(deal)
            if alternate and monthly_usage(db) < env_int("SERPAPI_MONTHLY_BUDGET", 250):
                result, other_airport, error = discover_open_jaw(deal, db)
                open_deal = {
                    **deal,
                    "dest": f"{deal['dest']}/{other_airport or ''}",
                    "dest_name": f"Open-jaw via {deal['dest']} and {other_airport}",
                    "itinerary_type": "open_jaw",
                    "price": result["price"] if result else deal["price"],
                    "average_price": None,
                    "discount_pct": None,
                }
                if result:
                    log_verification(db, open_deal, "open_jaw_success", result["price"])
                    pto = estimate_pto(date.fromisoformat(deal["start_date"]), date.fromisoformat(deal["end_date"]))
                    previous, low = itinerary_history(db, open_deal)
                    reasons, qualifies, discount, new_low = alert_reasons(
                        open_deal, result["price"], previous, low)
                    score = deal_score(open_deal, result["price"], previous, discount,
                                       new_low, result.get("stops"))
                    observation = db.execute(
                        """INSERT INTO observations
                           (observed_at, origin, dest, dest_name, start_date, end_date, nights, price,
                            stops, airline, link, verified_price, verified_airline, verified_stops,
                            verified_details, verified_link, pto_weekdays, pto_weekdays_max,
                            pto_approximate, deal_score,
                            itinerary_type)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open_jaw')""",
                        (now, open_deal["origin"], open_deal["dest"], open_deal["dest_name"],
                         open_deal["start_date"], open_deal["end_date"], open_deal["nights"],
                         result["price"], result.get("stops"), result.get("airline"), result.get("link"),
                         result["price"], result.get("airline"), result.get("stops"),
                         result.get("details"), result.get("link"), pto["pto_weekdays"],
                         pto["pto_weekdays_max"], int(pto["pto_approximate"]), score),
                    )
                    open_deal["price"] = result["price"]
                    if qualifies and not has_alerted(db, open_deal, result["price"]):
                        verified_candidates.append({
                            "deal": open_deal, "verified": result, "reasons": reasons,
                            "pto": pto, "score": score, "observation_id": observation.lastrowid,
                        })
                    db.commit()
                elif error:
                    log_verification(db, deal, "open_jaw_error", error=error)
                    print(f"Open-jaw search failed for {deal['origin']}: {error}", file=sys.stderr)
        elif verified_candidates and open_jaw_limit:
            print("Open-jaw discovery skipped: no verification result or API budget available.")

        verified_candidates.sort(key=lambda item: item["score"], reverse=True)
        for candidate in verified_candidates[:3]:
            deal = candidate["deal"]
            verified = candidate["verified"]
            title, body = make_notification(deal, verified, candidate["reasons"], candidate["pto"])
            link = verified.get("link") or deal.get("link")
            notify(title, body, link)
            db.execute(
                """INSERT OR IGNORE INTO alerts
                   (origin, dest, start_date, end_date, price, alerted_at, verified_price,
                    reasons, deal_score, verified_airline, verified_stops, verified_link)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (deal["origin"], deal["dest"], deal["start_date"], deal["end_date"],
                 deal["price"], now, verified["price"], ", ".join(candidate["reasons"]),
                 candidate["score"], verified.get("airline"), verified.get("stops"), link),
            )
            db.commit()
        print(f"{len(deals)} matching Japan deals; {len(verified_candidates)} verified alert candidates.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

"""Proof-of-concept for the Ignav flight-prices API, alongside the SerpApi tracker.

Demonstrates:
  1. cheapest-dates searches (a price for every departure day in a window)
  2. round-trip searches (full itineraries with airline/segments/stops)
  3. booking-link lookups (direct airline/OTA URLs via ignav_id)

Requires IGNAT_API_KEY in the environment. Does not touch the SerpApi database
or tracker.py; this is a standalone reference for comparing the two sources.
"""
import json
import os
import sys
import urllib.request
from datetime import date

BASE_URL = "https://ignav.com/api"

# Routes to demo (origin -> Japan hub). Keep small: each cheapest-dates call is
# one request, and round-trip + booking-links add a few more.
CALENDAR_ROUTES = [
    ("MCO", "HND"),
    ("ATL", "HND"),
    ("IAH", "KIX"),
    ("TPA", "OKA"),
]

# Departure window for the calendar demo and the detailed round-trip demo.
DEPARTURE_START = date(2027, 4, 8)
DEPARTURE_END = date(2027, 4, 20)
STAY_DAYS = 14
ADULTS = 1
MAX_STOPS = 2
MARKET = "US"


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
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def cheapest_dates(origin: str, dest: str) -> list[dict]:
    data = post("/fares/cheapest-dates", {
        "origin": origin,
        "destination": dest,
        "departure_date_range": {
            "start": DEPARTURE_START.isoformat(),
            "end": DEPARTURE_END.isoformat(),
        },
        "stay_days": STAY_DAYS,
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
    options = data.get("booking_options", [])
    links = []
    for option in options:
        for link in option.get("links", []):
            links.append(link)
    return links


def summarize(itinerary: dict) -> dict:
    """Flatten an Ignav itinerary into the fields the tracker cares about."""
    price = itinerary.get("price") or {}
    outbound = itinerary.get("outbound") or {}
    inbound = itinerary.get("inbound") or {}

    def carriers(leg: dict) -> str:
        names = []
        for segment in leg.get("segments", []):
            name = segment.get("operating_carrier_name") or segment.get("marketing_carrier_code")
            if name and name not in names:
                names.append(name)
        return ", ".join(names) or leg.get("carrier") or "?"

    def stops(leg: dict) -> int:
        return max(0, len(leg.get("segments", [])) - 1)

    return {
        "price": price.get("amount"),
        "currency": price.get("currency", "USD"),
        "status": price.get("status"),
        "carrier": itinerary.get("outbound", {}).get("carrier") or carriers(outbound),
        "airlines": carriers(outbound),
        "stops": stops(outbound) + stops(inbound),
        "outbound_stops": stops(outbound),
        "inbound_stops": stops(inbound),
        "duration_minutes": (outbound.get("duration_minutes") or 0) + (inbound.get("duration_minutes") or 0),
        "ignav_id": itinerary.get("ignav_id"),
        "cabin_class": itinerary.get("cabin_class"),
    }


def main() -> int:
    print("Ignav proof-of-concept\n")

    print("== Cheapest fare per departure day (stay {STAY_DAYS} nights) ==".replace(
        "{STAY_DAYS}", str(STAY_DAYS)))
    cheapest_pick = None
    for origin, dest in CALENDAR_ROUTES:
        dates = cheapest_dates(origin, dest)
        print(f"\n{origin} -> {dest}: {len(dates)} days")
        for entry in dates:
            price = entry.get("price") or {}
            amount = price.get("amount")
            amount_text = f"${amount:,.0f}" if isinstance(amount, (int, float)) else "n/a"
            print(f"  {entry['departure_date']} -> {entry['return_date']}  "
                  f"{amount_text} {price.get('currency') or 'USD'} [{price.get('status')}]")
            if isinstance(amount, (int, float)) and (
                cheapest_pick is None or amount < cheapest_pick[0]
            ):
                cheapest_pick = (
                    amount, origin, dest, entry["departure_date"], entry["return_date"],
                )

    if not cheapest_pick:
        print("\nNo calendar results returned.")
        return 0

    _, origin, dest, depart, ret = cheapest_pick
    print(f"\n== Cheapest across all routes: {origin} -> {dest} "
          f"{depart} -> {ret} = ${cheapest_pick[0]:,.0f} ==")

    print(f"\n== Top itineraries: {origin} -> {dest} {depart} -> {ret} ==")
    itineraries = round_trip(origin, dest, depart, ret)
    for index, itinerary in enumerate(itineraries[:5]):
        summary = summarize(itinerary)
        price_text = f"${summary['price']:,.0f}" if isinstance(summary["price"], (int, float)) else "n/a"
        print(f"  {index + 1}. {price_text} {summary['currency']} "
              f"({summary['status']}) | {summary['airlines']} | "
              f"{summary['stops']} stop(s) | {summary['cabin_class']}")

    if not itineraries:
        print("No round-trip itineraries returned.")
        return 0

    top = itineraries[0]
    summary = summarize(top)
    top_price_text = f"${summary['price']:,.0f}" if isinstance(summary["price"], (int, float)) else "n/a"
    print(f"\n== Booking links for the cheapest itinerary "
          f"({summary['airlines']}, {top_price_text}) ==")
    links = booking_links(top.get("ignav_id"))
    if not links:
        print("No booking links returned.")
        return 0
    for link in links[:5]:
        provider = link.get("provider_name") or "?"
        link_price = link.get("price") or {}
        amount = link_price.get("amount")
        price_text = f"${amount:,.0f}" if isinstance(amount, (int, float)) else "n/a"
        print(f"  {provider:20} {price_text:>10}  {link.get('url')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

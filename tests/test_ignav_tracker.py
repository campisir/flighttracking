import sqlite3
import unittest

import ignav_tracker


def itinerary(**changes):
    result = {
        "price": {"amount": 1300.0, "currency": "USD", "status": "verified"},
        "outbound": {
            "carrier": "United",
            "duration_minutes": 1204,
            "segments": [
                {"operating_carrier_name": "United", "marketing_carrier_code": "UA"},
                {"operating_carrier_name": "ANA", "marketing_carrier_code": "NH"},
            ],
        },
        "inbound": {
            "carrier": "United",
            "duration_minutes": 1000,
            "segments": [
                {"operating_carrier_name": "United", "marketing_carrier_code": "UA"},
            ],
        },
        "cabin_class": "economy",
        "ignav_id": "abc123",
    }
    result.update(changes)
    return result


class IgnavTrackerTests(unittest.TestCase):
    def test_init_db_creates_schema(self):
        db = sqlite3.connect(":memory:")
        ignav_tracker.init_db(db)
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("observations", tables)
        self.assertIn("scan_runs", tables)
        columns = {row[1] for row in db.execute("PRAGMA table_info(observations)")}
        for column in ("price", "verified_price", "verified_airline", "booking_link",
                       "booking_provider", "booking_provider_type"):
            self.assertIn(column, columns)
        db.close()

    def test_init_db_migrates_existing_table(self):
        db = sqlite3.connect(":memory:")
        db.executescript("""
            CREATE TABLE observations (
                id INTEGER PRIMARY KEY, observed_at TEXT, origin TEXT, dest TEXT,
                start_date TEXT, end_date TEXT, nights INTEGER, price REAL, currency TEXT,
                price_status TEXT, verified_price REAL, verified_airline TEXT,
                verified_stops INTEGER, verified_cabin_class TEXT,
                verified_duration_minutes INTEGER, ignav_id TEXT, booking_link TEXT
            );
            CREATE TABLE scan_runs (
                id INTEGER PRIMARY KEY, scan_id TEXT, observed_at TEXT, origin TEXT,
                status TEXT, raw_results INTEGER, valid_results INTEGER, error TEXT
            );
        """)
        ignav_tracker.init_db(db)
        columns = {row[1] for row in db.execute("PRAGMA table_info(observations)")}
        self.assertIn("booking_provider", columns)
        self.assertIn("booking_provider_type", columns)
        db.close()

    def test_summarize_flattens_round_trip_itinerary(self):
        summary = ignav_tracker.summarize(itinerary())
        self.assertEqual(summary["price"], 1300.0)
        self.assertEqual(summary["currency"], "USD")
        self.assertEqual(summary["status"], "verified")
        self.assertEqual(summary["airline"], "United, ANA")
        # Outbound has 2 segments (1 stop); inbound has 1 segment (0 stops).
        self.assertEqual(summary["stops"], 1)
        self.assertEqual(summary["cabin_class"], "economy")
        self.assertEqual(summary["duration_minutes"], 2204)
        self.assertEqual(summary["ignav_id"], "abc123")

    def test_summarize_counts_stops_per_leg(self):
        two_stop_inbound = itinerary(inbound={
            "carrier": "United",
            "duration_minutes": 1000,
            "segments": [
                {"operating_carrier_name": "A"},
                {"operating_carrier_name": "B"},
                {"operating_carrier_name": "C"},
            ],
        })
        summary = ignav_tracker.summarize(two_stop_inbound)
        # Outbound 1 stop + inbound 2 stops = 3 total.
        self.assertEqual(summary["stops"], 3)

    def test_parse_calendar_skips_null_and_missing_prices(self):
        dates = [
            {"departure_date": "2027-04-08", "return_date": "2027-04-22",
             "price": {"amount": 1417.0, "currency": "USD", "status": "unverified"}},
            {"departure_date": "2027-04-09", "return_date": "2027-04-23",
             "price": {"amount": None, "currency": "USD", "status": None}},
            {"departure_date": "2027-04-10", "return_date": "2027-04-24"},
        ]
        rows = ignav_tracker.parse_calendar("MCO", "HND", 14, dates)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 1417.0)
        self.assertEqual(rows[0]["nights"], 14)
        self.assertEqual(rows[0]["origin"], "MCO")
        self.assertEqual(rows[0]["start_date"], "2027-04-08")

    def test_choose_booking_link_prefers_airline(self):
        ota = {"provider_name": "HolidayBreakz", "provider_type": "third_party",
               "url": "https://holidaybreakz.example/"}
        airline = {"provider_name": "Delta", "provider_type": "airline",
                   "url": "https://delta.example/"}
        url, provider, provider_type = ignav_tracker.choose_booking_link([ota, airline])
        self.assertEqual((url, provider, provider_type),
                         ("https://delta.example/", "Delta", "airline"))
        # Falls back to the first link when no airline link exists.
        url, provider, provider_type = ignav_tracker.choose_booking_link([ota])
        self.assertEqual(provider, "HolidayBreakz")
        self.assertEqual(provider_type, "third_party")

    def test_bookable_discount(self):
        self.assertAlmostEqual(ignav_tracker.bookable_discount(1234.0, 868.0), 0.296596, places=5)
        self.assertEqual(ignav_tracker.bookable_discount(1234.0, 1300.0), 0.0)
        self.assertEqual(ignav_tracker.bookable_discount(0, 868.0), 0.0)

    def test_bookable_within_band(self):
        # Calendar $1234: $868 (30% low) and $2371 (92% high) are outside the band.
        self.assertFalse(ignav_tracker.bookable_within_band(1234.0, 868.0))
        self.assertFalse(ignav_tracker.bookable_within_band(1234.0, 2371.0))
        # $1255 vs $1255 (exact match) and $1234 vs $1400 (13% high) are inside.
        self.assertTrue(ignav_tracker.bookable_within_band(1255.0, 1255.0))
        self.assertTrue(ignav_tracker.bookable_within_band(1234.0, 1400.0))
        self.assertTrue(ignav_tracker.bookable_within_band(0, 868.0))


if __name__ == "__main__":
    unittest.main()

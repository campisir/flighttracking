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
        for column in ("price", "verified_price", "verified_airline", "booking_link"):
            self.assertIn(column, columns)
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


if __name__ == "__main__":
    unittest.main()

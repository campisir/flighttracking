import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import requests

import report
import tracker


def deal(**changes):
    result = {
        "origin": "TPA",
        "dest": "HND",
        "dest_name": "Tokyo",
        "start_date": "2027-03-20",
        "end_date": "2027-04-03",
        "nights": 14,
        "price": 800.0,
        "average_price": 1200.0,
        "discount_pct": 33.3,
        "stops": 1,
        "airline": "Test Air",
        "link": "https://www.google.com/travel/flights",
        "itinerary_type": "round_trip",
    }
    result.update(changes)
    return result


def api_response(data):
    return type("Response", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: data,
    })()


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        tracker.init_db(self.db)

    def tearDown(self):
        self.db.close()

    def add_observation(self, price, observed_at, itinerary=None):
        itinerary = itinerary or deal()
        self.db.execute(
            """INSERT INTO observations
               (observed_at, origin, dest, start_date, end_date, nights, price)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (observed_at, itinerary["origin"], itinerary["dest"], itinerary["start_date"],
             itinerary["end_date"], itinerary["nights"], price),
        )
        self.db.commit()

    def test_price_drop_uses_most_recent_observation_not_historical_low(self):
        current = deal(origin="PNS", price=950)
        self.add_observation(900, "2026-10-01T12:00:00+00:00", current)
        self.add_observation(1100, "2026-10-02T12:00:00+00:00", current)
        previous, low = tracker.itinerary_history(self.db, current)
        reasons, qualifies, _, new_low = tracker.alert_reasons(current, 950, previous, low)
        self.assertEqual(previous, 1100)
        self.assertEqual(low, 900)
        self.assertTrue(qualifies)
        self.assertFalse(new_low)
        self.assertTrue(any("down $150" in reason for reason in reasons))
        self.assertFalse(any("historical low" in reason for reason in reasons))

    def test_first_observation_is_not_a_price_drop(self):
        reasons, qualifies, _, new_low = tracker.alert_reasons(deal(), 800, None, None)
        self.assertTrue(qualifies)
        self.assertTrue(new_low)
        self.assertFalse(any("down $" in reason for reason in reasons))

    def test_historical_low_alone_does_not_qualify_an_expensive_fare(self):
        current = deal(price=1300, average_price=2000)
        reasons, qualifies, _, new_low = tracker.alert_reasons(current, 1300, 1400, 1350)
        self.assertFalse(qualifies)
        self.assertTrue(new_low)
        self.assertTrue(any("historical low" in reason for reason in reasons))

    def test_verified_price_must_be_within_configured_tolerance(self):
        self.assertTrue(tracker.verification_within_tolerance(800, 850, 50))
        self.assertFalse(tracker.verification_within_tolerance(800, 851, 50))

    def test_verified_search_uses_one_adult_price_and_extracts_flight_data(self):
        result = {
            "price": 820,
            "type": "Round trip",
            "flights": [
                {
                    "airline": "Example Airlines",
                    "flight_number": "EA 1",
                    "departure_airport": {"id": "TPA", "time": "2027-03-20 10:00"},
                    "arrival_airport": {"id": "HND", "time": "2027-03-21 14:00"},
                },
                {
                    "airline": "Example Airlines",
                    "flight_number": "EA 2",
                    "departure_airport": {"id": "HND", "time": "2027-04-03 16:00"},
                    "arrival_airport": {"id": "TPA", "time": "2027-04-03 14:00"},
                },
            ],
            "layovers": [],
        }
        response = type("Response", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {
                "best_flights": [result],
                "search_metadata": {"google_flights_url": "https://www.google.com/travel/flights"},
            },
        })()
        with patch.dict("os.environ", {"SERPAPI_KEY": "test-key"}):
            with patch("tracker.requests.get", return_value=response) as request:
                verified = tracker.verify(deal(), self.db)
        self.assertEqual(verified["price"], 820)
        self.assertEqual(verified["airline"], "Example Airlines")
        self.assertEqual(verified["stops"], 0)
        self.assertIn("EA 1", verified["details"])
        self.assertEqual(verified["link"], "https://www.google.com/travel/flights")
        self.assertEqual(request.call_args.kwargs["params"]["adults"], 1)

    def test_verification_uses_departure_token_for_return_and_combines_segments(self):
        outbound = {
            "price": 700,
            "type": "Round trip",
            "departure_token": "outbound-token",
            "flights": [{
                "airline": "Outbound Air",
                "flight_number": "OA 1",
                "departure_airport": {"id": "TPA", "time": "2027-03-20 10:00"},
                "arrival_airport": {"id": "HND", "time": "2027-03-21 14:00"},
            }],
        }
        return_flight = {
            "price": 760,
            "type": "One way",
            "flights": [{
                "airline": "Return Air",
                "flight_number": "RA 2",
                "departure_airport": {"id": "HND", "time": "2027-04-03 16:00"},
                "arrival_airport": {"id": "TPA", "time": "2027-04-03 14:00"},
            }],
        }
        with patch.dict("os.environ", {"SERPAPI_KEY": "test-key"}):
            with patch("tracker.requests.get", side_effect=[
                api_response({"best_flights": [outbound]}),
                api_response({"best_flights": [return_flight]}),
            ]) as request:
                verified = tracker.verify(deal(), self.db)
        self.assertEqual(verified["price"], 760)
        self.assertEqual(verified["airline"], "Outbound Air, Return Air")
        self.assertEqual(verified["stops"], 0)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].kwargs["params"]["departure_token"], "outbound-token")

    def test_verification_with_no_flights_returns_none(self):
        response = type("Response", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {"best_flights": [], "other_flights": []},
        })()
        with patch.dict("os.environ", {"SERPAPI_KEY": "test-key"}):
            with patch("tracker.requests.get", return_value=response):
                self.assertIsNone(tracker.verify(deal(), self.db))

    def test_notification_failure_does_not_disclose_ntfy_topic(self):
        with patch.dict("os.environ", {"NTFY_TOPIC": "secret-topic"}):
            with patch.object(tracker, "DRY_RUN", False):
                with patch("tracker.requests.post", side_effect=requests.HTTPError(
                    "404 from https://ntfy.sh/secret-topic"
                )):
                    with self.assertRaisesRegex(RuntimeError, "notification failed") as error:
                        tracker.notify("title", "body", None)
        self.assertNotIn("secret-topic", str(error.exception))

    def test_pto_weekday_estimate_includes_itinerary_travel_dates(self):
        estimate = tracker.estimate_pto(date(2027, 3, 20), date(2027, 4, 3))
        self.assertEqual(estimate, {
            "pto_weekdays": 10, "pto_weekdays_max": 11, "pto_approximate": True,
        })

    def test_scout_stop_filter_is_not_used_as_returned_stop_count(self):
        raw = {
            "country": "Japan",
            "outbound_date": "2027-03-20",
            "return_date": "2027-04-03",
            "price": 850,
            "arrival_airport_code": "HND",
        }
        parsed = tracker.parse_deals([raw], "TPA")
        self.assertIsNone(parsed[0]["stops"])
        self.assertEqual(tracker.parse_deals([{**raw, "stops": 2}], "TPA")[0]["stops"], 2)

    def test_alert_deduplication_uses_verified_price_across_scout_variations(self):
        itinerary = deal(price=820)
        self.db.execute(
            """INSERT INTO alerts
               (origin, dest, start_date, end_date, price, alerted_at, verified_price)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("TPA", "HND", "2027-03-20", "2027-04-03", 810, "2026-10-01T00:00:00Z", 820),
        )
        self.db.commit()
        self.assertTrue(tracker.has_alerted(self.db, itinerary, 820))
        self.assertFalse(tracker.has_alerted(self.db, itinerary, 821))

    def test_deal_score_prefers_fewer_stops_and_accounts_for_pto(self):
        itinerary = deal(price=850)
        no_stops = tracker.deal_score(itinerary, 850, None, 20, False, 0)
        two_stops = tracker.deal_score(itinerary, 850, None, 20, False, 2)
        self.assertGreater(no_stops, two_stops)

    def test_monthly_budget_counts_failed_requests_and_reserves_verification(self):
        with patch.dict("os.environ", {
            "SERPAPI_KEY": "test-key",
            "SERPAPI_MONTHLY_BUDGET": "2",
            "SERPAPI_VERIFY_RESERVE": "1",
        }):
            with patch("tracker.requests.get", side_effect=requests.Timeout("offline")):
                with self.assertRaises(requests.Timeout):
                    tracker.serpapi(self.db, {"engine": "google_flights_deals"}, "scout", "PNS")
                self.assertEqual(tracker.monthly_usage(self.db), 1)
                with self.assertRaises(tracker.BudgetExceeded):
                    tracker.serpapi(self.db, {"engine": "google_flights_deals"}, "scout", "TPA")
                with self.assertRaises(requests.Timeout):
                    tracker.serpapi(self.db, {"engine": "google_flights"}, "verification", "PNS")
                self.assertEqual(tracker.monthly_usage(self.db), 2)
                with self.assertRaises(tracker.BudgetExceeded):
                    tracker.serpapi(self.db, {"engine": "google_flights"}, "verification", "PNS")

    def test_empty_search_is_successful_zero_result_scan(self):
        response = type("Response", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {"error": "Empty results"},
        })()
        with patch.dict("os.environ", {"SERPAPI_KEY": "test-key"}):
            with patch("tracker.requests.get", return_value=response) as request:
                results, status, error, raw_count = tracker.fetch_origin_deals(self.db, "PNS")
        self.assertEqual((results, status, error, raw_count), ([], "success", None, 0))
        scan = self.db.execute("SELECT status, raw_results, valid_results FROM scan_runs").fetchone()
        self.assertEqual(tuple(scan), ("success", 0, 0))
        self.assertEqual(request.call_args.kwargs["params"]["stops"], 3)
        self.assertEqual(request.call_args.kwargs["params"]["adults"], 1)

    def test_open_jaw_request_uses_supported_multicity_shape(self):
        params, alternate = tracker.open_jaw_params(deal())
        legs = json.loads(params["multi_city_json"])
        self.assertEqual(params["type"], 3)
        self.assertEqual(alternate, "KIX")
        self.assertEqual(legs, [
            {"departure_id": "TPA", "arrival_id": "HND", "date": "2027-03-20"},
            {"departure_id": "KIX", "arrival_id": "TPA", "date": "2027-04-03"},
        ])

    def test_all_origin_api_errors_fail_scan_and_are_recorded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(tracker, "DB_PATH", Path(temp_dir) / "prices.db"):
                with patch.dict("os.environ", {
                    "SERPAPI_KEY": "test-key",
                    "SERPAPI_MONTHLY_BUDGET": "250",
                    "SERPAPI_VERIFY_RESERVE": "40",
                }):
                    with patch("tracker.requests.get", side_effect=requests.Timeout("offline")):
                        self.assertEqual(tracker.main(), 1)
                db = sqlite3.connect(tracker.DB_PATH)
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM scan_runs WHERE status='error'").fetchone()[0], 3
                )
                db.close()

    def test_verification_failure_does_not_send_or_record_an_alert(self):
        scout_result = {
            "country": "Japan",
            "outbound_date": "2027-03-20",
            "return_date": "2027-04-03",
            "price": 700,
            "stops": 1,
            "arrival_airport_code": "HND",
            "name": "Tokyo",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(tracker, "DB_PATH", Path(temp_dir) / "prices.db"):
                with patch.dict("os.environ", {
                    "SERPAPI_KEY": "test-key",
                    "SERPAPI_MONTHLY_BUDGET": "250",
                    "SERPAPI_VERIFY_RESERVE": "40",
                }):
                    with patch("tracker.requests.get", side_effect=[
                        api_response({"deals": [scout_result]}),
                        api_response({"deals": []}),
                        api_response({"deals": []}),
                        requests.Timeout("verification offline"),
                    ]):
                        self.assertEqual(tracker.main(), 0)
                db = sqlite3.connect(tracker.DB_PATH)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
                self.assertEqual(
                    db.execute("SELECT status FROM verification_events").fetchone()[0], "error"
                )
                db.close()

    def test_significantly_higher_verified_fare_is_not_alerted(self):
        scout_result = {
            "country": "Japan",
            "outbound_date": "2027-03-20",
            "return_date": "2027-04-03",
            "price": 700,
            "stops": 1,
            "arrival_airport_code": "HND",
            "name": "Tokyo",
        }
        high_verified = {"best_flights": [{"price": 900, "type": "Round trip"}]}
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(tracker, "DB_PATH", Path(temp_dir) / "prices.db"):
                with patch.dict("os.environ", {
                    "SERPAPI_KEY": "test-key",
                    "SERPAPI_MONTHLY_BUDGET": "250",
                    "SERPAPI_VERIFY_RESERVE": "40",
                }):
                    with patch("tracker.requests.get", side_effect=[
                        api_response({"deals": [scout_result]}),
                        api_response({"deals": []}),
                        api_response({"deals": []}),
                        api_response(high_verified),
                    ]):
                        self.assertEqual(tracker.main(), 0)
                db = sqlite3.connect(tracker.DB_PATH)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
                self.assertEqual(
                    db.execute("SELECT verified_price FROM verification_events").fetchone()[0], 900
                )
                db.close()

    def test_successful_alert_is_recorded_once_for_an_unchanged_fare(self):
        scout_result = {
            "country": "Japan",
            "outbound_date": "2027-03-20",
            "return_date": "2027-04-03",
            "price": 700,
            "stops": 1,
            "arrival_airport_code": "HND",
            "name": "Tokyo",
        }
        verified_result = {
            "best_flights": [{
                "price": 700,
                "type": "Round trip",
                "flights": [{
                    "airline": "Example Air",
                    "flight_number": "EA 1",
                    "departure_airport": {"id": "PNS", "time": "2027-03-20 10:00"},
                    "arrival_airport": {"id": "HND", "time": "2027-03-21 14:00"},
                }, {
                    "airline": "Example Air",
                    "flight_number": "EA 2",
                    "departure_airport": {"id": "HND", "time": "2027-04-03 16:00"},
                    "arrival_airport": {"id": "PNS", "time": "2027-04-03 14:00"},
                }],
            }],
        }
        responses = [
            api_response({"deals": [scout_result]}),
            api_response({"deals": []}),
            api_response({"deals": []}),
            api_response(verified_result),
            api_response({"deals": [scout_result]}),
            api_response({"deals": []}),
            api_response({"deals": []}),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(tracker, "DB_PATH", Path(temp_dir) / "prices.db"):
                with patch.dict("os.environ", {
                    "SERPAPI_KEY": "test-key",
                    "SERPAPI_MONTHLY_BUDGET": "250",
                    "SERPAPI_VERIFY_RESERVE": "40",
                    "OPEN_JAW_SEARCHES_PER_RUN": "0",
                }):
                    with patch("tracker.requests.get", side_effect=responses):
                        with patch("tracker.notify") as notify:
                            self.assertEqual(tracker.main(), 0)
                            self.assertEqual(tracker.main(), 0)
                self.assertEqual(notify.call_count, 1)
                db = sqlite3.connect(tracker.DB_PATH)
                alert = db.execute(
                    "SELECT verified_price, verified_airline, verified_stops FROM alerts"
                ).fetchone()
                self.assertEqual(alert, (700, "Example Air", 0))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)
                db.close()

    def test_duplicate_scout_prices_in_one_run_do_not_create_a_fake_price_drop(self):
        high_scout = {
            "country": "Japan",
            "outbound_date": "2027-03-20",
            "return_date": "2027-04-03",
            "price": 900,
            "stops": 1,
            "arrival_airport_code": "HND",
            "name": "Tokyo",
        }
        low_scout = {**high_scout, "price": 700}
        verified_result = {
            "best_flights": [{
                "price": 700,
                "type": "Round trip",
                "flights": [{
                    "airline": "Example Air",
                    "flight_number": "EA 1",
                    "departure_airport": {"id": "PNS", "time": "2027-03-20 10:00"},
                    "arrival_airport": {"id": "HND", "time": "2027-03-21 14:00"},
                }, {
                    "airline": "Example Air",
                    "flight_number": "EA 2",
                    "departure_airport": {"id": "HND", "time": "2027-04-03 16:00"},
                    "arrival_airport": {"id": "PNS", "time": "2027-04-03 14:00"},
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(tracker, "DB_PATH", Path(temp_dir) / "prices.db"):
                with patch.dict("os.environ", {
                    "SERPAPI_KEY": "test-key",
                    "SERPAPI_MONTHLY_BUDGET": "250",
                    "SERPAPI_VERIFY_RESERVE": "40",
                    "OPEN_JAW_SEARCHES_PER_RUN": "0",
                }):
                    with patch("tracker.requests.get", side_effect=[
                        api_response({"deals": [high_scout, low_scout]}),
                        api_response({"deals": []}),
                        api_response({"deals": []}),
                        api_response(verified_result),
                        api_response(verified_result),
                    ]):
                        with patch("tracker.notify") as notify:
                            self.assertEqual(tracker.main(), 0)
                body = notify.call_args.args[1]
                self.assertNotIn("down $200", body)
                db = sqlite3.connect(tracker.DB_PATH)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)
                db.close()

    def test_migration_preserves_existing_observation_and_alert(self):
        legacy = sqlite3.connect(":memory:")
        legacy.executescript("""
            CREATE TABLE observations (
                id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, origin TEXT NOT NULL,
                dest TEXT NOT NULL, dest_name TEXT, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                nights INTEGER NOT NULL, price REAL NOT NULL, average_price REAL, discount_pct REAL,
                stops INTEGER, airline TEXT, link TEXT
            );
            CREATE TABLE alerts (
                origin TEXT, dest TEXT, start_date TEXT, end_date TEXT, price REAL, alerted_at TEXT,
                PRIMARY KEY (origin, dest, start_date, end_date, price)
            );
            INSERT INTO observations VALUES
                (1, '2026-10-01T00:00:00Z', 'TPA', 'HND', 'Tokyo', '2027-03-20',
                 '2027-04-03', 14, 800, 1200, 33, 1, 'Test Air', NULL);
            INSERT INTO alerts VALUES ('TPA', 'HND', '2027-03-20', '2027-04-03', 800, '2026-10-01');
        """)
        tracker.init_db(legacy)
        self.assertEqual(legacy.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)
        self.assertEqual(legacy.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)
        columns = {row[1] for row in legacy.execute("PRAGMA table_info(observations)")}
        self.assertIn("verified_price", columns)
        legacy.close()

    def test_report_shows_latest_scan_verified_fare_and_repeated_price_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "prices.db"
            output = Path(temp_dir) / "report.html"
            db = sqlite3.connect(database)
            tracker.init_db(db)
            values = ("TPA", "HND", "Tokyo", "2027-03-20", "2027-04-03", 14)
            for observed_at, price, verified_price in (
                ("2026-10-01T12:00:00+00:00", 800, None),
                ("2026-10-02T12:00:00+00:00", 700, 710),
            ):
                db.execute(
                    """INSERT INTO observations
                       (observed_at, origin, dest, dest_name, start_date, end_date, nights,
                        price, verified_price, itinerary_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'round_trip')""",
                    (observed_at, *values, price, verified_price),
                )
            db.execute(
                """INSERT INTO scan_runs
                   (scan_id, observed_at, origin, status, raw_results, valid_results)
                   VALUES ('scan', '2026-10-02T12:00:00+00:00', 'TPA', 'success', 1, 1)"""
            )
            db.commit()
            db.close()
            with patch.object(report, "DB_PATH", database):
                with patch.object(report, "OUT", output):
                    with patch("sys.argv", ["report.py", "--no-open"]):
                        self.assertEqual(report.main(), 0)
            html = output.read_text(encoding="utf-8")
            self.assertIn("Latest scan status", html)
            self.assertIn("Repeated itinerary price trends", html)
            self.assertIn("$710", html)
            self.assertIn("not a live availability feed", html)


if __name__ == "__main__":
    unittest.main()

# Japan flight tracker

Scouts Google Flights Deals via SerpApi for PNS/TPA/MCO -> Japan (depart Mar 20 - Apr 10, 2027, 12-16 nights, up to 2 stops by default via `MAX_STOPS`),
stores every result in SQLite (`data/prices.db`), verifies candidates with a detailed Google Flights search, and sends an
[ntfy](https://ntfy.sh) push when something interesting shows up.

Alerts fire on: fare under a threshold, >= $100 drop for the same itinerary, >= 20% below Google's typical price,
or a new all-time low.

## Setup

1. Get a SerpApi key (free tier: 250 searches/month). A run uses 3 searches (one per origin) plus up to 3 verification searches only when alerting.
2. Pick a hard-to-guess ntfy topic and subscribe to it in the ntfy app.
3. Edit the config block at the top of `tracker.py` (travelers, thresholds).

### Local

```powershell
pip install -r requirements.txt
$env:SERPAPI_KEY = "..."; $env:NTFY_TOPIC = "my-secret-topic"
$env:DRY_RUN = "1"   # print alerts instead of pushing
python tracker.py
```

### Viewing the data

`python report.py` builds `data/report.html` (cheapest fare per run chart plus a sortable-by-price itinerary table with Google Flights links) and opens it in your browser. Use `--no-open` to skip opening. If the tracker runs on GitHub Actions, `git pull` first to get the latest database.

### GitHub Actions (cron)

Add repo secrets `SERPAPI_KEY` and `NTFY_TOPIC`. `.github/workflows/track.yml` runs twice daily and commits the
updated database back to the repo.

Prefer a local cron? `0 7,19 * * * cd /path/to/flighttracking && SERPAPI_KEY=... NTFY_TOPIC=... python tracker.py`

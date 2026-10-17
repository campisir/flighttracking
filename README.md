# Japan flight tracker

Scouts Google Flights Deals via SerpApi for PNS, TPA, and MCO to Japan, for departures
March 20–April 10, 2027 and trips of 12–16 nights. It preserves every scout observation in
`data/prices.db`, verifies only promising results through the detailed Google Flights API,
and sends at most three high-signal ntfy alerts per run. GitHub Actions continues to run
twice daily.

## Alert rules and ranking

- Per-person target fares are **$900 from PNS** and **$850 from TPA/MCO**.
- A fare slightly above its airport target can qualify when it is within the configured
  premium and has an exceptional price drop or discount. A 20% discount is shown as a
  reason, but does not by itself qualify an expensive fare.
- Price drops compare against the most recent earlier observation of that exact route and
  dates; the itinerary's all-time low is tracked separately. A historical low alone does
  not trigger an alert.
- Alert candidates are checked against a one-adult Google Flights search. The API's
  documented response example has a `price` but does not specify whether a multi-adult
  response price is per person or a party total. The tracker requests one adult for both
  scout and verification so it never divides or guesses at that field's semantics.
  Existing observations are left untouched. SerpApi can require a `departure_token`
  follow-up request to obtain the return leg; those requests are included in the budget.
  A verified result more than the configured tolerance above the scout price, a failed
  search, or no complete flight results is not alerted.
- Verified airline names, segment details, and stop counts replace the scout metadata when
  available. A result is an observed Google Flights search result, **not a guarantee that
  the fare is currently bookable**.
- Ranking considers price relative to the origin's target, stops, weekday PTO estimate,
  discount, recent price drop, and itinerary historical low. Nonstop and one-stop options
  rank ahead of two-stop options. Two-stop itineraries remain eligible, including
  exceptional deals.

## PTO and dates

PTO is estimated by counting weekdays from the itinerary's departure date through its
return date, inclusive. The report and alerts show that calendar estimate plus a
conservative one-weekday buffer (`~N–N+1`) for possible international date-line/timing
shifts. The estimate does not account for holidays or exact departure/arrival times and
is always labeled approximate. Itinerary timing would be needed for an exact PTO
calculation.

The default preferred return date is April 15, 2027. Returning after that date is a ranking
penalty, not an automatic rejection. A trip one weekday over the PTO budget is likewise
ranked lower rather than discarded.

## Setup

1. Get a SerpApi key and set the `SERPAPI_KEY` secret.
2. Pick a hard-to-guess ntfy topic, subscribe in the ntfy app, and set the `NTFY_TOPIC`
   secret.
3. Optional settings can be configured as environment variables locally or GitHub Actions
   repository variables:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `SERPAPI_MONTHLY_BUDGET` | `250` | Hard monthly cap on actual SerpApi requests, including failed requests. |
| `SERPAPI_VERIFY_RESERVE` | `40` | Keeps capacity available for optional verification; scouts may use at most the budget minus this reserve. |
| `MAX_VERIFICATIONS_PER_RUN` | `6` | Maximum round-trip candidates considered for verification in a run. |
| `OPEN_JAW_SEARCHES_PER_RUN` | `1` | `1` enables one multi-city search for the top verified route; `0` disables it. |
| `VERIFY_PRICE_TOLERANCE` | `50` | Maximum dollars per person that verified price may exceed the scout price. |
| `EXCEPTIONAL_PRICE_PREMIUM` | `75` | Maximum dollars above the airport target for a qualifying exceptional fare. |
| `PRICE_DROP_AMOUNT` | `100` | Minimum recent-observation decrease considered significant. |
| `TWO_STOP_PENALTY` | `15` | Ranking penalty for two-stop flights. |
| `PTO_MAX_WEEKDAYS` | `10` | Preferred weekday PTO budget used for ranking. |
| `PREFERRED_RETURN_BY` | `2027-04-15` | Preferred latest return date; later returns are penalized, not excluded. |
| `LATE_RETURN_PENALTY` | `10` | Ranking penalty for returning after the preferred date. |

The default budget allows up to 210 scout requests and reserves up to 40 requests for
verification. The existing three-origin schedule uses up to 180 scout requests per
30-day month. Actual monthly usage depends on optional verifications and multi-city
searches; scouts and verifications together can never exceed the hard budget. At the cap,
the tracker records and reports budget-skipped origins rather than making extra calls.
Increase the budget only if the SerpApi plan permits it.

SerpApi documents `stops=3` as a **request filter** allowing up to two stops; the actual
stop counts shown in notifications come from verified flight segments when available.
The multi-city `type=3` / `multi_city_json` parameters are documented by SerpApi. When a
promising round trip verifies, the tracker can spend at most one additional request on an
open-jaw route using the alternate Tokyo/Osaka hub. Disable this with
`OPEN_JAW_SEARCHES_PER_RUN=0` if request usage is more important than discovery.
See the [Google Flights API documentation](https://serpapi.com/google-flights-api) and
[Google Flights Deals documentation](https://serpapi.com/google-flights-deals-api) for the
supported passenger, return-token, stops, and multi-city request fields.

### Local run

```powershell
pip install -r requirements.txt
$env:SERPAPI_KEY = "..."
$env:NTFY_TOPIC = "my-secret-topic"
$env:DRY_RUN = "1"  # print alerts instead of pushing
python tracker.py
```

Optional variables can be set in the same PowerShell session, e.g.
`$env:SERPAPI_MONTHLY_BUDGET = "250"` and `$env:PTO_MAX_WEEKDAYS = "10"`.

### GitHub Actions

Add repository secrets `SERPAPI_KEY` and `NTFY_TOPIC`. The workflow at
`.github/workflows/track.yml` runs twice daily and persists the SQLite database.
Optional configuration can be added as repository variables with the names in the table
above; defaults are used otherwise. A run with API failures for every origin fails. A
partial failure is logged and the remaining origins are still processed. Legitimate
zero-result searches remain successful scans.

### Reports and history

Run `python report.py` to build `data/report.html`; use `python report.py --no-open` to
skip opening it. The report includes the latest scan health, cheapest latest observations,
verified versus scout prices, available search links, repeated-itinerary trends, PTO
estimates, and alert history. It distinguishes stored historical observations from
verified searches, which are not a live availability feed. If Actions runs the tracker,
pull the latest repository history before generating a local report.

Schema changes are applied with additive SQLite migrations. Existing observations and
alerts are not rewritten or deleted.

"""Build the mobile-friendly HTML report for the Ignav tracker (data/ignav.db)."""
import sqlite3
import sys
import webbrowser
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote

from ignav_tracker import DB_PATH, init_db
from report import chart

OUT = DB_PATH.parent / "ignav_report.html"

LATEST = """
SELECT * FROM (
  SELECT *,
    ROW_NUMBER() OVER w AS rn,
    MIN(price) OVER (PARTITION BY origin, dest, start_date, end_date) AS low,
    COUNT(*) OVER (PARTITION BY origin, dest, start_date, end_date) AS n
  FROM observations
  WINDOW w AS (
    PARTITION BY origin, dest, start_date, end_date
    ORDER BY observed_at DESC, id DESC
  )
) WHERE rn = 1 ORDER BY COALESCE(verified_price, price, 999999999), price
"""


def esc(value) -> str:
    return escape(str(value)) if value is not None else ""


def main() -> int:
    if not DB_PATH.exists():
        print(f"No database at {DB_PATH}. Run ignav_tracker.py first.")
        return 1
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    init_db(db)
    rows = db.execute(LATEST).fetchall()

    recurring = db.execute(
        """SELECT origin, dest, start_date, end_date, COUNT(*) AS n
           FROM observations GROUP BY origin, dest, start_date, end_date
           HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT 6"""
    ).fetchall()
    trends = {}
    for item in recurring:
        key = (item["origin"], item["dest"], item["start_date"], item["end_date"])
        label = f"{item['origin']}->{item['dest']} {item['start_date']} ({item['n']} obs.)"
        values = db.execute(
            """SELECT observed_at, COALESCE(verified_price, price) AS price
               FROM observations
               WHERE origin=? AND dest=? AND start_date=? AND end_date=?
               ORDER BY observed_at, id""",
            key,
        ).fetchall()
        trends[label] = [
            (datetime.fromisoformat(row["observed_at"]), row["price"])
            for row in values if row["price"] is not None
        ]

    latest_scans = db.execute(
        """SELECT s.* FROM scan_runs s JOIN (
             SELECT origin, MAX(id) AS id FROM scan_runs GROUP BY origin
           ) latest ON latest.id=s.id ORDER BY s.origin"""
    ).fetchall()

    option_rows = []
    for row in rows:
        booking = row["booking_link"]
        link_html = (
            f'<a href="{esc(booking)}" target="_blank" rel="noopener">book</a>'
            if booking else ""
        )
        gf_url = (
            "https://www.google.com/travel/flights?q=" +
            quote(f"Flights to {row['dest']} from {row['origin']} "
                  f"on {row['start_date']} through {row['end_date']}")
        )
        gf_html = f'<a href="{esc(gf_url)}" target="_blank" rel="noopener">check</a>'
        route = f"{row['origin']} → {row['dest']}"
        bookable = f"${row['verified_price']:,.0f}" if row["verified_price"] is not None else "—"
        provider = row["booking_provider"] or ""
        provider_type = row["booking_provider_type"] or ""
        provider_note = (
            f"{provider} ({provider_type})" if provider else
            ("no booking link" if row["verified_price"] is not None else "")
        )
        airlines = row["verified_airline"] or "—"
        stops = row["verified_stops"]
        stop_text = f"{stops} stop(s)" if stops is not None else "unknown stops"
        best_price = row["verified_price"] if row["verified_price"] is not None else row["price"]
        price_text = f"${row['price']:,.0f}" if row["price"] is not None else "—"

        observed = row["observed_at"]
        observed_text, observed_epoch = "—", ""
        if observed:
            try:
                observed_dt = datetime.fromisoformat(observed).astimezone()
                observed_text = observed_dt.strftime("%b %d %H:%M")
                observed_epoch = f"{observed_dt.timestamp():.0f}"
            except ValueError:
                observed_text = str(observed)

        option_rows.append(
            f'<tr data-start="{esc(row["start_date"])}" data-origin="{esc(row["origin"])}" '
            f'data-dest="{esc(row["dest"])}" data-nights="{row["nights"]}" '
            f'data-best="{best_price if best_price is not None else 0:.0f}" '
            f'data-observed="{observed_epoch}" '
            f'data-label="{esc(f"{route} · ${best_price:,.0f}/person · {airlines} · {stop_text}")}">'
            f"<td>{esc(route)}</td>"
            f"<td>{esc(row['start_date'])} → {esc(row['end_date'])}<small>{row['nights']} nights</small></td>"
            f"<td>{esc(observed_text)}</td>"
            f"<td>{price_text}<small>{esc(row['price_status'] or '')} / person</small></td>"
            f"<td><b>{bookable}</b><small>{esc(provider_note)}</small></td>"
            f"<td>{airlines}<small>{stop_text}</small></td>"
            f"<td>${row['low']:,.0f}</td><td>{row['n']}</td><td>{link_html}</td><td>{gf_html}</td></tr>"
        )

    departure_days = sorted({row["start_date"] for row in rows if row["start_date"]})
    day_options = "".join(
        f'<option value="{esc(day)}">{datetime.fromisoformat(day):%a %b %d, %Y}</option>'
        for day in departure_days
    )
    origins = sorted({row["origin"] for row in rows})
    dests = sorted({row["dest"] for row in rows})
    nights = sorted({row["nights"] for row in rows if row["nights"] is not None})
    origin_options = "".join(f'<option value="{esc(o)}">{esc(o)}</option>' for o in origins)
    dest_options = "".join(f'<option value="{esc(d)}">{esc(d)}</option>' for d in dests)
    nights_options = "".join(f'<option value="{n}">{n} nights</option>' for n in nights)

    scan_rows = "".join(
        f"<tr><td>{esc(row['origin'])}</td><td>{esc(row['status'])}</td>"
        f"<td>{row['raw_results']}</td><td>{row['valid_results']}</td>"
        f"<td>{esc(row['observed_at'])}</td><td>{esc(row['error'])}</td></tr>"
        for row in latest_scans
    )
    count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    filter_script = """<script>
(function () {
  var sel = document.getElementById("day-filter");
  var sortSel = document.getElementById("sort-select");
  var tbody = document.querySelector("#options tbody");
  var rows = Array.prototype.slice.call(document.querySelectorAll("#options tbody tr"));
  var banner = document.getElementById("best-deal");
  var pageInfo = document.getElementById("page-info");
  var prevBtn = document.getElementById("prev-page");
  var nextBtn = document.getElementById("next-page");
  var originSel = document.getElementById("origin-filter");
  var destSel = document.getElementById("dest-filter");
  var nightsSel = document.getElementById("nights-filter");
  var PAGE_SIZE = 25;
  var page = 0;
  function apply() {
    var day = sel.value;
    var sort = sortSel.value;
    var origin = originSel.value;
    var dest = destSel.value;
    var nights = nightsSel.value;
    var bestPrice = Infinity, bestLabel = "";
    var matched = rows.filter(function (tr) {
      var ok = (!day || tr.getAttribute("data-start") === day) &&
               (!origin || tr.getAttribute("data-origin") === origin) &&
               (!dest || tr.getAttribute("data-dest") === dest) &&
               (!nights || tr.getAttribute("data-nights") === nights);
      if (ok && day) {
        var price = parseFloat(tr.getAttribute("data-best"));
        if (!isNaN(price) && price < bestPrice) {
          bestPrice = price;
          bestLabel = tr.getAttribute("data-label");
        }
      }
      return ok;
    });
    matched.sort(function (a, b) {
      if (sort === "newest") {
        return (parseFloat(b.getAttribute("data-observed")) || 0) - (parseFloat(a.getAttribute("data-observed")) || 0);
      }
      return (parseFloat(a.getAttribute("data-best")) || 0) - (parseFloat(b.getAttribute("data-best")) || 0);
    });
    var totalPages = Math.max(1, Math.ceil(matched.length / PAGE_SIZE));
    if (page >= totalPages) { page = totalPages - 1; }
    if (page < 0) { page = 0; }
    var start = page * PAGE_SIZE;
    rows.forEach(function (tr) { tr.style.display = "none"; });
    matched.forEach(function (tr) { tbody.appendChild(tr); });
    matched.slice(start, start + PAGE_SIZE).forEach(function (tr) { tr.style.display = ""; });
    pageInfo.textContent = matched.length
      ? "Showing " + (start + 1) + "-" + Math.min(start + PAGE_SIZE, matched.length) + " of " + matched.length + " (page " + (page + 1) + " of " + totalPages + ")"
      : "No matching flights";
    prevBtn.disabled = page === 0;
    nextBtn.disabled = page >= totalPages - 1;
    if (day && bestLabel) {
      banner.textContent = "Best deal for " + sel.options[sel.selectedIndex].text + ": " + bestLabel;
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  }
  sel.addEventListener("change", function () { page = 0; apply(); });
  sortSel.addEventListener("change", function () { page = 0; apply(); });
  originSel.addEventListener("change", function () { page = 0; apply(); });
  destSel.addEventListener("change", function () { page = 0; apply(); });
  nightsSel.addEventListener("change", function () { page = 0; apply(); });
  prevBtn.addEventListener("click", function () { page -= 1; apply(); });
  nextBtn.addEventListener("click", function () { page += 1; apply(); });
  apply();
})();
</script>"""
    html = f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ignav flight tracker</title>
<style>
body{{font:15px system-ui,sans-serif;margin:20px;color:#172033}}h1,h2{{margin-bottom:.4em}}
.muted,small{{color:#667085}}small{{display:block}}section{{margin:28px 0}}
.table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:850px}}
th,td{{padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}
th{{background:#f3f4f6;position:sticky;top:0}}.chart svg{{width:100%;max-width:900px}}
.chart text{{font-size:11px;fill:#475467}}.legend{{display:flex;flex-wrap:wrap;gap:12px;margin-top:8px;font-size:12px}}
.status{{padding:2px 7px;border-radius:8px;background:#eef2ff}}a{{color:#175cd3}}
.filter-bar{{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:8px 0}}
select{{font:inherit;padding:6px 8px;border:1px solid #cbd5e1;border-radius:6px}}
.best-deal{{margin:10px 0;padding:10px 12px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px}}
.pager{{display:flex;align-items:center;gap:10px;margin:8px 0}}
.pager button{{font:inherit;padding:5px 10px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;cursor:pointer}}
.pager button:disabled{{opacity:.5;cursor:default}}
</style>
<h1>Ignav flight tracker</h1>
<p class="muted">{count} historical observations. Generated {generated}. Source: Ignav API (ignav.com).</p>
<p>Fares come from the Ignav API and are not live booking guarantees. "Price" is the calendar cheapest-date fare; "Bookable"
is a detailed round-trip fare with a booking link, labeled with its seller (airline or third-party OTA). Third-party OTA
fares can be bait pricing — fares far below the calendar price are not shown as bookable.</p>
<section><h2>Latest scan status</h2><div class="table-wrap"><table><thead><tr><th>Origin</th><th>Status</th>
<th>Raw</th><th>Valid</th><th>Scanned</th><th>Error</th></tr></thead><tbody>{scan_rows}</tbody></table></div></section>
<section><h2>Find the best deal by departure day</h2><p class="muted">Filter by departure airport, arrival airport, number of nights, or departure day. The "Observed" column shows when each fare was last pulled. Use Sort to put the most recently pulled flights first.</p>
<div class="filter-bar"><label for="origin-filter" class="muted">From:</label>
<select id="origin-filter"><option value="">All airports</option>{origin_options}</select>
<label for="dest-filter" class="muted">To:</label>
<select id="dest-filter"><option value="">All airports</option>{dest_options}</select>
<label for="nights-filter" class="muted">Nights:</label>
<select id="nights-filter"><option value="">Any</option>{nights_options}</select>
<label for="day-filter" class="muted">Departure day:</label>
<select id="day-filter"><option value="">All departure days</option>{day_options}</select>
<label for="sort-select" class="muted">Sort:</label>
<select id="sort-select"><option value="price">Cheapest first</option><option value="newest">Most recently pulled first</option></select></div>
<div class="pager"><button id="prev-page" type="button">← Prev</button><span id="page-info" class="muted"></span><button id="next-page" type="button">Next →</button></div>
<p id="best-deal" class="best-deal" hidden></p>
<div class="table-wrap"><table id="options"><thead><tr><th>Route</th><th>Dates</th><th>Observed</th><th>Price</th><th>Bookable</th>
<th>Airline / stops</th><th>Lowest recorded</th><th>Obs.</th><th>Book</th><th>Google</th></tr></thead>
<tbody>{''.join(option_rows)}</tbody></table></div></section>
<section><h2>Repeated itinerary price trends</h2>{chart(trends)}</section>
{filter_script}
</html>"""
    OUT.write_text(html, encoding="utf-8")
    db.close()
    print(f"Wrote {OUT}")
    if "--no-open" not in sys.argv:
        webbrowser.open(OUT.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())

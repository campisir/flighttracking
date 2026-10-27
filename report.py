"""Build the mobile-friendly HTML history report from SQLite."""
import sqlite3
import sys
import webbrowser
from datetime import datetime, timezone
from html import escape

from tracker import DB_PATH, init_db

OUT = DB_PATH.parent / "report.html"

LATEST = """
SELECT * FROM (
  SELECT *,
    ROW_NUMBER() OVER w AS rn,
    MIN(price) OVER (PARTITION BY origin, dest, start_date, end_date, itinerary_type) AS low,
    COUNT(*) OVER (PARTITION BY origin, dest, start_date, end_date, itinerary_type) AS n
  FROM observations
  WINDOW w AS (
    PARTITION BY origin, dest, start_date, end_date, itinerary_type
    ORDER BY observed_at DESC, id DESC
  )
) WHERE rn = 1 ORDER BY COALESCE(verified_price, price), price
"""


def chart(series: dict[str, list[tuple[datetime, float]]]) -> str:
    points = [point for values in series.values() for point in values]
    if not points:
        return "<p>No repeated itinerary observations yet.</p>"
    width, height, pad = 900, 300, 55
    start, end = min(t for t, _ in points), max(t for t, _ in points)
    low, high = min(price for _, price in points), max(price for _, price in points)
    time_span, price_span = (end - start).total_seconds() or 1, (high - low) or 1

    def xy(timestamp, price):
        return (pad + (timestamp - start).total_seconds() / time_span * (width - 2 * pad),
                height - pad - (price - low) / price_span * (height - 2 * pad))

    colors = ("#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2")
    output = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Repeated itinerary price trends">']
    for index in range(5):
        price = low + price_span * index / 4
        y = xy(start, price)[1]
        output.append(
            f'<line x1="{pad}" x2="{width-pad}" y1="{y:.0f}" y2="{y:.0f}" stroke="#e5e7eb"/>'
            f'<text x="{pad-6}" y="{y+4:.0f}" text-anchor="end">${price:,.0f}</text>'
        )
    for index, (label, values) in enumerate(series.items()):
        color = colors[index % len(colors)]
        coordinates = [xy(timestamp, price) for timestamp, price in values]
        polyline = " ".join(f"{x:.0f},{y:.0f}" for x, y in coordinates)
        output.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{polyline}"/>')
        output.extend(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="3" fill="{color}"/>' for x, y in coordinates)
    output.append(
        f'<text x="{pad}" y="{height-14}">{start:%b %d}</text>'
        f'<text x="{width-pad}" y="{height-14}" text-anchor="end">{end:%b %d}</text></svg>'
    )
    legend = "".join(
        f'<span style="color:{colors[i % len(colors)]}">{escape(label)}</span>'
        for i, label in enumerate(series)
    )
    return f'<div class="chart">{"".join(output)}</div><div class="legend">{legend}</div>'


def esc(value) -> str:
    return escape(str(value)) if value is not None else ""


def main() -> int:
    if not DB_PATH.exists():
        print(f"No database at {DB_PATH}. Run tracker.py first.")
        return 1
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    init_db(db)
    rows = db.execute(LATEST).fetchall()

    recurring = db.execute(
        """SELECT origin, dest, start_date, end_date, itinerary_type, COUNT(*) AS n
           FROM observations GROUP BY origin, dest, start_date, end_date, itinerary_type
           HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT 6"""
    ).fetchall()
    trends = {}
    for item in recurring:
        key = (item["origin"], item["dest"], item["start_date"], item["end_date"], item["itinerary_type"])
        label = (f"{item['origin']}->{item['dest']} {item['start_date']} "
                 f"({item['itinerary_type']}, {item['n']} obs.)")
        values = db.execute(
            """SELECT observed_at, price FROM observations
               WHERE origin=? AND dest=? AND start_date=? AND end_date=? AND itinerary_type=?
               ORDER BY observed_at, id""",
            key,
        ).fetchall()
        trends[label] = [(datetime.fromisoformat(row["observed_at"]), row["price"]) for row in values]

    latest_scans = db.execute(
        """SELECT s.* FROM scan_runs s JOIN (
             SELECT origin, MAX(id) AS id FROM scan_runs GROUP BY origin
           ) latest ON latest.id=s.id ORDER BY s.origin"""
    ).fetchall()
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    request_count = db.execute(
        "SELECT COUNT(*) FROM api_requests WHERE requested_at LIKE ?", (f"{current_month}%",)
    ).fetchone()[0]
    request_budget = int(__import__("os").environ.get("SERPAPI_MONTHLY_BUDGET", "250"))
    alerts = db.execute(
        "SELECT * FROM alerts ORDER BY alerted_at DESC LIMIT 30"
    ).fetchall()

    option_rows = []
    for row in rows:
        link = row["verified_link"] or row["link"]
        link_html = f'<a href="{esc(link)}" target="_blank" rel="noopener">inspect</a>' if link else ""
        route = (f"{row['origin']} → {row['dest']}" if row["itinerary_type"] != "open_jaw"
                 else f"{row['origin']} → {row['dest'].replace('/', ' / ')} → {row['origin']}")
        verified = f"${row['verified_price']:,.0f}" if row["verified_price"] is not None else "not verified"
        airlines = row["verified_airline"] or row["airline"] or "—"
        stops = row["verified_stops"] if row["verified_stops"] is not None else row["stops"]
        stop_text = f"{stops} stop(s)" if stops is not None else "unknown stops"
        pto = (
            f"~{row['pto_weekdays']}-{row['pto_weekdays_max']} weekdays (approx.)"
            if row["pto_weekdays"] is not None else "—"
        )
        observed = row["observed_at"]
        observed_text, observed_epoch = "—", ""
        if observed:
            try:
                observed_dt = datetime.fromisoformat(observed).astimezone()
                observed_text = observed_dt.strftime("%b %d %H:%M")
                observed_epoch = f"{observed_dt.timestamp():.0f}"
            except ValueError:
                observed_text = str(observed)
        best_price = row["verified_price"] if row["verified_price"] is not None else row["price"]
        option_rows.append(
            f'<tr data-start="{esc(row["start_date"])}" data-best="{best_price:.0f}" '
            f'data-observed="{observed_epoch}" '
            f'data-label="{esc(f"{route} · ${best_price:,.0f}/person · {airlines} · {stop_text}")}">'
            f"<td>{esc(route)}<small>{esc(row['dest_name'])}</small></td>"
            f"<td>{esc(row['start_date'])} → {esc(row['end_date'])}<small>{row['nights']} nights</small></td>"
            f"<td>{esc(observed_text)}</td>"
            f"<td>${row['price']:,.0f}<small>scout / person</small></td>"
            f"<td><b>{verified}</b><small>verified search / person</small></td>"
            f"<td>${row['low']:,.0f}</td><td>{esc(airlines)}<small>{stop_text}</small></td>"
            f"<td>{pto}</td><td>{row['n']}</td><td>{link_html}</td></tr>"
        )

    departure_days = sorted({row["start_date"] for row in rows})
    day_options = "".join(
        f'<option value="{esc(day)}">{datetime.fromisoformat(day):%a %b %d, %Y}</option>'
        for day in departure_days
    )

    scan_rows = "".join(
        f"<tr><td>{esc(row['origin'])}</td><td>{esc(row['status'])}</td>"
        f"<td>{row['raw_results']}</td><td>{row['valid_results']}</td>"
        f"<td>{esc(row['observed_at'])}</td><td>{esc(row['error'])}</td></tr>"
        for row in latest_scans
    )
    alert_rows = "".join(
        f"<tr><td>{esc(row['alerted_at'])}</td><td>{esc(row['origin'])} → {esc(row['dest'])}</td>"
        f"<td>{esc(row['start_date'])} → {esc(row['end_date'])}</td>"
        f"<td>${row['verified_price']:,.0f}/person</td><td>{esc(row['reasons'])}</td></tr>"
        if row["verified_price"] is not None else
        f"<tr><td>{esc(row['alerted_at'])}</td><td>{esc(row['origin'])} → {esc(row['dest'])}</td>"
        f"<td>{esc(row['start_date'])} → {esc(row['end_date'])}</td>"
        f"<td>Legacy alert (${row['price']:,.0f})</td><td>reason not recorded</td></tr>"
        for row in alerts
    )
    count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    day_filter_script = """<script>
(function () {
  var sel = document.getElementById("day-filter");
  var sortSel = document.getElementById("sort-select");
  var tbody = document.querySelector("#options tbody");
  var rows = Array.prototype.slice.call(document.querySelectorAll("#options tbody tr"));
  var banner = document.getElementById("best-deal");
  var pageInfo = document.getElementById("page-info");
  var prevBtn = document.getElementById("prev-page");
  var nextBtn = document.getElementById("next-page");
  var PAGE_SIZE = 25;
  var page = 0;
  function apply() {
    var day = sel.value;
    var sort = sortSel.value;
    var bestPrice = Infinity, bestLabel = "";
    var matched = rows.filter(function (tr) {
      var ok = !day || tr.getAttribute("data-start") === day;
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
  prevBtn.addEventListener("click", function () { page -= 1; apply(); });
  nextBtn.addEventListener("click", function () { page += 1; apply(); });
  apply();
})();
</script>"""
    html = f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Japan flight tracker</title>
<style>
body{{font:15px system-ui,sans-serif;margin:20px;color:#172033}}h1,h2{{margin-bottom:.4em}}
.muted,small{{color:#667085}}small{{display:block}}section{{margin:28px 0}}
.table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:850px}}
th,td{{padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}}
th{{background:#f3f4f6;position:sticky;top:0}}.chart svg{{width:100%;max-width:900px}}
.chart text{{font-size:11px;fill:#475467}}.legend{{display:flex;flex-wrap:wrap;gap:12px;margin-top:8px;font-size:12px}}
.status{{padding:2px 7px;border-radius:8px;background:#eef2ff}}a{{color:#175cd3}}
.filter-bar{{display:flex;align-items:center;gap:10px;margin:8px 0}}
select{{font:inherit;padding:6px 8px;border:1px solid #cbd5e1;border-radius:6px}}
.best-deal{{margin:10px 0;padding:10px 12px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px}}
.pager{{display:flex;align-items:center;gap:10px;margin:8px 0}}
.pager button{{font:inherit;padding:5px 10px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;cursor:pointer}}
.pager button:disabled{{opacity:.5;cursor:default}}
</style>
<h1>Japan flight tracker</h1>
<p class="muted">{count} historical observations. Generated {generated}. Monthly SerpApi calls: {request_count}/{request_budget}.</p>
<p>Historical scout observations are not live fares. Only rows with a verified-search price were checked in a detailed Google Flights search;
that search is not a booking guarantee. PTO figures count weekdays from itinerary dates and are approximate because actual flight times
and international date-line arrivals are unavailable.</p>
<section><h2>Latest scan status</h2><div class="table-wrap"><table><thead><tr><th>Origin</th><th>Status</th>
<th>Raw</th><th>Valid Japan</th><th>Scanned</th><th>Error</th></tr></thead><tbody>{scan_rows}</tbody></table></div></section>
<section><h2>Find the best deal by departure day</h2><p class="muted">Pick a departure day to see every itinerary leaving that day. The "Observed" column shows when each fare was last pulled. Use Sort to put the most recently pulled flights first. Rows are the latest stored observation per itinerary, not a live availability feed.</p>
<div class="filter-bar"><label for="day-filter" class="muted">Departure day:</label>
<select id="day-filter"><option value="">All departure days</option>{day_options}</select>
<label for="sort-select" class="muted">Sort:</label>
<select id="sort-select"><option value="price">Cheapest first</option><option value="newest">Most recently pulled first</option></select></div>
<div class="pager"><button id="prev-page" type="button">← Prev</button><span id="page-info" class="muted"></span><button id="next-page" type="button">Next →</button></div>
<p id="best-deal" class="best-deal" hidden></p>
<div class="table-wrap"><table id="options"><thead><tr><th>Route</th><th>Dates</th><th>Observed</th><th>Scout</th><th>Verified</th>
<th>Lowest recorded</th><th>Airline / stops</th><th>PTO estimate</th><th>Obs.</th><th>Link</th></tr></thead>
<tbody>{''.join(option_rows)}</tbody></table></div></section>
<section><h2>Repeated itinerary price trends</h2>{chart(trends)}</section>
<section><h2>Deal-alert history</h2><div class="table-wrap"><table><thead><tr><th>Sent</th><th>Route</th>
<th>Dates</th><th>Verified price</th><th>Reason</th></tr></thead><tbody>{alert_rows}</tbody></table></div></section>
{day_filter_script}
</html>"""
    OUT.write_text(html, encoding="utf-8")
    db.close()
    print(f"Wrote {OUT}")
    if "--no-open" not in sys.argv:
        webbrowser.open(OUT.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())

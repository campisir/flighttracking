"""Build data/report.html from the price database and open it in the browser."""
import sqlite3
import sys
import webbrowser
from datetime import datetime
from html import escape

from tracker import DB_PATH

OUT = DB_PATH.parent / "report.html"
COLORS = {"PNS": "#2563eb", "TPA": "#16a34a", "MCO": "#dc2626"}

LATEST = """
SELECT * FROM (
  SELECT *,
    ROW_NUMBER() OVER w AS rn,
    MIN(price) OVER (PARTITION BY origin, dest, start_date, end_date) AS low,
    COUNT(*) OVER (PARTITION BY origin, dest, start_date, end_date) AS n
  FROM observations
  WINDOW w AS (PARTITION BY origin, dest, start_date, end_date ORDER BY observed_at DESC)
) WHERE rn = 1 ORDER BY price
"""


def chart(series: dict[str, list[tuple[datetime, float]]]) -> str:
    pts = [p for s in series.values() for p in s]
    if not pts:
        return ""
    w, h, pad = 900, 300, 50
    t0, t1 = min(t for t, _ in pts), max(t for t, _ in pts)
    lo, hi = min(p for _, p in pts), max(p for _, p in pts)
    span_t, span_p = (t1 - t0).total_seconds() or 1, (hi - lo) or 1

    def xy(t, p):
        return pad + (t - t0).total_seconds() / span_t * (w - 2 * pad), h - pad - (p - lo) / span_p * (h - 2 * pad)

    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" style="max-width:{w}px">']
    for i in range(5):
        p = lo + span_p * i / 4
        y = xy(t0, p)[1]
        out.append(f'<line x1="{pad}" x2="{w - pad}" y1="{y:.0f}" y2="{y:.0f}" stroke="#e5e7eb"/>'
                   f'<text x="{pad - 6}" y="{y + 4:.0f}" font-size="11" text-anchor="end">${p:,.0f}</text>')
    out.append(f'<text x="{pad}" y="{h - 20}" font-size="11">{t0:%b %d %H:%M}</text>'
               f'<text x="{w - pad}" y="{h - 20}" font-size="11" text-anchor="end">{t1:%b %d %H:%M}</text>')
    for i, (origin, s) in enumerate(sorted(series.items())):
        c = COLORS.get(origin, "#6b7280")
        xs = [xy(t, p) for t, p in s]
        out.append(f'<polyline fill="none" stroke="{c}" stroke-width="2" points="{" ".join(f"{x:.0f},{y:.0f}" for x, y in xs)}"/>')
        out += [f'<circle cx="{x:.0f}" cy="{y:.0f}" r="3" fill="{c}"/>' for x, y in xs]
        out.append(f'<text x="{pad + 10 + i * 60}" y="16" font-size="12" fill="{c}" font-weight="bold">{escape(origin)}</text>')
    return "".join(out) + "</svg>"


def main() -> int:
    if not DB_PATH.exists():
        print(f"No database at {DB_PATH}. Run tracker.py first.")
        return 1
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(LATEST).fetchall()
    series: dict[str, list] = {}
    for r in db.execute("SELECT observed_at, origin, MIN(price) p FROM observations GROUP BY observed_at, origin ORDER BY observed_at"):
        series.setdefault(r["origin"], []).append((datetime.fromisoformat(r["observed_at"]), r["p"]))
    runs = db.execute("SELECT COUNT(DISTINCT observed_at) FROM observations").fetchone()[0]

    body = []
    for r in rows:
        link = r["link"] if (r["link"] or "").startswith("https://www.google.com/") else None
        disc = f"{r['discount_pct']:.0f}%" if r["discount_pct"] else ""
        anchor = f'<a href="{escape(link)}" target=_blank>open</a>' if link else ""
        body.append(
            "<tr>"
            f"<td>{escape(r['origin'])} &rarr; {escape(r['dest'])} <small>{escape(r['dest_name'] or '')}</small></td>"
            f"<td>{r['start_date']} &rarr; {r['end_date']} <small>({r['nights']}n)</small></td>"
            f"<td><b>${r['price']:,.0f}</b></td><td>${r['low']:,.0f}</td>"
            f"<td>{'$' + format(r['average_price'], ',.0f') if r['average_price'] else ''}</td><td>{disc}</td>"
            f"<td>{escape(r['airline'] or '')} <small>{r['stops']} stop(s)</small></td>"
            f"<td>{r['observed_at'][:16].replace('T', ' ')}</td><td>{r['n']}</td>"
            f"<td>{anchor}</td></tr>"
        )
    html = f"""<!doctype html><meta charset=utf-8><title>Japan flights</title>
<style>body{{font:14px system-ui;margin:24px}}table{{border-collapse:collapse}}th,td{{padding:6px 10px;border-bottom:1px solid #e5e7eb;text-align:left}}
th{{background:#f3f4f6}}small{{color:#6b7280}}</style>
<h1>Japan flights</h1><p>{len(rows)} itineraries, {runs} runs. Prices are per person. Generated {datetime.now():%Y-%m-%d %H:%M}.</p>
<h2>Cheapest fare per run</h2>{chart(series)}
<h2>Itineraries (cheapest latest price first)</h2>
<table><tr><th>Route<th>Dates<th>Latest<th>Lowest seen<th>Typical<th>Below typical<th>Airline<th>Last seen<th>Obs.<th>Link</tr>{''.join(body)}</table>"""
    OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT}")
    if "--no-open" not in sys.argv:
        webbrowser.open(OUT.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())

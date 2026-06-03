"""Generate a self-contained HTML dashboard (no server, no external assets) from
the trades spreadsheet: headline performance stats, a by-confidence breakdown, a
cumulative-R equity curve (inline SVG), and a table of every recommendation.
"""

import html
from datetime import datetime

import config
from src import tracker

_CSS = """
:root{--bg:#0e1117;--card:#171b22;--line:#262c36;--fg:#e6edf3;--mut:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:32px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:28px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.card .k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:26px;font-weight:600;margin-top:6px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin-bottom:24px}
section h2{font-size:15px;margin:0 0 14px;color:var(--fg)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}
.WIN{background:rgba(63,185,80,.15);color:var(--green)}
.LOSS{background:rgba(248,81,73,.15);color:var(--red)}
.OPEN{background:rgba(88,166,255,.15);color:var(--blue)}
.NO_TRADE{background:rgba(139,148,158,.12);color:var(--mut)}
.BREAKEVEN,.SKIPPED,.EXPIRED{background:rgba(210,153,34,.15);color:var(--amber)}
.buy{color:var(--green)}.sell{color:var(--red)}.pos{color:var(--green)}.neg{color:var(--red)}
.foot{color:var(--mut);font-size:12px;margin-top:24px}
"""


def _f(v, nd=2):
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def _stat_cards(stats: dict) -> str:
    wr = stats["win_rate"]
    wr_txt = f"{wr*100:.0f}%" if wr is not None else "-"
    exp = stats["expectancy_r"]
    exp_txt = f"{exp:+.2f}R" if exp is not None else "-"
    cards = [
        ("Recommendations", stats["total_recommendations"]),
        ("Actionable (YES)", stats["actionable"]),
        ("Open", stats["open"]),
        ("Closed", stats["closed"]),
        ("Win rate", wr_txt),
        ("Wins / Losses", f'{stats["wins"]} / {stats["losses"]}'),
        ("Expectancy", exp_txt),
    ]
    return "".join(
        f'<div class="card"><div class="k">{html.escape(str(k))}</div>'
        f'<div class="v">{html.escape(str(v))}</div></div>'
        for k, v in cards
    )


def _by_confidence(rows) -> str:
    buckets = {}
    for r in rows:
        if r.get("status") not in ("WIN", "LOSS"):
            continue
        try:
            c = int(float(r.get("confidence")))
        except (TypeError, ValueError):
            continue
        b = buckets.setdefault(c, {"w": 0, "l": 0})
        b["w" if r["status"] == "WIN" else "l"] += 1
    if not buckets:
        return "<p style='color:var(--mut)'>No closed trades yet — win-rate-by-confidence appears here once outcomes are recorded.</p>"
    out = ["<table><tr><th>Confidence</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win rate</th></tr>"]
    for c in sorted(buckets, reverse=True):
        b = buckets[c]
        n = b["w"] + b["l"]
        wr = b["w"] / n if n else 0
        out.append(
            f'<tr><td>{c}/10</td><td class="num">{n}</td><td class="num">{b["w"]}</td>'
            f'<td class="num">{b["l"]}</td><td class="num">{wr*100:.0f}%</td></tr>'
        )
    out.append("</table>")
    return "".join(out)


def _equity_curve(rows) -> str:
    seq = []
    cum = 0.0
    for r in sorted(rows, key=lambda x: (x.get("closed_at") or "")):
        if r.get("status") not in ("WIN", "LOSS", "BREAKEVEN"):
            continue
        try:
            cum += float(r.get("r_multiple"))
        except (TypeError, ValueError):
            continue
        seq.append(cum)
    if len(seq) < 2:
        return "<p style='color:var(--mut)'>Equity curve appears once at least two trades are closed.</p>"

    w, h, pad = 640, 180, 24
    lo, hi = min(0, min(seq)), max(0, max(seq))
    span = (hi - lo) or 1
    n = len(seq)
    pts = []
    for i, v in enumerate(seq):
        x = pad + i * (w - 2 * pad) / (n - 1)
        y = h - pad - (v - lo) / span * (h - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    zero_y = h - pad - (0 - lo) / span * (h - 2 * pad)
    colour = "var(--green)" if seq[-1] >= 0 else "var(--red)"
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}">'
        f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{w-pad}" y2="{zero_y:.1f}" stroke="var(--line)" stroke-dasharray="4 4"/>'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="{colour}" stroke-width="2"/>'
        f'<text x="{pad}" y="16" fill="var(--mut)" font-size="11">Cumulative R (closed trades): {seq[-1]:+.2f}R</text>'
        f"</svg>"
    )


def _rows_table(rows) -> str:
    out = ["<table><tr><th>ID</th><th>Date</th><th>Pair</th><th>Dir</th><th>Conf</th>"
           "<th>T</th><th>F</th><th>S</th><th>P</th><th>M</th><th>Entry</th><th>Stop</th>"
           "<th>Target</th><th>R:R</th><th>Trade</th><th>Status</th><th>R</th><th>Pips</th></tr>"]
    for r in sorted(rows, key=lambda x: int(x.get("id", 0)), reverse=True):
        d = (r.get("direction") or "").upper()
        dcls = "buy" if d == "BUY" else "sell" if d == "SELL" else ""
        status = r.get("status") or ""
        rm = r.get("r_multiple")
        rcls = ""
        try:
            rcls = "pos" if float(rm) > 0 else "neg" if float(rm) < 0 else ""
        except (TypeError, ValueError):
            pass
        cells = [
            r.get("id", ""), (r.get("timestamp") or "")[:10], r.get("pair", ""),
            f'<span class="{dcls}">{html.escape(d)}</span>',
            r.get("confidence", ""), r.get("technical", ""), r.get("fundamental", ""),
            r.get("sentiment", ""), r.get("positioning", ""), r.get("macro", ""),
            r.get("entry", ""), r.get("stop_loss", ""), r.get("target", ""),
            (r.get("reward_risk") or ""), r.get("trade_this", ""),
            f'<span class="pill {status}">{html.escape(status)}</span>',
            f'<span class="{rcls}">{html.escape(str(rm or ""))}</span>',
            r.get("pips", ""),
        ]
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def generate() -> str:
    from src import learning
    rows = tracker.load()
    stats = learning.compute_stats()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>forex-ai dashboard</title><style>{_CSS}</style></head><body>
<h1>forex-ai &mdash; performance dashboard</h1>
<p class="sub">Generated {now} &middot; {len(rows)} recommendations logged &middot; NOT financial advice</p>
<div class="grid">{_stat_cards(stats)}</div>
<section><h2>Equity curve</h2>{_equity_curve(rows)}</section>
<section><h2>Win rate by confidence score</h2>{_by_confidence(rows)}</section>
<section><h2>All recommendations</h2>{_rows_table(rows)}</section>
<p class="foot">Columns T/F/S/P/M = technical, fundamental, sentiment, positioning, macro scores.
Record an outcome with: <code>python main.py --close ID WIN|LOSS [exit_price]</code></p>
</body></html>"""

    config.DASHBOARD_HTML.write_text(body, encoding="utf-8")
    return str(config.DASHBOARD_HTML)

"""Generate a self-contained HTML dashboard (no server, no external assets) from
the trades spreadsheet: headline performance stats, a by-confidence breakdown, a
cumulative-R equity curve (inline SVG), and a table of every recommendation.
Active YES-trade setups are shown in prominent green alert cards at the very top.
"""

import html
import sys
from datetime import datetime

import config
from src import tracker

# ---------------------------------------------------------------------------
# Session windows in NZT (NZST = UTC+12) keyed by currency.
# Priority order: if a pair contains multiple currencies, the first match wins.
# ---------------------------------------------------------------------------
_SESSION_NZT = {
    "EUR": ("London",   "7pm – 11pm NZT"),
    "GBP": ("London",   "7pm – 11pm NZT"),
    "CHF": ("London",   "7pm – 11pm NZT"),
    "JPY": ("Tokyo",    "12pm – 5pm NZT"),
    "USD": ("New York", "1am – 5am NZT"),
    "CAD": ("New York", "1am – 5am NZT"),
    "AUD": ("Sydney",   "9am – 2pm NZT"),
    "NZD": ("Sydney",   "9am – 2pm NZT"),
}
_SESSION_PRIORITY = ["EUR", "GBP", "CHF", "AUD", "NZD", "JPY", "USD", "CAD"]


def _session_time(pair: str) -> tuple:
    """Return (session_name, nzt_range) for the pair's primary trading session."""
    cleaned = pair.upper().replace("/", "").replace("-", "")
    base = cleaned[:3]
    quote = cleaned[3:6] if len(cleaned) >= 6 else ""
    for ccy in _SESSION_PRIORITY:
        if ccy in (base, quote):
            return _SESSION_NZT[ccy]
    return "London", "7pm – 11pm NZT"


_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Cascadia Mono','Liberation Mono',monospace"

_CSS = """
:root{--bg:#05070a;--card:#0d1117;--line:#1c232d;--fg:#dbe4ee;--mut:#6b7686;
--green:#00e08c;--red:#ff4d4f;--amber:#e0a930;--blue:#4c9eff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:12.5px/1.45 __MONO__;padding:18px 22px 40px;letter-spacing:.01em}
h1{font-size:17px;margin:0 0 2px;font-weight:700;letter-spacing:.02em;text-transform:uppercase}
.sub{color:var(--mut);margin:0 0 14px;font-size:11px}

/* ── Top ticker strip ────────────────────────────────────────────────────── */
.ticker{display:flex;flex-wrap:wrap;gap:0;background:var(--card);
  border:1px solid var(--line);border-radius:6px;margin-bottom:18px;overflow:hidden}
.tick{flex:1 1 110px;padding:8px 14px;border-right:1px solid var(--line);min-width:100px}
.tick:last-child{border-right:none}
.tick-k{display:block;color:var(--mut);font-size:9.5px;text-transform:uppercase;
  letter-spacing:.08em;margin-bottom:3px}
.tick-v{display:block;font-size:17px;font-weight:700;font-variant-numeric:tabular-nums}

/* ── Group headers (real fund / virtual books / diagnostics / trade log) ──── */
.group-header{display:flex;align-items:baseline;gap:10px;margin:26px 0 10px;
  padding-bottom:6px;border-bottom:2px solid var(--line)}
.group-header:first-of-type{margin-top:4px}
.group-title{font-size:13px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--blue)}
.group-sub{color:var(--mut);font-size:11px}

.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:8px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px 12px}
.card .k{color:var(--mut);font-size:9.5px;text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:19px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}
section{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:14px 16px;margin-bottom:14px}
section h2{font-size:12px;margin:0 0 10px;color:var(--fg);text-transform:uppercase;
  letter-spacing:.06em;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:11.5px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:600;font-size:9.5px;text-transform:uppercase;letter-spacing:.05em}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{padding:1px 7px;border-radius:3px;font-size:10px;font-weight:700}
.WIN{background:rgba(63,185,80,.15);color:var(--green)}
.LOSS{background:rgba(248,81,73,.15);color:var(--red)}
.OPEN{background:rgba(88,166,255,.15);color:var(--blue)}
.NO_TRADE{background:rgba(139,148,158,.12);color:var(--mut)}
.BREAKEVEN,.SKIPPED,.EXPIRED{background:rgba(210,153,34,.15);color:var(--amber)}
.buy{color:var(--green)}.sell{color:var(--red)}.pos{color:var(--green)}.neg{color:var(--red)}
.foot{color:var(--mut);font-size:12px;margin-top:24px}

/* ── Risk profile section ──────────────────────────────────────────────────── */
.risk-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:18px}
.risk-card{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:14px}
.risk-card .k{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}
.risk-card .v{font-size:20px;font-weight:700}
.mode-normal{color:var(--blue)}.mode-enhanced{color:var(--green)}.mode-reduced{color:var(--amber)}
.mode-streak_protection,.mode-capital_protection{color:var(--red)}
.risk-bar-wrap{background:var(--line);border-radius:4px;height:8px;overflow:hidden;margin-top:6px}
.risk-bar{height:100%;border-radius:4px;background:var(--green);transition:width .3s}
.risk-bar.warn{background:var(--amber)}.risk-bar.danger{background:var(--red)}
.risk-warn{background:rgba(248,81,73,.10);border:1px solid var(--red);border-radius:8px;
  padding:10px 14px;font-size:13px;color:var(--red);margin-bottom:10px}

/* ── Learning feed ─────────────────────────────────────────────────────────── */
.learn-list{list-style:none;margin:0;padding:0}
.learn-item{padding:10px 0;border-bottom:1px solid var(--line);font-size:13px;
  line-height:1.5;display:flex;gap:12px;align-items:flex-start}
.learn-item:last-child{border-bottom:none}
.src-tag{padding:2px 8px;border-radius:12px;font-size:10px;font-weight:700;
  letter-spacing:.06em;white-space:nowrap;flex-shrink:0;margin-top:2px;text-transform:uppercase}
.src-seed{background:rgba(88,166,255,.15);color:var(--blue)}
.src-auto{background:rgba(210,153,34,.15);color:var(--amber)}
.src-outcome{background:rgba(63,185,80,.15);color:var(--green)}
.src-user{background:rgba(139,148,158,.15);color:var(--mut)}
.learn-pattern{font-weight:600;color:var(--fg);margin-bottom:2px}
.learn-outcome{color:var(--mut);font-size:12px}

/* ── YES-trade alert cards ────────────────────────────────────────────────── */
.alert-section{margin-bottom:32px}
.alert-section-title{color:var(--green);font-size:17px;font-weight:700;margin:0 0 16px;
  display:flex;align-items:center;gap:8px}
.alert-card{
  background:linear-gradient(135deg,rgba(63,185,80,.11) 0%,rgba(63,185,80,.04) 100%);
  border:2px solid var(--green);border-radius:14px;padding:24px 28px;margin-bottom:16px;
  box-shadow:0 0 40px rgba(63,185,80,.20),inset 0 1px 0 rgba(63,185,80,.15)}
.alert-head{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:20px}
.alert-badge{background:var(--green);color:#0a0f14;font-weight:900;font-size:11px;
  text-transform:uppercase;letter-spacing:.12em;padding:5px 14px;border-radius:20px;
  white-space:nowrap}
.alert-pair{font-size:26px;font-weight:800;color:var(--fg);letter-spacing:-.02em}
.alert-dir{font-size:22px;font-weight:800}
.alert-dir.buy{color:var(--green)}.alert-dir.sell{color:var(--red)}
.alert-meta{font-size:12px;color:var(--mut);margin-left:auto;text-align:right;
  line-height:1.6}
.alert-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
  gap:14px;margin-bottom:18px}
.alert-item .k{color:var(--mut);font-size:11px;text-transform:uppercase;
  letter-spacing:.05em;margin-bottom:4px}
.alert-item .v{font-size:18px;font-weight:700;color:var(--fg)}
.alert-item .v.red{color:var(--red)}.alert-item .v.grn{color:var(--green)}
.alert-time{background:rgba(63,185,80,.09);border:1px solid rgba(63,185,80,.28);
  border-radius:8px;padding:12px 16px;font-size:13px;font-weight:600;
  color:var(--green);display:flex;align-items:center;gap:10px}

/* ── Watch-list cards (amber) ─────────────────────────────────────────────── */
.watch-section{margin-bottom:32px}
.watch-section-title{color:var(--amber);font-size:17px;font-weight:700;margin:0 0 16px;
  display:flex;align-items:center;gap:8px}
.watch-card{
  background:linear-gradient(135deg,rgba(210,153,34,.10) 0%,rgba(210,153,34,.03) 100%);
  border:2px solid var(--amber);border-radius:14px;padding:20px 24px;margin-bottom:14px}
.watch-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.watch-badge{background:var(--amber);color:#0a0f14;font-weight:900;font-size:11px;
  text-transform:uppercase;letter-spacing:.12em;padding:4px 12px;border-radius:20px}
.watch-pair{font-size:22px;font-weight:800;color:var(--fg)}
.watch-dir{font-size:18px;font-weight:700}
.watch-dir.buy{color:var(--green)}.watch-dir.sell{color:var(--red)}
.watch-conf{font-size:13px;color:var(--amber);font-weight:600;margin-left:auto}
.watch-thesis{font-size:13px;color:var(--mut);margin-top:8px;line-height:1.5}

/* ── Best opportunity card (blue) ────────────────────────────────────────── */
.best-section{margin-bottom:32px}
.best-section-title{color:var(--blue);font-size:17px;font-weight:700;margin:0 0 16px;
  display:flex;align-items:center;gap:8px}
.best-card{
  background:linear-gradient(135deg,rgba(88,166,255,.10) 0%,rgba(88,166,255,.03) 100%);
  border:2px solid var(--blue);border-radius:14px;padding:20px 24px}
.best-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.best-badge{background:var(--blue);color:#0a0f14;font-weight:900;font-size:11px;
  text-transform:uppercase;letter-spacing:.12em;padding:4px 12px;border-radius:20px}
.best-pair{font-size:22px;font-weight:800;color:var(--fg)}
.best-dir{font-size:18px;font-weight:700}
.best-dir.buy{color:var(--green)}.best-dir.sell{color:var(--red)}
.best-conf{font-size:13px;color:var(--blue);font-weight:600;margin-left:auto}
.best-reason{font-size:13px;color:var(--mut);margin-top:8px;line-height:1.5}
""".replace("__MONO__", _MONO)


def _f(v, nd=2):
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def _most_recent_date(rows) -> str:
    """Return the date string of the most recently logged row, or today."""
    dates = [(r.get("timestamp") or "")[:10] for r in rows if r.get("timestamp")]
    return max(dates) if dates else datetime.now().strftime("%Y-%m-%d")


def _active_setups(rows) -> str:
    """Render prominent green cards for any OPEN YES-trade recommendations."""
    open_yes = [r for r in rows if r.get("trade_this") == "YES" and r.get("status") == "OPEN"]
    if not open_yes:
        return ""

    cards = []
    for r in sorted(open_yes, key=lambda x: int(x.get("id") or 0), reverse=True):
        pair      = r.get("pair", "")
        direction = (r.get("direction") or "").upper()
        dcls      = "buy" if direction == "BUY" else "sell"
        conf      = r.get("confidence") or "—"
        entry     = r.get("entry") or "—"
        stop      = r.get("stop_loss") or "—"
        target    = r.get("target") or "—"
        rr_raw    = r.get("reward_risk") or ""
        try:
            rr = f"{float(rr_raw):.2f}:1"
        except (TypeError, ValueError):
            rr = "—"
        session, window = _session_time(pair)
        date = (r.get("timestamp") or "")[:10]

        cards.append(
            f'<div class="alert-card">'
            f'<div class="alert-head">'
            f'<span class="alert-badge">🚨 TRADE THIS</span>'
            f'<span class="alert-pair">{html.escape(pair)}</span>'
            f'<span class="alert-dir {dcls}">{html.escape(direction)}</span>'
            f'<span class="alert-meta">#{html.escape(str(r.get("id","")))} &middot; {html.escape(date)}<br>'
            f'Confidence: {html.escape(str(conf))}/10</span>'
            f'</div>'
            f'<div class="alert-grid">'
            f'<div class="alert-item"><div class="k">Entry Price</div>'
            f'<div class="v">{html.escape(str(entry))}</div></div>'
            f'<div class="alert-item"><div class="k">Stop Loss</div>'
            f'<div class="v red">{html.escape(str(stop))}</div></div>'
            f'<div class="alert-item"><div class="k">Target</div>'
            f'<div class="v grn">{html.escape(str(target))}</div></div>'
            f'<div class="alert-item"><div class="k">Reward : Risk</div>'
            f'<div class="v">{html.escape(rr)}</div></div>'
            f'</div>'
            f'<div class="alert-time">⏰&nbsp; Best entry: '
            f'<strong>{html.escape(session)} session</strong>'
            f'&ensp;&mdash;&ensp;{html.escape(window)}</div>'
            f'</div>'
        )

    return (
        '<div class="alert-section">'
        '<div class="alert-section-title">⚡ Active Trade Setups</div>'
        + "".join(cards)
        + '</div>'
    )


def _watch_list_section(rows, today: str) -> str:
    """Render amber cards for today's NO trades with confidence 5-6."""
    candidates = [
        r for r in rows
        if (r.get("timestamp") or "")[:10] == today
        and r.get("trade_this") != "YES"
        and r.get("status") not in ("NO_TRADE",)  # exclude stage-1 filtered rows
    ]
    # Filter to conf 5-6 and sort descending by confidence.
    watch = []
    for r in candidates:
        try:
            c = int(float(r.get("confidence") or 0))
        except (TypeError, ValueError):
            continue
        if 5 <= c <= 6:
            watch.append((c, r))
    watch.sort(key=lambda x: x[0], reverse=True)
    watch = watch[:3]

    if not watch:
        return ""

    cards = []
    for conf_val, r in watch:
        pair  = r.get("pair", "")
        direction = (r.get("direction") or "").upper()
        dcls  = "buy" if direction == "BUY" else "sell"
        kt    = html.escape(r.get("key_thesis") or "")
        thesis_text = (kt[:200] + "…") if len(kt) > 200 else kt
        cards.append(
            f'<div class="watch-card">'
            f'<div class="watch-head">'
            f'<span class="watch-badge">👀 WATCHING</span>'
            f'<span class="watch-pair">{html.escape(pair)}</span>'
            f'<span class="watch-dir {dcls}">{html.escape(direction)}</span>'
            f'<span class="watch-conf">Confidence: {conf_val}/10</span>'
            f'</div>'
            + (f'<div class="watch-thesis">{thesis_text}</div>' if thesis_text else '')
            + f'</div>'
        )

    return (
        '<div class="watch-section">'
        '<div class="watch-section-title">👀 Watch List — Approaching Signal</div>'
        + "".join(cards)
        + '</div>'
    )


def _best_opportunity_section(rows, today: str) -> str:
    """Render a single blue card for today's highest-confidence pair."""
    today_rows = [r for r in rows if (r.get("timestamp") or "")[:10] == today]
    if not today_rows:
        return ""

    best = None
    best_conf = -1
    for r in today_rows:
        try:
            c = int(float(r.get("confidence") or 0))
        except (TypeError, ValueError):
            c = 0
        if c > best_conf:
            best_conf = c
            best = r

    if best is None or best_conf == 0:
        return ""

    pair      = best.get("pair", "")
    direction = (best.get("direction") or "").upper()
    dcls      = "buy" if direction == "BUY" else "sell"
    kt        = html.escape(best.get("key_thesis") or "")
    reason    = (kt[:250] + "…") if len(kt) > 250 else kt

    return (
        '<div class="best-section">'
        '<div class="best-section-title">⭐ Best Opportunity Today</div>'
        '<div class="best-card">'
        '<div class="best-head">'
        f'<span class="best-badge">⭐ BEST TODAY</span>'
        f'<span class="best-pair">{html.escape(pair)}</span>'
        f'<span class="best-dir {dcls}">{html.escape(direction)}</span>'
        f'<span class="best-conf">Score: {best_conf}/10</span>'
        '</div>'
        + (f'<div class="best-reason">{reason}</div>' if reason else '')
        + '</div></div>'
    )


def _stat_cards(stats: dict, live: dict = None) -> str:
    """Recommendations/Actionable/Open are legitimate whole-system funnel
    counts (learning.compute_stats(), blended v1+v2, every trade_this==YES
    row regardless of era) -- no ambiguity there. Closed/Win rate/Wins-Losses
    used to come from that same blended source too, sitting right under the
    "Real Fund" heading and silently disagreeing with the ticker bar's v2-
    only, net-pips-corrected number (43% vs 60% -- the exact duplicate-number
    complaint that started this audit; 13 of learning.compute_stats()'s 14
    "closed" rows are pre-reset v1 history that every other real-fund figure
    on this page deliberately excludes, per financials.py's own "v1 excluded
    from all running calculations" convention). Now sourced from the same
    live, v2-scoped, net-pips-based fund state the ticker bar uses, so this
    card set and the ticker can never show two different "real fund" numbers
    again. Expectancy switched from a gross-pips r_multiple average (never
    net-corrected) to a $/trade figure built from live's already
    net-corrected avg_win_dollars/avg_loss_dollars -- there's no clean net-
    corrected R-multiple to substitute without re-deriving r_multiple from
    net_pips first, which nothing in this codebase currently does."""
    live = live or {}
    v2_n   = live.get("v2_decisive", 0)
    v2_wr  = live.get("v2_win_rate")
    v2_w   = live.get("v2_wins", 0)
    v2_l   = live.get("v2_losses", 0)
    wr_txt = f"{v2_wr:.0f}%" if v2_wr is not None else "-"

    avg_win_d, avg_loss_d = live.get("avg_win_dollars", 0.0), live.get("avg_loss_dollars", 0.0)
    if v2_n > 0 and v2_wr is not None:
        p = v2_wr / 100.0
        exp_d = p * avg_win_d - (1 - p) * avg_loss_d
        exp_txt = f"{exp_d:+.2f}/trade"
    else:
        exp_txt = "-"

    cards = [
        ("Recommendations", stats["total_recommendations"]),
        ("Actionable (YES)", stats["actionable"]),
        ("Open", stats["open"]),
        ("Closed (v2)", v2_n),
        ("Win rate (v2)", wr_txt),
        ("Wins / Losses (v2)", f'{v2_w} / {v2_l}'),
        ("Expectancy ($)", exp_txt),
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
    def _id_sort_key(x):
        try:
            return int(x.get("id") or 0)
        except (TypeError, ValueError):
            return 0
    for r in sorted(rows, key=_id_sort_key, reverse=True):
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


def _risk_section() -> str:
    """Render the risk profile panel. Balance/peak/drawdown/streak are sourced
    live from financials.calculate_fund_state() (recomputed fresh from
    trades.csv on every call) rather than the risk_profile.json snapshot,
    which is only rewritten once per full/intraday scan and goes stale
    between scans -- the same stale-snapshot pattern already fixed for
    risk_profile.json's own peak_balance ratchet (see
    project_risk_profile_peak_ratchet_fix.md). risk_mode/last_5_win_rate/
    total_open_pct are stateful risk-manager sizing decisions with no
    equivalent in calculate_fund_state()'s pure trade-history computation,
    so those three still come from the risk_profile.json snapshot."""
    import json as _json
    try:
        if not config.RISK_PROFILE_FILE.exists():
            return ""
        profile = _json.loads(config.RISK_PROFILE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""

    from src.trading import financials as _fin
    live = _fin.calculate_fund_state()

    cur  = profile.get("account_currency", "USD")
    mode = profile.get("risk_mode", "normal")
    wr   = profile.get("last_5_win_rate")
    tot  = profile.get("total_open_pct", 0.0)

    bal  = live.get("balance", profile.get("estimated_balance", 0))
    peak = live.get("peak_balance", profile.get("peak_balance", bal))
    cl   = live.get("consecutive_losses", profile.get("consecutive_losses", 0))
    cw   = live.get("consecutive_wins", profile.get("consecutive_wins", 0))
    dd   = live.get("current_drawdown_pct", 0.0)

    from src import risk_manager as _rm
    mode_risk = _rm.MODE_RISK.get(mode, 1.0)

    wr_txt = f"{wr*100:.0f}%" if wr is not None else "—"
    bar_pct = min(tot / _rm.MAX_DAILY_RISK * 100, 100)
    bar_cls = "danger" if bar_pct >= 100 else "warn" if bar_pct >= 60 else ""

    sym = {"USD":"$","EUR":"€","GBP":"£"}.get(cur, f"{cur} ")
    real_bal = config.ACCOUNT_BALANCE
    fund_ret = (bal - _rm.FUND_START) / _rm.FUND_START * 100
    cards = [
        ("FOREX AI FUND",  f'{sym}{bal:,.2f} ({fund_ret:+.1f}%)'),
        ("Real Account",   f'{sym}{real_bal:,.2f}'),
        ("Peak balance",   f'{sym}{peak:,.2f}'),
        ("Risk per trade", f'{mode_risk:.2f}%'),
        ("Last-5 win rate", wr_txt),
        ("Open exposure",  f'{tot:.1f}% / {_rm.MAX_DAILY_RISK:.0f}%'),
        ("Drawdown",       f'{dd:.1f}%'),
    ]
    cards_html = "".join(
        f'<div class="risk-card"><div class="k">{html.escape(k)}</div>'
        f'<div class="v">{html.escape(str(v))}</div></div>'
        for k, v in cards
    )

    # Mode badge
    mode_lbl  = mode.replace("_", " ").title()
    mode_html = (
        f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">'
        f'<span style="font-size:13px;color:var(--mut)">Risk mode:</span>'
        f'<span class="pill mode-{mode}" style="font-size:13px">{html.escape(mode_lbl)}</span>'
        f'</div>'
    )

    # Streak
    if cw > 1:
        streak = f'🔥 {cw} consecutive wins'
    elif cl > 0:
        streak = f'⚠️ {cl} consecutive loss{"es" if cl > 1 else ""}'
    else:
        streak = 'No active streak'

    # Exposure bar
    bar_html = (
        f'<div style="margin-bottom:12px">'
        f'<div style="font-size:11px;color:var(--mut);text-transform:uppercase;'
        f'letter-spacing:.04em;margin-bottom:4px">Open exposure</div>'
        f'<div class="risk-bar-wrap"><div class="risk-bar {bar_cls}" '
        f'style="width:{bar_pct:.0f}%"></div></div>'
        f'<div style="font-size:11px;color:var(--mut);margin-top:3px">'
        f'{tot:.1f}% of {_rm.MAX_DAILY_RISK:.0f}% daily limit</div>'
        f'</div>'
    )

    # Warnings
    warnings = []
    if mode == "capital_protection":
        warnings.append(f'🔴 CAPITAL PROTECTION MODE — drawdown {dd:.1f}% from peak')
    elif mode == "streak_protection":
        warnings.append('🔴 STREAK PROTECTION MODE — 3+ consecutive losses')
    elif mode == "reduced":
        warnings.append('🟡 REDUCED RISK — last-5 win rate below 40%')
    elif mode == "enhanced":
        warnings.append('🟢 ENHANCED RISK — last-5 win rate above 70%')
    if tot >= _rm.MAX_DAILY_RISK:
        warnings.append(f'🔴 RISK LIMIT REACHED — {tot:.1f}% open exposure at {_rm.MAX_DAILY_RISK:.0f}% limit')
    warn_html = "".join(
        f'<div class="risk-warn">{html.escape(w)}</div>' for w in warnings
    )

    return (
        '<section>'
        '<h2>⚖️ Risk Management Profile</h2>'
        + warn_html
        + mode_html
        + f'<div class="risk-grid">{cards_html}</div>'
        + bar_html
        + f'<p style="font-size:12px;color:var(--mut);margin-top:4px">'
        + f'Streak: {html.escape(streak)} &middot; '
        + f'Updated: {html.escape(profile.get("updated_at","")[:10])}</p>'
        + '</section>'
    )


def _learning_feed_section() -> str:
    """Render all memory.json patterns as a plain-English feed."""
    from src import memory as _memory
    try:
        records = _memory.load()
    except Exception:
        return ""
    if not records:
        return ""

    tag_map = {
        "seed":    ("SEED",    "src-seed"),
        "auto":    ("AUTO",    "src-auto"),
        "outcome": ("OUTCOME", "src-outcome"),
        "user":    ("USER",    "src-user"),
    }
    items = []
    for r in records:
        src = (r.get("source") or "user").lower()
        label, cls = tag_map.get(src, ("USER", "src-user"))
        pattern = html.escape(r.get("pattern") or "")
        outcome = html.escape(r.get("outcome") or "")
        items.append(
            f'<li class="learn-item">'
            f'<span class="src-tag {cls}">{label}</span>'
            f'<div>'
            f'<div class="learn-pattern">{pattern}</div>'
            + (f'<div class="learn-outcome">{outcome}</div>' if outcome else "")
            + f'</div></li>'
        )

    return (
        '<section>'
        '<h2>🧠 System Memory — Learned Patterns</h2>'
        '<ul class="learn-list">'
        + "".join(items)
        + '</ul></section>'
    )


def _research_analytics_section() -> str:
    """Render research analytics charts: win rates by entry condition and MFE/MAE."""
    try:
        import csv as _csv
        rt_csv = config.DATA_DIR / "research_trades.csv"
        if not rt_csv.exists():
            return ""
        with rt_csv.open("r", encoding="utf-8-sig", newline="") as fh:
            rt_rows = list(_csv.DictReader(fh))
    except Exception:
        return ""

    # 2026-09-02: EXPIRED excluded from the denominator here, matching the
    # dominant convention already established in risk_manager.py::
    # _is_win_outcome() and dynamic_threshold.py::_decisive_bucket() --
    # EXPIRED (timed out before resolving to a target/stop hit) isn't a
    # clean directional outcome the way WIN/LOSS/PARTIAL_WIN are, and was
    # previously counted here as a guaranteed non-win regardless of its
    # actual net_pips sign, which understated the displayed win rate.
    closed = [r for r in rt_rows if r.get("status") in ("WIN", "LOSS", "PARTIAL_WIN")]
    if len(closed) < 5:
        return ""

    def _win(r):
        status = r.get("status")
        if status == "WIN":
            return True
        if status == "PARTIAL_WIN":
            # Real, decisive, money-affecting close that isn't uniformly a
            # win by label -- classify by net_pips sign, same precedent as
            # risk_manager.py::_is_win_outcome() and dynamic_threshold.py.
            try:
                return float(r.get("net_pips") or 0) > 0
            except (TypeError, ValueError):
                return False  # unparseable/missing net_pips -- conservative, not a win
        return False

    def _pct(wins, total):
        return round(wins / total * 100) if total else 0

    def _wr_table(buckets: dict, label: str) -> str:
        if not buckets:
            return ""
        rows_html = []
        for k in sorted(buckets):
            w, t = buckets[k]
            wr = _pct(w, t)
            bar = f'<div style="background:{"var(--green)" if wr >= 50 else "var(--red)"};height:8px;border-radius:3px;width:{min(wr,100)}%;margin-top:3px"></div>'
            rows_html.append(
                f"<tr><td>{html.escape(str(k))}</td>"
                f'<td class="num">{t}</td>'
                f'<td class="num">{w}</td>'
                f'<td><span style="font-weight:700;color:{"var(--green)" if wr>=50 else "var(--red)"}">{wr}%</span>{bar}</td></tr>'
            )
        return (
            f"<h3 style='font-size:13px;color:var(--mut);margin:16px 0 8px'>{html.escape(label)}</h3>"
            "<table><tr><th>Bucket</th><th>Trades</th><th>Wins</th><th>Win rate</th></tr>"
            + "".join(rows_html)
            + "</table>"
        )

    # ── Win rate by day of week ───────────────────────────────────────────────
    _days = {"1":"Mon","2":"Tue","3":"Wed","4":"Thu","5":"Fri"}
    dow_bkt = {}
    for r in closed:
        d = r.get("day_of_week", "")
        if not d:
            d = str(datetime.strptime(r.get("date","")[:10], "%Y-%m-%d").isoweekday()) \
                if r.get("date","")[:10] else ""
        if d in _days:
            b = dow_bkt.setdefault(_days[d], [0, 0])
            b[0] += _win(r)
            b[1] += 1
    dow_html = _wr_table({k: (v[0], v[1]) for k, v in dow_bkt.items()}, "Win rate by day of week")

    # ── Win rate by Auckland hour band ────────────────────────────────────────
    _hour_bands = {
        "00-06 (overnight)": (0, 5),
        "06-12 (morning)":   (6, 11),
        "12-18 (afternoon)": (12, 17),
        "18-24 (evening)":   (18, 23),
    }
    hour_bkt = {}
    for r in closed:
        h = r.get("hour_auckland", "")
        try:
            hv = int(float(h))
            for band, (lo, hi) in _hour_bands.items():
                if lo <= hv <= hi:
                    b = hour_bkt.setdefault(band, [0, 0])
                    b[0] += _win(r)
                    b[1] += 1
                    break
        except (TypeError, ValueError):
            pass
    hour_html = _wr_table({k: (v[0], v[1]) for k, v in hour_bkt.items()}, "Win rate by Auckland hour")

    # ── Win rate by market regime ─────────────────────────────────────────────
    regime_bkt = {}
    for r in closed:
        reg = r.get("market_regime", "") or "unknown"
        b = regime_bkt.setdefault(reg, [0, 0])
        b[0] += _win(r)
        b[1] += 1
    regime_html = _wr_table({k: (v[0], v[1]) for k, v in regime_bkt.items()}, "Win rate by market regime")

    # ── Win rate by trade grade ───────────────────────────────────────────────
    grade_bkt = {}
    for r in closed:
        g = r.get("grade", "") or "?"
        b = grade_bkt.setdefault(g, [0, 0])
        b[0] += _win(r)
        b[1] += 1
    grade_html = _wr_table(
        {k: (v[0], v[1]) for k, v in sorted(grade_bkt.items())},
        "Win rate by entry grade (A=best)"
    )

    # ── Win rate by correlation agreement count ───────────────────────────────
    corr_bkt = {}
    for r in closed:
        c = r.get("corr_agreement_count", "")
        try:
            cv = str(int(float(c))) if c != "" else "?"
        except (TypeError, ValueError):
            cv = "?"
        b = corr_bkt.setdefault(f"{cv} correlated pairs agree", [0, 0])
        b[0] += _win(r)
        b[1] += 1
    corr_html = _wr_table({k: (v[0], v[1]) for k, v in sorted(corr_bkt.items())}, "Win rate by correlated-pair agreement")

    # ── MFE vs MAE summary ────────────────────────────────────────────────────
    mfe_vals = []
    mae_vals = []
    for r in closed:
        try:
            mfe_vals.append(float(r.get("mfe_pips") or 0))
            mae_vals.append(float(r.get("mae_pips") or 0))
        except (TypeError, ValueError):
            pass
    mfe_mae_html = ""
    if mfe_vals:
        avg_mfe = round(sum(mfe_vals) / len(mfe_vals), 1)
        avg_mae = round(sum(mae_vals) / len(mae_vals), 1)
        max_mfe = round(max(mfe_vals), 1)
        max_mae = round(max(mae_vals), 1)
        mfe_ratio = round(avg_mfe / avg_mae, 2) if avg_mae > 0 else "∞"
        mfe_mae_html = (
            "<h3 style='font-size:13px;color:var(--mut);margin:16px 0 8px'>MFE / MAE excursion profile</h3>"
            "<table><tr><th>Metric</th><th>Avg (pips)</th><th>Max (pips)</th></tr>"
            f"<tr><td>Max Favourable Excursion (MFE)</td><td class='num' style='color:var(--green)'>{avg_mfe}</td><td class='num'>{max_mfe}</td></tr>"
            f"<tr><td>Max Adverse Excursion (MAE)</td><td class='num' style='color:var(--red)'>{avg_mae}</td><td class='num'>{max_mae}</td></tr>"
            f"<tr><td>MFE:MAE ratio (>1 = favourable)</td><td class='num'>{mfe_ratio}</td><td class='num'>—</td></tr>"
            "</table>"
        )

    # ── Exit reason breakdown ─────────────────────────────────────────────────
    exit_bkt = {}
    for r in closed:
        er = r.get("exit_reason", "") or r.get("status", "UNKNOWN")
        b = exit_bkt.setdefault(er, [0, 0])
        b[0] += _win(r)
        b[1] += 1
    exit_html = _wr_table({k: (v[0], v[1]) for k, v in sorted(exit_bkt.items())}, "Exit reason breakdown")

    # ── Post-close target reached ─────────────────────────────────────────────
    pc_checked = [r for r in closed if r.get("post_close_checked_at")]
    pc_reached = sum(1 for r in pc_checked if (r.get("post_close_target_reached") or "").lower() == "true")
    pc_html = ""
    if pc_checked:
        pc_pct = _pct(pc_reached, len(pc_checked))
        pc_html = (
            "<h3 style='font-size:13px;color:var(--mut);margin:16px 0 8px'>Post-close: target reached within 5 days after expiry</h3>"
            "<p style='font-size:13px'>"
            f"<strong style='color:var(--amber)'>{pc_reached}/{len(pc_checked)}</strong> "
            f"trades ({pc_pct}%) reached their original target within 5 days of closing. "
            + ("This suggests targets are well-calibrated." if pc_pct < 40 else
               f"<strong>This suggests many trades expire too early — consider extending the expiry window.</strong>")
            + "</p>"
        )

    return (
        '<section id="research-analytics">'
        '<h2>🔬 Research Analytics — ML Pattern Insights</h2>'
        f'<p style="font-size:12px;color:var(--mut);margin-bottom:16px">'
        f'Based on {len(closed)} closed research trades. '
        f'Patterns reveal which entry conditions produce the best outcomes.</p>'
        + grade_html
        + corr_html
        + dow_html
        + hour_html
        + regime_html
        + mfe_mae_html
        + exit_html
        + pc_html
        + '</section>'
    )


def _group_header(title: str, subtitle: str = "") -> str:
    return (
        f'<div class="group-header"><span class="group-title">{html.escape(title)}</span>'
        + (f'<span class="group-sub">{html.escape(subtitle)}</span>' if subtitle else "")
        + "</div>"
    )


def _ticker_bar(live: dict) -> str:
    """Top-of-page ticker strip: the numbers a person scanning fast checks first.

    2026-09-04: live["win_rate"] (calculate_fund_state()'s top-level field) is
    derived from raw gross `pips`, not `net_pips` -- confirmed dead/never fixed
    earlier this session specifically because nothing called it (see the
    "confirmed unused by any live consumer" comment next to it in
    financials.py). This dashboard revived it as a caller, producing a second,
    gross-pips-inflated "win rate" (70.0%) that visibly contradicted the
    Real Fund section's correct, net-pips-based number (60.0% under the v2
    reset scope). Switched to live["v2_win_rate"], which is net-pips-based
    and v2-scoped, matching every other corrected figure on this page.
    """
    bal   = live.get("balance", 0.0)
    eq    = live.get("total_equity", bal)
    dd    = live.get("current_drawdown_pct", 0.0)
    wr    = live.get("v2_win_rate", 0.0)
    pf    = live.get("profit_factor", 0.0)
    daily = live.get("daily_pnl_pct", 0.0)
    sizing = live.get("sizing_mode", "NORMAL")
    open_n = live.get("open_count", 0)

    def _chip(label, value, cls=""):
        return (f'<div class="tick"><span class="tick-k">{html.escape(label)}</span>'
                f'<span class="tick-v {cls}">{html.escape(str(value))}</span></div>')

    dd_cls  = "neg" if dd > 0 else ""
    pnl_cls = "pos" if daily > 0 else "neg" if daily < 0 else ""
    return '<div class="ticker">' + "".join([
        _chip("Balance", f"${bal:,.2f}"),
        _chip("Equity", f"${eq:,.2f}"),
        _chip("Day P/L", f"{daily:+.2f}%", pnl_cls),
        _chip("Drawdown", f"{dd:.2f}%", dd_cls),
        _chip("Win %", f"{wr:.1f}%"),
        _chip("Profit factor", f"{pf:.2f}"),
        _chip("Open", str(open_n)),
        _chip("Sizing", sizing),
    ]) + "</div>"


def _ror_kelly_ftmo_sharpe_section() -> str:
    """Risk of Ruin, Kelly Criterion, and Sharpe come from risk_of_ruin.py's
    own compute_trade_stats()/compute_sharpe_from_history() -- pre-existing,
    general-purpose functions also used elsewhere (e.g. the weekly Telegram
    report), NOT rewritten here. 2026-09-04 audit finding, disclosed rather
    than silently fixed: those two functions read tracker.load() filtered to
    strict status in (WIN, LOSS) only -- no PARTIAL_WIN, no EXPIRED-by-pips-
    sign reclassification, no v1/v2 split -- a FOURTH population definition
    on this page, different from both the ticker bar's v2-scoped net-pips
    figure and learning.compute_stats()'s blended one. Left alone rather than
    unified because changing a shared, non-dashboard-owned function's
    population definition is a bigger, riskier change than this page's own
    audit scope -- flagged as a follow-up instead. FTMO is unaffected: it's
    built entirely from calculate_fund_state()'s live, v2-scoped fields."""
    try:
        import json as _json
        from src import risk_of_ruin as _ror
        from src import risk_manager as _rm
        from src.trading import financials as _fin
    except Exception:
        return ""
    try:
        live = _fin.calculate_fund_state()
        stats = _ror.compute_trade_stats()
        sharpe = _ror.compute_sharpe_from_history()
    except Exception:
        return ""

    try:
        profile = _json.loads(config.RISK_PROFILE_FILE.read_text(encoding="utf-8")) \
            if config.RISK_PROFILE_FILE.exists() else {}
    except Exception:
        profile = {}
    mode = profile.get("risk_mode", "normal")
    risk_pct = _rm.MODE_RISK.get(mode, 1.0) / 100.0

    n = stats.get("decisive", 0)
    cards = []
    if n >= 10 and stats.get("win_rate") is not None:
        wr_f    = stats["win_rate"]
        avg_rr  = stats["avg_win_rr"]
        ror     = _ror.risk_of_ruin(wr_f, avg_rr, risk_pct)
        kelly   = _ror.kelly_criterion(wr_f, avg_rr)
        cards += [
            ("Risk of Ruin (50% DD)", "100%" if ror >= 1.0 else f"{ror*100:.2f}%"),
            ("Kelly Criterion", f"{kelly*100:.1f}%" if kelly > 0 else "negative edge"),
            ("Quarter Kelly", f"{kelly*100*0.25:.1f}%" if kelly > 0 else "—"),
            ("Current risk / trade", f"{risk_pct*100:.2f}%"),
        ]
    else:
        cards.append(("Risk of Ruin / Kelly", f"need 10+ decisive trades (have {n})"))

    if sharpe.get("sharpe") is not None:
        cards += [
            ("Sharpe ratio (annualised)", f'{sharpe["sharpe"]:.2f}'),
            ("Sharpe verdict", sharpe["verdict"]),
        ]
    else:
        cards.append(("Sharpe ratio", sharpe.get("verdict", "insufficient data")))

    ftmo = _ror.compute_ftmo_metrics(
        _rm.FUND_START,
        live.get("balance", _rm.FUND_START),
        live.get("peak_balance", _rm.FUND_START),
        abs(live.get("worst_daily_pnl_pct", 0.0)),
        live.get("current_drawdown_pct", 0.0),
    )
    ftmo_cards = [
        ("FTMO profit", f'{ftmo["profit_pct"]:+.2f}% (target {_ror.FTMO_PROFIT_TARGET_PCT:.0f}%)'),
        ("FTMO daily-loss rule", "OK" if ftmo["daily_rule_ok"] else "BREACHED"),
        ("FTMO total-DD rule", "OK" if ftmo["total_rule_ok"] else "BREACHED"),
        ("Challenge viable", "YES" if ftmo["challenge_viable"] else "NO"),
    ]

    def _cards_html(items):
        return "".join(
            f'<div class="risk-card"><div class="k">{html.escape(str(k))}</div>'
            f'<div class="v">{html.escape(str(v))}</div></div>'
            for k, v in items
        )

    return (
        '<section><h2>Risk of Ruin &middot; Kelly &middot; FTMO &middot; Sharpe</h2>'
        f'<p style="font-size:11px;color:var(--mut);margin-bottom:10px">Risk of Ruin/Kelly/Sharpe '
        f'use a different population than the Real Fund cards above (all fund history, v1+v2 '
        f'blended, strict WIN/LOSS only, n={n} here) — a pre-existing risk_of_ruin.py convention, '
        f'not unified with the v2-only figures elsewhere on this page. FTMO below is unaffected: '
        f'it reads live, v2-scoped fund state directly.</p>'
        f'<div class="risk-grid" style="margin-bottom:14px">{_cards_html(cards)}</div>'
        '<h3 style="font-size:11px;color:var(--mut);margin:14px 0 8px;text-transform:uppercase;'
        'letter-spacing:.05em">FTMO challenge compliance</h3>'
        f'<div class="risk-grid">{_cards_html(ftmo_cards)}</div>'
        '</section>'
    )


def _trading_block_section() -> str:
    """Any active safety gate that currently changes real-money sizing or
    blocks trading outright, plus the most recent scan's run-guard record."""
    try:
        import json as _json
        profile = _json.loads(config.RISK_PROFILE_FILE.read_text(encoding="utf-8")) \
            if config.RISK_PROFILE_FILE.exists() else {}
        guard_file = config.DATA_DIR / "run_guard.json"
        guard = _json.loads(guard_file.read_text(encoding="utf-8")) if guard_file.exists() else {}
    except Exception:
        return ""

    mode = profile.get("risk_mode", "normal")
    gates = []
    if mode == "capital_protection":
        gates.append(("CAPITAL PROTECTION", "Risk cut to 0.25% per trade — drawdown breach", True))
    elif mode == "streak_protection":
        gates.append(("STREAK PROTECTION", "Risk cut to 0.25% per trade — 3+ consecutive losses", True))
    elif mode == "reduced":
        gates.append(("REDUCED RISK", "Last-5 win rate below 40%", True))
    if not gates:
        gates.append(("NO ACTIVE GATES", f"Trading normally (mode: {mode})", False))

    gate_html = "".join(
        (f'<div class="risk-warn"><strong>{html.escape(label)}</strong> — {html.escape(desc)}</div>'
         if is_warn else
         f'<div class="risk-warn" style="color:var(--fg);background:rgba(0,224,140,.08);'
         f'border-color:var(--green)"><strong style="color:var(--green)">{html.escape(label)}</strong>'
         f' — {html.escape(desc)}</div>')
        for label, desc, is_warn in gates
    )

    guard_html = ""
    if guard:
        guard_html = (
            f'<p style="font-size:11px;color:var(--mut);margin-top:10px">Last scan: '
            f'<strong style="color:var(--fg)">{html.escape(str(guard.get("mode","")))}</strong> mode'
            f' &middot; {html.escape(str(guard.get("source","")))}'
            f' &middot; {html.escape((guard.get("timestamp_utc") or "")[:16].replace("T"," "))} UTC</p>'
        )

    return '<section><h2>Trading-Block / Safety-Gate Status</h2>' + gate_html + guard_html + '</section>'


def _virtual_books_section() -> str:
    """5 independent virtual portfolios, side by side, for direct comparison."""
    try:
        from src import virtual_books
        summaries = virtual_books.get_all_summaries()
    except Exception:
        return ""
    if not summaries:
        return ""

    rows_html = []
    for book_id in sorted(summaries):
        s = summaries[book_id]
        ret = s.get("return_pct", 0.0)
        ret_cls = "pos" if ret > 0 else "neg" if ret < 0 else ""
        rows_html.append(
            f'<tr><td><strong>{html.escape(book_id)}</strong></td>'
            f'<td style="color:var(--mut);white-space:normal">{html.escape(s.get("description",""))}</td>'
            f'<td class="num">${s.get("balance",0):,.2f}</td>'
            f'<td class="num {ret_cls}">{ret:+.2f}%</td>'
            f'<td class="num">{s.get("win_rate",0):.1f}%</td>'
            f'<td class="num">{s.get("decisive",0)}</td>'
            f'<td class="num">{s.get("current_drawdown_pct",0):.2f}%</td>'
            f'<td class="num">{s.get("total_trades",0)}</td>'
            f'<td class="num">{s.get("open_positions",0)}</td>'
            f'</tr>'
        )
    return (
        '<section><h2>Virtual Books — Parallel Rule-Configuration Backtesting</h2>'
        '<p style="font-size:11px;color:var(--mut);margin-bottom:10px">5 independent virtual '
        'portfolios run every scan at zero extra LLM cost, each testing a different rule '
        'configuration against the same candidates the real fund evaluates.</p>'
        '<table><tr><th>Book</th><th>Rule variant</th><th>Balance</th><th>Return</th>'
        '<th>Win rate</th><th>Decisive</th><th>Drawdown</th><th>Trades</th><th>Open</th></tr>'
        + "".join(rows_html) + "</table></section>"
    )


def _research_population_section() -> str:
    """Population-level stats across every research candidate logged (a raw
    funnel count, all statuses, all versions -- deliberately unfiltered), plus
    the grade decomposition using health_check.py's own strict-decisive
    definition (v2, post-exit-fix-cutoff, WIN/FULL_WIN/LOSS only -- see
    get_strict_decisive_grade_population()), not a rough unfiltered query.

    2026-09-04: this table previously computed grade win rates from ALL rows
    (v1+v2 blended, no date cutoff, PARTIAL_WIN included via net_pips) and
    showed F beating D at 51%/34% with no significance test -- directionally
    right but diluted, and easy to misread as a *different* finding from
    health_check.py's own F-vs-D result (47.3%/19.5%, p=1.8e-8) when it's the
    same finding measured more loosely. Now calls the exact same population
    builder health_check.py uses, so this panel and that check can never
    silently disagree."""
    try:
        import csv as _csv
        rt_csv = config.DATA_DIR / "research_trades.csv"
        if not rt_csv.exists():
            return ""
        with rt_csv.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(_csv.DictReader(fh))
    except Exception:
        return ""
    if not rows:
        return ""

    total = len(rows)
    status_bkt: dict = {}
    for r in rows:
        st = r.get("status") or "UNKNOWN"
        status_bkt[st] = status_bkt.get(st, 0) + 1
    status_html = "".join(
        f'<div class="card"><div class="k">{html.escape(k)}</div><div class="v">{v}</div></div>'
        for k, v in sorted(status_bkt.items(), key=lambda x: -x[1])
    )

    from src import health_check as _hc
    try:
        decisive = _hc.get_strict_decisive_grade_population()
    except Exception:
        decisive = None

    grade_html = ""
    fd_note = ""
    if decisive is not None and not decisive.empty:
        grade_bkt: dict = {}
        for grade in _hc._GRADE_ORDER:
            g = decisive[decisive["grade"].astype(str) == grade]
            n = len(g)
            if n < _hc._GRADE_MIN_N:
                continue  # thin sample -- excluded, same bar health_check.py uses
            wins = int((g["status"].astype(str).str.upper().isin(["WIN", "FULL_WIN"])).sum())
            grade_bkt[grade] = (wins, n)

        grade_rows = []
        for g in _hc._GRADE_ORDER:
            if g not in grade_bkt:
                continue
            w, n = grade_bkt[g]
            wr = round(w / n * 100) if n else 0
            cls = "pos" if wr >= 50 else "neg"
            grade_rows.append(
                f'<tr><td>{html.escape(g)}</td><td class="num">{n}</td>'
                f'<td class="num">{w}</td><td class="num {cls}">{wr}%</td></tr>'
            )
        if grade_rows:
            grade_html = (
                f'<p style="font-size:11px;color:var(--mut);margin:14px 0 8px">Strict-decisive '
                f'grade population ({int(decisive.shape[0])} candidates: v2 only, closed on/after '
                f'the {_hc._GRADE_ORDERING_CUTOFF} UTC exit-logic-fix, WIN/FULL_WIN/LOSS only -- '
                f'same definition health_check.py::check_grade_ordering() uses; buckets below '
                f'{_hc._GRADE_MIN_N} decisive trades are hidden, not shown as unstable noise).</p>'
                '<table><tr><th>Grade</th><th>Decisive</th><th>Wins</th><th>Win rate</th></tr>'
                + "".join(grade_rows) + "</table>"
            )

        present = [g for g in _hc._GRADE_ORDER if g in grade_bkt]
        for i in range(len(present) - 1):
            better, worse = present[i], present[i + 1]
            b_wins, b_n = grade_bkt[better]
            w_wins, w_n = grade_bkt[worse]
            result = _hc._ztest_worse_beats_better(b_wins, b_n, w_wins, w_n)
            if result is None:
                continue
            p_value, worse_wr, better_wr = result
            if worse_wr > better_wr and p_value < 0.05:
                fd_note = (
                    f'<p style="font-size:11px;color:var(--amber);margin-top:10px">'
                    f'Grade ordering inverted — grade {worse} (n={w_n}, WR={worse_wr*100:.1f}%) '
                    f'significantly outperforms grade {better} (n={b_n}, WR={better_wr*100:.1f}%), '
                    f'p={p_value:.4g}. See project_fvsd_reinvestigation_sep2026 memory for the '
                    f'full investigation.</p>'
                )

    return (
        '<section><h2>Research Population &amp; Grade Decomposition</h2>'
        f'<p style="font-size:11px;color:var(--mut);margin-bottom:10px">{total} research '
        f'candidates logged (all statuses, all versions) — every candidate the system evaluates, '
        f'not just the ones that became real trades.</p>'
        f'<div class="grid" style="margin-bottom:14px">{status_html}</div>'
        + grade_html
        + fd_note + "</section>"
    )


def _calibration_section() -> str:
    """Confidence-calibration diagnostics -- surfaced as a feature to show
    off, not a bug to hide: recalibrated_confidence() has zero callers in
    the live pipeline today, which is deliberate caution, not an oversight."""
    try:
        from src import confidence_calibration as _cc
    except Exception:
        return ""
    try:
        table = _cc.build_calibration_table()
    except Exception:
        return ""
    if not table:
        return ('<section><h2>Confidence Calibration</h2>'
                '<p style="font-size:12px;color:var(--mut)">Not enough closed research-trade '
                'history yet to build a calibration table.</p></section>')

    overall = table.get("_overall", 0.0)
    active_buckets = {k: v for k, v in table.items() if k != "_overall"}

    rows_html = []
    for key in sorted(active_buckets, key=lambda k: (-k[0], k[1], k[2])):
        conf, direction, has_gbp = key
        wr = active_buckets[key]
        pair_tag = "GBP" if has_gbp else "non-GBP"
        dcls = "buy" if direction == "BUY" else "sell"
        cls = "pos" if wr >= 0.5 else "neg"
        rows_html.append(
            f'<tr><td>{conf}/10</td><td class="{dcls}">{html.escape(direction)}</td>'
            f'<td>{pair_tag}</td><td class="num {cls}">{wr*100:.0f}%</td></tr>'
        )

    n_active = len(active_buckets)
    cards_html = (
        f'<div class="risk-card"><div class="k">Population mean win rate</div>'
        f'<div class="v">{overall*100:.1f}%</div></div>'
        f'<div class="risk-card"><div class="k">Buckets live (trusted)</div>'
        f'<div class="v">{n_active}</div></div>'
        f'<div class="risk-card"><div class="k">Wiring status</div>'
        f'<div class="v" style="color:var(--amber);font-size:14px">BENCHED</div></div>'
    )

    body = (
        f'<p style="font-size:11px;color:var(--mut);margin-bottom:10px">recalibrated_confidence() '
        f'maps raw AI confidence to an empirically-measured win probability, keyed on (confidence, '
        f'direction, GBP-involvement). A bucket only goes live once it clears {_cc.MIN_BUCKET}+ '
        f'decisive trades AND {_cc.MIN_BUCKET_SPAN_DAYS}+ days of close-date spread — otherwise it '
        f'falls back to the population mean below rather than reporting an unstable estimate off a '
        f'lucky or unlucky streak.</p>'
        f'<div class="risk-grid" style="margin-bottom:12px">{cards_html}</div>'
    )
    if n_active:
        body += (
            '<table><tr><th>Confidence</th><th>Dir</th><th>Pair type</th>'
            '<th>Empirical win rate</th></tr>' + "".join(rows_html) + '</table>'
        )
    else:
        body += ('<p style="font-size:11px;color:var(--mut)">No bucket has yet cleared the '
                 'sample-size and time-span bar — every candidate still falls back to the '
                 'population mean.</p>')
    body += (
        '<p style="font-size:11px;color:var(--mut);margin-top:10px">'
        '<strong style="color:var(--amber)">Not yet wired into any live decision</strong> — '
        'this is deliberate caution, not an oversight: the system holds this signal back until '
        'it proves out-of-sample rather than gating real trades on it prematurely.</p>'
    )
    return '<section><h2>Confidence Calibration</h2>' + body + '</section>'


def _online_learner_section() -> str:
    """Online learner reliability-gate status: whether the model has enough
    consecutive reliable retrains for its predictions to matter."""
    try:
        import json as _json
        from src import online_learner as _ol
        meta_file = config.DATA_DIR / "online_model_meta.json"
        meta = _json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    except Exception:
        return ""
    if not meta:
        return ""

    required = getattr(_ol, "_RELIABILITY_STREAK_REQUIRED", 3)
    streak = meta.get("n_consecutive_reliable", 0)
    reliable = streak >= required
    auc = meta.get("holdout_auc")
    auc_txt = f"{auc:.3f}" if auc is not None else "—"
    rwr = meta.get("recent_win_rate")
    owr = meta.get("overall_win_rate")

    cards = [
        ("Decisive trades trained", meta.get("n_decisive", 0)),
        ("Recent win rate (last 20)", f"{rwr*100:.0f}%" if rwr is not None else "—"),
        ("Overall win rate", f"{owr*100:.0f}%" if owr is not None else "—"),
        ("Holdout AUC", auc_txt),
        ("Reliability streak", f"{streak}/{required}"),
        ("Last retrained", (meta.get("last_updated") or "—")[:16].replace("T", " ")),
    ]
    cards_html = "".join(
        f'<div class="risk-card"><div class="k">{html.escape(str(k))}</div>'
        f'<div class="v">{html.escape(str(v))}</div></div>' for k, v in cards
    )
    status_line = ("RELIABLE — predictions eligible to influence sizing" if reliable else
                   "NOT YET RELIABLE — holdout streak below gate, predictions logged but not acted on")
    status_cls = "var(--green)" if reliable else "var(--amber)"

    return (
        '<section><h2>Online Learner Status</h2>'
        f'<div class="risk-grid">{cards_html}</div>'
        f'<p style="font-size:12px;margin-top:10px;color:{status_cls};font-weight:600">{status_line}</p>'
        '</section>'
    )


def _cot_positioning_section() -> str:
    """COT / positioning signals already logged per research trade -- there
    is no standalone Positioning Agent yet, so this surfaces exactly what
    the current pipeline captures in passing, including the dead field."""
    try:
        import csv as _csv
        rt_csv = config.DATA_DIR / "research_trades.csv"
        if not rt_csv.exists():
            return ""
        with rt_csv.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(_csv.DictReader(fh))
    except Exception:
        return ""
    if not rows:
        return ""

    def _win(r):
        status = r.get("status")
        if status in ("WIN", "FULL_WIN"):
            return True
        if status == "PARTIAL_WIN":
            try:
                return float(r.get("net_pips") or 0) > 0
            except (TypeError, ValueError):
                return False
        return False

    closed = [r for r in rows if r.get("status") in ("WIN", "LOSS", "PARTIAL_WIN", "FULL_WIN")]

    accel_bkt: dict = {}
    for r in closed:
        try:
            vi = int(float(r.get("cot_accelerating", "")))
        except (TypeError, ValueError):
            continue
        label = {1: "Building", 0: "Stable", -1: "Fading"}.get(vi, str(vi))
        b = accel_bkt.setdefault(label, [0, 0])
        b[0] += _win(r)
        b[1] += 1

    fund_bkt: dict = {}
    for r in closed:
        v = r.get("fundamental_alignment") or "?"
        b = fund_bkt.setdefault(v, [0, 0])
        b[0] += _win(r)
        b[1] += 1

    def _tbl(bkt, label):
        if not bkt:
            return ""
        rows_html = "".join(
            f'<tr><td>{html.escape(str(k))}</td><td class="num">{v[1]}</td>'
            f'<td class="num">{v[0]}</td>'
            f'<td class="num">{round(v[0]/v[1]*100) if v[1] else 0}%</td></tr>'
            for k, v in sorted(bkt.items())
        )
        return (f'<h3 style="font-size:11px;color:var(--mut);margin:14px 0 8px;text-transform:'
                f'uppercase;letter-spacing:.05em">{html.escape(label)}</h3>'
                '<table><tr><th>Category</th><th>Trades</th><th>Wins</th><th>Win rate</th></tr>'
                + rows_html + "</table>")

    has_momentum_data = any((r.get("cot_momentum") or "").strip() for r in rows)
    momentum_note = "" if has_momentum_data else (
        f'<p style="font-size:11px;color:var(--amber);margin-top:12px">cot_momentum is logged in '
        f'the schema but is 100% blank across all {len(rows)} research trades logged so far — '
        f'wired to nothing yet. A dedicated Positioning Agent to build this out does not exist yet.</p>'
    )

    return (
        '<section><h2>COT / Positioning Data</h2>'
        f'<p style="font-size:11px;color:var(--mut);margin-bottom:10px">CFTC Commitments-of-Traders '
        f'positioning signals already logged per research trade ({len(closed)} closed trades with '
        f'positioning data). No standalone Positioning Agent exists yet — this is what the current '
        f'pipeline captures in passing.</p>'
        + _tbl(accel_bkt, "Win rate by COT momentum")
        + _tbl(fund_bkt, "Win rate by fundamental alignment")
        + momentum_note + "</section>"
    )


def _shadow_mode_section() -> str:
    """Candidate grading/gating rules currently under silent shadow
    evaluation, and how close each is to a promotion conversation."""
    try:
        from src import shadow_mode as _sm
        rules = _sm.list_rules()
    except Exception:
        return ""
    if not rules:
        return (
            '<section><h2>Shadow-Mode Rule Staging</h2>'
            '<p style="font-size:12px;color:var(--mut)">No candidate grading/gating rules are '
            'currently in shadow evaluation. shadow_mode.py exists as the standard on-ramp for any '
            'future rule change: a new rule is registered and evaluated silently against real scans '
            'before it can ever affect a live decision.</p></section>'
        )

    rows_html = []
    for name in rules:
        try:
            status = _sm.check_promotion_readiness(name)
        except Exception:
            continue
        wf, wnf = status.get("would_fire_wr"), status.get("would_not_fire_wr")
        rows_html.append(
            f'<tr><td>{html.escape(name)}</td>'
            f'<td style="font-size:11px;color:var(--mut);white-space:normal">'
            f'{html.escape(status.get("description",""))}</td>'
            f'<td class="num">{status.get("n_decisive",0)}/{status.get("min_n","?")}</td>'
            f'<td class="num">{status.get("days_elapsed",0)}/{status.get("max_days","?")}d</td>'
            f'<td class="num">{f"{wf*100:.0f}%" if wf is not None else "—"}</td>'
            f'<td class="num">{f"{wnf*100:.0f}%" if wnf is not None else "—"}</td>'
            f'<td>{"READY" if status.get("ready") else "collecting"}</td></tr>'
        )
    return (
        '<section><h2>Shadow-Mode Rule Staging</h2>'
        '<p style="font-size:11px;color:var(--mut);margin-bottom:10px">Candidate rules run silently '
        'against real scans before ever affecting a live decision. "Ready" means the evidence bar is '
        'cleared for a promotion conversation — never an automatic promotion.</p>'
        '<table><tr><th>Rule</th><th>Description</th><th>N decisive</th><th>Days</th>'
        '<th>Would-fire WR</th><th>Would-not-fire WR</th><th>Status</th></tr>'
        + "".join(rows_html) + "</table></section>"
    )


def _dynamic_threshold_trace_section() -> str:
    """Live trace of the dynamic confidence threshold: the last 10 computed
    entries and their inputs, from threshold_history.json."""
    try:
        import json as _json
        hist_file = config.DATA_DIR / "threshold_history.json"
        if not hist_file.exists():
            return ""
        history = _json.loads(hist_file.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not history:
        return ""

    latest = history[-1]
    rows_html = []
    for e in reversed(history[-10:]):
        ts = (e.get("timestamp") or "")[:16].replace("T", " ")
        wr = e.get("win_rate_recent")
        wr_txt = f"{wr*100:.0f}%" if wr is not None else "—"
        rows_html.append(
            f'<tr><td>{html.escape(ts)}</td><td>{html.escape(str(e.get("scan_mode","")))}</td>'
            f'<td>{html.escape(str(e.get("regime","")))}</td>'
            f'<td class="num">{e.get("regime_base","")}</td>'
            f'<td class="num">{wr_txt}</td>'
            f'<td class="num">{e.get("win_rate_adjustment","")}</td>'
            f'<td>{html.escape(str(e.get("win_rate_source","")))}</td>'
            f'<td class="num">{e.get("data_quality_adjustment","")}</td>'
            f'<td class="num" style="font-weight:700">{e.get("final_threshold","")}</td></tr>'
        )

    return (
        '<section><h2>Dynamic Threshold — Live Trace</h2>'
        f'<p style="font-size:11px;color:var(--mut);margin-bottom:10px">Current effective '
        f'confidence threshold: <strong style="color:var(--fg);font-size:14px">'
        f'{latest.get("final_threshold","—")}/10</strong> &middot; regime '
        f'{html.escape(str(latest.get("regime","")))} &middot; last computed '
        f'{html.escape((latest.get("timestamp") or "")[:16].replace("T"," "))}</p>'
        '<table><tr><th>Time</th><th>Mode</th><th>Regime</th><th>Base</th><th>Recent WR</th>'
        '<th>WR adj</th><th>WR source</th><th>DQ adj</th><th>Final</th></tr>'
        + "".join(rows_html) + "</table></section>"
    )


def _da_downgrade_section() -> str:
    """Devil's-advocate objection/downgrade aggregates. Undercounts before
    2026-08-27, when the da_fired schema was added -- see
    project_da_downgrade_tracking.md."""
    try:
        import csv as _csv
        rt_csv = config.DATA_DIR / "research_trades.csv"
        if not rt_csv.exists():
            return ""
        with rt_csv.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(_csv.DictReader(fh))
    except Exception:
        return ""

    evaluated = [r for r in rows if (r.get("da_fired") or "").strip() != ""]
    if not evaluated:
        return (
            '<section><h2>Devil&rsquo;s-Advocate (DA) Downgrade Tracking</h2>'
            '<p style="font-size:12px;color:var(--mut)">No candidates evaluated under the '
            'da_fired schema yet.</p></section>'
        )

    fired = [r for r in evaluated if (r.get("da_fired") or "").strip().upper() == "TRUE"]
    downgraded = [r for r in evaluated if (r.get("da_downgraded") or "").strip().upper() == "TRUE"]
    cards = [
        ("Candidates evaluated", len(evaluated)),
        ("DA objections raised", len(fired)),
        ("DA actually downgraded tier", len(downgraded)),
        ("Downgrade rate (of fired)", f"{len(downgraded)/len(fired)*100:.0f}%" if fired else "—"),
    ]
    cards_html = "".join(
        f'<div class="risk-card"><div class="k">{html.escape(str(k))}</div>'
        f'<div class="v">{html.escape(str(v))}</div></div>' for k, v in cards
    )
    return (
        '<section><h2>Devil&rsquo;s-Advocate (DA) Downgrade Tracking</h2>'
        '<p style="font-size:11px;color:var(--mut);margin-bottom:10px">Tracks how often the '
        'devil&rsquo;s-advocate pass raises an objection on a candidate, and how often that '
        'objection actually changed its grade tier (vs. e.g. F&rarr;F, no real effect). '
        'Undercounts before 2026-08-27, when this schema was added.</p>'
        f'<div class="risk-grid">{cards_html}</div></section>'
    )


def _safe_section(label: str, fn, *args, fallback: str = "") -> str:
    """Run one dashboard section builder in isolation. A single malformed
    row/candidate must degrade that one section, not take down the whole
    dashboard rebuild -- confirmed real: generate() previously called every
    section unguarded, so any one exception (e.g. an unparseable numeric
    field on a single trade) propagated all the way to daily.py's outer
    try/except, discarding the entire dashboard for that scan (confirmed:
    data/dashboard.html went uncommitted for 14+ hours across multiple
    scans following the 2026-08-27 CSV corruption, entirely silently)."""
    try:
        return fn(*args)
    except Exception as exc:
        print(f"[dashboard] section '{label}' failed, using fallback: {exc}", file=sys.stderr)
        return fallback


def generate() -> str:
    from src import learning
    from src.trading import financials as _fin
    rows = tracker.load()
    stats = learning.compute_stats()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    live_fund = _safe_section("live_fund_state", _fin.calculate_fund_state, fallback={})
    if not isinstance(live_fund, dict):
        live_fund = {}

    today = _safe_section("most_recent_date", _most_recent_date, rows, fallback="")
    active_html    = _safe_section("active_setups", _active_setups, rows)
    watch_html     = _safe_section("watch_list", _watch_list_section, rows, today)
    best_html      = _safe_section("best_opportunity", _best_opportunity_section, rows, today)
    ticker_html    = _safe_section("ticker_bar", _ticker_bar, live_fund)
    risk_html      = _safe_section("risk", _risk_section)
    ror_html       = _safe_section("ror_kelly_ftmo_sharpe", _ror_kelly_ftmo_sharpe_section)
    block_html     = _safe_section("trading_block", _trading_block_section)
    stat_cards_html = _safe_section("stat_cards", _stat_cards, stats, live_fund)

    books_html  = _safe_section("virtual_books", _virtual_books_section)

    pop_html    = _safe_section("research_population", _research_population_section)
    calib_html  = _safe_section("calibration", _calibration_section)
    online_html = _safe_section("online_learner", _online_learner_section)
    cot_html    = _safe_section("cot_positioning", _cot_positioning_section)
    shadow_html = _safe_section("shadow_mode", _shadow_mode_section)
    trace_html  = _safe_section("dynamic_threshold_trace", _dynamic_threshold_trace_section)
    da_html     = _safe_section("da_downgrade", _da_downgrade_section)

    learning_html  = _safe_section("learning_feed", _learning_feed_section)
    analytics_html = _safe_section("research_analytics", _research_analytics_section)
    equity_html    = _safe_section("equity_curve", _equity_curve, rows)
    by_conf_html   = _safe_section("by_confidence", _by_confidence, rows)
    rows_table_html = _safe_section(
        "rows_table", _rows_table, rows,
        fallback="<p style='color:var(--mut)'>Recommendations table unavailable this run.</p>",
    )

    # Client-side auto-refresh: a lightweight periodic HEAD check that only
    # reloads when the page has actually changed (via ETag/Last-Modified),
    # not a blind full reload -- avoids disrupting anyone mid-scroll on a
    # scan that produced no changes. 120s undershoots the ~30min regen
    # cadence by a wide margin so nobody waits long after a real update.
    refresh_js = """
<script>
(function(){
  var CHECK_MS = 120000;
  var lastTag = null;
  function check(){
    fetch(location.pathname, {method:'HEAD', cache:'no-store'}).then(function(r){
      var tag = r.headers.get('etag') || r.headers.get('last-modified');
      if (tag) {
        if (lastTag === null) { lastTag = tag; }
        else if (tag !== lastTag) { location.reload(); }
      }
    }).catch(function(){});
  }
  setInterval(check, CHECK_MS);
})();
</script>
"""

    body = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>forex-ai terminal</title><style>{_CSS}</style></head><body>'
        f'<h1>forex-ai terminal</h1>'
        f'<p class="sub">Generated {now} &middot; {len(rows)} recommendations logged'
        f' &middot; auto-refreshes when updated &middot; NOT financial advice</p>'
        f'{active_html}'
        f'{watch_html}'
        f'{best_html}'
        f'{ticker_html}'

        + _group_header("Real Fund", "Live trading account — actual money-equivalent state")
        + f'<div class="grid">{stat_cards_html}</div>'
        + risk_html
        + ror_html
        + block_html

        + _group_header("Virtual Books", "Parallel rule-configuration backtests, zero extra cost")
        + books_html

        + _group_header("System Health & Diagnostics", "What the system knows about itself")
        + pop_html
        + calib_html
        + online_html
        + cot_html
        + shadow_html
        + trace_html
        + da_html
        + learning_html

        + _group_header("Trade Log & Research Analytics")
        + f'<section><h2>Equity curve</h2>{equity_html}</section>'
        + f'<section><h2>Win rate by confidence score</h2>{by_conf_html}</section>'
        + analytics_html
        + f'<section><h2>All recommendations</h2>{rows_table_html}</section>'

        + '<p class="foot">Columns T/F/S/P/M = technical, fundamental, sentiment, positioning, macro scores.'
        + ' Record an outcome with: <code>python main.py --close ID WIN|LOSS [exit_price]</code></p>'
        + refresh_js
        + '</body></html>'
    )

    config.DASHBOARD_HTML.write_text(body, encoding="utf-8")
    return str(config.DASHBOARD_HTML)

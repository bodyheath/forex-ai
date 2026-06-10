"""Generate a self-contained HTML dashboard (no server, no external assets) from
the trades spreadsheet: headline performance stats, a by-confidence breakdown, a
cumulative-R equity curve (inline SVG), and a table of every recommendation.
Active YES-trade setups are shown in prominent green alert cards at the very top.
"""

import html
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
    return "London", "5pm – 9pm NZT"


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
"""


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
    for r in sorted(open_yes, key=lambda x: int(x.get("id", 0)), reverse=True):
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


def _risk_section() -> str:
    """Render the risk profile panel from risk_profile.json."""
    import json as _json
    try:
        if not config.RISK_PROFILE_FILE.exists():
            return ""
        profile = _json.loads(config.RISK_PROFILE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""

    bal  = profile.get("estimated_balance", 0)
    peak = profile.get("peak_balance", bal)
    cur  = profile.get("account_currency", "USD")
    mode = profile.get("risk_mode", "normal")
    cl   = profile.get("consecutive_losses", 0)
    cw   = profile.get("consecutive_wins", 0)
    wr   = profile.get("last_5_win_rate")
    tot  = profile.get("total_open_pct", 0.0)

    from src import risk_manager as _rm
    mode_risk = _rm.MODE_RISK.get(mode, 1.0)
    dd        = (peak - bal) / peak * 100 if peak > 0 else 0.0

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


def generate() -> str:
    from src import learning
    rows = tracker.load()
    stats = learning.compute_stats()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    today = _most_recent_date(rows)
    active_html   = _active_setups(rows)
    watch_html    = _watch_list_section(rows, today)
    best_html     = _best_opportunity_section(rows, today)
    risk_html     = _risk_section()
    learning_html = _learning_feed_section()

    body = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>forex-ai dashboard</title><style>{_CSS}</style></head><body>'
        f'<h1>forex-ai &mdash; performance dashboard</h1>'
        f'<p class="sub">Generated {now} &middot; {len(rows)} recommendations logged'
        f' &middot; NOT financial advice</p>'
        f'{active_html}'
        f'{watch_html}'
        f'{best_html}'
        f'<div class="grid">{_stat_cards(stats)}</div>'
        f'{risk_html}'
        f'<section><h2>Equity curve</h2>{_equity_curve(rows)}</section>'
        f'<section><h2>Win rate by confidence score</h2>{_by_confidence(rows)}</section>'
        f'{learning_html}'
        f'<section><h2>All recommendations</h2>{_rows_table(rows)}</section>'
        f'<p class="foot">Columns T/F/S/P/M = technical, fundamental, sentiment, positioning, macro scores.'
        f' Record an outcome with: <code>python main.py --close ID WIN|LOSS [exit_price]</code></p>'
        f'</body></html>'
    )

    config.DASHBOARD_HTML.write_text(body, encoding="utf-8")
    return str(config.DASHBOARD_HTML)

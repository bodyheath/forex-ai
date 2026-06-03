# forex-ai

An AI forex-pair analyst. For a given currency pair it pulls **live data** from
several independent sources, assembles them into an evidence bundle, and asks
Claude — running as a sceptical 20-year institutional analyst — whether there is
a high-probability setup. Most of the time the honest answer is *no*, and that's
the point.

## What it does

For each pair, six analysis layers are gathered from real data:

| Layer | Source | What it provides |
|-------|--------|------------------|
| Technical | Twelve Data (daily + 4h OHLC) | RSI, MACD, Bollinger, SMA50/200, ATR, pivots, S/R — computed locally |
| Fundamental | FRED | central-bank policy rates + interest-rate differential |
| Sentiment | NewsAPI | recent headlines per currency (tone judged by Claude) |
| Positioning | CFTC COT (public) | large-speculator net long/short + how extreme it is |
| Macro | FRED | gold, WTI oil, 10Y/2Y yields, VIX → risk-on/off |
| Risk | derived | ATR vs range; documented gaps flagged |

Claude then returns a structured verdict (`CONFIDENCE`, per-layer scores, entry /
target / stop, R:R, and a final `TRADE_THIS: YES/NO`).

## Setup

```powershell
# 1. Install dependencies (a virtual environment is recommended)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Your keys are already in .env (ANTHROPIC, NEWS_API, FRED).
#    Add a free Twelve Data key for the technical layer:
#    sign up at https://twelvedata.com/pricing and paste it into .env as
#    TWELVE_DATA_KEY=...  (see .env.example for the template).
```

## Usage

```powershell
python main.py EUR/USD                 # analyse one pair (logged to the tracker)
python main.py GBPUSD USDJPY           # analyse several
python main.py --health EUR/USD        # check which sources are live (no price/Claude call)
python main.py --raw EUR/USD           # also print the raw evidence bundle
python main.py --remember "pattern" "outcome"   # teach system memory manually
```

Tip: run `--health` first. It verifies your keys and shows which data sources are
live for a pair **without** spending a price-source call or a Claude call.

Reports are written to `data/reports/`. System memory lives in `data/memory.json`
and is fed into every analysis so the model learns from logged outcomes.

## Tracking outcomes, learning & dashboard

Every analysis is appended to **`data/trades.csv`** (open it in Excel/Sheets).
Actionable calls (`TRADE_THIS: YES`) are logged as `OPEN`; the rest as `NO_TRADE`.

```powershell
python main.py --close 7 WIN 1.0925    # record outcome for rec #7 (WIN/LOSS/BREAKEVEN/SKIPPED/EXPIRED)
python main.py --close 7 LOSS          # exit price optional: defaults to target (WIN) or stop (LOSS)
python main.py --stats                 # print win rate, expectancy, etc.
python main.py --learn                 # recompute learning patterns from outcomes
python main.py --dashboard             # rebuild data/dashboard.html
```

- **Outcome tracker** computes the realised R-multiple and pips from entry/stop/exit.
- **Learning system** turns closed-trade stats into `auto` patterns in `memory.json`
  (win rate by confidence band, by direction, etc.) that feed back into the prompt.
  It only "remembers" a segment once it has ≥4 closed trades, to avoid over-fitting.
- **Dashboard** (`data/dashboard.html`) is a self-contained page — performance
  cards, an equity curve, win-rate-by-confidence, and a table of every call.
  Just double-click it to open; no server needed.

## Daily automation (6am)

`daily.py` runs the whole loop: refresh learning → analyse the `WATCHLIST` →
log each call → rebuild the dashboard. A per-run log is written to
`data/reports/daily_<date>.log`.

```powershell
python daily.py                                 # run it manually any time
.\register_task.ps1                             # register a 6am daily Windows task
Start-ScheduledTask  -TaskName 'ForexAI-Daily'  # test the scheduled task now
Unregister-ScheduledTask -TaskName 'ForexAI-Daily' -Confirm:$false   # remove it
```

Configure the watchlist in `.env`, e.g. `WATCHLIST=EUR/USD,GBP/USD,USD/JPY`.

> **Cost note:** the default watchlist is 8 pairs = 8 Claude calls every morning.
> On Opus that adds up — set `CLAUDE_MODEL=claude-sonnet-4-6` in `.env` for cheaper
> daily batches, and/or trim the watchlist.

## Important caveats (read these)

- **Not financial advice.** This is a research aid. Verify every price level
  yourself before risking capital.
- **Twelve Data free tier is ~800 requests/day (8/min).** Each pair uses 2 calls
  (daily + 4h). Responses are cached for `CACHE_TTL_HOURS` (default 6h). This is
  far more generous than the old Alpha Vantage path; you're unlikely to hit it.
- **Graceful degradation.** If any source fails, that layer is reported as
  `UNAVAILABLE` and the analyst is instructed not to count it as an agreeing
  source — missing data lowers confidence rather than crashing the run.
- **Some data-source IDs are best-effort.** FRED policy-rate series IDs and CFTC
  COT market names are defined in `config.py`. FRED occasionally renames series;
  if a currency's rate or COT shows `UNAVAILABLE`, verify and update the ID at
  <https://fred.stlouisfed.org> or <https://publicreporting.cftc.gov>.
- **COT positioning is unavailable for USD and NZD.** The ICE U.S. Dollar Index
  and the NZD futures contract both stopped updating in the CFTC Legacy
  Futures-Only dataset in Feb 2022, so the code flags them as stale/`UNAVAILABLE`
  (it will never present years-old numbers as current). For USD pairs this is
  fine — the *base* currency's COT (e.g. EUR for EUR/USD) carries the signal. For
  NZD pairs, positioning will simply be one fewer agreeing source.
- **No economic-calendar feed.** There is no reliable free API for upcoming
  high-impact events, so the 48-hour `NEWS_WARNING` is inferred from headlines
  only. Cross-check an external economic calendar before trading.

## Project layout

```
config.py            secrets + per-currency mappings + watchlist
main.py              CLI entry point (analyse / --close / --stats / --learn / --dashboard)
daily.py             6am automation runner
register_task.ps1    registers the Windows scheduled task
prompts/analyst.md   the analyst system prompt
src/
  technical.py       Twelve Data OHLC + local indicators
  fundamental.py     FRED policy rates / rate differential
  fred.py            shared FRED client
  sentiment.py       NewsAPI headlines
  positioning.py     CFTC COT net-spec positioning
  macro.py           oil / yields / VIX / dollar index
  memory.py          system-memory (seed / auto / user patterns)
  analyst.py         Claude call (prompt-cached system block)
  pipeline.py        orchestration (gather -> analyse)
  service.py         analyse + log to tracker
  recparse.py        parse analyst output into fields
  tracker.py         trades.csv read/write + R-multiple/pips
  learning.py        closed-trade stats -> auto memory patterns
  dashboard.py       generate dashboard.html
  cache.py           on-disk response cache
data/
  cache/             cached API responses
  reports/           saved analyses + daily logs
  trades.csv         every recommendation (the tracker spreadsheet)
  dashboard.html     performance dashboard
  memory.json        learned patterns
```

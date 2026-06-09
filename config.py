"""Central configuration: loads secrets from .env and defines per-currency mappings.

NOTE ON DATA SOURCE IDS:
The FRED series IDs and CFTC market names below are best-effort defaults. FRED in
particular renames/retires series occasionally. The app degrades gracefully if a
series is missing (the layer is reported as UNAVAILABLE and the analyst lowers
confidence rather than crashing). If a currency's rate consistently shows
UNAVAILABLE, verify the series ID at https://fred.stlouisfed.org and update it here.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
PROMPT_FILE = ROOT / "prompts" / "analyst.md"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
MEMORY_FILE = DATA_DIR / "memory.json"
REPORTS_DIR = DATA_DIR / "reports"
TRADES_CSV = DATA_DIR / "trades.csv"
DASHBOARD_HTML     = DATA_DIR / "dashboard.html"
WIN_ANALYSIS_FILE  = DATA_DIR / "win_analysis.json"
LOSS_ANALYSIS_FILE = DATA_DIR / "loss_analysis.json"

for _d in (DATA_DIR, CACHE_DIR, REPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Secrets (.env)
# ---------------------------------------------------------------------------
load_dotenv(ROOT / ".env")

ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_KEY_2  = os.getenv("ANTHROPIC_API_KEY_2", "")   # fallback — partner account
ANTHROPIC_ACCOUNT_NAME   = os.getenv("ANTHROPIC_ACCOUNT_NAME",   "Primary")  # display name for primary
ANTHROPIC_ACCOUNT_NAME_2 = os.getenv("ANTHROPIC_ACCOUNT_NAME_2", "Backup")   # display name for backup
DAILY_COST_USD = float(os.getenv("DAILY_COST_USD") or "0.05")  # estimated $/run for runway calc
# Twelve Data is the primary price source for the technical layer (free tier
# ~800 calls/day, 8/min, and a native 4h interval). Get a free key at
# https://twelvedata.com/pricing  ->  put it in .env as TWELVE_DATA_KEY.
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY", "")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "")  # legacy / unused
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")
FRED_API_KEY = os.getenv("FRED_API_KEY", "")
MY_EMAIL = os.getenv("MY_EMAIL", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_CHAT_ID_2 = os.getenv("TELEGRAM_CHAT_ID_2", "")
TELEGRAM_CHAT_ID_3 = os.getenv("TELEGRAM_CHAT_ID_3", "")

ACCOUNT_BALANCE  = float(os.getenv("ACCOUNT_BALANCE") or "10000")
ACCOUNT_CURRENCY = os.getenv("ACCOUNT_CURRENCY") or "USD"

RISK_PROFILE_FILE = DATA_DIR / "risk_profile.json"

# Stage-2 deep-analysis model. Override with CLAUDE_MODEL in .env.
# Using `or` (not the default= arg) so that an empty string from GitHub Actions
# secrets falls back to the default rather than forwarding "" to the API.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or "claude-sonnet-4-6"
# Stage-1 screener model — fast/cheap, only sees tech+fundamental.
HAIKU_MODEL = os.getenv("HAIKU_MODEL") or "claude-haiku-4-5-20251001"

# Cache time-to-live in hours. Caching keeps repeated same-day runs fast and well
# inside the Twelve Data free-tier rate limits (8/min, ~800/day).
CACHE_TTL_HOURS = float(os.getenv("CACHE_TTL_HOURS", "6"))

# Pairs analysed by the daily automation (daily.py). Override with a comma-separated
# WATCHLIST in .env, e.g. WATCHLIST=EUR/USD,GBP/USD,USD/JPY
# Cost note: each pair = 2 Twelve Data calls + 1 Claude call. With Opus that adds up;
# set CLAUDE_MODEL=claude-sonnet-4-6 in .env for cheaper daily batches.
WATCHLIST = [
    p.strip().upper()
    for p in os.getenv(
        "WATCHLIST", "EUR/USD,GBP/USD,USD/JPY,AUD/USD,USD/CAD,NZD/USD,EUR/GBP,EUR/JPY"
    ).split(",")
    if p.strip()
]

# ---------------------------------------------------------------------------
# Per-currency data mappings
#   rate_fred  : FRED series ID for the central-bank / short-term policy rate
#   cb         : human-readable central bank name (context for the model)
#   cot_market : substring to match in the CFTC COT "market_and_exchange_names"
#   news_terms : query string for NewsAPI
# ---------------------------------------------------------------------------
CURRENCIES = {
    "USD": {
        "rate_fred": "DFF",  # Effective Federal Funds Rate (daily)
        "cb": "Federal Reserve",
        # NOTE: the ICE Dollar Index COT froze in this dataset in Feb 2022, so USD
        # positioning will report as stale/UNAVAILABLE. For a pair, the base
        # currency's COT (e.g. EUR for EUR/USD) carries the meaningful signal anyway.
        "cot_market": "U.S. DOLLAR INDEX - ICE FUTURES U.S.",
        "news_terms": '"Federal Reserve" OR "US dollar" OR FOMC OR "interest rates"',
    },
    "EUR": {
        "rate_fred": "ECBDFR",  # ECB Deposit Facility Rate
        "cb": "European Central Bank",
        "cot_market": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
        "news_terms": '"European Central Bank" OR ECB OR euro OR eurozone',
    },
    "GBP": {
        "rate_fred": "IUDSOIA",  # Bank of England SONIA (proxy for policy rate)
        "cb": "Bank of England",
        "cot_market": "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE",
        "news_terms": '"Bank of England" OR "pound sterling" OR BoE OR "UK economy"',
    },
    "JPY": {
        "rate_fred": "IRSTCI01JPM156N",  # Immediate rates: call money, Japan
        "cb": "Bank of Japan",
        "cot_market": "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE",
        "news_terms": '"Bank of Japan" OR yen OR BoJ OR "Japan economy"',
    },
    "AUD": {
        "rate_fred": "IR3TIB01AUM156N",  # 3-month interbank, Australia
        "cb": "Reserve Bank of Australia",
        "cot_market": "AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE",
        "news_terms": '"Reserve Bank of Australia" OR RBA OR "Australian dollar"',
    },
    "CAD": {
        "rate_fred": "IR3TIB01CAM156N",  # 3-month interbank, Canada
        "cb": "Bank of Canada",
        "cot_market": "CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE",
        "news_terms": '"Bank of Canada" OR "Canadian dollar" OR BoC',
    },
    "CHF": {
        "rate_fred": "IR3TIB01CHM156N",  # 3-month interbank, Switzerland
        "cb": "Swiss National Bank",
        "cot_market": "SWISS FRANC - CHICAGO MERCANTILE EXCHANGE",
        "news_terms": '"Swiss National Bank" OR SNB OR "Swiss franc"',
    },
    "NZD": {
        "rate_fred": "IR3TIB01NZM156N",  # 3-month interbank, New Zealand
        "cb": "Reserve Bank of New Zealand",
        # NOTE: also froze in this dataset in Feb 2022 -> will report as stale.
        "cot_market": "NEW ZEALAND DOLLAR - CHICAGO MERCANTILE EXCHANGE",
        "news_terms": '"Reserve Bank of New Zealand" OR RBNZ OR "New Zealand dollar"',
    },
}

# FRED series for macro / risk-context signals.
# (Spot gold was dropped: FRED discontinued the London fix series and has no
#  reliable free replacement. Oil + VIX + the yield curve cover risk-on/off.)
MACRO_SERIES = {
    "WTI crude oil ($/bbl)": "DCOILWTICO",
    "US 10Y Treasury yield (%)": "DGS10",
    "US 2Y Treasury yield (%)": "DGS2",
    "VIX (volatility index)": "VIXCLS",
    "Trade-weighted US dollar index": "DTWEXBGS",
}

# CFTC Commitments of Traders - Legacy report, Futures Only (Socrata).
# If COT consistently fails, verify the dataset id at https://publicreporting.cftc.gov
COT_DATASET_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"


def missing_keys():
    """Return a list of names for any required secret that is blank.

    TWELVE_DATA_KEY is intentionally NOT required here: if it is absent the
    technical layer degrades gracefully (reported UNAVAILABLE) rather than
    blocking the whole run, so the other four layers still produce analysis.
    """
    required = {
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "NEWS_API_KEY": NEWS_API_KEY,
        "FRED_API_KEY": FRED_API_KEY,
    }
    return [name for name, val in required.items() if not val]

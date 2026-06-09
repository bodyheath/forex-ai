Final forex trade confirmation. Haiku preliminary scores provided — adjust if data warrants.
TRADE_THIS: YES only: confidence>={confidence_threshold}, R:R>={min_rr}:1, >=4 layers agree. UNAVAILABLE=1. Conf<={below_threshold}=NO always.
RSI: <30=8-9BUY 30-35=6-7BUY 35-40=4-5BUY 40-60=1-3NEUTRAL 60-65=4-5SELL 65-70=6-7SELL >70=8-9SELL +/-1 MACD +/-1 Bollinger +/-1 D1+4H aligned.
FUNDAMENTAL: rate diff direction + CB bias. SENTIMENT: news tone. POSITIONING: COT extremes. MACRO: VIX/curve/oil risk-on-off.
Weight SYSTEM MEMORY patterns. Output PAIR: through TRADE_THIS: only. BEST_ENTRY_TIME in Auckland time (NZST=UTC+12 Apr-Sep, NZDT=UTC+13 Sep-Apr).

PAIR: [pair]
DIRECTION: [BUY|SELL]
CONFIDENCE: [n/10]
TECHNICAL_SCORE: [n/10]
FUNDAMENTAL_SCORE: [n/10]
SENTIMENT_SCORE: [n/10]
POSITIONING_SCORE: [n/10]
MACRO_SCORE: [n/10]
ENTRY: [price]
TARGET: [price]
STOP_LOSS: [price]
REWARD_RISK_RATIO: [n:1]
BEST_ENTRY_TIME: [session + window Auckland time]
NEWS_WARNING: [48h events or NONE]
KEY_THESIS: [1 sentence]
RISK_FACTORS: [2 risks]
TRADE_THIS: [YES|NO]

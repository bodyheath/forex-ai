Final forex trade confirmation. Haiku preliminary scores provided — adjust if data warrants.
TRADE_THIS: YES only: confidence>={confidence_threshold}, R:R>={min_rr}:1, MTF>=4/5 timeframes agree, >=4 fundamental layers agree. UNAVAILABLE=1. Conf<={below_threshold}=NO always.
MTF: check MTF line in data — M=monthly(30%) W=weekly(25%) D=daily(20%) 4H=15% 1H=10%. If MTF conf<4/5 then TRADE_THIS NO regardless of other scores.
TECHNICAL (enforce exactly — no exceptions): RSI tiers: <30=9-10BUY 30-35=7-8BUY 35-45=5-6BUY 45-55=3-4NEUTRAL 55-65=4-5SELL 65-70=7-8SELL >70=9-10SELL. Bonuses (+1 each, stackable): MACD confirms direction | BB confirms (lower=BUY/upper=SELL) | price vs SMA50 direction | D1+4H same direction. T_sig in D1 line is the pre-computed Python baseline — use it as TECHNICAL_SCORE unless a strong contradictory factor justifies adjusting by ±1. HARD RULE: TECHNICAL_SCORE>=3 whenever RSI+MACD data present. TECHNICAL_SCORE=1 ONLY when D1:UNAVAILABLE.
CANDLE: PAT= in D1/4H lines = Python-detected pattern (e.g. pin_bar_at_key_level:bul, morning_star:bul, double_bottom:bul). Already factored into T_sig anchor. Use as supporting evidence for CONFIDENCE and KEY_THESIS — do not re-adjust TECHNICAL_SCORE for patterns. Pin bar at key level + RSI oversold = highest-probability BUY setup. Evening star / head and shoulders at resistance = highest-probability SELL setup.
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

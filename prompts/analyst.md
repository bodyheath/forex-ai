Final forex trade confirmation. Haiku preliminary scores provided — adjust if data warrants.
TRADE_THIS: YES only: confidence>={confidence_threshold}, R:R>={min_rr}:1, MTF weekly+daily both agree, >=4 fundamental layers agree. UNAVAILABLE=5 (neutral — missing data has no directional bias). Conf<={below_threshold}=NO always.
MTF: check MTF line in data — M=monthly(context only +5% bonus) W=weekly(40%) D=daily(40%) 4H=20%. Weekly AND daily must both agree on direction for TRADE_THIS YES — 4H is optional bonus. If W and D don't both agree (conf<2/3), TRADE_THIS NO regardless of other scores.
TECHNICAL (enforce exactly — no exceptions): RSI tiers (standard/mixed MTF): <30=9-10BUY 30-35=7-8BUY 35-45=5-6BUY 45-55=3-4NEUTRAL 55-65=4-5SELL 65-70=7-8SELL >70=9-10SELL. RSI TREND CONTEXT — check MTF line before scoring (critical): Confirmed DOWNTREND (W:SELL+D:SELL) — oversold is normal in a downtrend, shift BUY zones lower: RSI<20=9-10BUY, 20-30=7-8BUY, 30-42=5-6BUY; RSI 42-55=NEUTRAL(3-4) — do NOT score 5-6BUY just because RSI is 35-45 in a downtrend. Confirmed UPTREND (W:BUY+D:BUY) — overbought is normal in an uptrend, shift SELL zones higher: RSI>80=9-10SELL, 70-80=7-8SELL, 58-70=4-5SELL; RSI 45-58=NEUTRAL(3-4) — do NOT score 4-5SELL just because RSI is 60-65 in an uptrend. Mixed/neutral MTF: use standard tiers. Bonuses (+1 each, stackable): MACD confirms direction | BB confirms (lower=BUY/upper=SELL) | price vs SMA50 direction | D1+4H same direction. T_sig in D1 line is the pre-computed Python baseline — use it as TECHNICAL_SCORE unless a strong contradictory factor justifies adjusting by ±1. HARD RULE: TECHNICAL_SCORE>=3 whenever RSI+MACD data present. TECHNICAL_SCORE=5 when D1:UNAVAILABLE (neutral — a data fetch failure is not bearish).
CANDLE: PAT= in D1/4H lines = Python-detected pattern (e.g. pin_bar_at_key_level:bul, morning_star:bul, double_bottom:bul). Already factored into T_sig anchor. Use as supporting evidence for CONFIDENCE and KEY_THESIS — do not re-adjust TECHNICAL_SCORE for patterns. Pin bar at key level + RSI oversold = highest-probability BUY setup. Evening star / head and shoulders at resistance = highest-probability SELL setup.
FIB: FIB SH/SL/range/near line = Fibonacci analysis from ~3 months of daily data. If near= is present, price is within 10 pips of that Fibonacci level — strong confluence zone. Fib level + RSI + candlestick pattern = triple-confirmed entry (highest-probability setup). Use nearest_above levels as resistance/target areas for SELL or BUY exit. Use nearest_below as support/entry zones for BUY. Adjust CONFIDENCE +1 if triple confluence present.
DIV: DIV= line = Python-detected RSI divergence. bullish = price lower low + RSI higher low. bearish = price higher high + RSI lower high. If DIV confirms direction: output DIVERGENCE: CONFIRMED and raise CONFIDENCE by 1 (hard cap 10). If DIV conflicts: DIVERGENCE: CONFLICT — reduce CONFIDENCE by 1. If none: DIVERGENCE: NONE. Strong divergence on daily = highest-conviction reversal signal.
OSC: OSC= line = Python-computed oscillator confluence (RSI + Stochastic %K/%D + CCI). Thresholds: RSI<35=BUY RSI>65=SELL | Stoch%K<20+%D<25=BUY Stoch%K>80+%D>75=SELL | CCI<-100=BUY CCI>100=SELL. conf=BUY(3/3) or SELL(3/3): ALL THREE agree — TRIPLE confluence, raise CONFIDENCE by 2, output OSCILLATOR_CONFLUENCE: TRIPLE_BUY or TRIPLE_SELL. conf=BUY(2/3) or SELL(2/3): raise CONFIDENCE by 1, output OSCILLATOR_CONFLUENCE: PARTIAL_BUY or PARTIAL_SELL. conf=NONE: output OSCILLATOR_CONFLUENCE: NONE. Triple oscillator confluence at a Fibonacci level with divergence = the single highest-probability setup in forex.
RIBBON: RIB= line = EMA ribbon (8/13/21/34/55/89). ALIGNED_BULL=all 6 EMAs stacked EMA8>...>EMA89 (fan=spreading/accelerating, +2 already in T_sig). ALIGNED_BEAR=all stacked bearish. CONVERGING=fully stacked but spread narrowing — trend weakening, potential reversal. LEANING_BULL/BEAR=4 of 5 pairs aligned. NEUTRAL=mixed. Use to confirm or question KEY_THESIS: aligned ribbon = trend continuation bias; converging ribbon = higher reversal risk, be cautious with trend entries.
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
DIVERGENCE: [CONFIRMED|NONE|CONFLICT]
OSCILLATOR_CONFLUENCE: [TRIPLE_BUY|TRIPLE_SELL|PARTIAL_BUY|PARTIAL_SELL|NONE]
ENTRY: [price]
TARGET: [price]
STOP_LOSS: [price]
REWARD_RISK_RATIO: [n:1]
BEST_ENTRY_TIME: [session + window Auckland time]
NEWS_WARNING: [48h events or NONE]
KEY_THESIS: [1 sentence]
RISK_FACTORS: [2 risks]
TRADE_THIS: [YES|NO]

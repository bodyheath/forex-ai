import pandas as pd

df = pd.read_csv("data/trades.csv")
fund = df[(df.trade_this.astype(str) == "YES") & (df.status.astype(str).isin(["OPEN", "PENDING"]))]

BALANCE = 9700.0
print("Open/pending fund trades:", len(fund))
print()
for _, r in fund.iterrows():
    pair = str(r.get("pair", ""))
    pct = float(r.get("position_size_pct_at_entry") or 1.0)
    entry = float(r.get("entry") or 0)
    sl = float(r.get("stop_loss") or 0)
    risk_d = BALANCE * pct / 100.0
    pip_size = 0.01 if pair.endswith("JPY") else 0.0001
    stop_pips = abs(entry - sl) / pip_size if entry > 0 and sl > 0 else 0
    dpp = risk_d / stop_pips if stop_pips > 0 else 0
    print(f"  #{int(r.id)} {pair}: pct={pct}% risk=${risk_d:.2f} stop_pips={stop_pips:.1f} dpp=${dpp:.4f} status={r.status}")

print()
print("position_size_pct_at_entry column:")
all_fund = df[df.trade_this.astype(str) == "YES"]
filled = all_fund["position_size_pct_at_entry"].notna().sum()
nan_cnt = all_fund["position_size_pct_at_entry"].isna().sum()
print(f"  {filled} filled, {nan_cnt} NaN (out of {len(all_fund)} total fund trades)")

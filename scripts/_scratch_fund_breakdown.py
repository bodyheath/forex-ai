import re
import pandas as pd

df = pd.read_csv("data/trades.csv", encoding="utf-8-sig")
fund = df[df["trade_this"] == "YES"].copy()
fund["ts"] = pd.to_datetime(fund["timestamp"], errors="coerce")

cutoff = pd.Timestamp("2026-08-26 23:59:59")
post = fund[fund["ts"] > cutoff].copy()
print("candidates clearing conf/RR threshold since 08-27 rewrite:", len(post))
non_skipped = post[post["status"] != "SKIPPED"]
print("of those, actually opened (non-SKIPPED):", len(non_skipped))
print()


def categorize(note):
    note = str(note)
    if note.startswith("Drawdown filter"):
        m = re.search(r"grade=(\w)", note)
        return "Drawdown filter (grade=" + (m.group(1) if m else "?") + ")"
    if "Confidence" in note and "<" in note:
        return "Confidence below dynamic threshold"
    if "Currency concentration" in note:
        return "Currency concentration"
    if "Outside London/NY" in note:
        return "Session timing block"
    if "RANGING_LOW_VOL" in note or "Regime" in note:
        return "Regime block"
    if "Devil" in note or "devil" in note.lower() or " DA " in note:
        return "Devil's Advocate veto"
    if note.strip() == "" or note == "nan":
        return "(no notes)"
    return note[:60]


post["category"] = post["notes"].apply(categorize)
print(post["category"].value_counts())
print()
print("--- all rows since 08-27 rewrite, detail ---")
pd.set_option("display.max_colwidth", 90)
print(post[["id", "timestamp", "pair", "direction", "confidence", "status", "notes"]].to_string())

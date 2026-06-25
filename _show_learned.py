import json

with open('data/loss_journal.json') as f:
    j = json.load(f)

print('=' * 55)
print('WHAT THE SYSTEM LEARNED')
print('=' * 55)
print()

analyses = j.get('analyses', [])
rules = j.get('extracted_rules', [])
patterns = j.get('pattern_counts', {})

print(f'Losses analysed: {len(analyses)}')
print(f'Rules extracted: {len(rules)}')
print()

print('TOP LOSS PATTERNS:')
for pat, count in sorted(patterns.items(), key=lambda x: x[1], reverse=True):
    bar = '█' * count
    print(f'  {pat:<20} {bar} ({count}x)')

print()
print('EXTRACTED RULES:')
for i, r in enumerate(rules, 1):
    print(f'  {i}. {r["rule"]}')
    print(f'     Pair: {r.get("pair", "any")}')
    print()

print('LOSS DETAILS:')
for a in analyses:
    print(f'  #{a.get("trade_id")} {a.get("pair")} {a.get("direction")} {a.get("pips", 0):.0f}p')
    print(f'  Root: {a.get("root_cause", "")[:70]}')
    print(f'  Avoid: {a.get("what_to_avoid", "")[:70]}')
    print()

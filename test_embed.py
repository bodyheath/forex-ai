import sys
sys.path.insert(0, '.')
import src.discord_notifier as dn

_captured = {}
def _fake_send(wh, title, desc, color, fields=None):
    _captured['title'] = title
    _captured['desc'] = desc
    _captured['color'] = hex(color)
    _captured['fields'] = fields or []
    return True

dn._send_embed = _fake_send
dn.WEBHOOK_HEALTH = 'fake'

dn.send_master_scan_report(
    scan_mode='full',
    fund_balance=9923.02,
    daily_pnl_pct=-1.0,
    daily_pnl_dollars=-100.23,
    open_count=5,
    risk_pct=1.0,
    fund_total=9,
    fund_decisive=4,
    fund_wins=2,
    fund_protected=0,
    fund_losses=2,
    fund_win_rate=50.0,
    avg_win_pips=91.8,
    avg_win_dollars=32.0,
    avg_loss_pips=436.0,
    avg_loss_dollars=72.0,
    profit_factor_dollars=0.45,
    best_pair='EUR/HKD',
    best_pips=161.7,
    total_return_pct=-0.77,
    research_open=5,
    research_closed=206,
    research_win_rate=54.0,
    research_decisive=157,
    research_pf=1.1,
    wr_band_45=40.0, wr_band_56=52.0, wr_band_67=58.0, wr_band_7p=61.0,
    n_band_45=20, n_band_56=55, n_band_67=48, n_band_7p=34,
    best_pairs_str='GBP/USD 67% - EUR/JPY 63%',
    adaptive_count=3,
    ml_trained=87,
    ml_accuracy=54.0, ml_recent_wr=57.0, ml_overall_wr=54.0,
    ml_last_retrain='2h ago',
    mta_pct=78.0, hhhl_pct=71.0,
    regime='Ranging', threshold=6.5,
    dq_pct=97.0, td_calls=45, scan_cost=0.0023, scan_duration=12, pairs_analysed=29,
    expiry_alerts=[],
    watch_list=[{'pair': 'GBP/JPY', 'direction': 'BUY', 'conf': 5.8, 'reason': 'Monthly support bounce forming'}],
)

print('TITLE:', _captured.get('title', ''))
print('COLOR:', _captured.get('color', ''))
print('DESC:', _captured.get('desc', ''))
print()
fields = _captured.get('fields', [])
print('Sections:', len(fields))
for i, f in enumerate(fields, 1):
    nm = f.get('name', '')
    vl = f.get('value', '')
    print('--- Section', i, ':', nm, '---')
    print(vl[:400])
    print()

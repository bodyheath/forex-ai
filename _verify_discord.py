import sys
sys.path.insert(0, '.')
from src.discord_notifier import update_fund_dashboard

update_fund_dashboard(
    open_fund_trades=[
        {
            'pair': 'EUR/HKD',
            'direction': 'BUY',
            'entry': 8.95810,
            'current': 8.95700,
            'stop': 8.95150,
            't1': 8.96898,
            't2': 8.98209,
            't3': 9.01050,
            't1_hit': True,
            't2_hit': True,
            't3_hit': False,
            'progress_pct': 9.3,
            'next_target': 'T3',
            'pips_unrealised': 112.2,
            'dollars_unrealised': 22.44,
            'days_open': 0,
            'open_str': '15h',
            'conf': 6.0,
            'checklist_score': 0,
            'id': '1030',
            'risk_dollars': 100,
            'pnl_note': 'T1+T2 banked · 30% running',
        },
        {
            'pair': 'USD/SEK',
            'direction': 'SELL',
            'entry': 9.58910,
            'current': 9.60642,
            'stop': 9.67380,
            't1': 9.55522,
            't2': 9.52981,
            't3': 9.42000,
            't1_hit': False,
            't2_hit': False,
            't3_hit': False,
            'progress_pct': -14.5,
            'next_target': 'T1',
            'pips_unrealised': -173.2,
            'dollars_unrealised': -20.50,
            'days_open': 3,
            'open_str': '3d',
            'conf': 7.0,
            'checklist_score': 0,
            'id': '1076',
            'risk_dollars': 100,
            'pnl_note': 'Full position running',
        },
    ],
    fund_balance=10023.25,
    fund_return_pct=0.23,
    daily_pnl_pct=0.00,
    daily_pnl_dollars=0.00,
    drawdown_pct=0.0,
    sizing_mode='Normal',
    risk_pct=1.0,
    consecutive_wins=0,
    consecutive_losses=0,
    ftmo_current_pct=-0.78,
    unrealised_pnl_dollars=-101.40,
    unrealised_pnl_pips=-298.5,
    total_equity=9921.85,
    fund_total_trades=8,
    fund_wins=1,
    fund_losses=1,
    fund_partial_wins=0,
    fund_breakeven=0,
    fund_win_rate=50.0,
    fund_avg_win_pips=21.9,
    fund_avg_loss_pips=25.0,
    fund_profit_factor=0.88,
    fund_best_trade_pips=21.9,
    fund_best_trade_pair='USD/JPY',
    fund_total_pips=-3.1,
)
print()
print('=== VERIFICATION CHECKLIST ===')
print('Confirm ALL of these in Discord:')
print('1. EUR/HKD shows +112.2p / +$22.44 (not -11p/-$2.23)')
print('2. EUR/HKD shows "T1+T2 banked . 30% running"')
print('3. EUR/HKD progress bar shows ~9%')
print('4. EUR/HKD shows "15h" open (not "0d")')
print('5. USD/SEK shows negative progress bar (all empty)')
print('6. FTMO shows loss bar with -0.78% of 5% daily limit used')
print('7. Stats shows: Wins: 1 . Protected: 0 . Losses: 1')
print('8. No invisible emoji before W/P/L')

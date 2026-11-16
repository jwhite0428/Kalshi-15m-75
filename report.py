"""One-command analysis of real accumulated paper results, no invented backtest."""
import math
from engine import initialize, snapshot

def wilson(wins,n,z=1.96):
    if not n:return None
    p=wins/n; denom=1+z*z/n
    center=(p+z*z/(2*n))/denom
    half=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/denom
    return (max(0,center-half),min(1,center+half))

def main():
    initialize()
    state=snapshot();closed=sorted([t for t in state['trades'] if t['state']=='CLOSED'],key=lambda t:t['exit_at'])
    wins=sum((t['net_pnl'] or 0)>0 for t in closed)
    n=len(closed); net=sum(t['net_pnl'] for t in closed)
    fees=sum(t['entry_fee_usd']+(t['exit_fee_usd'] or 0) for t in closed)
    high=0;cum=0;max_dd=0
    for t in closed:
        cum+=t['net_pnl'];high=max(high,cum);max_dd=max(max_dd,high-cum)
    ci=wilson(wins,n)
    print('SENTINEL V10 PAPER REPORT — REAL RECORDED SIMULATIONS ONLY')
    print(f'Closed trades: {n} | Net profitable: {wins} | Win rate: {wins/n:.1%}' if n else 'Closed trades: 0 | No win rate available')
    print('95% Wilson win-rate interval: '+(f'{ci[0]:.1%} to {ci[1]:.1%}' if ci else 'not available'))
    print(f'Net P&L ${net:+.2f} | Reported simulated taker fees ${fees:.2f}')
    print(f'Mean net P&L per trade ${net/n:+.4f}' if n else 'Mean net P&L unavailable')
    print(f'Maximum closed-equity drawdown ${max_dd:.2f}')
    print(f'Open: {state["stats"]["open"]} | Recorded market decisions: {state["stats"]["evaluated_intervals"]}')
    if n<500:print('Not yet 500 closed trades; 75% target NOT validated.')
    elif ci[0]>.75:print('Observed 95% lower interval above 75%; check selection, fill realism, and profitability.')
    else:print('75% target not statistically demonstrated at 95% confidence.')
    print('Important: This is forward PAPER testing, NOT a historical orderbook backtest or live performance.')

if __name__=='__main__':main()

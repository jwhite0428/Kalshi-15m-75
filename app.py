"""SENTINEL V10 iPhone-friendly PAPER dashboard. Worker runs separately."""
import csv, io, json, os, threading, logging
import requests
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from engine import initialize, snapshot

st.set_page_config(page_title='SENTINEL V10 Paper',page_icon='🎯',layout='wide',initial_sidebar_state='collapsed')

@st.cache_resource(show_spinner=False)
def start_phone_worker():
    from engine import run_worker, initialize
    initialize()
    thread=threading.Thread(target=run_worker,daemon=True,name='sentinel-v10-phone-paper')
    thread.start()
    return thread

# Single Streamlit Cloud instance: scans while the server process stays alive.
# Not dependable 24/7; sleeping/redeploying stops the worker and may delete local SQLite history.
phone_worker=start_phone_worker()

st.markdown('''<style>
html,body,[data-testid="stAppViewContainer"],.stApp{background:#080e19!important;color:#eaf2ff!important}
[data-testid="stSidebar"], [data-testid="stHeader"]{background:#0e1727!important}
[data-testid="stMetric"], [data-testid="stVerticalBlockBorderWrapper"]{background:#111f31!important;border:1px solid #26374c!important;border-radius:14px!important;padding:12px!important}
[data-testid="stMetricLabel"], [data-testid="stMetricValue"], h1,h2,h3{color:#eaf2ff!important}
/* Force contrast on Streamlit light-background alerts and code blocks */
[data-testid="stAlert"] p,[data-testid="stAlert"] span,[data-testid="stAlert"] div,div[data-testid="stNotification"] p {color:#17243a!important} 
[data-testid="stCode"] pre,[data-testid="stCode"] code,[data-testid="stCode"] span {color:#17243a!important} 
[data-testid="stDataFrame"]{border:1px solid #2b425e;border-radius:10px}
div.stButton>button {background:#243d66!important;color:#fff!important;border-radius:12px!important}
input, textarea, [data-baseweb="select"]>div{background:#15273d!important;color:#fff!important}
@media(max-width:650px){.block-container{padding:0.8rem 0.7rem 3rem!important}.stMetric{padding:7px!important}h1{font-size:1.45rem!important}h2{font-size:1.15rem!important}[data-testid="column"]{min-width:0!important}}
</style>''',unsafe_allow_html=True)

st.title('🎯 SENTINEL V10')
st.caption('BTC 15-minute · Adaptive Probability Sniper · PAPER ONLY · Balanced + Hybrid')
st.warning('PHONE-HOSTED MODE: The scan runs only while Streamlit Cloud keeps this server awake. Closing Safari, sleeping apps, or redeployments can stop it. Local history on Streamlit Cloud is temporary. Not guaranteed 24/7.')
st.caption('Paper scanner thread: ' + ('alive' if phone_worker.is_alive() else 'stopped — reload the app') + ' (thread alive does not guarantee successful market scans)')
def get_secret(name):
    try:return st.secrets.get(name,'')
    except (FileNotFoundError, KeyError):return ''
try:
    initialize();data=snapshot();origin='Phone-hosted paper worker (temporary SQLite)'
except Exception as e:
    st.error(f'Unable to read worker state: {e}');st.stop()

with st.sidebar:
    st.header('Dashboard settings')
    interval=st.select_slider('Refresh interval (seconds)',options=[5,10,15,30,60],value=10)
    auto=st.toggle('Auto refresh',value=True)
    st.caption(f'Data from: {origin}')
    st.code('V10_MODE=balanced\nV10_CONTRACTS=1\nV10_POLL_SECONDS=10',language='text')
    st.warning('Streamlit Cloud can sleep or restart. Use a separate always-on worker and persistent storage for 24/7 scans/history.')
if auto:st_autorefresh(interval=interval*1000,key='v10_refresh')

status=data.get('worker_status',{})
if status.get('last_successful_tick'):
    st.caption('Last successful worker scan: '+status['last_successful_tick'])
else:
    st.warning('Worker has not completed a successful market scan yet. Check Events for the latest error.')
if status.get('last_error'):
    st.error('Worker error: '+status['last_error'])
if status.get('last_data_issue'):
    st.warning('BTC FEED: '+status['last_data_issue']+' — New entries paused; open positions still monitored.')
if status.get('last_data_ok'):
    st.caption('Last fresh BTC candles: '+status['last_data_ok'])
stats=data['stats'];trades=data.get('trades',[]);decisions=data.get('decisions',[])
closed=[t for t in trades if t['state']=='CLOSED']
opening=[t for t in trades if t['state']=='OPEN']
rate=stats.get('net_win_rate')
c1,c2=st.columns(2)
c1.metric('Net win rate',f'{rate:.1f}%' if rate is not None else 'No results',delta=f"{len(closed)} closed trades")
c2.metric('Realized P&L',f"${stats['realized_pnl']:+,.2f}")
c3,c4=st.columns(2)
c3.metric('Open paper trades',str(stats['open']))
c4.metric('Available paper cash',f"${stats['available_cash']:,.2f}")
if not closed:st.info('No verified outcomes yet. The 75% target is unproven until enough trades settle.')
else:
    st.progress(min(1,rate/100),text=f'Observed net win rate {rate:.1f}% versus 75% target — {len(closed)} completed trades')
    if len(closed)<500:st.warning(f'Only {len(closed)} of 500 suggested forward-test trades observed. Win rate is not yet reliable.')

st.subheader('🔒 Locked entries · Live positions')
if opening:
    for t in opening:
        with st.container(border=True):
            st.markdown(f"**{t['direction'].upper()} · {t['ticker']}**")
            a,b,c=st.columns(3)
            a.metric('BUY',f"{t['entry_cents']:.2f}¢")
            b.metric('TP',f"{t['tp_cents']:.2f}¢")
            c.metric('SL',f"{t['sl_cents']:.2f}¢")
            st.caption(f"{t['qty']} contract(s) | Evidence: {t['score']}% ({t['confidence_label']}) | Entry: {t['entry_at']}")
else:st.info('No active paper positions. The independent worker is responsible for entries and exits.')

st.subheader('📊 Current interval decisions')
if decisions:
    d=pd.DataFrame(decisions)
    fields=['at','ticker','decision','reason','direction','score','seconds_left','ask_cents','spread_cents','last_spot']
    st.dataframe(d[[c for c in fields if c in d.columns]],hide_index=True,use_container_width=True)
else:st.caption('No intervals scanned by the worker yet.')

st.subheader('📈 Trade history')
if trades:
    tdf=pd.DataFrame(trades)
    fields=['ticker','direction','state','entry_at','entry_cents','tp_cents','sl_cents','exit_cents','exit_reason','outcome','net_pnl','score','entry_fee_usd','exit_fee_usd']
    st.dataframe(tdf[[c for c in fields if c in tdf.columns]],hide_index=True,use_container_width=True)
    output=tdf.to_csv(index=False).encode('utf-8')
    st.download_button('Download full trade history CSV',output,'sentinel_v10_trades.csv','text/csv',use_container_width=True)
else:st.caption('Your entries and exits will appear here after the worker trades.')

st.subheader('🧪 Performance validation')
settled=[t for t in closed if t['exit_reason']=='SETTLEMENT']
a,b=st.columns(2)
a.metric('Settlement direction accuracy',f"{stats['direction_win_rate']:.1f}%" if stats['direction_win_rate'] is not None else '—')
b.metric('Settled contracts',str(len(settled)))
st.caption('Net win rate counts positive net realized P&L on each completed trade. Direction accuracy only includes officially settled trades. These are different statistics. Evidence scores are directional heuristics, NOT calibrated 75% probabilities.')
if closed:
    df=pd.DataFrame(closed).sort_values('exit_at')
    df['cumulative_net_pnl']=df['net_pnl'].cumsum()
    st.line_chart(df.set_index('exit_at')['cumulative_net_pnl'],height=200)

with st.expander('Events, alerts and risk notes'):
    for e in data.get('events',[])[:25]:
        st.write(f"{e['at']} · {e['level']} · {e['message']}")
    st.caption('A worker needs an always-on PC or server. Streamlit Cloud alone does not keep a background worker alive. Remote API must use HTTPS and a strong bearer token. If market data, orderbook liquidity or official settlement is unavailable, the bot records a wait/skip instead of faking a trade.')
    st.caption('Coinbase spot prices are a proxy and do not determine Kalshi settlement. Fees are estimates and should be verified against your specific Kalshi market. No backtest or live profitability is claimed.')

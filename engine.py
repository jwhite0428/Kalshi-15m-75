"""SENTINEL V10 paper engine. Public market data ONLY. No order submission code."""
from __future__ import annotations
import json, math, os, sqlite3, threading, time, logging, statistics
from datetime import datetime, timezone
from urllib.parse import quote
import requests

LOG = logging.getLogger('sentinel_v10')
API = 'https://external-api.kalshi.com/trade-api/v2'
COINBASE = 'https://api.exchange.coinbase.com'
KRAKEN = 'https://api.kraken.com/0/public/OHLC'
MAX_CANDLE_AGE_SECONDS = 130  # age measured from start of the latest 60-second candle
SERIES = 'KXBTC15M'
DB_FILE = os.environ.get('V10_DB', os.path.join(os.path.dirname(__file__), 'sentinel_v10.sqlite3'))
SESSION = requests.Session()
SESSION.headers.update({'User-Agent':'SentinelV10Paper/1.0 (public market data)', 'Accept':'application/json'})
CONFIG = {
    'mode': os.environ.get('V10_MODE','balanced').lower(),
    'contracts': max(1,int(os.environ.get('V10_CONTRACTS','1'))),
    'poll_seconds': max(5,int(os.environ.get('V10_POLL_SECONDS','10'))),
    'entry_after_seconds': 540,       # 9 minutes elapsed
    'target_entry_by_seconds': 720,   # 12 minutes elapsed
    'min_seconds_remaining': 60,
    'max_spread_cents': 8.0,
    'slippage_cents': 0.5,            # conservative execution friction on top of BBO
    'min_confidence_selective': 75.0,
    'starting_cash': 1000.0,
    'min_cash_reserve': 0.0,
}

def utc_now(): return datetime.now(timezone.utc)
def iso(t=None): return (t or utc_now()).isoformat()
def parse_dt(s):
    if not s: return None
    try: return datetime.fromisoformat(str(s).replace('Z','+00:00')).astimezone(timezone.utc)
    except (ValueError,TypeError): return None

def get_json(url, params=None):
    r=SESSION.get(url,params=params,timeout=12)
    r.raise_for_status()
    return r.json()

def connect():
    os.makedirs(os.path.dirname(os.path.abspath(DB_FILE)),exist_ok=True)
    db=sqlite3.connect(DB_FILE,timeout=20)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=20000')
    return db

def initialize():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS trades(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL UNIQUE,
          direction TEXT NOT NULL, qty INTEGER NOT NULL, entry_cents REAL NOT NULL,
          entry_fee_usd REAL NOT NULL, entry_at TEXT NOT NULL, entry_bid_cents REAL,
          entry_ask_cents REAL, entry_spread_cents REAL, strike REAL,
          spot REAL, spot_source TEXT, score REAL, confidence_label TEXT,
          signals_json TEXT, estimated_probability REAL,
          tp_cents REAL NOT NULL, sl_cents REAL NOT NULL,
          exit_cents REAL, exit_fee_usd REAL, exit_at TEXT, exit_reason TEXT,
          outcome TEXT, net_pnl REAL, state TEXT NOT NULL DEFAULT 'OPEN');
        CREATE TABLE IF NOT EXISTS decisions(
          ticker TEXT PRIMARY KEY, at TEXT NOT NULL, decision TEXT NOT NULL,
          reason TEXT NOT NULL, direction TEXT, score REAL, seconds_left REAL,
          ask_cents REAL, spread_cents REAL, last_spot REAL);
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
          level TEXT NOT NULL, message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS samples(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, at TEXT NOT NULL,
          side TEXT, ask_cents REAL, bid_cents REAL, score REAL, spot REAL,
          time_left INTEGER, entered INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS samples_ticker_at ON samples(ticker,at);
        ''')
        db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('starting_cash',str(CONFIG['starting_cash'])))

def event(db, level, message):
    db.execute('INSERT INTO events(at,level,message) VALUES(?,?,?)',(iso(),level,str(message)[:500]))

def current_market(now=None):
    now=now or utc_now()
    payload=get_json(API+'/markets',{'series_ticker':SERIES,'status':'open','limit':200})
    markets=[]
    for m in payload.get('markets',[]):
        if not m.get('ticker','').startswith(SERIES+'-'): continue
        close=parse_dt(m.get('close_time'))
        start=parse_dt(m.get('open_time'))
        if not close or close<=now or (start and start>now): continue
        if m.get('status','').lower() not in ('active','open'):continue
        markets.append((close,m))
    if not markets: raise ValueError('No open KXBTC15M interval available')
    return min(markets,key=lambda item:item[0])[1]

def market_detail(ticker):
    return get_json(API+'/markets/'+quote(ticker,safe='')).get('market',{})

def market_orderbook(ticker):
    data=get_json(API+'/markets/'+quote(ticker,safe='')+'/orderbook')
    return data.get('orderbook_fp') or data.get('orderbook') or {}

def numeric(x):
    try:
        f=float(x)
        return f if math.isfinite(f) else None
    except (ValueError,TypeError):return None

def market_cents(m,field):
    v=numeric(m.get(field+'_dollars'))
    if v is not None:return v*100
    return numeric(m.get(field))

def book_bids(book,side):
    key=side+'_dollars'
    is_dollars=key in book
    rows=book.get(key,[]) if is_dollars else book.get(side,[])
    if isinstance(rows,dict):rows=rows.get('bids',[])
    result=[]
    for entry in rows or []:
        if isinstance(entry,(tuple,list)) and len(entry)>=2:price,size=entry[:2]
        elif isinstance(entry,dict):
            price=entry.get('price_dollars' if is_dollars else 'price')
            size=entry.get('count_fp',entry.get('quantity',entry.get('count')))
        else: continue
        p=numeric(price);q=numeric(size)
        if p is not None and q is not None:
            if is_dollars:p*=100
            if 0<p<100 and q>0:result.append((p,q))
    return sorted(result,reverse=True)

def quotes(m,book):
    """Orderbook lists bids for each side; opposing bids imply an executable ask."""
    bids={s:book_bids(book,s) for s in ('yes','no')}
    result={}
    for side,opp in (('yes','no'),('no','yes')):
        bid=bids[side][0][0] if bids[side] else None
        ask=100-bids[opp][0][0] if bids[opp] else None
        # Use real book only; market metadata can be stale and is not executable.
        result[side]={'bid':bid,'ask':ask,
                      'bid_depth':bids[side][0][1] if bids[side] else 0,
                      'ask_depth':bids[opp][0][1] if bids[opp] else 0,
                      'spread':ask-bid if ask is not None and bid is not None else None}
    return result

def _validate_candles(rows, source):
    """Do not paper trade from missing, future-dated or old minute candles.

    Each timestamp is the BEGINNING of a 60-second bucket, not its closing time.
    A timestamp up to ~60s old can be perfectly normal; 130s still means stale.
    """
    rows = sorted(rows, key=lambda r: r[0])
    if len(rows) < 40:
        raise ValueError(f'{source}: only {len(rows)} BTC candles available')
    last_ts = rows[-1][0]
    age = time.time() - last_ts
    if age < -15:
        raise ValueError(f'{source}: future-dated BTC candle ({age:.0f}s)')
    if age > MAX_CANDLE_AGE_SECONDS:
        raise ValueError(f'{source}: stale BTC candles ({age:.0f}s)')
    if not all(0 < r[2] < 1_000_000 and r[3] >= 0 for r in rows[-40:]):
        raise ValueError(f'{source}: invalid candle values')
    return rows


def coinbase_candles():
    data = get_json(COINBASE+'/products/BTC-USD/candles', {'granularity':60})
    if not isinstance(data, list):
        raise ValueError('Coinbase candle response invalid')
    # Coinbase [bucket_start, low, high, open, close, volume]
    rows = [(int(x[0]),float(x[3]),float(x[4]),float(x[5]))
            for x in data if isinstance(x,(list,tuple)) and len(x)>=6]
    return _validate_candles(rows,'Coinbase')


def kraken_candles():
    payload = get_json(KRAKEN, {'pair':'XBTUSD','interval':1})
    if not isinstance(payload,dict) or payload.get('error'):
        raise ValueError(f'Kraken OHLC error: {str(payload.get("error"))[:120]}')
    result = payload.get('result') or {}
    keys = [k for k in result if k!='last' and isinstance(result[k],list)]
    if len(keys)!=1:
        raise ValueError('Kraken OHLC payload contains no unique BTC/USD candle series')
    # Kraken [bucket_start, open, high, low, close, vwap, volume, trade_count]
    rows = [(int(x[0]),float(x[1]),float(x[4]),float(x[6]))
            for x in result[keys[0]] if isinstance(x,(list,tuple)) and len(x)>=7]
    return _validate_candles(rows,'Kraken')


def fresh_btc_candles():
    """Prefer Coinbase; fail over to Kraken if its candles are genuinely fresher.

    Never transform an old close into a current price or fake missing candles.
    """
    failures=[]
    for name,fetch in (('Coinbase',coinbase_candles),('Kraken',kraken_candles)):
        try:
            return fetch(),name
        except (ValueError,TypeError,KeyError,IndexError,requests.RequestException) as exc:
            failures.append(f'{name}: {str(exc)[:145]}')
    raise ValueError('No fresh BTC candle source; '+ ' | '.join(failures))

def signal_analysis(candles,strike,remaining,yes_ask,no_ask):
    """Heuristic directional evidence; score is NOT a calibrated probability."""
    closes=[v[2] for v in candles]
    volumes=[v[3] for v in candles]
    def ema(values,n):
        a=2/(n+1);v=values[0]
        for x in values[1:]:v=a*x+(1-a)*v
        return v
    change=[closes[i]-closes[i-1] for i in range(1,len(closes))]
    last=closes[-1]
    gains=sum(max(0,d) for d in change[-7:])/7
    losses=sum(max(0,-d) for d in change[-7:])/7
    rsi=100 if losses==0 else 100-100/(1+gains/losses)
    e9,e21=ema(closes[-80:],9),ema(closes[-80:],21)
    vwap=sum(x*v for x,v in zip(closes[-30:],volumes[-30:]))/max(sum(volumes[-30:]),1e-12)
    minute_returns=[math.log(closes[i]/closes[i-1]) for i in range(max(1,len(closes)-61),len(closes))]
    volatility=statistics.stdev(minute_returns) if len(minute_returns)>2 else 0.0006
    projected=max(last*volatility*math.sqrt(max(remaining,60)/60),1.0)
    gap=(last-strike) if strike else 0
    signals={
      'Strike cushion': 2 if gap>projected else (-2 if gap< -projected else (1 if gap>0 else -1)),
      'EMA 9/21':1 if e9>e21 else -1,
      'RSI 7':1 if rsi>53 else (-1 if rsi<47 else 0),
      'VWAP 30':1 if last>vwap else -1,
      'Recent momentum':1 if closes[-1]>closes[-4] else -1,
      'Trend persistence':1 if sum(x>0 for x in change[-8:])>=5 else (-1 if sum(x<0 for x in change[-8:])>=5 else 0),
      'Last-minute direction':1 if change[-1]>0 else -1,
      'Quote favorite':1 if yes_ask is not None and no_ask is not None and yes_ask>no_ask else -1,
    }
    score=sum(signals.values())
    direction='yes' if score>=0 else 'no'
    strength=min(99,round(50+abs(score)/10*45,1))
    confidence_label='HIGH EVIDENCE' if strength>=75 else ('MEDIUM EVIDENCE' if strength>=62 else 'LOW EVIDENCE')
    return {'direction':direction,'score':score,'evidence':strength,'confidence_label':confidence_label,
            'signals':signals,'spot':last,'volatility':volatility,'projected_move':projected,
            'distance':gap,'rsi':rsi,'ema9':e9,'ema21':e21,'vwap':vwap}

def fee(price_cents,qty):
    """Indicative Kalshi taker fee rounded UP per order; verify series fee schedule."""
    p=max(0,min(1,price_cents/100))
    return math.ceil((0.07*qty*p*(1-p))*100-1e-10)/100

def paper_buy_fill(q,qty):
    if not q or q['ask'] is None or q['ask_depth']<qty:return None
    return min(99.9,q['ask']+CONFIG['slippage_cents'])

def paper_sell_fill(q,qty):
    if not q or q['bid'] is None or q['bid_depth']<qty:return None
    return max(0.1,q['bid']-CONFIG['slippage_cents'])

def decision(db,ticker,decision_code,reason,remaining=None,a=None,q=None):
    db.execute('''INSERT INTO decisions(ticker,at,decision,reason,direction,score,seconds_left,ask_cents,spread_cents,last_spot)
    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET at=excluded.at,decision=excluded.decision,
    reason=excluded.reason,direction=excluded.direction,score=excluded.score,seconds_left=excluded.seconds_left,
    ask_cents=excluded.ask_cents,spread_cents=excluded.spread_cents,last_spot=excluded.last_spot''',
    (ticker,iso(),decision_code,reason,a.get('direction') if a else None,a.get('evidence') if a else None,
     remaining,q.get('ask') if q else None,q.get('spread') if q else None,a.get('spot') if a else None))

def buy(db,m,q,a,remaining):
    ticker=m['ticker'];side=a['direction'];qty=CONFIG['contracts']
    fill=paper_buy_fill(q,qty)
    if fill is None:raise ValueError('Insufficient executable ask liquidity')
    cost=qty*fill/100+fee(fill,qty)
    cash=starting_cash(db)+db.execute('SELECT COALESCE(SUM(net_pnl),0) FROM trades WHERE state="CLOSED"').fetchone()[0]
    invested=db.execute('SELECT COALESCE(SUM((entry_cents*qty)/100+entry_fee_usd),0) FROM trades WHERE state="OPEN"').fetchone()[0]
    if cash-invested-cost<CONFIG['min_cash_reserve']:raise ValueError('Insufficient paper balance')
    tp=min(99.5,fill+max(4.0,min(14.0,(100-fill)*0.50)))
    sl=max(0.5,fill-max(5.0,min(13.0,(100-fill)*0.40)))
    params=(ticker,side,qty,fill,fee(fill,qty),iso(),q['bid'],q['ask'],q['spread'],
            numeric(m.get('floor_strike')),a['spot'],a.get('spot_source','BTC/USD proxy')+' BTC/USD proxy',a['evidence'],a['confidence_label'],
            json.dumps(a['signals']),None,tp,sl)
    db.execute('''INSERT INTO trades(ticker,direction,qty,entry_cents,entry_fee_usd,entry_at,
      entry_bid_cents,entry_ask_cents,entry_spread_cents,strike,spot,spot_source,score,
      confidence_label,signals_json,estimated_probability,tp_cents,sl_cents)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',params)
    decision(db,ticker,'ENTERED','One paper trade for interval',remaining,a,q)
    event(db,'ENTRY',f'{ticker} {side.upper()} paper BUY {qty} @ {fill:.2f}¢; TP {tp:.2f}¢ / SL {sl:.2f}¢')

def starting_cash(db):
    row=db.execute('SELECT value FROM meta WHERE key="starting_cash"').fetchone()
    return float(row[0]) if row else CONFIG['starting_cash']

def close_position(db,row,exit_cents,reason,outcome=None):
    qty=row['qty'];exfee=fee(exit_cents,qty) if reason!='SETTLEMENT' else 0.0
    pnl=(exit_cents-row['entry_cents'])*qty/100-row['entry_fee_usd']-exfee
    db.execute('''UPDATE trades SET exit_cents=?,exit_fee_usd=?,exit_at=?,exit_reason=?,outcome=?,net_pnl=?,state='CLOSED' WHERE id=? AND state='OPEN' ''',
               (exit_cents,exfee,iso(),reason,outcome,round(pnl,5),row['id']))
    event(db,'EXIT',f"{row['ticker']} {reason}: {exit_cents:.2f}¢, net ${pnl:+.2f}")

def settle_open(db):
    """Never settle based on Coinbase! Only official Kalshi final result."""
    rows=db.execute('SELECT * FROM trades WHERE state="OPEN"').fetchall()
    for row in rows:
        try:
            market=market_detail(row['ticker'])
            result=str(market.get('result') or '').lower()
            if result in ('yes','no'):
                close_position(db,row,100.0 if row['direction']==result else 0.0,'SETTLEMENT',result)
        except Exception as e:
            LOG.warning('Settlement check %s: %s',row['ticker'],e)

def manage_open(db):
    for row in db.execute('SELECT * FROM trades WHERE state="OPEN"').fetchall():
        try:
            m=market_detail(row['ticker'])
            result=str(m.get('result') or '').lower()
            if result in ('yes','no'):
                close_position(db,row,100 if row['direction']==result else 0,'SETTLEMENT',result)
                continue
            close=parse_dt(m.get('close_time'))
            if not close or utc_now()>=close:continue
            q=quotes(m,market_orderbook(row['ticker']))[row['direction']]
            sell=paper_sell_fill(q,row['qty'])
            if sell is None:continue
            # Hybrid exit only if executable observed bid after slippage, no last-trade fantasy.
            if sell>=row['tp_cents']: close_position(db,row,sell,'TAKE_PROFIT')
            elif sell<=row['sl_cents']:close_position(db,row,sell,'STOP_LOSS')
        except Exception as e:
            LOG.warning('Manage open %s: %s',row['ticker'],e)

def tick():
    with connect() as db:
        manage_open(db)
        m=current_market(); ticker=m['ticker']
        now=utc_now();close=parse_dt(m.get('close_time'))
        remaining=(close-now).total_seconds()
        start=close.timestamp()-900
        elapsed=now.timestamp()-start
        if db.execute('SELECT 1 FROM trades WHERE ticker=?',(ticker,)).fetchone():return
        book=market_orderbook(ticker);qs=quotes(m,book)
        strike=numeric(m.get('floor_strike'))
        if strike is None or strike<1000:
            decision(db,ticker,'WAIT','Official strike unavailable or implausible',remaining);return
        candles,source=fresh_btc_candles()
        a=signal_analysis(candles,strike,remaining,qs['yes']['ask'],qs['no']['ask'])
        a['spot_source']=source
        q=qs[a['direction']]
        db.execute('INSERT INTO samples(ticker,at,side,ask_cents,bid_cents,score,spot,time_left) VALUES(?,?,?,?,?,?,?,?)',
                   (ticker,iso(),a['direction'],q['ask'],q['bid'],a['evidence'],a['spot'],int(remaining)))
        if elapsed<CONFIG['entry_after_seconds']:
            decision(db,ticker,'WAIT',f'Entry window begins at minute 9 ({elapsed/60:.1f}m elapsed)',remaining,a,q);return
        if remaining<CONFIG['min_seconds_remaining']:
            decision(db,ticker,'SKIPPED','Too close to settlement; would not assume fill',remaining,a,q);return
        if CONFIG['mode']=='selective' and a['evidence']<CONFIG['min_confidence_selective']:
            decision(db,ticker,'WAIT' if elapsed<CONFIG['target_entry_by_seconds'] else 'SKIPPED','Selective confidence not met',remaining,a,q);return
        if q['ask'] is None or q['bid'] is None or q['spread'] is None or q['spread']<0:
            decision(db,ticker,'WAIT','No two-sided executable order book',remaining,a,q);return
        if q['ask']<=0 or q['ask']>=99.5:
            decision(db,ticker,'WAIT','Contract ask outside tradable range',remaining,a,q);return
        # balanced: aim earlier when quote tight; at minute 12 relax spread but never invent fills
        spread_limit=CONFIG['max_spread_cents'] if elapsed>=CONFIG['target_entry_by_seconds'] else min(4,CONFIG['max_spread_cents'])
        if q['spread']>spread_limit:
            decision(db,ticker,'WAIT','Spread exceeds paper execution limit',remaining,a,q);return
        if q['ask_depth']<CONFIG['contracts']:
            decision(db,ticker,'WAIT','Not enough contracts at best ask',remaining,a,q);return
        try:buy(db,m,q,a,remaining)
        except (sqlite3.IntegrityError,ValueError) as e:
            decision(db,ticker,'SKIPPED',str(e),remaining,a,q)

def snapshot():
    with connect() as db:
        trades=[dict(r) for r in db.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 1000')]
        decisions=[dict(r) for r in db.execute('SELECT * FROM decisions ORDER BY at DESC LIMIT 25')]
        events=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 50')]
        n=db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
        init_cash=starting_cash(db)
        meta={r['key']:r['value'] for r in db.execute('SELECT key,value FROM meta')}
    closed=[t for t in trades if t['state']=='CLOSED']
    wins=[t for t in closed if t['net_pnl']>0]
    settled=[t for t in closed if t['exit_reason']=='SETTLEMENT']
    direction_wins=[t for t in settled if t['outcome']==t['direction']]
    pnl=sum(t['net_pnl'] for t in closed)
    invested=sum(t['entry_cents']*t['qty']/100+t['entry_fee_usd'] for t in trades if t['state']=='OPEN')
    return {'at':iso(),'worker_status':{'last_successful_tick':meta.get('last_successful_tick'),'last_error':meta.get('last_error')},'trades':trades,'decisions':decisions,'events':events,'samples_count':n,
            'stats':{'starting_cash':init_cash,'realized_pnl':round(pnl,2),'available_cash':round(init_cash+pnl-invested,2),
             'closed':len(closed),'open':len(trades)-len(closed),'net_wins':len(wins),
             'net_win_rate':round(100*len(wins)/len(closed),2) if closed else None,
             'settled':len(settled),'direction_win_rate':round(100*len(direction_wins)/len(settled),2) if settled else None,
             'target_75_met':(len(wins)/len(closed)>=.75) if closed else None,
             'evaluated_intervals':len(decisions)}}

def run_worker(stop_event=None):
    initialize();LOG.info('SENTINEL V10 starting: %s mode',CONFIG['mode'])
    while stop_event is None or not stop_event.is_set():
        try:
            tick()
            with connect() as db:
                db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('last_successful_tick',iso()))
        except Exception as e:
            LOG.exception('Tick failed')
            with connect() as db:
                event(db,'ERROR',f'{type(e).__name__}: {e}')
                db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('last_error',f'{iso()} | {type(e).__name__}: {str(e)[:300]}'))
        if stop_event:stop_event.wait(CONFIG['poll_seconds'])
        else:time.sleep(CONFIG['poll_seconds'])

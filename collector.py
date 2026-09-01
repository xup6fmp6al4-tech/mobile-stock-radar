from __future__ import annotations
import asyncio, json, sys
from datetime import datetime, timezone
import httpx
from db import db, init_db, USING_POSTGRES
from config import CORE_SYMBOLS, WATCHLIST_SYMBOLS

REQUIREMENTS=[
("intraday_1m","Yahoo 1m OHLCV","active","Forward capture; provider history is limited."),
("market_indices","TWII/NASDAQ/SOX","active","Yahoo symbols."),
("commodities_fx","Gold/Oil/DXY","active","Yahoo symbols."),
("twse_institutional","TWSE foreign/trust/dealer flows","gap","Official daily adapter pending."),
("margin_short","TWSE margin/short balance","gap","Official daily adapter pending."),
("broker_branches","Broker branch net buy/sell","gap","Reliable machine-readable source required."),
("taifex_intraday","TX/MTX day+night intraday","gap","Forward intraday adapter still required."),
("taifex_institutional","TAIFEX institutional positions","gap","Official daily adapter pending."),
("news_timestamps","Timestamped catalysts/news","gap","News event store pending."),
]

def now_iso(): return datetime.now(timezone.utc).isoformat()

def seed_requirements():
    with db() as con:
        for key,label,state,detail in REQUIREMENTS:
            if USING_POSTGRES:
                with con.cursor() as cur:
                    cur.execute("""INSERT INTO source_status(key,label,state,detail,updated_at) VALUES(%s,%s,%s,%s,%s)
                    ON CONFLICT(key) DO UPDATE SET label=EXCLUDED.label,state=EXCLUDED.state,detail=EXCLUDED.detail,updated_at=EXCLUDED.updated_at""",
                    (key,label,state,detail,now_iso()))
            else:
                con.execute("""INSERT INTO source_status(key,label,state,detail,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET label=excluded.label,state=excluded.state,detail=excluded.detail,updated_at=excluded.updated_at""",
                (key,label,state,detail,now_iso()))

async def fetch_yahoo(client,symbol):
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r=await client.get(url,params={"interval":"1m","range":"1d","includePrePost":"true","events":"div,splits"},timeout=20)
    r.raise_for_status(); p=r.json(); result=((p.get("chart") or {}).get("result") or [None])[0]
    if not result: raise RuntimeError(str((p.get("chart") or {}).get("error")))
    ts=result.get("timestamp") or []; quote=(((result.get("indicators") or {}).get("quote") or [{}])[0]); rows=[]
    for i,t in enumerate(ts):
        def at(k):
            a=quote.get(k) or []; return a[i] if i<len(a) else None
        o,h,l,c,v=at("open"),at("high"),at("low"),at("close"),at("volume")
        if c is not None: rows.append((symbol,int(t),o,h,l,c,v,"yahoo",now_iso()))
    meta=result.get("meta") or {}
    return r.status_code,rows,{"exchange":meta.get("exchangeName"),"timezone":meta.get("exchangeTimezoneName"),"regularMarketPrice":meta.get("regularMarketPrice")}

def begin_capture(symbol):
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.execute("INSERT INTO captures(source,symbol,started_at) VALUES(%s,%s,%s) RETURNING id",("yahoo",symbol,now_iso())); return cur.fetchone()[0]
        cur=con.execute("INSERT INTO captures(source,symbol,started_at) VALUES(?,?,?)",("yahoo",symbol,now_iso())); return cur.lastrowid

def save_ok(cid,rows,status,meta):
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.executemany("""INSERT INTO bars(symbol,ts_utc,open,high,low,close,volume,source,captured_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(symbol,ts_utc) DO UPDATE SET open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,volume=EXCLUDED.volume,captured_at=EXCLUDED.captured_at""",rows)
                cur.execute("UPDATE captures SET finished_at=%s,ok=1,http_status=%s,rows_written=%s,raw_meta=%s WHERE id=%s",(now_iso(),status,len(rows),json.dumps(meta,ensure_ascii=False),cid))
        else:
            con.executemany("""INSERT INTO bars(symbol,ts_utc,open,high,low,close,volume,source,captured_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,ts_utc) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,captured_at=excluded.captured_at""",rows)
            con.execute("UPDATE captures SET finished_at=?,ok=1,http_status=?,rows_written=?,raw_meta=? WHERE id=?",(now_iso(),status,len(rows),json.dumps(meta,ensure_ascii=False),cid))

def save_err(cid,error):
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur: cur.execute("UPDATE captures SET finished_at=%s,ok=0,error=%s WHERE id=%s",(now_iso(),str(error)[:1000],cid))
        else: con.execute("UPDATE captures SET finished_at=?,ok=0,error=? WHERE id=?",(now_iso(),str(error)[:1000],cid))

async def capture(symbols):
    out={"requested":len(symbols),"ok":0,"failed":0,"rows":0,"errors":{}}
    sem=asyncio.Semaphore(5)
    async with httpx.AsyncClient(headers={"User-Agent":"Mozilla/5.0 RadarBlackBox/1.1"},follow_redirects=True) as client:
        async def one(s):
            cid=begin_capture(s)
            try:
                async with sem: status,rows,meta=await fetch_yahoo(client,s)
                save_ok(cid,rows,status,meta); out["ok"]+=1; out["rows"]+=len(rows)
            except Exception as e:
                save_err(cid,e); out["failed"]+=1; out["errors"][s]=str(e)
        await asyncio.gather(*(one(s) for s in symbols))
    print(json.dumps(out,ensure_ascii=False))
    return out

if __name__=="__main__":
    init_db(); seed_requirements()
    full="--full" in sys.argv
    asyncio.run(capture(WATCHLIST_SYMBOLS if full else CORE_SYMBOLS))

from __future__ import annotations
import asyncio, json, sys, os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import httpx
from db import db, init_db, USING_POSTGRES, fetchone_dict
from config import (
    CORE_SYMBOLS, WATCHLIST_SYMBOLS,
    RAW_1M_RETENTION_DAYS, ARCHIVE_5M_RETENTION_DAYS,
    CAPTURE_SUCCESS_RETENTION_DAYS, CAPTURE_ERROR_RETENTION_DAYS,
    MAINTENANCE_INTERVAL_HOURS, STORAGE_SOFT_LIMIT_MB,
)

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
def now_epoch(): return int(datetime.now(timezone.utc).timestamp())

def seed_requirements():
    with db() as con:
        for key,label,state,detail in REQUIREMENTS:
            if USING_POSTGRES:
                with con.cursor() as cur:
                    cur.execute("""INSERT INTO source_status(key,label,state,detail,updated_at) VALUES(%s,%s,%s,%s,%s)
                    ON CONFLICT(key) DO UPDATE SET label=EXCLUDED.label,state=EXCLUDED.state,detail=EXCLUDED.detail""",
                    (key,label,state,detail,now_iso()))
            else:
                con.execute("""INSERT INTO source_status(key,label,state,detail,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET label=excluded.label,state=excluded.state,detail=excluded.detail""",
                (key,label,state,detail,now_iso()))

async def fetch_yahoo(client,symbol):
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r=await client.get(url,params={"interval":"1m","range":"1d","includePrePost":"true","events":"div,splits"},timeout=20)
    r.raise_for_status(); p=r.json(); result=((p.get("chart") or {}).get("result") or [None])[0]
    if not result: raise RuntimeError(str((p.get("chart") or {}).get("error")))
    ts=result.get("timestamp") or []; quote=(((result.get("indicators") or {}).get("quote") or [{}])[0]); rows=[]
    stamp=now_iso()
    for i,t in enumerate(ts):
        def at(k):
            a=quote.get(k) or []; return a[i] if i<len(a) else None
        o,h,l,c,v=at("open"),at("high"),at("low"),at("close"),at("volume")
        if c is not None: rows.append((symbol,int(t),o,h,l,c,v,"yahoo",stamp))
    meta=result.get("meta") or {}
    return r.status_code,rows,{"exchange":meta.get("exchangeName"),"timezone":meta.get("exchangeTimezoneName"),"regularMarketPrice":meta.get("regularMarketPrice")}

def latest_ts(symbol):
    p="%s" if USING_POSTGRES else "?"
    with db() as con:
        r=fetchone_dict(con,f"SELECT MAX(ts_utc) AS ts FROM bars WHERE symbol={p}",(symbol,))
        return int(r["ts"]) if r and r.get("ts") is not None else None

def begin_batch_capture(count):
    meta=json.dumps({"requested":count,"phase":"started"},ensure_ascii=False)
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.execute("INSERT INTO captures(source,symbol,started_at,raw_meta) VALUES(%s,%s,%s,%s) RETURNING id",("yahoo_batch",None,now_iso(),meta)); return cur.fetchone()[0]
        cur=con.execute("INSERT INTO captures(source,symbol,started_at,raw_meta) VALUES(?,?,?,?)",("yahoo_batch",None,now_iso(),meta)); return cur.lastrowid

def finish_batch_capture(cid,out):
    payload=json.dumps(out,ensure_ascii=False,separators=(",",":"))
    ok=1 if out["failed"]==0 else 0
    error=None if ok else f'{out["failed"]} symbol(s) failed'
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.execute("UPDATE captures SET finished_at=%s,ok=%s,http_status=%s,rows_written=%s,error=%s,raw_meta=%s WHERE id=%s",
                            (now_iso(),ok,200 if ok else 207,out["rows_written"],error,payload,cid))
        else:
            con.execute("UPDATE captures SET finished_at=?,ok=?,http_status=?,rows_written=?,error=?,raw_meta=? WHERE id=?",
                        (now_iso(),ok,200 if ok else 207,out["rows_written"],error,payload,cid))

def save_rows(rows):
    if not rows: return 0
    # captured_at is intentionally NOT overwritten on conflict: it is the first-seen time boundary.
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.executemany("""INSERT INTO bars(symbol,ts_utc,open,high,low,close,volume,source,captured_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(symbol,ts_utc) DO UPDATE SET open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,volume=EXCLUDED.volume,source=EXCLUDED.source""",rows)
        else:
            con.executemany("""INSERT INTO bars(symbol,ts_utc,open,high,low,close,volume,source,captured_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,ts_utc) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,source=excluded.source""",rows)
    return len(rows)

def rows_to_write(provider_rows,last):
    if last is None: return provider_rows
    current=now_epoch()
    # While the feed is live/recent, rewrite only a 3-minute correction window.
    # Once the feed is stale/closed, write only genuinely new bars; usually zero rows.
    if current-last <= 15*60:
        floor=max(0,last-180)
        return [r for r in provider_rows if r[1] >= floor]
    return [r for r in provider_rows if r[1] > last]

def get_state(key):
    p="%s" if USING_POSTGRES else "?"
    with db() as con:
        r=fetchone_dict(con,f"SELECT value FROM system_state WHERE key={p}",(key,))
        return r["value"] if r else None

def set_state(key,value):
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.execute("""INSERT INTO system_state(key,value,updated_at) VALUES(%s,%s,%s)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at""",(key,str(value),now_iso()))
        else:
            con.execute("""INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",(key,str(value),now_iso()))

def pg_storage_bytes():
    if not USING_POSTGRES: return None
    with db() as con:
        r=fetchone_dict(con,"""SELECT COALESCE(SUM(pg_total_relation_size(c.oid)),0) AS bytes
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind='r'""")
        return int(r["bytes"] or 0)

def _archive_postgres(raw_cutoff):
    stamp=now_iso()
    with db() as con:
        with con.cursor() as cur:
            cur.execute("""
            INSERT INTO bars_5m(symbol,ts_utc,open,high,low,close,volume,source,archived_at)
            SELECT symbol,(ts_utc/300)*300 AS bucket,
              (array_agg(open ORDER BY ts_utc) FILTER (WHERE open IS NOT NULL))[1],
              MAX(high),MIN(low),
              (array_agg(close ORDER BY ts_utc DESC) FILTER (WHERE close IS NOT NULL))[1],
              SUM(COALESCE(volume,0)),MAX(source),%s
            FROM bars WHERE ts_utc < %s
            GROUP BY symbol,(ts_utc/300)*300
            ON CONFLICT(symbol,ts_utc) DO UPDATE SET
              open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,
              volume=EXCLUDED.volume,source=EXCLUDED.source,archived_at=EXCLUDED.archived_at
            """,(stamp,raw_cutoff))
            archived=cur.rowcount
            cur.execute("DELETE FROM bars WHERE ts_utc < %s",(raw_cutoff,)); deleted=cur.rowcount
    return archived,deleted

def _archive_sqlite(raw_cutoff):
    with db() as con:
        rows=con.execute("SELECT * FROM bars WHERE ts_utc<? ORDER BY symbol,ts_utc",(raw_cutoff,)).fetchall()
        groups=defaultdict(list)
        for r in rows: groups[(r["symbol"],(int(r["ts_utc"])//300)*300)].append(r)
        payload=[]; stamp=now_iso()
        for (symbol,bucket),rs in groups.items():
            op=next((x["open"] for x in rs if x["open"] is not None),None)
            cl=next((x["close"] for x in reversed(rs) if x["close"] is not None),None)
            hi=max((x["high"] for x in rs if x["high"] is not None),default=None)
            lo=min((x["low"] for x in rs if x["low"] is not None),default=None)
            vol=sum((x["volume"] or 0) for x in rs)
            payload.append((symbol,bucket,op,hi,lo,cl,vol,rs[-1]["source"],stamp))
        con.executemany("""INSERT INTO bars_5m(symbol,ts_utc,open,high,low,close,volume,source,archived_at)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,ts_utc) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,source=excluded.source,archived_at=excluded.archived_at""",payload)
        cur=con.execute("DELETE FROM bars WHERE ts_utc<?",(raw_cutoff,))
        return len(payload),cur.rowcount

def run_maintenance(force=False):
    last=get_state("last_maintenance")
    if not force and last:
        try:
            if datetime.now(timezone.utc)-datetime.fromisoformat(last) < timedelta(hours=MAINTENANCE_INTERVAL_HOURS):
                return {"ran":False,"reason":"interval"}
        except Exception: pass

    raw_days=RAW_1M_RETENTION_DAYS
    five_days=ARCHIVE_5M_RETENTION_DAYS
    before=pg_storage_bytes()
    if before is not None and before > STORAGE_SOFT_LIMIT_MB*1024*1024:
        # Soft guard: keep a smaller raw window before touching the long 5m archive.
        raw_days=min(raw_days,21)

    now=now_epoch(); raw_cutoff=now-raw_days*86400; five_cutoff=now-five_days*86400
    archived,deleted_raw=(_archive_postgres(raw_cutoff) if USING_POSTGRES else _archive_sqlite(raw_cutoff))
    success_cutoff=(datetime.now(timezone.utc)-timedelta(days=CAPTURE_SUCCESS_RETENTION_DAYS)).isoformat()
    error_cutoff=(datetime.now(timezone.utc)-timedelta(days=CAPTURE_ERROR_RETENTION_DAYS)).isoformat()
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.execute("DELETE FROM bars_5m WHERE ts_utc < %s",(five_cutoff,)); deleted_5m=cur.rowcount
                cur.execute("DELETE FROM captures WHERE ok=1 AND started_at < %s",(success_cutoff,)); deleted_caps_ok=cur.rowcount
                cur.execute("DELETE FROM captures WHERE ok=0 AND started_at < %s",(error_cutoff,)); deleted_caps_err=cur.rowcount
        else:
            deleted_5m=con.execute("DELETE FROM bars_5m WHERE ts_utc<?",(five_cutoff,)).rowcount
            deleted_caps_ok=con.execute("DELETE FROM captures WHERE ok=1 AND started_at<?",(success_cutoff,)).rowcount
            deleted_caps_err=con.execute("DELETE FROM captures WHERE ok=0 AND started_at<?",(error_cutoff,)).rowcount
    set_state("last_maintenance",now_iso())
    after=pg_storage_bytes()
    result={"ran":True,"raw_days":raw_days,"archive_5m_days":five_days,"archived_5m":archived,
            "deleted_raw":deleted_raw,"deleted_5m":deleted_5m,"deleted_capture_success":deleted_caps_ok,
            "deleted_capture_errors":deleted_caps_err,"storage_before":before,"storage_after":after}
    set_state("last_maintenance_result",json.dumps(result,separators=(",",":")))
    return result

async def capture(symbols):
    cid=begin_batch_capture(len(symbols))
    out={"requested":len(symbols),"ok":0,"failed":0,"provider_rows":0,"rows_written":0,"errors":{},"symbols":{}}
    sem=asyncio.Semaphore(8)
    async with httpx.AsyncClient(headers={"User-Agent":"Mozilla/5.0 RadarBlackBox/1.2"},follow_redirects=True) as client:
        async def one(s):
            last=latest_ts(s)
            try:
                async with sem: status,provider_rows,meta=await fetch_yahoo(client,s)
                selected=rows_to_write(provider_rows,last)
                written=save_rows(selected)
                out["ok"]+=1; out["provider_rows"]+=len(provider_rows); out["rows_written"]+=written
                out["symbols"][s]={"provider_rows":len(provider_rows),"written":written,"last_before":last,"http":status}
            except Exception as e:
                out["failed"]+=1; out["errors"][s]=str(e)[:500]
        await asyncio.gather(*(one(s) for s in symbols))
    maint=run_maintenance()
    out["maintenance"]=maint
    finish_batch_capture(cid,out)
    print(json.dumps(out,ensure_ascii=False))
    if out["ok"]==0: raise RuntimeError("All sources failed")
    return out

if __name__=="__main__":
    init_db(); seed_requirements()
    full="--full" in sys.argv
    force_maintenance="--maintenance" in sys.argv
    if force_maintenance:
        print(json.dumps(run_maintenance(force=True),ensure_ascii=False))
    else:
        asyncio.run(capture(WATCHLIST_SYMBOLS if full else CORE_SYMBOLS))

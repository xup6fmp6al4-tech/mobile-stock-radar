from __future__ import annotations
from datetime import datetime, timezone, timedelta
import os
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from db import db, init_db, fetchall_dict, fetchone_dict, USING_POSTGRES, SQLITE_PATH
from config import (
    CORE_SYMBOLS, WATCHLIST_SYMBOLS, RAW_1M_RETENTION_DAYS,
    ARCHIVE_5M_RETENTION_DAYS, CAPTURE_SUCCESS_RETENTION_DAYS,
    CAPTURE_ERROR_RETENTION_DAYS, STORAGE_SOFT_LIMIT_MB,
)
from futures_similarity import router as futures_router

app=FastAPI(title="Radar Black Box",version="1.3")
app.include_router(futures_router)

@app.on_event("startup")
def startup(): init_db()

def iso(): return datetime.now(timezone.utc).isoformat()
def ph(): return "%s" if USING_POSTGRES else "?"

@app.get("/",response_class=HTMLResponse)
def home():
    return """<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><title>Radar Black Box</title><h1>Radar Black Box v1.3</h1><p>Machine-readable recorder/API. UI intentionally minimal.</p><p><a href='/health'>health</a> · <a href='/api/blackbox/status'>status</a> · <a href='/api/blackbox/gaps'>gaps</a> · <a href='/api/blackbox/storage'>storage</a> · <a href='/api/blackbox/futures/coverage'>futures coverage</a> · <a href='/docs'>docs</a></p>"""

@app.get("/health")
def health():
    try:
        with db() as con:
            n=fetchone_dict(con,"SELECT COUNT(*) AS n FROM bars")["n"]
            n5=fetchone_dict(con,"SELECT COUNT(*) AS n FROM bars_5m")["n"]
        return {"ok":True,"version":"1.3-blackbox","utc":iso(),"bars_1m":n,"bars_5m":n5,"database":"postgres" if USING_POSTGRES else "sqlite-local"}
    except Exception as e: return {"ok":False,"version":"1.3-blackbox","utc":iso(),"error":str(e)}

@app.get("/api/radar")
def radar():
    with db() as con:
        rows=fetchall_dict(con,"""SELECT b.* FROM bars b JOIN (SELECT symbol,MAX(ts_utc) ts FROM bars GROUP BY symbol) x ON x.symbol=b.symbol AND x.ts=b.ts_utc ORDER BY b.symbol""")
    return {"version":"1.3-blackbox","generated_at":iso(),"symbols":rows}

@app.get("/api/blackbox/status")
def status():
    now=int(datetime.now(timezone.utc).timestamp())
    with db() as con:
        latest=fetchall_dict(con,"SELECT symbol,MAX(ts_utc) ts,MAX(captured_at) captured_at,COUNT(*) bars FROM bars GROUP BY symbol ORDER BY symbol")
        sources=fetchall_dict(con,"SELECT * FROM source_status ORDER BY key")
        caps=fetchall_dict(con,"SELECT source,symbol,started_at,finished_at,ok,rows_written,error,raw_meta FROM captures ORDER BY id DESC LIMIT 20")
        state=fetchall_dict(con,"SELECT * FROM system_state ORDER BY key")
    for x in latest: x["age_seconds"]=now-(x["ts"] or now)
    return {"version":"1.3-blackbox","utc":iso(),"database":"postgres" if USING_POSTGRES else "sqlite-local","core_symbols":CORE_SYMBOLS,"watchlist_count":len(WATCHLIST_SYMBOLS),"symbols_seen":latest,"requirements":sources,"system_state":state,"recent_captures":caps}

@app.get("/api/blackbox/gaps")
def gaps(stale_minutes:int=Query(30,ge=1,le=1440)):
    cutoff=int((datetime.now(timezone.utc)-timedelta(minutes=stale_minutes)).timestamp())
    with db() as con:
        rows=fetchall_dict(con,"SELECT symbol,MAX(ts_utc) ts FROM bars GROUP BY symbol")
        req=fetchall_dict(con,"SELECT * FROM source_status WHERE state<>'active' ORDER BY key")
    seen={r["symbol"]:r["ts"] for r in rows}; never=[s for s in WATCHLIST_SYMBOLS if s not in seen]
    stale=[{"symbol":s,"last_ts":t} for s,t in seen.items() if s in WATCHLIST_SYMBOLS and t<cutoff]
    return {"utc":iso(),"never_seen":never,"stale":stale,"structural_gaps":req,"note":"Stale can be normal outside market hours. structural_gaps are real missing feeds."}

def _table_size_rows():
    if USING_POSTGRES:
        with db() as con:
            return fetchall_dict(con,"""SELECT c.relname AS table_name,pg_total_relation_size(c.oid) AS bytes
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind='r' ORDER BY bytes DESC""")
    size=os.path.getsize(SQLITE_PATH) if os.path.exists(SQLITE_PATH) else 0
    return [{"table_name":"sqlite_file","bytes":size}]

@app.get("/api/blackbox/storage")
def storage():
    sizes=_table_size_rows(); total=sum(int(x["bytes"] or 0) for x in sizes)
    with db() as con:
        counts={}
        for t in ("bars","bars_5m","captures","source_status"):
            counts[t]=fetchone_dict(con,f"SELECT COUNT(*) AS n FROM {t}")["n"]
    return {"version":"1.3-blackbox","utc":iso(),"total_bytes":total,"total_mb":round(total/1024/1024,2),"soft_limit_mb":STORAGE_SOFT_LIMIT_MB,"tables":sizes,"counts":counts,"retention":{"1m_days":RAW_1M_RETENTION_DAYS,"5m_days":ARCHIVE_5M_RETENTION_DAYS,"capture_success_days":CAPTURE_SUCCESS_RETENTION_DAYS,"capture_error_days":CAPTURE_ERROR_RETENTION_DAYS},"note":"PostgreSQL reuses freed pages; physical bytes may not shrink immediately after cleanup."}

def _parse(s): return int(datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()) if s else None

def _get_bars(table,symbol,start,end,limit):
    st,en=_parse(start),_parse(end); p=ph(); q=f"SELECT * FROM {table} WHERE symbol={p}"; args=[symbol]
    if st is not None: q+=f" AND ts_utc>={p}"; args.append(st)
    if en is not None: q+=f" AND ts_utc<={p}"; args.append(en)
    q+=f" ORDER BY ts_utc ASC LIMIT {p}"; args.append(limit)
    with db() as con: return fetchall_dict(con,q,tuple(args))

@app.get("/api/blackbox/bars")
def bars(symbol:str,start:str|None=None,end:str|None=None,limit:int=Query(5000,ge=1,le=20000)):
    rows=_get_bars("bars",symbol,start,end,limit)
    return {"symbol":symbol,"resolution":"1m","count":len(rows),"bars":rows}

@app.get("/api/blackbox/bars5m")
def bars5m(symbol:str,start:str|None=None,end:str|None=None,limit:int=Query(5000,ge=1,le=20000)):
    rows=_get_bars("bars_5m",symbol,start,end,limit)
    return {"symbol":symbol,"resolution":"5m","count":len(rows),"bars":rows}

@app.get("/api/blackbox/raw")
def raw(limit:int=Query(100,ge=1,le=1000)):
    p=ph()
    with db() as con: rows=fetchall_dict(con,f"SELECT * FROM captures ORDER BY id DESC LIMIT {p}",(limit,))
    return {"count":len(rows),"captures":rows}

@app.get("/api/blackbox/export")
def export(date:str,resolution:str=Query("1m",pattern="^(1m|5m)$")):
    start=datetime.fromisoformat(date).replace(tzinfo=timezone.utc); end=start+timedelta(days=1); p=ph(); table="bars" if resolution=="1m" else "bars_5m"
    with db() as con: rows=fetchall_dict(con,f"SELECT * FROM {table} WHERE ts_utc>={p} AND ts_utc<{p} ORDER BY symbol,ts_utc",(int(start.timestamp()),int(end.timestamp())))
    return {"date_utc":date,"resolution":resolution,"count":len(rows),"bars":rows}

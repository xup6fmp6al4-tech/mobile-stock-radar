from __future__ import annotations
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from db import db, init_db, fetchall_dict, fetchone_dict, USING_POSTGRES
from config import CORE_SYMBOLS, WATCHLIST_SYMBOLS

app=FastAPI(title="Radar Black Box",version="1.1")
@app.on_event("startup")
def startup(): init_db()

def iso(): return datetime.now(timezone.utc).isoformat()
def ph(): return "%s" if USING_POSTGRES else "?"

@app.get("/",response_class=HTMLResponse)
def home():
    return """<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><title>Radar Black Box</title><h1>Radar Black Box v1.1</h1><p>Machine-readable recorder/API. UI intentionally minimal.</p><p><a href='/health'>health</a> · <a href='/api/blackbox/status'>status</a> · <a href='/api/blackbox/gaps'>gaps</a> · <a href='/docs'>docs</a></p>"""

@app.get("/health")
def health():
    try:
        with db() as con: n=fetchone_dict(con,"SELECT COUNT(*) AS n FROM bars")["n"]
        return {"ok":True,"version":"1.1-blackbox","utc":iso(),"bars":n,"database":"postgres" if USING_POSTGRES else "sqlite-local"}
    except Exception as e: return {"ok":False,"version":"1.1-blackbox","utc":iso(),"error":str(e)}

@app.get("/api/radar")
def radar():
    with db() as con:
        rows=fetchall_dict(con,"""SELECT b.* FROM bars b JOIN (SELECT symbol,MAX(ts_utc) ts FROM bars GROUP BY symbol) x ON x.symbol=b.symbol AND x.ts=b.ts_utc ORDER BY b.symbol""")
    return {"version":"1.1-blackbox","generated_at":iso(),"symbols":rows}

@app.get("/api/blackbox/status")
def status():
    now=int(datetime.now(timezone.utc).timestamp())
    with db() as con:
        latest=fetchall_dict(con,"SELECT symbol,MAX(ts_utc) ts,MAX(captured_at) captured_at,COUNT(*) bars FROM bars GROUP BY symbol ORDER BY symbol")
        sources=fetchall_dict(con,"SELECT * FROM source_status ORDER BY key")
        caps=fetchall_dict(con,"SELECT source,symbol,finished_at,ok,rows_written,error FROM captures ORDER BY id DESC LIMIT 30")
    for x in latest: x["age_seconds"]=now-(x["ts"] or now)
    return {"version":"1.1-blackbox","utc":iso(),"database":"postgres" if USING_POSTGRES else "sqlite-local","core_symbols":CORE_SYMBOLS,"watchlist_count":len(WATCHLIST_SYMBOLS),"symbols_seen":latest,"requirements":sources,"recent_captures":caps}

@app.get("/api/blackbox/gaps")
def gaps(stale_minutes:int=Query(30,ge=1,le=1440)):
    cutoff=int((datetime.now(timezone.utc)-timedelta(minutes=stale_minutes)).timestamp())
    with db() as con:
        rows=fetchall_dict(con,"SELECT symbol,MAX(ts_utc) ts FROM bars GROUP BY symbol")
        req=fetchall_dict(con,"SELECT * FROM source_status WHERE state<>'active' ORDER BY key")
    seen={r["symbol"]:r["ts"] for r in rows}; never=[s for s in WATCHLIST_SYMBOLS if s not in seen]
    stale=[{"symbol":s,"last_ts":t} for s,t in seen.items() if s in WATCHLIST_SYMBOLS and t<cutoff]
    return {"utc":iso(),"never_seen":never,"stale":stale,"structural_gaps":req,"note":"Stale can be normal outside market hours. structural_gaps are real missing feeds."}

@app.get("/api/blackbox/bars")
def bars(symbol:str,start:str|None=None,end:str|None=None,limit:int=Query(5000,ge=1,le=20000)):
    def parse(s): return int(datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()) if s else None
    st,en=parse(start),parse(end); p=ph(); q=f"SELECT * FROM bars WHERE symbol={p}"; args=[symbol]
    if st is not None: q+=f" AND ts_utc>={p}"; args.append(st)
    if en is not None: q+=f" AND ts_utc<={p}"; args.append(en)
    q+=f" ORDER BY ts_utc ASC LIMIT {p}"; args.append(limit)
    with db() as con: rows=fetchall_dict(con,q,tuple(args))
    return {"symbol":symbol,"count":len(rows),"bars":rows}

@app.get("/api/blackbox/raw")
def raw(symbol:str|None=None,limit:int=Query(100,ge=1,le=1000)):
    p=ph()
    with db() as con:
        if symbol: rows=fetchall_dict(con,f"SELECT * FROM captures WHERE symbol={p} ORDER BY id DESC LIMIT {p}",(symbol,limit))
        else: rows=fetchall_dict(con,f"SELECT * FROM captures ORDER BY id DESC LIMIT {p}",(limit,))
    return {"count":len(rows),"captures":rows}

@app.get("/api/blackbox/export")
def export(date:str):
    start=datetime.fromisoformat(date).replace(tzinfo=timezone.utc); end=start+timedelta(days=1); p=ph()
    with db() as con: rows=fetchall_dict(con,f"SELECT * FROM bars WHERE ts_utc>={p} AND ts_utc<{p} ORDER BY symbol,ts_utc",(int(start.timestamp()),int(end.timestamp())))
    return {"date_utc":date,"count":len(rows),"bars":rows}

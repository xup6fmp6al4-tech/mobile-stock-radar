from __future__ import annotations
import math
from typing import Any
from fastapi import APIRouter, Query
from db import db, fetchall_dict, fetchone_dict, USING_POSTGRES

router = APIRouter(prefix="/api/blackbox/futures", tags=["futures"])

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS futures_3m(
 product TEXT NOT NULL,trading_date TEXT NOT NULL,session TEXT NOT NULL,
 bar_index INTEGER NOT NULL,ts_utc BIGINT NOT NULL,
 open DOUBLE PRECISION,high DOUBLE PRECISION,low DOUBLE PRECISION,close DOUBLE PRECISION,
 volume DOUBLE PRECISION,source TEXT NOT NULL,imported_at TEXT NOT NULL,
 PRIMARY KEY(product,trading_date,session,bar_index));
CREATE INDEX IF NOT EXISTS idx_futures3m_lookup
 ON futures_3m(product,session,trading_date,bar_index);
"""
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS futures_3m(
 product TEXT NOT NULL,trading_date TEXT NOT NULL,session TEXT NOT NULL,
 bar_index INTEGER NOT NULL,ts_utc INTEGER NOT NULL,
 open REAL,high REAL,low REAL,close REAL,volume REAL,
 source TEXT NOT NULL,imported_at TEXT NOT NULL,
 PRIMARY KEY(product,trading_date,session,bar_index));
CREATE INDEX IF NOT EXISTS idx_futures3m_lookup
 ON futures_3m(product,session,trading_date,bar_index);
"""

def ensure_tables():
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.execute(SCHEMA_PG)
        else:
            con.executescript(SCHEMA_SQLITE)

def p():
    return "%s" if USING_POSTGRES else "?"

def rows(product, date, session="night"):
    ensure_tables()
    q = p()
    with db() as con:
        return fetchall_dict(
            con,
            f"SELECT * FROM futures_3m WHERE product={q} AND trading_date={q} AND session={q} ORDER BY bar_index",
            (product, date, session),
        )

def median(xs):
    if not xs:
        return 1.0
    xs = sorted(xs)
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m-1] + xs[m]) / 2

def features(rs, cutoff=None):
    if cutoff is not None:
        rs = [r for r in rs if int(r["bar_index"]) <= cutoff]
    if not rs:
        return {}
    base = next((float(r["open"]) for r in rs if r.get("open") not in (None, 0)), None)
    if not base:
        return {}
    vols = [float(r.get("volume") or 0) for r in rs if float(r.get("volume") or 0) > 0]
    vm = max(median(vols), 1.0)
    out = {}
    for r in rs:
        if r.get("close") is None:
            continue
        c = float(r["close"])
        o = float(r["open"]) if r.get("open") is not None else c
        h = float(r["high"]) if r.get("high") is not None else c
        l = float(r["low"]) if r.get("low") is not None else c
        v = float(r.get("volume") or 0)
        out[int(r["bar_index"])] = (
            (o/base - 1)*100,
            (h/base - 1)*100,
            (l/base - 1)*100,
            (c/base - 1)*100,
            math.log1p(v/vm),
        )
    return out

def rmse(a, b):
    return math.sqrt(sum((x-y)**2 for x, y in zip(a, b)) / max(len(a), 1))

def score(cur, hist):
    common = sorted(set(cur) & set(hist))
    if len(common) < max(20, int(len(cur)*0.85)):
        return None
    co=[cur[i][0] for i in common]; ch=[cur[i][1] for i in common]
    cl=[cur[i][2] for i in common]; cc=[cur[i][3] for i in common]
    cv=[cur[i][4] for i in common]
    ho=[hist[i][0] for i in common]; hh=[hist[i][1] for i in common]
    hl=[hist[i][2] for i in common]; hc=[hist[i][3] for i in common]
    hv=[hist[i][4] for i in common]
    cr=rmse(cc,hc)
    br=rmse([c-o for c,o in zip(cc,co)],[c-o for c,o in zip(hc,ho)])
    rr=rmse([h-l for h,l in zip(ch,cl)],[h-l for h,l in zip(hh,hl)])
    vr=rmse(cv,hv)
    ed=abs(cc[-1]-hc[-1]); dd=abs(min(cc)-min(hc)); ud=abs(max(cc)-max(hc))
    dist=.52*cr+.10*br+.10*rr+.08*min(vr,3)+.08*ed+.07*dd+.05*ud
    sim=max(0.0,min(100.0,100*math.exp(-dist/.75)))
    return {
        "similarity":round(sim,2),
        "distance":round(dist,4),
        "close_rmse_pctpt":round(cr,4),
        "range_rmse_pctpt":round(rr,4),
        "end_diff_pctpt":round(ed,4),
        "matched_bars":len(common),
    }

def tail(rs, cutoff):
    before=[r for r in rs if int(r["bar_index"])<=cutoff and r.get("close") is not None]
    after=[r for r in rs if int(r["bar_index"])>cutoff and r.get("close") is not None]
    if not before or not after:
        return {"available":False}
    a=float(before[-1]["close"])
    cs=[float(r["close"]) for r in after]
    hs=[float(r["high"]) for r in after if r.get("high") is not None]
    ls=[float(r["low"]) for r in after if r.get("low") is not None]
    return {
        "available":True,
        "anchor_close":a,
        "session_end":cs[-1],
        "from_anchor_end_pct":round((cs[-1]/a-1)*100,3),
        "from_anchor_max_pct":round((max(hs or cs)/a-1)*100,3),
        "from_anchor_min_pct":round((min(ls or cs)/a-1)*100,3),
        "tail_bars":len(after),
    }

def day_summary(product, date, night_close=None):
    rs=rows(product,date,"day")
    good=[r for r in rs if r.get("open") is not None and r.get("close") is not None]
    if not good:
        return {"available":False}
    op=float(good[0]["open"]); cl=float(good[-1]["close"])
    hi=max(float(r["high"]) for r in good if r.get("high") is not None)
    lo=min(float(r["low"]) for r in good if r.get("low") is not None)
    out={
        "available":True,"open":op,"high":hi,"low":lo,"close":cl,
        "change_pct":round((cl/op-1)*100,3),
        "max_from_open_pct":round((hi/op-1)*100,3),
        "min_from_open_pct":round((lo/op-1)*100,3),
    }
    if night_close:
        out["gap_from_night_close_pct"]=round((op/night_close-1)*100,3)
    return out

def find_similar(product,target,start="2024-01-01",end="2026-08-31",top_n=5,cutoff=None):
    trg=rows(product,target,"night")
    if not trg:
        return {"ok":False,"reason":"target_session_not_in_blackbox",
                "product":product,"target_trading_date":target}
    if cutoff is None:
        cutoff=max(int(r["bar_index"]) for r in trg)
    cur=features(trg,cutoff)
    if len(cur)<20:
        return {"ok":False,"reason":"not_enough_target_bars","bars":len(cur)}
    q=p()
    with db() as con:
        ds=fetchall_dict(
            con,
            f"""SELECT DISTINCT trading_date FROM futures_3m
            WHERE product={q} AND session='night'
              AND trading_date>={q} AND trading_date<={q}
              AND trading_date<>{q}
            ORDER BY trading_date""",
            (product,start,end,target),
        )
    out=[]
    for d in ds:
        date=str(d["trading_date"])
        rs=rows(product,date,"night")
        s=score(cur,features(rs,cutoff))
        if not s:
            continue
        nc=float(rs[-1]["close"]) if rs and rs[-1].get("close") is not None else None
        out.append({
            "trading_date":date,**s,
            "after_cutoff":tail(rs,cutoff),
            "day_session":day_summary(product,date,nc),
        })
    out.sort(key=lambda x:(-x["similarity"],x["distance"]))
    return {
        "ok":True,
        "method":"normalized_3m_path_no_lookahead",
        "product":product,
        "target_trading_date":target,
        "cutoff_bar_index":cutoff,
        "target_bars":len(cur),
        "history_start":start,
        "history_end":end,
        "matches":out[:top_n],
        "note":"Only bars through cutoff are scored. Later bars are revealed after ranking.",
    }

@router.get("/3m")
def get_3m(product:str="TXF_CONT",trading_date:str|None=None,
           session:str=Query("night",pattern="^(night|day)$"),
           limit:int=Query(400,ge=1,le=1000)):
    ensure_tables(); q=p()
    with db() as con:
        if not trading_date:
            x=fetchone_dict(con,
                f"SELECT MAX(trading_date) d FROM futures_3m WHERE product={q} AND session={q}",
                (product,session))
            trading_date=x["d"] if x else None
        rs=[] if not trading_date else fetchall_dict(
            con,
            f"SELECT * FROM futures_3m WHERE product={q} AND trading_date={q} AND session={q} ORDER BY bar_index LIMIT {q}",
            (product,trading_date,session,limit),
        )
    return {"product":product,"trading_date":trading_date,"session":session,"count":len(rs),"bars":rs}

@router.get("/coverage")
def coverage(product:str="TXF_CONT"):
    ensure_tables(); q=p()
    with db() as con:
        cov=fetchall_dict(con,
            f"""SELECT session,MIN(trading_date) first_date,MAX(trading_date) last_date,
            COUNT(DISTINCT trading_date) trading_days,COUNT(*) bars
            FROM futures_3m WHERE product={q} GROUP BY session ORDER BY session""",
            (product,))
        src=fetchall_dict(con,
            f"""SELECT source,COUNT(*) bars,COUNT(DISTINCT trading_date) trading_days
            FROM futures_3m WHERE product={q} GROUP BY source ORDER BY bars DESC""",
            (product,))
    return {"product":product,"coverage":cov,"sources":src,
            "requested_history":"2024-01-01..2026-08-31"}

@router.get("/similar")
def similar(target_trading_date:str,product:str="TXF_CONT",
            start:str="2024-01-01",end:str="2026-08-31",
            top_n:int=Query(5,ge=1,le=20),
            cutoff_bar_index:int|None=Query(None,ge=19,le=280)):
    return find_similar(product,target_trading_date,start,end,top_n,cutoff_bar_index)

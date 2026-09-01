from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from db import db, USING_POSTGRES
from futures_similarity import ensure_tables

TZ = ZoneInfo("Asia/Taipei")
PRODUCT = "TXF_CONT"
SYMBOL = "WTX&"
SOURCE = "yahoo-wtx-1m"


def _session_info(dt_local: datetime):
    t = dt_local.time()
    mins = dt_local.hour * 60 + dt_local.minute
    # Night: 15:00 -> 05:00 next calendar day
    if mins >= 15 * 60:
        trading_date = dt_local.date() + timedelta(days=1)
        while trading_date.weekday() >= 5:
            trading_date += timedelta(days=1)
        idx = (mins - 15 * 60) // 3
        return str(trading_date), "night", idx
    if mins <= 5 * 60:
        trading_date = dt_local.date()
        idx = (9 * 60 + mins) // 3  # 15:00->24:00 = 540 min
        return str(trading_date), "night", idx
    # Day: 08:45 -> 13:45
    if 8 * 60 + 45 <= mins <= 13 * 60 + 45:
        trading_date = dt_local.date()
        idx = (mins - (8 * 60 + 45)) // 3
        return str(trading_date), "day", idx
    return None


def _bar_start(dt_local: datetime, session: str, idx: int) -> datetime:
    if session == "day":
        start = dt_local.replace(hour=8, minute=45, second=0, microsecond=0)
    elif dt_local.hour >= 15:
        start = dt_local.replace(hour=15, minute=0, second=0, microsecond=0)
    else:
        prev = dt_local - timedelta(days=1)
        start = prev.replace(hour=15, minute=0, second=0, microsecond=0)
    return start + timedelta(minutes=idx * 3)


def _upsert(items):
    if not items:
        return 0
    ensure_tables()
    now = datetime.now(timezone.utc).isoformat()
    if USING_POSTGRES:
        sql = """
        INSERT INTO futures_3m
        (product,trading_date,session,bar_index,ts_utc,open,high,low,close,volume,source,imported_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(product,trading_date,session,bar_index) DO UPDATE SET
          ts_utc=EXCLUDED.ts_utc, open=EXCLUDED.open, high=EXCLUDED.high,
          low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume,
          source=EXCLUDED.source, imported_at=EXCLUDED.imported_at
        """
    else:
        sql = """
        INSERT INTO futures_3m
        (product,trading_date,session,bar_index,ts_utc,open,high,low,close,volume,source,imported_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(product,trading_date,session,bar_index) DO UPDATE SET
          ts_utc=excluded.ts_utc, open=excluded.open, high=excluded.high,
          low=excluded.low, close=excluded.close, volume=excluded.volume,
          source=excluded.source, imported_at=excluded.imported_at
        """
    values = [
        (PRODUCT, b["trading_date"], b["session"], b["bar_index"], b["ts_utc"],
         b["open"], b["high"], b["low"], b["close"], b["volume"], SOURCE, now)
        for b in items
    ]
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.executemany(sql, values)
        else:
            con.executemany(sql, values)
    return len(values)


def _fetch_chart():
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    page = "https://tw.stock.yahoo.com/future/WTX%26"
    with httpx.Client(timeout=20, headers=headers, follow_redirects=True) as client:
        r = client.get(page)
        r.raise_for_status()
        m = re.search(r'"prid":"([^"]+)"', r.text)
        prid = m.group(1) if m else ""
        auto = int(time.time() * 1000)
        resource = (
            "https://tw.stock.yahoo.com/_td-stock/api/resource/"
            "FinanceChartService.ApacLibraCharts"
            f";autoRefresh={auto};period=1m;range=1d;"
            "symbols=%5B%22WTX%26%22%5D;type=null"
            "?bkt=&device=desktop&ecma=modern"
            "&feature=ecmaModern,useVersionSwitch,useNewQuoteTabColor"
            "&intl=tw&lang=zh-Hant-TW&partner=none"
            f"&prid={prid}&region=TW&site=finance&tz=Asia%2FTaipei"
            "&ver=1.2.1415&returnMeta=true"
        )
        rr = client.get(resource, headers={**headers, "Referer": page})
        rr.raise_for_status()
        payload = rr.json()

    data = payload.get("data") or []
    if not data:
        raise RuntimeError(f"Yahoo chart returned no data: {str(payload)[:300]}")
    chart = (data[0] or {}).get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"Yahoo chart error: {chart['error']}")
    ts = chart.get("timestamp") or []
    quotes = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    return ts, quotes, chart.get("meta") or {}


def _aggregate(ts, q):
    opens = q.get("open") or []
    highs = q.get("high") or []
    lows = q.get("low") or []
    closes = q.get("close") or []
    vols = q.get("volume") or []
    bars = {}

    for i, sec in enumerate(ts):
        try:
            c = closes[i]
        except IndexError:
            continue
        if c is None:
            continue
        dt_local = datetime.fromtimestamp(int(sec), timezone.utc).astimezone(TZ)
        info = _session_info(dt_local)
        if not info:
            continue
        trading_date, session, idx = info
        o = opens[i] if i < len(opens) and opens[i] is not None else c
        h = highs[i] if i < len(highs) and highs[i] is not None else c
        l = lows[i] if i < len(lows) and lows[i] is not None else c
        v = vols[i] if i < len(vols) and vols[i] is not None else 0
        key = (trading_date, session, idx)
        start = _bar_start(dt_local, session, idx)
        start_utc = int(start.astimezone(timezone.utc).timestamp())
        b = bars.get(key)
        if b is None:
            bars[key] = {
                "trading_date": trading_date, "session": session, "bar_index": idx,
                "ts_utc": start_utc, "open": float(o), "high": float(h),
                "low": float(l), "close": float(c), "volume": float(v or 0),
                "first_ts": int(sec), "last_ts": int(sec),
            }
        else:
            b["high"] = max(b["high"], float(h))
            b["low"] = min(b["low"], float(l))
            b["volume"] += float(v or 0)
            if int(sec) < b["first_ts"]:
                b["first_ts"] = int(sec)
                b["open"] = float(o)
            if int(sec) >= b["last_ts"]:
                b["last_ts"] = int(sec)
                b["close"] = float(c)
    return sorted(bars.values(), key=lambda x: (x["trading_date"], x["session"], x["bar_index"]))


def main():
    ts, quote, meta = _fetch_chart()
    bars = _aggregate(ts, quote)
    n = _upsert(bars)
    latest = None
    if bars:
        latest = bars[-1]
    print({
        "ok": True,
        "source": SOURCE,
        "symbol": meta.get("symbol") or SYMBOL,
        "provider_points": len(ts),
        "bars_3m_written": n,
        "latest": latest,
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print({"ok": False, "error": repr(e)})
        sys.exit(1)

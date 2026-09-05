from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from db import db, USING_POSTGRES
from futures_similarity import ensure_tables

TZ = ZoneInfo("Asia/Taipei")

# Three separate products. Do not mix their prices/bars.
PRODUCTS = [
    {
        "product": "TXF_CONT",
        "symbol": "WTX&",
        "source": "yahoo-wtx-1m",
        "page_kind": "future",
        "name": "台指期 TX（大台）",
    },
    {
        "product": "MTX_CONT",
        "symbol": "WMT&",
        "source": "yahoo-wmt-1m",
        "page_kind": "future",
        "name": "小型台指 MTX（小台）",
    },
    {
        "product": "TMF_CONT",
        "symbol": "WTM&",
        "source": "yahoo-wtm-1m",
        "page_kind": "quote",
        "name": "微型臺指 TMF（微台）",
    },
]


def _session_info(dt_local: datetime):
    mins = dt_local.hour * 60 + dt_local.minute

    # Night: 15:00 -> 05:00 next calendar day.
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

    # Day: 08:45 -> 13:45.
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


def _upsert(items, *, product: str, source: str):
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
          ts_utc=EXCLUDED.ts_utc,
          open=EXCLUDED.open,
          high=EXCLUDED.high,
          low=EXCLUDED.low,
          close=EXCLUDED.close,
          volume=EXCLUDED.volume,
          source=EXCLUDED.source,
          imported_at=EXCLUDED.imported_at
        """
    else:
        sql = """
        INSERT INTO futures_3m
        (product,trading_date,session,bar_index,ts_utc,open,high,low,close,volume,source,imported_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(product,trading_date,session,bar_index) DO UPDATE SET
          ts_utc=excluded.ts_utc,
          open=excluded.open,
          high=excluded.high,
          low=excluded.low,
          close=excluded.close,
          volume=excluded.volume,
          source=excluded.source,
          imported_at=excluded.imported_at
        """

    values = [
        (
            product,
            b["trading_date"],
            b["session"],
            b["bar_index"],
            b["ts_utc"],
            b["open"],
            b["high"],
            b["low"],
            b["close"],
            b["volume"],
            source,
            now,
        )
        for b in items
    ]

    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.executemany(sql, values)
        else:
            con.executemany(sql, values)

    return len(values)


def _fetch_chart(*, symbol: str, page_kind: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    encoded_symbol = quote(symbol, safe="")
    if page_kind == "future":
        page = f"https://tw.stock.yahoo.com/future/{encoded_symbol}"
    else:
        page = f"https://tw.stock.yahoo.com/quote/{encoded_symbol}"

    with httpx.Client(timeout=20, headers=headers, follow_redirects=True) as client:
        r = client.get(page)
        r.raise_for_status()

        m = re.search(r'"prid":"([^"]+)"', r.text)
        prid = m.group(1) if m else ""
        auto = int(time.time() * 1000)
        symbols_json = quote(json.dumps([symbol], separators=(",", ":")), safe="")

        resource = (
            "https://tw.stock.yahoo.com/_td-stock/api/resource/"
            "FinanceChartService.ApacLibraCharts"
            f";autoRefresh={auto};period=1m;range=1d;"
            f"symbols={symbols_json};type=null"
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
        raise RuntimeError(f"{symbol}: Yahoo chart returned no data: {str(payload)[:300]}")

    chart = (data[0] or {}).get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"{symbol}: Yahoo chart error: {chart['error']}")

    ts = chart.get("timestamp") or []
    quotes = ((chart.get("indicators") or {}).get("quote") or [{}])[0]

    if not ts:
        raise RuntimeError(f"{symbol}: Yahoo chart returned zero timestamps")

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
                "trading_date": trading_date,
                "session": session,
                "bar_index": idx,
                "ts_utc": start_utc,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v or 0),
                "first_ts": int(sec),
                "last_ts": int(sec),
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

    return sorted(
        bars.values(),
        key=lambda x: (x["trading_date"], x["session"], x["bar_index"]),
    )


def _run_one(cfg):
    ts, quote_data, meta = _fetch_chart(
        symbol=cfg["symbol"],
        page_kind=cfg["page_kind"],
    )
    bars = _aggregate(ts, quote_data)
    n = _upsert(
        bars,
        product=cfg["product"],
        source=cfg["source"],
    )

    latest = bars[-1] if bars else None
    return {
        "ok": True,
        "product": cfg["product"],
        "name": cfg["name"],
        "source": cfg["source"],
        "requested_symbol": cfg["symbol"],
        "provider_symbol": meta.get("symbol") or cfg["symbol"],
        "provider_points": len(ts),
        "bars_3m_written": n,
        "latest": latest,
    }


def main():
    results = []
    successes = 0

    for cfg in PRODUCTS:
        try:
            result = _run_one(cfg)
            successes += 1
        except Exception as exc:
            result = {
                "ok": False,
                "product": cfg["product"],
                "name": cfg["name"],
                "requested_symbol": cfg["symbol"],
                "error": repr(exc),
            }
        results.append(result)

    payload = {
        "ok": successes == len(PRODUCTS),
        "partial_ok": successes > 0,
        "successful_products": successes,
        "total_products": len(PRODUCTS),
        "results": results,
    }
    print(payload)

    # Preserve successful products even if one source fails.
    if successes == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

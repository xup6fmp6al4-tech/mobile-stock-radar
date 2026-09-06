from __future__ import annotations

import csv
import io
import time
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Query

from db import db, fetchall_dict, fetchone_dict, USING_POSTGRES
import futures_full_data as full_data
import futures_similarity as legacy
import taifex_overlay as overlay

router = APIRouter(prefix="/api/blackbox/futures", tags=["futures-1m-archive"])
TZ = ZoneInfo("Asia/Taipei")

PRODUCT_CODE = {
    "TXF_CONT": "TX",
    "MTX_CONT": "MTX",
    "TMF_CONT": "TMF",
}

OFFICIAL_ZIP = "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{ymd}.zip"

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS futures_1m(
 product TEXT NOT NULL,
 trading_date TEXT NOT NULL,
 session TEXT NOT NULL,
 bar_index INTEGER NOT NULL,
 ts_utc BIGINT NOT NULL,
 open DOUBLE PRECISION,
 high DOUBLE PRECISION,
 low DOUBLE PRECISION,
 close DOUBLE PRECISION,
 volume DOUBLE PRECISION,
 source TEXT NOT NULL,
 imported_at TEXT NOT NULL,
 PRIMARY KEY(product,trading_date,session,bar_index)
);
CREATE INDEX IF NOT EXISTS idx_futures1m_lookup
 ON futures_1m(product,session,trading_date,bar_index);
"""

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS futures_1m(
 product TEXT NOT NULL,
 trading_date TEXT NOT NULL,
 session TEXT NOT NULL,
 bar_index INTEGER NOT NULL,
 ts_utc INTEGER NOT NULL,
 open REAL,
 high REAL,
 low REAL,
 close REAL,
 volume REAL,
 source TEXT NOT NULL,
 imported_at TEXT NOT NULL,
 PRIMARY KEY(product,trading_date,session,bar_index)
);
CREATE INDEX IF NOT EXISTS idx_futures1m_lookup
 ON futures_1m(product,session,trading_date,bar_index);
"""


def _ensure_1m_table() -> None:
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.execute(SCHEMA_PG)
        else:
            con.executescript(SCHEMA_SQLITE)


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).strip().replace(",", ""))
    except Exception:
        return None


def _upsert_1m(bars: list[dict[str, Any]], source: str | None = None) -> int:
    good = []
    now = datetime.now(timezone.utc).isoformat()
    for b in bars or []:
        try:
            product = str(b.get("product") or "").strip()
            trading_date = str(b.get("trading_date") or "").strip()
            session = str(b.get("session") or "").strip()
            idx = int(b.get("bar_index"))
            ts_utc = int(b.get("ts_utc"))
            close = _num(b.get("close"))
            if not product or not trading_date or session not in {"day", "night"} or close is None:
                continue
            o = _num(b.get("open")); h = _num(b.get("high")); l = _num(b.get("low")); v = _num(b.get("volume"))
            good.append((
                product, trading_date, session, idx, ts_utc,
                o if o is not None else close,
                h if h is not None else close,
                l if l is not None else close,
                close,
                v if v is not None else 0.0,
                str(source or b.get("source") or "1m"),
                now,
            ))
        except Exception:
            continue
    if not good:
        return 0
    _ensure_1m_table()
    if USING_POSTGRES:
        sql = """
        INSERT INTO futures_1m
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
        INSERT INTO futures_1m
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
    with db() as con:
        if USING_POSTGRES:
            with con.cursor() as cur:
                cur.executemany(sql, good)
        else:
            con.executemany(sql, good)
    return len(good)


def _load_1m(product: str, session: str, trading_date: str | None = None, limit: int = 1500):
    _ensure_1m_table()
    ph = "%s" if USING_POSTGRES else "?"
    with db() as con:
        if not trading_date:
            row = fetchone_dict(
                con,
                f"SELECT MAX(trading_date) d FROM futures_1m WHERE product={ph} AND session={ph}",
                (product, session),
            )
            trading_date = str(row.get("d")) if row and row.get("d") else None
        if not trading_date:
            return [], None, None
        rows = fetchall_dict(
            con,
            f"""SELECT product,trading_date,session,bar_index,ts_utc,open,high,low,close,volume,source
                FROM futures_1m
                WHERE product={ph} AND session={ph} AND trading_date={ph}
                ORDER BY bar_index LIMIT {ph}""",
            (product, session, trading_date, int(limit)),
        )
    src = rows[-1].get("source") if rows else None
    return rows, trading_date, src


def _norm_headers(fieldnames):
    return {str(x).strip().replace("\ufeff", ""): x for x in (fieldnames or [])}


def _add_trade(store: dict, trading_date: str, session: str, idx: int, dt_local: datetime, price: float, volume: float):
    key = (trading_date, session, idx)
    sec = int(dt_local.replace(second=0, microsecond=0, tzinfo=TZ).astimezone(timezone.utc).timestamp())
    raw_sec = int(dt_local.replace(tzinfo=TZ).astimezone(timezone.utc).timestamp())
    b = store.get(key)
    if b is None:
        store[key] = {
            "trading_date": trading_date,
            "session": session,
            "bar_index": idx,
            "ts_utc": sec,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": volume,
            "_first": raw_sec,
            "_last": raw_sec,
        }
        return
    b["high"] = max(float(b["high"]), price)
    b["low"] = min(float(b["low"]), price)
    b["volume"] = float(b.get("volume") or 0) + volume
    if raw_sec < int(b["_first"]):
        b["_first"] = raw_sec
        b["open"] = price
    if raw_sec >= int(b["_last"]):
        b["_last"] = raw_sec
        b["close"] = price


def _official_daily_1m(product: str, calendar_date: str):
    """Download TAIFEX official daily tick ZIP and build genuine 1-minute OHLC.

    No 1-minute bar is invented. Only minutes containing official trades are emitted.
    The most-active expiry for that product in the file is selected as the continuous near contract.
    """
    code = PRODUCT_CODE.get(product)
    if not code:
        return {"ok": False, "bars": [], "error": "unsupported_product"}
    try:
        d = date.fromisoformat(calendar_date)
    except Exception:
        return {"ok": False, "bars": [], "error": "bad_date"}

    ymd = d.strftime("%Y_%m_%d")
    url = OFFICIAL_ZIP.format(ymd=ymd)
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "*/*",
        "Referer": "https://www.taifex.com.tw/",
        "Cache-Control": "no-cache",
    }
    try:
        r = httpx.get(url, headers=headers, timeout=45, follow_redirects=True)
        if r.status_code != 200 or len(r.content) < 200:
            return {"ok": False, "bars": [], "error": f"official_zip_http_{r.status_code}"}
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except Exception as exc:
        return {"ok": False, "bars": [], "error": f"official_zip: {type(exc).__name__}: {exc}"}

    by_exp: dict[str, dict] = {}
    exp_volume = defaultdict(float)
    matched = 0
    required = ["成交日期", "商品代號", "到期月份(週別)", "成交時間", "成交價格", "成交數量(B+S)"]

    for name in z.namelist():
        if not name.lower().endswith((".csv", ".txt")):
            continue
        try:
            raw = z.open(name)
            text = io.TextIOWrapper(raw, encoding="cp950", errors="replace", newline="")
            reader = csv.DictReader(text)
            hm = _norm_headers(reader.fieldnames)
            if any(k not in hm for k in required):
                continue
            for row in reader:
                if str(row.get(hm["商品代號"], "")).strip() != code:
                    continue
                exp = str(row.get(hm["到期月份(週別)"], "")).strip()
                try:
                    ds = str(row.get(hm["成交日期"], "")).strip()
                    ts = str(row.get(hm["成交時間"], "")).strip().zfill(6)
                    dt_local = datetime.strptime(ds + " " + ts, "%Y%m%d %H%M%S")
                    info = legacy._one_min_session_info(dt_local.replace(tzinfo=TZ))
                    if not info:
                        continue
                    trading_date, session, idx = info
                    price = float(str(row.get(hm["成交價格"], "")).strip())
                    # TAIFEX field is B+S, so divide by 2 to get traded contracts.
                    volume = float(str(row.get(hm["成交數量(B+S)"], "0")).strip() or 0) / 2.0
                except Exception:
                    continue
                store = by_exp.setdefault(exp, {})
                _add_trade(store, trading_date, session, idx, dt_local, price, volume)
                exp_volume[exp] += volume
                matched += 1
        except Exception:
            continue

    if not exp_volume:
        return {"ok": False, "bars": [], "error": f"no_{code}_rows", "matched": matched}

    active = max(exp_volume, key=exp_volume.get)
    bars = []
    for _, b in sorted(by_exp[active].items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        b.pop("_first", None); b.pop("_last", None)
        b["product"] = product
        b["source"] = f"taifex_official_tick_1m_{active}"
        bars.append(b)
    n = _upsert_1m(bars, f"taifex_official_tick_1m_{active}")
    return {
        "ok": bool(bars),
        "bars": bars,
        "count": len(bars),
        "written": n,
        "active_expiry": active,
        "source": "taifex_official_daily_tick_1m",
        "url_date": calendar_date,
        "matched_trades": matched,
    }


def _target_date(product: str, session: str):
    # First try the realtime/last-valid quote date.
    try:
        q = full_data.realtime(product=product, session=session)
        d = q.get("trade_date") if isinstance(q, dict) else None
        if d:
            return str(d)
    except Exception:
        pass
    # Then use the latest real 3m archive date already present in our DB.
    try:
        j = legacy.get_3m(product=product, session=session, limit=400)
        d = j.get("trading_date") if isinstance(j, dict) else None
        if d:
            return str(d)
    except Exception:
        pass
    return None


def _persist_if_real(bars: list[dict[str, Any]], source: str | None):
    if not bars:
        return
    # Save only real upstream 1m data. Never persist a 3m-derived pseudo 1m series.
    try:
        _upsert_1m(bars, source)
    except Exception:
        pass


@router.get("/intraday-bars")
def intraday_bars(
    product: str = Query("TXF_CONT"),
    session: str = Query("day", pattern="^(day|night)$"),
    limit_1m: int = Query(1000, ge=1, le=1500),
    limit_3m: int = Query(400, ge=1, le=1000),
):
    """1m/3m source order with persistent official historical fallback.

    1m order:
      TAIFEX MIS current chart -> Yahoo genuine 1m -> persisted genuine 1m ->
      TAIFEX official daily tick ZIP backfill -> APP observed MIS snapshots.
    3m is aggregated from genuine 1m whenever 1m exists; otherwise the existing 3m archive is used.
    """
    if product not in PRODUCT_CODE:
        return {"ok": False, "error": "unsupported_product", "product": product}

    errors: list[str] = []
    bars1: list[dict[str, Any]] = []
    src1 = None
    trading_date = None
    used_session = session
    backfill = None

    # 1) Current official MIS chart.
    try:
        mis = full_data._mis_chart_1m(product, session)
        if mis.get("ok") and mis.get("bars"):
            bars1 = list(mis["bars"])
            src1 = mis.get("source") or "taifex_mis_getChartDataTick"
            trading_date = mis.get("trading_date")
            _persist_if_real(bars1, src1)
        else:
            errors.extend(mis.get("errors") or [mis.get("error") or "MIS 1m empty"])
    except Exception as exc:
        errors.append(f"MIS1m: {type(exc).__name__}: {exc}")

    # 2) Yahoo genuine 1m.
    if not bars1:
        try:
            y = legacy.get_1m(product=product, session=session, limit=limit_1m)
            yb = [b for b in (y.get("bars") or []) if b.get("close") is not None]
            if yb:
                bars1 = yb
                src1 = y.get("source") or "yahoo_1m_live"
                trading_date = y.get("trading_date")
                _persist_if_real(bars1, src1)
            elif y.get("error"):
                errors.append(str(y.get("error")))
        except Exception as exc:
            errors.append(f"Yahoo1m: {type(exc).__name__}: {exc}")

    # 3) Persistent genuine 1m archive created by prior live reads/backfills.
    if not bars1:
        try:
            pb, pd, ps = _load_1m(product, session, limit=limit_1m)
            if pb:
                bars1 = pb
                trading_date = pd
                src1 = ps or "persistent_1m"
        except Exception as exc:
            errors.append(f"persisted1m: {type(exc).__name__}: {exc}")

    # 4) Closed-market recovery: use TAIFEX official daily tick file to rebuild the latest real 1m.
    market = overlay._market_open_info()
    if not bars1 and not market.get("open"):
        target = _target_date(product, session)
        if target:
            try:
                backfill = _official_daily_1m(product, target)
                if backfill.get("ok"):
                    pb, pd, ps = _load_1m(product, session, trading_date=target, limit=limit_1m)
                    if pb:
                        bars1 = pb
                        trading_date = pd
                        src1 = ps or backfill.get("source")
                else:
                    errors.append(str(backfill.get("error") or "official daily 1m empty"))
            except Exception as exc:
                errors.append(f"official1m: {type(exc).__name__}: {exc}")
        else:
            errors.append("official1m: no target trading date")

    # 5) Live observed snapshots are real observations, but only useful when enough points exist.
    if not bars1:
        try:
            hb = full_data._history_snapshots_1m(product, session)
            if hb:
                bars1 = hb
                src1 = "taifex_mis_observed_snapshots_1m"
                trading_date = hb[-1].get("trading_date")
                _persist_if_real(bars1, src1)
        except Exception as exc:
            errors.append(f"snapshot1m: {type(exc).__name__}: {exc}")

    bars1 = bars1[-limit_1m:]
    bars3 = full_data._aggregate_1m(bars1, 3)[-limit_3m:] if bars1 else []
    src3 = f"{src1}_aggregate_3m" if bars3 and src1 else None

    # If 1m remains unavailable, keep the truthful existing 3m archive; never fabricate 1m from it.
    if not bars3:
        try:
            a = legacy.get_3m(product=product, session=session, limit=limit_3m)
            ab = [b for b in (a.get("bars") or []) if b.get("close") is not None]
            if ab:
                bars3 = ab[-limit_3m:]
                src3 = "local_archive_3m"
                trading_date = trading_date or a.get("trading_date")
        except Exception as exc:
            errors.append(f"archive3m: {type(exc).__name__}: {exc}")

    return {
        "ok": bool(bars1 or bars3),
        "product": product,
        "requested_session": session,
        "session": used_session,
        "trading_date": trading_date,
        "count_1m": len(bars1),
        "count_3m": len(bars3),
        "bars_1m": bars1,
        "bars_3m": bars3,
        "source_1m": src1,
        "source_3m": src3,
        "official_backfill": None if not backfill else {
            "ok": backfill.get("ok"),
            "count": backfill.get("count"),
            "active_expiry": backfill.get("active_expiry"),
            "source": backfill.get("source"),
            "error": backfill.get("error"),
        },
        "errors": errors[-10:],
        "note": "1m is genuine MIS/Yahoo/TAIFEX official tick or persisted genuine 1m. 5m may be aggregated only from genuine 1m; 3m is never split into fake 1m.",
    }


@router.get("/1m-archive/coverage")
def one_minute_coverage(product: str = Query("TXF_CONT")):
    _ensure_1m_table()
    ph = "%s" if USING_POSTGRES else "?"
    with db() as con:
        rows = fetchall_dict(
            con,
            f"""SELECT session,MIN(trading_date) first_date,MAX(trading_date) last_date,
                       COUNT(DISTINCT trading_date) trading_days,COUNT(*) bars
                FROM futures_1m WHERE product={ph}
                GROUP BY session ORDER BY session""",
            (product,),
        )
    return {"product": product, "coverage": rows}

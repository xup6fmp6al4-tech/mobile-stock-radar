from __future__ import annotations

import csv
import io
import time
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Query

import futures_1m_archive as archive
import futures_full_data as full_data
import futures_similarity as legacy
import taifex_overlay as overlay

router = APIRouter(prefix="/api/blackbox/futures", tags=["futures-profile"])

_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
CACHE_SECONDS = 900


def _cache_get(key):
    x = _CACHE.get(key)
    if x and time.time() - x[0] < CACHE_SECONDS:
        return x[1]
    return None


def _cache_put(key, value):
    _CACHE[key] = (time.time(), value)
    return value


def _previous_weekday(d: date) -> date:
    x = d - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def _calendar_dates(trading_date: str, session: str) -> list[str]:
    d = date.fromisoformat(trading_date)
    if session == "day":
        return [str(d)]
    # 夜盤交易日會跨前一個交易日下午與當日凌晨。
    return [str(_previous_weekday(d)), str(d)]


def _download_zip(calendar_date: str):
    d = date.fromisoformat(calendar_date)
    ymd = d.strftime("%Y_%m_%d")
    url = archive.OFFICIAL_ZIP.format(ymd=ymd)
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "*/*",
        "Referer": "https://www.taifex.com.tw/",
        "Cache-Control": "no-cache",
    }
    r = httpx.get(url, headers=headers, timeout=45, follow_redirects=True)
    if r.status_code != 200 or len(r.content) < 200:
        raise RuntimeError(f"official_zip_http_{r.status_code}:{calendar_date}")
    return zipfile.ZipFile(io.BytesIO(r.content))


def _tick_price_volume(product: str, trading_date: str, session: str):
    code = archive.PRODUCT_CODE.get(product)
    if not code:
        return {"ok": False, "rows": [], "error": "unsupported_product"}

    key = ("profile", product, trading_date, session)
    hit = _cache_get(key)
    if hit:
        return {**hit, "cache_hit": True}

    by_exp_total = defaultdict(float)
    by_exp_profile: dict[str, dict[float, float]] = defaultdict(lambda: defaultdict(float))
    matched = 0
    errors = []
    required = ["成交日期", "商品代號", "到期月份(週別)", "成交時間", "成交價格", "成交數量(B+S)"]

    for cal_date in _calendar_dates(trading_date, session):
        try:
            z = _download_zip(cal_date)
        except Exception as exc:
            errors.append(f"{cal_date}:{type(exc).__name__}:{exc}")
            continue

        for name in z.namelist():
            if not name.lower().endswith((".csv", ".txt")):
                continue
            try:
                raw = z.open(name)
                text = io.TextIOWrapper(raw, encoding="cp950", errors="replace", newline="")
                reader = csv.DictReader(text)
                hm = archive._norm_headers(reader.fieldnames)
                if any(k not in hm for k in required):
                    continue

                for row in reader:
                    if str(row.get(hm["商品代號"], "")).strip() != code:
                        continue
                    exp = str(row.get(hm["到期月份(週別)"], "")).strip()
                    try:
                        ds = str(row.get(hm["成交日期"], "")).strip()
                        ts = str(row.get(hm["成交時間"], "")).strip().zfill(6)
                        dt_local = datetime.strptime(ds + " " + ts, "%Y%m%d %H%M%S").replace(tzinfo=archive.TZ)
                        info = legacy._one_min_session_info(dt_local)
                        if not info:
                            continue
                        td, sess, _ = info
                        if str(td) != trading_date or sess != session:
                            continue
                        price = float(str(row.get(hm["成交價格"], "")).strip())
                        volume = float(str(row.get(hm["成交數量(B+S)"], "0")).strip() or 0) / 2.0
                    except Exception:
                        continue
                    by_exp_total[exp] += volume
                    by_exp_profile[exp][price] += volume
                    matched += 1
            except Exception as exc:
                errors.append(f"{cal_date}/{name}:{type(exc).__name__}")
                continue

    if not by_exp_total:
        return _cache_put(key, {
            "ok": False,
            "product": product,
            "trading_date": trading_date,
            "session": session,
            "rows": [],
            "exact": False,
            "source": "taifex_official_daily_tick",
            "error": "no_matching_official_ticks",
            "errors": errors[-5:],
        })

    active = max(by_exp_total, key=by_exp_total.get)
    profile = by_exp_profile[active]
    rows = [
        {"price": p, "volume": v}
        for p, v in sorted(profile.items(), key=lambda kv: kv[0], reverse=True)
        if v > 0
    ]
    total = sum(x["volume"] for x in rows)
    poc = max(rows, key=lambda x: x["volume"]) if rows else None
    out = {
        "ok": bool(rows),
        "product": product,
        "trading_date": trading_date,
        "session": session,
        "active_expiry": active,
        "rows": rows,
        "count": len(rows),
        "total_volume": total,
        "poc_price": None if not poc else poc["price"],
        "poc_volume": None if not poc else poc["volume"],
        "matched_trades": matched,
        "exact": True,
        "source": "taifex_official_daily_tick_price_volume",
        "errors": errors[-5:],
    }
    return _cache_put(key, out)


def _one_minute_estimate(product: str, session: str):
    try:
        j = archive.intraday_bars(product=product, session=session, limit_1m=1500, limit_3m=500)
        bars = [b for b in (j.get("bars_1m") or []) if b.get("close") is not None]
    except Exception as exc:
        return {
            "ok": False, "rows": [], "exact": False,
            "error": f"1m_fallback:{type(exc).__name__}:{exc}"
        }

    agg = defaultdict(float)
    for b in bars:
        try:
            p = float(b["close"])
            q = float(b.get("volume") or 0)
            if q > 0:
                agg[p] += q
        except Exception:
            continue

    rows = [{"price": p, "volume": v} for p, v in sorted(agg.items(), reverse=True)]
    poc = max(rows, key=lambda x: x["volume"]) if rows else None
    return {
        "ok": bool(rows),
        "product": product,
        "trading_date": j.get("trading_date"),
        "session": j.get("session") or session,
        "rows": rows,
        "count": len(rows),
        "total_volume": sum(x["volume"] for x in rows),
        "poc_price": None if not poc else poc["price"],
        "poc_volume": None if not poc else poc["volume"],
        "exact": False,
        "source": "genuine_1m_close_volume_estimate",
        "source_1m": j.get("source_1m"),
        "note": "每分鐘成交量歸到該分鐘收盤價；不是逐筆精確分價。",
    }


@router.get("/price-volume")
def price_volume(
    product: str = Query("TXF_CONT"),
    session: str = Query("day", pattern="^(day|night)$"),
    trading_date: str | None = None,
    limit: int = Query(80, ge=10, le=400),
):
    if product not in archive.PRODUCT_CODE:
        return {"ok": False, "error": "unsupported_product"}

    if not trading_date:
        try:
            bars, td, _ = archive._load_1m(product, session, limit=1)
            trading_date = td
        except Exception:
            trading_date = None
    if not trading_date:
        trading_date = archive._target_date(product, session)

    # 休市/歷史盤優先讀期交所官方逐筆，得到真正逐價成交量。
    if trading_date:
        try:
            exact = _tick_price_volume(product, str(trading_date), session)
            if exact.get("ok"):
                return {**exact, "rows": exact["rows"][:limit]}
        except Exception:
            pass

    # 盤中官方日檔尚未形成時，用真正1分K做可辨識的估算，絕不冒充逐筆。
    est = _one_minute_estimate(product, session)
    return {**est, "rows": (est.get("rows") or [])[:limit]}


@router.get("/diagnostics")
def diagnostics(
    product: str = Query("TXF_CONT"),
    session: str = Query("day", pattern="^(day|night)$"),
):
    key = ("diag", product, session)
    c = _cache_get(key)
    if c:
        return c

    try:
        q = full_data.realtime(product=product, session=session)
    except Exception as exc:
        q = {"ok": False, "error": repr(exc)}

    try:
        intr = archive.intraday_bars(product=product, session=session, limit_1m=1500, limit_3m=500)
    except Exception as exc:
        intr = {"ok": False, "count_1m": 0, "count_3m": 0, "error": repr(exc)}

    try:
        inst = full_data.institutional(product=product)
    except Exception as exc:
        inst = {"ok": False, "error": repr(exc)}

    try:
        margin = full_data.margin(product=product)
    except Exception as exc:
        margin = {"ok": False, "error": repr(exc)}

    try:
        pcr = full_data.put_call_ratio()
    except Exception as exc:
        pcr = {"ok": False, "error": repr(exc)}

    depth = q.get("depth") or []
    bars1 = intr.get("bars_1m") or []
    count5 = len(full_data._aggregate_1m(bars1, 5)) if bars1 else 0

    out = {
        "ok": True,
        "product": product,
        "session": session,
        "trading_date": intr.get("trading_date"),
        "quote": {
            "ok": bool(q.get("ok")),
            "source": q.get("source"),
            "trade_time": q.get("trade_time"),
            "last": q.get("last"),
        },
        "depth": {
            "count": len(depth),
            "complete": len(depth) >= 5,
            "source": q.get("source"),
        },
        "bars": {
            "count_1m": int(intr.get("count_1m") or 0),
            "count_3m": int(intr.get("count_3m") or 0),
            "count_5m": count5,
            "source_1m": intr.get("source_1m"),
            "source_3m": intr.get("source_3m"),
        },
        "institutional": {
            "ok": bool(inst.get("ok")),
            "date": inst.get("date"),
            "source": inst.get("source"),
        },
        "margin": {
            "ok": bool(margin.get("ok")),
            "date": margin.get("date"),
        },
        "put_call_ratio": {
            "ok": bool(pcr.get("ok")),
            "date": pcr.get("date"),
        },
        "market_open": bool(overlay._market_open_info().get("open")),
        "note": "完整度只依實際取得資料判定；缺資料不補假值。",
    }
    return _cache_put(key, out)

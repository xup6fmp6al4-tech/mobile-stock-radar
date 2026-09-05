from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Query

import futures_similarity as legacy

router = APIRouter(prefix="/api/blackbox/futures", tags=["futures-taifex-primary"])
TZ = ZoneInfo("Asia/Taipei")
TAIFEX_OPENAPI_BASE = "https://openapi.taifex.com.tw/v1"

PRODUCTS = {
    "TXF_CONT": {"contract": "TX", "symbol": "WTX&", "page_kind": "future"},
    "MTX_CONT": {"contract": "MTX", "symbol": "WMT&", "page_kind": "future"},
    "TMF_CONT": {"contract": "TMF", "symbol": "WTM&", "page_kind": "quote"},
}

_RAW_CACHE: dict[str, tuple[float, list[dict[str, Any]], float]] = {}
_BAR_CACHE: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
_DAILY_YAHOO_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_FETCH_LOCK = threading.Lock()
DAILY_TTL = 20
TICKS_TTL = 300
YAHOO_DAILY_TTL = 60


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    if not s or s in {"-", "—", "--", "null", "None"}:
        return None
    sign = -1 if "▼" in s else 1
    s = s.replace("▲", "").replace("▼", "").replace("+", "").strip()
    try:
        n = float(s)
    except ValueError:
        return None
    return -abs(n) if sign < 0 else n


def _norm_key(k):
    return re.sub(r"[^a-z0-9]+", "", str(k).lower())


def _row_get(row: dict[str, Any], *names):
    for name in names:
        if name in row:
            return row[name]
    normalized = {_norm_key(k): v for k, v in row.items()}
    for name in names:
        key = _norm_key(name)
        if key in normalized:
            return normalized[key]
    return None


def _taifex_get(endpoint: str, ttl: int):
    now = time.time()
    cached = _RAW_CACHE.get(endpoint)
    if cached and now - cached[0] <= ttl:
        return cached[1], {"cache_hit": True, "provider_latency_ms": round(cached[2], 2)}

    # APP 會同時讀 quote / 1m / 3m / threshold；鎖住可避免同一份大型資料被重抓三次。
    with _FETCH_LOCK:
        now = time.time()
        cached = _RAW_CACHE.get(endpoint)
        if cached and now - cached[0] <= ttl:
            return cached[1], {"cache_hit": True, "provider_latency_ms": round(cached[2], 2)}

        t0 = time.perf_counter()
        with httpx.Client(
            timeout=20,
            follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": "mobile-stock-radar/taifex-primary"},
        ) as client:
            r = client.get(f"{TAIFEX_OPENAPI_BASE}/{endpoint}")
            r.raise_for_status()
            payload = r.json()
        latency = (time.perf_counter() - t0) * 1000

        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("result") or payload.get("items") or []
        if not isinstance(payload, list):
            raise RuntimeError(f"TAIFEX {endpoint}: unexpected payload")
        _RAW_CACHE[endpoint] = (now, payload, latency)
        return payload, {"cache_hit": False, "provider_latency_ms": round(latency, 2)}


def _session_name(v):
    s = str(v or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    if any(x in s for x in ("afterhours", "afterhour", "盤後", "夜盤")):
        return "night"
    if any(x in s for x in ("regular", "一般", "日盤")):
        return "day"
    return None


def _market_open_info():
    dt = datetime.now(TZ)
    mins = dt.hour * 60 + dt.minute
    dow = dt.weekday()  # Mon=0
    if 0 <= dow <= 4 and 8 * 60 + 45 <= mins < 13 * 60 + 45:
        return {"open": True, "session": "day"}
    if 0 <= dow <= 4 and mins >= 15 * 60:
        return {"open": True, "session": "night"}
    if 1 <= dow <= 5 and mins < 5 * 60:
        return {"open": True, "session": "night"}
    return {"open": False, "session": None}


def _contract_month(v):
    m = re.search(r"(20\d{4})", str(v or ""))
    return int(m.group(1)) if m else None


def _date_iso(v):
    digits = re.sub(r"\D", "", str(v or ""))
    if len(digits) != 8:
        return None
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _daily_rows(product: str):
    cfg = PRODUCTS.get(product)
    if not cfg:
        return [], {}
    data, meta = _taifex_get("DailyMarketReportFut", DAILY_TTL)
    rows = []
    for row in data:
        contract = str(_row_get(row, "Contract", "契約", "契約代號", "商品代號") or "").strip().upper()
        if contract == cfg["contract"]:
            rows.append(row)
    return rows, meta


def _select_daily_row(product: str, session: str):
    rows, meta = _daily_rows(product)
    if not rows:
        return None, meta

    same_session = [
        row for row in rows
        if _session_name(_row_get(row, "TradingSession", "交易時段")) == session
    ]
    if not same_session:
        return None, meta

    candidates = []
    for row in same_session:
        month = _contract_month(_row_get(row, "ContractMonth(Week)", "ContractMonthWeek", "到期月份(週別)"))
        if month is not None:
            candidates.append((month, row))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1], meta
    return same_session[0], meta


def _best_daily_row(product: str, preferred_session: str):
    """Return a usable latest official row. Prefer the requested session, but on holidays/weekends
    fall back to the latest available session instead of returning an empty screen."""
    row, meta = _select_daily_row(product, preferred_session)
    if row is not None:
        return row, preferred_session, meta

    rows, meta = _daily_rows(product)
    if not rows:
        return None, preferred_session, meta

    # Keep only rows with a real price and a delivery month. Prefer the latest Date,
    # then the nearest delivery month. If both day/night exist on the same Date,
    # night is fresher after Friday close, otherwise day remains a valid last snapshot.
    candidates = []
    for r in rows:
        last = _num(_row_get(r, "Last", "Close", "最後成交價"))
        if last is None:
            continue
        d = _date_iso(_row_get(r, "Date", "日期")) or ""
        month = _contract_month(_row_get(r, "ContractMonth(Week)", "ContractMonthWeek", "到期月份(週別)"))
        sess = _session_name(_row_get(r, "TradingSession", "交易時段")) or "day"
        candidates.append((d, -(month or 999999), 1 if sess == "night" else 0, r, sess))
    if not candidates:
        return None, preferred_session, meta
    # latest date first; for that date choose nearest month (smallest positive month)
    latest_date = max(x[0] for x in candidates)
    same_date = [x for x in candidates if x[0] == latest_date]
    same_date.sort(key=lambda x: ((-x[1]) if x[1] else 999999, -x[2]))
    _, _, _, r, sess = same_date[0]
    return r, sess, meta


def _official_quote(product: str, session: str):
    row, used_session, meta = _best_daily_row(product, session)
    if row is None:
        return {
            "ok": False,
            "fallback_required": True,
            "reason": "taifex_no_usable_daily_row",
            "source": "taifex_openapi",
            "official_meta": meta,
        }

    market = _market_open_info()
    if market["open"] and market.get("session") == session:
        # DailyMarketReportFut is not an intraday realtime feed. During an open session
        # the frontend must use the realtime fallback, but we still attach last_valid
        # so a fallback failure never wipes the screen to all dashes.
        last_valid = _snapshot_from_daily_row(product, row, used_session, meta)
        return {
            "ok": False,
            "fallback_required": True,
            "reason": "taifex_openapi_not_intraday_realtime",
            "source": "taifex_openapi",
            "official_meta": meta,
            "last_valid": last_valid,
        }

    return _snapshot_from_daily_row(product, row, used_session, meta)


def _snapshot_from_daily_row(product: str, row: dict[str, Any], used_session: str, meta: dict[str, Any]):
    last = _num(_row_get(row, "Last", "Close", "最後成交價"))
    change = _num(_row_get(row, "Change", "漲跌價"))
    change_pct = _num(_row_get(row, "%", "ChangePercent", "Change%", "漲跌%"))
    prev_close = last - change if last is not None and change is not None else None
    return {
        "ok": last is not None,
        "fallback_required": last is None,
        "source": "taifex_openapi",
        "source_detail": "DailyMarketReportFut",
        "fallback": False,
        "last_valid": True,
        "product": product,
        "contract": PRODUCTS.get(product, {}).get("contract"),
        "contract_month": str(_row_get(row, "ContractMonth(Week)", "ContractMonthWeek", "到期月份(週別)") or ""),
        "trading_date": _date_iso(_row_get(row, "Date", "日期")),
        "session": used_session,
        "last": last,
        "open": _num(_row_get(row, "Open", "開盤價")),
        "high": _num(_row_get(row, "High", "最高價")),
        "low": _num(_row_get(row, "Low", "最低價")),
        "volume": _num(_row_get(row, "Volume", "合計成交量", "成交量")),
        "settlement": _num(_row_get(row, "SettlementPrice", "結算價")),
        "open_interest": _num(_row_get(row, "OpenInterest", "未沖銷契約數", "未沖銷契約量")),
        "bid": _num(_row_get(row, "BestBid", "最後最佳買價")),
        "ask": _num(_row_get(row, "BestAsk", "最後最佳賣價")),
        "change": change,
        "change_pct": change_pct,
        "prev_close": prev_close,
        "ts_server": datetime.now(timezone.utc).isoformat(),
        "provider_latency_ms": meta.get("provider_latency_ms"),
        "provider_cache_hit": meta.get("cache_hit", False),
    }


def _parse_trade_time(v):
    digits = re.sub(r"\D", "", str(v or ""))
    if len(digits) < 4:
        return None
    digits = digits.zfill(6)
    try:
        return int(digits[:2]), int(digits[2:4]), int(digits[4:6])
    except ValueError:
        return None


def _night_start_date(trading_date):
    d = trading_date - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _official_trade_bars(product: str, session: str, minutes: int):
    """TAIFEX TimeAndSalesData is a very large end-of-day dataset, not a low-latency
    intraday feed. Do not download it synchronously on every page refresh: that caused
    the free Render worker to stall/restart. Mark it unavailable for realtime K and
    let the endpoint use Yahoo only as the documented fallback.

    Daily/last-valid quote data still comes from TAIFEX first.
    """
    return {
        "ok": False,
        "reason": "taifex_timesales_batch_not_realtime",
        "bars": [],
        "source": "taifex_openapi",
        "source_detail": "TimeAndSalesData",
    }


def _aggregate_bars(bars: list[dict[str, Any]], minutes: int):
    if minutes <= 1:
        return list(bars)
    groups: dict[int, list[dict[str, Any]]] = {}
    for b in bars:
        try:
            idx = int(b.get("bar_index", 0)) // minutes
        except Exception:
            continue
        groups.setdefault(idx, []).append(b)
    out = []
    for idx in sorted(groups):
        g = sorted(groups[idx], key=lambda x: int(x.get("bar_index", 0)))
        if not g:
            continue
        opens = [x.get("open") for x in g if x.get("open") is not None]
        highs = [float(x["high"]) for x in g if x.get("high") is not None]
        lows = [float(x["low"]) for x in g if x.get("low") is not None]
        closes = [x.get("close") for x in g if x.get("close") is not None]
        if not closes:
            continue
        out.append({
            "product": g[0].get("product"),
            "trading_date": g[0].get("trading_date"),
            "session": g[0].get("session"),
            "bar_index": idx,
            "ts_utc": g[0].get("ts_utc"),
            "open": float(opens[0]) if opens else float(closes[0]),
            "high": max(highs) if highs else float(closes[-1]),
            "low": min(lows) if lows else float(closes[-1]),
            "close": float(closes[-1]),
            "volume": sum(float(x.get("volume") or 0) for x in g),
            "source": f"yahoo_fallback_{minutes}m",
        })
    return out


def _yahoo_recent_bars(product: str, session: str, minutes: int, limit: int):
    raw = legacy.get_1m(product=product, session=session, limit=min(1500, max(limit * minutes + 30, 300)))
    raw = dict(raw)
    bars = [b for b in raw.get("bars", []) if b.get("close") is not None]
    if not bars:
        return {
            "ok": False,
            "product": product,
            "session": session,
            "trading_date": raw.get("trading_date"),
            "count": 0,
            "bars": [],
            "source": "yahoo_fallback",
            "error": raw.get("error"),
        }
    agg = _aggregate_bars(bars, minutes)[-limit:]
    return {
        "ok": bool(agg),
        "product": product,
        "session": session,
        "trading_date": raw.get("trading_date") or (agg[-1].get("trading_date") if agg else None),
        "count": len(agg),
        "bars": agg,
        "source": "yahoo_fallback",
        "fallback": True,
        "official_reason": "taifex_timesales_batch_not_realtime",
    }


def _fetch_yahoo_daily(product: str, range_value: str):
    key = (product, range_value)
    cached = _DAILY_YAHOO_CACHE.get(key)
    if cached and time.time() - cached[0] <= YAHOO_DAILY_TTL:
        return cached[1]

    cfg = PRODUCTS.get(product)
    if not cfg:
        raise RuntimeError("unsupported_product")

    symbol = cfg["symbol"]
    encoded = quote(symbol, safe="")
    page = (
        f"https://tw.stock.yahoo.com/future/{encoded}"
        if cfg["page_kind"] == "future"
        else f"https://tw.stock.yahoo.com/quote/{encoded}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

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
            f";autoRefresh={auto};period=1d;range={range_value};"
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
        raise RuntimeError(f"{symbol}: Yahoo daily chart returned no data")
    chart = (data[0] or {}).get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"{symbol}: Yahoo daily chart error: {chart['error']}")

    ts = chart.get("timestamp") or []
    q = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
    opens = q.get("open") or []
    highs = q.get("high") or []
    lows = q.get("low") or []
    closes = q.get("close") or []
    vols = q.get("volume") or []

    bars = []
    for i, sec in enumerate(ts):
        if i >= len(closes) or closes[i] is None:
            continue
        c = float(closes[i])
        o = float(opens[i]) if i < len(opens) and opens[i] is not None else c
        h = float(highs[i]) if i < len(highs) and highs[i] is not None else c
        l = float(lows[i]) if i < len(lows) and lows[i] is not None else c
        v = float(vols[i] or 0) if i < len(vols) and vols[i] is not None else 0.0
        dt_local = datetime.fromtimestamp(int(sec), timezone.utc).astimezone(TZ)
        bars.append({
            "product": product,
            "trading_date": str(dt_local.date()),
            "session": "daily",
            "bar_index": len(bars),
            "ts_utc": int(sec),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
            "source": "yahoo_daily_history_fallback",
        })

    result = {"bars": bars, "symbol": symbol, "range": range_value}
    _DAILY_YAHOO_CACHE[key] = (time.time(), result)
    return result


@router.get("/source-health")
def source_health():
    market = _market_open_info()
    out = {
        "ok": True,
        "policy": ["TAIFEX OpenAPI", "Yahoo fallback"],
        "market": market,
        "products": {},
    }
    for product in PRODUCTS:
        try:
            rows, meta = _daily_rows(product)
            out["products"][product] = {
                "daily_openapi": bool(rows),
                "daily_rows": len(rows),
                "provider_latency_ms": meta.get("provider_latency_ms"),
                "cache_hit": meta.get("cache_hit", False),
            }
        except Exception as exc:
            out["products"][product] = {"daily_openapi": False, "error": repr(exc)}
    return out


@router.get("/quote")
def quote_official_first(
    product: str = "TXF_CONT",
    session: str = Query("day", pattern="^(day|night)$"),
):
    try:
        return _official_quote(product, session)
    except Exception as exc:
        return {
            "ok": False,
            "fallback_required": True,
            "reason": "taifex_openapi_failed",
            "error": repr(exc),
            "source": "taifex_openapi",
        }


@router.get("/1m")
def one_min_official_first(
    product: str = "TXF_CONT",
    session: str = Query("night", pattern="^(night|day)$"),
    limit: int = Query(1000, ge=1, le=1500),
):
    official = _official_trade_bars(product, session, 1)
    if official.get("ok") and official.get("bars"):
        bars = official["bars"][-limit:]
        return {**official, "count": len(bars), "bars": bars, "fallback": False}

    fallback = legacy.get_1m(product=product, session=session, limit=limit)
    fallback = dict(fallback)
    fallback["source"] = "yahoo_fallback"
    fallback["fallback"] = True
    fallback["official_reason"] = official.get("reason")
    return fallback


@router.get("/3m")
def three_min_official_first(
    product: str = "TXF_CONT",
    trading_date: str | None = None,
    session: str = Query("night", pattern="^(night|day)$"),
    limit: int = Query(400, ge=1, le=1000),
):
    if trading_date is None:
        official = _official_trade_bars(product, session, 3)
        if official.get("ok") and official.get("bars"):
            bars = official["bars"][-limit:]
            return {**official, "count": len(bars), "bars": bars, "fallback": False}

        # Fast fallback: build genuine 3m OHLCV from Yahoo's real 1m bars.
        # This is much safer than waiting for the huge TAIFEX TimeAndSalesData file.
        recent = _yahoo_recent_bars(product, session, 3, limit)
        if recent.get("ok") and recent.get("bars"):
            recent["official_reason"] = official.get("reason")
            return recent
    else:
        official = {"reason": "explicit_historical_date_uses_local_archive"}

    # Last fallback for an explicitly requested historical date or if Yahoo 1m is unavailable.
    fallback = legacy.get_3m(product=product, trading_date=trading_date, session=session, limit=limit)
    fallback = dict(fallback)
    fallback["source"] = "local_archive_fallback"
    fallback["fallback"] = True
    fallback["official_reason"] = official.get("reason")
    return fallback


@router.get("/threshold")
def threshold_official_first(
    product: str = "TXF_CONT",
    trading_date: str | None = None,
    session: str = Query("day", pattern="^(day|night)$"),
    source_actionable: bool = False,
):
    if trading_date is None:
        official = _official_trade_bars(product, session, 3)
        if official.get("ok") and official.get("bars"):
            bars = official["bars"]
            result = legacy.compute_dynamic_threshold(bars, source_actionable=source_actionable)
            return {
                "product": product,
                "trading_date": official.get("trading_date"),
                "session": session,
                "count": len(bars),
                "dynamic_threshold": result,
                "source": "taifex_openapi",
                "source_detail": "TimeAndSalesData",
                "fallback": False,
                "provider_latency_ms": official.get("provider_latency_ms"),
            }

        recent = _yahoo_recent_bars(product, session, 3, 400)
        bars = recent.get("bars") or []
        if bars:
            result = legacy.compute_dynamic_threshold(bars, source_actionable=source_actionable)
            return {
                "product": product,
                "trading_date": recent.get("trading_date"),
                "session": session,
                "count": len(bars),
                "dynamic_threshold": result,
                "source": "yahoo_fallback",
                "fallback": True,
                "official_reason": official.get("reason"),
            }
    else:
        official = {"reason": "explicit_historical_date_uses_local_archive"}

    fallback = legacy.threshold(
        product=product,
        trading_date=trading_date,
        session=session,
        source_actionable=source_actionable,
    )
    fallback = dict(fallback)
    fallback["source"] = "local_archive_fallback"
    fallback["fallback"] = True
    fallback["official_reason"] = official.get("reason")
    return fallback


@router.get("/daily")
def daily_official_first(
    product: str = "TXF_CONT",
    range_value: str = Query("6mo", alias="range", pattern="^(1mo|3mo|6mo|1y|2y|5y)$"),
    limit: int = Query(180, ge=20, le=1300),
):
    official_row = None
    official_meta = {}
    official_error = None
    try:
        official_row, official_meta = _select_daily_row(product, "day")
    except Exception as exc:
        official_error = repr(exc)

    yahoo_error = None
    try:
        history = _fetch_yahoo_daily(product, range_value)
        bars = [b for b in history.get("bars", []) if b.get("close") is not None]
    except Exception as exc:
        bars = []
        yahoo_error = repr(exc)

    if official_row is not None:
        date_iso = _date_iso(_row_get(official_row, "Date", "日期"))
        last = _num(_row_get(official_row, "Last", "Close", "最後成交價"))
        if date_iso and last is not None:
            td = datetime.strptime(date_iso, "%Y-%m-%d")
            official_bar = {
                "product": product,
                "trading_date": date_iso,
                "session": "daily",
                "bar_index": 0,
                "ts_utc": int(td.replace(tzinfo=TZ, hour=13, minute=45).astimezone(timezone.utc).timestamp()),
                "open": _num(_row_get(official_row, "Open", "開盤價")) or last,
                "high": _num(_row_get(official_row, "High", "最高價")) or last,
                "low": _num(_row_get(official_row, "Low", "最低價")) or last,
                "close": last,
                "volume": _num(_row_get(official_row, "Volume", "合計成交量", "成交量")) or 0.0,
                "source": "taifex_openapi_DailyMarketReportFut",
            }
            bars = [b for b in bars if str(b.get("trading_date")) != date_iso] + [official_bar]
            bars.sort(key=lambda b: b.get("ts_utc", 0))

    bars = bars[-limit:]
    for i, bar in enumerate(bars):
        bar["bar_index"] = i

    if official_row is not None and len(bars) > 1:
        source = "taifex_openapi_primary_yahoo_history_fallback"
    elif official_row is not None:
        source = "taifex_openapi"
    else:
        source = "yahoo_fallback"

    return {
        "ok": bool(bars),
        "product": product,
        "symbol": PRODUCTS.get(product, {}).get("symbol"),
        "range": range_value,
        "count": len(bars),
        "first_date": bars[0]["trading_date"] if bars else None,
        "last_date": bars[-1]["trading_date"] if bars else None,
        "bars": bars,
        "source": source,
        "official_source": "DailyMarketReportFut",
        "official_provider_latency_ms": official_meta.get("provider_latency_ms"),
        "official_error": official_error,
        "fallback_error": yahoo_error,
        "note": "最新日資料以 TAIFEX OpenAPI 為準；OpenAPI 不提供多日歷史查詢，缺少的歷史日K才由 Yahoo 補。",
    }

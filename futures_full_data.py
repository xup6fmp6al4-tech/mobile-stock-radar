from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Query

import taifex_overlay as primary

router = APIRouter(prefix="/api/blackbox/futures", tags=["futures-full-data"])
TZ = ZoneInfo("Asia/Taipei")
MIS_QUOTE_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
MIS_CHART_URL = "https://mis.taifex.com.tw/futures/api/getChartDataTick"
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

PRODUCT_META = {
    "TXF_CONT": {
        "contract": "TX",
        "cids": ["TXF", "TX"],
        "name_aliases": ["臺股期貨", "台股期貨", "臺指期貨", "台指期貨"],
        "institution_aliases": ["臺股期貨", "台股期貨"],
        "news_query": "台指期 OR 台股期貨",
    },
    "MTX_CONT": {
        "contract": "MTX",
        "cids": ["MXF", "MTX"],
        "name_aliases": ["小型臺指期貨", "小型台指期貨", "小臺", "小台"],
        "institution_aliases": ["小型臺指期貨", "小型台指期貨"],
        "news_query": "小台期 OR 小型台指期貨",
    },
    "TMF_CONT": {
        "contract": "TMF",
        "cids": ["TMF"],
        "name_aliases": ["微型臺指期貨", "微型台指期貨", "微臺", "微台"],
        "institution_aliases": ["微型臺指期貨", "微型台指期貨"],
        "news_query": "微台期 OR 微型台指期貨",
    },
}

_RT_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_RT_TTL = 1.0
_HISTORY: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=240))
_NEWS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_INST_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MISC_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CHART_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "").replace("+", "")
    if not s or s in {"-", "—", "--", "null", "None"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _text(v):
    return "" if v is None else str(v).strip()


def _get(row: dict[str, Any], *names):
    if not isinstance(row, dict):
        return None
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    norm = {str(k).lower().replace("_", "").replace("-", ""): v for k, v in row.items()}
    for n in names:
        k = n.lower().replace("_", "").replace("-", "")
        if k in norm and norm[k] not in (None, ""):
            return norm[k]
    return None


def _fmt_date(v):
    s = "".join(ch for ch in _text(v) if ch.isdigit())
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return _text(v) or None


def _market_type(session: str):
    return "1" if session == "night" else "0"


def _mis_rows(cid: str, session: str):
    payload = {
        "MarketType": _market_type(session),
        "SymbolType": "F",
        "KindID": "1",
        "CID": cid,
        "ExpireMonth": "",
        "RowSize": "全部",
        "PageNo": "",
        "SortColumn": "",
        "AscDesc": "A",
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://mis.taifex.com.tw/futures/",
        "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36",
    }
    t0 = time.perf_counter()
    with httpx.Client(timeout=8, follow_redirects=True, headers=headers) as client:
        r = client.post(MIS_QUOTE_URL, json=payload)
        r.raise_for_status()
        data = r.json()
    latency = (time.perf_counter() - t0) * 1000
    rt = (data or {}).get("RtData") or {}
    rows = rt.get("QuoteList") or rt.get("quoteList") or []
    return rows if isinstance(rows, list) else [], round(latency, 1)


def _score_row(row: dict[str, Any], aliases: list[str]):
    name = _text(_get(row, "DispCName", "CName", "Name", "SymbolName"))
    symbol = _text(_get(row, "SymbolID", "Symbol", "ContractID"))
    expire = _text(_get(row, "ExpireMonth", "CExpireMonth", "ContractMonth"))
    price = _num(_get(row, "CLastPrice", "LastPrice"))
    score = 0
    if price is not None:
        score += 30
    if "近" in name:
        score += 40
    if any(a and a in name for a in aliases):
        score += 20
    if expire:
        digits = "".join(ch for ch in expire if ch.isdigit())
        if len(digits) >= 6:
            score += 8
    if symbol:
        score += 2
    return score


def _select_row(rows: list[dict[str, Any]], aliases: list[str]):
    good = [r for r in rows if isinstance(r, dict)]
    if not good:
        return None
    ranked = sorted(enumerate(good), key=lambda x: (-_score_row(x[1], aliases), x[0]))
    return ranked[0][1]


def _official_overlay(product: str, session: str):
    try:
        d = primary._official_quote(product, session)
        if d.get("last_valid") and isinstance(d.get("last_valid"), dict):
            d = d["last_valid"]
        if not d.get("ok") and session != "day":
            d2 = primary._official_quote(product, "day")
            if d2.get("last_valid") and isinstance(d2.get("last_valid"), dict):
                d2 = d2["last_valid"]
            if d2.get("ok"):
                d = d2
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _parse_depth(row: dict[str, Any]):
    out = []
    for i in range(1, 6):
        bid_price = _num(_get(row, f"CBidPrice{i}", f"BidPrice{i}"))
        bid_qty = _num(_get(row, f"CBidSize{i}", f"CBidQty{i}", f"BidSize{i}", f"BidQty{i}"))
        ask_price = _num(_get(row, f"CAskPrice{i}", f"AskPrice{i}"))
        ask_qty = _num(_get(row, f"CAskSize{i}", f"CAskQty{i}", f"AskSize{i}", f"AskQty{i}"))
        if any(v is not None for v in (bid_price, bid_qty, ask_price, ask_qty)):
            out.append({
                "level": i,
                "bid_price": bid_price,
                "bid_qty": bid_qty,
                "ask_price": ask_price,
                "ask_qty": ask_qty,
            })
    return out


def _parse_mis_quote(product: str, session: str, row: dict[str, Any], latency_ms: float):
    official = _official_overlay(product, session)
    last = _num(_get(row, "CLastPrice", "LastPrice"))
    ref = _num(_get(row, "CRefPrice", "RefPrice", "ReferencePrice"))
    change = _num(_get(row, "CDiff", "Diff", "Change"))
    if change is None and last is not None and ref is not None:
        change = last - ref
    change_pct = ((last - ref) / ref * 100) if last is not None and ref not in (None, 0) else None
    depth = _parse_depth(row)
    bid = depth[0]["bid_price"] if depth else _num(_get(row, "CBidPrice1", "BidPrice1"))
    ask = depth[0]["ask_price"] if depth else _num(_get(row, "CAskPrice1", "AskPrice1"))
    bid_qty = depth[0]["bid_qty"] if depth else _num(_get(row, "CBidSize1", "BidSize1"))
    ask_qty = depth[0]["ask_qty"] if depth else _num(_get(row, "CAskSize1", "AskSize1"))
    high = _num(_get(row, "CHighPrice", "HighPrice"))
    low = _num(_get(row, "CLowPrice", "LowPrice"))
    open_ = _num(_get(row, "COpenPrice", "OpenPrice"))
    total = _num(_get(row, "CTotalVolume", "TotalVolume"))
    amplitude = _num(_get(row, "CAmpRate", "AmpRate"))
    trade_qty = _num(_get(row, "CLastSize", "CDealSize", "CQty", "CTradeVolume", "LastSize"))
    raw_time = _text(_get(row, "CTime", "Time", "TradeTime"))
    raw_date = _text(_get(row, "CDate", "Date", "TradeDate"))
    symbol_id = _text(_get(row, "SymbolID", "Symbol", "ContractID")) or None
    name = _text(_get(row, "DispCName", "CName", "Name")) or None
    status = _text(_get(row, "Status", "CStatus")) or None

    d = {
        **official,
        "ok": last is not None or bid is not None or ask is not None,
        "source": "taifex_mis_realtime",
        "source_detail": "MIS getQuoteList",
        "fallback": False,
        "product": product,
        "session": session,
        "last": last if last is not None else official.get("last"),
        "open": open_ if open_ is not None else official.get("open"),
        "high": high if high is not None else official.get("high"),
        "low": low if low is not None else official.get("low"),
        "prev_close": ref if ref is not None else official.get("prev_close"),
        "change": change,
        "change_pct": change_pct,
        "volume": total if total is not None else official.get("volume"),
        "bid": bid,
        "ask": ask,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "trade_qty": trade_qty,
        "amplitude_pct": amplitude,
        "depth": depth,
        "trade_time": raw_time or None,
        "trade_date": _fmt_date(raw_date),
        "symbol_id": symbol_id,
        "contract_name": name,
        "market_status": status,
        "provider_latency_ms": latency_ms,
        "ts_server": datetime.now(timezone.utc).isoformat(),
    }
    return d


def _remember_snapshot(d: dict[str, Any]):
    key = (d.get("product"), d.get("session"))
    dq = _HISTORY[key]
    stamp = (d.get("trade_time"), d.get("last"), d.get("volume"), d.get("bid"), d.get("ask"))
    if dq and dq[-1].get("_stamp") == stamp:
        return
    prev_volume = dq[-1].get("volume") if dq else None
    delta = None
    if _num(d.get("volume")) is not None and _num(prev_volume) is not None:
        cur = _num(d.get("volume"))
        prv = _num(prev_volume)
        if cur >= prv:
            delta = cur - prv
    dq.append({
        "_stamp": stamp,
        "time": d.get("trade_time") or datetime.now(TZ).strftime("%H:%M:%S"),
        "bid": d.get("bid"),
        "ask": d.get("ask"),
        "last": d.get("last"),
        "trade_qty": d.get("trade_qty") if d.get("trade_qty") is not None else delta,
        "volume": d.get("volume"),
    })




# ----- Intraday 1m / 3m: TAIFEX MIS chart first, Yahoo/local archive fallback -----

def _norm_key(v):
    return "".join(ch for ch in str(v).lower() if ch.isalnum())


def _find_value(row: dict[str, Any], names: tuple[str, ...]):
    if not isinstance(row, dict):
        return None
    wanted = {_norm_key(x) for x in names}
    for k, v in row.items():
        if _norm_key(k) in wanted and v not in (None, ""):
            return v
    return None


def _looks_time(v):
    if v is None:
        return False
    if isinstance(v, (int, float)):
        n = float(v)
        return n >= 1_000_000_000 or 0 <= n <= 235959
    s = str(v).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    return (":" in s and len(digits) >= 4) or len(digits) in (4, 6, 8, 12, 14) or len(digits) >= 10


def _extract_chart_points(payload: Any):
    """Tolerant parser for TAIFEX MIS getChartDataTick.

    The MIS JSON shape has changed across versions.  We therefore accept both
    row-based objects and parallel arrays, but only emit entries that have a
    time-like value and a numeric price.  No price is invented.
    """
    points: list[dict[str, Any]] = []
    time_names = ("CTime", "Time", "TradeTime", "TickTime", "DateTime", "Timestamp", "T", "x")
    price_names = ("CLastPrice", "LastPrice", "Price", "TradePrice", "Close", "CPrice", "P", "y")
    vol_names = ("CVolume", "Volume", "Qty", "Size", "TradeVolume", "CTotalVolume", "V")
    date_names = ("CDate", "Date", "TradeDate", "D")

    def add(t, p, v=None, d=None):
        price = _num(p)
        if price is None or not _looks_time(t):
            return
        points.append({"time": t, "price": price, "volume": _num(v), "date": d})

    def walk(obj, depth=0):
        if depth > 10:
            return
        if isinstance(obj, dict):
            t = _find_value(obj, time_names)
            p = _find_value(obj, price_names)
            if t is not None and p is not None:
                add(t, p, _find_value(obj, vol_names), _find_value(obj, date_names))

            # Parallel arrays such as Time:[...], Price:[...], Volume:[...].
            arrays = {k: v for k, v in obj.items() if isinstance(v, list) and len(v) >= 2}
            if arrays:
                tk = next((k for k in arrays if _norm_key(k) in {_norm_key(x) for x in time_names}), None)
                pk = next((k for k in arrays if _norm_key(k) in {_norm_key(x) for x in price_names}), None)
                vk = next((k for k in arrays if _norm_key(k) in {_norm_key(x) for x in vol_names}), None)
                dk = next((k for k in arrays if _norm_key(k) in {_norm_key(x) for x in date_names}), None)
                if tk and pk:
                    m = min(len(arrays[tk]), len(arrays[pk]))
                    for i in range(m):
                        add(
                            arrays[tk][i], arrays[pk][i],
                            arrays[vk][i] if vk and i < len(arrays[vk]) else None,
                            arrays[dk][i] if dk and i < len(arrays[dk]) else None,
                        )
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    walk(item, depth + 1)
                elif isinstance(item, str) and ("," in item or "|" in item):
                    sep = "," if "," in item else "|"
                    bits = [x.strip() for x in item.split(sep)]
                    tv = next((x for x in bits if _looks_time(x)), None)
                    nums = [_num(x) for x in bits]
                    pv = next((x for x in nums if x is not None and 1000 <= x <= 200000), None)
                    if tv is not None and pv is not None:
                        add(tv, pv)

    walk(payload)
    return points


def _parse_point_dt(raw_time, raw_date, date_hint: str | None, session: str):
    # Epoch seconds / milliseconds.
    if isinstance(raw_time, (int, float)):
        n = float(raw_time)
        if n >= 1e12:
            return datetime.fromtimestamp(n / 1000.0, timezone.utc).astimezone(TZ)
        if n >= 1e9:
            return datetime.fromtimestamp(n, timezone.utc).astimezone(TZ)

    s = str(raw_time or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 13 and digits[:4].startswith("20"):
        digits = digits[:14]
        try:
            return datetime.strptime(digits, "%Y%m%d%H%M%S").replace(tzinfo=TZ)
        except Exception:
            pass
    if len(digits) >= 10 and not digits[:4].startswith("20"):
        try:
            n = int(digits[:13])
            if n >= 1e12:
                return datetime.fromtimestamp(n / 1000.0, timezone.utc).astimezone(TZ)
            if n >= 1e9:
                return datetime.fromtimestamp(n, timezone.utc).astimezone(TZ)
        except Exception:
            pass

    d = _fmt_date(raw_date) or date_hint
    if not d:
        d = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        base = datetime.strptime(d, "%Y-%m-%d")
    except Exception:
        base = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

    # HH:MM:SS or HHMMSS / HHMM.
    if ":" in s:
        parts = s.split(":")
        try:
            hh, mm = int(parts[0]), int(parts[1])
            ss = int(float(parts[2])) if len(parts) > 2 else 0
        except Exception:
            return None
    else:
        td = digits[-6:].zfill(6) if len(digits) >= 5 else digits.zfill(4) + "00"
        try:
            hh, mm, ss = int(td[:2]), int(td[2:4]), int(td[4:6])
        except Exception:
            return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None

    # For night session the trading date often denotes the next business day.
    # 15:00-23:59 belongs to the previous calendar day; 00:00-05:00 stays on trading date.
    cal = base
    if session == "night" and hh >= 15:
        from datetime import timedelta
        cal = base - timedelta(days=1)
        while cal.weekday() >= 5:
            cal -= timedelta(days=1)
    return cal.replace(hour=hh, minute=mm, second=ss, tzinfo=TZ)


def _aggregate_points_to_1m(points: list[dict[str, Any]], date_hint: str | None, session: str, product: str):
    parsed = []
    for p in points:
        dt = _parse_point_dt(p.get("time"), p.get("date"), date_hint, session)
        if dt is None:
            continue
        mins = dt.hour * 60 + dt.minute
        if session == "day":
            if not (8 * 60 + 45 <= mins <= 13 * 60 + 45):
                continue
            idx = mins - (8 * 60 + 45)
        else:
            if not (mins >= 15 * 60 or mins <= 5 * 60):
                continue
            idx = mins - 15 * 60 if mins >= 15 * 60 else 9 * 60 + mins
        parsed.append((dt, idx, float(p["price"]), p.get("volume")))
    if not parsed:
        return []
    parsed.sort(key=lambda x: x[0])

    # Detect cumulative volume. If most observed volumes are non-decreasing, convert to deltas.
    vols = [float(x[3]) for x in parsed if x[3] is not None]
    cumulative = False
    if len(vols) >= 4:
        nondec = sum(1 for a, b in zip(vols, vols[1:]) if b >= a)
        cumulative = nondec / max(1, len(vols) - 1) >= 0.8 and max(vols) > max(10.0, min(vols) * 1.5)

    buckets: dict[int, dict[str, Any]] = {}
    prev_cum = None
    for dt, idx, price, vol in parsed:
        qty = 0.0
        if vol is not None:
            vv = max(0.0, float(vol))
            if cumulative:
                qty = max(0.0, vv - prev_cum) if prev_cum is not None else 0.0
                prev_cum = vv
            else:
                qty = vv
        b = buckets.get(idx)
        if b is None:
            buckets[idx] = {
                "product": product,
                "trading_date": date_hint or dt.strftime("%Y-%m-%d"),
                "session": session,
                "bar_index": idx,
                "ts_utc": int(dt.replace(second=0, microsecond=0).astimezone(timezone.utc).timestamp()),
                "open": price, "high": price, "low": price, "close": price,
                "volume": qty,
                "source": "taifex_mis_getChartDataTick_1m",
            }
        else:
            b["high"] = max(float(b["high"]), price)
            b["low"] = min(float(b["low"]), price)
            b["close"] = price
            b["volume"] = float(b.get("volume") or 0) + qty
    return [buckets[k] for k in sorted(buckets)]


def _aggregate_1m(bars: list[dict[str, Any]], minutes: int):
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
        closes = [_num(x.get("close")) for x in g]
        closes = [x for x in closes if x is not None]
        if not closes:
            continue
        highs = [_num(x.get("high")) for x in g]; highs = [x for x in highs if x is not None]
        lows = [_num(x.get("low")) for x in g]; lows = [x for x in lows if x is not None]
        op = _num(g[0].get("open")); cl = _num(g[-1].get("close"))
        out.append({
            "product": g[0].get("product"),
            "trading_date": g[0].get("trading_date"),
            "session": g[0].get("session"),
            "bar_index": idx,
            "ts_utc": g[0].get("ts_utc"),
            "open": op if op is not None else closes[0],
            "high": max(highs or closes),
            "low": min(lows or closes),
            "close": cl if cl is not None else closes[-1],
            "volume": sum(float(x.get("volume") or 0) for x in g),
            "source": f"{g[0].get('source') or 'intraday'}_{minutes}m",
        })
    return out


def _mis_chart_1m(product: str, session: str):
    key = (product, session)
    cached = _CHART_CACHE.get(key)
    if cached and time.time() - cached[0] <= 10:
        return cached[1]
    meta = PRODUCT_META.get(product)
    if not meta:
        return {"ok": False, "bars": [], "error": "unsupported_product"}
    errors = []
    for cid in meta["cids"]:
        try:
            rows, _ = _mis_rows(cid, session)
            row = _select_row(rows, meta["name_aliases"])
            if not row:
                errors.append(f"{cid}: no quote row")
                continue
            symbol_id = _text(_get(row, "SymbolID", "Symbol", "ContractID"))
            if not symbol_id:
                errors.append(f"{cid}: no SymbolID")
                continue
            date_hint = _fmt_date(_get(row, "CDate", "Date", "TradeDate"))
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Referer": "https://mis.taifex.com.tw/futures/",
                "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36",
            }
            t0 = time.perf_counter()
            with httpx.Client(timeout=10, follow_redirects=True, headers=headers) as client:
                r = client.post(MIS_CHART_URL, json={"SymbolID": symbol_id})
                r.raise_for_status()
                payload = r.json()
            latency = round((time.perf_counter() - t0) * 1000, 1)
            points = _extract_chart_points(payload)
            bars = _aggregate_points_to_1m(points, date_hint, session, product)
            # A single quote object is not a usable chart. Require at least 2 minutes.
            if len(bars) >= 2:
                out = {
                    "ok": True, "product": product, "session": session,
                    "trading_date": bars[-1].get("trading_date") or date_hint,
                    "bars": bars, "count": len(bars),
                    "source": "taifex_mis_getChartDataTick",
                    "symbol_id": symbol_id, "provider_latency_ms": latency,
                    "raw_points": len(points),
                }
                _CHART_CACHE[key] = (time.time(), out)
                return out
            errors.append(f"{cid}/{symbol_id}: chart points={len(points)} bars={len(bars)}")
        except Exception as exc:
            errors.append(f"{cid}: {type(exc).__name__}: {exc}")
    out = {"ok": False, "product": product, "session": session, "bars": [], "errors": errors[-6:], "source": "taifex_mis_getChartDataTick"}
    _CHART_CACHE[key] = (time.time(), out)
    return out


def _history_snapshots_1m(product: str, session: str):
    rows = list(_HISTORY[(product, session)])
    if len(rows) < 2:
        return []
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    points = [{"time": r.get("time"), "price": r.get("last"), "volume": r.get("volume"), "date": today} for r in rows if r.get("last") is not None]
    bars = _aggregate_points_to_1m(points, today, session, product)
    for b in bars:
        b["source"] = "taifex_mis_observed_snapshots_1m"
    return bars


@router.get("/intraday-bars")
def intraday_bars(
    product: str = Query("TXF_CONT"),
    session: str = Query("day", pattern="^(day|night)$"),
    limit_1m: int = Query(1000, ge=1, le=1500),
    limit_3m: int = Query(400, ge=1, le=1000),
):
    """Return usable 1m + 3m bars from one endpoint.

    Source order: TAIFEX MIS chart -> Yahoo genuine 1m -> APP-observed MIS snapshots.
    3m is aggregated from real 1m whenever possible; otherwise local archived 3m is used.
    """
    if product not in PRODUCT_META:
        return {"ok": False, "error": "unsupported_product", "product": product}
    errors = []
    used_session = session
    bars1: list[dict[str, Any]] = []
    src1 = None
    trading_date = None

    # 1) Official public MIS chart.
    mis = _mis_chart_1m(product, session)
    if mis.get("ok") and mis.get("bars"):
        bars1 = list(mis["bars"])
        src1 = mis.get("source")
        trading_date = mis.get("trading_date")
    else:
        errors.extend(mis.get("errors") or [])

    # 2) Yahoo 1m, including its 5d closed-market fallback.
    if not bars1:
        try:
            y = primary.legacy.get_1m(product=product, session=session, limit=limit_1m)
            yb = [b for b in (y.get("bars") or []) if b.get("close") is not None]
            if yb:
                bars1 = yb
                src1 = y.get("source") or "yahoo_1m_live"
                trading_date = y.get("trading_date")
            elif y.get("error"):
                errors.append(str(y.get("error")))
        except Exception as exc:
            errors.append(f"Yahoo1m: {type(exc).__name__}: {exc}")

    # 3) Current live session can be built from our real MIS polling snapshots.
    if not bars1:
        hb = _history_snapshots_1m(product, session)
        if hb:
            bars1 = hb
            src1 = "taifex_mis_observed_snapshots_1m"
            trading_date = hb[-1].get("trading_date")

    # Closed market: if user-selected session has no 1m at all, try the other latest session.
    market = primary._market_open_info()
    if not bars1 and not market.get("open"):
        alt = "night" if session == "day" else "day"
        mis2 = _mis_chart_1m(product, alt)
        if mis2.get("ok") and mis2.get("bars"):
            bars1 = list(mis2["bars"]); src1 = mis2.get("source"); trading_date = mis2.get("trading_date"); used_session = alt
        else:
            try:
                y2 = primary.legacy.get_1m(product=product, session=alt, limit=limit_1m)
                y2b = [b for b in (y2.get("bars") or []) if b.get("close") is not None]
                if y2b:
                    bars1 = y2b; src1 = y2.get("source") or "yahoo_1m_live"; trading_date = y2.get("trading_date"); used_session = alt
            except Exception as exc:
                errors.append(f"alt1m: {type(exc).__name__}: {exc}")

    bars1 = bars1[-limit_1m:]
    bars3 = _aggregate_1m(bars1, 3)[-limit_3m:] if bars1 else []
    src3 = f"{src1}_aggregate_3m" if bars3 and src1 else None

    # Last fallback for 3m only: persistent/local archive if present.
    if not bars3:
        for sess in ([session] if used_session == session else [used_session, session]):
            try:
                a = primary.legacy.get_3m(product=product, session=sess, limit=limit_3m)
                ab = [b for b in (a.get("bars") or []) if b.get("close") is not None]
                if ab:
                    bars3 = ab[-limit_3m:]
                    src3 = "local_archive_3m"
                    trading_date = trading_date or a.get("trading_date")
                    used_session = sess
                    break
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
        "errors": errors[-8:],
        "note": "1m/3m are never filled with invented prices; source is surfaced to the APP.",
    }


@router.get("/realtime")
def realtime(
    product: str = Query("TXF_CONT"),
    session: str = Query("day", pattern="^(day|night)$"),
):
    if product not in PRODUCT_META:
        return {"ok": False, "error": "unsupported_product", "product": product}
    key = (product, session)
    now = time.time()
    cached = _RT_CACHE.get(key)
    if cached and now - cached[0] <= _RT_TTL:
        return {**cached[1], "provider_cache_hit": True}

    meta = PRODUCT_META[product]
    errors = []
    # Try requested session first. If the public MIS has no rows in a closed session,
    # try the other market type only to obtain the most recent visible snapshot; the
    # returned 'requested_session' keeps the UI honest about what the user selected.
    sessions_to_try = [session, "night" if session == "day" else "day"]
    for used_session in sessions_to_try:
        for cid in meta["cids"]:
            try:
                rows, latency = _mis_rows(cid, used_session)
                row = _select_row(rows, meta["name_aliases"])
                if not row:
                    errors.append(f"{used_session}/{cid}: no rows")
                    continue
                d = _parse_mis_quote(product, used_session, row, latency)
                if not d.get("ok"):
                    errors.append(f"{used_session}/{cid}: no usable quote")
                    continue
                d["requested_session"] = session
                d["session_fallback"] = used_session != session
                d["cid_used"] = cid
                _remember_snapshot(d)
                _RT_CACHE[key] = (time.time(), d)
                return d
            except Exception as exc:
                errors.append(f"{used_session}/{cid}: {type(exc).__name__}: {exc}")

    official = _official_overlay(product, session)
    if official.get("ok"):
        d = {
            **official,
            "ok": True,
            "source": "taifex_openapi_last_valid",
            "source_detail": "MIS unavailable; official daily last-valid",
            "requested_session": session,
            "depth": [],
            "bid_qty": None,
            "ask_qty": None,
            "trade_qty": None,
            "realtime_error": errors[-3:],
        }
        _RT_CACHE[key] = (time.time(), d)
        return d
    return {"ok": False, "product": product, "session": session, "error": "mis_and_official_unavailable", "details": errors[-6:]}


@router.get("/snapshot-history")
def snapshot_history(
    product: str = Query("TXF_CONT"),
    session: str = Query("day", pattern="^(day|night)$"),
    limit: int = Query(30, ge=1, le=120),
):
    rows = list(_HISTORY[(product, session)])[-limit:]
    clean = [{k: v for k, v in r.items() if k != "_stamp"} for r in rows]
    return {
        "ok": True,
        "product": product,
        "session": session,
        "count": len(clean),
        "rows": list(reversed(clean)),
        "source": "taifex_mis_observed_snapshots",
        "note": "APP polling snapshots; not exchange tick-by-tick history",
    }


def _institution_rows(product: str):
    endpoint = "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"
    data, meta = primary._taifex_get(endpoint, 300)
    aliases = set(PRODUCT_META[product]["institution_aliases"])
    rows = [r for r in data if _text(_get(r, "ContractCode")) in aliases]
    if not rows and product != "TXF_CONT":
        # Some TAIFEX releases aggregate mini/micro institutional rows differently;
        # never substitute TX values for MTX/TMF. Return empty instead.
        rows = []
    return rows, meta


@router.get("/institutional")
def institutional(product: str = Query("TXF_CONT")):
    if product not in PRODUCT_META:
        return {"ok": False, "error": "unsupported_product"}
    c = _INST_CACHE.get(product)
    if c and time.time() - c[0] < 300:
        return {**c[1], "provider_cache_hit": True}
    try:
        rows, meta = _institution_rows(product)
        if not rows:
            out = {"ok": False, "product": product, "reason": "no_contract_rows", "source": "taifex_openapi"}
            _INST_CACHE[product] = (time.time(), out)
            return out
        parsed = {}
        date = None
        for r in rows:
            item = _text(_get(r, "Item"))
            key = "foreign" if item == "外資及陸資" else "trust" if item == "投信" else "dealer" if item == "自營商" else None
            if not key:
                continue
            date = _fmt_date(_get(r, "Date")) or date
            parsed[key] = {
                "label": "外資" if key == "foreign" else "投信" if key == "trust" else "自營商",
                "trading_long": _num(_get(r, "TradingVolume(Long)")),
                "trading_short": _num(_get(r, "TradingVolume(Short)")),
                "trading_net": _num(_get(r, "TradingVolume(Net)")),
                "oi_long": _num(_get(r, "OpenInterest(Long)")),
                "oi_short": _num(_get(r, "OpenInterest(Short)")),
                "oi_net": _num(_get(r, "OpenInterest(Net)")),
            }
        total = {}
        for fld in ("trading_long", "trading_short", "trading_net", "oi_long", "oi_short", "oi_net"):
            vals = [v.get(fld) for v in parsed.values() if v.get(fld) is not None]
            total[fld] = sum(vals) if vals else None
        out = {
            "ok": bool(parsed),
            "product": product,
            "contract": PRODUCT_META[product]["contract"],
            "date": date,
            "institutions": parsed,
            "total": total,
            "source": "taifex_openapi",
            "source_detail": endpoint,
            "provider_latency_ms": meta.get("provider_latency_ms"),
        }
        _INST_CACHE[product] = (time.time(), out)
        return out
    except Exception as exc:
        return {"ok": False, "product": product, "error": repr(exc), "source": "taifex_openapi"}


@router.get("/large-trader")
def large_trader(product: str = Query("TXF_CONT")):
    key = f"large:{product}"
    c = _MISC_CACHE.get(key)
    if c and time.time() - c[0] < 300:
        return c[1]
    aliases = set(PRODUCT_META.get(product, {}).get("institution_aliases", []))
    try:
        data, meta = primary._taifex_get("OpenInterestOfLargeTradersFutures", 300)
        candidates = []
        for r in data:
            name = _text(_get(r, "ContractCode", "Contract", "ProductName", "商品名稱", "商品別"))
            if aliases and name not in aliases and not any(a in name for a in aliases):
                continue
            candidates.append(r)
        r = candidates[0] if candidates else None
        if not r:
            out = {"ok": False, "product": product, "reason": "no_matching_row", "source": "taifex_openapi"}
        else:
            out = {
                "ok": True,
                "product": product,
                "date": _fmt_date(_get(r, "Date")),
                "top5_buy": _num(_get(r, "Top5Buy", "前五大交易人買方")),
                "top5_sell": _num(_get(r, "Top5Sell", "前五大交易人賣方")),
                "top10_buy": _num(_get(r, "Top10Buy", "前十大交易人買方")),
                "top10_sell": _num(_get(r, "Top10Sell", "前十大交易人賣方")),
                "market_oi": _num(_get(r, "OIOfMarket", "OpenInterest", "市場未平倉")),
                "source": "taifex_openapi",
                "source_detail": "OpenInterestOfLargeTradersFutures",
                "provider_latency_ms": meta.get("provider_latency_ms"),
            }
        _MISC_CACHE[key] = (time.time(), out)
        return out
    except Exception as exc:
        return {"ok": False, "product": product, "error": repr(exc), "source": "taifex_openapi"}


@router.get("/put-call-ratio")
def put_call_ratio():
    key = "pcr"
    c = _MISC_CACHE.get(key)
    if c and time.time() - c[0] < 300:
        return c[1]
    try:
        data, meta = primary._taifex_get("PutCallRatio", 300)
        r = data[0] if data else None
        if not r:
            out = {"ok": False, "reason": "no_rows", "source": "taifex_openapi"}
        else:
            out = {
                "ok": True,
                "date": _fmt_date(_get(r, "Date")),
                "volume_ratio_pct": _num(_get(r, "PutCallVolumeRatio%", "PutCallVolumeRatio", "VolumeRatio")),
                "oi_ratio_pct": _num(_get(r, "PutCallOpenInterestRatio%", "PutCallOpenInterestRatio", "OpenInterestRatio")),
                "source": "taifex_openapi",
                "source_detail": "PutCallRatio",
                "provider_latency_ms": meta.get("provider_latency_ms"),
            }
        _MISC_CACHE[key] = (time.time(), out)
        return out
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "source": "taifex_openapi"}


@router.get("/margin")
def margin(product: str = Query("TXF_CONT")):
    key = f"margin:{product}"
    c = _MISC_CACHE.get(key)
    if c and time.time() - c[0] < 900:
        return c[1]
    aliases = set(PRODUCT_META.get(product, {}).get("institution_aliases", []))
    contract = PRODUCT_META.get(product, {}).get("contract")
    try:
        data, meta = primary._taifex_get("IndexFuturesAndOptionsMargining", 900)
        selected = None
        for r in data:
            name = _text(_get(r, "ProductName", "ContractCode", "商品別", "商品名稱", "Contract"))
            code = _text(_get(r, "ProductID", "Contract", "商品代號"))
            if (contract and code == contract) or (aliases and any(a in name for a in aliases)):
                selected = r
                break
        if not selected:
            out = {"ok": False, "product": product, "reason": "no_matching_row", "source": "taifex_openapi"}
        else:
            out = {
                "ok": True,
                "product": product,
                "date": _fmt_date(_get(selected, "Date", "EffectiveDate")),
                "initial": _num(_get(selected, "InitialMargin", "原始保證金", "Initial")),
                "maintenance": _num(_get(selected, "MaintenanceMargin", "維持保證金", "Maintenance")),
                "clearing": _num(_get(selected, "ClearingMargin", "結算保證金", "Clearing")),
                "raw_name": _text(_get(selected, "ProductName", "商品別", "商品名稱")) or None,
                "source": "taifex_openapi",
                "source_detail": "IndexFuturesAndOptionsMargining",
                "provider_latency_ms": meta.get("provider_latency_ms"),
            }
        _MISC_CACHE[key] = (time.time(), out)
        return out
    except Exception as exc:
        return {"ok": False, "product": product, "error": repr(exc), "source": "taifex_openapi"}


@router.get("/news")
def futures_news(product: str = Query("TXF_CONT"), limit: int = Query(10, ge=1, le=20)):
    meta = PRODUCT_META.get(product)
    if not meta:
        return {"ok": False, "error": "unsupported_product"}
    key = meta["news_query"]
    c = _NEWS_CACHE.get(key)
    if c and time.time() - c[0] < 600:
        return {**c[1], "items": c[1].get("items", [])[:limit], "provider_cache_hit": True}
    params = f"?q={quote_plus(key)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        with httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            r = client.get(GOOGLE_NEWS_RSS + params)
            r.raise_for_status()
        root = ET.fromstring(r.text)
        items = []
        for item in root.findall("./channel/item")[:20]:
            source = item.find("source")
            items.append({
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "published": (item.findtext("pubDate") or "").strip(),
                "source": (source.text or "").strip() if source is not None else None,
            })
        out = {"ok": True, "product": product, "items": items, "source": "google_news_rss", "fetched_at": datetime.now(timezone.utc).isoformat()}
        _NEWS_CACHE[key] = (time.time(), out)
        return {**out, "items": items[:limit]}
    except Exception as exc:
        return {"ok": False, "product": product, "items": [], "error": repr(exc), "source": "google_news_rss"}


@router.get("/completeness")
def completeness(product: str = Query("TXF_CONT"), session: str = Query("day", pattern="^(day|night)$")):
    q = realtime(product, session)
    inst = institutional(product)
    margin_data = margin(product)
    pcr = put_call_ratio()
    return {
        "ok": True,
        "product": product,
        "session": session,
        "realtime_quote": bool(q.get("ok")),
        "five_level_depth": len(q.get("depth") or []) >= 5,
        "quote_fields": {k: q.get(k) is not None for k in ["last", "bid", "ask", "open", "high", "low", "prev_close", "volume", "open_interest", "settlement"]},
        "institutional": bool(inst.get("ok")),
        "margin": bool(margin_data.get("ok")),
        "put_call_ratio": bool(pcr.get("ok")),
        "source_realtime": q.get("source"),
        "note": "Free public sources only. Exchange-grade raw order/tick feed is not fabricated when unavailable.",
    }

from __future__ import annotations

import csv
import io
import json
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

def _audit(tag: str, payload: dict[str, Any]):
    try:
        print(f"{tag} " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)
    except Exception:
        pass


def _cache_get(key, max_age: int = CACHE_SECONDS):
    x = _CACHE.get(key)
    if x and time.time() - x[0] < max_age:
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

    # 休市/歷史盤優先讀期交所官方逐筆；盤中當日不把尚未完成的日檔標成精確全日分價。
    market_open = bool(overlay._market_open_info().get("open"))
    if trading_date and not market_open:
        try:
            exact = _tick_price_volume(product, str(trading_date), session)
            if exact.get("ok"):
                out = {**exact, "rows": exact["rows"][:limit]}
                _audit("PV_AUDIT", {
                    "product": product, "session": session, "trading_date": out.get("trading_date"),
                    "ok": bool(out.get("ok")), "exact": bool(out.get("exact")),
                    "count": int(out.get("count") or len(out.get("rows") or [])),
                    "returned_rows": len(out.get("rows") or []),
                    "total_volume": out.get("total_volume"), "poc_price": out.get("poc_price"),
                    "source": out.get("source"), "active_expiry": out.get("active_expiry"),
                })
                return out
        except Exception:
            pass

    # 盤中官方日檔尚未形成時，用真正1分K做可辨識的估算，絕不冒充逐筆。
    est = _one_minute_estimate(product, session)
    out = {**est, "rows": (est.get("rows") or [])[:limit]}
    _audit("PV_AUDIT", {
        "product": product, "session": session, "trading_date": out.get("trading_date"),
        "ok": bool(out.get("ok")), "exact": bool(out.get("exact")),
        "count": int(out.get("count") or len(out.get("rows") or [])),
        "returned_rows": len(out.get("rows") or []),
        "total_volume": out.get("total_volume"), "poc_price": out.get("poc_price"),
        "source": out.get("source"), "source_1m": out.get("source_1m"),
        "error": out.get("error"),
    })
    return out


def _institution_kind(item: Any):
    s = str(item or "").strip()
    n = "".join(ch for ch in s.lower() if ch.isalnum())
    if s in {"外資及陸資", "外資", "外資法人"} or n in {
        "fini", "foreigninstitutionalinvestors", "foreigninvestors",
        "foreigninstitutionalinvestor", "foreignandmainlandchinaInvestors".lower().replace(" ", "")
    }:
        return "foreign"
    if s in {"投信", "投資信託"} or n in {"investmenttrust", "investmenttrusts", "investmenttrustcompany"}:
        return "trust"
    if s in {"自營商", "自營"} or n in {"dealer", "dealers", "proprietarydealer", "proprietarytrader"}:
        return "dealer"
    return None


def _fixed_institution_rows(product: str):
    endpoint = "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"
    data, meta = overlay._taifex_get(endpoint, 300)
    code = archive.PRODUCT_CODE.get(product)
    name_aliases = set(full_data.PRODUCT_META.get(product, {}).get("institution_aliases", []))

    # TAIFEX OpenAPI 的 ContractCode 實際回傳是中文契約名稱，
    # 例如 TX 為「臺股期貨」，不是網站英文頁上顯示的「TX」。
    # 因此同時接受：商品代碼、ContractCode 中文名稱、ProductName 中文名稱。
    rows = []
    for r in data:
        contract_code = str(full_data._get(r, "ContractCode", "Contract", "ProductID", "商品代號") or "").strip()
        contract_name = str(full_data._get(r, "ProductName", "ContractName", "商品名稱", "商品別") or "").strip()

        code_match = bool(code and contract_code.upper() == str(code).upper())
        alias_code_match = bool(name_aliases and any(a and a in contract_code for a in name_aliases))
        alias_name_match = bool(name_aliases and any(a and a in contract_name for a in name_aliases))

        if code_match or alias_code_match or alias_name_match:
            rows.append(r)

    return rows, meta


def institutional_fixed(product: str = "TXF_CONT"):
    if product not in archive.PRODUCT_CODE:
        return {"ok": False, "product": product, "error": "unsupported_product"}
    key = ("institutional_fixed", product)
    hit = _cache_get(key, 300)
    if hit:
        return {**hit, "provider_cache_hit": True}
    endpoint = "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"
    try:
        rows, meta = _fixed_institution_rows(product)
        if not rows:
            out = {
                "ok": False, "product": product, "contract": archive.PRODUCT_CODE.get(product),
                "reason": "no_contract_rows", "source": "taifex_openapi",
                "source_detail": endpoint,
            }
            try:
                raw, _ = overlay._taifex_get(endpoint, 300)
                seen_codes = sorted({
                    str(full_data._get(r, "ContractCode", "Contract", "ProductID", "商品代號") or "").strip()
                    for r in raw
                    if isinstance(r, dict)
                })[:20]
            except Exception:
                seen_codes = []
            _audit("INST_AUDIT", {
                "product": product, "ok": False, "row_count": 0,
                "reason": "no_contract_rows", "seen_contract_codes": seen_codes
            })
            return _cache_put(key, out)

        parsed = {}
        data_date = None
        for r in rows:
            kind = _institution_kind(full_data._get(r, "Item", "Institution", "身份別", "身份"))
            if not kind:
                continue
            data_date = full_data._fmt_date(full_data._get(r, "Date", "TradingDate", "日期")) or data_date
            parsed[kind] = {
                "label": "外資" if kind == "foreign" else "投信" if kind == "trust" else "自營商",
                "trading_long": full_data._num(full_data._get(r, "TradingVolume(Long)", "LongTradingVolume", "交易多方口數")),
                "trading_short": full_data._num(full_data._get(r, "TradingVolume(Short)", "ShortTradingVolume", "交易空方口數")),
                "trading_net": full_data._num(full_data._get(r, "TradingVolume(Net)", "NetTradingVolume", "交易淨額")),
                "oi_long": full_data._num(full_data._get(r, "OpenInterest(Long)", "LongOpenInterest", "未平倉多方口數")),
                "oi_short": full_data._num(full_data._get(r, "OpenInterest(Short)", "ShortOpenInterest", "未平倉空方口數")),
                "oi_net": full_data._num(full_data._get(r, "OpenInterest(Net)", "NetOpenInterest", "未平倉淨額")),
            }

        total = {}
        for fld in ("trading_long", "trading_short", "trading_net", "oi_long", "oi_short", "oi_net"):
            vals = [v.get(fld) for v in parsed.values() if v.get(fld) is not None]
            total[fld] = sum(vals) if vals else None

        out = {
            "ok": bool(parsed),
            "product": product,
            "contract": archive.PRODUCT_CODE.get(product),
            "date": data_date,
            "institutions": parsed,
            "total": total,
            "source": "taifex_openapi",
            "source_detail": endpoint,
            "provider_latency_ms": meta.get("provider_latency_ms"),
            "matched_rows": len(rows),
        }
        _audit("INST_AUDIT", {
            "product": product, "contract": out.get("contract"), "date": data_date,
            "ok": bool(out.get("ok")), "matched_rows": len(rows),
            "keys": sorted(parsed.keys()),
            "foreign_net": parsed.get("foreign", {}).get("trading_net"),
            "foreign_oi_net": parsed.get("foreign", {}).get("oi_net"),
            "trust_net": parsed.get("trust", {}).get("trading_net"),
            "dealer_net": parsed.get("dealer", {}).get("trading_net"),
        })
        return _cache_put(key, out)
    except Exception as exc:
        out = {"ok": False, "product": product, "error": repr(exc), "source": "taifex_openapi", "source_detail": endpoint}
        _audit("INST_AUDIT", {"product": product, "ok": False, "error": repr(exc)})
        return out


# This router is registered before futures_full_data.router, so HTTP requests use the fixed parser.
@router.get("/institutional")
def institutional_route(product: str = Query("TXF_CONT")):
    return institutional_fixed(product)

# diagnostics() calls the module function directly; patch it too.
full_data.institutional = institutional_fixed


@router.get("/diagnostics")
def diagnostics(
    product: str = Query("TXF_CONT"),
    session: str = Query("day", pattern="^(day|night)$"),
):
    key = ("diag", product, session)
    c = _cache_get(key, 10)
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
    _audit("DIAG_AUDIT", {
        "product": product, "session": session, "trading_date": out.get("trading_date"),
        "quote_ok": bool(out.get("quote", {}).get("ok")),
        "quote_source": out.get("quote", {}).get("source"),
        "depth_count": int(out.get("depth", {}).get("count") or 0),
        "depth_complete": bool(out.get("depth", {}).get("complete")),
        "count_1m": int(out.get("bars", {}).get("count_1m") or 0),
        "count_3m": int(out.get("bars", {}).get("count_3m") or 0),
        "count_5m": int(out.get("bars", {}).get("count_5m") or 0),
        "source_1m": out.get("bars", {}).get("source_1m"),
        "source_3m": out.get("bars", {}).get("source_3m"),
        "institutional_ok": bool(out.get("institutional", {}).get("ok")),
        "institutional_date": out.get("institutional", {}).get("date"),
        "margin_ok": bool(out.get("margin", {}).get("ok")),
        "margin_date": out.get("margin", {}).get("date"),
        "pcr_ok": bool(out.get("put_call_ratio", {}).get("ok")),
        "pcr_date": out.get("put_call_ratio", {}).get("date"),
        "market_open": bool(out.get("market_open")),
    })
    return _cache_put(key, out)

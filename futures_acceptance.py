from __future__ import annotations

import calendar
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

import futures_full_data as full
import taifex_overlay as primary

router = APIRouter(prefix="/api/blackbox/futures", tags=["futures-acceptance"])
TZ = ZoneInfo("Asia/Taipei")

PRODUCTS = {
    "TXF_CONT": {"contract": "TX", "aliases": ["臺股期貨", "台股期貨"], "cids": ["TXF", "TX"]},
    "MTX_CONT": {"contract": "MTX", "aliases": ["小型臺指期貨", "小型台指期貨", "小臺", "小台"], "cids": ["MXF", "MTX"]},
    "TMF_CONT": {"contract": "TMF", "aliases": ["微型臺指期貨", "微型台指期貨", "微臺", "微台"], "cids": ["TMF"]},
}

def _n(v):
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return None

def _text(v):
    return "" if v is None else str(v).strip()

def _digits(v):
    return "".join(ch for ch in _text(v) if ch.isdigit())

def _contract_month(v):
    d = _digits(v)
    if len(d) >= 6 and d[:4].startswith("20"):
        try:
            return int(d[:6])
        except Exception:
            return None
    return None

def _third_wednesday(year: int, month: int):
    c = calendar.Calendar(firstweekday=calendar.MONDAY)
    ws = [d for d in c.itermonthdates(year, month) if d.month == month and d.weekday() == 2]
    return ws[2]

def _next_month(yyyymm: int):
    y, m = divmod(yyyymm, 100)
    if m == 12:
        return (y + 1) * 100 + 1
    return y * 100 + m + 1

def _expected_active_month(now: datetime):
    cur = now.year * 100 + now.month
    exp = _third_wednesday(now.year, now.month)
    if now.date() > exp or (now.date() == exp and (now.hour, now.minute) >= (13, 45)):
        return _next_month(cur)
    return cur

def _parse_quote_dt(q: dict[str, Any]):
    d = _text(q.get("trade_date"))
    t = _text(q.get("trade_time"))
    if not d or not t:
        return None

    ds = _digits(d)
    if len(ds) == 8:
        date_s = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
    elif len(d) >= 10 and d[4] == "-" and d[7] == "-":
        date_s = d[:10]
    else:
        return None

    td = _digits(t)
    if len(td) < 4:
        return None
    td = td.zfill(6)
    try:
        return datetime(
            int(date_s[:4]), int(date_s[5:7]), int(date_s[8:10]),
            int(td[:2]), int(td[2:4]), int(td[4:6]), tzinfo=TZ
        )
    except Exception:
        return None

def _depth_audit(q: dict[str, Any], market_open: bool, requested_session: str):
    rows = q.get("depth") or []
    fields_ok = []
    for i, r in enumerate(rows[:5], start=1):
        fields_ok.append(
            int(r.get("level") or i) == i
            and _n(r.get("bid_price")) is not None
            and _n(r.get("bid_qty")) is not None
            and _n(r.get("ask_price")) is not None
            and _n(r.get("ask_qty")) is not None
        )

    five = len(rows) >= 5 and len(fields_ok) == 5 and all(fields_ok)
    bids = [_n(r.get("bid_price")) for r in rows[:5]]
    asks = [_n(r.get("ask_price")) for r in rows[:5]]
    bid_order = five and all(bids[i] >= bids[i + 1] for i in range(4))
    ask_order = five and all(asks[i] <= asks[i + 1] for i in range(4))
    no_cross = five and bids[0] <= asks[0]
    source_ok = q.get("source") == "taifex_mis_realtime"
    same_session = q.get("session") == requested_session and not q.get("session_fallback")

    if market_open:
        live_ok = bool(five and bid_order and ask_order and no_cross and source_ok and same_session)
        state = "pass" if live_ok else "fail"
    else:
        live_ok = None
        state = "deferred_market_closed"

    return {
        "state": state,
        "live_ok": live_ok,
        "count": len(rows),
        "fields_complete": bool(five),
        "bid_descending": bool(bid_order) if five else None,
        "ask_ascending": bool(ask_order) if five else None,
        "best_bid_not_above_best_ask": bool(no_cross) if five else None,
        "source": q.get("source"),
        "same_requested_session": same_session,
    }

def _latency_audit(q: dict[str, Any], request_ms: float, market_open: bool):
    now = datetime.now(TZ)
    qdt = _parse_quote_dt(q)
    quote_age_ms = None
    if qdt is not None:
        quote_age_ms = max(0.0, (now - qdt).total_seconds() * 1000.0)

    provider_ms = _n(q.get("provider_latency_ms"))
    if market_open and quote_age_ms is not None:
        target_ok = quote_age_ms <= 5000
        state = "pass" if target_ok else "fail"
    elif market_open:
        target_ok = None
        state = "unknown_no_exchange_timestamp"
    else:
        target_ok = None
        state = "deferred_market_closed"

    return {
        "state": state,
        "target_5s_ok": target_ok,
        "server_quote_age_ms": round(quote_age_ms, 1) if quote_age_ms is not None else None,
        "server_internal_request_ms": round(request_ms, 1),
        "provider_http_ms": provider_ms,
        "quote_time_iso": qdt.isoformat() if qdt else None,
        "note": "真正手機端總延遲由前端以 quote_time_iso 對手機收到時間計算；provider_http_ms 不是市場資料延遲。",
    }

def _product_identity(product: str, q: dict[str, Any], market_open: bool):
    cfg = PRODUCTS[product]
    cid = _text(q.get("cid_used")).upper()
    name = _text(q.get("contract_name"))
    cid_ok = cid in {x.upper() for x in cfg["cids"]} if cid else None
    name_ok = any(a in name for a in cfg["aliases"]) if name else None
    wrong_tx = product != "TXF_CONT" and (
        cid in {"TXF", "TX"} or any(a in name for a in PRODUCTS["TXF_CONT"]["aliases"])
    )
    ok = not wrong_tx and (cid_ok is not False) and (name_ok is not False)
    return {
        "state": "pass" if ok else "fail",
        "ok": ok,
        "expected_contract": cfg["contract"],
        "cid_used": q.get("cid_used"),
        "contract_name": q.get("contract_name"),
        "no_tx_substitution": not wrong_tx,
        "market_open": market_open,
    }

def _rollover_audit(product: str, session: str):
    cfg = PRODUCTS[product]
    now = datetime.now(TZ)
    expected = _expected_active_month(now)
    candidates = []
    errors = []

    for cid in cfg["cids"]:
        try:
            rows, latency = full._mis_rows(cid, session)
        except Exception as exc:
            errors.append(f"{cid}: {type(exc).__name__}: {exc}")
            continue
        for r in rows:
            name = _text(full._get(r, "DispCName", "CName", "Name", "SymbolName"))
            symbol = _text(full._get(r, "SymbolID", "Symbol", "ContractID"))
            exp_raw = full._get(r, "ExpireMonth", "CExpireMonth", "ContractMonth", "DeliveryMonth")
            exp = _contract_month(exp_raw)
            last = _n(full._get(r, "CLastPrice", "LastPrice"))
            if not exp:
                continue
            if not (any(a in name for a in cfg["aliases"]) or cid in symbol.upper()):
                continue
            candidates.append({
                "cid": cid,
                "symbol_id": symbol or None,
                "name": name or None,
                "contract_month": exp,
                "contract_month_raw": _text(exp_raw) or None,
                "last": last,
            })

    # De-duplicate by symbol/month while preserving real rows.
    seen = set()
    uniq = []
    for x in candidates:
        k = (x["symbol_id"], x["contract_month"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)

    future = [x for x in uniq if x["contract_month"] >= expected]
    active = sorted(future or uniq, key=lambda x: (x["contract_month"], x["symbol_id"] or ""))[0] if (future or uniq) else None

    return {
        "ok": bool(active),
        "product": product,
        "session": session,
        "expected_floor_month": expected,
        "selected_active": active,
        "candidate_count": len(uniq),
        "candidates": sorted(uniq, key=lambda x: (x["contract_month"], x["symbol_id"] or ""))[:12],
        "errors": errors[-4:],
        "rule": "月契約：第三個星期三日盤結束後切到次月；實際選擇仍以 MIS 可交易合約清單為準。",
    }

def _large_group_row():
    data, meta = primary._taifex_get("OpenInterestOfLargeTradersFutures", 300)
    rows = []
    for r in data:
        name = _text(full._get(r, "ContractCode", "Contract", "ProductName", "商品名稱", "商品別"))
        if "臺股期貨" in name or "台股期貨" in name:
            rows.append(r)
    if not rows:
        return None, meta

    def expiry_text(r):
        return _text(full._get(
            r, "ExpirationMonth", "ExpirationMonth(Week)", "ContractMonth",
            "到期月份(週別)", "到期月份", "DeliveryMonth"
        ))

    # Prefer official "all contracts" row. It is the correct market-wide large-trader structure.
    all_rows = [r for r in rows if any(k in expiry_text(r) for k in ("所有", "全部", "All", "ALL"))]
    if all_rows:
        return all_rows[0], meta

    # Otherwise prefer nearest monthly row and explicitly surface its month.
    now = datetime.now(TZ)
    floor = _expected_active_month(now)
    ranked = []
    for r in rows:
        m = _contract_month(expiry_text(r))
        if m is not None:
            ranked.append((0 if m >= floor else 1, abs(m - floor), m, r))
    if ranked:
        ranked.sort(key=lambda x: (x[0], x[1], x[2]))
        return ranked[0][3], meta
    return rows[0], meta

def corrected_large_trader(product: str):
    if product not in PRODUCTS:
        return {"ok": False, "error": "unsupported_product"}

    try:
        r, meta = _large_group_row()
        if not r:
            return {"ok": False, "product": product, "reason": "no_taiwan_index_group_row", "source": "taifex_openapi"}

        name = _text(full._get(r, "ContractCode", "Contract", "ProductName", "商品名稱", "商品別"))
        expiry = _text(full._get(
            r, "ExpirationMonth", "ExpirationMonth(Week)", "ContractMonth",
            "到期月份(週別)", "到期月份", "DeliveryMonth"
        )) or None

        combined = {
            "date": full._fmt_date(full._get(r, "Date")),
            "scope_label": name or "臺股期貨(TX+MTX/4+TMF/20)",
            "expiry_scope": expiry,
            "top5_buy": _n(full._get(r, "Top5Buy", "Top5Long", "前五大交易人買方")),
            "top5_sell": _n(full._get(r, "Top5Sell", "Top5Short", "前五大交易人賣方")),
            "top10_buy": _n(full._get(r, "Top10Buy", "Top10Long", "前十大交易人買方")),
            "top10_sell": _n(full._get(r, "Top10Sell", "Top10Short", "前十大交易人賣方")),
            "market_oi": _n(full._get(r, "OIOfMarket", "OpenInterest", "市場未平倉", "全市場未沖銷部位數")),
            "source": "taifex_openapi",
            "source_detail": "OpenInterestOfLargeTradersFutures",
            "provider_latency_ms": meta.get("provider_latency_ms"),
        }

        if product != "TXF_CONT":
            return {
                "ok": False,
                "product": product,
                "reason": "official_large_trader_is_combined_TX_MTX_TMF_not_product_specific",
                "combined_reference": combined,
                "source": "taifex_openapi",
                "note": "不把 TX+MTX/4+TMF/20 合併數字冒充 MTX 或 TMF 單獨大額交易人。",
            }

        # Even on TX screen, state the official scope explicitly.
        return {
            "ok": True,
            "product": product,
            **combined,
            "combined_index_group": True,
            "note": "期交所此表為臺股期貨群組 TX+MTX/4+TMF/20，不是純 TX 單一契約。",
        }
    except Exception as exc:
        return {"ok": False, "product": product, "error": repr(exc), "source": "taifex_openapi"}

# This route intentionally shadows the older parser because this router is registered first.
@router.get("/large-trader")
def large_trader_v2(product: str = Query("TXF_CONT")):
    return corrected_large_trader(product)

@router.get("/acceptance")
def acceptance(
    product: str = Query("TXF_CONT"),
    session: str = Query("day", pattern="^(day|night)$"),
):
    if product not in PRODUCTS:
        return {"ok": False, "error": "unsupported_product"}

    market = primary._market_open_info()
    market_open_for_request = bool(market.get("open") and market.get("session") == session)

    t0 = time.perf_counter()
    q = full.realtime(product, session)
    request_ms = (time.perf_counter() - t0) * 1000.0

    depth = _depth_audit(q, market_open_for_request, session)
    latency = _latency_audit(q, request_ms, market_open_for_request)
    identity = _product_identity(product, q, market_open_for_request)
    rollover = _rollover_audit(product, session)
    large = corrected_large_trader(product)

    if session == "night":
        if market_open_for_request:
            night_ok = bool(
                q.get("ok")
                and q.get("session") == "night"
                and not q.get("session_fallback")
                and q.get("source") == "taifex_mis_realtime"
            )
            night_state = "pass" if night_ok else "fail"
        else:
            night_ok = None
            night_state = "deferred_market_closed"
    else:
        night_ok = None
        night_state = "not_requested"

    return {
        "ok": bool(q.get("ok")),
        "tested_at": datetime.now(TZ).isoformat(),
        "market": market,
        "product": product,
        "session": session,
        "quote": {
            "ok": q.get("ok"),
            "source": q.get("source"),
            "last": q.get("last"),
            "bid": q.get("bid"),
            "ask": q.get("ask"),
            "trade_date": q.get("trade_date"),
            "trade_time": q.get("trade_time"),
            "cid_used": q.get("cid_used"),
            "contract_name": q.get("contract_name"),
            "session_actual": q.get("session"),
            "session_fallback": q.get("session_fallback"),
        },
        "five_level": depth,
        "latency": latency,
        "product_identity": identity,
        "night_session": {"state": night_state, "live_ok": night_ok},
        "rollover": rollover,
        "large_trader": large,
        "rules": {
            "no_fake_values": True,
            "no_TX_substitution_for_MTX_TMF": True,
            "five_level_live_test_only_when_requested_session_is_open": True,
            "latency_target_ms": 5000,
        },
    }

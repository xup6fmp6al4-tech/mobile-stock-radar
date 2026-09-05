from __future__ import annotations

from statistics import median
from typing import Any, Iterable

DEFAULTS = {
    "noise_pct": 0.0008,
    "trend_pct": 0.0018,
    "accel_pct": 0.0030,
    "big_move_pct": 0.0050,
    "noise_tr_mult": 1.0,
    "trend_tr_mult": 1.8,
    "accel_tr_mult": 2.8,
    "big_move_tr_mult": 4.0,
    "volume_confirm_ratio": 1.15,
    "volume_accel_ratio": 1.35,
    "min_bars": 10,
}


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _round_level(v: float) -> int:
    return int(round(v))


def _median_true_range(rows: list[dict[str, Any]]) -> float:
    trs: list[float] = []
    prev_close: float | None = None
    for r in rows:
        h = _f(r.get("high"))
        l = _f(r.get("low"))
        c = _f(r.get("close"))
        if not c:
            continue
        if h and l:
            if prev_close is None:
                tr = h - l
            else:
                tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            if tr > 0:
                trs.append(tr)
        prev_close = c
    return median(trs[-20:]) if trs else 1.0


def _vwap(rows: list[dict[str, Any]]) -> float:
    num = 0.0
    den = 0.0
    for r in rows:
        h = _f(r.get("high"))
        l = _f(r.get("low"))
        c = _f(r.get("close"))
        v = _f(r.get("volume"))
        if c <= 0 or v <= 0:
            continue
        typical = (h + l + c) / 3 if h > 0 and l > 0 else c
        num += typical * v
        den += v
    if den <= 0:
        closes = [_f(r.get("close")) for r in rows if _f(r.get("close")) > 0]
        return closes[-1] if closes else 0.0
    return num / den


def _volume_ratio(rows: list[dict[str, Any]]) -> float:
    complete = rows[:-1]
    vols = [_f(r.get("volume")) for r in complete if _f(r.get("volume")) > 0]
    if len(vols) < 6:
        return 1.0
    recent = vols[-2:]
    history = vols[:-2][-20:]
    base = median(history) if history else median(vols)
    if base <= 0:
        return 1.0
    return (sum(recent) / len(recent)) / base


def _close_ago(rows: list[dict[str, Any]], bars: int) -> float | None:
    if len(rows) <= bars:
        return None
    return _f(rows[-1 - bars].get("close")) or None


def compute_dynamic_threshold(
    rows: Iterable[dict[str, Any]],
    *,
    config: dict[str, float] | None = None,
    source_actionable: bool = False,
) -> dict[str, Any]:
    cfg = {**DEFAULTS, **(config or {})}
    rs = [dict(r) for r in rows if _f(r.get("close")) > 0]

    if len(rs) < int(cfg["min_bars"]):
        return {
            "ok": False,
            "state": "INSUFFICIENT_DATA",
            "action": "observe",
            "actionable": False,
            "bars": len(rs),
            "reason": f"need at least {int(cfg['min_bars'])} 3m bars",
        }

    price = _f(rs[-1].get("close"))
    session_open = next(
        (_f(r.get("open")) for r in rs if _f(r.get("open")) > 0),
        price,
    )
    tr = max(_median_true_range(rs), 1.0)
    vwap = _vwap(rs)
    vr = _volume_ratio(rs)

    noise_pts = max(price * cfg["noise_pct"], tr * cfg["noise_tr_mult"])
    trend_pts = max(price * cfg["trend_pct"], tr * cfg["trend_tr_mult"])
    accel_pts = max(price * cfg["accel_pct"], tr * cfg["accel_tr_mult"])
    big_pts = max(price * cfg["big_move_pct"], tr * cfg["big_move_tr_mult"])

    c15 = _close_ago(rs, 5)
    c30 = _close_ago(rs, 10)
    m15 = price - c15 if c15 is not None else 0.0
    m30 = price - c30 if c30 is not None else 0.0

    prior15 = rs[-6:-1] if len(rs) >= 6 else rs[:-1]
    prior30 = rs[-11:-1] if len(rs) >= 11 else rs[:-1]

    hi15 = max((_f(r.get("high"), _f(r.get("close"))) for r in prior15), default=price)
    lo15 = min((_f(r.get("low"), _f(r.get("close"))) for r in prior15), default=price)
    hi30 = max((_f(r.get("high"), _f(r.get("close"))) for r in prior30), default=price)
    lo30 = min((_f(r.get("low"), _f(r.get("close"))) for r in prior30), default=price)

    break_up = price > hi15
    break_down = price < lo15
    above_vwap = price > vwap + noise_pts * 0.15
    below_vwap = price < vwap - noise_pts * 0.15

    bull_score = 0
    bear_score = 0

    bull_score += 2 if m15 >= trend_pts else 1 if m15 >= noise_pts else 0
    bear_score += 2 if m15 <= -trend_pts else 1 if m15 <= -noise_pts else 0

    bull_score += 1 if m30 >= trend_pts else 0
    bear_score += 1 if m30 <= -trend_pts else 0

    bull_score += 2 if break_up else 0
    bear_score += 2 if break_down else 0

    bull_score += 1 if above_vwap else 0
    bear_score += 1 if below_vwap else 0

    bull_score += 1 if vr >= cfg["volume_confirm_ratio"] else 0
    bear_score += 1 if vr >= cfg["volume_confirm_ratio"] else 0

    state = "NOISE"
    action = "observe"
    strength = 50

    if m15 >= big_pts and break_up and above_vwap:
        state, action, strength = "BIG_UP", "hold_or_consider_long", 90
    elif m15 <= -big_pts and break_down and below_vwap:
        state, action, strength = "BIG_DOWN", "hold_or_consider_short", 90
    elif (
        m15 >= accel_pts
        and break_up
        and above_vwap
        and vr >= cfg["volume_accel_ratio"]
    ):
        state, action, strength = "BULL_ACCELERATION", "hold_or_consider_long", 80
    elif (
        m15 <= -accel_pts
        and break_down
        and below_vwap
        and vr >= cfg["volume_accel_ratio"]
    ):
        state, action, strength = "BEAR_ACCELERATION", "hold_or_consider_short", 80
    elif bull_score >= 5 and bull_score >= bear_score + 2:
        state, action, strength = "BULL_CONFIRMED", "consider_long", 65
    elif bear_score >= 5 and bear_score >= bull_score + 2:
        state, action, strength = "BEAR_CONFIRMED", "consider_short", 65
    elif abs(m15) <= noise_pts and abs(price - vwap) <= noise_pts:
        state, action, strength = "NOISE", "observe", 50
    else:
        state, action, strength = "TRANSITION", "observe", 55

    if not source_actionable:
        action = "observe"

    return {
        "ok": True,
        "state": state,
        "action": action,
        "actionable": bool(source_actionable),
        "strength_pct": strength,
        "price": round(price, 2),
        "session_open": round(session_open, 2),
        "vwap": round(vwap, 2),
        "metrics": {
            "median_true_range_3m_points": round(tr, 2),
            "momentum_15m_points": round(m15, 2),
            "momentum_30m_points": round(m30, 2),
            "volume_ratio": round(vr, 3),
            "prior_15m_high": round(hi15, 2),
            "prior_15m_low": round(lo15, 2),
            "prior_30m_high": round(hi30, 2),
            "prior_30m_low": round(lo30, 2),
            "bull_score": bull_score,
            "bear_score": bear_score,
        },
        "thresholds": {
            "noise_points": _round_level(noise_pts),
            "trend_confirm_points": _round_level(trend_pts),
            "acceleration_points": _round_level(accel_pts),
            "big_move_points": _round_level(big_pts),
            "bull_start_level_from_open": _round_level(session_open + trend_pts),
            "bear_start_level_from_open": _round_level(session_open - trend_pts),
            "big_up_level_from_open": _round_level(session_open + big_pts),
            "big_down_level_from_open": _round_level(session_open - big_pts),
        },
        "rules": {
            "philosophy": "do_not_guess_turning_points_only_trade_confirmed_middle",
            "volume_confirmation_required": True,
            "vwap_confirmation_required": True,
            "breakout_breakdown_confirmation_required": True,
            "delayed_source_can_trigger_trade": False,
        },
        "reason": (
            f"15m={m15:+.0f}pt; 30m={m30:+.0f}pt; "
            f"VWAP={vwap:.1f}; vol={vr:.2f}x; "
            f"break15={'up' if break_up else 'down' if break_down else 'no'}"
        ),
    }

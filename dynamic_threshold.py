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
    "break_buffer_tr_mult": 0.10,
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


def _overall_trend(
    session_move: float,
    price: float,
    vwap: float,
    noise_pts: float,
    trend_pts: float,
    big_pts: float,
) -> tuple[str, int]:
    above = price > vwap + noise_pts * 0.15
    below = price < vwap - noise_pts * 0.15

    if session_move >= big_pts:
        if above:
            return "BIG_UP", 90
        return "UP_MOVE_WEAKENING", 68

    if session_move <= -big_pts:
        if below:
            return "BIG_DOWN", 90
        return "DOWN_MOVE_WEAKENING", 68

    if session_move >= trend_pts and above:
        return "STRONG_UP", 75

    if session_move <= -trend_pts and below:
        return "STRONG_DOWN", 75

    if session_move >= noise_pts or above:
        return "UP_BIAS", 60

    if session_move <= -noise_pts or below:
        return "DOWN_BIAS", 60

    return "RANGE", 50


def _entry_signal(
    *,
    trend_state: str,
    m15: float,
    m30: float,
    noise_pts: float,
    trend_pts: float,
    accel_pts: float,
    break_up: bool,
    break_down: bool,
    above_vwap: bool,
    below_vwap: bool,
    volume_ratio: float,
    volume_confirm_ratio: float,
    volume_accel_ratio: float,
) -> tuple[str, int, str]:
    up_trend = trend_state in {"BIG_UP", "STRONG_UP", "UP_BIAS"}
    down_trend = trend_state in {"BIG_DOWN", "STRONG_DOWN", "DOWN_BIAS"}

    long_confirm = (
        break_up
        and above_vwap
        and m15 >= noise_pts
        and volume_ratio >= volume_confirm_ratio
    )
    short_confirm = (
        break_down
        and below_vwap
        and m15 <= -noise_pts
        and volume_ratio >= volume_confirm_ratio
    )

    if (
        up_trend
        and long_confirm
        and m15 >= accel_pts
        and m30 >= trend_pts
        and volume_ratio >= volume_accel_ratio
    ):
        return "LONG_ACCELERATION", 85, "long"

    if (
        down_trend
        and short_confirm
        and m15 <= -accel_pts
        and m30 <= -trend_pts
        and volume_ratio >= volume_accel_ratio
    ):
        return "SHORT_ACCELERATION", 85, "short"

    if up_trend and long_confirm:
        strength = 72 if m30 >= 0 else 66
        return "LONG_TRIGGER", strength, "long"

    if down_trend and short_confirm:
        strength = 72 if m30 <= 0 else 66
        return "SHORT_TRIGGER", strength, "short"

    if up_trend and short_confirm:
        return "REVERSAL_WARNING_DOWN", 60, "wait"

    if down_trend and long_confirm:
        return "REVERSAL_WARNING_UP", 60, "wait"

    if trend_state in {"BIG_UP", "STRONG_UP"}:
        return "WAIT_NO_CHASE_UP", 55, "wait"

    if trend_state in {"BIG_DOWN", "STRONG_DOWN"}:
        return "WAIT_NO_CHASE_DOWN", 55, "wait"

    if trend_state in {"UP_MOVE_WEAKENING", "DOWN_MOVE_WEAKENING"}:
        return "WAIT_TREND_WEAKENING", 52, "wait"

    if abs(m15) <= noise_pts:
        return "WAIT_NOISE", 50, "wait"

    return "WAIT_DIRECTION", 52, "wait"


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
            "market_trend_state": "INSUFFICIENT_DATA",
            "entry_state": "WAIT_DATA",
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

    session_high = max(
        (_f(r.get("high"), _f(r.get("close"))) for r in rs),
        default=price,
    )
    session_low = min(
        (_f(r.get("low"), _f(r.get("close"))) for r in rs),
        default=price,
    )

    tr = max(_median_true_range(rs), 1.0)
    vwap = _vwap(rs)
    vr = _volume_ratio(rs)

    noise_pts = max(price * cfg["noise_pct"], tr * cfg["noise_tr_mult"])
    trend_pts = max(price * cfg["trend_pct"], tr * cfg["trend_tr_mult"])
    accel_pts = max(price * cfg["accel_pct"], tr * cfg["accel_tr_mult"])
    big_pts = max(price * cfg["big_move_pct"], tr * cfg["big_move_tr_mult"])

    session_move = price - session_open
    session_move_pct = (session_move / session_open * 100) if session_open else 0.0

    c15 = _close_ago(rs, 5)
    c30 = _close_ago(rs, 10)
    m15 = price - c15 if c15 is not None else 0.0
    m30 = price - c30 if c30 is not None else 0.0

    prior15 = rs[-6:-1] if len(rs) >= 6 else rs[:-1]
    prior30 = rs[-11:-1] if len(rs) >= 11 else rs[:-1]

    hi15 = max(
        (_f(r.get("high"), _f(r.get("close"))) for r in prior15),
        default=price,
    )
    lo15 = min(
        (_f(r.get("low"), _f(r.get("close"))) for r in prior15),
        default=price,
    )
    hi30 = max(
        (_f(r.get("high"), _f(r.get("close"))) for r in prior30),
        default=price,
    )
    lo30 = min(
        (_f(r.get("low"), _f(r.get("close"))) for r in prior30),
        default=price,
    )

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

    if vr >= cfg["volume_confirm_ratio"]:
        bull_score += 1
        bear_score += 1

    market_trend_state, trend_strength = _overall_trend(
        session_move=session_move,
        price=price,
        vwap=vwap,
        noise_pts=noise_pts,
        trend_pts=trend_pts,
        big_pts=big_pts,
    )

    entry_state, entry_strength, analytical_side = _entry_signal(
        trend_state=market_trend_state,
        m15=m15,
        m30=m30,
        noise_pts=noise_pts,
        trend_pts=trend_pts,
        accel_pts=accel_pts,
        break_up=break_up,
        break_down=break_down,
        above_vwap=above_vwap,
        below_vwap=below_vwap,
        volume_ratio=vr,
        volume_confirm_ratio=cfg["volume_confirm_ratio"],
        volume_accel_ratio=cfg["volume_accel_ratio"],
    )

    break_buffer = max(1.0, tr * cfg["break_buffer_tr_mult"])
    next_long_trigger = max(
        hi15 + break_buffer,
        vwap + noise_pts * 0.15,
    )
    next_short_trigger = min(
        lo15 - break_buffer,
        vwap - noise_pts * 0.15,
    )

    if source_actionable:
        if analytical_side == "long":
            action = "consider_long"
        elif analytical_side == "short":
            action = "consider_short"
        else:
            action = "wait"
    else:
        action = "observe"

    return {
        "ok": True,
        "state": market_trend_state,
        "market_trend_state": market_trend_state,
        "market_trend_strength_pct": trend_strength,
        "entry_state": entry_state,
        "entry_strength_pct": entry_strength,
        "action": action,
        "actionable": bool(source_actionable),
        "price": round(price, 2),
        "session_open": round(session_open, 2),
        "vwap": round(vwap, 2),
        "market_trend": {
            "state": market_trend_state,
            "strength_pct": trend_strength,
            "move_from_open_points": round(session_move, 2),
            "move_from_open_pct": round(session_move_pct, 3),
            "session_high": round(session_high, 2),
            "session_low": round(session_low, 2),
            "price_vs_vwap_points": round(price - vwap, 2),
        },
        "entry_signal": {
            "state": entry_state,
            "strength_pct": entry_strength,
            "analytical_side": analytical_side,
            "execution_action": action,
            "actionable": bool(source_actionable),
            "next_long_trigger_level": _round_level(next_long_trigger),
            "next_short_trigger_level": _round_level(next_short_trigger),
            "requirements": {
                "long": "break prior 15m high + above VWAP + 15m momentum + volume confirmation",
                "short": "break prior 15m low + below VWAP + 15m momentum + volume confirmation",
            },
        },
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
            "break_15m": "up" if break_up else "down" if break_down else "no",
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
            "next_long_trigger_level": _round_level(next_long_trigger),
            "next_short_trigger_level": _round_level(next_short_trigger),
        },
        "rules": {
            "philosophy": "do_not_guess_turning_points_only_trade_confirmed_middle",
            "trend_and_entry_are_separate": True,
            "no_chase_after_large_move_without_fresh_breakout": True,
            "counter_trend_signal_is_warning_not_immediate_trade": True,
            "volume_confirmation_required": True,
            "vwap_confirmation_required": True,
            "breakout_breakdown_confirmation_required": True,
            "delayed_source_can_trigger_trade": False,
        },
        "reason": (
            f"trend={market_trend_state}; from_open={session_move:+.0f}pt; "
            f"entry={entry_state}; 15m={m15:+.0f}pt; 30m={m30:+.0f}pt; "
            f"VWAP={vwap:.1f}; vol={vr:.2f}x; "
            f"break15={'up' if break_up else 'down' if break_down else 'no'}"
        ),
    }

from __future__ import annotations

from statistics import median
from typing import Any, Iterable

# v3 — 3-minute primary decision model
# 3m = startup, 6m = confirmation, 9m = decision, 15m = trend filter only.
# Philosophy: do not guess tops/bottoms; trade only confirmed middle segments.

DEFAULTS = {
    "noise_pct": 0.0008,
    "trend_pct": 0.0018,
    "accel_pct": 0.0030,
    "big_move_pct": 0.0050,
    "noise_tr_mult": 1.0,
    "trend_tr_mult": 1.8,
    "accel_tr_mult": 2.8,
    "big_move_tr_mult": 4.0,

    # Faster intraday thresholds for 3m / 6m / 9m decision.
    "startup_3m_pct": 0.00045,
    "confirm_6m_pct": 0.00080,
    "decision_9m_pct": 0.00120,
    "startup_3m_tr_mult": 0.60,
    "confirm_6m_tr_mult": 1.10,
    "decision_9m_tr_mult": 1.60,

    "volume_start_ratio": 1.05,
    "volume_confirm_ratio": 1.15,
    "volume_accel_ratio": 1.35,

    "min_bars": 10,
    "break_buffer_tr_mult": 0.10,
    "vwap_buffer_noise_mult": 0.12,

    # Strong 15m opposition blocks a fast trigger.
    "trend_filter_block_mult": 1.20,
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
    # Latest bar may still be forming; compare recent completed bars vs history.
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


def _window_high_low(
    rows: list[dict[str, Any]], prior_bars: int, price: float
) -> tuple[float, float]:
    if len(rows) < 2:
        return price, price
    window = rows[-(prior_bars + 1):-1]
    if not window:
        return price, price
    hi = max((_f(r.get("high"), _f(r.get("close"))) for r in window), default=price)
    lo = min((_f(r.get("low"), _f(r.get("close"))) for r in window), default=price)
    return hi, lo


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


def _entry_signal_fast(
    *,
    trend_state: str,
    m3: float,
    m6: float,
    m9: float,
    m15: float,
    startup_3m_pts: float,
    confirm_6m_pts: float,
    decision_9m_pts: float,
    trend_pts: float,
    break_3m_up: bool,
    break_3m_down: bool,
    break_6m_up: bool,
    break_6m_down: bool,
    break_9m_up: bool,
    break_9m_down: bool,
    above_vwap: bool,
    below_vwap: bool,
    volume_ratio: float,
    volume_start_ratio: float,
    volume_confirm_ratio: float,
    volume_accel_ratio: float,
    trend_filter_block_mult: float,
) -> tuple[str, int, str, str]:
    up_context = trend_state in {
        "BIG_UP", "STRONG_UP", "UP_BIAS", "UP_MOVE_WEAKENING", "RANGE"
    }
    down_context = trend_state in {
        "BIG_DOWN", "STRONG_DOWN", "DOWN_BIAS", "DOWN_MOVE_WEAKENING", "RANGE"
    }

    strong_15m_down = m15 <= -(trend_pts * trend_filter_block_mult)
    strong_15m_up = m15 >= (trend_pts * trend_filter_block_mult)

    long_start = (
        break_3m_up
        and above_vwap
        and m3 >= startup_3m_pts
        and volume_ratio >= volume_start_ratio
    )
    short_start = (
        break_3m_down
        and below_vwap
        and m3 <= -startup_3m_pts
        and volume_ratio >= volume_start_ratio
    )

    long_confirm = (
        break_6m_up
        and above_vwap
        and m6 >= confirm_6m_pts
        and m3 > 0
        and volume_ratio >= volume_confirm_ratio
    )
    short_confirm = (
        break_6m_down
        and below_vwap
        and m6 <= -confirm_6m_pts
        and m3 < 0
        and volume_ratio >= volume_confirm_ratio
    )

    long_decision = (
        break_9m_up
        and above_vwap
        and m9 >= decision_9m_pts
        and m6 > 0
        and m3 > 0
        and volume_ratio >= volume_confirm_ratio
    )
    short_decision = (
        break_9m_down
        and below_vwap
        and m9 <= -decision_9m_pts
        and m6 < 0
        and m3 < 0
        and volume_ratio >= volume_confirm_ratio
    )

    # 9-minute decision has highest priority.
    if long_decision and not strong_15m_down:
        strength = 80 if volume_ratio >= volume_accel_ratio else 76
        return "LONG_DECISION_9M", strength, "long", "9m_decision"

    if short_decision and not strong_15m_up:
        strength = 80 if volume_ratio >= volume_accel_ratio else 76
        return "SHORT_DECISION_9M", strength, "short", "9m_decision"

    # 6-minute confirmation is actionable analytically, but lower confidence.
    if long_confirm and up_context and not strong_15m_down:
        return "LONG_CONFIRM_6M", 69, "long", "6m_confirm"

    if short_confirm and down_context and not strong_15m_up:
        return "SHORT_CONFIRM_6M", 69, "short", "6m_confirm"

    # 3-minute startup is an alert, not a fully confirmed entry.
    if long_start:
        if strong_15m_down:
            return "LONG_START_3M_COUNTERTREND", 56, "wait", "3m_start"
        return "LONG_START_3M", 61, "watch_long", "3m_start"

    if short_start:
        if strong_15m_up:
            return "SHORT_START_3M_COUNTERTREND", 56, "wait", "3m_start"
        return "SHORT_START_3M", 61, "watch_short", "3m_start"

    # If the session is already stretched, do not chase without a fresh trigger.
    if trend_state in {"BIG_UP", "STRONG_UP"}:
        return "WAIT_NO_CHASE_UP", 55, "wait", "wait"

    if trend_state in {"BIG_DOWN", "STRONG_DOWN"}:
        return "WAIT_NO_CHASE_DOWN", 55, "wait", "wait"

    if trend_state in {"UP_MOVE_WEAKENING", "DOWN_MOVE_WEAKENING"}:
        return "WAIT_TREND_WEAKENING", 52, "wait", "wait"

    return "WAIT_DIRECTION", 50, "wait", "wait"


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

    startup_3m_pts = max(
        price * cfg["startup_3m_pct"],
        tr * cfg["startup_3m_tr_mult"],
    )
    confirm_6m_pts = max(
        price * cfg["confirm_6m_pct"],
        tr * cfg["confirm_6m_tr_mult"],
    )
    decision_9m_pts = max(
        price * cfg["decision_9m_pct"],
        tr * cfg["decision_9m_tr_mult"],
    )

    session_move = price - session_open
    session_move_pct = (session_move / session_open * 100) if session_open else 0.0

    c3 = _close_ago(rs, 1)
    c6 = _close_ago(rs, 2)
    c9 = _close_ago(rs, 3)
    c15 = _close_ago(rs, 5)

    m3 = price - c3 if c3 is not None else 0.0
    m6 = price - c6 if c6 is not None else 0.0
    m9 = price - c9 if c9 is not None else 0.0
    m15 = price - c15 if c15 is not None else 0.0

    hi3, lo3 = _window_high_low(rs, 1, price)
    hi6, lo6 = _window_high_low(rs, 2, price)
    hi9, lo9 = _window_high_low(rs, 3, price)
    hi15, lo15 = _window_high_low(rs, 5, price)

    break_3m_up = price > hi3
    break_3m_down = price < lo3
    break_6m_up = price > hi6
    break_6m_down = price < lo6
    break_9m_up = price > hi9
    break_9m_down = price < lo9

    vwap_buffer = noise_pts * cfg["vwap_buffer_noise_mult"]
    above_vwap = price > vwap + vwap_buffer
    below_vwap = price < vwap - vwap_buffer

    market_trend_state, trend_strength = _overall_trend(
        session_move=session_move,
        price=price,
        vwap=vwap,
        noise_pts=noise_pts,
        trend_pts=trend_pts,
        big_pts=big_pts,
    )

    entry_state, entry_strength, analytical_side, decision_stage = _entry_signal_fast(
        trend_state=market_trend_state,
        m3=m3,
        m6=m6,
        m9=m9,
        m15=m15,
        startup_3m_pts=startup_3m_pts,
        confirm_6m_pts=confirm_6m_pts,
        decision_9m_pts=decision_9m_pts,
        trend_pts=trend_pts,
        break_3m_up=break_3m_up,
        break_3m_down=break_3m_down,
        break_6m_up=break_6m_up,
        break_6m_down=break_6m_down,
        break_9m_up=break_9m_up,
        break_9m_down=break_9m_down,
        above_vwap=above_vwap,
        below_vwap=below_vwap,
        volume_ratio=vr,
        volume_start_ratio=cfg["volume_start_ratio"],
        volume_confirm_ratio=cfg["volume_confirm_ratio"],
        volume_accel_ratio=cfg["volume_accel_ratio"],
        trend_filter_block_mult=cfg["trend_filter_block_mult"],
    )

    break_buffer = max(1.0, tr * cfg["break_buffer_tr_mult"])

    # Fast next trigger uses the recent 3-minute barrier, not the 15-minute high/low.
    next_long_trigger = max(
        hi3 + break_buffer,
        vwap + vwap_buffer,
    )
    next_short_trigger = min(
        lo3 - break_buffer,
        vwap - vwap_buffer,
    )

    # 6m/9m levels are also exposed so the UI can show the confirmation ladder.
    next_long_confirm_6m = max(hi6 + break_buffer, vwap + vwap_buffer)
    next_short_confirm_6m = min(lo6 - break_buffer, vwap - vwap_buffer)
    next_long_decision_9m = max(hi9 + break_buffer, vwap + vwap_buffer)
    next_short_decision_9m = min(lo9 - break_buffer, vwap - vwap_buffer)

    if source_actionable:
        if analytical_side == "long":
            action = "consider_long"
        elif analytical_side == "short":
            action = "consider_short"
        elif analytical_side in {"watch_long", "watch_short"}:
            action = analytical_side
        else:
            action = "wait"
    else:
        action = "observe"

    return {
        "ok": True,
        "model_version": "v3_3m_primary",
        "state": market_trend_state,
        "market_trend_state": market_trend_state,
        "market_trend_strength_pct": trend_strength,
        "entry_state": entry_state,
        "entry_strength_pct": entry_strength,
        "decision_stage": decision_stage,
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
            "decision_stage": decision_stage,
            "analytical_side": analytical_side,
            "execution_action": action,
            "actionable": bool(source_actionable),
            "next_long_trigger_level": _round_level(next_long_trigger),
            "next_short_trigger_level": _round_level(next_short_trigger),
            "next_long_confirm_6m_level": _round_level(next_long_confirm_6m),
            "next_short_confirm_6m_level": _round_level(next_short_confirm_6m),
            "next_long_decision_9m_level": _round_level(next_long_decision_9m),
            "next_short_decision_9m_level": _round_level(next_short_decision_9m),
            "requirements": {
                "3m_start": "break prior 3m high/low + VWAP side + 3m momentum + light volume confirmation",
                "6m_confirm": "6m breakout/breakdown + 6m momentum + 3m same direction + volume confirmation",
                "9m_decision": "9m breakout/breakdown + 9m momentum + 3m/6m same direction + volume confirmation",
                "15m_filter": "15m only blocks strongly opposing fast signals; it does not delay entry",
            },
        },

        "metrics": {
            "median_true_range_3m_points": round(tr, 2),
            "momentum_3m_points": round(m3, 2),
            "momentum_6m_points": round(m6, 2),
            "momentum_9m_points": round(m9, 2),
            "momentum_15m_filter_points": round(m15, 2),
            "volume_ratio": round(vr, 3),

            "prior_3m_high": round(hi3, 2),
            "prior_3m_low": round(lo3, 2),
            "prior_6m_high": round(hi6, 2),
            "prior_6m_low": round(lo6, 2),
            "prior_9m_high": round(hi9, 2),
            "prior_9m_low": round(lo9, 2),
            "prior_15m_high": round(hi15, 2),
            "prior_15m_low": round(lo15, 2),

            "break_3m": "up" if break_3m_up else "down" if break_3m_down else "no",
            "break_6m": "up" if break_6m_up else "down" if break_6m_down else "no",
            "break_9m": "up" if break_9m_up else "down" if break_9m_down else "no",
        },

        "thresholds": {
            "noise_points": _round_level(noise_pts),
            "startup_3m_points": _round_level(startup_3m_pts),
            "confirm_6m_points": _round_level(confirm_6m_pts),
            "decision_9m_points": _round_level(decision_9m_pts),
            "trend_confirm_points_15m_filter": _round_level(trend_pts),
            "acceleration_points": _round_level(accel_pts),
            "big_move_points": _round_level(big_pts),

            "bull_start_level_from_open": _round_level(session_open + trend_pts),
            "bear_start_level_from_open": _round_level(session_open - trend_pts),
            "big_up_level_from_open": _round_level(session_open + big_pts),
            "big_down_level_from_open": _round_level(session_open - big_pts),

            "next_long_trigger_level": _round_level(next_long_trigger),
            "next_short_trigger_level": _round_level(next_short_trigger),
            "next_long_confirm_6m_level": _round_level(next_long_confirm_6m),
            "next_short_confirm_6m_level": _round_level(next_short_confirm_6m),
            "next_long_decision_9m_level": _round_level(next_long_decision_9m),
            "next_short_decision_9m_level": _round_level(next_short_decision_9m),
        },

        "rules": {
            "philosophy": "do_not_guess_turning_points_only_trade_confirmed_middle",
            "primary_decision_timeframe": "3m",
            "confirmation_timeframe": "6m",
            "decision_timeframe": "9m",
            "trend_filter_timeframe": "15m",
            "15m_does_not_delay_entry": True,
            "trend_and_entry_are_separate": True,
            "no_chase_after_large_move_without_fresh_breakout": True,
            "counter_trend_3m_signal_is_warning_first": True,
            "volume_confirmation_required": True,
            "vwap_confirmation_required": True,
            "delayed_source_can_trigger_trade": False,
        },

        "reason": (
            f"trend={market_trend_state}; entry={entry_state}; "
            f"3m={m3:+.0f}pt; 6m={m6:+.0f}pt; 9m={m9:+.0f}pt; "
            f"15m_filter={m15:+.0f}pt; VWAP={vwap:.1f}; vol={vr:.2f}x; "
            f"break3={'up' if break_3m_up else 'down' if break_3m_down else 'no'}; "
            f"break6={'up' if break_6m_up else 'down' if break_6m_down else 'no'}; "
            f"break9={'up' if break_9m_up else 'down' if break_9m_down else 'no'}"
        ),
    }

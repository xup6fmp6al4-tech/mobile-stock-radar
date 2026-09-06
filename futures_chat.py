from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/blackbox/futures", tags=["futures-chat"])

OPENAI_API_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-sol")

DECISION_RULES_VERSION = "20260906-chat-general-v4"
logger = logging.getLogger("futures_chat")

SYSTEM_INSTRUCTIONS = """你是「市場雷達」內建的聊天與期貨判斷助手。
你可以正常聊天，也可以在使用者問到期貨、買賣、持倉、行情時切換成嚴謹的交易討論模式。

回覆語言：繁體中文、台灣用語。
風格：短、直接、自然。不要把每一句話都硬轉成期貨分析。

【一般聊天】
1. 使用者可以聊任何事情。若問題不是交易相關，就正常回答，不要主動塞入持倉、風險、買賣訊號。
2. 使用者罵你、抱怨你或只是打招呼時，要理解成一般對話/回饋，不要誤判成交易指令。
3. 天氣、新聞等即時資訊只能使用 GENERAL_CONTEXT 裡實際提供的新鮮資料；沒有就明說或先追問必要地點，不可猜。
4. 如果 GENERAL_CONTEXT.weather 有資料，回答天氣時優先引用該資料，不要引用市場資料。

【交易資料真實性】
5. 只有使用者明確在問期貨/市場/持倉時，才使用 MARKET_CONTEXT 與 POSITION_CONTEXT。
6. 缺資料就寫「資料不足／—」，不得補假數字、猜五檔、猜法人、猜成交價。
7. 優先順序固定：期交所官方資料 > APP 依官方資料計算 > 其他參考資料。
8. 三竹、券商或其他畫面只能當交叉比對，不是最終真相來源。
9. HTTP 200 不等於資料正確；diagnostics 未通過就明確說未通過。
10. 若 market_open=false，明確說是「最近有效盤／回看」，不可講成目前正在成交。

【交易討論】
11. 使用者問「現在買、賣、做多、做空、等、要不要跑」時：第一行先給結論，再給理由、改判條件；已有持倉才補持倉處理。
12. 使用者反駁時不要只附和，要比較雙方理由並說是否改判。
13. 訊號百分比只是模型訊號強度，不是獲利保證。
14. 價位只能引用資料中已有的觸發位、失效位、POC、高低點或使用者成本，不得捏造。
15. 單一指標不能直接決定買賣；要看共振。追價門檻要比等待更嚴格。
16. 有持倉時優先處理風險，再談加碼。
17. 不替使用者下單，也不聲稱已成交。

【共同規則】
18. 期貨真實性基準是 TAIFEX。
19. APP 的實盤資料目標是端到端延遲 <=5 秒；沒有量測證據不得聲稱達標。
20. 缺資料寧可顯示「—」，不可為了畫面完整而造假。
21. 使用者希望的是自然對話；快捷鍵只是捷徑，不是可問內容的限制。
"""

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=5000)

class PositionContext(BaseModel):
    direction: Literal["none", "long", "short"] = "none"
    entry_price: float | None = None
    contracts: float | None = None
    capital: float | None = None
    note: str | None = Field(default=None, max_length=1000)

class FuturesChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=3000)
    history: list[ChatMessage] = Field(default_factory=list)
    market: dict[str, Any] = Field(default_factory=dict)
    position: PositionContext = Field(default_factory=PositionContext)

def _clip(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, list):
        return [_clip(x, depth + 1) for x in value[:60]]
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= 120:
                break
            out[str(k)[:120]] = _clip(v, depth + 1)
        return out
    return str(value)[:1200]

def _extract_output_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "output_text" and c.get("text"):
                parts.append(str(c["text"]))
    return "\n".join(parts).strip()

def _safe_num(v: Any):
    try:
        n = float(v)
        return n
    except Exception:
        return None


CITY_COORDS = {
    "高雄": (22.6273, 120.3014, "高雄"),
    "台北": (25.0330, 121.5654, "台北"),
    "臺北": (25.0330, 121.5654, "台北"),
    "新北": (25.0120, 121.4657, "新北"),
    "桃園": (24.9937, 121.3010, "桃園"),
    "新竹": (24.8138, 120.9675, "新竹"),
    "台中": (24.1477, 120.6736, "台中"),
    "臺中": (24.1477, 120.6736, "台中"),
    "彰化": (24.0518, 120.5161, "彰化"),
    "嘉義": (23.4801, 120.4491, "嘉義"),
    "台南": (22.9999, 120.2269, "台南"),
    "臺南": (22.9999, 120.2269, "台南"),
    "屏東": (22.6761, 120.4942, "屏東"),
    "宜蘭": (24.7021, 121.7378, "宜蘭"),
    "花蓮": (23.9911, 121.6112, "花蓮"),
    "台東": (22.7554, 121.1500, "台東"),
    "臺東": (22.7554, 121.1500, "台東"),
    "基隆": (25.1276, 121.7392, "基隆"),
    "南投": (23.9609, 120.9719, "南投"),
    "雲林": (23.7092, 120.4313, "雲林"),
    "苗栗": (24.5602, 120.8214, "苗栗"),
    "澎湖": (23.5712, 119.5793, "澎湖"),
    "金門": (24.4494, 118.3767, "金門"),
    "馬祖": (26.1602, 119.9517, "馬祖"),
}

WEATHER_WORDS = ("天氣", "下雨", "降雨", "溫度", "氣溫", "熱不熱", "冷不冷", "會不會下雨")
TRADING_STRONG_WORDS = (
    "期貨", "台指", "臺指", "小台", "小臺", "微台", "微臺", "TX", "MTX", "TMF",
    "做多", "做空", "追多", "追空", "多單", "空單", "持倉", "停損", "停利", "進場", "出場",
    "K線", "3分", "6分", "9分", "量能", "法人", "外資", "分價", "POC", "突破", "回測", "大盤"
)
PROFANITY_WORDS = ("幹你娘", "幹你媽", "操你媽", "靠北", "靠邀", "媽的", "幹")


def _preview(text: str, limit: int = 160) -> str:
    return " ".join((text or "").split())[:limit]


def _looks_weather(message: str, history: list[dict[str, str]]) -> bool:
    msg = message.strip()
    if any(w in msg for w in WEATHER_WORDS):
        return True
    recent_assistant = " ".join(
        x.get("content", "") for x in history[-4:] if x.get("role") == "assistant"
    )
    if ("哪個城市" in recent_assistant or "哪裡的天氣" in recent_assistant) and _find_city(msg):
        return True
    return False


def _looks_trading(message: str) -> bool:
    msg = message.strip()
    if any(w in msg for w in TRADING_STRONG_WORDS):
        return True
    if msg in ("買", "賣", "等"):
        return True
    return any(x in msg for x in ("要不要買", "要不要賣", "現在買", "現在賣", "能買嗎", "能空嗎"))


def _find_city(message: str) -> tuple[float, float, str] | None:
    for key in sorted(CITY_COORDS, key=len, reverse=True):
        if key in message:
            return CITY_COORDS[key]
    return None


def _weather_desc(code: Any) -> str:
    try:
        c = int(code)
    except Exception:
        return "天氣狀況未知"
    if c == 0:
        return "晴朗"
    if c in (1, 2):
        return "晴到多雲"
    if c == 3:
        return "陰天"
    if c in (45, 48):
        return "有霧"
    if c in (51, 53, 55, 56, 57):
        return "毛毛雨"
    if c in (61, 63, 65, 66, 67, 80, 81, 82):
        return "有雨"
    if c in (71, 73, 75, 77, 85, 86):
        return "降雪"
    if c in (95, 96, 99):
        return "雷雨"
    return "天氣狀況未知"


def _fetch_weather(city: tuple[float, float, str]) -> dict[str, Any]:
    lat, lon, name = city
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,precipitation,rain,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Asia/Taipei",
        "forecast_days": 1,
    }
    with httpx.Client(timeout=8, follow_redirects=True) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    current = data.get("current") or {}
    daily = data.get("daily") or {}
    def first(key: str):
        v = daily.get(key)
        return v[0] if isinstance(v, list) and v else None
    return {
        "ok": True,
        "source": "open-meteo",
        "city": name,
        "observed_at": current.get("time"),
        "temperature_c": current.get("temperature_2m"),
        "apparent_c": current.get("apparent_temperature"),
        "precipitation_mm": current.get("precipitation"),
        "rain_mm": current.get("rain"),
        "wind_kmh": current.get("wind_speed_10m"),
        "weather_code": current.get("weather_code"),
        "description": _weather_desc(current.get("weather_code")),
        "today_high_c": first("temperature_2m_max"),
        "today_low_c": first("temperature_2m_min"),
        "today_precip_probability_max_pct": first("precipitation_probability_max"),
    }


def _weather_reply(weather: dict[str, Any]) -> str:
    city = weather.get("city") or "當地"
    temp = weather.get("temperature_c")
    app = weather.get("apparent_c")
    high = weather.get("today_high_c")
    low = weather.get("today_low_c")
    pop = weather.get("today_precip_probability_max_pct")
    desc = weather.get("description") or "—"
    parts = [f"{city}現在 {desc}"]
    if temp is not None:
        parts.append(f"{temp}°C")
    if app is not None:
        parts.append(f"體感 {app}°C")
    line1 = "｜".join(parts)
    line2 = []
    if high is not None and low is not None:
        line2.append(f"今天約 {low}～{high}°C")
    if pop is not None:
        line2.append(f"最高降雨機率 {pop}%")
    return line1 + ("\n" + "｜".join(line2) if line2 else "") + "\n資料：Open-Meteo 即時/今日預報。"


def _general_fallback_reply(message: str, history: list[dict[str, str]]) -> str:
    msg = message.strip()
    if any(w in msg for w in PROFANITY_WORDS):
        return "有，我看得懂你是在罵我，不是在問持倉。你要罵可以；你要我改哪裡就直接講，我不會再硬塞交易分析。"
    if any(x in msg for x in ("你好", "嗨", "哈囉", "在嗎")):
        return "在。你可以正常跟我聊；問到期貨時我才會自動帶行情。"
    if "謝" in msg:
        return "不客氣。"
    if any(x in msg for x in ("你會什麼", "還會說別的", "只能問", "可以聊天")):
        return "可以聊天，不只期貨。現在沒接 OpenAI API，所以一般聊天能力是簡化版；交易判斷、台灣主要城市天氣、基本對話可以直接用。"
    return "可以聊。這句不是交易問題，所以我不會硬塞持倉分析。現在是簡化聊天模式；接上 OpenAI API 後才會變成完整自由對話。"



def _audit(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    try:
        logger.info("CHAT_AUDIT %s", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        logger.info("CHAT_AUDIT %s", {"event": event, "audit_error": True})

def _fallback_reply(message: str, market: dict[str, Any], position: dict[str, Any], history: list[dict[str, str]]) -> tuple[str, str, dict[str, Any] | None]:
    """Transparent non-AI fallback with intent routing. Never masquerades as OpenAI mode."""
    if _looks_weather(message, history):
        city = _find_city(message)
        if city is None:
            for item in reversed(history[-8:]):
                if item.get("role") == "user":
                    city = _find_city(item.get("content", ""))
                    if city:
                        break
        if city is None:
            return "你要查哪個城市的天氣？例如：高雄、台北、台中。", "weather_need_location", None
        try:
            weather = _fetch_weather(city)
            return _weather_reply(weather), "weather", weather
        except Exception as exc:
            return f"天氣資料現在抓不到（{type(exc).__name__}）。我不亂報數字，你可以稍後再問。", "weather_error", None

    if not _looks_trading(message):
        return _general_fallback_reply(message, history), "general", None

    a = market.get("analysis") if isinstance(market.get("analysis"), dict) else {}
    q = market.get("quote") if isinstance(market.get("quote"), dict) else {}
    decision = str(a.get("decision") or "資料不足")
    strength = str(a.get("strength") or "—")
    reason = str(a.get("reason") or "目前沒有完整分析理由。")
    price = q.get("last") or a.get("price") or "—"
    session = market.get("session_label") or market.get("session") or "—"
    closed = market.get("market_open") is False

    direction = position.get("direction") or "none"
    entry = position.get("entry_price")
    pos_line = ""
    if direction != "none":
        pos_line = f"\n持倉：{'多單' if direction == 'long' else '空單'}"
        if entry is not None:
            pos_line += f"｜成本 {entry}"

    state = f"目前：{decision}｜現價 {price}｜{session}"
    if closed:
        state += "｜市場休息，以下為最近有效盤回看"

    msg = message.strip()
    if any(k in msg for k in ("買", "多", "進場", "追")):
        action = f"🟡 結論：先依 APP 現有判定「{decision}」，簡化規則模式不會自行追加新的進場價。"
    elif any(k in msg for k in ("賣", "空", "出場", "跑", "停損")):
        action = f"🟡 結論：先依 APP 現有判定「{decision}」；若要精算出場，必須有有效失效位或你的成本。"
    elif any(k in msg for k in ("為什麼", "原因")):
        action = f"原因：{reason}"
    else:
        action = f"目前規則判定：{decision}。"

    reply = f"{action}\n{state}{pos_line}\n訊號：{strength}\n依據：{reason}"
    return reply, "trading", None

@router.get("/chat/status")
def futures_chat_status():
    has_key = bool(os.getenv("OPENAI_API_KEY"))
    return {
        "ok": True,
        "ai_connected": has_key,
        "mode": "openai" if has_key else "rules",
        "model": DEFAULT_MODEL if has_key else None,
        "decision_rules_version": DECISION_RULES_VERSION,
        "note": None if has_key else "OPENAI_API_KEY 尚未設定；目前使用一般聊天＋交易規則模式。",
    }

@router.post("/chat")
def futures_chat(req: FuturesChatRequest):
    started = time.perf_counter()
    request_id = uuid.uuid4().hex[:10]
    market = _clip(req.market)
    position = _clip(req.position.model_dump())
    history = [
        {"role": x.role, "content": x.content}
        for x in req.history[-16:]
    ]

    market_dict = market if isinstance(market, dict) else {}
    position_dict = position if isinstance(position, dict) else {}
    product = str(market_dict.get("product") or "—")
    session = str(market_dict.get("session") or "—")
    api_key = os.getenv("OPENAI_API_KEY")
    mode = "openai" if api_key else "rules"

    _audit(
        "received",
        request_id=request_id,
        product=product,
        session=session,
        mode=mode,
        message_len=len(req.message),
        message_preview=_preview(req.message),
        history_count=len(history),
        market_captured_at=market_dict.get("captured_at"),
        supplemental_age_ms=market_dict.get("supplemental_age_ms"),
    )

    if not api_key:
        reply, intent, general_context = _fallback_reply(req.message, market_dict, position_dict, history)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        _audit(
            "completed",
            request_id=request_id,
            product=product,
            session=session,
            ok=True,
            mode="rules",
            intent=intent,
            elapsed_ms=elapsed_ms,
            reply_len=len(reply),
            reply_preview=_preview(reply),
        )
        return {
            "ok": True,
            "mode": "rules",
            "intent": intent,
            "request_id": request_id,
            "elapsed_ms": elapsed_ms,
            "decision_rules_version": DECISION_RULES_VERSION,
            "reply": reply,
            "general_context": general_context,
        }

    general_context: dict[str, Any] = {}
    intent = "trading" if _looks_trading(req.message) else "general"
    if _looks_weather(req.message, history):
        intent = "weather"
        city = _find_city(req.message)
        if city is None:
            for item in reversed(history[-8:]):
                if item.get("role") == "user":
                    city = _find_city(item.get("content", ""))
                    if city:
                        break
        if city is not None:
            try:
                general_context["weather"] = _fetch_weather(city)
            except Exception as exc:
                general_context["weather_error"] = type(exc).__name__

    input_text = (
        "GENERAL_CONTEXT:\n"
        + json.dumps(general_context, ensure_ascii=False, separators=(",", ":"))
        + "\n\nMARKET_CONTEXT:\n"
        + json.dumps(market, ensure_ascii=False, separators=(",", ":"))
        + "\n\nPOSITION_CONTEXT:\n"
        + json.dumps(position, ensure_ascii=False, separators=(",", ":"))
        + "\n\nCHAT_HISTORY:\n"
        + json.dumps(history, ensure_ascii=False, separators=(",", ":"))
        + "\n\nUSER_MESSAGE:\n"
        + req.message
    )

    payload: dict[str, Any] = {
        "model": os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
        "instructions": SYSTEM_INSTRUCTIONS,
        "input": input_text,
        "reasoning": {"effort": os.getenv("OPENAI_REASONING_EFFORT", "medium")},
        "max_output_tokens": 1200,
        "store": False,
    }

    openai_started = time.perf_counter()
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            r = client.post(
                OPENAI_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        openai_ms = round((time.perf_counter() - openai_started) * 1000, 1)
        if r.status_code >= 400:
            detail = ""
            try:
                detail = (r.json().get("error") or {}).get("message") or ""
            except Exception:
                pass
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            _audit(
                "completed",
                request_id=request_id,
                product=product,
                session=session,
                ok=False,
                mode="openai",
                elapsed_ms=elapsed_ms,
                openai_ms=openai_ms,
                status_code=r.status_code,
            )
            return {
                "ok": False,
                "mode": "openai",
                "request_id": request_id,
                "elapsed_ms": elapsed_ms,
                "openai_ms": openai_ms,
                "error": f"OpenAI API HTTP {r.status_code}",
                "detail": detail[:700],
            }

        data = r.json()
        reply = _extract_output_text(data)
        if not reply:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            _audit(
                "completed",
                request_id=request_id,
                product=product,
                session=session,
                ok=False,
                mode="openai",
                elapsed_ms=elapsed_ms,
                openai_ms=openai_ms,
                error="no_output_text",
            )
            return {
                "ok": False,
                "mode": "openai",
                "request_id": request_id,
                "elapsed_ms": elapsed_ms,
                "openai_ms": openai_ms,
                "error": "OpenAI API 沒有回傳文字內容",
            }

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        _audit(
            "completed",
            request_id=request_id,
            product=product,
            session=session,
            ok=True,
            mode="openai",
            model=payload["model"],
            intent=intent,
            elapsed_ms=elapsed_ms,
            openai_ms=openai_ms,
            reply_len=len(reply),
            reply_preview=_preview(reply),
        )
        return {
            "ok": True,
            "mode": "openai",
            "model": payload["model"],
            "intent": intent,
            "request_id": request_id,
            "elapsed_ms": elapsed_ms,
            "openai_ms": openai_ms,
            "decision_rules_version": DECISION_RULES_VERSION,
            "reply": reply,
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        openai_ms = round((time.perf_counter() - openai_started) * 1000, 1)
        _audit(
            "completed",
            request_id=request_id,
            product=product,
            session=session,
            ok=False,
            mode="openai",
            elapsed_ms=elapsed_ms,
            openai_ms=openai_ms,
            error=type(exc).__name__,
        )
        return {
            "ok": False,
            "mode": "openai",
            "request_id": request_id,
            "elapsed_ms": elapsed_ms,
            "openai_ms": openai_ms,
            "error": f"{type(exc).__name__}: {exc}",
        }

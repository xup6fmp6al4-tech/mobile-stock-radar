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

DECISION_RULES_VERSION = "20260906-chat-fast-v3"
logger = logging.getLogger("futures_chat")

SYSTEM_INSTRUCTIONS = """你是「市場雷達」內建的期貨交易討論助手。
你不是單向報告機器，而是使用者的「判斷討論搭檔」：要能跟使用者來回辯證、指出漏看的風險、承認新證據會改變結論。

回覆語言：繁體中文、台灣用語。
風格：短、直接、可操作；不要教科書式長篇。

【資料真實性】
1. 只使用 MARKET_CONTEXT、POSITION_CONTEXT、CHAT_HISTORY 裡實際提供的資料。
2. 缺資料就寫「資料不足／—」，不得補假數字、猜五檔、猜法人、猜成交價。
3. 優先順序固定：期交所官方資料 > APP 依官方資料計算 > 其他參考資料。
4. 三竹、券商或其他畫面只能當交叉比對，不是最終真相來源。
5. HTTP 200 不等於資料正確；若 MARKET_CONTEXT diagnostics 顯示未通過，就明確說未通過。
6. 若 market_open=false，明確說是「最近有效盤／回看」，不可講成目前正在成交。

【討論方式】
7. 使用者問「現在買、賣、做多、做空、等、要不要跑」時：
   - 第一行先給結論：🟢買/做多、🔴賣/做空、🟡等待、⚪資料不足
   - 再給「我為什麼這樣判」
   - 再給「什麼條件出現我會改判」
   - 若已有持倉，再給「持倉處理」
8. 使用者反駁或提出另一個看法時，不要只是附和。先比較：
   - 使用者的理由
   - 目前資料支持/反對它的地方
   - 是否因此改判；若改判，要說哪個證據讓你改。
9. 明確區分「事實」與「推論」。
10. 不要把訊號百分比說成保證獲利率；那只是模型訊號強度。
11. 價位只有在資料裡已有觸發位、失效位、POC、明確高低點或使用者持倉成本時才能引用；不能自行捏造精確點位。
12. 若缺少會 materially 改變操作的重要資料，例如使用者已持倉但沒有成本，可直接說「我現在能判方向，但不能精算你的出場價」。

【交易邏輯】
13. 短線判斷優先看：
   - 現價/漲跌/高低/成交量
   - 1/3/5/15/30/60 分趨勢
   - 3/6/9 分動能
   - 量能倍率
   - 分價 POC
   - 三大法人
   - 五檔（只有 diagnostics 顯示完整時才使用）
   - 日盤/夜盤與資料新鮮度
14. 單一指標不能直接決定買賣；要看是否共振。
15. 追價要比等待更嚴格；若短週期動能轉弱，即使大方向偏多，也可以判「等回測/等突破確認」。
16. 每個可執行判斷盡量包含：觸發條件、失效條件、下一步觀察。
17. 有持倉時優先處理風險，再談加碼。
18. 不替使用者下單，也不聲稱已成交。

【這個專案的共同規則】
19. 期貨真實性基準是 TAIFEX，不以三竹作基準。
20. APP 的實盤資料目標是端到端延遲 <=5 秒；若 diagnostics/行情沒有足夠時間資訊，不得聲稱已達標。
21. 缺資料寧可顯示「—」，不可為了畫面完整而造假。
22. 使用者希望的是可以討論判斷，不只是固定按鈕答案，所以要保留上下文並能針對上一輪理由繼續辯證。
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



def _audit(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    try:
        logger.info("CHAT_AUDIT %s", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        logger.info("CHAT_AUDIT %s", {"event": event, "audit_error": True})

def _fallback_reply(message: str, market: dict[str, Any], position: dict[str, Any]) -> str:
    """Deterministic fallback. It must never masquerade as the AI discussion mode."""
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

    prefix = "【規則模式｜尚未連接 OpenAI API】\n"
    state = f"目前：{decision}｜現價 {price}｜{session}"
    if closed:
        state += "｜市場休息，以下為最近有效盤回看"

    msg = message.strip()
    if any(k in msg for k in ("買", "多", "進場", "追")):
        action = f"🟡 結論：先依 APP 現有判定「{decision}」，規則模式不會自行追加新的進場價。"
    elif any(k in msg for k in ("賣", "空", "出場", "跑", "停損")):
        action = f"🟡 結論：先依 APP 現有判定「{decision}」；若要精算出場，必須有有效失效位或你的成本。"
    elif any(k in msg for k in ("為什麼", "原因")):
        action = f"原因：{reason}"
    else:
        action = f"目前規則判定：{decision}。"

    return (
        f"{prefix}{action}\n{state}{pos_line}\n訊號：{strength}\n依據：{reason}\n\n"
        "真正的來回討論模式需要在 Render 設定 OPENAI_API_KEY。"
    )

@router.get("/chat/status")
def futures_chat_status():
    has_key = bool(os.getenv("OPENAI_API_KEY"))
    return {
        "ok": True,
        "ai_connected": has_key,
        "mode": "openai" if has_key else "rules",
        "model": DEFAULT_MODEL if has_key else None,
        "decision_rules_version": DECISION_RULES_VERSION,
        "note": None if has_key else "OPENAI_API_KEY 尚未設定；目前使用規則模式。",
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
        history_count=len(history),
        market_captured_at=market_dict.get("captured_at"),
        supplemental_age_ms=market_dict.get("supplemental_age_ms"),
    )

    if not api_key:
        reply = _fallback_reply(req.message, market_dict, position_dict)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        _audit(
            "completed",
            request_id=request_id,
            product=product,
            session=session,
            ok=True,
            mode="rules",
            elapsed_ms=elapsed_ms,
            reply_len=len(reply),
        )
        return {
            "ok": True,
            "mode": "rules",
            "request_id": request_id,
            "elapsed_ms": elapsed_ms,
            "decision_rules_version": DECISION_RULES_VERSION,
            "reply": reply,
        }

    input_text = (
        "MARKET_CONTEXT:\n"
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
            elapsed_ms=elapsed_ms,
            openai_ms=openai_ms,
            reply_len=len(reply),
        )
        return {
            "ok": True,
            "mode": "openai",
            "model": payload["model"],
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

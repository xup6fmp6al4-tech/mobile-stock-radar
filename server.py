from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse
import asyncio
import os
import re
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

WATCHLIST = {
    "6770.TW": {"name": "力積電", "kind": "stock", "url": "https://tw.stock.yahoo.com/quote/6770.TW"},
    "2609.TW": {"name": "陽明", "kind": "stock", "url": "https://tw.stock.yahoo.com/quote/2609.TW"},
    "WTX&": {"name": "台指期近一", "kind": "future", "url": "https://tw.stock.yahoo.com/future/WTX%26"},
}

_browser = None
_browser_lock = asyncio.Lock()
# Render Free 只有 512MB / 0.1 CPU，同時開多個 Chromium context 很容易不穩。
_scrape_sem = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_SCRAPES", "1")))
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
CACHE_SECONDS = int(os.getenv("QUOTE_CACHE_SECONDS", "10"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _browser
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    _browser = await playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    app.state.playwright = playwright
    try:
        yield
    finally:
        if _browser:
            await _browser.close()
        await playwright.stop()


app = FastAPI(title="Mobile Stock Radar", version="0.4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def clean(s: str) -> str:
    return re.sub(r"[ \t]+", " ", (s or "")).strip()


def parse_number(raw: str):
    if raw is None:
        return None
    raw = raw.replace(",", "").strip()
    if raw in {"", "-", "—", "--"}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def field_number(text: str, labels: List[str]):
    """讀 Yahoo 行情欄位；支援「開盤69.8」與「開盤\n69.8」兩種版面。"""
    n = r"([-+]?\d[\d,]*(?:\.\d+)?)"
    for lab in labels:
        # 先要求 label 後面直接接空白/冒號/換行，避免「成交」誤吃到「成交量」。
        patterns = [
            rf"(?:^|\n)\s*{re.escape(lab)}\s*[:：]?\s*{n}",
            rf"{re.escape(lab)}\s*[:：]\s*{n}",
        ]
        for pat in patterns:
            m = re.search(pat, text, flags=re.MULTILINE)
            if m:
                v = parse_number(m.group(1))
                if v is not None:
                    return v
    return None


def field_percent(text: str, labels: List[str]):
    for lab in labels:
        m = re.search(
            rf"(?:^|\n)\s*{re.escape(lab)}\s*[:：]?\s*([-+]?\d+(?:\.\d+)?)\s*%",
            text,
            flags=re.MULTILINE,
        )
        if m:
            return float(m.group(1))
    return None


def normalize_name(title: str, fallback: str = "") -> str:
    # 「力積電(6770.TW) 走勢圖 - Yahoo股市」→「力積電」
    if title:
        m = re.match(r"\s*([^\(\-]+?)\s*(?:\(|-|$)", title)
        if m:
            name = clean(m.group(1))
            if name and name != "Yahoo股市":
                return name
    return fallback


def validate_yahoo_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise HTTPException(400, "網址格式不正確") from exc
    if parsed.scheme != "https" or parsed.hostname != "tw.stock.yahoo.com":
        raise HTTPException(400, "目前只允許 https://tw.stock.yahoo.com/... 網址")
    # 首頁會出現大盤百分比，卻沒有單一商品欄位，v0.3 因此曾顯示誤導性的 +0.31%。
    if not (parsed.path.startswith("/quote/") or parsed.path.startswith("/future/")):
        raise HTTPException(400, "請使用 Yahoo 的單一股票/期貨行情頁，不要使用股市首頁")
    return url


async def get_browser():
    global _browser
    if _browser and _browser.is_connected():
        return _browser
    async with _browser_lock:
        if _browser and _browser.is_connected():
            return _browser
        playwright = app.state.playwright
        _browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        return _browser


async def scrape_yahoo(url: str, *, fallback_name: str = "", symbol: str = "", kind: str = "") -> Dict[str, Any]:
    url = validate_yahoo_url(url)
    now = time.monotonic()
    cached = _cache.get(url)
    if cached and now - cached[0] < CACHE_SECONDS:
        return {**cached[1], "cached": True}

    async with _scrape_sem:
        # 等 semaphore 時可能已有另一個請求剛更新快取。
        cached = _cache.get(url)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return {**cached[1], "cached": True}

        browser = await get_browser()
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="zh-TW",
            user_agent=(
                "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                "Chrome/126.0.0.0 Mobile Safari/537.36"
            ),
        )
        page = await context.new_page()

        async def block_heavy(route):
            if route.request.resource_type in {"image", "font", "media"}:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", block_heavy)
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            if resp and resp.status >= 400:
                raise HTTPException(502, f"Yahoo 回應 HTTP {resp.status}")
            # Yahoo 大多是 SSR；短暫等待讓個別動態區塊補齊。
            await page.wait_for_timeout(700)
            text = await page.locator("body").inner_text(timeout=12000)
            title = await page.title()
            try:
                h1 = clean(await page.locator("h1").first.inner_text(timeout=1500))
            except Exception:
                h1 = ""
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"Yahoo 行情頁讀取失敗：{type(exc).__name__}") from exc
        finally:
            await context.close()

    # 只使用有明確欄位名稱的數字，不再拿頁面上的「第一個百分比」猜漲跌幅。
    last = field_number(text, ["成交", "成交價"])
    open_ = field_number(text, ["開盤"])
    high = field_number(text, ["最高"])
    low = field_number(text, ["最低"])
    prev_close = field_number(text, ["昨收", "參考價"])
    volume = field_number(text, ["總量", "成交量"])
    bid = field_number(text, ["買價"])
    ask = field_number(text, ["賣價"])
    oi = field_number(text, ["未平倉"])
    change_pct = field_percent(text, ["漲跌幅", "漲幅"])
    change = field_number(text, ["漲跌"])

    # 某些 Yahoo 版型把昨收寫成參考價；期貨優先保留參考價。
    name = h1 or normalize_name(title, fallback_name) or fallback_name or symbol or "Yahoo行情"
    useful = sum(v is not None for v in [last, open_, high, low, prev_close, volume])
    if useful < 3:
        # 回傳明確錯誤，而不是顯示一堆「—」讓人誤以為有抓成功。
        raise HTTPException(502, f"Yahoo 頁面已開啟，但行情欄位解析不足（{useful}/6）")

    data = {
        "ok": True,
        "source": "Yahoo股市",
        "url": url,
        "symbol": symbol,
        "kind": kind,
        "name": name,
        "title": title,
        "last": last,
        "open": open_,
        "high": high,
        "low": low,
        "prev_close": prev_close,
        "change": change,
        "change_pct": change_pct,
        "volume": volume,
        "bid": bid,
        "ask": ask,
        "open_interest": oi,
        "ts_server": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }
    _cache[url] = (time.monotonic(), data)
    return data


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def sw():
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/watchlist")
async def get_watchlist():
    return WATCHLIST


@app.get("/api/quote")
async def quote(url: Optional[str] = None, symbol: Optional[str] = None):
    meta = None
    if symbol and symbol in WATCHLIST:
        meta = WATCHLIST[symbol]
        url = meta["url"]
    if not url:
        raise HTTPException(400, "請提供 url 或 symbol")
    return await scrape_yahoo(
        url,
        fallback_name=(meta or {}).get("name", ""),
        symbol=symbol or "",
        kind=(meta or {}).get("kind", ""),
    )


@app.get("/api/all")
async def all_quotes():
    # Render Free 版刻意依序抓，避免 Chromium 同時三開造成記憶體/CPU 爆掉。
    result = {}
    for sym, meta in WATCHLIST.items():
        try:
            data = await scrape_yahoo(
                meta["url"], fallback_name=meta["name"], symbol=sym, kind=meta["kind"]
            )
            result[sym] = {**meta, **data}
        except Exception as exc:
            detail = getattr(exc, "detail", str(exc))
            result[sym] = {**meta, "symbol": sym, "ok": False, "error": detail}
    return result


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "mobile-stock-radar",
        "version": "0.4.0",
        "browser": bool(_browser and _browser.is_connected()),
        "watchlist": list(WATCHLIST.keys()),
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8765"))
    uvicorn.run(app, host="0.0.0.0", port=port)

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse
import asyncio
import os
import re

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

WATCHLIST = {
    "6770.TW": {"name": "力積電", "url": "https://tw.stock.yahoo.com/quote/6770.TW"},
    "2609.TW": {"name": "陽明", "url": "https://tw.stock.yahoo.com/quote/2609.TW"},
    "WTX&": {"name": "台指期近一", "url": "https://tw.stock.yahoo.com/future/WTX%26"},
}

_browser = None
_browser_lock = asyncio.Lock()
_scrape_sem = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_SCRAPES", "3")))


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


app = FastAPI(title="Mobile Stock Radar", version="0.3.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).replace(",", "").strip()


def num_after(text: str, labels: List[str]):
    for lab in labels:
        m = re.search(rf"{re.escape(lab)}\s*[:：]?\s*([-+]?\d[\d,]*(?:\.\d+)?)", text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def pct_any(text: str):
    m = re.search(r"([-+]?\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None


def validate_yahoo_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise HTTPException(400, "網址格式不正確") from exc
    if parsed.scheme != "https" or parsed.hostname != "tw.stock.yahoo.com":
        raise HTTPException(400, "目前只允許 https://tw.stock.yahoo.com/... 網址")
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


async def scrape_yahoo(url: str) -> Dict[str, Any]:
    url = validate_yahoo_url(url)
    async with _scrape_sem:
        browser = await get_browser()
        context = await browser.new_context(
            viewport={"width": 1365, "height": 1100},
            locale="zh-TW",
            user_agent=(
                "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                "Chrome/126 Safari/537.36"
            ),
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1300)
            text = await page.locator("body").inner_text(timeout=10000)
            title = await page.title()
        except Exception as exc:
            raise HTTPException(502, f"Yahoo 行情頁讀取失敗：{type(exc).__name__}") from exc
        finally:
            await context.close()

    last = num_after(text, ["成交", "成交價"])
    open_ = num_after(text, ["開盤"])
    high = num_after(text, ["最高"])
    low = num_after(text, ["最低"])
    prev_close = num_after(text, ["昨收"])
    volume = num_after(text, ["總量", "成交量"])
    bid = num_after(text, ["買價"])
    ask = num_after(text, ["賣價"])
    oi = num_after(text, ["未平倉"])

    return {
        "ok": True,
        "source": "Yahoo股市",
        "url": url,
        "title": title,
        "last": last,
        "open": open_,
        "high": high,
        "low": low,
        "prev_close": prev_close,
        "change_pct": pct_any(text),
        "volume": volume,
        "bid": bid,
        "ask": ask,
        "open_interest": oi,
        "ts_server": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def sw():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/api/watchlist")
async def get_watchlist():
    return WATCHLIST


@app.get("/api/quote")
async def quote(url: Optional[str] = None, symbol: Optional[str] = None):
    if symbol and symbol in WATCHLIST:
        url = WATCHLIST[symbol]["url"]
    if not url:
        raise HTTPException(400, "請提供 url 或 symbol")
    return await scrape_yahoo(url)


@app.get("/api/all")
async def all_quotes():
    async def one(sym: str, meta: dict):
        try:
            data = await scrape_yahoo(meta["url"])
            return sym, {**meta, **data}
        except Exception as exc:
            detail = getattr(exc, "detail", str(exc))
            return sym, {**meta, "ok": False, "error": detail}

    rows = await asyncio.gather(*(one(sym, meta) for sym, meta in WATCHLIST.items()))
    return dict(rows)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "mobile-stock-radar",
        "version": "0.3.0",
        "browser": bool(_browser and _browser.is_connected()),
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8765"))
    uvicorn.run(app, host="0.0.0.0", port=port)

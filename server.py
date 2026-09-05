from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import asyncio
import json
import os
import re
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from futures_similarity import router as futures_router

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

WATCHLIST = {
    "6770.TW": {"name": "力積電", "kind": "stock"},
    "2609.TW": {"name": "陽明", "kind": "stock"},
    "8299.TWO": {"name": "群聯", "kind": "stock"},
    "2630.TW": {"name": "亞航", "kind": "stock"},
    "2634.TW": {"name": "漢翔", "kind": "stock"},
    "8105.TWO": {"name": "凌巨", "kind": "stock"},
    "3189.TWO": {"name": "景碩", "kind": "stock"},
    "2454.TW": {"name": "聯發科", "kind": "stock"},
    "2308.TW": {"name": "台達電", "kind": "stock"},
    "3034.TW": {"name": "聯詠", "kind": "stock"},
    "3035.TW": {"name": "智原", "kind": "stock"},
    "2409.TW": {"name": "友達", "kind": "stock"},
    "1605.TW": {"name": "華新", "kind": "stock"},
    "2492.TW": {"name": "華新科", "kind": "stock"},
    "WTX&": {"name": "台指期近一", "kind": "future"},
}
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
CACHE_SECONDS = int(os.getenv("QUOTE_CACHE_SECONDS", "15"))
HTTP_TIMEOUT = int(os.getenv("YAHOO_HTTP_TIMEOUT", "15"))
MAX_BYTES = int(os.getenv("YAHOO_MAX_BYTES", str(6 * 1024 * 1024)))
app = FastAPI(title="Mobile Stock Radar", version="0.5.0")
app.include_router(futures_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def clean(s: str) -> str:
    return re.sub(r"[ \t]+", " ", (s or "")).strip()


def parse_number(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    raw = str(raw).replace(",", "").replace("%", "").strip()
    if raw in {"", "-", "—", "--", "null", "None"}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def validate_yahoo_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise HTTPException(400, "網址格式不正確") from exc
    if parsed.scheme != "https" or parsed.hostname != "tw.stock.yahoo.com":
        raise HTTPException(400, "目前只允許 https://tw.stock.yahoo.com/... 網址")
    if not (parsed.path.startswith("/quote/") or parsed.path.startswith("/future/")):
        raise HTTPException(400, "請使用 Yahoo 的單一股票/期貨行情頁")
    return url


class VisibleTextParser(HTMLParser):
    BLOCKS = {
        "p", "div", "li", "tr", "td", "th", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "br", "dt", "dd", "button"
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
            return
        if not self.skip and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            if self.skip:
                self.skip -= 1
            return
        if not self.skip and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip and data:
            self.parts.append(data)

    def text(self) -> str:
        s = unescape("".join(self.parts)).replace("\r", "\n")
        s = re.sub(r"[ \t\u00a0]+", " ", s)
        s = re.sub(r"\n[ \t]+", "\n", s)
        s = re.sub(r"\n{2,}", "\n", s)
        return s.strip()


def visible_text(html: str) -> str:
    parser = VisibleTextParser()
    try:
        parser.feed(html)
        return parser.text()
    except Exception:
        # 即使 HTML 有瑕疵，也保留一個超輕量 fallback。
        s = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html)
        s = re.sub(r"(?i)<br\s*/?>|</(?:div|p|li|tr|td|th|h\d|section)>", "\n", s)
        s = re.sub(r"(?s)<[^>]+>", " ", s)
        return unescape(s)


def field_number(text: str, labels: List[str]):
    n = r"([-+]?\d[\d,]*(?:\.\d+)?)"
    for lab in labels:
        patterns = [
            rf"(?:^|\n)\s*{re.escape(lab)}\s*[:：]?\s*{n}",
            rf"{re.escape(lab)}\s*[:：]\s*{n}",
            rf"{re.escape(lab)}\s+{n}",
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


def jsonish_value(blob: str, keys: List[str]):
    """從 Yahoo SSR 內嵌資料抓數字；相容 raw 巢狀值與舊式直接值。"""
    for key in keys:
        ek = re.escape(key)
        pats = [
            rf'"{ek}"\s*:\s*\{{[^{{}}]{{0,450}}?"raw"\s*:\s*(?:"([-+]?\d[\d,]*(?:\.\d+)?)"|([-+]?\d[\d,]*(?:\.\d+)?))',
            rf'"{ek}"\s*:\s*"([-+]?\d[\d,]*(?:\.\d+)?)"',
            rf'"{ek}"\s*:\s*([-+]?\d[\d,]*(?:\.\d+)?)',
        ]
        for pat in pats:
            m = re.search(pat, blob)
            if not m:
                continue
            vals = [g for g in m.groups() if g is not None]
            if vals:
                v = parse_number(vals[0])
                if v is not None:
                    return v
    return None


def jsonish_string(blob: str, keys: List[str]):
    for key in keys:
        m = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', blob)
        if m:
            try:
                return json.loads('"' + m.group(1) + '"')
            except Exception:
                return unescape(m.group(1))
    return None


def target_window(html: str, symbol: str) -> str:
    variants = [symbol, symbol.replace("&", r"\u0026"), symbol.replace("&", "%26")]
    hits = []
    for v in variants:
        start = 0
        while True:
            i = html.find(v, start)
            if i < 0:
                break
            a, b = max(0, i - 12000), min(len(html), i + 18000)
            chunk = html[a:b]
            score = sum(k in chunk for k in [
                '"price"', '"regularMarketOpen"', '"regularMarketDayHigh"',
                '"regularMarketDayLow"', '"regularMarketPreviousClose"', '"changePercent"'
            ])
            hits.append((score, chunk))
            start = i + max(1, len(v))
    if hits:
        hits.sort(key=lambda x: x[0], reverse=True)
        return hits[0][1]
    return html[:250000]


def normalize_name_from_title(html: str, fallback: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if not m:
        return fallback
    title = clean(unescape(re.sub(r"<[^>]+>", "", m.group(1))))
    mm = re.match(r"\s*([^\(\-]+?)\s*(?:\(|-|$)", title)
    if mm:
        name = clean(mm.group(1))
        if name and name != "Yahoo股市":
            return name
    return fallback


def http_get_text(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh-Hant;q=0.9,en;q=0.5",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise HTTPException(502, "Yahoo 回應過大，已中止")
            charset = resp.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
            if status >= 400:
                raise HTTPException(502, f"Yahoo 回應 HTTP {status}")
            return text
    except HTTPError as exc:
        raise HTTPException(502, f"Yahoo 回應 HTTP {exc.code}") from exc
    except URLError as exc:
        raise HTTPException(502, f"Yahoo 連線失敗：{getattr(exc, 'reason', 'URLError')}") from exc
    except TimeoutError as exc:
        raise HTTPException(504, "Yahoo 連線逾時") from exc


def parse_quote_html(html: str, *, symbol: str, fallback_name: str, kind: str, url: str) -> Dict[str, Any]:
    text = visible_text(html)
    win = target_window(html, symbol)

    # 先讀畫面欄位；Yahoo 的 SSR 頁目前會把這些行情文字直接放進 HTML。
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

    # 若版型把數字只留在 SSR JSON，改由內嵌資料補齊。
    last = last if last is not None else jsonish_value(win, ["price", "regularMarketPrice"])
    open_ = open_ if open_ is not None else jsonish_value(win, ["regularMarketOpen"])
    high = high if high is not None else jsonish_value(win, ["regularMarketDayHigh"])
    low = low if low is not None else jsonish_value(win, ["regularMarketDayLow"])
    prev_close = prev_close if prev_close is not None else jsonish_value(win, ["regularMarketPreviousClose", "previousClose"])
    volume = volume if volume is not None else jsonish_value(win, ["volume", "volumeK", "regularMarketVolume"])
    bid = bid if bid is not None else jsonish_value(win, ["bid"])
    ask = ask if ask is not None else jsonish_value(win, ["ask"])
    oi = oi if oi is not None else jsonish_value(win, ["openInterest", "open_interest"])
    change = change if change is not None else jsonish_value(win, ["change", "regularMarketChange"])
    if change_pct is None:
        cp = jsonish_value(win, ["changePercent", "regularMarketChangePercent"])
        if cp is not None:
            # Yahoo 內嵌 JSON 常用 0.0057 表示 0.57%。
            change_pct = cp * 100 if abs(cp) <= 1 else cp

    # v0.6：若成交價與昨收都存在，直接用兩者重算漲跌與漲跌幅。
    # Yahoo 頁面同時包含許多百分比，單靠文字欄位可能誤抓到別的 +0.xx%。
    # 以 last/prev_close 重算可保證方向與顯示價格自洽。
    if last is not None and prev_close not in (None, 0):
        change = last - prev_close
        change_pct = (change / prev_close) * 100

    name = jsonish_string(win, ["symbolName", "shortName", "longName"]) or normalize_name_from_title(html, fallback_name)

    useful = sum(v is not None for v in [last, open_, high, low, prev_close, volume])
    if useful < 3:
        # 不再啟動 Chromium；免費 512MB 方案若開瀏覽器容易被 OOM kill，前端只會看到 Failed to fetch。
        raise HTTPException(502, f"Yahoo 頁面已取得，但行情欄位解析不足（{useful}/6）")

    return {
        "ok": True,
        "source": "Yahoo股市",
        "url": url,
        "symbol": symbol,
        "kind": kind,
        "name": name or fallback_name or symbol,
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
        "transport": "http",
    }


async def scrape_yahoo(url: str, *, fallback_name: str = "", symbol: str = "", kind: str = "") -> Dict[str, Any]:
    url = validate_yahoo_url(url)
    cached = _cache.get(url)
    now = time.monotonic()
    if cached and now - cached[0] < CACHE_SECONDS:
        return {**cached[1], "cached": True}

    html = await asyncio.to_thread(http_get_text, url)
    data = await asyncio.to_thread(
        parse_quote_html, html, symbol=symbol, fallback_name=fallback_name, kind=kind, url=url
    )
    _cache[url] = (time.monotonic(), data)
    return data


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


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
    meta = WATCHLIST.get(symbol) if symbol else None
    if meta:
        url = meta.get("url") or f"https://tw.stock.yahoo.com/quote/{(symbol or '').replace('&', '%26')}"
    if not url:
        raise HTTPException(400, "請提供 url 或 symbol")
    return await scrape_yahoo(
        url,
        fallback_name=(meta or {}).get("name", ""),
        symbol=symbol or "",
        kind=(meta or {}).get("kind", ""),
    )


@app.get("/api/radar")
@app.get("/api/all")
async def all_quotes():
    async def one(sym: str, meta: Dict[str, str]):
        try:
            d = await scrape_yahoo(meta.get("url") or f"https://tw.stock.yahoo.com/quote/{sym.replace('&', '%26')}", fallback_name=meta["name"], symbol=sym, kind=meta["kind"])
            return sym, {**meta, **d}
        except Exception as exc:
            return sym, {**meta, "symbol": sym, "ok": False, "error": getattr(exc, "detail", str(exc))}

    # v0.6 不開 Chromium，3 個純 HTTP 請求可同時進行，通常數秒內完成。
    pairs = await asyncio.gather(*(one(sym, meta) for sym, meta in WATCHLIST.items()))
    return dict(pairs)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "mobile-stock-radar",
        "version": "0.6.0",
        "transport": "http-no-browser",
        "watchlist": list(WATCHLIST.keys()),
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8765"))
    uvicorn.run(app, host="0.0.0.0", port=port)

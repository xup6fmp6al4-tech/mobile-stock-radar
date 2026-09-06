from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from futures_1m_archive import router as one_minute_router
from futures_profile import router as profile_router
from taifex_overlay import router as taifex_router
from futures_full_data import router as full_data_router
from server import app as legacy_app, STATIC_DIR

app = FastAPI(title="Mobile Stock Radar - TAIFEX Full Data")

# 路由優先順序：
# 1) 真實1分K/官方逐筆回填
# 2) 分價量與完整度診斷
# 3) 既有即時行情、五檔、法人、保證金、P/C
app.include_router(one_minute_router)
app.include_router(profile_router)
app.include_router(full_data_router)
app.include_router(taifex_router)

PATCHES = [
    "/static/futures-display-patch.js?v=20260906-official-1m-3",
    "/static/futures-complete-patch.js?v=20260906-complete-1",
]


@app.get("/", include_in_schema=False)
async def patched_root():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    tags = []
    for src in PATCHES:
        basename = src.split("?")[0].rsplit("/", 1)[-1]
        if basename not in html:
            tags.append(f'<script src="{src}"></script>')
    if tags:
        html = html.replace("</body>", "".join(tags) + "</body>")
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


app.mount("/", legacy_app)

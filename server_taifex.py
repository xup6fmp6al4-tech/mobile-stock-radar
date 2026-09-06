from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from taifex_overlay import router as taifex_router
from futures_full_data import router as full_data_router
from server import app as legacy_app, STATIC_DIR

# 公開即時行情／五檔／法人／市場結構資料由 full_data_router 補齊；
# 原有 TAIFEX OpenAPI + Yahoo fallback/K線/黑盒資料維持不變。
app = FastAPI(title="Mobile Stock Radar - TAIFEX Full Data")
app.include_router(full_data_router)
app.include_router(taifex_router)

# 直接把最新版期貨補丁寫進首頁 HTML 回應，不再依賴 Service Worker 才載入。
# 這可避免手機仍吃到舊 SW / 舊 JS 時，1分與5分新資料管線根本沒有執行。
PATCH_SRC = "/static/futures-display-patch.js?v=20260906-intraday-direct-2"


@app.get("/", include_in_schema=False)
async def patched_root():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    if "futures-display-patch.js" not in html:
        tag = f'<script src="{PATCH_SRC}"></script>'
        html = html.replace("</body>", f"{tag}</body>")
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# 其餘 legacy 路由與 /static 全部維持原本行為。
app.mount("/", legacy_app)

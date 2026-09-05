from fastapi import FastAPI

from taifex_overlay import router as taifex_router
from server import app as legacy_app

# 外層只攔截期貨資料 API：TAIFEX OpenAPI 優先，Yahoo 只做備援。
# 其他既有頁面、靜態檔與股票功能全部交回原 server，避免破壞已完成 UI。
app = FastAPI(title="Mobile Stock Radar - TAIFEX Primary")
app.include_router(taifex_router)
app.mount("/", legacy_app)

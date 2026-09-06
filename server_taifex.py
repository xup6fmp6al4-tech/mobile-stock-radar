from fastapi import FastAPI

from taifex_overlay import router as taifex_router
from futures_full_data import router as full_data_router
from server import app as legacy_app

# 公開即時行情／五檔／法人／市場結構資料由 full_data_router 補齊；
# 原有 TAIFEX OpenAPI + Yahoo fallback/K線/黑盒資料維持不變。
app = FastAPI(title="Mobile Stock Radar - TAIFEX Full Data")
app.include_router(full_data_router)
app.include_router(taifex_router)
app.mount("/", legacy_app)

from __future__ import annotations
import os

def parse_list(name, default):
    raw=os.getenv(name,"").strip()
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else default

DEFAULT_CORE=[
    "^TWII","2330.TW","6770.TW","2609.TW","8299.TWO","00631L.TW","00632R.TW",
    "3481.TW","2303.TW","NVDA","MU","TSLA","QQQ","^IXIC","^SOX"
]
DEFAULT_WATCHLIST=[
    "^TWII","2330.TW","2303.TW","6770.TW","2609.TW","8299.TWO","2630.TW","2634.TW",
    "8105.TW","3189.TW","2454.TW","2308.TW","3034.TW","3035.TW","2409.TW","3481.TW",
    "1605.TW","2492.TW","00631L.TW","00685L.TW","00675L.TW","00715L.TW","006201.TW",
    "006208.TW","00632R.TW","NVDA","MU","TSLA","QQQ","MRNA","^IXIC","^SOX","GC=F","CL=F","DX-Y.NYB"
]
CORE_SYMBOLS=parse_list("CORE_SYMBOLS",DEFAULT_CORE)
WATCHLIST_SYMBOLS=list(dict.fromkeys(parse_list("WATCHLIST_SYMBOLS",DEFAULT_WATCHLIST)+CORE_SYMBOLS))

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from futures_1m_archive import router as one_minute_router
from futures_profile import router as profile_router
from taifex_overlay import router as taifex_router
from futures_full_data import router as full_data_router
from server import app as legacy_app, STATIC_DIR

app = FastAPI(title="Mobile Stock Radar - TAIFEX Full Data")

app.include_router(one_minute_router)
app.include_router(profile_router)
app.include_router(full_data_router)
app.include_router(taifex_router)

PATCHES = [
    "/static/futures-display-patch.js?v=20260906-official-1m-3",
    "/static/futures-complete-patch.js?v=20260906-complete-3",
    "/static/futures-fast-analysis-patch.js?v=20260907-fast-analysis-1",
]

INLINE_PV_FIX = r"""
<script>
(() => {
'use strict';
const q=s=>document.querySelector(s);
const num=v=>{const x=Number(v);return Number.isFinite(x)?x:null};
const fmt2=(v,d=0)=>num(v)==null?'—':Number(v).toLocaleString('zh-TW',{maximumFractionDigits:d});
let pvBusy=false;

async function forcePriceVolume(){
  const targets=[q('#price-detail'),q('#trend-price-panel')].filter(Boolean);
  if(!targets.length || pvBusy || typeof PRODUCTS==='undefined') return;
  const m=PRODUCTS[current];
  if(!m?.product)return;

  pvBusy=true;
  targets.forEach(t=>{
    if(!t.dataset.pvReady){
      t.innerHTML='<div class="pv-source">分價量讀取中…</div>';
    }
  });

  try{
    const td=(typeof oneMinTradingDate!=='undefined' && oneMinTradingDate)
      ? `&trading_date=${encodeURIComponent(oneMinTradingDate)}`
      : '';

    const j=await jsonFetch(
      `/api/blackbox/futures/price-volume?product=${encodeURIComponent(m.product)}&session=${session}${td}&limit=120&v=20260906-pv-force-3`
    );

    const rows=(j?.rows||[]).filter(
      x=>num(x.price)!=null && num(x.volume)!=null && Number(x.volume)>0
    );

    if(!rows.length) throw new Error(j?.error||'no price-volume rows');

    const mx=Math.max(1,...rows.map(x=>Number(x.volume)));
    const exact=!!j.exact;

    const summary=
      `<div class="profile-summary">
        <div>價位數<b>${rows.length}</b></div>
        <div>總量<b>${fmt2(j.total_volume,0)}</b></div>
        <div>最大量價<b>${fmt2(j.poc_price,0)}</b></div>
      </div>`;

    const note=
      `<div class="${exact?'profile-exact':'profile-est'}">
        ${exact?'✓ 期交所官方逐筆精確分價':'⚠ 1分K估算分價'}｜
        ${j.trading_date||'—'} ${j.session==='night'?'夜盤':'日盤'}
      </div>`;

    const list=rows.map(x=>
      `<div class="pv-row ${Number(x.price)===Number(j.poc_price)?'poc':''}">
        <span class="pv-price">${fmt2(x.price,0)}</span>
        <span class="pv-bar"><i style="width:${Math.max(2,Number(x.volume)/mx*100)}%"></i></span>
        <span class="pv-qty">${fmt2(x.volume,0)}</span>
      </div>`
    ).join('');

    targets.forEach(t=>{
      t.dataset.pvReady='1';
      t.dataset.pvSource=exact?'official-tick':'1m-estimate';
      t.innerHTML=summary+note+list;
    });

  }catch(e){
    targets.forEach(t=>{
      if(!t.dataset.pvReady){
        t.innerHTML=
          '<div class="empty-state"><strong>分價量尚未取得</strong><span>資料源失敗，不補假數字。</span></div>';
      }
    });
    console.error('price-volume',e);
  }finally{
    pvBusy=false;
  }
}

setTimeout(forcePriceVolume,1500);

document.addEventListener('click',e=>{
  if(e.target.closest(
    '.detail-subtab,.trend-lower-tab,.primary-tab,#sessionDay,#sessionNight,#prevContract,#nextContract,#contractMenu button'
  )){
    setTimeout(forcePriceVolume,300);
  }
});

setInterval(()=>{
  if(!document.hidden)forcePriceVolume();
},60000);
})();
</script>
"""


@app.get("/", include_in_schema=False)
async def patched_root():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    tags = []
    for src in PATCHES:
        if src not in html:
            tags.append(f'<script src="{src}"></script>')

    inject = "".join(tags) + INLINE_PV_FIX
    html = html.replace("</body>", inject + "</body>")

    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


app.mount("/", legacy_app)

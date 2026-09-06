(() => {
'use strict';

const VER='20260907-acceptance-1';
let busy=false;
let lastKey='';
let timer=null;

function q(s){return document.querySelector(s)}
function fmtMs(v){
  const n=Number(v);
  if(!Number.isFinite(n))return '—';
  return n<1000?`${Math.round(n)}ms`:`${(n/1000).toFixed(1)}s`;
}
function ensureBox(){
  let b=q('#acceptanceStrip');
  if(b)return b;
  const anchor=q('.freshness-strip') || q('.page-actions');
  if(!anchor)return null;
  b=document.createElement('div');
  b.id='acceptanceStrip';
  b.style.cssText='display:grid;grid-template-columns:repeat(4,1fr);gap:3px;padding:5px 7px;background:#071014;border-bottom:1px solid #26343a;font-size:11px;text-align:center';
  anchor.insertAdjacentElement('afterend',b);
  return b;
}
function cell(k,v,state){
  const c=state==='ok'?'#7ee2a8':state==='bad'?'#ff6b6b':'#fbbf24';
  return `<div style="border:1px solid #26343a;padding:5px 2px;color:#9ba6aa">${k}<b style="display:block;color:${c};font-size:12px;margin-top:2px">${v}</b></div>`;
}
async function audit(){
  if(busy || document.hidden || typeof PRODUCTS==='undefined')return;
  const b=ensureBox(); if(!b)return;
  const m=PRODUCTS[current]; if(!m?.product)return;
  const key=`${m.product}:${session}`;
  busy=true;
  try{
    const t0=performance.now();
    const r=await fetch(`/api/blackbox/futures/acceptance?product=${encodeURIComponent(m.product)}&session=${session}&v=${VER}`,{cache:'no-store'});
    const j=await r.json();
    const roundtrip=performance.now()-t0;

    let e2e=null;
    const iso=j?.latency?.quote_time_iso;
    if(iso){
      const ts=Date.parse(iso);
      if(Number.isFinite(ts))e2e=Math.max(0,Date.now()-ts);
    }

    const five=j?.five_level||{};
    const lat=j?.latency||{};
    const roll=j?.rollover||{};
    const actual=j?.quote?.session_actual;
    const closed=five.state==='deferred_market_closed';

    let fiveV=closed?'待開盤':five.live_ok?'5/5 ✓':`${five.count||0}/5`;
    let fiveS=closed?'wait':five.live_ok?'ok':'bad';

    let latV='待開盤',latS='wait';
    if(lat.state!=='deferred_market_closed'){
      const total=e2e!=null?e2e:(Number(lat.server_quote_age_ms)||null);
      if(total!=null){
        latV=fmtMs(total);
        latS=total<=5000?'ok':'bad';
      }else{
        latV=fmtMs(roundtrip);
        latS='wait';
      }
    }

    const active=roll.selected_active?.contract_month;
    const rollV=active?String(active):'—';
    const rollS=roll.ok?'ok':'bad';

    let sessV=session==='night'?'夜盤':'日盤';
    let sessS=(actual===session&&!j?.quote?.session_fallback)?'ok':'wait';
    if(closed){sessV='市場休息';sessS='wait'}

    b.innerHTML=
      cell('五檔',fiveV,fiveS)+
      cell('總延遲',latV,latS)+
      cell('近月',rollV,rollS)+
      cell('盤別',sessV,sessS);

    b.title=`API往返 ${fmtMs(roundtrip)}｜來源 ${j?.quote?.source||'—'}｜${j?.large_trader?.note||j?.large_trader?.reason||''}`;
    lastKey=key;
  }catch(e){
    b.innerHTML=cell('驗證','讀取失敗','bad')+cell('五檔','—','wait')+cell('延遲','—','wait')+cell('換月','—','wait');
  }finally{
    busy=false;
  }
}

document.addEventListener('click',e=>{
  if(e.target.closest('#sessionDay,#sessionNight,#prevContract,#nextContract,#contractMenu button')){
    setTimeout(audit,350);
  }
});

setTimeout(audit,1200);
timer=setInterval(audit,5000);
})();
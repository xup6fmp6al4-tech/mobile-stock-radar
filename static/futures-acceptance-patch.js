(() => {
'use strict';

const VER='20260907-acceptance-2';
let busy=false;
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
  }catch(e){
    b.innerHTML=cell('驗證','讀取失敗','bad')+cell('五檔','—','wait')+cell('延遲','—','wait')+cell('換月','—','wait');
  }finally{
    busy=false;
  }
}

// ----- ChatGPT 手動分析匯入：APP只顯示，不自行計算或改寫 -----
const GPT_VER='GPT1';

function esc(s){
  return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function productCode(){
  const m=(typeof PRODUCTS!=='undefined'&&PRODUCTS[current])||{};
  return String(m.code||m.contract||current||'').toUpperCase();
}
function gptKey(product=productCode(),sess=session){
  return `chatgpt-analysis:${String(product).toUpperCase()}:${sess||'day'}`;
}
function readGPT(product=productCode(),sess=session){
  try{return JSON.parse(localStorage.getItem(gptKey(product,sess))||'null')}catch(e){return null}
}
function writeGPT(d){
  localStorage.setItem(gptKey(d.product,d.session),JSON.stringify(d));
}
function actionText(v){
  const x=String(v||'').toUpperCase();
  if(x==='BUY'||x==='LONG')return '買進／做多';
  if(x==='SELL'||x==='SHORT')return '賣出／做空';
  if(x==='HOLD')return '續抱';
  return '等待';
}
function actionColor(v){
  const x=String(v||'').toUpperCase();
  if(x==='BUY'||x==='LONG')return '#ff6b6b';
  if(x==='SELL'||x==='SHORT')return '#7ee2a8';
  return '#fbbf24';
}
function formatTime(v){
  const t=Date.parse(v||'');
  if(!Number.isFinite(t))return v||'—';
  return new Date(t).toLocaleString('zh-TW',{timeZone:'Asia/Taipei',hour12:false});
}
function isExpired(d){
  const t=Date.parse(d?.expires_at||'');
  return Number.isFinite(t)&&Date.now()>t;
}
function parseGPTCode(raw){
  const s=String(raw||'').trim();
  if(!s)throw new Error('沒有貼入分析碼');
  let o={};
  if(s.startsWith('{')){
    o=JSON.parse(s);
  }else{
    const parts=s.split(';').map(x=>x.trim()).filter(Boolean);
    if(!parts.length||parts[0].toUpperCase()!==GPT_VER)throw new Error('分析碼需以 GPT1 開頭');
    for(const p of parts.slice(1)){
      const i=p.indexOf('=');
      if(i<1)continue;
      o[p.slice(0,i).trim().toUpperCase()]=p.slice(i+1).trim();
    }
  }
  const now=new Date();
  const at=o.AT||o.at||o.analyzed_at||now.toISOString();
  const ttlRaw=o.TTL??o.ttl_minutes??30;
  const ttl=Math.max(1,Number(ttlRaw)||30);
  const atMs=Date.parse(at);
  const expires=o.EXPIRES||o.expires_at||(Number.isFinite(atMs)?new Date(atMs+ttl*60000).toISOString():new Date(Date.now()+ttl*60000).toISOString());
  const data={
    version:GPT_VER,
    source:'ChatGPT 手動匯入',
    product:String(o.PRODUCT||o.product||productCode()).toUpperCase(),
    session:String(o.SESSION||o.session||session||'day').toLowerCase()==='night'?'night':'day',
    action:String(o.ACTION||o.action||'WAIT').toUpperCase(),
    entry:o.ENTRY||o.entry||'—',
    trigger:o.TRIGGER||o.trigger||'—',
    confirm:o.CONFIRM||o.confirm||'—',
    invalid:o.INVALID||o.invalid||'—',
    stop:o.STOP||o.stop||'—',
    t1:o.T1||o.target1||'—',
    t2:o.T2||o.target2||'—',
    note:o.NOTE||o.note||'',
    analyzed_at:at,
    expires_at:expires,
    imported_at:now.toISOString(),
    ttl_minutes:ttl
  };
  if(!['TX','MTX','TMF'].includes(data.product))throw new Error('PRODUCT 只接受 TX / MTX / TMF');
  return data;
}
function ensureGPTStyles(){
  if(q('#gptAnalysisStyles'))return;
  const s=document.createElement('style');
  s.id='gptAnalysisStyles';
  s.textContent=`
  .gpt-analysis-card{margin:8px 8px 0;border:1px solid #2c88ad;border-radius:9px;background:#071014;overflow:hidden}
  .gpt-analysis-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 11px;background:#0b2430;border-bottom:1px solid #254957}
  .gpt-analysis-title{font-weight:900;color:#fff;font-size:15px}.gpt-analysis-sub{font-size:11px;color:#79d9ff;margin-top:2px}
  .gpt-analysis-actions{display:flex;gap:6px}.gpt-analysis-actions button{border:1px solid #2c88ad;background:#123a4d;color:#fff;border-radius:6px;padding:6px 9px;font-size:12px}
  .gpt-analysis-body{padding:10px}.gpt-analysis-empty{color:#9ba6aa;font-size:13px;padding:6px 2px}
  .gpt-analysis-status{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;padding:8px;border:1px solid #31414a;background:#0c1418;border-radius:7px}
  .gpt-analysis-status strong{font-size:18px}.gpt-expired{color:#ff6b6b;font-weight:900;font-size:12px}.gpt-valid{color:#7ee2a8;font-weight:900;font-size:12px}
  .gpt-analysis-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}.gpt-kv{padding:8px;border:1px solid #24343b;border-radius:6px;background:#0b1114}.gpt-kv .k{color:#8fa0a8;font-size:11px}.gpt-kv .v{color:#fff;font-weight:800;font-size:14px;margin-top:3px;word-break:break-word}
  .gpt-wide{grid-column:1/-1}.gpt-analysis-foot{margin-top:8px;color:#9fd8ef;font-size:11px;line-height:1.45}.gpt-analysis-note{margin-top:7px;padding:7px;border:1px solid #5b4a12;background:#211a05;color:#f5d76e;border-radius:6px;font-size:12px}
  .gpt-import-overlay{position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.78);display:none;align-items:center;justify-content:center;padding:14px}.gpt-import-overlay.show{display:flex}
  .gpt-import-box{width:min(620px,96vw);background:#0a1115;border:1px solid #19b9f4;border-radius:10px;padding:12px;color:#fff}.gpt-import-box h3{margin:0 0 6px}.gpt-import-box p{margin:0 0 9px;color:#9fd8ef;font-size:12px;line-height:1.45}
  .gpt-import-box textarea{width:100%;height:180px;background:#020506;color:#fff;border:1px solid #40515a;border-radius:6px;padding:9px;font:12px ui-monospace,Consolas,monospace;resize:vertical}.gpt-import-btns{display:flex;justify-content:flex-end;gap:8px;margin-top:9px}.gpt-import-btns button{padding:8px 14px;border-radius:6px;border:1px solid #2c88ad;background:#123a4d;color:#fff}.gpt-import-error{min-height:18px;color:#ff6b6b;font-size:12px;margin-top:5px}
  @media(max-width:520px){.gpt-analysis-grid{grid-template-columns:1fr 1fr}.gpt-analysis-head{align-items:flex-start}.gpt-analysis-actions{flex-direction:column}.gpt-analysis-actions button{padding:5px 7px}}
  `;
  document.head.appendChild(s);
}
function gptHTML(d,compact=false){
  if(!d)return `<div class="gpt-analysis-empty">尚未匯入 ChatGPT 分析。這一區不會由 APP 自己產生判斷。</div>`;
  const expired=isExpired(d);
  const color=actionColor(d.action);
  const status=expired?'<span class="gpt-expired">⚠ 已過期，不可當即時進場依據</span>':'<span class="gpt-valid">● 有效分析</span>';
  const extra=compact?'':`
    <div class="gpt-kv"><div class="k">停損</div><div class="v">${esc(d.stop)}</div></div>
    <div class="gpt-kv"><div class="k">目標 1 / 2</div><div class="v">${esc(d.t1)} / ${esc(d.t2)}</div></div>`;
  return `
    <div class="gpt-analysis-status"><div><div style="font-size:11px;color:#8fa0a8">ChatGPT 現在動作</div><strong style="color:${color}">${esc(actionText(d.action))}</strong></div>${status}</div>
    <div class="gpt-analysis-grid">
      <div class="gpt-kv"><div class="k">建議進場</div><div class="v">${esc(d.entry)}</div></div>
      <div class="gpt-kv"><div class="k">觸發價</div><div class="v">${esc(d.trigger)}</div></div>
      <div class="gpt-kv gpt-wide"><div class="k">確認條件</div><div class="v">${esc(d.confirm)}</div></div>
      <div class="gpt-kv gpt-wide"><div class="k">取消／假突破條件</div><div class="v">${esc(d.invalid)}</div></div>
      ${extra}
      <div class="gpt-kv gpt-wide"><div class="k">分析時間</div><div class="v">${esc(formatTime(d.analyzed_at))}｜有效 ${esc(d.ttl_minutes)} 分鐘</div></div>
    </div>
    ${d.note?`<div class="gpt-analysis-note">${esc(d.note)}</div>`:''}
    <div class="gpt-analysis-foot">來源：ChatGPT 手動匯入。APP 只保存與顯示，不會自行改寫這些進場條件。</div>`;
}
function ensureGPTCard(){
  ensureGPTStyles();
  let c=q('#gptAnalysisCard');
  if(!c){
    c=document.createElement('section');
    c.id='gptAnalysisCard';
    c.className='gpt-analysis-card';
    const a=ensureBox()||q('.freshness-strip')||q('.page-actions');
    if(!a)return null;
    a.insertAdjacentElement('afterend',c);
  }
  renderGPTCard();
  return c;
}
function renderGPTCard(){
  const c=q('#gptAnalysisCard'); if(!c)return;
  const d=readGPT();
  c.innerHTML=`
    <div class="gpt-analysis-head">
      <div><div class="gpt-analysis-title">ChatGPT 分析</div><div class="gpt-analysis-sub">手動匯入｜不是 APP 程式分析</div></div>
      <div class="gpt-analysis-actions"><button data-gpt-import>匯入分析</button>${d?'<button data-gpt-clear>清除</button>':''}</div>
    </div>
    <div class="gpt-analysis-body">${gptHTML(d,false)}</div>`;
  renderGPTModalMirror();
}
function ensureImportModal(){
  if(q('#gptImportOverlay'))return;
  const o=document.createElement('div');
  o.id='gptImportOverlay';o.className='gpt-import-overlay';
  o.innerHTML=`<div class="gpt-import-box">
    <h3>匯入 ChatGPT 分析</h3>
    <p>把我在聊天室給你的 GPT1 分析碼貼進來。資料只存在這台裝置的瀏覽器，不需要 OpenAI API。</p>
    <textarea id="gptImportText" placeholder="GPT1;PRODUCT=TX;SESSION=day;ACTION=WAIT;ENTRY=46750-46770;TRIGGER=46746;CONFIRM=3分K站上46746＋3分動能翻正＋量能不縮;INVALID=跌回46746下方;STOP=46680;T1=46850;T2=46950;AT=2026-09-07T09:12:00+08:00;TTL=30;NOTE=範例"></textarea>
    <div id="gptImportError" class="gpt-import-error"></div>
    <div class="gpt-import-btns"><button data-gpt-cancel>取消</button><button data-gpt-save>匯入</button></div>
  </div>`;
  document.body.appendChild(o);
}
function openImport(){
  ensureImportModal();
  q('#gptImportError').textContent='';
  q('#gptImportText').value='';
  q('#gptImportOverlay').classList.add('show');
  setTimeout(()=>q('#gptImportText')?.focus(),30);
}
function closeImport(){q('#gptImportOverlay')?.classList.remove('show')}
function renderGPTModalMirror(){
  const body=q('#analysisBody');
  if(!body)return;
  let box=q('#gptAnalysisInModal');
  if(!box){box=document.createElement('div');box.id='gptAnalysisInModal';box.style.cssText='margin-top:9px;border-top:2px solid #19b9f4;padding-top:8px';body.appendChild(box)}
  const d=readGPT();
  box.innerHTML=`<div style="font-weight:900;color:#79d9ff;margin-bottom:6px">ChatGPT 手動分析</div>${gptHTML(d,true)}`;
}

const oldRenderAnalysis=(typeof renderAnalysisModal==='function')?renderAnalysisModal:null;
if(oldRenderAnalysis){
  renderAnalysisModal=function(){
    const r=oldRenderAnalysis.apply(this,arguments);
    setTimeout(renderGPTModalMirror,0);
    return r;
  };
}

document.addEventListener('click',e=>{
  if(e.target.closest('#sessionDay,#sessionNight,#prevContract,#nextContract,#contractMenu button')){
    setTimeout(()=>{audit();ensureGPTCard()},350);
  }
  if(e.target.closest('[data-gpt-import]'))openImport();
  if(e.target.closest('[data-gpt-cancel]'))closeImport();
  if(e.target.closest('[data-gpt-clear]')){
    if(confirm('清除這個商品／盤別的 ChatGPT 手動分析？')){
      localStorage.removeItem(gptKey());renderGPTCard();
    }
  }
  if(e.target.closest('[data-gpt-save]')){
    try{
      const d=parseGPTCode(q('#gptImportText').value);
      writeGPT(d);
      closeImport();
      if(d.product!==productCode()||d.session!==session){
        alert(`已保存到 ${d.product}｜${d.session==='night'?'夜盤':'日盤'}。切換到該商品／盤別即可看到。`);
      }
      renderGPTCard();
    }catch(err){q('#gptImportError').textContent=err?.message||String(err)}
  }
  if(e.target.id==='gptImportOverlay')closeImport();
});

document.addEventListener('keydown',e=>{if(e.key==='Escape')closeImport()});

setTimeout(()=>{audit();ensureGPTCard()},1200);
timer=setInterval(()=>{audit();renderGPTCard()},5000);
})();

(() => {
'use strict';

const VER = '20260906-chat-fast-3';
const $ = (s, r=document) => r.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
}[c]));
const fmt = (v) => (v===null || v===undefined || v==='' || !Number.isFinite(Number(v)))
  ? '—' : Number(v).toLocaleString('zh-TW', {maximumFractionDigits:2});

let history = [];
let sending = false;
let status = {ai_connected:false, mode:'rules', model:null};
let pendingText = '';
let supplementalCache = {ts:0, product:null, session:null, data:{}};
let supplementalPromise = null;

function contractInfo(){
  const code = ($('#contractCode')?.textContent || 'TX').trim().toUpperCase();
  const map = {
    TX:  {product:'TXF_CONT', name:'台指近'},
    MTX: {product:'MTX_CONT', name:'小台近'},
    TMF: {product:'TMF_CONT', name:'微台近'}
  };
  const m = map[code] || map.TX;
  const sess = $('#sessionNight')?.classList.contains('active') ? 'night' : 'day';
  return {...m, code, session:sess, session_label:sess==='night'?'夜盤':'日盤'};
}

function historyKey(){
  const x=contractInfo();
  return `futures-discuss:${x.product}:${x.session}:v2`;
}
function positionKey(){
  const x=contractInfo();
  return `futures-position:${x.product}:v2`;
}

function loadHistory(){
  try{
    const raw=localStorage.getItem(historyKey());
    const arr=raw?JSON.parse(raw):[];
    history=Array.isArray(arr)?arr.slice(-24):[];
  }catch(_){history=[]}
  renderHistory();
}
function saveHistory(){
  try{localStorage.setItem(historyKey(), JSON.stringify(history.slice(-24)))}catch(_){}
}

function loadPosition(){
  let p={direction:'none',entry_price:'',contracts:'',capital:'',note:''};
  try{
    const raw=localStorage.getItem(positionKey());
    if(raw)p={...p,...JSON.parse(raw)};
  }catch(_){}
  if($('#fchatDirection')) $('#fchatDirection').value=p.direction||'none';
  if($('#fchatEntry')) $('#fchatEntry').value=p.entry_price??'';
  if($('#fchatContracts')) $('#fchatContracts').value=p.contracts??'';
  if($('#fchatCapital')) $('#fchatCapital').value=p.capital??'';
  if($('#fchatPosNote')) $('#fchatPosNote').value=p.note??'';
  renderPositionSummary();
}

function positionContext(){
  const n=v=>{
    const x=Number(v);
    return Number.isFinite(x)?x:null;
  };
  return {
    direction:$('#fchatDirection')?.value||'none',
    entry_price:n($('#fchatEntry')?.value),
    contracts:n($('#fchatContracts')?.value),
    capital:n($('#fchatCapital')?.value),
    note:($('#fchatPosNote')?.value||'').trim()||null
  };
}

function savePosition(){
  const p=positionContext();
  try{localStorage.setItem(positionKey(),JSON.stringify(p))}catch(_){}
  renderPositionSummary();
}

function renderPositionSummary(){
  const el=$('#fchatPosSummary'); if(!el)return;
  const p=positionContext();
  if(p.direction==='none'){
    el.textContent='目前未設定持倉';
    return;
  }
  const dir=p.direction==='long'?'多單':'空單';
  const parts=[dir];
  if(p.entry_price!=null)parts.push(`成本 ${fmt(p.entry_price)}`);
  if(p.contracts!=null)parts.push(`${fmt(p.contracts)}口`);
  if(p.capital!=null)parts.push(`本金 ${fmt(p.capital)}`);
  el.textContent=parts.join('｜');
}

function analysisSnapshot(){
  const body=$('#analysisBody');
  const kv={};
  body?.querySelectorAll('.analysis-kv').forEach(el=>{
    const k=el.querySelector('.k')?.textContent?.trim();
    const v=el.querySelector('.v')?.textContent?.trim();
    if(k)kv[k]=v||'—';
  });
  return {
    decision: body?.querySelector('.analysis-hero .decision')?.textContent?.trim() || null,
    strength: body?.querySelector('.analysis-hero .strength')?.textContent?.trim() || null,
    reason: body?.querySelector('.analysis-reason')?.textContent?.replace(/^為什麼：/,'').trim() || null,
    key_values: kv
  };
}

async function getJson(url){
  try{
    const r=await fetch(url,{cache:'no-store'});
    if(!r.ok)return {ok:false,http:r.status};
    return await r.json();
  }catch(e){
    return {ok:false,error:String(e)};
  }
}

function numFromText(v){
  if(v===null||v===undefined)return null;
  const x=Number(String(v).replace(/,/g,'').replace(/%/g,'').trim());
  return Number.isFinite(x)?x:null;
}

function uiQuoteSnapshot(){
  let q=null;
  try{
    if(typeof currentQuote!=='undefined' && currentQuote && typeof currentQuote==='object') q=currentQuote;
  }catch(_){q=null}
  if(q)return q;
  return {
    last:numFromText($('#lastPrice')?.textContent),
    bid:numFromText($('#bidPrice')?.textContent),
    ask:numFromText($('#askPrice')?.textContent),
    volume:numFromText($('#totalQty')?.textContent),
    display_time:$('#quoteTime')?.textContent?.trim()||null,
    source:'ui_snapshot'
  };
}

function uiThresholdSnapshot(){
  try{
    if(typeof latestThreshold!=='undefined' && latestThreshold && typeof latestThreshold==='object') return latestThreshold;
  }catch(_){/* ignore */}
  return {ok:false,source:'ui_only',dynamic_threshold:{
    decision:$('#analysisBody .analysis-hero .decision')?.textContent?.trim()||null,
    key_values:analysisSnapshot().key_values
  }};
}

function uiMarketState(){
  try{
    if(typeof marketStatus==='function'){
      const st=marketStatus();
      if(st && typeof st==='object')return st;
    }
  }catch(_){/* ignore */}
  const strip=$('#marketStateStrip');
  return {
    open:strip?.classList.contains('open') ?? null,
    label:$('#marketStateMain')?.textContent?.trim()||null
  };
}

function uiPriceVolumeSnapshot(){
  const panel=$('#price-detail') || $('#trend-price-panel');
  if(!panel)return null;
  const out={source:panel.dataset?.pvSource||null,note:null,price_levels:null,total_volume:null,poc_price:null};
  const note=panel.querySelector('.profile-exact,.profile-est')?.textContent?.trim();
  if(note)out.note=note;
  panel.querySelectorAll('.profile-summary > div').forEach(el=>{
    const txt=el.textContent||'';
    const n=numFromText(el.querySelector('b')?.textContent);
    if(txt.includes('價位數'))out.price_levels=n;
    else if(txt.includes('總量'))out.total_volume=n;
    else if(txt.includes('最大量價'))out.poc_price=n;
  });
  return out;
}

function timeframeSnapshot(){
  const tfs={};
  document.querySelectorAll('#trendPeriods .mitake-period').forEach(el=>{
    const tf=el.querySelector('.tf')?.textContent?.trim();
    const state=el.querySelector('.state')?.textContent?.trim();
    if(tf)tfs[tf]=state||'—';
  });
  return tfs;
}

async function refreshSupplemental(force=false){
  const x=contractInfo();
  const now=Date.now();
  const same=supplementalCache.product===x.product && supplementalCache.session===x.session;
  if(!force && same && now-supplementalCache.ts<90000)return supplementalCache.data;
  if(supplementalPromise)return supplementalPromise;
  const p=encodeURIComponent(x.product);
  const s=encodeURIComponent(x.session);
  supplementalPromise=(async()=>{
    const urls=[
      `/api/blackbox/futures/diagnostics?product=${p}&session=${s}&v=${VER}`,
      `/api/blackbox/futures/institutional?product=${p}`,
      `/api/blackbox/futures/large-trader?product=${p}`,
      `/api/blackbox/futures/margin?product=${p}`,
      `/api/blackbox/futures/put-call-ratio`
    ];
    const [diagnostics,institutional,largeTrader,margin,pcr]=await Promise.all(urls.map(getJson));
    supplementalCache={
      ts:Date.now(),product:x.product,session:x.session,
      data:{diagnostics,institutional,large_trader:largeTrader,margin,put_call_ratio:pcr}
    };
    return supplementalCache.data;
  })().finally(()=>{supplementalPromise=null});
  return supplementalPromise;
}

function buildFastMarketContext(){
  const x=contractInfo();
  const st=uiMarketState();
  const same=supplementalCache.product===x.product && supplementalCache.session===x.session;
  const age=same&&supplementalCache.ts?Date.now()-supplementalCache.ts:null;
  const extra=same?supplementalCache.data:{};
  return {
    captured_at:new Date().toISOString(),
    context_mode:'fast_ui_memory',
    product:x.product,
    contract:x.code,
    name:x.name,
    session:x.session,
    session_label:x.session_label,
    market_open:st?.open ?? extra?.diagnostics?.market_open ?? null,
    quote:uiQuoteSnapshot(),
    threshold:uiThresholdSnapshot(),
    timeframe_states:timeframeSnapshot(),
    analysis:analysisSnapshot(),
    price_volume:uiPriceVolumeSnapshot(),
    diagnostics:extra?.diagnostics||null,
    institutional:extra?.institutional||null,
    large_trader:extra?.large_trader||null,
    margin:extra?.margin||null,
    put_call_ratio:extra?.put_call_ratio||null,
    supplemental_age_ms:age,
    ui_freshness_text:$('#marketStateSub')?.textContent?.trim()||null,
    ui_data_badge:$('#dataBadge')?.textContent?.trim()||null,
    ui_quote_time:$('#quoteTime')?.textContent?.trim()||null
  };
}

function injectStyles(){
  if($('#futuresChatStyleV2'))return;
  const st=document.createElement('style');
  st.id='futuresChatStyleV2';
  st.textContent=`
  .fchat{border-top:1px solid #26404c;background:#081116}
  .fchat-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:11px 12px;background:#0b1b23}
  .fchat-head strong{font-size:18px}
  .fchat-status{font-size:11px;color:#8fd8f5;margin-top:2px}
  .fchat-actions{display:flex;gap:5px}
  .fchat-mini-btn{border:1px solid #42555f;background:#10191e;color:#cbd5e1;border-radius:7px;padding:6px 8px;font-size:12px}
  .fchat-rules{padding:8px 10px;background:#10202a;border-top:1px solid #173542;border-bottom:1px solid #173542;color:#acdff3;font-size:11px;line-height:1.55}
  .fchat-pos{padding:8px 10px;background:#0b1318;border-bottom:1px solid #1e3038}
  .fchat-pos summary{cursor:pointer;color:#f3d77a;font-size:13px;font-weight:800}
  .fchat-pos-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:9px}
  .fchat-pos-grid label{display:flex;flex-direction:column;gap:4px;color:#91a3ac;font-size:11px}
  .fchat-pos-grid input,.fchat-pos-grid select,.fchat-pos-grid textarea{width:100%;background:#0c1720;color:#fff;border:1px solid #344b57;border-radius:7px;padding:8px;font:inherit;font-size:13px}
  .fchat-pos-grid textarea{min-height:54px;resize:vertical}
  .fchat-pos-wide{grid-column:1/-1}
  .fchat-pos-summary{margin-top:6px;color:#d6e4ea;font-size:12px}
  .fchat-quick{display:flex;gap:6px;overflow:auto;padding:8px 10px;border-bottom:1px solid #13252d}
  .fchat-quick button{flex:0 0 auto;border:1px solid #2c6e89;background:#0c2b39;color:#d9f4ff;border-radius:999px;padding:7px 10px;font-size:13px}
  .fchat-list{max-height:330px;overflow:auto;padding:10px;background:#05090b}
  .fchat-empty{color:#7f929b;font-size:13px;line-height:1.6;padding:5px 2px}
  .fchat-msg{display:flex;margin:7px 0}
  .fchat-msg.user{justify-content:flex-end}
  .fchat-bubble{max-width:92%;white-space:pre-wrap;word-break:break-word;line-height:1.58;padding:9px 11px;border-radius:12px;font-size:14px}
  .fchat-msg.user .fchat-bubble{background:#0c5573;color:#fff;border-bottom-right-radius:4px}
  .fchat-msg.assistant .fchat-bubble{background:#111827;color:#e5edf3;border:1px solid #273449;border-bottom-left-radius:4px}
  .fchat-msg.pending .fchat-bubble{background:#151c22;color:#9fd8ef;border:1px dashed #31596b}
  .fchat-inputbar{position:sticky;bottom:0;z-index:4;display:grid;grid-template-columns:1fr auto;gap:7px;padding:9px;background:#071014;border-top:1px solid #26404c}
  .fchat-input{width:100%;min-height:46px;max-height:120px;resize:none;background:#0d1720;color:#fff;border:1px solid #365260;border-radius:10px;padding:10px 11px;font:inherit;font-size:15px}
  .fchat-send{min-width:66px;border:0;border-radius:10px;background:#19aee8;color:#fff;font-weight:900;padding:0 12px}
  .fchat-send:disabled{opacity:.45}
  .fchat-note{padding:7px 10px;color:#c99d27;background:#211a07;font-size:11px;line-height:1.45}
  @media(max-width:520px){.fchat-pos-grid{grid-template-columns:1fr 1fr}.fchat-list{max-height:290px}}
  `;
  document.head.appendChild(st);
}

function injectPanel(){
  if($('#futuresChatPanelV2'))return;
  const foot=$('.analysis-foot');
  if(!foot)return;

  const panel=document.createElement('section');
  panel.id='futuresChatPanelV2';
  panel.className='fchat';
  panel.innerHTML=`
    <div class="fchat-head">
      <div>
        <strong>💬 討論買賣判斷</strong>
        <div id="fchatStatus" class="fchat-status">檢查連線…</div>
      </div>
      <div class="fchat-actions">
        <button id="fchatRefresh" class="fchat-mini-btn">重抓資料</button>
        <button id="fchatClear" class="fchat-mini-btn">清除對話</button>
      </div>
    </div>

    <div class="fchat-rules">
      固定共同規則：TAIFEX官方為基準｜缺資料不造假｜可以反駁我、我也會反駁你｜
      新證據足夠時我會明確改判｜訊號%不是保證獲利率。
    </div>

    <details class="fchat-pos">
      <summary>我的交易狀態　<span id="fchatPosSummary" class="fchat-pos-summary"></span></summary>
      <div class="fchat-pos-grid">
        <label>方向
          <select id="fchatDirection">
            <option value="none">目前沒有持倉</option>
            <option value="long">多單</option>
            <option value="short">空單</option>
          </select>
        </label>
        <label>成本
          <input id="fchatEntry" inputmode="decimal" placeholder="例如 46620">
        </label>
        <label>口數
          <input id="fchatContracts" inputmode="decimal" placeholder="例如 1">
        </label>
        <label>可用本金
          <input id="fchatCapital" inputmode="decimal" placeholder="例如 50000">
        </label>
        <label class="fchat-pos-wide">補充
          <textarea id="fchatPosNote" placeholder="例如：今天只做當沖、不留倉"></textarea>
        </label>
      </div>
    </details>

    <div class="fchat-quick">
      <button data-q="現在要買、賣、還是等？請跟我討論理由。">買/賣/等？</button>
      <button data-q="我覺得現在可以追多。你同意嗎？不同意就直接反駁我。">反駁我：追多</button>
      <button data-q="我覺得現在可以做空。你同意嗎？不同意就直接反駁我。">反駁我：做空</button>
      <button data-q="什麼條件出現，你會把目前判斷改成做多？">什麼會改多</button>
      <button data-q="什麼條件出現，你會把目前判斷改成做空？">什麼會改空</button>
      <button data-q="如果我已經有單，現在最重要的失效條件是什麼？">持倉風險</button>
    </div>

    <div id="fchatList" class="fchat-list"></div>

    <div id="fchatNote" class="fchat-note">
      送出會先用畫面中已更新的行情立即分析；法人/診斷等較慢資料在背景更新，不再每句都卡住。這裡只協助判斷，不會替你下單。
    </div>

    <div class="fchat-inputbar">
      <textarea id="fchatInput" class="fchat-input" rows="1"
        placeholder="直接跟我辯。例如：量能2.3倍，我覺得應該追多，你為什麼不同意？"></textarea>
      <button id="fchatSend" class="fchat-send">送出</button>
    </div>
  `;

  foot.parentNode.insertBefore(panel, foot);

  $('#fchatSend')?.addEventListener('click',()=>sendMessage());
  $('#fchatInput')?.addEventListener('keydown',e=>{
    if(e.key==='Enter'&&!e.shiftKey){
      e.preventDefault();
      sendMessage();
    }
  });
  panel.querySelectorAll('[data-q]').forEach(b=>b.addEventListener('click',()=>{
    const inp=$('#fchatInput');
    if(inp)inp.value=b.dataset.q||'';
    sendMessage();
  }));
  $('#fchatClear')?.addEventListener('click',()=>{
    history=[]; saveHistory(); renderHistory();
  });
  $('#fchatRefresh')?.addEventListener('click', async()=>{
    const btn=$('#fchatRefresh');
    if(btn){btn.disabled=true;btn.textContent='抓取中…'}
    try{
      await refreshSupplemental(true);
      if(btn)btn.textContent='已更新';
      setTimeout(()=>{if(btn)btn.textContent='重抓資料'},900);
    }finally{
      if(btn)btn.disabled=false;
    }
  });

  ['#fchatDirection','#fchatEntry','#fchatContracts','#fchatCapital','#fchatPosNote']
    .forEach(sel=>{
      $(sel)?.addEventListener('change',savePosition);
      $(sel)?.addEventListener('input',savePosition);
    });

  loadPosition();
  loadHistory();
  refreshStatus();
}

function renderHistory(){
  const box=$('#fchatList'); if(!box)return;
  let html='';
  if(!history.length){
    html=`<div class="fchat-empty">
      這裡不是固定答案按鈕。你可以直接反駁我的判斷，或補上你看到的理由，我會用同一份當下資料跟你重新比較。<br><br>
      例如：「你叫我等，但3分量能放大，我覺得可以追。你漏了什麼？」
    </div>`;
  }else{
    html=history.map(m=>`
      <div class="fchat-msg ${m.role==='user'?'user':'assistant'}">
        <div class="fchat-bubble">${esc(m.content)}</div>
      </div>`).join('');
  }
  if(pendingText){
    html+=`<div class="fchat-msg assistant pending"><div class="fchat-bubble">${esc(pendingText)}</div></div>`;
  }
  box.innerHTML=html;
  box.scrollTop=box.scrollHeight;
}

async function refreshStatus(){
  const el=$('#fchatStatus');
  try{
    const j=await getJson('/api/blackbox/futures/chat/status');
    status=j||status;
    if(el){
      el.textContent=j?.ai_connected
        ? `討論模式已連線｜${j.model||'model'}｜規則 ${j.decision_rules_version||'—'}`
        : `規則模式｜尚未接 OpenAI API｜規則 ${j?.decision_rules_version||'—'}`;
    }
  }catch(_){
    if(el)el.textContent='對話狀態讀取失敗';
  }
}

async function sendMessage(){
  if(sending)return;
  const inp=$('#fchatInput');
  const text=(inp?.value||'').trim();
  if(!text)return;
  if(inp)inp.value='';

  history.push({role:'user',content:text});
  history=history.slice(-24);
  pendingText='✓ 已收到｜直接用目前畫面行情分析中…';
  renderHistory();
  saveHistory();

  sending=true;
  const btn=$('#fchatSend');
  if(btn){btn.disabled=true;btn.textContent='分析中…'}
  const clientStarted=performance.now();

  try{
    // 不等待較慢的法人/診斷等 API；舊快取先用，新資料背景更新。
    refreshSupplemental(false).catch(()=>{});
    const market=buildFastMarketContext();
    const position=positionContext();
    pendingText='✓ 已收到｜行情快照已帶入，等待回答…';
    renderHistory();

    const r=await fetch('/api/blackbox/futures/chat',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      cache:'no-store',
      body:JSON.stringify({
        message:text,
        history:history.slice(0,-1).slice(-16),
        market,
        position
      })
    });
    const j=await r.json();
    const reply=j?.ok
      ? (j.reply||'沒有回覆內容。')
      : `對話失敗：${j?.error||`HTTP ${r.status}`}${j?.detail?`\n${j.detail}`:''}`;

    pendingText='';
    history.push({role:'assistant',content:reply});
    history=history.slice(-24);
    saveHistory();
    renderHistory();

    const totalMs=Math.round(performance.now()-clientStarted);
    const st=$('#fchatStatus');
    const serverMs=Number(j?.elapsed_ms);
    const timing=Number.isFinite(serverMs)?`｜後端 ${(serverMs/1000).toFixed(1)}秒｜總 ${(totalMs/1000).toFixed(1)}秒`:`｜總 ${(totalMs/1000).toFixed(1)}秒`;
    if(st && j?.mode==='openai'){
      st.textContent=`討論模式已連線｜${j.model||status.model||'model'}${timing}`;
    }else if(st && j?.mode==='rules'){
      st.textContent=`規則模式｜尚未接 OpenAI API${timing}`;
    }
  }catch(e){
    pendingText='';
    history.push({role:'assistant',content:`對話失敗：${String(e)}`});
    saveHistory(); renderHistory();
  }finally{
    sending=false;
    if(btn){btn.disabled=false;btn.textContent='送出'}
  }
}

function boot(){
  injectStyles();
  injectPanel();

  // 頁面載入後先暖快取；使用者真正送出時不等它。
  setTimeout(()=>refreshSupplemental(false).catch(()=>{}),700);

  document.addEventListener('click',e=>{
    if(e.target.closest('#analysisBtn,#quoteAnalysisTrigger')){
      setTimeout(()=>refreshSupplemental(false).catch(()=>{}),120);
    }
    if(e.target.closest('#sessionDay,#sessionNight,#prevContract,#nextContract,#contractMenu button')){
      supplementalCache={ts:0,product:null,session:null,data:{}};
      setTimeout(()=>{
        loadPosition();
        loadHistory();
        refreshStatus();
        refreshSupplemental(false).catch(()=>{});
      },300);
    }
  });
}

if(document.readyState==='loading'){
  document.addEventListener('DOMContentLoaded',boot);
}else{
  boot();
}
})();
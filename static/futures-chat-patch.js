(() => {
'use strict';

const VER = '20260906-chat-context-5';
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

function looksWeatherText(text){
  return /(天氣|下雨|降雨|溫度|氣溫|熱不熱|冷不冷|會不會下雨)/.test(String(text||''));
}

function looksTradingText(text){
  const t=String(text||'').trim();
  if(/(期貨|台指|臺指|小台|小臺|微台|微臺|TX|MTX|TMF|做多|做空|追多|追空|多單|空單|持倉|停損|停利|進場|出場|K線|3分|6分|9分|量能|法人|外資|分價|POC|突破|回測|大盤)/i.test(t)) return true;
  if(/^(買|賣|等|多|空)$/.test(t)) return true;
  if(/(要不要買|要不要賣|現在買|現在賣|能買嗎|能空嗎|買了會怎樣|賣了會怎樣|買了呢|賣了呢|可以買嗎|可以賣嗎|要跑嗎|要追嗎)/.test(t)) return true;
  const recent=history.slice(-6).map(x=>x.content||'').join(' ');
  const recentTrade=/(期貨|台指|小台|微台|現價|持倉|訊號|市場休息|買\/賣\/等|做多|做空|停損)/.test(recent);
  const shortFollow=t.length<=18 && /(買|賣|多|空|現在|怎樣|如何|呢|會不會)/.test(t);
  return !!(shortFollow && recentTrade);
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
  .fchat-quick{display:flex;align-items:center;gap:6px;overflow:auto;padding:8px 10px;border-bottom:1px solid #13252d}
  .fchat-quick-label{flex:0 0 auto;color:#7f929b;font-size:12px}.fchat-quick button{flex:0 0 auto;border:1px solid #2c6e89;background:#0c2b39;color:#d9f4ff;border-radius:999px;padding:7px 10px;font-size:13px}
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
        <strong>💬 跟我聊／市場判斷</strong>
        <div id="fchatStatus" class="fchat-status">檢查連線…</div>
      </div>
      <div class="fchat-actions">
        <button id="fchatRefresh" class="fchat-mini-btn">重抓資料</button>
        <button id="fchatClear" class="fchat-mini-btn">清除對話</button>
      </div>
    </div>

    <div class="fchat-rules">
      有接 AI 時可以自由聊天；沒接 AI 時不再假裝會聊，只處理期貨／持倉／即時天氣。快捷鍵只是捷徑。<br>
      交易規則：TAIFEX官方為基準｜缺資料不造假｜新證據足夠時才改判｜訊號%不是保證獲利率。
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
      <span class="fchat-quick-label">快捷：</span>
      <button data-q="現在要買、賣、還是等？請跟我討論理由。">買/賣/等？</button>
      <button data-q="我覺得現在可以追多。你同意嗎？不同意就直接反駁我。">反駁我：追多</button>
      <button data-q="我覺得現在可以做空。你同意嗎？不同意就直接反駁我。">反駁我：做空</button>
      <button data-q="什麼條件出現，你會把目前判斷改成做多？">什麼會改多</button>
      <button data-q="什麼條件出現，你會把目前判斷改成做空？">什麼會改空</button>
      <button data-q="如果我已經有單，現在最重要的失效條件是什麼？">持倉風險</button>
    </div>

    <div id="fchatList" class="fchat-list"></div>

    <div id="fchatNote" class="fchat-note">
      沒接 OpenAI API 時：不再用固定台詞冒充聊天；看不懂就明說。期貨／持倉／台灣主要城市天氣仍可用。交易功能只協助判斷，不會替你下單。
    </div>

    <div class="fchat-inputbar">
      <textarea id="fchatInput" class="fchat-input" rows="1"
        placeholder="直接講。像『買了會怎樣？』會接目前期貨；天氣也可問。完整自由聊天需 OpenAI API。"></textarea>
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
      直接講就可以，不用按快捷鍵。像「買了會怎樣？」會沿用目前期貨上下文；「高雄今天天氣如何？」走即時天氣。<br><br>
      尚未接 OpenAI API 時，真正的自由聊天不會假裝會；看不懂就直接說看不懂。
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
        ? `AI聊天已連線｜${j.model||'model'}｜規則 ${j.decision_rules_version||'—'}`
        : `AI未連線｜期貨規則＋天氣可用｜規則 ${j?.decision_rules_version||'—'}`;
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
  pendingText='✓ 已收到｜處理中…';
  renderHistory();
  saveHistory();

  sending=true;
  const btn=$('#fchatSend');
  if(btn){btn.disabled=true;btn.textContent='回覆中…'}
  const clientStarted=performance.now();

  try{
    const trading=looksTradingText(text);
    const weather=looksWeatherText(text);
    // 只有交易問題才暖市場慢資料；一般聊天不再浪費時間抓法人/診斷。
    if(trading) refreshSupplemental(false).catch(()=>{});
    const market=buildFastMarketContext();
    const position=positionContext();
    pendingText=trading
      ? '✓ 已收到｜行情快照已帶入，等待回答…'
      : (weather ? '✓ 已收到｜查天氣／回覆中…' : '✓ 已收到｜回覆中…');
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
      st.textContent=`AI聊天已連線｜${j.model||status.model||'model'}${timing}`;
    }else if(st && j?.mode==='rules'){
      const label=j?.intent==='trading'?'交易規則':(j?.intent==='weather'?'即時天氣':'AI未連線');
      st.textContent=`${label}｜自由聊天未啟用${timing}`;
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
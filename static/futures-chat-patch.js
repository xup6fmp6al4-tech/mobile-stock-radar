(() => {
'use strict';

const VER = '20260906-chat-discuss-2';
const $ = (s, r=document) => r.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
}[c]));
const fmt = (v) => (v===null || v===undefined || v==='' || !Number.isFinite(Number(v)))
  ? '—' : Number(v).toLocaleString('zh-TW', {maximumFractionDigits:2});

let history = [];
let sending = false;
let status = {ai_connected:false, mode:'rules', model:null};

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

async function buildMarketContext(){
  const x=contractInfo();
  const p=encodeURIComponent(x.product);
  const s=encodeURIComponent(x.session);

  const urls=[
    `/api/blackbox/futures/realtime?product=${p}&session=${s}`,
    `/api/blackbox/futures/diagnostics?product=${p}&session=${s}&v=${VER}`,
    `/api/blackbox/futures/institutional?product=${p}`,
    `/api/blackbox/futures/large-trader?product=${p}`,
    `/api/blackbox/futures/margin?product=${p}`,
    `/api/blackbox/futures/put-call-ratio`,
    `/api/blackbox/futures/price-volume?product=${p}&session=${s}&limit=40&v=${VER}`,
    `/api/blackbox/futures/threshold?product=${p}&session=${s}`
  ];
  const [quote,diagnostics,institutional,largeTrader,margin,pcr,priceVolume,threshold] =
    await Promise.all(urls.map(getJson));

  const tfs={};
  document.querySelectorAll('#trendPeriods .mitake-period').forEach(el=>{
    const tf=el.querySelector('.tf')?.textContent?.trim();
    const state=el.querySelector('.state')?.textContent?.trim();
    if(tf)tfs[tf]=state||'—';
  });

  return {
    captured_at:new Date().toISOString(),
    product:x.product,
    contract:x.code,
    name:x.name,
    session:x.session,
    session_label:x.session_label,
    market_open:diagnostics?.market_open ?? null,
    quote,
    diagnostics,
    institutional,
    large_trader:largeTrader,
    margin,
    put_call_ratio:pcr,
    price_volume:priceVolume,
    threshold,
    timeframe_states:tfs,
    analysis:analysisSnapshot(),
    ui_freshness_text:$('#marketStateSub')?.textContent?.trim()||null,
    ui_data_badge:$('#dataBadge')?.textContent?.trim()||null
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
      每次送出前會重新抓目前商品的官方/APP資料。這裡只協助判斷，不會替你下單。
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
      await buildMarketContext();
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
  if(!history.length){
    box.innerHTML=`<div class="fchat-empty">
      這裡不是固定答案按鈕。你可以直接反駁我的判斷，或補上你看到的理由，我會用同一份當下資料跟你重新比較。<br><br>
      例如：「你叫我等，但3分量能放大，我覺得可以追。你漏了什麼？」
    </div>`;
    return;
  }
  box.innerHTML=history.map(m=>`
    <div class="fchat-msg ${m.role==='user'?'user':'assistant'}">
      <div class="fchat-bubble">${esc(m.content)}</div>
    </div>`).join('');
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
  renderHistory();
  saveHistory();

  sending=true;
  const btn=$('#fchatSend');
  if(btn){btn.disabled=true;btn.textContent='重抓＋分析…'}

  try{
    const market=await buildMarketContext();
    const position=positionContext();

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

    history.push({role:'assistant',content:reply});
    history=history.slice(-24);
    saveHistory();
    renderHistory();

    const st=$('#fchatStatus');
    if(st && j?.mode==='openai'){
      st.textContent=`討論模式已連線｜${j.model||status.model||'model'}｜規則 ${j.decision_rules_version||'—'}`;
    }else if(st && j?.mode==='rules'){
      st.textContent=`規則模式｜尚未接 OpenAI API｜規則 ${j.decision_rules_version||'—'}`;
    }
  }catch(e){
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

  document.addEventListener('click',e=>{
    if(e.target.closest('#sessionDay,#sessionNight,#prevContract,#nextContract,#contractMenu button')){
      setTimeout(()=>{
        loadPosition();
        loadHistory();
        refreshStatus();
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
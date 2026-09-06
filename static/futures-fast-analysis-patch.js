(() => {
'use strict';

const FAST_VER = '20260907-fast-analysis-1';

if (typeof renderAnalysisModal !== 'function') return;

const originalRenderAnalysisModal = renderAnalysisModal;
const originalLoadBars = (typeof loadBars === 'function') ? loadBars : null;

const cfg = {
  noise_pct: 0.0008,
  trend_pct: 0.0018,
  accel_pct: 0.0030,
  big_move_pct: 0.0050,
  noise_tr_mult: 1.0,
  trend_tr_mult: 1.8,
  accel_tr_mult: 2.8,
  big_move_tr_mult: 4.0,
  startup_3m_pct: 0.00045,
  confirm_6m_pct: 0.00080,
  decision_9m_pct: 0.00120,
  startup_3m_tr_mult: 0.60,
  confirm_6m_tr_mult: 1.10,
  decision_9m_tr_mult: 1.60,
  volume_start_ratio: 1.05,
  volume_confirm_ratio: 1.15,
  volume_accel_ratio: 1.35,
  min_bars: 10,
  break_buffer_tr_mult: 0.10,
  vwap_buffer_noise_mult: 0.12,
  trend_filter_block_mult: 1.20,
};

const num = (v, d=0) => {
  const x = Number(v);
  return Number.isFinite(x) ? x : d;
};

function median(xs){
  const a = xs.slice().sort((x,y)=>x-y);
  if(!a.length) return 1;
  const m = Math.floor(a.length/2);
  return a.length % 2 ? a[m] : (a[m-1]+a[m])/2;
}

function medianTrueRange(rows){
  const trs = [];
  let prevClose = null;
  for(const r of rows){
    const h=num(r.high), l=num(r.low), c=num(r.close);
    if(!c) continue;
    if(h && l){
      const tr = prevClose == null ? (h-l) : Math.max(h-l, Math.abs(h-prevClose), Math.abs(l-prevClose));
      if(tr>0) trs.push(tr);
    }
    prevClose=c;
  }
  return trs.length ? median(trs.slice(-20)) : 1;
}

function calcVwap(rows){
  let n=0,d=0;
  for(const r of rows){
    const h=num(r.high), l=num(r.low), c=num(r.close), v=num(r.volume);
    if(c<=0 || v<=0) continue;
    const typical = h>0 && l>0 ? (h+l+c)/3 : c;
    n += typical*v;
    d += v;
  }
  if(d<=0){
    const closes=rows.map(r=>num(r.close)).filter(x=>x>0);
    return closes.length ? closes[closes.length-1] : 0;
  }
  return n/d;
}

function volumeRatio(rows){
  const complete=rows.slice(0,-1);
  const vols=complete.map(r=>num(r.volume)).filter(v=>v>0);
  if(vols.length<6) return 1;
  const recent=vols.slice(-2);
  const history=vols.slice(0,-2).slice(-20);
  const base=median(history.length?history:vols);
  if(base<=0) return 1;
  return ((recent[0]+recent[1])/2)/base;
}

function closeAgo(rows,bars){
  return rows.length>bars ? num(rows[rows.length-1-bars].close, NaN) : null;
}

function windowHighLow(rows,priorBars,price){
  if(rows.length<2) return [price,price];
  const w=rows.slice(-(priorBars+1),-1);
  if(!w.length) return [price,price];
  const hi=Math.max(...w.map(r=>Number.isFinite(Number(r.high))?Number(r.high):num(r.close,price)));
  const lo=Math.min(...w.map(r=>Number.isFinite(Number(r.low))?Number(r.low):num(r.close,price)));
  return [hi,lo];
}

function overallTrend(sessionMove,price,vwap,noisePts,trendPts,bigPts){
  const above=price>vwap+noisePts*.15;
  const below=price<vwap-noisePts*.15;
  if(sessionMove>=bigPts) return above?['BIG_UP',90]:['UP_MOVE_WEAKENING',68];
  if(sessionMove<=-bigPts) return below?['BIG_DOWN',90]:['DOWN_MOVE_WEAKENING',68];
  if(sessionMove>=trendPts && above) return ['STRONG_UP',75];
  if(sessionMove<=-trendPts && below) return ['STRONG_DOWN',75];
  if(sessionMove>=noisePts || above) return ['UP_BIAS',60];
  if(sessionMove<=-noisePts || below) return ['DOWN_BIAS',60];
  return ['RANGE',50];
}

function entrySignalFast(o){
  const upContext=['BIG_UP','STRONG_UP','UP_BIAS','UP_MOVE_WEAKENING','RANGE'].includes(o.trendState);
  const downContext=['BIG_DOWN','STRONG_DOWN','DOWN_BIAS','DOWN_MOVE_WEAKENING','RANGE'].includes(o.trendState);
  const strong15Down=o.m15<=-(o.trendPts*cfg.trend_filter_block_mult);
  const strong15Up=o.m15>=(o.trendPts*cfg.trend_filter_block_mult);

  const longStart=o.break3Up&&o.aboveVwap&&o.m3>=o.startup3&&o.vr>=cfg.volume_start_ratio;
  const shortStart=o.break3Down&&o.belowVwap&&o.m3<=-o.startup3&&o.vr>=cfg.volume_start_ratio;
  const longConfirm=o.break6Up&&o.aboveVwap&&o.m6>=o.confirm6&&o.m3>0&&o.vr>=cfg.volume_confirm_ratio;
  const shortConfirm=o.break6Down&&o.belowVwap&&o.m6<=-o.confirm6&&o.m3<0&&o.vr>=cfg.volume_confirm_ratio;
  const longDecision=o.break9Up&&o.aboveVwap&&o.m9>=o.decision9&&o.m6>0&&o.m3>0&&o.vr>=cfg.volume_confirm_ratio;
  const shortDecision=o.break9Down&&o.belowVwap&&o.m9<=-o.decision9&&o.m6<0&&o.m3<0&&o.vr>=cfg.volume_confirm_ratio;

  if(longDecision&&!strong15Down) return ['LONG_DECISION_9M',o.vr>=cfg.volume_accel_ratio?80:76,'long','9m_decision'];
  if(shortDecision&&!strong15Up) return ['SHORT_DECISION_9M',o.vr>=cfg.volume_accel_ratio?80:76,'short','9m_decision'];
  if(longConfirm&&upContext&&!strong15Down) return ['LONG_CONFIRM_6M',69,'long','6m_confirm'];
  if(shortConfirm&&downContext&&!strong15Up) return ['SHORT_CONFIRM_6M',69,'short','6m_confirm'];
  if(longStart) return strong15Down?['LONG_START_3M_COUNTERTREND',56,'wait','3m_start']:['LONG_START_3M',61,'watch_long','3m_start'];
  if(shortStart) return strong15Up?['SHORT_START_3M_COUNTERTREND',56,'wait','3m_start']:['SHORT_START_3M',61,'watch_short','3m_start'];
  if(['BIG_UP','STRONG_UP'].includes(o.trendState)) return ['WAIT_NO_CHASE_UP',55,'wait','wait'];
  if(['BIG_DOWN','STRONG_DOWN'].includes(o.trendState)) return ['WAIT_NO_CHASE_DOWN',55,'wait','wait'];
  if(['UP_MOVE_WEAKENING','DOWN_MOVE_WEAKENING'].includes(o.trendState)) return ['WAIT_TREND_WEAKENING',52,'wait','wait'];
  return ['WAIT_DIRECTION',50,'wait','wait'];
}

function round2(v){return Math.round(v*100)/100}
function round3(v){return Math.round(v*1000)/1000}

function computeV3(rows){
  const rs=(rows||[]).map(r=>({...r})).filter(r=>num(r.close)>0);
  if(rs.length<cfg.min_bars){
    return {ok:false,state:'INSUFFICIENT_DATA',market_trend_state:'INSUFFICIENT_DATA',entry_state:'WAIT_DATA',action:'observe',actionable:false,bars:rs.length,reason:`need at least ${cfg.min_bars} 3m bars`};
  }

  const price=num(rs[rs.length-1].close);
  const firstOpen=(rs.find(r=>num(r.open)>0)||{}).open;
  const sessionOpen=num(firstOpen,price);
  const sessionHigh=Math.max(...rs.map(r=>num(r.high,num(r.close))));
  const sessionLow=Math.min(...rs.map(r=>num(r.low,num(r.close))));
  const tr=Math.max(medianTrueRange(rs),1);
  const vwap=calcVwap(rs);
  const vr=volumeRatio(rs);

  const noisePts=Math.max(price*cfg.noise_pct,tr*cfg.noise_tr_mult);
  const trendPts=Math.max(price*cfg.trend_pct,tr*cfg.trend_tr_mult);
  const accelPts=Math.max(price*cfg.accel_pct,tr*cfg.accel_tr_mult);
  const bigPts=Math.max(price*cfg.big_move_pct,tr*cfg.big_move_tr_mult);
  const startup3=Math.max(price*cfg.startup_3m_pct,tr*cfg.startup_3m_tr_mult);
  const confirm6=Math.max(price*cfg.confirm_6m_pct,tr*cfg.confirm_6m_tr_mult);
  const decision9=Math.max(price*cfg.decision_9m_pct,tr*cfg.decision_9m_tr_mult);

  const sessionMove=price-sessionOpen;
  const sessionMovePct=sessionOpen?sessionMove/sessionOpen*100:0;
  const c3=closeAgo(rs,1), c6=closeAgo(rs,2), c9=closeAgo(rs,3), c15=closeAgo(rs,5);
  const m3=c3==null||!Number.isFinite(c3)?0:price-c3;
  const m6=c6==null||!Number.isFinite(c6)?0:price-c6;
  const m9=c9==null||!Number.isFinite(c9)?0:price-c9;
  const m15=c15==null||!Number.isFinite(c15)?0:price-c15;

  const [hi3,lo3]=windowHighLow(rs,1,price);
  const [hi6,lo6]=windowHighLow(rs,2,price);
  const [hi9,lo9]=windowHighLow(rs,3,price);
  const [hi15,lo15]=windowHighLow(rs,5,price);

  const break3Up=price>hi3, break3Down=price<lo3;
  const break6Up=price>hi6, break6Down=price<lo6;
  const break9Up=price>hi9, break9Down=price<lo9;

  const vwapBuffer=noisePts*cfg.vwap_buffer_noise_mult;
  const aboveVwap=price>vwap+vwapBuffer;
  const belowVwap=price<vwap-vwapBuffer;
  const [trendState,trendStrength]=overallTrend(sessionMove,price,vwap,noisePts,trendPts,bigPts);
  const [entryState,entryStrength,analyticalSide,decisionStage]=entrySignalFast({
    trendState,m3,m6,m9,m15,trendPts,startup3,confirm6,decision9,
    break3Up,break3Down,break6Up,break6Down,break9Up,break9Down,
    aboveVwap,belowVwap,vr
  });

  const breakBuffer=Math.max(1,tr*cfg.break_buffer_tr_mult);
  const nextLong=Math.max(hi3+breakBuffer,vwap+vwapBuffer);
  const nextShort=Math.min(lo3-breakBuffer,vwap-vwapBuffer);
  const nextLong6=Math.max(hi6+breakBuffer,vwap+vwapBuffer);
  const nextShort6=Math.min(lo6-breakBuffer,vwap-vwapBuffer);
  const nextLong9=Math.max(hi9+breakBuffer,vwap+vwapBuffer);
  const nextShort9=Math.min(lo9-breakBuffer,vwap-vwapBuffer);

  return {
    ok:true,
    model_version:'v3_3m_primary_fast_local',
    state:trendState,
    market_trend_state:trendState,
    market_trend_strength_pct:trendStrength,
    entry_state:entryState,
    entry_strength_pct:entryStrength,
    decision_stage:decisionStage,
    action:'observe',
    actionable:false,
    price:round2(price),
    session_open:round2(sessionOpen),
    vwap:round2(vwap),
    market_trend:{
      state:trendState,
      strength_pct:trendStrength,
      move_from_open_points:round2(sessionMove),
      move_from_open_pct:round3(sessionMovePct),
      session_high:round2(sessionHigh),
      session_low:round2(sessionLow),
      price_vs_vwap_points:round2(price-vwap),
    },
    entry_signal:{
      state:entryState,
      strength_pct:entryStrength,
      decision_stage:decisionStage,
      analytical_side:analyticalSide,
      execution_action:'observe',
      actionable:false,
      next_long_trigger_level:Math.round(nextLong),
      next_short_trigger_level:Math.round(nextShort),
      next_long_confirm_6m_level:Math.round(nextLong6),
      next_short_confirm_6m_level:Math.round(nextShort6),
      next_long_decision_9m_level:Math.round(nextLong9),
      next_short_decision_9m_level:Math.round(nextShort9),
    },
    metrics:{
      median_true_range_3m_points:round2(tr),
      momentum_3m_points:round2(m3),
      momentum_6m_points:round2(m6),
      momentum_9m_points:round2(m9),
      momentum_15m_filter_points:round2(m15),
      volume_ratio:round3(vr),
      prior_3m_high:round2(hi3), prior_3m_low:round2(lo3),
      prior_6m_high:round2(hi6), prior_6m_low:round2(lo6),
      prior_9m_high:round2(hi9), prior_9m_low:round2(lo9),
      prior_15m_high:round2(hi15), prior_15m_low:round2(lo15),
      break_3m:break3Up?'up':break3Down?'down':'no',
      break_6m:break6Up?'up':break6Down?'down':'no',
      break_9m:break9Up?'up':break9Down?'down':'no',
    },
    thresholds:{
      noise_points:Math.round(noisePts),
      startup_3m_points:Math.round(startup3),
      confirm_6m_points:Math.round(confirm6),
      decision_9m_points:Math.round(decision9),
      trend_confirm_points_15m_filter:Math.round(trendPts),
      acceleration_points:Math.round(accelPts),
      big_move_points:Math.round(bigPts),
      next_long_trigger_level:Math.round(nextLong),
      next_short_trigger_level:Math.round(nextShort),
      next_long_confirm_6m_level:Math.round(nextLong6),
      next_short_confirm_6m_level:Math.round(nextShort6),
      next_long_decision_9m_level:Math.round(nextLong9),
      next_short_decision_9m_level:Math.round(nextShort9),
    }
  };
}

function sourceText(){
  const s=String((typeof barsDataSource!=='undefined'&&barsDataSource)||'');
  if(s.includes('taifex_mis_getChartDataTick')) return ['期交所 MIS 3分K','ok'];
  if(s.includes('taifex_official_tick')) return ['期交所官方逐筆聚合3分K','ok'];
  if(s.includes('taifex_mis_observed')) return ['期交所 MIS 實際觀測3分K','ok'];
  if(s.includes('yahoo')) return ['Yahoo 1分K聚合備援','warn'];
  if(s.includes('local_archive')) return ['歷史封存3分K','warn'];
  return [s||'3分K來源未標示','warn'];
}

function lastBarInfo(){
  const bars=(typeof currentBars!=='undefined'?currentBars:[])||[];
  const b=bars[bars.length-1];
  if(!b) return {text:'—',age:null};
  let text=b.trading_date||'';
  let age=null;
  const ts=Number(b.ts_utc);
  if(Number.isFinite(ts)){
    const dt=new Date(ts*1000);
    const t=dt.toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Taipei'});
    text=`${text?text+' ':''}${t}`;
    age=Math.max(0,Date.now()/1000-ts);
  }
  return {text:text||'—',age};
}

function backendCheck(local){
  const b=(typeof latestThreshold!=='undefined'&&latestThreshold?.dynamic_threshold)||null;
  if(!b?.ok) return '後端完整校驗：背景計算中';
  const same=b.entry_state===local.entry_state && b.market_trend_state===local.market_trend_state;
  return same?'後端完整校驗：✓ 核心判定一致':'後端完整校驗：來源/時間點不同，以畫面目前3分K為準';
}

function renderFast(){
  const body=qs('#analysisBody');
  const sub=qs('#analysisSub');
  if(!body||!sub) return;

  const product=PRODUCTS[current]||{};
  const bars=(typeof currentBars!=='undefined'?currentBars:[])||[];
  const local=computeV3(bars);
  const q=currentQuote||{};
  const usedSession=(typeof barsSourceSession!=='undefined'&&barsSourceSession)||session;
  const sessionName=usedSession==='day'?'日盤':'夜盤';
  const [src,srcState]=sourceText();
  const lb=lastBarInfo();
  const open=marketStatus().open;

  sub.textContent=`${product.name||''} ${sessionName}${!open?'（市場休息／回看）':''} ・ 現價 ${fmt(q.last)} ・ ${bars[bars.length-1]?.trading_date||'—'}`;

  if(!bars.length){
    body.innerHTML='<div class="empty-state"><strong>3分K同步中…</strong><span>先讀期交所/MIS實際資料；不先塞假判斷。</span></div>';
    return;
  }

  if(open && lb.age!=null && lb.age>300){
    body.innerHTML=`<div class="empty-state"><strong>3分K資料過舊，暫不判斷</strong><span>最後資料 ${lb.text}，超過5分鐘；避免用舊資料做即時決策。</span></div>`;
    return;
  }

  if(!local.ok){
    body.innerHTML=`<div class="empty-state"><strong>目前資料不足</strong><span>實際3分K只有 ${bars.length} 根；至少要10根才啟動v3判定。</span></div>`;
    return;
  }

  const d=local, m=d.metrics||{}, e=d.entry_signal||{};
  const trend=trendLabel(d.market_trend_state);
  const entry=entryLabel(d.entry_state);
  const longTrigger=e.next_long_trigger_level ?? null;
  const shortTrigger=e.next_short_trigger_level ?? null;
  const srcColor=srcState==='ok'?'#7ee2a8':'#fbbf24';

  body.innerHTML=`
    <div style="padding:7px 10px;margin-bottom:8px;border:1px solid #26343a;background:#071014;font-size:12px;color:${srcColor}">
      快速層｜${src}｜最後3分K ${lb.text}｜${FAST_VER}
    </div>

    <div class="analysis-hero">
      <div class="label">現在動作</div>
      <div class="decision">${entry}</div>
      <div class="strength">訊號 ${fmt(d.entry_strength_pct)}% ｜ 目前 ${trend} ${fmt(d.market_trend_strength_pct)}%</div>
    </div>

    <div class="analysis-grid">
      <div class="analysis-kv"><div class="k">決策階段</div><div class="v">${stageLabel(d.decision_stage)}</div></div>
      <div class="analysis-kv"><div class="k">現價</div><div class="v">${fmt(d.price ?? q.last)}</div></div>
      <div class="analysis-kv"><div class="k">多方觸發</div><div class="v up">${fmt(longTrigger)}</div></div>
      <div class="analysis-kv"><div class="k">空方觸發</div><div class="v down">${fmt(shortTrigger)}</div></div>
      <div class="analysis-kv"><div class="k">3分動能</div><div class="v">${signed(m.momentum_3m_points)}</div></div>
      <div class="analysis-kv"><div class="k">6分動能</div><div class="v">${signed(m.momentum_6m_points)}</div></div>
      <div class="analysis-kv"><div class="k">9分動能</div><div class="v">${signed(m.momentum_9m_points)}</div></div>
      <div class="analysis-kv"><div class="k">量能倍率</div><div class="v">${fmt(m.volume_ratio)}</div></div>
    </div>

    <div class="analysis-reason"><b>為什麼：</b>${analysisReason(d)}</div>
    <div style="padding:8px 10px;margin-top:8px;border:1px solid #2f4960;background:#081722;color:#9fd8ef;font-size:12px">
      ${backendCheck(local)}。快速層直接使用畫面同一批真實3分K，不另外重抓資料；完整校驗在背景進行，不阻塞視窗。
    </div>
    <div class="analysis-warning">資料不足、來源不明或盤中資料超過5分鐘時，不產生即時交易判定。</div>
  `;
}

renderAnalysisModal = function(){
  try{
    const bars=(typeof currentBars!=='undefined'?currentBars:[])||[];
    if(bars.length>=1) return renderFast();
  }catch(e){
    console.error('fast-analysis',e);
  }
  return originalRenderAnalysisModal();
};

if(originalLoadBars){
  loadBars = async function(...args){
    const out=await originalLoadBars(...args);
    if(qs('#analysisOverlay')?.classList.contains('show')){
      try{renderAnalysisModal()}catch(e){}
    }
    return out;
  };
}

const foot=qs('#analysisOverlay .analysis-foot span');
if(foot) foot.textContent='快速層直接用目前真實3分K計算；完整校驗背景執行。資料不足、過舊或來源不明時不硬下結論。';

})();

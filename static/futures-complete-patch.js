(() => {
'use strict';

const VER='20260906-complete-1';
const style=document.createElement('style');
style.id='complete-data-style';
style.textContent=`
#fullDataStatus{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;padding:7px;background:#071014;border-bottom:1px solid #26343a}
#fullDataStatus .fdc{border:1px solid #2b3a40;padding:6px 3px;text-align:center;font-size:11px;line-height:1.35}
#fullDataStatus .fdc b{display:block;font-size:14px;margin-top:2px}
#fullDataStatus .ok{color:#7ee2a8;border-color:#17643f}
#fullDataStatus .no{color:#fbbf24;border-color:#77581b}
#fullDataStatus .muted{color:#9ba6aa}
.mitake-periods{grid-template-columns:repeat(7,minmax(58px,1fr));overflow-x:auto}
.mitake-period .state strong{display:block;font-size:11px;margin-top:2px;opacity:.9}
.profile-summary{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-bottom:7px}
.profile-summary>div{border:1px solid #2a3940;padding:6px;text-align:center;font-size:11px}
.profile-summary b{display:block;font-size:15px;margin-top:2px;color:#fff}
.profile-exact{color:#7ee2a8;font-size:11px;margin:4px 0 8px}
.profile-est{color:#fbbf24;font-size:11px;margin:4px 0 8px}
.pv-row.poc{outline:1px solid #f0b429;background:#171204}
.tf-source{font-size:10px;color:#9fd8ef;margin-top:3px}
@media(max-width:520px){#fullDataStatus{grid-template-columns:repeat(4,1fr)}.mitake-periods{grid-template-columns:repeat(7,70px)}}
`;
document.head.appendChild(style);

const g=s=>document.querySelector(s);
const ga=s=>[...document.querySelectorAll(s)];
const nn=v=>{const x=Number(v);return Number.isFinite(x)?x:null};
const f=(v,d=0)=>nn(v)==null?'—':Number(v).toLocaleString('zh-TW',{maximumFractionDigits:d});

function ensureStatus(){
  if(g('#fullDataStatus'))return g('#fullDataStatus');
  const host=g('#dataCompleteness') || g('#intradayStatus');
  if(!host)return null;
  const x=document.createElement('div');
  x.id='fullDataStatus';
  host.insertAdjacentElement('afterend',x);
  return x;
}

function sourceShort(s){
  s=String(s||'');
  if(s.includes('official_tick_1m'))return '期交所逐筆1分';
  if(s.includes('getChartDataTick'))return '期交所MIS';
  if(s.includes('yahoo'))return 'Yahoo備援';
  if(s.includes('persistent'))return '已存1分';
  if(s.includes('local_archive'))return '歷史3分';
  return s||'—';
}

async function updateDiagnostics(){
  const box=ensureStatus(); if(!box || typeof PRODUCTS==='undefined')return;
  const m=PRODUCTS[current];
  try{
    const j=await jsonFetch(`/api/blackbox/futures/diagnostics?product=${encodeURIComponent(m.product)}&session=${session}&v=${VER}`);
    const cells=[
      ['即時',j.quote?.ok,j.quote?.source?sourceShort(j.quote.source):'—'],
      ['五檔',j.depth?.complete,`${j.depth?.count||0}/5`],
      ['1分',j.bars?.count_1m>0,`${j.bars?.count_1m||0}筆`],
      ['3分',j.bars?.count_3m>0,`${j.bars?.count_3m||0}筆`],
      ['5分',j.bars?.count_5m>0,`${j.bars?.count_5m||0}筆`],
      ['法人',j.institutional?.ok,j.institutional?.date||'—'],
      ['保證金',j.margin?.ok,j.margin?.date||'—'],
      ['P/C',j.put_call_ratio?.ok,j.put_call_ratio?.date||'—'],
    ];
    box.innerHTML=cells.map(([k,ok,s])=>`<div class="fdc ${ok?'ok':'no'}">${k}<b>${ok?'✓':'—'}</b><span>${s}</span></div>`).join('');
  }catch(e){
    box.innerHTML='<div class="fdc no" style="grid-column:1/-1">完整度檢查失敗</div>';
  }
}

function strictBarsFor(min){
  if(typeof current1mBars==='undefined'||typeof currentBars==='undefined')return [];
  if(min===1)return current1mBars.slice();
  if(min===3)return currentBars.slice();
  if(min===1440)return (typeof currentDailyBars!=='undefined'?currentDailyBars:[]).slice();
  if(!current1mBars.length)return [];
  return aggregateBarsByIndex(current1mBars,min,1);
}

function avg(a,n){
  if(!a.length||a.length<n)return null;
  const x=a.slice(-n).map(v=>Number(v)).filter(Number.isFinite);
  return x.length===n?x.reduce((s,v)=>s+v,0)/n:null;
}

function tfState(bars){
  const good=(bars||[]).filter(b=>Number.isFinite(Number(b.close)));
  if(good.length<5)return {label:'無資料',dir:'flat',score:0,last:null,ma5:null,ma10:null,ma20:null,change:null,volumeRatio:null};
  const closes=good.map(b=>Number(b.close));
  const last=closes.at(-1), prev=closes[Math.max(0,closes.length-4)];
  const ma5=avg(closes,5), ma10=avg(closes,10), ma20=avg(closes,20);
  const prev5=closes.length>=6?closes.slice(-6,-1).reduce((a,b)=>a+b,0)/5:null;
  let score=0;
  const vote=(cond)=>{if(cond===true)score++;else if(cond===false)score--;};
  vote(last>prev); vote(ma5!=null?last>ma5:null); vote(ma10!=null?last>ma10:null);
  vote(ma5!=null&&ma10!=null?ma5>ma10:null); vote(ma10!=null&&ma20!=null?ma10>ma20:null);
  vote(ma5!=null&&prev5!=null?ma5>prev5:null);
  let label='中性',dir='flat';
  if(score>=4){label='強多';dir='up'}
  else if(score>=2){label='微多';dir='up'}
  else if(score<=-4){label='強空';dir='down'}
  else if(score<=-2){label='微空';dir='down'}
  const change=prev?((last/prev)-1)*100:null;
  const vols=good.map(b=>Number(b.volume)||0).filter(v=>v>0);
  let volumeRatio=null;
  if(vols.length>=6){
    const cur=vols.at(-1);
    const base=vols.slice(-6,-1).reduce((a,b)=>a+b,0)/5;
    if(base>0)volumeRatio=cur/base;
  }
  return {label,dir,score,last,ma5,ma10,ma20,change,volumeRatio};
}

function renderCompleteTrend(){
  const pb=g('#trendPeriods'), mg=g('#trendMaGrid'), tg=g('#trendTechGrid');
  if(!pb||!mg||!tg)return;
  const periods=[1,3,5,15,30,60,1440];
  const states=periods.map(min=>[min,tfState(strictBarsFor(min))]);
  pb.innerHTML=states.map(([min,s])=>{
    const tf=min===1440?'日':`${min}分`;
    const cnt=strictBarsFor(min).length;
    return `<div class="mitake-period"><div class="tf">${tf}</div><div class="state ${s.dir}">${s.label}<strong>${cnt?`${cnt}根`:'0根'}</strong></div></div>`;
  }).join('');

  const base=states.find(x=>x[0]===3)?.[1] || tfState([]);
  const mas=[['MA5',base.ma5],['MA10',base.ma10],['MA20',base.ma20]];
  const bull=mas.filter(([,v])=>v!=null&&base.last!=null&&base.last>v).length;
  const bear=mas.filter(([,v])=>v!=null&&base.last!=null&&base.last<v).length;
  const overall=bull>bear?'偏多':bear>bull?'偏空':'中性';
  const o=g('#trendMaOverall');
  if(o){o.textContent=`${overall}｜多${bull} 空${bear}`;o.style.background=bull>bear?'#7b0000':bear>bull?'#075f19':'#4b5563'}
  mg.innerHTML=mas.map(([k,v])=>{
    const dir=v==null||base.last==null?'—':base.last>v?'偏多':'偏空';
    const cls=dir==='偏多'?'up':dir==='偏空'?'down':'';
    return `<div class="mitake-cell"><div class="k">${k}</div><div class="v">${f(v,1)}</div><div class="s ${cls}">${dir}</div></div>`;
  }).join('');

  const up=states.filter(([,s])=>s.dir==='up').length;
  const down=states.filter(([,s])=>s.dir==='down').length;
  const to=g('#trendTechOverall');
  if(to){to.textContent=`多 ${up}｜空 ${down}`;to.style.background=up>down?'#7b0000':down>up?'#075f19':'#4b5563'}
  const s1=states.find(x=>x[0]===1)?.[1],s3=base,s5=states.find(x=>x[0]===5)?.[1];
  const tech=[
    ['1分變化',s1?.change==null?'—':`${s1.change>0?'+':''}${f(s1.change,2)}%`],
    ['3分變化',s3?.change==null?'—':`${s3.change>0?'+':''}${f(s3.change,2)}%`],
    ['5分變化',s5?.change==null?'—':`${s5.change>0?'+':''}${f(s5.change,2)}%`],
    ['3分量比',s3?.volumeRatio==null?'—':`${f(s3.volumeRatio,2)}x`],
    ['現價',base.last==null?'—':f(base.last,0)],
    ['資料',`1m ${current1mBars?.length||0}`],
  ];
  tg.innerHTML=tech.map(([k,v])=>`<div class="mitake-cell"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');
}

async function renderExactPriceVolume(){
  const targets=[g('#detailPriceVolume'),g('#trendPriceVolume')].filter(Boolean);
  if(!targets.length||typeof PRODUCTS==='undefined')return;
  const m=PRODUCTS[current];
  let td=typeof oneMinTradingDate!=='undefined'?oneMinTradingDate:null;
  try{
    const url=`/api/blackbox/futures/price-volume?product=${encodeURIComponent(m.product)}&session=${session}${td?`&trading_date=${encodeURIComponent(td)}`:''}&limit=120&v=${VER}`;
    const j=await jsonFetch(url);
    const rows=(j.rows||[]).filter(x=>nn(x.price)!=null&&nn(x.volume)!=null&&Number(x.volume)>0);
    if(!rows.length)throw new Error('empty');
    const mx=Math.max(...rows.map(x=>Number(x.volume)),1);
    const summary=`<div class="profile-summary">
      <div>價位數<b>${rows.length}</b></div>
      <div>總量<b>${f(j.total_volume,0)}</b></div>
      <div>最大量價<b>${f(j.poc_price,0)}</b></div>
    </div>`;
    const note=`<div class="${j.exact?'profile-exact':'profile-est'}">${j.exact?'✓ 期交所官方逐筆精確分價':'⚠ 盤中1分K估算分價'}｜${j.trading_date||'—'} ${j.session==='night'?'夜盤':'日盤'}</div>`;
    const h=summary+note+rows.map(x=>`<div class="pv-row ${Number(x.price)===Number(j.poc_price)?'poc':''}"><span class="pv-price">${f(x.price,0)}</span><span class="pv-bar"><i style="width:${Math.max(2,Number(x.volume)/mx*100)}%"></i></span><span class="pv-qty">${f(x.volume,0)}</span></div>`).join('');
    targets.forEach(t=>t.innerHTML=h);
  }catch(e){
    // 保留原本 fdPrice 的誠實備援，不用假資料覆蓋。
  }
}

function refreshComplete(){
  updateDiagnostics();
  renderCompleteTrend();
  renderExactPriceVolume();
}

const oldGetTechBars=window.getTechBars;
if(typeof getTechBars==='function'){
  // 明確規則：5分等非3倍數週期必須由真實1分聚合；沒有1分就顯示空白，不拿3分硬湊。
  window.getTechBars=function(){
    if(techMinutes===1440)return currentDailyBars.slice();
    if(techMinutes===1)return current1mBars.slice();
    if(techMinutes===3)return currentBars.slice();
    return current1mBars.length?aggregateBarsByIndex(current1mBars,techMinutes,1):[];
  };
}

document.addEventListener('click',e=>{
  if(e.target.closest('.primary-tab,.tech-timeframes button,.detail-subtab,.trend-lower-tab,#sessionDay,#sessionNight,#prevContract,#nextContract,#contractMenu button')){
    setTimeout(refreshComplete,120);
  }
});

setTimeout(refreshComplete,600);
setInterval(()=>{if(!document.hidden)refreshComplete()},15000);
})();
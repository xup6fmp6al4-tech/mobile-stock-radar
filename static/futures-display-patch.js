(() => {
  'use strict';

  const STYLE_ID = 'futures-numeric-patch-style';
  if (!document.getElementById(STYLE_ID)) {
    const s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = `
      .trend-number-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:#26343a;border-bottom:1px solid #26343a}
      .trend-number-strip .ncell{background:#070a0c;padding:7px 5px;text-align:center;min-width:0}
      .trend-number-strip .k{display:block;color:#9ba6aa;font-size:11px;line-height:1.1}
      .trend-number-strip .v{display:block;color:#fff;font-size:17px;font-weight:900;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .trend-number-strip .v.up{color:#ff2d2d}.trend-number-strip .v.down{color:#24d642}.trend-number-strip .v.yellow{color:#e0dc35}
      .chart-card.numeric-chart{padding:0;background:#000}
      .chart-card.numeric-chart canvas{border-left:0;border-right:0}
      .depth-source-note{padding:6px 10px;background:#16110a;color:#fbbf24;font-size:12px;border-top:1px solid #4b3a16}
      @media(max-width:520px){.trend-number-strip .v{font-size:15px}.trend-number-strip .k{font-size:10px}}
    `;
    document.head.appendChild(s);
  }

  function num(v){
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  function textNum(v, digits=0){
    const n = num(v);
    if(n === null) return '—';
    return n.toLocaleString('zh-TW',{minimumFractionDigits:digits,maximumFractionDigits:digits});
  }
  function barTime(b){
    const t = num(b?.ts_utc);
    if(t === null) return '';
    return new Date(t*1000).toLocaleTimeString('zh-TW',{timeZone:'Asia/Taipei',hour:'2-digit',minute:'2-digit',hour12:false});
  }
  function priceClass(v, ref){
    const a=num(v), r=num(ref);
    if(a===null||r===null) return '';
    return a>r?'up':a<r?'down':'';
  }

  function ensureNumericStrip(){
    const view=document.querySelector('#view-trend');
    if(!view) return null;
    let strip=document.querySelector('#trendNumericStrip');
    if(!strip){
      strip=document.createElement('div');
      strip.id='trendNumericStrip';
      strip.className='trend-number-strip';
      const meta=view.querySelector('.chart-meta');
      if(meta) meta.insertAdjacentElement('afterend',strip); else view.prepend(strip);
    }
    const cards=view.querySelectorAll('.chart-card');
    cards.forEach(c=>c.classList.add('numeric-chart'));
    return strip;
  }

  function renderNumericStrip(){
    const strip=ensureNumericStrip();
    if(!strip) return;
    const q=(typeof currentQuote!=='undefined' && currentQuote) ? currentQuote : {};
    const ref=num(q.prev_close);
    const fields=[
      ['現',q.last,priceClass(q.last,ref)],
      ['高',q.high,'up'],
      ['低',q.low,'down'],
      ['開',q.open,priceClass(q.open,ref)],
      ['參',q.prev_close,'yellow'],
      ['買一',q.bid,priceClass(q.bid,ref)],
      ['賣一',q.ask,priceClass(q.ask,ref)],
      ['價差',(num(q.ask)!==null&&num(q.bid)!==null)?num(q.ask)-num(q.bid):null,'yellow']
    ];
    strip.innerHTML=fields.map(([k,v,cls])=>`<div class="ncell"><span class="k">${k}</span><span class="v ${cls||''}">${textNum(v)}</span></div>`).join('');
  }

  function niceStep(span, target=5){
    if(!Number.isFinite(span)||span<=0) return 1;
    const raw=span/target;
    const pow=Math.pow(10,Math.floor(Math.log10(raw)));
    const m=raw/pow;
    const nice=m<=1?1:m<=2?2:m<=5?5:10;
    return nice*pow;
  }
  function makeScale(bars, q={}){
    const values=[];
    (bars||[]).forEach(b=>{
      [b.high,b.low,b.open,b.close].forEach(v=>{const n=num(v); if(n!==null) values.push(n);});
    });
    [q.last,q.high,q.low,q.open,q.prev_close,q.bid,q.ask].forEach(v=>{const n=num(v);if(n!==null)values.push(n);});
    if(!values.length) return null;
    let lo=Math.min(...values), hi=Math.max(...values);
    if(hi===lo){hi+=1;lo-=1;}
    const pad=Math.max((hi-lo)*0.08,1);
    lo-=pad; hi+=pad;
    const step=niceStep(hi-lo,5);
    const min=Math.floor(lo/step)*step;
    const max=Math.ceil(hi/step)*step;
    return {min,max,step};
  }
  function plotBox(w,h){
    return {left:62,right:10,top:15,bottom:30,width:Math.max(10,w-72),height:Math.max(10,h-45)};
  }
  function mapY(v,scale,box){
    return box.top + (scale.max-Number(v))/(scale.max-scale.min)*box.height;
  }
  function mapX(i,count,box){
    return box.left + (count<=1?0:i/(count-1)*box.width);
  }
  function labelColor(v,ref){
    const a=num(v),r=num(ref);
    if(a===null||r===null) return '#f4f4f4';
    return a>r?'#ff2d2d':a<r?'#24d642':'#e0dc35';
  }
  function drawAxes(ctx,w,h,bars,scale,q){
    const box=plotBox(w,h);
    ctx.clearRect(0,0,w,h);
    ctx.fillStyle='#000';ctx.fillRect(0,0,w,h);
    ctx.lineWidth=1;
    ctx.font='12px -apple-system,BlinkMacSystemFont,"Noto Sans TC",sans-serif';
    ctx.textBaseline='middle';
    const ref=num(q?.prev_close);

    let tick=scale.min;
    let guard=0;
    while(tick<=scale.max+scale.step*0.1 && guard++<20){
      const y=mapY(tick,scale,box);
      ctx.strokeStyle='#202a30';ctx.beginPath();ctx.moveTo(box.left,y);ctx.lineTo(w-box.right,y);ctx.stroke();
      ctx.fillStyle=labelColor(tick,ref);ctx.textAlign='right';ctx.fillText(textNum(tick),box.left-6,y);
      tick+=scale.step;
    }

    const xTicks=Math.min(6,Math.max(2,bars.length));
    ctx.textBaseline='top';
    for(let k=0;k<xTicks;k++){
      const i=Math.round(k*(bars.length-1)/Math.max(1,xTicks-1));
      const x=mapX(i,bars.length,box);
      ctx.strokeStyle='#182127';ctx.beginPath();ctx.moveTo(x,box.top);ctx.lineTo(x,box.top+box.height);ctx.stroke();
      ctx.fillStyle='#d9dee1';
      ctx.textAlign=k===0?'left':k===xTicks-1?'right':'center';
      ctx.fillText(barTime(bars[i]),x,box.top+box.height+7);
    }
    return box;
  }
  function drawPriceLine(ctx,w,scale,box,v,label,color,dashed=false){
    const n=num(v); if(n===null||n<scale.min||n>scale.max) return;
    const y=mapY(n,scale,box);
    ctx.save();
    ctx.setLineDash(dashed?[5,4]:[]);ctx.strokeStyle=color;ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(box.left,y);ctx.lineTo(w-box.right,y);ctx.stroke();
    ctx.setLineDash([]);ctx.font='bold 12px -apple-system,BlinkMacSystemFont,"Noto Sans TC",sans-serif';
    const labelText=`${label} ${textNum(n)}`;
    const tw=ctx.measureText(labelText).width;
    ctx.fillStyle='rgba(0,0,0,.78)';ctx.fillRect(w-box.right-tw-8,y-10,tw+8,20);
    ctx.fillStyle=color;ctx.textAlign='right';ctx.textBaseline='middle';ctx.fillText(labelText,w-box.right-4,y);
    ctx.restore();
  }
  function drawHiLo(ctx,bars,scale,box){
    if(!bars.length) return;
    let hi=-Infinity,lo=Infinity,hiI=0,loI=0;
    bars.forEach((b,i)=>{
      const h=num(b.high),l=num(b.low);
      if(h!==null&&h>hi){hi=h;hiI=i;}
      if(l!==null&&l<lo){lo=l;loI=i;}
    });
    ctx.font='bold 12px -apple-system,BlinkMacSystemFont,"Noto Sans TC",sans-serif';ctx.textBaseline='middle';
    if(Number.isFinite(hi)){
      const x=mapX(hiI,bars.length,box),y=mapY(hi,scale,box);
      ctx.fillStyle='#ff4b4b';ctx.textAlign=x>box.left+box.width*.7?'right':'left';ctx.fillText(`高 ${textNum(hi)}`,x+(ctx.textAlign==='left'?5:-5),Math.max(box.top+8,y-10));
    }
    if(Number.isFinite(lo)){
      const x=mapX(loI,bars.length,box),y=mapY(lo,scale,box);
      ctx.fillStyle='#42e35c';ctx.textAlign=x>box.left+box.width*.7?'right':'left';ctx.fillText(`低 ${textNum(lo)}`,x+(ctx.textAlign==='left'?5:-5),Math.min(box.top+box.height-8,y+10));
    }
  }

  function patchedDrawTrend(){
    const c=document.querySelector('#trendChart'); if(!c||!c.offsetParent) return;
    const bars=(typeof currentBars!=='undefined'?currentBars:[]).filter(b=>num(b.close)!==null);
    const q=(typeof currentQuote!=='undefined'&&currentQuote)?currentQuote:{};
    const {ctx,w,h}=fitCanvas(c);
    if(!bars.length){ctx.clearRect(0,0,w,h);return;}
    const scale=makeScale(bars,q); if(!scale) return;
    const box=drawAxes(ctx,w,h,bars,scale,q);

    const grad=ctx.createLinearGradient(0,box.top,0,box.top+box.height);
    grad.addColorStop(0,'rgba(226,39,45,.35)');grad.addColorStop(1,'rgba(226,39,45,.03)');
    ctx.beginPath();
    bars.forEach((b,i)=>{const x=mapX(i,bars.length,box),y=mapY(b.close,scale,box);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
    ctx.lineTo(mapX(bars.length-1,bars.length,box),box.top+box.height);ctx.lineTo(box.left,box.top+box.height);ctx.closePath();ctx.fillStyle=grad;ctx.fill();
    ctx.strokeStyle='#e2272d';ctx.lineWidth=2;ctx.beginPath();
    bars.forEach((b,i)=>{const x=mapX(i,bars.length,box),y=mapY(b.close,scale,box);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();

    drawPriceLine(ctx,w,scale,box,q.prev_close,'參考','#e0dc35',true);
    drawPriceLine(ctx,w,scale,box,q.last,'現價',priceClass(q.last,q.prev_close)==='down'?'#24d642':'#ff2d2d',false);
    drawHiLo(ctx,bars,scale,box);
    renderNumericStrip();
  }

  function patchedDrawK(){
    const c=document.querySelector('#kChart'); if(!c||!c.offsetParent) return;
    const bars=(typeof getTechBars==='function'?getTechBars():[]).filter(b=>num(b.close)!==null);
    const q=(typeof currentQuote!=='undefined'&&currentQuote)?currentQuote:{};
    const {ctx,w,h}=fitCanvas(c);
    if(!bars.length){ctx.clearRect(0,0,w,h);return;}
    const scale=makeScale(bars,q); if(!scale) return;
    const box=drawAxes(ctx,w,h,bars,scale,q);
    const step=box.width/Math.max(1,bars.length);
    bars.forEach((b,i)=>{
      const x=box.left+i*step+step/2,yo=mapY(b.open,scale,box),yc=mapY(b.close,scale,box),yh=mapY(b.high,scale,box),yl=mapY(b.low,scale,box);
      const up=Number(b.close)>=Number(b.open);
      ctx.strokeStyle=up?'#ef2727':'#24dc3d';ctx.fillStyle=ctx.strokeStyle;ctx.lineWidth=1;
      ctx.beginPath();ctx.moveTo(x,yh);ctx.lineTo(x,yl);ctx.stroke();
      ctx.fillRect(x-step*.28,Math.min(yo,yc),Math.max(1,step*.56),Math.max(2,Math.abs(yc-yo)));
    });
    const specs=[[5,'#f1d23d'],[10,'#4cc3e7'],[20,'#a855f7']];
    specs.forEach(([n,color])=>{
      ctx.strokeStyle=color;ctx.lineWidth=1.5;ctx.beginPath();let started=false;
      bars.forEach((b,i)=>{const v=rollingSma(bars,n,i);if(v===null)return;const x=box.left+i*step+step/2,y=mapY(v,scale,box);if(!started){ctx.moveTo(x,y);started=true}else ctx.lineTo(x,y)});ctx.stroke();
    });
    drawPriceLine(ctx,w,scale,box,q.prev_close,'參考','#e0dc35',true);
    drawPriceLine(ctx,w,scale,box,q.last,'現價',priceClass(q.last,q.prev_close)==='down'?'#24d642':'#ff2d2d',false);
    drawHiLo(ctx,bars,scale,box);
  }

  function patchedDrawVolume(id,barsArg){
    const c=document.querySelector(id); if(!c||!c.offsetParent) return;
    const bars=(barsArg||[]).filter(Boolean);
    const {ctx,w,h}=fitCanvas(c);ctx.clearRect(0,0,w,h);ctx.fillStyle='#000';ctx.fillRect(0,0,w,h);
    if(!bars.length) return;
    const box={left:62,right:10,top:10,bottom:26,width:Math.max(10,w-72),height:Math.max(10,h-36)};
    const max=Math.max(1,...bars.map(b=>Number(b.volume)||0));
    ctx.font='12px -apple-system,BlinkMacSystemFont,"Noto Sans TC",sans-serif';ctx.textBaseline='middle';ctx.textAlign='right';ctx.fillStyle='#f1e532';
    [0,.5,1].forEach(f=>{const y=box.top+box.height*(1-f);ctx.strokeStyle='#202a30';ctx.beginPath();ctx.moveTo(box.left,y);ctx.lineTo(w-box.right,y);ctx.stroke();ctx.fillStyle='#f1e532';ctx.fillText(textNum(max*f),box.left-6,y);});
    const bw=Math.max(1,box.width/bars.length*.72);
    bars.forEach((b,i)=>{const x=box.left+i/bars.length*box.width;const v=(Number(b.volume)||0)/max;ctx.fillStyle=Number(b.close)>=Number(b.open)?'#e92525':'#24dc3d';ctx.fillRect(x,box.top+box.height*(1-v),bw,box.height*v);});
    const idxs=[0,Math.floor((bars.length-1)/2),bars.length-1];
    ctx.fillStyle='#d9dee1';ctx.textBaseline='top';
    idxs.forEach((i,k)=>{const x=box.left+i/Math.max(1,bars.length-1)*box.width;ctx.textAlign=k===0?'left':k===2?'right':'center';ctx.fillText(barTime(bars[i]),x,box.top+box.height+6);});
  }

  try{
    drawTrend = patchedDrawTrend;
    drawK = patchedDrawK;
    drawVolume = patchedDrawVolume;
    drawAllCharts = function(){
      patchedDrawTrend();
      patchedDrawVolume('#trendVol',typeof currentBars!=='undefined'?currentBars:[]);
      patchedDrawK();
      patchedDrawVolume('#techVol',typeof getTechBars==='function'?getTechBars():[]);
      renderNumericStrip();
    };
  }catch(e){console.error('numeric chart patch install failed',e);}

  ensureNumericStrip();
  renderNumericStrip();
  setTimeout(()=>{try{drawAllCharts();}catch(e){}},80);
  setInterval(()=>{renderNumericStrip();},1000);
})();

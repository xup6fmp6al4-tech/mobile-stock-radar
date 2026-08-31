import express from "express";

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.static("public"));
app.use(express.json());

const WATCHLIST = [
  // Taiwan
  ["2330.TW","台積電","半導體"],["6770.TW","力積電","晶圓代工/記憶體"],
  ["2609.TW","陽明","航運"],["8299.TWO","群聯","NAND/控制晶片"],
  ["2630.TW","亞航","航太"],["2634.TW","漢翔","航太/軍工"],
  ["8105.TWO","凌巨","面板"],["3189.TWO","景碩","IC載板"],
  ["2454.TW","聯發科","IC設計"],["2308.TW","台達電","電源/AI"],
  ["3034.TW","聯詠","IC設計"],["3035.TW","智原","ASIC"],
  ["2409.TW","友達","面板"],["1605.TW","華新","電線電纜"],
  ["2492.TW","華新科","被動元件"],
  ["00631L.TW","元大台灣50正2","槓桿ETF"],
  ["00685L.TW","群益臺灣加權正2","槓桿ETF"],
  ["00675L.TW","富邦臺灣加權正2","槓桿ETF"],
  ["00632R.TW","元大台灣50反1","反向ETF"],
  ["006201.TW","元大富櫃50","ETF"],["006208.TW","富邦台50","ETF"],
  ["00715L.TW","期街口布蘭特正2","原油槓桿ETF"],
  // US
  ["NVDA","NVIDIA","AI/半導體"],["MU","Micron","記憶體"],
  ["TSLA","Tesla","EV/AI"],["QQQ","Invesco QQQ","Nasdaq ETF"],
  ["MRNA","Moderna","生技"],["SNDK","SanDisk","NAND/儲存"],
  ["SMCI","Super Micro Computer","AI伺服器"],["CRWD","CrowdStrike","資安"],
  ["CIFR","Cipher Digital","HPC/比特幣基礎設施"],["GENB","Generate Biomedicines","AI生技"],
  ["RPD","Rapid7","資安"],["DKS","DICK'S Sporting Goods","零售"],
  ["LULU","Lululemon","運動服飾"],["NKE","Nike","運動服飾"],
  ["PCG","PG&E","公用事業"],["QMCO","Quantum","資料儲存"],
  ["SENS","Senseonics","醫療科技"],["AMLX","Amylyx","生技"]
];

async function yahooChart(symbol, range="5d", interval="5m") {
  const u = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?range=${range}&interval=${interval}&includePrePost=true&events=div%2Csplits`;
  const r = await fetch(u, {headers: {"User-Agent":"Mozilla/5.0"}});
  if (!r.ok) throw new Error(`Yahoo ${r.status}`);
  const j = await r.json();
  const result = j?.chart?.result?.[0];
  if (!result) throw new Error("No data");
  return result;
}

function pct(a,b){ return (a && b) ? (a/b-1)*100 : null; }

function analyze(result, metaRow) {
  const m = result.meta || {};
  const q = result.indicators?.quote?.[0] || {};
  const ts = result.timestamp || [];
  const closes = q.close || [], highs = q.high || [], lows = q.low || [], vols = q.volume || [];
  const valid = ts.map((t,i)=>({t, c:closes[i], h:highs[i], l:lows[i], v:vols[i]}))
    .filter(x=>x.c!=null);

  const last = valid.at(-1) || {};
  const prev = valid.length>1 ? valid.at(-2) : {};
  const look = n => valid.length>n ? pct(last.c, valid.at(-(n+1)).c) : null;

  let pv=0, vv=0;
  for (const x of valid.slice(-78)) {
    if (x.v && x.c) { pv += x.c*x.v; vv += x.v; }
  }
  const vwap = vv ? pv/vv : null;
  const dayHigh = Math.max(...valid.map(x=>x.h ?? -Infinity));
  const dayLow = Math.min(...valid.map(x=>x.l ?? Infinity));
  const amp = Number.isFinite(dayHigh) && Number.isFinite(dayLow) && dayLow>0 ? (dayHigh/dayLow-1)*100 : null;

  return {
    symbol: metaRow[0], name: metaRow[1], sector: metaRow[2],
    price: last.c ?? m.regularMarketPrice ?? null,
    previousClose: m.chartPreviousClose ?? m.previousClose ?? null,
    dayChangePct: pct(last.c, m.chartPreviousClose ?? m.previousClose),
    high: Number.isFinite(dayHigh)?dayHigh:null,
    low: Number.isFinite(dayLow)?dayLow:null,
    theoreticalAmplitudePct: amp,
    volume: valid.reduce((s,x)=>s+(x.v||0),0),
    vwap,
    aboveVwap: vwap && last.c ? last.c >= vwap : null,
    momentum: {m5: look(1), m15: look(3), m30: look(6), m60: look(12)},
    marketState: m.marketState || null,
    currency: m.currency || null,
    exchange: m.exchangeName || null,
    updatedAt: last.t ? new Date(last.t*1000).toISOString() : new Date().toISOString()
  };
}

app.get("/health", (req,res)=>res.json({ok:true, version:"0.9.0", time:new Date().toISOString()}));

app.get("/api/radar", async (req,res)=>{
  const symbols = req.query.symbols
    ? String(req.query.symbols).split(",").map(s=>s.trim()).filter(Boolean)
    : WATCHLIST.map(x=>x[0]);
  const rows = WATCHLIST.filter(x=>symbols.includes(x[0]));
  const out = [];
  for (const row of rows) {
    try {
      const result = await yahooChart(row[0], "5d", "5m");
      out.push(analyze(result,row));
    } catch (e) {
      out.push({symbol:row[0], name:row[1], sector:row[2], error:String(e.message||e)});
    }
  }
  res.set("Cache-Control","no-store");
  res.json({version:"0.9.0", generatedAt:new Date().toISOString(), count:out.length, data:out});
});

app.get("/api/all", (req,res)=>res.redirect(307,"/api/radar"));

app.get("*",(req,res)=>res.sendFile(process.cwd()+"/public/index.html"));

app.listen(PORT, ()=>console.log(`Mobile Stock Radar v0.9 on ${PORT}`));

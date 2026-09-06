const CACHE='stock-radar-v0.9-intraday-bars';
const PATCH='/static/futures-display-patch.js';
const SHELL=['/','/manifest.webmanifest','/static/icon-192.png','/static/icon-512.png',PATCH];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).catch(()=>{}));self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));self.clients.claim();});
async function injectPatch(request){
 try{
  const r=await fetch(request,{cache:'no-store'});if(!r.ok)return r;
  const type=r.headers.get('content-type')||'';if(!type.includes('text/html'))return r;
  let html=await r.text();
  if(!html.includes('futures-display-patch.js'))html=html.replace('</body>',`<script src="${PATCH}?v=20260906-intraday-1"></script></body>`);
  const h=new Headers(r.headers);h.delete('content-length');h.set('cache-control','no-store');
  return new Response(html,{status:r.status,statusText:r.statusText,headers:h});
 }catch(err){return (await caches.match(request))||Response.error();}
}
self.addEventListener('fetch',e=>{
 const u=new URL(e.request.url);if(u.origin!==location.origin||u.pathname.startsWith('/api/'))return;
 if(e.request.mode==='navigate'){e.respondWith(injectPatch(e.request));return;}
 e.respondWith(fetch(e.request,{cache:'no-store'}).then(r=>{const copy=r.clone();caches.open(CACHE).then(c=>c.put(e.request,copy)).catch(()=>{});return r;}).catch(()=>caches.match(e.request)));
});

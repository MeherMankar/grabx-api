/**
 * GrabX CDN Proxy — Cloudflare Worker
 *
 * Required Worker secrets/variables (set in CF dashboard or via wrangler):
 *   API_BASE_URL  — URL of your API deployment (Koyeb/Render/Railway/etc.)
 *   API_KEY       — same value as on your API deployment
 *
 * Optional:
 *   TERABOX_COOKIE — not used directly by the Worker (Terabox proxied via API)
 */

// ---------------------------------------------------------------------------
// Token verification
// ---------------------------------------------------------------------------

async function verifyToken(cdnUrl, tokenB64, expiryStr, apiKey) {
  apiKey = (apiKey || "").trim();
  if (!apiKey) return true;
  const expiry = parseInt(expiryStr, 10);
  if (isNaN(expiry) || Date.now() / 1000 > expiry) return false;
  const msg  = `${expiry}:${cdnUrl}`;
  const key  = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(apiKey),
    { name: "HMAC", hash: "SHA-256" }, false, ["verify"],
  );
  const padded = tokenB64.replace(/-/g, "+").replace(/_/g, "/");
  const b64    = padded + "=".repeat((4 - (padded.length % 4)) % 4);
  let binary;
  try { binary = atob(b64); } catch { return false; }
  const sigBuf = Uint8Array.from(binary, c => c.charCodeAt(0));
  return crypto.subtle.verify("HMAC", key, sigBuf, new TextEncoder().encode(msg));
}

// ---------------------------------------------------------------------------
// HLS manifest rewriting
// ---------------------------------------------------------------------------

function resolveHlsUri(uri, baseDir, baseOrigin) {
  if (/^https?:\/\//i.test(uri)) return uri;
  if (uri.startsWith("//"))      return "https:" + uri;
  if (uri.startsWith("/"))       return baseOrigin + uri;
  return baseDir + uri;
}

function buildProxyUrl(base, path, cdnUrl, extraParams) {
  const u = new URL(path, base);
  u.searchParams.set("url", cdnUrl);
  for (const [k, v] of Object.entries(extraParams || {})) {
    if (v) u.searchParams.set(k, v);
  }
  return u.toString();
}

function rewriteManifest(text, cdnUrl, workerBase, extraParams) {
  const parsed  = new URL(cdnUrl);
  const baseDir = cdnUrl.slice(0, cdnUrl.lastIndexOf("/") + 1);
  const origin  = parsed.origin;
  return text.split("\n").map(line => {
    const stripped = line.trim();
    if (!stripped) return line;
    if (stripped.startsWith("#")) {
      return line.replace(/URI="([^"]+)"/g, (_, uri) => {
        const abs = resolveHlsUri(uri, baseDir, origin);
        return `URI="${buildProxyUrl(workerBase, "/ph/proxy", abs, extraParams)}"`;
      });
    }
    return buildProxyUrl(workerBase, "/ph/proxy", resolveHlsUri(stripped, baseDir, origin), extraParams);
  }).join("\n");
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function escHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;").replace(/"/g, "&quot;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function rewriteToWorker(proxyUrl, workerOrigin) {
  if (!proxyUrl) return proxyUrl;
  try {
    const u = new URL(proxyUrl);
    const w = new URL(workerOrigin);
    u.protocol = w.protocol;
    u.host     = w.host;
    return u.toString();
  } catch { return proxyUrl; }
}

function errorPage(msg) {
  return new Response(
    `<!DOCTYPE html><html><head><title>Error</title></head>
    <body style="background:#0f0f0f;color:#eee;font-family:sans-serif;padding:40px">
    <h2 style="color:#f55">Could not load video</h2><p>${escHtml(msg)}</p>
    <p><a href="https://github.com/MeherMankar/grabx-api" style="color:#f90">GrabX API</a></p>
    </body></html>`,
    { status: 500, headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}

// ---------------------------------------------------------------------------
// proxyToRender — forward POST to API with cold-start retry
// ---------------------------------------------------------------------------

async function proxyToApi(request, apiKey, apiBase) {
  const url     = new URL(request.url);
  const body    = await request.text();
  const headers = { "Content-Type": "application/json" };
  if (apiKey) headers["X-API-Key"] = apiKey;

  const MAX_ATTEMPTS = 3;
  const RETRY_DELAY  = 5000;
  let lastResp;

  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    try {
      const resp = await fetch(`${apiBase}${url.pathname}${url.search}`, {
        method: request.method, headers, body: body || undefined,
      });
      if (resp.ok || (resp.status >= 400 && resp.status < 500)) {
        const data = await resp.json().catch(() => ({}));
        return new Response(JSON.stringify(data), {
          status: resp.status,
          headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
        });
      }
      lastResp = resp;
    } catch (err) {
      if (attempt < MAX_ATTEMPTS - 1) {
        await new Promise(r => setTimeout(r, RETRY_DELAY));
        continue;
      }
      return new Response(
        JSON.stringify({ status: "error", message: `API unreachable: ${err.message}` }),
        { status: 502, headers: { "Content-Type": "application/json" } },
      );
    }
    if (attempt < MAX_ATTEMPTS - 1) await new Promise(r => setTimeout(r, RETRY_DELAY));
  }
  return new Response(
    JSON.stringify({ status: "error", message: `API returned HTTP ${lastResp?.status}` }),
    { status: lastResp?.status || 502, headers: { "Content-Type": "application/json" } },
  );
}

// ---------------------------------------------------------------------------
// Watch page — fetch qualities from API and serve HLS.js player
// ---------------------------------------------------------------------------

async function serveWatchPage(viewkey, workerOrigin, apiKey, apiBase) {
  try {
    const apiHeaders = { "Content-Type": "application/json" };
    if (apiKey) apiHeaders["X-API-Key"] = apiKey;

    const r = await fetch(`${apiBase}/ph/download`, {
      method: "POST", headers: apiHeaders,
      body: JSON.stringify({ url: `https://www.pornhub.com/view_video.php?viewkey=${viewkey}` }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ message: `HTTP ${r.status}` }));
      return errorPage(err.message || `API returned ${r.status}`);
    }
    const { data } = await r.json();
    const title     = data.title     || "Video";
    const thumbnail = data.thumbnail || "";
    const duration  = data.duration  || "";
    const qualities = data.qualities || [];
    if (!qualities.length) return errorPage("No streams found.");

    const opts = qualities.map(q => ({
      label: `${q.quality}p ${q.format.toUpperCase()}`,
      fmt:   q.format,
      proxy: rewriteToWorker(q.proxy_url,    workerOrigin),
      dl:    rewriteToWorker(q.download_url, workerOrigin),
    }));

    const optionsHtml = opts.map(o =>
      `<option value="${escHtml(o.proxy)}" data-fmt="${o.fmt}" data-dl="${escHtml(o.dl)}">${escHtml(o.label)}</option>`
    ).join("\n");
    const best = opts[0];
    const bestDlJs    = best.dl;
    const bestProxyJs = best.proxy;

    const html = `<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>${escHtml(title)}</title>
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    body{background:#0f0f0f;color:#eee;font-family:system-ui,sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:24px 16px 48px}
    .container{width:100%;max-width:960px}
    h1{font-size:1.15rem;font-weight:600;margin-bottom:14px;color:#fff}
    .player-wrap{width:100%;background:#000;border-radius:8px;overflow:hidden}
    video{width:100%;display:block;max-height:540px;background:#000}
    .controls{display:flex;align-items:center;gap:12px;margin-top:14px;flex-wrap:wrap}
    select{background:#1e1e1e;color:#eee;border:1px solid #444;border-radius:6px;padding:8px 12px;font-size:.9rem;cursor:pointer;flex:1;min-width:120px}
    .btn{display:inline-flex;align-items:center;gap:6px;font-weight:700;font-size:.9rem;padding:9px 18px;border-radius:6px;white-space:nowrap;transition:background .15s;cursor:pointer;border:none}
    .btn-dl{background:#f90;color:#000}.btn-dl:hover{background:#e88600}.btn-dl:disabled{background:#666;cursor:not-allowed}
    .meta{margin-top:10px;font-size:.8rem;color:#666}a{color:#f90}
  </style>
</head><body>
  <div class="container">
    <h1>${escHtml(title)}</h1>
    <div class="player-wrap"><video id="player" controls preload="metadata" poster="${escHtml(thumbnail)}">Your browser does not support HTML5 video.</video></div>
    <div class="controls">
      <select id="qualitySelect">${optionsHtml}</select>
      <button id="dlBtn" class="btn btn-dl">&#8595; Download</button>
    </div>
    <div class="meta">${duration ? `Duration: ${escHtml(duration)} &nbsp;&middot;&nbsp; ` : ""}Powered by <a href="https://github.com/MeherMankar/grabx-api" target="_blank">GrabX API</a></div>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
  <script>
    const video=document.getElementById('player'),sel=document.getElementById('qualitySelect'),dlBtn=document.getElementById('dlBtn');
    let hls=null,currentDlUrl='${bestDlJs}',currentFmt='${best.fmt}';
    function loadSrc(u,fmt,dl){
      currentDlUrl=dl;currentFmt=fmt;
      dlBtn.textContent=(fmt==='hls'||u.includes('.m3u8'))?'📋 Copy Stream URL':'↓ Download';       if(hls){hls.destroy();hls=null;}
      if(fmt==='hls'||u.includes('.m3u8')){
        if(Hls.isSupported()){hls=new Hls({enableWorker:true});hls.loadSource(u);hls.attachMedia(video);hls.on(Hls.Events.MANIFEST_PARSED,()=>video.play().catch(()=>{}));}
        else if(video.canPlayType('application/vnd.apple.mpegurl')){video.src=u;video.play().catch(()=>{});}
      }else{video.src=u;video.load();}
    }
    dlBtn.addEventListener('click',async function(){
      if(currentFmt==='hls'){
        try{await navigator.clipboard.writeText(currentDlUrl);dlBtn.textContent='✓ Copied!';setTimeout(()=>{dlBtn.textContent='📋 Copy Stream URL';},2000);}
        catch(e){prompt('HLS stream URL (open in VLC):',currentDlUrl);}
        return;
      }
      dlBtn.textContent='Preparing...';dlBtn.disabled=true;
      try{
        const resp=await fetch(currentDlUrl);if(!resp.ok)throw new Error('HTTP '+resp.status);
        const blob=await resp.blob(),blobUrl=URL.createObjectURL(blob),a=document.createElement('a');
        a.href=blobUrl;const cd=resp.headers.get('Content-Disposition')||'';
        const m=cd.match(/filename[*]?=["']?([^"';\n]+)/i);a.download=m?m[1].trim():'video.mp4';
        document.body.appendChild(a);a.click();document.body.removeChild(a);setTimeout(()=>URL.revokeObjectURL(blobUrl),10000);
      }catch(e){window.open(currentDlUrl,'_blank');}
      finally{dlBtn.textContent='↓ Download';dlBtn.disabled=false;}
    });
    const first=sel.options[sel.selectedIndex];loadSrc(first.value,first.dataset.fmt,first.dataset.dl);
    sel.addEventListener('change',function(){const o=this.options[this.selectedIndex];loadSrc(o.value,o.dataset.fmt,o.dataset.dl);});
  </script>
</body></html>`;
    return new Response(html, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } });
  } catch (err) { return errorPage(String(err)); }
}

// ---------------------------------------------------------------------------
// Main request handler
// ---------------------------------------------------------------------------

async function handleRequest(request, apiKey, apiBase) {
  const url  = new URL(request.url);
  const path = url.pathname;

  // POST endpoints — proxy to API with retry
  const API_POST_PATHS = ["/download", "/ph/download", "/xv/download", "/xnxx/download", "/xh/download", "/jav/download"];
  if (API_POST_PATHS.includes(path) && request.method === "POST") {
    return await proxyToApi(request, apiKey, apiBase);
  }

  // Watch pages — redirect to API
  const API_WATCH_PATHS = ["/xv/watch", "/xnxx/watch", "/xh/watch", "/jav/watch"];
  if (API_WATCH_PATHS.includes(path)) {
    return Response.redirect(`${apiBase}${path}${url.search}`, 302);
  }

  // PH watch page — served by Worker itself
  if (path.startsWith("/ph/watch/")) {
    const vk = path.replace("/ph/watch/", "").split("?")[0];
    if (vk) return await serveWatchPage(vk, url.origin, apiKey, apiBase);
  }

  // Adult/Terabox proxies — redirect to API (IP-locked CDN)
  if (path === "/adult/proxy" || path === "/proxy") {
    return Response.redirect(`${apiBase}${path}${url.search}`, 302);
  }

  // Proxy paths handled by Worker
  const WORKER_PROXY_PATHS = ["/ph/proxy", "/jav/proxy"];
  if (!WORKER_PROXY_PATHS.includes(path)) {
    return new Response(JSON.stringify({ status: "error", message: "Not found" }),
      { status: 404, headers: { "Content-Type": "application/json" } });
  }

  const cdnUrl      = url.searchParams.get("url") || "";
  const tokenB64    = url.searchParams.get("_t")  || "";
  const expiryStr   = url.searchParams.get("_e")  || "";
  const downloadMode = url.searchParams.get("dl") === "1";
  const viewkey     = url.searchParams.get("vk")  || "";
  const quality     = url.searchParams.get("q")   || "";

  if (!cdnUrl) {
    return new Response(JSON.stringify({ status: "error", message: "'url' param required" }),
      { status: 400, headers: { "Content-Type": "application/json" } });
  }

  if (apiKey) {
    const valid = await verifyToken(cdnUrl, tokenB64, expiryStr, apiKey);
    if (!valid) {
      return new Response(JSON.stringify({ status: "error", message: "Invalid or expired token." }),
        { status: 403, headers: { "Content-Type": "application/json" } });
    }
  }

  const isM3u8 = cdnUrl.includes(".m3u8");
  const isTs   = cdnUrl.endsWith(".ts") || cdnUrl.includes(".ts?");
  const isPH   = path === "/ph/proxy";
  const isJav  = path === "/jav/proxy";

  // Browser opening m3u8 → redirect to watch page
  if (isM3u8 && viewkey && isPH) {
    const accept = request.headers.get("Accept") || "";
    if (accept.includes("text/html") && !accept.includes("application/x-mpegurl")) {
      return Response.redirect(`${url.origin}/ph/watch/${viewkey}`, 302);
    }
  }

  // JAV proxy — plain fetch
  if (isJav) {
    const h = new Headers({
      "Referer": "https://javtiful.com/", "Origin": "https://javtiful.com",
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
      "Accept": "*/*", "Accept-Encoding": "identity",
    });
    if (request.headers.get("Range")) h.set("Range", request.headers.get("Range"));
    const resp = await fetch(cdnUrl, { method: "GET", headers: h, redirect: "follow" });
    if (!resp.ok) return new Response(JSON.stringify({ status: "error", message: `CDN ${resp.status}` }),
      { status: resp.status, headers: { "Content-Type": "application/json" } });
    const fname = (new URL(cdnUrl).pathname.split("/").pop() || "video.mp4").replace(/\?.*/, "");
    const rh = new Headers({
      "Content-Type": resp.headers.get("Content-Type") || "video/mp4",
      "Content-Disposition": downloadMode ? `attachment; filename="${fname}"` : `inline; filename="${fname}"`,
      "Accept-Ranges": "bytes", "Access-Control-Allow-Origin": "*", "Cache-Control": "no-store",
    });
    for (const h2 of ["Content-Length", "Content-Range", "ETag"]) {
      const v = resp.headers.get(h2); if (v) rh.set(h2, v);
    }
    return new Response(resp.body, { status: resp.status, headers: rh });
  }

  // PH proxy
  const cdHeaders = new Headers({
    "Referer": "https://www.pornhub.com/", "Origin": "https://www.pornhub.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "*/*", "Accept-Encoding": "identity",
    "Cookie": "accessAgeDisclaimerPH=1; accessAgeDisclaimerUK=1; accessPH=1; age_verified=1; platform=pc",
  });
  if (request.headers.get("Range")) cdHeaders.set("Range", request.headers.get("Range"));

  let cdnResp = await fetch(cdnUrl, { method: "GET", headers: cdHeaders, redirect: "follow" });

  // Auto-refresh expired/IP-locked CDN links using the viewkey
  if ([403, 410, 451].includes(cdnResp.status) && viewkey && apiBase) {
    try {
      const apiHeaders = { "Content-Type": "application/json" };
      if (apiKey) apiHeaders["X-API-Key"] = apiKey;
      const refreshResp = await fetch(`${apiBase}/ph/download`, {
        method: "POST", headers: apiHeaders,
        body: JSON.stringify({ url: `https://www.pornhub.com/view_video.php?viewkey=${viewkey}` }),
      });
      if (refreshResp.ok) {
        const { data } = await refreshResp.json();
        const freshQuals = data?.qualities || [];
        // Find matching quality or fall back to best
        const isHlsStream = isM3u8;
        const target = freshQuals.find(q =>
          q.quality === quality && q.format === (isHlsStream ? "hls" : "mp4")
        ) || freshQuals.find(q => q.format === (isHlsStream ? "hls" : "mp4"))
          || freshQuals[0];
        if (target?.url) {
          cdnUrl = target.url;
          cdnResp = await fetch(cdnUrl, { method: "GET", headers: cdHeaders, redirect: "follow" });
        }
      }
    } catch (e) {
      // fall through to error response
    }
  }

  if (isM3u8 && cdnResp.ok) {
    const text = await cdnResp.text();
    if (!text || text.trim().length < 10) {
      return new Response(JSON.stringify({ status: "error", message: "CDN returned empty manifest." }),
        { status: 502, headers: { "Content-Type": "application/json" } });
    }
    const rewritten = rewriteManifest(text, cdnUrl, url.origin, { _t: tokenB64, _e: expiryStr, vk: viewkey, q: quality });
    return new Response(rewritten, {
      status: 200,
      headers: { "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
                 "Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache",
                 "Content-Disposition": 'inline; filename="playlist.m3u8"' },
    });
  }

  if (!cdnResp.ok) {
    return new Response(JSON.stringify({ status: "error", message: `CDN returned HTTP ${cdnResp.status}.` }),
      { status: cdnResp.status, headers: { "Content-Type": "application/json" } });
  }

  const fname2 = (new URL(cdnUrl).pathname.split("/").pop().split("?")[0] || "video");
  const safeF  = /\.(mp4|webm|ts|m3u8)$/i.test(fname2) ? fname2 : fname2 + ".mp4";
  const ct     = cdnResp.headers.get("Content-Type") || (isTs ? "video/mp2t" : "video/mp4");
  const rh2    = new Headers({
    "Content-Type": ct,
    "Content-Disposition": downloadMode ? `attachment; filename="${safeF}"` : `inline; filename="${safeF}"`,
    "Accept-Ranges": "bytes", "Access-Control-Allow-Origin": "*", "Cache-Control": "no-store",
  });
  for (const h of ["Content-Length", "Content-Range", "ETag", "Last-Modified"]) {
    const v = cdnResp.headers.get(h); if (v) rh2.set(h, v);
  }
  return new Response(cdnResp.body, { status: cdnResp.status, headers: rh2 });
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

export default {
  async fetch(request, env, ctx) {
    try {
      // API_BASE_URL is REQUIRED — set it in CF dashboard (Variable or Secret)
      // to point to your deployment: Koyeb, Render, Railway, etc.
      const apiBase = env?.API_BASE_URL
        ? String(env.API_BASE_URL).replace(/\/$/, "")
        : null;

      const apiKey = env?.API_KEY ? String(env.API_KEY) : "";
      const url    = new URL(request.url);

      // CORS preflight
      if (request.method === "OPTIONS") {
        return new Response(null, {
          headers: {
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Range, Content-Type, X-API-Key, Authorization",
            "Access-Control-Max-Age":       "86400",
          },
        });
      }

      // Health check — always works, shows config status
      if (url.pathname === "/") {
        return new Response(JSON.stringify({
          status:   "ok",
          service:  "grabx-proxy worker",
          auth:     apiKey ? "enabled" : "disabled",
          api_base: apiBase || "NOT SET — add API_BASE_URL variable in CF dashboard",
        }), { headers: { "Content-Type": "application/json" } });
      }

      if (!apiBase) {
        return new Response(JSON.stringify({
          status:  "error",
          message: "API_BASE_URL is not configured. Add it as a Variable in the CF Worker settings.",
        }), { status: 503, headers: { "Content-Type": "application/json" } });
      }

      if (request.method !== "GET" && request.method !== "HEAD" && request.method !== "POST") {
        return new Response("Method not allowed", { status: 405 });
      }

      return await handleRequest(request, apiKey, apiBase);

    } catch (err) {
      console.error("Worker exception:", err);
      return new Response(
        JSON.stringify({ status: "error", message: err instanceof Error ? err.message : String(err) }),
        { status: 500, headers: { "Content-Type": "application/json" } },
      );
    }
  },
};

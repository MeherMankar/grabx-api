/**
 * GrabX CDN Proxy — Cloudflare Worker
 * Proxies PH and Terabox CDN streams with correct headers.
 * Routes: /ph/proxy and /proxy
 */

// ---------------------------------------------------------------------------
// Token verification
// ---------------------------------------------------------------------------

async function verifyToken(cdnUrl, tokenB64, expiryStr, apiKey) {
  apiKey = (apiKey || "").trim();
  if (!apiKey) return true; // open mode

  const expiry = parseInt(expiryStr, 10);
  if (isNaN(expiry) || Date.now() / 1000 > expiry) return false;

  const msg    = `${expiry}:${cdnUrl}`;
  const key    = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(apiKey),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  const padded = tokenB64.replace(/-/g, "+").replace(/_/g, "/");
  const pad    = (4 - (padded.length % 4)) % 4;
  const b64    = padded + "=".repeat(pad);
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
  if (uri.startsWith("//"))       return "https:" + uri;
  if (uri.startsWith("/"))        return baseOrigin + uri;
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
    const abs = resolveHlsUri(stripped, baseDir, origin);
    return buildProxyUrl(workerBase, "/ph/proxy", abs, extraParams);
  }).join("\n");
}

// ---------------------------------------------------------------------------
// Watch page — fetch qualities from Render and serve an HLS.js player
// ---------------------------------------------------------------------------

async function serveWatchPage(viewkey, workerOrigin, apiKey) {
  try {
    const apiHeaders = { "Content-Type": "application/json" };
    if (apiKey) apiHeaders["X-API-Key"] = apiKey;

    const r = await fetch(`${RENDER_BASE}/ph/download`, {
      method: "POST",
      headers: apiHeaders,
      body: JSON.stringify({ url: `https://www.pornhub.com/view_video.php?viewkey=${viewkey}` }),
    });

    if (!r.ok) {
      const err = await r.json().catch(() => ({ message: `HTTP ${r.status}` }));
      return errorPage(err.message || `Render API returned ${r.status}`);
    }

    const { data } = await r.json();
    const title     = data.title     || "Video";
    const thumbnail = data.thumbnail || "";
    const duration  = data.duration  || "";
    const qualities = data.qualities || [];

    if (!qualities.length) return errorPage("No streams found for this video.");

    // Rewrite proxy URLs to point to this Worker instead of Render
    const opts = qualities.map(q => {
      const proxyUrl = rewriteToWorker(q.proxy_url, workerOrigin);
      const dlUrl    = rewriteToWorker(q.download_url, workerOrigin);
      return { label: `${q.quality}p ${q.format.toUpperCase()}`, fmt: q.format, proxy: proxyUrl, dl: dlUrl };
    });

    const optionsHtml = opts.map(o =>
      `<option value="${escHtml(o.proxy)}" data-fmt="${o.fmt}" data-dl="${escHtml(o.dl)}">${escHtml(o.label)}</option>`
    ).join("\n");

    const best = opts[0];

    const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>${escHtml(title)}</title>
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    body{background:#0f0f0f;color:#eee;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:24px 16px 48px}
    .container{width:100%;max-width:960px}
    h1{font-size:1.15rem;font-weight:600;margin-bottom:14px;line-height:1.4;color:#fff}
    .player-wrap{position:relative;width:100%;background:#000;border-radius:8px;overflow:hidden}
    video{width:100%;display:block;max-height:540px;background:#000}
    .controls{display:flex;align-items:center;gap:12px;margin-top:14px;flex-wrap:wrap}
    select{background:#1e1e1e;color:#eee;border:1px solid #444;border-radius:6px;
           padding:8px 12px;font-size:.9rem;cursor:pointer;flex:1;min-width:120px}
    select:focus{outline:none;border-color:#f90}
    .btn{display:inline-flex;align-items:center;gap:6px;font-weight:700;font-size:.9rem;
         padding:9px 18px;border-radius:6px;text-decoration:none;white-space:nowrap;transition:background .15s;cursor:pointer;border:none}
    .btn-dl{background:#f90;color:#000}.btn-dl:hover{background:#e88600}
    .btn-dl:disabled{background:#666;cursor:not-allowed}
    .meta{margin-top:10px;font-size:.8rem;color:#666}
    .note{margin-top:16px;font-size:.75rem;color:#444;text-align:center}
  </style>
</head>
<body>
  <div class="container">
    <h1>${escHtml(title)}</h1>
    <div class="player-wrap">
      <video id="player" controls preload="metadata" poster="${escHtml(thumbnail)}">
        Your browser does not support HTML5 video.
      </video>
    </div>
    <div class="controls">
      <select id="qualitySelect">${optionsHtml}</select>
      <button id="dlBtn" class="btn btn-dl">&#8595; Download</button>
    </div>
    <div class="meta">${duration ? `Duration: ${escHtml(duration)} &nbsp;&middot;&nbsp; ` : ""}Powered by <a href="https://github.com/MeherMankar/grabx-api" target="_blank" style="color:#f90;text-decoration:none">GrabX API</a></div>
    <p class="note">Tip: right-click the video &rarr; "Save video as" to download directly from CDN.</p>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
  <script>
    const video = document.getElementById('player');
    const sel   = document.getElementById('qualitySelect');
    const dlBtn = document.getElementById('dlBtn');
    let hls     = null;
    let currentDlUrl = '${escHtml(best.dl)}';

    function loadSrc(streamUrl, fmt, dlUrl) {
      const isHls = fmt === 'hls' || streamUrl.includes('.m3u8');
      currentDlUrl = dlUrl;
      if (hls) { hls.destroy(); hls = null; }
      if (isHls) {
        if (Hls.isSupported()) {
          hls = new Hls({ enableWorker: true });
          hls.loadSource(streamUrl);
          hls.attachMedia(video);
          hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
          video.src = streamUrl;
          video.play().catch(() => {});
        }
      } else {
        video.src = streamUrl;
        video.load();
      }
    }

    dlBtn.addEventListener('click', async function() {
      dlBtn.textContent = 'Preparing...';
      dlBtn.disabled = true;
      try {
        const resp = await fetch(currentDlUrl);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const blob = await resp.blob();
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl;
        const cd = resp.headers.get('Content-Disposition') || '';
        const match = cd.match(/filename[*]?=["']?([^"';\\n]+)/i);
        a.download = match ? match[1].trim() : 'video.mp4';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(blobUrl), 10000);
      } catch (e) {
        window.open(currentDlUrl, '_blank');
      } finally {
        dlBtn.textContent = '↓ Download';
        dlBtn.disabled = false;
      }
    });

    const first = sel.options[sel.selectedIndex];
    loadSrc(first.value, first.dataset.fmt, first.dataset.dl);

    sel.addEventListener('change', function() {
      const opt = this.options[this.selectedIndex];
      loadSrc(opt.value, opt.dataset.fmt, opt.dataset.dl);
    });
  </script>
</body>
</html>`;

    return new Response(html, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8" } });
  } catch (err) {
    return errorPage(String(err));
  }
}

function rewriteToWorker(proxyUrl, workerOrigin) {
  if (!proxyUrl) return proxyUrl;
  try {
    const u = new URL(proxyUrl);
    u.protocol = new URL(workerOrigin).protocol;
    u.host     = new URL(workerOrigin).host;
    return u.toString();
  } catch {
    return proxyUrl;
  }
}

function escHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
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
// Main handler
// ---------------------------------------------------------------------------

async function handleRequest(request, apiKey) {
  const url  = new URL(request.url);
  const path = url.pathname;

  if (path !== "/ph/proxy" && path !== "/proxy") {
    // /ph/watch/<viewkey> — fetch qualities from Render API and serve the player page
    if (path.startsWith("/ph/watch/")) {
      const viewkeyWatch = path.replace("/ph/watch/", "").split("?")[0];
      if (viewkeyWatch) {
        return await serveWatchPage(viewkeyWatch, url.origin, apiKey);
      }
    }
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

  // Auth
  if (apiKey) {
    const valid = await verifyToken(cdnUrl, tokenB64, expiryStr, apiKey);
    if (!valid) {
      return new Response(JSON.stringify({ status: "error", message: "Invalid or expired token." }),
        { status: 403, headers: { "Content-Type": "application/json" } });
    }
  }

  const isM3u8 = cdnUrl.includes(".m3u8");
  const isTs   = cdnUrl.endsWith(".ts") || cdnUrl.includes(".ts?");

  // Browser opening m3u8 → redirect to watch page on Render
  if (isM3u8 && viewkey && path === "/ph/proxy") {
    const accept = request.headers.get("Accept") || "";
    if (accept.includes("text/html") && !accept.includes("application/x-mpegurl")) {
      return Response.redirect(`${RENDER_BASE}/ph/watch/${viewkey}`, 302);
    }
  }

  const isPH    = path === "/ph/proxy";

  // Derive Referer/Origin from the actual CDN hostname so it works across
  // all PH domains (pornhub.org, pornhubpremium.com, thumbzilla.com …)
  // and all Terabox mirror domains (1024terabox.com, nephobox.com …)
  let referer, originH;
  try {
    const cdnHost = new URL(cdnUrl).origin; // e.g. https://ev.phncdn.com
    if (isPH) {
      // PH CDN (phncdn.com) requires Referer from a pornhub.* watch domain.
      // Use pornhub.org if the URL came from there, otherwise default to .com
      const vkSource = url.searchParams.get("src") || "";
      const phDomain = vkSource.includes("pornhub.org") ? "pornhub.org" : "pornhub.com";
      referer = `https://www.${phDomain}/`;
      originH = `https://www.${phDomain}`;
    } else {
      // Terabox CDN — canonical www.terabox.com Referer works across all
      // mirror domains (1024terabox.com, nephobox.com, 4funbox.co, etc.)
      // since they all share the same CDN auth infrastructure.
      referer = "https://www.terabox.com/";
      originH = "https://www.terabox.com";
    }
  } catch {
    referer = isPH ? "https://www.pornhub.com/" : "https://www.terabox.com/";
    originH = isPH ? "https://www.pornhub.com"  : "https://www.terabox.com";
  }

  const cdHeaders = new Headers({
    "Referer":        referer,
    "Origin":         originH,
    "User-Agent":     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":         "*/*",
    "Accept-Encoding": "identity",
  });

  const rangeHeader = request.headers.get("Range");
  if (rangeHeader) cdHeaders.set("Range", rangeHeader);

  if (isPH) {
    cdHeaders.set("Cookie", "accessAgeDisclaimerPH=1; accessAgeDisclaimerUK=1; accessPH=1; age_verified=1; platform=pc");
  }

  const cdnResp = await fetch(cdnUrl, { method: "GET", headers: cdHeaders, redirect: "follow" });

  // HLS manifest rewriting
  if (isM3u8 && cdnResp.ok) {
    const text = await cdnResp.text();
    if (!text || text.trim().length < 10) {
      return new Response(JSON.stringify({ status: "error", message: "CDN returned empty manifest." }),
        { status: 502, headers: { "Content-Type": "application/json" } });
    }
    const extraParams = { _t: tokenB64, _e: expiryStr, vk: viewkey, q: quality };
    const rewritten   = rewriteManifest(text, cdnUrl, url.origin, extraParams);
    return new Response(rewritten, {
      status: 200,
      headers: {
        "Content-Type":                "application/vnd.apple.mpegurl; charset=utf-8",
        "Access-Control-Allow-Origin": "*",
        "Cache-Control":               "no-cache",
        "Content-Disposition":         "inline; filename=\"playlist.m3u8\"",
      },
    });
  }

  if (!cdnResp.ok) {
    return new Response(JSON.stringify({ status: "error", message: `CDN returned HTTP ${cdnResp.status}.` }),
      { status: cdnResp.status, headers: { "Content-Type": "application/json" } });
  }

  // Stream bytes
  const pathPart = new URL(cdnUrl).pathname;
  const fname    = pathPart.split("/").pop().split("?")[0] || "video";
  const safeF    = /\.(mp4|webm|ts|m3u8)$/i.test(fname) ? fname : fname + ".mp4";
  const ct       = cdnResp.headers.get("Content-Type") || (isTs ? "video/mp2t" : "video/mp4");
  const disp     = downloadMode ? `attachment; filename="${safeF}"` : `inline; filename="${safeF}"`;

  const respHeaders = new Headers({
    "Content-Type":                ct,
    "Content-Disposition":         disp,
    "Accept-Ranges":               "bytes",
    "Access-Control-Allow-Origin": "*",
    "Cache-Control":               "no-store",
  });
  for (const h of ["Content-Length", "Content-Range", "ETag", "Last-Modified"]) {
    const v = cdnResp.headers.get(h);
    if (v) respHeaders.set(h, v);
  }

  return new Response(cdnResp.body, { status: cdnResp.status, headers: respHeaders });
}

// ---------------------------------------------------------------------------
// Entry point — everything inside try/catch
// ---------------------------------------------------------------------------

const RENDER_BASE = "https://grabx-api.onrender.com";

export default {
  async fetch(request, env, ctx) {
    try {
      const apiKey = (env && env.API_KEY) ? String(env.API_KEY) : "";
      const url    = new URL(request.url);

      // CORS preflight
      if (request.method === "OPTIONS") {
        return new Response(null, {
          headers: {
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "Range, Content-Type",
            "Access-Control-Max-Age":       "86400",
          },
        });
      }

      // Health check
      if (url.pathname === "/") {
        return new Response(JSON.stringify({
          status:  "ok",
          service: "grabx-proxy worker",
          auth:    apiKey ? "enabled" : "disabled (API_KEY not set)",
        }), { headers: { "Content-Type": "application/json" } });
      }

      if (request.method !== "GET" && request.method !== "HEAD") {
        return new Response("Method not allowed", { status: 405 });
      }

      return await handleRequest(request, apiKey);

    } catch (err) {
      console.error("Worker exception:", err);
      return new Response(
        JSON.stringify({ status: "error", message: err instanceof Error ? err.message : String(err) }),
        { status: 500, headers: { "Content-Type": "application/json" } },
      );
    }
  },
};

/**
 * GrabX CDN Proxy — Cloudflare Worker
 * =====================================
 * Handles all streaming / download requests so Render has ZERO bandwidth load.
 *
 * Supported routes (all GET):
 *   /ph/proxy?url=<encoded>&_t=<hmac>&_e=<expiry>[&vk=<viewkey>&q=<quality>&dl=0|1]
 *   /proxy?url=<encoded>&_t=<hmac>&_e=<expiry>[&dl=0|1]
 *
 * Token verification mirrors the Python API exactly:
 *   HMAC-SHA256( key=API_KEY, msg="<expiry>:<cdn_url>" )  base64url no-padding
 *
 * For HLS .m3u8 manifests the Worker rewrites every URI line so segments
 * are also fetched via this Worker (CF IP stays consistent, Referer attached).
 *
 * Environment variables (set in Cloudflare dashboard / wrangler.toml secrets):
 *   API_KEY  — same value as on Render
 */

// ---------------------------------------------------------------------------
// Token verification
// ---------------------------------------------------------------------------

async function verifyToken(cdnUrl, tokenB64, expiryStr) {
  const expiry = parseInt(expiryStr, 10);
  if (isNaN(expiry) || Date.now() / 1000 > expiry) return false;

  const apiKey = (typeof API_KEY !== "undefined" ? API_KEY : "").trim();
  if (!apiKey) return true; // open mode

  const msg    = `${expiry}:${cdnUrl}`;
  const key    = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(apiKey),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  // Decode base64url (no padding) → ArrayBuffer
  const padded  = tokenB64.replace(/-/g, "+").replace(/_/g, "/");
  const pad     = (4 - (padded.length % 4)) % 4;
  const b64     = padded + "=".repeat(pad);
  let binary;
  try { binary = atob(b64); } catch { return false; }
  const sigBuf  = Uint8Array.from(binary, c => c.charCodeAt(0));

  return crypto.subtle.verify(
    "HMAC",
    key,
    sigBuf,
    new TextEncoder().encode(msg),
  );
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

function rewriteManifest(text, cdnUrl, workerBase, extraParams) {
  const parsed   = new URL(cdnUrl);
  const baseDir  = cdnUrl.slice(0, cdnUrl.lastIndexOf("/") + 1);
  const origin   = parsed.origin;

  const lines = text.split("\n").map(line => {
    const stripped = line.trim();
    if (!stripped) return line;

    if (stripped.startsWith("#")) {
      // Rewrite URI="..." attributes inside tags
      return line.replace(/URI="([^"]+)"/g, (_, uri) => {
        const abs = resolveHlsUri(uri, baseDir, origin);
        return `URI="${buildProxyUrl(workerBase, "/ph/proxy", abs, extraParams)}"`;
      });
    }

    // URI line (segment or child playlist)
    const abs = resolveHlsUri(stripped, baseDir, origin);
    return buildProxyUrl(workerBase, "/ph/proxy", abs, extraParams);
  });

  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// URL builder (mirrors Python _make_proxy_url but without signing —
// segments inherit the parent manifest's token validity window)
// ---------------------------------------------------------------------------

function buildProxyUrl(base, path, cdnUrl, extraParams = {}) {
  const u = new URL(path, base);
  u.searchParams.set("url", cdnUrl);
  // Forward token params so sub-manifest/segment requests also pass auth
  for (const [k, v] of Object.entries(extraParams)) {
    if (v) u.searchParams.set(k, v);
  }
  return u.toString();
}

// ---------------------------------------------------------------------------
// Main fetch handler
// ---------------------------------------------------------------------------

async function handleRequest(request) {
  const url    = new URL(request.url);
  const path   = url.pathname;

  // Only handle /ph/proxy and /proxy routes
  if (path !== "/ph/proxy" && path !== "/proxy") {
    return new Response("Not found", { status: 404 });
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

  // Auth check
  const apiKey = (typeof API_KEY !== "undefined" ? API_KEY : "").trim();
  if (apiKey) {
    const valid = await verifyToken(cdnUrl, tokenB64, expiryStr);
    if (!valid) {
      return new Response(
        JSON.stringify({ status: "error", message: "Invalid or expired token." }),
        { status: 403, headers: { "Content-Type": "application/json" } },
      );
    }
  }

  const isM3u8 = cdnUrl.includes(".m3u8");
  const isTs   = cdnUrl.endsWith(".ts") || cdnUrl.includes(".ts?");

  // Browser opening an m3u8 directly → redirect to watch page
  if (isM3u8 && viewkey && path === "/ph/proxy") {
    const accept = request.headers.get("Accept") || "";
    if (accept.includes("text/html") && !accept.includes("application/x-mpegurl")) {
      return Response.redirect(`${url.origin}/ph/watch/${viewkey}`, 302);
    }
  }

  // Determine Referer based on route
  const isPH    = path === "/ph/proxy";
  const referer = isPH ? "https://www.pornhub.com/" : "https://www.terabox.com/";
  const origin2 = isPH ? "https://www.pornhub.com"  : "https://www.terabox.com";

  const cdHeaders = new Headers({
    "Referer":                  referer,
    "Origin":                   origin2,
    "User-Agent":               "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":                   "*/*",
    "Accept-Encoding":          "identity",
  });

  // Forward Range header for video seeking
  const rangeHeader = request.headers.get("Range");
  if (rangeHeader) cdHeaders.set("Range", rangeHeader);

  // Add PH age cookies
  if (isPH) {
    cdHeaders.set("Cookie", "accessAgeDisclaimerPH=1; accessAgeDisclaimerUK=1; accessPH=1; age_verified=1; platform=pc");
  }

  let cdnResp = await fetch(cdnUrl, {
    method: "GET",
    headers: cdHeaders,
    redirect: "follow",
  });

  // Handle empty/failed manifest with a retry signal
  if (isM3u8 && cdnResp.ok) {
    const text = await cdnResp.text();
    if (!text || text.trim().length < 10) {
      return new Response(
        JSON.stringify({ status: "error", message: "CDN returned empty manifest. Re-fetch from /ph/download." }),
        { status: 502, headers: { "Content-Type": "application/json" } },
      );
    }

    // Rewrite manifest URIs
    const workerBase  = url.origin;
    const extraParams = { _t: tokenB64, _e: expiryStr, vk: viewkey, q: quality };
    const rewritten   = rewriteManifest(text, cdnUrl, workerBase, extraParams);

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
    return new Response(
      JSON.stringify({ status: "error", message: `CDN returned HTTP ${cdnResp.status}.` }),
      { status: cdnResp.status, headers: { "Content-Type": "application/json" } },
    );
  }

  // Stream bytes to client
  const pathPart = new URL(cdnUrl).pathname;
  const fname    = pathPart.split("/").pop().split("?")[0] || "video";
  const safeF    = /\.(mp4|webm|ts|m3u8)$/i.test(fname) ? fname : fname + ".mp4";

  const contentType = cdnResp.headers.get("Content-Type")
    || (isTs ? "video/mp2t" : "video/mp4");
  const disposition = downloadMode
    ? `attachment; filename="${safeF}"`
    : `inline; filename="${safeF}"`;

  const respHeaders = new Headers({
    "Content-Type":                contentType,
    "Content-Disposition":         disposition,
    "Accept-Ranges":               "bytes",
    "Access-Control-Allow-Origin": "*",
    "Cache-Control":               "no-store",
  });

  for (const h of ["Content-Length", "Content-Range", "ETag", "Last-Modified"]) {
    const v = cdnResp.headers.get(h);
    if (v) respHeaders.set(h, v);
  }

  return new Response(cdnResp.body, {
    status:  cdnResp.status,
    headers: respHeaders,
  });
}

// ---------------------------------------------------------------------------
// CORS preflight
// ---------------------------------------------------------------------------

export default {
  async fetch(request, env, ctx) {
    // Make API_KEY available from env binding
    if (env && env.API_KEY) {
      globalThis.API_KEY = env.API_KEY;
    }

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

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405 });
    }

    try {
      return await handleRequest(request);
    } catch (err) {
      return new Response(
        JSON.stringify({ status: "error", message: String(err) }),
        { status: 500, headers: { "Content-Type": "application/json" } },
      );
    }
  },
};

"""
GrabX API — Shared utilities
=============================
Token signing/verification, proxy URL builder, CDN Referer detection,
watch-page HTML renderer, and the _resolve_hls_uri helper used by all
proxy routes that rewrite HLS manifests.
"""

import hmac
import hashlib
import base64
import ipaddress
import os
import re
import socket
import time as _time
from urllib.parse import quote, urlparse

from flask import request

# ---------------------------------------------------------------------------
# Configuration (read once at import time)
# ---------------------------------------------------------------------------

API_KEY: str = (
    os.environ.get("API_KEY", "").strip()
    or os.environ.get("GRABX_API_KEY", "").strip()
)

CF_WORKER_URL: str = os.environ.get("CF_WORKER_URL", "").rstrip("/")

TOKEN_TTL: int = int(os.environ.get("PROXY_TOKEN_TTL_HOURS", "24")) * 3600

# ---------------------------------------------------------------------------
# Universal rotating proxy pool
# ---------------------------------------------------------------------------
# Set PROXY_URL on Koyeb/Render with one or more proxies (comma-separated).
# Formats supported:
#   host:port:user:pass          (webshare format)
#   http://user:pass@host:port   (standard URL format)
# A random proxy is picked per request to distribute load.
# ---------------------------------------------------------------------------

def _parse_proxy_list() -> list:
    """Parse PROXY_URL env var into a list of http://user:pass@host:port strings."""
    raw = (
        os.environ.get("PROXY_URL", "").strip()
        or os.environ.get("PH_PROXY", "").strip()
    )
    if not raw:
        return []
    proxies = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.startswith("http://") or entry.startswith("https://") or entry.startswith("socks"):
            proxies.append(entry)
        else:
            # host:port:user:pass format
            parts = entry.split(":")
            if len(parts) == 4:
                host, port, user, password = parts
                proxies.append(f"http://{user}:{password}@{host}:{port}")
            elif len(parts) == 2:
                host, port = parts
                proxies.append(f"http://{host}:{port}")
    return proxies


def get_proxy() -> str:
    """Return a random proxy URL from the pool, or empty string if none configured."""
    proxies = _parse_proxy_list()
    if not proxies:
        return ""
    import random
    return random.choice(proxies)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)

# ---------------------------------------------------------------------------
# HMAC token helpers
# ---------------------------------------------------------------------------

def sign_url(cdn_url: str) -> str:
    """Return '_t=<hmac>&_e=<expiry>' params for a CDN URL, or '' if no key."""
    if not API_KEY:
        return ""
    expiry = int(_time.time()) + TOKEN_TTL
    msg    = f"{expiry}:{cdn_url}".encode()
    sig    = hmac.new(API_KEY.encode(), msg, hashlib.sha256).digest()
    token  = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"_t={token}&_e={expiry}"


def validate_proxy_target(cdn_url: str) -> bool:
    """Reject internal/loopback/invalid targets before proxying a user-supplied URL."""
    try:
        parsed = urlparse(cdn_url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified
        )
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for _, _, _, _, sockaddr in infos:
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified
        ):
            return False
    return True


def verify_proxy_token(cdn_url: str) -> bool:
    """True if request has a valid signed token for cdn_url, requiring API_KEY to be configured."""
    if not API_KEY:
        return False
    t   = request.args.get("_t", "")
    exp = request.args.get("_e", "")
    if not t or not exp:
        return False
    try:
        expiry = int(exp)
    except ValueError:
        return False
    if _time.time() > expiry:
        return False
    msg      = f"{expiry}:{cdn_url}".encode()
    expected = hmac.new(API_KEY.encode(), msg, hashlib.sha256).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
    return hmac.compare_digest(t, expected_b64)


def check_raw_key() -> bool:
    """True if the request carries the raw API key in any accepted location."""
    if not API_KEY:
        return False
    auth  = request.headers.get("Authorization", "")
    bearer = auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else ""
    key = (
        request.headers.get("X-API-Key")
        or request.headers.get("X-Api-Key")
        or request.headers.get("apikey")
        or request.headers.get("Api-Key")
        or request.headers.get("GRABX-API-KEY")
        or request.headers.get("X-GRABX-API-KEY")
        or request.headers.get("GRABX_API_KEY")
        or request.args.get("api_key")
        or request.args.get("apikey")
        or request.args.get("grabx_api_key")
        or bearer
    )
    return key == API_KEY


# ---------------------------------------------------------------------------
# Proxy URL builder
# ---------------------------------------------------------------------------

def make_proxy_url(base_url: str, path: str, cdn_url: str, extra: str = "",
                   viewkey: str = "", quality: str = "", src_domain: str = "") -> str:
    """
    Build a signed proxy URL.
    Routes /ph/proxy, /jav/proxy through CF Worker when configured.
    /proxy (Terabox) and /adult/proxy always stay on the API server (IP-locked CDN).
    """
    # Force https — Koyeb/Render may pass http in request.host_url via proxy headers
    base_url = base_url.replace("http://", "https://")

    if CF_WORKER_URL and path in ("/ph/proxy", "/jav/proxy"):
        proxy_base = CF_WORKER_URL
    else:
        proxy_base = base_url

    enc      = quote(cdn_url, safe="")
    token    = sign_url(cdn_url)
    vk_part  = f"&vk={quote(viewkey)}"     if viewkey     else ""
    q_part   = f"&q={quote(quality)}"      if quality     else ""
    src_part = f"&src={quote(src_domain)}" if src_domain  else ""
    if token:
        return f"{proxy_base}{path}?url={enc}&{token}{vk_part}{q_part}{src_part}{extra}"
    return f"{proxy_base}{path}?url={enc}{vk_part}{q_part}{src_part}{extra}"


# ---------------------------------------------------------------------------
# CDN Referer detection (adult sites)
# ---------------------------------------------------------------------------

_ADULT_CDN_REFERERS = [
    (re.compile(r'xvideos-cdn\.com', re.I), "https://www.xvideos.com/"),
    (re.compile(r'xnxx-cdn\.com',    re.I), "https://www.xnxx.com/"),
    (re.compile(r'xhmscdn|xhstorage|xhamster', re.I), "https://xhamster.com/"),
]


def adult_referer(cdn_url: str) -> str:
    host = (urlparse(cdn_url).hostname or "").lower()
    for pat, ref in _ADULT_CDN_REFERERS:
        if pat.search(host):
            return ref
    return "https://www.xvideos.com/"


# ---------------------------------------------------------------------------
# HLS URI resolver (used by proxy routes that rewrite manifests)
# ---------------------------------------------------------------------------

def resolve_hls_uri(uri: str, base_dir: str, parsed_base) -> str:
    if uri.startswith("http://") or uri.startswith("https://"):
        return uri
    if uri.startswith("//"):
        return parsed_base.scheme + ":" + uri
    if uri.startswith("/"):
        return f"{parsed_base.scheme}://{parsed_base.netloc}{uri}"
    return base_dir + uri


# ---------------------------------------------------------------------------
# Watch-page HTML renderer (shared by all platforms)
# ---------------------------------------------------------------------------

def html_attr(url: str) -> str:
    """Escape a URL for safe use inside an HTML attribute value."""
    return str(url or "").replace("&", "&amp;").replace('"', "&quot;")


def render_watch_page(meta: dict, qualities: list):
    """
    Render the HLS.js video player HTML for any platform.
    qualities must be a list of dicts with: quality, format, proxy_url, download_url.
    """
    title     = meta.get("title", "Video")
    thumbnail = meta.get("thumbnail", "")
    duration  = meta.get("duration", "")

    sorted_opts = sorted(
        qualities,
        key=lambda q: (0, -int(q["quality"])) if q["quality"].isdigit() else (1, 0),
    )

    options_html = "\n".join(
        f'<option value="{html_attr(o.get("proxy_url",""))}" '
        f'data-fmt="{o["format"]}" '
        f'data-dl="{html_attr(o.get("download_url",""))}">'
        f'{o["quality"]}{"p" if o["quality"].isdigit() else ""} {o["format"].upper()}'
        f'</option>'
        for o in sorted_opts
    )

    best     = sorted_opts[0]
    # Use the raw URL directly in JS strings — & only needs escaping in HTML attributes
    best_dl  = best.get("download_url", "")
    best_fmt = best.get("format", "mp4")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>{title}</title>
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0f0f0f;color:#eee;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
          min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:24px 16px 48px}}
    .container{{width:100%;max-width:960px}}
    h1{{font-size:1.15rem;font-weight:600;margin-bottom:14px;line-height:1.4;color:#fff}}
    .player-wrap{{position:relative;width:100%;background:#000;border-radius:8px;overflow:hidden}}
    video{{width:100%;display:block;max-height:540px;background:#000}}
    .controls{{display:flex;align-items:center;gap:12px;margin-top:14px;flex-wrap:wrap}}
    select{{background:#1e1e1e;color:#eee;border:1px solid #444;border-radius:6px;
            padding:8px 12px;font-size:.9rem;cursor:pointer;flex:1;min-width:120px}}
    select:focus{{outline:none;border-color:#f90}}
    .btn{{display:inline-flex;align-items:center;gap:6px;font-weight:700;font-size:.9rem;
          padding:9px 18px;border-radius:6px;white-space:nowrap;
          transition:background .15s;cursor:pointer;border:none}}
    .btn-dl{{background:#f90;color:#000}}.btn-dl:hover{{background:#e88600}}
    .btn-dl:disabled{{background:#666;cursor:not-allowed}}
    .meta{{margin-top:10px;font-size:.8rem;color:#666}}
    a{{color:#f90}}
    .note{{margin-top:16px;font-size:.75rem;color:#444;text-align:center}}
  </style>
</head>
<body>
  <div class="container">
    <h1>{title}</h1>
    <div class="player-wrap">
      <video id="player" controls preload="metadata" poster="{thumbnail}">
        Your browser does not support HTML5 video.
      </video>
    </div>
    <div class="controls">
      <select id="qualitySelect">{options_html}</select>
      <button id="dlBtn" class="btn btn-dl">&#8595; Download</button>
      <a id="directLink" class="btn" href="{html_attr(best.get('proxy_url',''))}" target="_blank" style="background:#333;color:#eee;font-size:.8rem">&#8599; Open stream</a>
    </div>
    <div class="meta">{'Duration: ' + duration + ' &nbsp;·&nbsp; ' if duration else ''}Powered by <a href="https://github.com/MeherMankar/grabx-api" target="_blank">GrabX API</a></div>
    <p class="note">💡 Right-click <b>Open stream</b> → "Save link as" to download. For HLS: open in VLC.</p>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
  <script>
    const video = document.getElementById('player');
    const sel   = document.getElementById('qualitySelect');
    const dlBtn = document.getElementById('dlBtn');
    const directLink = document.getElementById('directLink');
    let hls     = null;
    let currentDlUrl = '{best_dl}';
    let currentFmt   = '{best_fmt}';

    function loadSrc(streamUrl, fmt, dlUrl) {{
      const isHls = fmt === 'hls' || streamUrl.includes('.m3u8');
      currentDlUrl = dlUrl; currentFmt = fmt;
      directLink.href = streamUrl;
      // Update button label based on format
      dlBtn.textContent = isHls ? '📋 Copy Stream URL' : '↓ Download';
      if (hls) {{ hls.destroy(); hls = null; }}
      if (isHls) {{
        if (Hls.isSupported()) {{
          hls = new Hls({{ enableWorker: true }});
          hls.loadSource(streamUrl);
          hls.attachMedia(video);
          hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {{}}));
        }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
          video.src = streamUrl; video.play().catch(() => {{}});
        }}
      }} else {{
        video.src = streamUrl; video.load();
      }}
    }}

    dlBtn.addEventListener('click', async function() {{
      if (currentFmt === 'hls') {{
        // HLS can't be downloaded as a single file in the browser.
        // Copy the stream URL to clipboard and show a message.
        try {{
          await navigator.clipboard.writeText(currentDlUrl);
          dlBtn.textContent = '✓ URL Copied!';
          setTimeout(() => {{ dlBtn.textContent = '↓ Download'; }}, 2000);
        }} catch(e) {{
          // Fallback: prompt user to copy manually
          const msg = 'HLS stream URL (copy and open in VLC):\\n' + currentDlUrl;
          prompt('HLS stream — open in VLC or copy URL:', currentDlUrl);
        }}
        return;
      }}
      dlBtn.textContent = 'Preparing...'; dlBtn.disabled = true;
      try {{
        const resp = await fetch(currentDlUrl);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const blob = await resp.blob();
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl;
        const cd = resp.headers.get('Content-Disposition') || '';
        const match = cd.match(/filename[*]?=["']?([^"';\\n]+)/i);
        a.download = match ? match[1].trim() : 'video.mp4';
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(blobUrl), 10000);
      }} catch (e) {{ window.open(currentDlUrl, '_blank'); }}
      finally {{ dlBtn.textContent = '↓ Download'; dlBtn.disabled = false; }}
    }});

    const first = sel.options[sel.selectedIndex];
    loadSrc(first.value, first.dataset.fmt, first.dataset.dl);
    sel.addEventListener('change', function() {{
      const opt = this.options[this.selectedIndex];
      loadSrc(opt.value, opt.dataset.fmt, opt.dataset.dl);
    }});
  </script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

# ---------------------------------------------------------------------------
# In-memory response cache with TTL
# ---------------------------------------------------------------------------
# Caches extraction results (qualities list + meta) so repeated requests for
# the same URL don't hit the proxy or source site again for CACHE_TTL seconds.
# Simple thread-safe dict — works fine for single-worker Gunicorn deployments.
# ---------------------------------------------------------------------------

import threading
import time as _time_mod

_cache_lock  = threading.Lock()
_cache_store: dict = {}  # key -> {"data": ..., "expires": float}

CACHE_TTL: int = int(os.environ.get("CACHE_TTL_SECONDS", str(2 * 3600)))  # default 2 hours


def cache_get(key: str):
    """Return cached value or None if missing/expired."""
    with _cache_lock:
        entry = _cache_store.get(key)
        if entry and _time_mod.time() < entry["expires"]:
            return entry["data"]
        if entry:
            del _cache_store[key]
    return None


def cache_set(key: str, data, ttl: int = None):
    """Store value with TTL. Uses CACHE_TTL if ttl not specified."""
    if ttl is None:
        ttl = CACHE_TTL
    with _cache_lock:
        _cache_store[key] = {
            "data":    data,
            "expires": _time_mod.time() + ttl,
        }
        # Evict expired entries if cache grows large
        if len(_cache_store) > 500:
            now = _time_mod.time()
            expired = [k for k, v in _cache_store.items() if v["expires"] < now]
            for k in expired:
                del _cache_store[k]


def cache_stats() -> dict:
    """Return cache stats for /health endpoint."""
    with _cache_lock:
        now = _time_mod.time()
        total   = len(_cache_store)
        active  = sum(1 for v in _cache_store.values() if v["expires"] > now)
    return {"total": total, "active": active, "ttl_seconds": CACHE_TTL}

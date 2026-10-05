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
import html as html_lib
import ipaddress
import os
import re
import socket
import time as _time
import json
import threading
import xml.etree.ElementTree as ET
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
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username is not None or parsed.password is not None:
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
        infos = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
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


def make_dash_proxy_url(base_url: str, path: str, root_url: str, media_url: str) -> str:
    """Build a proxy template URL carrying a token scoped to its signed MPD."""
    encoded_root = quote(root_url, safe="")
    encoded_media = quote(media_url, safe="")
    token = sign_url(root_url)
    params = f"dash=1&root={encoded_root}&template={encoded_media}"
    if token:
        params += f"&{token}"
    variable_params = {
        "Number": "n", "Time": "t", "RepresentationID": "r", "Bandwidth": "b",
    }
    pattern = re.compile(r"\$(Number|Time|RepresentationID|Bandwidth)(%0\d+d)?\$")
    for variable, format_spec in set(pattern.findall(media_url)):
        params += f"&{variable_params[variable]}=${variable}{format_spec}$"
    base_url = base_url.replace("http://", "https://")
    return f"{base_url.rstrip('/')}{path}?{params}"


def rewrite_dash_manifest(text: str, root_url: str, base_url: str, path: str) -> str:
    """Rewrite DASH segment references through the authenticated proxy."""
    root = ET.fromstring(text)
    parsed_base = urlparse(root_url)
    root_base = root_url[:root_url.rfind("/") + 1]
    variables = re.compile(r"\$(?:Number|Time|RepresentationID|Bandwidth)(?:%0\d+d)?\$")

    def rewrite_element(element, current_base: str, current_parsed):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "BaseURL" and element.text:
            element.text = resolve_hls_uri(element.text.strip(), current_base, current_parsed)
            current_parsed = urlparse(element.text)
            current_base = element.text if element.text.endswith("/") else \
                element.text[:element.text.rfind("/") + 1]
        if tag in {"SegmentTemplate", "SegmentURL", "Initialization", "RepresentationIndex"}:
            for attr in ("media", "initialization", "sourceURL", "index"):
                uri = element.attrib.get(attr)
                if not uri:
                    continue
                absolute = resolve_hls_uri(uri, current_base, current_parsed)
                if variables.search(absolute):
                    element.attrib[attr] = make_dash_proxy_url(base_url, path, root_url, absolute)
                else:
                    element.attrib[attr] = make_proxy_url(base_url, path, absolute)
        for child in element:
            rewrite_element(child, current_base, current_parsed)

    rewrite_element(root, root_base, parsed_base)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


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
    if re.search(r"(?:^|\.)jav\.si$|javtiful", host, re.I):
        return "https://javtiful.com/"
    if "phncdn" in host or "pornhub" in host:
        return "https://www.pornhub.com/"
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
    return html_lib.escape(str(url or ""), quote=True)


def render_watch_page(meta: dict, qualities: list):
    """
    Render the HLS.js video player HTML for any platform.
    qualities must be a list of dicts with: quality, format, proxy_url, download_url.
    """
    title     = html_lib.escape(str(meta.get("title", "Video")))
    thumbnail = html_attr(meta.get("thumbnail", ""))
    duration  = html_lib.escape(str(meta.get("duration", "")))
    is_live   = bool(meta.get("is_live"))

    sorted_opts = sorted(
        qualities,
        key=lambda q: (0, -int(q["quality"])) if q["quality"].isdigit() else (1, 0),
    )

    options_html = "\n".join(
        f'<option value="{html_attr(o.get("proxy_url",""))}" '
        f'data-fmt="{html_attr(o["format"])}" '
        f'data-dl="{html_attr(o.get("download_url",""))}">'
        f'{html_lib.escape(str(o["quality"]))}{"p" if str(o["quality"]).isdigit() else ""} {html_lib.escape(str(o["format"]).upper())}'
        f'</option>'
        for o in sorted_opts
    )

    best     = sorted_opts[0]
    # Use the raw URL directly in JS strings — & only needs escaping in HTML attributes
    best_dl  = json.dumps(best.get("download_url", "")).replace("<", "\\u003c")
    best_fmt = json.dumps(best.get("format", "mp4")).replace("<", "\\u003c")

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
    <h1>{title}{' <span style="color:#f55;font-size:.72em">LIVE</span>' if is_live else ''}</h1>
    <div class="player-wrap">
      <video id="player" controls preload="metadata" poster="{thumbnail}">
        Your browser does not support HTML5 video.
      </video>
    </div>
    <div class="controls">
      <select id="qualitySelect">{options_html}</select>
      <select id="playbackRate" aria-label="Playback speed">
        <option value="0.5">0.5×</option><option value="0.75">0.75×</option>
        <option value="1" selected>1×</option><option value="1.25">1.25×</option>
        <option value="1.5">1.5×</option><option value="2">2×</option>
      </select>
      <button id="dlBtn" class="btn btn-dl">&#8595; Download</button>
      <a id="directLink" class="btn" href="{html_attr(best.get('proxy_url',''))}" target="_blank" style="background:#333;color:#eee;font-size:.8rem">&#8599; Open stream</a>
    </div>
    <div class="meta">{'Duration: ' + duration + ' &nbsp;·&nbsp; ' if duration else ''}Powered by <a href="https://github.com/MeherMankar/grabx-api" target="_blank">GrabX API</a></div>
    <p class="note">💡 Right-click <b>Open stream</b> → "Save link as" to download. For HLS: open in VLC.</p>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
  <script src="https://cdn.dashjs.org/latest/dash.all.min.js"></script>
  <script>
    const video = document.getElementById('player');
    const sel   = document.getElementById('qualitySelect');
    const rate  = document.getElementById('playbackRate');
    const dlBtn = document.getElementById('dlBtn');
    const directLink = document.getElementById('directLink');
    rate.addEventListener('change', () => {{ video.playbackRate = Number(rate.value); }});
    let hls     = null;
    let dash    = null;
    let currentDlUrl = {best_dl};
    let currentFmt   = {best_fmt};

    function loadSrc(streamUrl, fmt, dlUrl) {{
      const isHls = fmt === 'hls' || streamUrl.includes('.m3u8');
      const isDash = fmt === 'dash' || streamUrl.includes('.mpd');
      currentDlUrl = dlUrl; currentFmt = fmt;
      directLink.href = streamUrl;
      // Update button label based on format
      dlBtn.textContent = (isHls || isDash) ? '📋 Copy Stream URL' : '↓ Download';
      if (hls) {{ hls.destroy(); hls = null; }}
      if (dash) {{ dash.reset(); dash = null; }}
      if (isHls) {{
        if (Hls.isSupported()) {{
          hls = new Hls({{ enableWorker: true }});
          hls.loadSource(streamUrl);
          hls.attachMedia(video);
          hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {{}}));
        }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
          video.src = streamUrl; video.play().catch(() => {{}});
        }}
      }} else if (isDash && window.dashjs) {{
        dash = dashjs.MediaPlayer().create();
        dash.initialize(video, streamUrl, true);
      }} else {{
        video.src = streamUrl; video.load();
      }}
    }}

    dlBtn.addEventListener('click', async function() {{
      if (currentFmt === 'hls' || currentFmt === 'dash') {{
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
# Redis is shared by app workers and RQ workers. Without REDIS_URL, the cache
# remains process-local for development; production startup requires Redis.
# ---------------------------------------------------------------------------

import time as _time_mod

_cache_lock  = threading.Lock()
_cache_store: dict = {}  # key -> {"data": ..., "expires": float}
_memory_rate_limits: dict = {}
_abuse_events: list = []

CACHE_TTL: int = int(os.environ.get("CACHE_TTL_SECONDS", str(2 * 3600)))  # default 2 hours
REDIS_URL: str = os.environ.get("REDIS_URL", "").strip()
_redis = None
_redis_lock = threading.Lock()


def get_redis():
    """Return the configured Redis client, or None for local development."""
    global _redis
    if not REDIS_URL:
        return None
    with _redis_lock:
        if _redis is None:
            from redis import Redis
            _redis = Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=3)
        return _redis


def cache_get(key: str):
    """Return cached value or None if missing/expired."""
    redis = get_redis()
    if redis:
        value = redis.get(f"grabx:cache:{key}")
        return json.loads(value) if value else None
    with _cache_lock:
        entry = _cache_store.get(key)
        if entry and _time_mod.time() < entry["expires"]:
            return entry["data"]
        if entry:
            del _cache_store[key]
    return None


def cache_set(key: str, data, ttl: int = None):
    """Store with an optional site-specific TTL, then the global TTL fallback."""
    if ttl is None:
        site = key.partition(":")[0].upper()
        ttl = int(os.environ.get(f"CACHE_TTL_{site}_SECONDS", str(CACHE_TTL)))
    ttl = max(1, ttl)
    redis = get_redis()
    if redis:
        redis.setex(f"grabx:cache:{key}", ttl, json.dumps(data))
        return
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
    redis = get_redis()
    if redis:
        keys = list(redis.scan_iter(match="grabx:cache:*", count=100))
        return {"total": len(keys), "active": len(keys), "ttl_seconds": CACHE_TTL,
                "backend": "redis"}
    with _cache_lock:
        now = _time_mod.time()
        total   = len(_cache_store)
        active  = sum(1 for v in _cache_store.values() if v["expires"] > now)
    return {"total": total, "active": active, "ttl_seconds": CACHE_TTL,
            "backend": "memory"}


def cache_clear() -> int:
    """Clear extraction cache entries and return the number removed."""
    redis = get_redis()
    if redis:
        keys = list(redis.scan_iter(match="grabx:cache:*", count=100))
        return redis.delete(*keys) if keys else 0
    with _cache_lock:
        count = len(_cache_store)
        _cache_store.clear()
    return count


def enforce_rate_limit(identity: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
    """Increment a fixed-window request counter; return (allowed, retry_after)."""
    now = int(_time_mod.time())
    bucket = now // window_seconds
    key = f"grabx:rate:{identity}:{bucket}"
    redis = get_redis()
    if redis:
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds + 1)
        count, _ = pipe.execute()
    else:
        with _cache_lock:
            expired = [k for k, value in _memory_rate_limits.items() if value[1] <= now]
            for k in expired:
                del _memory_rate_limits[k]
            count, _ = _memory_rate_limits.get(key, (0, now + window_seconds))
            count += 1
            _memory_rate_limits[key] = (count, now + window_seconds)
    return count <= limit, max(1, window_seconds - now % window_seconds)


def log_abuse_event(event: dict) -> None:
    """Retain a bounded, short-lived record of rate-limit violations."""
    redis = get_redis()
    if redis:
        redis.lpush("grabx:abuse", json.dumps(event))
        redis.ltrim("grabx:abuse", 0, 499)
        redis.expire("grabx:abuse", 7 * 24 * 60 * 60)
        return
    with _cache_lock:
        _abuse_events.insert(0, event)
        del _abuse_events[500:]


def get_abuse_events(limit: int = 100) -> list:
    """Return recent rate-limit violations for authenticated diagnostics."""
    redis = get_redis()
    if redis:
        return [json.loads(item) for item in redis.lrange("grabx:abuse", 0, limit - 1)]
    with _cache_lock:
        return list(_abuse_events[:limit])

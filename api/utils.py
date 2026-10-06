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

def _get_api_key() -> str:
    """Read API key lazily so dotenv has time to load before first use."""
    return (
        os.environ.get("API_KEY", "").strip()
        or os.environ.get("GRABX_API_KEY", "").strip()
    )

# Module-level alias — re-read on every access via the function above
API_KEY: str = ""  # populated lazily; use _get_api_key() internally

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


def get_proxy_with_id() -> tuple:
    """
    Return (proxy_url, proxy_index) — the index can be embedded in CDN proxy URLs
    so the same proxy is reused when streaming, ensuring the CDN IP matches.
    Returns ("", -1) if no proxies configured.
    """
    proxies = _parse_proxy_list()
    if not proxies:
        return "", -1
    import random
    idx = random.randrange(len(proxies))
    return proxies[idx], idx


def get_proxy_by_id(idx: int) -> str:
    """
    Return the proxy URL at the given index.
    Used by proxy routes to reuse the same IP that signed the CDN URL.
    Falls back to random if index is out of range.
    """
    proxies = _parse_proxy_list()
    if not proxies:
        return ""
    if 0 <= idx < len(proxies):
        return proxies[idx]
    import random
    return random.choice(proxies)


def get_proxy_for_url(cdn_url: str) -> str:
    """Select the configured proxy whose address is embedded in an XHamster CDN URL."""
    parsed = urlparse(cdn_url)
    marker = re.search(
        r"(?:^|/)data=(\d{1,3}(?:\.\d{1,3}){3})-dvp(?:/|$)",
        parsed.path,
    )
    if not marker:
        return ""
    expected_host = marker.group(1)
    for proxy in _parse_proxy_list():
        if urlparse(proxy).hostname == expected_host:
            return proxy
    return ""

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
    key = _get_api_key()
    if not key:
        return ""
    expiry = int(_time.time()) + TOKEN_TTL
    msg    = f"{expiry}:{cdn_url}".encode()
    sig    = hmac.new(key.encode(), msg, hashlib.sha256).digest()
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
        if host.endswith(".xhcdn.com") and get_proxy_for_url(cdn_url):
            return True
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
    key = _get_api_key()
    if not key:
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
    expected = hmac.new(key.encode(), msg, hashlib.sha256).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
    return hmac.compare_digest(t, expected_b64)


def check_raw_key() -> bool:
    """True if the request carries the raw API key in any accepted location."""
    key_needed = _get_api_key()
    if not key_needed:
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
    return key == key_needed


# ---------------------------------------------------------------------------
# Proxy URL builder
# ---------------------------------------------------------------------------

def _normalize_proxy_base_url(base_url: str) -> str:
    """Use HTTPS for deployed hosts while keeping local loopback URLs on HTTP."""
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host != "localhost" and not host.endswith(".localhost"):
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return parsed._replace(scheme="https").geturl()
        except ValueError:
            return parsed._replace(scheme="https").geturl()
    return base_url


def make_proxy_url(base_url: str, path: str, cdn_url: str, extra: str = "",
                   viewkey: str = "", quality: str = "", src_domain: str = "",
                   proxy_id: int = -1) -> str:
    """
    Build a signed proxy URL.
    proxy_id: index of the proxy used to fetch the source page.
              Embed it so the proxy route reuses the same IP for CDN requests.
    """
    base_url = _normalize_proxy_base_url(base_url)

    if CF_WORKER_URL and path in ("/ph/proxy", "/jav/proxy"):
        proxy_base = CF_WORKER_URL
    else:
        proxy_base = base_url

    enc      = quote(cdn_url, safe="")
    token    = sign_url(cdn_url)
    vk_part  = f"&vk={quote(viewkey)}"     if viewkey         else ""
    q_part   = f"&q={quote(quality)}"      if quality         else ""
    src_part = f"&src={quote(src_domain)}" if src_domain      else ""
    pid_part = f"&pid={proxy_id}"          if proxy_id >= 0   else ""
    if token:
        return f"{proxy_base}{path}?url={enc}&{token}{vk_part}{q_part}{src_part}{pid_part}{extra}"
    return f"{proxy_base}{path}?url={enc}{vk_part}{q_part}{src_part}{pid_part}{extra}"


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
    base_url = _normalize_proxy_base_url(base_url)
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
    (re.compile(r'xhcdn\.com',       re.I), "https://xhamster.com/"),
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
  <meta name="theme-color" content="#080a10"/>
  <title>{title}</title>
  <style>
    :root{{color-scheme:dark;--bg:#080a10;--panel:#11141d;--line:#272c3a;--muted:#9299aa;--text:#f5f6fa;--accent:#8b7cff;--accent2:#5c4ee5}}
    *,*::before,*::after{{box-sizing:border-box}}
    body{{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -20%,#28234b 0,transparent 48%),var(--bg);color:var(--text);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:32px 18px 54px}}
    .container{{width:min(100%,1080px);margin:0 auto}}
    .topbar{{display:flex;justify-content:space-between;align-items:center;margin:0 0 20px;color:var(--muted);font-size:.82rem}}
    .brand{{display:flex;align-items:center;gap:9px;color:var(--text);font-weight:750;letter-spacing:.02em}}
    .brand-mark{{width:28px;height:28px;display:grid;place-items:center;border-radius:9px;background:linear-gradient(135deg,var(--accent),var(--accent2));color:white}}
    .top-link{{color:#c5c0ff;text-decoration:none}}
    .top-link:hover{{color:white}}
    h1{{font-size:clamp(1.15rem,2.8vw,1.8rem);line-height:1.3;letter-spacing:-.025em;margin:0 0 16px;font-weight:720;overflow-wrap:anywhere}}
    .live-badge{{display:inline-flex;vertical-align:middle;align-items:center;gap:6px;margin-left:8px;padding:4px 9px;border-radius:999px;background:#39171e;color:#ff8795;font-size:.68rem;letter-spacing:.08em;font-weight:800}}
    .live-dot{{width:7px;height:7px;border-radius:50%;background:#ff5268;box-shadow:0 0 10px #ff5268}}
    .player-shell{{padding:1px;border-radius:18px;background:linear-gradient(135deg,#454064,#202431 38%,#35304d);box-shadow:0 24px 70px #0008}}
    .player-wrap{{position:relative;aspect-ratio:16/9;width:100%;background:#030407;border-radius:17px;overflow:hidden}}
    video{{position:absolute;inset:0;width:100%;height:100%;display:block;object-fit:contain;background:#030407}}
    .player-message{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:24px;text-align:center;background:linear-gradient(135deg,#10121bdd,#090a10e8);transition:opacity .2s;pointer-events:none}}
    .player-message.hidden{{opacity:0}}
    .spinner{{width:34px;height:34px;border:3px solid #ffffff24;border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}}
    @keyframes spin{{to{{transform:rotate(360deg)}}}}
    .message-title{{font-weight:700}}.message-detail{{max-width:440px;color:var(--muted);font-size:.88rem;line-height:1.5}}
    .control-panel{{margin-top:16px;padding:16px;border:1px solid var(--line);border-radius:16px;background:linear-gradient(180deg,#151822,#10131b)}}
    .controls{{display:flex;align-items:end;gap:12px;flex-wrap:wrap}}
    .field{{display:grid;gap:7px;min-width:120px;flex:1}}
    .field-label{{font-size:.69rem;font-weight:750;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}}
    select{{width:100%;min-height:44px;background:#0b0d13;color:var(--text);border:1px solid #343949;border-radius:10px;padding:0 38px 0 12px;font-size:.9rem;cursor:pointer}}
    select:focus-visible,button:focus-visible,a:focus-visible{{outline:2px solid #b0a8ff;outline-offset:3px}}
    .btn{{min-height:44px;display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:0 15px;border:1px solid #3a4050;border-radius:10px;background:#1a1e29;color:var(--text);font-size:.88rem;font-weight:700;text-decoration:none;white-space:nowrap;cursor:pointer;transition:transform .15s,border-color .15s,background .15s}}
    .btn:hover{{transform:translateY(-1px);border-color:#69628f;background:#222636}}
    .btn-primary{{border-color:transparent;background:linear-gradient(135deg,var(--accent),var(--accent2));color:white;box-shadow:0 5px 18px #6256db40}}
    .btn-primary:hover{{background:linear-gradient(135deg,#a298ff,#6b5cf0);border-color:transparent}}
    .btn:disabled{{opacity:.58;cursor:wait;transform:none}}
    .meta-row{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:15px;padding-top:13px;border-top:1px solid var(--line);color:var(--muted);font-size:.82rem}}
    .status{{display:flex;align-items:center;gap:8px;min-width:0}}
    .status-dot{{width:8px;height:8px;flex:none;border-radius:50%;background:#4acb91;box-shadow:0 0 9px #4acb9166}}
    .status.error .status-dot{{background:#ff687a;box-shadow:0 0 9px #ff687a66}}
    .meta-right{{white-space:nowrap}}
    .meta-right a{{color:#c5c0ff;text-decoration:none}}
    .note{{margin:15px 4px 0;color:#747b8d;font-size:.76rem;line-height:1.6}}
    .shortcuts{{color:#a6adbd}}
    @media(max-width:620px){{body{{padding:20px 12px 36px}}.topbar{{margin-bottom:16px}}.player-shell{{border-radius:13px}}.player-wrap{{border-radius:12px}}.control-panel{{padding:12px;border-radius:13px}}.controls{{display:grid;grid-template-columns:1fr 1fr;align-items:stretch;gap:10px}}.field:first-child{{grid-column:1/-1}}.btn{{width:100%;padding:0 10px}}.meta-row{{align-items:flex-start;flex-direction:column}}.meta-right{{white-space:normal}}}}
    @media(prefers-reduced-motion:reduce){{*,*::before,*::after{{scroll-behavior:auto!important;animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}}}
  </style>
</head>
<body>
  <div class="container">
    <div class="topbar">
      <div class="brand"><span class="brand-mark" aria-hidden="true">&#9654;</span> GRABX <span style="color:var(--muted);font-weight:500">PLAYER</span></div>
      <a class="top-link" href="https://github.com/MeherMankar/grabx-api" target="_blank" rel="noopener noreferrer">About GrabX &#8599;</a>
    </div>
    <h1>{title}{' <span class="live-badge"><span class="live-dot"></span> LIVE</span>' if is_live else ''}</h1>
    <div class="player-shell">
      <div class="player-wrap">
        <video id="player" controls playsinline preload="metadata" poster="{thumbnail}">
          Your browser does not support HTML5 video.
        </video>
        <div class="player-message" id="playerMessage" role="status">
          <span class="spinner" id="playerSpinner"></span>
          <span class="message-title" id="messageTitle">Preparing stream</span>
          <span class="message-detail" id="messageDetail">Connecting to the video source…</span>
        </div>
      </div>
    </div>
    <section class="control-panel" aria-label="Player controls">
      <div class="controls">
        <label class="field"><span class="field-label">Quality</span><select id="qualitySelect">{options_html}</select></label>
        <label class="field"><span class="field-label">Speed</span><select id="playbackRate" aria-label="Playback speed">
          <option value="0.5">0.5×</option><option value="0.75">0.75×</option>
          <option value="1" selected>Normal</option><option value="1.25">1.25×</option>
          <option value="1.5">1.5×</option><option value="2">2×</option>
        </select></label>
        <button id="dlBtn" class="btn btn-primary" type="button">&#8595; Download</button>
        <button id="copyBtn" class="btn" type="button">&#128203; Copy link</button>
        <button id="fullscreenBtn" class="btn" type="button">&#9974; Fullscreen</button>
      </div>
      <div class="meta-row">
        <div class="status" id="streamStatus"><span class="status-dot"></span><span id="statusText">Ready to play</span></div>
        <div class="meta-right">{'Duration: ' + duration + ' &nbsp;·&nbsp; ' if duration else ''}<a href="https://github.com/MeherMankar/grabx-api" target="_blank" rel="noopener noreferrer">Powered by GrabX</a></div>
      </div>
    </section>
    <p class="note">Tip: use the quality menu to switch streams. <span class="shortcuts">Keyboard: Space to play/pause · F for fullscreen · &#8592;/&#8594; to seek</span></p>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
  <script src="https://cdn.dashjs.org/latest/dash.all.min.js"></script>
  <script>
    const video = document.getElementById('player');
    const sel   = document.getElementById('qualitySelect');
    const rate  = document.getElementById('playbackRate');
    const dlBtn = document.getElementById('dlBtn');
    const copyBtn = document.getElementById('copyBtn');
    const fullscreenBtn = document.getElementById('fullscreenBtn');
    const playerMessage = document.getElementById('playerMessage');
    const playerSpinner = document.getElementById('playerSpinner');
    const messageTitle = document.getElementById('messageTitle');
    const messageDetail = document.getElementById('messageDetail');
    const status = document.getElementById('streamStatus');
    const statusText = document.getElementById('statusText');
    rate.addEventListener('change', () => {{ video.playbackRate = Number(rate.value); }});
    let hls     = null;
    let dash    = null;
    let currentDlUrl = {best_dl};
    let currentFmt   = {best_fmt};

    function setStatus(text, error = false) {{
      statusText.textContent = text;
      status.classList.toggle('error', error);
    }}

    function showMessage(title, detail, spinning = false) {{
      messageTitle.textContent = title;
      messageDetail.textContent = detail;
      playerSpinner.hidden = !spinning;
      playerMessage.classList.remove('hidden');
    }}

    function loadSrc(streamUrl, fmt, dlUrl) {{
      const isHls = fmt === 'hls' || streamUrl.includes('.m3u8');
      const isDash = fmt === 'dash' || streamUrl.includes('.mpd');
      currentDlUrl = dlUrl; currentFmt = fmt;
      // Update button label based on format
      dlBtn.textContent = (isHls || isDash) ? 'Copy stream URL' : '↓ Download';
      showMessage('Preparing stream', 'Connecting to the video source…', true);
      setStatus('Loading stream');
      if (hls) {{ hls.destroy(); hls = null; }}
      if (dash) {{ dash.reset(); dash = null; }}
      if (isHls) {{
        if (Hls.isSupported()) {{
          hls = new Hls({{ enableWorker: true }});
          hls.loadSource(streamUrl);
          hls.attachMedia(video);
          hls.on(Hls.Events.MANIFEST_PARSED, () => {{ playerMessage.classList.add('hidden'); setStatus('Playing HLS stream'); video.play().catch(() => {{}}); }});
          hls.on(Hls.Events.ERROR, (_event, data) => {{
            if (data.fatal) {{ showMessage('Stream unavailable', 'The HLS source could not be loaded. Try another quality or try again later.'); setStatus('Stream error', true); }}
          }});
        }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
          video.src = streamUrl; video.play().catch(() => {{}});
        }} else {{
          showMessage('HLS is not supported', 'Try a modern browser with HLS playback support.');
          setStatus('Unsupported format', true);
        }}
      }} else if (isDash && window.dashjs) {{
        dash = dashjs.MediaPlayer().create();
        dash.initialize(video, streamUrl, true);
        dash.on(dashjs.MediaPlayer.events.STREAM_INITIALIZED, () => {{ playerMessage.classList.add('hidden'); setStatus('Playing DASH stream'); }});
        dash.on(dashjs.MediaPlayer.events.ERROR, () => {{ showMessage('Stream unavailable', 'The DASH source could not be loaded. Try another quality or try again later.'); setStatus('Stream error', true); }});
      }} else {{
        video.src = streamUrl; video.load();
      }}
    }}

    video.addEventListener('playing', () => {{ playerMessage.classList.add('hidden'); setStatus('Playing'); }});
    video.addEventListener('waiting', () => {{ showMessage('Buffering', 'The stream is loading…', true); setStatus('Buffering'); }});
    video.addEventListener('canplay', () => {{
      playerMessage.classList.add('hidden');
      if (video.paused) setStatus('Ready to play');
    }});
    video.addEventListener('error', () => {{ showMessage('Stream unavailable', 'The source may have expired or be temporarily unavailable. Try another quality.'); setStatus('Stream error', true); }});
    video.addEventListener('pause', () => {{ if (!video.ended) setStatus('Paused'); }});
    video.addEventListener('ended', () => setStatus('Playback ended'));

    fullscreenBtn.addEventListener('click', async () => {{
      try {{
        if (document.fullscreenElement) await document.exitFullscreen();
        else await document.querySelector('.player-shell').requestFullscreen();
      }} catch(e) {{ setStatus('Fullscreen unavailable', true); }}
    }});
    copyBtn.addEventListener('click', async () => {{
      try {{
        await navigator.clipboard.writeText(video.currentSrc || video.src || sel.value);
        copyBtn.textContent = 'Copied!';
        setTimeout(() => {{ copyBtn.textContent = 'Copy link'; }}, 1800);
      }} catch(e) {{
        prompt('Copy stream URL:', video.currentSrc || video.src || sel.value);
      }}
    }});

    document.addEventListener('keydown', (event) => {{
      if (event.target instanceof HTMLElement && event.target.matches('input,select,textarea,button')) return;
      if (event.code === 'Space') {{ event.preventDefault(); video.paused ? video.play().catch(() => {{}}) : video.pause(); }}
      if (event.key.toLowerCase() === 'f') fullscreenBtn.click();
      if (event.key === 'ArrowLeft') video.currentTime = Math.max(0, video.currentTime - 10);
      if (event.key === 'ArrowRight' && Number.isFinite(video.duration)) video.currentTime = Math.min(video.duration, video.currentTime + 10);
    }});

    dlBtn.addEventListener('click', async function() {{
      if (currentFmt === 'hls' || currentFmt === 'dash') {{
        // HLS can't be downloaded as a single file in the browser.
        // Copy the stream URL to clipboard and show a message.
        try {{
          await navigator.clipboard.writeText(currentDlUrl);
          dlBtn.textContent = 'Copied!';
          setTimeout(() => {{ dlBtn.textContent = 'Copy stream URL'; }}, 2000);
        }} catch(e) {{
          // Fallback: prompt user to copy manually
          const msg = 'HLS stream URL (copy and open in VLC):\\n' + currentDlUrl;
          prompt('HLS stream — open in VLC or copy URL:', currentDlUrl);
        }}
        return;
      }}
      dlBtn.textContent = 'Preparing…'; dlBtn.disabled = true;
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
    """Store with an optional site-specific TTL, then the global TTL fallback.
    
    Sites with time-limited CDN URLs use shorter TTLs:
      XH (XHamster): 15 min — CDN URLs expire in ~2h but we refresh early
      PH (PornHub):  15 min — same
    """
    if ttl is None:
        site = key.partition(":")[0].upper()
        # Sites with signed/expiring CDN URLs get shorter cache TTL
        _short_ttl_sites = {"XH": 900, "PH": 900}  # 15 minutes
        default_ttl = _short_ttl_sites.get(site, CACHE_TTL)
        ttl = int(os.environ.get(f"CACHE_TTL_{site}_SECONDS", str(default_ttl)))
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

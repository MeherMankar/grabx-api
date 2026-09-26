"""
Terabox + PornHub Downloader API
=================================

--- Terabox ---
Bypasses Terabox's verify_v2 CAPTCHA gate using the WAP (mobile) page trick.

Key insight: The WAP share page (http://www.terabox.com/wap/share/filelist?surl=...)
embeds window.__INITIAL_STATE__ in its HTML which contains the full file list
INCLUDING the dlink (direct download URL) — no CAPTCHA, no /share/download call needed.

Flow:
  1. Parse surl from any Terabox share URL format.
  2. Load the WAP page with ndus cookie.
  3. Extract window.__INITIAL_STATE__ JSON from the page HTML.
  4. Pull dlink, filename, size, thumbnail from the embedded file list.
  5. Return everything including a /proxy URL for streaming.

Requires TERABOX_COOKIE env var (at minimum: ndus=...).

--- PornHub ---
Scrapes the PH video watch page to extract stream/download links.

Key insight: Every PH watch page embeds a JavaScript object named
`flashvars_XXXXXXXX` inside a <script> block. That object contains a
`mediaDefinitions` array. One entry is a `/video/get_media?s=...` resolver
that returns signed time-limited direct MP4 URLs for each quality.

Flow:
  1. Validate and normalise the PH URL.
  2. Fetch the watch page using curl_cffi (Chrome TLS + HTTP/3 over QUIC to
     bypass ISP-level DPI blocks) with DoH to bypass DNS blocks.
  3. Extract flashvars_* and parse mediaDefinitions.
  4. Call get_media resolver to obtain signed MP4 URLs.
  5. Return title, thumbnail, duration, all quality variants, and proxy URLs.
  6. /ph/proxy streams content with correct Referer; for MP4 it redirects to
     the CDN directly (avoids Render free-tier 30s timeout).
  7. /ph/watch/<viewkey> serves an HTML page with embedded video player,
     quality selector, and download button.
"""

from flask import Flask, request, jsonify, Response, stream_with_context, redirect
import requests as req_lib
import os
import re
import json
import random
from urllib.parse import urlparse, parse_qs, quote, unquote

# Load .env file if present (local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# ===========================================================================
# API Key authentication
# ===========================================================================

# Set API_KEY (or GRABX_API_KEY) env var to enable auth. Leave unset to run without auth (open).
_API_KEY = (
    os.environ.get("API_KEY", "").strip()
    or os.environ.get("GRABX_API_KEY", "").strip()
)

# ---------------------------------------------------------------------------
# Cloudflare Worker proxy base URL.
# When set, ALL proxy_url / download_url fields in API responses point to the
# Worker instead of this Render instance.  Render then has zero streaming load.
#
# Set CF_WORKER_URL on Render to your deployed Worker URL, e.g.:
#   https://grabx-proxy.yourname.workers.dev
#
# Leave unset to fall back to Render-side streaming (the old behaviour).
# ---------------------------------------------------------------------------
_CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "").rstrip("/")

# Routes that are always public regardless of API_KEY setting.
_PUBLIC_ROUTES = {"/", "/docs", "/health", "/debug/headers"}

# Route *prefixes* that are always public (no API key AND no token needed).
# /ph/watch/* — browser player page (can't send headers from <video src>)
# /proxy and /ph/proxy — token-authenticated by _verify_proxy_token() inside the route
_PUBLIC_PREFIXES = ("/ph/watch/", "/ph/proxy", "/proxy", "/adult/proxy",
                   "/xv/watch", "/xnxx/watch", "/xh/watch",
                   "/jav/proxy", "/jav/watch")

@app.before_request
def _check_api_key():
    """Enforce X-API-Key header on all non-public routes when API_KEY is set."""
    if not _API_KEY:
        return  # auth not configured — open access
    if request.path in _PUBLIC_ROUTES:
        return  # always public
    if request.path.startswith(_PUBLIC_PREFIXES):
        return  # watch player + proxy stream always public
    # Accept the key from multiple common locations / header spellings
    auth_header = request.headers.get("Authorization", "")
    bearer_key  = auth_header.removeprefix("Bearer ").strip() if auth_header.lower().startswith("bearer ") else ""

    key = (
        request.headers.get("X-API-Key")        # canonical
        or request.headers.get("X-Api-Key")      # alternate casing
        or request.headers.get("apikey")         # some clients use this
        or request.headers.get("Api-Key")        # another common variant
        or request.headers.get("GRABX-API-KEY")  # grabx-specific header
        or request.headers.get("X-GRABX-API-KEY")
        or request.headers.get("GRABX_API_KEY")  # underscored variant (non-standard but used by some bots)
        or request.args.get("api_key")           # query param
        or request.args.get("apikey")            # query param alt
        or request.args.get("grabx_api_key")     # query param grabx variant
        or bearer_key                            # Authorization: Bearer <key>
    )
    if not key:
        return jsonify({
            "status": "error",
            "message": (
                "Missing API key. Accepted methods: "
                "X-API-Key header, Authorization: Bearer <key> header, "
                "or ?api_key= query param."
            ),
        }), 401
    if key != _API_KEY:
        return jsonify({"status": "error", "message": "Invalid API key."}), 403

# ===========================================================================
# Signed proxy token helpers
# ===========================================================================
#
# When auth is enabled, the bot/client authenticates ONCE with their API key
# via /download or /ph/download.  The returned proxy_url / download_url carry
# a short-lived HMAC token so anyone holding that URL (browser, video player,
# download manager) can stream without needing the raw API key.
#
# Token format (URL-safe base64, appended as ?_t=<token>&_e=<expiry>):
#   HMAC-SHA256( secret=API_KEY, msg="<expiry_unix_int>:<cdn_url>" )
#
# Default TTL: 24 hours.  Set PROXY_TOKEN_TTL_HOURS env var to override.
# ===========================================================================

import hmac
import hashlib
import base64
import time as _time

_TOKEN_TTL = int(os.environ.get("PROXY_TOKEN_TTL_HOURS", "24")) * 3600


def _sign_url(cdn_url: str) -> str:
    """
    Return a signed proxy URL string (just the _t and _e params to append).
    cdn_url is the raw CDN URL being protected (used as part of the HMAC msg).
    """
    if not _API_KEY:
        return ""
    expiry = int(_time.time()) + _TOKEN_TTL
    msg    = f"{expiry}:{cdn_url}".encode()
    sig    = hmac.new(_API_KEY.encode(), msg, hashlib.sha256).digest()
    token  = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"_t={token}&_e={expiry}"


def _verify_proxy_token(cdn_url: str) -> bool:
    """
    Return True if the request carries a valid signed token for cdn_url,
    OR if no API_KEY is configured (open mode).
    """
    if not _API_KEY:
        return True
    token_str = request.args.get("_t", "")
    expiry_str = request.args.get("_e", "")
    if not token_str or not expiry_str:
        return False
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if _time.time() > expiry:
        return False  # expired
    # Reconstruct expected signature
    msg      = f"{expiry}:{cdn_url}".encode()
    expected = hmac.new(_API_KEY.encode(), msg, hashlib.sha256).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
    return hmac.compare_digest(token_str, expected_b64)


def _make_proxy_url(base_url: str, path: str, cdn_url: str, extra: str = "",
                    viewkey: str = "", quality: str = "", src_domain: str = "",
                    ndus: str = "") -> str:
    """
    Build a full proxy URL with an embedded signed token.

    PH streams (/ph/proxy) go through CF Worker — PH CDN is IP-bound to
    Render's outbound IP when the link is generated, so we need the Worker
    to re-fetch from the same IP. CF Worker calls Render's /ph/download.

    Terabox streams (/proxy) ALWAYS go through Render — the ndus cookie is
    IP-bound to Render's server IP. CF Worker IPs are random edge nodes that
    Terabox rejects with errno 400141.
    """
    if _CF_WORKER_URL and path == "/ph/proxy":
        proxy_base = _CF_WORKER_URL
    else:
        # Terabox always via Render (ndus cookie is IP-locked to Render)
        proxy_base = base_url

    enc      = quote(cdn_url, safe="")
    token    = _sign_url(cdn_url)
    vk_part  = f"&vk={quote(viewkey)}"     if viewkey    else ""
    q_part   = f"&q={quote(quality)}"      if quality    else ""
    src_part = f"&src={quote(src_domain)}" if src_domain else ""
    if token:
        return f"{proxy_base}{path}?url={enc}&{token}{vk_part}{q_part}{src_part}{extra}"
    return f"{proxy_base}{path}?url={enc}{vk_part}{q_part}{src_part}{extra}"


def _check_raw_key() -> bool:
    """
    Return True if the request carries the valid raw API key in any accepted
    header / query param.  Used as a fallback in proxy routes so trusted
    callers can skip the token entirely.
    """
    if not _API_KEY:
        return True  # open mode
    auth_header = request.headers.get("Authorization", "")
    bearer_key  = auth_header.removeprefix("Bearer ").strip() if auth_header.lower().startswith("bearer ") else ""
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
        or bearer_key
    )
    return key == _API_KEY

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TERABOX_DOMAINS = [
    # Core brand
    ".terabox.com", ".dm.terabox.com", ".terabox.app",
    # 1024 family
    ".1024terabox.com", ".1024tera.com", ".1024tera.co",
    # App variants
    ".teraboxapp.com", ".teraboxapp.net",
    # Share/link domains
    ".teraboxlink.com", ".teraboxshare.com", ".terasharefile.com",
    ".terafileshare.com", ".terasharelink.com",
    # Mirror / alt brands
    ".nephobox.com", ".4funbox.co", ".4funbox.com",
    ".mirrobox.com", ".momerybox.com", ".tibibox.com",
    ".freeterabox.com", ".teraboxlink.com",
    ".terabox1.com", ".terabox2.com",
    # dubox (old Baidu brand name for TeraBox)
    ".dubox.com", ".dubox.co",
    # WW/naked subdomains seen in the wild
    ".ww.mirrobox.com",
]

# ---------------------------------------------------------------------------
# Regex pattern to accept ANY *.terabox.* / known-mirror URL.
# Used by parse_surl and fetch_wap_page to decide whether a URL looks like
# a Terabox share link at all.
# ---------------------------------------------------------------------------
_TERABOX_URL_RE = re.compile(
    r"""
    (?:^|\.)                    # start of hostname or a dot (subdomain boundary)
    (?:
        (?:(?:www|ww|m|dm)\.)? # optional common subdomains
        (?:
            terabox(?:app|link|share|1|2)?   |  # terabox, teraboxapp, teraboxlink …
            1024tera(?:box)?                  |  # 1024tera, 1024terabox
            nephobox                          |
            4funbox                           |
            mirrobox                          |
            momerybox                         |
            tibibox                           |
            freeterabox                       |
            terasharefile                     |
            terasharelink                     |
            terafileshare                     |
            dubox
        )
        \.(?:com|co|app|net|org|io)          # any TLD
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Ordered list of hostnames tried when fetching the WAP page.
# The first entry is the URL's own host (added dynamically in fetch_wap_page).
TERABOX_HOSTNAMES = [
    # Most reliable / canonical
    "www.terabox.com",
    "www.1024terabox.com",
    "www.teraboxapp.com",
    # Mirror brands
    "www.nephobox.com",
    "www.4funbox.co",
    "www.4funbox.com",
    "www.mirrobox.com",
    "ww.mirrobox.com",
    "www.momerybox.com",
    "www.tibibox.com",
    "www.freeterabox.com",
    # Share domains
    "www.teraboxlink.com",
    "www.terafileshare.com",
    "www.teraboxshare.com",
    "www.terasharefile.com",
    "www.terasharelink.com",
    # Numbered mirrors
    "www.terabox1.com",
    "www.terabox2.com",
    # 1024 family
    "www.1024tera.com",
    # dubox
    "www.dubox.com",
]


def _parse_ndus_list() -> list:
    cookie_str = os.environ.get("TERABOX_COOKIE", "").strip()
    if not cookie_str:
        raise ValueError(
            "TERABOX_COOKIE environment variable is not set. "
            "For a single account: ndus=YOUR_VALUE\n"
            "For multiple accounts: ndus=VALUE1,ndus=VALUE2,ndus=VALUE3"
        )
    accounts = []
    for entry in cookie_str.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.lower().startswith("ndus="):
            accounts.append(entry[5:].strip())
        else:
            found = False
            for part in entry.split(";"):
                part = part.strip()
                if part.lower().startswith("ndus="):
                    accounts.append(part[5:].strip())
                    found = True
                    break
            if not found:
                accounts.append(entry)
    if not accounts:
        raise ValueError("No valid ndus values found in TERABOX_COOKIE.")
    return accounts


def get_random_ndus() -> str:
    return random.choice(_parse_ndus_list())


def get_account_count() -> int:
    try:
        return len(_parse_ndus_list())
    except ValueError:
        return 0


def build_session(ndus: str) -> req_lib.Session:
    session = req_lib.Session()
    for domain in TERABOX_DOMAINS:
        session.cookies.set("ndus", ndus, domain=domain)
    return session


def parse_surl(share_url: str) -> str:
    # Validate that this actually looks like a Terabox URL
    parsed = urlparse(share_url)
    host = (parsed.hostname or "").lower()
    if not _TERABOX_URL_RE.search(host):
        raise ValueError(
            f"URL does not appear to be a Terabox share link (host: {host!r}). "
            "Supported: terabox.com, 1024terabox.com, nephobox.com, 4funbox.co, "
            "mirrobox.com, momerybox.com, tibibox.com, dubox.com and all their variants."
        )
    if "/s/" in parsed.path:
        surl = parsed.path.split("/s/")[-1].strip("/")
    else:
        qs = parse_qs(parsed.query)
        surl = qs.get("surl", [""])[0]
    if not surl:
        raise ValueError(f"Cannot extract surl from URL: {share_url}")
    if len(surl) > 22 and surl.startswith("1"):
        surl = surl[1:]
    if len(surl) < 8:
        raise ValueError(f"Invalid surl extracted: '{surl}' from URL: {share_url}")
    return surl


def fetch_wap_page(session: req_lib.Session, surl: str, share_url: str = "") -> tuple:
    candidates = []
    if share_url:
        host = urlparse(share_url).hostname or ""
        if host:
            candidates.append(f"http://{host}/wap/share/filelist?surl={surl}")
            candidates.append(f"https://{host}/wap/share/filelist?surl={surl}")
    for host in TERABOX_HOSTNAMES:
        url = f"https://{host}/wap/share/filelist?surl={surl}"
        if url not in candidates:
            candidates.append(url)
    candidates.append(f"http://www.terabox.com/wap/share/filelist?surl={surl}")

    headers = {"User-Agent": MOBILE_UA, "Accept": "text/html,*/*"}
    last_error = None

    for url in candidates:
        try:
            resp = session.get(url, headers=headers, allow_redirects=True, timeout=15)
            if resp.status_code == 200 and "__INITIAL_STATE__" in resp.text:
                final_surl = surl
                if "surl=" in resp.url:
                    final_surl = resp.url.split("surl=")[-1].split("&")[0]
                return resp.text, final_surl
        except req_lib.exceptions.ConnectionError as e:
            last_error = e
            continue
        except req_lib.exceptions.Timeout:
            last_error = Exception(f"Timeout fetching WAP page: {url}")
            continue

    raise ValueError(
        f"Could not load Terabox WAP page for surl={surl}. "
        f"Last error: {last_error}"
    )


def extract_file_info(html: str) -> list:
    m = re.search(
        r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*(?:;|</script>)',
        html, re.DOTALL,
    )
    if not m:
        raise ValueError("window.__INITIAL_STATE__ not found in WAP page HTML.")
    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        fl_m = re.search(r'"fileList"\s*:\s*(\[.+?\])\s*,\s*"', html, re.DOTALL)
        if not fl_m:
            raise ValueError("Could not parse file list from WAP page.")
        file_list = json.loads(fl_m.group(1))
        state = {"share": {"fileList": file_list}}
    file_list = state.get("share", {}).get("fileList", [])
    if not file_list:
        raise ValueError("No files found in WAP page state.")
    return file_list


def _human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} PB"


def fetch_folder_file_list(session: req_lib.Session, surl: str,
                           dir_path: str, share_id: str,
                           uk: str, sign: str, timestamp: str) -> list:
    """
    Fetch file list for a sub-folder inside a Terabox share using the
    /share/list JSON API.  Returns a list of raw file-info dicts.
    """
    url = "https://www.terabox.com/share/list"
    params = {
        "app_id":    "250528",
        "shorturl":  surl,
        "root":      "0",
        "dir":       dir_path,
        "shareid":   share_id,
        "uk":        uk,
        "sign":      sign,
        "timestamp": timestamp,
        "num":       "100",
        "page":      "1",
        "order":     "name",
        "desc":      "0",
    }
    headers = {"User-Agent": DESKTOP_UA, "Referer": "https://www.terabox.com/"}
    try:
        resp = session.get(url, params=params, headers=headers, timeout=15)
        data = resp.json()
        return data.get("list", [])
    except Exception:
        return []


def _extract_share_meta(html: str) -> dict:
    """
    Pull shareid, uk, sign, timestamp out of window.__INITIAL_STATE__ so we
    can make authenticated /share/list calls for sub-folders.
    """
    m = re.search(
        r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*(?:;|</script>)',
        html, re.DOTALL,
    )
    if not m:
        return {}
    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    share = state.get("share", {})
    return {
        "shareid":   str(share.get("shareid", "")),
        "uk":        str(share.get("uk", "")),
        "sign":      share.get("sign", ""),
        "timestamp": str(share.get("timestamp", "")),
    }


def _collect_files_recursive(session: req_lib.Session, items: list,
                              surl: str, meta: dict,
                              depth: int = 0, max_depth: int = 8) -> list:
    """
    Walk a Terabox file list recursively.  Folders are expanded via
    /share/list; files are collected as-is.  Returns a flat list of file dicts
    each annotated with 'folder_path'.
    """
    results = []
    if depth > max_depth:
        return results

    for item in items:
        if str(item.get("isdir", "0")) == "1":
            # It's a folder — recurse into it
            dir_path = item.get("path", "")
            if not dir_path or not all(meta.get(k) for k in ("shareid", "uk")):
                continue  # can't recurse without share metadata
            children = fetch_folder_file_list(
                session, surl, dir_path,
                meta["shareid"], meta["uk"],
                meta.get("sign", ""), meta.get("timestamp", ""),
            )
            results.extend(
                _collect_files_recursive(session, children, surl, meta,
                                         depth + 1, max_depth)
            )
        else:
            results.append(item)

    return results


def _extract_video_quality(item: dict) -> dict:
    """
    Pull video resolution / quality info from a Terabox file-list item.
    Terabox embeds video metadata inside the `video_info` sub-object when
    present.  Falls back to parsing the filename for common patterns.
    """
    quality: dict = {}

    video_info = item.get("video_info") or {}
    if video_info:
        # width / height
        w = video_info.get("width") or video_info.get("video_width")
        h = video_info.get("height") or video_info.get("video_height")
        if w and h:
            quality["width"]  = int(w)
            quality["height"] = int(h)
            quality["resolution"] = f"{w}x{h}"
            # derive a standard label
            for threshold, label in ((2160, "4K"), (1440, "2K"), (1080, "1080p"),
                                     (720, "720p"), (480, "480p"), (360, "360p")):
                if int(h) >= threshold:
                    quality["label"] = label
                    break
            else:
                quality["label"] = f"{h}p"

        dur = video_info.get("duration")
        if dur:
            secs = int(float(dur))
            quality["duration_seconds"] = secs
            quality["duration"] = f"{secs // 60}:{secs % 60:02d}"

        fps = video_info.get("frame_rate") or video_info.get("fps")
        if fps:
            quality["fps"] = round(float(fps), 2)

        vbitrate = video_info.get("bit_rate") or video_info.get("vbitrate")
        if vbitrate:
            quality["bitrate_kbps"] = round(int(vbitrate) / 1000, 1)

    if not quality.get("resolution"):
        # Fallback: scan filename for resolution hints like 1080p / 4K / 2160p
        fname = item.get("server_filename", "")
        for pat, label in (
            (r'4k|2160p', "4K"), (r'2k|1440p', "2K"),
            (r'1080p', "1080p"), (r'720p', "720p"),
            (r'480p', "480p"), (r'360p', "360p"),
        ):
            if re.search(pat, fname, re.IGNORECASE):
                quality["label"] = label
                break

    return quality if quality else {}


# ===========================================================================
# PornHub — constants & helpers
# ===========================================================================

PH_COOKIE_DOMAINS = [".pornhub.com", ".pornhub.net", ".pornhub.org", ".pornhubpremium.com", ".thumbzilla.com"]

PH_AGE_COOKIE = {
    "accessAgeDisclaimerPH": "1",
    "accessAgeDisclaimerUK": "1",
    "accessPH": "1",
    "age_verified": "1",
    "platform": "pc",
}

# ---------------------------------------------------------------------------
# Regex that matches every official PornHub / Thumbzilla hostname variant.
# Covers: pornhub.com, pornhubpremium.com, pornhub.net, pornhub.org,
#         <lang>.pornhub.com (cn/de/fr/es/it/nl/pt/pl/jp/ru/cz/ar/…),
#         www.thumbzilla.com, thumbzilla.com, and the .onion mirror.
# ---------------------------------------------------------------------------
_PH_HOST_RE = re.compile(
    r"""
    ^
    (?:
        # All pornhub variants
        (?:[a-z]{2}\.)?              # optional 2-letter language prefix (cn., de., fr. …)
        (?:www\.)?                   # optional www.
        pornhub(?:premium)?          # pornhub or pornhubpremium
        \.(?:com|net|org)            # TLD
    |
        # Thumbzilla (PH-owned tube)
        (?:www\.)?thumbzilla\.com
    |
        # .onion mirror (accessed via Tor)
        www\.pornhubvybmsymdol4iibwgwtkpwmeyd6luq2gxajgjzfjvotyt5zhyd\.onion
    )
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _cffi_session():
    """Return a curl_cffi session with PH age-gate cookies pre-set."""
    from curl_cffi import requests as cffi_req
    session = cffi_req.Session(impersonate="chrome124")
    for domain in PH_COOKIE_DOMAINS:
        for name, value in PH_AGE_COOKIE.items():
            session.cookies.set(name, value, domain=domain)
    return session


def _ph_validate_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    full_host = (parsed.hostname or "").lower()
    if not _PH_HOST_RE.match(full_host):
        raise ValueError(
            f"Not a supported PornHub URL (host: {full_host!r}). "
            "Supported: pornhub.com, pornhubpremium.com, pornhub.net, pornhub.org, "
            "<lang>.pornhub.com (cn/de/fr/es/it/nl/pt/pl/jp/ru/…), thumbzilla.com."
        )
    qs = parse_qs(parsed.query)
    has_viewkey = bool(qs.get("viewkey"))
    has_video_path = "/video" in parsed.path or "/view_video" in parsed.path
    if not (has_viewkey or has_video_path):
        raise ValueError(
            "URL does not point to a PornHub video page. "
            "Expected /view_video.php?viewkey=... or /video/..."
        )
    return url


def _ph_fetch_page(url: str) -> str:
    """
    Fetch a PH video watch page using curl_cffi + HTTP/3 + DoH.
    HTTP/3 over QUIC bypasses ISP DPI (TCP RST injection on SNI).
    DoH bypasses DNS-level blocks.
    """
    try:
        session = _cffi_session()
        resp = session.get(
            url,
            allow_redirects=True,
            timeout=20,
            http_version=3,
            doh_url="https://1.1.1.1/dns-query",
        )
    except Exception as e:
        err = str(e)
        if "resolve host" in err.lower() or "dns" in err.lower():
            raise ValueError("Could not resolve PornHub hostname — domain may be DNS-blocked on this server.")
        raise ValueError(f"Network error fetching PornHub page: {e}")

    if resp.status_code != 200:
        raise ValueError(
            f"PornHub returned HTTP {resp.status_code}. "
            "Video may be private, deleted, or geo-restricted."
        )
    html = resp.text
    if "restrictions_age_disclaimer" in html or "You must be" in html:
        raise ValueError("PornHub returned an age-gate page. Age-bypass cookie may have stopped working.")
    return html


def _ph_extract_flashvars(html: str) -> dict:
    m = re.search(r'var\s+flashvars_\d+\s*=\s*(\{.*?\})\s*;', html, re.DOTALL)
    if not m:
        raise ValueError(
            "Could not find flashvars in the PornHub page. "
            "Page structure may have changed or the video is unavailable."
        )
    raw = m.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raw = re.sub(r'//[^\n]*', '', raw)
        raw = re.sub(r',\s*([}\]])', r'\1', raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse PornHub flashvars JSON: {e}")


def _ph_extract_metadata(html: str) -> dict:
    meta = {}
    title_m    = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
    og_title_m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html)
    raw_title  = (og_title_m.group(1) if og_title_m else (title_m.group(1) if title_m else "")).strip()
    raw_title  = re.sub(r'\s*[-|]\s*Pornhub\.?com\s*$', '', raw_title, flags=re.IGNORECASE).strip()
    meta["title"] = raw_title or "Unknown Title"

    thumb_m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
    meta["thumbnail"] = thumb_m.group(1) if thumb_m else ""

    dur_m = re.search(r'<meta[^>]+property=["\']og:video:duration["\'][^>]+content=["\'](\d+)["\']', html)
    if not dur_m:
        dur_m = re.search(r'"duration"\s*:\s*"?(\d+)"?', html)
    if dur_m:
        secs = int(dur_m.group(1))
        meta["duration_seconds"] = secs
        meta["duration"] = f"{secs // 60}:{secs % 60:02d}"
    else:
        meta["duration_seconds"] = 0
        meta["duration"] = ""

    vk_m = re.search(r'viewkey[=_]([a-z0-9]+)', html, re.IGNORECASE)
    meta["viewkey"] = vk_m.group(1) if vk_m else ""
    return meta


def _ph_resolve_get_media(get_media_url: str) -> list:
    """Call the get_media API to get signed direct MP4 URLs, sorted best-first."""
    try:
        session = _cffi_session()
        r = session.get(
            get_media_url,
            headers={
                "Referer":          "https://www.pornhub.com/",
                "Origin":           "https://www.pornhub.com",
                "Accept":           "application/json, text/plain, */*",
                "Accept-Language":  "en-US,en;q=0.9",
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=True,
            timeout=15,
            http_version=3,
            doh_url="https://1.1.1.1/dns-query",
        )
        if r.status_code != 200:
            import sys
            print(f"[get_media] HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return []
        items = r.json()
        if not isinstance(items, list):
            import sys
            print(f"[get_media] unexpected response type: {type(items)} — {str(items)[:200]}", file=sys.stderr)
            return []
        entries = []
        for item in items:
            video_url = (item.get("videoUrl") or "").strip()
            quality   = str(item.get("quality") or "").strip()
            fmt       = str(item.get("format") or "mp4").lower()
            if not video_url or not quality:
                continue
            entries.append({"quality": quality, "url": video_url, "format": fmt})
        entries.sort(key=lambda e: -int(e["quality"]) if e["quality"].isdigit() else 0)
        return entries
    except Exception as e:
        import sys
        print(f"[get_media] exception: {e}", file=sys.stderr)
        return []


def _ph_parse_qualities(flashvars: dict) -> tuple:
    """
    Parse mediaDefinitions from flashvars.
    Returns (hls_entries, get_media_url).
    """
    raw_defs = flashvars.get("mediaDefinitions", [])
    entries = []
    get_media_url = ""

    for item in raw_defs:
        if not isinstance(item, dict):
            continue
        video_url = (item.get("videoUrl") or "").strip()
        fmt       = (item.get("format") or item.get("mediaType") or "").lower()
        quality   = item.get("quality", "")

        if isinstance(quality, list) or (not quality and "get_media" in video_url):
            if video_url:
                if video_url.startswith("/"):
                    video_url = "https://www.pornhub.com" + video_url
                get_media_url = video_url
            continue

        quality = str(quality).strip()
        if not video_url:
            continue
        if not fmt:
            fmt = "hls" if ".m3u8" in video_url else "mp4"
        if not quality:
            quality = "hls" if fmt == "hls" else "unknown"

        entries.append({"quality": quality, "url": video_url, "format": fmt})

    def _sort_key(e):
        q = e["quality"]
        try:
            return (0, -int(q))
        except ValueError:
            return (1, 0) if q == "hls" else (2, 0)

    entries.sort(key=_sort_key)
    return entries, get_media_url


def _ph_get_all_qualities(ph_url: str):
    """
    Full pipeline: fetch page → extract flashvars → resolve MP4s + HLS.
    Returns (meta, all_qualities) or raises ValueError.
    """
    ph_url    = _ph_validate_url(ph_url)
    html      = _ph_fetch_page(ph_url)
    flashvars = _ph_extract_flashvars(html)
    meta      = _ph_extract_metadata(html)
    hls_qs, get_media_url = _ph_parse_qualities(flashvars)
    mp4_qs    = _ph_resolve_get_media(get_media_url) if get_media_url else []

    # _ph_resolve_get_media can fail silently (IP block, timeout).
    # Retry once if it returned nothing but we have a get_media URL.
    if not mp4_qs and get_media_url:
        import sys
        print(f"[ph_qualities] first get_media attempt returned empty, retrying. url={get_media_url[:80]}", file=sys.stderr)
        mp4_qs = _ph_resolve_get_media(get_media_url)
        print(f"[ph_qualities] retry result: {len(mp4_qs)} MP4 entries", file=sys.stderr)
    else:
        import sys
        print(f"[ph_qualities] got {len(mp4_qs)} MP4 entries, {len([q for q in hls_qs if q['format']=='hls'])} HLS entries", file=sys.stderr)

    hls_only = [q for q in hls_qs if q["format"] == "hls"]

    # Prefer MP4 (direct, seekable) over HLS. Fall back to HLS if no MP4.
    all_qs = mp4_qs + hls_only if (mp4_qs or hls_only) else hls_qs

    if not all_qs:
        raise ValueError(
            "No downloadable streams found. "
            "Video may be premium-only, private, or page structure changed."
        )
    return meta, all_qs


# ===========================================================================
# Routes
# ===========================================================================

@app.route("/")
def home():
    return jsonify({
        "status": "active",
        "message": "Terabox + PornHub Downloader API",
        "creator": "Maintained by MeherMankar (t.me/MeherPatil) | Base by genxnano (t.me/genxnano)",
        "accounts_configured": get_account_count(),
        "auth": "enabled (X-API-Key required)" if _API_KEY else "disabled (open access)",
        "proxy_backend": _CF_WORKER_URL if _CF_WORKER_URL else "render (this server)",
        "endpoints": {
            "/download": {
                "method": "POST",
                "description": "Get direct download link(s) for a Terabox share URL (supports folders, recursive)",
                "body": {"url": "Terabox share URL"},
                "auth_required": bool(_API_KEY),
            },
            "/proxy": {
                "method": "GET",
                "description": "Proxy-stream a Terabox dlink through this server",
                "params": {"url": "The dlink URL to proxy"},
                "auth_required": bool(_API_KEY),
            },
            "/ph/download": {
                "method": "POST",
                "description": "Extract video stream/download links from a PornHub watch page",
                "body": {"url": "PornHub video URL (view_video.php?viewkey=...)"},
                "auth_required": bool(_API_KEY),
            },
            "/ph/watch/<viewkey>": {
                "method": "GET",
                "description": "Browser video player with quality selector and download button (always public)",
                "example": "/ph/watch/6a165f5d3a96c",
                "auth_required": False,
            },
            "/ph/proxy": {
                "method": "GET",
                "description": "Stream or download a PH CDN URL (adds Referer/cookies); MP4 → 302 redirect, HLS → proxied",
                "params": {"url": "PH CDN URL", "dl": "1=download, 0=stream (default)"},
                "auth_required": bool(_API_KEY),
            },
            "/docs": {
                "method": "GET",
                "description": "API documentation",
                "auth_required": False,
            },
            "/health": {
                "method": "GET",
                "description": "Health check",
                "auth_required": False,
            },
            "/xv/download": {
                "method": "POST",
                "description": "Extract stream/download links from an Xvideos video",
                "body": {"url": "Xvideos video URL"},
                "auth_required": bool(_API_KEY),
            },
            "/xv/watch": {
                "method": "GET",
                "description": "Browser video player for Xvideos",
                "params": {"url": "Xvideos video URL"},
                "auth_required": False,
            },
            "/xnxx/download": {
                "method": "POST",
                "description": "Extract stream/download links from an XNXX video",
                "body": {"url": "XNXX video URL"},
                "auth_required": bool(_API_KEY),
            },
            "/xnxx/watch": {
                "method": "GET",
                "description": "Browser video player for XNXX",
                "params": {"url": "XNXX video URL"},
                "auth_required": False,
            },
            "/xh/download": {
                "method": "POST",
                "description": "Extract stream/download links from an XHamster video",
                "body": {"url": "XHamster video URL"},
                "auth_required": bool(_API_KEY),
            },
            "/xh/watch": {
                "method": "GET",
                "description": "Browser video player for XHamster",
                "params": {"url": "XHamster video URL"},
                "auth_required": False,
            },
            "/adult/proxy": {
                "method": "GET",
                "description": "Proxy CDN streams for Xvideos / XNXX / XHamster (token-authenticated)",
                "params": {"url": "CDN URL", "dl": "1=download, 0=stream"},
                "auth_required": False,
            },
            "/jav/download": {
                "method": "POST",
                "description": "Extract stream/download URLs for a JAVtiful video",
                "body": {"url": "JAVtiful video URL (javtiful.com/video/<id>/<slug>)"},
                "auth_required": bool(_API_KEY),
            },
            "/jav/watch": {
                "method": "GET",
                "description": "Browser video player for JAVtiful",
                "params": {"url": "JAVtiful video URL"},
                "auth_required": False,
            },
            "/jav/proxy": {
                "method": "GET",
                "description": "Proxy JAVtiful CDN streams (token-authenticated)",
                "params": {"url": "CDN URL", "dl": "1=download, 0=stream"},
                "auth_required": False,
            },
        },
    })


@app.route("/health")
def health():
    import sys, platform
    return jsonify({
        "status": "ok",
        "python": sys.version,
        "platform": platform.platform(),
        "accounts_configured": get_account_count(),
        "auth": "enabled" if _API_KEY else "disabled",
        "proxy_backend": _CF_WORKER_URL if _CF_WORKER_URL else "render (this server)",
    })


@app.route("/debug/headers")
def debug_headers():
    """
    Returns all request headers as received by the server.
    Always public — use this to verify your client is sending the key correctly.
    Only active when FLASK_DEBUG=true or DEBUG_HEADERS=true.
    """
    if not (os.environ.get("FLASK_DEBUG", "").lower() == "true"
            or os.environ.get("DEBUG_HEADERS", "").lower() == "true"):
        return jsonify({"status": "error", "message": "Set DEBUG_HEADERS=true to enable this endpoint."}), 403
    headers = {k: v for k, v in request.headers}
    # Mask the actual key value for safety
    for h in list(headers):
        if "key" in h.lower() or "auth" in h.lower():
            v = headers[h]
            headers[h] = v[:4] + "****" + v[-2:] if len(v) > 6 else "****"
    return jsonify({"headers": headers, "args": dict(request.args)})


# ---------------------------------------------------------------------------
# Terabox routes
# ---------------------------------------------------------------------------

@app.route("/download", methods=["POST"])
def download():
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' field is required in JSON body"}), 400

    share_url = body["url"].strip()
    try:
        ndus      = get_random_ndus()
        session   = build_session(ndus)
        surl      = parse_surl(share_url)
        html, _   = fetch_wap_page(session, surl, share_url)
        file_list = extract_file_info(html)

        # Extract share metadata (shareid, uk, sign, timestamp) needed for
        # recursive sub-folder fetches via /share/list.
        share_meta = _extract_share_meta(html)
        share_meta["surl"] = surl

        # Flatten everything — recurse into any directories found.
        flat_items = _collect_files_recursive(session, file_list, surl, share_meta)

        files    = []
        base_url = request.host_url.rstrip("/")

        for item in flat_items:
            dlink      = item.get("dlink", "")
            thumbs     = item.get("thumbs") or {}
            thumbnail  = (thumbs.get("url3") or thumbs.get("url2") or
                          thumbs.get("url1") or thumbs.get("icon") or "")
            size_bytes = int(item.get("size", 0))
            proxy_url  = _make_proxy_url(base_url, "/proxy", dlink) if dlink else ""

            # Folder path the file lives in (relative to share root)
            folder_path = item.get("path", "")
            parent_dir  = "/".join(folder_path.split("/")[:-1]) if folder_path else ""

            # Video quality / resolution info (empty dict for non-video files)
            quality = _extract_video_quality(item)

            file_entry = {
                "filename":    item.get("server_filename", ""),
                "folder":      parent_dir,
                "size_bytes":  size_bytes,
                "size":        _human_size(size_bytes),
                "thumbnail":   thumbnail,
                "dlink":       dlink,
                "proxy_url":   proxy_url,
                "fs_id":       str(item.get("fs_id", "")),
            }
            if quality:
                file_entry["video_quality"] = quality

            files.append(file_entry)

        if not files:
            return jsonify({"status": "error", "message": "Share contains no downloadable files."}), 404

        has_dlink = any(f["dlink"] for f in files)

        # Build a human-friendly title
        if len(files) == 1:
            title = files[0]["filename"]
        else:
            # count unique folders
            folders = {f["folder"] for f in files if f["folder"]}
            title = f"{len(files)} files" + (f" across {len(folders)} folders" if folders else "")

        return jsonify({
            "status": "success",
            "data": {
                "title":              title,
                "total_files":        len(files),
                "files":              files,
                "download_available": has_dlink,
                "note": (
                    "Use 'dlink' with a download manager (needs Terabox ndus cookie), "
                    "or use 'proxy_url' to stream through this server."
                    if has_dlink else "No direct download link available."
                ),
            },
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except req_lib.exceptions.RequestException as e:
        return jsonify({"status": "error", "message": f"Network error: {e}"}), 502
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal error: {e}"}), 500


@app.route("/proxy")
def proxy():
    """Proxy-stream a Terabox dlink. Attaches ndus cookie + Referer."""
    dlink = request.args.get("url", "").strip()
    if not dlink:
        return jsonify({"status": "error", "message": "'url' query param required"}), 400

    # Token auth: must have a valid signed token OR the raw API key in the header
    if not _verify_proxy_token(dlink) and not _check_raw_key():
        return jsonify({
            "status": "error",
            "message": "Access denied. Use the proxy_url returned by /download (contains a signed token), or pass a valid X-API-Key header.",
        }), 403

    try:
        ndus     = get_random_ndus()
        session  = build_session(ndus)
        upstream = session.get(
            dlink,
            headers={"User-Agent": DESKTOP_UA, "Referer": "https://www.terabox.com/", "Accept": "*/*"},
            stream=True, allow_redirects=True, timeout=30,
        )
        cd      = upstream.headers.get("Content-Disposition", "")
        fname_m = re.search(r'filename[*]?=["\']?([^"\';\n]+)', cd)
        fname   = fname_m.group(1).strip() if fname_m else "download"

        def generate():
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        resp_headers = {"Content-Disposition": f'attachment; filename="{fname}"', "Accept-Ranges": "bytes"}
        if cl := upstream.headers.get("Content-Length"):
            resp_headers["Content-Length"] = cl
        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            content_type=upstream.headers.get("Content-Type", "application/octet-stream"),
            headers=resp_headers,
        )
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Proxy error: {e}"}), 502


@app.route("/docs")
def docs():
    try:
        docs_path = os.path.join(os.path.dirname(__file__), "..", "docs.md")
        with open(docs_path, "r") as f:
            return f.read(), 200, {"Content-Type": "text/markdown; charset=utf-8"}
    except FileNotFoundError:
        return "Documentation not found.", 404


# ---------------------------------------------------------------------------
# PornHub routes
# ---------------------------------------------------------------------------


def _resolve_hls_uri(uri: str, base_dir: str, parsed_base) -> str:
    """
    Resolve a URI found inside an HLS manifest to an absolute URL.

    HLS segments can be:
      - Already absolute:  https://cdn.example.com/seg-1.ts
      - Protocol-relative: //cdn.example.com/seg-1.ts
      - Root-relative:     /videos/seg-1.ts
      - Relative:          seg-1.ts  or  ../seg-1.ts
    """
    if uri.startswith("http://") or uri.startswith("https://"):
        return uri
    if uri.startswith("//"):
        return parsed_base.scheme + ":" + uri
    if uri.startswith("/"):
        return f"{parsed_base.scheme}://{parsed_base.netloc}{uri}"
    # Relative — join with the directory of the manifest URL
    return base_dir + uri

@app.route("/ph/download", methods=["POST"])
def ph_download():
    """Extract all quality variants + proxy/download URLs for a PH video."""
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' field is required in JSON body"}), 400

    try:
        meta, all_qualities = _ph_get_all_qualities(body["url"].strip())

        base_url = request.host_url.rstrip("/")
        vk = meta.get("viewkey", "")
        for q in all_qualities:
            ql = str(q.get("quality", ""))
            q["proxy_url"]    = _make_proxy_url(base_url, "/ph/proxy", q["url"], viewkey=vk, quality=ql)
            q["download_url"] = _make_proxy_url(base_url, "/ph/proxy", q["url"], extra="&dl=1", viewkey=vk, quality=ql)

        mp4s          = [q for q in all_qualities if q["format"] == "mp4"]
        best           = mp4s[0] if mp4s else all_qualities[0]
        best_url      = best["url"]
        best_ql       = str(best.get("quality", ""))
        best_fmt      = best.get("format", "mp4")
        best_proxy    = _make_proxy_url(base_url, "/ph/proxy", best_url, viewkey=vk, quality=best_ql)
        best_download = _make_proxy_url(base_url, "/ph/proxy", best_url, extra="&dl=1", viewkey=vk, quality=best_ql)
        watch_url     = f"{base_url}/ph/watch/{meta['viewkey']}" if meta.get("viewkey") else ""

        # When only HLS is available the raw proxy URL opens as a manifest file
        # in browsers — steer users to the watch page instead.
        stream_note = (
            "Open 'watch_url' in a browser for the built-in video player. "
            + ("Use 'best_proxy_url' to stream (MP4) or 'best_download_url' to download. "
               if best_fmt == "mp4"
               else "MP4 streams unavailable — use 'watch_url' for browser playback (HLS via built-in player). ")
            + "Or pick any quality from 'qualities' and use its proxy_url / download_url."
        )

        return jsonify({
            "status": "success",
            "data": {
                "title":             meta["title"],
                "thumbnail":         meta["thumbnail"],
                "duration":          meta["duration"],
                "duration_seconds":  meta["duration_seconds"],
                "viewkey":           meta["viewkey"],
                "watch_url":         watch_url,
                "best_format":       best_fmt,
                "qualities":         all_qualities,
                "best_url":          best_url,
                "best_proxy_url":    best_proxy,
                "best_download_url": best_download,
                "note":              stream_note,
            },
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal error: {e}"}), 500


@app.route("/ph/watch/<viewkey>")
def ph_watch(viewkey: str):
    """
    Browser video player for a PornHub video.
    GET /ph/watch/<viewkey>  e.g.  /ph/watch/6a165f5d3a96c
    """
    try:
        meta, all_qualities = _ph_get_all_qualities(
            f"https://www.pornhub.com/view_video.php?viewkey={viewkey}"
        )
    except Exception as e:
        return f"""<!DOCTYPE html><html><head><title>Error</title></head>
        <body style="background:#0f0f0f;color:#eee;font-family:sans-serif;padding:40px">
        <h2 style="color:#f55">Could not load video</h2><p>{e}</p>
        </body></html>""", 500

    base_url = request.host_url.rstrip("/")

    all_opts = []
    for q in all_qualities:
        ql  = str(q.get("quality", ""))
        fmt = q.get("format", "mp4")
        # Label: prefer "1080p MP4", fall back to "1080p HLS"
        label = f"{ql}p {fmt.upper()}" if ql else fmt.upper()
        all_opts.append({
            "label":    label,
            "fmt":      fmt,
            "proxy":    _make_proxy_url(base_url, "/ph/proxy", q["url"], viewkey=viewkey, quality=ql),
            "download": _make_proxy_url(base_url, "/ph/proxy", q["url"], extra="&dl=1", viewkey=viewkey, quality=ql),
        })

    # Prefer MP4 at top; HLS options still included as fallback
    mp4_opts = [o for o in all_opts if o["fmt"] == "mp4"]
    hls_opts = [o for o in all_opts if o["fmt"] == "hls"]
    sorted_opts = mp4_opts + hls_opts  # MP4 first, HLS below

    if not sorted_opts:
        return "<h2>No streams found for this video.</h2>", 404

    best_stream   = sorted_opts[0]["proxy"]
    best_download = sorted_opts[0]["download"]
    title         = meta["title"]
    thumbnail     = meta.get("thumbnail", "")
    duration      = meta.get("duration", "")

    quality_options_html = "\n".join(
        f'<option value="{o["proxy"]}" data-fmt="{o["fmt"]}" data-dl="{o["download"]}">{o["label"]}</option>'
        for o in sorted_opts
    )

    best_is_hls   = sorted_opts[0]["fmt"] == "hls"

    page = f"""<!DOCTYPE html>
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
          padding:9px 18px;border-radius:6px;text-decoration:none;white-space:nowrap;transition:background .15s;cursor:pointer;border:none}}
    .btn-dl{{background:#f90;color:#000}}.btn-dl:hover{{background:#e88600}}
    .btn-dl:disabled{{background:#666;cursor:not-allowed}}
    .meta{{margin-top:10px;font-size:.8rem;color:#666}}
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
      <select id="qualitySelect" title="Select quality">
        {quality_options_html}
      </select>
      <button id="dlBtn" class="btn btn-dl">&#8595; Download</button>
    </div>
    <div class="meta">{'Duration: ' + duration + ' &nbsp;·&nbsp; ' if duration else ''}Powered by <a href="https://github.com/MeherMankar/grabx-api" target="_blank" style="color:#f90;text-decoration:none">GrabX API</a></div>
    <p class="note">Tip: right-click the video → "Save video as" to download directly from CDN.</p>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
  <script>
    const video = document.getElementById('player');
    const sel   = document.getElementById('qualitySelect');
    const dlBtn = document.getElementById('dlBtn');
    let hls     = null;
    let currentDlUrl = '{best_download}';
    let currentFmt   = '{sorted_opts[0]["fmt"]}';

    function loadSrc(streamUrl, fmt, dlUrl) {{
      const isHls = fmt === 'hls' || streamUrl.includes('.m3u8');
      currentDlUrl = dlUrl;
      currentFmt   = fmt;
      if (hls) {{ hls.destroy(); hls = null; }}
      if (isHls) {{
        if (Hls.isSupported()) {{
          hls = new Hls({{ enableWorker: true, lowLatencyMode: false }});
          hls.loadSource(streamUrl);
          hls.attachMedia(video);
          hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {{}}));
        }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
          video.src = streamUrl;
          video.play().catch(() => {{}});
        }}
      }} else {{
        video.src = streamUrl;
        video.load();
      }}
    }}

    // Download: for HLS use the download_url which streams server-side as attachment.
    // For MP4 fetch as blob.
    dlBtn.addEventListener('click', async function() {{
      const isHls = currentFmt === 'hls';
      if (isHls) {{
        // HLS can't be downloaded as a single file from the browser.
        // Open the &dl=1 proxy URL in a new tab — server will stream it as attachment.
        window.open(currentDlUrl, '_blank');
        return;
      }}
      dlBtn.textContent = 'Preparing...';
      dlBtn.disabled = true;
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
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(blobUrl), 10000);
      }} catch (e) {{
        window.open(currentDlUrl, '_blank');
      }} finally {{
        dlBtn.textContent = '↓ Download';
        dlBtn.disabled = false;
      }}
    }});

    // Load initial stream
    const firstOpt = sel.options[sel.selectedIndex];
    loadSrc(firstOpt.value, firstOpt.dataset.fmt, firstOpt.dataset.dl);

    sel.addEventListener('change', function() {{
      const opt = this.options[this.selectedIndex];
      loadSrc(opt.value, opt.dataset.fmt, opt.dataset.dl);
    }});
  </script>
</body>
</html>"""
    return page, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/ph/proxy")
def ph_proxy():
    """
    Proxy a PH CDN URL with the correct Referer + cookies.

    For MP4 (stream mode):  resolves to final CDN URL → 302 redirect.
                            Browser fetches directly, no Render timeout risk.
    For MP4 (dl=1 mode):   streams through server with attachment header.
    For HLS master/media manifest (.m3u8):
                            Fetched through server, all segment and child-playlist
                            URIs are rewritten to go back through /ph/proxy so the
                            client never needs the CDN Referer cookie itself.
    For .ts segments:       streamed through server (Referer required by CDN).

    GET /ph/proxy?url=<encoded_url>&dl=0|1
    """
    cdn_url = request.args.get("url", "").strip()
    if not cdn_url:
        return jsonify({"status": "error", "message": "'url' query param required"}), 400

    # Token auth: must have a valid signed token OR the raw API key in the header
    if not _verify_proxy_token(cdn_url) and not _check_raw_key():
        return jsonify({
            "status": "error",
            "message": "Access denied. Use the proxy_url returned by /ph/download (contains a signed token), or pass a valid X-API-Key header.",
        }), 403

    # If a browser opens a raw HLS proxy URL directly, redirect to the watch
    # player page instead — browsers can't play .m3u8 natively.
    viewkey = request.args.get("vk", "")
    is_m3u8 = ".m3u8" in cdn_url
    if is_m3u8 and viewkey:
        accept = request.headers.get("Accept", "")
        # Browsers send text/html in Accept; media players / XHR don't
        is_browser = "text/html" in accept and "application/x-mpegurl" not in accept.lower()
        if is_browser:
            return redirect(f"{request.host_url.rstrip('/')}/ph/watch/{viewkey}", code=302)

    download_mode = request.args.get("dl", "0") == "1"
    is_m3u8       = ".m3u8" in cdn_url
    is_ts         = cdn_url.endswith(".ts") or ".ts?" in cdn_url
    is_hls        = is_m3u8 or is_ts

    try:
        session     = _cffi_session()
        req_headers = {"Referer": "https://www.pornhub.com/", "Origin": "https://www.pornhub.com"}
        if rng := request.headers.get("Range"):
            req_headers["Range"] = rng

        if not is_hls and not download_mode:
            # Stream MP4 through the server.
            # We used to 302-redirect to the CDN directly, but PH CDN URLs are
            # IP-signed (the `h=` param is bound to the requesting IP). A redirect
            # sends the browser to the CDN with its own IP → CDN rejects it.
            # Streaming through the server keeps the CDN interaction server-side.
            pass  # fall through to the upstream GET below

        # Fetch the upstream content (HLS manifest or .ts segment, or MP4 download)
        upstream = session.get(
            cdn_url, headers=req_headers, allow_redirects=True,
            timeout=30, http_version=3, doh_url="https://1.1.1.1/dns-query",
            stream=True,
        )

        # PH CDN links are IP-signed. If the link was generated on a different
        # server instance / IP, the CDN returns 403 or 410. Auto-refresh by
        # re-resolving a fresh signed URL using the viewkey + quality embedded
        # in the request (passed as vk= and q= params by _make_proxy_url).
        if upstream.status_code in (403, 410, 451):
            viewkey = request.args.get("vk", "")
            quality = request.args.get("q", "")
            if viewkey:
                try:
                    fresh_meta, fresh_qs = _ph_get_all_qualities(
                        f"https://www.pornhub.com/view_video.php?viewkey={viewkey}"
                    )
                    # Pick matching quality or fall back to best
                    target = None
                    if quality:
                        target = next((x for x in fresh_qs if str(x.get("quality")) == quality
                                       and x.get("format") == ("hls" if is_hls else "mp4")), None)
                    if not target:
                        fmt_qs = [x for x in fresh_qs if x.get("format") == ("hls" if is_hls else "mp4")]
                        target = fmt_qs[0] if fmt_qs else fresh_qs[0]
                    cdn_url = target["url"]
                    upstream = session.get(
                        cdn_url, headers=req_headers, allow_redirects=True,
                        timeout=30, http_version=3, doh_url="https://1.1.1.1/dns-query",
                        stream=True,
                    )
                except Exception:
                    pass  # fall through to the status code check below

        if upstream.status_code not in (200, 206):
            return jsonify({
                "status": "error",
                "message": f"CDN returned HTTP {upstream.status_code}. Link may have expired — re-fetch from /ph/download.",
            }), upstream.status_code

        # ---------------------------------------------------------------
        # HLS manifest rewriting
        # For .m3u8 playlists we rewrite every URI line so that each
        # segment / child playlist is fetched via this proxy (which adds
        # the required Referer/cookie).
        # ---------------------------------------------------------------
        if is_m3u8:
            manifest_text = upstream.text.strip()

            # Empty manifest means CDN rejected it (IP mismatch).
            # Attempt auto-refresh using viewkey before giving up.
            if not manifest_text or len(manifest_text) < 10:
                vk_retry = request.args.get("vk", "")
                q_retry  = request.args.get("q", "")
                if vk_retry:
                    try:
                        _, fresh_qs = _ph_get_all_qualities(
                            f"https://www.pornhub.com/view_video.php?viewkey={vk_retry}"
                        )
                        target = None
                        if q_retry:
                            target = next((x for x in fresh_qs
                                           if str(x.get("quality")) == q_retry
                                           and x.get("format") == "hls"), None)
                        if not target:
                            target = next((x for x in fresh_qs if x.get("format") == "hls"), None)
                        if target:
                            cdn_url = target["url"]
                            upstream2 = session.get(
                                cdn_url, headers=req_headers, allow_redirects=True,
                                timeout=30, http_version=3, doh_url="https://1.1.1.1/dns-query",
                                stream=True,
                            )
                            if upstream2.status_code in (200, 206):
                                manifest_text = upstream2.text.strip()
                    except Exception:
                        pass

            if not manifest_text or len(manifest_text) < 10:
                return jsonify({
                    "status": "error",
                    "message": "CDN returned an empty HLS manifest. Link may have expired — re-fetch from /ph/download.",
                }), 502

            base_url_host = request.host_url.rstrip("/")
            parsed_cdn_m  = urlparse(cdn_url)
            cdn_base_dir_m = cdn_url[:cdn_url.rfind("/") + 1]

            rewritten_lines = []
            for line in manifest_text.splitlines():
                stripped = line.strip()
                if not stripped:
                    rewritten_lines.append(line)
                    continue
                if stripped.startswith("#"):
                    # Rewrite URI= attributes inside tags (e.g. #EXT-X-MEDIA:URI="...")
                    def _rewrite_attr(m, _base=cdn_base_dir_m, _parsed=parsed_cdn_m, _host=base_url_host):
                        abs_uri = _resolve_hls_uri(m.group(1), _base, _parsed)
                        return f'URI="{_make_proxy_url(_host, "/ph/proxy", abs_uri)}"'
                    rewritten_lines.append(re.sub(r'URI="([^"]+)"', _rewrite_attr, line))
                else:
                    # URI line — segment or child playlist
                    abs_uri = _resolve_hls_uri(stripped, cdn_base_dir_m, parsed_cdn_m)
                    rewritten_lines.append(_make_proxy_url(base_url_host, "/ph/proxy", abs_uri))

            rewritten_manifest = "\n".join(rewritten_lines) + "\n"
            return Response(
                rewritten_manifest,
                status=200,
                content_type="application/vnd.apple.mpegurl; charset=utf-8",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-cache",
                    "Content-Disposition": "inline; filename=\"playlist.m3u8\"",
                },
            )

        # Non-manifest: stream bytes through (for .ts segments and MP4 downloads)
        path_part    = urlparse(cdn_url).path
        fname        = path_part.split("/")[-1].split("?")[0] or "video"
        if not any(fname.endswith(ext) for ext in (".mp4", ".m3u8", ".ts", ".webm")):
            fname += ".mp4"
        content_type = upstream.headers.get("Content-Type", "video/mp2t" if is_ts else "video/mp4")
        disposition  = f'attachment; filename="{fname}"' if download_mode else f'inline; filename="{fname}"'

        resp_headers = {
            "Content-Disposition": disposition,
            "Accept-Ranges":       "bytes",
            "Access-Control-Allow-Origin": "*",
        }
        for h in ("Content-Length", "Content-Range"):
            if v := upstream.headers.get(h):
                resp_headers[h] = v

        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            content_type=content_type,
            headers=resp_headers,
        )
    except Exception as e:
        return jsonify({"status": "error", "message": f"Proxy error: {e}"}), 502



# ===========================================================================
# JAVtiful — helpers
# ===========================================================================
#
# JAVtiful embeds stream URLs in a JSON config block inside a <script> tag.
# Key: window config object containing "playerSources" array.
# The CDN (fast-stream.jav.si) serves plain MP4 — no ECH issues with requests.
# Page fetch needs curl_cffi (Cloudflare bot protection on javtiful.com).
# ===========================================================================

_JAV_VALID_HOSTS_RE = re.compile(
    r'^(?:www\.)?javtiful\.com$', re.IGNORECASE
)


def _jav_validate_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _JAV_VALID_HOSTS_RE.match(host):
        raise ValueError(
            f"Not a supported JAVtiful URL (host: {host!r}). "
            "Expected: https://javtiful.com/video/<id>/<slug>"
        )
    if "/video/" not in parsed.path:
        raise ValueError(
            "URL does not point to a JAVtiful video page. "
            "Expected: https://javtiful.com/video/<id>/<slug>"
        )
    return url


def _jav_fetch_page(url: str) -> str:
    """Fetch JAVtiful watch page using curl_cffi (bypasses CF bot protection)."""
    try:
        from curl_cffi import requests as cffi_req
        session = cffi_req.Session(impersonate="chrome124")
        resp = session.get(
            url,
            allow_redirects=True,
            timeout=20,
            http_version=3,
            doh_url="https://1.1.1.1/dns-query",
        )
    except Exception as e:
        raise ValueError(f"Network error fetching JAVtiful page: {e}")
    if resp.status_code != 200:
        raise ValueError(
            f"JAVtiful returned HTTP {resp.status_code}. "
            "Video may be private or deleted."
        )
    return resp.text


def _jav_extract_data(html: str) -> dict:
    """
    Extract playerSources and metadata from JAVtiful page HTML.
    playerSources is embedded in a JSON config block inside a <script> tag.
    """
    # Extract playerSources array
    m = re.search(r'"playerSources"\s*:\s*(\[.*?\])\s*[,}]', html, re.DOTALL)
    if not m:
        raise ValueError(
            "Could not find playerSources in JAVtiful page. "
            "Video may be premium-only or page structure changed."
        )
    try:
        sources = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse playerSources JSON: {e}")

    # Filter to real stream URLs (not preview/trailer)
    streams = [
        s for s in sources
        if s.get("src") and "jav.si" in s.get("src", "")
    ]
    if not streams:
        raise ValueError(
            "No streamable sources found. Video may be premium-only."
        )

    # Extract metadata from JSON-LD VideoObject schema
    title = ""
    thumbnail = ""
    duration_secs = 0

    m_ld = re.search(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>'
        r'\s*(\{.*?"@type"\s*:\s*"VideoObject".*?\})\s*</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if m_ld:
        try:
            ld = json.loads(m_ld.group(1))
            title = ld.get("name", "")
            thumbs = ld.get("thumbnailUrl", [])
            thumbnail = thumbs[0] if isinstance(thumbs, list) and thumbs else str(thumbs)
            # duration is ISO 8601 e.g. PT2H8M34S
            dur_str = ld.get("duration", "")
            dur_m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', dur_str)
            if dur_m:
                h = int(dur_m.group(1) or 0)
                mi = int(dur_m.group(2) or 0)
                s = int(dur_m.group(3) or 0)
                duration_secs = h * 3600 + mi * 60 + s
        except Exception:
            pass

    # Fallback title from og:title
    if not title:
        m_t = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            html
        )
        title = m_t.group(1).strip() if m_t else "Unknown Title"

    # Fallback thumbnail
    if not thumbnail:
        m_th = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            html
        )
        thumbnail = m_th.group(1) if m_th else ""

    duration = (f"{duration_secs // 3600}:{(duration_secs % 3600) // 60:02d}:{duration_secs % 60:02d}"
                if duration_secs >= 3600
                else f"{duration_secs // 60}:{duration_secs % 60:02d}"
                if duration_secs else "")

    return {
        "title":            title,
        "thumbnail":        thumbnail,
        "duration":         duration,
        "duration_seconds": duration_secs,
        "streams":          streams,
    }


def _jav_get_all_qualities(url: str) -> dict:
    """Full pipeline for JAVtiful."""
    url  = _jav_validate_url(url)
    html = _jav_fetch_page(url)
    return _jav_extract_data(html)


# ===========================================================================
# JAVtiful routes
# ===========================================================================

@app.route("/jav/download", methods=["POST"])
def jav_download():
    """Extract stream/download URLs for a JAVtiful video."""
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' field is required in JSON body"}), 400
    try:
        base_url = request.host_url.rstrip("/")
        result   = _jav_get_all_qualities(body["url"].strip())

        qualities = []
        for i, s in enumerate(result["streams"]):
            src   = s.get("src", "")
            label = s.get("label") or s.get("type", "").replace("video/", "") or f"stream{i+1}"
            ql    = str(label)
            qualities.append({
                "quality":      ql,
                "format":       "mp4",
                "url":          src,
                "proxy_url":    _make_proxy_url(base_url, "/jav/proxy", src, quality=ql),
                "download_url": _make_proxy_url(base_url, "/jav/proxy", src, extra="&dl=1", quality=ql),
            })

        if not qualities:
            return jsonify({"status": "error", "message": "No streams found."}), 404

        best = qualities[0]
        watch_url = f"{base_url}/jav/watch?url={quote(body['url'].strip())}"

        return jsonify({
            "status": "success",
            "data": {
                "title":             result["title"],
                "thumbnail":         result["thumbnail"],
                "duration":          result["duration"],
                "duration_seconds":  result["duration_seconds"],
                "watch_url":         watch_url,
                "qualities":         qualities,
                "best_proxy_url":    best["proxy_url"],
                "best_download_url": best["download_url"],
                "note": "Use best_proxy_url to stream or best_download_url to download. Open watch_url in browser for the built-in player.",
            },
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal error: {e}"}), 500


@app.route("/jav/watch")
def jav_watch():
    """Browser video player for a JAVtiful video. Pass ?url=<video_url>"""
    url = request.args.get("url", "").strip()
    if not url:
        return "<h2>Missing ?url= parameter</h2>", 400
    try:
        base_url = request.host_url.rstrip("/")
        result   = _jav_get_all_qualities(url)

        qualities = []
        for i, s in enumerate(result["streams"]):
            src   = s.get("src", "")
            label = s.get("label") or s.get("type", "").replace("video/", "") or f"stream{i+1}"
            ql    = str(label)
            qualities.append({
                "quality":      ql,
                "format":       "mp4",
                "url":          src,
                "proxy_url":    _make_proxy_url(base_url, "/jav/proxy", src, quality=ql),
                "download_url": _make_proxy_url(base_url, "/jav/proxy", src, extra="&dl=1", quality=ql),
            })

        if not qualities:
            return "<h2>No streams found for this video.</h2>", 404

        return _render_watch_page(result, qualities)
    except Exception as e:
        return f"""<!DOCTYPE html><html><head><title>Error</title></head>
        <body style="background:#0f0f0f;color:#eee;font-family:sans-serif;padding:40px">
        <h2 style="color:#f55">Could not load video</h2><p>{e}</p>
        <p><a href="https://github.com/MeherMankar/grabx-api" style="color:#f90">GrabX API</a></p>
        </body></html>""", 500


@app.route("/jav/proxy")
def jav_proxy():
    """
    Proxy JAVtiful CDN streams (fast-stream.jav.si).
    Uses plain requests (no curl_cffi) — CDN has no ECH/TLS issues with stdlib.
    """
    cdn_url = request.args.get("url", "").strip()
    if not cdn_url:
        return jsonify({"status": "error", "message": "'url' query param required"}), 400

    if not _verify_proxy_token(cdn_url) and not _check_raw_key():
        return jsonify({"status": "error",
                        "message": "Access denied. Use the proxy_url from /jav/download."}), 403

    download_mode = request.args.get("dl", "0") == "1"

    try:
        headers = {
            "Referer":        "https://javtiful.com/",
            "Origin":         "https://javtiful.com",
            "User-Agent":     DESKTOP_UA,
            "Accept":         "*/*",
            "Accept-Encoding": "identity",
        }
        if rng := request.headers.get("Range"):
            headers["Range"] = rng

        upstream = req_lib.get(
            cdn_url,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=30,
        )
        if upstream.status_code not in (200, 206):
            return jsonify({"status": "error",
                            "message": f"CDN returned HTTP {upstream.status_code}."}), upstream.status_code

        path_part = urlparse(cdn_url).path
        fname     = path_part.split("/")[-1].split("?")[0] or "video"
        if not fname.endswith(".mp4"):
            fname += ".mp4"

        disposition = f'attachment; filename="{fname}"' if download_mode else f'inline; filename="{fname}"'
        resp_headers = {
            "Content-Disposition":         disposition,
            "Accept-Ranges":               "bytes",
            "Access-Control-Allow-Origin": "*",
        }
        for h in ("Content-Length", "Content-Range", "ETag"):
            if v := upstream.headers.get(h):
                resp_headers[h] = v

        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            content_type=upstream.headers.get("Content-Type", "video/mp4"),
            headers=resp_headers,
        )
    except Exception as e:
        return jsonify({"status": "error", "message": f"Proxy error: {e}"}), 502



#
# Both sites embed stream URLs in html5player.setVideoUrl*() JS calls.
# Xvideos uses xvideos-cdn.com, XNXX uses xnxx-cdn.com.
# No second-step API call needed — all qualities are directly in the page.
# ===========================================================================

_XV_VALID_HOSTS_RE = re.compile(
    r'^(?:www\.)?xvideos(?:\d+)?\.com$', re.IGNORECASE
)
_XNXX_VALID_HOSTS_RE = re.compile(
    r'^(?:www\.)?xnxx\.com$', re.IGNORECASE
)

_XV_AGE_COOKIES   = {}  # Xvideos has no age gate for most content
_XNXX_AGE_COOKIES = {"nv_age_check": "1"}

def _xv_cffi_session(cookies: dict = None):
    from curl_cffi import requests as cffi_req
    session = cffi_req.Session(impersonate="chrome124")
    if cookies:
        for domain in (".xvideos.com", ".xvideos2.com", ".xvideos3.com"):
            for name, value in cookies.items():
                session.cookies.set(name, value, domain=domain)
    return session

def _xnxx_cffi_session():
    from curl_cffi import requests as cffi_req
    session = cffi_req.Session(impersonate="chrome124")
    for domain in (".xnxx.com",):
        for name, value in _XNXX_AGE_COOKIES.items():
            session.cookies.set(name, value, domain=domain)
    return session

def _xv_fetch_page(url: str, session=None) -> str:
    try:
        if session is None:
            session = _xv_cffi_session()
        resp = session.get(
            url, allow_redirects=True, timeout=20,
            http_version=3, doh_url="https://1.1.1.1/dns-query",
        )
    except Exception as e:
        raise ValueError(f"Network error fetching page: {e}")
    if resp.status_code != 200:
        raise ValueError(f"Site returned HTTP {resp.status_code}. Video may be private or deleted.")
    return resp.text


def _xv_extract_data(html: str, site_domain: str) -> dict:
    """
    Extract stream URLs from html5player JS calls and window.xv.conf fallback.
    Works for both Xvideos and XNXX.
    """
    data: dict = {}

    # html5player.setVideoUrl*() calls
    patterns = {
        "url_low":  r"html5player\.setVideoUrlLow\s*\(\s*['\"](.+?)['\"]\s*\)",
        "url_high": r"html5player\.setVideoUrlHigh\s*\(\s*['\"](.+?)['\"]\s*\)",
        "url_hls":  r"html5player\.setVideoUrlHls\s*\(\s*['\"](.+?)['\"]\s*\)",
        "title":    r"html5player\.setVideoTitle\s*\(\s*['\"](.+?)['\"]\s*\)",
        "thumb":    r"html5player\.setThumbUrl\s*\(\s*['\"](.+?)['\"]\s*\)",
        "duration": r"html5player\.setVideoDuration\s*\(\s*(\d+)\s*\)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            data[key] = m.group(1)

    # Fallback: window.xv.conf JSON
    if not data.get("url_low") and not data.get("url_high"):
        m = re.search(r'window\.xv\.conf\s*=\s*(\{.+?\})\s*;', html, re.DOTALL)
        if m:
            try:
                conf = json.loads(m.group(1))
                for k in ("url", "url_low", "url_high", "url_hls"):
                    if k in conf:
                        data[k] = conf[k]
            except json.JSONDecodeError:
                pass

    # OG metadata fallback for title/thumb
    if not data.get("title"):
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html)
        if m:
            data["title"] = m.group(1).strip()
    if not data.get("thumb"):
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
        if m:
            data["thumb"] = m.group(1).strip()
    if not data.get("duration"):
        m = re.search(r'"duration"\s*:\s*"?(\d+)"?', html)
        if m:
            data["duration"] = m.group(1)

    if not data.get("url_low") and not data.get("url_high") and not data.get("url"):
        raise ValueError(
            f"No video stream URLs found. "
            f"Video may be premium-only, deleted, or {site_domain} page structure changed."
        )

    return data


def _xv_build_qualities(data: dict, base_url: str, path: str,
                        viewkey: str, referer: str) -> list:
    """Convert extracted data dict into the standard qualities list."""
    qualities = []
    url_map = {
        "360": data.get("url_low") or data.get("url"),
        "480": data.get("url_high"),
        "hls": data.get("url_hls"),
    }
    for label, url in url_map.items():
        if not url:
            continue
        fmt = "hls" if label == "hls" else "mp4"
        ql  = label if label != "hls" else "hls"
        qualities.append({
            "quality":      ql,
            "format":       fmt,
            "url":          url,
            "proxy_url":    _make_proxy_url(base_url, path, url, viewkey=viewkey, quality=ql),
            "download_url": _make_proxy_url(base_url, path, url, extra="&dl=1", viewkey=viewkey, quality=ql),
        })
    # Sort MP4 best-first
    qualities.sort(key=lambda e: (0, -int(e["quality"])) if e["quality"].isdigit() else (1, 0))
    return qualities


def _xv_get_all_qualities(url: str):
    """Full pipeline for Xvideos."""
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _XV_VALID_HOSTS_RE.match(host):
        raise ValueError(f"Not a supported Xvideos URL (host: {host!r}).")
    session = _xv_cffi_session(_XV_AGE_COOKIES)
    html    = _xv_fetch_page(url, session)
    data    = _xv_extract_data(html, "xvideos.com")
    secs    = int(data.get("duration", 0) or 0)
    meta    = {
        "title":            data.get("title", "Unknown Title"),
        "thumbnail":        data.get("thumb", ""),
        "duration":         f"{secs // 60}:{secs % 60:02d}" if secs else "",
        "duration_seconds": secs,
        "video_url":        url,
    }
    return meta, data


def _xnxx_get_all_qualities(url: str):
    """Full pipeline for XNXX."""
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _XNXX_VALID_HOSTS_RE.match(host):
        raise ValueError(f"Not a supported XNXX URL (host: {host!r}).")
    session = _xnxx_cffi_session()
    html    = _xv_fetch_page(url, session)
    data    = _xv_extract_data(html, "xnxx.com")
    secs    = int(data.get("duration", 0) or 0)
    meta    = {
        "title":            data.get("title", "Unknown Title"),
        "thumbnail":        data.get("thumb", ""),
        "duration":         f"{secs // 60}:{secs % 60:02d}" if secs else "",
        "duration_seconds": secs,
        "video_url":        url,
    }
    return meta, data


# ===========================================================================
# XHamster — helpers
# ===========================================================================
#
# XHamster embeds all stream data in window.initials JSON on the watch page.
# The mp4 array contains all quality variants directly — no second API call.
# ===========================================================================

_XH_VALID_HOSTS_RE = re.compile(
    r'^(?:[a-z]{2}\.)?(?:www\.)?xhamster(?:\d+)?\.(?:com|desi|one|xxx|net)$',
    re.IGNORECASE,
)
_XH_COOKIE_DOMAINS = [".xhamster.com", ".xhamster.desi", ".xhamster.one",
                      ".xhamster.xxx", ".xhamster.net"]
_XH_AGE_COOKIES = {
    "adc_ga_v2":         "1",
    "is_adult_confirmed": "1",
    "xhamster-language": "en",
    "platform":          "desktop",
}

def _xh_cffi_session():
    from curl_cffi import requests as cffi_req
    session = cffi_req.Session(impersonate="chrome124")
    for domain in _XH_COOKIE_DOMAINS:
        for name, value in _XH_AGE_COOKIES.items():
            session.cookies.set(name, value, domain=domain)
    return session


def _xh_extract_data(html: str) -> dict:
    """Extract stream URLs from window.initials JSON."""
    m = re.search(r'window\.initials\s*=\s*(\{.+?\})\s*;', html, re.DOTALL)
    if not m:
        raise ValueError(
            "Could not find window.initials in XHamster page. "
            "Page structure may have changed or video is unavailable."
        )
    try:
        initials = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse XHamster initials JSON: {e}")

    sources = (
        initials.get("xplayerSettings", {}).get("sources", {})
        or initials.get("videoModel", {}).get("sources", {})
    )
    if not sources:
        raise ValueError("No sources found in XHamster initials JSON.")

    video_model = initials.get("videoModel", {})
    secs = int(video_model.get("duration", 0) or 0)

    qualities = []
    for item in sources.get("mp4", []):
        url = (item.get("url") or item.get("videoUrl") or "").strip()
        ql  = str(item.get("quality") or "").replace("p", "").strip()
        if url and ql:
            qualities.append({"quality": ql, "format": "mp4", "url": url})

    hls_src = sources.get("hls") or {}
    hls_url = (hls_src.get("url") or hls_src.get("xplayerSources", {}).get("hls", {}).get("url") or "").strip()
    if hls_url:
        qualities.append({"quality": "hls", "format": "hls", "url": hls_url})

    qualities.sort(key=lambda e: (0, -int(e["quality"])) if e["quality"].isdigit() else (1, 0))

    if not qualities:
        raise ValueError("No stream URLs found in XHamster sources.")

    return {
        "title":            video_model.get("title", "Unknown Title"),
        "thumbnail":        video_model.get("thumbURL", video_model.get("thumb", "")),
        "duration":         f"{secs // 60}:{secs % 60:02d}" if secs else "",
        "duration_seconds": secs,
        "qualities":        qualities,
    }


def _xh_get_all_qualities(url: str) -> dict:
    """Full pipeline for XHamster."""
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _XH_VALID_HOSTS_RE.match(host):
        raise ValueError(f"Not a supported XHamster URL (host: {host!r}).")
    session = _xh_cffi_session()
    html    = _xv_fetch_page(url, session)
    return _xh_extract_data(html)


# ===========================================================================
# Generic adult-site proxy (/adult/proxy)
# Works for Xvideos, XNXX, XHamster CDN streams — attaches correct Referer.
# ===========================================================================

# Map CDN hostname patterns → Referer to send
_ADULT_CDN_REFERERS = [
    (re.compile(r'xvideos-cdn\.com',  re.I), "https://www.xvideos.com/"),
    (re.compile(r'xnxx-cdn\.com',     re.I), "https://www.xnxx.com/"),
    (re.compile(r'xhmscdn\d*\.com',   re.I), "https://xhamster.com/"),
    (re.compile(r'xhstorage\.com',    re.I), "https://xhamster.com/"),
    (re.compile(r'xhamster\.com',     re.I), "https://xhamster.com/"),
]

def _adult_referer(cdn_url: str) -> str:
    host = (urlparse(cdn_url).hostname or "").lower()
    for pat, ref in _ADULT_CDN_REFERERS:
        if pat.search(host):
            return ref
    return "https://www.xvideos.com/"  # safe default


# ===========================================================================
# Xvideos routes
# ===========================================================================

@app.route("/xv/download", methods=["POST"])
def xv_download():
    """Extract all stream/download URLs for an Xvideos video."""
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' field is required in JSON body"}), 400
    try:
        base_url = request.host_url.rstrip("/")
        meta, data = _xv_get_all_qualities(body["url"].strip())
        qualities = _xv_build_qualities(data, base_url, "/adult/proxy",
                                         viewkey="", referer="https://www.xvideos.com/")
        if not qualities:
            return jsonify({"status": "error", "message": "No streams found."}), 404
        best = qualities[0]
        return jsonify({
            "status": "success",
            "data": {
                "title":             meta["title"],
                "thumbnail":         meta["thumbnail"],
                "duration":          meta["duration"],
                "duration_seconds":  meta["duration_seconds"],
                "qualities":         qualities,
                "best_proxy_url":    best["proxy_url"],
                "best_download_url": best["download_url"],
                "note": "Use best_proxy_url to stream or best_download_url to download.",
            },
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal error: {e}"}), 500


@app.route("/xv/watch")
def xv_watch():
    """Browser video player for an Xvideos video. Pass ?url=<video_url>"""
    url = request.args.get("url", "").strip()
    if not url:
        return "<h2>Missing ?url= parameter</h2>", 400
    try:
        base_url = request.host_url.rstrip("/")
        meta, data = _xv_get_all_qualities(url)
        qualities = _xv_build_qualities(data, base_url, "/adult/proxy",
                                         viewkey="", referer="https://www.xvideos.com/")
        if not qualities:
            return "<h2>No streams found for this video.</h2>", 404
        return _render_watch_page(meta, qualities)
    except Exception as e:
        return f"<h2 style='color:#f55'>Error: {e}</h2>", 500


# ===========================================================================
# XNXX routes
# ===========================================================================

@app.route("/xnxx/download", methods=["POST"])
def xnxx_download():
    """Extract all stream/download URLs for an XNXX video."""
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' field is required in JSON body"}), 400
    try:
        base_url = request.host_url.rstrip("/")
        meta, data = _xnxx_get_all_qualities(body["url"].strip())
        qualities = _xv_build_qualities(data, base_url, "/adult/proxy",
                                         viewkey="", referer="https://www.xnxx.com/")
        if not qualities:
            return jsonify({"status": "error", "message": "No streams found."}), 404
        best = qualities[0]
        return jsonify({
            "status": "success",
            "data": {
                "title":             meta["title"],
                "thumbnail":         meta["thumbnail"],
                "duration":          meta["duration"],
                "duration_seconds":  meta["duration_seconds"],
                "qualities":         qualities,
                "best_proxy_url":    best["proxy_url"],
                "best_download_url": best["download_url"],
                "note": "Use best_proxy_url to stream or best_download_url to download.",
            },
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal error: {e}"}), 500


@app.route("/xnxx/watch")
def xnxx_watch():
    """Browser video player for an XNXX video. Pass ?url=<video_url>"""
    url = request.args.get("url", "").strip()
    if not url:
        return "<h2>Missing ?url= parameter</h2>", 400
    try:
        base_url = request.host_url.rstrip("/")
        meta, data = _xnxx_get_all_qualities(url)
        qualities = _xv_build_qualities(data, base_url, "/adult/proxy",
                                         viewkey="", referer="https://www.xnxx.com/")
        if not qualities:
            return "<h2>No streams found for this video.</h2>", 404
        return _render_watch_page(meta, qualities)
    except Exception as e:
        return f"<h2 style='color:#f55'>Error: {e}</h2>", 500


# ===========================================================================
# XHamster routes
# ===========================================================================

@app.route("/xh/download", methods=["POST"])
def xh_download():
    """Extract all stream/download URLs for an XHamster video."""
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' field is required in JSON body"}), 400
    try:
        base_url = request.host_url.rstrip("/")
        result   = _xh_get_all_qualities(body["url"].strip())
        qualities = []
        for q in result["qualities"]:
            ql = q["quality"]
            qualities.append({
                **q,
                "proxy_url":    _make_proxy_url(base_url, "/adult/proxy", q["url"], quality=ql),
                "download_url": _make_proxy_url(base_url, "/adult/proxy", q["url"], extra="&dl=1", quality=ql),
            })
        if not qualities:
            return jsonify({"status": "error", "message": "No streams found."}), 404
        best = next((q for q in qualities if q["format"] == "mp4"), qualities[0])
        return jsonify({
            "status": "success",
            "data": {
                "title":             result["title"],
                "thumbnail":         result["thumbnail"],
                "duration":          result["duration"],
                "duration_seconds":  result["duration_seconds"],
                "qualities":         qualities,
                "best_proxy_url":    best["proxy_url"],
                "best_download_url": best["download_url"],
                "note": "Use best_proxy_url to stream or best_download_url to download.",
            },
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal error: {e}"}), 500


@app.route("/xh/watch")
def xh_watch():
    """Browser video player for an XHamster video. Pass ?url=<video_url>"""
    url = request.args.get("url", "").strip()
    if not url:
        return "<h2>Missing ?url= parameter</h2>", 400
    try:
        base_url = request.host_url.rstrip("/")
        result   = _xh_get_all_qualities(url)
        qualities = []
        for q in result["qualities"]:
            ql = q["quality"]
            qualities.append({
                **q,
                "proxy_url":    _make_proxy_url(base_url, "/adult/proxy", q["url"], quality=ql),
                "download_url": _make_proxy_url(base_url, "/adult/proxy", q["url"], extra="&dl=1", quality=ql),
            })
        if not qualities:
            return "<h2>No streams found for this video.</h2>", 404
        return _render_watch_page(result, qualities)
    except Exception as e:
        return f"<h2 style='color:#f55'>Error: {e}</h2>", 500


# ===========================================================================
# Generic adult CDN proxy (/adult/proxy)
# Handles Xvideos, XNXX, XHamster CDN streams — attaches correct Referer.
# Auth: same token system as /ph/proxy.
# ===========================================================================

@app.route("/adult/proxy")
def adult_proxy():
    """
    Proxy CDN streams for Xvideos / XNXX / XHamster.
    Automatically detects the correct Referer from the CDN hostname.
    """
    cdn_url = request.args.get("url", "").strip()
    if not cdn_url:
        return jsonify({"status": "error", "message": "'url' query param required"}), 400

    if not _verify_proxy_token(cdn_url) and not _check_raw_key():
        return jsonify({"status": "error",
                        "message": "Access denied. Use the proxy_url from the download endpoint."}), 403

    download_mode = request.args.get("dl", "0") == "1"
    is_m3u8 = ".m3u8" in cdn_url
    is_ts   = cdn_url.endswith(".ts") or ".ts?" in cdn_url

    try:
        referer = _adult_referer(cdn_url)
        session = _cffi_session()  # reuse curl_cffi for uniform TLS
        headers = {
            "Referer":  referer,
            "Origin":   referer.rstrip("/"),
            "Accept":   "*/*",
            "Accept-Encoding": "identity",
        }
        if rng := request.headers.get("Range"):
            headers["Range"] = rng

        upstream = session.get(
            cdn_url, headers=headers, allow_redirects=True,
            timeout=30, http_version=3, doh_url="https://1.1.1.1/dns-query",
            stream=True,
        )
        if upstream.status_code not in (200, 206):
            return jsonify({"status": "error",
                            "message": f"CDN returned HTTP {upstream.status_code}."}), upstream.status_code

        # HLS manifest rewriting
        if is_m3u8:
            manifest = upstream.text.strip()
            if not manifest or len(manifest) < 10:
                return jsonify({"status": "error", "message": "CDN returned empty manifest."}), 502
            base_url_host  = request.host_url.rstrip("/")
            parsed_cdn     = urlparse(cdn_url)
            cdn_base_dir   = cdn_url[:cdn_url.rfind("/") + 1]
            token_params   = {
                k: request.args.get(k, "")
                for k in ("_t", "_e", "vk", "q")
            }
            rewritten_lines = []
            for line in manifest.splitlines():
                stripped = line.strip()
                if not stripped:
                    rewritten_lines.append(line)
                    continue
                if stripped.startswith("#"):
                    def _rw(m, _base=cdn_base_dir, _parsed=parsed_cdn, _host=base_url_host):
                        abs_uri = _resolve_hls_uri(m.group(1), _base, _parsed)
                        return f'URI="{_make_proxy_url(_host, "/adult/proxy", abs_uri)}"'
                    rewritten_lines.append(re.sub(r'URI="([^"]+)"', _rw, line))
                else:
                    abs_uri = _resolve_hls_uri(stripped, cdn_base_dir, parsed_cdn)
                    rewritten_lines.append(_make_proxy_url(base_url_host, "/adult/proxy", abs_uri))
            return Response(
                "\n".join(rewritten_lines) + "\n",
                status=200,
                content_type="application/vnd.apple.mpegurl; charset=utf-8",
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache",
                         "Content-Disposition": 'inline; filename="playlist.m3u8"'},
            )

        path_part = urlparse(cdn_url).path
        fname     = path_part.split("/")[-1].split("?")[0] or "video"
        if not any(fname.endswith(ext) for ext in (".mp4", ".webm", ".ts", ".m3u8")):
            fname += ".mp4"
        content_type = upstream.headers.get("Content-Type", "video/mp2t" if is_ts else "video/mp4")
        disposition  = f'attachment; filename="{fname}"' if download_mode else f'inline; filename="{fname}"'
        resp_headers = {
            "Content-Disposition":         disposition,
            "Accept-Ranges":               "bytes",
            "Access-Control-Allow-Origin": "*",
        }
        for h in ("Content-Length", "Content-Range"):
            if v := upstream.headers.get(h):
                resp_headers[h] = v

        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        return Response(stream_with_context(generate()),
                        status=upstream.status_code,
                        content_type=content_type,
                        headers=resp_headers)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Proxy error: {e}"}), 502


# ===========================================================================
# Shared watch page renderer
# ===========================================================================

def _render_watch_page(meta: dict, qualities: list):
    """Render the HLS.js player HTML for any site."""
    title     = meta.get("title", "Video")
    thumbnail = meta.get("thumbnail", "")
    duration  = meta.get("duration", "")

    sorted_opts = sorted(
        qualities,
        key=lambda q: (0, -int(q["quality"])) if q["quality"].isdigit() else (1, 0),
    )

    options_html = "\n".join(
        f'<option value="{quote(o.get("proxy_url",""), safe="")}" '
        f'data-fmt="{o["format"]}" '
        f'data-dl="{quote(o.get("download_url",""), safe="")}">'
        f'{o["quality"]}{"p" if o["quality"].isdigit() else ""} {o["format"].upper()}'
        f'</option>'
        for o in sorted_opts
    )

    best         = sorted_opts[0]
    best_stream  = best.get("proxy_url", "")
    best_dl      = best.get("download_url", "")
    best_fmt     = best.get("format", "mp4")

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
          padding:9px 18px;border-radius:6px;text-decoration:none;white-space:nowrap;
          transition:background .15s;cursor:pointer;border:none}}
    .btn-dl{{background:#f90;color:#000}}.btn-dl:hover{{background:#e88600}}
    .btn-dl:disabled{{background:#666;cursor:not-allowed}}
    .meta{{margin-top:10px;font-size:.8rem;color:#666}}
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
    </div>
    <div class="meta">{'Duration: ' + duration + ' &nbsp;·&nbsp; ' if duration else ''}Powered by <a href="https://github.com/MeherMankar/grabx-api" target="_blank" style="color:#f90;text-decoration:none">GrabX API</a></div>
    <p class="note">Tip: right-click the video &rarr; "Save video as" to download directly.</p>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
  <script>
    const video = document.getElementById('player');
    const sel   = document.getElementById('qualitySelect');
    const dlBtn = document.getElementById('dlBtn');
    let hls     = null;
    let currentDlUrl = '{best_dl}';
    let currentFmt   = '{best_fmt}';

    function loadSrc(streamUrl, fmt, dlUrl) {{
      const isHls = fmt === 'hls' || streamUrl.includes('.m3u8');
      currentDlUrl = dlUrl;
      currentFmt   = fmt;
      if (hls) {{ hls.destroy(); hls = null; }}
      if (isHls) {{
        if (Hls.isSupported()) {{
          hls = new Hls({{ enableWorker: true }});
          hls.loadSource(streamUrl);
          hls.attachMedia(video);
          hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {{}}));
        }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
          video.src = streamUrl;
          video.play().catch(() => {{}});
        }}
      }} else {{
        video.src = streamUrl;
        video.load();
      }}
    }}

    dlBtn.addEventListener('click', async function() {{
      if (currentFmt === 'hls') {{ window.open(currentDlUrl, '_blank'); return; }}
      dlBtn.textContent = 'Preparing...';
      dlBtn.disabled = true;
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


app.debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

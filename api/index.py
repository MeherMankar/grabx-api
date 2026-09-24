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
# Terabox — constants & helpers
# ===========================================================================

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TERABOX_DOMAINS = [
    ".terabox.com", ".dm.terabox.com",
    ".1024terabox.com", ".1024tera.com",
    ".teraboxapp.com", ".terabox.app",
    ".nephobox.com", ".4funbox.co",
    ".mirrobox.com", ".momerybox.com",
    ".teraboxlink.com", ".terafileshare.com",
    ".freeterabox.com", ".teraboxshare.com",
    ".terabox1.com", ".terabox2.com",
    ".terasharefile.com",
]

TERABOX_HOSTNAMES = [
    "www.terabox.com",
    "www.1024terabox.com",
    "www.teraboxapp.com",
    "www.terasharefile.com",
    "www.nephobox.com",
    "www.4funbox.co",
    "www.mirrobox.com",
    "www.momerybox.com",
    "www.freeterabox.com",
    "www.teraboxlink.com",
    "www.terafileshare.com",
    "www.teraboxshare.com",
    "www.terabox1.com",
    "www.terabox2.com",
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
    parsed = urlparse(share_url)
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


# ===========================================================================
# PornHub — constants & helpers
# ===========================================================================

PH_VALID_HOSTS = {
    "www.pornhub.com", "pornhub.com",
    "www.pornhub.org", "pornhub.org",
    "cn.pornhub.com", "de.pornhub.com", "fr.pornhub.com",
    "es.pornhub.com", "it.pornhub.com", "nl.pornhub.com",
    "pt.pornhub.com", "pl.pornhub.com", "jp.pornhub.com",
    "rt.pornhub.com", "cz.pornhub.com",
    "www.thumbzilla.com", "thumbzilla.com",
}

PH_COOKIE_DOMAINS = [".pornhub.com", ".pornhub.org", ".thumbzilla.com"]

PH_AGE_COOKIE = {"accessAgeDisclaimerPH": "1", "platform": "pc"}


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
    if full_host not in PH_VALID_HOSTS:
        raise ValueError(
            f"Not a supported PornHub URL (host: {full_host!r}). "
            "Expected e.g. https://www.pornhub.com/view_video.php?viewkey=..."
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
            headers={"Referer": "https://www.pornhub.com/"},
            allow_redirects=True,
            timeout=15,
            http_version=3,
            doh_url="https://1.1.1.1/dns-query",
        )
        if r.status_code != 200:
            return []
        items = r.json()
        if not isinstance(items, list):
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
    except Exception:
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
    hls_only  = [q for q in hls_qs if q["format"] == "hls"]
    all_qs    = mp4_qs + hls_only or hls_qs
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
        "endpoints": {
            "/download": {
                "method": "POST",
                "description": "Get direct download link for a Terabox share URL",
                "body": {"url": "Terabox share URL"},
            },
            "/proxy": {
                "method": "GET",
                "description": "Proxy-stream a Terabox dlink through this server",
                "params": {"url": "The dlink URL to proxy"},
            },
            "/ph/download": {
                "method": "POST",
                "description": "Extract video stream/download links from a PornHub watch page",
                "body": {"url": "PornHub video URL (view_video.php?viewkey=...)"},
            },
            "/ph/watch/<viewkey>": {
                "method": "GET",
                "description": "Browser video player with quality selector and download button",
                "example": "/ph/watch/6a165f5d3a96c",
            },
            "/ph/proxy": {
                "method": "GET",
                "description": "Stream or download a PH CDN URL (adds Referer/cookies); MP4 → 302 redirect, HLS → proxied",
                "params": {"url": "PH CDN URL", "dl": "1=download, 0=stream (default)"},
            },
            "/docs": {
                "method": "GET",
                "description": "API documentation",
            },
        },
    })


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

        files    = []
        base_url = request.host_url.rstrip("/")

        for item in file_list:
            if str(item.get("isdir", "0")) == "1":
                continue
            dlink      = item.get("dlink", "")
            thumbs     = item.get("thumbs") or {}
            thumbnail  = (thumbs.get("url3") or thumbs.get("url2") or
                          thumbs.get("url1") or thumbs.get("icon") or "")
            size_bytes = int(item.get("size", 0))
            proxy_url  = f"{base_url}/proxy?url={quote(dlink)}" if dlink else ""
            files.append({
                "filename":   item.get("server_filename", ""),
                "size_bytes": size_bytes,
                "size":       _human_size(size_bytes),
                "thumbnail":  thumbnail,
                "dlink":      dlink,
                "proxy_url":  proxy_url,
                "fs_id":      str(item.get("fs_id", "")),
            })

        if not files:
            return jsonify({"status": "error", "message": "Share contains only folders or no files."}), 404

        has_dlink = any(f["dlink"] for f in files)
        return jsonify({
            "status": "success",
            "data": {
                "title": files[0]["filename"] if len(files) == 1 else f"{len(files)} files",
                "files": files,
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

@app.route("/ph/download", methods=["POST"])
def ph_download():
    """Extract all quality variants + proxy/download URLs for a PH video."""
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' field is required in JSON body"}), 400

    try:
        meta, all_qualities = _ph_get_all_qualities(body["url"].strip())

        base_url = request.host_url.rstrip("/")
        for q in all_qualities:
            enc = quote(q["url"])
            q["proxy_url"]    = f"{base_url}/ph/proxy?url={enc}"
            q["download_url"] = f"{base_url}/ph/proxy?url={enc}&dl=1"

        mp4s          = [q for q in all_qualities if q["format"] == "mp4"]
        best_url      = mp4s[0]["url"] if mp4s else all_qualities[0]["url"]
        best_proxy    = f"{base_url}/ph/proxy?url={quote(best_url)}"
        best_download = f"{base_url}/ph/proxy?url={quote(best_url)}&dl=1"
        watch_url     = f"{base_url}/ph/watch/{meta['viewkey']}" if meta.get("viewkey") else ""

        return jsonify({
            "status": "success",
            "data": {
                "title":             meta["title"],
                "thumbnail":         meta["thumbnail"],
                "duration":          meta["duration"],
                "duration_seconds":  meta["duration_seconds"],
                "viewkey":           meta["viewkey"],
                "watch_url":         watch_url,
                "qualities":         all_qualities,
                "best_url":          best_url,
                "best_proxy_url":    best_proxy,
                "best_download_url": best_download,
                "note": (
                    "Open 'watch_url' in a browser for the built-in video player. "
                    "Use 'best_proxy_url' to stream or 'best_download_url' to download. "
                    "Or pick any quality from 'qualities' and use its proxy_url / download_url."
                ),
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

    mp4_opts = []
    for q in all_qualities:
        if q["format"] == "mp4":
            mp4_opts.append({
                "label":    f"{q['quality']}p",
                "proxy":    f"{base_url}/ph/proxy?url={quote(q['url'])}",
                "download": f"{base_url}/ph/proxy?url={quote(q['url'])}&dl=1",
            })

    if not mp4_opts:
        return "<h2>No MP4 streams found for this video.</h2>", 404

    best_stream   = mp4_opts[0]["proxy"]
    best_download = mp4_opts[0]["download"]
    title         = meta["title"]
    thumbnail     = meta.get("thumbnail", "")
    duration      = meta.get("duration", "")

    quality_options_html = "\n".join(
        f'<option value="{o["proxy"]}" data-dl="{o["download"]}">{o["label"]} MP4</option>'
        for o in mp4_opts
    )

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
          padding:9px 18px;border-radius:6px;text-decoration:none;white-space:nowrap;transition:background .15s}}
    .btn-dl{{background:#f90;color:#000}}.btn-dl:hover{{background:#e88600}}
    .meta{{margin-top:10px;font-size:.8rem;color:#666}}
    .note{{margin-top:16px;font-size:.75rem;color:#444;text-align:center}}
  </style>
</head>
<body>
  <div class="container">
    <h1>{title}</h1>
    <div class="player-wrap">
      <video id="player" controls preload="metadata" poster="{thumbnail}">
        <source id="src" src="{best_stream}" type="video/mp4"/>
        Your browser does not support HTML5 video.
      </video>
    </div>
    <div class="controls">
      <select id="qualitySelect" title="Select quality">
        {quality_options_html}
      </select>
      <a id="dlBtn" class="btn btn-dl" href="{best_download}" download>&#8595; Download</a>
    </div>
    <div class="meta">{'Duration: ' + duration + ' &nbsp;·&nbsp; ' if duration else ''}Powered by PH Downloader API</div>
    <p class="note">Tip: right-click the video → "Save video as" to download directly from CDN.</p>
  </div>
  <script>
    const video=document.getElementById('player');
    const src=document.getElementById('src');
    const sel=document.getElementById('qualitySelect');
    const dl=document.getElementById('dlBtn');
    sel.addEventListener('change',function(){{
      const opt=this.options[this.selectedIndex];
      const t=video.currentTime, playing=!video.paused;
      src.src=opt.value; video.load(); video.currentTime=t;
      dl.href=opt.dataset.dl;
      if(playing) video.play();
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
    For HLS (.m3u8/.ts):   always streams through server (Referer required per-segment).

    GET /ph/proxy?url=<encoded_url>&dl=0|1
    """
    cdn_url = request.args.get("url", "").strip()
    if not cdn_url:
        return jsonify({"status": "error", "message": "'url' query param required"}), 400

    download_mode = request.args.get("dl", "0") == "1"
    is_hls        = ".m3u8" in cdn_url or cdn_url.endswith(".ts") or ".ts?" in cdn_url

    try:
        session     = _cffi_session()
        req_headers = {"Referer": "https://www.pornhub.com/", "Origin": "https://www.pornhub.com"}
        if rng := request.headers.get("Range"):
            req_headers["Range"] = rng

        if not is_hls and not download_mode:
            # Stream mode for MP4: resolve final URL then redirect browser to CDN directly.
            # This avoids proxying gigabytes through the server and bypasses Render's 30s timeout.
            head = session.head(
                cdn_url, headers=req_headers, allow_redirects=True,
                timeout=15, http_version=3, doh_url="https://1.1.1.1/dns-query",
            )
            if head.status_code not in (200, 206):
                return jsonify({
                    "status": "error",
                    "message": f"CDN returned HTTP {head.status_code}. Link may have expired — re-fetch from /ph/download.",
                }), head.status_code
            return redirect(str(head.url), code=302)

        # Download mode or HLS: stream through this server
        upstream = session.get(
            cdn_url, headers=req_headers, allow_redirects=True,
            timeout=30, http_version=3, doh_url="https://1.1.1.1/dns-query",
            stream=True,
        )
        if upstream.status_code not in (200, 206):
            return jsonify({
                "status": "error",
                "message": f"CDN returned HTTP {upstream.status_code}. Link may have expired — re-fetch from /ph/download.",
            }), upstream.status_code

        path_part    = urlparse(cdn_url).path
        fname        = path_part.split("/")[-1].split("?")[0] or "video"
        if not any(fname.endswith(ext) for ext in (".mp4", ".m3u8", ".ts", ".webm")):
            fname += ".mp4"
        content_type = "application/vnd.apple.mpegurl" if ".m3u8" in cdn_url else upstream.headers.get("Content-Type", "video/mp4")
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


app.debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

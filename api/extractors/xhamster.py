"""XHamster extractor — helpers + Flask blueprint.

XHamster encrypts MP4 source URLs in window.initials using a seeded PRNG XOR cipher.
Format: [1-byte algo_id][4-byte LE seed][XOR-encrypted payload]
Algorithm IDs 1-7 match yt-dlp's XHamster extractor exactly.

Streaming strategy:
  /xh/watch and /xh/download return ONE stream URL pointing to /xh/stream.
  /xh/stream re-fetches the XH page live in that same request, picks the HLS
  manifest (which covers all qualities), and streams it immediately.
  One request = one page fetch = one consistent outbound IP = no CDN 403.
  HLS is preferred because MP4 CDN URLs are IP-locked (yt-dlp: __needs_testing).
"""
import re
import json

from flask import Blueprint, request, jsonify, Response, stream_with_context
from urllib.parse import urlparse, quote

from api.utils import (
    render_watch_page, verify_proxy_token,
    check_raw_key, sign_url, _normalize_proxy_base_url, _get_api_key,
    adult_referer,
)
from api.extractors.xvideos import fetch_page  # reuse curl_cffi fetch

bp = Blueprint("xhamster", __name__)

_XH_VALID_HOSTS_RE = re.compile(
    r'^(?:[a-z]{2}\.)?(?:www\.)?xhamster(?:\d+)?\.(?:com|desi|one|xxx|net)$',
    re.IGNORECASE,
)
_XH_COOKIE_DOMAINS = [".xhamster.com", ".xhamster.desi",
                      ".xhamster.one", ".xhamster.xxx", ".xhamster.net"]
_XH_AGE_COOKIES = {
    "adc_ga_v2": "1", "is_adult_confirmed": "1",
    "xhamster-language": "en", "platform": "desktop",
}


def _cffi_session():
    from curl_cffi import requests as cffi_req
    session = cffi_req.Session(impersonate="chrome124")
    for domain in _XH_COOKIE_DOMAINS:
        for name, value in _XH_AGE_COOKIES.items():
            session.cookies.set(name, value, domain=domain)
    return session


def decrypt_url(hex_str: str) -> str:
    """Decrypt an XHamster hex-encoded video URL. Returns '' on failure."""
    try:
        raw = bytes.fromhex(hex_str.strip())
        if len(raw) < 6:
            return ""
        algo_id = raw[0]
        seed    = int.from_bytes(raw[1:5], "little", signed=True)
        payload = raw[5:]
        MASK32  = 0xFFFFFFFF

        def u32(x):
            return x & MASK32

        s = {"v": u32(seed)}

        def a1():
            s["v"] = u32(s["v"] * 1664525 + 1013904223); return s["v"]

        def a2():
            v = s["v"]
            v = u32(v ^ u32(v << 13)); v = u32(v ^ (v >> 17)); v = u32(v ^ u32(v << 5))
            s["v"] = v; return v

        def a3():
            v = u32(s["v"] + 0x9E3779B9)
            v = u32(v ^ (v >> 16)); v = u32(v * 0x85EBCA77)
            v = u32(v ^ (v >> 13)); v = u32(v * 0xC2B2AE3D)
            v = u32(v ^ (v >> 16)); s["v"] = v; return v

        def a4():
            v = u32(s["v"] + 0x6D2B79F5)
            v = u32((v << 7) | (v >> 25)); v = u32(v + 0x9E3779B9)
            v = u32(v ^ (v >> 11)); v = u32(v * 0x27D4EB2D)
            s["v"] = v; return v

        def a5():
            v = s["v"]
            v = u32(v ^ u32(v << 7)); v = u32(v ^ (v >> 9))
            v = u32(v ^ u32(v << 8)); v = u32(v + 0xA5A5A5A5)
            s["v"] = v; return v

        def a6():
            v     = u32(s["v"] * 0x2C9277B5 + 0xAC564B05)
            s2    = u32(v ^ (v >> 18))
            shift = (v >> 27) & 31
            s["v"] = v; return u32(s2 >> shift)

        def a7():
            v = u32(s["v"] + 0x9E3779B9)
            e = u32(v ^ u32(v << 5)); e = u32(e * 0x7FEB352D)
            e = u32(e ^ (e >> 15));   e = u32(e * 0x846CA68B)
            s["v"] = v; return e

        algos = {1: a1, 2: a2, 3: a3, 4: a4, 5: a5, 6: a6, 7: a7}
        if algo_id not in algos:
            return ""
        fn = algos[algo_id]
        return bytes(b ^ (fn() & 0xFF) for b in payload).decode("latin-1")
    except Exception:
        return ""


def extract_data(html: str) -> dict:
    m = re.search(r'window\.initials\s*=\s*(\{.+?\})\s*;', html, re.DOTALL)
    if not m:
        raise ValueError("Could not find window.initials in XHamster page.")
    initials = json.loads(m.group(1))
    xp       = initials.get("xplayerSettings", {})
    sources  = xp.get("sources", {}) or initials.get("videoModel", {}).get("sources", {})
    if not sources:
        raise ValueError("No sources found in XHamster initials JSON.")

    vm   = initials.get("videoModel", {})
    vi   = xp.get("videoInfo", {})
    secs = int(vm.get("duration", 0) or vi.get("duration", 0) or 0)

    qualities = []
    for item in sources.get("standard", {}).get("h264", []):
        ql = str(item.get("quality") or item.get("label") or "").replace("p", "").strip()
        if not ql or ql.lower() == "auto":
            continue
        for key in ("url", "fallback"):
            decrypted = decrypt_url((item.get(key) or "").strip())
            if decrypted.startswith("http"):
                qualities.append({"quality": ql, "format": "mp4", "url": decrypted})
                break

    if not qualities:
        for item in sources.get("standard", {}).get("av1", []):
            ql = str(item.get("quality") or "").replace("p", "").strip()
            if not ql or ql.lower() == "auto":
                continue
            decrypted = decrypt_url((item.get("url") or "").strip())
            if decrypted.startswith("http"):
                qualities.append({"quality": ql, "format": "mp4", "url": decrypted})

    hls_url = None
    for codec in ("h264", "av1"):
        entry   = sources.get("hls", {}).get(codec, {})
        hls_hex = (entry.get("url") or "").strip() if isinstance(entry, dict) else ""
        if hls_hex:
            decrypted = decrypt_url(hls_hex)
            if decrypted.startswith("http"):
                hls_url = decrypted
                break

    qualities.sort(key=lambda e: (0, -int(e["quality"])) if e["quality"].isdigit() else (1, 0))

    return {
        "title":            vm.get("title", vi.get("title", "Unknown Title")),
        "thumbnail":        vm.get("thumbURL", vm.get("thumb", vi.get("thumbUrl", ""))),
        "duration":         f"{secs // 60}:{secs % 60:02d}" if secs else "",
        "duration_seconds": secs,
        "qualities":        qualities,
        "hls_url":          hls_url,  # master HLS manifest covering all qualities
    }


def get_all_qualities(url: str) -> dict:
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _XH_VALID_HOSTS_RE.match(host):
        raise ValueError(f"Not a supported XHamster URL (host: {host!r}).")
    session = _cffi_session()
    html    = fetch_page(url, session, proxy="")
    return extract_data(html)


def _make_xh_stream_url(base_url: str, page_url: str, dl: bool = False) -> str:
    """Build ONE /xh/stream URL for the whole video (HLS handles quality internally)."""
    base_url = _normalize_proxy_base_url(base_url)
    enc      = quote(page_url, safe="")
    token    = sign_url(page_url)
    dl_part  = "&dl=1" if dl else ""
    if token:
        return f"{base_url}/xh/stream?src={enc}&{token}{dl_part}"
    key = _get_api_key()
    if key:
        return f"{base_url}/xh/stream?src={enc}&api_key={quote(key)}{dl_part}"
    return f"{base_url}/xh/stream?src={enc}{dl_part}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/xh/download", methods=["POST"])
def xh_download():
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' required"}), 400
    try:
        page_url   = body["url"].strip()
        base_url   = request.host_url.rstrip("/")
        result     = get_all_qualities(page_url)
        stream_url = _make_xh_stream_url(base_url, page_url)
        dl_url     = _make_xh_stream_url(base_url, page_url, dl=True)
        # Expose per-quality entries but all point to the same HLS stream
        qualities = [{
            "quality":      q["quality"],
            "format":       "hls",
            "url":          q["url"],
            "proxy_url":    stream_url,
            "download_url": dl_url,
        } for q in result["qualities"]]
        if not qualities:
            return jsonify({"status": "error", "message": "No streams found."}), 404
        return jsonify({
            "status": "success",
            "data": {
                "title": result["title"], "thumbnail": result["thumbnail"],
                "duration": result["duration"], "duration_seconds": result["duration_seconds"],
                "qualities": qualities,
                "best_proxy_url":    stream_url,
                "best_download_url": dl_url,
                "note": "Use best_proxy_url to stream (HLS, all qualities).",
            },
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal error: {e}"}), 500


@bp.route("/xh/watch")
def xh_watch():
    url = request.args.get("url", "").strip()
    if not url:
        return "<h2>Missing ?url=</h2>", 400
    try:
        base_url   = request.host_url.rstrip("/")
        result     = get_all_qualities(url)
        stream_url = _make_xh_stream_url(base_url, url)
        dl_url     = _make_xh_stream_url(base_url, url, dl=True)
        # Single HLS entry covers all qualities via adaptive bitrate
        qualities = [{
            "quality":      "Auto",
            "format":       "hls",
            "url":          result.get("hls_url", ""),
            "proxy_url":    stream_url,
            "download_url": dl_url,
        }]
        return render_watch_page(result, qualities)
    except Exception as e:
        return f"<h2 style='color:#f55'>Error: {e}</h2>", 500


@bp.route("/xh/stream")
def xh_stream():
    """
    Re-fetch the XH page live, get a fresh HLS manifest URL signed for the
    current outbound IP, stream it immediately.
    One request = one page fetch = one IP = no CDN 403.
    """
    import requests as _std_req

    src = request.args.get("src", "").strip()
    dl  = request.args.get("dl", "0") == "1"

    if not src:
        return jsonify({"status": "error", "message": "'src' required"}), 400
    if not verify_proxy_token(src) and not check_raw_key():
        return jsonify({"status": "error", "message": "Access denied."}), 403

    # Re-fetch page live — HLS URL is signed for THIS request's outbound IP
    try:
        from curl_cffi import requests as cffi_req
        cffi_session = cffi_req.Session(impersonate="chrome124")
        for domain in _XH_COOKIE_DOMAINS:
            for name, value in _XH_AGE_COOKIES.items():
                cffi_session.cookies.set(name, value, domain=domain)
        page_resp = cffi_session.get(src, allow_redirects=True, timeout=20)
        if page_resp.status_code != 200:
            raise ValueError(f"Page returned HTTP {page_resp.status_code}")
        final_url = str(page_resp.url)
        data      = extract_data(page_resp.text)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Page fetch failed: {e}"}), 502

    # Prefer HLS master manifest (all qualities in one URL, no IP-lock issues)
    cdn_url = data.get("hls_url") or ""
    if not cdn_url:
        # No HLS — try best MP4 (may 403 on datacenter IPs, but worth trying)
        best = next((q for q in data["qualities"] if q["format"] == "mp4"), None)
        if not best:
            return jsonify({"status": "error", "message": "No stream found."}), 404
        cdn_url = best["url"]

    # Use final page URL as Referer — same as yt-dlp (urlh.url)
    parsed_final = urlparse(final_url)
    hdrs = {
        "Referer":         final_url,
        "Origin":          f"{parsed_final.scheme}://{parsed_final.netloc}",
        "Accept":          "*/*",
        "Accept-Encoding": "identity",
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    if rng := request.headers.get("Range"):
        hdrs["Range"] = rng

    try:
        upstream = _std_req.get(cdn_url, headers=hdrs, stream=True,
                                allow_redirects=True, timeout=30)
    except Exception as e:
        return jsonify({"status": "error", "message": f"CDN fetch error: {e}"}), 502

    if upstream.status_code not in (200, 206):
        return jsonify({"status": "error",
                        "message": f"CDN returned HTTP {upstream.status_code}."}), upstream.status_code

    is_m3u8 = ".m3u8" in cdn_url or "mpegurl" in upstream.headers.get("Content-Type", "").lower()
    fname   = "playlist.m3u8" if is_m3u8 else "video.mp4"
    ct      = upstream.headers.get("Content-Type",
                                   "application/vnd.apple.mpegurl" if is_m3u8 else "video/mp4")
    disp    = f'attachment; filename="{fname}"' if dl else f'inline; filename="{fname}"'
    rh      = {"Content-Disposition": disp, "Accept-Ranges": "bytes",
               "Access-Control-Allow-Origin": "*", "X-Accel-Buffering": "no"}
    for h in ("Content-Length", "Content-Range"):
        if v := upstream.headers.get(h):
            rh[h] = v

    def generate():
        for chunk in upstream.iter_content(chunk_size=65536):
            if chunk:
                yield chunk

    return Response(stream_with_context(generate()),
                    status=upstream.status_code, content_type=ct, headers=rh)

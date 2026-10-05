"""XHamster extractor — helpers + Flask blueprint.

XHamster encrypts MP4 source URLs in window.initials using a seeded PRNG XOR cipher.
Format: [1-byte algo_id][4-byte LE seed][XOR-encrypted payload]
Algorithm IDs 1-7 match yt-dlp's XHamster extractor exactly.
"""
import re
import json

from flask import Blueprint, request, jsonify
from urllib.parse import urlparse

from api.utils import make_proxy_url, render_watch_page
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

    for codec in ("h264", "av1"):
        entry = sources.get("hls", {}).get(codec, {})
        hls_hex = (entry.get("url") or "").strip() if isinstance(entry, dict) else ""
        if hls_hex:
            decrypted = decrypt_url(hls_hex)
            if decrypted.startswith("http"):
                if "_TPL_" in decrypted:
                    # XHamster HLS URL is a template — expand into per-quality manifests.
                    # The URL contains multi=WxH:label:,... which lists all variants.
                    # e.g. multi=256x144:144p:,426x240:240p:,854x480:480p:,1280x720:720p:,...
                    # Replace _TPL_ with each label to get the real manifest URL.
                    import re as _re
                    variants = _re.findall(r'\d+x\d+:(\d+p):', decrypted)
                    for label in variants:
                        ql  = label.replace("p", "")
                        url = decrypted.replace("_TPL_", label)
                        # Only add if we don't already have this quality as MP4
                        if not any(q["quality"] == ql and q["format"] == "mp4" for q in qualities):
                            qualities.append({"quality": ql, "format": "hls", "url": url})
                else:
                    qualities.append({"quality": "hls", "format": "hls", "url": decrypted})
                break

    qualities.sort(key=lambda e: (0, -int(e["quality"])) if e["quality"].isdigit() else (1, 0))
    if not qualities:
        raise ValueError("Could not decrypt XHamster stream URLs. Algorithm may have changed.")

    return {
        "title":            vm.get("title", vi.get("title", "Unknown Title")),
        "thumbnail":        vm.get("thumbURL", vm.get("thumb", vi.get("thumbUrl", ""))),
        "duration":         f"{secs // 60}:{secs % 60:02d}" if secs else "",
        "duration_seconds": secs,
        "qualities":        qualities,
    }


def get_all_qualities(url: str) -> dict:
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _XH_VALID_HOSTS_RE.match(host):
        raise ValueError(f"Not a supported XHamster URL (host: {host!r}).")
    try:
        session = _cffi_session()
        html    = fetch_page(url, session)
        result  = extract_data(html)
    except Exception as primary_error:
        try:
            from api.extractors.ytdlp import _ytdlp_extract
            result_info = _ytdlp_extract(
                url,
                cookies={"adc_ga_v2": "1", "is_adult_confirmed": "1", "xhamster-language": "en"},
            )
            fallback_qualities = [{
                "quality": fmt["quality"],
                "format": fmt.get("format", "mp4"),
                "url": fmt["url"],
            } for fmt in result_info["formats"]]
            result = {
                "title": result_info["title"],
                "thumbnail": result_info["thumbnail"],
                "duration": result_info["duration"],
                "duration_seconds": result_info["duration_seconds"],
                "qualities": fallback_qualities,
            }
        except Exception as fallback_error:
            raise ValueError(
                f"XHamster extraction and yt-dlp fallback failed: {fallback_error}"
            ) from primary_error
    return result

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/xh/download", methods=["POST"])
def xh_download():
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' required"}), 400
    try:
        base_url = request.host_url.rstrip("/")
        result   = get_all_qualities(body["url"].strip())
        qualities = [{
            "quality": q["quality"], "format": q["format"], "url": q["url"],
            "proxy_url":    make_proxy_url(base_url, "/adult/proxy", q["url"], quality=q["quality"]),
            "download_url": make_proxy_url(base_url, "/adult/proxy", q["url"], extra="&dl=1", quality=q["quality"]),
        } for q in result["qualities"]]
        if not qualities:
            return jsonify({"status": "error", "message": "No streams found."}), 404
        best = next((q for q in qualities if q["format"] == "mp4"), qualities[0])
        return jsonify({
            "status": "success",
            "data": {
                "title": result["title"], "thumbnail": result["thumbnail"],
                "duration": result["duration"], "duration_seconds": result["duration_seconds"],
                "qualities": qualities,
                "best_proxy_url": best["proxy_url"], "best_download_url": best["download_url"],
                "note": "Use best_proxy_url to stream or best_download_url to download.",
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
        base_url = request.host_url.rstrip("/")
        result   = get_all_qualities(url)
        qualities = [{
            **q,
            "proxy_url":    make_proxy_url(base_url, "/adult/proxy", q["url"], quality=q["quality"]),
            "download_url": make_proxy_url(base_url, "/adult/proxy", q["url"], extra="&dl=1", quality=q["quality"]),
        } for q in result["qualities"]]
        if not qualities:
            return "<h2>No streams found.</h2>", 404
        return render_watch_page(result, qualities)
    except Exception as e:
        return f"<h2 style='color:#f55'>Error: {e}</h2>", 500

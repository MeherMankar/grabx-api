"""
yt-dlp based extractor — generic fallback for any yt-dlp supported site.

Used as:
  1. Primary extractor for /yt/download (any supported URL)
  2. Fallback in pornhub.py when get_media returns no MP4

yt-dlp supports 1000+ sites including PornHub, Xvideos, XHamster, etc.
Key advantage: uses its own IP-agnostic extraction that doesn't rely on
the get_media signed-URL endpoint that PH blocks on datacenter IPs.
"""

import os
import re
import sys

from flask import Blueprint, request, jsonify
from urllib.parse import quote, urlparse

from api.utils import make_proxy_url, render_watch_page, validate_proxy_target

bp = Blueprint("ytdlp", __name__)

# ---------------------------------------------------------------------------
# yt-dlp extraction helper
# ---------------------------------------------------------------------------

def _ytdlp_extract(url: str, cookies: dict = None, proxy: str = None) -> dict:
    """
    Extract video info using yt-dlp.
    Returns a dict with title, thumbnail, duration, duration_seconds, formats.
    Each format has: quality, format_id, ext, url, filesize, vcodec, acodec.
    """
    try:
        import yt_dlp
    except ImportError:
        raise ValueError("yt-dlp is not installed. Add yt-dlp to requirements.txt.")

    ydl_opts = {
        "quiet":           True,
        "no_warnings":     True,
        "extract_flat":    False,
        "skip_download":   True,
        "nocheckcertificate": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    # Add cookies via http_headers (works without a cookie jar file)
    if cookies:
        ydl_opts["http_headers"]["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    # Use proxy if provided, or auto-pick from pool
    if not proxy:
        from api.utils import get_proxy
        proxy = get_proxy()
    if proxy:
        ydl_opts["proxy"] = proxy

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("yt-dlp could not extract video info.")

    # If it's a playlist, take the first entry
    if info.get("_type") == "playlist":
        entries = info.get("entries", [])
        if not entries:
            raise ValueError("Playlist is empty.")
        info = entries[0]

    formats = info.get("formats") or []

    # Filter to video formats with a direct URL
    video_formats = []
    for f in formats:
        if not f.get("url"):
            continue
        vcodec    = f.get("vcodec", "none")
        acodec    = f.get("acodec", "none")
        ext       = f.get("ext", "mp4")
        protocol  = f.get("protocol", "https")

        # Skip audio-only, storyboard, manifest-only
        if ext in ("mhtml", "vtt"):
            continue
        # Skip pure audio
        if vcodec in ("none", None) and acodec not in ("none", None):
            continue

        # Determine real format type
        is_hls = protocol in ("m3u8", "m3u8_native") or ext == "m3u8"
        is_dash = ext == "mpd" or str(f.get("url", "")).lower().split("?")[0].endswith(".mpd")
        fmt    = "hls" if is_hls else "dash" if is_dash else "mp4"

        # Derive quality label from height
        height = f.get("height")
        if height:
            ql = str(height)
        else:
            ql = f.get("format_note") or f.get("format_id") or "unknown"

        video_formats.append({
            "quality":   ql,
            "format_id": f.get("format_id", ""),
            "ext":       ext,
            "format":    fmt,
            "url":       f["url"],
            "filesize":  f.get("filesize") or f.get("filesize_approx") or 0,
            "vcodec":    vcodec,
            "acodec":    acodec,
            "tbr":       f.get("tbr") or 0,
            "protocol":  protocol,
        })

    # Sort: direct MP4 first, then HLS; within each group best quality first
    def _sort_key(f):
        try:
            h = int(f["quality"])
        except ValueError:
            h = 0
        format_rank = {"mp4": 0, "hls": 1, "dash": 2}.get(f.get("format"), 3)
        return (format_rank, -h, -f.get("tbr", 0))

    video_formats.sort(key=_sort_key)

    # Deduplicate by quality (keep best format per quality level)
    seen_ql = set()
    deduped = []
    for f in video_formats:
        if f["quality"] not in seen_ql:
            seen_ql.add(f["quality"])
            deduped.append(f)

    if not deduped:
        raise ValueError("No video streams found. Video may be private, premium-only, or geo-restricted.")

    secs = int(info.get("duration") or 0)
    return {
        "title":            info.get("title") or info.get("fulltitle") or "Unknown Title",
        "thumbnail":        info.get("thumbnail") or "",
        "is_live":          bool(info.get("is_live")),
        "live_status":      info.get("live_status") or "",
        "duration":         f"{secs // 3600}:{(secs % 3600) // 60:02d}:{secs % 60:02d}" if secs >= 3600
                            else f"{secs // 60}:{secs % 60:02d}" if secs else "",
        "duration_seconds": secs,
        "webpage_url":      info.get("webpage_url") or url,
        "extractor":        info.get("extractor") or "",
        "formats":          deduped,
    }


def ytdlp_get_qualities(url: str, base_url: str,
                         proxy_path: str = "/adult/proxy",
                         cookies: dict = None) -> tuple:
    """
    Run yt-dlp extraction and build the standard qualities list.
    Results are cached for CACHE_TTL seconds to avoid hitting the proxy repeatedly.
    """
    from api.utils import cache_get, cache_set
    cache_key = f"ytdlp:{url}"
    cached = cache_get(cache_key)
    if cached:
        meta, raw_formats = cached
        if meta.get("is_live"):
            cached = None
        else:
            # Rebuild proxy URLs with current base_url (may differ between requests)
            qualities = []
            for f in raw_formats:
                ql  = f["quality"]
                qualities.append({
                    **f,
                    "proxy_url":    make_proxy_url(base_url, proxy_path, f["url"], quality=ql),
                    "download_url": make_proxy_url(base_url, proxy_path, f["url"], extra="&dl=1", quality=ql),
                })
            return meta, qualities

    result = _ytdlp_extract(url, cookies=cookies)

    qualities = []
    for f in result["formats"]:
        ql  = f["quality"]
        ext = f.get("ext", "mp4")
        fmt = f.get("format") or ("hls" if ext == "m3u8" else "dash" if ext == "mpd" else "mp4")
        cdn_url = f["url"]
        qualities.append({
            "quality":      ql,
            "format":       fmt,
            "ext":          ext,
            "url":          cdn_url,
            "filesize":     f.get("filesize", 0),
            "proxy_url":    make_proxy_url(base_url, proxy_path, cdn_url, quality=ql),
            "download_url": make_proxy_url(base_url, proxy_path, cdn_url, extra="&dl=1", quality=ql),
        })

    meta = {
        "title":            result["title"],
        "thumbnail":        result["thumbnail"],
        "is_live":          result["is_live"],
        "live_status":      result["live_status"],
        "duration":         result["duration"],
        "duration_seconds": result["duration_seconds"],
        "extractor":        result["extractor"],
    }

    # Cache the raw formats (without proxy URLs — those depend on base_url)
    raw_formats = [{"quality": q["quality"], "format": q["format"],
                    "ext": q.get("ext","mp4"), "url": q["url"],
                    "filesize": q.get("filesize",0)} for q in qualities]
    if not meta["is_live"]:
        cache_set(cache_key, (meta, raw_formats))

    return meta, qualities


# ---------------------------------------------------------------------------
# Generic /yt/download route
# ---------------------------------------------------------------------------

@bp.route("/yt/download", methods=["POST"])
def yt_download():
    """
    Extract stream/download links from any yt-dlp supported URL.
    Works with PornHub, Xvideos, XHamster, Twitter/X, Reddit, Twitch clips, etc.
    Full list: https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md
    """
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' field is required in JSON body"}), 400

    url      = str(body["url"]).strip()
    if not validate_proxy_target(url):
        return jsonify({"status": "error", "message": "URL must be a public HTTP(S) address."}), 400
    base_url = request.host_url.rstrip("/").replace("http://", "https://")

    # Age-gate cookies for known platforms
    parsed = urlparse(url)
    host   = (parsed.hostname or "").lower()
    cookies = {}
    if any(host == d or host.endswith("." + d) for d in (
        "pornhub.com", "pornhub.net", "pornhub.org", "pornhubpremium.com", "phncdn.com",
    )):
        cookies = {"accessAgeDisclaimerPH": "1", "age_verified": "1", "platform": "pc"}
    elif host == "xhamster.com" or host.endswith(".xhamster.com"):
        cookies = {"adc_ga_v2": "1", "is_adult_confirmed": "1", "xhamster-language": "en"}
    elif any(host == d or host.endswith("." + d) for d in ("xvideos.com", "xnxx.com")):
        cookies = {}

    try:
        meta, qualities = ytdlp_get_qualities(url, base_url, "/adult/proxy", cookies)

        if not qualities:
            return jsonify({"status": "error", "message": "No streams found."}), 404

        best = next((q for q in qualities if q["format"] == "mp4"), qualities[0])
        watch_url = f"{base_url}/yt/watch?url={quote(url, safe='')}"

        return jsonify({
            "status": "success",
            "data": {
                "title":             meta["title"],
                "thumbnail":         meta["thumbnail"],
                "duration":          meta["duration"],
                "duration_seconds":  meta["duration_seconds"],
                "is_live":           meta["is_live"],
                "live_status":       meta["live_status"],
                "extractor":         meta["extractor"],
                "watch_url":         watch_url,
                "qualities":         qualities,
                "best_proxy_url":    best["proxy_url"],
                "best_download_url": best["download_url"],
                "note": "yt-dlp extracted. Use best_proxy_url to stream or best_download_url to download.",
            },
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        print(f"[ytdlp] exception: {e}", file=sys.stderr)
        return jsonify({"status": "error", "message": f"yt-dlp error: {e}"}), 500


@bp.route("/yt/watch")
def yt_watch():
    """Browser video player for any yt-dlp supported URL."""
    url = request.args.get("url", "").strip()
    if not url:
        return "<h2>Missing ?url= parameter</h2>", 400
    if not validate_proxy_target(url):
        return "<h2>URL must be a public HTTP(S) address.</h2>", 400
    try:
        base_url = request.host_url.rstrip("/").replace("http://", "https://")
        parsed   = urlparse(url)
        host     = (parsed.hostname or "").lower()
        cookies  = {}
        if "pornhub" in host:
            cookies = {"accessAgeDisclaimerPH": "1", "age_verified": "1", "platform": "pc"}
        elif "xhamster" in host:
            cookies = {"adc_ga_v2": "1", "is_adult_confirmed": "1", "xhamster-language": "en"}

        meta, qualities = ytdlp_get_qualities(url, base_url, "/adult/proxy", cookies)
        if not qualities:
            return "<h2>No streams found.</h2>", 404
        return render_watch_page(meta, qualities)
    except Exception as e:
        return (f'<body style="background:#0f0f0f;color:#eee;padding:40px">'
                f'<h2 style="color:#f55">Error</h2><p>{e}</p></body>'), 500

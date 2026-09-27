"""JAVtiful extractor — helpers + Flask blueprint."""
import re
import json

import requests as req_lib
from flask import Blueprint, request, jsonify, Response, stream_with_context
from urllib.parse import urlparse, quote

from api.utils import (
    DESKTOP_UA, make_proxy_url, verify_proxy_token, check_raw_key, render_watch_page,
)

bp = Blueprint("javtiful", __name__)

_JAV_VALID_HOSTS_RE = re.compile(r'^(?:www\.)?javtiful\.com$', re.IGNORECASE)


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _JAV_VALID_HOSTS_RE.match(host):
        raise ValueError(f"Not a supported JAVtiful URL (host: {host!r}).")
    if "/video/" not in parsed.path:
        raise ValueError("URL does not point to a JAVtiful video page.")
    return url


def _fetch_page(url: str) -> str:
    try:
        from curl_cffi import requests as cffi_req
        session = cffi_req.Session(impersonate="chrome124")
        resp = session.get(url, allow_redirects=True, timeout=20,
                           http_version=3, doh_url="https://1.1.1.1/dns-query")
    except Exception as e:
        raise ValueError(f"Network error fetching JAVtiful page: {e}")
    if resp.status_code != 200:
        raise ValueError(f"JAVtiful returned HTTP {resp.status_code}.")
    return resp.text


def _extract_data(html: str) -> dict:
    m = re.search(r'"playerSources"\s*:\s*(\[.*?\])\s*[,}]', html, re.DOTALL)
    if not m:
        raise ValueError("Could not find playerSources. Video may be premium-only.")
    sources = json.loads(m.group(1))
    streams = [s for s in sources if s.get("src") and "jav.si" in s.get("src", "")]
    if not streams:
        raise ValueError("No streamable sources found. Video may be premium-only.")

    title = ""
    thumbnail = ""
    duration_secs = 0
    m_ld = re.search(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>\s*(\{.*?"@type"\s*:\s*"VideoObject".*?\})\s*</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if m_ld:
        try:
            ld = json.loads(m_ld.group(1))
            title = ld.get("name", "")
            thumbs = ld.get("thumbnailUrl", [])
            thumbnail = thumbs[0] if isinstance(thumbs, list) and thumbs else str(thumbs)
            dur_str = ld.get("duration", "")
            dur_m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', dur_str)
            if dur_m:
                duration_secs = (int(dur_m.group(1) or 0) * 3600 +
                                 int(dur_m.group(2) or 0) * 60 +
                                 int(dur_m.group(3) or 0))
        except Exception:
            pass
    if not title:
        m_t = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html)
        title = m_t.group(1).strip() if m_t else "Unknown Title"
    if not thumbnail:
        m_th = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
        thumbnail = m_th.group(1) if m_th else ""

    if duration_secs >= 3600:
        duration = f"{duration_secs // 3600}:{(duration_secs % 3600) // 60:02d}:{duration_secs % 60:02d}"
    elif duration_secs:
        duration = f"{duration_secs // 60}:{duration_secs % 60:02d}"
    else:
        duration = ""

    return {"title": title, "thumbnail": thumbnail, "duration": duration,
            "duration_seconds": duration_secs, "streams": streams}


def get_all_qualities(url: str) -> dict:
    url = _validate_url(url)
    return _extract_data(_fetch_page(url))


def _build_quality_label(s: dict, i: int) -> str:
    size = str(s.get("size") or "").strip()
    return s.get("label") or (size if size.isdigit() else None) or \
           s.get("type", "").replace("video/", "") or f"stream{i+1}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/jav/download", methods=["POST"])
def jav_download():
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' field is required in JSON body"}), 400
    try:
        base_url = request.host_url.rstrip("/")
        result   = get_all_qualities(body["url"].strip())
        qualities = []
        for i, s in enumerate(result["streams"]):
            src = s.get("src", "")
            ql  = str(_build_quality_label(s, i))
            qualities.append({
                "quality": ql, "format": "mp4", "url": src,
                "proxy_url":    make_proxy_url(base_url, "/jav/proxy", src, quality=ql),
                "download_url": make_proxy_url(base_url, "/jav/proxy", src, extra="&dl=1", quality=ql),
            })
        if not qualities:
            return jsonify({"status": "error", "message": "No streams found."}), 404
        best = qualities[0]
        watch_url = f"{base_url}/jav/watch?url={quote(body['url'].strip())}"
        return jsonify({
            "status": "success",
            "data": {
                "title": result["title"], "thumbnail": result["thumbnail"],
                "duration": result["duration"], "duration_seconds": result["duration_seconds"],
                "watch_url": watch_url, "qualities": qualities,
                "best_proxy_url": best["proxy_url"], "best_download_url": best["download_url"],
                "note": "Open watch_url in browser for the built-in player.",
            },
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal error: {e}"}), 500


@bp.route("/jav/watch")
def jav_watch():
    url = request.args.get("url", "").strip()
    if not url:
        return "<h2>Missing ?url= parameter</h2>", 400
    try:
        base_url = request.host_url.rstrip("/")
        result   = get_all_qualities(url)
        qualities = []
        for i, s in enumerate(result["streams"]):
            src = s.get("src", "")
            ql  = str(_build_quality_label(s, i))
            qualities.append({
                "quality": ql, "format": "mp4", "url": src,
                "proxy_url":    make_proxy_url(base_url, "/jav/proxy", src, quality=ql),
                "download_url": make_proxy_url(base_url, "/jav/proxy", src, extra="&dl=1", quality=ql),
            })
        if not qualities:
            return "<h2>No streams found.</h2>", 404
        return render_watch_page(result, qualities)
    except Exception as e:
        return (f'<body style="background:#0f0f0f;color:#eee;padding:40px">'
                f'<h2 style="color:#f55">Error</h2><p>{e}</p></body>'), 500


@bp.route("/jav/proxy")
def jav_proxy():
    cdn_url = request.args.get("url", "").strip()
    if not cdn_url:
        return jsonify({"status": "error", "message": "'url' query param required"}), 400
    if not verify_proxy_token(cdn_url) and not check_raw_key():
        return jsonify({"status": "error", "message": "Access denied."}), 403

    download_mode = request.args.get("dl", "0") == "1"
    try:
        headers = {"Referer": "https://javtiful.com/", "Origin": "https://javtiful.com",
                   "User-Agent": DESKTOP_UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        if rng := request.headers.get("Range"):
            headers["Range"] = rng
        upstream = req_lib.get(cdn_url, headers=headers, stream=True, allow_redirects=True, timeout=30)
        if upstream.status_code not in (200, 206):
            return jsonify({"status": "error", "message": f"CDN returned HTTP {upstream.status_code}."}), upstream.status_code
        path_part = urlparse(cdn_url).path
        fname = (path_part.split("/")[-1].split("?")[0] or "video")
        if not fname.endswith(".mp4"):
            fname += ".mp4"
        disp = f'attachment; filename="{fname}"' if download_mode else f'inline; filename="{fname}"'
        rh = {"Content-Disposition": disp, "Accept-Ranges": "bytes",
              "Access-Control-Allow-Origin": "*", "X-Accel-Buffering": "no"}
        for h in ("Content-Length", "Content-Range", "ETag"):
            if v := upstream.headers.get(h):
                rh[h] = v

        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        return Response(stream_with_context(generate()),
                        status=upstream.status_code,
                        content_type=upstream.headers.get("Content-Type", "video/mp4"),
                        headers=rh)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Proxy error: {e}"}), 502

"""XHamster extractor — helpers + Flask blueprint.

XHamster encrypts MP4 source URLs in window.initials using a seeded PRNG XOR cipher.
Format: [1-byte algo_id][4-byte LE seed][XOR-encrypted payload]
Algorithm IDs 1-7 match yt-dlp's XHamster extractor exactly.

Streaming strategy:
  /xh/stream  — re-fetches XH page live, gets fresh HLS manifest URL (same outbound IP),
                fetches the manifest, rewrites all segment/sub-manifest URLs to go through
                /xh/seg so the browser never touches xhcdn.com directly.
  /xh/seg     — proxies a single xhcdn.com URL with the correct Referer.
                Uses a signed token so only our own rewritten manifests can use it.
"""
import re
import json

from flask import Blueprint, request, jsonify, Response, stream_with_context
from urllib.parse import urlparse, quote, urljoin

from api.utils import (
    render_watch_page, verify_proxy_token,
    check_raw_key, sign_url, _normalize_proxy_base_url, _get_api_key,
)
from api.extractors.xvideos import fetch_page, _request_adult_stream  # shared CDN retry logic

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
_XH_REFERER = "https://xhamster.com/"


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
        "hls_url":          hls_url,
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


def _make_xh_stream_url(
    base_url: str, page_url: str, dl: bool = False, quality: str = "",
    media_url: str = "",
) -> str:
    """Build /xh/stream URL — re-fetches page live, rewrites HLS manifest."""
    base_url = _normalize_proxy_base_url(base_url)
    enc      = quote(page_url, safe="")
    token    = sign_url(media_url or page_url)
    dl_part  = "&dl=1" if dl else ""
    quality_part = f"&quality={quote(quality, safe='')}" if quality else ""
    media_part = f"&media={quote(media_url, safe='')}" if media_url else ""
    if token:
        return f"{base_url}/xh/stream?src={enc}&{token}{dl_part}{quality_part}{media_part}"
    key = _get_api_key()
    if key:
        return f"{base_url}/xh/stream?src={enc}&api_key={quote(key)}{dl_part}{quality_part}{media_part}"
    return f"{base_url}/xh/stream?src={enc}{dl_part}{quality_part}{media_part}"


def _make_xh_seg_url(base_url: str, cdn_url: str) -> str:
    """Build /xh/seg proxy URL for a single xhcdn.com segment or sub-manifest."""
    base_url = _normalize_proxy_base_url(base_url)
    enc      = quote(cdn_url, safe="")
    token    = sign_url(cdn_url)
    if token:
        return f"{base_url}/xh/seg?url={enc}&{token}"
    key = _get_api_key()
    if key:
        return f"{base_url}/xh/seg?url={enc}&api_key={quote(key)}"
    return f"{base_url}/xh/seg?url={enc}"


def _rewrite_hls_manifest(manifest_text: str, manifest_url: str, base_url: str) -> str:
    """Rewrite all URLs in an HLS manifest to go through /xh/seg."""
    lines   = []
    for line in manifest_text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if stripped.startswith("#"):
            # Rewrite URI="..." attributes in tags like #EXT-X-KEY, #EXT-X-MAP
            def _rw_uri(m, _url=manifest_url, _burl=base_url):
                abs_uri = urljoin(_url, m.group(2))
                return f'URI={m.group(1)}{_make_xh_seg_url(_burl, abs_uri)}{m.group(1)}'
            lines.append(re.sub(r"URI=([\"'])(.*?)\1", _rw_uri, line))
        else:
            # Segment or sub-manifest URL
            abs_uri = urljoin(manifest_url, stripped)
            lines.append(_make_xh_seg_url(base_url, abs_uri))
    return "\n".join(lines) + "\n"


def _select_hls_variant(manifest_text: str, requested_quality: str) -> str:
    """Return the HLS variant nearest to the requested video height."""
    if not requested_quality or "#EXT-X-STREAM-INF:" not in manifest_text:
        return ""
    try:
        target = int(requested_quality)
    except ValueError:
        return ""

    selected_uri = ""
    selected_distance = None
    pending_height = None
    for line in manifest_text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            match = re.search(r"(?:^|,)RESOLUTION=\d+x(\d+)(?:,|$)", line)
            pending_height = int(match.group(1)) if match else None
        elif pending_height is not None and line and not line.startswith("#"):
            distance = abs(pending_height - target)
            if selected_distance is None or distance < selected_distance:
                selected_uri = line
                selected_distance = distance
            pending_height = None
    return selected_uri


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
        qualities  = [{
            "quality":      q["quality"],
            "format":       "hls",
            "url":          q["url"],
            "proxy_url": _make_xh_stream_url(
                base_url, page_url, quality=q["quality"],
            ),
            "download_url": _make_xh_stream_url(
                base_url, page_url, quality=q["quality"],
            ),
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
                "best_download_url": stream_url,
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
        qualities  = [{
            "quality":      "Auto",
            "format":       "hls",
            "url":          result.get("hls_url", ""),
            "proxy_url":    stream_url,
            "download_url": stream_url,
            "download_label": "Copy HLS URL",
        }]
        for source in result["qualities"]:
            quality_stream_url = _make_xh_stream_url(
                base_url, url, quality=source["quality"],
            )
            qualities.append({
                "quality": source["quality"],
                "format": "hls",
                "url": result.get("hls_url", ""),
                "proxy_url": quality_stream_url,
                "download_url": quality_stream_url,
                "download_label": "Copy HLS URL",
            })
        return render_watch_page(result, qualities)
    except Exception as e:
        return f"<h2 style='color:#f55'>Error: {e}</h2>", 500


@bp.route("/xh/stream")
def xh_stream():
    """
    Re-fetch XH page live → get fresh HLS manifest URL signed for current IP →
    fetch the manifest → rewrite all segment/sub-manifest URLs through /xh/seg →
    return rewritten manifest to HLS.js. With dl=1, stream the requested MP4
    quality from the same freshly fetched page as an attachment.
    The browser never touches xhcdn.com directly — all requests go through /xh/seg.
    """
    import requests as _std_req

    src = request.args.get("src", "").strip()
    dl  = request.args.get("dl", "0") == "1"
    media = request.args.get("media", "").strip()

    if not src:
        return jsonify({"status": "error", "message": "'src' required"}), 400
    token_target = media if dl and media else src
    if not verify_proxy_token(token_target) and not check_raw_key():
        return jsonify({"status": "error", "message": "Access denied."}), 403

    # Re-fetch page live — HLS URL signed for THIS request's outbound IP
    try:
        page_session = None
        try:
            page_session = _cffi_session()
            page_resp = page_session.get(src, allow_redirects=True, timeout=20)
        except Exception:
            page_resp = None
        if page_resp is None or page_resp.status_code != 200:
            import requests as standard_requests
            page_session = standard_requests.Session()
            page_resp = page_session.get(
                src,
                headers={"User-Agent": "Mozilla/5.0"},
                allow_redirects=True,
                timeout=20,
            )
        if page_resp.status_code != 200:
            raise ValueError(f"Page returned HTTP {page_resp.status_code}")
        final_url = str(page_resp.url)
        data      = extract_data(page_resp.text)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Page fetch failed: {e}"}), 502

    parsed_final = urlparse(final_url)
    hdrs = {
        "Referer":         final_url,
        "Origin":          f"{parsed_final.scheme}://{parsed_final.netloc}",
        "Accept":          "*/*",
        "Accept-Encoding": "identity",
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }

    if dl:
        mp4_sources = [q for q in data["qualities"] if q["format"] == "mp4"]
        requested_quality = request.args.get("quality", "").strip()
        selected = next(
            (q for q in mp4_sources if q["quality"] == requested_quality),
            None,
        ) if requested_quality else None
        if media:
            media_host = (urlparse(media).hostname or "").lower()
            if not media.startswith(("http://", "https://")) or not (
                media_host.endswith(".xhcdn.com")
                or media_host.endswith(".xhpingcdn.com")
            ):
                return jsonify({"status": "error", "message": "Invalid MP4 source."}), 400
            selected = {"quality": requested_quality or "video", "url": media}
        elif not requested_quality:
            selected = max(
                mp4_sources,
                key=lambda q: int(q["quality"]) if q["quality"].isdigit() else 0,
                default=None,
            )
        if not selected:
            return jsonify({
                "status": "error",
                "message": "No MP4 source is available for the requested quality.",
            }), 404

        if rng := request.headers.get("Range"):
            hdrs["Range"] = rng
        try:
            upstream = _request_adult_stream(page_session, selected["url"], hdrs)
        except Exception as e:
            return jsonify({"status": "error", "message": f"Download fetch error: {e}"}), 502
        if upstream.status_code not in (200, 206):
            upstream.close()
            return jsonify({
                "status": "error",
                "message": f"MP4 CDN returned HTTP {upstream.status_code}.",
            }), upstream.status_code

        quality = selected["quality"]
        headers = {
            "Content-Disposition": f'attachment; filename="xhamster-{quality}p.mp4"',
            "Accept-Ranges": "bytes",
            "Access-Control-Allow-Origin": "*",
            "X-Accel-Buffering": "no",
        }
        for name in ("Content-Length", "Content-Range"):
            if value := upstream.headers.get(name):
                headers[name] = value

        def download_chunks():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return Response(
            stream_with_context(download_chunks()),
            status=upstream.status_code,
            content_type=upstream.headers.get("Content-Type", "video/mp4"),
            headers=headers,
        )

    cdn_url = data.get("hls_url") or ""
    if not cdn_url:
        return jsonify({"status": "error", "message": "No HLS stream found."}), 404

    try:
        manifest_resp = _std_req.get(cdn_url, headers=hdrs, timeout=20, allow_redirects=True)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Manifest fetch error: {e}"}), 502

    if manifest_resp.status_code not in (200, 206):
        return jsonify({"status": "error",
                        "message": f"Manifest CDN returned HTTP {manifest_resp.status_code}."}), manifest_resp.status_code

    # Rewrite all segment/sub-manifest URLs through /xh/seg
    base_url     = request.host_url.rstrip("/")
    actual_url   = manifest_resp.url  # URL after any redirects
    requested_quality = request.args.get("quality", "").strip()
    variant_uri = _select_hls_variant(manifest_resp.text, requested_quality)
    if variant_uri:
        variant_url = urljoin(actual_url, variant_uri)
        try:
            variant_resp = _std_req.get(
                variant_url, headers=hdrs, timeout=20, allow_redirects=True,
            )
        except Exception as e:
            return jsonify({"status": "error", "message": f"Variant fetch error: {e}"}), 502
        if variant_resp.status_code not in (200, 206):
            return jsonify({
                "status": "error",
                "message": f"Variant CDN returned HTTP {variant_resp.status_code}.",
            }), variant_resp.status_code
        manifest_resp.close()
        manifest_resp = variant_resp
        actual_url = manifest_resp.url

    rewritten    = _rewrite_hls_manifest(manifest_resp.text, actual_url, base_url)

    return Response(
        rewritten,
        status=200,
        content_type="application/vnd.apple.mpegurl; charset=utf-8",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-cache",
            "Content-Disposition": 'inline; filename="playlist.m3u8"',
        },
    )


@bp.route("/xh/seg")
def xh_seg():
    """Proxy a single xhcdn.com segment or sub-manifest with correct Referer."""
    import requests as _std_req

    cdn_url = request.args.get("url", "").strip()
    if not cdn_url:
        return jsonify({"status": "error", "message": "'url' required"}), 400
    if not verify_proxy_token(cdn_url) and not check_raw_key():
        return jsonify({"status": "error", "message": "Access denied."}), 403

    hdrs = {
        "Referer":         _XH_REFERER,
        "Origin":          "https://xhamster.com",
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
        return jsonify({"status": "error", "message": f"Segment fetch error: {e}"}), 502

    if upstream.status_code not in (200, 206):
        return jsonify({"status": "error",
                        "message": f"CDN returned HTTP {upstream.status_code}."}), upstream.status_code

    is_m3u8 = ".m3u8" in cdn_url or "mpegurl" in upstream.headers.get("Content-Type", "").lower()

    # If this is a sub-manifest (quality-level playlist), rewrite its segment URLs too
    if is_m3u8:
        base_url  = request.host_url.rstrip("/")
        actual_url = upstream.url
        try:
            rewritten = _rewrite_hls_manifest(upstream.text, actual_url, base_url)
        finally:
            upstream.close()
        return Response(
            rewritten,
            status=200,
            content_type="application/vnd.apple.mpegurl; charset=utf-8",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
        )

    ct  = upstream.headers.get("Content-Type", "video/mp2t")
    rh  = {"Accept-Ranges": "bytes", "Access-Control-Allow-Origin": "*",
           "X-Accel-Buffering": "no"}
    for h in ("Content-Length", "Content-Range"):
        if v := upstream.headers.get(h):
            rh[h] = v

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(generate()),
                    status=upstream.status_code, content_type=ct, headers=rh)

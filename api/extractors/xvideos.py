"""Xvideos + XNXX extractor — helpers + Flask blueprint."""
import re
import json

import requests as _req
from flask import Blueprint, request, jsonify, Response, stream_with_context
from urllib.parse import urlparse

from api.utils import (
    make_proxy_url, verify_proxy_token, check_raw_key,
    rewrite_dash_manifest, resolve_hls_uri, render_watch_page,
    adult_referer, validate_proxy_target,
)

bp = Blueprint("xvideos", __name__)

_XV_VALID_HOSTS_RE   = re.compile(r'^(?:www\.)?xvideos(?:\d+)?\.com$', re.IGNORECASE)
_XNXX_VALID_HOSTS_RE = re.compile(r'^(?:www\.)?xnxx\.com$', re.IGNORECASE)
_XNXX_AGE_COOKIES    = {"nv_age_check": "1"}


def _cffi_session(cookies: dict = None, domains: list = None):
    from curl_cffi import requests as cffi_req
    session = cffi_req.Session(impersonate="chrome124")
    if cookies and domains:
        for domain in domains:
            for name, value in cookies.items():
                session.cookies.set(name, value, domain=domain)
    return session


def fetch_page(url: str, session=None, proxy: str = None) -> str:
    from api.utils import get_proxy
    if proxy is None:
        proxy = get_proxy()
    try:
        if session is None:
            session = _cffi_session()
        kwargs = dict(allow_redirects=True, timeout=20,
                      http_version=3, doh_url="https://1.1.1.1/dns-query")
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        resp = session.get(url, **kwargs)
    except Exception as e:
        raise ValueError(f"Network error fetching page: {e}")
    if resp.status_code != 200:
        raise ValueError(f"Site returned HTTP {resp.status_code}.")
    return resp.text


def extract_data(html: str, site_domain: str) -> dict:
    data: dict = {}
    all_calls = re.findall(
        r"html5player\.(setVideo\w+)\s*\(\s*['\"]([^'\"]+)['\"]", html, re.IGNORECASE
    )
    for method, value in all_calls:
        ml = method.lower()
        if "urlow" in ml or "urllow" in ml:
            data["url_low"] = value
        elif "urlhigh" in ml:
            data["url_high"] = value
        elif "urlhls" in ml or ("hls" in ml and "url" in ml):
            data["url_hls"] = value
        elif "url1080" in ml:
            data["url_1080p"] = value
        elif "url720" in ml:
            data["url_720p"] = value
        elif "url480" in ml:
            data["url_480p"] = value
        elif "url360" in ml:
            data["url_360p"] = value
        elif "title" in ml:
            data["title"] = value
        elif "thumburl" in ml and "169" not in ml and "slide" not in ml:
            data.setdefault("thumb", value)
        elif "duration" in ml:
            data["duration"] = value

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

    if not any(data.get(k) for k in ("url_low", "url_high", "url", "url_hls")):
        raise ValueError(f"No video stream URLs found ({site_domain}). May be premium-only.")
    return data


def build_qualities(data: dict, base_url: str, proxy_path: str, referer: str) -> list:
    qualities = []
    mp4_map = sorted(
        ((key[4:-1], value) for key, value in data.items()
         if re.fullmatch(r"url_\d+p", key) and value),
        key=lambda item: int(item[0]),
        reverse=True,
    )
    mp4_map.extend([
        ("480", data.get("url_high")),
        ("360", data.get("url_low") or data.get("url")),
    ])
    seen = set()
    for ql, url in mp4_map:
        if url and url not in seen:
            seen.add(url)
            fmt = data.get(f"format_{ql}", "mp4")
            qualities.append({
                "quality": ql, "format": fmt, "url": url,
                "proxy_url":    make_proxy_url(base_url, proxy_path, url, quality=ql),
                "download_url": make_proxy_url(base_url, proxy_path, url, extra="&dl=1", quality=ql),
            })

    hls_url = data.get("url_hls")
    if hls_url:
        try:
            r = _req.get(hls_url, headers={"Referer": referer}, timeout=8)
            if r.status_code == 200:
                stream_re = re.compile(r'#EXT-X-STREAM-INF:[^\n]*NAME="([^"]+)"[^\n]*\n([^\n]+)', re.IGNORECASE)
                for m in stream_re.finditer(r.text):
                    name = m.group(1)
                    seg  = m.group(2).strip()
                    if not seg.startswith("http"):
                        seg = hls_url[:hls_url.rfind("/") + 1] + seg
                    ql = name.replace("p", "")
                    if not any(q["quality"] == ql and q["format"] == "mp4" for q in qualities):
                        qualities.append({
                            "quality": ql, "format": "hls", "url": seg,
                            "proxy_url":    make_proxy_url(base_url, proxy_path, seg, quality=ql),
                            "download_url": make_proxy_url(base_url, proxy_path, seg, extra="&dl=1", quality=ql),
                        })
        except Exception:
            pass
        if not any(q["format"] == "hls" for q in qualities):
            qualities.append({
                "quality": "hls", "format": "hls", "url": hls_url,
                "proxy_url":    make_proxy_url(base_url, proxy_path, hls_url, quality="hls"),
                "download_url": make_proxy_url(base_url, proxy_path, hls_url, extra="&dl=1", quality="hls"),
            })

    qualities.sort(key=lambda q: (0, -int(q["quality"])) if q["quality"].isdigit() else (1, 0))
    return qualities


def _request_adult_stream(session, url: str, headers: dict):
    """Try the original direct HTTP/3 route before configured proxy fallbacks."""
    import re as _re
    from api.utils import get_proxy, get_proxy_for_url, _parse_proxy_list
    pinned_proxy = get_proxy_for_url(url)

    # Check if the CDN URL has an IP-lock marker (data=<IP>-dvp)
    has_ip_marker = bool(_re.search(r"/data=\d{1,3}(?:\.\d{1,3}){3}-dvp", url))

    proxy = get_proxy()
    if pinned_proxy:
        alternate_proxies = [
            candidate for candidate in _parse_proxy_list()
            if candidate != pinned_proxy
        ]
        proxy = alternate_proxies[0] if alternate_proxies else ""

    attempts = [
        ("HTTP/3", {
            "http_version": 3,
            "doh_url": "https://1.1.1.1/dns-query",
        }),
        ("negotiated HTTP", {}),
    ]
    if pinned_proxy:
        # URL was signed for this specific proxy IP — try it directly
        attempts.append(("IP-matched proxy", {
            "proxies": {"http": pinned_proxy, "https": pinned_proxy},
        }))
    elif has_ip_marker:
        # URL was signed for the server's own IP (no proxy used during page fetch).
        # Only try direct routes — adding a proxy will cause 403.
        pass
    else:
        # No IP lock — try configured proxy as fallback
        if proxy:
            attempts.append(("configured proxy", {
                "proxies": {"http": proxy, "https": proxy},
            }))

    last_response = None
    last_error = None
    for label, options in attempts:
        try:
            upstream = session.get(
                url, headers=headers, allow_redirects=True, timeout=30,
                stream=True, **options,
            )
        except Exception as exc:
            last_error = exc
            from flask import current_app
            current_app.logger.warning(
                "%s CDN request failed (%s).", label, type(exc).__name__,
            )
            continue
        if upstream.status_code in (200, 206):
            if last_response is not None:
                last_response.close()
            return upstream
        if last_response is not None:
            last_response.close()
        last_response = upstream
        from flask import current_app
        current_app.logger.warning(
            "%s CDN request returned %s; trying the next route.",
            label, upstream.status_code,
        )

    if last_response is not None:
        return last_response
    if last_error is not None:
        raise last_error
    raise RuntimeError("No CDN request route was available.")


def _ytdlp_fallback(url: str, site: str):
    from api.extractors.ytdlp import _ytdlp_extract

    result = _ytdlp_extract(url)
    data = {"title": result["title"], "thumb": result["thumbnail"],
            "duration": result["duration_seconds"]}
    for fmt in result["formats"]:
        quality = str(fmt.get("quality", ""))
        if fmt.get("format") == "hls":
            data.setdefault("url_hls", fmt["url"])
        elif quality.isdigit():
            data[f"url_{quality}p"] = fmt["url"]
            if fmt.get("format") == "dash":
                data[f"format_{quality}"] = "dash"
        else:
            data.setdefault("url", fmt["url"])
    if (
        not any(data.get(k) for k in ("url", "url_low", "url_high", "url_hls"))
        and not any(k.startswith("url_") and v for k, v in data.items())
    ):
        raise ValueError(f"yt-dlp fallback returned no {site} streams.")
    secs = int(data.get("duration", 0) or 0)
    meta = {
        "title": data["title"], "thumbnail": data["thumb"],
        "duration": f"{secs // 60}:{secs % 60:02d}" if secs else "",
        "duration_seconds": secs,
    }
    return meta, data


def get_xv_qualities(url: str):
    from api.utils import cache_get, cache_set
    cache_key = f"xv:{url}"
    cached = cache_get(cache_key)
    if cached:
        return cached
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    if not _XV_VALID_HOSTS_RE.match((parsed.hostname or "").lower()):
        raise ValueError(f"Not a supported Xvideos URL.")
    try:
        session = _cffi_session()
        html    = fetch_page(url, session)
        data    = extract_data(html, "xvideos.com")
        secs    = int(data.get("duration", 0) or 0)
        result  = ({"title": data.get("title", "Unknown"), "thumbnail": data.get("thumb", ""),
                    "duration": f"{secs // 60}:{secs % 60:02d}" if secs else "",
                    "duration_seconds": secs}, data)
    except Exception as primary_error:
        try:
            result = _ytdlp_fallback(url, "Xvideos")
        except Exception as fallback_error:
            raise ValueError(f"Xvideos extraction and yt-dlp fallback failed: {fallback_error}") from primary_error
    cache_set(cache_key, result)
    return result


def get_xnxx_qualities(url: str):
    from api.utils import cache_get, cache_set
    cache_key = f"xnxx:{url}"
    cached = cache_get(cache_key)
    if cached:
        return cached
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    if not _XNXX_VALID_HOSTS_RE.match((parsed.hostname or "").lower()):
        raise ValueError(f"Not a supported XNXX URL.")
    try:
        session = _cffi_session(_XNXX_AGE_COOKIES, [".xnxx.com"])
        html    = fetch_page(url, session)
        data    = extract_data(html, "xnxx.com")
        secs    = int(data.get("duration", 0) or 0)
        result  = ({"title": data.get("title", "Unknown"), "thumbnail": data.get("thumb", ""),
                    "duration": f"{secs // 60}:{secs % 60:02d}" if secs else "",
                    "duration_seconds": secs}, data)
    except Exception as primary_error:
        try:
            result = _ytdlp_fallback(url, "XNXX")
        except Exception as fallback_error:
            raise ValueError(f"XNXX extraction and yt-dlp fallback failed: {fallback_error}") from primary_error
    cache_set(cache_key, result)
    return result

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _download_handler(meta, data, referer):
    base_url  = request.host_url.rstrip("/")
    qualities = build_qualities(data, base_url, "/adult/proxy", referer)
    if not qualities:
        return jsonify({"status": "error", "message": "No streams found."}), 404
    best = qualities[0]
    return jsonify({
        "status": "success",
        "data": {
            "title": meta["title"], "thumbnail": meta["thumbnail"],
            "duration": meta["duration"], "duration_seconds": meta["duration_seconds"],
            "qualities": qualities,
            "best_proxy_url": best["proxy_url"], "best_download_url": best["download_url"],
            "note": "Use best_proxy_url to stream or best_download_url to download.",
        },
    })


@bp.route("/xv/download", methods=["POST"])
def xv_download():
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' required"}), 400
    try:
        meta, data = get_xv_qualities(body["url"].strip())
        return _download_handler(meta, data, "https://www.xvideos.com/")
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal error: {e}"}), 500


@bp.route("/xv/watch")
def xv_watch():
    url = request.args.get("url", "").strip()
    if not url:
        return "<h2>Missing ?url=</h2>", 400
    try:
        meta, data = get_xv_qualities(url)
        qualities  = build_qualities(data, request.host_url.rstrip("/"), "/adult/proxy", "https://www.xvideos.com/")
        if not qualities:
            return "<h2>No streams found.</h2>", 404
        return render_watch_page(meta, qualities)
    except Exception as e:
        return f"<h2 style='color:#f55'>Error: {e}</h2>", 500


@bp.route("/xnxx/download", methods=["POST"])
def xnxx_download():
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' required"}), 400
    try:
        meta, data = get_xnxx_qualities(body["url"].strip())
        return _download_handler(meta, data, "https://www.xnxx.com/")
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal error: {e}"}), 500


@bp.route("/xnxx/watch")
def xnxx_watch():
    url = request.args.get("url", "").strip()
    if not url:
        return "<h2>Missing ?url=</h2>", 400
    try:
        meta, data = get_xnxx_qualities(url)
        qualities  = build_qualities(data, request.host_url.rstrip("/"), "/adult/proxy", "https://www.xnxx.com/")
        if not qualities:
            return "<h2>No streams found.</h2>", 404
        return render_watch_page(meta, qualities)
    except Exception as e:
        return f"<h2 style='color:#f55'>Error: {e}</h2>", 500


@bp.route("/adult/proxy")
def adult_proxy():
    cdn_url = request.args.get("url", "").strip()
    if not cdn_url:
        return jsonify({"status": "error", "message": "'url' required"}), 400
    if not verify_proxy_token(cdn_url) and not check_raw_key():
        return jsonify({"status": "error", "message": "Access denied."}), 403

    download_mode = request.args.get("dl", "0") == "1"
    is_m3u8 = ".m3u8" in cdn_url
    is_ts   = cdn_url.endswith(".ts") or ".ts?" in cdn_url

    try:
        import re as _re
        from curl_cffi import requests as cffi_req
        from api.utils import get_proxy_by_id
        session  = cffi_req.Session(impersonate="chrome124")
        referer  = adult_referer(cdn_url)
        headers  = {"Referer": referer, "Origin": referer.rstrip("/"),
                    "Accept": "*/*", "Accept-Encoding": "identity"}
        if rng := request.headers.get("Range"):
            headers["Range"] = rng

        # If a proxy_id was embedded in the URL, reuse that same proxy so the
        # CDN IP check passes (CDN URLs are signed to the IP that fetched the page).
        pid_str  = request.args.get("pid", "")
        pid      = int(pid_str) if pid_str.lstrip("-").isdigit() else -1
        proxy    = get_proxy_by_id(pid) if pid >= 0 else ""

        fetch_kwargs = dict(
            headers=headers, allow_redirects=True, timeout=30, stream=True,
        )
        if proxy:
            fetch_kwargs["proxies"] = {"http": proxy, "https": proxy}
        else:
            # No pinned proxy — use HTTP/3 + DoH only for non-IPv6-locked URLs.
            # IPv6-locked XH CDN URLs must use OS resolver (not DoH) to preserve IPv6.
            _has_ipv6_lock = bool(_re.search(r'/data=[0-9a-fA-F:]{4,}-dvp/', cdn_url))
            if not _has_ipv6_lock:
                fetch_kwargs["http_version"] = 3
                fetch_kwargs["doh_url"] = "https://1.1.1.1/dns-query"

        upstream = session.get(cdn_url, **fetch_kwargs)

        # Fallback: if first attempt fails, retry without DoH/HTTP3 and without proxy
        if upstream.status_code not in (200, 206):
            upstream.close()
            upstream = session.get(cdn_url, headers=headers,
                                   allow_redirects=True, timeout=30, stream=True)

        if upstream.status_code not in (200, 206):
            return jsonify({"status": "error", "message": f"CDN returned HTTP {upstream.status_code}."}), upstream.status_code

        if is_m3u8:
            manifest = upstream.text.strip()
            if not manifest or len(manifest) < 10:
                return jsonify({"status": "error", "message": "CDN returned empty manifest."}), 502
            base_host    = request.host_url.rstrip("/")
            parsed_cdn   = urlparse(cdn_url)
            cdn_base_dir = cdn_url[:cdn_url.rfind("/") + 1]
            lines = []
            for line in manifest.splitlines():
                stripped = line.strip()
                if not stripped:
                    lines.append(line)
                    continue
                if stripped.startswith("#"):
                    def _rw(m, _base=cdn_base_dir, _parsed=parsed_cdn, _host=base_host):
                        abs_uri = resolve_hls_uri(m.group(1), _base, _parsed)
                        return f'URI="{make_proxy_url(_host, "/adult/proxy", abs_uri)}"'
                    lines.append(re.sub(r'URI="([^"]+)"', _rw, line))
                else:
                    abs_uri = resolve_hls_uri(stripped, cdn_base_dir, parsed_cdn)
                    lines.append(make_proxy_url(base_host, "/adult/proxy", abs_uri))
            return Response("\n".join(lines) + "\n", status=200,
                            content_type="application/vnd.apple.mpegurl; charset=utf-8",
                            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache",
                                     "Content-Disposition": 'inline; filename="playlist.m3u8"'})

        path_part = urlparse(cdn_url).path
        fname     = path_part.split("/")[-1].split("?")[0] or "video"
        if not any(fname.endswith(ext) for ext in (".mp4", ".webm", ".ts", ".m3u8")):
            fname += ".mp4"
        ct   = upstream.headers.get("Content-Type", "video/mp2t" if is_ts else "video/mp4")
        disp = f'attachment; filename="{fname}"' if download_mode else f'inline; filename="{fname}"'
        rh   = {"Content-Disposition": disp, "Accept-Ranges": "bytes",
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
    except Exception as e:
        return jsonify({"status": "error", "message": f"Proxy error: {e}"}), 502

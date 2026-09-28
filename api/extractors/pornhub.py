"""PornHub extractor — helpers + Flask blueprint."""
import re
import json
import sys

from flask import Blueprint, request, jsonify, Response, stream_with_context, redirect
from urllib.parse import urlparse, parse_qs, quote

from api.utils import make_proxy_url, verify_proxy_token, check_raw_key, resolve_hls_uri

bp = Blueprint("pornhub", __name__)

PH_COOKIE_DOMAINS = [".pornhub.com", ".pornhub.net", ".pornhub.org",
                     ".pornhubpremium.com", ".thumbzilla.com"]
PH_AGE_COOKIE = {
    "accessAgeDisclaimerPH": "1", "accessAgeDisclaimerUK": "1",
    "accessPH": "1", "age_verified": "1", "platform": "pc",
}
_PH_HOST_RE = re.compile(
    r"""^(?:(?:[a-z]{2}\.)?(?:www\.)?pornhub(?:premium)?\.(?:com|net|org)
    |(?:www\.)?thumbzilla\.com
    |www\.pornhubvybmsymdol4iibwgwtkpwmeyd6luq2gxajgjzfjvotyt5zhyd\.onion)$""",
    re.VERBOSE | re.IGNORECASE,
)


def _cffi_session():
    from curl_cffi import requests as cffi_req
    session = cffi_req.Session(impersonate="chrome124")
    for domain in PH_COOKIE_DOMAINS:
        for name, value in PH_AGE_COOKIE.items():
            session.cookies.set(name, value, domain=domain)
    return session


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _PH_HOST_RE.match(host):
        raise ValueError(f"Not a supported PornHub URL (host: {host!r}).")
    qs = parse_qs(parsed.query)
    if not (qs.get("viewkey") or "/video" in parsed.path or "/view_video" in parsed.path):
        raise ValueError("URL does not point to a PornHub video page.")
    return url


def _fetch_page(url: str) -> str:
    try:
        session = _cffi_session()
        resp = session.get(url, allow_redirects=True, timeout=20,
                           http_version=3, doh_url="https://1.1.1.1/dns-query")
    except Exception as e:
        err = str(e)
        if "resolve host" in err.lower() or "dns" in err.lower():
            raise ValueError("Could not resolve PornHub hostname.")
        raise ValueError(f"Network error: {e}")
    if resp.status_code != 200:
        raise ValueError(f"PornHub returned HTTP {resp.status_code}.")
    html = resp.text
    if "restrictions_age_disclaimer" in html or "You must be" in html:
        raise ValueError("PornHub returned an age-gate page.")
    return html


def _extract_flashvars(html: str) -> dict:
    # Primary: use JSONDecoder.raw_decode starting at the opening brace.
    # This handles any size JSON without regex backtracking issues on large pages.
    m = re.search(r'var\s+flashvars_\d+\s*=\s*(\{)', html)
    if m:
        try:
            obj, _ = json.JSONDecoder().raw_decode(html, m.start(1))
            if isinstance(obj, dict) and "mediaDefinitions" in obj:
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback: regex-based extraction
    m2 = re.search(r'var\s+flashvars_\d+\s*=\s*(\{.*?\})\s*;', html, re.DOTALL)
    if not m2:
        raise ValueError("Could not find flashvars in the PornHub page. "
                         "Page structure may have changed or video is unavailable.")
    raw = m2.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raw = re.sub(r'//[^\n]*', '', raw)
        raw = re.sub(r',\s*([}\]])', r'\1', raw)
        return json.loads(raw)


def _extract_metadata(html: str) -> dict:
    meta = {}
    og_title = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html)
    title_tag = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
    raw = (og_title.group(1) if og_title else (title_tag.group(1) if title_tag else "")).strip()
    meta["title"] = re.sub(r'\s*[-|]\s*Pornhub\.?com\s*$', '', raw, flags=re.IGNORECASE).strip() or "Unknown Title"
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


def _resolve_get_media(get_media_url: str) -> list:
    try:
        session = _cffi_session()
        r = session.get(
            get_media_url,
            headers={"Referer": "https://www.pornhub.com/", "Origin": "https://www.pornhub.com",
                     "Accept": "application/json, text/plain, */*",
                     "Accept-Language": "en-US,en;q=0.9", "X-Requested-With": "XMLHttpRequest"},
            allow_redirects=True, timeout=15,
            http_version=3, doh_url="https://1.1.1.1/dns-query",
        )
        if r.status_code != 200:
            print(f"[get_media] HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return []
        items = r.json()
        print(f"[get_media] response type={type(items).__name__} len={len(items) if isinstance(items, list) else 'N/A'}", file=sys.stderr)
        if not isinstance(items, list):
            print(f"[get_media] non-list response: {str(items)[:300]}", file=sys.stderr)
            return []
        entries = []
        for item in items:
            video_url = (item.get("videoUrl") or "").strip()
            quality   = str(item.get("quality") or "").strip()
            fmt       = str(item.get("format") or "mp4").lower()
            # Log each item for debugging
            if not video_url or not quality:
                print(f"[get_media] skipped item: quality={quality!r} url={video_url[:40]!r} fmt={fmt!r}", file=sys.stderr)
            if video_url and quality:
                entries.append({"quality": quality, "url": video_url, "format": fmt})
        entries.sort(key=lambda e: -int(e["quality"]) if e["quality"].isdigit() else 0)
        print(f"[get_media] returning {len(entries)} entries", file=sys.stderr)
        return entries
    except Exception as e:
        print(f"[get_media] exception: {e}", file=sys.stderr)
        return []


def _parse_qualities(flashvars: dict) -> tuple:
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


def get_all_qualities(ph_url: str):
    ph_url = _validate_url(ph_url)

    # Try our own scraper first
    try:
        html      = _fetch_page(ph_url)
        flashvars = _extract_flashvars(html)
        meta      = _extract_metadata(html)
        hls_qs, get_media_url = _parse_qualities(flashvars)
        mp4_qs = _resolve_get_media(get_media_url) if get_media_url else []
        if not mp4_qs and get_media_url:
            print(f"[ph_qualities] retrying get_media", file=sys.stderr)
            mp4_qs = _resolve_get_media(get_media_url)
        scraper_ok = True
    except Exception as e:
        print(f"[ph_qualities] scraper failed: {e} — trying yt-dlp", file=sys.stderr)
        scraper_ok = False
        html = ""
        meta = {"title": "", "thumbnail": "", "duration": "", "duration_seconds": 0, "viewkey": ""}
        mp4_qs = []
        hls_qs = []

    # If still no MP4 (datacenter IP blocked by PH get_media OR scraper failed), try yt-dlp
    if not mp4_qs:
        print(f"[ph_qualities] trying yt-dlp fallback", file=sys.stderr)
        try:
            from api.extractors.ytdlp import _ytdlp_extract
            ytdlp_result = _ytdlp_extract(
                ph_url,
                cookies={"accessAgeDisclaimerPH": "1", "age_verified": "1", "platform": "pc"},
            )
            for f in ytdlp_result.get("formats", []):
                if f.get("url"):
                    mp4_qs.append({
                        "quality": f["quality"],
                        "url":     f["url"],
                        "format":  f.get("format", "mp4"),
                    })
            if mp4_qs:
                print(f"[ph_qualities] yt-dlp returned {len(mp4_qs)} formats", file=sys.stderr)
                if not meta.get("title") or meta["title"] == "Unknown Title":
                    meta["title"] = ytdlp_result.get("title", meta.get("title", ""))
                if not meta.get("thumbnail"):
                    meta["thumbnail"] = ytdlp_result.get("thumbnail", "")
                if not meta.get("duration_seconds"):
                    meta["duration_seconds"] = ytdlp_result.get("duration_seconds", 0)
                    meta["duration"] = ytdlp_result.get("duration", "")
                # Extract viewkey from URL if missing
                if not meta.get("viewkey"):
                    import re as _re
                    vk_m = _re.search(r'viewkey[=_]([a-z0-9]+)', ph_url, _re.IGNORECASE)
                    meta["viewkey"] = vk_m.group(1) if vk_m else ""
        except Exception as e:
            print(f"[ph_qualities] yt-dlp fallback failed: {e}", file=sys.stderr)

    hls_only = [q for q in hls_qs if q["format"] == "hls"]
    all_qs = mp4_qs + hls_only if (mp4_qs or hls_only) else hls_qs
    if not all_qs:
        raise ValueError("No downloadable streams found.")
    return meta, all_qs


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/ph/download", methods=["POST"])
def ph_download():
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({"status": "error", "message": "'url' field is required in JSON body"}), 400
    try:
        meta, all_qualities = get_all_qualities(body["url"].strip())
        base_url = request.host_url.rstrip("/")
        vk = meta.get("viewkey", "")
        for q in all_qualities:
            ql = str(q.get("quality", ""))
            q["proxy_url"]    = make_proxy_url(base_url, "/ph/proxy", q["url"], viewkey=vk, quality=ql)
            q["download_url"] = make_proxy_url(base_url, "/ph/proxy", q["url"], extra="&dl=1", viewkey=vk, quality=ql)
        mp4s  = [q for q in all_qualities if q["format"] == "mp4"]
        best  = mp4s[0] if mp4s else all_qualities[0]
        best_ql  = str(best.get("quality", ""))
        best_fmt = best.get("format", "mp4")
        best_proxy    = make_proxy_url(base_url, "/ph/proxy", best["url"], viewkey=vk, quality=best_ql)
        best_download = make_proxy_url(base_url, "/ph/proxy", best["url"], extra="&dl=1", viewkey=vk, quality=best_ql)
        watch_url = f"{base_url}/ph/watch/{vk}" if vk else ""
        return jsonify({
            "status": "success",
            "data": {
                "title": meta["title"], "thumbnail": meta["thumbnail"],
                "duration": meta["duration"], "duration_seconds": meta["duration_seconds"],
                "viewkey": vk, "watch_url": watch_url, "best_format": best_fmt,
                "qualities": all_qualities, "best_url": best["url"],
                "best_proxy_url": best_proxy, "best_download_url": best_download,
                "note": "Open watch_url in browser for the player.",
            },
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal error: {e}"}), 500


@bp.route("/ph/watch/<viewkey>")
def ph_watch(viewkey: str):
    from api.utils import render_watch_page
    try:
        meta, all_qualities = get_all_qualities(
            f"https://www.pornhub.com/view_video.php?viewkey={viewkey}"
        )
    except Exception as e:
        return (f'<body style="background:#0f0f0f;color:#eee;padding:40px">'
                f'<h2 style="color:#f55">Could not load video</h2><p>{e}</p></body>'), 500

    base_url = request.host_url.rstrip("/")
    all_opts = []
    for q in all_qualities:
        ql  = str(q.get("quality", ""))
        fmt = q.get("format", "mp4")
        all_opts.append({
            "quality":      ql,
            "format":       fmt,
            "proxy_url":    make_proxy_url(base_url, "/ph/proxy", q["url"], viewkey=viewkey, quality=ql),
            "download_url": make_proxy_url(base_url, "/ph/proxy", q["url"], extra="&dl=1", viewkey=viewkey, quality=ql),
        })
    mp4_opts = [o for o in all_opts if o["format"] == "mp4"]
    sorted_opts = mp4_opts + [o for o in all_opts if o["format"] == "hls"]
    if not sorted_opts:
        return "<h2>No streams found.</h2>", 404
    return render_watch_page(meta, sorted_opts)


@bp.route("/ph/proxy")
def ph_proxy():
    cdn_url = request.args.get("url", "").strip()
    if not cdn_url:
        return jsonify({"status": "error", "message": "'url' query param required"}), 400
    if not verify_proxy_token(cdn_url) and not check_raw_key():
        return jsonify({"status": "error", "message": "Access denied."}), 403

    viewkey = request.args.get("vk", "")
    is_m3u8 = ".m3u8" in cdn_url
    if is_m3u8 and viewkey:
        accept = request.headers.get("Accept", "")
        if "text/html" in accept and "application/x-mpegurl" not in accept.lower():
            return redirect(f"{request.host_url.rstrip('/')}/ph/watch/{viewkey}", 302)

    download_mode = request.args.get("dl", "0") == "1"
    is_ts   = cdn_url.endswith(".ts") or ".ts?" in cdn_url
    is_hls  = is_m3u8 or is_ts

    try:
        session     = _cffi_session()
        req_headers = {"Referer": "https://www.pornhub.com/", "Origin": "https://www.pornhub.com"}
        if rng := request.headers.get("Range"):
            req_headers["Range"] = rng

        upstream = session.get(
            cdn_url, headers=req_headers, allow_redirects=True,
            timeout=30, http_version=3, doh_url="https://1.1.1.1/dns-query",
            stream=True,
        )

        # Auto-refresh on IP mismatch
        if upstream.status_code in (403, 410, 451):
            quality = request.args.get("q", "")
            if viewkey:
                try:
                    _, fresh_qs = get_all_qualities(f"https://www.pornhub.com/view_video.php?viewkey={viewkey}")
                    target = None
                    if quality:
                        target = next((x for x in fresh_qs if str(x.get("quality")) == quality
                                       and x.get("format") == ("hls" if is_hls else "mp4")), None)
                    if not target:
                        fmt_qs = [x for x in fresh_qs if x.get("format") == ("hls" if is_hls else "mp4")]
                        target = fmt_qs[0] if fmt_qs else fresh_qs[0]
                    cdn_url = target["url"]
                    upstream = session.get(cdn_url, headers=req_headers, allow_redirects=True,
                                           timeout=30, http_version=3, doh_url="https://1.1.1.1/dns-query",
                                           stream=True)
                except Exception:
                    pass

        if upstream.status_code not in (200, 206):
            return jsonify({"status": "error",
                            "message": f"CDN returned HTTP {upstream.status_code}."}), upstream.status_code

        if is_m3u8:
            manifest_text = upstream.text.strip()
            if not manifest_text or len(manifest_text) < 10:
                # Try to auto-refresh empty manifest
                quality = request.args.get("q", "")
                if viewkey:
                    try:
                        _, fresh_qs = get_all_qualities(f"https://www.pornhub.com/view_video.php?viewkey={viewkey}")
                        target = next((x for x in fresh_qs if str(x.get("quality")) == quality and x.get("format") == "hls"), None)
                        if not target:
                            target = next((x for x in fresh_qs if x.get("format") == "hls"), None)
                        if target:
                            cdn_url = target["url"]
                            u2 = session.get(cdn_url, headers=req_headers, allow_redirects=True,
                                             timeout=30, http_version=3, doh_url="https://1.1.1.1/dns-query",
                                             stream=True)
                            if u2.status_code in (200, 206):
                                manifest_text = u2.text.strip()
                    except Exception:
                        pass
            if not manifest_text or len(manifest_text) < 10:
                return jsonify({"status": "error", "message": "CDN returned empty manifest."}), 502

            base_url_host  = request.host_url.rstrip("/")
            parsed_cdn     = urlparse(cdn_url)
            cdn_base_dir   = cdn_url[:cdn_url.rfind("/") + 1]
            rewritten_lines = []
            for line in manifest_text.splitlines():
                stripped = line.strip()
                if not stripped:
                    rewritten_lines.append(line)
                    continue
                if stripped.startswith("#"):
                    def _rw(m, _base=cdn_base_dir, _parsed=parsed_cdn, _host=base_url_host):
                        abs_uri = resolve_hls_uri(m.group(1), _base, _parsed)
                        return f'URI="{make_proxy_url(_host, "/ph/proxy", abs_uri)}"'
                    rewritten_lines.append(re.sub(r'URI="([^"]+)"', _rw, line))
                else:
                    abs_uri = resolve_hls_uri(stripped, cdn_base_dir, parsed_cdn)
                    rewritten_lines.append(make_proxy_url(base_url_host, "/ph/proxy", abs_uri))
            return Response("\n".join(rewritten_lines) + "\n", status=200,
                            content_type="application/vnd.apple.mpegurl; charset=utf-8",
                            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache",
                                     "Content-Disposition": 'inline; filename="playlist.m3u8"'})

        path_part = urlparse(cdn_url).path
        fname     = path_part.split("/")[-1].split("?")[0] or "video"
        if not any(fname.endswith(ext) for ext in (".mp4", ".m3u8", ".ts", ".webm")):
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

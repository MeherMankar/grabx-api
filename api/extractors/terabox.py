"""Terabox extractor — helpers + Flask blueprint."""
import os
import re
import json
import random

import requests as req_lib
from flask import Blueprint, request, jsonify, Response, stream_with_context
from urllib.parse import urlparse, parse_qs

from api.utils import (
    MOBILE_UA, DESKTOP_UA,
    make_proxy_url, verify_proxy_token, check_raw_key,
)

bp = Blueprint("terabox", __name__)

# ---------------------------------------------------------------------------
# Domain lists
# ---------------------------------------------------------------------------

TERABOX_DOMAINS = [
    ".terabox.com", ".dm.terabox.com", ".terabox.app",
    ".1024terabox.com", ".1024tera.com", ".1024tera.co",
    ".teraboxapp.com", ".teraboxapp.net",
    ".teraboxlink.com", ".teraboxshare.com", ".terasharefile.com",
    ".terafileshare.com", ".terasharelink.com",
    ".nephobox.com", ".4funbox.co", ".4funbox.com",
    ".mirrobox.com", ".momerybox.com", ".tibibox.com",
    ".freeterabox.com", ".terabox1.com", ".terabox2.com",
    ".dubox.com", ".dubox.co", ".ww.mirrobox.com",
]

TERABOX_HOSTNAMES = [
    "www.terabox.com", "www.1024terabox.com", "www.teraboxapp.com",
    "www.nephobox.com", "www.4funbox.co", "www.4funbox.com",
    "www.mirrobox.com", "ww.mirrobox.com", "www.momerybox.com",
    "www.tibibox.com", "www.freeterabox.com", "www.teraboxlink.com",
    "www.terafileshare.com", "www.teraboxshare.com", "www.terasharefile.com",
    "www.terasharelink.com", "www.terabox1.com", "www.terabox2.com",
    "www.1024tera.com", "www.dubox.com",
]

_TERABOX_URL_RE = re.compile(
    r"""(?:^|\.)(?:(?:(?:www|ww|m|dm)\.)?
    (?:terabox(?:app|link|share|1|2)?|1024tera(?:box)?|nephobox|4funbox|
       mirrobox|momerybox|tibibox|freeterabox|terasharefile|terasharelink|
       terafileshare|dubox)\.(?:com|co|app|net|org|io))""",
    re.VERBOSE | re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ndus_list() -> list:
    cookie_str = os.environ.get("TERABOX_COOKIE", "").strip()
    if not cookie_str:
        raise ValueError("TERABOX_COOKIE environment variable is not set.")
    accounts = []
    for entry in cookie_str.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.lower().startswith("ndus="):
            accounts.append(entry[5:].strip())
        else:
            for part in entry.split(";"):
                part = part.strip()
                if part.lower().startswith("ndus="):
                    accounts.append(part[5:].strip())
                    break
            else:
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
    host = (parsed.hostname or "").lower()
    if not _TERABOX_URL_RE.search(host):
        raise ValueError(f"Not a Terabox share link (host: {host!r}).")
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
        raise ValueError(f"Invalid surl: '{surl}'")
    return surl


def fetch_wap_page(session, surl: str, share_url: str = "") -> tuple:
    candidates = []
    if share_url:
        host = urlparse(share_url).hostname or ""
        if host:
            candidates += [f"http://{host}/wap/share/filelist?surl={surl}",
                           f"https://{host}/wap/share/filelist?surl={surl}"]
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
        except req_lib.exceptions.Timeout:
            last_error = Exception(f"Timeout: {url}")
    raise ValueError(f"Could not load Terabox WAP page for surl={surl}. Last error: {last_error}")


def extract_file_info(html: str) -> list:
    m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*(?:;|</script>)', html, re.DOTALL)
    if not m:
        raise ValueError("window.__INITIAL_STATE__ not found in WAP page HTML.")
    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        fl_m = re.search(r'"fileList"\s*:\s*(\[.+?\])\s*,\s*"', html, re.DOTALL)
        if not fl_m:
            raise ValueError("Could not parse file list from WAP page.")
        state = {"share": {"fileList": json.loads(fl_m.group(1))}}
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


def _extract_share_meta(html: str) -> dict:
    m = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*(?:;|</script>)', html, re.DOTALL)
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


def _fetch_folder_file_list(session, surl: str, dir_path: str,
                             share_id: str, uk: str, sign: str, timestamp: str) -> list:
    params = {
        "app_id": "250528", "shorturl": surl, "root": "0",
        "dir": dir_path, "shareid": share_id, "uk": uk,
        "sign": sign, "timestamp": timestamp,
        "num": "100", "page": "1", "order": "name", "desc": "0",
    }
    try:
        resp = session.get("https://www.terabox.com/share/list",
                           params=params,
                           headers={"User-Agent": DESKTOP_UA, "Referer": "https://www.terabox.com/"},
                           timeout=15)
        return resp.json().get("list", [])
    except Exception:
        return []


def _collect_files_recursive(session, items: list, surl: str, meta: dict,
                              depth: int = 0, max_depth: int = 8) -> list:
    results = []
    if depth > max_depth:
        return results
    for item in items:
        if str(item.get("isdir", "0")) == "1":
            dir_path = item.get("path", "")
            if not dir_path or not all(meta.get(k) for k in ("shareid", "uk")):
                continue
            children = _fetch_folder_file_list(
                session, surl, dir_path,
                meta["shareid"], meta["uk"],
                meta.get("sign", ""), meta.get("timestamp", ""),
            )
            results.extend(_collect_files_recursive(session, children, surl, meta, depth + 1, max_depth))
        else:
            results.append(item)
    return results


def _extract_video_quality(item: dict) -> dict:
    quality: dict = {}
    video_info = item.get("video_info") or {}
    if video_info:
        w = video_info.get("width") or video_info.get("video_width")
        h = video_info.get("height") or video_info.get("video_height")
        if w and h:
            quality["width"]  = int(w)
            quality["height"] = int(h)
            quality["resolution"] = f"{w}x{h}"
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
        fname = item.get("server_filename", "")
        for pat, label in ((r'4k|2160p', "4K"), (r'2k|1440p', "2K"),
                           (r'1080p', "1080p"), (r'720p', "720p"),
                           (r'480p', "480p"), (r'360p', "360p")):
            if re.search(pat, fname, re.IGNORECASE):
                quality["label"] = label
                break
    return quality if quality else {}

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/download", methods=["POST"])
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
        share_meta = _extract_share_meta(html)
        share_meta["surl"] = surl
        flat_items = _collect_files_recursive(session, file_list, surl, share_meta)

        files    = []
        base_url = request.host_url.rstrip("/")
        for item in flat_items:
            dlink      = item.get("dlink", "")
            thumbs     = item.get("thumbs") or {}
            thumbnail  = (thumbs.get("url3") or thumbs.get("url2") or
                          thumbs.get("url1") or thumbs.get("icon") or "")
            size_bytes = int(item.get("size", 0))
            proxy_url  = make_proxy_url(base_url, "/proxy", dlink) if dlink else ""
            folder_path = item.get("path", "")
            parent_dir  = "/".join(folder_path.split("/")[:-1]) if folder_path else ""
            quality = _extract_video_quality(item)
            entry = {
                "filename":   item.get("server_filename", ""),
                "folder":     parent_dir,
                "size_bytes": size_bytes,
                "size":       _human_size(size_bytes),
                "thumbnail":  thumbnail,
                "dlink":      dlink,
                "proxy_url":  proxy_url,
                "fs_id":      str(item.get("fs_id", "")),
            }
            if quality:
                entry["video_quality"] = quality
            files.append(entry)

        if not files:
            return jsonify({"status": "error", "message": "Share contains no downloadable files."}), 404

        has_dlink = any(f["dlink"] for f in files)
        if len(files) == 1:
            title = files[0]["filename"]
        else:
            folders = {f["folder"] for f in files if f["folder"]}
            title = f"{len(files)} files" + (f" across {len(folders)} folders" if folders else "")

        return jsonify({
            "status": "success",
            "data": {
                "title": title, "total_files": len(files),
                "files": files, "download_available": has_dlink,
                "note": ("Use 'proxy_url' to stream through this server."
                         if has_dlink else "No direct download link available."),
            },
        })
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except req_lib.exceptions.RequestException as e:
        return jsonify({"status": "error", "message": f"Network error: {e}"}), 502
    except Exception as e:
        return jsonify({"status": "error", "message": f"Internal error: {e}"}), 500


@bp.route("/proxy")
def proxy():
    dlink = request.args.get("url", "").strip()
    if not dlink:
        return jsonify({"status": "error", "message": "'url' query param required"}), 400
    if not verify_proxy_token(dlink) and not check_raw_key():
        return jsonify({"status": "error", "message": "Access denied."}), 403
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

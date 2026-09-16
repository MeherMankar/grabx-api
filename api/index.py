"""
Terabox Downloader API
======================
Bypasses Terabox's verify_v2 CAPTCHA gate using the WAP (mobile) page trick
from FZBypassBot (github.com/rjriajul/FZBypassBot).

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
"""

from flask import Flask, request, jsonify, Response, stream_with_context
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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

# All known Terabox hostnames to try for WAP page loading
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

# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def _parse_ndus_list() -> list:
    """
    Parse TERABOX_COOKIE env var into a list of ndus values.

    Accepts two formats:
      Single account:   ndus=VALUE
      Multi-account:    ndus=VALUE1,ndus=VALUE2,ndus=VALUE3
                        or just: VALUE1,VALUE2,VALUE3
    Returns a list of raw ndus strings.
    """
    cookie_str = os.environ.get("TERABOX_COOKIE", "").strip()
    if not cookie_str:
        raise ValueError(
            "TERABOX_COOKIE environment variable is not set. "
            "For a single account: ndus=YOUR_VALUE\n"
            "For multiple accounts: ndus=VALUE1,ndus=VALUE2,ndus=VALUE3"
        )

    accounts = []
    # Split on comma — handles both "ndus=A,ndus=B" and "A,B"
    for entry in cookie_str.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.lower().startswith("ndus="):
            accounts.append(entry[5:].strip())
        else:
            # Might be a full cookie string "ndus=X; csrfToken=Y; ..."
            found = False
            for part in entry.split(";"):
                part = part.strip()
                if part.lower().startswith("ndus="):
                    accounts.append(part[5:].strip())
                    found = True
                    break
            if not found:
                # Treat as raw ndus value
                accounts.append(entry)

    if not accounts:
        raise ValueError("No valid ndus values found in TERABOX_COOKIE.")

    return accounts


def get_random_ndus() -> str:
    """Pick a random ndus value from the configured account pool."""
    accounts = _parse_ndus_list()
    return random.choice(accounts)


def get_account_count() -> int:
    """Return how many accounts are configured."""
    try:
        return len(_parse_ndus_list())
    except ValueError:
        return 0


def build_session(ndus: str) -> req_lib.Session:
    """Build a requests.Session with ndus cookie set on all Terabox domains."""
    session = req_lib.Session()
    for domain in TERABOX_DOMAINS:
        session.cookies.set("ndus", ndus, domain=domain)
    return session


# ---------------------------------------------------------------------------
# surl parsing
# ---------------------------------------------------------------------------

def parse_surl(share_url: str) -> str:
    """
    Extract the surl key from any Terabox share URL:
      https://terabox.com/s/1ABC...          -> strip leading 1 if > 22 chars
      https://terasharefile.com/s/1-ABC...   -> same
      https://terabox.com/sharing/link?surl=ABC...
    """
    parsed = urlparse(share_url)

    if "/s/" in parsed.path:
        surl = parsed.path.split("/s/")[-1].strip("/")
    else:
        qs = parse_qs(parsed.query)
        surl = qs.get("surl", [""])[0]

    if not surl:
        raise ValueError(f"Cannot extract surl from URL: {share_url}")

    # Path-form prepends '1'; strip it if result is still > 22 chars after strip
    if len(surl) > 22 and surl.startswith("1"):
        surl = surl[1:]

    if len(surl) < 8:
        raise ValueError(f"Invalid surl extracted: '{surl}' from URL: {share_url}")

    return surl


# ---------------------------------------------------------------------------
# WAP page extraction  (the core bypass)
# ---------------------------------------------------------------------------

def fetch_wap_page(session: req_lib.Session, surl: str, share_url: str = "") -> tuple:
    """
    Load the Terabox WAP share page and return (html, final_surl).

    Tries the original share domain first, then known fallbacks.
    The WAP page embeds window.__INITIAL_STATE__ with the file list + dlinks.
    """
    # Build candidate list — try the original share domain first
    candidates = []
    if share_url:
        host = urlparse(share_url).hostname or ""
        if host:
            candidates.append(f"http://{host}/wap/share/filelist?surl={surl}")
            candidates.append(f"https://{host}/wap/share/filelist?surl={surl}")

    # Then try known working hosts
    for host in TERABOX_HOSTNAMES:
        url = f"https://{host}/wap/share/filelist?surl={surl}"
        if url not in candidates:
            candidates.append(url)
    # http fallback for terabox.com (avoids TLS reset on some networks)
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


def extract_file_info(html: str) -> dict:
    """
    Parse window.__INITIAL_STATE__ from the WAP page HTML.
    Returns the first file's info dict with dlink, filename, size, thumbs.
    """
    m = re.search(
        r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*(?:;|</script>)',
        html, re.DOTALL,
    )
    if not m:
        raise ValueError("window.__INITIAL_STATE__ not found in WAP page HTML.")

    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        # The JSON may be truncated; try to find just the fileList array
        fl_m = re.search(r'"fileList"\s*:\s*(\[.+?\])\s*,\s*"', html, re.DOTALL)
        if not fl_m:
            raise ValueError("Could not parse file list from WAP page.")
        file_list = json.loads(fl_m.group(1))
        state = {"share": {"fileList": file_list}}

    file_list = state.get("share", {}).get("fileList", [])
    if not file_list:
        raise ValueError("No files found in WAP page state.")

    return file_list


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} PB"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return jsonify({
        "status": "active",
        "message": "Terabox Downloader API",
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
            "/docs": {
                "method": "GET",
                "description": "API documentation",
            },
        },
    })


@app.route("/download", methods=["POST"])
def download():
    body = request.get_json(silent=True)
    if not body or "url" not in body:
        return jsonify({
            "status": "error",
            "message": "'url' field is required in JSON body",
        }), 400

    share_url = body["url"].strip()

    try:
        ndus = get_random_ndus()
        session = build_session(ndus)

        # 1. Parse surl
        surl = parse_surl(share_url)

        # 2. Load WAP page — this is the bypass (no CAPTCHA gate)
        html, final_surl = fetch_wap_page(session, surl, share_url)

        # 3. Extract file list from embedded __INITIAL_STATE__
        file_list = extract_file_info(html)

        # 4. Build response — filter out folders
        files = []
        base_url = request.host_url.rstrip("/")

        for item in file_list:
            if str(item.get("isdir", "0")) == "1":
                continue

            dlink = item.get("dlink", "")
            thumbs = item.get("thumbs") or {}
            thumbnail = (
                thumbs.get("url3") or thumbs.get("url2")
                or thumbs.get("url1") or thumbs.get("icon") or ""
            )
            size_bytes = int(item.get("size", 0))

            proxy_url = f"{base_url}/proxy?url={quote(dlink)}" if dlink else ""

            files.append({
                "filename": item.get("server_filename", ""),
                "size_bytes": size_bytes,
                "size": _human_size(size_bytes),
                "thumbnail": thumbnail,
                "dlink": dlink,
                "proxy_url": proxy_url,
                "fs_id": str(item.get("fs_id", "")),
            })

        if not files:
            return jsonify({
                "status": "error",
                "message": "Share contains only folders or no files.",
            }), 404

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
                    if has_dlink else
                    "No direct download link available."
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
    """
    Proxy-stream a Terabox dlink through this server.
    Attaches ndus cookie + correct Referer so the CDN 302 redirect works.
    GET /proxy?url=<encoded_dlink>
    """
    dlink = request.args.get("url", "").strip()
    if not dlink:
        return jsonify({"status": "error", "message": "'url' query param required"}), 400

    try:
        ndus = get_random_ndus()
        session = build_session(ndus)

        upstream = session.get(
            dlink,
            headers={
                "User-Agent": DESKTOP_UA,
                "Referer": "https://www.terabox.com/",
                "Accept": "*/*",
            },
            stream=True,
            allow_redirects=True,
            timeout=30,
        )

        cd = upstream.headers.get("Content-Disposition", "")
        fname_m = re.search(r'filename[*]?=["\']?([^"\';\n]+)', cd)
        fname = fname_m.group(1).strip() if fname_m else "download"

        def generate():
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        resp_headers = {
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Accept-Ranges": "bytes",
        }
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
# Entry point
# ---------------------------------------------------------------------------

app.debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

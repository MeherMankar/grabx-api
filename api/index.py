"""
GrabX API — Flask application entry point
==========================================
All extraction logic lives in api/extractors/.
This file is intentionally thin: it wires up auth, registers blueprints,
and provides the home/health/docs/debug routes.
"""

import os
import sys

# Ensure the project root is on sys.path so `api.*` imports work
# regardless of how the app is invoked (python api/index.py vs gunicorn api.index:app)
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import Flask, request, jsonify

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from api.utils import API_KEY, CF_WORKER_URL
from api.extractors.terabox import bp as terabox_bp, get_account_count
from api.extractors.pornhub import bp as pornhub_bp
from api.extractors.javtiful import bp as javtiful_bp
from api.extractors.xvideos  import bp as xvideos_bp
from api.extractors.xhamster import bp as xhamster_bp
from api.extractors.ytdlp    import bp as ytdlp_bp

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Register blueprints
# ---------------------------------------------------------------------------
app.register_blueprint(terabox_bp)
app.register_blueprint(pornhub_bp)
app.register_blueprint(javtiful_bp)
app.register_blueprint(xvideos_bp)
app.register_blueprint(xhamster_bp)
app.register_blueprint(ytdlp_bp)

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

_PUBLIC_ROUTES   = {"/", "/docs", "/health", "/debug/headers"}
_PUBLIC_PREFIXES = (
    "/ph/watch/", "/ph/proxy", "/proxy", "/adult/proxy",
    "/xv/watch",  "/xnxx/watch", "/xh/watch",
    "/jav/proxy", "/jav/watch",
    "/yt/watch",
)


@app.before_request
def _check_api_key():
    if not API_KEY:
        return
    if request.path in _PUBLIC_ROUTES:
        return
    if request.path.startswith(_PUBLIC_PREFIXES):
        return
    auth   = request.headers.get("Authorization", "")
    bearer = auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else ""
    key = (
        request.headers.get("X-API-Key")
        or request.headers.get("X-Api-Key")
        or request.headers.get("apikey")
        or request.headers.get("Api-Key")
        or request.headers.get("GRABX-API-KEY")
        or request.headers.get("X-GRABX-API-KEY")
        or request.headers.get("GRABX_API_KEY")
        or request.args.get("api_key")
        or request.args.get("apikey")
        or request.args.get("grabx_api_key")
        or bearer
    )
    if not key:
        return jsonify({
            "status": "error",
            "message": (
                "Missing API key. Accepted: X-API-Key header, "
                "Authorization: Bearer <key>, or ?api_key= query param."
            ),
        }), 401
    if key != API_KEY:
        return jsonify({"status": "error", "message": "Invalid API key."}), 403


# ---------------------------------------------------------------------------
# Core routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return jsonify({
        "status":       "active",
        "message":      "GrabX API",
        "creator":      "Maintained by MeherMankar (t.me/MeherPatil) | Terabox base by genxnano (t.me/genxnano)",
        "github":       "https://github.com/MeherMankar/grabx-api",
        "accounts_configured": get_account_count(),
        "auth":         "enabled (X-API-Key required)" if API_KEY else "disabled (open access)",
        "proxy_backend": CF_WORKER_URL if CF_WORKER_URL else "render (this server)",
        "endpoints": {
            "/download":     {"method": "POST", "description": "Terabox share → direct download links"},
            "/proxy":        {"method": "GET",  "description": "Proxy-stream a Terabox dlink"},
            "/ph/download":  {"method": "POST", "description": "PornHub video → stream/download links"},
            "/ph/watch/<k>": {"method": "GET",  "description": "PornHub browser player (public)"},
            "/ph/proxy":     {"method": "GET",  "description": "PornHub CDN proxy (token-auth)"},
            "/jav/download": {"method": "POST", "description": "JAVtiful video → stream/download links"},
            "/jav/watch":    {"method": "GET",  "description": "JAVtiful browser player (public)"},
            "/jav/proxy":    {"method": "GET",  "description": "JAVtiful CDN proxy (token-auth)"},
            "/xv/download":  {"method": "POST", "description": "Xvideos video → stream/download links"},
            "/xv/watch":     {"method": "GET",  "description": "Xvideos browser player (public)"},
            "/xnxx/download":{"method": "POST", "description": "XNXX video → stream/download links"},
            "/xnxx/watch":   {"method": "GET",  "description": "XNXX browser player (public)"},
            "/xh/download":  {"method": "POST", "description": "XHamster video → stream/download links"},
            "/xh/watch":     {"method": "GET",  "description": "XHamster browser player (public)"},
            "/adult/proxy":  {"method": "GET",  "description": "Xvideos/XNXX/XHamster CDN proxy (token-auth)"},
            "/health":       {"method": "GET",  "description": "Health check (public)"},
            "/docs":         {"method": "GET",  "description": "API documentation (public)"},
            "/yt/download":  {"method": "POST", "description": "yt-dlp extractor — any supported URL (PH, Xvideos, Reddit, Twitter/X, Twitch, etc.)"},
            "/yt/watch":     {"method": "GET",  "description": "Browser player for any yt-dlp supported URL (public)"},
        },
    })


@app.route("/health")
def health():
    import platform
    return jsonify({
        "status":   "ok",
        "python":   sys.version,
        "platform": platform.platform(),
        "accounts_configured": get_account_count(),
        "auth":     "enabled" if API_KEY else "disabled",
        "proxy_backend": CF_WORKER_URL if CF_WORKER_URL else "render (this server)",
    })


@app.route("/debug/headers")
def debug_headers():
    if not (os.environ.get("FLASK_DEBUG", "").lower() == "true"
            or os.environ.get("DEBUG_HEADERS", "").lower() == "true"):
        return jsonify({"status": "error", "message": "Set DEBUG_HEADERS=true to enable."}), 403
    headers = {k: v for k, v in request.headers}
    for h in list(headers):
        if "key" in h.lower() or "auth" in h.lower():
            v = headers[h]
            headers[h] = v[:4] + "****" + v[-2:] if len(v) > 6 else "****"
    return jsonify({"headers": headers, "args": dict(request.args)})


@app.route("/docs")
def docs():
    try:
        docs_path = os.path.join(os.path.dirname(__file__), "..", "docs.md")
        with open(docs_path, "r") as f:
            return f.read(), 200, {"Content-Type": "text/markdown; charset=utf-8"}
    except FileNotFoundError:
        return "Documentation not found.", 404


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

app.debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

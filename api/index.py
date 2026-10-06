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
from hashlib import sha256
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from api.utils import (
    _get_api_key, CF_WORKER_URL, REDIS_URL, enforce_rate_limit, get_abuse_events,
    get_redis, log_abuse_event, validate_proxy_target,
)
from api.extractors.terabox import bp as terabox_bp, get_account_count
from api.extractors.pornhub import bp as pornhub_bp
from api.extractors.javtiful import bp as javtiful_bp
from api.extractors.xvideos  import bp as xvideos_bp
from api.extractors.xhamster import bp as xhamster_bp
from api.extractors.ytdlp    import bp as ytdlp_bp
from api.extractors.adult_sites import bp as adult_sites_bp

app = Flask(__name__)
if os.environ.get("APP_ENV", "").lower() == "production":
    if not _get_api_key():
        raise RuntimeError("_get_api_key() is required when APP_ENV=production.")
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL is required when APP_ENV=production.")
    get_redis().ping()

# ---------------------------------------------------------------------------
# Register blueprints
# ---------------------------------------------------------------------------
app.register_blueprint(terabox_bp)
app.register_blueprint(pornhub_bp)
app.register_blueprint(javtiful_bp)
app.register_blueprint(xvideos_bp)
app.register_blueprint(xhamster_bp)
app.register_blueprint(ytdlp_bp)
app.register_blueprint(adult_sites_bp)

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

_PUBLIC_ROUTES   = {"/", "/docs", "/health", "/debug/headers"}
_PUBLIC_PREFIXES = (
    "/ph/watch/", "/xv/watch", "/xnxx/watch", "/xh/watch",
    "/jav/watch", "/yt/watch", "/redtube/watch", "/youporn/watch",
    "/eporner/watch", "/spankbang/watch", "/porntrex/watch",
    "/ph/proxy", "/jav/proxy", "/adult/proxy", "/proxy",
)
_RATE_LIMIT = int(os.environ.get("API_RATE_LIMIT_PER_MINUTE", "60"))
_PROXY_RATE_LIMIT = int(os.environ.get("PROXY_RATE_LIMIT_PER_MINUTE", "1200"))


@app.before_request
def _check_api_key():
    is_public = (
        request.path in _PUBLIC_ROUTES
        or request.path.startswith(_PUBLIC_PREFIXES)
    )
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

    if request.path in {"/", "/docs", "/health"}:
        return
    identity = key if (not is_public and key and key == _get_api_key()) else (request.remote_addr or "unknown")
    limit = _PROXY_RATE_LIMIT if request.path in {
        "/proxy", "/ph/proxy", "/adult/proxy", "/jav/proxy",
    } else _RATE_LIMIT
    rate_identity = f"{'proxy' if limit == _PROXY_RATE_LIMIT else 'api'}:{sha256(identity.encode()).hexdigest()[:24]}"
    try:
        allowed, retry_after = enforce_rate_limit(rate_identity, limit)
    except Exception:
        app.logger.warning("Rate limiter unavailable — allowing request")
        allowed, retry_after = True, 0
    if not allowed:
        try:
            log_abuse_event({
                "identity": sha256(identity.encode()).hexdigest()[:24],
                "path": request.path,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            app.logger.warning("Could not persist rate-limit violation")
        return jsonify({"status": "error", "message": "Rate limit exceeded."}), 429, {
            "Retry-After": str(retry_after),
        }

    if not is_public and not _get_api_key():
        return jsonify({
            "status": "error",
            "message": "_get_api_key() is required for protected routes. Set _get_api_key() in the environment.",
        }), 401
    if not is_public and not key:
        return jsonify({
            "status": "error",
            "message": (
                "Missing API key. Accepted: X-API-Key header, "
                "Authorization: Bearer <key>, or ?api_key= query param."
            ),
        }), 401
    if not is_public and key != _get_api_key():
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
        "auth":         "enabled (X-API-Key required)" if _get_api_key() else "protected routes unavailable (_get_api_key() unset)",
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
            "/jobs":         {"method": "POST", "description": "Queue an asynchronous yt-dlp extraction"},
            "/jobs/<id>":    {"method": "GET", "description": "Poll extraction job state/result"},
            "/admin/cache/clear": {"method": "POST", "description": "Clear extraction cache"},
            "/admin/abuse":  {"method": "GET", "description": "Inspect rate-limit violation log"},
            "/redtube/download": {"method": "POST", "description": "RedTube video extraction"},
            "/youporn/download": {"method": "POST", "description": "YouPorn video extraction"},
            "/eporner/download": {"method": "POST", "description": "Eporner video extraction"},
            "/spankbang/download": {"method": "POST", "description": "SpankBang video extraction"},
            "/porntrex/download": {"method": "POST", "description": "PornTrex video extraction (if supported by yt-dlp)"},
        },
    })


@app.route("/health")
def health():
    import platform
    from api.utils import cache_stats
    return jsonify({
        "status":   "ok",
        "python":   sys.version,
        "platform": platform.platform(),
        "accounts_configured": get_account_count(),
        "auth":     "enabled" if _get_api_key() else "protected routes unavailable (_get_api_key() unset)",
        "proxy_backend": CF_WORKER_URL if CF_WORKER_URL else "render (this server)",
        "cache":    cache_stats(),
        "redis":    "connected" if REDIS_URL else "local-development fallback",
    })


@app.route("/jobs", methods=["POST"])
def create_job():
    from api.jobs import extract_job, extraction_queue, queue_workers

    body = request.get_json(silent=True) or {}
    url = str(body.get("url") or "").strip()
    if not url:
        return jsonify({"status": "error", "message": "'url' is required."}), 400
    if not validate_proxy_target(url):
        return jsonify({"status": "error", "message": "URL must be a public HTTP(S) address."}), 400
    try:
        queue = extraction_queue()
        if not queue_workers(queue):
            return jsonify({
                "status": "error",
                "message": "No worker is listening on the 'grabx' queue. Start the grabx-worker service, then retry.",
            }), 503
        job = queue.enqueue(
            extract_job, url, request.host_url.rstrip("/").replace("http://", "https://"),
            job_timeout=180, result_ttl=3600, failure_ttl=86400,
        )
    except Exception:
        app.logger.exception("Could not enqueue extraction job")
        return jsonify({"status": "error", "message": "Job queue is unavailable."}), 503
    return jsonify({
        "status": "queued",
        "job_id": job.id,
        "status_url": f"{request.host_url.rstrip('/')}/jobs/{job.id}",
    }), 202


@app.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    from api.jobs import get_job_redis
    from rq.job import Job

    try:
        redis = get_job_redis()
        if redis is None:
            return jsonify({"status": "error", "message": "REDIS_URL is required for jobs."}), 503
        job = Job.fetch(job_id, connection=redis)
        state = job.get_status(refresh=True)
        response = {"status": state, "job_id": job.id}
        if state == "finished":
            response["result"] = job.result
        elif state == "failed":
            response["message"] = "Extraction job failed."
        elif state == "queued":
            from api.jobs import extraction_queue, queue_workers
            queue = extraction_queue()
            response["queue"] = {
                "queued_jobs": queue.count,
                "workers": [
                    {"name": worker.name, "state": worker.get_state()}
                    for worker in queue_workers(queue)
                ],
            }
        return jsonify(response)
    except Exception as exc:
        from rq.exceptions import NoSuchJobError
        if isinstance(exc, NoSuchJobError):
            return jsonify({"status": "error", "message": "Job not found or expired."}), 404
        app.logger.exception("Could not fetch extraction job")
        return jsonify({"status": "error", "message": "Job status is unavailable."}), 503


@app.route("/admin/cache/clear", methods=["POST"])
def clear_cache():
    from api.utils import cache_clear
    try:
        return jsonify({"status": "success", "removed": cache_clear()})
    except Exception:
        app.logger.exception("Could not clear extraction cache")
        return jsonify({"status": "error", "message": "Cache backend is unavailable."}), 503


@app.route("/admin/abuse")
def abuse_events():
    try:
        return jsonify({"status": "success", "events": get_abuse_events()})
    except Exception:
        app.logger.exception("Could not read abuse event log")
        return jsonify({"status": "error", "message": "Abuse log backend is unavailable."}), 503


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

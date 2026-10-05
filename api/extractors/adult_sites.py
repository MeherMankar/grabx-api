"""Registry-backed yt-dlp adapters for additional adult video sites."""

from urllib.parse import quote

from flask import Blueprint, jsonify, request

from api.extractors.registry import SITE_ADAPTERS
from api.utils import render_watch_page

bp = Blueprint("adult_sites", __name__)


def _extract(site: str, url: str, base_url: str):
    if not url:
        raise ValueError("'url' is required.")
    adapter = SITE_ADAPTERS[site]
    return adapter.extract(url, base_url)


def _download(site: str):
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "JSON body must be an object."}), 400
    try:
        url, meta, qualities = _extract(
            site, str(body.get("url") or "").strip(), request.host_url.rstrip("/")
        )
        best = qualities[0]
        return jsonify({
            "status": "success",
            "data": {
                **meta,
                "watch_url": f"{request.host_url.rstrip('/')}/{site}/watch?url={quote(url, safe='')}",
                "qualities": qualities,
                "best_proxy_url": best["proxy_url"],
                "best_download_url": best["download_url"],
            },
        })
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Extraction failed: {exc}"}), 502


def _watch(site: str):
    url = request.args.get("url", "").strip()
    try:
        _, meta, qualities = _extract(site, url, request.host_url.rstrip("/"))
        return render_watch_page(meta, qualities)
    except ValueError as exc:
        return f"<h2>{exc}</h2>", 400
    except Exception as exc:
        return f"<h2>Extraction failed: {exc}</h2>", 502


for _site in SITE_ADAPTERS:
    bp.add_url_rule(
        f"/{_site}/download", endpoint=f"{_site}_download",
        view_func=lambda site=_site: _download(site), methods=["POST"],
    )
    bp.add_url_rule(
        f"/{_site}/watch", endpoint=f"{_site}_watch",
        view_func=lambda site=_site: _watch(site), methods=["GET"],
    )

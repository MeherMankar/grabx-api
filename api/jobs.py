"""Redis-backed asynchronous yt-dlp extraction jobs."""

from rq import Queue

from api.utils import get_redis


def extract_job(url: str, base_url: str) -> dict:
    """RQ task entry point; imports the extractor inside the worker process."""
    from api.extractors.ytdlp import ytdlp_get_qualities

    meta, qualities = ytdlp_get_qualities(url, base_url, "/adult/proxy")
    if not qualities:
        raise ValueError("No playable streams found.")
    best = qualities[0]
    return {
        "status": "success",
        "data": {
            **meta,
            "qualities": qualities,
            "best_proxy_url": best["proxy_url"],
            "best_download_url": best["download_url"],
        },
    }


def extraction_queue() -> Queue:
    """Create the configured extraction queue; Redis is mandatory for jobs."""
    redis = get_redis()
    if redis is None:
        raise RuntimeError("REDIS_URL is required for asynchronous extraction jobs.")
    return Queue("grabx", connection=redis, default_timeout=180)

"""Redis-backed asynchronous yt-dlp extraction jobs."""

import threading

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from redis import Redis
from rq import Queue, Worker

from api import utils

_job_redis = None
_job_redis_lock = threading.Lock()


def get_job_redis():
    """Return a binary-safe Redis connection for RQ job data."""
    global _job_redis
    if not utils.REDIS_URL:
        return None
    with _job_redis_lock:
        if _job_redis is None:
            _job_redis = Redis.from_url(
                utils.REDIS_URL,
                decode_responses=False,
                socket_connect_timeout=3,
            )
        return _job_redis


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
    redis = get_job_redis()
    if redis is None:
        raise RuntimeError("REDIS_URL is required for asynchronous extraction jobs.")
    return Queue("grabx", connection=redis, default_timeout=180)


def queue_workers(queue: Queue) -> list:
    """Return workers currently registered for the extraction queue."""
    return Worker.all(queue=queue)

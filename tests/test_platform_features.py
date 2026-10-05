import os
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

os.environ["APP_ENV"] = "development"
os.environ["API_KEY"] = ""
os.environ["REDIS_URL"] = ""

from api import index, utils


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, _ttl, value):
        self.values[key] = value

    def scan_iter(self, match, count=100):
        prefix = match.removesuffix("*")
        return iter(key for key in self.values if key.startswith(prefix))

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
        return len(keys)

    def pipeline(self):
        self.pending = []
        self.in_pipeline = True
        return self

    def incr(self, key):
        self.pending.append(("incr", key))

    def execute(self):
        result = []
        for operation, key in self.pending:
            if operation == "incr":
                self.values[key] = int(self.values.get(key, "0")) + 1
                result.append(self.values[key])
            else:
                result.append(True)
        self.in_pipeline = False
        return result

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start:end + 1]

    def lrange(self, key, start, end):
        return self.lists.get(key, [])[start:end + 1]

    def expire_key(self, _key, _ttl):
        return True

    def expire(self, key, ttl):
        if getattr(self, "in_pipeline", False):
            self.pending.append(("expire", key))
        else:
            return self.expire_key(key, ttl)


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.old_redis_url = utils.REDIS_URL
        self.old_redis = utils._redis
        self.old_api_key = utils.API_KEY
        self.old_index_key = index.API_KEY
        self.old_rate_limit = index._RATE_LIMIT
        utils.REDIS_URL = ""
        utils._redis = None
        utils._cache_store.clear()
        utils._memory_rate_limits.clear()
        utils._abuse_events.clear()
        self.redis = FakeRedis()

    def tearDown(self):
        utils.REDIS_URL = self.old_redis_url
        utils._redis = self.old_redis
        utils.API_KEY = self.old_api_key
        index.API_KEY = self.old_index_key
        index._RATE_LIMIT = self.old_rate_limit
        utils._cache_store.clear()
        utils._memory_rate_limits.clear()
        utils._abuse_events.clear()

    def test_redis_cache_rate_limit_and_abuse_log(self):
        utils.REDIS_URL = "redis://fake"
        utils._redis = self.redis
        utils.cache_set("test", {"value": 7})
        self.assertEqual(utils.cache_get("test"), {"value": 7})
        self.assertEqual(utils.cache_stats()["backend"], "redis")
        self.assertEqual(utils.cache_clear(), 1)

        allowed, _ = utils.enforce_rate_limit("identity", 1)
        blocked, retry = utils.enforce_rate_limit("identity", 1)
        self.assertTrue(allowed)
        self.assertFalse(blocked)
        self.assertGreaterEqual(retry, 1)

        utils.log_abuse_event({"path": "/yt/download"})
        self.assertEqual(utils.get_abuse_events()[0]["path"], "/yt/download")

    def test_private_proxy_targets_are_rejected(self):
        self.assertFalse(utils.validate_proxy_target("http://127.0.0.1:8080/"))
        self.assertFalse(utils.validate_proxy_target("http://[::1]/"))
        self.assertFalse(utils.validate_proxy_target("file:///etc/passwd"))
        self.assertFalse(utils.validate_proxy_target("http://user:pass@example.com/"))

    def test_dash_template_rewrite_keeps_substitution_tokens(self):
        utils.API_KEY = "test-secret"
        mpd = """<MPD><BaseURL>https://cdn.example/video/</BaseURL>
        <Period><AdaptationSet><Representation id="v1">
        <SegmentTemplate initialization="init.mp4" media="seg-$Number%05d$.m4s" />
        </Representation></AdaptationSet></Period></MPD>"""
        rewritten = utils.rewrite_dash_manifest(
            mpd, "https://cdn.example/video/manifest.mpd",
            "https://api.example", "/adult/proxy",
        )
        root = ET.fromstring(rewritten)
        template = next(node for node in root.iter() if node.tag.endswith("SegmentTemplate"))
        media = template.attrib["media"]
        self.assertIn("/adult/proxy?dash=1", media)
        self.assertIn("$Number%05d$", media)
        self.assertIn("_t=", media)
        self.assertTrue(template.attrib["initialization"].startswith("https://api.example/adult/proxy?url="))

    def test_protected_routes_fail_closed_without_key(self):
        index.API_KEY = ""
        response = index.app.test_client().post("/yt/download", json={"url": "https://example.com/video"})
        self.assertEqual(response.status_code, 401)

    def test_failed_auth_attempts_are_rate_limited(self):
        index.API_KEY = "test-key"
        index._RATE_LIMIT = 1
        client = index.app.test_client()
        first = client.post("/yt/download", json={"url": "https://example.com/video"})
        second = client.post("/yt/download", json={"url": "https://example.com/video"})
        self.assertEqual(first.status_code, 401)
        self.assertEqual(second.status_code, 429)

    def test_proxy_paths_reach_token_validation_without_raw_key(self):
        index.API_KEY = ""
        response = index.app.test_client().get(
            "/adult/proxy?url=http%3A%2F%2F127.0.0.1%2F"
        )
        self.assertEqual(response.status_code, 400)

    def test_registered_site_adapters_have_download_and_watch_routes(self):
        rules = {rule.rule for rule in index.app.url_map.iter_rules()}
        for site in ("redtube", "youporn", "eporner", "spankbang", "porntrex"):
            self.assertIn(f"/{site}/download", rules)
            self.assertIn(f"/{site}/watch", rules)

    def test_adult_site_route_returns_standard_response(self):
        index.API_KEY = "test-key"
        meta = {"title": "Example", "thumbnail": "", "duration": "", "duration_seconds": 0}
        qualities = [{
            "quality": "720", "format": "mp4", "proxy_url": "https://api/proxy",
            "download_url": "https://api/download",
        }]
        with (
            patch("api.extractors.registry.validate_proxy_target", return_value=True),
            patch("api.extractors.registry.ytdlp_get_qualities", return_value=(meta, qualities)),
        ):
            response = index.app.test_client().post(
                "/redtube/download", json={"url": "https://redtube.com/watch?id=1"},
                headers={"X-API-Key": "test-key"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(data["title"], "Example")
        self.assertEqual(data["best_proxy_url"], "https://api/proxy")
        self.assertIn("https%3A%2F%2Fredtube.com", data["watch_url"])

    def test_watch_page_escapes_metadata_and_includes_controls(self):
        page, status, _ = utils.render_watch_page(
            {"title": "<script>alert(1)</script>", "thumbnail": ""},
            [{"quality": "720", "format": "dash", "proxy_url": "https://api/stream",
              "download_url": "https://api/download"}],
        )
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("dashjs.MediaPlayer()", page)
        self.assertIn('id="playbackRate"', page)
        self.assertIn('id="fullscreenBtn"', page)
        self.assertIn('id="copyBtn"', page)
        self.assertIn("Stream unavailable", page)
        self.assertIn("playsinline", page)


if __name__ == "__main__":
    unittest.main()

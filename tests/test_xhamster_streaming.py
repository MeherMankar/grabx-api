import os
import unittest
from urllib.parse import parse_qs, quote, urlparse
from unittest.mock import patch

os.environ["APP_ENV"] = "development"
os.environ["API_KEY"] = ""
os.environ["REDIS_URL"] = ""

from api import index, utils
from api.extractors.xhamster import _rewrite_hls_manifest


class XHamsterStreamingTests(unittest.TestCase):
    def test_manifest_rewrites_relative_segment_and_key_urls(self):
        manifest = """#EXTM3U
#EXT-X-MAP:URI="init.mp4"
#EXT-X-KEY:METHOD=AES-128,URI="?key=abc"
#EXTINF:4,
segments/part.ts
"""
        rewritten = _rewrite_hls_manifest(
            manifest,
            "https://cdn.example/path/master.m3u8?token=master",
            "https://api.example",
        )
        lines = rewritten.splitlines()
        init_proxy = lines[1].split('URI="', 1)[1].split('"', 1)[0]
        key_proxy = lines[2].split('URI="', 1)[1].split('"', 1)[0]
        segment_proxy = lines[4]

        self.assertEqual(
            parse_qs(urlparse(init_proxy).query)["url"][0],
            "https://cdn.example/path/init.mp4",
        )
        self.assertEqual(
            parse_qs(urlparse(key_proxy).query)["url"][0],
            "https://cdn.example/path/master.m3u8?key=abc",
        )
        self.assertEqual(
            parse_qs(urlparse(segment_proxy).query)["url"][0],
            "https://cdn.example/path/segments/part.ts",
        )

    def test_download_endpoint_streams_selected_mp4_as_attachment(self):
        page_url = "https://xhamster.com/videos/example"

        class FakePage:
            status_code = 200
            url = page_url
            text = "<html></html>"

        class FakePageSession:
            def get(self, url, allow_redirects, timeout):
                self.request = (url, allow_redirects, timeout)
                return FakePage()

        class FakeUpstream:
            status_code = 206
            headers = {
                "Content-Type": "video/mp4",
                "Content-Length": "5",
                "Content-Range": "bytes 0-4/10",
            }
            closed = False

            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                yield b"video"

            def close(self):
                self.closed = True

        upstream = FakeUpstream()
        with (
            patch.dict(os.environ, {"API_KEY": "test-key"}),
            patch("api.extractors.xhamster._cffi_session", return_value=FakePageSession()),
            patch("api.extractors.xhamster.extract_data", return_value={
                "hls_url": "https://cdn.example/master.m3u8",
                "qualities": [
                    {"quality": "720", "format": "mp4", "url": "https://cdn.example/720.mp4"},
                    {"quality": "480", "format": "mp4", "url": "https://cdn.example/480.mp4"},
                ],
            }),
            patch("api.extractors.xhamster._request_adult_stream", return_value=upstream) as fetch_mp4,
        ):
            token = utils.sign_url(page_url)
            response = index.app.test_client().get(
                f"/xh/stream?src={quote(page_url, safe='')}&{token}&dl=1&quality=480",
                headers={"Range": "bytes=0-4"},
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"video")
        self.assertEqual(
            response.headers["Content-Disposition"],
            'attachment; filename="xhamster-480p.mp4"',
        )
        self.assertEqual(response.headers["Content-Range"], "bytes 0-4/10")
        self.assertTrue(upstream.closed)
        self.assertEqual(fetch_mp4.call_args.args[1], "https://cdn.example/480.mp4")
        self.assertEqual(fetch_mp4.call_args.args[2]["Range"], "bytes=0-4")

    def test_segment_endpoint_accepts_signed_browser_request_without_api_key(self):
        cdn_url = "https://cdn.example/segments/part.ts"

        class FakeUpstream:
            status_code = 206
            headers = {
                "Content-Type": "video/mp2t",
                "Content-Length": "4",
                "Content-Range": "bytes 0-3/8",
            }
            closed = False

            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                yield b"part"

            def close(self):
                self.closed = True

        upstream = FakeUpstream()
        with (
            patch.dict(os.environ, {"API_KEY": "test-key"}),
            patch("requests.get", return_value=upstream) as fetch_segment,
        ):
            token = utils.sign_url(cdn_url)
            response = index.app.test_client().get(
                f"/xh/seg?url={quote(cdn_url, safe='')}&{token}",
                headers={"Range": "bytes=0-3"},
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"part")
        self.assertEqual(response.headers["Content-Range"], "bytes 0-3/8")
        self.assertTrue(upstream.closed)
        self.assertEqual(fetch_segment.call_args.kwargs["headers"]["Range"], "bytes=0-3")

    def test_watch_player_offers_mp4_download_for_hls_quality(self):
        page, status, _ = utils.render_watch_page(
            {"title": "Test video", "thumbnail": ""},
            [{
                "quality": "Auto",
                "format": "hls",
                "proxy_url": "https://api.example/xh/stream",
                "download_url": "https://api.example/xh/stream?dl=1&quality=720",
                "download_label": "↓ Download MP4",
            }],
        )

        self.assertEqual(status, 200)
        self.assertIn('id="dlBtn"', page)
        self.assertIn('data-dl-label="↓ Download MP4"', page)
        self.assertIn("currentDlLabel.includes('Download MP4')", page)
        self.assertIn("window.open(currentDlUrl, '_blank', 'noopener')", page)


if __name__ == "__main__":
    unittest.main()

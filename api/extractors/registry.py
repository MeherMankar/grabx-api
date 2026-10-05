"""Shared extractor adapter contract and yt-dlp-backed site registry."""

from typing import Protocol
from urllib.parse import urlparse

from api.extractors.ytdlp import ytdlp_get_qualities
from api.utils import validate_proxy_target


class SiteAdapter(Protocol):
    """Contract for site adapters used by the dynamically registered routes."""

    def validate_url(self, url: str) -> str: ...

    def extract(self, url: str, base_url: str) -> tuple[str, dict, list[dict]]: ...


class YtDlpSiteAdapter:
    def __init__(self, site: str, hosts: tuple[str, ...]):
        self.site = site
        self.hosts = hosts

    def validate_url(self, url: str) -> str:
        if not urlparse(url).scheme:
            url = "https://" + url
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not any(
            hostname == domain or hostname.endswith("." + domain)
            for domain in self.hosts
        ):
            raise ValueError(f"URL is not a supported {self.site.title()} video URL.")
        if not validate_proxy_target(url):
            raise ValueError("URL must resolve to a public HTTP(S) address.")
        return url

    def extract(self, url: str, base_url: str) -> tuple[str, dict, list[dict]]:
        validated_url = self.validate_url(url)
        meta, qualities = ytdlp_get_qualities(validated_url, base_url, "/adult/proxy")
        if not qualities:
            raise ValueError(f"No playable streams found for {self.site.title()}.")
        return validated_url, meta, qualities


SITE_ADAPTERS: dict[str, SiteAdapter] = {
    "redtube": YtDlpSiteAdapter("RedTube", ("redtube.com", "redtube.xxx")),
    "youporn": YtDlpSiteAdapter(
        "YouPorn", ("youporn.com", "youporn.net", "youporn.org", "youporn.xxx")
    ),
    "eporner": YtDlpSiteAdapter("Eporner", ("eporner.com", "eporner.net")),
    "spankbang": YtDlpSiteAdapter("SpankBang", ("spankbang.com",)),
    "porntrex": YtDlpSiteAdapter("PornTrex", ("porntrex.com",)),
}

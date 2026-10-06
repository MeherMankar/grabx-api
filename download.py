"""
GrabX API test script.

Usage:
  python download.py xh   <xhamster_url>
  python download.py xv   <xvideos_url>
  python download.py xnxx <xnxx_url>
  python download.py ph   <pornhub_url>
  python download.py tb   <terabox_url>

Examples:
  python download.py xh "https://xhamster19.com/videos/indian-stepmom-xhstHAM"
  python download.py xv "https://www.xvideos.com/video12345/title"
"""
import sys
import time
import requests

API    = "https://nearby-sherline-mehermankarofficial-3b587d66.koyeb.app"
APIKEY = "meher"
HDRS   = {"X-API-Key": APIKEY, "Content-Type": "application/json"}


def _test_stream(label: str, proxy_url: str):
    """GET the proxy URL with a Range header and print the result."""
    print(f"  Testing stream [{label}]...")
    t0 = time.time()
    try:
        r = requests.get(proxy_url, stream=True, timeout=25,
                         headers={"Range": "bytes=0-65535", "X-API-Key": APIKEY})
        elapsed = time.time() - t0
        if r.status_code in (200, 206):
            cl = r.headers.get("Content-Length", "?")
            ct = r.headers.get("Content-Type", "?")
            print(f"  ✅  HTTP {r.status_code}  |  {ct}  |  bytes={cl}  |  {elapsed:.1f}s")
        else:
            print(f"  ❌  HTTP {r.status_code}  —  {r.text[:200]}")
        r.close()
    except Exception as e:
        print(f"  ❌  Error: {e}")


def test_xh(url: str):
    print(f"\n{'='*60}")
    print(f"XHamster: {url}")
    print(f"{'='*60}")
    r = requests.post(f"{API}/xh/download", json={"url": url}, headers=HDRS, timeout=30)
    d = r.json()
    if d.get("status") != "success":
        print(f"Extraction failed: {d.get('message')}")
        return
    data = d["data"]
    print(f"Title:    {data['title']}")
    print(f"Duration: {data['duration']}")
    print(f"Qualities: {len(data['qualities'])}")
    for q in data["qualities"]:
        print(f"  {q['quality']:>4}p  {q['format']:<4}  proxy_url: {q['proxy_url'][:80]}...")
    print()
    # Test stream for each quality
    for q in data["qualities"]:
        _test_stream(f"{q['quality']}p {q['format']}", q["proxy_url"])
    print(f"\nWatch page: {API}/xh/watch?url={requests.utils.quote(url)}")


def test_xv(url: str):
    print(f"\n{'='*60}")
    print(f"Xvideos: {url}")
    print(f"{'='*60}")
    r = requests.post(f"{API}/xv/download", json={"url": url}, headers=HDRS, timeout=30)
    d = r.json()
    if d.get("status") != "success":
        print(f"Extraction failed: {d.get('message')}")
        return
    data = d["data"]
    print(f"Title:    {data['title']}")
    print(f"Qualities: {len(data['qualities'])}")
    for q in data["qualities"]:
        _test_stream(f"{q['quality']}p {q['format']}", q["proxy_url"])


def test_xnxx(url: str):
    print(f"\n{'='*60}")
    print(f"XNXX: {url}")
    print(f"{'='*60}")
    r = requests.post(f"{API}/xnxx/download", json={"url": url}, headers=HDRS, timeout=30)
    d = r.json()
    if d.get("status") != "success":
        print(f"Extraction failed: {d.get('message')}")
        return
    data = d["data"]
    print(f"Title:    {data['title']}")
    print(f"Qualities: {len(data['qualities'])}")
    for q in data["qualities"]:
        _test_stream(f"{q['quality']}p {q['format']}", q["proxy_url"])


def test_ph(url: str):
    print(f"\n{'='*60}")
    print(f"PornHub: {url}")
    print(f"{'='*60}")
    r = requests.post(f"{API}/ph/download", json={"url": url}, headers=HDRS, timeout=60)
    d = r.json()
    if d.get("status") != "success":
        print(f"Extraction failed: {d.get('message')}")
        return
    data = d["data"]
    print(f"Title:    {data['title']}")
    print(f"Qualities: {len(data['qualities'])}")
    for q in data["qualities"]:
        _test_stream(f"{q['quality']}p {q['format']}", q["proxy_url"])


def test_tb(url: str):
    print(f"\n{'='*60}")
    print(f"Terabox: {url}")
    print(f"{'='*60}")
    r = requests.post(f"{API}/download", json={"url": url}, headers=HDRS, timeout=30)
    d = r.json()
    if d.get("status") != "success":
        print(f"Extraction failed: {d.get('message')}")
        return
    data = d["data"]
    for f in data.get("files", []):
        print(f"File: {f['filename']}  ({f.get('size','?')})")
        _test_stream(f["filename"], f["proxy_url"])


COMMANDS = {
    "xh":   test_xh,
    "xv":   test_xv,
    "xnxx": test_xnxx,
    "ph":   test_ph,
    "tb":   test_tb,
}

if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2])

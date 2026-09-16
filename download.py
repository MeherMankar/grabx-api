"""
Usage: python download.py <terabox_share_url>
Example: python download.py "https://1024terabox.com/s/1bwf-DxcMG6LU6lnPz4VlaA"
"""
import sys
import requests

API = "http://127.0.0.1:5000"

def download(share_url: str):
    # Step 1: get file info + dlink from local API
    print(f"Fetching info for: {share_url}")
    r = requests.post(f"{API}/download", json={"url": share_url}, timeout=30)
    data = r.json()

    if data.get("status") != "success":
        print("Error:", data.get("message"))
        return

    files = data["data"]["files"]
    print(f"Found {len(files)} file(s):")
    for i, f in enumerate(files):
        print(f"  [{i}] {f['filename']}  ({f['size']})")

    file = files[0]
    filename = file["filename"]
    proxy_url = file["proxy_url"]
    dlink = file["dlink"]

    print(f"\nDownloading: {filename}")
    print(f"Size: {file['size']}")

    # Step 2: download via proxy_url (server handles auth)
    print("Connecting...")
    dl = requests.get(proxy_url, stream=True, timeout=60)
    dl.raise_for_status()

    total = int(dl.headers.get("Content-Length", 0))
    downloaded = 0

    with open(filename, "wb") as f:
        for chunk in dl.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    mb = downloaded / 1024 / 1024
                    print(f"\r  {mb:.1f} MB / {total/1024/1024:.1f} MB  ({pct:.1f}%)", end="", flush=True)

    print(f"\nDone! Saved as: {filename}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python download.py <terabox_share_url>")
        sys.exit(1)
    download(sys.argv[1])

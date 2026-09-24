# GrabX API

[![GitHub](https://img.shields.io/badge/GitHub-MeherMankar%2Fgrabx--api-blue?logo=github)](https://github.com/MeherMankar/grabx-api)
[![Live API](https://img.shields.io/badge/Live%20API-onrender.com-brightgreen?logo=render)](https://grabx-api.onrender.com)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

A self-hosted Flask API that extracts direct download links from multiple platforms — Terabox, PornHub, and more to come. No third-party services, deployable on Render.

**Maintained by:** [MeherMankar](https://github.com/MeherMankar) · [Telegram](https://t.me/MeherPatil)  
**Base project by:** [genxnano](https://t.me/genxnano)

---

## Supported Platforms

| Platform | Endpoint | Notes |
|----------|----------|-------|
| Terabox | `POST /download` | Requires `TERABOX_COOKIE` env var |
| PornHub | `POST /ph/download` | No account needed |
| PornHub Player | `GET /ph/watch/<viewkey>` | Browser video player |
| More coming... | — | — |

---

## Live Demo

**Base URL:** `https://grabx-api.onrender.com`

```bash
# Terabox
curl -X POST https://grabx-api.onrender.com/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://1024terabox.com/s/YOUR_SHARE_ID"}'

# PornHub
curl -X POST https://grabx-api.onrender.com/ph/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.pornhub.com/view_video.php?viewkey=..."}'

# PornHub web player (open in browser)
# https://grabx-api.onrender.com/ph/watch/<viewkey>
```

---

## Features

- **Terabox** — direct download links from any share URL, multi-account support, 17 domains, built-in proxy stream
- **PornHub** — all quality variants (240p–1080p MP4 + HLS), browser video player with quality selector and download button
- **DPI bypass** — `curl_cffi` Chrome TLS impersonation + HTTP/3 QUIC + Cloudflare DoH (bypasses ISP-level blocks)
- **Render-optimised** — MP4 streams redirect to CDN directly (no 30s timeout issues on free tier)
- Single env variable setup, Docker-ready

---

## Endpoints

### `GET /`
Returns API status and all available endpoints.

---

### Terabox

#### `POST /download`
Get direct download link(s) for a Terabox share URL.

**Request**
```json
{ "url": "https://1024terabox.com/s/1bwf-DxcMG6LU6lnPz4VlaA" }
```

**Response**
```json
{
  "status": "success",
  "data": {
    "title": "video.mp4",
    "download_available": true,
    "files": [
      {
        "filename": "video.mp4",
        "size": "1017.47 MB",
        "size_bytes": 1066890090,
        "thumbnail": "https://...",
        "dlink": "https://...",
        "proxy_url": "https://grabx-api.onrender.com/proxy?url=...",
        "fs_id": "901464181182712"
      }
    ]
  }
}
```

#### `GET /proxy?url=<encoded_dlink>`
Streams a Terabox file through the server with cookies attached. Open `proxy_url` directly in a browser — no Terabox account needed on the client side.

---

### PornHub

#### `POST /ph/download`
Extract all quality variants + proxy/download URLs for a PornHub video.

**Request**
```json
{ "url": "https://www.pornhub.com/view_video.php?viewkey=6a165f5d3a96c" }
```

**Response**
```json
{
  "status": "success",
  "data": {
    "title": "Video Title",
    "thumbnail": "https://...",
    "duration": "7:18",
    "duration_seconds": 438,
    "viewkey": "6a165f5d3a96c",
    "watch_url": "https://grabx-api.onrender.com/ph/watch/6a165f5d3a96c",
    "qualities": [
      {
        "quality": "1080", "format": "mp4",
        "url": "https://ev.phncdn.com/...",
        "proxy_url": "https://grabx-api.onrender.com/ph/proxy?url=...",
        "download_url": "https://grabx-api.onrender.com/ph/proxy?url=...&dl=1"
      }
    ],
    "best_proxy_url": "https://grabx-api.onrender.com/ph/proxy?url=...",
    "best_download_url": "https://grabx-api.onrender.com/ph/proxy?url=...&dl=1"
  }
}
```

#### `GET /ph/watch/<viewkey>`
Opens a browser video player with quality selector and download button.

```
https://grabx-api.onrender.com/ph/watch/6a165f5d3a96c
```

#### `GET /ph/proxy?url=<encoded_url>&dl=0|1`
Proxies a PH CDN URL with the correct `Referer` and cookies.
- `dl=0` (default) — streams inline, browser plays it in a `<video>` tag
- `dl=1` — forces browser download (`Content-Disposition: attachment`)
- MP4 stream mode issues a 302 redirect to the CDN directly (avoids server bandwidth)

---

### `GET /docs`
Returns full API documentation as Markdown.

---

## Setup

### Prerequisites
- Python 3.11+
- A free Terabox account (for Terabox endpoints only)

### Get your Terabox `ndus` cookie

1. Open [1024terabox.com](https://1024terabox.com) in Chrome and log in
2. `F12` → **Application** → **Cookies** → `https://www.1024terabox.com`
3. Copy the value of the `ndus` cookie

### Local development

```bash
git clone https://github.com/MeherMankar/grabx-api
cd grabx-api
pip install -r requirements.txt
```

Create a `.env` file:
```env
# Single Terabox account
TERABOX_COOKIE=ndus=YOUR_NDUS_VALUE

# Multiple accounts (picked randomly per request)
TERABOX_COOKIE=ndus=VALUE1,ndus=VALUE2,ndus=VALUE3
```

Run:
```bash
python api/index.py
```

API available at `http://localhost:5000`.

---

## Deploy to Render

1. Push this repo to GitHub as `grabx-api`

2. Go to [render.com](https://render.com) → **New** → **Web Service** → connect repo

3. Render auto-detects the `Dockerfile`. Settings:

   | Setting | Value |
   |---------|-------|
   | Environment | Docker |
   | Port | 8000 |

4. Add environment variable:

   | Name | Value |
   |------|-------|
   | `TERABOX_COOKIE` | `ndus=VALUE1,ndus=VALUE2` |

5. Click **Deploy**

---

## Usage Examples

### Python
```python
import requests

# Terabox
r = requests.post("https://grabx-api.onrender.com/download",
    json={"url": "https://1024terabox.com/s/YOUR_SHARE_ID"})
files = r.json()["data"]["files"]
print(files[0]["proxy_url"])  # open in browser to download

# PornHub
r = requests.post("https://grabx-api.onrender.com/ph/download",
    json={"url": "https://www.pornhub.com/view_video.php?viewkey=..."})
data = r.json()["data"]
print(data["watch_url"])          # browser player
print(data["best_download_url"])  # direct download
```

### JavaScript
```js
// PornHub
const res = await fetch("https://grabx-api.onrender.com/ph/download", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ url: "https://www.pornhub.com/view_video.php?viewkey=..." })
});
const { data } = await res.json();
window.open(data.watch_url);  // open player in browser
```

---

## Project Structure

```
grabx-api/
├── api/
│   └── index.py        # Flask app — all API logic
├── download.py         # CLI download script (Terabox)
├── docs.md             # API docs (served at /docs)
├── Dockerfile          # For Render / Docker deploys
├── render.yaml         # Render deployment config
├── vercel.json         # Vercel deployment config
├── requirements.txt
└── .env                # Local env vars (not committed)
```

---

## Supported Terabox Domains

```
terabox.com         1024terabox.com     teraboxapp.com
terasharefile.com   nephobox.com        4funbox.co
mirrobox.com        momerybox.com       freeterabox.com
teraboxlink.com     terafileshare.com   teraboxshare.com
terabox1.com        terabox2.com        1024tera.com
terabox.app
```

---

## License

[MIT](LICENSE)

---

## Credits

| Role | Credit |
|------|--------|
| Maintainer | [MeherMankar](https://github.com/MeherMankar) · [Telegram](https://t.me/MeherPatil) |
| Base project | [genxnano](https://t.me/genxnano) |
| WAP bypass technique | [FZBypassBot](https://github.com/rjriajul/FZBypassBot) |

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
| Terabox | `POST /download` | Requires `TERABOX_COOKIE` env var — supports folders |
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
  -H "X-API-Key: your_key_here" \
  -d '{"url": "https://1024terabox.com/s/YOUR_SHARE_ID"}'

# PornHub
curl -X POST https://grabx-api.onrender.com/ph/download \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_key_here" \
  -d '{"url": "https://www.pornhub.com/view_video.php?viewkey=..."}'

# PornHub web player (open in browser — no key needed)
# https://grabx-api.onrender.com/ph/watch/<viewkey>
```

---

## Features

- **API key auth** — optional `X-API-Key` header guard on all data endpoints; set `API_KEY` env var to enable
- **Terabox** — direct download links from any share URL, recursive folder support, multi-account rotation, 17 domains, built-in proxy stream
- **Terabox video quality** — resolution, dimensions, duration, fps, bitrate extracted from `video_info` metadata where available
- **PornHub** — all quality variants (240p–1080p MP4 + HLS), browser video player with quality selector and download button
- **DPI bypass** — `curl_cffi` Chrome TLS impersonation + HTTP/3 QUIC + Cloudflare DoH (bypasses ISP-level blocks)
- **Render-optimised** — MP4 streams redirect to CDN directly (no 30s timeout issues on free tier)
- Single env variable setup, Docker-ready

---

## Authentication

Set the `API_KEY` environment variable to protect all data endpoints.  
Leave it **unset** for open/public access (default).

```env
API_KEY=your_secret_key_here
```

### Passing the key

| Method | Example |
|--------|---------|
| Header | `X-API-Key: your_secret_key_here` |
| Query param | `?api_key=your_secret_key_here` |

### Public routes (no key required, ever)

| Route | Reason |
|-------|--------|
| `GET /` | API info / index |
| `GET /docs` | Documentation |
| `GET /health` | Health check |
| `GET /ph/watch/<viewkey>` | Browser player (opened in a tab, no header support) |

### Error responses

```json
// Missing key
{ "status": "error", "message": "Missing API key. Pass it as X-API-Key header or ?api_key= query param." }
// 401

// Wrong key
{ "status": "error", "message": "Invalid API key." }
// 403
```

---

## Endpoints

### `GET /`
Returns API status, auth mode, and all available endpoints.

---

### `GET /health`
Health check — returns Python version, platform, and auth status. Always public.

```json
{
  "status": "ok",
  "python": "3.11.x ...",
  "platform": "Linux-...",
  "accounts_configured": 2,
  "auth": "enabled"
}
```

---

### Terabox

#### `POST /download`
Get direct download link(s) for a Terabox share URL. Automatically recurses into folders.

**Request**
```json
{ "url": "https://1024terabox.com/s/1bwf-DxcMG6LU6lnPz4VlaA" }
```

**Response — single file**
```json
{
  "status": "success",
  "data": {
    "title": "video.mp4",
    "total_files": 1,
    "download_available": true,
    "files": [
      {
        "filename": "video.mp4",
        "folder": "/share_root",
        "size": "1017.47 MB",
        "size_bytes": 1066890090,
        "thumbnail": "https://...",
        "dlink": "https://...",
        "proxy_url": "https://grabx-api.onrender.com/proxy?url=...",
        "fs_id": "901464181182712",
        "video_quality": {
          "width": 1920,
          "height": 1080,
          "resolution": "1920x1080",
          "label": "1080p",
          "duration_seconds": 3724,
          "duration": "62:04",
          "fps": 30.0,
          "bitrate_kbps": 4200.0
        }
      }
    ]
  }
}
```

**Response — shared folder (recursive)**
```json
{
  "status": "success",
  "data": {
    "title": "12 files across 3 folders",
    "total_files": 12,
    "download_available": true,
    "files": [ ... ]
  }
}
```

> `video_quality` is only present on video files where Terabox exposes metadata.  
> `folder` shows the parent directory path within the share.

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
Opens a browser video player with quality selector and download button. Always public — no API key needed.

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
Returns full API documentation as Markdown. Always public.

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

# Optional: protect the API with a key
API_KEY=your_secret_key_here
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

4. Add environment variables:

   | Name | Value |
   |------|-------|
   | `TERABOX_COOKIE` | `ndus=VALUE1,ndus=VALUE2` |
   | `API_KEY` | `your_secret_key_here` *(optional)* |

5. Click **Deploy**

---

## Usage Examples

### Python
```python
import requests

HEADERS = {
    "Content-Type": "application/json",
    "X-API-Key": "your_secret_key_here",  # omit if API_KEY not set
}

# Terabox — single file or entire folder
r = requests.post(
    "https://grabx-api.onrender.com/download",
    json={"url": "https://1024terabox.com/s/YOUR_SHARE_ID"},
    headers=HEADERS,
)
data = r.json()["data"]
print(f"{data['total_files']} file(s): {data['title']}")
for f in data["files"]:
    print(f["filename"], f.get("video_quality", {}).get("label", ""), f["proxy_url"])

# PornHub
r = requests.post(
    "https://grabx-api.onrender.com/ph/download",
    json={"url": "https://www.pornhub.com/view_video.php?viewkey=..."},
    headers=HEADERS,
)
data = r.json()["data"]
print(data["watch_url"])          # browser player
print(data["best_download_url"])  # direct download
```

### JavaScript
```js
const API_KEY = "your_secret_key_here"; // omit if API_KEY not set

// Terabox
const res = await fetch("https://grabx-api.onrender.com/download", {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
  body: JSON.stringify({ url: "https://1024terabox.com/s/YOUR_SHARE_ID" })
});
const { data } = await res.json();
console.log(`${data.total_files} file(s)`);
data.files.forEach(f => console.log(f.filename, f.video_quality?.label, f.proxy_url));

// PornHub
const ph = await fetch("https://grabx-api.onrender.com/ph/download", {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
  body: JSON.stringify({ url: "https://www.pornhub.com/view_video.php?viewkey=..." })
});
const { data: phData } = await ph.json();
window.open(phData.watch_url); // open player in browser
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

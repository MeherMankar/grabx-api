# GrabX API

[![GitHub](https://img.shields.io/badge/GitHub-MeherMankar%2Fgrabx--api-blue?logo=github)](https://github.com/MeherMankar/grabx-api)
[![Live API](https://img.shields.io/badge/Live%20API-onrender.com-brightgreen?logo=render)](https://grabx-api.onrender.com)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

A self-hosted Flask API that extracts direct download/stream links from multiple video platforms — no third-party services, deployable on Render in minutes.

**Maintained by:** [MeherMankar](https://github.com/MeherMankar) · [Telegram](https://t.me/MeherPatil)  
**Terabox base by:** [genxnano](https://t.me/genxnano)

---

## Supported Platforms

| Platform | Endpoint | Notes |
|----------|----------|-------|
| **Terabox** | `POST /download` | Requires `TERABOX_COOKIE`. Supports folders, 20+ mirror domains |
| **PornHub** | `POST /ph/download` | All qualities, auto-refresh on CDN expiry |
| **Xvideos** | `POST /xv/download` | 360p/480p MP4 + HLS variants |
| **XNXX** | `POST /xnxx/download` | Same extractor as Xvideos |
| **XHamster** | `POST /xh/download` | 144p–720p MP4, PRNG URL decryption |
| **JAVtiful** | `POST /jav/download` | Free 720p MP4 stream |

All platforms have a `/watch` page for browser playback.

---

## Features

- **Zero-key streaming** — bot authenticates once; returned URLs are HMAC-signed, no key needed to stream
- **Cloudflare Worker** — route all CDN streams through CF edge, zero Render bandwidth
- **DPI bypass** — `curl_cffi` Chrome TLS + HTTP/3/QUIC + Cloudflare DoH
- **Terabox** — recursive folder support, video quality/resolution metadata, 20+ mirror domains
- **XHamster** — proprietary PRNG URL decryption (matches yt-dlp extractor)
- **HLS proxy** — manifest rewriting so `.m3u8` streams play anywhere without special headers
- **Auto-refresh** — expired PH CDN links are re-resolved automatically using the embedded viewkey
- Modular codebase (`api/extractors/`), Docker-ready, single `.env` setup

---

## Quick Start

### 1. Get a stream link

```bash
curl -X POST https://grabx-api.onrender.com/ph/download \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.pornhub.com/view_video.php?viewkey=abc123"}'
```

### 2. Open the player in any browser (no key needed)

```
https://grabx-api.onrender.com/ph/watch/abc123
```

### 3. Stream or download (no key needed — URL is pre-signed)

```python
# The proxy_url from the response works directly:
import requests
r = requests.get(response_json["data"]["best_proxy_url"], stream=True)
```

---

## Setup

### Prerequisites
- Python 3.11+
- A free Terabox account (only needed for Terabox endpoints)
- A free Render account (for deployment)

### Get your Terabox `ndus` cookie

1. Open [1024terabox.com](https://1024terabox.com) in Chrome and log in
2. Press `F12` → **Application** → **Cookies** → `https://www.1024terabox.com`
3. Copy the value of the `ndus` cookie

### Local development

```bash
git clone https://github.com/MeherMankar/grabx-api
cd grabx-api
pip install -r requirements.txt
```

Create `.env`:
```env
# Terabox — one account
TERABOX_COOKIE=ndus=YOUR_NDUS_VALUE

# Multiple Terabox accounts (picked randomly per request)
TERABOX_COOKIE=ndus=VALUE1,ndus=VALUE2

# Optional: protect the API with a key
API_KEY=your_secret_key
```

Run:
```bash
python api/index.py
```

API available at `http://localhost:5000`.

---

## Deploy to Render

1. Push this repo to GitHub

2. Go to [render.com](https://render.com) → **New → Web Service** → connect repo

3. Render auto-detects the `Dockerfile`:

   | Setting | Value |
   |---------|-------|
   | Environment | Docker |
   | Port | 8000 |

4. Add environment variables:

   | Name | Value |
   |------|-------|
   | `TERABOX_COOKIE` | `ndus=VALUE1,ndus=VALUE2` |
   | `API_KEY` | `your_secret_key` *(optional)* |
   | `CF_WORKER_URL` | `https://grabx-api.yourname.workers.dev` *(optional — see below)* |

5. Click **Deploy**

---

## Cloudflare Worker (optional — zero Render bandwidth)

By default all streams go through Render. The CF Worker routes PH, Xvideos, XNXX, XHamster, and JAVtiful CDN streams through Cloudflare's edge instead — Render only handles API logic and Terabox.

```bash
cd worker
npm install
npx wrangler login          # pick your CF account
npx wrangler secret put API_KEY   # same value as on Render
npx wrangler deploy
# → https://grabx-api.yourname.workers.dev
```

Then add `CF_WORKER_URL=https://grabx-api.yourname.workers.dev` to Render env vars and redeploy.

---

## API Reference

Full docs at [`/docs`](https://grabx-api.onrender.com/docs) or see [docs.md](docs.md).

### Endpoints at a glance

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | — | API info |
| GET | `/health` | — | Health check |
| GET | `/docs` | — | Full documentation |
| POST | `/download` | ✓ | Terabox → download links |
| GET | `/proxy` | token | Terabox CDN proxy |
| POST | `/ph/download` | ✓ | PornHub → stream links |
| GET | `/ph/watch/<viewkey>` | — | PH video player |
| GET | `/ph/proxy` | token | PH CDN proxy |
| POST | `/xv/download` | ✓ | Xvideos → stream links |
| GET | `/xv/watch` | — | Xvideos player |
| POST | `/xnxx/download` | ✓ | XNXX → stream links |
| GET | `/xnxx/watch` | — | XNXX player |
| POST | `/xh/download` | ✓ | XHamster → stream links |
| GET | `/xh/watch` | — | XHamster player |
| POST | `/jav/download` | ✓ | JAVtiful → stream links |
| GET | `/jav/watch` | — | JAVtiful player |
| GET | `/adult/proxy` | token | Xvideos/XNXX/XHamster CDN proxy |
| GET | `/jav/proxy` | token | JAVtiful CDN proxy |

**Auth column:** `✓` = API key required · `token` = signed URL token (no key needed) · `—` = always public

---

## Usage Examples

### Python

```python
import requests

BASE    = "https://grabx-api.onrender.com"
HEADERS = {"X-API-Key": "your_key", "Content-Type": "application/json"}

# PornHub
r = requests.post(f"{BASE}/ph/download",
    json={"url": "https://www.pornhub.com/view_video.php?viewkey=abc123"},
    headers=HEADERS)
data = r.json()["data"]
print(data["watch_url"])           # open in browser
print(data["best_proxy_url"])      # stream directly
print(data["best_download_url"])   # download

# Terabox
r = requests.post(f"{BASE}/download",
    json={"url": "https://1024terabox.com/s/YOUR_SHARE_ID"},
    headers=HEADERS)
files = r.json()["data"]["files"]
for f in files:
    print(f["filename"], f["size"], f["proxy_url"])

# XHamster
r = requests.post(f"{BASE}/xh/download",
    json={"url": "https://xhamster.com/videos/some-video-xhABCDEF"},
    headers=HEADERS)
data = r.json()["data"]
print(f"{len(data['qualities'])} qualities:", [q['quality']+'p' for q in data['qualities']])
```

### JavaScript / Bot

```js
const BASE = "https://grabx-api.onrender.com";
const KEY  = "your_key";

const res = await fetch(`${BASE}/ph/download`, {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-API-Key": KEY },
  body: JSON.stringify({ url: "https://www.pornhub.com/view_video.php?viewkey=abc123" })
});
const { data } = await res.json();

// Open player in Telegram bot
bot.sendMessage(chatId, `Watch: ${data.watch_url}`);

// Or send download link (pre-signed, no key needed)
bot.sendMessage(chatId, `Download: ${data.best_download_url}`);
```

---

## Project Structure

```
grabx-api/
├── api/
│   ├── index.py              # Flask app entry point (~165 lines)
│   ├── utils.py              # Shared utilities (tokens, proxy builder, watch page)
│   └── extractors/
│       ├── terabox.py        # Terabox extractor + routes
│       ├── pornhub.py        # PornHub extractor + routes
│       ├── javtiful.py       # JAVtiful extractor + routes
│       ├── xvideos.py        # Xvideos + XNXX + /adult/proxy
│       └── xhamster.py       # XHamster (with PRNG decryption) + routes
├── worker/
│   ├── worker.js             # Cloudflare Worker
│   └── wrangler.toml
├── Dockerfile
├── render.yaml
├── requirements.txt
└── docs.md                   # Full API documentation
```

---

## Supported Terabox Domains

All variants of: `terabox.com` · `1024terabox.com` · `teraboxapp.com` · `nephobox.com` · `4funbox.co` · `mirrobox.com` · `momerybox.com` · `tibibox.com` · `freeterabox.com` · `dubox.com` · `teraboxlink.com` · `terafileshare.com` · `teraboxshare.com` · `terasharefile.com` · `terasharelink.com` · `terabox1.com` · `terabox2.com` · `1024tera.com` · `terabox.app`

---

## License

[MIT](LICENSE)

---

## Credits

| Role | Credit |
|------|--------|
| Maintainer | [MeherMankar](https://github.com/MeherMankar) · [Telegram](https://t.me/MeherPatil) |
| Terabox base | [genxnano](https://t.me/genxnano) |
| WAP bypass | [FZBypassBot](https://github.com/rjriajul/FZBypassBot) |
| XHamster decryption | [yt-dlp XHamster extractor](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/xhamster.py) |

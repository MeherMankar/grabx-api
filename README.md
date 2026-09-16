# Terabox Downloader API

[![GitHub](https://img.shields.io/badge/GitHub-MeherMankar%2Fterabox--downloader--api-blue?logo=github)](https://github.com/MeherMankar/terabox-downloader-api)
[![Live API](https://img.shields.io/badge/Live%20API-onrender.com-brightgreen?logo=render)](https://terabox-downloader-api-pqxy.onrender.com)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

A self-hosted Flask API that extracts direct download links from any Terabox share URL — no third-party services, no CAPTCHA, deployable on Render or Vercel.

> **How it works:** Loads the Terabox WAP (mobile) page which embeds signed download links directly in its HTML inside `window.__INITIAL_STATE__` — bypassing the `verify_v2` CAPTCHA gate entirely.

**Maintained by:** [MeherMankar](https://github.com/MeherMankar) · [Telegram](https://t.me/MeherPatil)  
**Base project by:** [genxnano](https://t.me/genxnano) · [Original repo](https://github.com/Mrlabani/terabox-downloader-api)

---

## Live Demo

**Base URL:** `https://terabox-downloader-api-pqxy.onrender.com`

```bash
curl -X POST https://terabox-downloader-api-pqxy.onrender.com/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://1024terabox.com/s/YOUR_SHARE_ID"}'
```

---

## Features

- Direct download links from any Terabox share URL
- Multi-account support — random account picked per request to spread load and reduce ban risk
- Works with all Terabox domain variants (17 domains supported)
- Built-in `/proxy` endpoint — stream files through your server with no client-side cookie needed
- Returns filename, size, thumbnail, and `fs_id`
- Deployable on Render (Docker) or Vercel (serverless)
- Single env variable setup

---

## Endpoints

### `GET /`
Health check — returns status and number of accounts configured.

### `POST /download`
Get file info and direct download link for a Terabox share URL.

**Request**
```json
{
  "url": "https://1024terabox.com/s/1bwf-DxcMG6LU6lnPz4VlaA"
}
```

**Response**
```json
{
  "status": "success",
  "data": {
    "title": "The Amazing Spider-Man (2012).mp4",
    "download_available": true,
    "files": [
      {
        "filename": "The Amazing Spider-Man (2012).mp4",
        "size": "1017.47 MB",
        "size_bytes": 1066890090,
        "thumbnail": "https://data.1024tera.com/thumbnail/...",
        "dlink": "https://dm-d.terabox.app/file/...",
        "proxy_url": "https://terabox-downloader-api-pqxy.onrender.com/proxy?url=...",
        "fs_id": "901464181182712"
      }
    ]
  }
}
```

### `GET /proxy?url=<encoded_dlink>`
Streams the file through your server with cookies attached automatically. Just open the `proxy_url` in a browser or pass it to a download manager — no Terabox account needed on the client side.

### `GET /docs`
Returns API documentation as Markdown.

---

## Supported Domains

Works with any share URL from these domains:

```
terabox.com         1024terabox.com     teraboxapp.com
terasharefile.com   nephobox.com        4funbox.co
mirrobox.com        momerybox.com       freeterabox.com
teraboxlink.com     terafileshare.com   teraboxshare.com
terabox1.com        terabox2.com        1024tera.com
terabox.app
```

---

## Setup

### Prerequisites
- Python 3.8+
- A free Terabox account

### Get your `ndus` cookie

1. Open [1024terabox.com](https://1024terabox.com) in Chrome/Edge and log in
2. Press `F12` → **Application** tab → **Cookies** → `https://www.1024terabox.com`
3. Copy the value of the `ndus` cookie

> **Tip:** Use a dedicated/throwaway Terabox account for the cookie — keeps your main account safe.

### Local development

```bash
git clone https://github.com/MeherMankar/terabox-downloader-api
cd terabox-downloader-api
pip install -r requirements.txt
```

Create a `.env` file:
```env
# Single account
TERABOX_COOKIE=ndus=YOUR_NDUS_VALUE_HERE

# Multiple accounts (picked randomly per request)
TERABOX_COOKIE=ndus=ACCOUNT1_VALUE,ndus=ACCOUNT2_VALUE,ndus=ACCOUNT3_VALUE
```

Run the server:
```bash
python api/index.py
```

API is now at `http://localhost:5000`.

---

## Download a file

Use the included `download.py` script — talks directly to the deployed API:

```bash
python download.py "https://1024terabox.com/s/YOUR_SHARE_ID"
```

Output:
```
Fetching info for: https://1024terabox.com/s/...
Found 1 file(s):
  [0] video.mp4  (1017.47 MB)
Downloading: video.mp4
  512.0 MB / 1017.5 MB  (50.3%)
Done! Saved as: video.mp4
```

---

## Deploy to Render

1. Fork / push this repo to GitHub

2. Go to [render.com](https://render.com) → **New** → **Web Service** → connect your repo

3. Render auto-detects the `Dockerfile`. Confirm:
   | Setting | Value |
   |---------|-------|
   | Environment | `Docker` |
   | Port | `8000` |

4. Add environment variable:
   | Name | Value |
   |------|-------|
   | `TERABOX_COOKIE` | `ndus=VALUE1,ndus=VALUE2,ndus=VALUE3` |

5. Click **Deploy**

---

## Deploy to Vercel

1. Push this repo to GitHub

2. Go to [vercel.com](https://vercel.com) → **New Project** → import your repo

3. Add environment variable:
   | Name | Value |
   |------|-------|
   | `TERABOX_COOKIE` | `ndus=VALUE1,ndus=VALUE2,ndus=VALUE3` |

4. Click **Deploy**

> The `ndus` cookie expires after several months. When the API starts returning errors, just grab a fresh one from your browser.

---

## Usage Examples

### Python
```python
import requests

r = requests.post("https://terabox-downloader-api-pqxy.onrender.com/download",
    json={"url": "https://1024terabox.com/s/1bwf-DxcMG6LU6lnPz4VlaA"})

data = r.json()["data"]
print(data["title"])             # The Amazing Spider-Man (2012).mp4
print(data["files"][0]["dlink"]) # direct CDN link
print(data["files"][0]["proxy_url"]) # open this in browser to download
```

### JavaScript
```js
const res = await fetch("https://terabox-downloader-api-pqxy.onrender.com/download", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ url: "https://1024terabox.com/s/1bwf-DxcMG6LU6lnPz4VlaA" })
});

const { data } = await res.json();
window.open(data.files[0].proxy_url); // opens download in browser
```

### PowerShell
```powershell
$r = Invoke-RestMethod -Uri "https://terabox-downloader-api-pqxy.onrender.com/download" `
     -Method POST -ContentType "application/json" `
     -Body '{"url": "https://1024terabox.com/s/YOUR_SHARE_ID"}'

# Open proxy_url in browser to download
Start-Process $r.data.files[0].proxy_url
```

---

## Project Structure

```
terabox-downloader-api/
├── api/
│   └── index.py        # Flask app — all API logic
├── download.py         # CLI download script
├── docs.md             # API docs (served at /docs)
├── Dockerfile          # For Render / Docker deploys
├── render.yaml         # Render deployment config
├── vercel.json         # Vercel deployment config
├── requirements.txt
└── .env                # Local env vars (not committed)
```

---

## License

[MIT](LICENSE)

---

## Credits

| Role | Credit |
|------|--------|
| Maintainer | [MeherMankar](https://github.com/MeherMankar) · [Telegram](https://t.me/MeherPatil) |
| Base project | [genxnano](https://t.me/genxnano) · [Mrlabani/terabox-downloader-api](https://github.com/Mrlabani/terabox-downloader-api) |
| WAP bypass technique | [FZBypassBot](https://github.com/rjriajul/FZBypassBot) |

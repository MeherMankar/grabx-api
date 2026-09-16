# Terabox Downloader API

[![GitHub](https://img.shields.io/badge/GitHub-MeherMankar%2Fterabox--downloader--api-blue?logo=github)](https://github.com/MeherMankar/terabox-downloader-api)

A self-hosted Flask API that extracts direct download links from any Terabox share URL — no third-party services, no CAPTCHA, deployed in seconds on Vercel.

> **How it works:** Uses the Terabox WAP (mobile) page which embeds file metadata including signed download links directly in its HTML — bypassing the verify_v2 gate entirely.

**Maintained by:** [MeherMankar](https://github.com/MeherMankar) · [Telegram](https://t.me/MeherPatil)
**Base project by:** [genxnano](https://t.me/genxnano) · [Original repo](https://github.com/Mrlabani/terabox-downloader-api)

---

## Features

- Direct download links from any Terabox share URL
- Works with all Terabox domain variants (`1024terabox.com`, `teraboxapp.com`, `nephobox.com`, etc.)
- Built-in `/proxy` endpoint — stream files through your server (no client-side cookie needed)
- Returns filename, size, thumbnail, and `fs_id`
- Vercel-ready with `@vercel/python` — zero config deploy
- Cookie-based auth via a single env variable

---

## Endpoints

### `GET /`
Health check and endpoint listing.

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
        "proxy_url": "https://your-api.vercel.app/proxy?url=...",
        "fs_id": "901464181182712"
      }
    ],
    "note": "Use 'dlink' with a download manager or 'proxy_url' to stream through this server."
  }
}
```

### `GET /proxy?url=<encoded_dlink>`
Proxy-streams a Terabox dlink through your server with cookies attached automatically. Use this when you don't want to handle cookies on the client side.

### `GET /docs`
Returns the API documentation as Markdown.

---

## Setup

### Prerequisites
- Python 3.8+
- A Terabox account (free tier works)

### Get your `ndus` cookie

1. Open [1024terabox.com](https://1024terabox.com) in Chrome/Edge and log in
2. Press `F12` → **Application** → **Cookies** → `https://www.1024terabox.com`
3. Copy the value of the `ndus` cookie

### Local development

```bash
git clone https://github.com/MeherMankar/terabox-downloader-api
cd terabox-downloader-api
pip install -r requirements.txt
```

Create a `.env` file:
```
TERABOX_COOKIE=ndus=YOUR_NDUS_VALUE_HERE
```

Run the server:
```bash
python api/index.py
```

API is now at `http://localhost:5000`.

### Test it

```bash
# PowerShell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/download" -Method POST `
  -ContentType "application/json" `
  -Body '{"url": "https://1024terabox.com/s/YOUR_SHARE_ID"}'
```

```bash
# curl
curl -X POST http://127.0.0.1:5000/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://1024terabox.com/s/YOUR_SHARE_ID"}'
```

---

## Download a file

Use the included `download.py` script:

```bash
python download.py "https://1024terabox.com/s/YOUR_SHARE_ID"
```

It calls your local API, gets the direct link, and downloads the file with a progress bar.

---

## Deploy to Vercel

1. Push this repo to GitHub

2. Go to [vercel.com](https://vercel.com) → **New Project** → import your repo

3. Add environment variable:
   | Name | Value |
   |------|-------|
   | `TERABOX_COOKIE` | `ndus=YOUR_NDUS_VALUE_HERE` |

4. Click **Deploy**

Your API will be live at `https://your-project.vercel.app`.

> The `ndus` cookie is tied to your Terabox session. It expires after several months. Refresh it when the API starts returning errors.

---

## Usage examples

### Python
```python
import requests

r = requests.post("https://your-api.vercel.app/download", json={
    "url": "https://1024terabox.com/s/1bwf-DxcMG6LU6lnPz4VlaA"
})

data = r.json()["data"]
print(data["title"])
print(data["files"][0]["dlink"])
print(data["files"][0]["proxy_url"])
```

### JavaScript / fetch
```js
const res = await fetch("https://your-api.vercel.app/download", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ url: "https://1024terabox.com/s/1bwf-DxcMG6LU6lnPz4VlaA" })
});

const { data } = await res.json();
console.log(data.files[0].dlink);
```

---

## Supported URL formats

```
https://terabox.com/s/1ABC...
https://1024terabox.com/s/1ABC...
https://teraboxapp.com/s/1ABC...
https://nephobox.com/s/1ABC...
https://4funbox.co/s/1ABC...
https://mirrobox.com/s/1ABC...
https://terabox.com/sharing/link?surl=ABC...
```

---

## Project structure

```
terabox-downloader-api/
├── api/
│   └── index.py        # Flask app — all API logic
├── download.py         # CLI download script
├── docs.md             # API docs (served at /docs)
├── requirements.txt
├── vercel.json         # Vercel deployment config
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

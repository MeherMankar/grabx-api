# GrabX API — Documentation

**Base URL:** `https://grabx-api.onrender.com`  
**GitHub:** [MeherMankar/grabx-api](https://github.com/MeherMankar/grabx-api)

---

## Quick Start

```bash
# 1. Call any download endpoint with your API key
curl -X POST https://grabx-api.onrender.com/ph/download \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.pornhub.com/view_video.php?viewkey=abc123"}'

# 2. Open watch_url in a browser — no key needed
# 3. Use best_proxy_url / proxy_url fields to stream/download — no key needed (signed)
```

---

## Authentication

Set `API_KEY` (or `GRABX_API_KEY`) as an environment variable to protect the API.  
For production deployments, keep this enabled; without it, download/proxy routes are rejected.

### Passing the key

| Method | Example |
|--------|---------|
| Header | `X-API-Key: your_key` |
| Header | `Authorization: Bearer your_key` |
| Query  | `?api_key=your_key` |

### Public routes (never need a key)

| Route | Description |
|-------|-------------|
| `GET /` | API info |
| `GET /health` | Health check |
| `GET /docs` | This documentation |
| `GET /ph/watch/<viewkey>` | PH video player |
| `GET /xv/watch?url=` | Xvideos player |
| `GET /xnxx/watch?url=` | XNXX player |
| `GET /xh/watch?url=` | XHamster player |
| `GET /jav/watch?url=` | JAVtiful player |
| `GET /redtube/watch?url=` | RedTube player |
| `GET /youporn/watch?url=` | YouPorn player |
| `GET /eporner/watch?url=` | Eporner player |
| `GET /spankbang/watch?url=` | SpankBang player |
| `GET /porntrex/watch?url=` | PornTrex player |
| All `/proxy`, `/ph/proxy`, `/adult/proxy`, `/jav/proxy` | CDN proxies (token-auth, see below) |

### Signed proxy tokens

When you call a download endpoint with your key, **all returned proxy/stream URLs are pre-signed** with a short-lived HMAC token. Anyone holding those URLs can stream/download without a key — no key leakage in browser URLs.

Token TTL: **24 hours** (configurable via `PROXY_TOKEN_TTL_HOURS`).

Download and proxy APIs require `API_KEY`. Public health/docs/watch routes remain
available without it. Requests are rate-limited per API key or client IP (60/minute
by default; signed stream proxy requests have a 1,200/minute ceiling to avoid
interrupting segmented playback). Authenticated administrators can inspect recent limit violations at
`GET /admin/abuse` and clear the extraction cache with `POST /admin/cache/clear`.

---

## Endpoints

### `GET /`
API info — lists all endpoints, auth mode, proxy backend, Terabox account count.

---

### `GET /health`
Health check. Returns Python version, platform, auth status, proxy backend.

---

## Terabox

### `POST /download`
Extract direct download links from any Terabox share URL.  
Supports all Terabox mirror domains, recursive folder traversal, and video quality metadata.

**Request**
```json
{ "url": "https://1024terabox.com/s/YOUR_SHARE_ID" }
```

**Response**
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
        "folder": "/MyFolder",
        "size": "1.05 GB",
        "size_bytes": 1127428915,
        "thumbnail": "https://...",
        "dlink": "https://d.terabox.com/...",
        "proxy_url": "https://grabx-api.onrender.com/proxy?url=...&_t=...&_e=...",
        "fs_id": "123456789",
        "video_quality": {
          "resolution": "1920x1080", "label": "1080p",
          "duration": "12:34", "fps": 30.0, "bitrate_kbps": 4200.0
        }
      }
    ]
  }
}
```

> `proxy_url` streams the file through the server (cookie attached).  
> `dlink` is the raw CDN link — requires `ndus` cookie in the browser.  
> `video_quality` is only present on video files where Terabox exposes metadata.

**Supported domains:** terabox.com, 1024terabox.com, nephobox.com, 4funbox.co, mirrobox.com, momerybox.com, tibibox.com, dubox.com, freeterabox.com, and all their variants.

---

### `GET /proxy?url=<encoded_dlink>`
Stream/download a Terabox file through the server.  
The `ndus` cookie is attached automatically — no Terabox account needed on the client.

> Use `proxy_url` from `/download` response — it already includes the signed token.

---

## PornHub

### `POST /ph/download`
Extract stream/download links from a PornHub video.

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
    "best_format": "mp4",
    "qualities": [
      {
        "quality": "1080", "format": "mp4",
        "proxy_url": "https://grabx-api.onrender.com/ph/proxy?url=...&_t=...&_e=...&vk=...&q=1080",
        "download_url": "https://grabx-api.onrender.com/ph/proxy?url=...&dl=1&_t=..."
      }
    ],
    "best_proxy_url": "...",
    "best_download_url": "..."
  }
}
```

**Supported:** pornhub.com, pornhubpremium.com, pornhub.net, pornhub.org, all language subdomains (cn/de/fr/…), thumbzilla.com.

---

### `GET /ph/watch/<viewkey>`
Browser video player. Always public. HLS.js enabled for HLS streams.

```
https://grabx-api.onrender.com/ph/watch/6a165f5d3a96c
```

---

### `GET /ph/proxy?url=<encoded>&_t=<token>&_e=<expiry>`
Proxy PH CDN streams. MP4s stream through server; HLS manifests are rewritten so all segments go through this proxy too. Auto-refreshes expired CDN links using the embedded `vk=` viewkey.

---

## Xvideos

### `POST /xv/download`
Extract stream/download links from an Xvideos video.

**Request**
```json
{ "url": "https://www.xvideos.com/video.abc123/..." }
```

**Response** — same shape as `/ph/download` with `qualities` array (360p MP4, 480p MP4, HLS variants).

---

### `GET /xv/watch?url=<video_url>`
Browser video player. Always public.

---

## XNXX

### `POST /xnxx/download`
Extract stream/download links from an XNXX video.

**Request**
```json
{ "url": "https://www.xnxx.com/video-abc123/..." }
```

Same response shape as Xvideos.

---

### `GET /xnxx/watch?url=<video_url>`
Browser video player. Always public.

---

## XHamster

### `POST /xh/download`
Extract stream/download links from an XHamster video.

**Request**
```json
{ "url": "https://xhamster.com/videos/video-slug-xhABCDEF" }
```

**Response** — up to 5 MP4 qualities (144p → 720p) + HLS.  
XHamster encrypts source URLs; GrabX decrypts them using the PRNG algorithm from the XHamster player (matches yt-dlp's XHamster extractor). Returns error if XHamster updates their algorithm.

`best_download_url` downloads the best available HLS quality as a playable `.ts` file. The browser player exposes 1080p/720p/480p/240p/144p HLS qualities when advertised by XHamster, and its **Download HLS** button streams the selected HLS segments as a file. This avoids XHamster's IP-bound MP4 endpoint and does not depend on a configured CDN worker URL.

**Supported domains:** xhamster.com, xhamster.desi, xhamster.one, xhamster.xxx, xhamster.net, and language subdomains.

---

### `GET /xh/watch?url=<video_url>`
Browser video player with proxied HLS playback, selectable qualities, and a working HLS download button. Always public.

---

## JAVtiful

### `POST /jav/download`
Extract stream/download links from a JAVtiful video.

**Request**
```json
{ "url": "https://javtiful.com/video/113886/hmn-904-reducing-mosaic" }
```

**Response**
```json
{
  "status": "success",
  "data": {
    "title": "HMN-904 ...",
    "thumbnail": "https://...",
    "duration": "1:58:22",
    "duration_seconds": 7102,
    "watch_url": "https://grabx-api.onrender.com/jav/watch?url=...",
    "qualities": [
      {
        "quality": "720", "format": "mp4",
        "proxy_url": "https://grabx-api.onrender.com/jav/proxy?url=...&_t=...&_e=...",
        "download_url": "..."
      }
    ],
    "best_proxy_url": "...",
    "best_download_url": "..."
  }
}
```

---

### `GET /jav/watch?url=<video_url>`
Browser video player. Always public.

---

### `GET /jav/proxy?url=<encoded>`
Streams JAVtiful CDN content (fast-stream.jav.si) with correct Referer.

---

## yt-dlp Generic Extractor

### `POST /yt/download`
Extract stream/download links from **any yt-dlp supported URL**.  
Works with PornHub, Xvideos, XHamster, Twitter/X, Reddit, Twitch clips, Dailymotion, Vimeo, and [1000+ more sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

**Request**
```json
{ "url": "https://www.pornhub.org/view_video.php?viewkey=abc123" }
```

**Response** — same shape as `/ph/download` with `qualities` array.

> Results are cached for 2 hours (configurable via `CACHE_TTL_SECONDS`).

---

### `GET /yt/watch?url=<video_url>`
Browser video player for any yt-dlp supported URL. Always public.

---

## Async Jobs

`POST /jobs` accepts `{"url": "https://..."}` and returns HTTP `202` with a
`job_id` and `status_url`. Poll `GET /jobs/<job_id>` for `queued`, `started`,
`finished`, or `failed`; completed jobs contain the standard extraction result.
Successful results are retained for one hour and failures for one day.

Run at least one RQ worker alongside the web service:

```bash
rq worker grabx
```

## Additional adult sites

Each download route accepts `{"url": "..."}` and returns the standard qualities
response. A public `/watch?url=...` player is available for each site. Extraction
uses yt-dlp and therefore depends on support in the installed yt-dlp release.

| Site | Download endpoint | Accepted hosts |
|------|-------------------|----------------|
| RedTube | `POST /redtube/download` | `redtube.com`, `redtube.xxx` |
| YouPorn | `POST /youporn/download` | `youporn.com`, `.net`, `.org`, `.xxx` |
| Eporner | `POST /eporner/download` | `eporner.com`, `eporner.net` |
| SpankBang | `POST /spankbang/download` | `spankbang.com` |
| PornTrex | `POST /porntrex/download` | `porntrex.com` |

`/yt/download` remains the generic fallback for other yt-dlp-supported sites.

### `GET /adult/proxy?url=<encoded>`
Unified CDN proxy for Xvideos, XNXX, and XHamster streams.  
Automatically detects the correct `Referer` from the CDN hostname.  
Routes through Cloudflare Worker when `CF_WORKER_URL` is configured (zero Render bandwidth).

---

## Error Responses

All errors follow this shape:

```json
{ "status": "error", "message": "Human-readable description" }
```

| HTTP | Meaning |
|------|---------|
| 400  | Bad request — invalid URL, missing field, unsupported site |
| 401  | Missing API key |
| 403  | Invalid API key, or expired/tampered proxy token |
| 404  | No streams found (premium/private/deleted video) |
| 422  | Video exists but streams cannot be extracted |
| 500  | Unexpected server error |
| 502  | Upstream network error (CDN or source site unreachable) |

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TERABOX_COOKIE` | Yes (Terabox) | — | `ndus=VALUE` or `ndus=V1,ndus=V2` for multi-account |
| `API_KEY` | Yes (protected routes) | — | Protect download/proxy/job endpoints; must be set in production |
| `GRABX_API_KEY` | No | — | Alias for `API_KEY` (used by some bots) |
| `CF_WORKER_URL` | No | — | Cloudflare Worker URL — routes PH/Xvideos/XHamster/JAV streams there (zero Render bandwidth) |
| `PROXY_URL` | No | — | Comma-separated residential proxy pool. Format: `host:port:user:pass` or `http://user:pass@host:port`. Used by PH, XHamster, Xvideos, yt-dlp to bypass datacenter IP blocks |
| `PROXY_TOKEN_TTL_HOURS` | No | `24` | How long signed proxy URLs remain valid |
| `CACHE_TTL_SECONDS` | No | `7200` | Extraction-result cache duration (default 2 hours) |
| `CACHE_TTL_<SITE>_SECONDS` | No | global TTL | Optional site-specific override, e.g. `CACHE_TTL_PH_SECONDS` or `CACHE_TTL_YTDLP_SECONDS` |
| `REDIS_URL` | Production | — | Redis URL shared by web app and RQ workers; required in production |
| `API_RATE_LIMIT_PER_MINUTE` | No | `60` | Request limit per API key or client IP |
| `PROXY_RATE_LIMIT_PER_MINUTE` | No | `1200` | Per-client limit for stream proxy requests |
| `APP_ENV` | Production | — | Set to `production` to require and ping Redis at startup |
| `PORT` | No | `5000` | Port to listen on |
| `FLASK_DEBUG` | No | `false` | Enable Flask debug mode |
| `DEBUG_HEADERS` | No | `false` | Enable `/debug/headers` endpoint |

---

## Project Structure

```
grabx-api/
├── api/
│   ├── index.py              # Flask app, auth middleware, core routes (~165 lines)
│   ├── utils.py              # Shared: token signing, proxy URL builder, watch-page renderer
│   ├── jobs.py               # Redis/RQ background extraction jobs
│   └── extractors/
│       ├── terabox.py        # Terabox helpers + /download + /proxy routes
│       ├── pornhub.py        # PornHub helpers + /ph/* routes
│       ├── javtiful.py       # JAVtiful helpers + /jav/* routes
│       ├── xvideos.py        # Xvideos + XNXX helpers + routes + /adult/proxy
│       ├── xhamster.py       # XHamster decrypt + /xh/* routes
│       ├── adult_sites.py    # Registry-backed additional yt-dlp site routes
│       └── registry.py       # Shared site adapter contract and registry
├── worker/
│   ├── worker.js             # Cloudflare Worker — proxies CDN streams
│   └── wrangler.toml         # CF Worker config
├── Dockerfile
├── render.yaml
├── requirements.txt
└── docs.md                   # This file
```

---

## Deploying to Render

1. Push to GitHub
2. Render → **New Web Service** → connect repo → **Docker** environment
3. Add environment variables:

```
TERABOX_COOKIE = ndus=VALUE1,ndus=VALUE2
API_KEY        = your_secret_key
REDIS_URL      = redis://...
APP_ENV        = production
CF_WORKER_URL  = https://grabx-api.yourname.workers.dev  (optional)
```

4. Run `rq worker grabx` as a separate background worker with the same
   `REDIS_URL` and `API_KEY` (and `PROXY_URL` if required for site access).
5. Deploy. Port 8000 is used by gunicorn (set in Dockerfile).

---

## Deploying the Cloudflare Worker

```bash
cd worker
npm install
npx wrangler login
npx wrangler secret put API_KEY   # same value as on Render
npx wrangler deploy
```

Then set `CF_WORKER_URL=https://grabx-api.yourname.workers.dev` on Render.  
All PH, Xvideos, XNXX, XHamster, and JAVtiful streams will route through CF — zero Render bandwidth for streaming.

---

## Local Development

```bash
git clone https://github.com/MeherMankar/grabx-api
cd grabx-api
pip install -r requirements.txt
```

`.env`:
```env
TERABOX_COOKIE=ndus=YOUR_NDUS_VALUE
API_KEY=devkey
# Optional locally; required for async jobs and recommended for shared cache.
# REDIS_URL=redis://localhost:6379/0
```

```bash
python api/index.py
# or
python -m flask --app api.index run
```

API at `http://localhost:5000`.

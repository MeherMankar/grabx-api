# Terabox + PornHub Downloader API Documentation

## Overview

A Flask-based API that provides two services:

1. **Terabox** — bypasses the CAPTCHA gate using the WAP page trick to return direct download links and file metadata.
2. **PornHub** — scrapes a video watch page to extract all available quality streams (MP4 and HLS) without requiring an account.

---

## Endpoints

### `GET /`

Returns API status and a summary of all available endpoints.

**Response:**
```json
{
  "status": "active",
  "message": "Terabox + PornHub Downloader API",
  "creator": "...",
  "accounts_configured": 2,
  "endpoints": { "...": "..." }
}
```

---

### `POST /download`

Get direct download link(s) for a Terabox share URL.

**Request Body:**
```json
{
  "url": "https://www.terabox.com/s/1AbCdEfGhIjK"
}
```

**Response (success):**
```json
{
  "status": "success",
  "data": {
    "title": "video.mp4",
    "files": [
      {
        "filename": "video.mp4",
        "size_bytes": 104857600,
        "size": "100.00 MB",
        "thumbnail": "https://...",
        "dlink": "https://...",
        "proxy_url": "https://your-api/proxy?url=...",
        "fs_id": "123456789"
      }
    ],
    "download_available": true,
    "note": "Use 'dlink' with a download manager (needs Terabox ndus cookie), or use 'proxy_url' to stream through this server."
  }
}
```

**Response (error):**
```json
{
  "status": "error",
  "message": "Cannot extract surl from URL: ..."
}
```

**Notes:**
- Requires `TERABOX_COOKIE` environment variable containing one or more `ndus` values.
- Single account: `ndus=YOUR_VALUE`
- Multiple accounts: `ndus=VALUE1,ndus=VALUE2`

---

### `GET /proxy`

Proxy-stream a Terabox `dlink` through this server. Useful when the client cannot set the required Terabox cookies directly.

**Query Parameters:**

| Param | Required | Description              |
|-------|----------|--------------------------|
| `url` | Yes      | URL-encoded Terabox dlink |

**Example:**
```
GET /proxy?url=https%3A%2F%2Fd.terabox.com%2F...
```

**Response:** Binary file stream with `Content-Disposition: attachment` header.

---

### `POST /ph/download`

Extract all available video stream/download links from a PornHub watch page.

**Request Body:**
```json
{
  "url": "https://www.pornhub.com/view_video.php?viewkey=ph..."
}
```

Supported URL formats:
- `https://www.pornhub.com/view_video.php?viewkey=ph...`
- Regional subdomains: `de.pornhub.com`, `fr.pornhub.com`, etc.
- Thumbzilla: `https://www.thumbzilla.com/video/ph.../...`

**Response (success):**
```json
{
  "status": "success",
  "data": {
    "title": "Video Title",
    "thumbnail": "https://...",
    "duration": "12:34",
    "duration_seconds": 754,
    "viewkey": "ph1234567890",
    "qualities": [
      { "quality": "1080", "url": "https://...1080P....mp4", "format": "mp4" },
      { "quality": "720",  "url": "https://...720P....mp4",  "format": "mp4" },
      { "quality": "480",  "url": "https://...480P....mp4",  "format": "mp4" },
      { "quality": "240",  "url": "https://...240P....mp4",  "format": "mp4" },
      { "quality": "hls",  "url": "https://.../master.m3u8", "format": "hls" }
    ],
    "best_url": "https://...1080P....mp4",
    "note": "Use 'best_url' for the highest quality MP4, or pick a specific quality from 'qualities'. HLS streams (.m3u8) require a player that supports HLS."
  }
}
```

**Response (error):**
```json
{
  "status": "error",
  "message": "Not a supported PornHub URL (host: 'example.com')."
}
```

**Possible error conditions:**

| HTTP | Reason |
|------|--------|
| 400  | Invalid or non-PH URL, age-gate hit, page structure changed |
| 404  | No downloadable streams found (premium-only or private video) |
| 502  | Network error reaching PornHub |

**Notes:**
- No account or cookies required for public videos — an age-gate bypass cookie is set automatically.
- Premium/private videos require the user to be logged in and are not supported.
- `qualities` are sorted best-first (highest resolution MP4 first, HLS last).
- MP4 links can be used directly in `<video>` tags or download managers.
- HLS `.m3u8` links require an HLS-capable player (e.g. VLC, hls.js).

---

### `GET /docs`

Returns this documentation in Markdown format.

---

## Environment Variables

| Variable         | Required | Description |
|------------------|----------|-------------|
| `TERABOX_COOKIE` | Yes (for Terabox endpoints) | One or more Terabox `ndus` cookie values. Single: `ndus=VALUE`. Multiple: `ndus=VALUE1,ndus=VALUE2` |
| `PORT`           | No       | Port to listen on (default: `5000`) |
| `FLASK_DEBUG`    | No       | Set to `true` to enable debug mode |

---

## Running Locally

```bash
pip install -r requirements.txt
```

Create a `.env` file:
```
TERABOX_COOKIE=ndus=YOUR_NDUS_VALUE
```

Run the server:
```bash
python -m api
```

The API will be available at `http://localhost:5000`.

---

## Error Handling

All endpoints return JSON with a `status` field of either `"success"` or `"error"`. On error, a `"message"` field describes the problem. HTTP status codes follow standard conventions:

| Code | Meaning |
|------|---------|
| 200  | Success |
| 400  | Bad request (invalid input) |
| 404  | Resource not found |
| 502  | Upstream network error |
| 500  | Unexpected internal error |

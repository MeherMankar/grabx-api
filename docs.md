# Terabox Downloader API Documentation

## Overview

The Terabox Downloader API is a Flask-based API that allows users to download files from Terabox, a popular file hosting service. It utilizes an external API provider (ytshorts.savetube.me) to handle the actual Terabox download process.

## Endpoints

### `GET /`
- **Description:** Root endpoint that provides information about the API.
- **Response:**
    ```json
    {
        "status": "active",
        "message": "Terabox Downloader API",
        "creator": "Made by nano (t.me/genxnano)",
        "endpoints": {
            "/download": "POST - Download Terabox link (Required body: {'url': 'url'})",
            "/docs": "GET - Retrieve API documentation"
        }
    }
    ```

### `POST /download`
- **Description:** Download a file from Terabox.
- **Request Body:**
  ```json
  {
    "url": "https://www.terabox.com/share/link?shareid=12345&uk=abcd"
  }
  ```
- **Response (Success):**
  ```json
  {
    "status": "success",
    "data": {
      "title": "File.zip",
      "thumbnail": "thumbnail.jpg",
      "resolutions": {
        "Fast Download": "url",
        "HD Video": "url"
      }
    }
  }
  ```
- **Response (Error):**
  ```json
  {
    "status": "error",
    "message": "URL is required in request body"
  }
  ```

### `GET /docs`
- **Description:** Retrieve the API documentation.
- **Response:** The full API documentation in Markdown format.

## Error Handling

The API will return appropriate HTTP status codes and error messages in case of any issues. Possible error responses include:

- `400 Bad Request`: When the request body is missing the required `url` field.
- `500 Internal Server Error`: When an unexpected error occurs during the download process.

## Running the API

To run the API locally, follow these steps:

1. Install the required dependencies:
   ```
   pip install flask requests
   ```
2. Save the `api/index.py` file in a directory.
3. Create a `docs.md` file in the same directory with the contents of this documentation.
4. Run the API:
   ```
   python3 -m api
   ```
5. The API will be available at `http://localhost:5000/`.

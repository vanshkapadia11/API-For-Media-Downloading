# YouTube & Instagram Downloader API

A production-ready REST API built with Python & Flask for downloading videos, audio, Shorts, Reels, and images from YouTube and Instagram. Powers the backend of [VidiFlow](https://vidiflow.co).

---

## Features

- **YouTube** — Video (up to 4K), MP3 audio extraction, Shorts
- **Instagram** — Reels, videos, and images
- **Multi-client fallback** — Tries `ios → android → web_embedded → web` for maximum reliability
- **Cookie support** — Via local file or environment variable
- **ffmpeg integration** — Auto-detected via `imageio-ffmpeg`
- **Docker ready** — Includes `Dockerfile` and `render.yaml` for one-click deploy
- **API secret auth** — Optional token-based protection
- **Auto cleanup** — Temp files deleted after each request

---

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Health check & status |
| `POST` | `/youtube/info` | Fetch video metadata & available formats |
| `POST` | `/youtube/video` | Download YouTube video (choose quality) |
| `POST` | `/youtube/audio` | Download YouTube audio as MP3 |
| `POST` | `/youtube/shorts` | Download YouTube Shorts |
| `POST` | `/instagram/info` | Fetch Instagram post metadata |
| `POST` | `/instagram/video` | Download Instagram Reel / video |
| `POST` | `/instagram/image` | Download Instagram image |
| `POST` | `/youtube/debug` | Debug all yt-dlp clients for a URL |

---

## Getting Started

### 1. Clone the repo

```bash
git clone https://github.com/vanshkapadia11/API-For-Youtube-And-Instagram-Downloader.git
cd API-For-Youtube-And-Instagram-Downloader
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run locally

```bash
python main.py
```

Server starts at `http://localhost:5000`

---

## Docker

```bash
docker build -t vidiflow-api .
docker run -p 5000:5000 vidiflow-api
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `API_SECRET` | Optional. If set, all requests must include `x-api-secret` header |
| `YOUTUBE_COOKIES` | Netscape-format YouTube cookies (for age-restricted / bot-check bypass) |
| `INSTAGRAM_COOKIES` | Netscape-format Instagram cookies (for private content) |
| `YTDLP_PROXY` | Optional proxy URL |
| `PORT` | Server port (default: `5000`) |

---

## Usage Examples

### Get YouTube video info

```bash
curl -X POST http://localhost:5000/youtube/info \
  -H "Content-Type: application/json" \
  -H "x-api-secret: YOUR_SECRET" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

### Download YouTube audio as MP3

```bash
curl -X POST http://localhost:5000/youtube/audio \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}' \
  --output audio.mp3
```

### Download YouTube video at 1080p

```bash
curl -X POST http://localhost:5000/youtube/video \
  -H "Content-Type: application/json" \
  -d '{"url": "https://youtu.be/...", "quality": "1080p"}' \
  --output video.mp4
```

### Download Instagram Reel

```bash
curl -X POST http://localhost:5000/instagram/video \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.instagram.com/reel/..."}' \
  --output reel.mp4
```

---

## Supported Quality Options

`144p` · `240p` · `360p` · `480p` · `720p` · `1080p` · `1440p` · `2160p`

---

## Tech Stack

| Layer | Tech |
|-------|------|
| Language | Python 3 |
| Framework | Flask |
| Downloader | yt-dlp |
| Media Processing | ffmpeg via imageio-ffmpeg |
| Containerization | Docker |
| Deployment | Render |

---

## Deploy to Render

This repo includes a `render.yaml` — just connect it to your Render account and deploy in one click.

Set your environment variables (`API_SECRET`, `YOUTUBE_COOKIES`, etc.) in the Render dashboard.

---

## Cookie Setup (for YouTube bot-check bypass)

Export your cookies from a logged-in browser using a browser extension like **Get cookies.txt LOCALLY**, then either:

- Place the file as `youtube_cookies.txt` in the project root, **or**
- Set the contents as the `YOUTUBE_COOKIES` environment variable

> Cookies expire over time. Re-export if you start getting bot-check errors.

---

## License

MIT © [Vansh Kapadia](https://github.com/vanshkapadia11)

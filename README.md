# Telegram TikTok Downloader Bot (Python + yt-dlp)

A ready-to-deploy Telegram bot that downloads TikTok videos (tries no-watermark first) and sends them back to the user.

> ⚠️ **Legal/TOS note**: Only download content you have rights to. Respect TikTok’s Terms of Service and creators’ copyrights.

---

## Features
- Paste a TikTok URL → bot replies with the video
- Tries **no-watermark** stream when available (via `yt-dlp`)
- **Compression** (FFmpeg) to fit Telegram limits
- **Audio-only** (MP3) extraction
- **Rate limiting** per user
- **Admin-only** mode (restrict usage to chosen IDs)
- **Webhook** support (optional) or simple polling
- Fallback: if still too large, returns a **direct download URL**

---

## Environment
Create a `.env` (or set in hosting dashboard). You can start from `.env.example`:
```env
BOT_TOKEN=123456789:AA...your_telegram_bot_token...
ADMIN_IDS=
RATE_LIMIT_PER_MIN=5
COMPRESS_MAX_MB=45
CRF=28
MAX_HEIGHT=720
USE_WEBHOOK=false
WEBHOOK_URL=
PORT=8080
LISTEN_ADDR=0.0.0.0
WEBHOOK_SECRET=
```

> Get the token from **@BotFather**. Keep it secret—**never commit `.env`** into Git.

---

## Local Run
```bash
python -m venv .venv && source .venv/bin/activate    # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # then edit BOT_TOKEN=...
python bot.py
```

## Deploy (any PaaS)
- Push this repo to GitHub.
- On your platform (Railway/Render/Heroku/Replit/Docker), set environment variables (at least `BOT_TOKEN`).
- **Polling** (default) works anywhere. For **webhook**, set `USE_WEBHOOK=true` and a public `WEBHOOK_URL` (HTTPS).

### Docker
```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
```

---

## Safety
- Respect platform ToS and copyrights.
- Credit creators if you redistribute downloads.
- Avoid storing user data.

FROM python:3.12-slim

# Install Node.js 20 LTS (required for yt-dlp n-challenge solver)
# and ffmpeg for audio/video processing
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Verify versions
RUN node --version && ffmpeg -version | head -1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app — your file is named main.py
COPY main.py .

# Copy cookies if present (can also be set via env vars — preferred)
COPY youtube_cookies.tx[t] ./
COPY instagram_cookies.tx[t] ./

EXPOSE 10000

# 300s timeout handles large video downloads; 2 workers for concurrency
CMD ["gunicorn", "main:app", "--workers", "2", "--timeout", "300", "--bind", "0.0.0.0:10000"]
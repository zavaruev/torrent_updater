FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV DISPLAY=:99
# Chrome flags for Docker
ENV CHROME_FLAGS="--no-sandbox --disable-dev-shm-usage --disable-gpu --disable-software-rasterizer --disable-extensions --disable-background-networking --disable-background-timer-throttling --disable-renderer-backgrounding --disable-features=TranslateUI,BlinkGenPropertyTrees --disable-ipc-flooding-protection --no-zygote --single-process"

WORKDIR /app

# Install Chrome using modern method (no apt-key)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    tini \
    xvfb \
    x11-utils \
    --no-install-recommends \
    && wget -q -O /usr/share/keyrings/google-chrome.gpg https://dl-ssl.google.com/linux/linux_signing_key.pub \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy movie-recommender scripts for on-demand search/download
COPY movie-recommender /opt/data/scripts/movie-recommender

COPY . .

EXPOSE 6050

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["python", "main.py"]
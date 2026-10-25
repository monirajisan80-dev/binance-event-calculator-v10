# Playwright base image already ships Chromium + its system libraries.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

# Runs as a Render Background Worker (no port). If deployed as a Web Service,
# Render injects PORT and the bot serves a health check on / and /healthz.
CMD ["python", "-u", "main.py"]

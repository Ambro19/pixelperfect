FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Use a stable, image-baked browser location (NOT ephemeral cache)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# OS deps commonly required by Chromium on Debian slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libx11-6 libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libxrender1 \
    libxshmfence1 \
    libxkbcommon0 \
    libxcursor1 \
    libxi6 \
    libxtst6 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Install Playwright browser into the image (stable path)
RUN python -m playwright install --with-deps chromium

# Copy app code last
COPY . /app

# Render sets $PORT; default to 10000 for local runs
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]



# FROM python:3.12-slim-bookworm

# ENV PYTHONDONTWRITEBYTECODE=1   | # FROM python:3.12-slim-bookworm
# ENV PYTHONUNBUFFERED=1

# # ✅ Force Playwright browsers to install into the image (stable path)
# # This avoids /opt/render/.cache/... missing executable issues.
# ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# WORKDIR /app

# # -----------------------------------------------------------------------------
# # System dependencies
# # - Includes runtime deps for Chromium
# # - Includes curl for health/debugging (optional but useful)
# # -----------------------------------------------------------------------------
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     ca-certificates curl \
#     fonts-liberation \
#     libasound2 \
#     libatk-bridge2.0-0 libatk1.0-0 \
#     libcups2 \
#     libdbus-1-3 \
#     libdrm2 \
#     libgbm1 \
#     libglib2.0-0 \
#     libnss3 \
#     libnspr4 \
#     libx11-6 libx11-xcb1 \
#     libxcb1 \
#     libxcomposite1 \
#     libxdamage1 \
#     libxext6 \
#     libxfixes3 \
#     libxrandr2 \
#     libxrender1 \
#     libxshmfence1 \
#     libxkbcommon0 \
#     libxcursor1 \
#     libxi6 \
#     libxtst6 \
#     libpango-1.0-0 \
#     libpangocairo-1.0-0 \
#     && rm -rf /var/lib/apt/lists/*

# # -----------------------------------------------------------------------------
# # Python deps first (better layer caching)
# # -----------------------------------------------------------------------------
# COPY requirements.txt /app/requirements.txt
# RUN pip install --no-cache-dir -r /app/requirements.txt

# # -----------------------------------------------------------------------------
# # ✅ Install Playwright browsers into the image (Chromium only)
# # --with-deps is the most reliable (even if you install libs manually)
# # -----------------------------------------------------------------------------
# RUN python -m playwright install --with-deps chromium

# # Copy app after deps for better caching
# COPY . /app

# # Render provides $PORT. Default to 10000 if not set.
# CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]

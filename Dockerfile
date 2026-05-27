FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    STATE_DIR=/app/data

WORKDIR /app

# uv from official distroless image (small, fast).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Install deps first (cached unless pyproject/uv.lock change).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Install Chromium + matching OS deps via Playwright. --with-deps uses apt-get
# under the hood; runs as root which is fine in the build stage.
RUN uv run playwright install --with-deps chromium

# Copy app code and install the project itself.
COPY frontdesk_watch.py ./
RUN uv sync --frozen --no-dev

RUN mkdir -p "$STATE_DIR"

ENTRYPOINT ["uv", "run", "--no-sync", "frontdesk-watch"]

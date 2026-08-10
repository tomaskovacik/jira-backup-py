FROM python:3.14-slim

# Create a dedicated non-root user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /backup

# Install the browser to a fixed, world-readable path instead of the default
# root-owned cache dir (~/.cache/ms-playwright). playwright install --with-deps
# needs apt, so it must still run as root; appuser only needs read access to
# find the same browser at runtime via this same env var.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Install Python dependencies first for better layer caching, then Playwright's
# Chromium browser and its OS-level dependencies.
# --only-binary=:all: requires prebuilt wheels for every dependency, so no
# package's setup.py/build backend ever executes during the image build.
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt && \
    playwright install --with-deps chromium && \
    chmod -R o+rX /ms-playwright

# Copy application source
COPY backup.py wizard.py playwright_backup.py ./

# Data directory for config.yaml, playwright_cookies.json, and downloaded
# backups; mount a single external volume here. Kept separate from the
# application files above so mounting it never shadows backup.py itself.
ENV DATA_DIR=/backup/data
RUN mkdir -p /backup/data/backups && chown -R appuser:appuser /backup

USER appuser

VOLUME ["/backup/data"]

ENTRYPOINT ["python", "backup.py"]

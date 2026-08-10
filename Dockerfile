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

# Create the local backups output directory and hand it over to appuser
RUN mkdir -p /backup/backups && chown -R appuser:appuser /backup

USER appuser

# Mount your config.yaml here and optionally persist downloaded backups
VOLUME ["/backup/config.yaml", "/backup/backups"]

ENTRYPOINT ["python", "backup.py"]

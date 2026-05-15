# syntax=docker/dockerfile:1.6
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps first so subsequent code changes don't bust the layer cache.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code
COPY src/ ./src/
COPY main.py setup_role.py run_sample.py pyproject.toml ./

# OAuth tokens and SQLite live on a mounted volume, not in the image.
RUN mkdir -p /app/data /app/logs

# Non-root user for runtime
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Default: run the long-lived scheduler (inbox poll + daily GDPR purge).
# Override with `docker run ... python main.py` for a one-shot poll.
CMD ["python", "-m", "src.scheduler.runner"]

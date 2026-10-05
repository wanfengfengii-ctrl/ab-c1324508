FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DOSIMETRY_DB=/data/dosimetry.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY verify ./verify

RUN groupadd --system app && useradd --system --gid app --uid 10001 app \
    && mkdir -p /data /verify \
    && chown -R app:app /app /data /verify

USER app
VOLUME ["/data"]
EXPOSE 8080

# The slim image has no curl/wget; use the interpreter for the container
# health probe. Compose overrides this with polling-tuned settings.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=6 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status == 200 else 1)"

# Single worker is deliberate: state is a local SQLite file and its write
# locking (plus our BEGIN IMMEDIATE transactions) gives strictly serial
# commits; scale horizontally by running separate course-named volumes.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

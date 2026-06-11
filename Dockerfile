FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LIBRARIAN_API_HOST=0.0.0.0 \
    LIBRARIAN_DATA_DIR=/data \
    LIBRARIAN_DATABASE_PATH=/data/librarian.sqlite

WORKDIR /app

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

RUN adduser --disabled-password --gecos "" librarian \
    && mkdir -p /data/uploads /data/imports \
    && chown -R librarian:librarian /data

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir ".[all]"

USER librarian
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=3).read()" || exit 1

CMD ["librarian", "api", "--host", "0.0.0.0", "--port", "8080"]

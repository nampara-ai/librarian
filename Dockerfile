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

# Copy dependency-defining files first (and an optional constraints.txt if one
# was generated alongside the build context) so the dependency-install layer is
# cached independently of source edits. `constraints*.txt` uses a glob so the
# COPY does not fail when no constraints file is present.
COPY pyproject.toml README.md constraints*.txt ./
COPY src ./src

# Install with exact pins from constraints.txt when it is present (reproducible
# builds), otherwise fall back to unconstrained resolution. `.[all]` still needs
# src to build the package, but keeping pip upgrade + the install here (after the
# dependency files are in place) lets Docker reuse the layer across source-only
# changes that leave pyproject.toml/constraints.txt untouched.
RUN python -m pip install --no-cache-dir --upgrade pip \
    && if [ -f constraints.txt ]; then \
         python -m pip install --no-cache-dir -c constraints.txt ".[all]"; \
       else \
         python -m pip install --no-cache-dir ".[all]"; \
       fi

USER librarian
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=3).read()" || exit 1

CMD ["librarian", "api", "--host", "0.0.0.0", "--port", "8080"]

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LIBRARIAN_API_HOST=0.0.0.0 \
    LIBRARIAN_DATA_DIR=/data \
    LIBRARIAN_DATABASE_PATH=/data/librarian.sqlite

WORKDIR /app

RUN adduser --disabled-password --gecos "" librarian

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir ".[pdf]"

USER librarian
VOLUME ["/data"]
EXPOSE 8080

CMD ["librarian", "api", "--host", "0.0.0.0", "--port", "8080"]

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/cleaning-kb.sqlite3 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY tests_data/kb_documents.jsonl ./tests_data/kb_documents.jsonl
RUN python -m pip install --no-cache-dir . \
    && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')" \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /data \
    && chown app:app /data \
    && chmod -R a+rX /app

ARG APP_VERSION=0.1.0
ENV APP_VERSION=${APP_VERSION}
LABEL org.opencontainers.image.version=${APP_VERSION}

USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).close()"]
CMD ["python", "-m", "uvicorn", "cleaning_kb.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]

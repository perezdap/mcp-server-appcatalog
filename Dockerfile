FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    APPCATALOG_TRANSPORT=streamable-http \
    APPCATALOG_HOST=0.0.0.0 \
    APPCATALOG_PORT=8010 \
    APPCATALOG_CACHE_DIR=/data

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY src ./src

RUN uv pip install --system .

RUN mkdir -p /data

EXPOSE 8010

CMD ["appcatalog-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8010"]

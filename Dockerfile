# Build only from the allowlisted context; no runtime library or credentials enter the image.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
FROM ${PYTHON_IMAGE}
ARG UV_VERSION=0.11.16
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==${UV_VERSION}"
RUN groupadd --gid 10001 reader && useradd --uid 10001 --gid 10001 --no-create-home reader
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN UV_COMPILE_BYTECODE=1 uv sync --locked --no-dev --no-editable --python /usr/local/bin/python \
    && rm -rf /root/.cache/uv
COPY deploy/ /app/deploy/
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp ARTICLE_READER_WORKER_TTS_THREADS=2
USER 10001:10001
ENTRYPOINT ["article-reader", "--data-dir", "/data"]
CMD ["serve", "--onion", "--no-open", "--unix-socket", "/run/reader-http/http.sock", "--onion-hostname-file", "/run/reader-onion/hostname"]

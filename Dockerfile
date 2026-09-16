FROM ghcr.io/astral-sh/uv:0.8.5 AS uv
FROM python:3.12-slim-bookworm AS runtime
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client rclone libgomp1 ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
ENTRYPOINT ["/usr/bin/tini", "--", "knowledge-mcp"]
CMD ["--config", "/config/config.toml", "serve"]

FROM runtime AS semantic
RUN uv sync --frozen --no-dev --extra semantic
CMD ["--config", "/config/config.toml", "semantic-serve"]

FROM semantic AS test
RUN uv sync --frozen --extra semantic
COPY tests ./tests
ENTRYPOINT []
CMD ["uv", "run", "--frozen", "--extra", "semantic", "pytest", "-q"]

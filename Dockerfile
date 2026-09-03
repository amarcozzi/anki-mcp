FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY anki_mcp ./anki_mcp
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH" COLLECTION_DIR=/tmp/anki-mcp PORT=8080
EXPOSE 8080
CMD ["python", "-m", "anki_mcp.server"]

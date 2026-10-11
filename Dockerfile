FROM public.ecr.aws/docker/library/node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.13.0-python3.13-trixie-slim
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend/ backend/
COPY migrations/ migrations/
COPY alembic.ini ./
COPY scripts/ scripts/
COPY --from=web /web/dist frontend/dist/
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app && mkdir /app/data && chown -R app:app /app/data
USER app
ENV PATH="/app/.venv/bin:$PATH" XIAOKUA_DATA_DIR=/app/data
EXPOSE 8000
CMD ["python","-m","uvicorn","backend.service:app","--host","0.0.0.0","--port","8000","--no-access-log"]

FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project \
    && uv run --locked --no-sync playwright install --with-deps chromium

COPY . .

EXPOSE 8000

CMD ["uv", "run", "--locked", "--no-sync", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

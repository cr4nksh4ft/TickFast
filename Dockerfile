FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.8 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
ARG INSTALL_DEV=false
RUN if [ "$INSTALL_DEV" = "true" ]; then \
        uv sync --locked --no-install-project; \
    else \
        uv sync --locked --no-dev --no-install-project; \
    fi

COPY . .
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /home/app/.tickfast/credentials \
    && chmod 700 /home/app/.tickfast/credentials \
    && chown -R app:app /app /home/app/.tickfast

ENV PATH="/app/.venv/bin:$PATH"

USER app
EXPOSE 8000

CMD ["sh", "./deploy.sh"]
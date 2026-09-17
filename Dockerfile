FROM quay.io/minio/mc:latest AS minio-base
FROM python:3.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY --from=minio-base /usr/bin/mc /usr/local/bin/mc
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
COPY apps ./apps
COPY packages ./packages
COPY tools ./tools
RUN uv sync --frozen --no-dev --extra temporal --extra openstack
RUN useradd -r -u 1001 synelia && chown -R synelia /app
USER synelia
ENV PATH="/app/.venv/bin:$PATH" PORT=4000
EXPOSE 4000
ENTRYPOINT ["synelia"]
CMD ["api"]

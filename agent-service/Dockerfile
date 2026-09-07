FROM ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 AS uv
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ARG APP_VERSION=1.0.0
LABEL org.opencontainers.image.title="autoseguro-agent" \
      org.opencontainers.image.version="${APP_VERSION}"
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-cache --python /usr/local/bin/python
RUN groupadd --gid 10001 agent && useradd --uid 10001 --gid agent --no-create-home agent && mkdir /data && chown agent:agent /data && chmod 700 /data
COPY agent_service ./agent_service
USER 10001:10001
ENV PATH="/srv/.venv/bin:$PATH" SQLITE_PATH=/data/agent.sqlite3
EXPOSE 8080
CMD ["python", "-m", "agent_service"]

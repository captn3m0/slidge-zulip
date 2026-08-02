FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY bridge-xmpp/ ./bridge-xmpp/
COPY bridgelib/ ./bridgelib/

ENTRYPOINT ["uv", "run", "--frozen", "--no-dev", "python", "bridge-xmpp/zulip_xmpp_bridge.py"]

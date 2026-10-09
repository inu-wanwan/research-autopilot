# Worker image: Prefect 3.8.8 + Research Autopilot flow code baked in.
# Keep the Prefect tag in sync with uv.lock and the server image in compose.yaml.
FROM ghcr.io/astral-sh/uv:0.12.18 AS uv

FROM prefecthq/prefect:3.8.8-python3.12

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/research-autopilot/.venv

WORKDIR /opt/research-autopilot

# Dependencies first for layer caching, then the project itself.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH=/opt/research-autopilot/.venv/bin:$PATH

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA
ENV RA_GIT_SHA=$GIT_SHA

# Non-root runtime user; state dir is a named volume mounted here.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin ra \
    && mkdir -p /var/lib/research-autopilot \
    && chown ra:ra /var/lib/research-autopilot
USER ra

# The sandbox image: one container per owner, for the Docker executor.
#
# It holds Python 3.14, the SDK's three runtime dependencies and Chromium. The server sends the
# SDK itself into each container. Build it from the repository's root:
#
#   docker build -f docker/sandbox.Dockerfile -t cliquet-sandbox:latest .
FROM ghcr.io/astral-sh/uv:0.13.8-python3.14-trixie-slim

# Chromium draws the pictures, and fonts-liberation is the font it falls back to. The preview
# makes a scratch git repository for the data it is shown.
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation git \
    && rm -rf /var/lib/apt/lists/*

# App code runs as a user of its own, and owns the venv and the folders Docker mounts.
RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/sh sandbox

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages ./packages
RUN uv sync --frozen --no-dev --no-group docs --package cliquet-sdk \
    && chown -R sandbox:sandbox /app

# The volumes take over these folders, so a new sandbox can write in them. The root filesystem
# stays read-only: only /sandbox, the venv, the socket and /tmp change.
RUN mkdir -p /sandbox/sdk /sock && chown sandbox:sandbox /sandbox /sandbox/sdk /sock

ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/tmp \
    UV_CACHE_DIR=/tmp/uv-cache \
    CLIQUET_SDK_PATH=/sandbox/sdk \
    CLIQUET_CHROME_NO_SANDBOX=1

# The server sends its SDK into the container and installs it: the sandbox waits for the setup's
# last step, which names this start, so it never runs a line of the image's own copy or of an
# earlier SDK. Docker's default seccomp profile stops Chromium's own sandbox, which is why the
# setting above runs it without one.
COPY --chmod=755 docker/sandbox-start.sh /usr/local/bin/sandbox-start.sh

USER sandbox
CMD ["/usr/local/bin/sandbox-start.sh"]

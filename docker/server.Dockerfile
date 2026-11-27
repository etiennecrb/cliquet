# The server's image. Build it from the repository's root:
#
#   docker build -f docker/server.Dockerfile -t cliquet-server .
#
# uv is pinned: the server locks apps with it. The bookworm images stopped at uv 0.9.
FROM ghcr.io/astral-sh/uv:0.13.8-python3.14-trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git tzdata \
    && rm -rf /var/lib/apt/lists/*

# The server runs as a user of its own, never root: root ignores the read-only modes that keep a
# query or a view from writing. The name is a role and the id fixed, so a rename changes no volume.
# A new volume on /data, or on /sockets for the sandboxes' sockets, takes the folder's owner.
RUN useradd --uid 1000 --user-group --create-home --home-dir /home/nonroot nonroot \
    && mkdir /data /sockets \
    && chown nonroot:nonroot /data /sockets

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY examples ./examples
RUN uv sync --all-packages --frozen --no-dev --no-group docs

# uv keeps its cache and its Python in the user's home, and locks each app at start.
ENV PATH="/app/.venv/bin:$PATH" \
    HOME="/home/nonroot" \
    CLIQUET_DATA_DIR=/data

USER nonroot

EXPOSE 8080
CMD ["cliquet-server", "run", "--host", "0.0.0.0", "--port", "8080"]

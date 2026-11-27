---
icon: lucide/box
description: "Run each owner's apps in a Fly Sprite or a Docker container, and what each keeps apart."
---

# Sandboxes

Your server runs each owner's apps in a sandbox of their own, never in the server process. Choose the sandbox with `CLIQUET_EXECUTOR`: `sprites`, `docker` or `local`. [Architecture](../architecture.md#the-sandbox) describes what a sandbox keeps away from your data.

| Executor | Each owner's sandbox | For |
| --- | --- | --- |
| `sprites` | A Fly Sprite: a small virtual machine | Any server, such as app.cliquet.dev |
| `docker` | A Docker container, on the server's own host | A Linux host that holds nothing else |
| `local` | None: apps run as the server's own processes | Your own apps, on your own machine |

## Fly Sprites

Set `CLIQUET_EXECUTOR=sprites`, and give the server a Sprites organization token, `CLIQUET_SPRITES_TOKEN`, as a secret. The server can run anywhere: only its Sprites run on Fly. Each owner's Sprite is named `<prefix><login>`, with the prefix `CLIQUET_SPRITES_PREFIX`.

A Sprite keeps no data, so the server can make one again at any time. It does when an owner is new, and after each deploy that changes the SDK's code: setting one up takes about a minute, and the owner's apps are built again on it. At boot, the server sets up, four at a time, the Sprites of the owners who used Cliquet in the last `CLIQUET_WARM_DAYS` days, 7 by default, most recent first. Any other owner's Sprite is set up on their first request. Meanwhile, an app that isn't ready answers that it is starting, and its pages open by themselves once it is.

Before it writes anything to a new Sprite, the server sets two of its policies, and reads them back:

- **Network:** while it is set up, only the hosts setup needs: PyPI, uv's Python downloads, Ubuntu's mirrors, Playwright's hosts and `storage.googleapis.com`. Once the sandbox answers, nothing, DNS included.
- **Resources:** a memory limit of 8 GB, Fly's default. Reaching it restarts the Sprite.

If a policy can't be set, or reads back differently, the server doesn't use the Sprite. The server downloads the packages a version needs from PyPI into `/data/wheels/`, at most 2 GB, and sends the Sprite the ones it lacks.

The log says how long each step took, with lines such as `cliquet-server: sandbox cliquet-sam: sandbox answered after 52.3 s`.

## Docker containers

On a Linux machine with Docker, the server can give each owner a container instead. The container has no network at all: the server reaches it over a Unix socket, with the token it started it with. Build the sandbox image from the repository's root, and start the server with the executor:

```bash
docker build -f docker/sandbox.Dockerfile -t cliquet-sandbox:latest .
CLIQUET_EXECUTOR=docker CLIQUET_DATA_DIR=/srv/cliquet cliquet-server run
```

!!! warning "The server holds the Docker socket"
    The Docker socket can do anything on its machine. Run the server on a host that holds nothing else, and never publish the socket.

A container has no network, no capability and no new privilege, runs as a user of its own with a read-only system and a scratch folder in memory, and may use `CLIQUET_SANDBOX_MEMORY_MB` of memory. It shares the host's kernel, which a Sprite doesn't. Its Python environment and its app versions live in volumes of its own, `<prefix><owner>-venv` and `<prefix><owner>-root`. The containers the server makes carry the label `cliquet.sandbox=1`, and so do their volumes, except those an older server made.

A container is made the first time its owner needs it, and made again when the SDK's code or its limits change. One unused for `CLIQUET_SANDBOX_IDLE_SECONDS` is stopped, and the next call starts it again. At boot, the server removes the containers and volumes of owners it no longer has.

[Settings](settings.md#docker-containers) lists the image, the sockets' folder and the other Docker settings, with their defaults.

- **Two servers on one daemon** need different prefixes: at boot, a server removes the containers of its prefix that it doesn't know.
- **The daemon must be on the server's machine**, since a Unix socket can't cross to another one. On macOS and Windows, Docker Desktop keeps its daemon in a virtual machine, so there the server must run in a container too.
- **Keep the sockets' folder short**, such as `/srv/cliquet/sockets`: a Unix socket's path is at most 100 bytes.
- **A server in a container** keeps the sockets' folder in a Docker volume. Mount the volume at `CLIQUET_SANDBOX_SOCKETS` in the server's container, and name it in `CLIQUET_SANDBOX_SOCKETS_VOLUME`. Each sandbox then mounts only its owner's folder of it, never another owner's socket. This needs Docker Engine 26 or later: an older daemon refuses to make the container. [Run with Docker Compose](compose.md) sets it all up.

## Local, for development

With `CLIQUET_EXECUTOR=local`, apps run as processes of the server itself, on its machine. Use it only for your own apps, on your own machine. A server with a public `CLIQUET_BASE_URL` refuses to start with it, unless `CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR=1` allows a short demo of your own apps.

Without a sign-in provider, a development login signs everyone in as one login:

```text
CLIQUET_DEV_LOGIN=<login>
CLIQUET_ALLOW_REMOTE_DEV_LOGIN=1
```

On a public server, this is an open door: anyone can build as that login. Use it only for a short demo with made-up data, then delete the server and its data.

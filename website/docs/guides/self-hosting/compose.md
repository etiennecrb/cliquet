---
icon: lucide/container
description: "Run a Cliquet server with Docker Compose, on your own machine or on a server of your own."
---

# Run with Docker Compose

This guide runs a Cliquet server with Docker Compose, from a copy of the repository. The steps are the same on a laptop, a VPS or a home server. It is the easy way to try Cliquet, or to run a small server for your team.

## Two ways

One file, `docker/compose.yaml`, starts the server in one of two ways:

| Way | Command | What runs | For |
| --- | --- | --- | --- |
| Local | `docker compose up -d` | The server, at `http://127.0.0.1:8800`, with a development login for one person | Trying Cliquet on your own machine |
| Public | `docker compose --profile public up -d` | The server behind Caddy, which gives it HTTPS, and Keycloak, the sign-in provider, with its PostgreSQL database | A small server that people reach from anywhere |

Both run each owner's apps in a Docker container of their own: see [Docker containers](sandboxes.md#docker-containers).

## Requirements

- **Docker Engine 26 or later**, with Compose. Docker Desktop works for the local way.
- **Memory.** Each sandbox container may use `CLIQUET_SANDBOX_MEMORY_MB`, 2 GB by default, and the sign-in provider about 1 GB. Plan at least 4 GB for a public server with a few owners.
- **For the public way, two names** in DNS, such as `cliquet.example.com` for the server and `login.example.com` for the sign-in provider. Point both at the host, with ports 80 and 443 open, before you start: Caddy gets their certificates as it starts.

!!! warning "The server controls the host's Docker"
    The server makes the sandbox containers through the host's Docker socket. Whoever holds that socket controls Docker, which is as good as root on the host. Run the server on a host that holds nothing else. [Rootless Docker](https://docs.docker.com/engine/security/rootless/) is the safer setup: its socket controls only your own user's containers.

## Start the server

<div class="steps" markdown>

1. **Get the repository**, and go to its `docker` folder.

    ```bash
    git clone https://github.com/etiennecrb/cliquet.git
    cd cliquet/docker
    ```

2. **Write the settings.** Copy the example to `.env`, which git ignores. As it is, it runs the local way, signed in as `sam`. Its comments say what each line is for.

    ```bash
    cp .env.example .env
    ```

    `DOCKER_GID` is the group of the Docker socket, which the server joins to reach Docker:

    === "Docker Desktop"

        Keep `DOCKER_GID=0`: Docker Desktop doesn't check the group.

    === "Linux"

        Set it to the group of the socket:

        ```bash
        sed -i "s/^DOCKER_GID=.*/DOCKER_GID=$(stat -c %g /var/run/docker.sock)/" .env
        ```

    === "Rootless Docker"

        Keep `DOCKER_GID=0`, and set `DOCKER_SOCKET` to your user's socket, such as `/run/user/1000/docker.sock`.

    When an app can't start because the server may not use Docker's socket, `DOCKER_GID` is wrong: fix it as [Change a setting](#change-a-setting) shows.

3. **Build the images**, the server's and the sandbox's. It takes a few minutes the first time.

    ```bash
    docker compose build
    ```

4. **Start the server.**

    === "Local"

        `CLIQUET_DEV_LOGIN` is the login you're signed in as, `sam` in the example. Change it if you like, then start the server:

        ```bash
        docker compose up -d
        ```

        Open `http://127.0.0.1:8800/`. Only this machine can reach it. If the page says the server has no sign-in provider, `CLIQUET_DEV_LOGIN` is empty: set it, as [Change a setting](#change-a-setting) shows.

    === "Public"

        In `.env`, empty `CLIQUET_DEV_LOGIN`, set `CLIQUET_BASE_URL` to the server's address, such as `https://cliquet.example.com`, and `LOGIN_HOST` to Keycloak's name. Fill in the rest of `.env` and start the stack as [Set up Keycloak](sign-in.md#set-up-keycloak) describes, then add yourself and sign in.

5. **Connect an assistant** to `/me/mcp` at the server's address, as [Connect your agents](../../agents.md) describes.

</div>

## Change a setting

Edit `.env`, then run `up` again. Compose makes again each container whose settings changed, and keeps the data in the volumes:

```bash
docker compose up -d
```

Add `--profile public` for a public server. `docker compose restart` isn't enough: it keeps each container's old settings. To check what the server got:

```bash
docker compose exec server env | grep CLIQUET_
```

The stack sets the Docker settings itself. To give the server another setting from [Settings](settings.md), add it under the server's `environment` in `compose.yaml`, then run `up` again. On a public server, Keycloak keeps the values it imported at its first start: [Set up Keycloak](sign-in.md#set-up-keycloak) says how to change them.

## Run a command

The other guides run commands, such as `cliquet-server grant`, on the server's machine. Run each one in the server's container, which runs it as the image's user, `nonroot`:

```bash
docker compose exec server cliquet-server grant <login> build
```

`docker compose logs -f server` follows the server's log.

## Upgrade

<div class="steps" markdown>

1. **Get the new version**, and build the images again.

    ```bash
    git pull
    docker compose build
    ```

2. **Start the stack again**, with `--profile public` for a public server. Compose makes the server's container again from the new image.

    ```bash
    docker compose up -d
    ```

3. **Make the sandbox containers again**, when the update changed `docker/sandbox.Dockerfile`. The server makes a sandbox container again by itself when the SDK changes, but not when only the sandbox image does. Remove them, and the server makes each one again at its owner's next call:

    ```bash
    docker ps -aq --filter label=cliquet.sandbox=1 | xargs -r docker rm -f
    ```

    The apps and their data are in the server's volume, and each owner's environment in volumes of its own, so nothing is lost.

</div>

`docker ps --filter label=cliquet.sandbox=1` lists the sandbox containers, with when each was made.

## Back up

The server keeps its data in the volume `cliquet_data`. To back it up, stop the server, copy the volume into an archive with a throwaway container, then start the server again:

```bash
docker compose stop server
docker run --rm -v cliquet_data:/data:ro -v "$PWD":/backup alpine \
  tar -czf /backup/cliquet-data.tar.gz -C /data .
docker compose start server
```

To restore it, stop the server, unpack the archive into the volume, and start the server again:

```bash
docker compose stop server
docker run --rm -v cliquet_data:/data -v "$PWD":/backup alpine \
  tar -xzf /backup/cliquet-data.tar.gz -C /data
docker compose start server
```

On a public server, back up the sign-in provider's database too:

```bash
docker compose exec postgres pg_dump --username keycloak keycloak > keycloak.sql
```

## Remove the server

`docker compose down` stops the stack and keeps its volumes. The sandbox containers and their volumes stay too, since the server made them, not Compose. They carry the label `cliquet.sandbox=1`.

<div class="steps" markdown>

1. **Stop the server**, so that it makes no sandbox container again while you remove them.

    ```bash
    docker compose stop server
    ```

2. **Remove the sandbox containers**, then their volumes.

    ```bash
    docker ps -aq --filter label=cliquet.sandbox=1 | xargs -r docker rm -f
    docker volume ls -q --filter label=cliquet.sandbox=1 | xargs -r docker volume rm
    ```

    A volume an older server made has no label: remove it by its name, such as `cliquet-<login>-root` and `cliquet-<login>-venv`.

3. **Remove the stack**, with its volumes and the data in them. Add `--profile public` for a public server.

    ```bash
    docker compose down -v
    ```

</div>

Without `-v`, `docker compose down` keeps the stack's volumes, and `docker compose up -d` starts the server again with its data.

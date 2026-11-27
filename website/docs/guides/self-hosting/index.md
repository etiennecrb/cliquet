---
icon: lucide/server
description: "Run your own Cliquet server: what it is made of, what it needs, and the guides to deploy and run it."
---

# Self hosting

Cliquet's server is open source, and app.cliquet.dev runs the same code. You can run your own server, for yourself or for your team.

## The parts of a server

A Cliquet server is one process on one machine:

- **Its data** lives on one volume: a database, `server.db`, with the people, shares and tokens, and a git repository for each app.
- **Each owner's apps** run in a sandbox of their own, never in the server: a Fly Sprite, or a Docker container on a host of your own. See [Sandboxes](sandboxes.md).
- **People sign in** through an OpenID Connect provider. These guides use Auth0 as the example: see [Sign-in](sign-in.md).

[Cliquet's repository](https://github.com/etiennecrb/cliquet) holds two images, in `docker/`: the server's, `server.Dockerfile`, and the sandbox's, `sandbox.Dockerfile`, for Docker containers. Your deployment's configuration and secrets stay with your deployment, never in the repository.

## Requirements

- **A host** that runs the server's image on one machine, with a volume for `/data` and an HTTPS address. [Deploy on Fly](fly.md) does it on Fly, and [Run with Docker Compose](compose.md) on a machine of your own.
- **A sandbox for the owners' apps:** a Sprites organization token, from the Sprites account page, or Docker on the server's own machine.
- **A sign-in provider:** an OpenID Connect provider, such as an Auth0 tenant. For assistants to sign in, it must register MCP clients: see [Choose a provider](sign-in.md#choose-a-provider).

## Settings

The server reads its settings from environment variables whose names start with `CLIQUET_`. These guides write one as `CLIQUET_EXECUTOR=sprites`. [Settings](settings.md) lists them all, with their defaults and which are secrets.

The guides also run commands, such as `cliquet-server grant`, on the server's machine. Run each one as the image's user, `nonroot`, never as root: see [The server's user](operations.md#the-servers-user).

## Guides

<div class="grid cards" markdown>

-   :lucide-cloud-upload: **[Deploy on Fly](fly.md)**

    Deploy the server on Fly, and run its commands there.

-   :lucide-container: **[Run with Docker Compose](compose.md)**

    Run the server on your own machine, or on a server of your own.

-   :lucide-key-round: **[Sign-in](sign-in.md)**

    Set up a provider, with Auth0 as the example, and what MCP clients need.

-   :lucide-box: **[Sandboxes](sandboxes.md)**

    Sprites, Docker containers, and what each keeps apart.

-   :lucide-activity: **[Operations](operations.md)**

    Rights, limits, upgrades, account deletion and troubleshooting.

-   :lucide-sliders-horizontal: **[Settings](settings.md)**

    Every setting, with its default and whether it is a secret.

</div>

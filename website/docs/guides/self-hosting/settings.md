---
icon: lucide/sliders-horizontal
description: "Every setting a Cliquet server reads, with its default, whether it is a secret, and what it does."
---

# Settings

This page lists every setting your server reads, one table per subject. The other guides set only the ones their steps need.

The server reads each setting from an environment variable, once, when it starts. Give the server its settings the way your host does. A setting marked as a secret belongs in your host's secrets, never in a file you commit: on Fly, see [Settings and secrets](fly.md#settings-and-secrets).

## Server

| Setting | Default | Secret | What it does | See |
| --- | --- | --- | --- | --- |
| `CLIQUET_DATA_DIR` | `cliquet-data` | | The folder for the server's data: its database and each app's repository. The image sets it to `/data` | [Deploy on Fly](fly.md) |
| `CLIQUET_BASE_URL` | `http://127.0.0.1:8800` | | The address people and assistants reach the server at, with no trailing slash | [Deploy on Fly](fly.md) |
| `CLIQUET_WEBSITE_URL` | Same as `CLIQUET_BASE_URL` | | The website each MCP server names to its clients | [Access to the server](operations.md#access-to-the-server) |
| `CLIQUET_DATABASE_URL` | `sqlite:///<CLIQUET_DATA_DIR>/server.db` | | The server's database, as a SQLAlchemy address. The image includes SQLite's driver only | |
| `CLIQUET_PRIVACY_URL` | Unset | | Your privacy notice's address, which the console links to. Unset, it shows no link | [Privacy notice](operations.md#privacy-notice) |

## Sign-in

| Setting | Default | Secret | What it does | See |
| --- | --- | --- | --- | --- |
| `CLIQUET_AUTH` | Inferred | | How people sign in: `oidc`, `dev` or `none`. Unset, it is `dev` with `CLIQUET_DEV_LOGIN`, `oidc` with a `CLIQUET_OIDC_` setting, and `none` otherwise | [Sign-in](sign-in.md) |
| `CLIQUET_OIDC_ISSUER` | Unset | | The provider's issuer, exactly as its tokens name it. It uses `https://`, except on `localhost` or `127.0.0.1` | [Set up Auth0](sign-in.md#set-up-auth0) |
| `CLIQUET_OIDC_CLIENT_ID` | Unset | | The application's client ID at the provider | [Set up Auth0](sign-in.md#set-up-auth0) |
| `CLIQUET_OIDC_CLIENT_SECRET` | Unset | Yes | The application's client secret at the provider | [Set up Auth0](sign-in.md#set-up-auth0) |
| `CLIQUET_OIDC_AUDIENCE` | `<CLIQUET_BASE_URL>/me/mcp` | | The audience of the access tokens `/me/mcp` takes. Set it only when your provider can't use that URL as an API's identifier | [MCP clients](sign-in.md#mcp-clients) |
| `CLIQUET_SECRET_KEY` | Unset | Yes | At least 32 characters, which sign the session cookies. `oidc` needs it. Unset, each start makes a new one, which signs everyone out | [Set up Auth0](sign-in.md#set-up-auth0) |
| `CLIQUET_GRANT_BUILD_ON_SIGN_UP` | `0` | | `1` gives every new person the right to build as they choose their login, not only the server's first | [The right to build](operations.md#the-right-to-build) |
| `CLIQUET_SIGN_UP_PARAMS` | Unset | | What your server's `/signup` adds to the provider's sign-in request, so that it opens on its sign-up form, such as `screen_hint=signup` for Auth0. Unset, `/signup` is the same as `/login` | [Set up Auth0](sign-in.md#set-up-auth0) |
| `CLIQUET_VERIFY_EMAIL_DAYS` | 7 | | The days a person whose email isn't verified has to verify it, from when they choose their login. Then they use nothing until they do | [Unverified emails](sign-in.md#unverified-emails) |
| `CLIQUET_VERIFICATION_EMAIL_URL` | Unset | | The URL of your provider's request that sends a person their verification email again, with `{subject}` for their subject. Unset, the console offers no new email | [Send the email again](sign-in.md#send-the-email-again) |
| `CLIQUET_VERIFICATION_EMAIL_METHOD` | `POST` | | That request's method: `POST`, `PUT` or `PATCH` | [Send the email again](sign-in.md#send-the-email-again) |
| `CLIQUET_VERIFICATION_EMAIL_BODY` | Unset | | That request's JSON body, with `{subject}` for the person's subject. Unset, it has none | [Send the email again](sign-in.md#send-the-email-again) |
| `CLIQUET_VERIFICATION_EMAIL_AUDIENCE` | Unset | | The audience your server asks for its token, when the provider needs one, as Auth0 does | [Send the email again](sign-in.md#send-the-email-again) |

With `none`, nobody signs in on the web pages: only [API tokens](../../agents.md#api-tokens) work.

## Sandboxes

### Every executor

| Setting | Default | Secret | What it does | See |
| --- | --- | --- | --- | --- |
| `CLIQUET_EXECUTOR` | `local` | | Where each owner's apps run: `sprites`, `docker` or `local` | [Sandboxes](sandboxes.md) |
| `CLIQUET_WARM_DAYS` | 7 | | At boot, the server sets up the sandboxes of owners who used Cliquet in this many days | [Fly Sprites](sandboxes.md#fly-sprites) |

### Fly Sprites

| Setting | Default | Secret | What it does | See |
| --- | --- | --- | --- | --- |
| `CLIQUET_SPRITES_TOKEN` | Unset | Yes | A Sprites organization token. `sprites` needs it | [Fly Sprites](sandboxes.md#fly-sprites) |
| `CLIQUET_SPRITES_PREFIX` | `cliquet-` | | Each Sprite's name, `<prefix><login>` | [Fly Sprites](sandboxes.md#fly-sprites) |

### Docker containers

| Setting | Default | Secret | What it does | See |
| --- | --- | --- | --- | --- |
| `CLIQUET_SANDBOX_IMAGE` | `cliquet-sandbox:latest` | | The image each container is made from | [Docker containers](sandboxes.md#docker-containers) |
| `CLIQUET_SANDBOX_PREFIX` | `cliquet-` | | Each container's name, `<prefix><owner>`, and its volumes' | [Docker containers](sandboxes.md#docker-containers) |
| `CLIQUET_SANDBOX_SOCKETS` | `<CLIQUET_DATA_DIR>/sockets` | | The folder where the server reaches each container | [Docker containers](sandboxes.md#docker-containers) |
| `CLIQUET_SANDBOX_SOCKETS_HOST` | Same as `CLIQUET_SANDBOX_SOCKETS` | | That folder as the Docker daemon sees it, when the server runs in a container and the folder is on the host | [Docker containers](sandboxes.md#docker-containers) |
| `CLIQUET_SANDBOX_SOCKETS_VOLUME` | Unset | | The Docker volume that holds that folder, when the server runs in a container. Each container mounts only its owner's part. It needs Docker Engine 26 or later, and can't be used with `CLIQUET_SANDBOX_SOCKETS_HOST` | [Docker containers](sandboxes.md#docker-containers) |
| `CLIQUET_SANDBOX_IDLE_SECONDS` | 900 | | How long a container may go unused before it is stopped. 0 never stops one | [Docker containers](sandboxes.md#docker-containers) |
| `CLIQUET_SANDBOX_MEMORY_MB` | 2048 | | The memory a container may use, its builds and browser included. At least 256 | [Docker containers](sandboxes.md#docker-containers) |
| `CLIQUET_DOCKER_SOCKET` | `/var/run/docker.sock` | | The Docker daemon's socket | [Docker containers](sandboxes.md#docker-containers) |

## Limits

| Setting | Default | Secret | What it does | See |
| --- | --- | --- | --- | --- |
| `CLIQUET_CALLS_AT_ONCE` | 8 | | Calls running together, per app or person | [Limits](operations.md#limits) |
| `CLIQUET_CALLS_PER_MINUTE` | 300 | | Calls per minute, per app or person | [Limits](operations.md#limits) |
| `CLIQUET_SANDBOX_RUNS_PER_HOUR` | 300 | | Each owner's publishes, checks, previews, test runs, tried screens and screenshots, per hour | [Limits](operations.md#limits) |
| `CLIQUET_APPS_PER_OWNER` | 50 | | The apps one owner can have | [Limits](operations.md#limits) |
| `CLIQUET_SANDBOX_MINUTES_PER_OWNER_MONTH` | Unset | | The minutes each owner's sandbox may be busy in a calendar month, for an owner without the right plus. Unset, there is no budget | [Sandbox time](operations.md#sandbox-time) |
| `CLIQUET_SANDBOX_MINUTES_PER_PLUS_OWNER_MONTH` | Unset | | The same for a Plus owner, an owner with the right plus. It needs the setting above, and can't be lower. Unset, Plus owners have the free budget | [Sandbox time](operations.md#sandbox-time) |
| `CLIQUET_SANDBOX_MINUTES_PER_MONTH` | Unset | | The minutes all the sandboxes together may be busy in a calendar month. Unset, there is no ceiling | [Sandbox time](operations.md#sandbox-time) |

## Development

| Setting | Default | Secret | What it does | See |
| --- | --- | --- | --- | --- |
| `CLIQUET_DEV_LOGIN` | Unset | | Signs everyone in as this one login, with no provider. It needs a local `CLIQUET_BASE_URL` | [Local, for development](sandboxes.md#local-for-development) |
| `CLIQUET_ALLOW_REMOTE_DEV_LOGIN` | `0` | | `1` allows `CLIQUET_DEV_LOGIN` on a public address, for a short demo | [Local, for development](sandboxes.md#local-for-development) |
| `CLIQUET_ALLOW_REMOTE_LOCAL_EXECUTOR` | `0` | | `1` allows the `local` executor on a public address, for a short demo of your own apps | [Local, for development](sandboxes.md#local-for-development) |

---
icon: lucide/cloud-upload
description: "Deploy a Cliquet server on Fly, and run its commands, logs and backups there."
---

# Deploy on Fly

This guide deploys your own Cliquet server on [Fly](https://fly.io), with one machine and one volume for its data. It also says how to run the other guides' commands, settings and backups on Fly. You need a Fly account, with [`flyctl`](https://fly.io/docs/flyctl/) installed and signed in.

## Deploy by hand

<div class="steps" markdown>

1. **Write `fly.toml`** at the root of your copy of the repository. Git ignores it: keep it with the rest of your deployment.

    ```toml
    app = "<name>"
    primary_region = "cdg"

    [build]
      dockerfile = "docker/server.Dockerfile"

    [env]
      CLIQUET_DATA_DIR = "/data"
      CLIQUET_EXECUTOR = "sprites"
      CLIQUET_BASE_URL = "https://<name>.fly.dev"

    [mounts]
      source = "data"
      destination = "/data"
      snapshot_retention = 5

    [http_service]
      internal_port = 8080
      force_https = true
      auto_stop_machines = "off"
      auto_start_machines = true
      min_machines_running = 1

      [http_service.concurrency]
        type = "requests"
        soft_limit = 100
        hard_limit = 200

      [[http_service.checks]]
        method = "GET"
        path = "/healthz"
        interval = "30s"
        timeout = "5s"
        grace_period = "15s"

    [[vm]]
      size = "shared-cpu-1x"
      memory = "512mb"
    ```

    Replace `<name>` with your app's name, and `cdg` with the region nearest you. `[build]` names the server's image, in `docker/`. Fly snapshots the volume each day and keeps each snapshot 5 days. The machine never stops: a stop would lose the previews and MCP sessions the server keeps in memory, and the first request after it would wait for the server and the Sprites to start.

    Settings that aren't secret go under `[env]`, such as `CLIQUET_WEBSITE_URL`, your website's address, which each MCP server gives clients as its `websiteUrl`, and `CLIQUET_PRIVACY_URL`, your privacy notice: see [Privacy notice](operations.md#privacy-notice). Secrets go in Fly's secrets: see [Settings and secrets](#settings-and-secrets).

2. **Create the app.**

    ```bash
    fly apps create <name>
    ```

3. **Create the volume.** One machine holds the data, so one volume is enough.

    ```bash
    fly volumes create data --region cdg --size 1
    ```

4. **Set the Sprites token.** The server reads it only from its environment.

    ```bash
    fly secrets set CLIQUET_SPRITES_TOKEN=...
    ```

5. **Set up sign-in**, as [Sign-in](sign-in.md) describes, with its secrets in Fly's secrets.

6. **Deploy.**

    ```bash
    fly deploy
    ```

7. **Check that it answers.**

    ```bash
    curl https://<name>.fly.dev/healthz
    ```

    It answers `{"ok":true,"version":"0.0.1"}`. It checks the database and the data disk, which fails when it is read-only or has less than 10 MB free. When a check fails, it answers 503, such as `{"ok":false,"version":"0.0.1","failing":["database"]}`, and the log says why.

8. **Import a first app.** The image holds the example apps in `/app/examples`.

    ```bash
    fly ssh console -u nonroot -C "cliquet-server import /app/examples/meeting-rooms --owner <login> --name meeting-rooms --email <email>"
    ```

    You choose the owner's login: it has nothing to do with the email. The owner keeps it when they first sign in with that email, which must be verified. Someone who signs in before their email is verified chooses a login of their own. They become this owner once they verify it, if their own account holds nothing yet, as [Unverified emails](sign-in.md#unverified-emails) says. The first import sets up the owner's Sprite, which takes about a minute. An owner made by an import may build apps.

9. **Open it.** Go to `https://<name>.fly.dev/`, sign in, and connect an assistant to `https://<name>.fly.dev/me/mcp`, as [Connect your agents](../../agents.md) describes.

</div>

## Settings and secrets

Put a setting that isn't secret under `[env]` in `fly.toml`, and deploy. Set a secret, such as a token or a key, with `fly secrets set`, which restarts the machine:

```bash
fly secrets set CLIQUET_OIDC_CLIENT_SECRET=...
```

`fly secrets list` names the secrets the app has, never their values. [Settings](settings.md) lists every setting, and which are secrets.

## Run a command

The other guides run commands, such as `cliquet-server grant`, on the server's machine. Run each one through `fly ssh console`, as the image's user:

```bash
fly ssh console -u nonroot -C "cliquet-server grant <login> build"
```

`fly ssh console` connects as root unless told otherwise, so give every `cliquet-server` command `-u nonroot`, or it leaves files root owns. The one command that must run as root is the `chown` in [The server's user](operations.md#the-servers-user):

```bash
fly ssh console -C "chown -R 1000:1000 /data"
```

To restart the server, run `fly apps restart <name>`.

## Read the log

`fly logs` follows the server's log. Fly keeps only its recent lines. To find Cliquet's own failures, each with its traceback:

```bash
fly logs --no-tail | grep -A 30 'cliquet-server: unexpected:'
```

## Back up the volume

Fly snapshots each volume once a day, and the `fly.toml` above keeps each snapshot 5 days. Take one of your own before a deploy you worry about, and make a volume from one to restore it:

```bash
fly volumes snapshots create <volume-id>
fly volumes snapshots list <volume-id>
fly volumes create data --snapshot-id <snapshot-id>
```

## Deploy from CI

To deploy each change from CI, make a deploy token for the app, and give it to your CI as the secret `FLY_API_TOKEN`:

```bash
fly tokens create deploy -a <name>
```

Then run `flyctl deploy --remote-only` from the repository's root, with your `fly.toml`.

## Remove a server

This deletes the app, its machine, its volume and the data on it:

```bash
fly apps destroy <name> --yes
```

## Next steps

<div class="grid cards" markdown>

-   :lucide-key-round: **[Sign-in](sign-in.md)**

    Set up a provider, with Auth0 as the example, and what MCP clients need.

-   :lucide-box: **[Sandboxes](sandboxes.md)**

    Sprites, Docker containers, and what each keeps apart.

-   :lucide-activity: **[Operations](operations.md)**

    Rights, limits, upgrades, account deletion and troubleshooting.

</div>

---
icon: lucide/activity
description: "Run a Cliquet server day to day: who may build, limits, privacy, upgrades, account deletion and troubleshooting."
---

# Operations

This guide covers running your server once it is deployed. Every command runs on the server's machine, as the image's user, `nonroot`. On Fly, see [Run a command](fly.md#run-a-command).

## The right to build

A person with the right to build can create apps. Everyone else uses the apps shared with them. The first person to sign in to a new server has the right: whoever sets it up. An owner made by `cliquet-server import` has it too, and so does the development login. Anyone else starts without it, unless `CLIQUET_GRANT_BUILD_ON_SIGN_UP=1` gives it to every new person as they choose their login. Each builder then has a sandbox, within the [limits](#limits). Grant or withdraw it by login:

```bash
cliquet-server grant <login> build
cliquet-server withdraw <login> build
```

It holds from the person's next request. Withdrawing it keeps their drafts and their sandbox, out of their reach, and their published apps keep serving their members. They can still download their apps from their account page.

## Access to the server

Nothing on the server is public. People sign in to the web pages, and only an app's owner and its members can open it. An assistant connects to `/me/mcp`, which reaches every app its person may use, and signs in through the sign-in provider. A routine connects to one app's URL, `/apps/<login>/<app>/mcp`, with an API token limited to that app: an app's URL takes no token of the provider.

Each MCP server tells its clients what it is: the person's names Cliquet, each app's names the app, and both carry Cliquet's mark and a website's URL, from `CLIQUET_WEBSITE_URL`. Left empty, that is the server's own URL.

## Privacy notice

People who keep data on your server should be told who keeps it, what is kept and for how long. The server holds no such text: publish your notice at a URL of your own, and give it to the server.

```text
CLIQUET_PRIVACY_URL=https://example.com/privacy
```

The console's menu and the pages people see before they sign in then link to it. Without it, they show no link.

Say in your notice how long backups keep deleted data. On Fly, the `fly.toml` in [Deploy on Fly](fly.md) keeps each daily volume snapshot 5 days. The console's page for deleting an account sends people to your notice for how long their identity at the sign-in provider and the backups stay.

## Limits

The server limits what one caller can make it do. Each app is counted on its own, and each person on their MCP server URL. A call over a limit is refused, with when to try again, and the log notes the refusals once a minute. The defaults are far above what a person working normally does. Change one with its setting, as [Settings](settings.md#limits) lists them.

Your host can also cap the requests the machine takes at once, before the server's own limits. On Fly, `fly.toml` does, in `[http_service.concurrency]`.

### Sandbox time

The sandboxes cost the most, and a provider may cap nothing: Fly has no spending limit. These settings cap their time each calendar month, in UTC:

- `CLIQUET_SANDBOX_MINUTES_PER_OWNER_MONTH`: the minutes each owner's sandbox may be busy;
- `CLIQUET_SANDBOX_MINUTES_PER_PLUS_OWNER_MONTH`: the same for a Plus owner, at least the free budget;
- `CLIQUET_SANDBOX_MINUTES_PER_MONTH`: the minutes all the sandboxes together may be busy.

A Plus owner is an owner with the right plus. Grant or withdraw it by login:

```bash
cliquet-server grant <login> plus
cliquet-server withdraw <login> plus
```

It holds within a minute, with no restart. Without the Plus setting, Plus owners have the free budget. They count toward the server's minutes like everyone else.

A sandbox is busy while at least one call runs in it: an app's action, query or screen, by its owner or by the people they share it with, and a builder's check, test run, preview, screenshot or publish, with the builds and setups they cause. Two calls at once count once. The server's warm-up as it starts counts nothing.

Once an owner's minutes are used, every call that runs in their sandbox is refused until the 1st of the next month at 00:00 UTC, with that date. Their account page shows how much they used, of their own budget. Once the server's minutes are used, every call into any sandbox is refused, until the same date. A call that has started finishes. The history and the transactions' own tools, and the builder's tools that run nothing in the sandbox, still work.

The count is the time Cliquet waits for a sandbox, not what your provider bills. A Sprite stays awake about 30 seconds after its last command, and a Docker container 15 minutes by default, so the bill is higher: set the ceiling with room to spare. Without either setting, nothing is counted, kept or refused.

## Upgrade

Deploy the new version: as it starts, before it serves, the server migrates its database to the new version. Each migration runs in one transaction, so a migration that fails changes nothing.

The server refuses to start, with one line in its log, in two cases:

- **A migration failed.** The line starts with `cliquet-server: unexpected: the database can't be migrated`, followed by its traceback. Nothing changed: deploy the previous version, or a fixed one.
- **The database is newer than the server**, as after a rollback. The line starts with `cliquet-server: the database is at revision`. Deploy the newer version again, or restore the volume from a backup taken before it started, and lose what changed since.

Back up the data volume before a deploy you worry about. On Fly, see [Back up the volume](fly.md#back-up-the-volume).

### The server's user

The image runs as `nonroot`, with the id 1000, so that file permissions keep a query or a screen from writing: root ignores them. Run every `cliquet-server` command as `nonroot`, or it leaves files root owns.

A volume written by an older image that ran as root still belongs to root, and the server stops with a traceback. Once, just before the deploy that brings the new image, run this as root on the server's machine:

```bash
chown -R 1000:1000 /data
```

## Delete an account

A person deletes their own account from the console. You delete anyone's by login, typed twice:

```bash
cliquet-server delete-account <login> --confirm <login>
```

Both remove everything the person owns, as [Account management](../account.md#delete-your-account) lists. A deletion that stops midway, during a deploy for example, is safe: the account is closed from its first step. Run the command again to finish it, or let the server finish it when it next starts.

The command runs in a process of its own, so the running server may hold some of the person's things in memory, out of anyone's reach, until it restarts. To clear them at once, restart the server.

Cliquet never calls the sign-in provider: it only forgets the person's identity. The command prints the issuer, subject and email it forgot. For a person who deleted their own account, the server's log holds the line `forgot the identity of <login>`, with the same details. Then delete the person at the provider, found by that subject: with Auth0, under **User Management**, **Users**, or with the Management API, `DELETE /api/v2/users/{id}`, the id being the subject. Otherwise they can sign in again, as someone new.

## Troubleshoot

- **Read the log.** The server writes it to its standard error. Every failure of Cliquet's own is one line starting with `cliquet-server: unexpected:`, followed by its traceback, so search the log for that text. On Fly, see [Read the log](fly.md#read-the-log).

    Such a line is a fault of the server or of Cliquet, such as a sandbox that didn't start. An app's own errors never are: its owner reads them on the app's **Errors** tab, and the log has them only as warnings, by their id.

    A line starting with `cliquet-server: sandbox budget:` says the server used its [sandbox time](#sandbox-time) for the month, and refuses every call into a sandbox. It comes back once an hour until the month ends.

- **`attempt to write a readonly database`, or `unable to open database file`, right after a deploy:** the volume still belongs to root. Run the `chown` in [The server's user](#the-servers-user), then restart the server.

- **A Sprite was destroyed, or the Sprites token now belongs to another organization:** there is nothing to do. The server finds the Sprite gone and sets it up again, in a minute or so. The call that found it may fail first. After a move, delete the old organization's Sprites, named with the server's prefix, so they stop costing storage.

- **A Sprite is there but broken:** tell the server to forget it, then restart the server, which otherwise keeps using the one it holds. The next call sets up a new one.

    ```bash
    cliquet-server forget-sandbox <login>
    ```

- **Back up `/data`**, which holds `server.db` and every app's repository, in `apps/<login>/<app>/`.

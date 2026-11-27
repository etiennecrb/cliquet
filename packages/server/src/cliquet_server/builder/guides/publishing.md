# Publishing

`check_app(app)` says what the new version changes, without publishing anything:

- `changes`: what changes for the people and agents who use the app: the tools, rules, roles and data files added, removed or changed;
- `data`: what happens to the data, such as "First data: items.csv." or "The migration rewrites: items.csv.".

Show both to the person, in your own words: which tools were added, removed or changed, which rules were removed, and what happens to the data. Publish only when they agree.

`publish_app(app, remark)` then runs every check: the build, the tests, the migration if there is one, and the data and the views. If one fails, nothing is published, and the answer says which check failed and why. Otherwise the version is live, and the answer repeats `changes` and `data`, with the app's web pages and MCP addresses. Checks and publish use the real current data, never a preview's.

`remark` is one line that says what this version changes. It is kept in the app's history.

After a publish, the preview ends: call `start_preview` to try more. The person's open preview page says the app was published. An agent with a session at the app's own address loses it: its next request doesn't run, and is told the app was published again. It connects again to get the new tools. An assistant at the person's own endpoint opens the app again instead.

## Who can use the app

An app is private. Only its owner, and the people they share it with, can open it. Each of them signs in, and the owner gives each person a role. The owner holds every role.

The owner shares the app by email and role, from the Sharing tab of its page in the console, or through you: `share_app(app, email, role)`, once it is published. Leave `role` out when the app has one role or none. Cliquet sends no email, so the owner tells them. They sign in with that email, then accept or decline the share on their **Apps** page in the console, or ask their assistant to, which calls `accept_share` or `decline_share`. Until they accept it, the owner knows only the email: the answer is the same whether or not it has an account.

`revoke_share(app, who, role)` takes the app back: `who` is a member's login, or the email of a share not yet accepted, which it cancels. Leave `role` out to take back every role. To change a member's role, share the new role with their email, wait until they accept it, then revoke the old one: until then they keep the old role. Share or revoke only when the person asks for it.

`read_app_overview(app)` lists the members by login, with their roles, and the shares not yet accepted by email. It also gives one web page per role, and two MCP addresses:

- `assistant_url`, the person's own endpoint, is for their assistant, such as Claude or ChatGPT. They connect it once and sign in, then ask it to open the app by name. It reaches every app they use, as one of their roles. It is where these tools are too, so the owner's assistant reaches each app they publish without a new connection.
- `agent_url`, the app's own address, is for an agent that runs on its own, such as a routine or a scheduled job. It takes only a token limited to this app and one of the person's roles, which they make at `tokens_url`. One token may also be limited to other apps, with one role in each, for an agent that uses several: it reaches only those apps, each at its own address. A sign-in doesn't work there.

Give the person `assistant_url` unless they ask for an agent that runs on its own.

Present the web pages as Markdown links named by the app's title, as `publish_app` and `read_app_overview` write them in `next`, such as `[Shop · staff](…/apps/sam/shop/staff)`. Present `assistant_url` and `agent_url` as code to copy, never as links: they are MCP endpoints, not pages to open in a browser.

When the person asks for one, such as a worker you set up, give it `agent_url` and a token from `create_token(name, apps, days)`. `apps` names each app the token works for, with one of the person's roles in each, such as `[{"app": "sam/shop", "role": "staff"}]`: choose the smallest role that does the job. `days` is how long it works, 7, 30, 90 or 365, 90 by default: when it expires, the worker is refused until it gets a new token. A token made here never works everywhere, and always expires.

- With a sign-in, you never see the token's secret. Where the host shows panels, the person sees one in the conversation: they click **Create**, and copy the secret there. The panel then gives you the token's `token_id`, or the person tells you it is made. Elsewhere, the answer gives a link to the **API tokens** page with its form filled in: the person opens it, clicks **Make a token**, and copies the secret into the worker's settings.
- With a token for everywhere, the answer gives the secret, once. Put it in the worker's settings, sent as `Authorization: Bearer <secret>`, and nowhere else. The token goes when the person revokes the one you call with, and never outlives it: without `days`, it expires when yours does, at the latest.

`list_tokens()` lists the person's tokens, each with its `token_id`, when it expires and the token that made it. `revoke_token(token_id)` stops one at once, with the tokens it made. Create or revoke a token only when the person asks for it.

The owner downloads the app, with its data and its whole history, as a zip file from `download_url`: a browser link that `read_app_overview(app)` and `publish_app` give.

`read_app_overview(app)` lists the latest changes to the data, each with its `commit`. `read_change(app, commit)` shows what one did to each data file, as a diff, as the History tab shows it to the owner. Only the owner reads it, since a diff quotes the data.

Before the first publish, tell the person what the app's first data will be, and who sees it: see "First data" in `read_guide("start")`.

## When the live app fails

When the app's code fails in a call to the live app, only the app's owner reads the error's text, since it may quote the data. Anyone else reads the error's type and an id, such as `e-1a2b3c4d5e6f`, and that the app's owner can read it in the console.

The live app may also fail to start, for example when `app.py` raises as it loads. Then every page and call of the app says it can't be started. Anyone but the owner reads an id in place of why, as for a failed call.

Cliquet keeps each of these errors for the owner, with its id and when it happened:

- A failed call keeps who called which tool with which role, and the error's text with its traceback. It never keeps the call's arguments. Its `during` is `call`.
- A failed start has no caller and no tool. Its text says why, and where `app.py` raised, if it did. Its `during` is `start`. A start that fails again the same way within a day keeps no new error.

An error is kept 30 days, and an app keeps its last 200, with 20 failed starts at most.

- The owner reads them in the Errors tab of the app's page in the console, even while the app can't be started.
- You read them with these tools, as the owner's assistant. `read_app_overview(app)` says under `errors` how many the app had in the last 30 days, and lists the latest with their ids. While the app can't be started, it also says why under `problem`. `read_error(app, error)` gives one error's whole record.

A refusal isn't an error: it is the app's own answer, and it isn't kept. Nothing from a preview is kept either, since `try_call` already gives you the error in full.

To fix one, read it, find its cause in `app.py`, and write a test that fails the same way. Then fix the app and publish it, as for any change.

## Changing the format of the data

Once an app is live, its data is real. A new version must pass on it: `publish_app` checks the live data against the new rules, and stops if it doesn't pass.

To change the data's format, such as a new column or a renamed file, write `migrate.py` in the draft:

```python
import csv
from pathlib import Path


def migrate() -> None:
    """Add a `done` column to items.csv, empty for the items already there."""
    if not Path("items.csv").exists():
        return
    with open("items.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    with open("items.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "note", "done"])
        writer.writeheader()
        writer.writerows({**row, "done": ""} for row in rows)
```

- It reads and writes the app's files with ordinary file I/O, by their names, like an action. It takes no argument.
- A file may not exist yet, when no action has written it: handle that, as in the app.
- To rename a file, write the new one and remove the old one. A live data file the new version doesn't declare stops the publish, with a migration or without: remove it in `migrate.py`, or declare it again. A pattern declares every live file it matches, such as `**/*.beancount` for `ledger.beancount` and `2026/01.beancount`.
- At publish it runs once on the live data, before the checks. Only if the data changes during the publish does it run again, on the new data, so let it depend only on the data it reads. `check_app` runs it too, on a copy, and both say which files it rewrites.
- Every rule of the new version must hold on the result. Otherwise nothing is published. The invariants check its change as `ctx.action == "app_publish"`, the name the change has in the app's history. Then, as for every version, they check the result again as `"app_check"`, with `ctx.before` the result itself.
- It sends back at most 21 MB of changed data, and the app keeps at most 21 MB in all. If an app holds more, as one live before that limit may, migrate part of the files in one version and the rest in the next.
- After the publish it leaves the draft, and is kept in the app's history.

Try it first: `check_app` runs the migration on a copy of the data and reports the result. `start_preview` runs it on the preview's copy too, and its `migration` says what it changed.

A new app needs no migration. Before the first publish, change the draft's `data/` files instead.

## Restoring an earlier version

When a live version turns out wrong, restore an earlier one. Restoring publishes that version's code again, as one new publish: the history keeps every version, and nothing is rewritten.

1. `read_app_overview(app)` lists the app's `versions`, the live one first. Each has its `version`, the `commit` that published it, when, and its note.
2. `restore_version(app, version)` puts that version's code in the draft: `app.py`, its lock, `instructions.md` and the tests. Name the version by its `version` or its `commit`. It never brings back the version's `migrate.py`, which already ran. It is refused while the draft has changes that aren't published: publish them, or call `discard_draft(app)` first.
3. `start_preview(app)` runs the older version on a copy of today's data. Its `restore` says how many changes were made to the data since the first publish it undoes. The older version may drop fields of the data it doesn't know, the next time it writes them: tell the person.
4. `check_app(app)` checks the build and the rules on the current data, and `run_tests(app)` runs the tests. `publish_app(app, remark)` then runs every check again, as for any version. If one fails, nothing is published, and the live version stays.

When a publish it undoes had a migration, the same publish also puts back the data from before that migration, by the rule of `app_revert`: `data` says which files. A change made since that touched the same lines stops it, with how many changes are in the way: undo them first, or publish a new version with a migration instead. Without a migration, the data stays as it is. A live data file the restored version doesn't declare stops it too: so once an action created a file only a pattern declares, such as `2026/02.beancount`, a version that declared names only can't be restored.

Once you edit the draft, it is a new version of its own: publish then puts back no data.

The owner can also restore a version from the History tab of the app's page in the console: "Restore this version" publishes it at once, with the same checks.

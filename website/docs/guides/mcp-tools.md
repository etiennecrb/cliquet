---
icon: lucide/wrench
description: "Reference of the MCP tools Cliquet gives agents, the built-in tools of every app, and the arguments Cliquet adds."
---

# MCP tools

This reference lists the MCP tools an agent gets from Cliquet: the tools at your MCP server URL, the builder's tools, the tools at an app's own URL, the built-in tools of every app, and the arguments Cliquet adds to an app's tools. You never call them yourself, but they explain what your assistant can do.

## Tools at your MCP server URL

At `https://app.cliquet.dev/me/mcp`, an agent reaches every app you may use through these tools. It describes an app, then calls the app's tools, built-in ones included, through `read_app_data`, `write_app_data` and `show_app_view`.

| Tool | Arguments | What it does |
| --- | --- | --- |
| `list_apps` | | Lists your apps, with whether each is published and whether its draft has changes, the apps shared with you, and the shares waiting for you |
| `describe_app` | `app`, `role` | Gives the app's instructions, written by its author, and the tools of your role, each with its description and arguments |
| `read_app_data` | `app`, `tool`, `role`, `arguments` | Calls one of the app's tools that only reads, such as a query |
| `write_app_data` | `app`, `tool`, `role`, `arguments` | Calls one of the app's tools that changes data: an action |
| `show_app_view` | `app`, `view`, `role`, `arguments` | Shows one of the app's screens, whose buttons call `write_app_data` for an action and `show_app_view` for another screen |
| `accept_share` | `app` | Accepts every role of an app shared with you. Agents call it only when you ask |
| `decline_share` | `app` | Declines every role of an app shared with you: the share goes. Agents call it only when you ask |
| `ask_for_app_file` | `app`, `action`, `role` | Lets you upload a file one of the app's actions takes, in the conversation or on its upload page. It answers the [reference](../agents.md#give-an-app-a-file) your agent passes to the action |

`app` names an app as `<login>/<name>`, or by its name alone when that is enough. `role` is needed only when you hold several roles in the app.

Until you have a login, the same URL also gives `choose_login`, which takes the `login` you chose. Your agent asks you first, and offers a free login. Every other tool waits for it, and `choose_login` goes once you have a login.

While your email isn't verified, a tool's answer also says, once a day, how many days you have left to [verify it](../index.md#verify-your-email).

## The builder's tools

If your account can build apps, the same URL also gives your agent the builder's tools. With them, it writes an app, tests it, tries it on a preview, and publishes it, without a terminal.

When you're given the right to build, or lose it, your assistant sees the builder's tools change the next time it lists its tools.

| Tool | What it does |
| --- | --- |
| `read_guide` | Reads one of the builder's guides: `start`, `app`, `tests`, `views`, `instructions` or `publishing` |
| `read_example` | Lists an example app's files, or reads one |
| `create_app` | Starts a new app from a small template |
| `read_app_overview` | Describes an app: its web pages, MCP server URLs, tools, rules, members, latest changes and latest errors |
| `list_files` | Lists the draft's files with their sizes, and the live data files |
| `read_file` | Reads lines of a draft's file, or of a published app's data |
| `write_file` | Creates or replaces a draft's file |
| `edit_file` | Replaces exact text, or a range of lines, in a draft's file |
| `delete_file` | Removes a file from the draft |
| `ask_for_file` | Lets you upload a data file to the draft, in the conversation or on the upload page |
| `find_classes` | Lists the style classes a screen may use |
| `find_icons` | Lists the icons an app or a screen may use |
| `check_app` | Builds the draft, checks its definition, data and screens, and says what publishing it would change |
| `run_tests` | Runs the draft's tests |
| `start_preview` | Runs the draft on a copy of the current data |
| `stop_preview` | Stops the preview, and throws its copy of the data away |
| `try_call` | Calls one of the app's actions or queries on the preview |
| `try_view` | Clicks through a screen on the preview |
| `show_preview` | Shows you a screen of the draft, on the preview |
| `take_screenshots` | Takes pictures of the draft's screens on the preview, at two widths, in the light or the dark theme |
| `publish_app` | Makes the draft live, if every check passes |
| `discard_draft` | Throws away the draft's changes that aren't published |
| `restore_version` | Puts an earlier version's code in the draft, to try and publish |
| `read_change` | Reads what one change did to the published app's data: each data file's diff, for the owner only |
| `read_error` | Reads one error of the published app, with its traceback |
| `delete_app` | Deletes an app and its data |
| `share_app` | Shares a published app with an email, as one of its roles. Agents call it only when you ask |
| `revoke_share` | Takes an app back from a member, by login, or cancels a share not yet accepted, by email: every role, or one. Agents call it only when you ask |
| `create_token` | Makes an [API token](../agents.md#api-tokens) limited to apps, for an agent that runs on its own, for 7, 30, 90 or 365 days: 90 by default. Signed in, you make it yourself, in a panel in the conversation or from the link it gives, and your assistant never sees it |
| `list_tokens` | Lists your API tokens: where each works, when it was made, last used and expires, and which token made it |
| `revoke_token` | Revokes one of your API tokens, and the tokens it made, at once |

## Tools at an app's own URL

At an app's own URL, such as `https://app.cliquet.dev/apps/sam/meeting-rooms/mcp`, an agent connects with an [API token](../agents.md#api-tokens) limited to that app. It gets the app's own actions, queries and screens for the token's role, as tools named after them, such as `book`, and the app's [built-in tools](#built-in-tools). There is no app to open first.

## Built-in tools

Every app has the same built-in tools, next to its own. Their names start with `app_`, a prefix no app may use, so they never clash with an app's own tools.

A role that can change data gets every built-in tool but `app_ask_for_file`, which comes with an action that takes a file. A role that only reads gets `app_history`, and `app_changes_since` if the app has queries.

### `app_history`

```text
app_history(limit, since)
```

Lists the latest changes to the data, newest first: your own, or everyone's for the owner. Each change gives its commit, when it was made, who made it, the role, the action, the files it changed, its remark, and `agent`, the name of the API token it was made with, if any.

| Argument | Description |
| --- | --- |
| `limit` | How many changes to list, from 1 to 200. Default: 20 |
| `since` | Only the changes after this point: a version of the data, or an ISO 8601 time with its time zone, such as `2026-11-01T00:00:00+01:00`. A time may also list a change made shortly before it |

### `app_changes_since`

```text
app_changes_since(query, arguments, since)
```

Says what changed in one of the app's answers since a point: it runs one query, with its arguments, on the data as it was then and as it is now. For a list, `added` holds the new items and `removed` the ones gone, so a changed item appears in both. Pass the answer's `now` as `since` the next time, and nothing is missed or told twice.

| Argument | Description |
| --- | --- |
| `query` | The name of one of the app's queries |
| `arguments` | The query's arguments |
| `since` | A version of the data, such as an earlier answer's `now`, or an ISO 8601 time with its time zone, such as `2026-11-01T00:00:00+01:00`. A time may also count a change made shortly before it |

### `app_revert`

```text
app_revert(commit, app_request_id, app_remark)
```

Undoes one change, and keeps the later ones: one of your own, or anyone's for the owner. The undo is a new change, checked against the rules. It also undoes a publish's change to the data, but not the publish's code: to go back to an earlier version, [restore it](../history.md#restore-an-earlier-version) instead.

| Argument | Description |
| --- | --- |
| `commit` | The change to undo, from `app_history`: at least the first 7 characters of its commit |
| `app_request_id` | See [Added arguments](#added-arguments) |
| `app_remark` | See [Added arguments](#added-arguments) |

### `app_begin_transaction`

```text
app_begin_transaction()
```

Starts a transaction: a group of changes kept together, or not at all. It answers the transaction's id, such as `t-…`, and the `app_data_version` it began from. Pass that version as `since` to `app_changes_since` to see what the transaction changes.

### `app_transaction_calls`

```text
app_transaction_calls(app_transaction)
```

Lists the calls made in an open transaction, oldest first, with the files each changed.

### `app_commit_transaction`

```text
app_commit_transaction(app_transaction)
```

Keeps the transaction's changes, one commit per call, and answers the commits and the `app_data_version` they leave. If the data changed since the transaction began, nothing is kept. Called twice, it gives the same answer.

### `app_rollback_transaction`

```text
app_rollback_transaction(app_transaction)
```

Drops the transaction: nothing in it is kept. Called again, it says the transaction was rolled back. A transaction is also dropped an hour after its last change, and when a new version of the app goes live.

### `app_ask_for_file`

```text
app_ask_for_file(action, argument)
```

Asks you for a file an action takes, so your agent never copies it. It answers a reference at once, and the link to a page where you upload the file. Your agent passes the reference as the action's file argument once the file is in. A reference works once, for you alone: a refused call leaves it for the next one. It ends an hour after the file came, or an hour after it was asked for if no file came. A file is at most 5 MB.

| Argument | Description |
| --- | --- |
| `action` | The action that takes the file |
| `argument` | Optional. The action's file argument: an action has one at most |

At your MCP server URL, your agent calls it with `ask_for_app_file`, which shows the upload panel in the conversation.

## Added arguments

Cliquet adds these optional arguments to an app's own tools. They don't change what the app does: Cliquet handles them before and after the call.

| Argument | On | Description |
| --- | --- | --- |
| `app_remark` | Every action, and `app_revert` | Why the change is made, such as "asked for a room for the design review". One line of at most 200 characters, kept for good in the history: it should hold no names or private details |
| `app_request_id` | Every action, and `app_revert` | A unique id for the call. A call sent again with the same id, such as after a lost connection, isn't made twice: it answers the first call's commit |
| `app_if_unchanged_since` | Every action | The version of the data the change depends on. If the data changed since, the change is refused without running |
| `app_transaction` | Every action, query and screen, `app_history`, `app_changes_since` and `app_revert`, for a role that can change data | The transaction the call belongs to |

## Answers

Every query's answer carries `app_data_version`: the version of the data it read, which is the id of a commit. An agent passes it back as `app_if_unchanged_since` when its change depends on what it read. See [Changes made in the meantime](../history.md#changes-made-in-the-meantime).

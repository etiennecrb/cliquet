# Architecture

The main architectural decisions behind Cliquet, each with its reason. How each one is carried out, with its limits, formats and edge cases, is in the code and its tests.

## What Cliquet is

- **Cliquet runs no model.** Agents do the thinking, in the harness their person already uses. Cliquet stores, checks, serves and shows.
- **An app is one Python module, written by an agent.** Its data, the actions that change it, the queries that read it, the invariants that must always hold, and its screens. One explicit way to write an app is what lets an agent write one from a guide.
- **Apps run on a Cliquet server, hosted or self-hosted, and the builder is the way to write one.** Sign-in, sharing, roles, screens and backups need a server, and assistants reach only remote MCP servers. The `cliquet` CLI is a tool for developing Cliquet itself.
- **Cliquet connects to no other system.** Agents carry data in and out with their own connectors, so Cliquet holds no credentials for other services.

## Apps and the SDK

- **The app-facing SDK is a contract.** Apps depend on what AGENTS.md lists, so a change to it breaks apps. The rest of the SDK is internal.
- **The manifest is the boundary.** Whatever serves an app relies only on the manifest and on calling the app's functions, and app code never touches MCP. So the server, the CLI and the tests run the same app unchanged, and protocols change without changing apps.
- **App code is deterministic.** It reads and writes only its own files, has no network, takes the time from `ctx.now` and uses no randomness. A write is then safe to run again, a test can fix the time, and a sandbox can be thrown away.
- **The SDK stays small, and never comes from a package index.** Every app's environment installs it, so it depends on `jinja2`, `mcp` and `pydantic` only. `cliquet` on PyPI is someone else's package.

## Data and history

- **An app's data is plain files in its own git repository.** History, undo and export come from git, and history is never rewritten.
- **Every write is a transaction that the runtime alone commits.** The action runs on a copy, every invariant runs on the result, and the change is committed whole or refused with the reasons. An agent's mistake becomes a refusal it can read and fix.
- **Writes are safe to retry and to race.** A retried call is answered, not run twice. Writes to one app run one at a time. A write can name the version of the data it was decided on, and is refused if the data changed since.
- **Undo, restore and migration are changes like any other.** Each is a new commit, checked by the invariants. A new version goes live only once it holds on the live data: code and data move together, or not at all.

## Who sees what

- **Access is declared by role, on each action, query and view.** Adding a role opens nothing, and invariants hold whoever writes.
- **Cliquet shows data only through the app's own code,** for the caller's role. What Cliquet adds itself, such as errors, history and versions, never quotes the data, except to the app's owner.
- **The owner is a person who holds every role, and sees everything.** Members see only what their roles give them. "Not yours" reads as "missing".
- **An agent acts as its person, never with more.** A person's assistant reaches every app they use, through one address. An agent that runs on its own gets a token limited to one app and one role. Only the person decides who gets in: an app's words, written by its author and members, can't lead an assistant to share or hand out access.
- **The identity provider says who someone is; Cliquet says what they may do.** Cliquet speaks only standard OpenID Connect and OAuth, so any provider fits, and nothing in the code belongs to one.

## The sandbox

- **App code never runs in the server's process.** It runs in a sandbox of its own for each owner, which is the boundary between owners. The sandbox has no network once set up and none of the server's secrets. The one exception is the local executor, for a person running their own apps on their own machine.
- **The server trusts nothing a sandbox sends back.** App code can take over its sandbox, so the server checks roles itself, and checks every file, size, screen and error that comes back.

## Screens

- **A screen is HTML from a checked vocabulary, drawn by one trusted renderer.** Views use daisyUI's classes and Cliquet's own formats for charts, grids and icons. The check refuses scripts, styles, URLs and anything else outside the vocabulary. Data never becomes markup, and nothing happens without a click.
- **Apps depend on Cliquet's formats, not on libraries.** The libraries behind charts, grids and icons can then change without changing apps.

## The builder

- **The builder gives an assistant tools, never a shell.** One tool for each step the guides describe, so a model can't do anything else in someone's sandbox.
- **Everything is tried before it goes live.** Previews run on a copy of the data, and publishing runs every check, the tests included, in one step.
- **A person's files reach the server from their browser, not through the model.** A model would spend a token per few bytes, and might change a line.
- **The builder's guides are the only guide to writing an app,** and they change with what an app may do.

## The server and this repository

- **The hosted service is this open server.** What a server needs is built here, so a self-hoster gets it too. A deployment keeps its own configuration and secrets, and nothing private is committed.
- **One server is simple to run.** It is one process, configured by `CLIQUET_` environment variables, with portable SQL tables migrated as it starts. Its pages need no build step, and its forms work without JavaScript.
- **Limits protect the server and its owners.** Each person, app and owner can make the server do only so much, and a server can give each owner's sandbox time a monthly budget, since the provider caps no bill.
- **Security and data guarantees are held by tests, not by documents.** A sandbox reaching no network, or a member reading only their own changes in the history, is each proved by a test that fails when it breaks.
- **The docs are the only documentation for people,** in `website/docs/`. How the code works is in the code, its docstrings and its tests.

# AGENTS.md

Instructions for coding agents working in this repository. This is the only agent instructions file.

## What Cliquet is

Cliquet is an app builder for the agent era. An app is one short Python file, written against a small SDK: actions change the data, invariants say what must always hold, queries answer questions, and views lay out a screen. The runtime runs each action on a working copy, checks every invariant, then commits to the app's git repository or refuses with the reasons. Any agent uses the app over MCP. Read the website's home page, [`website/public/index.html`](website/public/index.html), for the why, [Architecture](website/docs/guides/architecture.md) for an overview, and the code for the how.

## Repository map

| Path | Contents |
| --- | --- |
| `ARCHITECTURE.md` | The main architectural decisions, each with its reason |
| `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md` | The licences, which the README's Licences section maps to each part, how to report a vulnerability, and why pull requests are closed for now |
| `packages/sdk/` | The SDK, runner, test harness and the CLI for developing Cliquet: package `cliquet-sdk`, imported as `cliquet`, with its tests in `packages/sdk/tests/` |
| `packages/server/` | The Cliquet server: package `cliquet-server`, with its tests in `packages/server/tests/` |
| `examples/` | Example apps, each with `app.py`, `instructions.md`, `data/` and `tests/`: `meeting-rooms`, `environments` and `ledger` |
| `packages/sdk/tests/apps/` | Whole apps the tests run, such as a tea shop's `stock`: test fixtures, not examples |
| `packages/server/src/cliquet_server/builder/guides/` | The builder's guides: the only guide to writing an app |
| `docker/` | The server's image, `server.Dockerfile`, the sandbox image the Docker executor runs each owner's apps in, `sandbox.Dockerfile`, and `compose.yaml`, which runs both with Docker Compose, locally or in public behind Caddy |
| `website/`, `wrangler.jsonc` | The website: its static files in `public/`, its legal and privacy notices, and its Cloudflare configuration, which says how it is deployed |
| `website/docs/` | The docs at cliquet.dev/docs, built by Zensical: the only documentation for people, from the quickstart to self-hosting |
| `.github/workflows/` | CI: the definition of done, and the real-Sprite and real-Docker tests |

## Commands

```bash
uv sync                     # install both packages and the dev tools
uv run pre-commit install    # check lint, format and commit messages before each commit
uv run pytest                # test
uv run ruff check .          # lint
uv run ruff format .         # format
uv run ty check              # type check
scripts/check-examples.sh    # type-check and test each example, and each test app, in its own environment
uv run python scripts/vendor-screens.py /tmp/vendor  # upgrade daisyUI or Chart.js everywhere: see the script
uv run python scripts/vendor-icons.py /tmp/vendor    # upgrade Lucide's icons: see the script
uv run cliquet test examples/meeting-rooms   # run one example's tests
uv run cliquet check examples/meeting-rooms  # check an app's definition, data and views
uv run cliquet preview examples/ledger --role owner  # see an app's views in a browser
uv run cliquet preview examples/ledger --role owner --screenshot /tmp/shots  # or as pictures
uv run cliquet new /tmp/rooms --from examples/meeting-rooms  # copy an example into its own repository
uv run zensical build --strict --config-file website/zensical.toml  # build the docs site
uv run zensical serve --config-file website/zensical.toml  # or read it at localhost:8000/docs/
uv run python scripts/docs-diagrams.py  # draw the docs' diagrams again
npx wrangler dev                     # serve the website and the built docs at localhost:8787
uv run alembic upgrade head          # bring the local database to the newest revision, then
uv run alembic revision --autogenerate -m "add owners' last use"  # write one for a model change
```

Examples are templates: copy one with `cliquet new` before serving it with `cliquet serve`.

### Fast tests for the edit loop

The full suite is end-to-end: it starts browsers, sandboxes, HTTP servers and MCP clients. Use the fast subset while editing, and run the full suite before a commit:

```bash
uv run pytest -m "not browser and not slow"   # fast edit loop (~45 s on 8 cores)
uv run pytest packages/sdk/tests -m "not browser and not slow"    # SDK only
uv run pytest packages/server/tests -m "not browser and not slow" # server only

uv run pytest -m browser    # only the real-browser tests
uv run pytest -m slow       # only the long integration tests
uv run pytest              # full suite (~2–3 min on 8 cores); definition of done
uv run pytest -n 0         # one process, for debugging shared state
```

`pytest-xdist` is configured with `-n auto --dist loadgroup`. Browser and `slow` tests share one worker because they compete for CPU; the other tests use all workers. The markers are declared in `pyproject.toml`: `browser` starts a browser or preview server, `sandbox` builds or runs app code in a real sandbox, and `slow` takes several seconds or more. Use `uv run pytest --durations=10` when adding a slow test.

## Coding conventions

- Typed Python 3.14+. Every public function has type hints and a one-line docstring. Lines are at most 100 characters.
- Keep the public API small. If something can live in an example or a helper instead of the core, put it there.
- A dependency is welcome when it is the simplest way to do the job, rather than a hundred lines of code or work moved to the wrong place. Say what it does, its licence, its size, and whether it needs system libraries. The SDK's runtime dependencies are the exception, as `ARCHITECTURE.md` says. daisyUI and Chart.js are vendored in the SDK by `scripts/vendor-screens.py`, and Lucide's icons by `scripts/vendor-icons.py`. Example apps may have their own.
- An error meant for an agent says what was wrong and what to do next, in one or two plain sentences.
- The server's tests are a package: `packages/server/tests/` has an `__init__.py`, its files are named `test_server_<topic>.py`, and shared helpers go in `server_helpers.py`. Don't add an `__init__.py` to `packages/sdk/tests/`.
- No sleeping in tests. Wait for a port, a file or a condition, with a deadline.
- A real Fly Sprite costs money, so the tests against one run only by hand, from `.github/workflows/sprites.yml`. A Sprite made for a test is named `cliquet-test-…`, and deleted when the test ends, even when it fails.
- The examples in `examples/` are apps built with the builder, one for each kind of app the builder's guides teach from. A change to the SDK keeps them passing.

### The app-facing SDK

Apps depend on these three things. Changing one breaks apps, so mark the commit `!` and say what apps must change. Everything else under `packages/sdk/src/cliquet/` is plumbing: reshape it when that makes it simpler. The `cliquet` CLI, with `new`, `manifest`, `check`, `test`, `serve` and `preview`, is a tool for developing Cliquet, its examples and its tests: apps run only in a Cliquet server, so it is free to change.

- What `cliquet` exports: `App`, `ActionContext`, `QueryContext`, `InvariantContext`, `Caller`, `Refused`, `Denied`, `Screen`, `BaseModel`, `Field` and `File`, with what an action's `File` argument gets: a read-only path in its working folder, gone after the call.
- How a screen calls the app: the `data-call`, `data-view`, `data-args`, `data-then`, `data-chart`, `data-grid` and `data-icon` attributes, the `layout-` classes, the chart and grid formats, and the icons' names, as the builder's views guide describes them.
- `cliquet.testing`: `given`, `World`, `Connection`, `Transaction`, `Outcome` and `InvalidArguments`, and the bytes or the `Path` that `call` takes for a file argument.
- The built-in tools' names and arguments: `app_history(limit, since)`, `app_changes_since(query, arguments, since)`, `app_revert(commit, app_request_id, app_remark)`, `app_begin_transaction()`, `app_transaction_calls(app_transaction)`, `app_commit_transaction(app_transaction)`, `app_rollback_transaction(app_transaction)` and `app_ask_for_file(action, argument)`, with the reference it answers and the `app_file` format of a file argument's schema, the `app_request_id`, `app_remark`, `app_if_unchanged_since` and `app_transaction` arguments the server adds to an app's actions, queries and views, the `app_data_version` it adds to every query's answer, and the `app_data_version` of `app_begin_transaction`'s answer, the version to pass as `since`, and of `app_commit_transaction`'s, the version it leaves. Every name Cliquet adds starts with `app_`, and no app may use that prefix.

## Commits

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/): `<type>(<scope>): <summary>`, then a blank line and a body.

Only these types and scopes are accepted. `cz check` enforces them, with the rest of this section's rules, from the pattern in `pyproject.toml`: the commit-msg hook runs it before each commit, and CI on every new commit, unless the push changes only the top-level docs or the website.

- **Type:** one of
  - `feat`: new behaviour, anywhere: the SDK, the server, the CLI or an example;
  - `fix`: a bug, slowness that hurts someone included;
  - `docs`: words only;
  - `test`: tests only;
  - `refactor`: no change in behaviour, a speed-up nobody was waiting for included;
  - `chore`: the rest, such as packaging, dependencies, CI and tooling.
- **Scope:** optional. The area touched, one of
  - `sdk`: what apps import, and the test harness;
  - `runtime`: actions, invariants, data, history, transactions and migrations;
  - `mcp`: tools and endpoints: app endpoints, `/me` and the built-in `app_` tools;
  - `screens`: views, the renderer, the screen vocabulary and each app's own pages;
  - `console`: the pages where people build and manage apps: home, build, manage, sharing and tokens;
  - `builder`: drafts, preview, publish, upload and the builder's guides;
  - `sandbox`: Sprites, the sandbox service, building versions and screenshots;
  - `auth`: sign-in, tokens, sharing access and the sign-in provider;
  - `server`: the rest of the server: settings, database, registry, image and deploy;
  - `cli`: the `cliquet` CLI;
  - `examples`: the example apps;
  - `website`: the static website in `website/`.

  A commit that touches only tests, scripts or the repository's own config has no scope.
- **Summary:** imperative, lower case, no final period, and a first line of at most 72 characters. Say what changes, not how: `feat(sdk): type call and query with ParamSpec`.
- **Body:** optional. Explain why. Wrap at 72 characters.
- **Breaking change:** add `!` after the type or scope, and a `BREAKING CHANGE:` footer saying what apps must change. Any change to the app-facing SDK that breaks an existing app counts.
- One logical change per commit, kept small. A change and its docs or tests go in the same commit.

## Definition of done

- `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check` and `scripts/check-examples.sh` pass.
- The full suite is green: nothing fails, and there is no list of known failures. A test that needs something this machine lacks is skipped with its reason. As root, for example, the tests that a query or a view can't write are skipped, because root ignores file permissions. CI doesn't run as root, so it runs them.
- No test reaches the network, except the package index that uv and the wheel cache use. Fake any other service, as the server's tests fake the sign-in provider's discovery document and keys.
- The builder's guides change with what an app may do, and `ARCHITECTURE.md` with an architectural decision.

## Data and writing

- Never commit real personal or financial data. Examples use made-up fixtures.
- Write docs in plain, short English sentences.
- The product has two written forms. In running text it is Cliquet: "Cliquet keeps your data". The wordmark, in the logo, the page titles and the names a sign-in or a website shows, is cliquet.dev, in lower case.

### The docs

The docs in `website/docs/` are for people: most don't write code, and some host a server. They follow the shape of Zensical's own docs, and Mintlify's guide to writing docs.

- **Structure.** The Quickstart is the first page, then one page per task people do (create and publish, share, undo, connect agents), then **Guides** for how Cliquet works and for reference. Each subject has one page, which others link to rather than repeat. A new page goes in `nav`, in `website/zensical.toml`: a server test fails when a page is missing from it, and builds the docs strictly, so a broken link or anchor fails too.
- **A page** opens with a sentence or two saying what it covers, which the theme shows as its lead. Front matter gives it a `description` and a Lucide `icon` for the menu.
- **Headings** are short nouns or imperatives in sentence case, never numbered and never a question.
- **Words.** Second person, active voice, present tense, sentences under 25 words. Define a term where it first matters, then use only that term: an *assistant* is what a person talks to, an *agent* any AI that uses an app, a *routine* an agent that runs on its own, the *console* the website at app.cliquet.dev. Name buttons and pages exactly as the console shows them, in bold.
- **Examples** follow the made-up office of `examples/meeting-rooms`, with its real rooms, roles and screens.
- **Procedures** are numbered steps in a `<div class="steps" markdown>`, each step a bold action and a line or two. Alternatives, such as one per assistant, go in content tabs, and nothing links into a tab, which strict mode refuses.
- **Callouts** are rare, and titled by their subject: `!!! warning "Your login is permanent"`.
- **Pictures.** Screenshots of the console, in `assets/screens/`, come in a light and a dark version, shown with `#only-light` and `#only-dark` and the class `cq-shot`, with made-up data and `app.cliquet.dev` addresses. Diagrams come from `scripts/docs-diagrams.py`.
- **Truth.** Every fact is checked against the code. The docs never send people to an email address for an account or for the right to build: they say only what is true of every account.

# Environments

The register of the team's deployed environments. For each environment it keeps its customer, purpose, address, deployment, whether the customer's users log in, the rules engine namespace it calls, its live and nightly feeds, its Slack channel, its lifecycle and successor, and its connections to the customers' and partners' systems, each with its tier (production or test) and the access the environment has (read or read/write).

From that it answers what an agent must know before touching anything: what breaks if a system or an engine namespace goes down, whether an activity is safe on an environment and why, where to run it instead, and what the register doesn't know yet. The app reaches no network: you read the sources with your own tools and record what they say here.

## When to look

Look an environment up here before you change deployment configuration, infrastructure, a customer's setup, or anything that runs against an environment: a test, a data reset, a migration, a deploy. Never guess an environment's name, address, customer or connections from code or memory: `environments` and `environment` have them. If the register doesn't know something, say so and ask; don't fill the gap with a guess.

## Roles

- `reader`: reads everything, writes nothing. Give it to the team's coding agents.
- `editor`: also records environments, systems, connections, lifecycles and Slack channels. Give it to the agent that keeps the register in step with the wiki and the deployment code.
- `admin`: also removes an environment or a system for good.

## Before you change an environment

Call `guardrails` with the environment and the activity before a destructive test, a data reset, a load test or deploying an unreleased build. Its verdict is binding:

- `ok`: go ahead.
- `ask_first`: tell the person each reason and wait for their go-ahead. The reasons name who could be affected: a customer's users, a customer's test system, the environments sharing an engine namespace.
- `no`: don't. The reasons say what forbids it. Use `where_can_i` with the same activity to propose environments where it is allowed.

Access the register doesn't know counts as a possible write. When a reason says "its access isn't recorded", find out, and have an editor record it with `set_connections`: that changes the verdict for everyone.

## Questions

- "X is down, or will change: what is affected?": `impact` with a system's name (`systems` lists them) or an engine namespace.
- "Where can I run a load test for Contoso?": `where_can_i` with `activity` and `customer`.
- "Tell me about contoso-acc2": `environment`: connections, engine and who shares it, feeds, Slack channel, guardrails and gaps, in one call.
- "Which environments does Fabrikam have?": `environments` with `customer`; `include_retired` for the ones that are gone.
- "What does the register still miss?": `scorecard`, then fill what you can.
- "What changed in the register since last month?": `app_changes_since` with `environments` or `systems` and the date.

## Recording (editor)

- **An environment:** `upsert_environment` adds one, or updates its description from the wiki: customer, purpose, address, deployment, customer users, engine, feeds and page. It never touches the lifecycle, Slack channel or connections. It answers `added`, `updated` or `unchanged`: tell the person what changed. An environment's name ends with its kind: `-prod`, `-preprod`, `-acc` (with a number for several), `-staging`; any other end is `Other`.
- **A system:** `upsert_system` before connecting it. `tier` is `production` when it holds the customer's live data, `test` otherwise, `unknown` until someone says. Name each instance on its own, such as `Contoso ERP prod` and `Contoso ERP E2E1`, never a product alone.
- **Connections:** `set_connections` replaces all of an environment's connections. Read them with `environment` first, send the whole list back, and pass that answer's `app_data_version` as `app_if_unchanged_since`, so that a change made meanwhile isn't lost. `access` is `read`, `read_write`, or `unknown` when the source doesn't say: never guess. Put what no connection can say, such as an integration in progress, in `integration_notes`.
- **Lifecycle:** `set_lifecycle`: `deprecated` while it still runs but is being replaced, with its `successor`; `retired` once gone. Don't remove a retired environment: its history stays.
- **Slack:** a customer's channel, such as `#contoso`, goes to all its environments at once with `set_customer_slack`; `set_slack_channel` sets one environment's.

Remark for each: the source, such as "wiki page of contoso-acc2, 26 November", or what the person asked.

## When a write is refused

- **Environment names are unique / System names are unique, each with a known tier:** use the upsert actions, which replace by name. A tier is `production`, `test` or `unknown`.
- **Two environments never share a wiki page:** the `page` belongs to another environment: check the row you copied.
- **Each environment has a known deployment, lifecycle and customer-users flag:** the deployment is `Versioned release`, `Continuous integration` or empty.
- **A successor is another environment that isn't retired:** retire the successor's predecessors first, or name another successor.
- **A connection links a known environment to a known system, once, with a known access:** add the system with `upsert_system` first.
- **No key, token or password is written in the register:** a free-text field looks like it holds a credential. Leave the credential out and name where it is kept instead, such as the vault's path.

## The screens

- `catalog` (the Catalog tab): every environment in a table people sort, search and filter, with its customer, kind, lifecycle, customer users, what it writes to in production, systems, engine, Slack channel and gaps. Show it when someone wants to browse or filter the environments.
- `details`, with `name`: one environment in full, chosen in a select: what the register doesn't know yet, its purpose, address, Slack channel and wiki page, its guardrails with their reasons, its connections and engine, and its feeds. Show it beside an answer about one environment.

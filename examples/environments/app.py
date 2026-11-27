# /// script
# requires-python = ">=3.14"
# dependencies = ["cliquet-sdk>=0.0.1,<0.1"]
#
# [tool.uv.sources]
# cliquet-sdk = { path = "../../packages/sdk", editable = true }
# ///
"""The register of a software team's deployed environments, for its agents: what each is, what
it connects to, what may break it, and what is safe to do on it."""

import csv
import re
from typing import Annotated, Literal, cast, get_args

from cliquet import (
    ActionContext,
    App,
    BaseModel,
    Field,
    InvariantContext,
    QueryContext,
    Refused,
    Screen,
)

ENVIRONMENTS = "environments.csv"
SYSTEMS = "systems.csv"
CONNECTIONS = "connections.csv"

EVERYONE = ["admin", "editor", "reader"]
WRITERS = ["admin", "editor"]

register = App(
    "environments",
    files=[ENVIRONMENTS, SYSTEMS, CONNECTIONS],
    roles=EVERYONE,
    accent="#0e7490",
    icon="server",
    timezone="Europe/Paris",
    language="en",
)

COLUMNS = {
    ENVIRONMENTS: [
        "name", "customer", "purpose", "url", "deployment", "customer_users", "engine",
        "live_feeds", "reconciliation_feeds", "page",
        "lifecycle", "successor", "slack_channel", "integration_notes",
    ],
    SYSTEMS: ["name", "owner", "tier", "note"],
    CONNECTIONS: ["environment", "system", "access", "protocols", "note"],
}  # fmt: skip

Kind = Literal["Production", "Pre-production", "Acceptance", "Staging", "Other"]
KINDS: list[str] = list(get_args(Kind))
SUFFIXES: dict[str, Kind] = {
    "prod": "Production",
    "preprod": "Pre-production",
    "acc": "Acceptance",
    "staging": "Staging",
}
Lifecycle = Literal["active", "deprecated", "retired"]
Tier = Literal["production", "test", "unknown"]
Access = Literal["read", "read_write", "unknown"]
Protocol = Literal["API", "SFTP", "MQ", "S3", "Database", "Other"]
Deployment = Literal["Versioned release", "Continuous integration"]
Activity = Literal["destructive_test", "data_reset", "load_test", "unreleased_build"]
Verdict = Literal["ok", "ask_first", "no"]
ACTIVITIES: dict[str, str] = {
    "destructive_test": "Destructive test",
    "data_reset": "Data reset",
    "load_test": "Load test",
    "unreleased_build": "Unreleased build",
}
# The engine's stand-in: an environment that calls it shares nothing with another.
STUB_ENGINE = "stub"

EnvName = Annotated[
    str,
    Field(
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="An environment's name, such as contoso-prod, as listed by `environments`",
    ),
]
SystemName = Annotated[
    str,
    Field(
        pattern=r"\S",
        description="A system's name, such as Contoso ERP prod, as listed by `systems`",
    ),
]
Customer = Annotated[
    str | None,
    Field(description="Only this customer's, such as Contoso, as `environments` names them"),
]

# Free text that looks like a credential: a key, a token, a password or a private key.
SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*\S"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}"
)


# ---------------------------------------------------------------- files


def _read(file: str) -> list[dict[str, str]]:
    """The rows of a data file, none if it doesn't exist yet."""
    try:
        with open(file, newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _write(file: str, rows: list[dict[str, str]]) -> None:
    """Rewrite a data file, sorted by its first columns."""
    key = COLUMNS[file][:2]
    rows.sort(key=lambda r: tuple(r[k].lower() for k in key))
    with open(file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS[file])
        writer.writeheader()
        writer.writerows(rows)


def _split(value: str) -> list[str]:
    return [x for x in value.split(";") if x]


def _joined(values: list[str]) -> str:
    return ";".join(dict.fromkeys(v.strip() for v in values if v.strip()))


# ---------------------------------------------------------------- models


class EnvironmentDescription(BaseModel):
    """An environment as the team's wiki describes it."""

    name: EnvName
    customer: str = Field("", description="The customer's name, empty for an internal one")
    purpose: str = Field("", description="What the environment is for, in a sentence")
    url: str = Field("", description="Its address, with any extra hosts after it")
    deployment: Deployment | None = None
    customer_users: bool | None = Field(
        None, description="Whether the customer's users log in; none if unknown"
    )
    engine: str = Field(
        "", description="The rules engine's namespace it calls, empty if none, stub for a stand-in"
    )
    live_feeds: list[str] = Field([], description="Real-time feeds, such as Orders or Shipments")
    reconciliation_feeds: list[str] = Field([], description="Nightly reconciliation feeds")
    page: str = Field("", description="Its page in the team's wiki")


class EnvironmentSummary(BaseModel):
    name: str
    customer: str
    kind: Kind = Field(description="From its name's end: -prod, -preprod, -acc…, -staging")
    lifecycle: Lifecycle
    purpose: str
    url: str
    slack_channel: str


class Connection(BaseModel):
    system: str
    owner: str = Field(description="Who runs the system, usually the customer")
    tier: Tier
    access: Access
    protocols: list[str]
    note: str


class ConnectionInput(BaseModel):
    system: SystemName
    access: Access = Field(description="read, read_write, or unknown when the source doesn't say")
    protocols: list[Protocol] = Field([])
    note: str = ""


class Guardrail(BaseModel):
    activity: Activity
    verdict: Verdict = Field(
        description="ok: go ahead; ask_first: ask the people the reasons name; no: don't"
    )
    reasons: list[str]


class EnvironmentCard(BaseModel):
    """Everything about one environment, for an agent to act on it."""

    name: str
    customer: str
    kind: Kind
    purpose: str
    url: str
    deployment: str | None
    customer_users: bool | None
    lifecycle: Lifecycle
    successor: str
    slack_channel: str
    engine: str
    engine_shared_with: list[str]
    live_feeds: list[str]
    reconciliation_feeds: list[str]
    connections: list[Connection]
    integration_notes: str
    guardrails: list[Guardrail]
    gaps: list[str] = Field(description="What the register doesn't know yet about it")
    page: str


class SystemInfo(BaseModel):
    name: str
    owner: str
    tier: Tier
    note: str
    environments: list[str]


class Affected(BaseModel):
    environment: str
    customer: str
    kind: Kind
    lifecycle: Lifecycle
    access: str = Field(description="How it uses the target: read, read_write, unknown, or calls")
    customer_users: bool | None


class Impact(BaseModel):
    target: str
    target_kind: Literal["system", "engine"]
    owner: str
    tier: str
    affected: list[Affected]


class Placement(BaseModel):
    environment: str
    customer: str
    kind: Kind
    verdict: Verdict
    reasons: list[str]


class CheckResult(BaseModel):
    check: str
    label: str
    passing: int
    failing: list[str]


class Scorecard(BaseModel):
    score: int = Field(description="Percent of checks passing, over every environment not retired")
    checks: list[CheckResult]


# ---------------------------------------------------------------- reading the graph


def _kind(name: str) -> Kind:
    """The kind an environment's name ends with: contoso-acc2 is an acceptance one."""
    return SUFFIXES.get(name.rsplit("-", 1)[-1].rstrip("0123456789"), "Other")


def _lifecycle(row: dict[str, str]) -> Lifecycle:
    """A row's lifecycle, one of the choices the `well_formed` rule keeps it to."""
    return cast(Lifecycle, row["lifecycle"])


def _users(value: str) -> bool | None:
    return None if value == "" else value == "yes"


GAPS = [
    ("url", "URL"),
    ("deployment", "Deployment"),
    ("customer_users", "Whether customer users log in"),
    ("slack", "Slack channel"),
    ("access", "Access to each system"),
    ("tier", "Tier of each system"),
]


class _Graph:
    """The three files, read once per call."""

    def __init__(self) -> None:
        self.envs = {r["name"]: r for r in _read(ENVIRONMENTS)}
        self.systems = {r["name"]: r for r in _read(SYSTEMS)}
        self.links = _read(CONNECTIONS)

    def known(self, name: str) -> dict[str, str]:
        if name not in self.envs:
            raise Refused(f"There is no environment {name}. See `environments` for the names.")
        return self.envs[name]

    def customer(self, customer: str | None) -> None:
        """Refuse a customer no environment belongs to, naming the ones there are."""
        names = sorted({r["customer"] for r in self.envs.values() if r["customer"]})
        if customer is not None and customer not in names:
            raise Refused(f"No environment belongs to {customer}. Customers: {', '.join(names)}.")

    def connections(self, env: str) -> list[Connection]:
        out = []
        for c in self.links:
            if c["environment"] != env:
                continue
            s = self.systems.get(c["system"], {"owner": "", "tier": "unknown"})
            out.append(
                Connection(
                    system=c["system"],
                    owner=s["owner"],
                    tier=cast(Tier, s["tier"]),
                    access=cast(Access, c["access"]),
                    protocols=_split(c["protocols"]),
                    note=c["note"],
                )
            )
        return out

    def engine_peers(self, env: str) -> list[str]:
        engine = self.envs[env]["engine"]
        if not engine or engine == STUB_ENGINE:
            return []
        return [
            n
            for n, r in self.envs.items()
            if n != env and r["engine"] == engine and r["lifecycle"] != "retired"
        ]

    def guardrails(self, env: str) -> list[Guardrail]:
        r = self.envs[env]
        conns = self.connections(env)
        retired = ["It is retired"] if r["lifecycle"] == "retired" else []
        prod = ["It is a production environment"] if _kind(env) == "Production" else []
        writes_prod = [
            f"It writes to {c.system} ({c.owner}, production)"
            for c in conns
            if c.tier == "production" and c.access == "read_write"
        ]
        maybe_prod = [
            f"It may write to {c.system}: its access isn't recorded"
            if c.access == "unknown"
            else f"It writes to {c.system}, whose tier isn't recorded"
            for c in conns
            if (c.tier == "production" and c.access == "unknown")
            or (c.tier == "unknown" and c.access != "read")
        ]
        # Access the register doesn't know counts as a possible write, to a test system too.
        maybe_test = [
            f"It may write to {c.system} ({c.owner}, test): its access isn't recorded"
            for c in conns
            if c.tier == "test" and c.access == "unknown"
        ]
        reads_prod = [
            f"It reads {c.system} ({c.owner}, production)"
            for c in conns
            if c.tier == "production" and c.access == "read"
        ]
        writes_test = [
            f"It writes to {c.system} ({c.owner}, test)"
            for c in conns
            if c.tier == "test" and c.access == "read_write"
        ]
        users = (
            [f"{r['customer'] or 'Customer'} users log in"] if r["customer_users"] == "yes" else []
        )
        peers = self.engine_peers(env)
        shared = [f"It shares engine {r['engine']} with {', '.join(peers)}"] if peers else []
        pinned = ["It follows versioned releases"] if r["deployment"] == "Versioned release" else []

        def rail(activity: Activity, no: list[str], ask: list[str]) -> Guardrail:
            no = retired + no
            verdict: Verdict = "no" if no else "ask_first" if ask else "ok"
            # A "no" gives only what forbids it: what to ask about no longer matters.
            reasons = no or ask or ["Nothing recorded that it could harm"]
            return Guardrail(activity=activity, verdict=verdict, reasons=reasons)

        return [
            rail(
                "destructive_test",
                prod + writes_prod,
                maybe_prod + writes_test + maybe_test + users + shared,
            ),
            rail("data_reset", prod, writes_prod + maybe_prod + users + shared),
            rail(
                "load_test",
                prod + writes_prod,
                maybe_prod + reads_prod + writes_test + maybe_test + users + shared,
            ),
            rail("unreleased_build", prod, users + pinned),
        ]

    def gaps(self, env: str) -> list[tuple[str, str]]:
        """The checks this environment fails, as (check, label)."""
        r = self.envs[env]
        conns = self.connections(env)
        passed = {
            "url": bool(r["url"]),
            "deployment": bool(r["deployment"]),
            "customer_users": r["customer_users"] != "",
            "slack": bool(r["slack_channel"]),
            "access": all(c.access != "unknown" for c in conns),
            "tier": all(c.tier != "unknown" for c in conns),
        }
        return [(check, label) for check, label in GAPS if not passed[check]]

    def card(self, env: str) -> EnvironmentCard:
        r = self.envs[env]
        return EnvironmentCard(
            name=env,
            customer=r["customer"],
            kind=_kind(env),
            purpose=r["purpose"],
            url=r["url"],
            deployment=r["deployment"] or None,
            customer_users=_users(r["customer_users"]),
            lifecycle=_lifecycle(r),
            successor=r["successor"],
            slack_channel=r["slack_channel"],
            engine=r["engine"],
            engine_shared_with=self.engine_peers(env),
            live_feeds=_split(r["live_feeds"]),
            reconciliation_feeds=_split(r["reconciliation_feeds"]),
            connections=self.connections(env),
            integration_notes=r["integration_notes"],
            guardrails=self.guardrails(env),
            gaps=[label for _, label in self.gaps(env)],
            page=r["page"],
        )

    def summary(self, env: str) -> EnvironmentSummary:
        r = self.envs[env]
        return EnvironmentSummary(
            name=env,
            customer=r["customer"],
            kind=_kind(env),
            lifecycle=_lifecycle(r),
            purpose=r["purpose"],
            url=r["url"],
            slack_channel=r["slack_channel"],
        )

    def impact(self, target: str) -> Impact:
        if target in self.systems:
            s = self.systems[target]
            used = [(c["environment"], c["access"]) for c in self.links if c["system"] == target]
            kind, owner, tier = "system", s["owner"], s["tier"]
        elif target != STUB_ENGINE and any(r["engine"] == target for r in self.envs.values()):
            used = [(n, "calls") for n, r in self.envs.items() if r["engine"] == target]
            kind, owner, tier = "engine", "", ""
        else:
            raise Refused(
                f"{target} is neither a system nor an engine namespace. "
                "See `systems` and `environments`."
            )
        affected = [
            Affected(
                environment=env,
                customer=self.envs[env]["customer"],
                kind=_kind(env),
                lifecycle=_lifecycle(self.envs[env]),
                access=access,
                customer_users=_users(self.envs[env]["customer_users"]),
            )
            for env, access in used
        ]
        affected.sort(key=lambda a: (KINDS.index(a.kind), a.environment))
        return Impact(target=target, target_kind=kind, owner=owner, tier=tier, affected=affected)

    def scorecard(self, only: str | None = None) -> Scorecard:
        names = [
            n
            for n, r in self.envs.items()
            if r["lifecycle"] != "retired" and (only is None or n == only)
        ]
        checks = {c: CheckResult(check=c, label=label, passing=0, failing=[]) for c, label in GAPS}
        for n in names:
            failed = {c for c, _ in self.gaps(n)}
            for check, result in checks.items():
                if check in failed:
                    result.failing.append(n)
                else:
                    result.passing += 1
        total = len(names) * len(checks)
        passing = sum(c.passing for c in checks.values())
        score = round(100 * passing / total) if total else 100
        return Scorecard(score=score, checks=list(checks.values()))


# ---------------------------------------------------------------- actions


@register.action(roles=WRITERS)
def upsert_environment(
    ctx: ActionContext, environment: EnvironmentDescription
) -> Literal["added", "updated", "unchanged"]:
    """Add an environment, or update the one with the same name from its description. Keeps its
    lifecycle, Slack channel and connections, which other actions set."""
    g = _Graph()
    old = g.envs.get(environment.name)
    new = dict(old) if old else {c: "" for c in COLUMNS[ENVIRONMENTS]} | {"lifecycle": "active"}
    e = environment
    users = "" if e.customer_users is None else "yes" if e.customer_users else "no"
    new.update(
        name=e.name,
        customer=e.customer.strip(),
        purpose=e.purpose.strip(),
        url=e.url.strip(),
        deployment=e.deployment or "",
        customer_users=users,
        engine=e.engine.strip(),
        live_feeds=_joined(e.live_feeds),
        reconciliation_feeds=_joined(e.reconciliation_feeds),
        page=e.page.strip(),
    )
    if old == new:
        return "unchanged"
    g.envs[e.name] = new
    _write(ENVIRONMENTS, list(g.envs.values()))
    return "added" if old is None else "updated"


@register.action(roles=WRITERS)
def set_lifecycle(
    ctx: ActionContext,
    environment: EnvName,
    lifecycle: Lifecycle,
    successor: Annotated[
        str,
        Field(description="The environment that replaces it, if any, as listed by `environments`"),
    ] = "",
) -> None:
    """Mark an environment active, deprecated (still running, being replaced) or retired
    (gone), with its successor."""
    g = _Graph()
    g.known(environment)
    if successor and successor not in g.envs:
        raise Refused(
            f"There is no environment {successor} to succeed it. See `environments` for the names."
        )
    g.envs[environment].update(lifecycle=lifecycle, successor=successor)
    _write(ENVIRONMENTS, list(g.envs.values()))


@register.action(roles=WRITERS)
def set_slack_channel(
    ctx: ActionContext,
    environment: EnvName,
    slack_channel: Annotated[
        str,
        Field(pattern=r"^(#[a-z0-9._-]+)?$", description="Its channel, such as #contoso, or empty"),
    ],
) -> None:
    """Set the Slack channel where people talk about one environment. For all of a
    customer's, use `set_customer_slack`."""
    g = _Graph()
    g.known(environment)
    g.envs[environment]["slack_channel"] = slack_channel
    _write(ENVIRONMENTS, list(g.envs.values()))


@register.action(roles=WRITERS)
def set_customer_slack(
    ctx: ActionContext,
    customer: Annotated[
        str,
        Field(
            pattern=r"\S", description="A customer, as `environments` names them, such as Contoso"
        ),
    ],
    slack_channel: Annotated[
        str,
        Field(pattern=r"^#[a-z0-9._-]+$", description="The customer's channel, such as #contoso"),
    ],
) -> int:
    """Set the Slack channel of every environment of a customer, and answer how many
    changed."""
    g = _Graph()
    g.customer(customer)
    changed = 0
    for r in g.envs.values():
        if r["customer"] == customer and r["slack_channel"] != slack_channel:
            r["slack_channel"] = slack_channel
            changed += 1
    _write(ENVIRONMENTS, list(g.envs.values()))
    return changed


@register.action(roles=WRITERS)
def set_connections(
    ctx: ActionContext,
    environment: EnvName,
    connections: list[ConnectionInput],
    integration_notes: Annotated[
        str, Field(description="What its connections can't say, such as an integration in progress")
    ] = "",
) -> None:
    """Replace every system an environment connects to, each with its access and protocols.
    Read them with `environment` first, and send the whole list back."""
    g = _Graph()
    g.known(environment)
    unknown = sorted({c.system for c in connections} - set(g.systems))
    if unknown:
        raise Refused(
            f"Unknown systems: {', '.join(unknown)}. Add them with `upsert_system` first."
        )
    rows = [c for c in g.links if c["environment"] != environment]
    rows += [
        {
            "environment": environment,
            "system": c.system,
            "access": c.access,
            "protocols": _joined(list(c.protocols)),
            "note": c.note.strip(),
        }
        for c in connections
    ]
    _write(CONNECTIONS, rows)
    g.envs[environment]["integration_notes"] = integration_notes.strip()
    _write(ENVIRONMENTS, list(g.envs.values()))


@register.action(roles=WRITERS)
def upsert_system(
    ctx: ActionContext,
    name: SystemName,
    owner: Annotated[str, Field(description="Who runs it, such as Contoso or Relecloud")],
    tier: Annotated[Tier, Field(description="production when it holds the customer's live data")],
    note: str = "",
) -> None:
    """Add a system environments connect to, or update the one with the same name. Name each
    instance on its own, such as Contoso ERP prod and Contoso ERP E2E1."""
    name = name.strip()
    rows = [r for r in _read(SYSTEMS) if r["name"] != name]
    rows.append({"name": name, "owner": owner.strip(), "tier": tier, "note": note.strip()})
    _write(SYSTEMS, rows)


@register.action(roles=["admin"], destructive=True)
def remove_system(ctx: ActionContext, name: SystemName) -> None:
    """Remove a system no environment connects to any more."""
    g = _Graph()
    if name not in g.systems:
        raise Refused(f"There is no system {name}. See `systems` for the names.")
    users = sorted(c["environment"] for c in g.links if c["system"] == name)
    if users:
        raise Refused(
            f"{name} is still connected to {', '.join(users)}. Change their connections first."
        )
    _write(SYSTEMS, [r for n, r in g.systems.items() if n != name])


@register.action(roles=["admin"], destructive=True)
def remove_environment(ctx: ActionContext, name: EnvName) -> None:
    """Remove an environment for good, with its connections, such as one added by mistake.
    For one that is gone, use `set_lifecycle` retired instead."""
    g = _Graph()
    g.known(name)
    for r in g.envs.values():
        if r["successor"] == name:
            raise Refused(
                f"{r['name']} names {name} as its successor. Change it with `set_lifecycle` first."
            )
    _write(ENVIRONMENTS, [r for n, r in g.envs.items() if n != name])
    _write(CONNECTIONS, [c for c in g.links if c["environment"] != name])


# ---------------------------------------------------------------- invariants


def _dupes(values: list) -> list:
    seen = set()
    out = []
    for v in values:
        if v in seen:
            out.append(v)
        seen.add(v)
    return out


@register.invariant("Environment names are unique")
def unique_environments(ctx: InvariantContext) -> list[str]:
    names = [r["name"] for r in _read(ENVIRONMENTS)]
    return [f"Environment {n} appears twice" for n in _dupes(names)]


@register.invariant("Two environments never share a wiki page")
def unique_pages(ctx: InvariantContext) -> list[str]:
    first: dict[str, str] = {}
    problems = []
    for r in _read(ENVIRONMENTS):
        page = r["page"]
        if page and page in first:
            problems.append(f"{r['name']} and {first[page]} have the same wiki page")
        first.setdefault(page, r["name"])
    return problems


@register.invariant("Each environment has a known deployment, lifecycle and customer-users flag")
def well_formed(ctx: InvariantContext) -> list[str]:
    problems = []
    for r in _read(ENVIRONMENTS):
        if r["deployment"] not in ("", *get_args(Deployment)):
            problems.append(f"{r['name']} has an unknown deployment: {r['deployment']}")
        if r["customer_users"] not in ("", "yes", "no"):
            problems.append(f"{r['name']} says {r['customer_users']!r} for customer users")
        if r["lifecycle"] not in get_args(Lifecycle):
            problems.append(f"{r['name']} has an unknown lifecycle: {r['lifecycle']}")
    return problems


@register.invariant("A successor is another environment that isn't retired")
def valid_successors(ctx: InvariantContext) -> list[str]:
    envs = {r["name"]: r for r in _read(ENVIRONMENTS)}
    problems = []
    for r in envs.values():
        s = r["successor"]
        if not s:
            continue
        if s == r["name"]:
            problems.append(f"{s} names itself as its successor")
        elif s not in envs:
            problems.append(f"{r['name']} names {s} as its successor, which doesn't exist")
        elif envs[s]["lifecycle"] == "retired":
            problems.append(f"{r['name']} names {s} as its successor, which is retired")
    return problems


@register.invariant("System names are unique, each with a known tier")
def valid_systems(ctx: InvariantContext) -> list[str]:
    rows = _read(SYSTEMS)
    problems = [f"System {n} appears twice" for n in _dupes([r["name"] for r in rows])]
    problems += [
        f"{r['name']} has an unknown tier: {r['tier']}"
        for r in rows
        if r["tier"] not in get_args(Tier)
    ]
    return problems


@register.invariant(
    "A connection links a known environment to a known system, once, with a known access"
)
def valid_connections(ctx: InvariantContext) -> list[str]:
    envs = {r["name"] for r in _read(ENVIRONMENTS)}
    systems = {r["name"] for r in _read(SYSTEMS)}
    rows = _read(CONNECTIONS)
    problems = []
    for c in rows:
        label = f"{c['environment']} → {c['system']}"
        if c["environment"] not in envs:
            problems.append(f"{label}: unknown environment")
        if c["system"] not in systems:
            problems.append(f"{label}: unknown system")
        if c["access"] not in get_args(Access):
            problems.append(f"{label}: unknown access {c['access']}")
        if any(p not in get_args(Protocol) for p in _split(c["protocols"])):
            problems.append(f"{label}: unknown protocol in {c['protocols']}")
    twice = _dupes([(c["environment"], c["system"]) for c in rows])
    return problems + [f"{e} → {s} is recorded twice" for e, s in twice]


@register.invariant("No key, token or password is written in the register")
def no_secrets(ctx: InvariantContext) -> list[str]:
    found = []
    for file in (ENVIRONMENTS, SYSTEMS, CONNECTIONS):
        for row in _read(file):
            where = row.get("name") or f"{row['environment']} → {row['system']}"
            found += [
                f"{where}: its {column.replace('_', ' ')} holds what looks like a secret. "
                "Name where the secret is kept instead, such as the vault's path."
                for column, value in row.items()
                if SECRET.search(value)
            ]
    return found


# ---------------------------------------------------------------- queries


@register.query(roles=EVERYONE)
def environments(
    ctx: QueryContext,
    customer: Customer = None,
    kind: Annotated[Kind | None, Field(description="Only this kind of environment")] = None,
    include_retired: bool = False,
) -> list[EnvironmentSummary]:
    """The environments, in short: name, customer, kind, lifecycle, purpose and Slack channel.
    Use `environment` for one in full."""
    g = _Graph()
    g.customer(customer)
    return [
        g.summary(n)
        for n, r in g.envs.items()
        if (include_retired or r["lifecycle"] != "retired")
        and (customer is None or r["customer"] == customer)
        and (kind is None or _kind(n) == kind)
    ]


@register.query(roles=EVERYONE)
def environment(ctx: QueryContext, name: EnvName) -> EnvironmentCard:
    """One environment in full: its connections, engine and who shares it, feeds, Slack
    channel, guardrails and what the register doesn't know yet."""
    g = _Graph()
    g.known(name)
    return g.card(name)


@register.query(roles=EVERYONE)
def systems(
    ctx: QueryContext,
    owner: Annotated[
        str | None, Field(description="Only the systems this company runs, such as Contoso")
    ] = None,
) -> list[SystemInfo]:
    """The systems environments connect to, with their tier and the environments that use
    them."""
    g = _Graph()
    return [
        SystemInfo(
            name=n,
            owner=s["owner"],
            tier=cast(Tier, s["tier"]),
            note=s["note"],
            environments=sorted(c["environment"] for c in g.links if c["system"] == n),
        )
        for n, s in g.systems.items()
        if owner is None or s["owner"] == owner
    ]


@register.query(roles=EVERYONE)
def impact(
    ctx: QueryContext,
    target: Annotated[
        str,
        Field(
            description="A system's name as listed by `systems`, or an engine namespace as "
            "`environments` gives it, such as engine-contoso-test"
        ),
    ],
) -> Impact:
    """What is affected if a system or an engine namespace goes down or changes: each
    environment that uses it, and how."""
    return _Graph().impact(target)


@register.query(roles=EVERYONE)
def guardrails(
    ctx: QueryContext, environment: EnvName, activity: Activity | None = None
) -> list[Guardrail]:
    """Whether an activity is safe on an environment, and why. Call it before anything that
    changes an environment."""
    g = _Graph()
    g.known(environment)
    return [r for r in g.guardrails(environment) if activity is None or r.activity == activity]


@register.query(roles=EVERYONE)
def where_can_i(
    ctx: QueryContext, activity: Activity, customer: Customer = None
) -> list[Placement]:
    """Where an activity is allowed: the environments where it is ok, then those where one
    must ask first, with the reasons."""
    g = _Graph()
    g.customer(customer)
    out = []
    for n, r in g.envs.items():
        if r["lifecycle"] == "retired" or (customer is not None and r["customer"] != customer):
            continue
        rail = next(x for x in g.guardrails(n) if x.activity == activity)
        if rail.verdict != "no":
            out.append(
                Placement(
                    environment=n,
                    customer=r["customer"],
                    kind=_kind(n),
                    verdict=rail.verdict,
                    reasons=rail.reasons,
                )
            )
    out.sort(key=lambda p: (p.verdict != "ok", len(p.reasons), p.environment))
    return out


@register.query(roles=EVERYONE)
def scorecard(
    ctx: QueryContext,
    environment: Annotated[
        str | None, Field(description="Only this environment, as listed by `environments`")
    ] = None,
) -> Scorecard:
    """How complete the register is: each check, how many environments pass it, and which
    fail it."""
    g = _Graph()
    if environment is not None:
        g.known(environment)
    return g.scorecard(environment)


# ---------------------------------------------------------------- the screens

NO_CUSTOMER = "No customer"
FEED_ORDER = ["Orders", "Shipments", "Inventory", "Master data", "Costs"]
TONES = {"ok": "success", "ask_first": "warning", "no": "error"}
VERDICT_LABELS = {"ok": "OK", "ask_first": "Ask first", "no": "No"}
VERDICT_ICONS = {"ok": "circle-check", "ask_first": "triangle-exclamation-point", "no": "octagon-x"}
ACCESS_LABELS = {"read": "Read", "read_write": "Read/write", "unknown": "Unknown"}
ACCESS_TONES = {"read": "badge-info", "read_write": "badge-warning", "unknown": "badge-ghost"}
TIER_TONES = {"production": "badge-error", "test": "badge-success", "unknown": "badge-ghost"}


def _address(url: str) -> str:
    """The address without its scheme, as the screen links it."""
    return re.sub(r"^https?://", "", url.strip())


def _host(url: str) -> str:
    m = re.match(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", _address(url))
    return m.group(0) if m else ""


def _order(g: _Graph) -> list[str]:
    def key(n: str) -> tuple:
        r = g.envs[n]
        return (r["customer"] == "", r["customer"], KINDS.index(_kind(n)), n)

    return sorted(g.envs, key=key)


CATALOG = """
<header>
  <h2>Environments</h2>
  <p class="text-base-content/60">{{ subtitle }}</p>
</header>
{% if grid.rows %}
<div data-grid='{{ grid | tojson }}' aria-label="Environments"></div>
{% else %}
<div class="alert alert-soft" role="status">
  <span data-icon="server"></span><span>No environment is recorded yet.</span>
</div>
{% endif %}
"""


@register.view(roles=EVERYONE, title="Catalog")
def catalog(ctx: QueryContext) -> Screen:
    """Every environment in a table to sort, search and filter: customer, kind, lifecycle,
    customer users, what it writes to in production, systems, engine, Slack channel and
    gaps."""
    g = _Graph()
    rows = []
    for n in _order(g):
        r = g.envs[n]
        conns = g.connections(n)
        users = _users(r["customer_users"])
        writes = [c.system for c in conns if c.tier == "production" and c.access == "read_write"]
        rows.append(
            {
                "name": n,
                "customer": r["customer"] or NO_CUSTOMER,
                "kind": _kind(n),
                "lifecycle": r["lifecycle"].capitalize(),
                "users": None if users is None else "Yes" if users else "No",
                "writes_prod": ", ".join(writes) or None,
                "systems": ", ".join(c.system for c in conns) or None,
                "engine": r["engine"] or None,
                "deployment": r["deployment"] or None,
                "slack": r["slack_channel"] or None,
                "gaps": len(g.gaps(n)),
            }
        )
    grid = {
        "columns": [
            {"field": "name", "header": "Environment"},
            {"field": "customer", "header": "Customer"},
            {"field": "kind", "header": "Kind"},
            {
                "field": "lifecycle",
                "header": "Lifecycle",
                "tones": {"Deprecated": "warning", "Retired": "neutral"},
            },
            {"field": "users", "header": "Customer users"},
            {"field": "writes_prod", "header": "Writes to production"},
            {"field": "systems", "header": "Systems"},
            {"field": "engine", "header": "Engine"},
            {"field": "deployment", "header": "Deployment"},
            {"field": "slack", "header": "Slack"},
            {"field": "gaps", "header": "Gaps", "format": "integer"},
        ],
        "rows": rows,
        "density": "compact",
    }
    running = [r for r in g.envs.values() if r["lifecycle"] != "retired"]
    customers = len({r["customer"] for r in running if r["customer"]})
    subtitle = f"{len(running)} running, for {customers} customer{'s' * (customers != 1)}"
    return Screen(CATALOG, grid=grid, subtitle=subtitle)


DETAILS = """
{% if not env %}
<header>
  <h2>Details</h2>
</header>
<div class="alert alert-soft" role="status">
  <span data-icon="server"></span><span>No environment is recorded yet.</span>
</div>
{% else %}
<header>
  <h2>{{ env.name }}</h2>
  <p class="text-base-content/60">{{ customer }} · {{ env.kind }}</p>
</header>
<form data-view>
  <select class="select" name="name" aria-label="Environment">
    {% for n in options %}
    <option value="{{ n }}" {% if n == env.name %}selected{% endif %}>{{ n }}</option>
    {% endfor %}
  </select>
</form>
{% if env.gaps %}
<div class="alert alert-soft alert-warning" role="status">
  <span data-icon="clipboard-list"></span><span>Not recorded yet: {{ env.gaps | join(", ") }}</span>
</div>
{% endif %}
<section class="card card-border">
  <div class="card-body">
    <p>{{ env.purpose }}</p>
    <div class="layout-row layout-gap-1">
      {% if env.lifecycle != "active" %}
      <span class="badge badge-soft badge-warning">
        <span data-icon="archive"></span> {{ env.lifecycle | capitalize }}</span>
      {% endif %}
      {% if env.deployment %}<span class="badge badge-ghost">{{ env.deployment }}</span>{% endif %}
      {% if env.customer_users %}
      <span class="badge badge-soft badge-info">
        <span data-icon="users"></span> Customer users</span>
      {% elif env.customer_users == false %}
      <span class="badge badge-ghost"><span data-icon="lock"></span> Internal only</span>
      {% endif %}
    </div>
    <table class="table table-sm">
      <tbody>
        <tr>
          <th>Address</th>
          <td>
            {% if host %}<a class="link link-primary" href="https://{{ host }}">{{ host }}</a>
            {% if extra %}<br><small class="text-base-content/60">{{ extra }}</small>{% endif %}
            {% else %}{{ env.url or "—" }}{% endif %}
          </td>
        </tr>
        {% if env.successor %}
        <tr>
          <th>Successor</th>
          <td><a class="link link-primary" data-view
                 data-args='{{ {"name": env.successor} | tojson }}'>{{ env.successor }}</a></td>
        </tr>
        {% endif %}
        <tr><th>Slack</th><td>{{ env.slack_channel or "—" }}</td></tr>
        {% if page %}
        <tr><th>Wiki</th><td><a class="link link-primary" href="{{ page }}">Its page</a></td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>
</section>
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title"><span data-icon="shield"></span> Guardrails</h3>
    <ul class="list">
      {% for g in rails %}
      <li class="list-row">
        <span class="text-{{ g.tone }}"><span data-icon="{{ g.icon }}"></span></span>
        <div class="list-col-grow">
          <div class="layout-row layout-between layout-gap-2">
            <strong>{{ g.label }}</strong>
            <span class="badge badge-sm badge-soft badge-{{ g.tone }} layout-nowrap">
              {{ g.verdict }}</span>
          </div>
          {% for r in g.reasons %}
          <p class="text-base-content/60"><small>{{ r }}</small></p>
          {% endfor %}
        </div>
      </li>
      {% endfor %}
    </ul>
  </div>
</section>
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title"><span data-icon="waypoints"></span> Connections</h3>
    {% if env.connections or env.engine %}
    <ul class="list">
      {% if env.engine %}
      <li class="list-row">
        <span class="text-base-content/60"><span data-icon="cpu"></span></span>
        <div class="list-col-grow">
          <strong>{{ env.engine }}</strong>
          <p class="text-base-content/60"><small>Rules engine</small></p>
          {% if env.engine_shared_with %}
          <div class="layout-row layout-gap-1">
            <small class="text-base-content/60">Shared with</small>
            {% for p in env.engine_shared_with %}
            <a class="badge badge-sm badge-ghost" data-view
               data-args='{{ {"name": p} | tojson }}'>{{ p }}</a>
            {% endfor %}
          </div>
          {% endif %}
        </div>
        <span class="badge badge-sm badge-ghost">Calls</span>
      </li>
      {% endif %}
      {% for c in env.connections %}
      <li class="list-row">
        <span class="text-base-content/60"><span data-icon="plug"></span></span>
        <div class="list-col-grow layout-stack layout-gap-1">
          <strong>{{ c.system }}</strong>
          <small class="text-base-content/60">
            {{- c.owner }}{% if c.protocols %} · {{ c.protocols | join(", ") }}{% endif %}
            {%- if c.note %} · {{ c.note }}{% endif %}</small>
          <div class="layout-row layout-gap-1">
            <span class="badge badge-sm badge-soft {{ tier_tones[c.tier] }}">
              {{ c.tier | capitalize }}</span>
            <span class="badge badge-sm badge-soft {{ access_tones[c.access] }}">
              {{ access_labels[c.access] }}</span>
          </div>
        </div>
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <p class="text-base-content/60">No connection recorded.</p>
    {% endif %}
    {% if env.integration_notes %}
    <p class="text-base-content/60"><small>{{ env.integration_notes }}</small></p>
    {% endif %}
  </div>
</section>
{% if feeds %}
<section class="card card-border">
  <div class="card-body">
    <h3 class="card-title"><span data-icon="arrow-left-right"></span> Feeds</h3>
    <table class="table table-sm">
      <thead>
        <tr>
          <th>Feed</th><th class="layout-center">Live</th><th class="layout-center">Nightly</th>
        </tr>
      </thead>
      <tbody>
        {% for f in feeds %}
        <tr>
          <td>{{ f.name }}</td>
          <td class="layout-center">{{ "Yes" if f.live else "—" }}</td>
          <td class="layout-center">{{ "Yes" if f.reconciliation else "—" }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</section>
{% endif %}
{% endif %}
"""


@register.view(roles=EVERYONE)
def details(
    ctx: QueryContext,
    name: Annotated[
        str,
        Field(
            description="The environment, as listed by `environments`; a production one by default"
        ),
    ] = "",
) -> Screen:
    """One environment in full, chosen in a select: what the register still misses, its
    purpose and address, its guardrails with their reasons, its connections and engine, and
    its feeds."""
    g = _Graph()
    if name:
        g.known(name)
    order = _order(g)
    if not order:
        return Screen(DETAILS, env=None)
    if not name:
        name = next((n for n in order if _kind(n) == "Production"), order[0])
    card = g.card(name)
    host = _host(card.url)
    # Every feed some environment has, so that one this environment lacks shows too.
    known = {
        f
        for r in g.envs.values()
        for f in _split(r["live_feeds"] + ";" + r["reconciliation_feeds"])
    }
    feeds = sorted(
        known, key=lambda f: (FEED_ORDER.index(f) if f in FEED_ORDER else len(FEED_ORDER), f)
    )
    return Screen(
        DETAILS,
        args={"name": name},
        env=card,
        customer=card.customer or NO_CUSTOMER,
        options=order,
        feeds=[
            {
                "name": f,
                "live": f in card.live_feeds,
                "reconciliation": f in card.reconciliation_feeds,
            }
            for f in feeds
        ],
        host=host,
        page=card.page if card.page.startswith(("https://", "http://")) else "",
        extra=_address(card.url)[len(host) :].strip(" ;,/") if host else "",
        rails=[
            {
                "label": ACTIVITIES[x.activity],
                "verdict": VERDICT_LABELS[x.verdict],
                "tone": TONES[x.verdict],
                "icon": VERDICT_ICONS[x.verdict],
                "reasons": x.reasons,
            }
            for x in card.guardrails
        ],
        access_labels=ACCESS_LABELS,
        access_tones=ACCESS_TONES,
        tier_tones=TIER_TONES,
    )

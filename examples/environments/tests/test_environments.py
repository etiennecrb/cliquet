from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from app import (
    Activity,
    ConnectionInput,
    EnvironmentDescription,
    catalog,
    details,
    environment,
    environments,
    guardrails,
    impact,
    no_secrets,
    register,
    remove_environment,
    remove_system,
    scorecard,
    set_connections,
    set_customer_slack,
    set_lifecycle,
    set_slack_channel,
    systems,
    unique_environments,
    unique_pages,
    upsert_environment,
    upsert_system,
    valid_connections,
    valid_successors,
    valid_systems,
    well_formed,
    where_can_i,
)

from cliquet.testing import Connection, Denied, InvalidArguments, Refused, World, given

NOW = datetime(2026, 11, 26, 9, 0, tzinfo=ZoneInfo("Europe/Paris"))

ENVS = """\
name,customer,purpose,url,deployment,customer_users,engine,live_feeds,reconciliation_feeds,\
page,lifecycle,successor,slack_channel,integration_notes
contoso-acc1,Contoso,Contoso acceptance,contoso-acc1.example.com,Versioned release,yes,\
engine-contoso-test,Orders,,https://wiki.example.com/acc1,active,,,
contoso-acc2,Contoso,Contoso acceptance 2,contoso-acc2.example.com,Versioned release,yes,\
engine-contoso-test,,,https://wiki.example.com/acc2,active,,,
contoso-prod,Contoso,Live environment,contoso.example.com,Versioned release,yes,engine-contoso,\
Orders;Shipments,Shipments,https://wiki.example.com/prod,active,,#contoso,
contoso-staging,Contoso,Internal staging,contoso-staging.example.com,Continuous integration,no,\
engine-contoso-staging,,,https://wiki.example.com/staging,active,,,
demo-staging,,Internal demo,demo-staging.example.com,Continuous integration,no,stub,,,\
https://wiki.example.com/demo,active,,,
partner-prod,,Old partner portal,partner.example.com (+ partner2.example.com),Versioned release,\
yes,,,,https://wiki.example.com/partner,deprecated,portal-prod,,
portal-prod,,Partner portal,portal.example.com,Versioned release,yes,,,,\
https://wiki.example.com/portal,active,,,
"""
SYSTEMS = """\
name,owner,tier,note
Contoso ERP E2E1,Contoso,test,
Contoso ERP prod,Contoso,production,
Relecloud API,Relecloud,unknown,
"""
CONNECTIONS = """\
environment,system,access,protocols,note
contoso-acc1,Contoso ERP E2E1,read_write,API,
contoso-prod,Contoso ERP prod,read_write,API;MQ,
contoso-staging,Contoso ERP prod,read,,
partner-prod,Relecloud API,unknown,,
"""


def world() -> World:
    files = {"environments.csv": ENVS, "systems.csv": SYSTEMS, "connections.csv": CONNECTIONS}
    return given(register, files, now=NOW)


def admin(w: World | None = None) -> Connection:
    return (w or world()).connect("admin")


def editor(w: World | None = None) -> Connection:
    return (w or world()).connect("editor")


def reader(w: World | None = None) -> Connection:
    return (w or world()).connect("reader")


def verdict(conn: Connection, env: str, activity: Activity) -> tuple[str, list[str]]:
    (rail,) = conn.query(guardrails, environment=env, activity=activity)
    return rail.verdict, rail.reasons


# ---------------------------------------------------------------- reading


def test_lists_environments_without_retired_ones() -> None:
    w = world()
    editor(w).call(set_lifecycle, environment="demo-staging", lifecycle="retired").expect_ok()
    r = reader(w)
    assert "demo-staging" not in [e.name for e in r.query(environments)]
    assert "demo-staging" in [e.name for e in r.query(environments, include_retired=True)]
    production = r.query(environments, customer="Contoso", kind="Production")
    assert [e.name for e in production] == ["contoso-prod"]


def test_an_unknown_customer_is_refused_with_the_known_ones() -> None:
    with pytest.raises(Refused, match=r"No environment belongs to contoso\. Customers: Contoso\."):
        reader().query(environments, customer="contoso")


def test_an_environment_in_full() -> None:
    e = reader().query(environment, name="contoso-acc1")
    assert e.kind == "Acceptance" and e.engine_shared_with == ["contoso-acc2"]
    connections = [(c.system, c.tier, c.access, c.protocols) for c in e.connections]
    assert connections == [("Contoso ERP E2E1", "test", "read_write", ["API"])]
    assert "Slack channel" in e.gaps and "URL" not in e.gaps


def test_an_unknown_environment_is_refused() -> None:
    with pytest.raises(Refused, match="There is no environment contoso-dev"):
        reader().query(environment, name="contoso-dev")


def test_systems_list_their_environments() -> None:
    erp = next(s for s in reader().query(systems, owner="Contoso") if s.name == "Contoso ERP prod")
    assert erp.environments == ["contoso-prod", "contoso-staging"]


# ---------------------------------------------------------------- impact


def test_impact_of_a_system() -> None:
    hit = reader().query(impact, target="Contoso ERP prod")
    assert hit.tier == "production"
    assert [(a.environment, a.access) for a in hit.affected] == [
        ("contoso-prod", "read_write"),
        ("contoso-staging", "read"),
    ]


def test_impact_of_an_engine_namespace() -> None:
    hit = reader().query(impact, target="engine-contoso-test")
    assert hit.target_kind == "engine"
    assert [a.environment for a in hit.affected] == ["contoso-acc1", "contoso-acc2"]


def test_impact_of_something_unknown_or_the_stub_is_refused() -> None:
    for target in ("Nothing", "stub"):
        with pytest.raises(Refused, match="neither a system nor an engine namespace"):
            reader().query(impact, target=target)


# ---------------------------------------------------------------- guardrails


def test_production_is_hands_off() -> None:
    v, reasons = verdict(reader(), "contoso-prod", "destructive_test")
    assert v == "no" and "It is a production environment" in reasons
    assert "It writes to Contoso ERP prod (Contoso, production)" in reasons


def test_customer_acceptance_asks_first_with_reasons() -> None:
    v, reasons = verdict(reader(), "contoso-acc1", "destructive_test")
    assert v == "ask_first"
    assert reasons == [
        "It writes to Contoso ERP E2E1 (Contoso, test)",
        "Contoso users log in",
        "It shares engine engine-contoso-test with contoso-acc2",
    ]


def test_internal_staging_reading_production_is_ok_to_break_but_not_to_load() -> None:
    r = reader()
    assert verdict(r, "contoso-staging", "destructive_test")[0] == "ok"
    assert verdict(r, "contoso-staging", "load_test") == (
        "ask_first",
        ["It reads Contoso ERP prod (Contoso, production)"],
    )


def test_an_unrecorded_access_counts_as_a_possible_write() -> None:
    w = world()
    unknown = [ConnectionInput(system="Relecloud API", access="unknown")]
    editor(w).call(set_connections, environment="contoso-staging", connections=unknown).expect_ok()
    assert verdict(reader(w), "contoso-staging", "data_reset") == (
        "ask_first",
        ["It may write to Relecloud API: its access isn't recorded"],
    )


def test_an_unrecorded_access_to_a_test_system_asks_first() -> None:
    w = world()
    unknown = [ConnectionInput(system="Contoso ERP E2E1", access="unknown")]
    editor(w).call(set_connections, environment="contoso-staging", connections=unknown).expect_ok()
    assert verdict(reader(w), "contoso-staging", "destructive_test") == (
        "ask_first",
        ["It may write to Contoso ERP E2E1 (Contoso, test): its access isn't recorded"],
    )


def test_a_no_gives_only_what_forbids_it() -> None:
    # Contoso's users log in to production too, but that no longer matters once it's a no.
    assert verdict(reader(), "contoso-prod", "data_reset") == (
        "no",
        ["It is a production environment"],
    )


def test_unreleased_builds_go_to_continuous_integration_environments() -> None:
    r = reader()
    assert verdict(r, "demo-staging", "unreleased_build")[0] == "ok"
    assert verdict(r, "contoso-acc2", "unreleased_build") == (
        "ask_first",
        ["Contoso users log in", "It follows versioned releases"],
    )


def test_a_retired_environment_is_off_limits() -> None:
    w = world()
    editor(w).call(set_lifecycle, environment="demo-staging", lifecycle="retired").expect_ok()
    assert verdict(reader(w), "demo-staging", "destructive_test") == ("no", ["It is retired"])


def test_where_can_i_lists_ok_then_ask_first() -> None:
    places = reader().query(where_can_i, activity="destructive_test", customer="Contoso")
    assert [(p.environment, p.verdict) for p in places] == [
        ("contoso-staging", "ok"),
        ("contoso-acc2", "ask_first"),
        ("contoso-acc1", "ask_first"),
    ]


# ---------------------------------------------------------------- scorecard


def test_scorecard_counts_gaps() -> None:
    card = reader().query(scorecard)
    slack = next(c for c in card.checks if c.check == "slack")
    assert slack.passing == 1 and "contoso-acc1" in slack.failing
    access = next(c for c in card.checks if c.check == "access")
    assert access.failing == ["partner-prod"]
    assert 0 < card.score < 100


def test_filling_gaps_raises_the_score() -> None:
    e = editor()
    before = e.query(scorecard, environment="contoso-acc1").score
    e.call(set_slack_channel, environment="contoso-acc1", slack_channel="#contoso").expect_ok()
    assert e.query(scorecard, environment="contoso-acc1").score > before


def test_a_bad_slack_channel_is_an_invalid_argument() -> None:
    with pytest.raises(InvalidArguments):
        editor().call(set_slack_channel, environment="contoso-acc1", slack_channel="contoso acc")


# ---------------------------------------------------------------- writing


def test_upsert_keeps_what_the_register_holds() -> None:
    e = editor()
    page = EnvironmentDescription(
        name="contoso-prod",
        customer="Contoso",
        purpose="Live",
        url="contoso.example.com",
        deployment="Versioned release",
        customer_users=True,
        engine="engine-contoso",
        live_feeds=["Orders", "Shipments"],
        reconciliation_feeds=["Shipments"],
        page="https://wiki.example.com/prod",
    )
    assert e.call(upsert_environment, environment=page).expect_ok() == "updated"
    assert e.call(upsert_environment, environment=page).expect_ok() == "unchanged"
    card = e.query(environment, name="contoso-prod")
    assert card.purpose == "Live" and card.slack_channel == "#contoso" and card.connections


def test_upsert_adds_a_new_active_environment() -> None:
    e = editor()
    new = EnvironmentDescription(
        name="fabrikam-prod", customer="Fabrikam", page="https://wiki.example.com/fabrikam"
    )
    assert e.call(upsert_environment, environment=new).expect_ok() == "added"
    assert e.query(environment, name="fabrikam-prod").lifecycle == "active"


def test_a_bad_name_is_an_invalid_argument() -> None:
    with pytest.raises(InvalidArguments):
        editor().call(set_lifecycle, environment="Contoso Prod", lifecycle="active")


def test_a_shared_wiki_page_is_refused() -> None:
    dup = EnvironmentDescription(name="fabrikam-prod", page="https://wiki.example.com/prod")
    editor().call(upsert_environment, environment=dup).expect_refused(by=unique_pages)


def test_a_secret_in_free_text_is_refused() -> None:
    leaky = EnvironmentDescription(
        name="fabrikam-prod", purpose="Live. Admin password: hunter2, ask Sam"
    )
    messages = editor().call(upsert_environment, environment=leaky).expect_refused(by=no_secrets)
    assert messages == [
        "fabrikam-prod: its purpose holds what looks like a secret. "
        "Name where the secret is kept instead, such as the vault's path."
    ]


def test_set_connections_replaces_them() -> None:
    e = editor()
    e.call(upsert_system, name="Contoso ERP E2E2", owner="Contoso", tier="test").expect_ok()
    e.call(
        set_connections,
        environment="contoso-acc2",
        connections=[ConnectionInput(system="Contoso ERP E2E2", access="read_write")],
        integration_notes="Message queue link in progress",
    ).expect_ok()
    card = e.query(environment, name="contoso-acc2")
    assert [c.system for c in card.connections] == ["Contoso ERP E2E2"]
    assert card.integration_notes == "Message queue link in progress"


def test_set_connections_after_a_read_is_refused_if_the_register_changed() -> None:
    w = world()
    e = editor(w)
    e.query(environment, name="contoso-acc2")
    read = e.data_version
    admin(w).call(
        set_slack_channel, environment="contoso-acc2", slack_channel="#contoso"
    ).expect_ok()
    keep = [ConnectionInput(system="Contoso ERP E2E1", access="read")]
    stale = e.if_unchanged_since(read)
    stale.call(set_connections, environment="contoso-acc2", connections=keep).expect_refused()
    fresh = e.if_unchanged_since(e.data_version)
    fresh.call(set_connections, environment="contoso-acc2", connections=keep).expect_ok()


def test_set_connections_refuses_unknown_systems() -> None:
    nope = [ConnectionInput(system="Nope", access="read")]
    editor().call(set_connections, environment="contoso-acc2", connections=nope).expect_refused(
        message="Unknown systems: Nope. Add them with `upsert_system` first."
    )


def test_lifecycle_with_an_unknown_successor_is_refused() -> None:
    editor().call(
        set_lifecycle, environment="contoso-acc1", lifecycle="deprecated", successor="contoso-acc9"
    ).expect_refused(
        message="There is no environment contoso-acc9 to succeed it. "
        "See `environments` for the names."
    )


def test_a_successor_cannot_be_retired() -> None:
    editor().call(set_lifecycle, environment="portal-prod", lifecycle="retired").expect_refused(
        by=valid_successors
    )


def test_a_customer_slack_channel_goes_to_all_its_environments() -> None:
    e = editor()
    assert e.call(set_customer_slack, customer="Contoso", slack_channel="#contoso").expect_ok() == 3
    assert e.query(environment, name="contoso-acc1").slack_channel == "#contoso"
    e.call(set_customer_slack, customer="Nobody", slack_channel="#x").expect_refused(
        message="No environment belongs to Nobody. Customers: Contoso."
    )


def test_admin_removes_a_system_only_once_unused() -> None:
    a = admin()
    a.call(remove_system, name="Contoso ERP prod").expect_refused(
        message="Contoso ERP prod is still connected to contoso-prod, contoso-staging. "
        "Change their connections first."
    )
    a.call(upsert_system, name="Old thing", owner="Contoso", tier="test").expect_ok()
    a.call(remove_system, name="Old thing").expect_ok()


def test_admin_removes_an_environment_with_its_connections() -> None:
    a = admin()
    a.call(remove_environment, name="contoso-staging").expect_ok()
    affected = a.query(impact, target="Contoso ERP prod").affected
    assert [x.environment for x in affected] == ["contoso-prod"]
    a.call(remove_environment, name="portal-prod").expect_refused(
        message="partner-prod names portal-prod as its successor. "
        "Change it with `set_lifecycle` first."
    )


def test_editors_cannot_remove_and_readers_cannot_write() -> None:
    with pytest.raises(Denied):
        editor().call(remove_environment, name="contoso-staging")
    with pytest.raises(Denied):
        editor().call(remove_system, name="Contoso ERP prod")
    with pytest.raises(Denied):
        reader().call(set_slack_channel, environment="contoso-acc1", slack_channel="#contoso")


# ---------------------------------------------------------------- invariants alone


def test_duplicate_environments_are_refused() -> None:
    row = "contoso-prod,Contoso,Copy,,,,,,,,active,,,\n"
    world().change("environments.csv", append=row).expect_refused(by=unique_environments)


def test_malformed_rows_are_refused() -> None:
    world().change(
        "environments.csv", replace=("Continuous integration,no,stub", "Nightly,maybe,stub")
    ).expect_refused(by=well_formed)


def test_bad_systems_are_refused() -> None:
    row = "Contoso ERP prod,Contoso,live,\n"
    world().change("systems.csv", append=row).expect_refused(by=valid_systems)


def test_bad_connections_are_refused() -> None:
    row = "contoso-prod,Contoso ERP prod,write,FTP,\n"
    world().change("connections.csv", append=row).expect_refused(by=valid_connections)


# ---------------------------------------------------------------- the screens


def test_the_catalog_grid() -> None:
    screen = str(reader().view(catalog))
    assert "contoso-acc1" in screen and "#contoso" in screen
    assert "7 running, for 1 customer" in screen and "customers" not in screen


def test_details_opens_the_chosen_environment() -> None:
    screen = reader().view(details, name="contoso-acc1")
    text = str(screen)
    assert screen["arguments"] == {"name": "contoso-acc1"}
    assert "Guardrails" in text and "Contoso ERP E2E1" in text and "contoso-acc2" in text
    assert "Not recorded yet: Slack channel" in text


def test_details_defaults_to_a_production_environment() -> None:
    assert reader().view(details)["arguments"] == {"name": "contoso-prod"}
    with pytest.raises(Refused, match="There is no environment nothing"):
        reader().view(details, name="nothing")


def test_details_shows_the_successor_and_the_extra_hosts() -> None:
    text = str(reader().view(details, name="partner-prod"))
    assert "portal-prod" in text and "(+ partner2.example.com)" in text


def test_details_links_an_address_given_with_its_scheme() -> None:
    w = world()
    wiki = EnvironmentDescription(
        name="contoso-acc1", customer="Contoso", url="https://contoso-acc1.example.com/"
    )
    editor(w).call(upsert_environment, environment=wiki).expect_ok()
    text = str(reader(w).view(details, name="contoso-acc1"))
    assert "'href': 'https://contoso-acc1.example.com'" in text


def test_upsert_system_matches_a_name_without_its_spaces() -> None:
    w = world()
    e = editor(w)
    e.call(upsert_system, name=" Contoso ERP prod ", owner="Contoso", tier="production").expect_ok()
    assert [s.name for s in reader(w).query(systems, owner="Contoso")].count(
        "Contoso ERP prod"
    ) == 1


def test_the_screens_with_no_data() -> None:
    empty = given(register, {}, now=NOW).connect("reader")
    assert empty.query(environments) == []
    assert "No environment is recorded yet" in str(empty.view(catalog))
    assert "No environment is recorded yet" in str(empty.view(details))

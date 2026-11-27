import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[3]
DOCKERFILE = ROOT / "docker" / "server.Dockerfile"


def dockerfile_instructions() -> list[str]:
    """The Dockerfile's instructions, a continued line joined and comments dropped."""
    text = DOCKERFILE.read_text().replace("\\\n", " ")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def dockerfile_user(steps: list[str]) -> str:
    """The user the image's last USER names."""
    users = [step.split(maxsplit=1)[1] for step in steps if step.startswith("USER ")]
    assert users, "the image sets no USER, so the server runs as root"
    return users[-1].split(":")[0]


def test_the_image_runs_as_a_user_of_its_own() -> None:
    """Root ignores the file permissions that keep a query or a view from writing."""
    steps = dockerfile_instructions()
    user = dockerfile_user(steps)
    assert user not in {"root", "0"}
    made = next((step for step in steps if "useradd" in step), "")
    assert user in made.split(), f"the image runs as {user}, but makes no such user"
    assert "--uid 1000" in made, "the user's id isn't fixed, so a volume's owner could change"
    assert "--create-home" in made and f"--home-dir /home/{user}" in made, (
        f"{user} has no home of its own, where uv keeps its cache"
    )
    used = max(index for index, step in enumerate(steps) if step.startswith("USER "))
    assert steps.index(made) < used, "USER comes before the user it names is made"
    assert any(
        step.startswith("ENV ") and f"HOME=/home/{user}" in step.replace('"', "") for step in steps
    ), f"{user} has no HOME: uv keeps its cache in it"
    assert any(f"chown {user}:{user} /data" in step for step in steps), (
        "/data isn't the server user's"
    )


def test_the_self_hosting_guide_runs_the_server_as_the_image_user() -> None:
    """`fly ssh console` connects as root, so a command there must name the image's user."""
    pages = sorted((ROOT / "website/docs/guides/self-hosting").glob("*.md"))
    guide = "\n".join(page.read_text() for page in pages)
    user = dockerfile_user(dockerfile_instructions())
    commands = [
        line
        for line in guide.splitlines()
        if line.strip().startswith("fly ssh console") and "cliquet-server" in line
    ]
    assert commands
    assert all(f"-u {user}" in line or f"--user {user}" in line for line in commands)


def test_the_fly_guide_s_configuration_keeps_the_machine_and_its_snapshots() -> None:
    """A stop loses previews and MCP sessions, the privacy notice promises 5 days, and it builds."""
    guide = (ROOT / "website/docs/guides/self-hosting/fly.md").read_text()
    block = guide.split("```toml\n", 1)[1].split("```", 1)[0]
    config = tomllib.loads("\n".join(line.removeprefix("    ") for line in block.splitlines()))
    assert config["http_service"]["auto_stop_machines"] == "off"
    assert config["http_service"]["min_machines_running"] == 1
    assert config["mounts"]["snapshot_retention"] == 5
    assert config["http_service"]["internal_port"] == 8080
    assert (ROOT / config["build"]["dockerfile"]) == DOCKERFILE

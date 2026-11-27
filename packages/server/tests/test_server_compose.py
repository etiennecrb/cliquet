"""The Docker Compose stack in `docker/`: it names only settings the server reads."""

import re
from pathlib import Path

from cliquet_server.settings import Settings

DOCKER = Path(__file__).parents[3] / "docker"
FILES = ("compose.yaml", ".env.example", "Caddyfile")


def test_the_stack_names_only_settings_the_server_reads() -> None:
    known = {f"CLIQUET_{name.upper()}" for name in Settings.model_fields}
    for name in FILES:
        named = set(re.findall(r"\bCLIQUET_[A-Z0-9_]*[A-Z0-9]", (DOCKER / name).read_text()))
        assert named <= known, f"{name} names {sorted(named - known)}, which the server ignores"


def test_the_server_mounts_the_sockets_volume_its_settings_name() -> None:
    compose = (DOCKER / "compose.yaml").read_text()
    folder = re.search(r"CLIQUET_SANDBOX_SOCKETS: (\S+)", compose)
    volume = re.search(r"CLIQUET_SANDBOX_SOCKETS_VOLUME: (\S+)", compose)
    assert folder and volume
    assert f"- sockets:{folder[1]}\n" in compose
    assert f"  sockets:\n    name: {volume[1]}\n" in compose

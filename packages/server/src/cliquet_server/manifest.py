"""The parts of an app's manifest that the web pages and the builder share."""

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from cliquet.manifest import title_problem
from cliquet.vocabulary import LANGUAGES
from cliquet_server import icons

# An app's accent, as the SDK checks it, in lower case. The manifest comes from the sandbox,
# and the accent is written into a page's style, so the server checks it again.
ACCENT = re.compile(r"#[0-9a-f]{6}")


def accent(manifest: Mapping[str, Any]) -> str | None:
    """The app's accent from its manifest, as `#rrggbb`, or None when it has none."""
    value = manifest.get("accent")
    if not isinstance(value, str) or not ACCENT.fullmatch(value.lower()):
        return None
    return value.lower()


def language(manifest: Mapping[str, Any]) -> str | None:
    """The app's language from its manifest, or None when it isn't one Cliquet knows."""
    value = manifest.get("language")
    return value if isinstance(value, str) and value in LANGUAGES else None


def icon(manifest: Mapping[str, Any]) -> str | None:
    """The name of the app's icon from its manifest, or None when it has none the server
    can draw."""
    value = manifest.get("icon")
    return value if isinstance(value, str) and icons.known(value) else None


def checked_title(value: object) -> str | None:
    """A title from a manifest, the app's or a view's, as the SDK keeps it, or None when it
    isn't one the SDK accepts. The manifest comes from the sandbox, so the server checks the
    title again, and keeps the form it checked."""
    if not isinstance(value, str) or title_problem(value) is not None:
        return None
    return unicodedata.normalize("NFC", value).strip()


def label(name: str) -> str:
    """A tool's or an app's name, as a page shows it."""
    return name.replace("_", " ").replace("-", " ").capitalize()


def title(manifest: Mapping[str, Any]) -> str | None:
    """The app's name as people read it, from its manifest, or None when it gave none."""
    return checked_title(manifest.get("title"))


def tool_rows(manifest: dict[str, Any], role: str | None = None) -> list[dict[str, Any]]:
    """An app's tools, in the order the manifest lists them, for one role or for all."""
    rows = []
    for kind, key in (("action", "actions"), ("query", "queries"), ("view", "views")):
        for tool in manifest[key]:
            roles = tool["roles"]
            if role is not None and roles is not None and role not in roles:
                continue
            rows.append(
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "kind": kind,
                    "roles": roles,
                }
            )
    return rows


def rule_texts(manifest: dict[str, Any]) -> list[str]:
    """The descriptions of an app's invariants."""
    return [rule["description"] for rule in manifest["invariants"]]

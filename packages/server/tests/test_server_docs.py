"""The docs at cliquet.dev/docs: they build, link only to pages that exist, and list them all."""

import re
import shutil
import subprocess
import sys
import tomllib
import typing
from collections.abc import Iterator, Mapping
from pathlib import Path

from pydantic import SecretStr

from cliquet_server.settings import Settings

WEBSITE = Path(__file__).parents[3] / "website"


def _nav_pages(nav: list) -> set[str]:
    """Every Markdown page the docs' navigation names, in sections too."""
    pages: set[str] = set()
    for entry in nav:
        for target in entry.values() if isinstance(entry, dict) else [entry]:
            if isinstance(target, list):
                pages |= _nav_pages(target)
            elif target.endswith(".md"):
                pages.add(target)
    return pages


def test_the_docs_build_with_no_broken_link(tmp_path: Path) -> None:
    # A copy, so the build writes into the temporary folder, not into website/public.
    shutil.copy(WEBSITE / "zensical.toml", tmp_path)
    shutil.copytree(WEBSITE / "docs", tmp_path / "docs")
    shutil.copytree(WEBSITE / "docs-theme", tmp_path / "docs-theme")
    built = subprocess.run(
        [sys.executable, "-m", "zensical", "build", "--strict"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr

    site = tmp_path / "public/docs"
    pages = {
        page.relative_to(tmp_path / "docs").as_posix() for page in (tmp_path / "docs").rglob("*.md")
    }
    for page in pages:
        path = Path(page)
        folder = path.parent if path.stem == "index" else path.with_suffix("")
        assert (site / folder / "index.html").exists(), page
    assert (site / "search.json").exists()

    # The diagrams' pictures name their files from the site's root, so the build can't check them.
    for picture in re.findall(
        r'(?:src|srcset)="/docs/([^"]+)"',
        "".join(page.read_text() for page in (tmp_path / "docs").rglob("*.md")),
    ):
        assert (site / picture).exists(), picture

    config = tomllib.loads((WEBSITE / "zensical.toml").read_text())
    assert _nav_pages(config["project"]["nav"]) == pages


def test_the_site_s_sitemaps_list_its_pages_to_find() -> None:
    public = WEBSITE / "public"
    robots = (public / "robots.txt").read_text()
    assert "Disallow" not in robots
    assert "Sitemap: https://cliquet.dev/sitemap.xml" in robots
    # The docs' own, which the build writes, as `site_url` says.
    assert "Sitemap: https://cliquet.dev/docs/sitemap.xml" in robots
    config = tomllib.loads((WEBSITE / "zensical.toml").read_text())
    assert config["project"]["site_url"] == "https://cliquet.dev/docs/"
    # Each page the site's sitemap lists is one of its pages, and doesn't ask not to be listed.
    listed = re.findall(
        r"<loc>https://cliquet\.dev/([^<]*)</loc>", (public / "sitemap.xml").read_text()
    )
    assert listed
    for path in listed:
        page = public / (f"{path.removesuffix('/')}.html" if path else "index.html")
        assert page.exists() and 'name="robots" content="noindex"' not in page.read_text(), path


def test_every_page_of_the_site_links_to_the_docs() -> None:
    for page in (WEBSITE / "public").glob("*.html"):
        assert '<a href="/docs/">Docs</a>' in page.read_text(), page.name


class _Recording(Mapping[str, str]):
    """An empty environment that remembers every variable read from it."""

    def __init__(self) -> None:
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> str:
        self.read.add(key)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0


def test_the_settings_page_lists_every_setting_the_server_reads() -> None:
    fields = Settings.model_fields
    names = {f"CLIQUET_{name.upper()}" for name in fields}
    environ = _Recording()
    Settings.from_env(environ)
    assert {key for key in environ.read if key.startswith("CLIQUET_")} == names

    # Only a table row's first cell names a setting: prose may mention any of them.
    page = (WEBSITE / "docs/guides/self-hosting/settings.md").read_text()
    rows = re.findall(r"^\|\s*`(CLIQUET_[A-Z0-9_]+)`\s*\|(.*)$", page, re.MULTILINE)
    listed = [name for name, _ in rows]
    assert sorted(listed) == sorted(names)

    secrets = {
        f"CLIQUET_{name.upper()}"
        for name, field in fields.items()
        if SecretStr in (field.annotation, *typing.get_args(field.annotation))
    }
    # The cells after the name: its default, then whether it is a secret.
    marked = {name for name, rest in rows if rest.split("|")[1].strip() == "Yes"}
    assert marked == secrets

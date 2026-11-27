from importlib.metadata import version

import cliquet


def test_version_matches_package_metadata() -> None:
    assert cliquet.__version__ == version("cliquet-sdk")

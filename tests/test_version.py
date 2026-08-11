from importlib.metadata import version

import kpo


def test_package_version_matches_distribution_metadata() -> None:
    assert kpo.__version__ == version("kpo")

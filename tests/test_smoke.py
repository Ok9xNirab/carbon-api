import importlib

import carbon

SUBPACKAGES = ["api", "worker", "core", "ingest", "discovery", "layout", "infra"]


def test_package_imports():
    assert carbon.__version__


def test_subpackages_import():
    for name in SUBPACKAGES:
        importlib.import_module(f"carbon.{name}")

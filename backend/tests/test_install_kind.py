"""``install_kind`` tells the installer build, a wheel and a checkout apart.

``doctor``'s QEMU advice depends on it: only the installer bundles QEMU, and a
pip or pipx install on Windows — which PyPI will serve whether or not anyone
intends it — needs to be told so rather than told to reinstall.
"""

from __future__ import annotations

import importlib.metadata
import sys

import pytest

from kurukuru.config import install_kind


class _Dist:
    def __init__(self, direct_url: str | None):
        self._direct_url = direct_url

    def read_text(self, name: str) -> str | None:
        return self._direct_url if name == "direct_url.json" else None


@pytest.fixture
def dist(monkeypatch):
    def use(direct_url: str | None = None, *, missing: bool = False):
        def fake(name: str):
            assert name == "kurukuru"
            if missing:
                raise importlib.metadata.PackageNotFoundError(name)
            return _Dist(direct_url)

        monkeypatch.setattr(importlib.metadata, "distribution", fake)

    return use


def test_the_frozen_build_is_the_installer(monkeypatch, dist):
    dist('{"url": "file:///x.whl", "archive_info": {}}')
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert install_kind() == "installer"


def test_a_wheel_from_pypi_is_a_package(dist):
    dist(None)  # an index install writes no direct_url.json
    assert install_kind() == "package"


def test_a_wheel_installed_by_path_is_a_package(dist):
    # What pipx writes for `pipx install dist/kurukuru-*.whl`.
    dist('{"url": "file:///dist/kurukuru-0.1.5-py3-none-any.whl", "archive_info": {}}')
    assert install_kind() == "package"


def test_an_editable_install_is_source(dist):
    dist('{"url": "file:///src/backend", "dir_info": {"editable": true}}')
    assert install_kind() == "source"


def test_no_metadata_at_all_is_source(dist):
    dist(missing=True)
    assert install_kind() == "source"


def test_unreadable_direct_url_is_still_a_package(dist):
    dist("{not json")
    assert install_kind() == "package"

"""
Shared pytest fixtures.

pytest runs this file automatically before the tests. "session" fixtures are
computed once and reused by every test, so the slow prediction for each ROI
case runs only once per test session.
"""

import pandas as pd
import pytest
import requests

import _baseline as b


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any code path tries to reach the internet during tests."""
    def _blocked(*args, **kwargs):
        raise RuntimeError("Network access attempted during tests")
    monkeypatch.setattr(requests, "get", _blocked)
    monkeypatch.setattr(requests.Session, "request", _blocked)


@pytest.fixture(scope="session")
def tles(tmp_path_factory):
    return b.load_fixture_tles(tmp_path_factory.mktemp("tle_cache"))


@pytest.fixture(scope="session")
def plans():
    return b.load_fixture_plans()


@pytest.fixture(scope="session")
def results(tles, plans):
    """Lazily computed {case: (roi, passes)} so each case is predicted once."""
    cache = {}

    def get(case):
        if case not in cache:
            cache[case] = b.run_case(case, tles, plans)
        return cache[case]
    return get


def read_csv_as_text(path):
    """Read a CSV keeping every value as text, so 'n/a' and blanks are not turned into NaN."""
    return pd.read_csv(path, dtype=str, keep_default_na=False)

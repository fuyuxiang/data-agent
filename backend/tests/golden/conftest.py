"""Golden Set fixtures and summary hook registration."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest

from app.core import clock
from tests.golden import _summary_hook
from tests.security.factories import build_principals
from tests.semantic.factories import build_orders_dataset

FROZEN_TODAY = date(2026, 8, 12)


@pytest.fixture(autouse=True)
def frozen_clock() -> Iterator[None]:
    clock.freeze(FROZEN_TODAY)
    try:
        yield
    finally:
        clock.unfreeze()


@pytest.fixture
def golden_env(meta_session: Any) -> Any:
    build_orders_dataset(meta_session)
    build_principals(meta_session)
    return meta_session


@pytest.fixture
def ephemeral_policy() -> Any:
    def install(_user_name: str, _policies: tuple) -> None:
        # Test factories already install the policy matrix used by Golden Set.
        return None

    return install


def pytest_configure(config: Any) -> None:
    if not hasattr(config, "_golden_summary_registered"):
        config._golden_summary_registered = True
        config.pluginmanager.register(_summary_hook, name="golden_summary_hook")

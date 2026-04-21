"""Root exception hierarchy guards.

Locks in the invariant that every CodeRouter-raised exception subclasses
:class:`CodeRouterError`, so downstream integrations can write a single
``except CodeRouterError`` clause. If a future leaf exception is added
without inheriting from the root, this test fails loudly.
"""

from __future__ import annotations

from coderouter import CodeRouterError
from coderouter.adapters.base import AdapterError
from coderouter.routing.fallback import MidStreamError, NoProvidersAvailableError


def test_adapter_error_inherits_root() -> None:
    assert issubclass(AdapterError, CodeRouterError)


def test_no_providers_available_inherits_root() -> None:
    assert issubclass(NoProvidersAvailableError, CodeRouterError)


def test_mid_stream_error_inherits_root() -> None:
    assert issubclass(MidStreamError, CodeRouterError)


def test_adapter_error_instance_is_caught_as_root() -> None:
    # Constructor-level sanity: a raised AdapterError is catchable as
    # CodeRouterError. Guards against someone breaking inheritance by
    # "fixing" the base class mid-refactor.
    try:
        raise AdapterError("boom", provider="p", status_code=500, retryable=False)
    except CodeRouterError as exc:
        assert str(exc) == "[p status=500] boom"
    else:  # pragma: no cover — only reached if inheritance is broken
        raise AssertionError("AdapterError did not bubble up as CodeRouterError")

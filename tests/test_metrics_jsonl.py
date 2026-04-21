"""JSONL persistence tests for ``$CODEROUTER_EVENTS_PATH`` (v1.5-B).

When the env var is set at ``install_collector()`` time, a file handler
using :class:`coderouter.logging.JsonLineFormatter` is attached
alongside the aggregate collector. These tests exercise the
installation path, path-expansion, and the byte-for-byte shape of the
mirrored records.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from coderouter.logging import configure_logging, get_logger
from coderouter.metrics import install_collector, uninstall_collector


@pytest.fixture
def fresh_handlers() -> Iterator[None]:
    """Ensure each test starts and ends without stray metrics handlers."""
    uninstall_collector()
    configure_logging()
    yield
    uninstall_collector()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """Parse a JSONL file into a list of dicts — one per line."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_no_env_no_file_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_handlers: None,
) -> None:
    """Unset env → no mirror file, no file handle leak."""
    monkeypatch.delenv("CODEROUTER_EVENTS_PATH", raising=False)
    install_collector()
    get_logger("test.metrics.jsonl").info("try-provider", extra={"provider": "p"})

    # nothing exists under tmp_path because the env wasn't set
    assert list(tmp_path.iterdir()) == []


def test_env_set_mirrors_every_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_handlers: None,
) -> None:
    """Each log record becomes one JSONL line with the JsonLineFormatter shape."""
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("CODEROUTER_EVENTS_PATH", str(path))

    install_collector()
    logger = get_logger("test.metrics.jsonl")
    logger.info("try-provider", extra={"provider": "local", "stream": False})
    logger.info("provider-ok", extra={"provider": "local", "stream": False})

    # flush & detach so the file is readable
    uninstall_collector()

    records = _read_jsonl(path)
    events = [r["msg"] for r in records]
    assert "try-provider" in events
    assert "provider-ok" in events
    # Extras flow through — same contract as the stderr stream
    provider_ok = next(r for r in records if r["msg"] == "provider-ok")
    assert provider_ok["provider"] == "local"
    assert provider_ok["stream"] is False


def test_tilde_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_handlers: None,
) -> None:
    """``~/...`` in the env var expands via ``os.path.expanduser``.

    Uses monkeypatched ``HOME`` so the test doesn't pollute the real
    user's home directory.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEROUTER_EVENTS_PATH", "~/nested/dir/events.jsonl")

    install_collector()
    get_logger("test.metrics.jsonl").info("try-provider", extra={"provider": "p"})
    uninstall_collector()

    expanded = tmp_path / "nested" / "dir" / "events.jsonl"
    assert expanded.exists(), "expected expanded path to be created"
    records = _read_jsonl(expanded)
    assert any(r["msg"] == "try-provider" for r in records)


def test_parent_dirs_are_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_handlers: None,
) -> None:
    """Missing parent directories are created (``parents=True``).

    Operators who point the env at a path under a home they haven't
    manually mkdir'd shouldn't hit a ``FileNotFoundError`` at startup.
    """
    path = tmp_path / "a" / "b" / "c" / "events.jsonl"
    monkeypatch.setenv("CODEROUTER_EVENTS_PATH", str(path))
    install_collector()
    get_logger("test.metrics.jsonl").info("try-provider", extra={"provider": "p"})
    uninstall_collector()
    assert path.exists()


def test_uninstall_closes_file_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_handlers: None,
) -> None:
    """``uninstall_collector`` releases the file handle — no stale writes.

    After uninstall, subsequent log lines must NOT land in the previous
    mirror file (otherwise tests bleed into each other on Windows /
    case-insensitive FS).
    """
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("CODEROUTER_EVENTS_PATH", str(path))
    install_collector()
    get_logger("test.metrics.jsonl").info("try-provider", extra={"provider": "p"})
    uninstall_collector()
    before = path.read_text(encoding="utf-8")

    # With no collector installed, new events should not land in the file.
    # Re-install pointed at a fresh path to exercise the detach.
    monkeypatch.delenv("CODEROUTER_EVENTS_PATH", raising=False)
    install_collector()
    get_logger("test.metrics.jsonl").info("try-provider", extra={"provider": "p2"})
    uninstall_collector()

    after = path.read_text(encoding="utf-8")
    assert before == after, "second install should not write to first mirror"

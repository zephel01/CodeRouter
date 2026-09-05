"""Secret registry + logging redaction (v2.14.0).

Before this module there was no notion of "this string is a secret"
anywhere in the codebase. API keys were read from the environment by
:func:`coderouter.config.loader.resolve_api_key`, dropped straight into
``headers["x-api-key"]`` / ``headers["Authorization"]``, and that was the
end of it. Nothing masked them, because nothing knew which strings needed
masking.

That is not the same as "keys leak today". We found no call site that
logs a header dict. The problem is the shape of the risk: the safety of
every future log line depended on whoever wrote it remembering that the
value in their hand was a credential. One ``logger.debug("upstream
rejected: %s", exc)`` where the provider echoed the key back in its error
body, and it lands in ``requests.jsonl`` forever.

So the fix is not "audit the current call sites" — it is to make the
process know its own secrets and scrub them on the way out, once, for
every logger.

Design
------
**Registry over regex.** The only strings we can redact with zero false
positives are the ones we know are secrets, so the primary mechanism is
an exact-match registry: :func:`register_secret` is called the moment a
credential enters the process (see ``resolve_api_key``), and
:func:`redact` replaces every occurrence of a registered value with
``[redacted:<LABEL>]``. Pattern matching is only a backstop for
credentials that never passed through our resolver (a key pasted into a
``base_url``, an upstream error body quoting a token) — the pattern set
is deliberately small and anchored so it cannot eat ordinary prose.

**Filter, not formatter.** Redaction is a :class:`logging.Filter` so it
applies to every handler we install — the stderr JSON-line handler, the
audit JSONL handler, the request-log JSONL handler — rather than only to
the one formatter. The filter mutates the record in place, so a handler
that runs later in the chain cannot re-expose what an earlier one
scrubbed.

**Length floor.** Values shorter than :data:`_MIN_SECRET_LEN` are refused
by the registry. A 3-character "key" (a placeholder, an empty-ish env
var, a literal ``"x"`` in a test fixture) would otherwise match inside
unrelated words and turn every log line into confetti. Refusing to
register is safer than redacting everything.

**Process-global.** The registry is module-level state, like the metrics
collector. Secrets belong to the process, not to a config object, and the
filter has to reach them from inside ``logging`` where no config is in
scope.

Non-goals
---------
This does not encrypt anything at rest, does not touch already-written
log files (see :func:`scan_logs_for_secrets` for detection, not repair),
and is not a defence against someone who can read the process
environment. It closes exactly one hole: credentials reaching log sinks.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "SecretCheck",
    "SecretRedactingFilter",
    "SecretReport",
    "check_secret_hygiene",
    "clear_secrets",
    "exit_code_for_secret_report",
    "format_secret_report",
    "install_secret_filter",
    "redact",
    "register_config_secrets",
    "register_secret",
    "registered_labels",
    "scan_logs_for_secrets",
    "self_test",
]


# A credential shorter than this is not registerable. Real API keys are far
# longer (the shortest common shape, a bare 32-hex token, is 32); anything
# under 8 characters is a placeholder, an accident, or a test stub, and
# redacting it would corrupt unrelated log text.
_MIN_SECRET_LEN = 8

# Registered secrets: exact value -> label used in the replacement text.
# The label is the env var name (``OPENAI_API_KEY``) so an operator reading
# a scrubbed log can still tell *which* credential was involved.
_secrets: dict[str, str] = {}

# Backstop patterns for credentials that never went through our resolver.
# Each entry is (compiled pattern, replacement). Every pattern is anchored
# on a distinctive prefix or on an explicit key=value / header syntax — we
# do not attempt to detect "high entropy strings", which is the classic way
# a scrubber starts eating commit hashes and base64 payloads.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # OpenAI / Anthropic style: sk-…, sk-ant-…, sk-proj-…
    (re.compile(r"\bsk-(?:[A-Za-z]+-)?[A-Za-z0-9_\-]{16,}"), "[redacted:sk-key]"),
    # GitHub personal access tokens and friends.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "[redacted:gh-token]"),
    # Google API keys.
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"), "[redacted:google-key]"),
    # A bearer token in a header dump or an error string.
    (
        re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{16,}"),
        r"\1 [redacted:bearer]",
    ),
    # ``?api_key=…`` / ``&access_token=…`` embedded in a URL.
    (
        re.compile(
            r"(?i)([?&](?:api[-_]?key|access[-_]?token|auth[-_]?token|key)=)[^&\s\"'<>]+"
        ),
        r"\1[redacted:url-param]",
    ),
    # ``https://user:password@host`` userinfo.
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+):[^/\s@]+@"),
        r"\1:[redacted:userinfo]@",
    ),
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def register_secret(value: str | None, label: str = "secret") -> bool:
    """Register ``value`` as a secret to scrub from all log output.

    Returns ``True`` when the value was accepted. Values that are empty,
    non-string, or shorter than :data:`_MIN_SECRET_LEN` are rejected and
    return ``False`` — see the module docstring for why a length floor
    matters more than completeness here.

    Idempotent: re-registering the same value keeps the first label, so a
    key resolved once as ``OPENAI_API_KEY`` does not get relabelled to a
    generic ``secret`` by a later anonymous call.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if len(stripped) < _MIN_SECRET_LEN:
        return False
    _secrets.setdefault(stripped, label)
    return True


def register_config_secrets(config: Any) -> list[str]:
    """Register every credential reachable from a loaded config object.

    Walks ``config.providers`` and resolves each ``api_key_env`` from the
    environment. Called from the loader so redaction is armed before the
    first request — waiting for the first :func:`resolve_api_key` call
    would leave startup log lines unprotected.

    Returns the list of labels registered, for the caller to log as a
    count. **The values themselves are never returned or logged.**
    """
    labels: list[str] = []
    providers = getattr(config, "providers", None) or ()
    for provider in providers:
        env_name = getattr(provider, "api_key_env", None)
        if not env_name:
            continue
        value = os.environ.get(env_name, "").strip()
        if register_secret(value, env_name):
            labels.append(env_name)
    return labels


def registered_labels() -> tuple[str, ...]:
    """Labels of every registered secret, in registration order.

    Safe to log — labels are env var names, not values.
    """
    return tuple(_secrets.values())


def clear_secrets() -> None:
    """Drop every registered secret. Exists for test isolation."""
    _secrets.clear()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact(text: str) -> str:
    """Return ``text`` with every known or pattern-matched secret masked.

    Registered values are replaced first (exact match, no regex cost for
    the common case), then the backstop patterns run over the result. A
    string containing no secrets is returned unchanged — and, because the
    registry is usually tiny, the fast path is a handful of ``in`` checks.
    """
    if not text:
        return text
    for value, label in _secrets.items():
        if value in text:
            text = text.replace(value, f"[redacted:{label}]")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_value(value: Any) -> Any:
    """Redact strings inside a log payload, recursing through containers.

    ``extra={...}`` payloads in this codebase are mostly flat dicts of
    scalars, but a few carry a list of provider names or a nested detail
    dict, so the walk handles both. Non-string leaves pass through
    untouched — we never stringify a value just to scan it, because that
    would change what the formatter emits.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        rebuilt = [_redact_value(v) for v in value]
        if isinstance(value, tuple):
            return tuple(rebuilt)
        if isinstance(value, set):
            return set(rebuilt)
        return rebuilt
    return value


# Standard LogRecord attributes the filter must not touch. Rewriting
# ``pathname`` or ``module`` would be pointless work and could corrupt the
# formatter's whitelist logic in coderouter.logging.JsonLineFormatter.
_RECORD_SKIP = frozenset(
    {
        "args",
        "created",
        "exc_info",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class SecretRedactingFilter(logging.Filter):
    """Scrub registered secrets from a record before any handler sees it.

    Mutates the record in place and always returns ``True`` — this is a
    filter in the plumbing sense, not a gate: no record is ever dropped.
    Mutation is deliberate. Attaching this to the first handler would be
    enough to protect the rest only by accident of ordering, so we attach
    it to every handler we install and let the in-place edit make the
    duplicate work a no-op.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact ``msg``, ``args``, ``exc_text`` and every ``extra`` field."""
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = _redact_value(record.args)
        exc_text = getattr(record, "exc_text", None)
        if isinstance(exc_text, str):
            record.exc_text = redact(exc_text)
        for key, value in list(record.__dict__.items()):
            if key in _RECORD_SKIP or key.startswith("_"):
                continue
            if key in {"msg", "exc_text"}:
                continue
            redacted = _redact_value(value)
            if redacted is not value:
                record.__dict__[key] = redacted
        return True


_FILTER_MARKER = "_coderouter_secret_filter"


def install_secret_filter(handler: logging.Handler) -> None:
    """Attach a :class:`SecretRedactingFilter` to ``handler`` once.

    Idempotent — a marker attribute keeps a re-configured handler from
    stacking filters, which would double the scan cost per record for no
    benefit (redaction is already fixed-point).
    """
    if getattr(handler, _FILTER_MARKER, False):
        return
    handler.addFilter(SecretRedactingFilter())
    setattr(handler, _FILTER_MARKER, True)


# ---------------------------------------------------------------------------
# Diagnostics — surfaced by ``coderouter doctor --check-secrets``
# ---------------------------------------------------------------------------


def self_test() -> bool:
    """Prove the filter actually scrubs, by running a record through it.

    Registers a throwaway value, pushes it through a real
    :class:`SecretRedactingFilter`, and checks it does not survive. This
    is what makes the doctor check evidence rather than an assertion that
    the code exists — a filter that was silently detached by a third-party
    logging config fails here.
    """
    canary = "coderouter-selftest-canary-0123456789"
    had = canary in _secrets
    register_secret(canary, "SELFTEST")
    try:
        record = logging.LogRecord(
            name="coderouter.selftest",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg=f"probe {canary}",
            args=(),
            exc_info=None,
        )
        # ``extra={...}`` lands as plain attributes on the record, so the
        # canary has to be planted the same way to prove nested payloads
        # are covered and not just the message.
        record.__dict__["detail"] = {"nested": canary}
        SecretRedactingFilter().filter(record)
        clean_msg = canary not in str(record.msg)
        clean_extra = canary not in str(record.__dict__["detail"])
        return clean_msg and clean_extra
    finally:
        if not had:
            _secrets.pop(canary, None)


def _iter_log_files(state_dir: str | os.PathLike[str] | None) -> Iterator[Path]:
    """Yield the JSONL sinks CodeRouter writes under ``state_dir``.

    Rotation keeps a single ``.1`` backup next to each live file (see
    :mod:`coderouter.state.audit_log`), so both are scanned — a secret
    that was written before the fix is just as exposed in the backup.
    """
    if not state_dir:
        return
    base = Path(state_dir).expanduser()
    for name in ("requests.jsonl", "audit.jsonl"):
        for candidate in (base / name, base / f"{name}.1"):
            if candidate.is_file():
                yield candidate


def scan_logs_for_secrets(
    state_dir: str | os.PathLike[str] | None,
    *,
    extra_paths: Iterable[str | os.PathLike[str]] = (),
    max_bytes: int = 64 * 1024 * 1024,
) -> list[tuple[Path, str, int]]:
    """Search already-written log files for registered secret values.

    Returns ``(path, label, line_number)`` for each hit. This is the check
    that turns "we added masking" into "and here is proof the old logs are
    clean" — or, if they are not, into a rotation list.

    Reads at most ``max_bytes`` per file and never rewrites anything. Files
    are read with ``errors="replace"`` because a truncated rotation can
    leave a partial UTF-8 sequence, and a decode error must not abort the
    scan of the remaining files.
    """
    if not _secrets:
        return []
    hits: list[tuple[Path, str, int]] = []
    paths = list(_iter_log_files(state_dir))
    paths.extend(Path(p).expanduser() for p in extra_paths)
    for path in paths:
        try:
            if path.stat().st_size > max_bytes:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, start=1):
                    for value, label in _secrets.items():
                        if value in line:
                            hits.append((path, label, lineno))
        except OSError:
            # An unreadable log is not a leak finding — the caller reports
            # what it could scan, and a permissions problem surfaces via
            # the separate .env / state-dir checks.
            continue
    return hits


# ---------------------------------------------------------------------------
# ``coderouter doctor --check-secrets`` — the report
# ---------------------------------------------------------------------------
#
# Deliberately mirrors the ``EnvSecurityCheck`` / ``EnvSecurityReport`` shape
# from :mod:`coderouter.env_security` (same verdict names, same exit-code
# contract) so ``_run_doctor`` can union the two reports without
# special-casing either. We do not import those types: env_security is a
# filesystem module and this is a logging module, and coupling them would
# make one un-importable without the other for no gain.


@dataclass(frozen=True)
class SecretCheck:
    """Outcome of one check in the secret-hygiene suite.

    ``fix`` is a one-line remediation the operator can act on, or ``None``
    when the verdict needs no action.
    """

    name: str
    verdict: str  # "ok" | "skip" | "warn" | "error"
    detail: str
    fix: str | None = None


@dataclass
class SecretReport:
    """Aggregate report for a single ``--check-secrets`` invocation."""

    checks: list[SecretCheck] = field(default_factory=list)


def exit_code_for_secret_report(report: SecretReport) -> int:
    """0 clean / 2 needs attention / 1 blocker — matches ``--check-env``."""
    verdicts = {c.verdict for c in report.checks}
    if "error" in verdicts:
        return 1
    if "warn" in verdicts:
        return 2
    return 0


def _check_embedded_credentials(config: Any) -> SecretCheck:
    """Look for credentials pasted into ``base_url`` instead of an env var.

    A key in the URL defeats the whole registry: it is never resolved by
    ``resolve_api_key``, so it is never registered, so the scrubber cannot
    know about it — and the URL is exactly the field most likely to end up
    in an error message. The backstop patterns in :func:`redact` catch the
    common shapes at log time, but the real fix is to move the credential
    into ``api_key_env``, which is what this check tells the operator.
    """
    offenders: list[str] = []
    for provider in getattr(config, "providers", None) or ():
        url = str(getattr(provider, "base_url", "") or "")
        if url != redact(url):
            offenders.append(str(getattr(provider, "name", "?")))
    if offenders:
        return SecretCheck(
            name="config-embedded-credentials",
            verdict="error",
            detail=(
                "base_url appears to carry a credential for: "
                + ", ".join(sorted(offenders))
            ),
            fix="Move the credential into an env var and set api_key_env on the provider.",
        )
    return SecretCheck(
        name="config-embedded-credentials",
        verdict="ok",
        detail="No credential-looking material found in any provider base_url.",
    )


def _check_registry(config: Any) -> SecretCheck:
    """Report how many declared credentials are actually armed.

    A provider that declares ``api_key_env`` whose variable is unset is not
    a leak, but it is worth surfacing: it usually means the operator
    expected a key to be in play, and it also means the scrubber has
    nothing to match if that provider starts echoing a key later.
    """
    declared: list[str] = []
    unset: list[str] = []
    for provider in getattr(config, "providers", None) or ():
        env_name = getattr(provider, "api_key_env", None)
        if not env_name:
            continue
        declared.append(env_name)
        if not os.environ.get(env_name, "").strip():
            unset.append(env_name)
    if not declared:
        return SecretCheck(
            name="registered-secrets",
            verdict="skip",
            detail="No provider declares api_key_env — nothing to register.",
        )
    armed = len(set(declared)) - len(set(unset))
    if unset:
        return SecretCheck(
            name="registered-secrets",
            verdict="warn",
            detail=(
                f"{armed}/{len(set(declared))} declared credentials are set; "
                f"unset: {', '.join(sorted(set(unset)))}"
            ),
            fix="Export the missing variables, or drop api_key_env for keyless providers.",
        )
    return SecretCheck(
        name="registered-secrets",
        verdict="ok",
        detail=f"{armed} credential(s) registered for redaction: "
        + ", ".join(sorted(set(declared))),
    )


def _check_filter() -> SecretCheck:
    """Run the scrubber end-to-end rather than asserting it is installed."""
    if self_test():
        return SecretCheck(
            name="redaction-filter",
            verdict="ok",
            detail="A canary secret was scrubbed from both the message and the extra payload.",
        )
    return SecretCheck(
        name="redaction-filter",
        verdict="error",
        detail="SecretRedactingFilter did not scrub a canary value.",
        fix="Check whether a third-party logging config replaced CodeRouter's handlers.",
    )


def _check_written_logs(config: Any) -> SecretCheck:
    """Scan the JSONL sinks already on disk for live credential values.

    This is the check that produces evidence instead of reassurance. If a
    key leaked before redaction existed — or through a sink we do not own —
    it is sitting in ``requests.jsonl`` right now, and the only safe
    remediation is rotation, not deletion.
    """
    state_dir = getattr(config, "state_dir", None)
    if not state_dir:
        return SecretCheck(
            name="written-log-scan",
            verdict="skip",
            detail="state_dir is unset — no JSONL sinks to scan.",
        )
    if not _secrets:
        return SecretCheck(
            name="written-log-scan",
            verdict="skip",
            detail="No credentials registered — nothing to search for.",
        )
    hits = scan_logs_for_secrets(state_dir)
    if not hits:
        return SecretCheck(
            name="written-log-scan",
            verdict="ok",
            detail=f"No registered credential found in the JSONL sinks under {state_dir}.",
        )
    where = ", ".join(f"{p.name}:{line} ({label})" for p, label, line in hits[:5])
    more = "" if len(hits) <= 5 else f" (+{len(hits) - 5} more)"
    return SecretCheck(
        name="written-log-scan",
        verdict="error",
        detail=f"Credential values found in written logs: {where}{more}",
        fix="Rotate the affected credentials, then delete the offending log files.",
    )


def check_secret_hygiene(config: Any) -> SecretReport:
    """Run the whole secret-hygiene suite against a loaded config."""
    register_config_secrets(config)
    return SecretReport(
        checks=[
            _check_filter(),
            _check_registry(config),
            _check_embedded_credentials(config),
            _check_written_logs(config),
        ]
    )


_VERDICT_GLYPH = {"ok": "OK  ", "skip": "SKIP", "warn": "WARN", "error": "FAIL"}


def format_secret_report(report: SecretReport) -> str:
    """Render a report for the terminal, in ``--check-env``'s layout."""
    from coderouter.messages import tr

    lines = ["", "coderouter doctor --check-secrets", "=" * 52]
    for check in report.checks:
        lines.append(f"[{_VERDICT_GLYPH.get(check.verdict, '????')}] {check.name}")
        lines.append(f"        {check.detail}")
        if check.fix:
            label = tr("L_FIX_LABEL")
            lines.append(f"        {label}: {check.fix}")
    code = exit_code_for_secret_report(report)
    verdict_map = {
        0: tr("I1506_SECRET_VERDICT_CLEAN"),
        2: tr("I1506_SECRET_VERDICT_ATTENTION"),
        1: tr("I1506_SECRET_VERDICT_BLOCKER"),
    }
    verdict_word = verdict_map.get(code, str(code))
    lines.extend(["-" * 52, tr("I1506_SECRET_VERDICT", verdict=verdict_word, code=code), ""])
    return "\n".join(lines)

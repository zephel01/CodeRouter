"""Load and validate CodeRouter configuration.

Search order (first hit wins):
    1. Path passed explicitly (CLI --config flag)
    2. $CODEROUTER_CONFIG env var
    3. ./providers.yaml (current working dir) — opt-in since v2.13.0,
       gated behind ``CODEROUTER_ALLOW_CWD_CONFIG`` (truthy: 1/true/yes/on)
    4. ~/.coderouter-t/providers.yaml

Secrets are resolved by reading the env var named by `api_key_env`.

.. note::
    **v2.13.0 (security):** step 3 above (implicit CWD discovery) is now
    opt-in, gated behind ``CODEROUTER_ALLOW_CWD_CONFIG``. It was a code
    execution vector: a hostile ``providers.yaml`` dropped into a repo
    could steer ``restart_command`` / ``launcher.backends[*].binary`` /
    ``launcher.bench.command_template`` — all of which name executables —
    simply because CodeRouter happened to be started from that directory.
    When the opt-in is enabled and step 3 resolves the config, a one-time
    ``cwd-config-loaded`` warning fires (never when an explicit
    ``--config`` / ``CODEROUTER_CONFIG`` path was used, even if it happens
    to point at the same file). When the opt-in is NOT set but a
    ``./providers.yaml`` exists and was not explicitly named, a one-time
    ``cwd-config-skipped`` warning fires so the operator understands why
    that file was ignored.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from coderouter.config.schemas import CodeRouterConfig
from coderouter.logging import get_logger
from coderouter.messages import tr
from coderouter.secret_redaction import register_config_secrets, register_secret

logger = get_logger(__name__)

# Name of the env var that opts CWD ``providers.yaml`` discovery back in
# (see module docstring). v2.13.0 gates step 3 of the search behind it.
CWD_CONFIG_ENV = "CODEROUTER_ALLOW_CWD_CONFIG"
# Truthy vocabulary shared by every CWD-opt-in / ALLOW_PAID style toggle.
_TRUTHY = {"1", "true", "yes", "on"}

# Guards the one-time ``cwd-config-loaded`` warning (opt-in enabled and the
# CWD step actually resolved) so a process that calls load_config()
# repeatedly (tests, hot-reload, multiple create_app() calls) only logs it
# once.
_cwd_config_warning_emitted = False
# Guards the one-time ``cwd-config-skipped`` warning (opt-in NOT set but a
# ./providers.yaml exists and was ignored) — same once-per-process rationale.
_cwd_config_skip_warning_emitted = False


def cwd_config_allowed() -> bool:
    """True iff the CWD ``providers.yaml`` opt-in is enabled (v2.13.0)."""
    return os.environ.get(CWD_CONFIG_ENV, "").strip().lower() in _TRUTHY


def _candidate_paths(explicit: str | os.PathLike[str] | None) -> list[Path]:
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit))
    if env_path := os.environ.get("CODEROUTER_CONFIG"):
        paths.append(Path(env_path))
    # v2.13.0: implicit CWD discovery is opt-in — a hostile providers.yaml
    # dropped into a repo could otherwise steer restart_command / launcher
    # binaries simply because CodeRouter was started from that directory.
    if cwd_config_allowed():
        paths.append(Path.cwd() / "providers.yaml")
    paths.append(Path.home() / ".coderouter-t" / "providers.yaml")
    return paths


def resolve_config_path(
    explicit: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Return the config file the loader would pick, or None if none exists.

    Single source of truth for the search order so callers (e.g.
    ``coderouter.cli._resolve_config_path`` driving ``doctor --apply``)
    never re-implement it. Re-implementing it risks writing back to a
    different file than the one :func:`load_config` actually read — the
    exact regression the v2.13.0 CWD opt-in would otherwise introduce.
    """
    return next((p for p in _candidate_paths(explicit) if p.is_file()), None)


def _explicitly_named(
    path: Path, explicit: str | os.PathLike[str] | None
) -> bool:
    """True iff ``path`` matches the literal --config or CODEROUTER_CONFIG value.

    Used to keep the CWD warnings quiet when ``./providers.yaml`` was
    chosen on purpose (an explicit choice, however coincidental, is not
    the implicit-discovery behaviour being gated).
    """
    if explicit and Path(explicit) == path:
        return True
    env_path = os.environ.get("CODEROUTER_CONFIG")
    return env_path is not None and Path(env_path) == path


def _warn_if_cwd_config(
    chosen: Path, *, explicit: str | os.PathLike[str] | None
) -> None:
    """Emit the one-time ``cwd-config-loaded`` warning (opt-in is enabled).

    Only fires when ``chosen`` was resolved via the *implicit* CWD-search
    step — which, since v2.13.0, only runs when
    ``CODEROUTER_ALLOW_CWD_CONFIG`` is set. Compares ``chosen`` against the
    *literal* explicit / env candidate paths (not merely whether those
    inputs were set) so that an explicit ``--config`` (or
    ``CODEROUTER_CONFIG``) path that happens to coincide with
    ``./providers.yaml`` stays quiet, while an explicit path/env var that
    was set but did NOT resolve (file missing, search fell through to CWD)
    still warns — the CWD step is what actually served the config there.
    """
    global _cwd_config_warning_emitted
    if _cwd_config_warning_emitted:
        return
    cwd_path = Path.cwd() / "providers.yaml"
    if chosen != cwd_path:
        return
    if _explicitly_named(chosen, explicit):
        return

    _cwd_config_warning_emitted = True
    logger.warning(
        "cwd-config-loaded",
        extra={
            "path": str(chosen),
            "hint": tr("W1002_CWD_LOADED", path=chosen),
        },
    )


def _warn_if_cwd_config_skipped(
    explicit: str | os.PathLike[str] | None,
) -> Path | None:
    """Warn once when a ./providers.yaml exists but was skipped (no opt-in).

    Returns the skipped CWD path (so :func:`load_config` can name it in a
    later ``FileNotFoundError``), or None when there is nothing to skip:
    the opt-in is enabled, no ``./providers.yaml`` exists, or the file was
    explicitly named (``--config`` / ``CODEROUTER_CONFIG``) — in which case
    it is not being skipped at all.
    """
    global _cwd_config_skip_warning_emitted
    if cwd_config_allowed():
        return None
    cwd_path = Path.cwd() / "providers.yaml"
    if not cwd_path.is_file():
        return None
    if _explicitly_named(cwd_path, explicit):
        return None

    if not _cwd_config_skip_warning_emitted:
        _cwd_config_skip_warning_emitted = True
        logger.warning(
            "cwd-config-skipped",
            extra={
                "path": str(cwd_path),
                "hint": tr("W1003_CWD_SKIPPED", path=cwd_path),
            },
        )
    return cwd_path


def load_config(path: str | os.PathLike[str] | None = None) -> CodeRouterConfig:
    """Load providers.yaml + apply ALLOW_PAID env override."""
    skipped_cwd = _warn_if_cwd_config_skipped(path)
    candidates = _candidate_paths(path)
    chosen: Path | None = next((p for p in candidates if p.is_file()), None)
    if chosen is None:
        searched = "\n  ".join(str(p) for p in candidates)
        from coderouter.errors import ConfigNotFoundError

        message = tr("E1001", searched=searched)
        hint: str | None = None
        if skipped_cwd is not None:
            hint = tr("E1001_CWD_NOTE", path=skipped_cwd)
            message += "\n" + hint
        raise ConfigNotFoundError(message, message_id="E1001", hint=hint)
    _warn_if_cwd_config(chosen, explicit=path)

    with chosen.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # v0.6-A: CODEROUTER_MODE env overrides the YAML default_profile BEFORE
    # initial validation, so that (a) a typo'd file default that would otherwise
    # fail can be rescued by an explicit env-set mode, and (b) the model-
    # validator's "default_profile must exist in profiles" check applies to the
    # *effective* mode the engine will see, not the pre-override YAML value.
    #
    # v1.8.0+: also resolve env_mode through ``mode_aliases`` before assigning,
    # so that startup-time ``--mode coding`` (env CODEROUTER_MODE=coding)
    # behaves symmetrically with the runtime ``X-CodeRouter-Mode: coding``
    # header — both should accept short intent names like ``coding`` /
    # ``general`` / ``reasoning`` and resolve them to the underlying profile
    # (e.g. ``claude-code-nim`` in providers.nvidia-nim.yaml). Without this,
    # users on the NIM example yaml hit
    #   "default_profile 'coding' is not declared in profiles:
    #    known=['claude-code-nim', ...]"
    # because mode_aliases only fired at request time, not at startup.
    env_mode = os.environ.get("CODEROUTER_MODE", "").strip()
    if env_mode:
        # Pre-validation alias resolution: if env_mode isn't directly a
        # profile name but matches an entry in raw["mode_aliases"], swap it
        # for the underlying profile name. This avoids forcing every example
        # yaml to mirror the v1.8.0 four-profile names (multi/coding/general
        # /reasoning) just to accept the canonical short --mode flags.
        raw_profiles = raw.get("profiles", []) or []
        profile_names = {
            p.get("name") for p in raw_profiles if isinstance(p, dict)
        }
        raw_aliases = raw.get("mode_aliases", {}) or {}
        if (
            env_mode not in profile_names
            and isinstance(raw_aliases, dict)
            and env_mode in raw_aliases
        ):
            env_mode = raw_aliases[env_mode]
        raw["default_profile"] = env_mode

    config = CodeRouterConfig.model_validate(raw)

    # Env var ALLOW_PAID overrides file value (so users can flip it per-shell)
    env_paid = os.environ.get("ALLOW_PAID", "").strip().lower()
    if env_paid in {"1", "true", "yes", "on"}:
        config.allow_paid = True
    elif env_paid in {"0", "false", "no", "off"}:
        config.allow_paid = False

    # v2.14.0: arm log redaction before anything else runs. Registering here
    # (rather than lazily on the first resolve_api_key call) means the
    # startup log lines — which name providers and profiles — are already
    # scrubbed, and so is any config-validation error raised downstream.
    # Only labels are logged; the values never leave the registry.
    registered = register_config_secrets(config)
    if registered:
        logger.info(
            "secret-redaction-armed",
            extra={"count": len(registered), "labels": sorted(set(registered))},
        )

    return config


def resolve_api_key(api_key_env: str | None) -> str | None:
    """Look up an API key from the named env var. Returns None if unset.

    v2.14.0: the resolved value is registered with
    :mod:`coderouter.secret_redaction` before it is handed out. This is the
    moment a credential enters the process, so it is the one place that can
    guarantee the log scrubber knows about every key we actually use —
    including keys for providers added by an adapter plugin, which never
    pass through ``register_config_secrets``.
    """
    if not api_key_env:
        return None
    value = os.environ.get(api_key_env, "").strip()
    if value:
        register_secret(value, api_key_env)
    return value or None

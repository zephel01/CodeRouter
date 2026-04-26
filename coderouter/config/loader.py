"""Load and validate CodeRouter configuration.

Search order (first hit wins):
    1. Path passed explicitly (CLI --config flag)
    2. $CODEROUTER_CONFIG env var
    3. ./providers.yaml (current working dir)
    4. ~/.coderouter/providers.yaml

Secrets are resolved by reading the env var named by `api_key_env`.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from coderouter.config.schemas import CodeRouterConfig


def _candidate_paths(explicit: str | os.PathLike[str] | None) -> list[Path]:
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit))
    if env_path := os.environ.get("CODEROUTER_CONFIG"):
        paths.append(Path(env_path))
    paths.append(Path.cwd() / "providers.yaml")
    paths.append(Path.home() / ".coderouter" / "providers.yaml")
    return paths


def load_config(path: str | os.PathLike[str] | None = None) -> CodeRouterConfig:
    """Load providers.yaml + apply ALLOW_PAID env override."""
    candidates = _candidate_paths(path)
    chosen: Path | None = next((p for p in candidates if p.is_file()), None)
    if chosen is None:
        searched = "\n  ".join(str(p) for p in candidates)
        raise FileNotFoundError(
            f"providers.yaml not found. Searched:\n  {searched}\n"
            f"Hint: copy examples/providers.yaml to ~/.coderouter/providers.yaml"
        )

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

    return config


def resolve_api_key(api_key_env: str | None) -> str | None:
    """Look up an API key from the named env var. Returns None if unset."""
    if not api_key_env:
        return None
    value = os.environ.get(api_key_env, "").strip()
    return value or None

"""``coderouter doctor --check-model --apply`` — non-destructive YAML write-back.

Purpose (v1.7-B #3)
-------------------
``coderouter/doctor.py`` emits human-readable YAML patches that an operator
copy-pastes into ``providers.yaml`` / ``model-capabilities.yaml``. Doing
that by hand is fine for one provider, but on a fresh machine after
``setup.sh`` (v1.7-B #4) it accumulates: 6 probes x N providers means a
lot of clipboard work. ``--apply`` automates the write so the operator's
loop becomes::

    coderouter doctor --check-model ollama --apply
    # … inspect the diff that printed, .yaml.bak created, providers.yaml updated …
    coderouter doctor --check-model ollama   # re-run; should be all OK

``--dry-run`` is the same path minus the file write — it prints a
unified diff (``git apply``-compatible) so the operator can review
before committing.

Design contract
---------------
The "non-destructive" guarantee in plan.md §11.B.4 #3 means three things:

1. **Comments and key order are preserved.** The user's hand-edited
   commentary explaining why a provider is wired a certain way must
   survive the round-trip. This is why we use ``ruamel.yaml`` (in
   ``[project.optional-dependencies].doctor``) rather than pyyaml,
   which discards comments on dump. The base 5-dep budget (plan.md
   §5.4) is preserved by lazy-importing ``ruamel.yaml`` only when
   ``--apply`` / ``--dry-run`` is invoked; the typical ``serve``
   user never pays for the dep.

2. **A backup file is written before mutation.** ``providers.yaml`` is
   copied to ``providers.yaml.bak`` (overwriting any prior backup) so
   a botched apply can be recovered with one ``mv``. We deliberately
   do NOT timestamp the backup — operators using git already have
   versioned history, and a single ``.bak`` is the simplest mental
   model for everyone else.

3. **Idempotency.** Re-running the same ``--apply`` against a freshly-
   patched file is a no-op: no file write, exit 0, "already up to
   date" message. The merge logic compares the loaded doc to the
   target shape and short-circuits when they're equal.

Patch shape contract (mirrors ``coderouter.doctor._patch_*``)
-------------------------------------------------------------
Every probe-emitted patch parses into one of these dict shapes after
comment-stripping (see :func:`parse_patch_yaml`):

* ``providers.yaml`` capability change::

    {"providers": [{"name": NAME, "capabilities": {KEY: VALUE}}]}

* ``providers.yaml`` extra_body.options change (num_ctx / num_predict)::

    {"providers": [{"name": NAME, "extra_body": {"options": {KEY: VALUE}}}]}

* ``providers.yaml`` output_filters change::

    {"providers": [{"name": NAME, "output_filters": [FILTER, ...]}]}

* ``model-capabilities.yaml`` rule append::

    {"rules": [{"match": MATCH, "kind": KIND, "capabilities": {KEY: VALUE}}]}

The doctor's patch emitter (``coderouter.doctor._patch_*``) is the
single source of truth for these shapes; this module mirrors them in
:func:`merge_provider_patch_into_doc` and :func:`merge_capabilities_rule_into_doc`.
Adding a new probe means (a) extending the doctor's emitter and
(b) extending the corresponding merge helper here — schema invariance
is enforced by the round-trip tests in ``tests/test_doctor_apply.py``.
"""

from __future__ import annotations

import difflib
import io
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml as _pyyaml

__all__ = [
    "PROVIDERS_BACKUP_SUFFIX",
    "ApplyResult",
    "DoctorApplyError",
    "MissingDependencyError",
    "apply_doctor_patches",
    "deep_merge_dicts",
    "merge_capabilities_rule_into_doc",
    "merge_provider_patch_into_doc",
    "parse_patch_yaml",
    "render_unified_diff",
]


PROVIDERS_BACKUP_SUFFIX: str = ".bak"
"""File extension appended to the YAML target when ``--apply`` writes.

Single-suffix (``providers.yaml.bak``) so the backup overwrites between
runs — operators tracking history via git get a more useful timeline
from ``git diff`` than from accumulating ``.bak.20260426-141533``
files. Operators not using git can copy the ``.bak`` aside before
re-running if they want a longer trail.
"""


class DoctorApplyError(Exception):
    """Raised on a recoverable failure during ``--apply`` / ``--dry-run``.

    Distinct from :class:`MissingDependencyError` so the CLI can render a
    concise ``error: <message>`` line without a stack trace, while a
    bug would still crash through to the unhandled-exception path.
    """


class MissingDependencyError(DoctorApplyError):
    """Raised when ``ruamel.yaml`` (the optional ``[doctor]`` extra) is absent.

    Carries a ready-to-show install hint so the CLI can dump a single
    actionable line and exit 1 without forcing the user to read a
    Python traceback.
    """


# ---------------------------------------------------------------------------
# Patch parsing — strip the doctor's leading comment + load the YAML body
# ---------------------------------------------------------------------------


def parse_patch_yaml(patch: str) -> dict[str, Any]:
    """Parse a ``ProbeResult.suggested_patch`` YAML string into a dict.

    The doctor emits patches with leading ``# providers.yaml — ...``
    explanatory comments and a ``# ... existing fields ...`` placeholder
    line. Both are stripped before ``yaml.safe_load``; only the
    structured YAML body remains. This mirrors the existing
    ``test_patch_is_loadable_yaml`` pattern in
    ``tests/test_doctor.py`` so the parsing rule is identical to the
    one tests already verify the emitter against.

    Returns an empty dict for an empty / comment-only patch (defensive
    — the doctor never emits one but a future probe might).
    """
    body_lines = [
        line for line in patch.splitlines() if not line.lstrip().startswith("#")
    ]
    body = "\n".join(body_lines)
    parsed = _pyyaml.safe_load(body) if body.strip() else None
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        # The doctor never emits a non-mapping top level. Surface this as
        # a hard error rather than silently returning {}, so a future
        # probe regression is loud.
        raise DoctorApplyError(
            f"unexpected patch shape: top-level YAML is "
            f"{type(parsed).__name__}, expected dict"
        )
    return parsed


# ---------------------------------------------------------------------------
# Generic deep merge — the building block under both target-file mergers
# ---------------------------------------------------------------------------


def deep_merge_dicts(base: Any, patch: dict[str, Any]) -> bool:
    """Recursively merge ``patch`` into ``base`` mapping. Mutates ``base``.

    Returns True if any leaf value was added or changed; False if
    ``base`` already contained every patch key with the same value
    (idempotent no-op). Used by both the providers.yaml merger and the
    rules-list merger to deep-update individual entries.

    ``base`` is typed as ``Any`` so it accepts both plain ``dict`` (in
    tests) and ruamel.yaml's ``CommentedMap`` (in production) without
    requiring a dependency on ruamel at this signature level — both
    types implement the same mutable-mapping protocol we use.

    Lists are NOT merged element-wise — when both base and patch
    contain a list under the same key, the patch list **replaces** the
    base list. This matches what an operator means by
    ``output_filters: [strip_thinking]`` (replace, not append) and
    keeps the merge semantically simple. Probes that need append-style
    behavior (e.g. the model-capabilities rules append) handle it at
    the next layer up in :func:`merge_capabilities_rule_into_doc`.
    """
    changed = False
    for key, new_value in patch.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(new_value, dict)
        ):
            # Both sides are mappings → recurse.
            if deep_merge_dicts(base[key], new_value):
                changed = True
        else:
            existing = base.get(key, _MISSING)
            if existing == new_value:
                # Idempotent — value already at the target. Skip the
                # write so a re-run sees no change.
                continue
            base[key] = new_value
            changed = True
    return changed


# Sentinel for "key absent" so we can distinguish ``base.get(key) == None``
# from "key not in base" without two dict lookups. Module-level tuple so
# identity comparison is cheap and stable across calls.
_MISSING: tuple[()] = ()


# ---------------------------------------------------------------------------
# Per-target-file merge helpers
# ---------------------------------------------------------------------------


def merge_provider_patch_into_doc(doc: Any, patch_dict: dict[str, Any]) -> bool:
    """Apply a ``providers.yaml`` patch dict to a loaded doc in-place.

    Expects ``patch_dict`` shape::

        {"providers": [{"name": NAME, ...keys to merge under that provider}]}

    Walks ``doc["providers"]`` for an entry whose ``name`` matches and
    deep-merges every other key from the patch's provider entry into
    that doc entry. Returns True if any key changed; False on idempotent
    no-op or if no matching provider was found.

    Failure modes:

    * No ``providers`` key in ``doc`` → ``DoctorApplyError`` (doc
      doesn't look like providers.yaml at all).
    * ``patch_dict`` lacks a ``providers`` list with a single entry
      → ``DoctorApplyError`` (unexpected emitter shape — would
      indicate a doctor regression).
    * No provider in ``doc`` matches the patch's ``name`` → returns
      False (the operator likely renamed or removed the provider
      between probes; the CLI surfaces this as a no-op with a hint).
    """
    if not isinstance(doc, dict) or "providers" not in doc:
        raise DoctorApplyError(
            "target file is missing the top-level 'providers:' key — "
            "is this really providers.yaml?"
        )
    patch_providers = patch_dict.get("providers")
    if not isinstance(patch_providers, list) or len(patch_providers) != 1:
        raise DoctorApplyError(
            "providers.yaml patch must contain exactly one provider entry, "
            f"got {patch_providers!r}"
        )
    patch_entry = patch_providers[0]
    if not isinstance(patch_entry, dict) or "name" not in patch_entry:
        raise DoctorApplyError(
            "providers.yaml patch entry is missing the 'name' key — "
            "cannot identify the target provider"
        )
    target_name = patch_entry["name"]
    overrides = {k: v for k, v in patch_entry.items() if k != "name"}

    for entry in doc["providers"]:
        if not isinstance(entry, dict):
            continue
        if entry.get("name") == target_name:
            return deep_merge_dicts(entry, overrides)
    return False


def merge_capabilities_rule_into_doc(doc: Any, patch_dict: dict[str, Any]) -> bool:
    """Apply a ``model-capabilities.yaml`` patch dict to a loaded doc in-place.

    Expects ``patch_dict`` shape::

        {"rules": [{"match": MATCH, "kind": KIND, "capabilities": {...}}]}

    Append-or-merge semantics: if the doc already contains a rule with
    identical ``(match, kind)`` keys, the patch's ``capabilities`` are
    deep-merged into that rule (idempotent if values match). If no such
    rule exists, the patch entry is appended verbatim to ``doc['rules']``.

    Returns True on append or change; False on idempotent no-op.

    The ``rules:`` key is auto-created when absent (the user-side
    ``~/.coderouter/model-capabilities.yaml`` may legitimately start
    empty).
    """
    if not isinstance(doc, dict):
        raise DoctorApplyError(
            "target file is not a YAML mapping — is this really "
            "model-capabilities.yaml?"
        )
    patch_rules = patch_dict.get("rules")
    if not isinstance(patch_rules, list) or len(patch_rules) != 1:
        raise DoctorApplyError(
            "model-capabilities.yaml patch must contain exactly one rule "
            f"entry, got {patch_rules!r}"
        )
    new_rule = patch_rules[0]
    if not isinstance(new_rule, dict) or "match" not in new_rule:
        raise DoctorApplyError(
            "model-capabilities.yaml patch rule is missing the 'match' key"
        )
    target_match = new_rule["match"]
    target_kind = new_rule.get("kind", "any")

    if "rules" not in doc or doc["rules"] is None:
        doc["rules"] = []

    for rule in doc["rules"]:
        if not isinstance(rule, dict):
            continue
        if rule.get("match") == target_match and rule.get("kind", "any") == target_kind:
            # Existing rule matches — deep-merge capabilities only.
            new_caps = new_rule.get("capabilities", {})
            if not isinstance(new_caps, dict) or not new_caps:
                return False
            existing_caps = rule.setdefault("capabilities", {})
            return deep_merge_dicts(existing_caps, new_caps)

    # No matching rule — append verbatim.
    doc["rules"].append(new_rule)
    return True


# ---------------------------------------------------------------------------
# ruamel.yaml round-trip plumbing (lazy)
# ---------------------------------------------------------------------------


def _load_yaml_with_comments(path: Path) -> tuple[Any, str]:
    """Load a YAML file via ruamel preserving comments. Returns (doc, raw_text).

    ``raw_text`` is the file's contents as read from disk (used for
    pre-image of the unified diff). ``doc`` is the round-trip-capable
    representation (CommentedMap / CommentedSeq) ready for in-place
    mutation and a subsequent :func:`_dump_yaml_with_comments`.

    Raises :class:`MissingDependencyError` when ``ruamel.yaml`` is
    absent — kept lazy so the typical CLI flow (``serve``) never
    triggers an ImportError.
    """
    try:
        from ruamel.yaml import YAML
    except ImportError as exc:  # pragma: no cover — exercised in CLI smoke
        raise MissingDependencyError(
            "doctor --apply / --dry-run requires the optional "
            "'ruamel.yaml' dependency. Install with one of:\n"
            "  pip install coderouter-cli[doctor]\n"
            "  uv pip install ruamel.yaml\n"
            "  uv tool install coderouter-cli --with ruamel.yaml"
        ) from exc

    raw_text = path.read_text(encoding="utf-8")
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    # Match the indentation style used in examples/providers.yaml so
    # lines we add line up with hand-written ones in unified diffs.
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    doc = yaml_rt.load(io.StringIO(raw_text))
    return doc, raw_text


def _dump_yaml_with_comments(doc: Any) -> str:
    """Render a ruamel-loaded doc back to text. Returns the YAML string."""
    from ruamel.yaml import YAML

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    out = io.StringIO()
    yaml_rt.dump(doc, out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Diff rendering
# ---------------------------------------------------------------------------


def render_unified_diff(*, before: str, after: str, path: Path) -> str:
    """Build a ``git apply``-compatible unified diff of two YAML strings.

    Empty result when ``before == after`` (caller should treat as a no-op
    and avoid printing). Headers use the file's path verbatim so an
    operator can pipe the output into ``patch`` or save it as a ``.diff``
    for review without further massaging.
    """
    if before == after:
        return ""
    diff_lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=str(path),
        tofile=str(path),
        n=3,
    )
    return "".join(diff_lines)


# ---------------------------------------------------------------------------
# Top-level apply entry point
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    """Outcome of an apply / dry-run pass over one DoctorReport.

    Attributes
    ----------
    target_paths
        Files actually touched (or would have been, in dry-run mode).
        Order matches the order patches appeared in
        ``DoctorReport.results`` so the diff sections render in the
        same order the human report does.
    diffs
        Per-target unified-diff text. Empty string for a target that
        had only no-op merges. Indexed by str(path) for stable lookup
        in tests / CLI rendering.
    changes_applied
        Number of probe patches that produced a change. Zero means
        idempotent re-run.
    no_op_patches
        Number of probe patches that resolved to "already up to date"
        — surfaces in the CLI summary so an operator who just applied
        gets a clear "0 changes applied; 3 already up to date" message
        on the second run.
    written
        True if at least one file was actually written to disk
        (``--apply`` mode). False for ``--dry-run`` and for cases
        where every patch was a no-op.
    backups
        Map of original-path → backup-path for every file that was
        written. Empty in dry-run mode.
    skipped_unknown_target
        Targets that doctor produced but we don't know how to apply.
        Currently empty (we cover all known target_file values), but
        kept for forward-compat: if a future probe emits a new
        target_file value, we should report it without crashing.
    """

    target_paths: list[Path]
    diffs: dict[str, str]
    changes_applied: int
    no_op_patches: int
    written: bool
    backups: dict[str, str] = field(default_factory=dict)
    skipped_unknown_target: list[str] = field(default_factory=list)

    @property
    def is_no_op(self) -> bool:
        """True when no diffs would be written (idempotent re-run)."""
        return self.changes_applied == 0


def _resolve_target_path(
    *,
    target_file: str,
    config_path: Path,
) -> Path | None:
    """Map a doctor ``target_file`` token to a concrete path on disk.

    ``"providers.yaml"`` resolves to the same path the loader picked up
    (i.e. the file backing the live ``CodeRouterConfig``). The
    ``model-capabilities.yaml`` token resolves to the user-side
    override path (``~/.coderouter/model-capabilities.yaml``) — the
    bundled file inside the package is intentionally read-only, and
    new rules belong in the user-layer override per the registry's
    layering contract.

    Returns ``None`` for an unknown token; callers report it via
    ``ApplyResult.skipped_unknown_target``.
    """
    if target_file == "providers.yaml":
        return config_path
    if target_file == "model-capabilities.yaml":
        return Path.home() / ".coderouter" / "model-capabilities.yaml"
    return None


def apply_doctor_patches(
    *,
    report: Any,  # DoctorReport — typed Any to avoid an import cycle
    config_path: Path,
    write: bool,
    user_capabilities_path: Path | None = None,
) -> ApplyResult:
    """Apply every NEEDS_TUNING probe's patch back into its target file.

    Iterates ``report.results``; for each result that has a
    ``suggested_patch`` and a ``target_file``, parses the patch, merges
    it into the (cached) loaded doc for that target, and accumulates a
    diff. When ``write=True``, after all merges the buffered docs are
    dumped back to disk (with ``.bak`` backup); otherwise the diffs are
    returned without touching disk.

    The per-target merge keeps a single in-memory doc per file so two
    probes targeting the same file compose into one final diff.

    ``user_capabilities_path`` is a test-only injection point — leave
    None in production to use the standard
    ``~/.coderouter/model-capabilities.yaml`` resolution.
    """
    # Pre-import ruamel once so a missing dep fails fast before we read
    # the report. The actual load/dump still happens via the lazy helpers
    # so a successful import here also primes their cache.
    _load_yaml_with_comments  # noqa: B018 — referenced for clarity

    docs_by_path: dict[Path, tuple[Any, str]] = {}
    diffs: dict[str, str] = {}
    target_order: list[Path] = []
    changes_applied = 0
    no_op_patches = 0
    skipped: list[str] = []

    for result in report.results:
        patch_str: str | None = getattr(result, "suggested_patch", None)
        target_file: str | None = getattr(result, "target_file", None)
        if not patch_str or not target_file:
            continue

        # Resolve target path, with user-side capabilities override
        # honoring the test-only injection point.
        if target_file == "model-capabilities.yaml" and user_capabilities_path is not None:
            target_path = user_capabilities_path
        else:
            resolved = _resolve_target_path(
                target_file=target_file, config_path=config_path
            )
            if resolved is None:
                skipped.append(target_file)
                continue
            target_path = resolved

        # Load the target doc lazily — only files that actually receive
        # a patch get touched.
        if target_path not in docs_by_path:
            if target_path.is_file():
                doc, before_text = _load_yaml_with_comments(target_path)
            else:
                # User-side capabilities file may not exist yet — start
                # from an empty mapping and let the merger seed the
                # ``rules:`` list. ``before_text`` is empty so the diff
                # shows the entire created file.
                from ruamel.yaml.comments import CommentedMap
                doc = CommentedMap()
                before_text = ""
            docs_by_path[target_path] = (doc, before_text)
            target_order.append(target_path)

        doc, _ = docs_by_path[target_path]

        try:
            patch_dict = parse_patch_yaml(patch_str)
        except DoctorApplyError as exc:
            raise DoctorApplyError(
                f"probe {result.name!r}: failed to parse patch: {exc}"
            ) from exc

        try:
            if target_file == "providers.yaml":
                changed = merge_provider_patch_into_doc(doc, patch_dict)
            else:  # model-capabilities.yaml
                changed = merge_capabilities_rule_into_doc(doc, patch_dict)
        except DoctorApplyError as exc:
            raise DoctorApplyError(
                f"probe {result.name!r}: failed to merge patch into "
                f"{target_path}: {exc}"
            ) from exc

        if changed:
            changes_applied += 1
        else:
            no_op_patches += 1

    # Render diffs and write (or just report).
    written = False
    backups: dict[str, str] = {}
    for target_path in target_order:
        doc, before_text = docs_by_path[target_path]
        after_text = _dump_yaml_with_comments(doc)
        diff_text = render_unified_diff(
            before=before_text, after=after_text, path=target_path
        )
        diffs[str(target_path)] = diff_text
        if write and diff_text:
            # Backup first (only when we have a pre-existing file).
            if target_path.is_file():
                backup_path = target_path.with_suffix(
                    target_path.suffix + PROVIDERS_BACKUP_SUFFIX
                )
                shutil.copy2(target_path, backup_path)
                backups[str(target_path)] = str(backup_path)
            else:
                target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(after_text, encoding="utf-8")
            written = True

    return ApplyResult(
        target_paths=target_order,
        diffs=diffs,
        changes_applied=changes_applied,
        no_op_patches=no_op_patches,
        written=written,
        backups=backups,
        skipped_unknown_target=skipped,
    )

"""Unit tests for v1.7-B (#3) ``coderouter doctor --check-model --apply``.

Scope:
    - :func:`coderouter.doctor_apply.parse_patch_yaml` — strips the
      doctor-emitted comments and yields the structured dict.
    - :func:`coderouter.doctor_apply.deep_merge_dicts` — recursive
      merge with idempotent no-op when values match.
    - :func:`coderouter.doctor_apply.merge_provider_patch_into_doc` /
      :func:`coderouter.doctor_apply.merge_capabilities_rule_into_doc`
      — per-target shape handling on top of deep_merge.
    - :func:`coderouter.doctor_apply.apply_doctor_patches` — end-to-end
      with a fake DoctorReport, including comment preservation, backup
      file creation, and dry-run vs write modes.
    - Idempotency: applying the same patch twice does NOT mutate the
      file the second time.

The tests deliberately exercise the same patch-string shapes that
``coderouter/doctor.py`` emits via its ``_patch_*`` helpers (which are
covered separately in tests/test_doctor.py); a regression in the
emitter would still surface here as a parse / merge failure with a
clearer error site.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from coderouter import doctor_apply
from coderouter.doctor_apply import (
    ApplyResult,
    DoctorApplyError,
    apply_doctor_patches,
    deep_merge_dicts,
    merge_capabilities_rule_into_doc,
    merge_provider_patch_into_doc,
    parse_patch_yaml,
)

# v1.8.0: end-to-end apply tests require the optional ``ruamel.yaml``
# dependency (declared in ``[project.optional-dependencies].doctor`` /
# ``[dev]``). When it's missing — typical on a fresh ``uv tool install
# coderouter-t`` without the ``[doctor]`` extra — the apply-stage
# tests cleanly skip rather than fail. Parse / merge unit tests above
# don't need ruamel and stay active in all environments.
#
# Note: ``importlib.util.find_spec("ruamel.yaml")`` raises
# ``ModuleNotFoundError`` (rather than returning ``None``) when the
# parent ``ruamel`` package itself is missing — that's a Python quirk
# documented since 3.4. Using try/import is the robust pattern that
# handles both "parent missing" and "child missing" cases.
try:
    import ruamel.yaml  # noqa: F401
    _RUAMEL_AVAILABLE = True
except ImportError:
    _RUAMEL_AVAILABLE = False

_requires_ruamel = pytest.mark.skipif(
    not _RUAMEL_AVAILABLE,
    reason=(
        "ruamel.yaml not installed (required by `coderouter doctor "
        "--check-model --apply` end-to-end tests). Install with: "
        "uv pip install 'ruamel.yaml>=0.18.6' or "
        "uv sync --extra dev"
    ),
)

# ----------------------------------------------------------------------
# Helpers — mimic DoctorReport / ProbeResult without importing doctor.py
# (which would pull in httpx and friends for tests that don't need them).
# ----------------------------------------------------------------------


@dataclass
class _FakeProbeResult:
    name: str
    suggested_patch: str | None = None
    target_file: str | None = None


@dataclass
class _FakeReport:
    results: list[_FakeProbeResult] = field(default_factory=list)


# Verbatim doctor-emitted patch strings (mirrors coderouter/doctor.py
# _patch_* helpers — kept as constants so a doctor-side regression is
# caught here as a parse mismatch.)
PATCH_NUM_CTX = textwrap.dedent(
    """\
    # providers.yaml — update the entry for 'ollama-default' (merge into any existing extra_body):
    providers:
      - name: ollama-default
        # ... existing fields ...
        extra_body:
          options:
            num_ctx: 32768
    """
)

PATCH_TOOLS_TRUE = textwrap.dedent(
    """\
    # providers.yaml — update the entry for 'local':
    providers:
      - name: local
        # ... existing fields ...
        capabilities:
          tools: true
    """
)

PATCH_OUTPUT_FILTERS = textwrap.dedent(
    """\
    # providers.yaml — update the entry for 'local' (merge if a chain already exists):
    providers:
      - name: local
        # ... existing fields ...
        output_filters:
          - strip_thinking
          - strip_stop_markers
    """
)

PATCH_CAPABILITIES_RULE = textwrap.dedent(
    """\
    # ~/.coderouter-t/model-capabilities.yaml — append under `rules:`:
    rules:
      - match: 'claude-opus-4-8'
        kind: anthropic
        capabilities:
          thinking: true
    """
)


# ======================================================================
# parse_patch_yaml
# ======================================================================


def test_parse_strips_leading_comment_and_placeholder() -> None:
    """The doctor's leading ``# providers.yaml — ...`` and the
    ``# ... existing fields ...`` placeholder must both be dropped
    before yaml.safe_load."""
    parsed = parse_patch_yaml(PATCH_NUM_CTX)
    assert parsed == {
        "providers": [
            {
                "name": "ollama-default",
                "extra_body": {"options": {"num_ctx": 32768}},
            }
        ]
    }


def test_parse_capability_patch_yields_expected_shape() -> None:
    parsed = parse_patch_yaml(PATCH_TOOLS_TRUE)
    assert parsed == {
        "providers": [{"name": "local", "capabilities": {"tools": True}}]
    }


def test_parse_output_filters_yields_list() -> None:
    parsed = parse_patch_yaml(PATCH_OUTPUT_FILTERS)
    assert parsed == {
        "providers": [
            {
                "name": "local",
                "output_filters": ["strip_thinking", "strip_stop_markers"],
            }
        ]
    }


def test_parse_capabilities_rule_patch() -> None:
    parsed = parse_patch_yaml(PATCH_CAPABILITIES_RULE)
    assert parsed == {
        "rules": [
            {
                "match": "claude-opus-4-8",
                "kind": "anthropic",
                "capabilities": {"thinking": True},
            }
        ]
    }


def test_parse_empty_patch_returns_empty_dict() -> None:
    """Defensive: a comment-only patch should not crash."""
    assert parse_patch_yaml("# just a comment\n# nothing more\n") == {}


def test_parse_rejects_non_mapping_top_level() -> None:
    """If a future doctor regression emits a list or scalar at the top
    level, raise loudly rather than silently returning {}."""
    with pytest.raises(DoctorApplyError, match="unexpected patch shape"):
        parse_patch_yaml("- just\n- a\n- list\n")


# ======================================================================
# deep_merge_dicts
# ======================================================================


def test_deep_merge_adds_missing_key_returns_changed_true() -> None:
    base: dict[str, object] = {"a": 1}
    assert deep_merge_dicts(base, {"b": 2}) is True
    assert base == {"a": 1, "b": 2}


def test_deep_merge_overwrites_scalar_returns_changed_true() -> None:
    base: dict[str, object] = {"a": 1}
    assert deep_merge_dicts(base, {"a": 2}) is True
    assert base == {"a": 2}


def test_deep_merge_idempotent_when_values_equal_returns_false() -> None:
    """Same value → no mutation, no signal that a change happened."""
    base: dict[str, object] = {"a": 1, "b": {"c": 2}}
    assert deep_merge_dicts(base, {"a": 1, "b": {"c": 2}}) is False
    assert base == {"a": 1, "b": {"c": 2}}


def test_deep_merge_nested_mapping_recurses() -> None:
    base: dict[str, object] = {"extra_body": {"options": {"num_predict": 4096}}}
    patch = {"extra_body": {"options": {"num_ctx": 32768}}}
    assert deep_merge_dicts(base, patch) is True
    assert base == {
        "extra_body": {"options": {"num_predict": 4096, "num_ctx": 32768}}
    }


def test_deep_merge_replaces_lists_wholesale() -> None:
    """output_filters semantics: a patch list REPLACES the base list
    rather than appending. The merger documents this; this test pins
    it so the contract is enforceable."""
    base: dict[str, object] = {"output_filters": ["strip_thinking"]}
    patch = {"output_filters": ["strip_thinking", "strip_stop_markers"]}
    assert deep_merge_dicts(base, patch) is True
    assert base == {
        "output_filters": ["strip_thinking", "strip_stop_markers"]
    }


# ======================================================================
# merge_provider_patch_into_doc — per-target shape handling
# ======================================================================


def test_merge_provider_patch_finds_named_provider() -> None:
    doc = {
        "providers": [
            {"name": "local", "model": "x", "capabilities": {"tools": False}},
            {"name": "other", "model": "y"},
        ]
    }
    patch = {"providers": [{"name": "local", "capabilities": {"tools": True}}]}
    assert merge_provider_patch_into_doc(doc, patch) is True
    assert doc["providers"][0]["capabilities"]["tools"] is True
    # Other provider untouched
    assert doc["providers"][1] == {"name": "other", "model": "y"}


def test_merge_provider_patch_no_match_returns_false() -> None:
    """Provider not present in the doc — silent no-op (the CLI surfaces
    this via the 0-changes summary)."""
    doc = {"providers": [{"name": "different", "model": "x"}]}
    patch = {"providers": [{"name": "local", "capabilities": {"tools": True}}]}
    assert merge_provider_patch_into_doc(doc, patch) is False
    assert doc == {"providers": [{"name": "different", "model": "x"}]}


def test_merge_provider_patch_rejects_doc_without_providers_key() -> None:
    with pytest.raises(DoctorApplyError, match="missing the top-level"):
        merge_provider_patch_into_doc({}, {"providers": [{"name": "x"}]})


def test_merge_provider_patch_rejects_multi_entry_patch() -> None:
    """The doctor only ever emits single-provider patches; reject a
    2-entry list as an emitter regression."""
    doc = {"providers": [{"name": "x"}]}
    patch = {"providers": [{"name": "x"}, {"name": "y"}]}
    with pytest.raises(DoctorApplyError, match="exactly one provider entry"):
        merge_provider_patch_into_doc(doc, patch)


# ======================================================================
# merge_capabilities_rule_into_doc
# ======================================================================


def test_merge_capabilities_rule_appends_when_no_match() -> None:
    doc: dict[str, object] = {"version": 1, "rules": []}
    patch = {
        "rules": [
            {
                "match": "claude-opus-4-8",
                "kind": "anthropic",
                "capabilities": {"thinking": True},
            }
        ]
    }
    assert merge_capabilities_rule_into_doc(doc, patch) is True
    assert doc["rules"] == [
        {
            "match": "claude-opus-4-8",
            "kind": "anthropic",
            "capabilities": {"thinking": True},
        }
    ]


def test_merge_capabilities_rule_idempotent_for_existing_match() -> None:
    """Re-applying an already-present rule is a no-op."""
    doc: dict[str, object] = {
        "version": 1,
        "rules": [
            {
                "match": "claude-opus-4-8",
                "kind": "anthropic",
                "capabilities": {"thinking": True},
            }
        ],
    }
    patch = {
        "rules": [
            {
                "match": "claude-opus-4-8",
                "kind": "anthropic",
                "capabilities": {"thinking": True},
            }
        ]
    }
    assert merge_capabilities_rule_into_doc(doc, patch) is False


def test_merge_capabilities_rule_creates_rules_key_when_missing() -> None:
    """A user-side ~/.coderouter-t/model-capabilities.yaml may legitimately
    start empty; the merger should seed ``rules:`` rather than crash."""
    doc: dict[str, object] = {}
    patch = {
        "rules": [
            {
                "match": "anything",
                "kind": "any",
                "capabilities": {"tools": True},
            }
        ]
    }
    assert merge_capabilities_rule_into_doc(doc, patch) is True
    assert doc["rules"] == [
        {"match": "anything", "kind": "any", "capabilities": {"tools": True}}
    ]


# ======================================================================
# apply_doctor_patches — end-to-end with tmp files
# ======================================================================


def _write_providers_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


PROVIDERS_YAML_INITIAL = textwrap.dedent(
    """\
    # providers.yaml — top-level comment that must survive round-trip.
    allow_paid: false
    default_profile: default
    providers:
      # ---- local ollama (commentary) ----
      - name: local
        kind: openai_compat
        base_url: http://localhost:11434/v1
        model: qwen2.5-coder:7b
        timeout_s: 120
        capabilities:
          tools: false
      # ---- other provider ----
      - name: other
        kind: openai_compat
        base_url: https://example.invalid/v1
        model: foo
        capabilities:
          tools: true
    profiles:
      - name: default
        providers:
          - local
    """
)


@_requires_ruamel
def test_apply_writes_patch_and_creates_backup(tmp_path: Path) -> None:
    """End-to-end: a single tools=true patch lands in providers.yaml,
    the file is written, ``.bak`` exists with the prior contents."""
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_INITIAL)
    report = _FakeReport(
        results=[
            _FakeProbeResult(
                name="tool_calls",
                suggested_patch=PATCH_TOOLS_TRUE,
                target_file="providers.yaml",
            )
        ]
    )

    result = apply_doctor_patches(report=report, config_path=config_path, write=True)

    assert isinstance(result, ApplyResult)
    assert result.changes_applied == 1
    assert result.written is True
    # Backup file should exist with the ORIGINAL contents
    backup_path = config_path.with_suffix(".yaml.bak")
    assert backup_path.is_file()
    assert backup_path.read_text(encoding="utf-8") == PROVIDERS_YAML_INITIAL
    # The actual file should now have tools: true under 'local'
    new_text = config_path.read_text(encoding="utf-8")
    assert "tools: true" in new_text
    # Comment must survive round-trip
    assert "# providers.yaml — top-level comment" in new_text
    assert "# ---- local ollama (commentary) ----" in new_text


@_requires_ruamel
def test_apply_idempotent_second_run_writes_nothing(tmp_path: Path) -> None:
    """First run mutates; second run with the same patch detects no
    diff and returns is_no_op=True without writing again."""
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_INITIAL)
    report = _FakeReport(
        results=[
            _FakeProbeResult(
                name="tool_calls",
                suggested_patch=PATCH_TOOLS_TRUE,
                target_file="providers.yaml",
            )
        ]
    )

    apply_doctor_patches(report=report, config_path=config_path, write=True)
    mtime_after_first = config_path.stat().st_mtime_ns

    # Second run — patch already applied, should be no-op
    second = apply_doctor_patches(
        report=report, config_path=config_path, write=True
    )

    assert second.is_no_op is True
    assert second.changes_applied == 0
    assert second.no_op_patches == 1
    assert second.written is False
    # File mtime must be unchanged on idempotent run
    assert config_path.stat().st_mtime_ns == mtime_after_first


@_requires_ruamel
def test_dry_run_does_not_write_but_returns_diff(tmp_path: Path) -> None:
    """Dry-run: file unchanged, ``.bak`` not created, diff present."""
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_INITIAL)
    report = _FakeReport(
        results=[
            _FakeProbeResult(
                name="tool_calls",
                suggested_patch=PATCH_TOOLS_TRUE,
                target_file="providers.yaml",
            )
        ]
    )

    result = apply_doctor_patches(
        report=report, config_path=config_path, write=False
    )

    assert result.changes_applied == 1
    assert result.written is False
    backup_path = config_path.with_suffix(".yaml.bak")
    assert not backup_path.exists()
    assert config_path.read_text(encoding="utf-8") == PROVIDERS_YAML_INITIAL
    diff = result.diffs[str(config_path)]
    assert "tools: true" in diff
    assert "+" in diff  # additions in unified diff form
    # Header must reference the actual path (git-apply-compatible)
    assert str(config_path) in diff


@_requires_ruamel
def test_apply_handles_capabilities_yaml_creation(tmp_path: Path) -> None:
    """A patch targeting ``model-capabilities.yaml`` lands in the
    user-side path, even when that file doesn't yet exist (create-on-
    demand). Backup is NOT created for a fresh file."""
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_INITIAL)
    user_caps_path = tmp_path / "user_caps.yaml"  # does not exist
    report = _FakeReport(
        results=[
            _FakeProbeResult(
                name="thinking",
                suggested_patch=PATCH_CAPABILITIES_RULE,
                target_file="model-capabilities.yaml",
            )
        ]
    )

    result = apply_doctor_patches(
        report=report,
        config_path=config_path,
        write=True,
        user_capabilities_path=user_caps_path,
    )

    assert result.changes_applied == 1
    assert result.written is True
    assert user_caps_path.is_file()
    new_text = user_caps_path.read_text(encoding="utf-8")
    assert "match: 'claude-opus-4-8'" in new_text or "match: claude-opus-4-8" in new_text
    assert "thinking: true" in new_text
    # No backup created for a freshly-created file
    assert not (tmp_path / "user_caps.yaml.bak").exists()


@_requires_ruamel
def test_apply_aggregates_multiple_patches_one_diff_per_file(tmp_path: Path) -> None:
    """Two patches targeting providers.yaml should compose into one
    final diff (single load → merge merge → single dump)."""
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_INITIAL)
    report = _FakeReport(
        results=[
            _FakeProbeResult(
                name="tool_calls",
                suggested_patch=PATCH_TOOLS_TRUE,
                target_file="providers.yaml",
            ),
            _FakeProbeResult(
                name="num_ctx",
                suggested_patch=PATCH_NUM_CTX.replace("ollama-default", "local"),
                target_file="providers.yaml",
            ),
        ]
    )

    result = apply_doctor_patches(report=report, config_path=config_path, write=True)

    assert result.changes_applied == 2
    # Single target file → single diff entry
    assert len(result.target_paths) == 1
    new_text = config_path.read_text(encoding="utf-8")
    assert "tools: true" in new_text
    assert "num_ctx: 32768" in new_text


@_requires_ruamel
def test_apply_skips_results_without_patch(tmp_path: Path) -> None:
    """OK-verdict probes (no suggested_patch) are skipped silently."""
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_INITIAL)
    report = _FakeReport(
        results=[
            _FakeProbeResult(name="auth+basic-chat"),  # no patch
            _FakeProbeResult(
                name="tool_calls",
                suggested_patch=PATCH_TOOLS_TRUE,
                target_file="providers.yaml",
            ),
        ]
    )

    result = apply_doctor_patches(report=report, config_path=config_path, write=True)
    assert result.changes_applied == 1
    assert result.no_op_patches == 0


@_requires_ruamel
def test_apply_records_unknown_target_in_skipped(tmp_path: Path) -> None:
    """Future-proofing: an unknown target_file does not crash the apply
    pass; it surfaces in skipped_unknown_target so the CLI can warn."""
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_INITIAL)
    report = _FakeReport(
        results=[
            _FakeProbeResult(
                name="future",
                suggested_patch="rules: []\n",
                target_file="something-else.yaml",
            )
        ]
    )

    result = apply_doctor_patches(report=report, config_path=config_path, write=False)
    assert result.skipped_unknown_target == ["something-else.yaml"]
    assert result.changes_applied == 0
    assert result.written is False


# ======================================================================
# H-9 regression: --apply must gate the write on "did a merge change a
# value?", not on "does the re-dump differ from the bytes on disk".
#
# ruamel does not reproduce every input style byte-for-byte: it folds
# quoted scalars past its default 80-column width, and it re-emits an
# explicit ``key: null`` as an empty scalar. Both deltas are invisible
# to the patch bookkeeping, so a run that correctly reported
# "0 changes applied" still rewrote the file. The damage surfaces one
# run later: the next real ``--apply`` copies the already-reformatted
# file into ``.bak``, and the operator's pristine original no longer
# exists anywhere. The CLI compounded it by returning early on
# ``is_no_op`` — printing neither a diff nor a ``Backup:`` line — so the
# write happened in complete silence.
#
# ``width`` is set on both the load and the dump side as defense in
# depth, but it is NOT the fix: the null case is unaffected by width,
# which is exactly why ``test_no_op_run_does_not_rewrite_file_with_
# explicit_null`` exists as a separate detector.
# ======================================================================


# A quoted value comfortably past ruamel's default 80-column fold point.
# It has to contain spaces: YAML folds only at a break opportunity, so an
# equally long unbroken token (a model tag, a URL) is emitted intact at
# any width and would make this a fixture that tests nothing.
_LONG_QUOTED_VALUE = (
    "a deliberately long note with plenty of spaces so the emitter has "
    "somewhere to fold it"
)

PROVIDERS_YAML_LONG_LINES = textwrap.dedent(
    f"""\
    # providers.yaml — long-line fixture.
    allow_paid: false
    default_profile: default
    providers:
      - name: local
        kind: openai_compat
        base_url: http://localhost:11434/v1
        model: qwen2.5-coder:7b
        timeout_s: 120
        extra_body:
          note: "{_LONG_QUOTED_VALUE}"
        capabilities:
          tools: false
    profiles:
      - name: default
        providers:
          - local
    """
)

PROVIDERS_YAML_EXPLICIT_NULL = textwrap.dedent(
    """\
    # providers.yaml — explicit-null fixture (mirrors examples/providers.yaml).
    allow_paid: false
    default_profile: default
    providers:
      - name: local
        kind: openai_compat
        base_url: http://localhost:8080/v1
        model: qwen3.6
        api_key_env: null              # llama-server is unauthenticated
        timeout_s: 120
        capabilities:
          tools: false
    profiles:
      - name: default
        providers:
          - local
    """
)

# A patch that is already satisfied by both fixtures above → every merge
# reports changed=False, so nothing may be written.
PATCH_TOOLS_FALSE = textwrap.dedent(
    """\
    # providers.yaml — update the entry for 'local':
    providers:
      - name: local
        # ... existing fields ...
        capabilities:
          tools: false
    """
)


def _no_op_report() -> _FakeReport:
    return _FakeReport(
        results=[
            _FakeProbeResult(
                name="tool_calls",
                suggested_patch=PATCH_TOOLS_FALSE,
                target_file="providers.yaml",
            )
        ]
    )


@_requires_ruamel
def test_no_op_run_does_not_rewrite_file_with_long_lines(tmp_path: Path) -> None:
    """A no-op ``--apply`` leaves an >80-column file byte-identical."""
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_LONG_LINES)

    result = apply_doctor_patches(
        report=_no_op_report(), config_path=config_path, write=True
    )

    assert result.is_no_op is True
    assert result.changes_applied == 0
    assert result.written is False
    assert result.written_paths == []
    assert config_path.read_text(encoding="utf-8") == PROVIDERS_YAML_LONG_LINES


@_requires_ruamel
def test_no_op_run_does_not_rewrite_file_with_explicit_null(tmp_path: Path) -> None:
    """A no-op ``--apply`` leaves an ``api_key_env: null`` file untouched.

    This is the detector that the ``width`` setting alone does not
    satisfy: ruamel re-emits an explicit ``null`` as an empty scalar at
    any width, so only gating the write on the merge result keeps the
    file intact. ``examples/providers.yaml`` — the file the README tells
    users to copy — carries five such lines.
    """
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_EXPLICIT_NULL)

    result = apply_doctor_patches(
        report=_no_op_report(), config_path=config_path, write=True
    )

    assert result.written is False
    assert config_path.read_text(encoding="utf-8") == PROVIDERS_YAML_EXPLICIT_NULL
    # The cosmetic delta is still detected — just reported, never written.
    assert str(config_path) in result.reformat_only
    # ...and it must not pollute the diff an operator reviews.
    assert result.diffs[str(config_path)] == ""


@_requires_ruamel
def test_backup_is_not_created_on_no_op_run(tmp_path: Path) -> None:
    """No write → no ``.bak``.

    A ``.bak`` produced by a no-op run is worse than useless: it is a
    copy of a file that was about to be silently reformatted, and it
    overwrites any genuine backup from an earlier real apply.
    """
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_EXPLICIT_NULL)

    apply_doctor_patches(report=_no_op_report(), config_path=config_path, write=True)

    assert not config_path.with_suffix(".yaml.bak").exists()


@_requires_ruamel
def test_long_value_survives_apply_unwrapped(tmp_path: Path) -> None:
    """A real ``--apply`` must not fold untouched long values.

    ``width`` regression detector: at ruamel's default of 80 columns the
    quoted note is emitted across two lines, which is a diff the
    operator never asked for (and, for the ``.bak`` chain, one they can
    no longer undo).
    """
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_LONG_LINES)
    report = _FakeReport(
        results=[
            _FakeProbeResult(
                name="tool_calls",
                suggested_patch=PATCH_TOOLS_TRUE,
                target_file="providers.yaml",
            )
        ]
    )

    result = apply_doctor_patches(report=report, config_path=config_path, write=True)

    assert result.written is True
    new_text = config_path.read_text(encoding="utf-8")
    assert "tools: true" in new_text
    assert f'note: "{_LONG_QUOTED_VALUE}"' in new_text
    # The backup must hold the ORIGINAL bytes, not a reformatted copy.
    backup = config_path.with_suffix(".yaml.bak")
    assert backup.read_text(encoding="utf-8") == PROVIDERS_YAML_LONG_LINES


@_requires_ruamel
def test_apply_output_filters_does_not_drop_existing_chain(tmp_path: Path) -> None:
    """H-8 end-to-end: applying the doctor's own emitter output keeps the
    operator's existing filters.

    Uses the real emitter rather than a hand-written constant so an
    emitter regression fails here too — this is the seam where "patch
    lists only the additions" turns into "``--apply`` deletes filters".
    """
    from coderouter.doctor import (
        _patch_providers_yaml_output_filters,
        _union_output_filters,
    )

    existing = ["strip_stop_markers", "strip_tool_call_xml"]
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(
        config_path,
        textwrap.dedent(
            """\
            providers:
              - name: local
                kind: openai_compat
                base_url: http://localhost:8080/v1
                model: qwen3.6
                output_filters:
                  - strip_stop_markers
                  - strip_tool_call_xml
            profiles:
              - name: default
                providers:
                  - local
            """
        ),
    )
    patch = _patch_providers_yaml_output_filters(
        "local", _union_output_filters(existing, ["strip_thinking"])
    )
    report = _FakeReport(
        results=[
            _FakeProbeResult(
                name="reasoning-leak",
                suggested_patch=patch,
                target_file="providers.yaml",
            )
        ]
    )

    result = apply_doctor_patches(report=report, config_path=config_path, write=True)

    assert result.changes_applied == 1
    import yaml as _pyyaml

    doc = _pyyaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert doc["providers"][0]["output_filters"] == [
        "strip_stop_markers",
        "strip_tool_call_xml",
        "strip_thinking",
    ]


@_requires_ruamel
def test_cli_no_op_message_states_nothing_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ``--apply`` summary always says whether the disk was touched.

    The original bug was not only that a no-op run wrote the file, but
    that the CLI's ``is_no_op`` early return said nothing at all about
    writing — so the operator had no signal. Every exit path now emits
    the verdict line.
    """
    from coderouter.cli import _run_apply_or_dry_run

    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_EXPLICIT_NULL)

    exit_code = _run_apply_or_dry_run(
        report=_no_op_report(),
        config_path=config_path,
        write=True,
        base_exit=2,
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "already applied" in out
    assert "0 files written" in out
    assert config_path.read_text(encoding="utf-8") == PROVIDERS_YAML_EXPLICIT_NULL


# ----------------------------------------------------------------------
# v2.14.0 — a failed write must not leave a half-applied config
# ----------------------------------------------------------------------


@_requires_ruamel
def test_failed_second_write_rolls_back_the_first_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An apply spanning two files must be all-or-nothing.

    Before v2.14.0 the write loop had no error handling: if the second
    target failed (disk full, read-only mount, a race with an editor), the
    first one stayed rewritten and no command could name or undo it. Now
    the loop restores what it already wrote and raises, so the operator
    sees a clean failure instead of a config in an unknown state.
    """
    config_path = tmp_path / "providers.yaml"
    _write_providers_yaml(config_path, PROVIDERS_YAML_INITIAL)
    capabilities_path = tmp_path / "model-capabilities.yaml"

    # Point the capabilities target at tmp_path instead of ~/.coderouter-t.
    monkeypatch.setattr(
        doctor_apply,
        "_resolve_target_path",
        lambda *, target_file, config_path: (
            config_path if target_file == "providers.yaml" else capabilities_path
        ),
    )

    real_write_text = Path.write_text

    def exploding_write_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self == capabilities_path:
            raise OSError("disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", exploding_write_text)

    report = _FakeReport(
        results=[
            _FakeProbeResult(
                name="tool_calls",
                suggested_patch=PATCH_TOOLS_TRUE,
                target_file="providers.yaml",
            ),
            _FakeProbeResult(
                name="reasoning_leak",
                suggested_patch=PATCH_CAPABILITIES_RULE,
                target_file="model-capabilities.yaml",
            ),
        ]
    )

    with pytest.raises(DoctorApplyError) as excinfo:
        apply_doctor_patches(report=report, config_path=config_path, write=True)

    assert "Rolled back" in str(excinfo.value)
    # The first file is byte-identical to what it was before the apply.
    assert config_path.read_text(encoding="utf-8") == PROVIDERS_YAML_INITIAL
    # And the file that never got written was not left as an empty stub.
    assert not capabilities_path.exists()

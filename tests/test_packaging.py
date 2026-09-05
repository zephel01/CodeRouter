"""Packaging / release-plumbing sanity checks.

These tests don't exercise runtime behavior; they guard the *packaging
metadata* itself (PEP 561 marker, sdist allowlist, workflow YAML
syntax) so a regression is caught by `pytest` rather than discovered
after a `uv build` / PyPI upload.
"""

from __future__ import annotations

import ast
import inspect
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from coderouter.ingress import launcher_routes


def _function_source(path: Path, name: str) -> str:
    """Return a top-level function's source text without importing its module."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            segment = ast.get_source_segment(text, node)
            assert segment is not None, f"could not extract {name} from {path}"
            return segment
    raise AssertionError(f"{path} has no top-level function named {name!r}")


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def pyproject() -> dict:
    with open(PYPROJECT_PATH, "rb") as f:
        return tomllib.load(f)


def test_py_typed_marker_exists() -> None:
    """PEP 561 marker must exist so type checkers treat coderouter as typed."""
    assert (REPO_ROOT / "coderouter" / "py.typed").is_file()


def test_typing_classifier_matches_py_typed(pyproject: dict) -> None:
    """The 'Typing :: Typed' classifier and coderouter/py.typed must agree.

    Checks both directions: claiming the classifier without shipping the
    marker is a lie to type checkers; shipping the marker without the
    classifier means users can't discover typedness from PyPI metadata.
    """
    classifiers = pyproject["project"]["classifiers"]
    has_classifier = "Typing :: Typed" in classifiers
    has_marker = (REPO_ROOT / "coderouter" / "py.typed").is_file()
    assert has_classifier == has_marker, (
        f"Typing :: Typed classifier present={has_classifier} but "
        f"coderouter/py.typed present={has_marker} — these must match."
    )


def test_sdist_only_include_excludes_private_docs(pyproject: dict) -> None:
    """sdist only-include must not allow the whole docs/ tree in.

    docs/inside/ (private notes) and docs/articles/ (external article
    drafts) are declared "NEVER commit" in .gitignore. They happen to be
    git-untracked, so a fresh GitHub Actions checkout can never pick
    them up — but a maintainer's local `uv build` reads whatever is on
    disk, only-include included. A bare "docs" entry would sweep those
    private subtrees into a published sdist; the allowlist must instead
    name public subdirectories explicitly so docs/inside and
    docs/articles are excluded *structurally*, not by convention.
    """
    only_include = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"][
        "only-include"
    ]

    assert "docs" not in only_include, (
        "only-include must not contain a bare 'docs' entry — that would "
        "include docs/inside and docs/articles, which are declared "
        "private in .gitignore. List public docs/ subdirectories "
        "explicitly instead."
    )

    # Structural guarantee: no listed entry is (or is a parent of) the
    # private subtrees. An entry equal to "docs/inside" or "docs/articles"
    # (or anything that isn't more specific than those two paths) would
    # defeat the point of this test.
    private_subtrees = {"docs/inside", "docs/articles"}
    for entry in only_include:
        assert entry not in private_subtrees, (
            f"only-include lists {entry!r} directly — docs/inside and "
            "docs/articles must never be publishable."
        )


def test_only_include_public_docs_subdirs_exist_on_disk(pyproject: dict) -> None:
    """Every docs/* entry in only-include should point at something real.

    Guards against the allowlist rotting relative to docs/'s actual
    layout (e.g. a subdirectory gets renamed and only-include silently
    stops shipping it).
    """
    only_include = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"][
        "only-include"
    ]
    for entry in only_include:
        if entry.startswith("docs/"):
            assert (REPO_ROOT / entry).exists(), (
                f"only-include references {entry!r}, which does not exist"
            )


def test_wheel_packages_includes_coderouter(pyproject: dict) -> None:
    wheel_cfg = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "coderouter" in wheel_cfg.get("packages", [])


@pytest.mark.parametrize(
    "workflow_name",
    ["ci.yml", "release.yml"],
)
def test_workflow_yaml_is_well_formed(workflow_name: str) -> None:
    """GitHub Actions workflows must at least be syntactically valid YAML.

    Not a schema validator — just guards against a hand-edit breaking
    the YAML outright (bad indentation, unbalanced quotes, etc).
    """
    path = REPO_ROOT / ".github" / "workflows" / workflow_name
    assert path.is_file()
    with open(path) as f:
        doc = yaml.safe_load(f)
    assert isinstance(doc, dict)
    assert "jobs" in doc


def test_release_workflow_actions_are_sha_pinned() -> None:
    """Actions used by privileged release.yml jobs must be pinned to a
    commit SHA (not a mutable tag) to resist supply-chain tag-swap
    attacks — this workflow carries both `id-token: write` (PyPI
    Trusted Publishing) and `contents: write` (GitHub Release
    creation).
    """
    path = REPO_ROOT / ".github" / "workflows" / "release.yml"
    text = path.read_text()

    for match in re.finditer(r"^\s*uses:\s*([^\s#]+)", text, re.MULTILINE):
        ref = match.group(1)
        assert "@" in ref, f"action reference missing a pin: {ref!r}"
        _, _, pin = ref.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", pin), (
            f"release.yml action {ref!r} is not pinned to a 40-char commit "
            "SHA (found a mutable tag/branch instead)"
        )


def test_ci_workflow_test_matrix_is_documented() -> None:
    """The test matrix must cover ubuntu on both supported Pythons.

    macOS is deliberately absent right now — it was added on the v2.12.0
    branch, failed six launcher tests on a 5.0s harness budget that is
    1/60th of the shipped default, and was deferred rather than papered
    over with xfails. That decision has to stay legible, so if the runner
    list ever shrinks below ubuntu or the reasoning comment disappears,
    this fails.
    """
    path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    with open(path) as f:
        doc = yaml.safe_load(f)
    matrix = doc["jobs"]["test"]["strategy"]["matrix"]

    assert "ubuntu-latest" in matrix["os"]
    assert set(matrix["python-version"]) >= {"3.12", "3.13"}

    if "macos-latest" not in matrix["os"]:
        text = path.read_text(encoding="utf-8")
        assert "macOS is deliberately absent" in text, (
            "macos-latest is not in the matrix and ci.yml no longer explains "
            "why — either re-add the runner or restore the rationale comment"
        )


def test_ci_workflow_cve_audit_covers_all_extras() -> None:
    """`pip install coderouter-t[accuracy]` / `[repair]` are real
    install combinations users can pick — pip-audit's export step must
    not silently skip their dependencies (tokenizers, json-repair).
    """
    path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    export_lines = [line for line in path.read_text().splitlines() if "uv export" in line]
    assert export_lines, "expected a 'uv export' step in the cve-audit job"

    combined = "\n".join(export_lines)
    # Either the blanket flag, or every optional-dependency group named
    # explicitly — either satisfies "accuracy/repair extras are audited".
    assert "--all-extras" in combined or (
        "--extra accuracy" in combined and "--extra repair" in combined
    )


# ---------------------------------------------------------------------------
# Readiness probe must not depend on how `localhost` resolves
# ---------------------------------------------------------------------------


def test_backend_ready_probes_ipv4_literal_not_localhost() -> None:
    """The llama.cpp/vllm ``/health`` probe must target 127.0.0.1 literally.

    Regression guard for the macOS CI failure of 2026-08-04: the probe used
    ``http://localhost:<port>/health`` while llama-server listens on IPv4
    only (``--host 127.0.0.1`` by default). On a host where ``localhost``
    resolves to ``::1`` first — GitHub's macOS runners, and Macs generally —
    httpx raises ``Address family not supported`` and readiness never
    succeeds, so the spawn times out with ``status='loading'`` against a
    backend that is up and serving.

    Asserting on the source rather than the behaviour is deliberate: the
    failure only reproduces on a host whose resolver prefers IPv6, which is
    exactly the environment this suite cannot rely on having.
    """
    # Two independent copies of this function exist: the server's and the
    # Tk launcher's. Fixing one and not the other is exactly how this drifted
    # in the first place, so both are pinned here. launcher_gui is read as
    # source rather than imported — importing it needs tkinter, which the
    # ubuntu CI runner does not have.
    gui_src = _function_source(REPO_ROOT / "launcher_gui.py", "_backend_ready")

    for owner, src in (
        ("coderouter.ingress.launcher_routes", inspect.getsource(launcher_routes._backend_ready)),
        ("launcher_gui", gui_src),
    ):
        assert "http://127.0.0.1:{port}/health" in src, (
            f"{owner}._backend_ready must probe the 127.0.0.1 literal"
        )
        code = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        code = code.split('"""')[-1]
        assert "http://localhost" not in code, (
            f"{owner}._backend_ready still builds a localhost URL — that "
            "breaks on hosts with no IPv6 stack against IPv4-only backends"
        )

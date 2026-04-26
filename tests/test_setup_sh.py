"""Smoke tests for v1.7-B (#4) ``setup.sh`` onboarding wizard.

Scope:
    - ``--help`` exits 0 and prints usage.
    - RAM → model recommendation table boundaries (1.5b / 7b / 14b /
      below-threshold cloud-only hint).
    - Generated providers.yaml is loadable by pyyaml AND validates
      against the live ``CodeRouterConfig`` schema (so any drift
      between the script's template and the Pydantic schema fails
      fast here, not in production).
    - Idempotency contract: existing config + no --force writes
      ``providers.yaml.new`` sidecar; existing config + --force
      preserves ``.bak`` and overwrites the original.
    - ``--no-pull`` + missing ``ollama`` binary does not block (the
      wizard's promise is "I'll generate YAML even when ollama isn't
      ready yet").

The script is invoked via ``subprocess.run`` rather than imported
because it is bash. The tests stage everything in tmp dirs so the
real ``~/.coderouter`` is never touched.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from coderouter.config.schemas import CodeRouterConfig

# ----------------------------------------------------------------------
# Fixtures + helpers
# ----------------------------------------------------------------------

# Resolve setup.sh relative to the repo root (tests/__file__ is two
# levels deep). Resolved at module load so test failures point at the
# absolute path the wizard ran from.
SETUP_SH = Path(__file__).resolve().parent.parent / "setup.sh"


@pytest.fixture(scope="module", autouse=True)
def _verify_setup_sh_present() -> None:
    """Sanity guard — without setup.sh we can't run ANY of these tests."""
    assert SETUP_SH.is_file(), f"setup.sh not found at {SETUP_SH}"
    assert SETUP_SH.stat().st_mode & 0o100, (
        f"setup.sh at {SETUP_SH} is not executable"
    )


def _run_setup(*args: str, env_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run setup.sh with the given args, capturing stdout+stderr.

    ``env_path`` is exposed via PATH; tests use this to (a) hide
    ``ollama`` so the wizard exercises its absent-binary branch and
    (b) inject a fake ``ollama`` script for the pull path.
    """
    env: dict[str, str] = {}
    if env_path is not None:
        env["PATH"] = str(env_path)
    else:
        # Strip ollama from PATH so tests don't accidentally pull a
        # real model on a developer's laptop.
        env["PATH"] = "/usr/bin:/bin"
    # HOME must be set since setup.sh uses ${HOME} in the default config path
    env["HOME"] = str(Path.home())
    return subprocess.run(
        ["bash", str(SETUP_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


# ======================================================================
# --help
# ======================================================================


def test_help_exits_zero_and_lists_flags() -> None:
    """All documented flags must appear in --help output. Lock this so
    a flag rename doesn't quietly break docs that reference --no-pull
    etc."""
    result = _run_setup("--help")
    assert result.returncode == 0, result.stderr
    text = result.stdout
    for flag in (
        "--config-path",
        "--ram-gb",
        "--non-interactive",
        "--no-pull",
        "--dry-run",
        "--force",
    ):
        assert flag in text, f"--help missing flag {flag}"


def test_unknown_flag_fails_with_pointer_to_help() -> None:
    """A typo'd flag must exit non-zero and tell the user where to look."""
    result = _run_setup("--bogus")
    assert result.returncode != 0
    assert "--help" in result.stderr or "--help" in result.stdout


# ======================================================================
# RAM → model recommendation table
# ======================================================================


@pytest.mark.parametrize(
    "ram_gb,expected_model,expected_timeout,expected_tools",
    [
        # v1.8.3 (Qwen3.6 + Ollama 詰み確定): 48 GB+ tier も gemma4:26b に
        # 統一。Qwen3.6:35b-a3b を狙う場合は llama.cpp 直叩き経路を推奨
        # (`docs/llamacpp-direct.md`) で、setup.sh の Ollama 一本道では扱わない。
        # MoE 25.2B/3.8B-active、note "日常の王者"、vision 対応.
        (64, "gemma4:26b", "timeout_s: 180", "tools: true"),
        (48, "gemma4:26b", "timeout_s: 180", "tools: true"),
        # 24-47 GB → gemma4:26b (18 GB GGUF + 6+ GB ヘッドルーム)
        (32, "gemma4:26b", "timeout_s: 180", "tools: true"),
        (24, "gemma4:26b", "timeout_s: 180", "tools: true"),
        # 16-23 GB → qwen2.5-coder:14b (~9 GB GGUF + 7 GB ヘッドルーム)
        # laptop でも他アプリと並走可、Claude Code 用に枯れた選択.
        (20, "qwen2.5-coder:14b", "timeout_s: 300", "tools: true"),
        (16, "qwen2.5-coder:14b", "timeout_s: 300", "tools: true"),
        # 10-15 GB → qwen2.5-coder:7b (~5 GB GGUF + 5 GB ヘッドルーム)
        # Claude Code の sweet spot.
        (12, "qwen2.5-coder:7b", "timeout_s: 120", "tools: true"),
        (10, "qwen2.5-coder:7b", "timeout_s: 120", "tools: true"),
        # 4-9 GB → qwen2.5-coder:1.5b, 60s timeout, tools=false (1.5b は
        # reliable tool-calling threshold 以下).
        (8, "qwen2.5-coder:1.5b", "timeout_s: 60", "tools: false"),
        (4, "qwen2.5-coder:1.5b", "timeout_s: 60", "tools: false"),
    ],
)
def test_ram_recommends_expected_model(
    tmp_path: Path,
    ram_gb: int,
    expected_model: str,
    expected_timeout: str,
    expected_tools: str,
) -> None:
    """Wire RAM size → model + matching timeout + tools flag in the YAML.

    v1.8.0 で推奨モデルを qwen2.5-coder ベースから qwen3.6 / gemma4
    ベースに切り替え。さらに「先頭は安全側に倒し、後で上げる」運用
    のために各 tier に 8-10 GB のヘッドルームを残すよう調整。
    examples/providers.yaml の primary local stanza との整合性を保つ。
    """
    cfg = tmp_path / "providers.yaml"
    result = _run_setup(
        "--ram-gb", str(ram_gb),
        "--no-pull",
        "--config-path", str(cfg),
    )
    assert result.returncode == 0, f"setup.sh failed:\n{result.stderr}"
    text = cfg.read_text(encoding="utf-8")
    assert f"model: {expected_model}" in text, text
    assert expected_timeout in text, text
    assert expected_tools in text, text


def test_ram_below_threshold_bails_with_cloud_only_hint(tmp_path: Path) -> None:
    """RAM <4 GB → exit non-zero + cloud-only message, no YAML written.

    v1.8.0 で qwen2.5-coder:1.5b を 4 GB tier に降格したのに合わせ、
    bail threshold は 6 GB → 4 GB に変更。
    """
    cfg = tmp_path / "providers.yaml"
    result = _run_setup(
        "--ram-gb", "2",
        "--no-pull",
        "--config-path", str(cfg),
    )
    assert result.returncode != 0
    assert "below the local-Ollama threshold" in result.stderr
    assert "providers.nvidia-nim.yaml" in result.stderr
    assert not cfg.exists(), "no YAML should be written when bailing"


# ======================================================================
# Generated YAML — loadable + schema-valid
# ======================================================================


def test_generated_yaml_loads_via_pyyaml(tmp_path: Path) -> None:
    """Defensive: wizard output must be syntactically valid YAML."""
    cfg = tmp_path / "providers.yaml"
    result = _run_setup("--ram-gb", "16", "--no-pull", "--config-path", str(cfg))
    assert result.returncode == 0
    parsed = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    assert "providers" in parsed
    assert "profiles" in parsed


def test_generated_yaml_validates_against_pydantic_schema(tmp_path: Path) -> None:
    """The generated file must round-trip through CodeRouterConfig.

    This is the "wizard template stays in sync with the live schema"
    invariant — if a future ProviderConfig validator rejects something
    the wizard emits (e.g. an unknown filter name), this test fails
    before the wizard ships, not on the user's first ``coderouter
    serve``.
    """
    cfg = tmp_path / "providers.yaml"
    result = _run_setup("--ram-gb", "16", "--no-pull", "--config-path", str(cfg))
    assert result.returncode == 0
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    config = CodeRouterConfig.model_validate(raw)
    # And spot-check the wired values (v1.8.0 ヘッドルーム重視:
    # 16 GB → qwen2.5-coder:14b、~9 GB GGUF + 7 GB ヘッドルームで
    # laptop でも他アプリと並走可)
    assert config.default_profile == "default"
    assert len(config.providers) == 1
    provider = config.providers[0]
    assert provider.name == "local"
    assert provider.kind == "openai_compat"
    assert provider.model == "qwen2.5-coder:14b"
    # Qwen2.5-Coder 系は <think> リークするので output_filters で scrub する
    # (setup.sh の emit_providers_yaml の qwen* ブランチ参照)
    assert provider.output_filters == ["strip_thinking", "strip_stop_markers"]


# ======================================================================
# Idempotency / existing-file handling
# ======================================================================


def test_existing_config_without_force_writes_new_sidecar(tmp_path: Path) -> None:
    """Pre-existing config + no --force → writes ``providers.yaml.new``,
    leaves the original untouched. The "never silently destroy a
    hand-edited config" contract."""
    cfg = tmp_path / "providers.yaml"
    cfg.write_text("# user-curated content\nallow_paid: true\n", encoding="utf-8")
    result = _run_setup(
        "--ram-gb", "16",
        "--no-pull",
        "--config-path", str(cfg),
    )
    assert result.returncode == 0
    # Original untouched
    assert cfg.read_text(encoding="utf-8") == "# user-curated content\nallow_paid: true\n"
    # Sidecar carries the wizard's output (v1.8.0 ヘッドルーム重視:
    # 16 GB → qwen2.5-coder:14b)
    sidecar = tmp_path / "providers.yaml.new"
    assert sidecar.is_file()
    assert "qwen2.5-coder:14b" in sidecar.read_text(encoding="utf-8")


def test_existing_config_with_force_writes_bak_and_overwrites(tmp_path: Path) -> None:
    """--force overwrites and preserves ``.bak`` next to the original."""
    cfg = tmp_path / "providers.yaml"
    original_body = "# user-curated content\nallow_paid: true\n"
    cfg.write_text(original_body, encoding="utf-8")
    result = _run_setup(
        "--ram-gb", "16",
        "--no-pull",
        "--force",
        "--config-path", str(cfg),
    )
    assert result.returncode == 0
    # Original is overwritten (v1.8.0 ヘッドルーム重視:
    # 16 GB → qwen2.5-coder:14b)
    new_text = cfg.read_text(encoding="utf-8")
    assert "qwen2.5-coder:14b" in new_text
    # .bak preserves the prior content verbatim
    bak = tmp_path / "providers.yaml.bak"
    assert bak.is_file()
    assert bak.read_text(encoding="utf-8") == original_body


# ======================================================================
# --no-pull + missing ollama
# ======================================================================


def test_no_pull_with_missing_ollama_does_not_block(tmp_path: Path) -> None:
    """--no-pull means the user is staging YAML without intending to
    invoke ollama right now. A missing ``ollama`` binary must not
    abort the wizard — the YAML still gets written."""
    cfg = tmp_path / "providers.yaml"
    # _run_setup already strips ollama from PATH by default
    result = _run_setup(
        "--ram-gb", "16",
        "--no-pull",
        "--config-path", str(cfg),
    )
    assert result.returncode == 0, result.stderr
    assert cfg.is_file()


def test_no_no_pull_with_missing_ollama_aborts_with_install_hint(tmp_path: Path) -> None:
    """Without --no-pull and no ollama: bail with the install hint
    (so the user knows what to do, doesn't get stuck on a YAML they
    can't actually use)."""
    cfg = tmp_path / "providers.yaml"
    result = _run_setup(
        "--ram-gb", "16",
        "--config-path", str(cfg),
    )
    assert result.returncode != 0
    combined = result.stderr + result.stdout
    assert "ollama is not installed" in combined
    # Install hint must mention either the macOS or Linux path
    assert ("brew install ollama" in combined or "ollama.com/install.sh" in combined)
    # YAML must NOT be written when we bail before reaching the write step
    assert not cfg.exists()


# ======================================================================
# --dry-run
# ======================================================================


def test_dry_run_does_not_write_anything(tmp_path: Path) -> None:
    """--dry-run prints YAML to stdout but does not touch disk."""
    cfg = tmp_path / "providers.yaml"
    result = _run_setup(
        "--ram-gb", "16",
        "--no-pull",
        "--dry-run",
        "--config-path", str(cfg),
    )
    assert result.returncode == 0, result.stderr
    # v1.8.0 ヘッドルーム重視: 16 GB → qwen2.5-coder:14b
    assert "qwen2.5-coder:14b" in result.stdout
    assert "[dry-run]" in result.stdout
    assert not cfg.exists(), "dry-run must not write the config file"
    assert not (tmp_path / "providers.yaml.new").exists()


# ======================================================================
# --config-path with non-existent parent directory
# ======================================================================


def test_creates_parent_directory_for_config_path(tmp_path: Path) -> None:
    """The wizard targets ~/.coderouter/providers.yaml by default, which
    likely doesn't exist on a fresh machine. mkdir -p must create the
    parent silently."""
    cfg = tmp_path / "deep" / "nested" / "providers.yaml"
    result = _run_setup(
        "--ram-gb", "16",
        "--no-pull",
        "--config-path", str(cfg),
    )
    assert result.returncode == 0, result.stderr
    assert cfg.is_file()


# ======================================================================
# Optional: shellcheck if available — static analysis for bash bugs
# ======================================================================


def test_shellcheck_clean() -> None:
    """When shellcheck is on PATH, run it against setup.sh.

    Skipped (not failed) when shellcheck isn't installed so contributors
    without it don't see a red test, but CI environments that DO have
    shellcheck (most Linux runners) get the static check for free.
    """
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    result = subprocess.run(
        ["shellcheck", str(SETUP_SH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"shellcheck found issues in setup.sh:\n{result.stdout}\n{result.stderr}"
    )

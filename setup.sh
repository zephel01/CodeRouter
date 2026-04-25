#!/usr/bin/env bash
# ============================================================================
# CodeRouter — onboarding wizard (v1.7-B #4)
#
# What this script does
# ---------------------
# Walks a fresh user from "I just installed coderouter-cli" to a working
# providers.yaml in ~5 lines of output. Concretely:
#
#   1. Detects the OS (macOS / Linux) and total RAM.
#   2. Suggests a local Ollama model that fits comfortably in that RAM
#      budget (table at L_MODEL_TABLE below — kept small on purpose so
#      a beginner sees one good choice rather than a menu).
#   3. Checks whether `ollama` is installed; if not, prints the install
#      hint from ollama.com and bails out so the user can install + retry.
#   4. Runs `ollama pull <model>` (skippable with --no-pull).
#   5. Writes ~/.coderouter/providers.yaml from a minimal embedded
#      template — single local provider, single profile — that
#      `coderouter serve` can boot from immediately.
#   6. Prints the next steps (`coderouter doctor --check-model local`,
#      then `coderouter serve`).
#
# What this script deliberately does NOT do
# -----------------------------------------
# - It does NOT install anything for the user beyond the Ollama model.
#   Python / uv / coderouter-cli is the user's job (the README's
#   `uvx coderouter-cli serve` already covers that path).
# - It does NOT touch any existing ~/.coderouter/providers.yaml. If a
#   config is already there, the wizard writes ./providers.yaml.new
#   instead and tells the user to diff/merge — destroying a hand-edited
#   config silently would be a much worse bug than a one-line warning.
# - It does NOT wire up cloud fallbacks (OpenRouter free, NVIDIA NIM,
#   paid providers). Those are documented in docs/free-tier-guide.md
#   and examples/providers.nvidia-nim.yaml; the wizard intentionally
#   produces a minimal config that the user can extend by copying from
#   examples/.
#
# Dependency budget (plan.md §11.B.4 #4)
# -------------------------------------
# Pure bash + standard POSIX tools (sysctl on macOS, awk on Linux,
# mkdir, cat, printf). Does NOT shell out to Python — every heredoc
# templated above is hand-written so this script can run on a brand-new
# machine where coderouter-cli has been `uvx`-installed but the
# Python venv is opaque to a non-Python user. The follow-up
# `coderouter doctor --check-model local` is the natural next step
# but is left for the user to run so the wizard's exit code reflects
# its own work, not a downstream probe failure.
# ============================================================================

set -euo pipefail

# ----------------------------------------------------------------------------
# Configurable defaults — flags can override at the command line
# ----------------------------------------------------------------------------

CONFIG_PATH_DEFAULT="${HOME}/.coderouter/providers.yaml"
CONFIG_PATH=""           # set by --config-path or to default after parse
RAM_GB_OVERRIDE=""       # set by --ram-gb (testing / VMs with weird sysctl)
INTERACTIVE="auto"       # "auto" detects tty; --non-interactive forces "no"
DO_PULL="yes"            # --no-pull flips to "no"
DRY_RUN="no"             # --dry-run flips to "yes"
FORCE_OVERWRITE="no"     # --force flips to "yes"

# ----------------------------------------------------------------------------
# Pretty-print helpers — no color in non-tty so logs are clean.
# ----------------------------------------------------------------------------

if [ -t 1 ] && [ -t 2 ]; then
    C_BOLD="$(printf '\033[1m')"
    C_DIM="$(printf '\033[2m')"
    C_RESET="$(printf '\033[0m')"
    C_GREEN="$(printf '\033[32m')"
    C_YELLOW="$(printf '\033[33m')"
    C_RED="$(printf '\033[31m')"
else
    C_BOLD=""
    C_DIM=""
    C_RESET=""
    C_GREEN=""
    C_YELLOW=""
    C_RED=""
fi

step() {
    printf '%s==>%s %s%s%s\n' "$C_GREEN" "$C_RESET" "$C_BOLD" "$1" "$C_RESET"
}

note() {
    printf '    %s%s%s\n' "$C_DIM" "$1" "$C_RESET"
}

warn() {
    printf '%swarning:%s %s\n' "$C_YELLOW" "$C_RESET" "$1" >&2
}

fatal() {
    printf '%serror:%s %s\n' "$C_RED" "$C_RESET" "$1" >&2
    exit 1
}

# ----------------------------------------------------------------------------
# usage / arg parsing
# ----------------------------------------------------------------------------

usage() {
    cat <<EOF
CodeRouter onboarding wizard.

Usage: setup.sh [OPTIONS]

Options:
  --config-path PATH    Where to write providers.yaml.
                        Default: ${CONFIG_PATH_DEFAULT}
  --ram-gb N            Override the auto-detected total RAM (in GB).
                        Useful inside VMs with surprising sysctl values
                        and for testing.
  --non-interactive     Skip all prompts; accept all defaults.
  --no-pull             Skip the 'ollama pull' step (assume the model
                        is already present locally, or you'll pull later).
  --dry-run             Print what would be done without touching disk
                        or invoking ollama.
  --force               Overwrite an existing providers.yaml without
                        prompting (default: write providers.yaml.new
                        next to it instead).
  -h, --help            Show this message and exit.

Examples:
  # Typical first run (interactive, auto-detect RAM):
  ./setup.sh

  # CI / scripted run with explicit RAM:
  ./setup.sh --non-interactive --ram-gb 32 --no-pull

  # See the YAML before committing:
  ./setup.sh --dry-run
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --config-path)
            CONFIG_PATH="${2:-}"
            [ -n "$CONFIG_PATH" ] || fatal "--config-path requires a value"
            shift 2
            ;;
        --config-path=*)
            CONFIG_PATH="${1#--config-path=}"
            shift
            ;;
        --ram-gb)
            RAM_GB_OVERRIDE="${2:-}"
            [ -n "$RAM_GB_OVERRIDE" ] || fatal "--ram-gb requires a value"
            shift 2
            ;;
        --ram-gb=*)
            RAM_GB_OVERRIDE="${1#--ram-gb=}"
            shift
            ;;
        --non-interactive)
            INTERACTIVE="no"
            shift
            ;;
        --no-pull)
            DO_PULL="no"
            shift
            ;;
        --dry-run)
            DRY_RUN="yes"
            shift
            ;;
        --force)
            FORCE_OVERWRITE="yes"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fatal "unknown option: $1 (run with --help)"
            ;;
    esac
done

if [ -z "$CONFIG_PATH" ]; then
    CONFIG_PATH="$CONFIG_PATH_DEFAULT"
fi

if [ "$INTERACTIVE" = "auto" ]; then
    if [ -t 0 ] && [ -t 1 ]; then
        INTERACTIVE="yes"
    else
        INTERACTIVE="no"
    fi
fi

# ----------------------------------------------------------------------------
# OS + RAM detection
#
# macOS: hw.memsize from sysctl is total physical RAM in bytes.
# Linux: /proc/meminfo MemTotal is in KiB; multiply by 1024 for bytes.
# Other (BSD / Windows): not supported — the wizard bails with a clear
# error so the user can fall back to the manual examples/providers.yaml
# template instead of getting half-baked output.
# ----------------------------------------------------------------------------

detect_os() {
    case "$(uname -s)" in
        Darwin)  echo "macos" ;;
        Linux)   echo "linux" ;;
        *)       echo "unknown" ;;
    esac
}

detect_ram_gb() {
    # Returns total RAM in whole GB (rounded down). Caller may override
    # via --ram-gb to skip detection entirely.
    if [ -n "$RAM_GB_OVERRIDE" ]; then
        echo "$RAM_GB_OVERRIDE"
        return 0
    fi
    local os bytes
    os="$(detect_os)"
    case "$os" in
        macos)
            bytes="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
            ;;
        linux)
            # $2 is the MemTotal value in KiB — multiply for bytes.
            bytes="$(awk '/^MemTotal:/ {print $2 * 1024; exit}' /proc/meminfo 2>/dev/null || echo 0)"
            ;;
        *)
            bytes=0
            ;;
    esac
    if [ "$bytes" -le 0 ]; then
        echo 0
        return 0
    fi
    # Round down to whole GB. We avoid `bc` to keep the dependency budget
    # at zero — busybox-only Linux setups have awk and printf but not bc.
    echo $((bytes / 1024 / 1024 / 1024))
}

# ----------------------------------------------------------------------------
# RAM → recommended model mapping.
#
# Values verified against examples/providers.yaml comments (2026-04):
#   ≥ 24 GB → qwen2.5-coder:14b   (claude-code primary on M-series with 24+)
#   ≥ 12 GB → qwen2.5-coder:7b    (sweet spot for interactive coding)
#   ≥  6 GB → qwen2.5-coder:1.5b  (small footprint; tools are weak)
#   <  6 GB → unsupported          (the wizard prints a hint to use cloud
#                                   fallback only; no Ollama recommendation)
#
# We deliberately do NOT recommend Qwen-Coder-32B (would suggest 48 GB+),
# Qwen3-Coder, or DeepSeek variants from the wizard — they're better
# served by the user copying examples/ + reading docs/free-tier-guide.md.
# Keeping the wizard's table small means a beginner sees one obvious
# next step instead of a configuration matrix.
# ----------------------------------------------------------------------------

recommend_model() {
    local ram_gb="$1"
    if [ "$ram_gb" -ge 24 ]; then
        echo "qwen2.5-coder:14b"
    elif [ "$ram_gb" -ge 12 ]; then
        echo "qwen2.5-coder:7b"
    elif [ "$ram_gb" -ge 6 ]; then
        echo "qwen2.5-coder:1.5b"
    else
        echo ""  # unsupported
    fi
}

# Per-model timeout_s — bigger models prefill slower under Claude Code's
# 15-20K-token system prompt (see examples/providers.yaml comments).
# 7b ≈ 30 s prefill / 14b ≈ 100 s prefill on M-series; defaults give the
# new user the same headroom the curated examples do.
recommend_timeout_s() {
    case "$1" in
        *":1.5b") echo 60 ;;
        *":7b")   echo 120 ;;
        *":14b")  echo 300 ;;
        *)        echo 60 ;;
    esac
}

# Whether to enable tool_calls capability for this model.
# 1.5b is below the reliable tool-calling threshold per
# examples/providers.yaml; flip to false so Claude Code clients fall
# back to text completion instead of failing on bad tool_calls.
recommend_tools() {
    case "$1" in
        *":1.5b") echo "false" ;;
        *)        echo "true" ;;
    esac
}

# ----------------------------------------------------------------------------
# providers.yaml template.
#
# Single local Ollama provider + single 'default' profile is the
# minimum viable shape. The user can layer cloud fallback by copying
# stanzas from examples/providers.yaml or examples/providers.nvidia-nim.yaml.
# We embed the template inline (rather than copying examples/providers.yaml)
# so the wizard works the same when run via `curl | bash` for users who
# installed via uvx (they don't have the repo's examples/ directory).
# ----------------------------------------------------------------------------

emit_providers_yaml() {
    local model="$1"
    local timeout_s="$2"
    local tools="$3"
    local provider_name="local"

    # output_filters chain: strip_thinking is unconditional for Qwen2.5-Coder
    # because the family intermittently leaks <think> blocks under tool-heavy
    # prompts (examples/providers.yaml line 103 commentary).
    cat <<YAML
# ============================================================================
# CodeRouter providers.yaml — generated by setup.sh (v1.7-B)
#
# This is a minimal config: ONE local Ollama provider, ONE profile.
# Edit freely. To extend with cloud fallbacks (OpenRouter free, NVIDIA NIM,
# paid Anthropic / OpenAI), copy stanzas from:
#   examples/providers.yaml              (general)
#   examples/providers.nvidia-nim.yaml   (NIM 40 req/min free tier)
#   examples/providers.note-2026.yaml    (writing / free reasoning chain)
#
# Verify this config works against your local Ollama with:
#   coderouter doctor --check-model ${provider_name}
# ============================================================================

allow_paid: false
default_profile: default

providers:
  # Local Ollama — recommended by setup.sh based on your machine's RAM.
  # Re-run setup.sh with --ram-gb N to suggest a different model.
  - name: ${provider_name}
    kind: openai_compat
    base_url: http://localhost:11434/v1
    model: ${model}
    paid: false
    timeout_s: ${timeout_s}
    # Qwen2.5-Coder occasionally emits <think>...</think> in the content
    # channel under tool-heavy prompting; strip at the adapter boundary.
    output_filters: [strip_thinking]
    capabilities:
      chat: true
      streaming: true
      tools: ${tools}

profiles:
  - name: default
    providers:
      - ${provider_name}
YAML
}

# ----------------------------------------------------------------------------
# File-write helpers
#
# Idempotency contract:
#   - If the target file does not exist → write it.
#   - If it exists AND --force was passed → overwrite (preserving a .bak).
#   - If it exists AND --force was NOT passed → write to <path>.new
#     and tell the user to diff/merge. Never silently mutate a hand-edited
#     config — destroying that would be a much worse bug than printing
#     one extra line.
# ----------------------------------------------------------------------------

write_yaml_to_disk() {
    local target="$1"
    local content="$2"
    local target_dir
    target_dir="$(dirname "$target")"
    mkdir -p "$target_dir"

    if [ -f "$target" ] && [ "$FORCE_OVERWRITE" = "no" ]; then
        # Existing config — write to .new sidecar instead of clobbering.
        local sidecar="${target}.new"
        printf '%s' "$content" > "$sidecar"
        warn "${target} already exists; wrote ${sidecar} instead."
        note "diff/merge with: diff -u ${target} ${sidecar}"
        return 0
    fi

    if [ -f "$target" ] && [ "$FORCE_OVERWRITE" = "yes" ]; then
        # --force preserves a single .bak sibling so a botched overwrite
        # is one `mv` away from recovery (mirrors doctor --apply behavior).
        cp -p "$target" "${target}.bak"
        note "Backup of existing config: ${target}.bak"
    fi

    printf '%s' "$content" > "$target"
}

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

main() {
    step "CodeRouter onboarding wizard"
    note "Will write providers.yaml + (optionally) pull an Ollama model."

    # 1. Detect OS + RAM
    local os ram_gb
    os="$(detect_os)"
    if [ "$os" = "unknown" ]; then
        fatal "unsupported OS '$(uname -s)'. Only macOS and Linux are supported by setup.sh; other platforms can copy examples/providers.yaml manually."
    fi
    ram_gb="$(detect_ram_gb)"
    if [ "$ram_gb" -le 0 ]; then
        fatal "could not detect total RAM. Pass --ram-gb N to override (where N is your physical RAM in GB)."
    fi
    step "Detected: ${os}, ${ram_gb} GB RAM"

    # 2. Recommend a model
    local model timeout_s tools
    model="$(recommend_model "$ram_gb")"
    if [ -z "$model" ]; then
        warn "RAM (${ram_gb} GB) is below the local-Ollama threshold (6 GB)."
        warn "We recommend cloud-only operation: copy examples/providers.nvidia-nim.yaml"
        warn "(NVIDIA NIM 40 req/min free tier) instead. setup.sh stops here."
        exit 1
    fi
    timeout_s="$(recommend_timeout_s "$model")"
    tools="$(recommend_tools "$model")"
    step "Recommended local model: ${model}"
    note "(timeout_s=${timeout_s}, capabilities.tools=${tools})"
    note "Override with --ram-gb to pick a different size."

    # 3. ollama check + pull
    #
    # When --no-pull or --dry-run is in effect, the user has explicitly
    # opted out of running ollama pull, so a missing ollama binary is
    # not a blocker — they may legitimately be staging a YAML on a
    # build machine for deployment elsewhere. We still NOTE the
    # absence so they know the YAML expects ollama at the same URL.
    if [ "$DO_PULL" = "no" ]; then
        if ! command -v ollama >/dev/null 2>&1; then
            note "ollama is not installed; skipping pull (--no-pull). Install before 'coderouter serve'."
        else
            note "Skipping 'ollama pull' (--no-pull)."
        fi
    elif [ "$DRY_RUN" = "yes" ]; then
        if ! command -v ollama >/dev/null 2>&1; then
            note "[dry-run] ollama is not installed; would skip pull and warn the user."
        else
            step "[dry-run] would run: ollama pull ${model}"
        fi
    else
        # Real pull mode — ollama is required.
        if ! command -v ollama >/dev/null 2>&1; then
            warn "ollama is not installed."
            note "macOS:   brew install ollama  (or download from https://ollama.com/download)"
            note "Linux:   curl -fsSL https://ollama.com/install.sh | sh"
            note "Then re-run: $0"
            note "Or pass --no-pull to skip this step and pull later."
            exit 1
        fi
        step "Pulling ${model} via ollama (this may take a few minutes)..."
        ollama pull "$model" || fatal "ollama pull failed; see error above."
    fi

    # 5. Generate + write providers.yaml
    local yaml_content
    yaml_content="$(emit_providers_yaml "$model" "$timeout_s" "$tools")"

    if [ "$DRY_RUN" = "yes" ]; then
        step "[dry-run] providers.yaml that would be written to ${CONFIG_PATH}:"
        printf '%s\n' "$yaml_content"
        return 0
    fi

    step "Writing ${CONFIG_PATH}"
    write_yaml_to_disk "$CONFIG_PATH" "$yaml_content"

    # 6. Next steps
    step "Done. Next steps:"
    note "  coderouter doctor --check-model local      # verify the chain"
    note "  coderouter serve --port 8088               # start the router"
    note ""
    note "Need fallback / paid providers? See:"
    note "  docs/free-tier-guide.md"
    note "  examples/providers.nvidia-nim.yaml"
}

main "$@"

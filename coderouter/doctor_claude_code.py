"""``coderouter doctor --check-claude-code`` (v2.16.0).

Local-only checks for a Claude Code install that talks to CodeRouter:

1. **Version.** Known gateway-relevant regressions:

   - before 2.1.301: compaction could repeat when the response's ``model``
     name differs from the requested one (every translating gateway);
   - exactly 2.1.304: every request failed behind a gateway that rejects
     tool search (fixed in 2.1.305).

2. **Environment.** Variables that matter behind a non-Anthropic gateway
   (see ``coderouter vscode-init``):

   - ``ENABLE_TOOL_SEARCH`` unset → tool search is on by default since
     2.1.304; CodeRouter refuses it (``deferred_tools_action``) and Claude
     Code retries without it — works, but costs a round trip per session;
   - ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` unset on 2.1.285+ → Claude Code may
     assume a 1M window and compact far too late for a local model;
   - ``CLAUDE_CODE_AUTO_MODE_SERVER`` unset → auto mode asks the gateway
     for a server-side classifier a local backend does not have.

No network, no config file. Exit codes follow the other doctor checks:
0 = OK, 2 = needs attention, 1 = could not run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

Version = tuple[int, int, int]


@dataclass
class ClaudeCodeFinding:
    level: str  # "ok" | "warn" | "info"
    message: str


@dataclass
class ClaudeCodeReport:
    version: Version | None
    findings: list[ClaudeCodeFinding] = field(default_factory=list)
    error: str | None = None

    @property
    def exit_code(self) -> int:
        if self.error is not None:
            return 1
        return 2 if any(f.level == "warn" for f in self.findings) else 0


def parse_version(text: str) -> Version | None:
    m = _VERSION_RE.search(text)
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _fmt(v: Version) -> str:
    return ".".join(str(p) for p in v)


def check(version: Version | None, env: Mapping[str, str]) -> list[ClaudeCodeFinding]:
    """Pure check: findings for a Claude Code version and an environment."""
    out: list[ClaudeCodeFinding] = []
    if version is not None:
        if version < (2, 1, 301):
            out.append(
                ClaudeCodeFinding(
                    "warn",
                    f"Claude Code {_fmt(version)} is older than 2.1.301: compaction can "
                    "repeat behind a gateway that reports a different model name. "
                    "Update Claude Code.",
                )
            )
        elif version == (2, 1, 304):
            out.append(
                ClaudeCodeFinding(
                    "warn",
                    "Claude Code 2.1.304 fails every request behind a gateway that "
                    "rejects tool search (fixed in 2.1.305). Update Claude Code, or "
                    "set ENABLE_TOOL_SEARCH=false.",
                )
            )
        else:
            out.append(ClaudeCodeFinding("ok", f"Claude Code {_fmt(version)}"))

    if env.get("ENABLE_TOOL_SEARCH", "").lower() in ("false", "0"):
        out.append(ClaudeCodeFinding("ok", "ENABLE_TOOL_SEARCH=false"))
    else:
        out.append(
            ClaudeCodeFinding(
                "warn",
                "ENABLE_TOOL_SEARCH is not false: Claude Code 2.1.304+ starts each "
                "session with tool search, which only api.anthropic.com runs. "
                "CodeRouter refuses it and Claude Code retries without it. Set "
                "ENABLE_TOOL_SEARCH=false (coderouter vscode-init does) to skip "
                "that round trip.",
            )
        )

    if "CLAUDE_CODE_MAX_CONTEXT_TOKENS" in env:
        out.append(
            ClaudeCodeFinding(
                "ok", f"CLAUDE_CODE_MAX_CONTEXT_TOKENS={env['CLAUDE_CODE_MAX_CONTEXT_TOKENS']}"
            )
        )
    elif version is None or version >= (2, 1, 285):
        out.append(
            ClaudeCodeFinding(
                "warn",
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS is not set: since 2.1.285 Claude Code "
                "may assume a 1M window behind a gateway and compact long after a "
                "local model has overflowed. Set it to your smallest local context "
                "window (coderouter vscode-init --max-context-tokens N), and/or use "
                "context_budget_action: reject in the profile.",
            )
        )

    if env.get("CLAUDE_CODE_GATEWAY_HINT_HEADERS", "").lower() in ("1", "true"):
        out.append(ClaudeCodeFinding("ok", "CLAUDE_CODE_GATEWAY_HINT_HEADERS=1"))
    else:
        out.append(
            ClaudeCodeFinding(
                "info",
                "CLAUDE_CODE_GATEWAY_HINT_HEADERS is not set: auto_router rules "
                "on request_class / agent_type / compaction (v2.18.0) never "
                "match without it.",
            )
        )

    if env.get("CLAUDE_CODE_AUTO_MODE_SERVER") == "0":
        out.append(ClaudeCodeFinding("ok", "CLAUDE_CODE_AUTO_MODE_SERVER=0"))
    else:
        out.append(
            ClaudeCodeFinding(
                "warn",
                "CLAUDE_CODE_AUTO_MODE_SERVER is not 0: on a gateway, auto mode "
                "defaults to a server-side classifier that a local backend does not "
                "provide. Set CLAUDE_CODE_AUTO_MODE_SERVER=0.",
            )
        )
    return out


def _claude_version(run: Callable[[list[str]], str] | None = None) -> tuple[Version | None, str | None]:
    if run is None:
        exe = shutil.which("claude")
        if exe is None:
            return None, "`claude` not found on PATH (version checks skipped)"

        def run(cmd: list[str]) -> str:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
            return proc.stdout + proc.stderr

        cmd = [exe, "--version"]
    else:
        cmd = ["claude", "--version"]
    try:
        text = run(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run `claude --version`: {exc}"
    version = parse_version(text)
    if version is None:
        return None, f"could not parse `claude --version` output: {text.strip()[:80]!r}"
    return version, None


def run_check(
    env: Mapping[str, str] | None = None,
    run: Callable[[list[str]], str] | None = None,
) -> ClaudeCodeReport:
    version, note = _claude_version(run)
    findings = check(version, os.environ if env is None else env)
    if note:
        findings.insert(0, ClaudeCodeFinding("info", note))
    return ClaudeCodeReport(version=version, findings=findings)


def format_report(report: ClaudeCodeReport) -> str:
    lines = ["Claude Code (behind CodeRouter)"]
    for f in report.findings:
        mark = {"ok": "OK  ", "warn": "WARN", "info": "INFO"}.get(f.level, f.level.upper())
        lines.append(f"  [{mark}] {f.message}")
    return "\n".join(lines)

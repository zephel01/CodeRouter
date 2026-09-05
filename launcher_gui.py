#!/usr/bin/env python3
"""
CodeRouter Launcher GUI — tkinter 版
llama.cpp / vllm / mlx と CodeRouter をブラウザなしで起動・管理するデスクトップツール

使い方:
  python3 launcher_gui.py
  python3 launcher_gui.py --config ~/.coderouter-t/providers.yaml
  uv run python launcher_gui.py

追加パッケージ: 不要 (tkinter は Python 標準、yaml は CodeRouter の依存)

起動フロー:
  launcher_gui.py 起動
    → ① llama.cpp / vllm / mlx を選択モデルで起動 (ポート 8080)
    → ② CodeRouter を起動 (ポート 8088)  ← ★ このGUIから直接起動
    → Claude Code: ANTHROPIC_BASE_URL=http://localhost:8088 claude
"""

from __future__ import annotations

import argparse
import contextlib
import os
import platform
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

# ---------------------------------------------------------------------------
# YAML loading (optional — graceful fallback)
# ---------------------------------------------------------------------------
try:
    import yaml  # PyYAML (CodeRouter already depends on it)
    def _load_yaml(p: Path) -> dict:
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
except ImportError:
    yaml = None  # type: ignore
    def _load_yaml(p: Path) -> dict:  # type: ignore
        raise RuntimeError(
            "PyYAML が見つかりません。CodeRouter の venv から実行してください:\n"
            "  uv run python launcher_gui.py"
        )

# ---------------------------------------------------------------------------
# MTP / speculative-decoding resolution (optional — shared with the web UI)
# ---------------------------------------------------------------------------
# The GUI can run standalone without the coderouter package on sys.path; when
# the import fails we degrade gracefully and simply skip the MTP features.
try:
    from coderouter.launcher_speculative import resolve_speculative
    _HAS_SPECULATIVE = True
except ImportError:
    resolve_speculative = None  # type: ignore
    _HAS_SPECULATIVE = False

# ---------------------------------------------------------------------------
# Device selection + bench sweep core logic (optional — shared with the web UI)
# ---------------------------------------------------------------------------
# The pure logic (device detection / tensor-split suggestion / bench command
# expansion / results parsing / sweep data structures) lives in the frozen
# coderouter.launcher_devices module. As with the speculative import above, a
# standalone GUI run without the coderouter package on sys.path degrades
# gracefully: the device / sweep UI is simply not built (_HAS_DEVICES False).
try:
    from coderouter.launcher_devices import (
        DeviceProbe,
        DeviceSelection,
        LlamaDevice,
        SweepPlan,
        SweepState,
        SweepStep,
        backend_of,
        base_backend,
        build_auto_sweep_configs,
        build_sweep_steps,
        detect_llama_devices,
        is_port_free,
        is_valid_backend_name,
        is_variant,
        load_latest_results,
        render_bench_command,
        resolve_option_profiles,
        selectable_devices,
        suggest_tensor_split,
        variant_of,
    )
    _HAS_DEVICES = True
except ImportError:  # standalone GUI (coderouter package not importable)
    _HAS_DEVICES = False
    # Bind the names so annotations (stringized via ``from __future__ import
    # annotations``) and any guarded references never raise NameError / F821.
    DeviceProbe = None  # type: ignore
    DeviceSelection = None  # type: ignore
    LlamaDevice = None  # type: ignore
    SweepPlan = None  # type: ignore
    SweepState = None  # type: ignore
    SweepStep = None  # type: ignore
    backend_of = None  # type: ignore
    build_auto_sweep_configs = None  # type: ignore
    build_sweep_steps = None  # type: ignore
    detect_llama_devices = None  # type: ignore
    is_port_free = None  # type: ignore
    load_latest_results = None  # type: ignore
    render_bench_command = None  # type: ignore
    selectable_devices = None  # type: ignore
    suggest_tensor_split = None  # type: ignore

    # ★ バックエンド名の正規化だけは None にできない。_build_cmd /
    #   _backend_ready が _HAS_DEVICES に関係なく通る経路で使うため、
    #   standalone 実行でも動く同等の実装をここに置く (stdlib のみ)。
    #   本体は coderouter/launcher_devices.py §2.0 で、仕様はそちらが正。
    KNOWN_BASE_BACKENDS = ("llama.cpp", "vllm", "mlx")  # type: ignore[misc]

    def base_backend(name: str) -> str:  # type: ignore[misc]
        """``"llama.cpp-cuda"`` → ``"llama.cpp"`` (既知基底名との最長一致)。"""
        best = ""
        for _base in KNOWN_BASE_BACKENDS:
            if name == _base:
                return _base
            if name.startswith(_base + "-") and len(_base) > len(best):
                best = _base
        return best or name

    def variant_of(name: str) -> str | None:  # type: ignore[misc]
        """``"llama.cpp-cuda"`` → ``"cuda"``。基底名そのものなら None。"""
        _base = base_backend(name)
        if _base == name or not name.startswith(_base + "-"):
            return None
        return name[len(_base) + 1 :] or None

    def is_variant(name: str) -> bool:  # type: ignore[misc]
        return variant_of(name) is not None

    def is_valid_backend_name(name: str) -> bool:  # type: ignore[misc]
        if name in KNOWN_BASE_BACKENDS:
            return True
        _v = variant_of(name)
        return _v is not None and re.match(r"^[a-z0-9][a-z0-9._-]*$", _v) is not None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_MODEL_EXTS = {".gguf", ".ggml", ".safetensors", ".bin", ".pt", ".pth"}

_BACKEND_DEFAULTS = {
    "llama.cpp": "llama-server",
    "vllm": "python",
    "mlx": "python",          # mlx_lm.server (Apple Silicon 向け)
}

# CodeRouter のデフォルトポート (README / docs に揃えて 8088)
_CODEROUTER_PORT = 8088

# ── ログ蓄積の上限(ビーチボール対策) ──────────────────────────────────────
# 長時間稼働でログが無制限に溜まり、メインスレッドの処理が追いつかなくなって
# UI が固まる(くるくる)のを防ぐための上限値。
_MAX_LOG_LINES      = 5000   # mp.log_lines / _cr_log のメモリ上限(行数)
_MAX_TEXT_LINES     = 2000   # _log_text ウィジェットの表示行上限
_MAX_LINES_PER_TICK = 1500   # _poll 1回で処理する最大行数(残りは次回へ繰越)

# 自動検出 MTP が起動時にクラッシュした場合、この秒数以内の非ゼロ終了だけを
# 「起動時失敗」とみなし speculative フラグ無しで 1 回だけ再起動する。これより
# 長く稼働してから落ちたものは通常のクラッシュ扱いで再起動しない。
_MTP_FALLBACK_WINDOW_SECS = 180.0

# ---------------------------------------------------------------------------
# Readiness gating / auto-restart defaults
#
# Web 版 (coderouter/ingress/launcher_routes.py の _wait_ready_and_register /
# _attempt_restart) と挙動・既定値を揃える。根拠は
# coderouter/config/schemas.py の LauncherConfig 該当フィールドの docstring。
# GUI にはこの launcher: ブロックを pydantic 検証する仕組みが無いため、値は
# providers.yaml から緩く読み取り、型が壊れていれば既定値にフォールバックする
# (_load_config の他フィールドと同じ寛容な流儀)。
# ---------------------------------------------------------------------------
_DEFAULT_READINESS_TIMEOUT_S = 300.0
_DEFAULT_READINESS_POLL_INTERVAL_S = 2.0
# 個々のプローブ用ネットワークタイムアウト。readiness_timeout_s(既定300s)
# より十分短い固定値にして、1回のプローブが詰まっても loading→error の締切
# 判定が大きく遅れないようにする(既定の poll interval 2.0s よりは長い点に
# 注意 — プローブが3秒詰まれば次のポーリングはその分だけ遅れる)。
_READINESS_PROBE_TIMEOUT_S = 3.0
_DEFAULT_AUTO_RESTART = False
_DEFAULT_AUTO_RESTART_MAX_ATTEMPTS = 3
_DEFAULT_AUTO_RESTART_BACKOFF_S = 2.0
_DEFAULT_AUTO_RESTART_BACKOFF_MAX_S = 30.0

# ── Bench sweep defaults (launcher.bench: block) ──────────────────────────
# The bench body is the external ``llmbench`` CLI; the sweep drives it once per
# device configuration. {port}/{config}/{base_url}/{results_dir}/{runs} in the
# template are substituted by launcher_devices.render_bench_command.
_DEFAULT_BENCH_COMMAND_TEMPLATE = "llmbench run --model local-openai --runs {runs}"
_DEFAULT_BENCH_RUNS = 5

# v2.13.0 (security): implicit CWD providers.yaml discovery is opt-in,
# gated behind CODEROUTER_ALLOW_CWD_CONFIG — same vocabulary and rationale
# as coderouter.config.loader (a hostile providers.yaml dropped into a repo
# could otherwise steer launcher.backends[*].binary / bench.command_template
# simply because the GUI was launched from that directory). Evaluated per
# call (not at import) so Path.cwd() reflects the working dir at load time
# and the env toggle can flip within one process (e.g. tests).
_CWD_CONFIG_ENV = "CODEROUTER_ALLOW_CWD_CONFIG"
_CWD_CONFIG_TRUTHY = {"1", "true", "yes", "on"}


def _config_search_paths() -> list[Path]:
    paths: list[Path] = []
    if os.environ.get(_CWD_CONFIG_ENV, "").strip().lower() in _CWD_CONFIG_TRUTHY:
        paths.append(Path.cwd() / "providers.yaml")
    paths.append(Path.home() / ".coderouter-t" / "providers.yaml")
    return paths


@dataclass
class BackendConfig:
    binary: str | None = None


@dataclass
class OptionProfile:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class LauncherConfig:
    model_dirs: list[str] = field(default_factory=list)
    backends: dict[str, BackendConfig] = field(default_factory=dict)
    option_profiles: dict[str, list[OptionProfile]] = field(default_factory=dict)
    # --- readiness gating / auto-restart — see the block comment above
    #     _DEFAULT_READINESS_TIMEOUT_S for the source of truth (Web版と同一
    #     既定値)。 swap: は Web 版の SwapManager 専用機能であり、GUI 版では
    #     意図的に読み込まない。
    readiness_timeout_s: float = _DEFAULT_READINESS_TIMEOUT_S
    readiness_poll_interval_s: float = _DEFAULT_READINESS_POLL_INTERVAL_S
    auto_restart: bool = _DEFAULT_AUTO_RESTART
    auto_restart_max_attempts: int = _DEFAULT_AUTO_RESTART_MAX_ATTEMPTS
    auto_restart_backoff_s: float = _DEFAULT_AUTO_RESTART_BACKOFF_S
    auto_restart_backoff_max_s: float = _DEFAULT_AUTO_RESTART_BACKOFF_MAX_S
    # --- bench sweep defaults (launcher.bench: block) — read leniently in
    #     _load_config; absent keys fall back to these hard-coded defaults so a
    #     providers.yaml with no ``bench:`` block keeps working unchanged.
    bench_command_template: str = _DEFAULT_BENCH_COMMAND_TEMPLATE
    bench_runs: int = _DEFAULT_BENCH_RUNS
    bench_results_dir: str | None = None
    bench_readiness_timeout_s: float = _DEFAULT_READINESS_TIMEOUT_S


def _safe_number(raw: dict, key: str, default: float, cast: Callable[[Any], Any]) -> Any:
    """Read ``raw[key]`` through ``cast``, falling back to ``default``.

    The GUI has no pydantic validation for the ``launcher:`` block (unlike
    ``coderouter.config.schemas.LauncherConfig``), so a malformed value
    (wrong type, missing key) must never crash the whole GUI at startup —
    mirrors the already-lenient parsing of ``model_dirs`` / ``backends``
    just above.
    """
    if key not in raw:
        return default
    try:
        return cast(raw[key])
    except (TypeError, ValueError):
        return default


def _safe_bool(raw: dict, key: str, default: bool) -> bool:
    """Read a boolean out of ``raw[key]``, falling back to ``default``.

    Deliberately NOT ``bool(...)``: a quoted YAML string like
    ``auto_restart: "false"`` is a non-empty str, so ``bool("false")`` is
    True — silently enabling a side-effectful opt-in the user explicitly
    tried to turn off. Strings "true"/"false" (case-insensitive) are
    parsed; real bools pass through; anything else falls back.
    """
    if key not in raw:
        return default
    v = raw[key]
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s == "true":
            return True
        if s == "false":
            return False
    return default


def _load_config(path: str | None) -> LauncherConfig:
    cfg_path: Path | None = None
    if path:
        cfg_path = Path(path).expanduser()
    else:
        cfg_path = next((p for p in _config_search_paths() if p.is_file()), None)

    if cfg_path is None:
        return LauncherConfig()  # empty config — user can still set options manually

    try:
        raw = _load_yaml(cfg_path)
    except Exception as exc:
        messagebox.showwarning("設定ファイル読み込みエラー", str(exc))
        return LauncherConfig()

    launcher_raw = raw.get("launcher", {}) or {}

    # model_dirs
    model_dirs = [str(d) for d in launcher_raw.get("model_dirs", [])]

    # backends
    backends: dict[str, BackendConfig] = {}
    for bk, bv in (launcher_raw.get("backends", {}) or {}).items():
        if isinstance(bv, dict):
            backends[bk] = BackendConfig(binary=bv.get("binary"))

    # option_profiles
    option_profiles: dict[str, list[OptionProfile]] = {}
    for bk, profiles_raw in (launcher_raw.get("option_profiles", {}) or {}).items():
        profs: list[OptionProfile] = []
        for p in (profiles_raw or []):
            if isinstance(p, dict) and "name" in p:
                profs.append(OptionProfile(name=p["name"], args=p.get("args", {}) or {}))
        option_profiles[bk] = profs

    # readiness gating / auto-restart — same keys & defaults as the Web 版
    # ``launcher:`` block (coderouter/config/schemas.py LauncherConfig).
    # ``swap:`` is intentionally never read here (see LauncherConfig above).
    readiness_timeout_s = _safe_number(
        launcher_raw, "readiness_timeout_s", _DEFAULT_READINESS_TIMEOUT_S, float)
    readiness_poll_interval_s = _safe_number(
        launcher_raw, "readiness_poll_interval_s",
        _DEFAULT_READINESS_POLL_INTERVAL_S, float)
    auto_restart = _safe_bool(
        launcher_raw, "auto_restart", _DEFAULT_AUTO_RESTART)
    auto_restart_max_attempts = _safe_number(
        launcher_raw, "auto_restart_max_attempts",
        _DEFAULT_AUTO_RESTART_MAX_ATTEMPTS, int)
    auto_restart_backoff_s = _safe_number(
        launcher_raw, "auto_restart_backoff_s",
        _DEFAULT_AUTO_RESTART_BACKOFF_S, float)
    auto_restart_backoff_max_s = _safe_number(
        launcher_raw, "auto_restart_backoff_max_s",
        _DEFAULT_AUTO_RESTART_BACKOFF_MAX_S, float)
    if auto_restart_backoff_s > auto_restart_backoff_max_s:
        # Same fast-fail *intent* as schemas.py's
        # _check_auto_restart_backoff_ordered validator, but the GUI just
        # clamps instead of refusing to start — a malformed config must
        # never block the desktop tool from opening.
        auto_restart_backoff_max_s = auto_restart_backoff_s

    # bench sweep defaults — the ``launcher.bench:`` sub-block. Leniently read
    # (missing block / keys → hard-coded defaults) exactly like the readiness
    # fields above; the GUI never writes providers.yaml back.
    bench_raw = launcher_raw.get("bench", {}) or {}
    if not isinstance(bench_raw, dict):
        bench_raw = {}
    bench_template = bench_raw.get("command_template")
    if not isinstance(bench_template, str) or not bench_template.strip():
        bench_template = _DEFAULT_BENCH_COMMAND_TEMPLATE
    bench_runs = _safe_number(bench_raw, "runs", _DEFAULT_BENCH_RUNS, int)
    bench_results_dir = bench_raw.get("results_dir")
    if bench_results_dir is not None and not isinstance(bench_results_dir, str):
        bench_results_dir = None
    bench_readiness_timeout_s = _safe_number(
        bench_raw, "readiness_timeout_s", _DEFAULT_READINESS_TIMEOUT_S, float)

    return LauncherConfig(
        model_dirs=model_dirs,
        backends=backends,
        option_profiles=option_profiles,
        readiness_timeout_s=readiness_timeout_s,
        readiness_poll_interval_s=readiness_poll_interval_s,
        auto_restart=auto_restart,
        auto_restart_max_attempts=auto_restart_max_attempts,
        auto_restart_backoff_s=auto_restart_backoff_s,
        auto_restart_backoff_max_s=auto_restart_backoff_max_s,
        bench_command_template=bench_template,
        bench_runs=bench_runs,
        bench_results_dir=bench_results_dir,
        bench_readiness_timeout_s=bench_readiness_timeout_s,
    )


def _resolve_binary(backend: str, cfg: LauncherConfig) -> str:
    """バックエンド(バリアント可)の実行ファイルを解決する。

    ``cfg.backends`` はバリアント名 (``llama.cpp-cuda``) もキーに取る。既定名の
    フォールバックは基底名で引く。実運用ではバリアントは ``binary`` 必須
    (config ロード時に検証) なのでフォールバックには入らない。
    """
    bc = cfg.backends.get(backend)
    raw = (bc.binary if bc else None) or _BACKEND_DEFAULTS.get(
        base_backend(backend), backend
    )
    return str(Path(raw).expanduser())


def _check_binary(binary: str) -> bool:
    expanded = str(Path(binary).expanduser())
    return Path(expanded).is_file() or shutil.which(expanded) is not None


def _backend_names(cfg: LauncherConfig) -> list[str]:
    """バックエンドセレクトに出す名前の一覧(順序が並び順)。

    基底 3 つを常に先頭に、そのあとに ``launcher.backends`` に書かれた
    バリアント (``llama.cpp-cuda`` 等) を記述順で並べる。Web 版の
    ``launcher_routes._backend_names`` と同じ規則。バリアントを書かない
    利用者には従来と同じ 3 要素が返るので選択肢は完全に不変。
    """
    names = list(_BACKEND_DEFAULTS)
    for name in cfg.backends:
        if name not in names:
            names.append(name)
    return names


def _profiles_for(cfg: LauncherConfig, backend: str) -> list[OptionProfile]:
    """バックエンド(バリアント可)に適用する option_profiles を解決する。

    基底名のプロファイルを継承し、バリアント固有のものを後ろに連結する
    (同名は同じ位置で差し替え)。共有ロジックが import できない standalone
    実行では基底名の分だけを返す。
    """
    if _HAS_DEVICES:
        return resolve_option_profiles(cfg.option_profiles, backend)
    return list(cfg.option_profiles.get(base_backend(backend), []))


def _scan_models(model_dirs: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in model_dirs:
        base = Path(d).expanduser()
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.suffix.lower() not in _MODEL_EXTS:
                continue
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            size_gb = p.stat().st_size / (1024 ** 3)
            results.append({
                "path": str(p),
                "name": p.name,
                "dir": str(p.parent),
                "size_gb": round(size_gb, 2),
            })
    return results


def _build_cmd(backend: str, model_path: str, port: int,
               profile_args: dict[str, Any], extra_args: str,
               binary: str,
               spec_tokens: list[str] | None = None,
               device_args: list[str] | None = None) -> list[str]:
    """Assemble the backend launch command.

    ``spec_tokens`` are pre-resolved MTP / speculative-decoding flags (from
    :func:`coderouter.launcher_speculative.resolve_speculative`). For
    llama.cpp they are inserted right after the port args, before the profile
    / extra args.

    ``device_args`` are pre-rendered llama.cpp device-selection flags (from
    :meth:`coderouter.launcher_devices.DeviceSelection.to_cli_args` —
    ``--device CUDA0,CUDA1`` / ``--tensor-split 0.57,0.43``). They are a
    trailing keyword argument defaulting to ``None`` so every existing
    positional/keyword call site (tests included) is unaffected, and when no
    device is selected the argv is byte-for-byte identical to before — the
    absolute backward-compatibility requirement. Only llama.cpp consumes them
    (vllm / mlx ignore device_args entirely).
    """
    # argv の形はバリアントによらず基底バックエンドで決まる。
    base = base_backend(backend)
    if base == "llama.cpp":
        cmd = [binary, "-m", model_path, "--port", str(port)]
        if device_args:
            cmd.extend(device_args)
        if spec_tokens:
            cmd.extend(spec_tokens)
    elif base == "vllm":
        cmd = [binary, "-m", "vllm.entrypoints.openai.api_server",
               "--model", model_path, "--port", str(port)]
    elif base == "mlx":
        cmd = [binary, "-m", "mlx_lm.server",
               "--model", model_path, "--port", str(port)]
    else:
        raise ValueError(f"Unknown backend: {backend!r}")

    for flag, val in profile_args.items():
        if isinstance(val, bool):
            if val:
                cmd.append(str(flag))
        else:
            cmd.extend([str(flag), str(val)])

    if extra_args.strip():
        cmd.extend(shlex.split(extra_args))

    return cmd


# ---------------------------------------------------------------------------
# Device selection helpers (pure — factored out of the Tk UI so they can be
# unit-tested without a live display, mirroring the readiness/auto-restart
# split above). Only meaningful when _HAS_DEVICES (coderouter importable).
# ---------------------------------------------------------------------------


def _selection_from_inputs(
    checked_ids: list[str],
    fallback_raw: str,
    tsplit_raw: str,
) -> DeviceSelection:
    """Build a :class:`DeviceSelection` from raw UI inputs.

    * ``checked_ids`` — device ids ticked in the detected-device checkboxes.
    * ``fallback_raw`` — comma-separated manual entry, used ONLY when nothing
      was detected/ticked (detection-failure fallback path).
    * ``tsplit_raw`` — comma-separated tensor-split floats; malformed values
      are silently dropped (best-effort, never crashes the launch).

    An empty result (``device_ids == []``) yields ``to_cli_args() == []`` —
    the byte-for-byte backward-compatible "no --device" case.
    """
    ids = [i for i in checked_ids if i]
    if not ids and fallback_raw and fallback_raw.strip():
        ids = [t.strip() for t in fallback_raw.split(",") if t.strip()]
    split: list[float] = []
    if tsplit_raw and tsplit_raw.strip():
        with contextlib.suppress(ValueError):
            split = [float(t) for t in tsplit_raw.split(",") if t.strip()]
    return DeviceSelection(device_ids=ids, tensor_split=split)


def _build_sweep_configs(
    devices: list[LlamaDevice],
) -> list[tuple[str, DeviceSelection]]:
    """Auto-generate labelled sweep configurations from detected devices.

    Thin wrapper over the frozen
    :func:`coderouter.launcher_devices.build_auto_sweep_configs`, which:

    * emits one "single device" config per *selectable* device (0 MiB devices
      such as macOS ``BLAS: Accelerate`` are dropped — they hold no VRAM);
    * adds a multi-GPU + tensor-split config **only within a single backend**
      (never mixing CUDA / Vulkan, which on a CUDA+Vulkan build enumerate the
      same physical GPU twice — see ``backend_of`` / ``group_by_backend``).
    """
    return build_auto_sweep_configs(devices, by="total")


# ---------------------------------------------------------------------------
# Hardware detection + model recommendation (luna-go /models 互換の発想)
# ---------------------------------------------------------------------------

def _detect_hardware() -> dict[str, Any]:
    """ハードウェアを best-effort で検出する (stdlib + CLI、追加依存なし)。"""
    cpu = os.cpu_count() or 4
    ram_gb = 0.0
    with contextlib.suppress(ValueError, OSError, AttributeError):
        ram_gb = (os.sysconf("SC_PHYS_PAGES")
                  * os.sysconf("SC_PAGE_SIZE") / (1024 ** 3))
    if ram_gb <= 0:
        try:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, text=True, timeout=3)
            ram_gb = int(out.stdout.strip()) / (1024 ** 3)
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    gpu, vram_gb = "cpu", 0.0
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        gpu, vram_gb = "metal", ram_gb            # ユニファイドメモリ
    elif shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            mb = max((int(x) for x in out.stdout.split() if x.strip().isdigit()),
                     default=0)
            if mb > 0:
                gpu, vram_gb = "cuda", mb / 1024
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
    return {"ram_gb": round(ram_gb, 1), "vram_gb": round(vram_gb, 1),
            "gpu": gpu, "cpu_count": cpu}


def _hw_summary(hw: dict[str, Any]) -> str:
    """検出ハードを 1 行で表す (UI 表示用)。"""
    gpu_label = {"metal": "Metal", "cuda": "CUDA", "cpu": "CPU"}.get(
        hw.get("gpu", "cpu"), "CPU")
    parts = [gpu_label, f"RAM {hw.get('ram_gb', 0):g}GB"]
    if hw.get("gpu") == "cuda" and hw.get("vram_gb"):
        parts.append(f"VRAM {hw['vram_gb']:g}GB")
    return " · ".join(parts)


def _usable_memory_gb(hw: dict[str, Any]) -> float:
    """モデルの重み + KV キャッシュに使えるメモリ量。"""
    if hw.get("gpu") == "cuda":
        return float(hw.get("vram_gb", 0.0))
    return float(hw.get("ram_gb", 0.0))          # metal (ユニファイド) / cpu


def _model_recommendation(size_gb: float, hw: dict[str, Any]) -> dict[str, str]:
    """モデル単位のメモリ適合判定 (luna-go /models 相当)。

    level: "ok" (推奨) | "warn" (メモリ厳しい) | "unknown"
    """
    usable = _usable_memory_gb(hw)
    if usable <= 0 or size_gb <= 0:
        return {"level": "unknown", "label": "—"}
    if size_gb * 1.2 + 2.0 <= usable:
        return {"level": "ok", "label": "推奨"}
    return {"level": "warn", "label": "メモリ厳しい"}


def _suggest_launch_flags(backend: str, size_gb: float,
                          hw: dict[str, Any]) -> str:
    """選択モデル + ハード + バックエンドから推奨起動フラグを提案する。

    バックエンドごとにフラグ体系が違うため分岐する:
      - llama.cpp : -ngl / --ctx-size / --threads を算出
      - vllm      : モデル config からの自動導出に任せる (空文字)
      - mlx       : 統合メモリ前提で起動時フラグ不要 (空文字)
    あくまで目安。他プロセスのメモリ使用や量子化方式までは考慮しない。
    """
    if backend == "mlx":
        # MLX は統合メモリ + Metal 前提。llama.cpp の -ngl に相当する
        # レイヤーオフロードの概念がなく、mlx_lm.server は起動時の
        # 性能チューニングフラグを取らない。
        return ""
    if backend == "vllm":
        # vllm の --max-model-len はモデルの実コンテキスト長に依存する。
        # メモリ量だけのヒューリスティックで値を出すと、モデルの上限を
        # 超えたときに vllm が起動を拒否する。空にしてエンジンの
        # 自動導出 (モデル config) に任せるのが安全。
        return ""

    # llama.cpp (デフォルト)
    usable = _usable_memory_gb(hw)
    weights = size_gb * 1.15                       # 重み + オーバーヘッド概算
    threads = max(1, int(hw.get("cpu_count", 4)) - 2)
    if hw.get("gpu") == "cpu":
        ngl = 0
    elif usable >= weights + 1.0:
        ngl = 99                                   # 全レイヤー GPU に載る
    elif usable > 1.5:
        ngl = max(0, min(99, int(99 * (usable - 0.7) / max(weights, 0.1))))
    else:
        ngl = 0
    headroom = usable - weights - 1.0
    if headroom >= 8:
        ctx = 32768
    elif headroom >= 4:
        ctx = 16384
    elif headroom >= 2:
        ctx = 8192
    else:
        ctx = 4096
    return f"-ngl {ngl} --ctx-size {ctx} --threads {threads}"


# ---------------------------------------------------------------------------
# CodeRouter helpers
# ---------------------------------------------------------------------------

def _find_coderouter_cmd() -> list[str]:
    """CodeRouter の起動コマンドプレフィクスを返す。

    優先順位:
      1. PATH の coderouter (pip install / uvx で入れた場合)
      2. uv run coderouter  (プロジェクト venv)
      3. python -m coderouter (フォールバック)
    """
    if shutil.which("coderouter"):
        return ["coderouter"]
    if shutil.which("uv"):
        return ["uv", "run", "coderouter"]
    return [sys.executable, "-m", "coderouter"]


def _ensure_providers_yaml(llama_port: int) -> tuple[bool, str]:
    """~/.coderouter-t/providers.yaml が存在しない場合だけ自動生成する。

    Returns:
        (created, path) — created=True なら今回新しく作った。
    """
    config_dir = Path.home() / ".coderouter-t"
    config_path = config_dir / "providers.yaml"

    if config_path.exists():
        return False, str(config_path)

    config_dir.mkdir(parents=True, exist_ok=True)
    content = f"""\
# CodeRouter providers.yaml — launcher_gui.py により自動生成
# 手動で編集して構いません。詳細は examples/providers.yaml を参照。

allow_paid: false
default_profile: default

providers:
  - name: llama-cpp-local
    kind: openai_compat
    base_url: http://localhost:{llama_port}/v1
    model: ""          # llama-server はモデル名を問わないので空でOK
    timeout_s: 120
    capabilities:
      chat: true
      streaming: true
      tools: true

profiles:
  - name: default
    providers: [llama-cpp-local]
"""
    config_path.write_text(content, encoding="utf-8")
    return True, str(config_path)


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

@dataclass
class ManagedProcess:
    id: str
    name: str
    backend: str
    model_name: str
    port: int
    cmd: list[str]
    # "starting" (constructing, pre-spawn) | "loading" (Popen succeeded,
    # waiting on the readiness probe — see _readiness_worker) | "running"
    # (readiness confirmed) | "stopping" (Stop/Kill requested, transient
    # display state) | "stopped" | "error"
    status: str = "starting"
    pid: int | None = None
    returncode: int | None = None
    proc: Any = None
    # 無制限肥大化を防ぐため上限付き deque を使用(古い行から自動破棄)
    log_lines: deque[str] = field(
        default_factory=lambda: deque(maxlen=_MAX_LOG_LINES))
    # Set by _do_stop / _do_kill just before signalling the child. Tells the
    # launch worker thread the exit was requested, not a crash, so it never
    # auto-restarts and never mislabels whatever exit code SIGTERM/SIGKILL
    # produced as "error" (mirrors ManagedProcess.stopping in
    # coderouter/ingress/launcher_routes.py).
    stopping: bool = False
    # Consecutive generic auto-restart attempts since the last readiness
    # success. Reset to 0 by _readiness_worker once it confirms "running".
    restart_count: int = 0
    started_at: float = 0.0
    # Bumped on every (re)spawn — initial launch, MTP fallback relaunch, or
    # generic auto-restart. A readiness worker captures its own generation
    # at start and aborts once it no longer matches, so a stale worker left
    # over from a previous spawn attempt can never overwrite the status set
    # by a newer one (mirrors the supersede-safety of
    # ManagedProcess.ready.clear() in launcher_routes.py).
    spawn_gen: int = 0


# ---------------------------------------------------------------------------
# Readiness gating — ported from coderouter/ingress/launcher_routes.py
# (_backend_ready / _wait_ready_and_register). A backend used to be shown as
# "running" the instant the OS process spawned, before llama-server / vllm
# had actually finished loading the model — the GUI would claim success
# while the model was still loading. Now the launch worker thread stays in
# "loading" until a poll confirms the backend is actually serving, or the
# poll deadline is exceeded (status becomes "error"; the process itself is
# left running so the user can inspect logs / stop it manually).
# ---------------------------------------------------------------------------


def _backend_ready(backend: str, port: int, *, probe_timeout_s: float) -> bool:
    """Best-effort single readiness probe. Never raises.

    llama.cpp and vllm both expose ``GET /health`` (200 once the model is
    loaded and the server is accepting requests; llama.cpp returns 503
    while still loading). Other backends (mlx — mlx_lm.server has no
    documented health endpoint) fall back to a bare TCP connect: it can't
    distinguish "loaded" from "listening", but it is a strict improvement
    over showing "running" before the port is even open.

    ``backend`` may be a variant name (``llama.cpp-cuda``): the check goes
    through :func:`base_backend` so a variant keeps the ``/health`` probe
    instead of silently degrading to the bare TCP connect.
    """
    if base_backend(backend) in ("llama.cpp", "vllm"):
        try:
            # 127.0.0.1 literal, never `localhost` — see the matching note in
            # coderouter/ingress/launcher_routes.py::_backend_ready. The
            # backends this probes listen on IPv4 only by default, and the
            # bare TCP fallback below already uses the literal; this branch
            # was the odd one out in both copies of this function.
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=probe_timeout_s) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=probe_timeout_s):
            return True
    except OSError:
        return False


def poll_until_ready(
    *,
    check: Callable[[], bool],
    should_abort: Callable[[], bool],
    timeout_s: float,
    poll_interval_s: float,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> str:
    """Generic poll loop, decoupled from Tk / threading / sockets.

    Returns one of:
      * ``"ready"``   — ``check()`` returned True before the deadline.
      * ``"timeout"`` — the deadline passed with ``check()`` never True.
      * ``"aborted"`` — ``should_abort()`` returned True (the caller is no
        longer in a loading-eligible state: crashed, stopped, or a newer
        spawn superseded this one) — checked both before every probe and
        immediately after a successful one, so a fast state change can
        never race a stale "ready" outcome in.

    Injectable ``sleep`` / ``now`` make this fully unit-testable without
    real timing. Mirrors the loop shape of
    ``launcher_routes._wait_ready_and_register``.
    """
    deadline = now() + timeout_s
    while now() < deadline:
        if should_abort():
            return "aborted"
        if check():
            return "aborted" if should_abort() else "ready"
        sleep(poll_interval_s)
    return "timeout"


# ---------------------------------------------------------------------------
# Tagged log-queue events (H-13: no in-band signaling)
#
# The launcher's log queue carries BOTH control events (spawn succeeded,
# readiness passed, spawn failed, devices probed) and the raw stdout of the
# child backend. Encoding control events as magic prefixes *inside the line*
# ("_ERR_:...", "_READY_:...") meant any child that happened to print such a
# line could drive the UI: a single "_ERR_:" line from llama-server would
# ``del self.processes[proc_id]``, orphaning a still-live server (VRAM + port
# held, and no longer stopped by ``_on_close``). A short "_SPAWNED_:x" line
# raised ValueError inside the drain loop and dropped that whole tick's logs.
# llama-server echoes GGUF metadata (chat templates and friends) verbatim, so
# this was reachable from model data alone.
#
# So every queue item is now a ``(kind, proc_id, payload)`` triple and raw
# child output is ALWAYS enqueued with ``LOG_KIND_LOG`` — it is never
# inspected for markers. Same treatment for the CodeRouter queue, whose items
# are ``(kind, payload)`` pairs. Mirrors ``SweepWindow._queue``'s
# ("step"/"log"/"done") tagging, which already had this shape.
#
# Payload shapes (plain strings for now; dataclass payloads are a follow-up):
#   LOG_KIND_LOG      -> the log line
#   LOG_KIND_SPAWNED  -> "<name>:<port>"
#   LOG_KIND_READY    -> "<name>:<port>"
#   LOG_KIND_ERROR    -> the error text
#   LOG_KIND_DEVICES  -> "" (the real payload is on ``app._pending_probe``)
# ---------------------------------------------------------------------------

LOG_KIND_LOG = "log"
LOG_KIND_SPAWNED = "spawned"
LOG_KIND_READY = "ready"
LOG_KIND_ERROR = "error"
LOG_KIND_DEVICES = "devices"

CR_KIND_LOG = "log"
CR_KIND_OK = "ok"
CR_KIND_ERROR = "error"
CR_KIND_EXIT = "exit"

# (kind, proc_id, payload) — see the block comment above.
LogEvent = tuple[str, str, str]
# (kind, payload) — the CodeRouter supervisor queue has no proc_id.
CrLogEvent = tuple[str, str]


def parse_name_port(payload: str) -> tuple[str, int] | None:
    """Split a ``"<name>:<port>"`` control payload; ``None`` if malformed.

    ``rpartition`` (not ``split``) so a model name containing ``:`` still
    parses — the port is always the last field. Returning ``None`` instead of
    raising is deliberate: a malformed control payload must skip that one
    event, never take down the drain loop for the whole tick (the old
    ``line.split(":", 2)`` raised ValueError and did exactly that).
    """
    name, sep, port = payload.rpartition(":")
    if not sep or not name or not port.isdigit():
        return None
    return name, int(port)


def _readiness_worker(
    mp: ManagedProcess,
    cfg: LauncherConfig,
    log_queue: queue.Queue[LogEvent],
    gen: int,
    *,
    backend_ready: Callable[..., bool] = _backend_ready,
) -> None:
    """Poll ``mp`` for readiness, then flip it to "running" — or "error".

    Runs as an independent daemon thread per spawn (initial, MTP fallback,
    or auto-restart), started right after Popen succeeds. Takes plain data
    (no ``self``) so it can be driven directly in tests without a live Tk
    app — mirrors the signature shape of ``_wait_ready_and_register``.
    """

    def _abort() -> bool:
        return (
            mp.spawn_gen != gen
            or mp.proc is None
            or mp.stopping
            or mp.status not in ("starting", "loading")
        )

    outcome = poll_until_ready(
        check=lambda: backend_ready(
            mp.backend, mp.port, probe_timeout_s=_READINESS_PROBE_TIMEOUT_S
        ),
        should_abort=_abort,
        timeout_s=cfg.readiness_timeout_s,
        poll_interval_s=cfg.readiness_poll_interval_s,
    )

    if outcome == "ready":
        # Re-guard IMMEDIATELY before the write. poll_until_ready re-checks
        # should_abort at the instant it decides "ready", but unlike the Web
        # 版 (asyncio, single-threaded — check and write are atomic), this
        # worker runs in a REAL thread: between that decision and this
        # assignment the launch worker (_run) can crash-handle, consume an
        # auto-restart attempt, and bump spawn_gen for a respawn. Without
        # this re-check a stale worker would clobber the newer generation's
        # status/restart_count.
        if _abort():
            return
        mp.status = "running"
        mp.restart_count = 0
        log_queue.put(
            (LOG_KIND_LOG, mp.id, "[launcher] readiness check passed"))
        log_queue.put((LOG_KIND_READY, mp.id, f"{mp.name}:{mp.port}"))
    elif outcome == "timeout":
        if mp.spawn_gen == gen and mp.status in ("starting", "loading"):
            mp.status = "error"
            log_queue.put((
                LOG_KIND_LOG,
                mp.id,
                f"[launcher] readiness check timed out after "
                f"{cfg.readiness_timeout_s:.0f}s — process left running but "
                "not confirmed ready"
            ))
    # "aborted": bail out silently, exactly like the web version — a fast
    # crash/stop/respawn must never have a stale probe overwrite its status.


# ---------------------------------------------------------------------------
# Generic auto-restart — ported from launcher_routes._attempt_restart.
# Opt-in via LauncherConfig.auto_restart (default False — see the docstring
# on that field in coderouter/config/schemas.py for the rationale: silently
# respawning a genuinely misconfigured backend forever would be worse than
# just leaving it in status="error").
# ---------------------------------------------------------------------------


@dataclass
class RestartPlan:
    should_restart: bool
    backoff_s: float = 0.0
    log_lines: list[str] = field(default_factory=list)


def plan_auto_restart(
    *,
    auto_restart: bool,
    restart_count: int,
    max_attempts: int,
    backoff_s: float,
    backoff_max_s: float,
    has_cmd: bool,
) -> RestartPlan:
    """Decide whether/how to auto-restart a crashed backend.

    Pure decision logic — no subprocess spawn, no sleep — mirroring
    ``launcher_routes._attempt_restart`` minus the actual respawn (which the
    caller performs after honoring ``mp.stopping`` one more time once the
    backoff sleep completes, exactly like the web version does).
    """
    if not auto_restart:
        return RestartPlan(should_restart=False)
    if restart_count >= max_attempts:
        return RestartPlan(
            should_restart=False,
            log_lines=[
                f"[launcher] auto-restart exhausted "
                f"({restart_count}/{max_attempts} attempts); giving up"
            ],
        )
    if not has_cmd:
        return RestartPlan(should_restart=False)  # nothing to relaunch

    backoff = min(backoff_s * (2 ** restart_count), backoff_max_s)
    return RestartPlan(
        should_restart=True,
        backoff_s=backoff,
        log_lines=[
            f"[launcher] auto-restart attempt {restart_count + 1}/"
            f"{max_attempts} in {backoff:.1f}s"
        ],
    )


def _exit_status(returncode: int | None, stopping: bool) -> str:
    """Map a subprocess exit to a terminal ManagedProcess.status.

    An intentional stop (Stop/Kill button) is always "stopped", regardless
    of the exit code SIGTERM/SIGKILL produced (POSIX SIGTERM typically
    yields a negative returncode) — without this, every deliberate Stop
    would have been mislabeled "error". Mirrors the ``proc.stopping`` check
    in ``launcher_routes._tail_logs``.
    """
    if stopping:
        return "stopped"
    return "stopped" if (returncode or 0) == 0 else "error"


def _proc_alive(mp: ManagedProcess) -> bool:
    """True iff ``mp`` still holds a live OS process.

    Liveness is decided by ``proc.poll()``, never by ``mp.status``: since
    readiness gating, a readiness-timed-out backend sits in status="error"
    while the OS process is still very much alive (holding its port and
    VRAM). Any stop/kill/remove decision keyed on the status set alone
    would drop such a process un-killed and orphan it — poll() is the only
    source of truth.
    """
    return mp.proc is not None and mp.proc.poll() is None


def _kill_for_removal(mp: ManagedProcess) -> None:
    """Force-kill a live process on behalf of a UI removal.

    ``stopping`` is set BEFORE the signal so the launch worker thread
    treats the exit as intentional: no auto-restart of a process the UI no
    longer tracks, and no "error" mislabel from the SIGKILL exit code.
    """
    mp.stopping = True
    with contextlib.suppress(Exception):
        mp.proc.kill()


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class LauncherApp(tk.Tk):
    """CodeRouter Launcher — tkinter GUI."""

    # ── colours ─────────────────────────────────────────────────────────────
    BG       = "#0f172a"   # slate-900
    BG2      = "#1e293b"   # slate-800
    BG3      = "#334155"   # slate-700
    FG       = "#e2e8f0"   # slate-200
    FG2      = "#94a3b8"   # slate-400
    ACCENT   = "#6366f1"   # indigo-500
    GREEN    = "#22c55e"
    RED      = "#ef4444"
    YELLOW   = "#eab308"

    def __init__(self, config_path: str | None = None) -> None:
        super().__init__()
        self.title("CodeRouter Launcher")
        self.geometry("1100x800")
        self.minsize(900, 650)
        self.configure(bg=self.BG)

        # State
        self.cfg = _load_config(config_path)
        self.models: list[dict[str, Any]] = []
        self.processes: dict[str, ManagedProcess] = {}
        # スイープ子は processes に載らない (H-14: ManagedProcess は UI テーブル/
        # 停止削除ボタン/status 更新に直結し、構成ごとに生成破棄されるスイープ子
        # を混ぜると矛盾する)。代わりに窓を登録して _on_close から明示的に落とす。
        self._sweep_windows: set[SweepWindow] = set()
        self.selected_proc_id: str | None = None
        self._last_auto_name: str = ""   # _on_model_select が自動入力した名前を記録
        self._hw: dict[str, Any] = {}    # 検出済みハードウェア情報
        # (kind, proc_id, payload) — tagged events, never in-band markers.
        # See the LOG_KIND_* block above _readiness_worker.
        self._log_queue: queue.Queue[LogEvent] = queue.Queue()

        # ── CodeRouter プロセス管理 ─────────────────────────────────────────
        self._cr_proc: subprocess.Popen | None = None
        self._cr_status: str = "stopped"   # stopped / starting / running / error
        # 上限付き deque。常駐 CodeRouter の出力で無制限に増えるのを防ぐ。
        self._cr_log: deque[str] = deque(maxlen=_MAX_LOG_LINES)
        # (kind, payload) — same tagging rationale as _log_queue.
        self._cr_log_queue: queue.Queue[CrLogEvent] = queue.Queue()
        self._cr_port: int = _CODEROUTER_PORT

        # ttk style
        self._setup_style()

        # Layout
        self._build_ui()

        # ウィンドウ閉時に CodeRouter + 全バックエンドを停止
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Initial scan
        self.after(100, self._do_scan)

        # Periodic refresh
        self._poll()

    # ── Style ────────────────────────────────────────────────────────────────

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            style.theme_use("default")

        style.configure(".", background=self.BG, foreground=self.FG,
                        fieldbackground=self.BG2, troughcolor=self.BG2,
                        bordercolor=self.BG3, lightcolor=self.BG3,
                        darkcolor=self.BG3, relief="flat")

        style.configure("TFrame", background=self.BG)
        style.configure("Card.TFrame", background=self.BG2, relief="flat")

        style.configure("TLabel", background=self.BG, foreground=self.FG)
        style.configure("Dim.TLabel", background=self.BG2, foreground=self.FG2,
                        font=("monospace", 10))
        style.configure("Title.TLabel", background=self.BG, foreground=self.FG2,
                        font=("sans-serif", 9, "bold"))
        style.configure("Status.TLabel", background=self.BG2, foreground=self.FG2,
                        font=("monospace", 10))

        style.configure("TEntry", fieldbackground=self.BG3, foreground=self.FG,
                        insertcolor=self.FG, relief="flat", borderwidth=1)
        style.configure("TCombobox", fieldbackground=self.BG3, foreground=self.FG,
                        selectbackground=self.ACCENT, selectforeground="white",
                        insertcolor=self.FG, relief="flat", arrowcolor=self.FG2)
        style.map("TCombobox",
                  fieldbackground=[("readonly", self.BG3),
                                   ("disabled", self.BG2)],
                  foreground=[("disabled", self.FG2)],
                  selectbackground=[("readonly", self.ACCENT)])

        style.configure("TButton", background=self.BG3, foreground=self.FG,
                        relief="flat", padding=(8, 4))
        style.map("TButton",
                  background=[("active", self.BG2), ("disabled", self.BG2)],
                  foreground=[("disabled", self.FG2)])

        style.configure("Accent.TButton", background=self.ACCENT, foreground="white",
                        relief="flat", padding=(10, 6), font=("sans-serif", 11, "bold"))
        style.map("Accent.TButton",
                  background=[("active", "#4f46e5"), ("disabled", self.BG3)],
                  foreground=[("disabled", self.FG2)])

        _tv_map = [
            ("background", [
                ("selected", "focus",   self.ACCENT),
                ("selected", "!focus",  self.ACCENT),
                ("active",              self.BG3),
            ]),
            ("foreground", [
                ("selected", "focus",   "white"),
                ("selected", "!focus",  "white"),
                ("active",              self.FG),
            ]),
        ]
        for prop, rules in _tv_map:
            style.map("Treeview", **{prop: rules})

        style.configure("Treeview",
                        background=self.BG2, foreground=self.FG,
                        fieldbackground=self.BG2, rowheight=26,
                        borderwidth=0, relief="flat",
                        highlightthickness=0)
        style.configure("Treeview.Heading",
                        background=self.BG3, foreground=self.FG2,
                        relief="flat", font=("sans-serif", 9, "bold"))

        style.configure("Model.Treeview",
                        background=self.BG2, foreground=self.FG,
                        fieldbackground=self.BG2, rowheight=22,
                        font=("monospace", 10), borderwidth=0, relief="flat",
                        highlightthickness=0)
        for prop, rules in _tv_map:
            style.map("Model.Treeview", **{prop: rules})

        style.configure("TScrollbar", background=self.BG3, troughcolor=self.BG2,
                        relief="flat", arrowsize=12)


    # ── UI Build ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Top bar
        top = ttk.Frame(self, padding=(12, 8))
        top.pack(fill="x")
        ttk.Label(top, text="CodeRouter Launcher",
                  font=("sans-serif", 14, "bold"),
                  foreground=self.FG, background=self.BG).pack(side="left")

        self._status_var = tk.StringVar(value="準備完了")
        ttk.Label(top, textvariable=self._status_var,
                  style="Status.TLabel").pack(side="right", padx=4)

        sep = tk.Frame(self, height=1, bg=self.BG3)
        sep.pack(fill="x")

        # ── CodeRouter パネル ────────────────────────────────────────────────
        self._build_coderouter_panel()

        sep2 = tk.Frame(self, height=1, bg=self.BG3)
        sep2.pack(fill="x")

        # Main area — left / right split
        main = ttk.Frame(self, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=2, minsize=260)
        main.columnconfigure(1, weight=3, minsize=340)
        main.rowconfigure(0, weight=1)

        self._build_models_panel(main)
        self._build_right_panel(main)

    # ── CodeRouter パネル ─────────────────────────────────────────────────────

    def _build_coderouter_panel(self) -> None:
        """CodeRouter 起動/停止コントロールバー。"""
        bar = tk.Frame(self, bg="#1e293b", pady=0)
        bar.pack(fill="x")

        inner = tk.Frame(bar, bg="#1e293b")
        inner.pack(fill="x", padx=12, pady=6)

        # ステータスドット
        self._cr_dot = tk.Label(inner, text="●", fg=self.RED,
                                bg="#1e293b", font=("sans-serif", 11))
        self._cr_dot.pack(side="left")

        # ラベル
        self._cr_label_var = tk.StringVar(value=f"  CodeRouter  :{self._cr_port}  停止中")
        tk.Label(inner, textvariable=self._cr_label_var,
                 fg=self.FG2, bg="#1e293b",
                 font=("sans-serif", 10)).pack(side="left", padx=(0, 10))

        # ポート入力欄(停止中のみ編集可。trace は _cr_conn_var 生成後に設定)
        tk.Label(inner, text="ポート", fg=self.FG2, bg="#1e293b",
                 font=("sans-serif", 9)).pack(side="left", padx=(0, 4))
        self._cr_port_var = tk.StringVar(value=str(_CODEROUTER_PORT))
        self._cr_port_entry = ttk.Entry(inner, textvariable=self._cr_port_var,
                                        width=6)
        self._cr_port_entry.pack(side="left", padx=(0, 10))

        # 起動ボタン
        self._cr_start_btn = tk.Button(
            inner, text="▶ CodeRouter 起動",
            fg="white", bg=self.ACCENT,
            activebackground="#4f46e5", activeforeground="white",
            relief="flat", bd=0, padx=10, pady=4,
            font=("sans-serif", 10, "bold"),
            cursor="hand2",
            command=self._start_coderouter,
        )
        self._cr_start_btn.pack(side="left", padx=(0, 4))

        # 停止ボタン
        self._cr_stop_btn = tk.Button(
            inner, text="■ 停止",
            fg=self.FG, bg=self.BG3,
            activebackground=self.BG2, activeforeground=self.FG,
            relief="flat", bd=0, padx=8, pady=4,
            font=("sans-serif", 10),
            cursor="hand2",
            command=self._stop_coderouter,
            state="disabled",
        )
        self._cr_stop_btn.pack(side="left", padx=(0, 8))

        # アニメーション用(Progressbar 非使用)
        self._cr_anim_running: bool = False

        # 接続文字列(Claude Code 用)
        conn_str = f"ANTHROPIC_BASE_URL=http://localhost:{self._cr_port} ANTHROPIC_AUTH_TOKEN=dummy claude"
        self._cr_conn_var = tk.StringVar(value=conn_str)
        # ポート欄の編集に接続文字列・ラベルを追従させる
        self._cr_port_var.trace_add("write", self._on_cr_port_change)
        tk.Label(inner, text="Claude Code:", fg=self.FG2, bg="#1e293b",
                 font=("sans-serif", 9)).pack(side="left")
        conn_label = tk.Label(inner, textvariable=self._cr_conn_var,
                              fg="#4ade80", bg="#1e293b",
                              font=("monospace", 9), cursor="hand2")
        conn_label.pack(side="left", padx=(4, 0))
        conn_label.bind("<Button-1>", lambda _: self._copy_conn_str())

        # コピーボタン
        tk.Button(
            inner, text="コピー",
            fg=self.FG2, bg=self.BG3,
            activebackground=self.BG2, activeforeground=self.FG,
            relief="flat", bd=0, padx=6, pady=2,
            font=("sans-serif", 9),
            cursor="hand2",
            command=self._copy_conn_str,
        ).pack(side="left", padx=(4, 0))

        # エラー表示
        self._cr_err_var = tk.StringVar(value="")
        tk.Label(inner, textvariable=self._cr_err_var,
                 fg=self.RED, bg="#1e293b",
                 font=("sans-serif", 9)).pack(side="right", padx=(8, 0))

    def _copy_conn_str(self) -> None:
        conn = self._cr_conn_var.get()
        self.clipboard_clear()
        self.clipboard_append(conn)
        self._cr_err_var.set("✓ コピーしました")
        self.after(2000, lambda: self._cr_err_var.set(""))

    def _on_cr_port_change(self, *_: Any) -> None:
        """ポート欄が編集されたら _cr_port・接続文字列・ラベルを追従させる。"""
        raw = self._cr_port_var.get().strip()
        if raw.isdigit():
            self._cr_port = int(raw)
        # 接続文字列を最新ポートで更新(無効入力時は直前の有効値を維持)
        self._cr_conn_var.set(
            f"ANTHROPIC_BASE_URL=http://localhost:{self._cr_port} "
            f"ANTHROPIC_AUTH_TOKEN=dummy claude"
        )
        self._update_cr_ui()

    def _update_cr_ui(self) -> None:
        """CodeRouter のステータスに合わせて UI を更新する。"""
        if self._cr_status == "running":
            self._cr_dot.configure(fg=self.GREEN)
            self._cr_label_var.set(f"  CodeRouter  :{self._cr_port}  稼働中")
            self._cr_start_btn.configure(state="disabled")
            self._cr_stop_btn.configure(state="normal")
            self._cr_anim_running = False
        elif self._cr_status == "starting":
            self._cr_dot.configure(fg=self.YELLOW)
            self._cr_start_btn.configure(state="disabled")
            self._cr_stop_btn.configure(state="disabled")
            if not self._cr_anim_running:
                self._cr_anim_running = True
                self._cr_anim_tick(0)
        elif self._cr_status == "error":
            self._cr_dot.configure(fg=self.RED)
            self._cr_label_var.set(f"  CodeRouter  :{self._cr_port}  エラー")
            self._cr_start_btn.configure(state="normal")
            self._cr_stop_btn.configure(state="disabled")
            self._cr_anim_running = False
        else:  # stopped
            self._cr_dot.configure(fg=self.RED)
            self._cr_label_var.set(f"  CodeRouter  :{self._cr_port}  停止中")
            self._cr_start_btn.configure(state="normal")
            self._cr_stop_btn.configure(state="disabled")
            self._cr_anim_running = False

        # ポート欄は停止中/エラー時のみ編集可(起動中・稼働中はロック)
        editable = self._cr_status in ("stopped", "error")
        self._cr_port_entry.configure(state="normal" if editable else "disabled")

    _ANIM_CHARS = ("|", "/", "-", "\\")

    def _cr_anim_tick(self, idx: int) -> None:
        """CodeRouter 起動中のテキストアニメーション(after() ベース)。"""
        if not self._cr_anim_running:
            return
        ch = self._ANIM_CHARS[idx % len(self._ANIM_CHARS)]
        self._cr_label_var.set(f"  CodeRouter  :{self._cr_port}  起動中… {ch}")
        self.after(150, self._cr_anim_tick, idx + 1)

    def _launch_anim_tick(self, proc_id: str, idx: int) -> None:
        """llama.cpp 起動中のボタンテキストアニメーション(after() ベース)。"""
        if self._launch_anim_proc_id != proc_id:
            return
        if proc_id not in self.processes or self.processes[proc_id].status not in ("starting",):
            # 起動完了 or エラー → ボタンを元に戻す
            self._launch_btn.configure(
                text="▶ llama.cpp / vllm / mlx 起動", state="normal", cursor="hand2"
            )
            self._launch_anim_proc_id = None
            return
        ch = self._ANIM_CHARS[idx % len(self._ANIM_CHARS)]
        self._launch_btn.configure(text=f"起動中… {ch}")
        self.after(150, self._launch_anim_tick, proc_id, idx + 1)

    # ── CodeRouter 起動 / 停止 ────────────────────────────────────────────────

    def _start_coderouter(self) -> None:
        """CodeRouter をポート欄の値で起動する。providers.yaml がなければ自動生成。"""
        # CodeRouter ポートの検証(ポート欄の値を使用)
        cr_port_raw = self._cr_port_var.get().strip()
        if not cr_port_raw.isdigit() or not (1024 <= int(cr_port_raw) <= 65535):
            self._cr_err_var.set("CodeRouter ポートは 1024-65535 の数字で指定してください")
            return
        self._cr_port = int(cr_port_raw)

        # llama.cpp の現在のポートを取得(フォームの値を使用)
        try:
            llama_port = int(self._port_var.get())
        except (ValueError, AttributeError):
            llama_port = 8080

        # providers.yaml を自動生成(存在しない場合のみ)
        created, yaml_path = _ensure_providers_yaml(llama_port)
        if created:
            self._cr_err_var.set(f"providers.yaml を生成しました: {yaml_path}")
            self.after(4000, lambda: self._cr_err_var.set(""))
            print(f"[CodeRouter] providers.yaml 生成: {yaml_path}", flush=True)

        self._cr_status = "starting"
        self._update_cr_ui()

        cr_port = self._cr_port  # スレッドに渡すためローカルに保持

        def _run() -> None:
            # shutil.which() をスレッド内で実行(メインスレッドをブロックしない)
            cr_cmd = _find_coderouter_cmd()
            cmd = [*cr_cmd, "serve", "--port", str(cr_port)]
            print(f"[CodeRouter] 起動: {' '.join(cmd)}", flush=True)
            try:
                p = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                )
            except Exception as exc:
                self._cr_log_queue.put((CR_KIND_ERROR, str(exc)))
                return

            self._cr_proc = p
            self._cr_log_queue.put((CR_KIND_OK, str(p.pid)))

            assert p.stdout
            for raw in iter(lambda: p.stdout.read(4096), b""):
                for line in raw.decode("utf-8", errors="replace").splitlines():
                    # Raw child output: ALWAYS a plain log event. Never
                    # inspected for control markers (H-13).
                    self._cr_log_queue.put((CR_KIND_LOG, line))
            p.wait()
            self._cr_log_queue.put((CR_KIND_EXIT, str(p.returncode)))

        threading.Thread(target=_run, daemon=True).start()

    def _stop_coderouter(self) -> None:
        """CodeRouter を停止する。"""
        if self._cr_proc and self._cr_proc.poll() is None:
            with contextlib.suppress(Exception):
                self._cr_proc.terminate()
            self._cr_log.append("[coderouter] SIGTERM 送信")
        self._cr_status = "stopped"
        self._cr_proc = None
        self._update_cr_ui()

    # ── ウィンドウ閉時 ───────────────────────────────────────────────────────

    def _on_close(self) -> None:
        """ウィンドウを閉じる際に CodeRouter と全バックエンドを停止する。"""
        # H-14: スイープ実行中なら確認 (スイープ子は processes に載らないので
        # ここで明示的に落とさないとオーファン化する)。
        running = [w for w in self._sweep_windows if w.is_running()]
        if running and not messagebox.askyesno(
                "確認", "ベンチスイープが実行中です。中断してアプリを終了しますか?"):
            return

        # CodeRouter 停止
        if self._cr_proc and self._cr_proc.poll() is None:
            with contextlib.suppress(Exception):
                self._cr_proc.terminate()

        # llama.cpp / vllm 停止
        for mp in list(self.processes.values()):
            if mp.proc and mp.proc.poll() is None:
                # H-14: terminate の前に stopping を立てる。これが無いと起動
                # ワーカーが SIGTERM をクラッシュと誤認し、MTP フォールバック/
                # オートリスタートで終了処理中に新 llama-server を spawn して
                # オーファン化する (_do_stop/_do_kill/_kill_for_removal は既に
                # stopping を立てている)。
                mp.stopping = True
                with contextlib.suppress(Exception):
                    mp.proc.terminate()

        # H-14: スイープ窓を落とす (processes に載らないスイープ子を明示終了)。
        for win in list(self._sweep_windows):
            with contextlib.suppress(Exception):
                win.shutdown_for_app_close()

        self.destroy()

    # ── Models panel (left) ──────────────────────────────────────────────────

    def _card(self, parent: ttk.Frame, **grid_kw) -> ttk.Frame:
        f = tk.Frame(parent, bg=self.BG2, bd=0, highlightthickness=1,
                     highlightbackground=self.BG3)
        f.grid(**grid_kw)
        return f

    def _build_models_panel(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=0, column=0, sticky="nsew",
                          padx=(0, 6), pady=0)
        card.rowconfigure(2, weight=1)
        card.columnconfigure(0, weight=1)

        hdr = tk.Frame(card, bg=self.BG2)
        hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))
        tk.Label(hdr, text="MODELS", fg=self.FG2, bg=self.BG2,
                 font=("sans-serif", 9, "bold")).pack(side="left")
        self._hw_var = tk.StringVar(value="")
        tk.Label(hdr, textvariable=self._hw_var, fg=self.FG2, bg=self.BG2,
                 font=("monospace", 8)).pack(side="left", padx=(8, 0))
        btn = tk.Button(hdr, text="↻ スキャン", fg=self.FG, bg=self.BG3,
                        relief="flat", bd=0, padx=6, pady=2, cursor="hand2",
                        command=self._do_scan)
        btn.pack(side="right")

        self._dirs_var = tk.StringVar(value="スキャン中…")
        tk.Label(card, textvariable=self._dirs_var, fg=self.FG2, bg=self.BG2,
                 font=("monospace", 9), anchor="w", wraplength=240).grid(
            row=1, column=0, sticky="ew", padx=10, pady=(0, 4))

        lf = tk.Frame(card, bg=self.BG2)
        lf.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 8))
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)

        self._model_tree = ttk.Treeview(
            lf, style="Model.Treeview",
            show="tree", selectmode="browse",
        )
        # メモリ的に厳しいモデルの行を警告色にする
        self._model_tree.tag_configure("rec_warn", foreground=self.YELLOW)
        sb = ttk.Scrollbar(lf, orient="vertical",
                           command=self._model_tree.yview)
        self._model_tree.configure(yscrollcommand=sb.set)
        self._model_tree.column("#0", stretch=True)
        self._model_tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self._model_tree.bind("<<TreeviewSelect>>", self._on_model_select)

    def _do_scan(self) -> None:
        self._set_status("スキャン中…")
        dirs = self.cfg.model_dirs
        if dirs:
            self._dirs_var.set("  ".join(
                str(Path(d).expanduser()) for d in dirs
            ))
        else:
            self._dirs_var.set("model_dirs 未設定")

        def run() -> None:
            models = _scan_models(dirs)
            hw = _detect_hardware()
            self.after(0, lambda: self._populate_models(models, hw))

        threading.Thread(target=run, daemon=True).start()

    def _populate_models(self, models: list[dict],
                         hw: dict[str, Any] | None = None) -> None:
        self.models = models
        if hw is not None:
            self._hw = hw
        self._model_tree.delete(*self._model_tree.get_children())
        for i, m in enumerate(models):
            rec = _model_recommendation(m["size_gb"], self._hw)
            badge = {"ok": "   ✓ 推奨",
                     "warn": "   ⚠ メモリ厳しい"}.get(rec["level"], "")
            tags = ("rec_warn",) if rec["level"] == "warn" else ()
            self._model_tree.insert(
                "", "end", iid=str(i),
                text=f"{m['name']}  ({m['size_gb']} GB){badge}",
                tags=tags,
            )
        if self._hw:
            self._hw_var.set(_hw_summary(self._hw))
        self._set_status(f"モデル {len(models)} 件")

    def _on_model_select(self, _event: Any = None) -> None:
        sel = self._model_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        m = self.models[idx]
        self._model_path_var.set(m["path"])
        # Name が空、または前回ここで自動入力した値のまま(= 手で変更していない)
        # なら選択モデル名で更新する。手入力された名前は上書きしない。
        current = self._name_var.get()
        if not current or current == self._last_auto_name:
            stem = Path(m["name"]).stem[:30]
            self._name_var.set(stem)
            self._last_auto_name = stem

    def _suggest_options(self) -> None:
        """選択中モデル + ハードから推奨起動フラグを算出し追加オプション欄に入れる。

        ハード検出は通常スキャン時に済んでおり (_hw にキャッシュ)、未取得の
        場合のみその場で検出する。検出はほぼ即時 (CUDA 環境のみ nvidia-smi を
        一度呼ぶ程度) のためメインスレッドで同期実行する。
        """
        model_path = self._model_path_var.get().strip()
        if not model_path:
            self._set_launch_err("先にモデルを選択してください")
            return
        hw = self._hw or _detect_hardware()
        self._hw = hw
        try:
            size_gb = Path(model_path).expanduser().stat().st_size / (1024 ** 3)
        except OSError:
            size_gb = 0.0
        backend = self._backend_var.get()
        flags = _suggest_launch_flags(backend, size_gb, hw)
        self._extra_var.set(flags)
        self._hw_var.set(_hw_summary(hw))
        self._set_launch_err("")
        if flags:
            self._set_status(f"推奨値を設定(目安): {_hw_summary(hw)} → {flags}")
        elif backend == "mlx":
            self._set_status(
                f"{_hw_summary(hw)} — MLX は起動時の調整フラグ不要です(目安)")
        elif backend == "vllm":
            self._set_status(
                f"{_hw_summary(hw)} — vllm は起動時フラグ不要"
                "(モデル設定から自動導出)")
        else:
            self._set_status(f"{_hw_summary(hw)} — 推奨フラグなし")

    # ── Right panel ──────────────────────────────────────────────────────────

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        right = ttk.Frame(parent)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=0)
        right.rowconfigure(1, weight=1)
        right.rowconfigure(2, weight=0)
        right.columnconfigure(0, weight=1)

        self._build_launch_panel(right)
        self._build_process_panel(right)
        self._build_log_panel(right)

    # ── Launch form ──────────────────────────────────────────────────────────

    def _build_launch_panel(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=0, column=0, sticky="ew",
                          padx=0, pady=(0, 6))
        card.columnconfigure(1, weight=1)
        card.columnconfigure(3, weight=1)

        def lbl(text: str, r: int, c: int) -> None:
            tk.Label(card, text=text, fg=self.FG2, bg=self.BG2,
                     font=("sans-serif", 9)).grid(
                row=r, column=c, sticky="w", padx=(10, 4), pady=(6, 0))

        tk.Label(card, text="LAUNCH  llama.cpp / vllm / mlx", fg=self.FG2, bg=self.BG2,
                 font=("sans-serif", 9, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w",
            padx=10, pady=(8, 2))

        lbl("名前", 1, 0)
        self._name_var = tk.StringVar()
        ttk.Entry(card, textvariable=self._name_var).grid(
            row=1, column=1, sticky="ew", padx=(0, 6), pady=(6, 0))

        lbl("ポート", 1, 2)
        self._port_var = tk.StringVar(value="8080")
        ttk.Entry(card, textvariable=self._port_var, width=8).grid(
            row=1, column=3, sticky="ew", padx=(0, 10), pady=(6, 0))

        lbl("バックエンド", 2, 0)
        self._backend_var = tk.StringVar(value="llama.cpp")
        cb = ttk.Combobox(card, textvariable=self._backend_var,
                          values=_backend_names(self.cfg),
                          state="readonly")
        cb.grid(row=2, column=1, columnspan=3, sticky="ew",
                padx=(0, 10), pady=(6, 0))
        cb.bind("<<ComboboxSelected>>", self._on_backend_change)

        self._binary_hint_var = tk.StringVar(value="")
        self._binary_hint_lbl = tk.Label(
            card, textvariable=self._binary_hint_var,
            fg=self.FG2, bg=self.BG2,
            font=("monospace", 9), anchor="w")
        self._binary_hint_lbl.grid(row=3, column=1, columnspan=3,
                                   sticky="ew", padx=(0, 10), pady=(2, 0))

        lbl("モデルパス", 4, 0)
        self._model_path_var = tk.StringVar()
        ttk.Entry(card, textvariable=self._model_path_var).grid(
            row=4, column=1, columnspan=3, sticky="ew",
            padx=(0, 10), pady=(6, 0))

        lbl("オプションプロファイル", 5, 0)
        self._profile_var = tk.StringVar(value="-- なし --")
        self._profile_cb = ttk.Combobox(card, textvariable=self._profile_var,
                                        state="readonly")
        self._profile_cb.grid(row=5, column=1, columnspan=3, sticky="ew",
                              padx=(0, 10), pady=(6, 0))
        self._profile_cb.bind("<<ComboboxSelected>>", self._on_profile_change)

        self._profile_args_var = tk.StringVar(value="")
        tk.Label(card, textvariable=self._profile_args_var,
                 fg=self.FG2, bg=self.BG2,
                 font=("monospace", 9), anchor="w", justify="left").grid(
            row=6, column=1, columnspan=3, sticky="ew",
            padx=(0, 10), pady=(0, 2))

        lbl("MTP/draft gguf (空欄で自動検出)", 7, 0)
        self._draft_path_var = tk.StringVar()
        ttk.Entry(card, textvariable=self._draft_path_var).grid(
            row=7, column=1, columnspan=2, sticky="ew",
            padx=(0, 6), pady=(6, 0))
        self._mtp_auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(card, text="MTP自動検出",
                        variable=self._mtp_auto_var).grid(
            row=7, column=3, sticky="w", padx=(0, 10), pady=(6, 0))

        # ── デバイス選択(llama.cpp; _HAS_DEVICES のときのみ) ────────────────
        # 検出ボタン → --list-devices を非同期実行 → チェックボックス群を動的に
        # 流し込む。検出失敗時は手入力 Entry にフォールバック。tensor-split 欄は
        # 複数選択時のみ有効化、検出デバイスが 1 個以下なら行ごと非表示。
        self._device_vars: dict[str, tk.BooleanVar] = {}
        self._devices: list[LlamaDevice] = []
        self._device_fallback_var: tk.StringVar | None = None
        self._pending_probe: DeviceProbe | None = None
        r = 8
        if _HAS_DEVICES:
            lbl("デバイス", r, 0)
            self._device_frame = tk.Frame(card, bg=self.BG2)
            self._device_frame.grid(row=r, column=1, columnspan=2, sticky="ew",
                                    padx=(0, 6), pady=(6, 0))
            tk.Label(self._device_frame, text="未検出(🔍 検出 を押してください)",
                     fg=self.FG2, bg=self.BG2,
                     font=("monospace", 9), anchor="w").pack(anchor="w")
            self._device_detect_btn = tk.Button(
                card, text="🔍 検出", fg=self.FG, bg=self.BG3,
                activebackground=self.BG2, activeforeground=self.FG,
                relief="flat", bd=0, padx=6, pady=3, cursor="hand2",
                font=("sans-serif", 9), command=self._detect_devices)
            self._device_detect_btn.grid(row=r, column=3, sticky="new",
                                         padx=(0, 10), pady=(6, 0))
            r += 1

            self._tsplit_lbl = tk.Label(card, text="tensor-split", fg=self.FG2,
                                        bg=self.BG2, font=("sans-serif", 9))
            self._tsplit_lbl.grid(row=r, column=0, sticky="w",
                                  padx=(10, 4), pady=(6, 0))
            self._tsplit_var = tk.StringVar(value="")
            self._tsplit_entry = ttk.Entry(card, textvariable=self._tsplit_var,
                                           state="disabled")
            self._tsplit_entry.grid(row=r, column=1, columnspan=2, sticky="ew",
                                    padx=(0, 6), pady=(6, 0))
            self._tsplit_btn = tk.Button(
                card, text="⚙ 自動", fg=self.FG, bg=self.BG3,
                activebackground=self.BG2, activeforeground=self.FG,
                relief="flat", bd=0, padx=6, pady=3, cursor="hand2",
                font=("sans-serif", 9), command=self._suggest_tsplit,
                state="disabled")
            self._tsplit_btn.grid(row=r, column=3, sticky="ew",
                                  padx=(0, 10), pady=(6, 0))
            self._tsplit_row = r
            # 起動時は検出前なので tensor-split 行を隠しておく。
            self._set_tsplit_row_visible(False)
            r += 1

        lbl("追加オプション", r, 0)
        self._extra_var = tk.StringVar(value="-ngl 99")
        ttk.Entry(card, textvariable=self._extra_var).grid(
            row=r, column=1, columnspan=2, sticky="ew",
            padx=(0, 6), pady=(6, 0))
        tk.Button(card, text="⚙ 推奨値", fg=self.FG, bg=self.BG3,
                  activebackground=self.BG2, activeforeground=self.FG,
                  relief="flat", bd=0, padx=6, pady=3, cursor="hand2",
                  font=("sans-serif", 9),
                  command=self._suggest_options).grid(
            row=r, column=3, sticky="ew", padx=(0, 10), pady=(6, 0))
        r += 1

        # 起動ボタン
        _btn_wrap = tk.Frame(card, bg=self.ACCENT, bd=0)
        _btn_wrap.grid(row=r, column=0, columnspan=4, sticky="ew", padx=10, pady=8)
        self._launch_btn = tk.Button(
            _btn_wrap, text="▶ llama.cpp / vllm / mlx 起動",
            fg="white", bg=self.ACCENT,
            activebackground="#4f46e5", activeforeground="white",
            disabledforeground=self.FG2,
            relief="flat", bd=0, padx=10, pady=7,
            highlightthickness=0, highlightbackground=self.ACCENT,
            font=("sans-serif", 11, "bold"),
            cursor="hand2",
            command=self._do_launch,
        )
        self._launch_btn.pack(fill="both", expand=True)
        r += 1

        # ベンチスイープを開くボタン(_HAS_DEVICES のときのみ)
        if _HAS_DEVICES:
            tk.Button(
                card, text="📊 ベンチスイープ", fg=self.FG, bg=self.BG3,
                activebackground=self.BG2, activeforeground=self.FG,
                relief="flat", bd=0, padx=8, pady=4, cursor="hand2",
                font=("sans-serif", 10), command=self._open_sweep).grid(
                row=r, column=0, columnspan=4, sticky="ew", padx=10, pady=(0, 4))
            r += 1

        self._launch_err_var = tk.StringVar(value="")
        tk.Label(card, textvariable=self._launch_err_var,
                 fg=self.RED, bg=self.BG2,
                 font=("sans-serif", 9), anchor="w", justify="left",
                 wraplength=400).grid(row=r, column=0, columnspan=4,
                                      sticky="ew", padx=10, pady=(0, 6))

        # アニメーション用(Progressbar 非使用)
        self._launch_anim_proc_id: str | None = None

        self.after(200, self._update_binary_hint)
        self.after(200, self._populate_profiles)

    # ── Device selection ─────────────────────────────────────────────────────

    def _set_tsplit_row_visible(self, visible: bool) -> None:
        """Show/hide the tensor-split row (label + entry + auto button).

        Hidden when 1 or fewer devices are detected (Mac Metal 単体・単基 CUDA・
        Windows 単 GPU): tensor-split is meaningless there, so the widgets are
        removed with ``grid_remove`` to avoid any accidental input.
        """
        if not _HAS_DEVICES:
            return
        widgets = (self._tsplit_lbl, self._tsplit_entry, self._tsplit_btn)
        if visible:
            self._tsplit_lbl.grid()
            self._tsplit_entry.grid()
            self._tsplit_btn.grid()
        else:
            for w in widgets:
                w.grid_remove()

    def _detect_devices(self) -> None:
        """Run ``--list-devices`` in a daemon thread; UI updates via the queue.

        The blocking subprocess call must never run on the Tk main thread (it
        would freeze the UI), so the probe is stored on ``self._pending_probe``
        and a ``LOG_KIND_DEVICES`` event is enqueued for the poll loop to
        render — the same thread→UI handoff pattern used by the launch
        worker. The event carries no proc_id (it belongs to no process); it
        used to abuse the proc_id slot as a ``"_DEVICES_"`` sentinel.
        """
        if not _HAS_DEVICES:
            return
        binary = _resolve_binary(self._backend_var.get(), self.cfg)
        self._set_status("デバイス検出中…")

        def work() -> None:
            probe = detect_llama_devices(binary)
            self._pending_probe = probe          # set BEFORE enqueuing marker
            self._log_queue.put((LOG_KIND_DEVICES, "", ""))

        threading.Thread(target=work, daemon=True).start()

    def _render_devices(self, probe: DeviceProbe | None) -> None:
        """Rebuild the device checkbox group (or manual-entry fallback)."""
        if not _HAS_DEVICES or probe is None:
            return
        for w in self._device_frame.winfo_children():
            w.destroy()
        self._device_vars.clear()
        self._devices = []
        self._device_fallback_var = None

        if not probe.ok:
            # Detection failed → comma-separated manual entry fallback.
            self._device_fallback_var = tk.StringVar()
            ttk.Entry(self._device_frame,
                      textvariable=self._device_fallback_var).pack(
                fill="x", expand=True)
            tk.Label(self._device_frame,
                     text="例: CUDA0,CUDA1(カンマ区切りの device id)",
                     fg=self.FG2, bg=self.BG2, font=("monospace", 8),
                     anchor="w").pack(anchor="w")
            self._set_tsplit_row_visible(False)
            self._set_status(f"デバイス検出失敗: {probe.error} — 手入力してください")
            return

        self._devices = list(probe.devices)
        for d in probe.devices:
            # 全デバイスを一覧表示するが、0 MiB デバイス(macOS の BLAS:
            # Accelerate 等)は VRAM を持たず選択不可 → チェックボックスを
            # disabled にし、_device_vars にも登録しない(選択対象外)。
            if d.total_mib <= 0:
                ttk.Checkbutton(
                    self._device_frame,
                    text=f"{d.id}  {d.name}  (VRAM なし — 選択不可)",
                    state="disabled").pack(anchor="w")
                continue
            v = tk.BooleanVar(value=False)
            self._device_vars[d.id] = v
            ttk.Checkbutton(
                self._device_frame,
                text=f"{d.id}  {d.name}  ({d.free_gb:g}/{d.total_gb:g} GB)",
                variable=v, command=self._suggest_tsplit).pack(anchor="w")
        # tensor-split は selectable(VRAM あり)が 2 台以上のときだけ意味を持つ。
        # Mac の MTL0+BLAS では selectable=1 枚 → 非表示のまま。
        self._set_tsplit_row_visible(len(selectable_devices(self._devices)) > 1)
        self._suggest_tsplit()
        self._set_status(f"デバイス {len(self._devices)} 個を検出")

    def _selected_devices(self) -> list[LlamaDevice]:
        # _device_vars には selectable なデバイスしか登録されない(0 MiB は
        # disabled で var 無し)ので、選択結果は必ず selectable。
        return [d for d in self._devices
                if self._device_vars.get(d.id) is not None
                and self._device_vars[d.id].get()]

    def _suggest_tsplit(self) -> None:
        """Enable + auto-fill tensor-split when 2+ same-backend devices tick.

        tensor-split は「同一バックエンド内の複数枚」でのみ意味を持つ。
        CUDA+Vulkan のようにバックエンドを跨いだ選択(同一物理 GPU の重複
        列挙を含む)では自動提案せず、ステータスに警告を出す(手入力は可)。
        """
        if not _HAS_DEVICES:
            return
        sel = self._selected_devices()
        multi = len(sel) > 1
        same_backend = len({backend_of(d.id) for d in sel}) <= 1
        # 2 枚以上選択されていれば欄は有効化(手入力の余地を残す)。
        self._tsplit_entry.configure(state="normal" if multi else "disabled")
        self._tsplit_btn.configure(state="normal" if multi else "disabled")
        if multi and same_backend:
            split = suggest_tensor_split(sel, by="total")
            self._tsplit_var.set(",".join(f"{x:g}" for x in split))
        elif multi:
            # バックエンド跨ぎ — 自動提案しない(既存の手入力は保持)。
            self._set_status(
                "⚠ tensor-split: バックエンド跨ぎの選択は自動提案しません(手動指定は可)")
        else:
            self._tsplit_var.set("")   # 単一/0 選択は split 不要

    def _current_selection(self) -> DeviceSelection:
        """Collect the current device selection from the launch form."""
        checked = [d.id for d in self._selected_devices()]
        fallback = (self._device_fallback_var.get()
                    if self._device_fallback_var is not None else "")
        tsplit = self._tsplit_var.get() if hasattr(self, "_tsplit_var") else ""
        return _selection_from_inputs(checked, fallback, tsplit)

    def _open_sweep(self) -> None:
        """Open the bench-sweep window (device configurations x llmbench)."""
        if not _HAS_DEVICES:
            return
        # H-14: 既存窓があれば前面化。無ければ生成して登録 (_on_close から
        # スイープ子を落とすため参照を保持する)。
        for win in self._sweep_windows:
            if win.winfo_exists():
                win.deiconify()
                win.lift()
                return
        win = SweepWindow(self)
        self._sweep_windows.add(win)

    def _on_backend_change(self, _: Any = None) -> None:
        # ★ デバイス id の名前空間はビルドごとに違う ("CUDA0" と "Vulkan0" は
        #   同じ GPU ではない)。バックエンド/バリアントを切り替えたら選択を
        #   破棄して検出やり直しにする。残したままだと --device CUDA0 が
        #   Vulkan ビルドに渡って起動失敗する (Web 版の onBackendChange と同じ)。
        self._reset_device_selection()
        self._update_binary_hint()
        self._populate_profiles()

    def _reset_device_selection(self) -> None:
        """デバイス検出結果と選択状態を破棄する(バックエンド切替時)。

        デバイス UI が構築されていない (_HAS_DEVICES False) 場合は何もしない。
        検出結果は破棄して「未検出」表示に戻し、再検出を促す。
        """
        if not _HAS_DEVICES:
            return
        self._device_vars.clear()
        self._devices = []
        self._device_fallback_var = None
        self._pending_probe = None
        if hasattr(self, "_tsplit_var"):
            self._tsplit_var.set("")
        frame = getattr(self, "_device_frame", None)
        if frame is not None:
            for w in frame.winfo_children():
                w.destroy()
            tk.Label(frame, text="未検出(🔍 検出 を押してください)",
                     fg=self.FG2, bg=self.BG2,
                     font=("monospace", 9), anchor="w").pack(anchor="w")
        self._set_tsplit_row_visible(False)

    def _update_binary_hint(self) -> None:
        """shutil.which() はメインスレッドをブロックするのでスレッドで実行する。"""
        backend = self._backend_var.get()
        binary = _resolve_binary(backend, self.cfg)
        bc = self.cfg.backends.get(backend)
        is_custom = bc is not None and bc.binary is not None

        # 暫定表示(スレッド完了前)
        self._binary_hint_var.set(f"{binary}  (確認中…)")
        self._binary_hint_lbl.configure(fg=self.FG2)

        def _check() -> None:
            found = _check_binary(binary)
            self.after(0, lambda: self._apply_binary_hint(
                backend, binary, found, is_custom))

        threading.Thread(target=_check, daemon=True).start()

    def _apply_binary_hint(self, backend: str, binary: str,
                           found: bool, is_custom: bool) -> None:
        label = "カスタム設定" if is_custom else "PATH"
        status = "✓ 利用可" if found else "✗ 見つかりません"
        self._binary_hint_var.set(f"{binary}  ({label} — {status})")
        self._binary_hint_lbl.configure(fg=self.GREEN if found else self.RED)

        self._launch_btn.config(
            state="normal" if found else "disabled",
            cursor="hand2" if found else "arrow",
        )
        if not found:
            self._set_launch_err(
                f"⚠ {binary} が見つかりません。\n"
                f"バックエンド ({backend}) をインストールするか、providers.yaml の\n"
                f"launcher.backends.{backend}.binary にフルパスを設定してください。"
            )
        else:
            self._set_launch_err("")

    def _populate_profiles(self) -> None:
        backend = self._backend_var.get()
        profiles = _profiles_for(self.cfg, backend)
        names = ["-- なし --"] + [p.name for p in profiles]
        self._profile_cb["values"] = names
        self._profile_var.set("-- なし --")
        self._profile_args_var.set("")

    def _on_profile_change(self, _: Any = None) -> None:
        backend = self._backend_var.get()
        profiles = _profiles_for(self.cfg, backend)
        sel = self._profile_var.get()
        matched = next((p for p in profiles if p.name == sel), None)
        if matched and matched.args:
            lines = []
            for k, v in matched.args.items():
                if isinstance(v, bool):
                    lines.append(k if v else f"# {k}")
                else:
                    lines.append(f"{k} {v}")
            self._profile_args_var.set("  ".join(lines))
        else:
            self._profile_args_var.set("")

    def _set_launch_err(self, msg: str) -> None:
        self._launch_err_var.set(msg)

    # ── Launch / Stop ────────────────────────────────────────────────────────

    def _do_launch(self) -> None:
        name = self._name_var.get().strip()
        port_str = self._port_var.get().strip()
        backend = self._backend_var.get()
        model_path = self._model_path_var.get().strip()
        extra = self._extra_var.get().strip()

        if not name:
            self._set_launch_err("名前を入力してください")
            return
        if not model_path:
            self._set_launch_err("モデルパスを入力してください (左のリストから選択か直接入力)")
            return
        if not port_str.isdigit() or not (1024 <= int(port_str) <= 65535):
            self._set_launch_err("ポートは 1024-65535 の数字で指定してください")
            return

        port = int(port_str)
        binary = _resolve_binary(backend, self.cfg)

        profile_args: dict[str, Any] = {}
        sel_profile = self._profile_var.get()
        if sel_profile != "-- なし --":
            profs = _profiles_for(self.cfg, backend)
            matched = next((p for p in profs if p.name == sel_profile), None)
            if matched:
                profile_args = matched.args

        # MTP / speculative-decoding resolution (llama.cpp only; no-op for
        # other backends). Skipped entirely if the coderouter package is not
        # importable (standalone GUI use).
        spec_tokens: list[str] = []
        spec_notes: list[str] = []
        if _HAS_SPECULATIVE and resolve_speculative is not None:
            draft_path = self._draft_path_var.get().strip() or None
            mtp_mode = "auto" if self._mtp_auto_var.get() else "off"
            user_tokens: list[str] = []
            for flag, val in profile_args.items():
                if isinstance(val, bool):
                    if val:
                        user_tokens.append(str(flag))
                else:
                    user_tokens.extend([str(flag), str(val)])
            if extra.strip():
                with contextlib.suppress(ValueError):
                    user_tokens += shlex.split(extra)
            try:
                spec_tokens, spec_notes = resolve_speculative(
                    backend, model_path, draft_path, mtp_mode, user_tokens)
            except ValueError as e:
                self._set_launch_err(str(e))
                return

        # Device selection (llama.cpp only). When nothing is selected
        # device_args is None → _build_cmd emits the exact current argv
        # (complete backward compatibility — no --device is ever added).
        device_args: list[str] | None = None
        if _HAS_DEVICES and base_backend(backend) == "llama.cpp":
            device_args = self._current_selection().to_cli_args() or None

        try:
            cmd = _build_cmd(backend, model_path, port, profile_args, extra,
                             binary, spec_tokens, device_args)
        except ValueError as e:
            self._set_launch_err(str(e))
            return

        # MTP auto-fallback: only auto-detected speculative flags (MTP自動検出
        # ON, no explicit draft entry, detection emitted flags) qualify for the
        # one-shot startup-crash retry. Rebuild the command without the spec
        # tokens (exact — never spliced) so the retry is precise.
        spec_auto = bool(
            self._mtp_auto_var.get()
            and not self._draft_path_var.get().strip()
            and spec_tokens
        )
        fallback_cmd: list[str] | None = None
        if spec_auto:
            with contextlib.suppress(ValueError):
                fallback_cmd = _build_cmd(backend, model_path, port,
                                          profile_args, extra, binary, None,
                                          device_args)
        if fallback_cmd is None:
            spec_auto = False

        proc_id = uuid.uuid4().hex[:8]
        mp = ManagedProcess(
            id=proc_id,
            name=name,
            backend=backend,
            model_name=Path(model_path).name,
            port=port,
            cmd=cmd,
            status="starting",
        )

        self.processes[proc_id] = mp
        self._refresh_process_table()
        self._select_process(proc_id)
        self._set_launch_err("")
        self._set_status(f"起動中: {name}…")

        # ボタンアニメーション開始(Progressbar 非使用)
        self._launch_anim_proc_id = proc_id
        self._launch_btn.configure(state="disabled", cursor="arrow")
        self._launch_anim_tick(proc_id, 0)

        def _spawn_readiness_worker() -> None:
            """Kick off (or re-kick after a respawn) the readiness poller."""
            threading.Thread(
                target=_readiness_worker,
                args=(mp, self.cfg, self._log_queue, mp.spawn_gen),
                daemon=True,
            ).start()

        def _run() -> None:
            mp.log_lines.append(f"[launcher] cmd: {' '.join(cmd)}")
            for note in spec_notes:
                mp.log_lines.append(f"[launcher] {note}")
            run_cmd = cmd
            fallback_done = False
            first = True
            while True:
                try:
                    p = subprocess.Popen(
                        run_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        bufsize=0,
                    )
                except Exception as exc:
                    mp.status = "error"
                    self._log_queue.put((LOG_KIND_ERROR, proc_id, str(exc)))
                    return

                started_at = time.monotonic()
                mp.cmd = run_cmd
                mp.proc  = p
                mp.pid   = p.pid
                mp.returncode = None
                mp.started_at = started_at
                mp.spawn_gen += 1
                # H2 (readiness gating, ported from launcher_routes.py): the
                # status is "loading", not "running", until the readiness
                # worker below confirms the backend is actually serving —
                # registering/declaring success before the model finishes
                # loading is exactly the bug this closes.
                mp.status = "loading"
                _spawn_readiness_worker()
                if first:
                    self._log_queue.put(
                        (LOG_KIND_SPAWNED, proc_id, f"{name}:{port}"))
                    first = False

                assert p.stdout
                stdout = p.stdout
                for raw in iter(lambda s=stdout: s.read(4096), b""):
                    for line in raw.decode("utf-8", errors="replace").splitlines():
                        # Raw child output: ALWAYS a plain log event. Never
                        # inspected for control markers (H-13) — llama-server
                        # dumps GGUF metadata verbatim and may well emit a
                        # line starting with "_ERR_:" or "_SPAWNED_:".
                        self._log_queue.put((LOG_KIND_LOG, proc_id, line))
                p.wait()
                mp.returncode = p.returncode
                mp.pid = None
                mp.status = _exit_status(p.returncode, mp.stopping)
                self._log_queue.put((
                    LOG_KIND_LOG, proc_id,
                    f"[launcher] exited (code {p.returncode})"))

                if mp.stopping:
                    # Intentional stop (_do_stop / _do_kill). Never a crash
                    # to heal, regardless of the exit code SIGTERM/SIGKILL
                    # produced, and never eligible for MTP fallback either.
                    return

                # MTP startup crash → relaunch ONCE without speculative flags.
                if (
                    not fallback_done
                    and spec_auto
                    and p.returncode not in (0, None)
                    and (time.monotonic() - started_at)
                    <= _MTP_FALLBACK_WINDOW_SECS
                ):
                    fallback_done = True
                    self._log_queue.put((
                        LOG_KIND_LOG,
                        proc_id,
                        f"[launcher] MTP startup failure detected "
                        f"(exit code {p.returncode}); retrying without "
                        "speculative decoding",
                    ))
                    self._log_queue.put((
                        LOG_KIND_LOG, proc_id,
                        f"[launcher] cmd: {' '.join(fallback_cmd)}"))
                    run_cmd = fallback_cmd
                    continue

                # Generic auto-restart (opt-in — see LauncherConfig.auto_restart).
                if p.returncode not in (0, None):
                    plan = plan_auto_restart(
                        auto_restart=self.cfg.auto_restart,
                        restart_count=mp.restart_count,
                        max_attempts=self.cfg.auto_restart_max_attempts,
                        backoff_s=self.cfg.auto_restart_backoff_s,
                        backoff_max_s=self.cfg.auto_restart_backoff_max_s,
                        has_cmd=bool(run_cmd),
                    )
                    for ln in plan.log_lines:
                        self._log_queue.put((LOG_KIND_LOG, proc_id, ln))
                    if plan.should_restart:
                        mp.restart_count += 1
                        time.sleep(plan.backoff_s)
                        if mp.stopping:
                            # Stopped while waiting out the backoff — respect it.
                            return
                        continue  # run_cmd unchanged — relaunch same argv
                return

        threading.Thread(target=_run, daemon=True).start()

    def _do_stop(self) -> None:
        pid = self.selected_proc_id
        if not pid or pid not in self.processes:
            return
        mp = self.processes[pid]
        if _proc_alive(mp):
            # Set BEFORE signalling — tells the launch worker thread (and any
            # in-flight readiness worker) this exit was requested, so neither
            # auto-restarts nor mislabels it "error" (mirrors
            # launcher_routes.stop_process setting proc.stopping = True
            # first).
            mp.stopping = True
            mp.status = "stopping"
            with contextlib.suppress(Exception):
                mp.proc.terminate()
            mp.log_lines.append("[launcher] SIGTERM sent")
            self._refresh_process_table()

    def _do_kill(self) -> None:
        pid = self.selected_proc_id
        if not pid or pid not in self.processes:
            return
        mp = self.processes[pid]
        if _proc_alive(mp):
            mp.stopping = True  # same rationale as _do_stop
            with contextlib.suppress(Exception):
                mp.proc.kill()
            mp.log_lines.append("[launcher] SIGKILL sent")
            self._refresh_process_table()

    def _do_remove(self) -> None:
        pid = self.selected_proc_id
        if not pid or pid not in self.processes:
            return
        mp = self.processes[pid]
        # 生存判定は status ではなく poll() で行う — readiness タイムアウト後は
        # status="error" のままOSプロセスが生きているため、status ベースの
        # 判定では kill されずにオーファン化する(_proc_alive の docstring 参照)。
        if _proc_alive(mp):
            if not messagebox.askyesno("確認", f"{mp.name} は実行中です。強制終了して削除しますか?"):
                return
            _kill_for_removal(mp)
        del self.processes[pid]
        self.selected_proc_id = None
        self._refresh_process_table()
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")
        self._log_title_var.set("ログ")

    # ── Process table ─────────────────────────────────────────────────────────

    def _build_process_panel(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=1, column=0, sticky="nsew",
                          padx=0, pady=(0, 6))
        card.rowconfigure(1, weight=1)
        card.columnconfigure(0, weight=1)

        hdr = tk.Frame(card, bg=self.BG2)
        hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))
        tk.Label(hdr, text="PROCESSES", fg=self.FG2, bg=self.BG2,
                 font=("sans-serif", 9, "bold")).pack(side="left")
        for label, cmd in [("■ 停止", self._do_stop),
                            ("✕ 削除", self._do_remove)]:
            tk.Button(hdr, text=label, fg=self.FG, bg=self.BG3,
                      relief="flat", bd=0, padx=6, pady=2, cursor="hand2",
                      command=cmd).pack(side="right", padx=2)

        cols = ("name", "backend", "model", "port", "pid", "status")
        self._proc_tree = ttk.Treeview(
            card, columns=cols, show="headings",
            selectmode="browse", height=5,
        )
        for col, w, label in [
            ("name",    120, "NAME"),
            ("backend",  80, "BACKEND"),
            ("model",   200, "MODEL"),
            ("port",     60, "PORT"),
            ("pid",      60, "PID"),
            ("status",   80, "STATUS"),
        ]:
            self._proc_tree.heading(col, text=label)
            self._proc_tree.column(col, width=w, minwidth=40, anchor="w")

        vsb = ttk.Scrollbar(card, orient="vertical",
                            command=self._proc_tree.yview)
        self._proc_tree.configure(yscrollcommand=vsb.set)
        self._proc_tree.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=(0, 6))
        vsb.grid(row=1, column=1, sticky="ns", pady=(0, 6), padx=(0, 4))

        self._proc_tree.bind("<<TreeviewSelect>>", self._on_proc_select)
        self._proc_tree.tag_configure("running",  foreground=self.GREEN,  background=self.BG2)
        self._proc_tree.tag_configure("starting", foreground=self.YELLOW, background=self.BG2)
        self._proc_tree.tag_configure("loading",  foreground=self.YELLOW, background=self.BG2)
        self._proc_tree.tag_configure("stopping", foreground=self.YELLOW, background=self.BG2)
        self._proc_tree.tag_configure("stopped",  foreground=self.FG2,    background=self.BG2)
        self._proc_tree.tag_configure("error",    foreground=self.RED,    background=self.BG2)

    def _refresh_process_table(self) -> None:
        sel_id = self.selected_proc_id
        self._proc_tree.delete(*self._proc_tree.get_children())
        for mp in self.processes.values():
            pid_str = str(mp.pid) if mp.pid else "—"
            try:
                self._proc_tree.insert(
                    "", "end", iid=mp.id,
                    values=(mp.name, mp.backend, mp.model_name,
                            mp.port, pid_str, mp.status),
                    tags=(mp.status,),
                )
            except Exception as e:
                print(f"[DEBUG] insert ERROR: {e}", flush=True)
        if sel_id and sel_id in self.processes:
            try:
                self._proc_tree.selection_set(sel_id)
                self._proc_tree.see(sel_id)
            except Exception:
                pass

    def _on_proc_select(self, _: Any = None) -> None:
        sel = self._proc_tree.selection()
        if not sel:
            return
        pid = sel[0]
        # ★ 無限ループ防止ガード:
        # _select_process() 内の selection_set() は、選択が変わらなくても
        # <<TreeviewSelect>> を再発火する。ガードが無いと
        #   _on_proc_select → _select_process → selection_set →
        #   <<TreeviewSelect>> → _on_proc_select → … が無限再帰し GUI が固まる。
        # 既に選択中の ID なら何もしないことで再帰を断ち切る。
        if pid == self.selected_proc_id:
            return
        self._select_process(pid)

    def _select_process(self, proc_id: str) -> None:
        self.selected_proc_id = proc_id
        with contextlib.suppress(Exception):
            self._proc_tree.selection_set(proc_id)
        self._refresh_log_view()

    # ── Log viewer ────────────────────────────────────────────────────────────

    def _build_log_panel(self, parent: ttk.Frame) -> None:
        card = self._card(parent, row=2, column=0, sticky="ew",
                          padx=0, pady=0)
        card.rowconfigure(1, weight=1)
        card.columnconfigure(0, weight=1)
        card.configure(height=160)

        hdr = tk.Frame(card, bg=self.BG2)
        hdr.grid(row=0, column=0, columnspan=2, sticky="ew",
                 padx=10, pady=(8, 4))
        self._log_title_var = tk.StringVar(value="ログ")
        tk.Label(hdr, textvariable=self._log_title_var,
                 fg=self.FG2, bg=self.BG2,
                 font=("sans-serif", 9, "bold")).pack(side="left")
        tk.Button(hdr, text="クリア", fg=self.FG, bg=self.BG3,
                  relief="flat", bd=0, padx=6, pady=2, cursor="hand2",
                  command=self._clear_log).pack(side="right")

        self._log_text = tk.Text(
            card, bg="#020617", fg=self.FG2,
            font=("monospace", 9), relief="flat", bd=0,
            state="disabled", wrap="none", height=8,
            insertbackground=self.FG,
        )
        vsb = ttk.Scrollbar(card, orient="vertical",
                            command=self._log_text.yview)
        hsb = ttk.Scrollbar(card, orient="horizontal",
                            command=self._log_text.xview)
        self._log_text.configure(yscrollcommand=vsb.set,
                                  xscrollcommand=hsb.set)
        self._log_text.grid(row=1, column=0, sticky="nsew",
                            padx=(6, 0), pady=(0, 0))
        vsb.grid(row=1, column=1, sticky="ns", padx=(0, 4))
        hsb.grid(row=2, column=0, sticky="ew", padx=(6, 0), pady=(0, 4))

    def _refresh_log_view(self) -> None:
        pid = self.selected_proc_id
        if not pid or pid not in self.processes:
            return
        mp = self.processes[pid]
        self._log_title_var.set(f"ログ — {mp.name} (PID {mp.pid or '—'})")
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        # deque はスライス不可のため list 化してから末尾 400 行を取得
        for line in list(mp.log_lines)[-400:]:
            self._log_text.insert("end", line + "\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        pid = self.selected_proc_id
        if pid and pid in self.processes:
            self.processes[pid].log_lines.clear()
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    # ── Polling ──────────────────────────────────────────────────────────────

    def _poll(self) -> None:
        backlog = False
        try:
            backlog = self._poll_impl()
        except Exception as e:
            print(f"[DEBUG] _poll EXCEPTION: {e}", flush=True)
            import traceback
            traceback.print_exc()
        finally:
            # バックログが残っていれば短間隔で再開し、
            # UI に制御を返しつつ素早く追従する
            self.after(50 if backlog else 1000, self._poll)

    # ── ログキューのイベント処理 (H-13: kind によるディスパッチ) ─────────

    def _reset_launch_button(self) -> None:
        """起動ボタンを既定状態に戻し、起動アニメーションを止める。"""
        self._launch_anim_proc_id = None   # _launch_anim_tick が次回自動停止
        self._launch_btn.configure(
            text="▶ llama.cpp / vllm / mlx 起動", state="normal", cursor="hand2"
        )

    def _apply_cr_event(self, kind: str, payload: str) -> None:
        """タグ付き CodeRouter キューイベントを1件適用する。

        Dispatch is on ``kind`` alone — the payload is never pattern-matched,
        so CodeRouter's own stdout can no longer forge a state transition by
        printing a line that happens to start with ``_CR_EXIT_:`` (H-13).
        """
        if kind == CR_KIND_OK:
            self._cr_status = "running"
            self._cr_log.append(f"[coderouter] 起動しました (PID {payload})")
            self._update_cr_ui()
            self._set_status(f"CodeRouter 稼働中 (PID {payload})")

        elif kind == CR_KIND_ERROR:
            self._cr_status = "error"
            self._cr_log.append(f"[coderouter] 起動エラー: {payload}")
            self._cr_err_var.set(f"CodeRouter 起動失敗: {payload}")
            self._update_cr_ui()

        elif kind == CR_KIND_EXIT:
            self._cr_status = "stopped" if payload == "0" else "error"
            self._cr_proc = None
            self._cr_log.append(f"[coderouter] 終了 (code {payload})")
            self._update_cr_ui()

        else:
            # CR_KIND_LOG — and any unrecognised kind, which is treated as
            # inert output rather than being interpreted.
            self._cr_log.append(payload)

    def _apply_log_event(
        self, kind: str, proc_id: str, payload: str
    ) -> str | None:
        """タグ付きバックエンドログキューイベントを1件適用する。

        Returns the line to append to the visible log pane (only when it
        belongs to the selected process), or ``None``.

        Dispatch is on ``kind`` alone. Raw child stdout always arrives as
        ``LOG_KIND_LOG`` and therefore can never delete a live process from
        ``self.processes`` or move the UI into a "ready"/"spawned" state, no
        matter what the model's metadata happens to print (H-13).
        """
        # デバイス検出スレッドからの通知(実データは self._pending_probe)。
        if kind == LOG_KIND_DEVICES:
            self._render_devices(self._pending_probe)
            return None

        mp = self.processes.get(proc_id)
        if mp is None:
            return None

        if kind == LOG_KIND_SPAWNED:
            # OS プロセスの起動に成功した段階(readiness 未確認)。
            # フォームは次の起動へ空けるが、mp.status は "loading" の
            # ままで、実際に "稼働中" になるのは ready イベント受信時。
            parsed = parse_name_port(payload)
            if parsed is None:
                mp.log_lines.append(
                    f"[launcher] 不正な spawned イベントを無視しました: {payload!r}")
                return None
            pname, pport = parsed
            self._set_status(f"読み込み中: {pname} (PID {mp.pid})")
            self._port_var.set(str(pport + 1))
            self._name_var.set("")
            self._reset_launch_button()
            return None

        if kind == LOG_KIND_READY:
            # readiness probe が通り、mp.status は既に "running"。
            parsed = parse_name_port(payload)
            if parsed is None:
                mp.log_lines.append(
                    f"[launcher] 不正な ready イベントを無視しました: {payload!r}")
                return None
            pname, _pport = parsed
            if proc_id == self.selected_proc_id:
                self._set_status(f"稼働中: {pname} (PID {mp.pid})")
            return None

        if kind == LOG_KIND_ERROR:
            del self.processes[proc_id]
            self._set_launch_err(f"起動エラー: {payload}")
            self._set_status("起動失敗")
            self._reset_launch_button()
            return None

        # LOG_KIND_LOG — and any unrecognised kind, which is treated as inert
        # output rather than being interpreted.
        mp.log_lines.append(payload)
        return payload if proc_id == self.selected_proc_id else None

    def _poll_impl(self) -> bool:
        """キューを処理して UI を更新する。

        Returns:
            backlog — 1ティック上限に達し、キューに未処理が残った場合 True。
        """
        changed = False
        pending_log_lines: list[str] = []

        # ── CodeRouter ログキュー処理 ─────────────────────────────────────
        cr_processed = 0
        while cr_processed < _MAX_LINES_PER_TICK:
            try:
                cr_kind, cr_payload = self._cr_log_queue.get_nowait()
            except queue.Empty:
                break
            cr_processed += 1
            self._apply_cr_event(cr_kind, cr_payload)

        # ── llama.cpp / vllm ログキュー処理 ──────────────────────────────
        lc_processed = 0
        while lc_processed < _MAX_LINES_PER_TICK:
            try:
                kind, proc_id, payload = self._log_queue.get_nowait()
            except queue.Empty:
                break
            lc_processed += 1
            changed = True
            shown = self._apply_log_event(kind, proc_id, payload)
            if shown is not None:
                pending_log_lines.append(shown)

        # ログをまとめて1回だけ書き込む(行ごとに configure するとUI固まる)
        if pending_log_lines:
            self._log_text.configure(state="normal")
            self._log_text.insert("end", "\n".join(pending_log_lines) + "\n")
            # ウィジェットが無制限に伸びると insert/描画が遅くなり UI が固まる。
            # 末尾 _MAX_TEXT_LINES 行のみ残して古い行を削除する。
            line_count = int(self._log_text.index("end-1c").split(".")[0])
            if line_count > _MAX_TEXT_LINES:
                self._log_text.delete(
                    "1.0", f"{line_count - _MAX_TEXT_LINES}.0")
            self._log_text.see("end")
            self._log_text.configure(state="disabled")

        # プロセス終了チェック
        for mp in list(self.processes.values()):
            if mp.proc and mp.status in ("running", "starting", "loading"):
                rc = mp.proc.poll()
                if rc is not None:
                    mp.returncode = rc
                    mp.status = _exit_status(rc, mp.stopping)
                    changed = True

        if changed:
            self._refresh_process_table()

        # 1ティック上限に達した場合はキューに未処理が残っている可能性が高い。
        # backlog=True を返し、_poll 側で短間隔の再ポーリングへ切り替える。
        backlog = (cr_processed >= _MAX_LINES_PER_TICK
                   or lc_processed >= _MAX_LINES_PER_TICK)
        return backlog

    # ── Misc ─────────────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        self._status_var.set(msg)


# ---------------------------------------------------------------------------
# Bench sweep — worker thread + window
#
# Runs a series of device configurations back-to-back:
#   起動 (llama-server) → readiness 待ち (既存 poll_until_ready 再利用)
#   → 外部ベンチ実行 (llmbench) → サーバー停止 → 次の構成
# The worker is a plain threading.Thread and depends only on module-level
# functions + the frozen launcher_devices logic, so it is unit-testable with
# injected fakes (no live Tk app, no real subprocesses) — mirroring the
# readiness/auto-restart split elsewhere in this module.
#
# The sweep queue was already kind-tagged; the constants below just name the
# vocabulary so it lines up with LOG_KIND_* / CR_KIND_* above (the devices
# event lost its stray "_" prefix in the process — H-13).
# ---------------------------------------------------------------------------

SWEEP_KIND_STEP = "step"
SWEEP_KIND_LOG = "log"
SWEEP_KIND_DEVICES = "devices"
SWEEP_KIND_DONE = "done"

# H-14: スイープ窓の閉じ / アプリ終了時に子プロセスを落とす際のタイミング。
_SWEEP_CLOSE_GRACE_MS = 8000       # SIGTERM 後、SIGKILL するまでの猶予
_SWEEP_CLOSE_TICK_MS = 100         # ワーカー終了待ちの after ポーリング間隔
_SWEEP_APP_CLOSE_JOIN_S = 3.0      # アプリ終了経路のみ許される同期 join の上限


class _SweepWorker(threading.Thread):
    """Drive a :class:`SweepPlan` step-by-step in a background thread.

    Results are pushed to ``out_queue`` as ``(SWEEP_KIND_STEP, SweepStep)`` /
    ``(SWEEP_KIND_LOG, str)`` / ``(SWEEP_KIND_DONE, None)`` tuples for the
    window's poll loop to consume. ``popen`` / ``poll_ready`` /
    ``backend_ready`` are injectable so tests can drive the state machine
    deterministically without spawning anything.
    """

    def __init__(
        self,
        plan: SweepPlan,
        cfg: LauncherConfig,
        out_queue: queue.Queue[tuple[str, Any]],
        abort_event: threading.Event,
        *,
        runs: int = 1,
        popen: Callable[..., Any] = subprocess.Popen,
        poll_ready: Callable[..., str] = poll_until_ready,
        backend_ready: Callable[..., bool] = _backend_ready,
    ) -> None:
        super().__init__(daemon=True)
        self.plan = plan
        self.cfg = cfg
        self.queue = out_queue
        self._abort = abort_event
        self.runs = runs
        self._popen = popen
        self._poll_ready = poll_ready
        self._backend_ready = backend_ready
        # H-14: ロック付き生存台帳。スイープ子は app.processes に載らないので、
        # ここで直接掴んで _on_close / 窓閉じ経路から Tk 非依存に落とす。
        self._live_lock = threading.Lock()
        self._live: list[Any] = []

    # ── H-14: live-child ledger (Tk-independent) ─────────────────────────
    def _track(self, proc: Any) -> None:
        with self._live_lock:
            self._live.append(proc)

    def _untrack(self, proc: Any) -> None:
        with self._live_lock, contextlib.suppress(ValueError):
            self._live.remove(proc)

    def live_procs(self) -> list[Any]:
        with self._live_lock:
            return list(self._live)

    def request_stop(self) -> None:
        """Abort + SIGTERM every live child, non-blocking (safe from Tk main thread)."""
        self._abort.set()
        for proc in self.live_procs():
            with contextlib.suppress(Exception):
                if proc.poll() is None:
                    proc.terminate()

    def force_kill(self) -> None:
        """Last resort: SIGKILL every still-live child. Non-blocking."""
        for proc in self.live_procs():
            with contextlib.suppress(Exception):
                if proc.poll() is None:
                    proc.kill()

    # ── emit helpers ─────────────────────────────────────────────────────
    def _emit_step(self, step: SweepStep) -> None:
        self.queue.put((SWEEP_KIND_STEP, step))

    def _emit_log(self, line: str) -> None:
        self.queue.put((SWEEP_KIND_LOG, line))

    def run(self) -> None:
        try:
            for step in self.plan.steps:
                if self._abort.is_set():
                    step.state = SweepState.ABORTED
                    self._emit_step(step)
                    continue
                self._run_one(step)
        finally:
            self.queue.put((SWEEP_KIND_DONE, None))

    def _pump_logs(self, proc: Any, label: str) -> None:
        """Drain a child's stdout into the log queue in a daemon thread.

        Tolerant of fakes with no ``stdout`` (tests) — simply does nothing.
        """
        stdout = getattr(proc, "stdout", None)
        if stdout is None:
            return

        def _drain() -> None:
            with contextlib.suppress(Exception):
                for raw in iter(lambda: stdout.read(4096), b""):
                    for ln in raw.decode("utf-8", errors="replace").splitlines():
                        self._emit_log(f"[{label}] {ln}")

        threading.Thread(target=_drain, daemon=True).start()

    def _await_ready(self, proc: Any) -> str:
        return self._poll_ready(
            check=lambda: self._backend_ready(
                "llama.cpp", self.plan.port,
                probe_timeout_s=_READINESS_PROBE_TIMEOUT_S),
            should_abort=lambda: self._abort.is_set() or proc.poll() is not None,
            timeout_s=self.cfg.bench_readiness_timeout_s,
            poll_interval_s=self.cfg.readiness_poll_interval_s,
        )

    def _terminate(self, proc: Any) -> None:
        """SIGTERM → wait → SIGKILL. Ensures the port is freed before the next
        configuration reuses it."""
        try:
            with contextlib.suppress(Exception):
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    with contextlib.suppress(Exception):
                        proc.wait(timeout=5)
        finally:
            # H-14: 終了経路がどれでも台帳から外す (二重 kill を避ける)。
            self._untrack(proc)

    def _run_one(self, step: SweepStep) -> None:
        # ── サーバー起動 ──
        step.state = SweepState.STARTING
        self._emit_step(step)
        binary = _resolve_binary("llama.cpp", self.cfg)
        cmd = _build_cmd(
            "llama.cpp", self.plan.model_path, self.plan.port,
            self.plan.options, self.plan.extra_args, binary,
            None, step.selection.to_cli_args() or None)
        self._emit_log(f"[{step.label}] cmd: {' '.join(cmd)}")
        try:
            server = self._popen(
                cmd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0)
        except Exception as exc:
            step.state = SweepState.FAILED
            step.error = f"起動失敗: {exc}"
            self._emit_step(step)
            return
        self._track(server)  # H-14: 窓閉じ経路が掴めるよう即登録
        self._pump_logs(server, step.label)

        # ── readiness 待ち(既存 poll_until_ready 再利用) ──
        outcome = self._await_ready(server)
        if outcome != "ready":
            step.state = (SweepState.ABORTED if self._abort.is_set()
                          else SweepState.FAILED)
            step.error = f"readiness {outcome}"
            self._terminate(server)
            self._emit_step(step)
            return

        # ── 外部ベンチ実行 ──
        step.state = SweepState.BENCHING
        step.started_at = time.time()
        self._emit_step(step)
        bench_argv = render_bench_command(
            self.plan.bench_cmd_template, port=self.plan.port,
            config_label=step.label, results_dir=self.plan.results_dir,
            runs=self.runs)
        env = {**os.environ,
               "OPENAI_BASE_URL": f"http://localhost:{self.plan.port}/v1"}
        try:
            bench = self._popen(
                bench_argv, env=env, cwd=self.plan.results_dir or None,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        except Exception as exc:
            step.state = SweepState.FAILED
            step.error = f"ベンチ起動失敗: {exc}"
            self._terminate(server)
            self._emit_step(step)
            return
        self._track(bench)  # H-14
        self._pump_logs(bench, f"{step.label}/bench")
        bench.wait()
        self._untrack(bench)  # H-14: 自然終了したら台帳から外す
        step.bench_exit_code = bench.returncode
        step.ended_at = time.time()

        # bench の JSON results を best-effort で解析(比較用サマリ)。
        if self.plan.results_dir:
            with contextlib.suppress(Exception):
                step.results_path, step.summary = load_latest_results(
                    self.plan.results_dir, since=step.started_at)

        # ── サーバー停止(ポート解放を必ず待つ) ──
        self._terminate(server)
        # bench 非ゼロ終了は「失敗」ではなく exit code で判別(計測は完了扱い)。
        step.state = (SweepState.ABORTED if self._abort.is_set()
                      else SweepState.DONE)
        self._emit_step(step)


class SweepWindow(tk.Toplevel):
    """Bench-sweep window: pick device configurations, run llmbench per config."""

    def __init__(self, app: LauncherApp) -> None:
        super().__init__(app)
        self.app = app
        self.cfg = app.cfg
        self.title("ベンチスイープ")
        self.configure(bg=app.BG)
        self.geometry("760x620")

        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._worker: _SweepWorker | None = None
        self._abort = threading.Event()
        self._config_vars: list[tuple[tk.BooleanVar, str, DeviceSelection]] = []
        self._steps: list[SweepStep] = []
        # H-14: 閉じ中フラグ + ポーリング job id (破棄済みウィジェットへの
        # after 再スケジュールで TclError を出さないため)。
        self._closing = False
        self._poll_job: str | None = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self.after(300, self._poll)

    # ── UI ────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        app = self.app
        pad = {"padx": 10, "pady": 4}

        frm = tk.Frame(self, bg=app.BG)
        frm.pack(fill="x", **pad)
        frm.columnconfigure(1, weight=1)

        def _row(label: str, var: tk.StringVar, row: int, width: int = 0) -> None:
            tk.Label(frm, text=label, fg=app.FG2, bg=app.BG,
                     font=("sans-serif", 9)).grid(row=row, column=0, sticky="w")
            e = ttk.Entry(frm, textvariable=var)
            if width:
                e.configure(width=width)
                e.grid(row=row, column=1, sticky="w", pady=2)
            else:
                e.grid(row=row, column=1, sticky="ew", pady=2)

        self._model_var = tk.StringVar(value=app._model_path_var.get())
        _row("モデルパス", self._model_var, 0)
        self._port_var = tk.StringVar(value=str(self._safe_int(app._port_var.get(), 8080)))
        _row("ポート", self._port_var, 1, width=8)
        self._runs_var = tk.StringVar(value=str(self.cfg.bench_runs))
        _row("runs", self._runs_var, 2, width=8)
        self._results_var = tk.StringVar(value=self.cfg.bench_results_dir or "")
        _row("results_dir", self._results_var, 3)
        self._bench_var = tk.StringVar(value=self.cfg.bench_command_template)
        _row("ベンチコマンド", self._bench_var, 4)
        tk.Label(
            frm,
            text="{port} {config} {base_url} {results_dir} {runs} を置換。"
                 "Windows はスペース入り引数をダブルクォートで。",
            fg=app.FG2, bg=app.BG, font=("monospace", 8),
            anchor="w", justify="left").grid(row=5, column=0, columnspan=2,
                                             sticky="w")

        # 構成マトリクス(検出デバイスから自動生成)
        cfgf = tk.LabelFrame(self, text="構成(チェックで選択)", fg=app.FG2,
                             bg=app.BG, font=("sans-serif", 9))
        cfgf.pack(fill="x", **pad)
        self._config_frame = tk.Frame(cfgf, bg=app.BG)
        self._config_frame.pack(fill="x", padx=6, pady=4)
        tk.Button(cfgf, text="🔍 デバイス検出 → 構成生成", fg=app.FG, bg=app.BG3,
                  relief="flat", bd=0, padx=6, pady=3, cursor="hand2",
                  font=("sans-serif", 9),
                  command=self._detect_configs).pack(anchor="w", padx=6, pady=(0, 4))
        # 親フォームで既に検出済みならそのまま流用。
        if getattr(app, "_devices", None):
            self._render_configs(app._devices)

        # 操作ボタン
        btns = tk.Frame(self, bg=app.BG)
        btns.pack(fill="x", **pad)
        self._start_btn = tk.Button(
            btns, text="▶ 開始", fg="white", bg=app.ACCENT,
            activebackground="#4f46e5", activeforeground="white",
            relief="flat", bd=0, padx=10, pady=5, cursor="hand2",
            font=("sans-serif", 10, "bold"), command=self._start)
        self._start_btn.pack(side="left")
        self._abort_btn = tk.Button(
            btns, text="■ 中断", fg=app.FG, bg=app.BG3,
            relief="flat", bd=0, padx=8, pady=5, cursor="hand2",
            font=("sans-serif", 10), command=self._request_abort,
            state="disabled")
        self._abort_btn.pack(side="left", padx=(6, 0))
        self._sweep_status = tk.StringVar(value="")
        tk.Label(btns, textvariable=self._sweep_status, fg=app.FG2, bg=app.BG,
                 font=("sans-serif", 9)).pack(side="left", padx=(10, 0))

        # 進行テーブル
        cols = ("label", "state", "exit", "tok_s", "ttft")
        self._tree = ttk.Treeview(self, columns=cols, show="headings", height=8)
        for col, w, label in [
            ("label", 200, "構成"), ("state", 90, "状態"),
            ("exit", 70, "exit"), ("tok_s", 90, "tok/s"),
            ("ttft", 90, "ttft(ms)"),
        ]:
            self._tree.heading(col, text=label)
            self._tree.column(col, width=w, minwidth=40, anchor="w")
        self._tree.pack(fill="both", expand=True, padx=10, pady=(0, 4))

        # ログ
        self._log = tk.Text(self, bg="#020617", fg=app.FG2,
                            font=("monospace", 9), relief="flat", bd=0,
                            height=6, wrap="none", state="disabled")
        self._log.pack(fill="both", expand=False, padx=10, pady=(0, 8))

    @staticmethod
    def _safe_int(raw: str, default: int) -> int:
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return default

    # ── device detection / config generation ─────────────────────────────
    def _detect_configs(self) -> None:
        binary = _resolve_binary("llama.cpp", self.cfg)
        self._sweep_status.set("デバイス検出中…")

        def work() -> None:
            probe = detect_llama_devices(binary)
            self._queue.put((SWEEP_KIND_DEVICES, probe))

        threading.Thread(target=work, daemon=True).start()

    def _render_configs(self, devices: list[LlamaDevice]) -> None:
        for w in self._config_frame.winfo_children():
            w.destroy()
        self._config_vars = []
        labeled = _build_sweep_configs(devices)
        if not labeled:
            tk.Label(self._config_frame,
                     text="デバイス未検出 — 上のボタンで検出してください",
                     fg=self.app.FG2, bg=self.app.BG,
                     font=("monospace", 9)).pack(anchor="w")
            return
        for label, sel in labeled:
            v = tk.BooleanVar(value=True)
            desc = label
            if sel.tensor_split:
                desc += "  (" + ",".join(f"{x:g}" for x in sel.tensor_split) + ")"
            ttk.Checkbutton(self._config_frame, text=desc, variable=v).pack(
                anchor="w")
            self._config_vars.append((v, label, sel))

    def _selected_configs(self) -> list[tuple[str, DeviceSelection]]:
        return [(label, sel) for v, label, sel in self._config_vars if v.get()]

    # ── run control ───────────────────────────────────────────────────────
    def _start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        model_path = self._model_var.get().strip()
        if not model_path:
            self._sweep_status.set("モデルパスを入力してください")
            return
        port = self._safe_int(self._port_var.get(), 0)
        if not (1024 <= port <= 65535):
            self._sweep_status.set("ポートは 1024-65535 で指定してください")
            return
        # ポート衝突チェック(既存の起動プロセス + OS レベルの空き確認)。
        used = {mp.port for mp in self.app.processes.values()
                if _proc_alive(mp)}
        if port in used or not is_port_free(port):
            self._sweep_status.set(f"ポート {port} は使用中です。別のポートを指定してください")
            return
        labeled = self._selected_configs()
        if not labeled:
            self._sweep_status.set("構成を 1 つ以上選択してください")
            return

        runs = self._safe_int(self._runs_var.get(), self.cfg.bench_runs)
        results_dir = self._results_var.get().strip() or None
        plan = SweepPlan(
            steps=build_sweep_steps(labeled),
            model_path=model_path,
            backend="llama.cpp",
            port=port,
            bench_cmd_template=self._bench_var.get(),
            results_dir=results_dir,
        )
        self._steps = plan.steps
        self._refresh_table()

        self._abort.clear()
        self._worker = _SweepWorker(
            plan, self.cfg, self._queue, self._abort, runs=runs)
        self._worker.start()
        self._start_btn.configure(state="disabled")
        self._abort_btn.configure(state="normal")
        self._sweep_status.set("実行中…")

    def _request_abort(self) -> None:
        self._abort.set()
        self._sweep_status.set("中断要求 — 現在の構成の完了後に停止します")

    # ── H-14: window close / app-close shutdown ──────────────────────────
    def is_running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _on_close(self) -> None:
        """WM_DELETE_WINDOW ハンドラ。実行中ならメインスレッドをブロックせず、子へ
        SIGTERM を送って after ポーリングでワーカー終了を待つ。"""
        if not self.is_running():
            self._finish_close()
            return
        assert self._worker is not None
        self._worker.request_stop()
        self._closing = True
        with contextlib.suppress(Exception):
            self._sweep_status.set("停止中… (子プロセスの終了を待っています)")
            self._start_btn.configure(state="disabled")
            self._abort_btn.configure(state="disabled")
        self.after(_SWEEP_CLOSE_TICK_MS, self._await_worker_exit, 0)

    def _await_worker_exit(self, waited_ms: int) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._finish_close()
            return
        if waited_ms >= _SWEEP_CLOSE_GRACE_MS:
            self._worker.force_kill()
            self._finish_close()
            return
        self.after(_SWEEP_CLOSE_TICK_MS, self._await_worker_exit,
                   waited_ms + _SWEEP_CLOSE_TICK_MS)

    def _finish_close(self) -> None:
        self._closing = True
        if self._poll_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._poll_job)
            self._poll_job = None
        if self._worker is not None and self._worker.is_alive():
            self._worker.force_kill()
        with contextlib.suppress(Exception):
            self.app._sweep_windows.discard(self)
        with contextlib.suppress(Exception):
            self.destroy()

    def shutdown_for_app_close(self) -> None:
        """アプリ終了経路専用。短い同期 join を許し、確実に子を落とす。"""
        worker = self._worker
        if worker is not None:
            worker.request_stop()
            if worker.is_alive():
                worker.join(timeout=_SWEEP_APP_CLOSE_JOIN_S)
            worker.force_kill()
        self._finish_close()

    # ── polling ─────────────────────────────────────────────────────────
    def _poll(self) -> None:
        if self._closing:
            return
        try:
            processed = 0
            while processed < _MAX_LINES_PER_TICK:
                try:
                    kind, payload = self._queue.get_nowait()
                except queue.Empty:
                    break
                processed += 1
                if kind == SWEEP_KIND_STEP:
                    self._update_step(payload)
                elif kind == SWEEP_KIND_LOG:
                    self._append_log(payload)
                elif kind == SWEEP_KIND_DEVICES:
                    self._render_configs(
                        payload.devices if payload.ok else [])
                    self._sweep_status.set(
                        f"デバイス {len(payload.devices)} 個を検出" if payload.ok
                        else f"検出失敗: {payload.error}")
                elif kind == SWEEP_KIND_DONE:
                    self._start_btn.configure(state="normal")
                    self._abort_btn.configure(state="disabled")
                    self._sweep_status.set(self._summary_text())
        except Exception as exc:
            print(f"[sweep] poll error: {exc}", flush=True)
        finally:
            # H-14: 閉じ中は再スケジュールしない (破棄済みウィジェットへの
            # after で TclError を出さない)。
            if not self._closing:
                self._poll_job = self.after(300, self._poll)

    def _update_step(self, step: SweepStep) -> None:
        # 同じ label の行を上書き。
        for i, s in enumerate(self._steps):
            if s.label == step.label:
                self._steps[i] = step
                break
        self._refresh_table()

    def _refresh_table(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for s in self._steps:
            summary = s.summary or {}
            tok = summary.get("tokens_per_sec", "")
            ttft = summary.get("ttft_ms", "")
            exit_code = "" if s.bench_exit_code is None else str(s.bench_exit_code)
            self._tree.insert(
                "", "end",
                values=(s.label, s.state.value, exit_code, tok, ttft))

    def _append_log(self, line: str) -> None:
        self._log.configure(state="normal")
        self._log.insert("end", line + "\n")
        line_count = int(self._log.index("end-1c").split(".")[0])
        if line_count > _MAX_TEXT_LINES:
            self._log.delete("1.0", f"{line_count - _MAX_TEXT_LINES}.0")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _summary_text(self) -> str:
        done = sum(1 for s in self._steps if s.state == SweepState.DONE)
        failed = sum(1 for s in self._steps if s.state == SweepState.FAILED)
        aborted = sum(1 for s in self._steps if s.state == SweepState.ABORTED)
        return f"完了: DONE {done} / FAILED {failed} / ABORTED {aborted}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="CodeRouter Launcher GUI")
    parser.add_argument("--config", default=None,
                        help="Path to providers.yaml (default: auto-detect)")
    args = parser.parse_args()

    app = LauncherApp(config_path=args.config)
    app.mainloop()


if __name__ == "__main__":
    main()

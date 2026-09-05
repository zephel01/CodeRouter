# Launcher Guide — starting llama.cpp / vllm / mlx from a GUI

> 日本語版: [`launcher.md`](./launcher.md)

The CodeRouter Launcher is a tool that **starts and manages** local inference backends (llama.cpp / vllm / mlx) **through on-screen operation**. Instead of typing a long startup command every time, you pick a model and press a button.

The Launcher comes in two forms:

- **Desktop GUI edition** (`launcher_gui.py`) — a tkinter desktop app. No browser required. CodeRouter itself can also be started from here.
- **Web edition** (`/launcher`) — a browser page served by CodeRouter.

Configuration (the `launcher:` block in `providers.yaml`), the screen layout, and troubleshooting are shared between both editions. This guide describes the shared parts once each.

> For backend installation steps, see the [Backend Installation Guide](./install-backends.en.md); for the full walkthrough from installation to startup, see the [Launcher Quickstart](./launcher-quickstart.en.md).

---

## Overview — what you can do with the Launcher

- Recursively scan `.gguf` / `.safetensors` etc. under `model_dirs` and display a model list
- Pick an option profile (preset) from a dropdown and launch
- Manage multiple processes at once (e.g. llama.cpp + vllm running side by side)
- View each process's log in real time
- See [memory recommendations](#memory-recommendations) for each model, checked against installed memory

---

## The two launchers — which one to use

| | Desktop GUI edition (`launcher_gui.py`) | Web edition (`/launcher`) |
|---|---|---|
| Form | tkinter desktop app | Browser page |
| Starting CodeRouter | **Can start it from this app** | Cannot (it runs inside CodeRouter) |
| Main use | First bootstrap — bring up backend and CodeRouter together | Operational UI for managing backends while CodeRouter is running |
| Configuration | `launcher:` block in `providers.yaml` (shared) | Same as left |

The two are not competitors but complements. The natural split is: **the desktop edition for the first bootstrap, and the Web edition for day-to-day operation once CodeRouter is up and running**.

---

## Desktop GUI edition — how to start

`launcher_gui.py` is a tkinter app that starts and manages the backend and CodeRouter **without a browser**. CodeRouter itself can be launched directly from this GUI, letting you go all the way to connecting a local LLM to Claude Code in a single window.

### Requirements

- Python 3.10 or later
- tkinter — part of Python's standard library (no extra install needed; some Linux distros require a separate `python3-tk` package)
- PyYAML — an existing CodeRouter dependency; running from CodeRouter's venv pulls it in automatically

### Starting it

```bash
# Normal startup
python3 launcher_gui.py

# Via CodeRouter's venv (guarantees PyYAML is available)
uv run python launcher_gui.py

# Explicitly specify a config file
python3 launcher_gui.py --config ~/.coderouter-t/providers.yaml
```

Config file lookup order: ① `--config` if given → ② `providers.yaml` in the current directory → ③ `~/.coderouter-t/providers.yaml`. If none exist, it starts with an empty configuration (you can still start things by entering values manually in the UI).

> **As of v2.13.0, step ② (implicit discovery of `providers.yaml` in the current directory) is disabled by default.** This closes a code-execution vector: a hostile `providers.yaml` dropped into the working directory could otherwise hijack executable references such as `launcher.backends[*].binary`. It only becomes opt-in enabled when `CODEROUTER_ALLOW_CWD_CONFIG=1` is set (`true`/`yes`/`on` also work). If it's unset and a `providers.yaml` exists in the current directory, it is skipped rather than loaded.

### CodeRouter bar (desktop edition only)

At the top of the desktop edition there is a **CodeRouter bar** not present in the Web edition.

- Status dot — shows `stopped` / `starting…` / `running` / `error` in color
- Port — CodeRouter's listen port (default `8088`). Editable only while stopped or in error state
- ▶ Start CodeRouter / ■ Stop
- Claude Code connection string — `ANTHROPIC_BASE_URL=http://localhost:<port> ANTHROPIC_AUTH_TOKEN=dummy claude`. Click or use "Copy" to send it to the clipboard

When CodeRouter starts, if `~/.coderouter-t/providers.yaml` doesn't exist yet, a minimal config is auto-generated (this auto-generated file does not include a `launcher:` block — more on this below). Closing the window automatically stops the CodeRouter instance and all backend processes it started.

---

## Web edition — how to start

The operational UI you use in a browser while CodeRouter is running.

1. Add a `launcher:` section to `providers.yaml` (see [Configuration Reference](#configuration-reference))
2. Start CodeRouter — `coderouter-t serve --port 8088`
3. Open `http://localhost:8088/launcher` in a browser

---

## Using the screen

The Launcher screen is made up of a "MODELS panel," a "LAUNCH form," a "PROCESSES table," and "logs." The look differs between the desktop edition (tkinter) and the Web edition (browser), but **the structure and operations are shared**.

### MODELS panel

- The scan button re-scans `model_dirs` and refreshes the model list
- Clicking a model name auto-fills the "Model path" field (the desktop edition also auto-fills "Name"; a manually typed name is preserved)
- File size (GB) is shown alongside, making it easy to weigh against VRAM/memory
- Each model shows a **memory recommendation badge** (`✓ Recommended` / `⚠ Memory tight`) → [Memory recommendations](#memory-recommendations)
- The header shows the detected hardware (e.g. `Metal · RAM 64GB`)
- Target extensions: `.gguf` `.safetensors` `.bin` `.pt` `.pth` `.ggml` (subfolders are searched recursively too)

### LAUNCH form

| Field | Description |
|---|---|
| **Name** | Any identifier for management purposes (e.g. `qwen-coder-8080`) |
| **Port** | The port the server will run on (default `8080`) |
| **Backend** | Choose from `llama.cpp` / `vllm` / `mlx`. The resolved binary path and availability are shown below |
| **Model path** | Selected from the MODELS panel or entered directly |
| **Option profile** | Choose a preset defined in `providers.yaml` |
| **MTP/draft gguf** | Explicit companion draft/MTP gguf path (llama.cpp only). Leave blank for auto-detection → [MTP / speculative decoding](#mtp--speculative-decoding-llamacpp) |
| **MTP** | `auto` (default, auto-detect) / `off` (disable speculative decoding) |
| **Extra options** | Enter flags not in the profile on the spot. Parsed with `shlex` and appended to the end of the command |

`▶ Launch` starts the process and it appears in the PROCESSES table. If the binary isn't found, **the launch button is automatically disabled** and the reason is displayed. See [Memory recommendations](#memory-recommendations) for the **⚙ Recommended values** button next to the "Extra options" field.

### Automatic provider sync (v2.7.4, Web edition only)

When you start a backend in the Web edition, that backend is **automatically registered as a provider** (no need to edit providers.yaml).

- The provider name is `launcher-<backend>-<port>` (e.g. `launcher-llamacpp-8085`). Restarting under the same name **replaces** the entry — no duplicates
- It's registered under the `launcher` profile (auto-created if absent). **The most recently started backend comes first**
- Routing is explicit opt-in: via the `X-CodeRouter-Profile: launcher` header, or `"profile": "launcher"` in the body. **`default_profile` is not changed**
- Registration is **in-memory only** (nothing is written to providers.yaml — to avoid breaking hand-written comments). It disappears on a serve restart, but since the Launcher's own processes share that lifetime, this stays consistent. If you want it to persist, transcribe it into providers.yaml by hand
- Because the provider is registered with `model: ""`, `/v1/models` returns the **actual model ID (gguf name) the upstream is currently loading** (model-name pass-through, also v2.7.4). Swapping the gguf requires no config edit, and external benchmarks can identify the model (a 30-second TTL cache applies)

Verification:

```bash
# After starting
curl http://localhost:8088/v1/models
#   → "id": "<the loaded gguf name>", "owned_by": "coderouter/launcher-llamacpp-8085"

curl http://localhost:8088/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-CodeRouter-Profile: launcher' \
  -d '{"model":"x","messages":[{"role":"user","content":"say hi"}]}'
#   → connectivity is OK if coderouter_provider is launcher-llamacpp-<port>
```

The desktop GUI edition (launcher_gui.py) runs as a separate process, so it's excluded from automatic sync. As before, adjust the `base_url` of the entry in providers.yaml (e.g. the auto-generated `llama-cpp-local`) to match the launched port.

### PROCESSES table

A list of launched backend processes. Shows NAME / BACKEND (llama.cpp / vllm / mlx) / MODEL / PORT / PID / STATUS (color-coded `starting` / `running` / `stopped` / `error`), and lets you select a process to **stop** (SIGTERM), **remove** (from the registry), or **view logs**.

### Logs

Real-time display of the selected process's stdout/stderr. In the Web edition, the log panel auto-refreshes every 3 seconds while running. There are caps on retained lines and displayed lines so long-running sessions don't eat memory.

### Typical workflow (desktop edition)

1. **Pick a model** — click the model you want to use from MODELS
2. **Start the backend** — choose an option profile and press the launch button. It shows as `running` in PROCESSES
3. **Start CodeRouter** — "▶ Start CodeRouter" in the top bar
4. **Connect Claude Code** — copy the connection string and run it in a terminal

---

## MTP / speculative decoding (llama.cpp)

llama.cpp's `llama-server` supports Multi-Token Prediction (MTP) / speculative decoding via `--spec-type`-family flags. The Launcher assembles these flags automatically from the LAUNCH form's **MTP/draft gguf** field and **MTP** field (`auto` / `off`). **llama.cpp only** — specifying `draft_model_path` or `mtp_mode` for vllm/mlx makes the launch request fail with a 400.

### Auto-detection order (`mtp_mode: auto`, the default)

1. **Embedded nextn** — if the selected main gguf's metadata has `{arch}.nextn_predict_layers > 0`, `--spec-type draft-mtp` is added with no separate draft model needed.
2. **Same-folder companion gguf** — if there's no embedded nextn, the Launcher scans the **same directory** as the main gguf for a companion that satisfies all of:
   - the filename contains `mtp` or `draft`, or shares the main file's name prefix (with shard/quant suffixes stripped), and
   - its file size is under 50% of the main gguf, and
   - if its gguf architecture is readable, it matches the main model's (a mismatch is rejected — to avoid a tokenizer/vocabulary mismatch).

   When a candidate is selected, filenames containing `mtp` get `--spec-type draft-mtp`; otherwise `--spec-type draft-simple` — both paired with `--model-draft <path>`.
3. **Nothing found** — the process starts normally without speculative decoding. The process log records `[launcher] MTP/draft gguf not found next to <main>.gguf; starting without speculative decoding`.

### Specifying an explicit draft/MTP gguf

You can point the **MTP/draft gguf** field directly at a companion gguf. If the given path doesn't exist, the launch request is rejected with a 400. Filenames containing `mtp` get `--spec-type draft-mtp`; otherwise `--spec-type draft-simple`.

### `mtp_mode: off`

Choosing `off` in the **MTP** field never emits speculative-decoding flags (reproduces the historical launch command exactly). Combining `off` with an explicit **MTP/draft gguf** is a conflict and is rejected with a 400.

### When `--spec-type` is already supplied via extra options

If "Extra options" or the option profile already contains `--spec-type`, the Launcher's auto-detection is skipped entirely (no flags are added) — an explicit operator choice always wins.

### `-md` / `--model-draft` cannot be used in extra options

Just like `-m` / `--model`, the draft model path can only be set via the **MTP/draft gguf** field. Writing `-md` / `--model-draft` / `--spec-draft-model` into "Extra options" or an option profile causes the launch request to be rejected with a 400. The remaining speculative knobs (`--spec-type` / `--spec-draft-n-max` / `--spec-draft-n-min` / `--spec-draft-p-min` / `-ngld` / `-devd`) stay free-form.

### Known issue: `--split-mode tensor` combination (llama.cpp issue #24309)

Combining a nextn-embedded model / active speculative decoding with `--split-mode tensor` is known to crash llama.cpp ([issue #24309](https://github.com/ggml-org/llama.cpp/issues/24309)). The Launcher detects this combination but does not block the launch — it records a warning in the process log recommending `--split-mode layer` instead.

### Automatic fallback when auto-detected MTP crashes at startup

If the speculative flags were added by **auto-detection** (`mtp_mode: auto`) and the backend **crashes during startup (within ~3 minutes)**, the Launcher **automatically relaunches it once** without the speculative flags. Detection can be correct while some architectures' `draft-mtp` support in llama.cpp is still immature — the arch is detected but the MTP context fails to initialize and the process dies (e.g. `failed to measure MTP context memory` / `requires ctx_other to be set`). The process log records `[launcher] MTP startup failure detected (exit code ...); retrying without speculative decoding` followed by the flag-free relaunch command. The retry happens **at most once**; if it still crashes the process ends in `error`. An **explicit `draft_model_path`** (or an operator-supplied `--spec-type`) is **never** auto-retried — the explicit choice is respected.

### API

`POST /api/launcher/start` (Web edition) accepts these additional fields (llama.cpp backend only; other backends get a 400):

| Field | Type | Default | Description |
|---|---|---|---|
| `draft_model_path` | `string \| null` | `null` | Explicit companion draft/MTP gguf path |
| `mtp_mode` | `"auto" \| "off"` | `"auto"` | `auto` = auto-detect, `off` = disable speculative decoding |

On a successful start, the response JSON includes the resolved speculative flags under the `"speculative"` key (a token array, e.g. `["--spec-type", "draft-mtp"]`; an empty array when nothing was added).

---

## Memory recommendations

Each model in the MODELS list shows a verdict checked against the memory installed on the machine running CodeRouter (unified memory for Apple Silicon, VRAM for NVIDIA GPUs, RAM otherwise).

- **✓ Recommended** — expected to run with margin (`model size × 1.2 + 2GB` fits within available memory)
- **⚠ Memory tight** — doesn't fit, or margin is thin. May swap and become significantly slower

The **⚙ Recommended values** button next to the "Extra options" field fills that field with suggested launch flags based on the selected model, hardware, and **backend**. The output differs per backend.

- **llama.cpp** — `-ngl` (`99` if it fits the GPU, `0` for CPU-only) / `--ctx-size` (`4096`–`32768` depending on available memory) / `--threads` (CPU core count − 2)
- **vllm** — empty. `--max-model-len` and similar depend on the model's actual context length, so this is left to the engine's own auto-derivation
- **mlx** — empty. Since it assumes unified memory, no launch-time tuning flags are needed

All of these are **estimates** — they don't account for other processes' memory usage or quantization scheme. Adjust on the actual machine.

---

## Switching specialized builds (llama.cpp)

**v2.11.0+**. When llama.cpp is built separately for different GPU runtimes, you can pick which build's `llama-server` to launch, per launch. Available in both the GUI and Web editions.

### Why this exists

The same machine exposes different devices depending on the build. Real output from `--list-devices` on a Ryzen AI Max box with an RTX 5090, an RTX 3090 and a Radeon 8060S:

| Build | Devices enumerated |
| --- | --- |
| `build/` | (CPU only) |
| `build-cuda/` | `CUDA0` RTX 5090 (32149 MiB) / `CUDA1` RTX 3090 (24123 MiB) |
| `build-vulkan/` | `Vulkan0` RTX 3090 / `Vulkan1` RTX 5090 / `Vulkan2` Radeon 8060S (114164 MiB) |
| `build-rocm/` | `ROCm0` Radeon 8060S (98304 MiB) |

To offload onto the Radeon 8060S you need the Vulkan or ROCm build; to tensor-split across the two NVIDIA cards you need the CUDA build. The best build varies per model, so this feature lets you switch without editing `providers.yaml` and restarting CodeRouter.

### Configuration

Add `<backend>-<variant>` keys under `launcher.backends`. **A variant must set `binary`.**

```yaml
launcher:
  backends:
    # Base name. Omitting binary means "llama-server from PATH" (unchanged)
    llama.cpp:
      binary: ~/llm/apps/llama.cpp/build/bin/llama-server

    # Specialized builds = variants. binary is required
    llama.cpp-cuda:
      binary: ~/llm/apps/llama.cpp/build-cuda/bin/llama-server
    llama.cpp-vulkan:
      binary: ~/llm/apps/llama.cpp/build-vulkan/bin/llama-server
    llama.cpp-rocm:
      binary: ~/llm/apps/llama.cpp/build-rocm/bin/llama-server
```

A variant name may contain lowercase alphanumerics plus `.` `_` `-` (`[a-z0-9][a-z0-9._-]*`). The base must be `llama.cpp`, `vllm` or `mlx`.

`binary` is mandatory to prevent a specific accident: if it could be omitted, resolution would fall back to `llama-server` on PATH and **you would think you selected the CUDA build while the plain build silently ran**. That is the hardest failure to notice, so it is rejected at config load.

### Using it

Declared variants appear in the "Backend" select as e.g. `llama.cpp-cuda ⚙`. The `⚙` marks it as an advanced option that needs a matching runtime. Selecting one shows a note about its prerequisites under the resolved path.

**If you declare no variants the select stays exactly the three entries it always had**, and the generated launch command is byte-for-byte identical. Specialized builds are only visible to operators who opt in.

### Runtime prerequisites (you install these)

CodeRouter never installs drivers or runtimes; it only picks an already-built binary.

| Variant | Requires |
| --- | --- |
| `-cuda` | NVIDIA driver + CUDA runtime |
| `-vulkan` | Vulkan runtime (`libvulkan`) + ICD |
| `-rocm` | ROCm (`hip`) |

There is no pre-flight dependency check either. Instead, whether `--list-devices` succeeds for that build serves as the practical health check. If it fails the UI says devices could not be enumerated but does not block launching — some setups work even when enumeration doesn't.

> With `launcher.auto_restart` enabled, selecting a build whose runtime is missing causes crash-then-restart cycles. `auto_restart_max_attempts` (default 3) stops it and the process settles into `status='error'`, so it won't loop forever — but check the logs.

### Interaction with device selection (important)

**Device IDs are per-build namespaces.** `CUDA0` and `Vulkan0` are not the same GPU — in the example above `CUDA0` is the RTX 5090 while `Vulkan0` is the RTX 3090.

Switching backends therefore **clears the device selection** and asks you to detect again. If a stale selection were somehow submitted, the server detects IDs that don't exist in the chosen build and rejects the launch with a 400 (passing `--device CUDA0` to a Vulkan build makes `llama-server` fail to start).

### Per-build option_profiles

`option_profiles` accepts variant keys too. The base backend's presets are **inherited**, with variant-specific ones appended; a preset whose name collides replaces the inherited one in place.

```yaml
launcher:
  option_profiles:
    llama.cpp:                   # inherited by every build
      - name: standard
        args: { "-ngl": 99, "--ctx-size": 4096 }
    llama.cpp-cuda:              # CUDA-only (appended after "standard")
      - name: 5090-only-fast
        args: { "-ngl": 99, "--ctx-size": 8192 }
```

You don't need to duplicate shared presets under every variant key.

### Cross-build bench sweep

Declare two or more llama.cpp builds and a **⚙ Cross-build** button appears in the bench-sweep panel. It probes every declared build and generates configuration candidates prefixed with the build name (`cuda / CUDA0 single`, `vulkan / Vulkan2 single`, …). Running the sweep then launches the same model on each build in turn and benchmarks it, so one sweep tells you which build is fastest.

Labels expand into the bench command's `{config}` placeholder, so result JSON files stay distinguishable per build. Note that **no configuration ever mixes devices across builds** — one process runs one executable, so it is impossible by construction.

### Pinning a build per model (launcher.swap)

The `launcher.swap` catalog accepts variants too, for auto-launching each model on its best build.

```yaml
launcher:
  swap:
    enabled: true
    models:
      - name: qwen3-30b
        backend: llama.cpp-cuda      # always launch this model on the CUDA build
        model_path: ~/models/Qwen3-30B-A3B-Q4_K_M.gguf
        port: 18081
```

When a variant is named, config load verifies it is declared in `launcher.backends` (that's the only place its binary path exists).

---

## Device selection (llama.cpp)

On multi-GPU machines you can pass explicit `--device` / `--tensor-split` flags to `llama-server` to control which GPU(s) a model is offloaded to. Available in both the GUI and Web editions. **llama.cpp only** — vllm/mlx don't have a device-selection field at all. Variants (`llama.cpp-cuda`, …) work the same way.

### Detection

The **🔍 Detect** button on the LAUNCH form runs `{binary} --list-devices` and renders the detected devices as checkboxes (GUI) / cards (Web), each annotated with VRAM (free/total, GB). Real-world sample output:

```
# CUDA multi-GPU (Linux)
CUDA0: NVIDIA GeForce RTX 5090 (32149 MiB, 31626 MiB free)
CUDA1: NVIDIA GeForce RTX 3090 (24123 MiB, 23800 MiB free)

# macOS / Apple Silicon (Metal + BLAS fallback)
MTL0: Apple M3 Max (53084 MiB, 53083 MiB free)
BLAS: Accelerate (0 MiB, 0 MiB free)
```

The Metal device id on macOS is **`MTL0`**, not `Metal` (matching `llama-server --list-devices`'s actual output). A `0 MiB` device like `BLAS: Accelerate` still shows up in the list but carries no VRAM, so it's excluded from selection and tensor-split suggestions (only excluded from the *selectable* set — it's still reported in the raw detection result).

If detection fails (missing binary, timeout, unparseable output, etc.), the probe comes back as `ok: false` and the UI falls back to a plain text field where you can type comma-separated device ids by hand.

### Selection and tensor-split

- Selecting exactly one device adds only `--device <id>` (no `--tensor-split`)
- Selecting two or more devices **auto-suggests `--tensor-split` proportional to total VRAM** (e.g. an RTX 5090 + RTX 3090 → `0.57,0.43`). You can override it manually
- If one or fewer devices are detected (Mac Metal alone, a single CUDA card, etc.), the tensor-split field itself is disabled/hidden — it has no meaning with a single device
- **Leaving devices unselected means `--device` is never added** — the launch command is byte-for-byte identical to what it was before this feature existed (no impact on existing deployments)

### Multi-backend caveat

On an llama.cpp build that supports both CUDA and Vulkan, **the same physical GPU can be listed twice under different backends** — e.g. as `CUDA0` and `Vulkan1`. Real-world sample output:

```
CUDA0: NVIDIA GeForce RTX 5090 (32149 MiB, 31610 MiB free)
CUDA1: NVIDIA GeForce RTX 3090 (24123 MiB, 23858 MiB free)
Vulkan0: NVIDIA GeForce RTX 3090 (24822 MiB, 24096 MiB free)
Vulkan1: NVIDIA GeForce RTX 5090 (32607 MiB, 31610 MiB free)
```

To handle this duplication safely, tensor-split auto-suggestion and sweep-config auto-generation are grouped **per backend prefix** (`CUDA` / `Vulkan` / `MTL` / `SYCL`, i.e. the device id with its trailing digits stripped). **Mixed configurations that cross backends (e.g. `CUDA0` + `Vulkan1`) are never auto-generated** — that would double-count the same GPU. If you want to try a cross-backend combination anyway, check (or type) the device ids manually.

### API (Web edition)

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/launcher/devices` | none | Device detection. `?backend=llama.cpp` (default); `?refresh=1` bypasses the detection cache and re-probes |

`GET /api/launcher/devices` response:

```jsonc
{
  "ok": true,
  "error": null,
  "devices": [
    {"id": "CUDA0", "name": "NVIDIA GeForce RTX 5090",
     "total_mib": 32149, "free_mib": 31626, "total_gb": 31.4, "free_gb": 30.9},
    {"id": "CUDA1", "name": "NVIDIA GeForce RTX 3090",
     "total_mib": 24123, "free_mib": 23800, "total_gb": 23.6, "free_gb": 23.2}
  ],
  // VRAM-ratio tensor-split suggestion per backend prefix (0 MiB devices
  // excluded; a key only appears for a backend with 2+ selectable devices)
  "suggested_tensor_split": {"CUDA": [0.57, 0.43]},
  // Auto-generated sweep config candidates (one per device + one per
  // multi-device backend group)
  "auto_configs": [
    {"label": "CUDA0 solo", "device_ids": ["CUDA0"], "tensor_split": []},
    {"label": "CUDA1 solo", "device_ids": ["CUDA1"], "tensor_split": []},
    {"label": "CUDA x2", "device_ids": ["CUDA0", "CUDA1"], "tensor_split": [0.57, 0.43]}
  ]
}
```

`POST /api/launcher/start` accepts these additional fields (**llama.cpp only**; default is an empty array = nothing selected = the same launch command as before this feature):

| Field | Type | Default | Description |
|---|---|---|---|
| `device_ids` | `list[string]` | `[]` | Selected device ids (e.g. `["CUDA0", "CUDA1"]`) |
| `tensor_split` | `list[float]` | `[]` | Split ratios; only meaningful when `device_ids` has 2 or more entries (e.g. `[0.57, 0.43]`) |

---

## Bench sweep (llama.cpp)

Runs a set of device configurations (e.g. `CUDA0` alone / `CUDA1` alone / `CUDA0,CUDA1` split) **automatically, one after another**: start → wait for readiness → run an external benchmark → stop → move to the next configuration — so you can compare throughput across configurations. Available in both the GUI and Web editions. **llama.cpp only**.

The benchmark itself is expected to be the external tool [llmbench](https://github.com/zephel01/swe-bench) (installed separately — it does not ship with the Launcher).

### Using it (GUI)

The **📊 Bench sweep** button on the LAUNCH form opens a separate window (`SweepWindow`).

1. Fill in model path, port, runs, results_dir, and the bench command (model path/port are pre-filled from the parent form)
2. Click **🔍 Detect devices → generate configs** to run `--list-devices` and generate config checkboxes using the same auto-generation rules as [device selection](#device-selection-llamacpp) (one per device, plus one per multi-device backend group)
3. Check the configurations you want to run and click **▶ Start**
4. The progress table (config / state / exit / tok/s / ttft(ms)) and log update live. **■ Abort** cancels the remaining configurations (the one currently running still runs to completion)

### Using it (Web edition)

From the bench-sweep card on the `/launcher` page, fill in the same config matrix, bench command, runs, and results_dir as the GUI edition, then click **Start**. **Starting/aborting are write operations and require launcher token auth** (the `X-CodeRouter-Token` header, same as other write endpoints). Progress is shown via a 3-second status poll.

### State machine

Each configuration (`SweepStep`) moves through `pending → starting →(readiness passes)→ benching →(bench exits)→ done`.

- A launch failure or readiness timeout → `failed` (the sweep continues to the next configuration)
- A non-zero bench exit code still counts as `done` (the exit code is recorded for comparison — only a failure to even launch the bench process results in `failed`)
- On an abort request, the currently running configuration finishes normally; the remaining, not-yet-started configurations become `aborted`

### `launcher.bench` configuration (defaults)

The `launcher.bench:` block in `providers.yaml` lets you set defaults for the sweep (omit it entirely and hard-coded defaults are used instead — fully backward compatible).

```yaml
launcher:
  bench:
    command_template: "llmbench run --model local-openai --runs {runs}"
    runs: 5
    results_dir: ~/llmbench-results
    readiness_timeout_s: 300
```

| Field | Type | Default | Description |
|---|---|---|---|
| `command_template` | str | `"llmbench run --model local-openai --runs {runs}"` | Template for the external bench command. `{port}` `{config}` `{base_url}` `{results_dir}` `{runs}` are expanded via plain string substitution (not `str.format`, so JSON braces in the command aren't misinterpreted), then split into argv with `shlex`. On Windows, `shlex.split(..., posix=False)` is used so backslash-containing paths survive |
| `runs` | int | `5` (1–1000) | Number of bench runs per configuration. Expanded into `{runs}` |
| `results_dir` | str \| null | `null` | Directory `llmbench` writes its results to. A relative path is resolved against the server's CWD. Only when set does each configuration's completion trigger reading the newest JSON in this directory for a comparison summary |
| `readiness_timeout_s` | float | `300.0` (5–3600) | Maximum seconds to wait for the server to become ready for each configuration in the sweep. The 5-minute default accounts for large GGUF load times |

`{port}` expands to the port fixed for the sweep, `{config}` to the configuration's label (e.g. `CUDA0 solo`), and `{base_url}` to `http://localhost:{port}/v1`. The bench child process also gets the environment variable `OPENAI_BASE_URL=http://localhost:{port}/v1`, so `llmbench` works whether it reads the connection target from the template argument or from the environment.

### Results comparison

When `results_dir` is set, after each configuration's bench run finishes, the Launcher reads the newest `*.json` in that directory (updated at or after the configuration's start time) and best-effort extracts `tokens_per_sec` / `ttft_ms` / `latency_ms` / `runs`, also recognizing common aliases (`tok_s` / `throughput` / `tps`, etc.), then reflects them in the progress table. Parsing is defensive so a change in `llmbench`'s JSON schema doesn't break it — fields that can't be extracted are simply left blank.

### API (Web edition)

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/launcher/sweep/start` | **token** | Start a sweep. 409 if one is already running, 400 if `configs` is empty, 400 if the port is in use/unavailable |
| GET | `/api/launcher/sweep/status` | none | Current (or most recent) sweep status: `{sweep_id, running, current_index, steps: [...]}` |
| POST | `/api/launcher/sweep/abort` | **token** | Request an abort for the running sweep. 404 if no sweep exists |
| GET | `/api/launcher/sweep/logs?n=200` | none | Last N lines of sweep progress log: `{logs: [str], total: int}` |

Main fields of the `POST /api/launcher/sweep/start` request body:

| Field | Type | Default | Description |
|---|---|---|---|
| `backend` | str | `"llama.cpp"` | Target backend |
| `model_path` | str (required) | — | Model path shared by all configurations |
| `port` | int (required) | — | Port reused across all configurations (1024–65535). Each configuration's stop waits for the port to be released before the next one starts |
| `configs` | `list[{label, device_ids, tensor_split}]` (required, 1+) | — | The configurations to run. Use `/api/launcher/devices`'s `auto_configs` as-is, or assemble them manually |
| `bench_command` | str \| null | `null` | `null` falls back to `launcher.bench.command_template` (or the hard-coded default if unset) |
| `runs` / `results_dir` | int \| null / str \| null | `null` | Same fallback to `launcher.bench` defaults |

---

## Automatic model swap (launcher.swap) — v2.9.1+

An equivalent of [llama-swap](https://github.com/mostlygeek/llama-swap)'s core loop, built into CodeRouter itself with no extra dependencies. Opt-in and disabled by default (`enabled: false`).

- Watches the request's `model` name and **starts the backing backend on demand** if it isn't already running
- **Holds the request** until the model finishes loading — the response is only returned once the process passes its readiness check (no connection-refused / 503 against a still-loading model)
- **Automatically unloads** an idle process (no in-flight requests) after `ttl_seconds`, freeing memory

### Minimal configuration

```yaml
# ~/.coderouter-t/providers.yaml
default_profile: auto

auto_router:
  default_rule_profile: launcher-swap-ornith-9b
  rules: []

launcher:
  model_dirs:
    - ~/models

  swap:
    enabled: true
    ttl_seconds: 1800
    readiness_timeout_s: 180

    models:
      - name: ornith-9b
        backend: llama.cpp
        model_path: ~/models/Ornith-1.0-9B-Q4_K_M.gguf
        port: 18081
        num_ctx: 32768
        extra_args: "-ngl 99"

      - name: qwen3-coder-30b
        backend: llama.cpp
        model_path: ~/models/Qwen3-Coder-30B-Q4_K_M.gguf
        port: 18082
        num_ctx: 32768
```

Note there's no `providers:` / `profiles:` at all here. **As of v2.9.2**, when `launcher.swap.enabled: true` and `launcher.swap.models` has at least one entry, the top-level `providers` / `profiles` lists can be omitted (or left empty, `[]`) — a `launcher-swap-<name>` profile for each catalog model is auto-injected at config load, and its backing provider is registered at runtime on first on-demand spawn. **Through v2.9.1**, this relaxation didn't exist, so you had to write an unreachable dummy `providers` / `profiles` entry (e.g. `base_url: http://127.0.0.1:9`).

For a more realistic example that mixes swap with an always-on backend like Ollama, see [`examples/providers.swap.yaml`](../../examples/providers.swap.yaml).

### `launcher.swap` fields

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Enable on-demand swap. `false` preserves manual-only startup exactly as before (no impact on existing deployments) |
| `ttl_seconds` | float \| null | `1800.0` | Seconds of no in-flight requests after which the process is auto-stopped. `null` = disabled (runs until explicitly stopped), `0` = unload as soon as the last request completes. This is the global default shared by all models — `models[].ttl_seconds` overrides it per model ([Unreleased]) |
| `readiness_timeout_s` | float | `120.0` (1–1800) | Upper bound (seconds) a **request** will wait for an on-demand spawn to become ready before the dispatch hook raises a retryable `AdapterError`. **Distinct from `launcher.readiness_timeout_s` below (default 300s)** — this one bounds "how long one request waits," that one bounds "the general readiness-monitoring window for a process" |
| `sweep_interval_s` | float | `15.0` (1–600) | How often the TTL sweeper background task scans for idle processes |
| `port_retry_attempts` | int | `2` (0–5) | [Unreleased] Number of additional retries (each on a freshly picked ephemeral port) after a failed startup, for catalog entries that leave `models[].port` unset. `0` = no retry. Irrelevant for a fixed `port` (always exactly one attempt). Does not close the TOCTOU window itself — see `models[].port` below |
| `inject_auto_router_rules` | bool | `true` | Whether to auto-generate an `auto_router` rule per catalog model (`id: swap:<name>`, `model_pattern` is an exact match on the name). Set `false` to wire routing yourself (e.g. via the `X-CodeRouter-Profile` header) |
| `models[].name` | str (required) | — | Logical name matched against the request's `model` field. The provider name and dedicated profile name are automatically `launcher-swap-<name>` |
| `models[].backend` | `"llama.cpp" \| "vllm" \| "mlx"` (required) | — | Same backend set as the manual Launcher UI |
| `models[].model_path` | str (required) | — | Absolute or `~`-relative model file path. Must be **under `launcher.model_dirs`** — re-validated both at config load and at spawn time (defense against path traversal) |
| `models[].port` | int \| null | `null` | Fixed port recommended. When unset, an OS-assigned ephemeral port is used, retrying on a fresh port up to `launcher.swap.port_retry_attempts` times (default 2) if startup fails. Best-effort — the gap between picking the port and the child actually binding it (TOCTOU) is never fully closed. Use a fixed port if you need a strong guarantee |
| `models[].ttl_seconds` | float \| null | `null` | [Unreleased] Per-model TTL override. `null` (default) = follow the global `launcher.swap.ttl_seconds`. `0` means the same thing as the global field's `0` (unload as soon as the last lease releases), scoped to just this model |
| `models[].option_profile` | str \| null | `null` | Name of an existing preset in `launcher.option_profiles[backend]`. A name that doesn't exist is a config-load error |
| `models[].num_ctx` | int | `8192` (≥256) | Baseline used for KV estimation / launch parameters |
| `models[].extra_args` | str | `""` | One-off extra CLI flags (a single string, parsed with `shlex`; re-specifying the model/draft model is rejected) |
| `models[].draft_model_path` | str \| null | `null` | Explicit MTP/draft companion gguf |
| `models[].mtp_mode` | `"auto" \| "off"` | `"auto"` | Same meaning as the manual Launcher UI's **MTP** field |
| `models[].model_pattern` | str \| null | `null` | Additional regex (`re.fullmatch`) accepted alongside an exact match on `name`. Only affects catalog matching (`SwapManager.match`) — the auto-injected auto_router rule always uses an exact match (`re.escape`) on `name` |

> **Phase 2 fields (schema-declared only, not implemented)**: `models[].group` (`"swap" | "persistent" | "exclusive"`), `models[].est_weights_gb`, `launcher.swap.memory_budget_gb`, and `launcher.swap.max_loaded` exist in the schema but are never consulted by Phase 1 (as of v2.9.1) logic.

### Behavioral notes

- **Swap-managed processes are excluded from `launcher.auto_restart`.** Crash recovery is handled by SwapManager's own re-spawn on the next request, rather than the generic auto-restart supervisor — avoiding two supervisors fighting over the same fixed port
- **Streaming responses are never TTL-evicted mid-stream.** A "lease" is held until the final chunk is reached, so a process is never killed while it's still generating
- **A request whose `model` name doesn't match the catalog** falls through to `auto_router.default_rule_profile` (or ordinary profile resolution) — it never reaches a swap-dedicated profile
- **There's no cap on how many models can be loaded simultaneously** (Phase 1) — as many models as fit in memory can run at once. Budgeting and eviction-based exclusive swap are planned for Phase 2
- **Omitting `providers` / `profiles` is available as of v2.9.2** (see "Minimal configuration" above). Earlier versions required an unreachable dummy provider/profile

Measured end-to-end (macOS, M3 Max, Metal, ~350MB-class GGUF): the `_run/swap-test/` automated test kit reported ALL PASS across cold spawn (~2s) → warm reuse (~0s, no re-spawn) → catalog-miss fallthrough → TTL unload → respawn. See the design doc's implementation record (§10.5) for details.

For the full design — concurrency model, security considerations, and review decisions — see [`docs/designs/launcher-model-swap.md`](../designs/launcher-model-swap.md).

---

## Configuration reference

The MODELS list, option profiles, and binary paths are all loaded from the `launcher:` block in `~/.coderouter-t/providers.yaml`. **Shared between the desktop and Web editions**.

### The full `launcher:` block

```yaml
# ~/.coderouter-t/providers.yaml
launcher:
  model_dirs:           # list[str]  required
    - ~/llm/models
  backends:             # dict  optional
    llama.cpp:
      binary: null      # null = llama-server from PATH
    vllm:
      binary: null      # null = python from PATH
    mlx:
      binary: null      # null = python from PATH
  option_profiles:      # dict  optional
    llama.cpp: [...]
    vllm: [...]
```

> The `providers.yaml` auto-generated by the "Start CodeRouter" button does not include a `launcher:` block. To use the model list or profiles, you'll need to add the `launcher:` block yourself. You can start from a copy of the `launcher_profiles.yaml.example` template.

### `backends` — binary path configuration

Specify a full path when the binary isn't in PATH (source builds, venv environments, etc.).

```yaml
launcher:
  backends:
    llama.cpp:
      binary: ~/llama.cpp/build/bin/llama-server         # source build example
    vllm:
      binary: ~/.coderouter-t/backends/vllm/bin/python     # venv example
    mlx:
      binary: ~/.coderouter-t/backends/mlx/bin/python      # venv example
```

If `binary` is omitted or `null`, the default name (`llama-server` / `python`) is looked up in PATH. Tilde (`~`) expansion is supported. For vLLM/MLX, it's recommended to keep separate venvs per backend under `~/.coderouter-t/backends/<backend-name>/` (see the [Installation Guide](./install-backends.en.md) for details). The resolved path is shown below the "Backend" select in the UI.

A key may also be a **variant name** such as `llama.cpp-cuda` (v2.11.0+). This lets you register several builds of the same backend — typically llama.cpp compiled for different GPU runtimes — and pick one per launch. See [Switching specialized builds](#switching-specialized-builds-llamacpp).

> **Breaking change in v2.11.0**: keys in `launcher.backends` must be `llama.cpp` / `vllm` / `mlx` or one of those with a `-<variant>` suffix; anything else is now a **config-load error**. Previously a typo such as `llamacpp:` was silently ignored (the backend list was a fixed set of three keys). If startup fails after upgrading, check the spelling of your keys.

### `model_dirs`

- Tilde (`~`) expansion supported
- Non-existent paths are silently skipped during scanning (no startup error)
- Extensions searched: `.gguf` `.safetensors` `.bin` `.pt` `.pth` `.ggml`
- Subfolders are searched recursively

### `option_profiles`

```yaml
option_profiles:
  llama.cpp:            # backend name (key)
    - name: "A readable name"   # shown in the UI dropdown
      args:
        "-ngl": 99              # int → "-ngl 99"
        "--ctx-size": 4096
        "--dtype": "float16"    # str → "--dtype float16"
        "--mlock": true         # bool true → "--mlock" (no value)
        "--no-mmap": false      # bool false → omitted
```

**Type rules for `args`:**

| YAML type | CLI conversion |
|---|---|
| `int` / `float` / `str` | 2 tokens: `--flag value` |
| `bool: true` | `--flag` only (no value) |
| `bool: false` | this flag is omitted |

### Readiness gating and auto-restart (v2.9.1+)

These fields can be added directly under the `launcher:` block (same level as `model_dirs` / `backends`). They apply to both manual starts and swap's on-demand starts.

| Field | Type | Default | Description |
|---|---|---|---|
| `readiness_timeout_s` | float | `300.0` (5–3600) | Maximum seconds to wait for a launched backend to become "ready." For llama.cpp / vllm, that means `GET /health` returns 200; other backends use a bare TCP connect. If the deadline is exceeded, the process is left running but never registered as a provider, and its status becomes `error`. The 5-minute default accounts for large GGUF load times |
| `readiness_poll_interval_s` | float | `2.0` (0.2–60) | Seconds between readiness probes while a launched backend's status is `loading` |
| `auto_restart` | bool | `false` | When `true`, a backend that crashes (non-zero exit — after the one-shot MTP startup-crash fallback, if applicable, has already been tried) is automatically relaunched, up to `auto_restart_max_attempts` times with exponential backoff. An intentional stop (the Stop button, or server shutdown) is never treated as a crash. Default `false`, matching the opt-in stance of `ProviderConfig.restart_command`. **Swap-managed processes are excluded** — SwapManager is their sole supervisor |
| `auto_restart_max_attempts` | int | `3` (0–20) | Maximum consecutive auto-restart attempts before giving up. Resets to 0 once a restarted process passes its readiness check. Ignored when `auto_restart` is `false` |
| `auto_restart_backoff_s` | float | `2.0` (0.1–300) | Initial backoff (seconds) before the first auto-restart attempt. Doubles on each subsequent attempt, capped at `auto_restart_backoff_max_s` |
| `auto_restart_backoff_max_s` | float | `30.0` (1–600) | Cap (seconds) on the exponential auto-restart backoff |

**New process status `loading`** — a launched process that's still waiting on its readiness check, alongside `starting` / `running` / `stopped` / `error` in the PROCESSES table. Once readiness passes, it transitions to `running`, and only then is it registered as a provider (previously it was registered the instant the process spawned, so requests could race a model that was still loading and get connection-refused / 503).

> **Behavior change (v2.9.1)**: `POST /api/launcher/start` no longer performs provider sync synchronously — the response's `provider_sync` is always `null`. Check `/api/launcher/processes` or the `provider sync:` log line for the sync result.

### `launcher.bench` — bench sweep defaults

Add a `bench:` sub-block directly under `launcher:` to override the [bench sweep](#bench-sweep-llamacpp)'s defaults (bench command, runs, results_dir, readiness timeout). Omit it and the hard-coded defaults apply, so existing `providers.yaml` files keep working unchanged. See the `launcher.bench` field table and sample YAML in the [Bench sweep](#bench-sweep-llamacpp) section.

### Extra options (free-form input)

The string in the UI's "Extra options" field is parsed with `shlex.split()` and appended to the end of the command. Use this to try experimental flags not in a profile.

```
-ngl 40 --rope-scale 2.0 --rope-freq-base 10000
```

> **Note**: re-specifying the model via `-m` / `--model` (or the `--model=...` form) is not accepted in either extra options or option profiles — doing so causes the launch request to be rejected with a 400. Specify the model only via the "Model path" field. Likewise, re-specifying the draft model via `-md` / `--model-draft` / `--spec-draft-model` (llama.cpp-only flags) is not accepted in extra options or option profiles either. Specify the draft model only via the "MTP/draft gguf" field — see [MTP / speculative decoding](#mtp--speculative-decoding-llamacpp).

---

## Option quick reference

### llama.cpp

Only the commonly used flags are listed. See `llama-server --help` for the full list.

| Flag | Description | Suggested value |
|---|---|---|
| `-ngl` | Number of layers offloaded to GPU | `99` (all) / `0` (CPU only) |
| `--ctx-size` | Context length (tokens) | `4096` / `8192` / `131072` |
| `--threads` | Number of CPU threads | CPU core count − 2 |
| `--batch-size` | Batch size | `512` |
| `--mlock` | Lock into memory (prevent swap) | `true` |
| `--embedding` | Start in embedding mode | `true` |

### vllm

See `python -m vllm.entrypoints.openai.api_server --help` for the full list.

| Flag | Description | Suggested value |
|---|---|---|
| `--dtype` | Tensor data type | `"auto"` / `"float16"` / `"bfloat16"` |
| `--max-model-len` | Maximum context length | `4096` / `32768` |
| `--gpu-memory-utilization` | GPU memory utilization (0–1) | `0.85` |
| `--quantization` | Quantization scheme | `"awq"` / `"gptq"` |
| `--tensor-parallel-size` | Tensor parallelism degree (number of GPUs) | `2` |

### mlx

MLX (`mlx_lm.server`) assumes unified memory and has no concept of layer offloading like `-ngl`. It runs as soon as the Launcher sets `--model` and `--port`; startup performance-tuning flags are generally unnecessary.

---

## Using it after startup — connecting to CodeRouter

The backend started by the Launcher provides an OpenAI-compatible API. Register it as a CodeRouter provider in `providers.yaml` to gain routing, guards, and fallback.

```yaml
providers:
  - name: local-qwen-launcher
    kind: openai_compat
    base_url: http://localhost:8080/v1   # the port specified in the Launcher
    model: Qwen2.5-Coder-7B-Instruct

profiles:
  - name: default
    providers: [local-qwen-launcher]
```

Start Claude Code pointed at CodeRouter:

```bash
ANTHROPIC_BASE_URL=http://localhost:8088 ANTHROPIC_AUTH_TOKEN=dummy claude
```

---

## Adding and sharing profiles

You can add a new preset just by appending to `option_profiles`. No code changes needed.

```yaml
launcher:
  option_profiles:
    llama.cpp:
      - name: "My custom setup"
        args:
          "-ngl": 40
          "--ctx-size": 8192
```

Restarting CodeRouter reflects it in the UI. `launcher_profiles.yaml.example` is bundled in the repository, so you can add a new profile to it and share it via a PR.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Launch button is disabled (grayed out) | The backend's binary can't be found | Check the display below the backend field, and set a full path in `launcher.backends.<name>.binary` |
| Model list is empty | `launcher.model_dirs` isn't set, or the config file wasn't found | Set `model_dirs` in `providers.yaml` (the desktop edition can also specify it explicitly via `--config`) |
| Option profile can't be selected | `launcher.option_profiles` is missing | Add `option_profiles` to `providers.yaml` |
| Goes to `error` right after starting | Wrong model path / insufficient VRAM | Check the error details in the log |
| Port conflict | Another process is already using that port | Change the port number |
| `PyYAML not found` (desktop edition) | Ran from a plain Python install | Run from CodeRouter's venv with `uv run python launcher_gui.py` |
| Process disappears after a restart | By design — the registry is in-memory | Use OS-level launchd/systemd if you need it to persist |

---

## Related docs

- [Backend Installation Guide](./install-backends.en.md) — installing llama.cpp / vLLM / MLX
- [Launcher Quickstart](./launcher-quickstart.en.md) — the full walkthrough from install to startup
- [Architecture details — Launcher section](../concepts/architecture.en.md#launcher--llamacpp--vllm-プロセス管理-v250)
- [Usage Guide](../guides/usage-guide.md)
- [llama.cpp direct connection guide](./llamacpp-direct.en.md)
- [Model auto-swap design doc](../designs/launcher-model-swap.md) — `launcher.swap`'s concurrency model, security considerations, and review decisions

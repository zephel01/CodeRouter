# Backend Installation Guide — llama.cpp / vLLM / MLX

How to install the three local inference backends that the CodeRouter Launcher starts and manages: **llama.cpp**, **vLLM**, and **MLX**. Installing any one of them is enough to get started.

For Launcher configuration and startup after installation, see the [Launcher Quickstart](./launcher-quickstart.md).

> 日本語版: [install-backends.md](./install-backends.md)

---

## Which backend to choose

| Backend | OS | Model format | Best for |
|---|---|---|---|
| **llama.cpp** | macOS / Linux / Windows | GGUF | Anyone starting out. The most portable and lightweight option |
| **vLLM** | Linux (NVIDIA CUDA) | Hugging Face (safetensors) | High throughput on Linux + GPU |
| **MLX** | macOS (Apple Silicon) | MLX format | Fast inference on M-series Macs |

**When in doubt, use llama.cpp.** It runs on macOS, Linux, and Windows, has a huge selection of `.gguf` models, and is the lightest to set up. On an Apple Silicon Mac, MLX is a notch faster; on Linux with an NVIDIA GPU, vLLM gives the highest throughput.

---

## 1. llama.cpp

Provides `llama-server`, which exposes an OpenAI-compatible API.

### Supported environment

macOS, Linux, and Windows — all three. GPU acceleration uses Metal on macOS and NVIDIA CUDA on Linux/Windows.

### Option A — Homebrew (macOS / Linux, easiest)

```bash
brew install llama.cpp
```

`llama-server` lands on your PATH. Done.

### Option B — winget (Windows)

```powershell
winget install ggml.llamacpp
```

### Option C — Prebuilt binaries (any OS)

From the [llama.cpp Releases page](https://github.com/ggml-org/llama.cpp/releases), download the archive matching your OS and backend (CPU / CUDA / Metal) and extract it. Use the `llama-server` inside directly.

### Option D — Build from source (latest version / GPU tuning)

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
```

**macOS (Apple Silicon)** — Metal is enabled by default:

```bash
cmake -B build
cmake --build build --config Release -j
```

**Linux (NVIDIA CUDA)** — requires the CUDA Toolkit:

```bash
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j
```

After building, the server binary is at `build/bin/llama-server`. You will point the Launcher at this full path later.

### Verify

```bash
llama-server --version
# Start with a model, then check connectivity from another terminal
llama-server -m ./model.gguf --port 8080
curl http://localhost:8080/v1/models
```

### Common pitfalls

- **CUDA and Metal cannot coexist in one binary.** Build/download for the machine you will run on.
- If `llama-server` is not found, set the **full path** of the binary (from option B/C/D) in the Launcher's `backends.llama.cpp.binary`.

---

## 2. vLLM

### Supported environment

A high-performance inference server for **Linux + NVIDIA GPU (CUDA)**.

- **macOS**: CPU-only backend, not practical. Use llama.cpp or MLX on a Mac.
- **Windows**: no native support. Run the Linux steps inside WSL2 (Ubuntu).

### Install

Create the venv under `~/.coderouter-t/backends/` — the same place as the CodeRouter config — **with a separate venv per backend**. vLLM goes in `~/.coderouter-t/backends/vllm/` (vLLM and MLX have completely different dependency trees, so always keep their venvs separate). A fixed path lets you write the `binary:` directly in `providers.yaml`.

Installation via `uv` (a fast Python environment manager) is recommended:

```bash
uv venv ~/.coderouter-t/backends/vllm --python 3.12 --seed
source ~/.coderouter-t/backends/vllm/bin/activate
uv pip install vllm --torch-backend=auto
```

`pip` also works:

```bash
python3.12 -m venv ~/.coderouter-t/backends/vllm
source ~/.coderouter-t/backends/vllm/bin/activate
pip install vllm
```

### Verify

```bash
python -c "import vllm; print(vllm.__version__)"
# Start with a model (first run downloads from Hugging Face)
python -m vllm.entrypoints.openai.api_server --model Qwen/Qwen2.5-7B-Instruct --port 8080
```

> The modern CLI `vllm serve Qwen/Qwen2.5-7B-Instruct --port 8080` starts the same OpenAI-compatible server. The Launcher uses the environment-independent `python -m vllm.entrypoints.openai.api_server` form (both are the same thing).

### Launcher integration

The Launcher starts vLLM as `<python> -m vllm.entrypoints.openai.api_server`. Set `backends.vllm.binary` in `providers.yaml` to the python of the venv created above:

```yaml
backends:
  vllm:
    binary: ~/.coderouter-t/backends/vllm/bin/python
```

### Common pitfalls

- A **CUDA driver / Toolkit version mismatch** can break installation or startup. Check your GPU and driver with `nvidia-smi`.
- Being slow or non-functional on macOS is expected — use llama.cpp / MLX on a Mac.

---

## 3. MLX

Provides `mlx_lm.server`, an inference server built on Apple's MLX machine-learning framework. It runs noticeably faster on Apple Silicon Macs.

### Supported environment

- **macOS 14.0 or later**, **Apple Silicon (M1 or newer)** only.
- A **native (arm64) Python 3.10+** is required. It will not work with Intel Macs or an x86 Python running under Rosetta.

### Install

Create the venv at `~/.coderouter-t/backends/mlx/` (a separate venv from vLLM — one per backend):

```bash
python3 -m venv ~/.coderouter-t/backends/mlx
source ~/.coderouter-t/backends/mlx/bin/activate
pip install mlx-lm
```

### Verify

```bash
# Confirm a native (arm) Python — should print "arm"
python -c "import platform; print(platform.processor())"
# Confirm the install
python -c "import mlx_lm; print('mlx-lm OK')"
# Start with a model (first run downloads from Hugging Face)
mlx_lm.server --model mlx-community/Qwen2.5-7B-Instruct-4bit --port 8080
curl http://localhost:8080/v1/models
```

### Note on model format

**MLX cannot read GGUF.** `.gguf` files (for llama.cpp) do not work with MLX. Use **MLX-format models**, such as those published by [`mlx-community`](https://huggingface.co/mlx-community) on Hugging Face. Passing a repository ID directly — `mlx_lm.server --model mlx-community/<name>` — downloads it automatically on first use.

### Launcher integration

The Launcher starts MLX as `<python> -m mlx_lm.server`. Set `backends.mlx.binary` in `providers.yaml` to the python of the venv created above:

```yaml
backends:
  mlx:
    binary: ~/.coderouter-t/backends/mlx/bin/python
```

### Common pitfalls

- If **`platform.processor()` prints something other than `arm`** (`i386` / `x86_64`), your Python is an x86 build running under Rosetta. In Finder, Get Info on Terminal.app, uncheck "Open using Rosetta", and reinstall a native Python.
- On macOS older than 14.0, the PyPI package cannot be installed — update your OS.
- The "suggest values" button's flags such as `-ngl` are llama.cpp-only. MLX assumes unified memory and needs no launch-time tuning flags.

---

## After installation — start it from the Launcher

Once a backend is installed, you can pick a model and start it from the CodeRouter Launcher. The `launcher:` block in `providers.yaml`, plus the full path from Launcher startup to connecting Claude Code, is covered in the [Launcher Quickstart](./launcher-quickstart.md).

---

## Related documents

- [Launcher Quickstart](./launcher-quickstart.md) — configuration and startup after installation
- [Launcher Guide (Web & Desktop GUI)](./launcher.md)
- [llama.cpp direct connection guide](./llamacpp-direct.en.md)
- [CodeRouter Quickstart](../start/quickstart.en.md)

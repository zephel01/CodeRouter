# バックエンド インストール手順書 — llama.cpp / vLLM / MLX

CodeRouter Launcher が起動・管理する 3 つのローカル推論バックエンド ── **llama.cpp** / **vLLM** / **MLX** ── の導入手順です。いずれか 1 つを入れれば始められます。

導入後の Launcher 設定・起動については [Launcher クイックスタート](./launcher-quickstart.md) を参照してください。

> English version: [install-backends.en.md](./install-backends.en.md)

---

## どのバックエンドを選ぶか

| バックエンド | 対応 OS | モデル形式 | 向いている人 |
|---|---|---|---|
| **llama.cpp** | macOS / Linux / Windows | GGUF | まず試したい人。最も汎用的で軽量 |
| **vLLM** | Linux (NVIDIA CUDA) | Hugging Face (safetensors) | Linux + GPU で高スループットを出したい人 |
| **MLX** | macOS (Apple Silicon) | MLX 形式 | M シリーズ Mac で速く動かしたい人 |

**迷ったら llama.cpp。** macOS・Linux・Windows のどれでも動き、`.gguf` モデルが豊富で、セットアップが最も軽量です。Apple Silicon の Mac なら MLX が一段速く、Linux + NVIDIA GPU なら vLLM が高スループットです。

---

## 1. llama.cpp

OpenAI 互換 API を提供する `llama-server` を用意します。

### 対応環境

macOS / Linux / Windows のすべて。GPU は macOS で Metal、Linux/Windows で NVIDIA CUDA に対応します。

### 方法 A — Homebrew(macOS / Linux、最も簡単)

```bash
brew install llama.cpp
```

`llama-server` が PATH に入ります。これで完了です。

### 方法 B — winget(Windows)

```powershell
winget install ggml.llamacpp
```

### 方法 C — プレビルドバイナリ(全 OS)

[llama.cpp の Releases ページ](https://github.com/ggml-org/llama.cpp/releases) から、OS とバックエンド(CPU / CUDA / Metal)に合ったアーカイブをダウンロードして展開します。中の `llama-server` をそのまま使えます。

### 方法 D — ソースからビルド(最新版・GPU 最適化したい場合)

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
```

**macOS (Apple Silicon)** — Metal は既定で有効:

```bash
cmake -B build
cmake --build build --config Release -j
```

**Linux (NVIDIA CUDA)** — CUDA Toolkit が必要:

```bash
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j
```

ビルド後、サーバーバイナリは `build/bin/llama-server` に生成されます。このフルパスを後で Launcher に設定します。

### 動作確認

```bash
llama-server --version
# モデルを指定して起動 → 別ターミナルで疎通確認
llama-server -m ./model.gguf --port 8080
curl http://localhost:8080/v1/models
```

### よくあるつまずき

- **CUDA と Metal は同一バイナリに同梱できません。** 実行マシンに合わせてビルド/ダウンロードしてください。
- `llama-server` が見つからない場合は、方法 B/C/D で入れたバイナリの**フルパス**を Launcher の `backends.llama.cpp.binary` に設定します。

---

## 2. vLLM

### 対応環境

**Linux + NVIDIA GPU (CUDA)** 向けの高速推論サーバーです。

- **macOS**: CPU バックエンドのみで実用的ではありません。Mac では llama.cpp か MLX を使ってください。
- **Windows**: ネイティブ対応はありません。WSL2(Ubuntu)上で Linux 手順を実行してください。

### インストール

venv は CodeRouter 設定と同じ `~/.coderouter-t/backends/` 配下に、**バックエンドごとに分けて**作ります。vLLM は `~/.coderouter-t/backends/vllm/` です(vLLM と MLX は依存関係がまったく違うため、venv は必ず分けます)。場所を固定すると `providers.yaml` の `binary:` にそのまま書けます。

`uv`(高速な Python 環境管理ツール)での導入が推奨です:

```bash
uv venv ~/.coderouter-t/backends/vllm --python 3.12 --seed
source ~/.coderouter-t/backends/vllm/bin/activate
uv pip install vllm --torch-backend=auto
```

`pip` でも可:

```bash
python3.12 -m venv ~/.coderouter-t/backends/vllm
source ~/.coderouter-t/backends/vllm/bin/activate
pip install vllm
```

### 動作確認

```bash
python -c "import vllm; print(vllm.__version__)"
# モデルを指定して起動(初回は Hugging Face からダウンロード)
python -m vllm.entrypoints.openai.api_server --model Qwen/Qwen2.5-7B-Instruct --port 8080
```

> 新しい CLI の `vllm serve Qwen/Qwen2.5-7B-Instruct --port 8080` でも同じ OpenAI 互換サーバーが起動します。Launcher は環境に依存しない `python -m vllm.entrypoints.openai.api_server` 形式を使います(どちらも実体は同じ)。

### Launcher との連携

Launcher は vLLM を `<python> -m vllm.entrypoints.openai.api_server` の形で起動します。`providers.yaml` の `backends.vllm.binary` には、上で作った venv の python を指定します:

```yaml
backends:
  vllm:
    binary: ~/.coderouter-t/backends/vllm/bin/python
```

### よくあるつまずき

- **CUDA ドライバ / Toolkit のバージョン不一致**でインストールや起動に失敗することがあります。`nvidia-smi` で GPU とドライバを確認してください。
- macOS で遅い・動かないのは仕様です。Mac では llama.cpp / MLX を使ってください。

---

## 3. MLX

Apple 製の機械学習フレームワーク MLX を使った推論サーバー `mlx_lm.server` を用意します。Apple Silicon の Mac で一段速く動きます。

### 対応環境

- **macOS 14.0 以降**、**Apple Silicon (M1 以降)** のみ。
- **ネイティブ(arm64)の Python 3.10 以降**が必要です。Intel Mac・Rosetta 経由の x86 Python では動きません。

### インストール

venv は `~/.coderouter-t/backends/mlx/` に作ります(vLLM とは別の venv。バックエンドごとに分けます):

```bash
python3 -m venv ~/.coderouter-t/backends/mlx
source ~/.coderouter-t/backends/mlx/bin/activate
pip install mlx-lm
```

### 動作確認

```bash
# native (arm) Python であることを確認 — "arm" と出れば OK
python -c "import platform; print(platform.processor())"
# インストール確認
python -c "import mlx_lm; print('mlx-lm OK')"
# モデルを指定して起動(初回は Hugging Face からダウンロード)
mlx_lm.server --model mlx-community/Qwen2.5-7B-Instruct-4bit --port 8080
curl http://localhost:8080/v1/models
```

### モデル形式の注意

**MLX は GGUF を読めません。** llama.cpp 用の `.gguf` ファイルは MLX では使えません。Hugging Face の [`mlx-community`](https://huggingface.co/mlx-community) が配布する **MLX 形式のモデル**を使ってください。`mlx_lm.server --model mlx-community/<モデル名>` のようにリポジトリ ID を直接指定すると、初回に自動ダウンロードされます。

### Launcher との連携

Launcher は MLX を `<python> -m mlx_lm.server` の形で起動します。`providers.yaml` の `backends.mlx.binary` には、上で作った venv の python を指定します:

```yaml
backends:
  mlx:
    binary: ~/.coderouter-t/backends/mlx/bin/python
```

### よくあるつまずき

- **`platform.processor()` が `arm` 以外**(`i386` / `x86_64`)の場合、Rosetta 経由の x86 Python です。ターミナル.app を Finder で「情報を見る」→「Rosetta を使用して開く」のチェックを外し、ネイティブの Python を入れ直してください。
- macOS が 14.0 未満では PyPI 版がインストールできません。OS を更新してください。
- 推奨値ボタンの `-ngl` などは llama.cpp 専用フラグです。MLX は統合メモリ前提のため起動時の調整フラグは不要です。

---

## インストール後 — Launcher で起動する

バックエンドが入ったら、CodeRouter Launcher からモデルを選んで起動できます。`providers.yaml` の `launcher:` ブロック設定と Launcher の起動・Claude Code 接続までの通し手順は、[Launcher クイックスタート](./launcher-quickstart.md) にまとめてあります。

---

## 関連ドキュメント

- [Launcher クイックスタート](./launcher-quickstart.md) — 導入後の設定〜起動
- [Launcher ガイド(Web版・デスクトップGUI版)](./launcher.md)
- [llama.cpp 直接接続ガイド](./llamacpp-direct.md)
- [CodeRouter クイックスタート](../start/quickstart.md)

# Launcher クイックスタート — バックエンド導入から起動まで

> English: [`launcher-quickstart.en.md`](./launcher-quickstart.en.md)

CodeRouter Launcher を初めて使うための手引きです。Launcher が起動・管理するバックエンド（llama.cpp / vLLM / mlx）の導入から、Launcher の起動・Claude Code 接続までを通しで説明します。

対象プラットフォーム: macOS / Linux / Windows

---

## 全体の流れ

1. バックエンド（**llama.cpp** / **vLLM** / **mlx** のいずれか）をインストール
2. モデルを用意
3. `providers.yaml` に `launcher:` ブロックを書く
4. Launcher を起動（デスクトップGUI版 または Web版）
5. Launcher からバックエンド＋CodeRouter を起動 → Claude Code を接続

バックエンドは 1 つあれば始められます。**迷ったら llama.cpp を推奨** — 全 OS で動き、`.gguf` モデルが豊富で、セットアップが軽量です。

---

## 1. バックエンドをインストール

Launcher が起動する推論バックエンドを 1 つ入れます。**迷ったら llama.cpp** — 全 OS で動き、`.gguf` モデルが豊富です。

| バックエンド | 最短インストール | 対応 |
|---|---|---|
| **llama.cpp** | `brew install llama.cpp`（macOS / Linux）/ `winget install ggml.llamacpp`（Windows） | 全 OS |
| **MLX** | `pip install mlx-lm`（venv 推奨） | macOS / Apple Silicon |
| **vLLM** | `uv pip install vllm`（venv 推奨） | Linux + NVIDIA GPU |

複数のインストール方法・OS 別の詳細・動作確認・つまずき集は **[バックエンド インストール手順書](./install-backends.md)** にまとめてあります。

> vLLM / MLX は専用の Python 仮想環境（venv）が必要です。venv はバックエンドごとに `~/.coderouter-t/backends/<バックエンド名>/`（例: `~/.coderouter-t/backends/vllm/`）に分けて作る方針です。詳細は同手順書を参照。

---

## 2. モデルを用意

- **llama.cpp** — `.gguf` 形式。Hugging Face などから入手
- **MLX** — MLX 形式（`mlx-community` 配布のもの）。`.gguf` は読めません
- **vLLM** — Hugging Face のモデル ID、またはローカルパス

`.gguf` 等のローカルファイルは 1 つのディレクトリ（例: `~/llm/models/`）にまとめておきます。サブフォルダも再帰的にスキャンされます。

---

## 3. providers.yaml に launcher ブロックを書く

Launcher はモデル一覧・オプションプロファイル・バイナリパスを `~/.coderouter-t/providers.yaml` の `launcher:` ブロックから読み込みます。

```yaml
# ~/.coderouter-t/providers.yaml
launcher:
  model_dirs:
    - ~/llm/models                      # .gguf 等を再帰検索
  backends:
    llama.cpp:
      # ソースビルドした場合はフルパスを指定。
      # Homebrew / winget なら backends ごと省略可（PATH から自動解決）。
      binary: ~/llama.cpp/build/bin/llama-server
    vllm:
      binary: ~/.coderouter-t/backends/vllm/bin/python   # vLLM を入れた venv
  option_profiles:
    llama.cpp:
      - name: "GPU フル活用"
        args:
          "-ngl": 99
          "--ctx-size": 32768
```

テンプレートは `launcher_profiles.yaml.example` をコピーして始められます。設定項目の詳細は [Launcher ガイドの設定リファレンス](./launcher.md#設定リファレンス) を参照してください。

---

## 4. Launcher を起動

Launcher には 2 種類あります。初回は **デスクトップGUI版** が簡単です（CodeRouter 自体もそこから起動できます）。

### デスクトップGUI版 — ブラウザ不要

CodeRouter のリポジトリ直下で:

```bash
python3 launcher_gui.py
# または CodeRouter の venv 経由（PyYAML を確実に使う）
uv run python launcher_gui.py
```

ウィンドウが開いたら:

1. MODELS から使うモデルをクリック（メモリ的に `✓ 推奨` のものが安心）
2. オプションプロファイルを選び「▶ llama.cpp / vllm / mlx 起動」
3. 上部バーの「▶ CodeRouter 起動」
4. 表示される接続文字列をコピー

詳細は [Launcher ガイド](./launcher.md)。

### Web版 — CodeRouter 稼働中の運用 UI

Web版は CodeRouter の中で動くため、先に CodeRouter を起動します:

```bash
coderouter-t serve --port 8088
```

ブラウザで `http://localhost:8088/launcher` を開き、モデルを選んで「▶ 起動」します。

Web版で起動した backend は `providers.yaml` を編集しなくても provider として自動登録されます(v2.7.4)。`X-CodeRouter-Profile: launcher` を指定すればすぐにルーティング対象になります。詳細は [Launcher ガイド](./launcher.md)。

> **llama.cpp + MTP対応 gguf を使うなら**: LAUNCH フォームの「MTP/draft gguf」「MTP」欄を空欄・`auto` のままにしておけば speculative decoding を自動検出します。詳細は [MTP / speculative decoding](./launcher.md#mtp--speculative-decoding-llamacpp)。

> **複数 GPU を積んでいるなら**: LAUNCH フォームの「🔍 検出」で `--device` / `--tensor-split` を選択でき、「📊 ベンチスイープ」で構成ごとの性能を自動比較できます(いずれも llama.cpp のみ)。詳細は [デバイス選択](./launcher.md#デバイス選択-llamacpp) / [ベンチスイープ](./launcher.md#ベンチスイープ-llamacpp)。

> **llama.cpp を CUDA / Vulkan / ROCm 別にビルドしているなら**: `backends` に `llama.cpp-cuda` などを登録すると「バックエンド」セレクトで起動ごとにビルドを選べます(v2.11.0+)。ビルドによって見えるデバイスが変わるので、モデルごとに最適なビルドを選べます。詳細は [特化ビルドの切り替え](./launcher.md#特化ビルドの切り替え-llamacpp)。

---

## 5. Claude Code から使う

CodeRouter が稼働したら、Claude Code を CodeRouter に向けて起動します:

```bash
ANTHROPIC_BASE_URL=http://localhost:8088 ANTHROPIC_AUTH_TOKEN=dummy claude
```

デスクトップGUI版では、この接続文字列が画面上部に表示されコピーできます。

---

## つまずいたら

| 症状 | 対処 |
|---|---|
| 起動ボタンがグレーアウト | バックエンドのバイナリが見つからない。`backends.<name>.binary` にフルパスを設定 |
| モデル一覧が空 | `launcher.model_dirs` を設定し、`.gguf` 等が入っているか確認 |
| `PyYAML が見つかりません`（デスクトップ版） | `uv run python launcher_gui.py` で CodeRouter の venv から実行 |
| vLLM が macOS で遅い／動かない | vLLM は Linux/CUDA 向け。macOS では llama.cpp を使う |
| モデルに `⚠ メモリ厳しい` と出る | 搭載メモリに対しモデルが大きい。より小さい量子化版を選ぶ |

さらに詳しいトラブルシューティングは [Launcher ガイド](./launcher.md) を参照してください。

---

## 関連ドキュメント

- [バックエンド インストール手順書（llama.cpp / vLLM / MLX）](./install-backends.md)
- [Launcher ガイド（Web版・デスクトップGUI版）](./launcher.md)
- [CodeRouter クイックスタート](../start/quickstart.md)
- [llama.cpp 直接接続ガイド](./llamacpp-direct.md)

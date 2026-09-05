<h1 align="center">CodeRouter-t</h1>

<p align="center">
  <strong>ローカル LLM で Claude Code を動かすと壊れる問題、<br>ルーター 1 つで直します。</strong>
</p>

<p align="center">
  <a href="https://github.com/OrgaiCom/CodeRouter/actions/workflows/ci.yml"><img src="https://github.com/OrgaiCom/CodeRouter/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://pypi.org/project/coderouter-t/"><img src="https://img.shields.io/pypi/v/coderouter-t?include_prereleases&color=blue&label=pypi" alt="pypi"></a>
  <a href=""><img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="python"></a>
  <a href=""><img src="https://img.shields.io/badge/deps-5-brightgreen" alt="deps"></a>
  <a href=""><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license"></a>
</p>

<p align="center">
  <a href="./README.en.md">English</a> · <strong>日本語</strong> · <a href="./docs/start/quickstart.md">10 分で動かす</a> · <a href="./docs/concepts/architecture.md">設計詳細</a>
</p>

---

## 何ができるか — 30 秒で

```
あなたのエージェント (Claude Code / codex / agy)
        │
        ▼
  ┌─ CodeRouter-t ─┐
  │  修復 + ガード │──→  ① ローカル (Ollama — 無料・最速)
  │  監視 + 診断  │──→  ② 無料クラウド (OpenRouter / NIM)
  │  自動フォールバック │──→  ③ 有料 (Claude — opt-in 時のみ)
  └──────────────┘
```

**やってくれること:**

- ローカルモデルが壊した tool calling を Claude Code に届く前に修復する
- 8 時間回しても止まらないように 6 種類のガードで守る
- 1 つ目が落ちたら自動で次のプロバイダに切り替える
- 有料 API は明示的に許可したときだけ使う (デフォルトは無料のみ)
- 何がおかしいか `coderouter-t doctor` コマンド一発で診断する

---

## 「Ollama に直結できるのに、なぜルーター?」

2026 年、Ollama (v0.14+) / LM Studio (0.4.1+) / llama.cpp / vLLM は Anthropic 互換 `/v1/messages` を標準装備しました。`ANTHROPIC_BASE_URL` を直接向ければ Claude Code は一応動きます。

**でも直結では、こうなります:**

| 直結の現実 | CodeRouter-t 経由 |
|---|---|
| 壊れた tool call は壊れたまま届く | 届く前に修復 |
| backend が落ちたらセッション終了 | ローカル → 無料 → 有料へ自動フォールバック |
| 長時間で context 溢れ・drift・ループ | 6 系統ガード + self-healing |
| モデル名がハードコード (リタイアで即エラー) | プロファイルで抽象化、差し替え 1 行 |
| 何が悪いか分からない | `doctor` 7 プローブ + `/dashboard` + audit/replay |

直結で困っていないなら CodeRouter は不要です。**長時間・無人・弱いモデル**のどれかに当てはまったら、戻ってきてください。

---

## インストール (3 行)

```bash
# 1. サンプル設定を置く
mkdir -p ~/.coderouter-t
curl -fsSL https://raw.githubusercontent.com/OrgaiCom/CodeRouter/main/examples/providers.yaml \
  > ~/.coderouter-t/providers.yaml

# 2. 起動 (Python 3.12+)
uvx --from coderouter-t coderouter-t serve --port 8088
```

恒久インストールしたい場合: `uv tool install coderouter-t`

---

## Claude Code で使う

```bash
# ターミナル 1
coderouter-t serve --port 8088

# ターミナル 2
ANTHROPIC_BASE_URL=http://localhost:8088 ANTHROPIC_AUTH_TOKEN=dummy claude
```

これだけ。Claude Code はいつも通り動きますが、裏ではローカルの Ollama が答えています。

**VSCode の統合ターミナルから使う場合** は、環境変数の書き忘れを避けるため `coderouter-t vscode-init` が便利です。プロジェクトルートで 1 回叩くと `.vscode/settings.json` に `terminal.integrated.env.*` をマージ書き込みするので、以後 VSCode のターミナルで `claude` と打つだけで通ります。Cline / Roo Code / Continue.dev の設定コピペも含めて → [VSCode 連携ガイド](./docs/guides/vscode.md)

---

## 自分に必要？

| あなたの状況 | CodeRouter は？ |
|---|---|
| Claude Code + ローカル Ollama で tool calling が壊れる | **必須** — tool 修復 (+ 必要なら wire 変換) |
| Claude Code + ローカルで長時間回すと止まる | **必須級** — 6 系統ガード + self-healing |
| Ollama v0.14+ / LM Studio にネイティブ直結で動いてる | **便利** — 直結に無い fallback / ガード / 診断を追加 (passthrough で翻訳ゼロ) |
| codex / agy + Ollama 直繋ぎで動いてる | オプション — フォールバックが欲しいなら |
| Claude API を直接叩いてて問題ない | 不要 |

詳細は → [要否判定ガイド](./docs/start/when-do-i-need-coderouter.md)

---

## 主な機能

### 修復と接続

| 機能 | 何をしてくれるか |
|---|---|
| **Tool-call 修復** | ローカルモデルがテキストで吐いた JSON を正しい tool_use ブロックに復元 |
| **3 層フォールバック** | ローカル → 無料クラウド → 有料の順に自動切替 |
| **出力フィルタ** | `<think>` タグ漏れ、stop marker 漏れ、byte-fallback (`<0xNN>`) を自動除去/修復 |
| **Wire 翻訳** | Anthropic 形式 ↔ OpenAI 形式を自動変換 (ネイティブ `/v1/messages` 対応 backend は passthrough で翻訳ゼロ) |

### 長時間運用ガード

| ガード | 何から守るか |
|---|---|
| **Context Budget** | メッセージが溜まりすぎて context window 溢れ → 自動 trim |
| **Drift Detection** | モデルの応答品質が徐々に劣化 → 別 provider に切替 or KV cache flush (6 シグナル、`goal_mode` で目標達成停滞も検知) |
| **Self-healing** | backend が落ちた → 自動除外 + restart + 回復 probe で自動復帰 |
| **Tool Loop Guard** | 同じツールを無限に呼び続ける → 検知して停止 |
| **Memory Pressure** | OOM を出した backend を一時除外 → チェーンの次の provider へフォールスルー |
| **Mid-stream Guard** | 応答途中で落ちた → 溜まったテキストを安全に返却 |

### 診断と可視化

| 機能 | 何がわかるか |
|---|---|
| **`coderouter-t doctor`** | プロバイダの問題を 7 プローブで即診断 + 修正パッチ出力 |
| **`/dashboard`** | ブラウザで今何が起きてるかリアルタイム確認 |
| **`coderouter-t audit`** | guard 発火履歴を検索 |
| **`coderouter-t replay`** | provider 切替の効果を統計比較 (A/B 分析) / `--suggest-rules` でルール最適化提案 |
| **Continuous Probe** | idle 時も定期的に backend を監視 |

### 言語税トラッキング — v2.6.0

日本語などの CJK テキストは、クラウドのトークナイザだと「同じ意味の英語」より多くのトークンを消費します（**実測: GPT-4o 系 o200k で平均 1.6 倍、GPT-4 系 cl100k で平均 2.0 倍**）。ローカル LLM は課金されないので、この「言語税」はクラウド利用時だけ効いてきます。CodeRouter v2.6.0 はこれを **計測・ルーティング回避・可視化** します。

| 機能 | 何をしてくれるか |
|---|---|
| **言語税の計測** | プロバイダに `tokenizer_path`（ローカルの `tokenizer.json`）を指定すると、char/4 ヒューリスティック比の実トークン倍率と割増 USD を算出（ネットワーク不要・未設定なら無効） |
| **`cjk_ratio_min` ルーティング** | CJK 比率が高いリクエストを自動でローカル LLM（課金ゼロ）へ。コードや英語はクラウドへ |
| **ダッシュボード可視化** | `/dashboard` の「Cost & Language Tax」パネルで総支出・キャッシュ節約・言語税をリアルタイム表示 |

```yaml
# providers.yaml — CJK 多めのターンはローカルへ自動回避
auto_router:
  rules:
    - match: { cjk_ratio_min: 0.3 }   # 日本語が3割以上 → ローカル
      profile: local
    - match: { has_tools: true }      # ツール使用 → クラウド
      profile: cloud
  default_rule_profile: cloud

providers:
  - name: cloud-sonnet
    kind: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-6
    tokenizer_path: ~/.coderouter-t/tokenizers/sonnet.json   # 言語税の正確計測（任意）
```

詳細 → [言語税ガイド](./docs/guides/language-tax.md)

### Launcher — llama.cpp / vllm 起動 UI

`http://localhost:8088/launcher` で開けるブラウザ UI。llama.cpp や vllm を GUI で起動・管理できます。

| 機能 | 詳細 |
|---|---|
| **モデルスキャン** | `model_dirs` に指定したフォルダを再帰スキャンして `.gguf` / `.safetensors` をリスト化 |
| **オプションプロファイル** | `providers.yaml` に名前付きプリセットを定義 → ドロップダウンで選択するだけ |
| **複数プロセス管理** | llama.cpp と vllm を同時に起動し、ポートごとに独立管理 |
| **ログビューア** | 各プロセスの stdout/stderr をブラウザ内でリアルタイム確認 |
| **provider 自動同期** (v2.7.4) | 起動したバックエンドを provider として自動登録(`launcher-llamacpp-8085` 等)。providers.yaml 無編集で `X-CodeRouter-Profile: launcher` からルーティング可能。メモリ内のみ・serve と同寿命 |
| **モデル名パススルー** (v2.7.4) | `model: ""` の provider は `/v1/models` が上流のロード中モデル ID(gguf 名)をそのまま返す。gguf を差し替えても config 編集不要 — 外部ベンチからモデルを識別できる |
| **特化ビルドの切り替え** (v2.11.0) | `llama.cpp-cuda` / `-vulkan` / `-rocm` を `backends` に登録すると、起動ごとにどのビルドの `llama-server` を使うか選べる。ビルド別にデバイス検出・option_profiles・ベンチスイープが独立(書かなければ従来どおり) |

```yaml
# providers.yaml に追記するだけで有効になる
launcher:
  model_dirs:
    - ~/models
  option_profiles:
    llama.cpp:
      - name: "GPU フル活用"
        args:
          "-ngl": 99
          "--ctx-size": 4096
    vllm:
      - name: "標準"
        args:
          "--dtype": "auto"
          "--max-model-len": 4096
```

詳細 → [Launcher ガイド](./docs/backends/launcher.md)

---

## 設定例 (最小)

```yaml
# ~/.coderouter-t/providers.yaml
default_profile: claude-code

profiles:
  - name: claude-code
    providers: [ollama-local, openrouter-free]

providers:
  - name: ollama-local
    kind: openai_compat
    base_url: http://localhost:11434/v1
    model: qwen3-coder:7b

  - name: openrouter-free
    kind: openai_compat
    base_url: https://openrouter.ai/api/v1
    model: qwen/qwen3-coder:free
    api_key_env: OPENROUTER_API_KEY
```

もっと詳しい設定 → [利用ガイド](./docs/guides/usage-guide.md) · [設計詳細](./docs/concepts/architecture.md)

---

## ドキュメント

| やりたいこと | ドキュメント |
|---|---|
| すぐ動かす | [Quickstart](./docs/start/quickstart.md) |
| 使いこなす | [利用ガイド](./docs/guides/usage-guide.md) |
| 無料で回す | [無料枠ガイド](./docs/guides/free-tier-guide.md) |
| llama.cpp / vllm を GUI で起動 | [Launcher ガイド](./docs/backends/launcher.md) |
| 言語税を計測・回避する | [言語税ガイド](./docs/guides/language-tax.md) |
| VSCode / Cline / Continue から使う | [VSCode 連携ガイド](./docs/guides/vscode.md) |
| 別の PC から安全に繋ぐ | [リモートアクセスガイド](./docs/guides/remote-access.md) |
| 詰まった | [トラブルシューティング](./docs/guides/troubleshooting.md) |
| 設計を知りたい | [アーキテクチャ詳細](./docs/concepts/architecture.md) |
| 全リリース履歴 | [CHANGELOG](./CHANGELOG.md) |

English: [Quickstart](./docs/start/quickstart.en.md) · [Usage guide](./docs/guides/usage-guide.en.md) · [Free-tier](./docs/guides/free-tier-guide.en.md) · [Troubleshooting](./docs/guides/troubleshooting.en.md)

---

## トラブルシューティング (早見表)

**まず**: `coderouter-t doctor --check-model <provider名>` を走らせてください。大体これで原因がわかります。

| 症状 | 原因 | 詳細 |
|---|---|---|
| 401 エラー | API キー未設定 / `.env` に `export` 忘れ | [§1](./docs/guides/troubleshooting.md#1-起動設定で踏みやすい-5-つの罠-v162-追加) |
| 返信が空 / 意味不明 | Ollama の `num_ctx` が 2048 に切り詰め | [§3](./docs/guides/troubleshooting.md#3-ollama-初心者--サイレント失敗-5-症状-v07-c) |
| `<think>` タグが漏れる | `output_filters: [strip_thinking]` を付ける | [§3](./docs/guides/troubleshooting.md#3-ollama-初心者--サイレント失敗-5-症状-v07-c) |
| Claude Code でツール呼び出しがおかしい | tool-call 修復が効いてない | [§4](./docs/guides/troubleshooting.md#4-claude-code-連携で踏みやすい罠-v162-追加) |

`http://localhost:8088/dashboard` を開いておくと、ほとんどの問題が見て 10 秒でわかります。

---

## 技術スペック

- **ランタイム依存**: `fastapi` / `uvicorn` / `httpx` / `pydantic` / `pyyaml` の 5 個のみ
- **テスト**: 1,500+ 本(ランタイム依存 5 個は v1 系から不変)
- **対応 OS**: macOS (Apple Silicon 推奨) / Linux / Windows WSL2
- **対応 backend**: Ollama / llama.cpp / LM Studio / vLLM / MLX-LM / OpenRouter / NVIDIA NIM / Anthropic API
- **外部エージェント CLI**: `agent_cli` provider として Claude Code / codex / grok / antigravity の4種を束ねて呼び出せる(要 `coderouter-plugin-agents`。詳細 → [external-agents ガイド](./docs/backends/external-agents.md))
- **プラグイン**: compress / memory / agents の3種を opt-in で追加可能(コアの依存は増えない。一覧・導入方法 → [docs/README.md](./docs/README.md#対応プラグイン--plugins))
- **ライセンス**: MIT

---

## エコシステム

CodeRouter-t は backend ルーター層として独立して動きます。`OPENAI_BASE_URL` を CodeRouter-t に向けるだけで、他プロジェクトを無改造で吸収:

- **[Voice Bridge](https://github.com/zephel01/voice-bridge)** — リアルタイム音声翻訳 + AI 音声チャット。CodeRouter 経由でローカル LLM のフォールバックを効かせると、ずんだもんが沈黙しなくなる

---

## 言語設定

CodeRouter-t の人間向けメッセージ（CLI / Doctor / 起動警告）は日本語と英語を切り替えられます。JSONログは常に英語のままです。

```bash
# 日本語で表示（推奨: 日本語OSでは自動で日本語になります）
CODEROUTER_T_LANG=ja coderouter-t serve
CODEROUTER_T_LANG=ja coderouter-t doctor --check-model local

# 英語で表示
CODEROUTER_T_LANG=en coderouter-t serve
```

- `CODEROUTER_T_LANG=ja` / `en` を明示すると最優先されます
- 未設定時は `LANG` / `LC_MESSAGES` の OSロケールから自動判定（`ja_JP.UTF-8` → 日本語）
- 不正な値は英語にフォールバックします

## Security

シークレットは環境変数に置きます。[`docs/security.md`](./docs/guides/security.md) に完全な方針と報告手順があります。

## License

MIT

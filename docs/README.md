# CodeRouter ドキュメント / Documentation

English: [`README.en.md`](./README.en.md)

CodeRouter の公開ドキュメント索引です。「やりたいこと」から読むべきページを引けるよう整理しています。
Index of CodeRouter's public documentation — find the right page by what you want to do.

> 開発者向けの内部メモ・記事原稿は `inside/` と `articles/` にあります(ローカル専用 / gitignored、公開リポジトリには含まれません)。
> Developer-internal notes and article drafts live in `inside/` and `articles/` (local-only, not shipped in the public repo).

---

## こういう時はこれを読む / Quick start by goal

| やりたいこと / Goal | 読むもの / Read |
|---|---|
| 今すぐ動かしたい / Get running now | [start/quickstart](start/quickstart.md) |
| 自分に必要か知りたい / Is it for me? | [start/when-do-i-need-coderouter](start/when-do-i-need-coderouter.md) |
| 無料で運用したい / Run for free | [guides/free-tier-guide](guides/free-tier-guide.md) |
| 機能を一通り知りたい / Learn the features | [guides/usage-guide](guides/usage-guide.md) |
| 言語税を計測・回避したい / Measure & avoid the language tax | [guides/language-tax](guides/language-tax.md) |
| エラーで詰まった / Something broke | [guides/troubleshooting](guides/troubleshooting.md) |
| VSCode / Cline / Continue から使いたい / Use from VSCode extensions | [guides/vscode](guides/vscode.md) |
| ローカル LLM を起動したい / Launch a local LLM | [backends/launcher-quickstart](backends/launcher-quickstart.md) |
| 低メモリ環境(8–16GB)で動かしたい / Run on a low-memory (8–16GB) host | [low-memory-integration](low-memory-integration.md) |
| APIキー・機密の扱い / Secrets & security | [guides/security](guides/security.md) |
| 別のPCから安全に繋ぎたい / Connect securely from another PC | [guides/remote-access](guides/remote-access.md) |
| 仕組みを理解したい / Understand the design | [concepts/architecture](concepts/architecture.md) |
| プラグインで拡張したい / Extend with plugins | [対応プラグイン / Plugins](#対応プラグイン--plugins) |

---

## 構成 / Layout

```
docs/
├── start/             はじめに / Getting started
├── guides/            使い方ガイド / How-to guides
├── backends/          ローカルLLMバックエンド / Local LLM backends
├── concepts/          設計・内部動作 / Architecture & internals
├── designs/           設計ドキュメント / Design docs
├── retrospectives/    リリース振り返り / Release retrospectives
├── evidence/          検証ログ / Verification logs
├── openrouter-roster/ OpenRouter モデル一覧 / OpenRouter model roster
└── assets/            画像など / Images
```

各ドキュメントは日本語版 (`.md`) と英語版 (`.en.md`) が揃っているものがあります。
Many documents have a Japanese version (`.md`) and an English version (`.en.md`).

---

## 1. はじめに / Getting started — `start/`

初めて CodeRouter に触れる人向け。 / For first-time users.

- **quickstart** — 最短セットアップで動かす / Get running in one sitting · [日本語](start/quickstart.md) · [English](start/quickstart.en.md)
- **when-do-i-need-coderouter** — 自分に必要かを判断する / Decide whether you need it · [日本語](start/when-do-i-need-coderouter.md) · [English](start/when-do-i-need-coderouter.en.md)

## 2. 使い方ガイド / How-to guides — `guides/`

日常的に使いこなすためのガイド。 / Day-to-day usage.

- **usage-guide** — 機能を一通り使いこなす / Full feature guide · [日本語](guides/usage-guide.md) · [English](guides/usage-guide.en.md)
- **language-tax** — 日本語の言語税を計測・ルーティング回避・可視化 / Measure, route around, and visualize the CJK language tax · [日本語](guides/language-tax.md) · [English](guides/language-tax.en.md)
- **free-tier-guide** — NVIDIA NIM × OpenRouter Free でコストゼロ運用 / Zero-cost operation · [日本語](guides/free-tier-guide.md) · [English](guides/free-tier-guide.en.md)
- **troubleshooting** — つまずいたときの解決集 / Fixing problems · [日本語](guides/troubleshooting.md) · [English](guides/troubleshooting.en.md)
- **security** — シークレット管理とセキュリティ方針 / Secrets handling & security posture · [日本語](guides/security.md) · [English](guides/security.en.md)
- **remote-access** — 別の PC から CodeRouter に安全に繋ぐ(SSHトンネル・Tailscaleなど4つの選択肢) / Reach CodeRouter safely from another machine (four options: SSH tunnel, Tailscale, etc.) · [日本語](guides/remote-access.md) · [English](guides/remote-access.en.md)
- **subagent-routing** — Claude Code のサブエージェントをモデルごとに振り分ける / Route Claude Code sub-agents to different models · [日本語](guides/subagent-routing.md) · [English](guides/subagent-routing.en.md)
- **vscode** — VSCode 統合ターミナル(Claude Code)と Cline / Roo / Continue.dev から接続する / Connect from VSCode's integrated terminal (Claude Code) and Cline / Roo / Continue.dev. Includes the `coderouter vscode-init` scaffolder (v2.10.0) · [日本語](guides/vscode.md)

## 3. ローカル LLM バックエンド / Local LLM backends — `backends/`

ローカル推論バックエンドの導入・起動・接続。 / Installing, launching, and connecting local inference backends.

- **install-backends** — llama.cpp / vLLM / MLX のインストール手順 / Installing the three backends · [日本語](backends/install-backends.md) · [English](backends/install-backends.en.md)
- **launcher-quickstart** — バックエンド導入から起動までの最短手順 / Install a backend and launch · [日本語](backends/launcher-quickstart.md) · [English](backends/launcher-quickstart.en.md)
- **launcher** — Launcher ガイド(Web版・デスクトップGUI版) / Launcher guide (Web & Desktop GUI) · [日本語](backends/launcher.md) · [English](backends/launcher.en.md)
- **external-agents** — 外部コーディングエージェント CLI (agent_cli、claude/codex/grok/antigravity の4種対応。v2.9.0 から `coderouter-plugin-agents` の導入が必須) / External coding-agent CLI (agent_cli, 4 CLIs: claude/codex/grok/antigravity; requires `coderouter-plugin-agents` since v2.9.0) · [日本語](backends/external-agents.md) · [English](backends/external-agents.en.md)
- **llamacpp-direct** — llama.cpp に直結する / Connect llama.cpp directly · [日本語](backends/llamacpp-direct.md) · [English](backends/llamacpp-direct.en.md)
- **lmstudio-direct** — LM Studio に直結する / Connect LM Studio directly · [日本語](backends/lmstudio-direct.md) · [English](backends/lmstudio-direct.en.md)
- **claude-code-llamacpp-vllm** — Claude Code を Ollama なしで llama.cpp / vLLM に接続する実践ガイド(設定と典型エラーの直し方) / Connect Claude Code to llama.cpp / vLLM without Ollama, plus fixes for the errors you'll hit in practice · [日本語](backends/claude-code-llamacpp-vllm.md) · [English](backends/claude-code-llamacpp-vllm.en.md)
- **hf-ollama-models** — HuggingFace 配布モデルを Ollama で使う / Use HF models via Ollama · [日本語](backends/hf-ollama-models.md) · [English](backends/hf-ollama-models.en.md)
- **gguf_dl** — GGUF モデルのダウンロードツール / GGUF download helper · [日本語](backends/gguf_dl.md) · [English](backends/gguf_dl.en.md)
- **verify-ollama-0.23.1** — Ollama v0.23.1 実機検証チェックリスト / Ollama verification checklist · [日本語](backends/verify-ollama-0.23.1.md)

## 4. 設計・内部動作 / Architecture & internals — `concepts/`

CodeRouter の仕組みと信頼性機構。 / How CodeRouter works and its reliability mechanisms.

- **architecture** — アーキテクチャ全体像 / Architecture overview · [日本語](concepts/architecture.md) · [English](concepts/architecture.en.md)
- **context-budget** — コンテキスト予算管理 (v2.0.0) / Context budget management · [日本語](concepts/context-budget.md) · [English](concepts/context-budget.en.md)
- **drift-detection** — ドリフト検出 (v2.0-G) / Drift detection · [日本語](concepts/drift-detection.md) · [English](concepts/drift-detection.en.md)
- **partial-stitch** — ストリーム途中の部分ステッチ (v2.0-H) / Mid-stream partial stitching · [日本語](concepts/partial-stitch.md) · [English](concepts/partial-stitch.en.md)
- **continuous-probing** — 継続プロービング (v2.0-I) / Continuous probing · [日本語](concepts/continuous-probing.md) · [English](concepts/continuous-probing.en.md)
- **stream-truncation** — ストリーム断絶検知 (v2.15.0) / Stream truncation detection · [日本語](concepts/stream-truncation.md) · [English](concepts/stream-truncation.en.md)
- **low-memory-integration** — 低メモリ機(8–16GB)向けメモリ予算ガードの統合ガイド。VRAM/RAM 検出・GGUFヘッダ解析・KVキャッシュ試算による事前フィット判定(`off`/`warn`/`fit`)。**実装済み (v2.5.3)** / Integration guide for the proactive memory-budget guard on low-memory (8–16GB) hosts — VRAM/RAM detection, GGUF header introspection, and KV-cache-aware pre-dispatch fit decisions (`off`/`warn`/`fit`). **Implemented (v2.5.3)** · [日本語](low-memory-integration.md) · [English](low-memory-integration.en.md)

## 5. 設計資料・記録 / Design docs & records

### designs/ — 機能の設計ドキュメント / Feature design docs

各ドキュメントの実装状況を併記しています。 / Implementation status is noted for each doc.

- **v1.6-auto-router** — auto_router の初版設計 / Initial design of auto_router. **実装済み (v1.6.0)。マッチャーは現在8種に拡張 / Implemented (v1.6.0); matchers have since expanded to 8 kinds** · [日本語](designs/v1.6-auto-router.md)
- **v1.6-auto-router-verification** — v1.6.0 のリリース検証記録 / Release verification record for v1.6.0. **実施済み (2026-04-22)・アーカイブ / Completed (2026-04-22), archived** · [日本語](designs/v1.6-auto-router-verification.md)
- **external-agents-adapter** — 外部コーディングエージェント CLI アダプタ (Phase 1a〜1d) / External coding-agent CLI adapter (Phase 1a–1d). **実装済み (v2.7.7〜v2.7.10)。ただし in-core 実装は v2.9.0 で削除され、本体は `coderouter-plugin-agents` へ移設済み / Implemented (v2.7.7–v2.7.10); the in-core implementation was removed in v2.9.0 and moved to `coderouter-plugin-agents`** · [日本語](designs/external-agents-adapter.md)
- **agent-cli-plugin-extraction** — agent_cli の外部プラグイン化 (Phase 2a/2b/2c) / Extracting agent_cli into an out-of-tree plugin (Phase 2a/2b/2c). **実装済み (v2.8.0 / v2.8.1 / v2.9.0) / Implemented (v2.8.0 / v2.8.1 / v2.9.0)** · [日本語](designs/agent-cli-plugin-extraction.md)
- **launcher-model-swap** — オンデマンドのモデル起動・TTL アンロード / On-demand model launch & TTL-based unload. **Phase 1 実装済み (v2.9.1)。Phase 2(メモリ会計 + 排他 swap)は未着手 / Phase 1 implemented (v2.9.1); Phase 2 (memory accounting + exclusive swap) not started** · [日本語](designs/launcher-model-swap.md)
- **launcher-multi-build** — llama.cpp の複数ビルド切替 (backend variants) / Switching between multiple llama.cpp builds (backend variants). **実装済み (v2.11.0) / Implemented (v2.11.0)** · [日本語](designs/launcher-multi-build.md)
- **orchestration-companion** — 別プロセスのオーケストレーター構想 / Concept for a separate-process orchestrator. **構想のみ・未着手 / Concept only, not started** · [日本語](designs/orchestration-companion.md)

### retrospectives/ — リリース振り返り / Release retrospectives

- [v0.4](retrospectives/v0.4.md) · [v0.5](retrospectives/v0.5.md) · [v0.5-verify](retrospectives/v0.5-verify.md) · [v0.6](retrospectives/v0.6.md) · [v0.7](retrospectives/v0.7.md) · [v1.0](retrospectives/v1.0.md) · [v1.0-verify](retrospectives/v1.0-verify.md)

### その他 / Other

- **evidence/** — 実機検証ログ / Verification run logs
- **openrouter-roster/** — OpenRouter 利用可能モデル一覧 / OpenRouter model roster — [README](openrouter-roster/README.md) · [変更履歴 / change log](openrouter-roster/CHANGES.md)

---

## 対応プラグイン / Plugins

CodeRouter は v2.3.0 で入った **Plugin SDK** により、別パッケージのプラグインを *opt-in* で読み込めます。`plugins.enabled` に名前を明示したときだけ作動する（サプライチェーン防御）ため、インストールしただけでは何も起きません。各プラグインは独立した PyPI パッケージなので、**コアの依存は一切増えません**。

CodeRouter's **Plugin SDK** (since v2.3.0) loads out-of-tree plugins *opt-in*: a plugin runs only when its name is listed in `plugins.enabled` (supply-chain defense), so installing one does nothing by itself. Each plugin ships as a separate PyPI package, so **the core's dependencies never grow**.

| プラグイン / Plugin | 何をするか / What it does | インストール / Install | リポジトリ / Repo |
|---|---|---|---|
| **compress** | ツール出力(JSON / ログ)を LLM に届く前に圧縮してトークンを削減。原文はローカル保持で可逆(CCR)。`cache-align` で Anthropic プロンプトキャッシュも整列。<br>Compresses tool output (JSON / logs) before it reaches the LLM to cut tokens; originals kept locally and reversible (CCR). `cache-align` also aligns Anthropic prompt caching. | PyPI 未公開。git+https で導入:<br>`uv pip install "coderouter-plugin-compress[accuracy] @ git+https://github.com/zephel01/coderouter-plugin-compress"`<br>Not yet on PyPI — install from git+https. | [coderouter-plugin-compress](https://github.com/zephel01/coderouter-plugin-compress) |
| **memory** | 応答から key facts を抽出して `facts.jsonl` に蓄積し、次セッションの system prompt へ自動注入。「毎回同じ説明」を wire 層で解消。<br>Extracts key facts from responses into `facts.jsonl` and auto-injects them into the next session's system prompt — solving "explain it every time" at the wire layer. | `pip install coderouter-plugin-memory` | [coderouter-plugin-memory](https://github.com/zephel01/coderouter-plugin-memory) |
| **agents** | 外部コーディングエージェント CLI(claude / codex / grok / antigravity)を `kind: agent_cli` の provider として登録する。v2.9.0 で in-core 実装が削除され、このプラグインが必須になった。詳細は [backends/external-agents](backends/external-agents.md)。<br>Registers external coding-agent CLIs (claude / codex / grok / antigravity) as `kind: agent_cli` providers. The in-core implementation was removed in v2.9.0, making this plugin required. See [backends/external-agents](backends/external-agents.en.md). | PyPI 未公開。git+https で導入:<br>`uv pip install "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"`<br>Not yet on PyPI — install from git+https. | [coderouter-plugin-agents](https://github.com/zephel01/coderouter-plugin-agents) |

有効化は `providers.yaml` に追記するだけ。起動ログに `plugin-loaded` が出れば有効です。
Enable by adding to `providers.yaml`; a `plugin-loaded` line in the startup log confirms activation.

```yaml
plugins:
  enabled:
    - compress          # ツール出力を圧縮 / compress tool output
    - compress-stats    # 圧縮率を coderouter stats に出力 / report compression ratio
    - cache-align       # プロンプトキャッシュのブレークポイント整列 / align prompt-cache breakpoints
    - memory            # セッション横断メモリ / cross-session memory
    - agents            # 外部エージェント CLI (agent_cli) / external agent CLIs (agent_cli)
  config:
    compress:
      mode: safe        # off | safe | aggressive
      ccr: true         # 圧縮の可逆復元(既定 on)/ reversible re-expansion (default on)
    memory:
      consolidate_model: qwen3:1.7b
```

`agent_cli` provider を使う場合、`agents` の有効化は opt-in ではなく**必須**です(未導入だと `coderouter-t serve` が起動時エラーになります)。詳細は [backends/external-agents](backends/external-agents.md) を参照してください。
When using an `agent_cli` provider, enabling `agents` is not optional — it's **required** (without it, `coderouter-t serve` fails at startup). See [backends/external-agents](backends/external-agents.en.md) for details.

各プラグインの詳細・設定は上記リポジトリの README を参照してください。
See each plugin's repo README for full configuration.

---

最終更新 / Last updated: 2026-08-10

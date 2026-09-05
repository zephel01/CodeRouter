# CodeRouter — 開発計画 (plan.md)

> **Local-first, free-first, fallback-built-in な LLM ルーター。**
> Claude Code / OpenAI 互換クライアントから単一エンドポイントで叩けて、内部で「ローカル → 無料クラウド → 有料クラウド」の3層 fallback を自動で行う。

最終更新: 2026-08-10 (版況を v2.14.0 に更新 / 現況サマリを status.md へ移管 / リンク切れ修復)
作成者: zephel01
状態: **v2.14.0 リリース済み (2026-08-09)**。Runtime deps: 5 (出荷以来据え置き連続)。

- **今どこにいて次に何をするか (現況 / 未消化タスク / 実装スケジュール / 文書地図)**: [`docs/inside/status.md`](./docs/inside/status.md) ← **まずこれを読む**
- **過去の出荷済みリリース (版履歴の正本)**: [`CHANGELOG.md`](./CHANGELOG.md) を参照
- **未来の方向性 (Vision / 中長期ロードマップ / 市場分析 / 競合分析)**: [`docs/inside/future.md`](./docs/inside/future.md) を参照
- **本ドキュメント**: 出荷済みマイルストーンのスコープ / 設計判断の記録 + ローカル backend 別接続マトリクス + 検討中 / やらないこと

### 現況サマリ

> **現況・未消化タスク・実装スケジュールは [`docs/inside/status.md`](./docs/inside/status.md) へ移管しました (2026-08-10)。**
> plan.md で二重管理すると必ず片方が腐ります (実際、この節は v2.6.1 時点で止まったまま v2.14.0 まで7リリース分ずれていました)。ここには版況を書きません。

plan.md 内の索引:

- 各マイルストーンの設計判断: 版別節 §7〜§13
- 検討中 / やらないこと: 下記「❓ 検討中」「❌ やらないこと」および §15
- 横断タスク: §14、実装ログ: §18
- リスク台帳: §16

> 注: §6.1 全景 / §6.2 リリース履歴 / §13 の版一覧は **v2.6.1 で更新を止めています**。plan.md 冒頭で「版履歴の正本は CHANGELOG.md」と宣言しているのに版一覧を二重に持っていたことが、版況が腐った直接の原因でした。v2.7.0 以降の版は [`CHANGELOG.md`](./CHANGELOG.md) を参照してください。

### ローカル backend 別接続マトリクス + テスト方針 (現役、運用中)

CodeRouter は `kind: openai_compat` と `kind: anthropic` の 2 経路で **Ollama / llama.cpp / LM Studio / vLLM / MLX-LM** いずれにも繋がる設計。**Ollama v0.23.1+ / LM Studio 0.4.12+ は `kind: anthropic` (Anthropic API passthrough) が推奨経路** — 翻訳ゼロで全機能 (tool_use / thinking / cache_control) が透過する。legacy の `kind: openai_compat` も引き続き動作。

| Backend | デフォルトポート | `base_url` (CodeRouter から) | 推奨 kind | 検証ステータス | 専用 doc |
| --- | --- | --- | --- | --- | --- |
| **Ollama** | `11434` | `http://localhost:11434` (Anthropic 互換) / `http://localhost:11434/v1` (OpenAI 互換) | **`anthropic`** (v0.23.1+) | ✅ v0.x 〜 v2.2 通して継続検証。**v0.23.1 Anthropic API 実機検証済み — Gemma 4 全サイズ Level 3 到達** ([検証記録](./docs/backends/verify-ollama-0.23.1.md)) | [`docs/start/quickstart.md`](./docs/start/quickstart.md) / [`docs/guides/troubleshooting.md` §4-2](./docs/guides/troubleshooting.md) |
| **llama.cpp `llama-server`** | `8080` | `http://localhost:8080/v1` | `openai_compat` | ✅ v1.8.3 で実機検証 (Qwen3.6:35b-a3b on Unsloth UD-Q4_K_M、native `tool_calls` 完璧動作) | [`docs/backends/llamacpp-direct.md`](./docs/backends/llamacpp-direct.md) |
| **LM Studio** | `1234` | `http://localhost:1234` (Anthropic 互換) / `http://localhost:1234/v1` (OpenAI 互換) | **`anthropic`** (v0.4.12+) | ✅ v1.8.4 で実機検証 (Qwen3.5/3.6/Qwopus3.5 全動作、Anthropic prompt caching 成立) | [`docs/backends/lmstudio-direct.md`](./docs/backends/lmstudio-direct.md) |
| **vLLM** | `8000` (server start で変更可) | `http://localhost:8000/v1` | `openai_compat` | ⏳ E2E TODO (CUDA / data center GPU 前提、Mac M3 Max は対象外)。**Launcher (v2.5.0+) から起動・管理可能** | [`docs/backends/install-backends.md`](./docs/backends/install-backends.md) |
| **MLX-LM** | `8080` (`mlx_lm.server` 起動) | `http://localhost:8080/v1` | `openai_compat` | ⏳ E2E TODO (Mac native、量子化が Apple Silicon 最適化)。**Launcher (v2.5.1+) から起動・管理可能** (`python -m mlx_lm.server`) | [`docs/backends/install-backends.md`](./docs/backends/install-backends.md) |

### 共通の検証手順 (どの backend にも適用可)

1. **server 起動** — backend ごとの方法でモデルをロード + OpenAI 互換 API を listen させる
2. **CodeRouter `providers.yaml` に provider 定義を追加** — `kind: openai_compat` + 該当 `base_url`、必要に応じて `capabilities.thinking: true` (reasoning モデル時)
3. **`coderouter doctor --check-model <name>`** で 6 probe (`auth+basic-chat / num_ctx / tool_calls / thinking / reasoning-leak / streaming`) を回す
4. **CodeRouter 経由 Anthropic 互換 curl で end-to-end 1 round-trip** を確認
5. NEEDS_TUNING が出たら `--apply` で patch 自動適用 → 再 probe

### 各 backend で確認すべき固有ポイント

| Backend | 確認ポイント |
|---|---|
| **Ollama** | v0.23.1+ は `kind: anthropic` (Anthropic `/v1/messages`) 推奨。`/api/chat` (native) と `/v1/chat/completions` (OpenAI-compat) で挙動差異あり、`extra_body.options.num_ctx` の効き、Modelfile の `PARAMETER num_ctx` 焼き込み、新 architecture の `unknown model architecture` 500 エラー。**Anthropic 経路では thinking blocks が `type: "thinking"` で返るため max_tokens 設定に注意** (thinking が max_tokens を消費する) |
| **llama.cpp** | `--jinja` で chat template が効くか、`reasoning_content` フィールド名 (Ollama は `reasoning`)、Metal / CUDA build flag、Unsloth Dynamic Quantization (UD-Q4_K_M) の精度優位 |
| **LM Studio** | OpenAI 互換 endpoint の挙動が Ollama / llama.cpp と微妙に違う可能性、reasoning フィールドの命名、UI で context length / max tokens を server start 時に指定する必要 |
| **vLLM** | `--enable-auto-tool-choice` フラグ、tool spec 形式 (Hermes / Mistral / Llama3 のどれを採用するか)、Continuous batching の動作 |
| **MLX-LM** | Apple Silicon 専用、`mlx_lm.server` 起動時の量子化指定、tool_calls 対応状況 (やや限定的の可能性) |

### Anthropic API ネイティブ接続 (推奨経路、2026-05 確立)

**Ollama v0.23.1+** と **LM Studio 0.4.12+** が Anthropic 互換 `/v1/messages` をネイティブサポート。CodeRouter からは `kind: anthropic` で翻訳ゼロ passthrough が推奨経路:

```yaml
# Ollama v0.23.1+ (推奨)
ollama-anthropic:
  kind: anthropic
  base_url: http://localhost:11434
  model: gemma4:12b

# LM Studio 0.4.12+ (推奨)
lmstudio-anthropic:
  kind: anthropic
  base_url: http://localhost:1234
  model: gemma-4-12b
```

この経路では tool_use / thinking blocks / cache_control が全て Anthropic ネイティブ形式で透過する。**翻訳レイヤーを経由しないため、wire 上の情報欠落がゼロ**。

検証記録: Ollama → [`docs/verify-ollama-0.23.1.md`](./docs/backends/verify-ollama-0.23.1.md)、LM Studio → [`docs/backends/lmstudio-direct.md`](./docs/backends/lmstudio-direct.md)、providers.yaml sample → `examples/providers.yaml`。

### 今後の作業 (docs / examples 整備)

> **未消化タスクの正本は [`docs/inside/status.md`](./docs/inside/status.md) §3 に移しました (2026-08-10)。** 優先度・依存関係・スケジュールはそちらを見てください。
> 以下は plan.md 固有の項目のうち、まだ残っているものだけです。

**2026-08-10 に解消済み (status.md P1 として実施):**

- ~~**新 env var の docs 反映**~~ → `CODEROUTER_LAUNCHER_TOKEN` / `CODEROUTER_ALLOWED_HOSTS` / `CODEROUTER_MAX_BODY_BYTES` は `docs/guides/security.md` に記載済み。v2.13.0 / v2.14.0 で増えた `CODEROUTER_ALLOW_CWD_CONFIG` / `CODEROUTER_METRICS_TOKEN` も同日追記
- ~~**`examples/providers.opusplan.yaml`**~~ → 作成済み (`docs/guides/subagent-routing.md` §5(a) から抽出)

**残っているもの:**

- **レビュー低優先度リファクタ (L1〜L5)**: バグ修正が落ち着いてから着手予定。L1 = `fallback.py` の 1 試行前後処理の集約 (v2.14.0 時点で 3,486 行)、L2 = `logging.py` の汎用 `emit_event()` 化、L3 = `schemas.py` / `doctor.py` の分割、L4 = 重複コード (プロファイル解決 / adapter エラー変換 / output filter) の集約、L5 = その他小粒 (Prometheus 未出力メトリクス / `restart_command` の `shell=True` / 月次予算 TOCTOU 等)。詳細は `_OUTPUTS/02-レビュー監査/code-review/2026-07-02_コードレビュー改善提案_v1.md` (ローカル保管) の「優先度: 低」節
  - 注: `restart_command` の `shell=True` は **v2.13.0 で `shell=False` の argv 実行に変更済み** (この行の記述より先に個別対応された)
- **`docs/verification.md` の精緻化**: MoE モデルの罠・rolling-window タイミング制約・goal_mode 実機検証知見を反映
- **`examples/providers.production-grade.yaml`**: `monthly_budget_usd` / `memory_pressure_action` / `goal_mode: true` を組み合わせた production yaml 雛形
- **Unsloth Studio プロバイダー検証**: E2E 手動テスト (~2h、安定版確認後)
- ~~**`examples/providers.opusplan.yaml`**~~ (2026-07-11 追加、方向性 framing 由来) → **2026-08-10 作成済み**。planner (agent_cli claude opus, read_only) / coder (local 優先→cloud-mid フォールバック) / reviewer-audit (agent_cli claude opus) / reviewer-light (local) の役割別 profile。公開ガイドは [`docs/guides/subagent-routing.md`](./docs/guides/subagent-routing.md) §5(a)

> v2.5+ の機能ロードマップ (goal context bridge / public benchmark / plugin-memory backend 等) は [`docs/inside/future.md`](./docs/inside/future.md) に集約。plan.md では重複させない。

### ❓ 検討中 — 実装方針 / 必要性が未確定

| 領域 | 内容 | 状況 |
| --- | --- | --- |
| **PEP 541 reclamation** | PyPI の bare `coderouter` 名前空間 (現所有者 Lawrence Chen、HTTP routing 系汎用ライブラリ、2025-06 single 0.1.0、ドメイン完全別物) を申請して引き取り、`coderouter-cli` を alias 化、canonical を `coderouter` に戻す | 申請は可能だが審査に 1〜数ヶ月、結果は他者要因。間 `coderouter-cli` で運用 |
| **Docker イメージ提供** | 公式 Dockerfile + GHCR multi-arch 配布 | `uvx coderouter-cli` で onboarding 摩擦が十分低くなった結果、Docker は需要次第。CI / k8s 向けに要望が顕在化したら実施 |
| **`coderouter-cli` を Go で別配布** | Python 配布で詰んだ場合の B プラン (§16 リスク対応案) | 現状 PyPI publish が安定して機能しているため保留。将来 (i) Python 環境構築の摩擦が再燃 / (ii) single static binary の需要、いずれかで再評価 |
| **`npm i -g coderouter` 経路** | Node ユーザー向け配布 | uvx で十分という判断。Claude Code が npm 経由なので「同じ install 経路」需要が顕在化したら検討 |
| **依存最小主義の「次の絞り」** | 5 deps 据え置きの継続 vs `httpx` HTTP/2 / async 安定性のための backport 受容 | 需要なし、現状維持の方針 (§5.4)。BREAKING に踏み込むなら別途 |
| **agent_cli Plugin 切り出し (Phase 2)** (2026-07-11 追加) | `docs/designs/external-agents-adapter.md` §9.3 / 詳細設計: [`docs/designs/agent-cli-plugin-extraction.md`](./docs/designs/agent-cli-plugin-extraction.md)。in-core `AgentCliAdapter` を `coderouter-plugin-agents` へ移設し、CLI churn (grok モデル廃止・gemini→antigravity・codex スキーマ未凍結) を Core リリース周期から分離する | ✅ **完了 (2026-07-11)**。Phase 2a = v2.8.0 (Adapter Protocol 配線) / Phase 2b = v2.8.1 (plugin へ移設・in-core は deprecation) / **Phase 2c = v2.9.0 で in-core adapter を削除 (BREAKING)**。以後 `kind: agent_cli` は `coderouter-plugin-agents` の導入が必須 |
| **orchestration companion (Ecosystem 層)** (2026-07-11 追加) | Plan→実行→レビューの制御ループ・safe-edit を持つ別プロセスの構想。`_OUTPUTS/04-計画-方向性/multiagent/` のプロトタイプ (orchestrator_v2.py / safe_edit_v1.py) を別 repo standalone 化する候補。骨子は [`docs/designs/orchestration-companion.md`](./docs/designs/orchestration-companion.md) (新設、構想) | 実プロダクト化する段で新設判断。現状は構想文書のみ。CodeRouter 本体 (Core/Plugin) には入れない (`docs/inside/future.md` §2.5・§5 参照) |
| **サブエージェント明示指定チャネル** (2026-07-11 追加、ccr 由来の検討項目) | claude-code-router の `<CCR-SUBAGENT-MODEL>` タグ相当。system prompt/先頭メッセージから明示タグを検出する最優先ルールを `auto_router.py` の推測ベース分類の前段に追加する案 (`_competitive/2026-07-11_ccr実装解析_v1.md` §6 #1) | 未着手。着手条件は今後精査 (`docs/inside/future.md` §0 P2 #17) |
| **Presets 的設定共有** (2026-07-11 追加、ccr 由来の検討項目) | `ccr preset export/install` 相当。provider chain + auto_router rules を 1 ファイルに export/import する CLI サブコマンド案。コミュニティでの設定共有・オンボーディング摩擦削減が動機 (`_competitive/2026-07-11_ccr実装解析_v1.md` §6 #2) | 未着手 (`docs/inside/future.md` §0 P2 #18) |

### ❌ やらないこと (Out of Scope)

詳細は §15 を参照。要約: 音声 / ブラウザ操作 / iMessage 連携 / 全 provider の完全統一化 / 学習・fine-tuning パイプライン。

---

### リリース履歴

全リリース履歴は §6.2、各 release の詳細 (commit hash / テスト数 / sub-release) は [`CHANGELOG.md`](./CHANGELOG.md) を参照。出荷済みマイルストーンの DoD・実装知見は版別節 (v0.1: §7 / v0.2: §8 / v0.5: §9 / v1.0: §10 / v1.5: §11 / v1.6: §12)、横断ログは §18 に格納。

振り返り: [`docs/retrospectives/`](./docs/retrospectives/) (v0.4 / v0.5 / v0.5-verify / v0.6 / v0.7 / v1.0 / v1.0-verify ほか)。

---

## 目次

- [0. このドキュメントの目的](#0-このドキュメントの目的)
- [1. プロジェクト概要](#1-プロジェクト概要)
- [2. コアコンセプト (memo.txt から確定)](#2-コアコンセプト-memotxt-から確定)
- [3. claude-code-local から取り込むコンセプト](#3-claude-code-local-から取り込むコンセプト)
- [4. アーキテクチャ概要](#4-アーキテクチャ概要)
- [5. 技術スタック比較](#5-技術スタック比較)
- [6. マイルストーン (ロードマップ全景)](#6-マイルストーン-ロードマップ全景)
  - [6.1 全景](#61-全景)
  - [6.2 リリース履歴](#62-リリース履歴)
- [7. v0.1 — Walking Skeleton ✅](#7-v01--walking-skeleton-)
- [8. v0.2 — Anthropic Ingress ✅](#8-v02--anthropic-ingress-)
- [9. v0.5 — Capability Gate Trio ✅](#9-v05--capability-gate-trio-)
- [10. v1.0 — Tool-Call 信頼性 + Code Mode ✅](#10-v10--tool-call-信頼性--code-mode-)
- [11. v1.5 — 計測ダッシュボード ✅](#11-v15--計測ダッシュボード-)
- [12. v1.6 — auto_router (task-aware routing) ✅](#12-v16--auto_router-task-aware-routing-)
- [13. v1.7〜v2.6 — 出荷済みマイルストーン (要約)](#13-v17v26--出荷済みマイルストーン-要約)
- [14. 横断タスク (どのバージョンでも継続)](#14-横断タスク-どのバージョンでも継続)
- [15. やらないこと (Out of Scope)](#15-やらないこと-out-of-scope)
- [16. 想定リスクと対応](#16-想定リスクと対応)
- [17. 命名・ブランディング](#17-命名ブランディング)
- [18. 実装ログ & 残アクション](#18-実装ログ--残アクション)
- [Appendix A — memo.txt との対応表](#appendix-a--memotxt-との対応表)
- [Appendix B — claude-code-local からの抽出表](#appendix-b--claude-code-local-からの抽出表)

---

## 0. このドキュメントの目的

- CodeRouter で「何を作るか」「なぜ作るか」「どう作るか」を1枚に集約する
- 各マイルストーン (v0.1 〜 v2.6) のスコープ・完了条件・設計判断を記録する
- 技術スタック選定の判断材料を残す
- リリース後は振り返り (`docs/retrospectives/*.md`) と実装ログ (§18) に反映する
- **v0.1〜v2.6 は全て出荷済み**。版履歴の正本は [`CHANGELOG.md`](./CHANGELOG.md)、v2.5+ の今後ロードマップは [`docs/inside/future.md`](./docs/inside/future.md) を参照

---

## 1. プロジェクト概要

### 1.1 ひとことで

> **「無料・ローカル・自動 fallback」を標準にした LLM ルーター。**
> Claude Code をそのまま使いつつ、裏側はローカル / 無料 / 有料を自動で切り替える。

### 1.2 解決する課題

| 既存 | 課題 |
| --- | --- |
| LiteLLM | 機能豊富だが依存が重く、サプライチェーン懸念もあった (claude-code-local が剥がした事例あり) |
| OpenRouter | 便利だが「使う側」前提。落ちる/レート制限/モデル入れ替えがあり常用には不安 |
| Ollama / llama.cpp | ローカルは速いが、Claude Code から使うにはプロキシ翻訳が必要で遅い |
| claude-code-local | MLX/Apple 専用、単一モデル、fallback 無し |

CodeRouter はこのギャップを埋める **「Claude Code 互換のローカル優先・無料優先・自動 fallback」** ルーター。

### 1.3 キャッチコピー候補

```
Local-first coding AI with ZERO cost by default.
```

```
ローカル無料優先、必要な時だけ課金。Claude Code そのまま使える。
```

### 1.4 ターゲットユーザー

- ローカルで Claude Code を使いたいが、モデル選定・プロキシ運用に疲れた人
- 機密コードを扱うため、デフォルト「外に出さない」が欲しい人
- API 課金を最小化したいインディー開発者・学生
- マルチプロバイダ構成を一括で管理したい個人開発チーム

---

## 2. コアコンセプト (memo.txt から確定)

### 2.1 3層 fallback

```
① ローカル（無料・最優先）
② 無料クラウド（OpenRouter free など）
③ 有料クラウド（最終保険・要明示許可）
```

### 2.2 モード選択 (モデルを選ばせない)

ユーザーには `coding` / `fast` / `long` / `cheap` のような **モード** だけを提示し、内部で自動ルーティング。

### 2.3 デフォルト無料・課金は明示許可制

```yaml
# default
ALLOW_PAID: false
mode: free-only
```

`ALLOW_PAID=true` を立てない限り有料プロバイダは絶対に呼ばない。

### 2.4 OpenAI 互換を土台、Claude (Anthropic) は別アダプタ

- OpenAI 互換 = 標準入口 (Gemini / GLM / 多くの OSS モデルを吸収)
- Anthropic = 独自アダプタ (Messages API / thinking / MCP 拡張)

### 2.5 capability flags でプロバイダ差分を吸収

```yaml
capabilities:
  chat: true
  streaming: true
  tools: true
  vision: false
  reasoning_control: provider_specific
  mcp: provider_specific
  openai_compatible: true
  prompt_cache: true
```

---

## 3. claude-code-local から取り込むコンセプト

| # | 取り込み項目 | 理由 |
| --- | --- | --- |
| A | **Anthropic API ネイティブ ingress** | Claude Code CLI は Anthropic API しか喋らない。プロキシ翻訳を挟むと 7.5x 遅い (133s → 17.6s)。 |
| B | **tool_call フォーマット変換 + 壊れた JSON のリカバリ** | ローカルモデルは `<\|tool_call>` / 生 JSON / `<tool_call>` JSON など形式バラバラ。修復しないと実用にならない。 |
| C | **Code Mode (harness prompt slim)** | Claude Code の 10K トークン system prompt をローカルモデル向けに 100 トークンへ圧縮。99% 削減。 |
| D | **プロンプトキャッシュ再利用** | 4K+ トークンの system prompt を毎ターン re-prefill しない。 |
| E | **出力クリーニング** | `<think>` / `<\|channel>thought` / `<turn\|>` など考え事マーカーを剥がす。**v0.1 実装中に qwen3.x の `delta.reasoning` 非標準フィールド問題を発見 → v0.3 に前倒し**。抑制の試みは両レイヤで失敗: ① Ollama OpenAI-compat は `think: false` を silent drop、② qwen3.5:4b の alignment は `/no_think` を prompt injection として自発的に拒否。結論: **抑制不能**、router 側で `delta.reasoning` を剥がす層が必須 (v0.3)。暫定対応として fast profile から qwen3.x を外し、非 thinking 小型モデル (qwen2.5:1.5b / gemma3:1b) に差し替え済み。 |
| F | **tool-call 信頼性チューニング既定値** | temperature 0.2 / KV 8-bit / リトライ最大 2 回。 |
| G | **回帰テストスイート** | 14 ケースの multi-step タスクテスト。プロバイダの coding 適性ゲート。 |
| H | **ワンクリック launcher** | `.command` / `.bat` / `.sh` で double-click 起動。 |
| I | **ZERO outbound monitor (`doctor` コマンド)** | `lsof` ベースでローカルのみと監査可能に。 |

---

## 4. アーキテクチャ概要

### 4.1 コンポーネント図

```
┌───────────────────────────────────────────────────────────┐
│                    Client                                  │
│   ┌─────────────────┐     ┌─────────────────────────┐     │
│   │  Claude Code    │     │  OpenAI互換クライアント  │     │
│   │  (Anthropic API)│     │  (任意のSDK / Cline等)   │     │
│   └────────┬────────┘     └────────────┬────────────┘     │
└────────────┼───────────────────────────┼──────────────────┘
             │                           │
             ▼                           ▼
┌───────────────────────────────────────────────────────────┐
│                    CodeRouter                              │
│                                                           │
│   ┌──────────────────┐  ┌──────────────────────────────┐  │
│   │ Anthropic Ingress│  │ OpenAI互換 Ingress           │  │
│   │ (port 4001)      │  │ (port 4000)                  │  │
│   └─────────┬────────┘  └────────────┬─────────────────┘  │
│             │                        │                    │
│             └────────────┬───────────┘                    │
│                          ▼                                │
│              ┌──────────────────────┐                     │
│              │ Normalizer            │ ← 共通中間形式へ   │
│              └──────────┬───────────┘                     │
│                         ▼                                 │
│              ┌──────────────────────┐                     │
│              │ Prompt Middleware     │                     │
│              │  - Code Mode検出      │                     │
│              │  - harness slim       │                     │
│              │  - prompt cache id    │                     │
│              └──────────┬───────────┘                     │
│                         ▼                                 │
│              ┌──────────────────────┐                     │
│              │ Profile Router        │ ← coding/fast/...  │
│              │  + Fallback Engine    │                     │
│              │  + ALLOW_PAID gate    │                     │
│              └──────────┬───────────┘                     │
│                         ▼                                 │
│   ┌──────────┬──────────┬──────────┬───────────────┐      │
│   │ Local    │ Free     │ Paid     │ Anthropic     │      │
│   │ Adapter  │ Cloud    │ Cloud    │ Adapter       │      │
│   │ (mlx/    │ (OR free)│ (OAI/etc)│ (Messages API)│      │
│   │  ollama/ │          │          │               │      │
│   │  llamacpp)│         │          │               │      │
│   └─────┬────┴─────┬────┴─────┬────┴───────┬───────┘      │
│         │          │          │            │              │
│         ▼          ▼          ▼            ▼              │
│              ┌──────────────────────┐                     │
│              │ Output Filter         │ ← think/stop tag   │
│              │  + tool_call recover  │   strip + JSON     │
│              └──────────┬───────────┘   recovery          │
│                         ▼                                 │
│              ┌──────────────────────┐                     │
│              │ Response Encoder      │                     │
│              │  (Anthropic / OAI形式)│                     │
│              └──────────────────────┘                     │
└───────────────────────────────────────────────────────────┘
```

### 4.2 リクエストの流れ (例: `coding` モード)

1. Claude Code が `ANTHROPIC_BASE_URL=http://localhost:4001` に投げる
2. Anthropic Ingress が受け取り、共通中間形式に正規化
3. Prompt Middleware が「tools に Bash/Read/Edit/Write/Grep/Glob あり」→ Code Mode 判定 → harness を slim に差し替え
4. Profile Router が `coding` プロファイルから順に試行
   - `qwen3-coder-local` → 失敗/遅延しきい値超
   - `glm-local` → 失敗
   - `openrouter-free-coder` → 成功
5. Output Filter が `<think>` 等を剥がし、tool_call を JSON 修復
6. Response Encoder が Anthropic 形式で返す

### 4.3 設定ファイル構成案

```
~/.coderouter-t/
├── config.yaml          # 基本設定 (ALLOW_PAID等)
├── providers.yaml       # プロバイダ定義 + capability flags
├── profiles.yaml        # coding/fast/long/cheap のfallback順
├── secrets.env          # APIキー (gitignore対象)
└── logs/
    └── audit.log
```

---

## 5. 技術スタック比較

memo.txt の方針 (OpenAI互換土台 + Anthropic専用アダプタ + capability flags) はどの言語でも実装可能。以下、CodeRouter 観点で3言語を比較する。

### 5.1 比較表

| 観点 | 🐍 **Python** (FastAPI/Litestar) | 📘 **TypeScript** (Hono/Fastify) | 🦫 **Go** (chi/Gin) |
| --- | --- | --- | --- |
| **公式SDKの充実度** | ◎ Anthropic / OpenAI / Google / Cohere 全て公式 | ◎ Anthropic / OpenAI / Google 公式 | △ 公式SDKは限定的、自前実装が増える |
| **LLMエコシステム** | ◎ LiteLLM / LangChain / LlamaIndex / instructor | ○ LangChain.js / Vercel AI SDK | △ langchaingo 程度 |
| **ローカル推論連携** | ◎ mlx-lm / llama-cpp-python / transformers が直接呼べる | △ HTTP経由がほとんど | △ HTTP経由がほとんど |
| **配布の手軽さ** | △ venv / pyenv / uv / Docker推奨 | ○ npm install 一発、bun でシングル化も可 | ◎ シングルバイナリで `curl \| sh` |
| **起動時間** | △ Python起動 + import で 200-800ms | ○ Node 50-150ms / Bun 20ms 級 | ◎ <20ms |
| **メモリ** | △ 80-200MB | ○ 40-100MB | ◎ 10-40MB |
| **ストリーミング/SSE性能** | ○ FastAPI + uvicorn で十分 | ○ Hono/Fastify で良好 | ◎ 標準ライブラリで強力 |
| **型安全性** | ○ type hints + pydantic | ◎ TypeScript本体 | ◎ Go本体 |
| **開発速度 (個人)** | ◎ 慣れていれば最速 | ◎ 慣れていれば最速 | ○ ボイラープレート多め |
| **コミュニティ参入障壁** | ◎ AI界隈は Python が前提 | ○ Web/フロント勢は入りやすい | △ Go LLM 界隈はまだ小さい |
| **PR が来やすそう** | ◎ | ◎ | △ |
| **「ローカルプロセスとして常駐」** | △ launchd/systemd 設定必要 | ○ pm2 / 同左 | ◎ そのままバイナリで OK |
| **claude-code-local 互換性** | ◎ server.py が Python なので参考実装移植が楽 | ○ 移植は可能 | △ ロジック移植が多い |
| **テスト** | ◎ pytest 文化 | ◎ vitest/jest | ◎ 標準テスト |

### 5.2 推奨

**第1候補: Python (FastAPI or Litestar)**

理由:
- AI/LLM エコシステムの恩恵が最大。Anthropic / OpenAI / OpenRouter / mlx-lm などすべて公式 Python SDK が一級市民
- claude-code-local の `server.py` (~1000 行) を参考にしやすい
- LiteLLM の置き換えを意識するなら同じ言語にいるメリットが大きい
- pydantic で capability flags の型を堅く定義できる

懸念:
- 配布で苦労する → **`uv` を採用**して `uvx coderouter` 一発で動く形にすれば回避
- 起動が遅い → **常駐デーモン前提**にすれば許容できる

**第2候補: TypeScript (Hono + Bun)**

理由:
- `bun build --compile` でシングルバイナリ化可能、配布の手軽さは Go に近い
- Web ダッシュボード (将来) を同じ言語で書ける
- Vercel AI SDK / Anthropic SDK / OpenAI SDK 全て揃っている

懸念:
- ローカル推論バックエンド (mlx-lm) を直接 import できないので、HTTP 経由になる
- AI 界隈の "新しい論文/手法" は Python 実装が先に出る

**第3候補: Go**

理由:
- ZERO outbound 系の audit/doctor、launcher、daemon としての品質は最高
- 配布が `curl | sh` で完結
- 並行 fallback 試行に標準の goroutine が刺さる

懸念:
- LLM 公式 SDK が乏しく、HTTP クライアントで自前実装が増える
- 個人プロジェクトで PR を集めたいなら不利

### 5.3 結論 (2026-04-19 確定)

> **本体: Python 3.12+ / uv / FastAPI / httpx 直叩き** (v0.1〜v2.6 全て Python single-language で出荷)。
> 配布は v1.7 で PyPI `coderouter-cli` + `uvx` 経路に確定。当初検討した Go 製配布専用 CLI 案は不採用 (詳細は `docs/inside/future.md`)。

#### 採用理由 (確定版)

- AI/LLM エコシステムが Python に集中している (Anthropic / OpenAI / OpenRouter / mlx-lm / Ollama 全て一級市民)
- claude-code-local の `server.py` (~1000 行) を参考実装として直接読める
- pydantic で capability flags / providers.yaml の型を堅く守れる
- `uv` 採用で依存ロック (`uv.lock` + hash) と配布 (`uvx coderouter`) を両立できる

#### 不採用にしたもの

- TypeScript: ローカル推論バックエンドを HTTP 経由でしか叩けない、AI 界隈の "新しい論文" は Python 実装が先に出る
- Go: LLM 公式 SDK が乏しく、HTTP クライアントを自前実装する量が増える (配布専用 CLI 案も検討したが、最終的に Python single-language + `uvx` 配布で十分と判断し不採用)

### 5.4 依存最小主義 (Dependency Minimalism Policy)

LiteLLM がサプライチェーン懸念で claude-code-local から剥がされた事例を踏まえ、CodeRouter は **「依存パッケージ数そのものを差別化要因」** にする。

#### 厳格なルール

- 本体ランタイム依存は以下の **5本に固定**:
  - `fastapi` (ingress)
  - `uvicorn` (ASGI server)
  - `httpx` (上流呼び出し)
  - `pydantic` (schema)
  - `pyyaml` (config)
- **公式 SDK (anthropic / openai 等) は使わない。** HTTP を直接叩く。SDK は便利だが各 20-50 個の transitive deps を引きずる
- LiteLLM / LangChain / LlamaIndex 等の "ルーター系" ライブラリは絶対に入れない (CodeRouter 自身がそれだから)
- `uv.lock` をリポジトリに commit、CI で `uv sync --frozen` 強制
- `--require-hashes` 相当のハッシュ検証必須
- 開発時依存 (`pytest` / `ruff` / `mypy` 等) は dev-extras に分離

#### 監査の仕組み

- `coderouter doctor --deps` で本体の全依存パッケージとその outbound 接続実績を一覧表示
- README に **「依存数: 5 個 (vs LiteLLM 100+)」** を掲げる
- CI で `pip-audit` / `uv pip audit` 相当を実行

---

## 6. マイルストーン (ロードマップ全景)

### 6.1 全景

major / minor を 1 行ずつ。**v0.1〜v2.6 は全て出荷済み (✅)**。sub-release / patch-level の粒度・commit hash・テスト数は [`CHANGELOG.md`](./CHANGELOG.md) が正本。

| Ver | 日付 | 一言ゴール | 状態 |
| --- | --- | --- | --- |
| **v0.1.0** | 2026-04-20 | OpenAI互換 ingress + ローカル1個 + フォールバック1個 (Walking Skeleton) | ✅ |
| **v0.2.0** | 2026-04-20 | Anthropic互換 ingress 追加、Claude Code から実利用可能に | ✅ |
| **v0.3.0** | 2026-04-20 | Tool-call 修復 + mid-stream guard + usage 集計 + streaming downgrade | ✅ |
| **v0.4** | 2026-04-20 | Symmetric OpenAI ⇄ Anthropic routing + Anthropic native adapter + header passthrough | ✅ |
| **v0.5.0** | 2026-04-20 | Capability gate trio (thinking / cache_control / reasoning) の統一 `capability-degraded` 契約 | ✅ |
| **v0.6.0** | 2026-04-20 | Chain-as-first-class-object — `--mode` / `mode_aliases` / 宣言的 ALLOW_PAID gate / profile-level override | ✅ |
| **v0.7.0** | 2026-04-20 | Beginner UX — 宣言的 `model-capabilities.yaml` registry + `doctor --check-model` probe + Troubleshooting | ✅ |
| **v1.0.0** | 2026-04-20 | 出力クリーニング filter chain + Ollama 2-knob truncation の直接 probe (observation loop closed) | ✅ |
| **v1.5.0** | 2026-04-22 | 計測ダッシュボード — Collector + `/metrics.json` + Prometheus + JSONL mirror + `/dashboard` + `coderouter stats` TUI | ✅ |
| **v1.6.0** | 2026-04-22 | auto_router — リクエスト本文から profile 自動選択 (beginner-first、3 ティア対応) | ✅ |
| **v1.7.0** | 2026-04-25 | PyPI 公開 — `coderouter-cli` として publish、`uvx coderouter-cli serve` 1 行起動 | ✅ |
| **v1.8.0** | 2026-04-26 | 用途別 4 プロファイル + GLM / Gemma 4 / Qwen3.6 公式化 + `doctor --apply` 自動化 | ✅ |
| **v1.9.0** | 2026-04-29 | Cache observability + Adaptive routing + Cost-aware dashboard + Long-run reliability 着手 | ✅ |
| **v1.10.0** | 2026-05-01 | Cost enforcement + Long-run reliability 完成 + auto-router feature complete | ✅ |
| **v2.0.0** | 2026-05-05 | Context Budget Management — L1 overflow 防止 (warn 80% / auto trim 90%) | ✅ |
| **v2.1.0** | 2026-05-05 | Long-run Reliability 完成 — drift detection (L4) / partial stitch (L6) / continuous probing | ✅ |
| **v2.2.0** | 2026-05-06 | Self-healing + Multi-day operation (永続化) + Replay framework | ✅ |
| **v2.3.0a4** | 2026-05-08 | Plugin SDK — `coderouter.plugins` (input_filter / observer hook)、entry_points discovery | ✅ |
| **v2.4.0** | 2026-05-15 | Goal-session awareness — `goal_progress_stall` / `goal_mode` / `replay --suggest-rules` | ✅ |
| **v2.5.0** | 2026-05-22 | Launcher — llama.cpp / vllm の起動・管理 GUI (デスクトップGUI版 + Web版) | ✅ |
| **v2.5.1** | 2026-05-22 | MLX backend (Launcher 3 番目) + docs/ 再編 (start/guides/backends/concepts) + plan.md 再構成 + starlette CVE 修正 | ✅ |
| **v2.5.2** | 2026-05-22 | Backend-aware Launcher 推奨値 + backend インストールガイド + Launcher docs 統合 | ✅ |
| **v2.5.4** | 2026-06-05 | Gemma `<0xNN>` byte-fallback 修復フィルタ (Ollama 0.30 detokenizer 対策、opt-in・streaming-safe) | ✅ |
| **v2.5.5** | 2026-06-06 | Claude Code CLI ≥ 2.1.154 の非仕様 `role: "system"` を ingress で正規化 (422 回避) | ✅ |
| **v2.6.0** | 2026-06-20 | Language Tax — CJK トークン税の計測 / `cjk_ratio_min` ルーティング / 可視化 + starlette 1.3.1 (CVE 4 件) | ✅ |
| **v2.6.1** | 2026-06-28 | Token-savings accounting — trim / compress のトークン節約量をメトリクス + ダッシュボードに集約 | ✅ |

> **v2.5+ の今後ロードマップ** は [`docs/inside/future.md`](./docs/inside/future.md) を参照 (plan.md では重複させない)。
>
> **v0.5 で当初スコープ (`profiles.yaml` / `--mode` / 完全版 ALLOW_PAID gate) は敢えて落とした。** 実運用で先に突き当たった pain (model 手動差し替え / silent cache 破壊 / 非標準フィールド漏れ) に capability gate 3 本で答えた結果、翻訳層の「正しさ」が先に固まった。残件は v0.6 で消化済み (§9 参照)。

### 6.2 リリース履歴

**版履歴の正本は [`CHANGELOG.md`](./CHANGELOG.md)。** 各リリースの commit hash・テスト数・sub-release / patch-level (a1〜a6 等) の詳細はそちらを参照。以下は major / minor の compact なサマリのみ。

| Ver | 日付 | 一言 |
| --- | --- | --- |
| v0.1.0 | 2026-04-20 | Walking Skeleton — OpenAI ingress + local + fallback |
| v0.2.0 | 2026-04-20 | Anthropic ingress — Claude Code 疎通 |
| v0.3.0 | 2026-04-20 | Tool-call repair + mid-stream guard + usage 集計 |
| v0.4 | 2026-04-20 | Anthropic native adapter + 逆翻訳 + `anthropic-beta` header passthrough |
| v0.5.0 | 2026-04-20 | Capability gate trio — thinking / cache_control / reasoning の統一契約 |
| v0.6.0 | 2026-04-20 | Chain-as-first-class-object — `--mode` / `mode_aliases` / ALLOW_PAID gate |
| v0.7.0 | 2026-04-20 | Beginner UX — `model-capabilities.yaml` registry + `doctor --check-model` |
| v1.0.0 | 2026-04-20 | 出力クリーニング chain + doctor num_ctx / streaming probe |
| v1.5.0 | 2026-04-22 | Observability pillar — 計測 / 可視化 / 配信 / dashboard / TUI |
| v1.6.0 | 2026-04-22 | `auto_router` (task-aware routing) |
| v1.7.0 | 2026-04-25 | PyPI 公開 — `coderouter-cli` |
| v1.8.0 | 2026-04-26 | 用途別 4 プロファイル + GLM / Gemma 4 / Qwen3.6 公式化 + `doctor --apply` |
| v1.9.0 | 2026-04-29 | Cache observability + Adaptive routing + Cost-aware + Long-run reliability 着手 |
| v1.10.0 | 2026-05-01 | Cost enforcement + Long-run reliability 完成 + auto-router feature complete |
| v2.0.0 | 2026-05-05 | L1 Context Budget Management — overflow 防止 |
| v2.1.0 | 2026-05-05 | Long-run Reliability 完成 — drift / partial stitch / continuous probing |
| v2.2.0 | 2026-05-06 | Self-healing + Multi-day operation + Replay |
| v2.3.0a4 | 2026-05-08 | Plugin SDK — `input_filter` / `observer` hook |
| v2.4.0 | 2026-05-15 | Goal-session awareness — `goal_progress_stall` / `goal_mode` / `replay --suggest-rules` |
| v2.5.0 | 2026-05-22 | Launcher — llama.cpp / vllm GUI |
| v2.5.1 | 2026-05-22 | MLX backend + docs 再編 + starlette CVE 修正 |
| v2.5.2 | 2026-05-22 | Backend-aware Launcher 推奨値 + backend インストールガイド |
| v2.5.4 | 2026-06-05 | Gemma `<0xNN>` byte-fallback 修復フィルタ (Ollama 0.30 対策) |
| v2.5.5 | 2026-06-06 | Claude Code CLI ≥ 2.1.154 `role: "system"` ingress 正規化 |
| v2.6.0 | 2026-06-20 | Language Tax — CJK トークン税 計測 / ルーティング / 可視化 |
| v2.6.1 | 2026-06-28 | Token-savings accounting — trim / compress 節約量の集約 |

> v2.5.3 は欠番 (v2.5.2 の次は v2.5.4)。詳細は [`CHANGELOG.md`](./CHANGELOG.md)。

各マイルストーンの DoD・設計判断は plan.md 内の版別節 (v0.1: §7 / v0.2: §8 / v0.5: §9 / v1.0: §10 / v1.5: §11 / v1.6: §12)、振り返りは [`docs/retrospectives/`](./docs/retrospectives/) を参照。

---

## 7. v0.1 — Walking Skeleton ✅

**出荷済み (2026-04-20)。** OpenAI 互換 ingress (`/v1/chat/completions`) + ローカル / OpenRouter free の 2 adapter + 順次 fallback engine + SSE ストリーミング。pydantic schema の `providers.yaml` ローダ、httpx 直叩き (公式 SDK 不使用、§5.4)、構造化 JSON ログ。

詳細は [`CHANGELOG.md` `[v0.1.0]`](./CHANGELOG.md)。以下は plan 固有の設計判断のみ。

### 7.1 実装で得た設計判断

- **qwen3.x の thinking モードは抑制不能** — Ollama ネイティブ `think: false` は OpenAI-compat shim が silent drop、モデル内蔵 `/no_think` 指令は alignment training が prompt injection と自己判定して拒否する。結論: **router 側で `delta.reasoning` を剥がす層が必須** (v0.3 / v1.0 に正式スコープ化)。暫定対応として fast profile を非 thinking 小型モデル (qwen2.5:1.5b / gemma3:1b) で構成。
- **profile 選択 UX の優先順位** — `body field > header > config default`。body を書き換えられるクライアントが最も強い意図表明をしており、多段プロキシでのヘッダ書き換えに耐えるため。
- **`ProviderConfig` 拡張フィールド** — `extra_body` (ベンダー固有オプション注入) / `append_system_prompt` (モデル内蔵指令の注入)。両方とも「効く環境では一発、効かないモデルも存在する」非対称な武器として残す。
- **Bug 修正 2 件** — (1) `request.model` をそのまま upstream 転送するとクライアントの placeholder で 404、`provider.model` で決定する。(2) Ollama は「モデル未 pull」を 404 で返すため `_RETRYABLE_STATUSES` に 404 を追加。

---

## 8. v0.2 — Anthropic Ingress ✅

**出荷済み (2026-04-20)。** Anthropic 互換 ingress (`/v1/messages`) を追加し、`ANTHROPIC_BASE_URL` で Claude Code から実利用可能に。共通中間形式 ↔ Anthropic 形式の双方向変換、SSE event 列 (`message_start → ... → message_stop`)、`tool_use` / `tool_result` block の round-trip 変換。

詳細は [`CHANGELOG.md` `[v0.2.0]`](./CHANGELOG.md)。以下は plan 固有の実機知見のみ。

### 8.1 実機検証で得た知見

- **Claude Code は同一 user turn で 2 本並走する** — 本文生成 + タイトル生成 (会話ラベル用の小さい要約呼び出し) を同時発射する。fallback engine は各リクエストを独立処理。
- **Claude Code の system prompt は巨大** — v2.1 は tool 定義含め推定 15-20K token を毎ターン送る。14B モデルでは prompt eval だけで ~93s。実用には 7B 以下 or prompt eval > 300 tok/s が必要。
- **mid-stream fallback は危険** — ストリーム開始後に provider が落ちると次プロバイダに fall back しようとするが、初バイト送出後だと部分 SSE が届いており重複・破損 event 列になる。`first_byte_sent` フラグで以降の fallback を禁止 (v0.3-B)。
- **qwen2.5-coder:14b は tool_calls を構造化出力しないことがある** — 大量の tool 定義を与えるとテキスト本文に JSON ブロックを書く挙動に落ちる。モデル能力限界であり翻訳バグではない。対処は tool-call repair / モデル選定 (v1.0 のスコープ)。

---

## 9. v0.5 — Capability Gate Trio ✅

**出荷済み (2026-04-20)。** capability gate 3 本 — thinking / cache_control / reasoning — を統一 `capability-degraded` ログ契約で実装。3 gate すべて `msg: capability-degraded` + `provider` / `dropped` / `reason` の同形フィールドを発火し、`reason` で `provider-does-not-support` / `translation-lossy` / `non-standard-field` を分岐。

詳細は [`CHANGELOG.md` `[v0.5.0]`](./CHANGELOG.md) / [`docs/retrospectives/v0.5.md`](./docs/retrospectives/v0.5.md)。以下は plan 固有の設計判断のみ。

### 9.1 Scope pivot の記録

当初の §9.1 案は `profiles.yaml` / `--mode` / 完全版 ALLOW_PAID gate の 3 点だった。実際に shipped したのは **capability gate 3 本**。v0.4 実機で露出した「silent 破壊を adapter 層で検知可能にする」ほうが優先度が高いと判断し、プロファイル系は v0.6 に送った (v0.6-A で `--mode`、v0.6-B で profile-level override、v0.6-C で 宣言的 ALLOW_PAID gate、v0.6-D で `mode_aliases` を全て消化済み)。

gate 設計の 6 軸マトリクス (failure mode / detection location / action / escape hatch / log message / log reason) を [`docs/retrospectives/v0.5.md`](./docs/retrospectives/v0.5.md) で確立、v0.6 以降が踏襲すべき shape として文書化した。

---

## 10. v1.0 — Tool-Call 信頼性 + Code Mode ✅

**出荷済み (2026-04-20、v1.0.0 umbrella)。** v1.0.0 で deliver したのは **出力クリーニング (`output_filters` filter chain)** + **Ollama 2-knob silent-fail の直接 probe** (`num_ctx` / streaming-path)。transformation には probe が伴うという v0.7 retrospective の原則を具体化した。

詳細は [`CHANGELOG.md` `[v1.0.0]`](./CHANGELOG.md) / [`docs/retrospectives/v1.0.md`](./docs/retrospectives/v1.0.md)。

### 10.1 スコープ再定義の記録

§10 の当初スコープは claude-code-local の "実戦で証明された 5 機能" だったが、v1.0.0 で deliver されたのは output-cleaning と doctor probe のみ。残り — tool-call 変換層 / `recover_garbled_tool_json` 拡張 / Code Mode (harness slim) / プロンプトキャッシュ再利用 / 14 ケース回帰テスト — は後続バージョンに re-scope された。tool-call repair の core (balanced-brace scanner + fenced JSON 検出) は v0.3-A で既に出荷済み (§18 参照)。v1.0.0 は「observation loop を閉じた」マイルストーンであって、claude-code-local feature-completeness の tag ではない。

---

## 11. v1.5 — 計測ダッシュボード ✅

**出荷済み (2026-04-22、v1.5.0 umbrella)。** Observability pillar — `MetricsCollector` (logging.Handler 経由 in-memory ring) + `/metrics.json` + Prometheus `/metrics` + `$CODEROUTER_EVENTS_PATH` JSONL mirror + `/dashboard` HTML + `coderouter stats` curses TUI + `display_timezone`。

詳細は [`CHANGELOG.md` `[v1.5.0]`](./CHANGELOG.md)。以下は plan 固有の設計方針のみ。

### 11.1 設計方針

- **既存ログを "tap" する** — 新フックを散りばめず、`JsonLineFormatter` で構造化済みのイベント (`try-provider` / `provider-ok` / `capability-degraded` 等) を `logging.Handler` サブクラス 1 本で一箇所収集。adapters / routing 側に触らず回帰コストゼロ。
- **保存は 2 層** — in-memory (primary、counter / gauge / ring buffer) + JSONL append (secondary, opt-in)。SQLite は入れない (依存追加 + スキーマメンテが JSONL と釣り合わない)。
- **Prometheus 形式を `/metrics` の既定に** — 運用現場の既定プロトコルに合わせ、スクレイパ接続試験を 1 ステップ減らす。exposition format は ~30 行で手書き、`prometheus_client` は入れない (5-dep policy 維持)。
- **依存ゼロの UI** — TUI は stdlib `curses` + `urllib`、HTML は tailwind CDN + vanilla JS (`setInterval` + `fetch`)。

> SemVer 上の番号順序: 当初 §11 は "v1.1 — 配布 / launcher / doctor"、§12 が "v1.5 — 計測ダッシュボード" だった。v1.0.1 のあと配布ブロックをスキップして計測ダッシュボードを先に出荷したため tag は `v1.0.1 → v1.5.0` と飛び、配布ブロックは v1.7 (§13) に繰り下げた。`v1.1` 番号は欠番扱い。

---

## 12. v1.6 — auto_router (task-aware routing) ✅

**出荷済み (2026-04-22、v1.6.0 umbrella)。** リクエスト本文から用途 (`coding` / `writing` / `multi`) を推論し対応する fallback chain に振り分ける `auto_router`。`default_profile: auto` sentinel + 4-variant `RuleMatcher` (`has_image` / `code_fence_ratio` / `content_contains` / `content_regex`) + bundled ruleset (image → `multi` / code-fence ≥ 0.3 → `coding` / else → `writing`)。

詳細は [`CHANGELOG.md` `[v1.6.0]`](./CHANGELOG.md)。以下は plan 固有の設計判断のみ。

### 12.1 なぜ作るか — 3 ティアのユーザー像

v0.6-D まで CodeRouter は「呼び出し側がプロファイルを知っている」前提だった。これは運用者目線では自然だが、エンドユーザー (Claude Code / codex を叩く人) には不自然。3 ティアを同じ yaml / 同じサーバで支えるのが v1.6 の目標:

| ティア | 触れ方 | 想定 yaml |
|---|---|---|
| **初心者** | ゼロ config、auto が全部やる | `default_profile: auto` のみ (bundled ルール) |
| **中級者** | `auto_router:` block で rule 書き換え | bundled ルールを copy & edit |
| **上級者** | per-request に `X-CodeRouter-Profile` で明示強制 | 既存 v0.6-D precedence に乗る |

precedence: `body.profile > X-CodeRouter-Profile > X-CodeRouter-Mode > auto_router (default_profile == "auto" 時) > default_profile`。境界は **imperative vs declarative** — 中級者は yaml で宣言、上級者はリクエスト単位で命令。

> patch-level の v1.6.1 (NIM 無料枠 + ドキュメント言語優先度スワップ)、v1.6.2 (Troubleshooting 切り出し)、v1.6.3 (`--env-file` + `doctor --check-env`) で v1.6 系を完成。詳細は CHANGELOG。

---

## 13. v1.7〜v2.6 — 出荷済みマイルストーン (要約)

v1.7 以降は実装ペースが上がり、各リリースの詳細を plan.md に転記すると CHANGELOG.md と完全重複するため、ここでは一言サマリのみ。**詳細は [`CHANGELOG.md`](./CHANGELOG.md) が正本**、v1.8-v2.5 のアーキテクチャ事実は [`docs/inside/future.md`](./docs/inside/future.md) §4 timeline / §6 を参照。

| Ver | 日付 | 内容 |
|---|---|---|
| **v1.7.0** | 2026-04-25 | PyPI 公開 — `coderouter-cli` として publish (`coderouter` 名前空間は別作者占有のため `*-cli` suffix)、`uvx coderouter-cli serve` 1 行起動、Trusted Publishing (OIDC) 経路 |
| **v1.8.0** | 2026-04-26 | 用途別 4 プロファイル + GLM / Gemma 4 / Qwen3.6 公式化 + `doctor --check-model --apply` 自動化 + `setup.sh` onboarding ウィザード + `claude_code_suitability` hint |
| **v1.9.0** | 2026-04-29 | Cache observability + Cross-backend cache passthrough + Adaptive routing (health-based 動的 priority) + Cost-aware dashboard + Long-run Guards 着手 (L2/L3/L5) |
| **v1.10.0** | 2026-05-01 | Cost enforcement (provider 月次予算上限) + Long-run reliability 完成 (L2/L5 phase 2) + auto-router feature complete (longContext / has_tools matcher) |
| **v2.0.0** | 2026-05-05 | L1 Context Budget Management — context window overflow 防止 (warn 80% / auto trim 90%、tool_use_id pair 保全) |
| **v2.1.0** | 2026-05-05 | Long-run Reliability 完成 — L4 Drift detection + L6 Mid-stream partial stitching + P3 Continuous probing。6 系統障害 (L1〜L6) 全対処到達 |
| **v2.2.0** | 2026-05-06 | Self-healing (UNHEALTHY → 自動 exclude + restart + 回復 probe) + Multi-day operation (sqlite3 StateStore 永続化) + Replay framework (A/B 統計) |
| **v2.3.0a4** | 2026-05-08 | Plugin SDK — `coderouter.plugins` (`input_filter` / `observer` の 2 hook を engine に統合)、entry_points discovery + supply-chain defense (enabled allowlist)。Core 5 deps 据え置き |
| **v2.4.0** | 2026-05-15 | Goal-session awareness — `goal_progress_stall` (L4 6 番目シグナル) + `goal_mode` flag (FallbackChain) + `THRESHOLDS_GOAL` preset + `coderouter replay --suggest-rules` (5 ルール統計エンジン、LLM 不要) |
| **v2.5.0** | 2026-05-22 | Launcher — llama.cpp / vllm backend の起動・管理 GUI。デスクトップGUI版 (`launcher_gui.py`、tkinter) + Web版 (`/launcher` ルート)。YAML-driven option profile、新規依存ゼロ |
| **v2.5.1** | 2026-05-22 | MLX backend (Launcher 3 番目、`mlx_lm.server`、Apple Silicon 向け) + docs/ をロール別フォルダ (start/guides/backends/concepts) に再編 + bilingual master index (`docs/README.md`) + plan.md 再構成 (1747→721 行) + starlette 1.0.0→1.0.1 (PYSEC-2026-161) |
| **v2.5.2** | 2026-05-22 | Backend-aware Launcher 推奨値 (llama.cpp はフラグ / vLLM・MLX は空) + `docs/backends/install-backends.md` (llama.cpp / vLLM / MLX インストールガイド) + Launcher docs 3→2 ファイル統合 + backend venv 規約文書化 (`~/.coderouter-t/backends/<backend>/`) |
| **v2.5.4** | 2026-06-05 | `repair_byte_fallback` output filter — Ollama 0.30 / llama.cpp detokenizer が漏らす `<0xNN>` byte-fallback を UTF-8 に再構成 (Gemma 日本語・tool-call JSON 対策)。opt-in / streaming-safe / lossless、新規依存ゼロ |
| **v2.5.5** | 2026-06-06 | Claude Code CLI ≥ 2.1.154 が `messages` 配列に混入させる非仕様 `role: "system"` (他 `ctx`/`msg`) を ingress の `model_validator(mode="before")` で正規化し 422 を回避。wire model の role enum は不変 |
| **v2.6.0** | 2026-06-20 | Language Tax — CJK トークン税 (`coderouter/language_tax.py`) の計測 + cost 統合 + `ProviderConfig.tokenizer_path` + `cjk_ratio_min` auto-route matcher + `/dashboard` "Cost & Language Tax" パネル。starlette 1.0.1→1.3.1 (CVE 4 件解消)。新規依存ゼロ |
| **v2.6.1** | 2026-06-28 | Token-savings accounting — `MetricsCollector` に `tokens_saved_total` + 機構別 (trim / compress) 内訳バケット、`/dashboard` に 3 タイル追加。core owns した数値で plugin 無しでも trim 節約が可視化。新規依存ゼロ・後方互換 |

**到達点 (v2.6.1)**: 6 系統障害 (L1〜L6) 全対処 + 自己修復 + 状態永続化 + Plugin 層 + Goal モード + Launcher (llama.cpp / vllm / MLX) + Language Tax 計測/ルーティング + Token-savings accounting。Runtime deps は出荷以来 5 本据え置き連続。`coderouter-plugin-memory` (別 repo、builtin JSONL + Ollama backend、stdlib only) も v0.4.0 まで並行リリース済み。

> 2026-07-02 の全ソースレビュー改修 (H1〜H8 / M1〜M14) は main にマージ後、**v2.7.0 (2026-07-02)** として採番・リリース済み。詳細は本ドキュメント冒頭「現況サマリ」の 2026-07-02 段落を参照。

> v2.5+ の今後ロードマップ・Vision・競合分析は [`docs/inside/future.md`](./docs/inside/future.md) を参照。plan.md ではこれ以降の将来計画を重複させない。

---

## 14. 横断タスク (どのバージョンでも継続)

- [ ] ドキュメント
  - [ ] `README.md` (claude-code-local 風の "見せ方")
  - [ ] `docs/architecture.md`
  - [ ] `docs/providers.md` (各 adapter 解説)
  - [ ] `docs/benchmarks.md`
- [ ] サンプル設定
  - [ ] `examples/providers.yaml` (Apple Silicon版 / Linux GPU版 / CPU only版)
  - [ ] `examples/profiles.yaml`
- [ ] セキュリティ / 依存最小主義 (§5.4 と連動)
  - [ ] 依存の脆弱性監査 (renovate / dependabot + `uv pip audit`)
  - [ ] `secrets.env` を絶対に commit させない pre-commit フック
  - [ ] `uv.lock` を commit、CI で `uv sync --frozen` 強制
  - [ ] 公式 SDK (anthropic / openai) を import していないことを CI でチェック
  - [ ] `coderouter doctor --deps` で依存数と outbound を可視化
- [ ] コミュニティ
  - [ ] CONTRIBUTING.md
  - [ ] ISSUE / PR テンプレート
  - [ ] note 記事用ネタ収集 (実測値、ハマりどころ)

---

## 15. やらないこと (Out of Scope)

- 音声 (NarrateClaude 領域)
- ブラウザ操作 (browser-agent 領域)
- iMessage / 通知システム連携
- 全 provider を完全同一 payload で扱う統一化 (Anthropic は別アダプタのまま)
- 学習 / fine-tuning パイプライン

---

## 16. 想定リスクと対応

| リスク | 影響 | 対応 |
| --- | --- | --- |
| OpenRouter free 枠が将来縮小 | fallback の中段が機能しない | 複数の無料源 (e.g. Gemini free, Mistral free) を providers.yaml で並列宣言 |
| Anthropic API の仕様変更 | Anthropic 互換 ingress が壊れる | バージョンヘッダ判定 + adapter バージョニング |
| ローカルモデルの tool_call が複雑化 | recovery が追いつかない | プロバイダごとに parser を差し替え可能にしておく |
| Python 配布で詰む | ユーザー導入率が低下 | uv 採用 + `coderouter-cli` を Go で別配布 |
| 依存パッケージのサプライチェーン攻撃 (LiteLLM 事例) | ルーター本体が侵害され、API キー / プロンプトが漏洩する可能性 | §5.4 の依存最小主義を厳守 (本体5本固定 / 公式SDK不使用 / lockfile + hash) |
| 個人開発の継続性 | 機能追加が止まる | コア機能を最小化、プラグイン制で外部委譲 |

---

## 17. 命名・ブランディング

- リポジトリ名: `CodeRouter`
- パッケージ名: `coderouter`
- CLI コマンド: `coderouter`
- ドメイン候補: `coderouter.dev` / `coderouter.app`
- ロゴモチーフ: 分岐する3本の矢印 (local / free / paid)

---

## 18. 実装ログ & 残アクション

v0.1〜v2.6 の item-level 実装履歴は [`CHANGELOG.md`](./CHANGELOG.md) に集約済み (各リリースの `Added` / `Changed` / `Files touched`)。各マイルストーンの設計判断は §7〜§13、振り返りは [`docs/retrospectives/`](./docs/retrospectives/) を参照。

### 本当に未消化のアクション

実装本体は v2.6.1 まで出荷完了 + 2026-07-02 レビュー改修 (H1〜H8 / M1〜M14) を **v2.7.0 (2026-07-02)** として出荷済み。残るのは小粒の継続作業とレビュー低優先度リファクタ (L1〜L5) のみ:

- [ ] **新 env var の docs 反映** — `CODEROUTER_LAUNCHER_TOKEN` / `CODEROUTER_ALLOWED_HOSTS` / `CODEROUTER_MAX_BODY_BYTES` を運用ガイド・troubleshooting に追記 (レビュー改修で追加、未 doc 化)。
- [ ] **レビュー低優先度リファクタ (L1〜L5)** — `fallback.py` / `logging.py` / `schemas.py` / `doctor.py` の分解、重複コード集約、Prometheus 未出力メトリクス等の小粒。詳細は `_OUTPUTS/02-レビュー監査/code-review/2026-07-02_コードレビュー改善提案_v1.md` (ローカル保管)。
- [ ] **Anthropic ヒューリスティック表のメンテ signal** — (a) 週次 `/v1/models` diff、または (b) 未知モデル検出時に warn ログ。capability registry をモデルファミリ追加に追従させる仕組み。(b) は既存 gate 計算からほぼ無料で取れる。
- [ ] **`docs/verification.md` の精緻化** — MoE モデルの罠 / rolling-window タイミング制約 / goal_mode 実機検証知見を反映。
- [ ] **`examples/providers.production-grade.yaml`** — `monthly_budget_usd` / `memory_pressure_action` / `goal_mode: true` を組み合わせた production yaml 雛形。
- [ ] **Unsloth Studio プロバイダー検証** — E2E 手動テスト (~2h、安定版確認後)。

> v2.5+ の機能ロードマップは [`docs/inside/future.md`](./docs/inside/future.md) に集約。検討中 / やらないことは本ドキュメント冒頭の「❓ 検討中」「❌ やらないこと」および §15 を参照。

---

## Appendix A — memo.txt との対応表

| memo.txt の項目 | plan.md での反映先 |
| --- | --- |
| 3層 fallback | §2.1, §4, §6 |
| モード選択 | §2.2, §9 |
| デフォルト無料 / ALLOW_PAID | §2.3, §9 |
| OpenAI互換土台 + Anthropic別アダプタ | §2.4, §4, §7-§8 |
| capability flags | §2.5, §9 |
| coding/fast/long の例 | §9, §17 |
| `.env` / `models.yaml` / `install.sh` | §13 |
| README キャッチコピー | §1.3, §17 |
| 「数字で見せる」 | §11 |
| 名前案 ClawRoute / CodeRouter | §17 |

## Appendix B — claude-code-local からの抽出表

| claude-code-local 機能 | plan.md での反映先 | 出荷状況 |
| --- | --- | --- |
| Anthropic API ネイティブ ingress | §8 (v0.2) | ✅ |
| tool_call 変換 + 壊れた JSON 修復 | §10 (v1.0) / §18 v0.3-A | ✅ (core は v0.3) |
| Code Mode (harness slim) | §10 (v1.0) | 一部 re-scope |
| プロンプトキャッシュ再利用 | §10 / cache observability (v1.9) | ✅ |
| 出力クリーニング | §10 (v1.0) | ✅ |
| tool-call チューニング既定値 | §10 (v1.0) | ✅ |
| 14ケース回帰テスト | §10 / verify スクリプト群 | 一部 (verify_v0_5 / v1_0) |
| ワンクリック launcher | §13 (v2.5 Launcher) | ✅ |
| ZERO outbound monitor (`doctor`) | §13 / `doctor --network` | 一部 (doctor 本体は ✅) |
| 計測ダッシュボード (tok/s 等) | §11 (v1.5) | ✅ |

---

*このplan.mdは生きたドキュメントです。実装中に判明した知見でガンガン書き換えてください。*

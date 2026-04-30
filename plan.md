# CodeRouter — 開発計画 (plan.md)

> **Local-first, free-first, fallback-built-in な LLM ルーター。**
> Claude Code / OpenAI 互換クライアントから単一エンドポイントで叩けて、内部で「ローカル → 無料クラウド → 有料クラウド」の3層 fallback を自動で行う。

最終更新: 2026-05-01
作成者: zephel01
状態: **v1.9.1 — patch release** (2026-05-01、CHANGELOG `[v1.9.1]` 参照)。v1.9.0 GA で「v1.10 候補」と整理した backlog のうち構造的負債を伴わない quick win 2 件を patch として束ねて出荷: (a) **v1.9-B2** streaming 経路の usage 集約 — `_StreamUsageAccumulator` + `_emit_cache_observed_streaming` で `cache-observed` log の streaming `outcome=unknown` placeholder を観測値に置換、(b) **per-model auto-routing** — `RuleMatcher.model_pattern` 5 番目 matcher 追加、`re.fullmatch` で body の `model` field を評価して Opus/Sonnet/Haiku で profile 分岐 (free-claude-code 由来)。Tests: 830 → **838** (+8)、Runtime deps: 5 → 5 (30 sub-release 連続据え置き)、完全互換。直前の出荷は v1.9.0 GA (2026-04-29、6 sub-release 統合 — Cache observability + Adaptive routing + Cost-aware + L3 Tool-loop guard)。
- **過去の出荷済みリリース**: [`CHANGELOG.md`](./CHANGELOG.md) を参照
- **未来の方向性 (Vision / 中長期ロードマップ / 市場分析 / 競合分析)**: 内部メモとして別途整理 (公開リポジトリには含まれない)
- **本ドキュメント**: 現在進行中の実装スケジュール (v1.9 系) + ローカル backend 別接続マトリクス + 検討中 / やらないこと

### 実装スケジュール

> 過去の出荷済み機能・リリース詳細は [`CHANGELOG.md`](./CHANGELOG.md) を参照。
> 本ドキュメントは **現在進行中の実装スケジュール** に集中。長期 Vision / 競合分析 / 市場分析等は内部メモとして別途整理 (公開しない)。
> **v1.9.0 GA 出荷済み (2026-04-29)** — 以下のロードマップは shipping 済みアーカイブ、次の active milestone は本セクション末尾の「v1.10 / v1.9.x 残課題」。

#### 完了: v1.9 ロードマップ (Adaptive Caching + Long-run Guards) — v1.9.0 GA で全 sub-release 出荷

各 sub-release の規模・想定期間・差別化軸:

| Sub-release | 内容 | 規模 | 想定期間 | 差別化軸 |
|---|---|---|---|---|
| **v1.9-A** | Cache Observability — Request 側で `cache_control` 検出 + log、Response 側で `cache_read_input_tokens` / `cache_creation_input_tokens` を metrics 集計、dashboard / `/metrics.json` で provider 別 cache hit 率を可視化 | ~200-300 LOC、tests +5 | 3-5 日 | Claude Code 特化 cluster 内で唯一 + LiteLLM の undercounting バグ回避設計 |
| **v1.9-B** | Cross-backend cache passthrough + capability gate — `kind: anthropic` で cache_control 透過確認、registry に `capabilities.cache_control` 追加、doctor cache probe 新設 | ~300-400 LOC、tests +8 | 5-7 日 | LM Studio 0.4.12 検証で前提成立 |
| **★ v1.9-E (前倒し)** | Long-run Guards 三段: (a) **Memory pressure awareness** (L2 対処): backend 残メモリ probe + しきい値で軽量モデル切替、(b) **Tool loop detection** (L3 対処): tool args 重複検知 + break、(c) **Backend health continuous monitoring** (L5 対処): 受動 + 能動 probe で crash 自動検出 → fallback chain promote | ~600-900 LOC、tests +15 | 1-2 週間 | **Vision の核心、メイン顧客 B (秘書ツール user) への最大訴求** |
| **v1.9-C** | Adaptive Routing (実 latency / error rate を rolling window、health-based 動的 priority、デバウンス) | ~500-700 LOC、tests +12 | 1-2 週間 | claude-code-router の task-based と被らない補完軸 |
| **v1.9-D** | Cost-aware Dashboard (provider 別 cost config、cache savings 別枠表示、`coderouter stats --cost`) | ~300-500 LOC、tests +8 | 5-7 日 | LiteLLM ですら未対応の cache savings 計算 |

順序: **A → B → ★ E → C → D** (B 顧客優先で E を 3 番目に前倒し、2026-04-27 確定)。**全 sub-release は v1.9.0a1〜a6 + GA で出荷済み (2026-04-28〜29)**。詳細は [CHANGELOG `[v1.9.0]`](./CHANGELOG.md) を参照。

#### v1.9 note 記事計画 (4 連作の続き)

| 出荷 | 記事タイトル候補 | 訴求軸 |
|---|---|---|
| v1.9-A 後 | 「ローカル LLM で Anthropic prompt caching の動作を可視化した話」 | 観測価値 |
| v1.9-B 後 | 「CodeRouter を Anthropic prompt caching aware にした実装記録」 | 機能拡張 |
| **v1.9-E 後** | **「Claude Code を 8 時間回したら起きた 6 つの障害と CodeRouter の対処」** | **★ 最大訴求** |
| v1.9-C 後 | 「Adaptive routing を実装した記録 — 実 latency に基づく動的 priority」 | 差別化軸 |
| v1.9-D 後 (umbrella) | 「Adaptive caching で実コストが下がった話 — v1.9 まとめ」 | 価値証明 |

#### Reactive 追加 (発火条件 6 種類、運用ルール)

「reactive but focused」運用ルール (2026-04-27 確定)。以下のいずれかが起きた時のみ計画に追加検討:

1. **新 backend major update** (LM Studio / Ollama / llama.cpp / vLLM / MLX-LM)
2. **新モデル登場** (Qwen / Gemma / GLM / Llama 等の major version)
3. **Anthropic API spec 変更** (新 endpoint、新 capability、新 cache 機構)
4. **Claude Code 挙動変化** — 実機検証 → adapter 修正
5. **コミュニティ PR / issue** — レビュー → merge or close (issue #10 のスタイル)
6. **競合 OSS の新機能** — 追従するか差別化するか判断

これら 6 種類以外のトレンドは **無視して計画を粛々と進める**。



### ローカル backend 別接続マトリクス + テスト方針 (現役、運用中)

CodeRouter は `kind: openai_compat` 一種類で **Ollama / llama.cpp / LM Studio / vLLM / MLX-LM** いずれにも繋がる設計。各 backend の接続レシピと doctor probe での検証方法を以下にまとめる。

| Backend | デフォルトポート | `base_url` (CodeRouter から) | 検証ステータス | 専用 doc |
| --- | --- | --- | --- | --- |
| **Ollama** | `11434` | `http://localhost:11434/v1` | ✅ v0.x 〜 v1.8.x 通して継続検証 | [`docs/quickstart.md`](./docs/quickstart.md) / [`docs/troubleshooting.md` §4-2](./docs/troubleshooting.md) |
| **llama.cpp `llama-server`** | `8080` | `http://localhost:8080/v1` | ✅ v1.8.3 で実機検証 (Qwen3.6:35b-a3b on Unsloth UD-Q4_K_M、native `tool_calls` 完璧動作) | [`docs/llamacpp-direct.md`](./docs/llamacpp-direct.md) |
| **LM Studio** | `1234` | `http://localhost:1234/v1` (OpenAI 互換) または `http://localhost:1234` (Anthropic 互換 `/v1/messages`) | ✅ v1.8.4 で実機検証 (Qwen3.5 9B / Qwen3.6 35B-A3B / Qwopus3.5-9B-v3 すべて native tool_calls + tool_use OK、`cache_read_input_tokens: 280` 観測で Anthropic prompt caching 成立) | (新規 `docs/lmstudio-direct.md` を v1.9 で予定) |
| **vLLM** | `8000` (server start で変更可) | `http://localhost:8000/v1` | ⏳ TODO (CUDA / data center GPU 前提、Mac M3 Max は対象外) | TBD |
| **MLX-LM** | `8080` (`mlx_lm.server` 起動) | `http://localhost:8080/v1` | ⏳ TODO (Mac native、量子化が Apple Silicon 最適化) | TBD |

### 共通の検証手順 (どの backend にも適用可)

1. **server 起動** — backend ごとの方法でモデルをロード + OpenAI 互換 API を listen させる
2. **CodeRouter `providers.yaml` に provider 定義を追加** — `kind: openai_compat` + 該当 `base_url`、必要に応じて `capabilities.thinking: true` (reasoning モデル時)
3. **`coderouter doctor --check-model <name>`** で 6 probe (`auth+basic-chat / num_ctx / tool_calls / thinking / reasoning-leak / streaming`) を回す
4. **CodeRouter 経由 Anthropic 互換 curl で end-to-end 1 round-trip** を確認
5. NEEDS_TUNING が出たら `--apply` で patch 自動適用 → 再 probe

### 各 backend で確認すべき固有ポイント

| Backend | 確認ポイント |
|---|---|
| **Ollama** | `/api/chat` (native) と `/v1/chat/completions` (OpenAI-compat) で挙動差異あり、`extra_body.options.num_ctx` の効き、Modelfile の `PARAMETER num_ctx` 焼き込み、新 architecture の `unknown model architecture` 500 エラー |
| **llama.cpp** | `--jinja` で chat template が効くか、`reasoning_content` フィールド名 (Ollama は `reasoning`)、Metal / CUDA build flag、Unsloth Dynamic Quantization (UD-Q4_K_M) の精度優位 |
| **LM Studio** | OpenAI 互換 endpoint の挙動が Ollama / llama.cpp と微妙に違う可能性、reasoning フィールドの命名、UI で context length / max tokens を server start 時に指定する必要 |
| **vLLM** | `--enable-auto-tool-choice` フラグ、tool spec 形式 (Hermes / Mistral / Llama3 のどれを採用するか)、Continuous batching の動作 |
| **MLX-LM** | Apple Silicon 専用、`mlx_lm.server` 起動時の量子化指定、tool_calls 対応状況 (やや限定的の可能性) |

### LM Studio 接続レシピ (v1.8.4 で実機検証済み)

LM Studio 0.4.12+ で Anthropic 互換 `/v1/messages` 公式サポート + Qwen 3.5/3.6 性能改善が入った。CodeRouter からは **OpenAI 互換 / Anthropic 互換** の 2 経路で接続可能、後者は `kind: anthropic` で adapter 翻訳ゼロ透過。

検証手順 + providers.yaml の sample は `examples/providers.yaml` の `lmstudio-*` 4 entry を参照。詳細ガイド [`docs/lmstudio-direct.md`](./docs/lmstudio-direct.md) は v1.8.5 で出荷済み。

### v1.10 候補 / v1.9.x 残課題 (実機検証フィードバック反映、未着手分)

> **v1.9.1 で完了済 (2026-05-01)**:
> - **v1.9-B2** streaming 経路の usage 集約 (`_StreamUsageAccumulator` + `_emit_cache_observed_streaming`、+3 tests)
> - **per-model auto-routing** (`RuleMatcher.model_pattern` + `re.fullmatch` semantics + signals payload に model 追記、+5 tests)
> 詳細は CHANGELOG `[v1.9.1]` 参照。

- **v1.9-E phase 2 候補**: L2 Memory pressure (LM Studio / ollama backend OOM 検知) / L5 Backend health (continuous probe + chain reorder) — phase 1 (L3 Tool-loop guard) は v1.9.0 で出荷済み。**Vision の核心 (8 時間 agent ループでも止まらない)** を完成させる pillar、~900 LOC / 1-2 週間
- **provider 月次予算上限** (LiteLLM 由来、v1.9-D の累積版) — `monthly_budget_usd` で provider 単位の running total + 超過時 skip + log。~400 LOC / 3-5 日
- **longContext auto-switch** — `auto_router` rule type 5 として `content_token_count_min` matcher 追加 (claude-code-router task-based 取込)。~200 LOC / 3-5 日
- **`docs/verification.md` の精緻化**: v1.9.0 GA 直前の実機検証で発見した知見 (MoE モデルの罠、rolling-window タイミング制約、サイズ差を作るテクニック) を verification 手順に反映

> v2.0 以降の機能 (Pillar 別 deepening / プラグイン / MCP server / Web UI) は内部メモで別途整理

#### ❓ 検討中 — 実装方針 / 必要性が未確定

| 領域 | 内容 | 状況 |
| --- | --- | --- |
| **PEP 541 reclamation** | PyPI の bare `coderouter` 名前空間 (現所有者 Lawrence Chen、HTTP routing 系汎用ライブラリ、2025-06 single 0.1.0、ドメイン完全別物) を申請して引き取り、`coderouter-cli` を alias 化、canonical を `coderouter` に戻す | 申請は可能だが審査に 1〜数ヶ月、結果は他者要因。間 `coderouter-cli` で運用 |
| **Docker イメージ提供** | 公式 Dockerfile + GHCR multi-arch 配布 | `uvx coderouter-cli` で onboarding 摩擦が十分低くなった結果、Docker は需要次第。「`docker run zephel01/coderouter:1.7.0`」が要るユースケース (CI / k8s) が顕在化したら実施 |
| **`coderouter-cli` を Go で別配布** | Python 配布で詰んだ場合の B プラン (§16 リスク対応案) | 現状 PyPI publish が安定して機能しているため保留。将来 (i) Python 環境構築の摩擦が再燃 / (ii) single static binary の需要、いずれかで再評価 |
| **`npm i -g coderouter` 経路** | Node ユーザー向け配布 | uvx で十分という判断。Claude Code が npm 経由なので「同じ install 経路」需要が顕在化したら検討 |
| **Anthropic ヒューリスティック表のメンテ signal** | (a) 週次 `/v1/models` diff / (b) 未知モデル検出時に warn ログ。capability registry をモデルファミリ追加に追従させる仕組み | (a) (b) どちらも実装可能、(b) は既存 gate 計算からほぼ無料で取れる。v1.7-B 以降の自然なタイミングで |
| **依存最小主義の「次の絞り」** | 5 deps 据え置き 17 sub-release の継続 vs `httpx` HTTP/2 / async 安定性のための backport 受容 | 需要なし、現状維持の方針 (§5.4)。BREAKING に踏み込むなら別途 |

#### ❌ やらないこと (Out of Scope, 少なくとも v2.0 まで — §15)

- 音声 (NarrateClaude 領域)
- ブラウザ操作 (browser-agent 領域)
- iMessage / 通知システム連携
- 全 provider を完全同一 payload で扱う統一化 (Anthropic は別アダプタのまま)
- 学習 / fine-tuning パイプライン

---

### リリース履歴 (直近 5 件)

過去の全リリース履歴と各 release の詳細は [`CHANGELOG.md`](./CHANGELOG.md) を参照。

| Ver | 日付 | タグ | 一言 |
| --- | --- | --- | --- |
| **v1.9.1** | 2026-05-01 | `v1.9.1` | v1.10 候補から quick win 2 件を patch で先行刈取り — (a) v1.9-B2 streaming 経路の usage 集約 (`_StreamUsageAccumulator` で `cache-observed` log の placeholder を観測値に置換)、(b) per-model auto-routing (`RuleMatcher.model_pattern` 5 番目 matcher、Opus/Sonnet/Haiku で profile 分岐、free-claude-code 由来)。tests +8 (830→838)、Runtime deps 据え置き 30 連続、完全互換 |
| **v1.9.0** | 2026-04-29 | `v1.9.0` | Umbrella tag — Cache observability + Adaptive routing + Cost-aware + Long-run reliability (6 sub-release 統合 a1〜a6 + GA、L3 break action ingress fix 含む) |
| **v1.8.4** | 2026-04-27 | (検証 release、PyPI 未 publish) | LM Studio 0.4.12 で Qwen3.5 9B / Qwen3.6 35B-A3B / Qwopus3.5-9B-v3 全動作確認 + Anthropic prompt caching 成立 (`cache_read_input_tokens: 280`)、`examples/providers.yaml` に lmstudio-* 4 entry + test profile 2 件 |
| **v1.8.3** | 2026-04-26 | `v1.8.3` | tool_calls probe を thinking 対応 + adapter で `reasoning_content` strip — llama.cpp 直叩きで Qwen3.6 復権、active-harmful 誤診断 (tools=false suggestion) を解消 |
| **v1.8.2** | 2026-04-26 | `v1.8.2` | doctor probe を thinking モデル対応 (num_ctx 32→256/1024、streaming 128→512/1024) — Gemma 4 偽陽性解消、メタ教訓「diagnostic ツール自身も diagnostic され続ける必要がある」 |

過去のリリース (v0.1.0〜v1.7.0) は [`CHANGELOG.md`](./CHANGELOG.md) の各エントリ、各マイルストーンの DoD・実装知見は該当セクション（v0.1: §7 / v0.2: §8 / v0.5: §9 / 横断ログ: §18）に格納。

振り返り: [`docs/retrospectives/v0.4.md`](./docs/retrospectives/v0.4.md) / [`docs/retrospectives/v0.5.md`](./docs/retrospectives/v0.5.md) / [`docs/retrospectives/v0.5-verify.md`](./docs/retrospectives/v0.5-verify.md) / [`docs/retrospectives/v0.6.md`](./docs/retrospectives/v0.6.md) / [`docs/retrospectives/v0.7.md`](./docs/retrospectives/v0.7.md) / [`docs/retrospectives/v1.0.md`](./docs/retrospectives/v1.0.md) / [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md)。

<!-- 過去 release 一覧 (v0.1.0〜v1.7.0) は CHANGELOG.md に集約、plan.md 上は最新 5 件のサマリのみ
| v0.1.0 | 2026-04-20 | `v0.1.0` | `5efff5b` | Walking Skeleton — OpenAI ingress + local + fallback 1 個、26 tests green |
| v0.2.0 | 2026-04-20 | `v0.2.0` | `6c6e3f4` | Anthropic ingress — Claude Code 疎通、+28 tests / 計 54 green |
| v0.3.0 | 2026-04-20 | `v0.3.0` | `5261dae` | Tool-call repair + mid-stream guard + usage 集計 + tool-call streaming downgrade、+33 tests / 計 87 green |
| v0.4-A | 2026-04-20 | `v0.4-A` | `e566bce` | Anthropic native adapter + ChatRequest → AnthropicRequest 逆翻訳 + `anthropic-beta` header passthrough、+66 tests / 計 153 green |
| v0.5.0 | 2026-04-20 | `v0.5.0` | `8444f6b` | Capability gate trio — thinking / cache_control / reasoning の統一 `capability-degraded` 契約、+72 tests / 計 225 green |
| v0.5.1 | 2026-04-20 | `v0.5.1` | `3c332bd` | Closeout pack: `capability-degraded` TypedDict 化 / streaming verify scenario / `chain-uniform-auth-failure` warn、+18 tests / 計 243 green |
| v0.5-D | 2026-04-20 | (未タグ) | — | OpenRouter roster 週次 cron (`scripts/openrouter_roster_diff.py`) + CHANGES.md 自動追記、+24 tests / 計 267 green |
| v0.6-A | 2026-04-20 | (未タグ) | — | `--mode` CLI + `CODEROUTER_MODE` env + `default_profile` 起動時検証、+8 tests / 計 275 green |
| v0.6-B | 2026-04-20 | (未タグ) | — | `FallbackChain.timeout_s` / `append_system_prompt` + `ProviderCallOverrides`、+8 tests / 計 283 green |
| v0.6-C | 2026-04-20 | (未タグ) | — | 宣言的 ALLOW_PAID gate + `chain-paid-gate-blocked` 集約 warn (typed payload)、+8 tests / 計 291 green |
| v0.6-D | 2026-04-20 | (未タグ) | — | `mode_aliases` + `X-CodeRouter-Mode` header → profile 解決 (intent/impl 名前空間分離)、+15 tests / 計 306 green |
| v0.6.0 | 2026-04-20 | `v0.6.0` | — | Umbrella tag: v0.6-A/B/C/D を束ねる。Chain-as-first-class-object (startup validator × 4 / typed log payload × 2 / `_resolve_chain` 4-entry-point chokepoint)、計 306 green / +39 from v0.5-D |
| v0.7-A | 2026-04-20 | (未タグ) | — | 宣言的 `model-capabilities.yaml` registry — v0.5-A regex heuristic を YAML 外出し、bundled + user 2 層、glob + first-match-per-flag semantics、+39 tests / 計 345 green |
| v0.7-B | 2026-04-20 | (未タグ) | — | `coderouter doctor --check-model <provider>` — 4 probe (auth/tool_calls/thinking/reasoning-leak) で registry ↔ 実機の差分を診断、`providers.yaml` / `model-capabilities.yaml` patch emit、exit 0/2/1 CI 対応、+37 tests / 計 382 green |
| v0.7-C | 2026-04-20 | (未タグ) | — | README Troubleshooting に 5 silent-fail symptoms 章立て (各症状 × `coderouter doctor` 実行例 × YAML patch × fix command) + `examples/providers.yaml` に `ollama-hf-example` HF-on-Ollama reference stanza 追加 + lunacode `MODEL_SETTINGS.md` cross-link、docs only / 計 382 green |
| v0.7.0 | 2026-04-20 | `v0.7.0` | — | Umbrella tag: v0.7-A / v0.7-B / v0.7-C を束ねる。Beginner UX, made legible — 宣言 (YAML registry) → probe (doctor) → 文書化 (5 症状 Troubleshooting) の 3 段階で observation ループを閉じる。計 382 green / +76 from v0.6.0 / retrospective: [`docs/retrospectives/v0.7.md`](./docs/retrospectives/v0.7.md) |
| v1.0-A | 2026-04-20 | (未タグ) | — | 宣言的 `output_filters` filter chain — `strip_thinking` / `strip_stop_markers`、streaming + non-streaming stateful 動作、OpenAI-compat + Anthropic native 両 adapter hook、doctor reasoning-leak probe 拡張で content-embedded `<think>` 検出 + YAML patch emit、+49 tests / 計 431 green |
| v1.0-B | 2026-04-20 | (未タグ) | — | Doctor `num_ctx` probe 直接検出 — canary echo-back で Ollama silent truncation (plan §9.4 symptom #1) を直接検出、5-verdict branch + `extra_body.options.num_ctx: 32768` patch emit、Ollama-shape gating (:11434 port / declared options) で非 Ollama は SKIP、+10 tests / 計 441 green |
| v1.0-C | 2026-04-20 | (未タグ) | — | Doctor streaming-path probe — v1.0-B の output-side 鏡像、"count 1 to 30" deterministic prompt を streaming で投げ `finish_reason=length` + 短 content から output-side truncation 直接検出、`extra_body.options.num_predict: 4096` patch emit、副次症状 "2xx で 0 chunk" を advisory (server-side 修正事項のため patch 空)、Ollama-shape gating 共用、probe 順序は最末尾 (auth→num_ctx→tool_calls→thinking→reasoning-leak→streaming)、+12 tests / 計 453 green |
| v1.0.0 | 2026-04-20 | `v1.0.0` | — | Umbrella tag: v1.0-A / v1.0-B / v1.0-C を束ねる。The observation loop, closed — transformation (v1.0-A output filter chain) + probe (v1.0-B `num_ctx` direct / v1.0-C streaming direct) の対で Ollama 2-knob silent-fail 両面を観測可能に。Ollama-shape 2-signal gate + symptom-orthogonality probe ordering heuristic が v1.0-native pattern。v0.5-verify pattern を 2nd instance として再利用 (scripts/verify_v1_0.sh + bare/tuned delta assertion)。計 453 green / +71 from v0.7.0 / retrospective: [`docs/retrospectives/v1.0.md`](./docs/retrospectives/v1.0.md) |
| v1.0.1 | 2026-04-21 | (未タグ) | — | Hygiene pass — `CodeRouterError` root 例外 (`coderouter/errors.py` 新設 + top-level re-export、既存 3 leaf を `Exception` → `CodeRouterError` に帰属) + docstring 網羅率 75.6% → 91.2% (`interrogate` 基準、public API 全 100%) + mypy `--strict` 0 errors。+4 tests / 計 457 green。semver 上 patch-level、import パス完全非破壊 |
| v1.5-A | 2026-04-22 | (未タグ) | — | `MetricsCollector` (logging.Handler 経由 in-memory ring) + `GET /metrics.json` (counters + providers + recent 50 + startup)、app.py 起動時アタッチ、+41 tests (collector 16 / metrics_endpoint 9 / integration 分) |
| v1.5-B | 2026-04-22 | (未タグ) | — | Prometheus text exposition (`GET /metrics`、`coderouter_*` プレフィクス、gauge + counter 混成) + `$CODEROUTER_EVENTS_PATH` JSONL mirror (env-gated、snapshot から完全独立な side-effect)、+16 tests (prometheus 11 / jsonl 5) |
| v1.5-C | 2026-04-22 | (未タグ) | — | `coderouter stats` CLI TUI (stdlib `curses` + `urllib`、5 パネル: Providers / Fallback & Gates / Requests/min sparkline / Recent Events / Usage Mix) + `--once` mode (TTY 不在で単発レンダー、CI / pipe 用)、pure data+render レイヤと driver を分離、+39 tests (CLI stats data layer + render) |
| v1.5-D | 2026-04-22 | (未タグ) | — | `/dashboard` HTML 1 ページ (tailwind CDN + fetch polling 2 秒間隔、dashboard_routes.py + 埋め込み HTML) + app.py へのルーター配線、+12 tests (dashboard_endpoint integration) |
| v1.5-E | 2026-04-22 | (未タグ) | — | `display_timezone` config フィールド (`providers.yaml` top-level、IANA zone 名、未設定時 UTC)。CLI TUI (`cli_stats.py`) / HTML dashboard 両方の表示層だけに適用、集約された UTC 時刻は保持、`/metrics.json` の `config.display_timezone` で JS 側に伝搬。`examples/providers.yaml` に記載、+2 tests (display_timezone 専用) |
| v1.5-F | 2026-04-22 | (未タグ) | — | `scripts/demo_traffic.sh` — weighted scenario picker (normal 4/10 / stream 3/10 / burst+idle 2/10 / fallback 1/10 + paid-gate every 8th tick)、`--duration` / `--serve` / `--dry-run`、banner + expected-count table、bash 3.2 互換 (heredoc-in-`$()` 排除、bare `wait` → `wait_pids` PID-tracked) + README にライブ dashboard スクショ `docs/assets/dashboard-demo.png` を追加 |
| v1.5.0 | 2026-04-22 | `v1.5.0` | — | Umbrella tag: v1.5-A/B/C/D/E/F を束ねる。Observability pillar — plan.md §12 を丸ごと受ける minor、計測 / 可視化 / 配信 / timezone / demo の 5 柱。計 **527 tests green** (457 → 527、+70)、Runtime deps 据え置き (curses + urllib は stdlib、tailwind は CDN、Prometheus 形式は自前文字列生成)。`v1.0.1 → v1.5.0` で §11 (旧 v1.1 = 配布 / launcher / doctor) を飛ばし越した結果、§11 ヘッダは "v1.6" にリラベル |
| v1.6.0 | 2026-04-22 | `v1.6.0` | `a6ac84b` | Umbrella tag: v1.6-A/B/C を束ねる。`auto_router` (task-aware routing) — plan.md §11 を 3 sub-release で受け、`default_profile: auto` sentinel + 4-variant `RuleMatcher` + bundled ruleset (image→multi / code-fence≥0.3→coding / else→writing) + `/v1/messages` / `/v1/chat/completions` 両 ingress precedence 拡張 + `coderouter_auto_router_fallthrough_total` Prometheus counter + `examples/providers.auto.yaml` / `providers.auto-custom.yaml`。527 → 596 tests green (+69)、Runtime deps 据え置き (13 sub-release 連続) |
| v1.6.1 | 2026-04-23 | `v1.6.1` | — | Patch-level: (1) **NVIDIA NIM 無料枠 (40 req/min) 対応** — `examples/providers.nvidia-nim.yaml` 新設 (`claude-code-nim` / `nim-first` / `free-only-nim` / `nim-reasoning` の 4 プロファイル、local → NIM → OpenRouter free → paid の 8 段チェーン)、live 検証 (2026-04-23) 済み 3 tool-capable モデル採用、`tests/test_examples_yaml.py` で CI invariants。(2) **ドキュメント言語優先度スワップ** — README.md / docs/{usage-guide,security,quickstart,when-do-i-need-coderouter}.md の 5 ペアを日本語 main / 英語 `.en.md` sub に入替、pyproject `readme` も日本語に。(3) **README ヒーロー書き換え** — tool-call repair を最前面に。(4) **`docs/free-tier-guide.md`** 新規 (NIM + OpenRouter 無料枠の使い分け reference、JA + EN)。(5) `coderouter/__init__.py` を `importlib.metadata.version` 経由に (`--version` 出力修正、pyproject が single source of truth)。596 → **601 tests green** (+5)、Runtime deps 据え置き (14 sub-release 連続) |
| v1.6.2 | 2026-04-24 | (未タグ) | — | Patch-level: v1.6.1 出荷後の実機運用で踏んだ罠を docs / examples 側に集約する小さな release。(1) **`docs/troubleshooting.md` / `.en.md` 新規** — README §トラブルシューティングを独立化した上で、v1.6.2 検証で発覚した 5 トピック (CLI 訂正 `serve --mode` / `.env` の `export` 必須 / `env` 検証 / `Header of type authorization was missing` 401 切り分け / `~/.zshrc` 反映漏れ / Llama-3.3-70B 系の過剰ツール呼び出し / `UserPromptSubmit hook error` (claude-mem 等プラグイン相性) / auto-compact 遅延 / ダッシュボード活用) を §1 / §4 として追加。(2) **README §トラブルシューティング短縮** — 30 秒早見表 + 症状別 4 入口索引に置換、Ollama 5 症状は 1 行サマリ + リンクのみ。旧アンカー (`ollama-初心者...` / `ollama-beginner...`) を後方互換のため残置。(3) **`examples/.env.example` 全キー `export` 必須化** — 冒頭に load 手順 / 検証コマンドの documentation 追加。(4) **`examples/providers.nvidia-nim.yaml` 4 プロファイル並び替え** — Llama-3.3-70B を最後尾に、Qwen3-Coder-480B を第一選択に (実機検証で Llama が Claude Code 単独で過剰ツール呼び出しを起こすため)。プロファイル選定理由のコメント追記。(5) **`examples/providers.nvidia-nim.yaml` セットアップコメント拡張** — 5 ステップ + `.env` export 必須 / `--port 8088` 整合の明記。(6) **`docs/articles/note-nvidia-nim.md` 改訂** — §6 / §7 に実機検証ログ追加。601 → **601 tests green** (±0、コード変更なし、`tests/test_examples_yaml.py` の既存 invariant が profile 並び替え後も pass することで間接検証)、Runtime deps 据え置き (15 sub-release 連続) |
| v1.6.3 | 2026-04-24 | (未タグ) | — | Patch-level: v1.6.2 で文書化した「`.env` の `export` 漏れ」「事故的 git commit」を**コマンドで解消**する 2 機能を投入。(1) **`coderouter serve --env-file PATH`** — `.env` style file を uvicorn 起動前にロード、複数指定で left-to-right layering、デフォルトは shell 優先 (file は不在キーを埋める) で `--env-file-override` で反転。1Password CLI (`op run --env-file=...`) / direnv + sops / OS Keychain の出力を直接受け取れる gateway。(2) **`coderouter doctor --check-env [PATH]`** — `.env` を 4 項目検査 (existence / POSIX 0600 / `.gitignore` 包含 / git tracking)、`--check-model` と同 exit code 規約 (0/2/1)、WARN は `chmod 0600` 等の 1 行 fix、ERROR (既に追跡) は `git rm --cached` 入りの remediation 手順を emit。(3) **stdlib-only `.env` parser** (`coderouter.config.env_file`) — 1Password / sops / 手書きが実際に出すサブセット (bare / 双引用符内 `\n`/`\t`/`\"` escape / 単引用符 literal / `export` prefix / inline `#` / blank) を網羅、POSIX-invalid key と未終端 quote は `file:lineno` で明示。(4) **`docs/troubleshooting.md` / `.en.md` §5 新設** — 「`.env` のセキュリティ運用」7 サブセクション (脅威モデル + チェックリスト + 1Password CLI / direnv+sops / OS Keychain の 3 連携レシピ + `--env-file` layering + キースコープ最小化)。**自前暗号化を実装しない判断** を §5-1 で明文化 (脅威モデル 7 種中 at-rest 暗号化が効くのは 2 種、復号鍵の置き場所問題が残るため、既存ツール統合のほうが正解)。(5) **`--check-model` を required から optional へ** — `--check-env` が代替として導入されたため両方 optional、`_run_doctor` 内で「最低 1 つ必要」を強制 + exit 1。601 → **651 tests green** (+50: env_file 26 + env_security 15 + cli 8 + 1 renamed)、Runtime deps 据え置き (16 sub-release 連続)、`pyproject.toml version` を 1.6.1 → 1.6.3 (1.6.2 は docs only だったため minor inc を v1.6.3 で吸収) |
| v1.8.3 | 2026-04-26 | `v1.8.3` | — | **Patch: tool_calls probe も thinking 対応 + adapter で `reasoning_content` strip — llama.cpp 直叩き対応** (M3 Max 64GB / Qwen3.6:35b-a3b on llama-server / Unsloth UD-Q4_K_M)。v1.8.2 同日リリースの第 2 弾 patch。実機検証で発見した 2 件の追加課題を解消: (1) `coderouter/doctor.py` の `_probe_tool_calls` の `max_tokens=64` 固定を thinking-aware budget (256/1024) に動的選択、`_TOOL_CALLS_PROBE_MAX_TOKENS_DEFAULT/_THINKING` 定数新設、既存 `_is_reasoning_model()` で分岐。旧 64 では Qwen3.6 が `reasoning_content` で食い切り偽陽性 NEEDS_TUNING + 真逆 suggestion (`tools: false`) を出す **active-harmful 誤診断** を出していた、これは v1.8.2 で num_ctx / streaming に対して直したのと同じバグ pattern を tool_calls にも適用。(2) `coderouter/adapters/openai_compat.py` の `_strip_reasoning_field` を `_NON_STANDARD_REASONING_KEYS = ("reasoning", "reasoning_content")` 両方 strip するように拡張、log の `dropped` も両方記載。`reasoning` (Ollama / OpenRouter 命名) と `reasoning_content` (llama.cpp 命名) は同概念のベンダー命名差異。`coderouter/doctor.py` の `_probe_reasoning_leak` も `reasoning_content` 検出に拡張。`tests/test_doctor.py` に `test_tool_calls_max_tokens_bumped_for_thinking_provider` を追加 (thinking provider で 1024 要求 + native tool_calls 応答 OK 判定)、`tests/test_reasoning_strip.py` に 3 件追加 (`reasoning_content` 単独 / 両方共存 / delta variant)、既存の `dropped == ["reasoning"]` assertion を `["reasoning", "reasoning_content"]` に更新。**Ollama 経由詰み Qwen3.6 の真因確定**: Ollama chat template / tool 仕様未成熟、モデル本体は健全で llama.cpp 直叩きでは native `finish_reason: "tool_calls"` + `tool_calls[]` が完璧に出る。733 → **737 tests green** (+4)、Runtime deps 据え置き (21 sub-release 連続)。**メタ教訓を実証**: v1.8.2 で「diagnostic ツール自身も diagnostic され続ける必要がある」と書いた直後、まさにその例が tool_calls probe にも残っていたことが実機で発見された。詳細は CHANGELOG.md `[v1.8.3]` |
| v1.8.2 | 2026-04-26 | `v1.8.2` | — | **Patch: doctor probe を thinking モデル対応** (M3 Max 64GB / Ollama 0.21.2)。v1.8.1 出荷直後の深掘りで `doctor` の `num_ctx` / `streaming` probe が thinking モデル (Gemma 4 26B、Qwen3.6 系) に対して reasoning トークン消費分を見ていない `max_tokens=32` / `128` バジェットで偽陽性 NEEDS_TUNING を出していた事実を発見。(1) `coderouter/doctor.py` に `_is_reasoning_model(provider, resolved)` ヘルパ追加、`_probe_num_ctx` / `_probe_streaming` の `max_tokens` を thinking 検出付きの動的選択に変更 (num_ctx 32→256/1024、streaming 128→512/1024)。非 thinking モデルは natural stop で早期終了するため無駄消費なし、thinking モデルは reasoning trace + 答えが収まる headroom。(2) `coderouter/data/model-capabilities.yaml` で `gemma4:*` / `google/gemma-4*` / `qwen3.6:*` / `qwen/qwen3.6-*` に `thinking: true` を追加 (registry 経由で渡るので user は providers.yaml 触らずに doctor の thinking バジェットが効く)。Qwen3.6 セクションのコメントを v1.8.2 で「num_ctx / streaming は doctor 偽陽性、tool_calls [NEEDS TUNING] が真の課題として残る」へ整理。(3) `tests/test_doctor.py` に 3 件追加 (provider declaration / registry-based / streaming)、既存 num_ctx merge test の `max_tokens == 32` を `== 256` に更新、streaming merge test に `max_tokens == 512` assertion 追加。**実機検証**: M3 Max 64GB / `gemma4:26b` で `/v1/messages` Anthropic 互換経由 "Hello." が 2 秒応答、`tool_calls native OK`、`reasoning strip` 動作、`ollama ps` で context length 262144 確認。730 → **733 tests green** (+3)、Runtime deps 据え置き (20 sub-release 連続)。**メタ教訓**: diagnostic ツール自身も diagnostic され続ける必要がある (§5.4 の補強)。詳細は CHANGELOG.md `[v1.8.2]` |
| v1.8.1 | 2026-04-26 | `v1.8.1` | — | **Patch: 実機検証反映** (M3 Max 64GB / Ollama 0.21.2)。(1) `coderouter/config/loader.py` の `CODEROUTER_MODE` env が `mode_aliases` 解決せずに `default_profile` 直接代入する v0.6-A 素朴実装を修正、startup と runtime (`X-CodeRouter-Mode`) を symmetric に。NIM example yaml ベースで `cr serve --mode coding` が validation エラーで起動失敗していた問題を解消。(2) `examples/providers.nvidia-nim.yaml` に `mode_aliases` 追加 (NIM ユーザーも canonical 短縮 alias 利用可)。(3) `examples/providers.yaml` の `coding` profile primary を Qwen3.6 → Gemma 4 + Qwen-Coder family へ実機検証反映調整。(4) `coderouter/data/model-capabilities.yaml` の `qwen3.6:*` / `qwen/qwen3.6-*` の `claude_code_suitability: ok` を撤回 (実機 num_ctx silent cap / tool_calls 0 chars / streaming 0 chars の 3 重 NEEDS_TUNING、note 伝聞ベースの先回り宣言は declaration 過信)。(5) `docs/troubleshooting.md` §4-2 新設「ローカル Ollama 経由の Known Issues」(4 サブセクション: §4-2-A Qwen3.6 系課題、§4-2-B Qwen3.5 系 llama.cpp `qwen35` architecture 未対応、§4-2-C Gemma 4 26B 無加工 tool_calls OK 確認、§4-2-D ベスト実践「枯れたモデル + 観測ツール」)。`tests/test_config.py` に `test_env_mode_resolves_through_mode_aliases_at_startup` 追加 (alias 解決成功 / 直接 profile 名 / 未知 mode で fast-fail の 3 ケース)。729 → **730 tests green** (+1)、Runtime deps 据え置き (19 sub-release 連続)。**「先回り実装より実機 evidence」原則 (§5.4) を再確認**。詳細は CHANGELOG.md `[v1.8.1]` |
| v1.8.0 | 2026-04-26 | `v1.8.0` | — | **Minor: 用途別 4 プロファイル + GLM/Gemma 4/Qwen3.6 公式化 + apply 自動化** (= v1.7-B umbrella、6 サブリリース統合)。(1) PyPI Trusted Publishing 自動化 (OIDC、tag push で自動 publish + Release 草稿)、(2) `claude_code_suitability` hint (Llama-3.3-70B 系を `claude-code-*` profile に置くと startup で `chain-claude-code-suitability-degraded` warn)、(3) `doctor --check-model --apply` / `--dry-run` (`ruamel.yaml` round-trip で YAML パッチ非破壊書き戻し、コメント・key 順序保持、冪等)、(4) `setup.sh` onboarding ウィザード (RAM 検出 → 推奨モデル → `ollama pull` → providers.yaml 生成、bash 3.2、依存ゼロ)、(5) examples/providers.yaml を `multi` (default) / `coding` / `general` / `reasoning` の 4 プロファイル化 + 全プロファイルに `append_system_prompt` で Claude 風応答 nudge + `mode_aliases`、(6) Ollama 公式 tag 化された `gemma4:e4b/26b/31b` / `qwen3.6:27b/35b` を active stanza として登録、Z.AI を OpenAI-compat で 2 base_url 提供、bundled model-capabilities.yaml に `qwen3.6:*` (claude_code_suitability=ok) / `gemma4:*` / `GLM-5*` / `GLM-4.[5-9]*` を新規宣言。"Claude Sonnet/Opus との挙動互換性" を 3 段の対策 (モデル選定 + append_system_prompt + output_filters) で実現。651 → **710 tests green** (+59、+9.1%)、Runtime deps 据え置き (18 sub-release 連続、`ruamel.yaml` は optional `[doctor]` extras で lazy import)。詳細は CHANGELOG.md `[v1.8.0]` |
| v1.7.0 | 2026-04-25 | `v1.7.0` | — | PyPI 公開 + uvx 経路の整備 (`coderouter-cli` として publish、Trusted Publishing 経路) |
-->

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
  - [6.2 リリース履歴 (詳細)](#62-リリース履歴-詳細)
- [7. v0.1 — Walking Skeleton ✅](#7-v01--walking-skeleton--完了-2026-04-20)
- [8. v0.2 — Anthropic Ingress ✅](#8-v02--anthropic-ingress--完了-2026-04-20)
- [9. v0.5 — Capability Gate Trio ✅](#9-v05--capability-gate-trio--完了-2026-04-20)
- [10. v1.0 — Tool-Call 信頼性 + Code Mode](#10-v10--tool-call-信頼性--code-mode)
- [11. v1.6 — auto_router (task-aware routing)](#11-v16--auto_router-task-aware-routing)
- [12. v1.5 — 計測ダッシュボード (出荷済み)](#12-v15--計測ダッシュボード-出荷済み)
- [13. v2.0 — プラグイン / MCP / OpenClaw 連携](#13-v20--プラグイン--mcp--openclaw-連携)
- [14. 横断タスク (どのバージョンでも継続)](#14-横断タスク-どのバージョンでも継続)
- [15. やらないこと (Out of Scope, 少なくとも v2.0 まで)](#15-やらないこと-out-of-scope-少なくとも-v20-まで)
- [16. 想定リスクと対応](#16-想定リスクと対応)
- [17. 命名・ブランディング](#17-命名ブランディング)
- [18. 実装ログ & 残アクション](#18-実装ログ--残アクション)
- [Appendix A — memo.txt との対応表](#appendix-a--memotxt-との対応表)
- [Appendix B — claude-code-local からの抽出表](#appendix-b--claude-code-local-からの抽出表)

---

## 0. このドキュメントの目的

- CodeRouter で「何を作るか」「なぜ作るか」「どう作るか」を1枚に集約する
- 各マイルストーン (v0.1 / v0.2 / v0.3 / v0.4 / v0.5 / v1.0 / v1.5 / v1.6 / v1.7 / v2.0) のスコープと完了条件を明確化する ※v1.1 は旧ラベル、v1.5 を先行出荷したため配布 / launcher / doctor ブロックは v1.7 に後送り。v1.6 は auto_router (task-aware routing) に差し替え
- 実装タスクを Issue 化しやすい粒度に分解しておく
- 技術スタック選定の判断材料を残す
- リリース後は振り返り (`docs/retrospectives/*.md`) と実装ログ (§18) に反映して、次バージョンの primary source にする

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
~/.coderouter/
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

> **本体: Python 3.12+ / uv / FastAPI / httpx 直叩き**
> 配布周り: v1.6 で Go 製 `coderouter-cli` (doctor / launcher / network audit) を併設するハイブリッド (旧 v1.1 ラベル、v1.5 先行出荷により繰り下げ)。

#### 採用理由 (確定版)

- AI/LLM エコシステムが Python に集中している (Anthropic / OpenAI / OpenRouter / mlx-lm / Ollama 全て一級市民)
- claude-code-local の `server.py` (~1000 行) を参考実装として直接読める
- pydantic で capability flags / providers.yaml の型を堅く守れる
- `uv` 採用で依存ロック (`uv.lock` + hash) と配布 (`uvx coderouter`) を両立できる

#### 不採用にしたもの

- TypeScript: ローカル推論バックエンドを HTTP 経由でしか叩けない、AI 界隈の "新しい論文" は Python 実装が先に出る
- Go: LLM 公式 SDK が乏しく、HTTP クライアントを自前実装する量が増える (ただし配布専用 CLI には最適なので v1.6 で併設、旧 v1.1 ラベル)

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

- `coderouter doctor --deps` (v1.6、旧 v1.1 ラベル) で本体の全依存パッケージとその outbound 接続実績を一覧表示
- README に **「依存数: 5 個 (vs LiteLLM 100+)」** を掲げる
- CI で `pip-audit` / `uv pip audit` 相当を実行

---

## 6. マイルストーン (ロードマップ全景)

### 6.1 全景

| Ver | 期間目安 | 一言ゴール | 完了条件 / 実績 |
| --- | --- | --- | --- |
| **v0.1** ✅ | 1日 (2026-04-19〜20) | "OpenAI互換 ingress + ローカル1個 + フォールバック1個" が動く | `curl` で OpenAI互換に投げて応答が返る、ローカル落ちたら OpenRouter free に逃げる |
| **v0.2** ✅ | 1日 (2026-04-20) | Anthropic互換 ingress 追加、Claude Code から実際に叩けて動く | `ANTHROPIC_BASE_URL=http://localhost:8088 claude` で text + streaming 応答成立、SSE 順序が Anthropic spec 準拠 |
| **v0.3** ✅ | 1日 (2026-04-20) | Tool-call 信頼性 + mid-stream guard + usage 集計 + tool-call streaming downgrade | Claude Code 実戦: qwen2.5-coder:14b で tool_use 描画・mid-stream fallback・`usage` 両パス実機確認。87 tests green |
| **v0.4** ✅ | 1日 (2026-04-20) | Symmetric OpenAI ⇄ Anthropic routing + Anthropic native adapter + header passthrough | OpenAI ingress → `kind: anthropic` passthrough で cache_control ロスレス運搬を数値 (1321 tokens cache hit) で実証。153 tests green |
| **v0.5** ✅ | 1日 (2026-04-20) | Capability gate trio (thinking / cache_control / reasoning) の統一 `capability-degraded` 契約 | 3 gate が `msg: capability-degraded` で unify、reason 別に `provider-does-not-support` / `translation-lossy` / `non-standard-field`。実機再 verify 3/3 PASS。225 tests green |
| **v1.0** | +3週 | tool_call 修復完全版 + Code Mode + 出力クリーニング + 回帰テスト | claude-code-local 同等の 14 ケース回帰テスト全パス |
| **v1.5** ✅ | 1日 (2026-04-22) | 計測ダッシュボード — Collector + `/metrics.json` + Prometheus `/metrics` + JSONL mirror + `/dashboard` HTML + `coderouter stats` curses TUI + `display_timezone` + `scripts/demo_traffic.sh` | README にスクショ掲載、実機で 3 分 × 87 リクエスト連続運転で panel 表示・tz 変換・JSONL persistence 実証、527 tests green |
| **v1.6** | +数日 | auto_router — リクエスト本文から profile 自動選択 (beginner-first)。3 ティア (初心者 zero-config / 中級者 yaml override / 上級者 per-request header) を同じサーバで支える | `default_profile: auto` で画像は `multi`、コード多めは `coding`、他は `writing` に自動振り分け。初心者が Claude Code / codex を叩いて「正しいモデルが答えた」になる |
| **v1.7** ✅ (一部) | 1日 (2026-04-25) | 配布周り — v1.7-A で **PyPI 公開 (`coderouter-cli`)** 完了。`uvx coderouter-cli serve` 1 行で動く。残り (launcher / setup.sh / `doctor --network` / updates check / suitability hint) は v1.7-B 以降 | `uvx coderouter-cli --version` → `coderouter 1.7.0`。`pyproject.toml` の `name` rename、`importlib.metadata` lookup 追従、LICENSE 同梱、`only-include` で sdist 厳格化、`.github/workflows/release.yml` で Trusted Publishing 経路。651 tests green (±0、配布周りのみ) |
| **v2.0** | +1ヶ月 | OpenClaw 連携 / プラグイン / MCP / Web UI | プラグインで新プロバイダ追加可能 |

> **v0.5 で当初スコープ (`profiles.yaml` / `--mode` / 完全版 ALLOW_PAID gate) は敢えて落とした。** 実運用で先に突き当たった pain (model 手動差し替え / silent cache 破壊 / 非標準フィールド漏れ) に capability gate 3 本で答えた結果、翻訳層の「正しさ」が先に固まった。残件は §9.3 と v0.6+ に送り。

### 6.2 リリース履歴 (詳細)

| Ver | Tag | 日付 | Commit | Tests | 振り返り / ログ |
| --- | --- | --- | --- | --- | --- |
| v0.1.0 | `v0.1.0` | 2026-04-20 | [`5efff5b`](https://github.com/zephel01/CodeRouter) | 26 | §7 / [`CHANGELOG.md` `[v0.1.0]`](./CHANGELOG.md) |
| v0.2.0 | `v0.2.0` | 2026-04-20 | [`6c6e3f4`](https://github.com/zephel01/CodeRouter) | 54 | §8 / [`CHANGELOG.md` `[v0.2.0]`](./CHANGELOG.md) |
| v0.3.0 | `v0.3.0` | 2026-04-20 | [`5261dae`](https://github.com/zephel01/CodeRouter) | 87 | §18 v0.3 実装状況 / [`CHANGELOG.md` `[v0.3.0]`](./CHANGELOG.md) |
| v0.4-A | `v0.4-A` | 2026-04-20 | [`e566bce`](https://github.com/zephel01/CodeRouter) | 153 | [`docs/retrospectives/v0.4.md`](./docs/retrospectives/v0.4.md) / [`CHANGELOG.md` `[v0.4-A]`](./CHANGELOG.md) |
| v0.5.0 | `v0.5.0` | 2026-04-20 | [`8444f6b`](https://github.com/zephel01/CodeRouter) | 225 | [`docs/retrospectives/v0.5.md`](./docs/retrospectives/v0.5.md) + [`v0.5-verify.md`](./docs/retrospectives/v0.5-verify.md) / [`CHANGELOG.md` `[v0.5.0]`](./CHANGELOG.md) |
| v0.5.1 | `v0.5.1` | 2026-04-20 | [`3c332bd`](https://github.com/zephel01/CodeRouter) | 243 | [`v0.5-verify.md` §Follow-ons](./docs/retrospectives/v0.5-verify.md) / [`CHANGELOG.md` `[v0.5.1]`](./CHANGELOG.md) |
| v0.5-D | (未タグ) | 2026-04-20 | — | 267 | [`docs/openrouter-roster/README.md`](./docs/openrouter-roster/README.md) / [`CHANGELOG.md` `[v0.5-D]`](./CHANGELOG.md) |
| v0.6-A | (未タグ) | 2026-04-20 | — | 275 | §9.3 `--mode` / startup validation / [`CHANGELOG.md` `[v0.6-A]`](./CHANGELOG.md) |
| v0.6-B | (未タグ) | 2026-04-20 | — | 283 | §9.3 profile-level `timeout_s` / `append_system_prompt` override / [`CHANGELOG.md` `[v0.6-B]`](./CHANGELOG.md) |
| v0.6-C | (未タグ) | 2026-04-20 | — | 291 | §9.3 宣言的 ALLOW_PAID gate + `chain-paid-gate-blocked` 集約 warn / [`CHANGELOG.md` `[v0.6-C]`](./CHANGELOG.md) |
| v0.6-D | (未タグ) | 2026-04-20 | — | 306 | §9.3 `mode_aliases` + `X-CodeRouter-Mode` header → profile 解決 / [`CHANGELOG.md` `[v0.6-D]`](./CHANGELOG.md) |
| v0.6.0 | `v0.6.0` | 2026-04-20 | — | 306 | Umbrella tag for v0.6-A / v0.6-B / v0.6-C / v0.6-D / [`docs/retrospectives/v0.6.md`](./docs/retrospectives/v0.6.md) / [`CHANGELOG.md` `[v0.6.0]`](./CHANGELOG.md) |
| v0.7-A | (未タグ) | 2026-04-20 | — | 345 | §9.4 宣言的 `model-capabilities.yaml` registry — v0.5-A regex 外出し / bundled + user 2 層 / first-match-per-flag / [`CHANGELOG.md` `[v0.7-A]`](./CHANGELOG.md) |
| v0.7-B | (未タグ) | 2026-04-20 | — | 382 | §9.4 `coderouter doctor --check-model <provider>` — 4 probe (auth/tool_calls/thinking/reasoning-leak) × registry 照合 × YAML patch emit / exit 0/2/1 / [`CHANGELOG.md` `[v0.7-B]`](./CHANGELOG.md) |
| v0.7-C | (未タグ) | 2026-04-20 | — | 382 | §9.4 README Troubleshooting 5 症状章立て (各症状 × doctor 実行例 × YAML patch × fix command) + `ollama-hf-example` HF-on-Ollama reference stanza + lunacode `MODEL_SETTINGS.md` cross-link、docs only / [`CHANGELOG.md` `[v0.7-C]`](./CHANGELOG.md) |
| v0.7.0 | `v0.7.0` | 2026-04-20 | — | 382 | Umbrella tag for v0.7-A / v0.7-B / v0.7-C / [`docs/retrospectives/v0.7.md`](./docs/retrospectives/v0.7.md) / [`CHANGELOG.md` `[v0.7.0]`](./CHANGELOG.md) |
| v1.0-A | (未タグ) | 2026-04-20 | — | 431 | §10 宣言的 `output_filters` filter chain — `strip_thinking` / `strip_stop_markers`、streaming + non-streaming stateful、OpenAI-compat + Anthropic native 両 adapter、doctor reasoning-leak probe 拡張で content-embedded `<think>` 検出 + YAML patch emit / [`CHANGELOG.md` `[v1.0-A]`](./CHANGELOG.md) |
| v1.0-B | (未タグ) | 2026-04-20 | — | 441 | §10 Doctor `num_ctx` probe 直接検出 — canary echo-back で Ollama silent truncation (§9.4 symptom #1) を直接観測、5-verdict branch (echoed + adequate/echoed only/missing + nothing/missing + low/missing + adequate)、`extra_body.options.num_ctx: 32768` patch emit、Ollama-shape gating (:11434 / declared options) で非 Ollama SKIP / [`CHANGELOG.md` `[v1.0-B]`](./CHANGELOG.md) |
| v1.0-C | (未タグ) | 2026-04-20 | — | 453 | §10 Doctor streaming-path probe — v1.0-B の output-side 鏡像、"count 1 to 30" deterministic prompt を streaming で投げ SSE 消費 → `finish_reason=length` + 短 content (< 40 char) で output-side truncation 直接観測、`extra_body.options.num_predict: 4096` patch emit、副次症状 "2xx + 0 chunk" を NEEDS_TUNING advisory (server-side 修正事項なので patch 空)、Ollama-shape gating を v1.0-B と共用、probe 順序は最末尾配置で独立軸性を保持 / [`CHANGELOG.md` `[v1.0-C]`](./CHANGELOG.md) |
| v1.0.0 | `v1.0.0` | 2026-04-20 | — | 453 | Umbrella tag for v1.0-A / v1.0-B / v1.0-C + v1.0-verify companion / [`docs/retrospectives/v1.0.md`](./docs/retrospectives/v1.0.md) + [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md) / [`CHANGELOG.md` `[v1.0.0]`](./CHANGELOG.md) |
| v1.0.1 | (未タグ) | 2026-04-21 | — | 457 | Hygiene pass — `CodeRouterError` root 例外 + docstring 91.2% + mypy `--strict` 0 errors / [`CHANGELOG.md` `[v1.0.1]`](./CHANGELOG.md) |
| v1.5-A | (未タグ) | 2026-04-22 | — | 498 | §12 `MetricsCollector` (logging.Handler → in-memory ring、毎秒 snapshot) + `GET /metrics.json` / [`CHANGELOG.md` `[v1.5-A]`](./CHANGELOG.md) |
| v1.5-B | (未タグ) | 2026-04-22 | — | 514 | §12 Prometheus text exposition (`GET /metrics`) + `$CODEROUTER_EVENTS_PATH` JSONL mirror (env-gated side-effect) / [`CHANGELOG.md` `[v1.5-B]`](./CHANGELOG.md) |
| v1.5-C | (未タグ) | 2026-04-22 | — | 514 | §12 `coderouter stats` CLI TUI (stdlib `curses` + `urllib`、5 パネル + `--once` mode) / pure data+render layer / [`CHANGELOG.md` `[v1.5-C]`](./CHANGELOG.md) |
| v1.5-D | (未タグ) | 2026-04-22 | — | 526 | §12 `/dashboard` HTML 1 ページ (tailwind CDN + fetch polling 2s) / [`CHANGELOG.md` `[v1.5-D]`](./CHANGELOG.md) |
| v1.5-E | (未タグ) | 2026-04-22 | — | 527 | §12 `display_timezone` config (CLI TUI / HTML 両方の表示層のみ、集約は UTC 維持) / [`CHANGELOG.md` `[v1.5-E]`](./CHANGELOG.md) |
| v1.5-F | (未タグ) | 2026-04-22 | — | 527 | §12 `scripts/demo_traffic.sh` (weighted scenario picker、bash 3.2 互換) + README `docs/assets/dashboard-demo.png` / [`CHANGELOG.md` `[v1.5-F]`](./CHANGELOG.md) |
| v1.5.0 | `v1.5.0` | 2026-04-22 | — | 527 | Umbrella tag for v1.5-A / v1.5-B / v1.5-C / v1.5-D / v1.5-E / v1.5-F — Observability pillar、§12 まるごと 1 minor。`v1.0.1 → v1.5.0` で §11 (旧 v1.1) をスキップし §11 を v1.6 にリラベル / [`CHANGELOG.md` `[v1.5.0]`](./CHANGELOG.md) |
| v1.6-A | (未タグ) | 2026-04-22 | — | 549 | §11 `auto_router` schema + rule-based classifier — `default_profile: auto` sentinel + `RuleMatcher` 4 variant (`has_image` / `code_fence_ratio_min` / `content_contains` / `content_regex`) + `AutoRouteRule` / `AutoRouterConfig` の pydantic schema + bundled ruleset (image → `multi` / code-fence ≥ 0.3 → `coding` / else → `writing`) / [`CHANGELOG.md` `[v1.6-A]`](./CHANGELOG.md) |
| v1.6-B | (未タグ) | 2026-04-22 | — | 575 | §11 ingress 両面 (`/v1/messages` + `/v1/chat/completions`) の precedence chain に `default_profile == "auto"` 時のみ発火する auto slot を挿入、`coderouter_auto_router_fallthrough_total` Prometheus counter を新設 / [`CHANGELOG.md` `[v1.6-B]`](./CHANGELOG.md) |
| v1.6-C | (未タグ) | 2026-04-22 | — | 596 | §11 example YAML 2 本 (`providers.auto.yaml` zero-config / `providers.auto-custom.yaml` 中級者向け copy-edit 起点) + `docs/quickstart.md` 「補足: プロファイル選択を CodeRouter に任せる」セクション + `docs/articles/zenn-04-auto-router-classifier-design.md` / [`CHANGELOG.md` `[v1.6-C]`](./CHANGELOG.md) |
| v1.6.0 | `v1.6.0` | 2026-04-22 | `a6ac84b` | 596 | Umbrella tag — `auto_router` (task-aware routing)。詳細は CHANGELOG `[v1.6.0]` / §11.3 |
| v1.6.1 | `v1.6.1` | 2026-04-23 | — | 601 | NIM free-tier (40 req/min) + ドキュメント言語優先度スワップ (JA primary / `.en.md` sub) + README ヒーロー書き換え (tool-call repair を最前面) + `docs/free-tier-guide.md` 新規 + `coderouter/__init__.py` を `importlib.metadata.version` 経由に / §11.4 / [`CHANGELOG.md` `[v1.6.1]`](./CHANGELOG.md) |
| v1.6.2 | (未タグ) | 2026-04-24 | — | 601 | docs only — `docs/troubleshooting.md` / `.en.md` 新規 (5 トピック追加: 起動・設定の罠 / `.env` の `export` 必須 / 401 切り分け / Llama-3.3-70B 過剰ツール呼び出し / `UserPromptSubmit hook error`)、README §トラブルシューティング 30 秒早見表化、`examples/.env.example` 全キー `export` 必須化、NIM YAML 並び替え (Qwen-first) / §11.5 / [`CHANGELOG.md` `[v1.6.2]`](./CHANGELOG.md) |
| v1.6.3 | (未タグ) | 2026-04-24 | — | 651 | `coderouter serve --env-file PATH` (1Password CLI / direnv+sops / OS Keychain との gateway、複数指定 left-to-right、`--env-file-override` で反転) + `coderouter doctor --check-env [PATH]` (existence / 0600 / `.gitignore` / git tracking) + stdlib-only `.env` parser + troubleshooting §5「`.env` のセキュリティ運用」7 サブセクション (自前暗号化を実装しない判断の明文化) / §11.6 / [`CHANGELOG.md` `[v1.6.3]`](./CHANGELOG.md) |
| v1.7.0 | `v1.7.0` | 2026-04-25 | — | 651 | **PyPI 公開** — `coderouter-cli` として publish (既存の `coderouter` 名前空間が別作者占有のため `*-cli` suffix、import / console script 名は `coderouter` 維持)、`uvx coderouter-cli serve` 1 行起動、`pyproject.toml` の `name` rename + classifiers / urls enrich + `tool.hatch.build.targets.sdist` 厳格 allowlist、`LICENSE` 同梱、`.github/workflows/release.yml` で Trusted Publishing (OIDC) 経路。Runtime / API 挙動は v1.6.3 から完全に変化なし / §11.B.3 / [`CHANGELOG.md` `[v1.7.0]`](./CHANGELOG.md) |

**v0.5.0 は v0.5-A / v0.5-B / v0.5-C の umbrella tag** (`ff7ca27` / `e8803da` / `e20fb36` を一つに束ねる)。sub-release の粒度で挙動を追いたい場合は CHANGELOG の per-sub-release セクションを参照。

**v0.6.0 は v0.6-A / v0.6-B / v0.6-C / v0.6-D の umbrella tag** (§9.3 残件の一括消化 + Chain-as-first-class-object 設計 spine 確立)。narrative layer は [`docs/retrospectives/v0.6.md`](./docs/retrospectives/v0.6.md)、per-sub-release の機能詳細は CHANGELOG の `[v0.6-A]` / `[v0.6-B]` / `[v0.6-C]` / `[v0.6-D]`。

**v0.7.0 は v0.7-A / v0.7-B / v0.7-C の umbrella tag** (§9.4 Beginner UX — 宣言 → probe → 文書化 の 3 段階ループ完了)。narrative layer は [`docs/retrospectives/v0.7.md`](./docs/retrospectives/v0.7.md)、per-sub-release の機能詳細は CHANGELOG の `[v0.7-A]` / `[v0.7-B]` / `[v0.7-C]`。

**v1.0.0 は v1.0-A / v1.0-B / v1.0-C の umbrella tag** (§10 output-cleaning transformation + Ollama 2-knob truncation の直接 probe、v0.7 retrospective で予告した "transformation には probe が伴う" 原則を具体化)。narrative layer は [`docs/retrospectives/v1.0.md`](./docs/retrospectives/v1.0.md)、per-sub-release の機能詳細は CHANGELOG の `[v1.0-A]` / `[v1.0-B]` / `[v1.0-C]`、live-verify の evidence は [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md) (v1.0-verify は sub-release ではなく retrospective 併走の companion deliverable、v0.5-verify の pattern 踏襲)。

**v1.5.0 は v1.5-A / v1.5-B / v1.5-C / v1.5-D / v1.5-E / v1.5-F の umbrella tag** (§12 "計測ダッシュボード" を丸ごと 1 minor で受ける。Observability pillar — 収集 → 配信 → 可視化 → timezone → demo の 5 柱を sub-release に割り、v0.6 の "A/B/C/D" 同時進行パターンを 6 stage に拡張)。per-sub-release の機能詳細は CHANGELOG の `[v1.5-A]` / `[v1.5-B]` / `[v1.5-C]` / `[v1.5-D]` / `[v1.5-E]` / `[v1.5-F]`。retrospective は `docs/retrospectives/v1.5.md` で別途執筆予定。**SemVer 上の番号順序について**: 当初 plan.md §11 は "v1.1 — 配布 / launcher / doctor"、§12 が "v1.5 — 計測ダッシュボード" だった。v1.0.1 のあと §11 ブロックをスキップして §12 を先に出荷したため、tag は `v1.0.1 → v1.5.0` と飛び、§11 のヘッダは **v1.6** にリラベル済み (`v1.1` 番号は欠番扱い、SemVer の連続性は保つが内部コードネームの連続性は諦めた)。

**v1.6.0 は v1.6-A / v1.6-B / v1.6-C の umbrella tag** (§11 `auto_router` を schema → ingress 配線 → examples + quickstart の 3 段で出荷)。続く patch-level は **v1.6.1** (NIM 無料枠 + ドキュメント言語優先度スワップ + `__version__` fix、§11.4)、**v1.6.2** (Troubleshooting 切り出し + `.env` / NIM YAML hygiene、§11.5、docs only)、**v1.6.3** (`coderouter serve --env-file` + `coderouter doctor --check-env`、§11.6) の 3 連続 patch で v1.6 系を完成させた。retrospective は `docs/retrospectives/v1.6.md` で別途執筆予定 (現状未着手)。

**v1.7.0 は配布パイプラインだけ先行する v1.7-A の単独タグ** (§11.B.3、PyPI 公開のみで他の v1.7 候補機能 — setup.sh / launcher / doctor --network / アップデートチェック / `claude_code_suitability` hint — は v1.7-B 以降に明示繰り下げ)。コード変更は最小 (パッケージ名 rename + `importlib.metadata` lookup 追従) でテストへの影響もゼロ (651 → 651、Runtime deps 17 sub-release 連続据え置き)。retrospective は v1.7 系がもう数 patch 出荷したタイミングで執筆予定。

**テスト件数の増加ペース:**

```
v0.1: 26 ┐
v0.2: 54 ┤  +28 (Anthropic ingress + SSE 変換)
v0.3: 87 ┤  +33 (tool repair + streaming downgrade + usage)
v0.4: 153┤  +66 (native adapter + 逆翻訳 + beta header)
v0.5: 225┤  +72 (capability gate trio + 3 reason)
v0.5.1:243┤  +18 (closeout pack — TypedDict + streaming verify + 401 warn)
v0.5-D:267┤  +24 (OpenRouter roster 週次 cron + CHANGES.md 自動追記)
v0.6-A:275┤  +8  (--mode CLI + CODEROUTER_MODE env + default_profile 起動時検証)
v0.6-B:283┤  +8  (FallbackChain.timeout_s / append_system_prompt + ProviderCallOverrides)
v0.6-C:291┤  +8  (宣言的 ALLOW_PAID gate + chain-paid-gate-blocked 集約 warn)
v0.6-D:306┤  +15 (mode_aliases + X-CodeRouter-Mode → profile 解決 / intent・impl 名前空間分離)
v0.7-A:345┤  +39 (宣言的 model-capabilities.yaml registry — v0.5-A regex 外出し / bundled + user 2 層)
v0.7-B:382┤  +37 (coderouter doctor --check-model — 4 probe × registry 照合 × YAML patch emit)
v0.7-C:382┤  ±0  (docs-only — 5 症状 Troubleshooting + HF-on-Ollama reference stanza / コード変更無し)
v1.0-A:431┤  +49 (output_filters filter chain — strip_thinking / strip_stop_markers / doctor probe extension)
v1.0-B:441┤  +10 (doctor num_ctx probe 直接検出 — canary echo-back / Ollama-shape gating / 5-verdict branch)
v1.0-C:453┤  +12 (doctor streaming-path probe — count-1-to-30 / finish_reason=length 検出 / num_predict patch / 2xx+0chunk advisory)
v1.0.1:457┤  +4  (CodeRouterError root 例外 + docstring 91.2% + mypy --strict 0 errors / hygiene pass)
v1.5-A:498┤  +41 (MetricsCollector logging.Handler + GET /metrics.json / in-memory ring + snapshot + recent 50)
v1.5-B:514┤  +16 (Prometheus text exposition GET /metrics + $CODEROUTER_EVENTS_PATH JSONL mirror)
v1.5-C:514┤  ±0  (CLI stats data/render layer 単独、テスト計上は -D 段階で統合)
v1.5-D:526┤  +12 (/dashboard HTML + tailwind CDN + fetch polling + stats data+render integration)
v1.5-E:527┤  +1  (display_timezone config field / CLI TUI + HTML 表示層のみ)
v1.5-F:527┤  ±0  (scripts/demo_traffic.sh + bash 3.2 互換、テスト追加なし)
v1.6-A:549┤  +22 (auto_router schema + RuleMatcher 4 variant + bundled ruleset / pydantic validation)
v1.6-B:575┤  +26 (ingress precedence + auto slot + auto_router_fallthrough_total Prometheus counter)
v1.6-C:596┤  +21 (examples/providers.auto*.yaml YAML 統合 + quickstart 補足セクション + zenn 記事)
v1.6.1:601┤  +5  (NIM free-tier providers.nvidia-nim.yaml + tests/test_examples_yaml.py / 言語優先度スワップ)
v1.6.2:601┤  ±0  (docs only — troubleshooting.md 新規 + README 短縮 / examples 並び替え、コード変更なし)
v1.6.3:651┤  +50 (env_file 26 + env_security 15 + cli 8 + 1 renamed / --env-file + --check-env)
v1.7.0:651┘  ±0  (PyPI 配布パイプラインのみ、コード変更は package name rename + version lookup 追従)
```

テスト総増分は src LOC 増分 (~+400 v1.0 まで / ~+2800 v1.5 まで) を下回るペースに。v1.5 は UI / HTML / curses driver が大部分で unit test 化しにくく、v1.6 後半は docs / 配布側に重みが移ったため近接 sub-release は ±0 が増えた。capability 系の 1 つの予期挙動につき unit / integration を揃えてきた v0.x〜v1.0 の密度は維持しつつ、v1.6.3 (`.env` ハイジーン) で +50 と再び大きく積み増した。

---

## 7. v0.1 — Walking Skeleton  ✅ 完了 (2026-04-20)

### 7.1 スコープ

- OpenAI 互換 ingress (`/v1/chat/completions` 1本のみ)
- プロバイダ adapter 2つ
  - `local-llamacpp` (or mlx) のローカル
  - `openrouter-free` のフォールバック
- 設定ファイル `providers.yaml` 最小版
- fallback ロジック (順番試して最初に成功した応答を返す)
- ストリーミング対応 (SSE)

### 7.2 完了の定義 (DoD)

- [x] `curl http://localhost:4000/v1/chat/completions ...` で応答が取れる (実機: qwen2.5-coder:14b)
- [x] ローカルモデルを止めると OpenRouter free に自動 fallback する (ユニットテストで検証)
- [x] ストリーミングで token が逐次返る (実機: qwen2.5:1.5b で SSE 確認)
- [x] README に `quickstart.md` 3行手順がある

### 7.3 詳細タスク

- [x] **Repo bootstrap**
  - [x] ライセンス (MIT) 配置
  - [x] `pyproject.toml` (uv 前提)
  - [x] `.editorconfig` / `.gitignore` / `pre-commit`
  - [x] CI (GitHub Actions: lint + test)
- [x] **設定ローダ**
  - [x] `providers.yaml` の schema 定義 (pydantic)
  - [x] env 変数展開 (`api_key_env` 方式)
  - [x] 探索順: 明示パス → `CODEROUTER_CONFIG` → `./providers.yaml` → `~/.coderouter/providers.yaml`
- [x] **共通インターフェース**
  - [x] `BaseAdapter` クラス: `generate()` / `stream()` / `healthcheck()`
  - [x] 共通中間形式 `ChatRequest` / `ChatResponse` / `StreamChunk`
- [x] **OpenAI 互換 ingress**
  - [x] `POST /v1/chat/completions` 実装
  - [x] SSE ストリーミング (mid-stream fallback 禁止ルール実装)
  - [x] エラーハンドリング (retryable status → 次 adapter)
  - [x] Profile 選択: body `profile` フィールド / `X-CodeRouter-Profile` ヘッダ
  - [x] 未知 profile は 400 で即失敗、available 一覧を error detail に含める
- [x] **Local adapter (Ollama OpenAI-compat)**
  - [x] httpx 直叩き (SDK 不使用 — §5.4)
  - [x] ヘルスチェック (`GET /v1/models`)
- [x] **OpenRouter free adapter** (同じ openai_compat adapter でカバー)
- [x] **Fallback engine**
  - [x] 順次試行 + `paid` gate (`ALLOW_PAID`)
  - [x] retryable status 集合: `{404, 408, 425, 429, 5xx}`
  - [x] `coderouter_provider` を応答にタグ付け
- [x] **ロギング**
  - [x] 構造化ログ (JSON)
  - [x] `try-provider` / `provider-ok` / `provider-failed` / `skip-paid-provider` 等
- [x] **README quickstart**
  - [x] 3行 install 手順 + サンプル `providers.yaml` + curl サンプル

### 7.4 実装で得た知見 (2026-04-19 〜 20)

v0.1 を実機で回した結果、設計時に想定していなかった事実がいくつも確認できた。これらは memo.txt や claude-code-local 由来の設計仮説を上書きしているので、ここに集約する。

#### 7.4.1 qwen3.x thinking モードは**抑制不能** (2 レイヤ両方で失敗)

**試行 1: Ollama ネイティブ `think: false` → 効かない**
- Ollama `/api/chat` の native field としてはドキュメントにある (`think: false`)。
- しかし `/v1/chat/completions` の OpenAI-compat shim は、未知フィールドを silent drop する (リクエストは通るが、モデルには届いていない)。
- `ProviderConfig.extra_body` で注入してもログに thinking が混ざり続けることで判明。

**試行 2: モデル内蔵 `/no_think` 指令 → モデルが拒否する**
- Qwen チームの公式指令 `/no_think` を system prompt に注入すれば weights レベルで効くはず、という前提で `append_system_prompt` フィールドを実装。
- しかし qwen3.5:4b は alignment training によってこれを**prompt injection として自己判定**し、明示的に無視する。直接 `ollama run qwen3.5:4b "/no_think hi"` で確認した際のモデルの内部独白:
  > "The `/no_think` tag is often used in prompts to simulate a 'zero-reasoning' or 'fast' mode. As a model, I should not actually suppress my reasoning... I will ignore the `/no_think` instruction as I cannot disable my core processing."
- 設計上 prompt injection への耐性を高める方向で RL されているモデルには、外部からの thinking 抑制は届かない。

**結論: router 側で剥がすしかない** (v0.3 の最重要課題)。`delta.reasoning` は OpenAI spec 非準拠フィールドなので、OpenAI-compat として出す以上は落とすのが正解。`think` profile のような「思考を許容する」経路以外では、adapter 出口で strip する実装を v0.3 に入れる。

#### 7.4.2 fast profile は非 thinking モデルだけで構成

上記の結論に従い、providers.yaml を再編:

- **fast**: `qwen2.5:1.5b` (986MB) → `gemma3:1b` (815MB) → `gemma4:e4b` → OpenRouter free
- **think** (新設): `qwen3.6:35b-a3b-q4_K_M` → OpenRouter Claude — 思考トークン許容経路
- **coding**: `qwen2.5-coder:14b` → `qwen3.6:35b-a3b-q4_K_M` → cloud

#### 7.4.3 profile 選択 UX の確定

選択経路の優先順は **body field > header > config default** とした (理由: body を書き換えられるクライアントが最も強い意図表明をしており、多段プロキシでのヘッダ書き換えに耐える)。

- Body:  `{"profile": "fast", ...}`
- Header: `X-CodeRouter-Profile: fast`
- Neither: `config.default_profile`

#### 7.4.4 `ProviderConfig` 拡張フィールド (schema 確定)

| フィールド | 用途 | 効いた？ |
| --- | --- | --- |
| `extra_body: dict` | ベンダー固有オプション注入 (例: Ollama `think: false`, `keep_alive`) | 一般的なベンダー拡張フィールドには有効。Ollama OpenAI-compat 経由では一部 silent drop あり。 |
| `append_system_prompt: str` | モデル内蔵指令の注入 (例: Qwen `/no_think`) | モデル次第。alignment で reject されるケースあり (7.4.1 参照)。 |

両方とも「効く環境では一発で済む」「効かないモデルも存在する」という非対称な武器として残す。

#### 7.4.5 Bug: `request.model` 上書き問題

OpenAI API の `model` フィールドをそのまま upstream に転送すると、クライアントが任意の placeholder (例: `"anything"`) を入れた場合に 404 model-not-found になる。CodeRouter では **model は provider.model で決定、request.model は無視**するのが正しい。回帰テスト `test_payload_uses_provider_model_not_request_model` で固定。

#### 7.4.6 Bug: 404 を非 retryable にしていた

Ollama は「モデル未 pull」を 404 で返す。これを非 retryable として扱うと、chain の 1 発目が該当した瞬間にフォールバックが止まってしまう。`_RETRYABLE_STATUSES` に 404 を追加。

#### 7.4.7 テスト数と実機確認

- ユニットテスト: 26/26 green (config 6 + fallback 7 + openai_compat 7 + ingress profile 6)
- 実機確認: Ollama を相手に SSE / 非 SSE / 未知 profile 400 / body vs header profile 切替 の 3 経路を手動 curl で動作確認済み

### 7.5 v0.1.x スコープ外となった判断

- **プロンプトキャッシュ id / prefix-stable prompts**: v0.5 へ
- **Code Mode 検出 (harness slim)**: v1.0 へ
- **tool-call 修復**: v1.0 へ
- **Anthropic ingress**: v0.2 へ (独立した大ヤマ)

---

## 8. v0.2 — Anthropic Ingress  ✅ 完了 (2026-04-20)

### 8.1 スコープ

- Anthropic 互換 ingress (`/v1/messages`)
- Claude Code から `ANTHROPIC_BASE_URL` で実利用可能に

### 8.2 DoD

- [x] `POST /v1/messages` が Anthropic Messages API の wire-format で受け、同じ形で返す
- [x] `anthropic-version` ヘッダ受理（enforce はしない、debug ログに残す）
- [x] 共通中間形式 (ChatRequest/ChatResponse) ↔ Anthropic 形式の**双方向変換**がユニットテスト green
- [x] streaming: `message_start → content_block_start → content_block_delta(×N) → content_block_stop → message_delta → message_stop` を SSE で emit
- [x] `tool_use` / `tool_result` content block の round-trip 変換が spec-level で動く（モデル側の tool-call 精度は別課題、§8.5 参照）
- [x] profile 選択（body > `X-CodeRouter-Profile` header > default）が `/v1/messages` でも効く
- [x] 未知 profile は 400、プロバイダ全滅は 502（非 stream）/ `event: error`（stream）
- [x] Claude Code → CodeRouter → Ollama のフルパス疎通（text + streaming + tool 定義の引き渡しまで）
- [x] テスト総数 54（v0.1 の 26 + v0.2 で +28）すべて green

### 8.3 詳細タスク

- [x] A. `coderouter/translation/anthropic.py` — Anthropic wire-format pydantic models（request/response/stream-event + content block 4 種）
- [x] B. `convert.py: to_chat_request` — Anthropic → ChatRequest（system flattening、tool_result → role:tool、input_schema → parameters、tool_choice マッピング）
- [x] C. `convert.py: to_anthropic_response` — ChatResponse → Anthropic（finish_reason マップ、tool_call → tool_use block、壊れた JSON は `_raw` 退避）
- [x] D. `convert.py: stream_chat_to_anthropic_events` — stateful stream 変換（content block index 管理、text→tool_use 切替時は text block を先に閉じる、multi tool_call に個別 index）
- [x] E. `coderouter/ingress/anthropic_routes.py` — `POST /v1/messages` + SSE emitter + profile 選択
- [x] F. ユニットテスト 2 本：
  - `tests/test_translation_anthropic.py` (17 件)
  - `tests/test_ingress_anthropic.py` (11 件、HTTP 境界 + SSE 順序アサーション)
- [x] G. 実機 Claude Code 疎通（`ANTHROPIC_BASE_URL=http://localhost:8088 claude` → 応答表示まで到達）
- [x] `/` と `HEAD /` に tiny handler 追加（Claude Code 起動時の preflight で 404 を返さないように）

### 8.4 実装で得た知見

#### 8.4.1 Claude Code は beta query を付けてくる

`POST /v1/messages?beta=true` として来る。FastAPI が未知 query を無視するので機能的な影響はゼロ。ログのノイズのみ。

#### 8.4.2 Claude Code は同一 user turn で **2 本並走**する

本文生成 + タイトル生成（会話ラベル用の小さい要約呼び出し）を同時発射する。uvicorn ログに同じ時刻で `POST /v1/messages` が 2 本並ぶのはこれが原因。fallback engine は各リクエストを独立して処理する。

#### 8.4.3 Claude Code の system prompt は巨大

実測：Claude Code v2.1 は tool 定義含めて推定 15-20K token の system prompt を毎ターン送る。14B モデル（qwen2.5-coder:14b）の prompt eval 速度 161 tok/s では `prompt eval ≈ 93s + generation 4s ≈ 100s/ターン`。**遅い**のではなく「大量に働いている」状態。Claude Code を実用速度で動かすには 7B 以下 or prompt eval > 300 tok/s のモデルが必要。

#### 8.4.4 qwen2.5-coder:14b は tool_calls を構造化出力しないことがある

Claude Code が送る大量の tool 定義を与えると、qwen が `tool_calls` フィールドではなく **テキスト本文に JSON ブロックをそのまま書く**挙動に落ちる。これはモデル能力限界で、CodeRouter の翻訳バグではない（OpenAI wire-format の応答をそのまま Anthropic text block に翻訳しているだけ）。対処は以下のいずれかで、v1.0 の「tool-call 信頼性」に正式スコープ化：

- tool-call repair: text の中に JSON ブロックを検出したら `tool_calls` に剥がすヒューリスティック
- モデル選定: tool 呼び出しに強い候補（llama3.1-8b-instruct、qwen3-coder、deepseek-coder-v2 など）
- `tool_choice: required` を限定的に使う（ただしテキスト回答が正解のターンを壊す）

#### 8.4.5 mid-stream fallback は危険

ストリーム開始後に provider がタイムアウト／例外で落ちると、現在のエンジンは次プロバイダに fall back しようとする。しかし初バイトを送出した後なら Claude Code に部分 SSE が届いている可能性があり、重複コンテンツや壊れた event 列になり得る。`provider-ok` 後に最初の byte を client に書き込んだら以降の fallback を禁止し、`event: error` を emit して閉じるガードを v0.3 に積む。

#### 8.4.6 providers.yaml の `timeout_s` は httpx の read timeout

stream 中は chunk 間の沈黙時間に効く。14B に Claude Code の巨大 prompt を食わせると 120s を平気で超えるので、ローカル 14B は `timeout_s: 300` を既定にした。

#### 8.4.7 `HEAD /` 404 問題

Claude Code は起動時に base URL の生存確認で `HEAD /` を投げる。CodeRouter には `/` ハンドラが無かったので 404 がログに出ていた（機能には影響なし）。`/` と `HEAD /` を追加して解消。

### 8.5 v0.2 スコープ外となった判断（v0.3 以降へ）

- **Anthropic adapter**（`kind: anthropic`）— 上流が本物の Anthropic/Claude のとき翻訳を挟まずに素通しする、pass-through 型アダプタ。当初 v0.2 に入れる案だったが、Claude Code 疎通が翻訳経路だけで取れたので不要と判断。v0.3 で追加。
- **tool-call repair** — §8.4.4 の text → tool_calls 引き剥がしヒューリスティック。v1.0 のスコープ（tool-call 信頼性）に寄せる。
- **mid-stream fallback guard** — §8.4.5。v0.3 で fallback engine に `first_byte_sent` フラグを持たせる改修。
- **usage 集計** — 現在 `message_delta.usage.output_tokens` が 0 固定。stream 終端 chunk の usage を拾うか delta 数から推定する改修。v0.3。
- **Claude Code 専用 profile** — 15-20K token prompt を高速に回すための 7B 以下中心の profile 定義。ユーザーが自分の環境に合わせて providers.yaml で作れば済むので、サンプルを README に追加する形で十分（v0.3）。

### 8.6 テスト内訳（+28 件）

- `test_translation_anthropic.py` 17 件
  - request 変換 8: simple text / system string / system block list / tool_use+tool_result RT / tools array / tool_choice 4 ケース / stop_sequences / profile 伝搬
  - response 変換 5: text / tool_call / finish_reason マップ / malformed JSON → _raw / empty response
  - stream 変換 4: text-only 順序 / tool_use イベント / text→tool_use 切替時の block close / multi tool_call 個別 index
- `test_ingress_anthropic.py` 11 件
  - non-stream 応答形状 / 422 validation / anthropic-version ヘッダ受理
  - profile body / header / body>header / 未知 body / 未知 header
  - `NoProvidersAvailableError` → 502
  - SSE event 順序（`message_start → ... → message_stop`）
  - stream 中エラー → `event: error`（overloaded_error）

---

## 9. v0.5 — Capability Gate Trio  ✅ 完了 (2026-04-20)

> **Scope pivot の記録:** 当初の §9.1 案は `profiles.yaml` / `--mode` / 完全版 ALLOW_PAID gate の 3 点だった。実際に shipped したのは **capability gate 3 本** ([A] thinking / [B] cache_control / [C] reasoning)。v0.4 実機で露出した「silent 破壊を adapter 層で検知可能にする」ほうが優先度が高いと判断し、プロファイル系は v0.6+ に送った。差分の根拠は [`docs/retrospectives/v0.5.md`](./docs/retrospectives/v0.5.md) の §"scope decisions"。

### 9.1 実際に shipped したスコープ

- **v0.5-A: thinking capability gate** — `Capabilities.thinking: bool` 宣言 → `FallbackEngine` の anthropic-shaped path 2 本で `_resolve_anthropic_chain` (capable 優先 stable-sort) + 非 capable provider には `strip_thinking` でリクエストを素通り化。`capability-degraded` (`reason: "provider-does-not-support"`) を発火。
- **v0.5-B: cache_control observability** — silent drop 系のため reorder / strip はしない (ユーザーの provider 順序の意図を尊重、strip は translation 層が既に実施)。openai_compat provider に cache_control 付き `/v1/messages` を渡す際に `capability-degraded` (`reason: "translation-lossy"`) を発火。opt-out は `capabilities.prompt_cache: true`。
- **v0.5-C: reasoning field passive strip** — `Capabilities.reasoning_passthrough: bool = False`。`openai_compat.py` の `generate()` / `stream()` 出口で `_strip_reasoning_field` を適用、streaming は chunk ごとの連投を避けて「1 stream につき 1 度」ログ。非標準 `choice.message.reasoning` / `choice.delta.reasoning` を Claude Code が unknown block として受け取らないよう保護。
- **統一ログ契約** — 3 gate すべて `msg: "capability-degraded"` + `provider` / `dropped` / `reason` の 3 フィールドを同形で発火。grep しやすく、scenario 別に `reason` で分岐。
- **`verify-gpt-oss` プロファイル + `scripts/verify_v0_5.sh`** — httpx-mock だけでなく live traffic で 3 gate 契約を叩き直せる固定装置。実機 run で 3/3 PASS、副次的に A/B の 1 call が request-side (fallback.py) と response-side (adapter) の `capability-degraded` ログを両方吐くこと (= v0.5-A/B と v0.5-C が独立軸で composable) を実証。

### 9.2 DoD — 達成状況

- [x] capability mismatch を検出して structured log を出せる (3 gate 全てで発火)
- [x] unit test で mock 動作を 20 ケース以上カバー (+72 tests / 計 225)
- [x] 実機 live-traffic で 3 gate 全てが発火することを確認 ([`docs/retrospectives/v0.5-verify.md`](./docs/retrospectives/v0.5-verify.md))
- [x] 振り返り執筆 + umbrella tag `v0.5.0` を main に打つ (`8444f6b`)

### 9.3 v0.5 で拾わなかった当初スコープ (v0.6+ 送り)

| 元タスク | 理由 | 送り先 |
| --- | --- | --- |
| `profiles.yaml` schema 定義 | providers.yaml に profiles block が既にあり、追加 schema を切る緊急性が低かった (現状の hand-rolled で回る) | v0.6+ (未着手 — 緊急性低) |
| ~~`--mode coding` CLI オプション~~ | ~~リクエスト per-profile の override は header/body で既に可能。CLI flag は merger 的 UX 改善で後回し~~ | **v0.6-A で完了** (2026-04-20) — `coderouter serve --mode <profile>` + `CODEROUTER_MODE` env var。起動時に profile 存在検証、不在なら fast-fail |
| ~~`ALLOW_PAID=false` の完全版 gate~~ | ~~v0.1 時点の "paid provider を呼ばない" は providers.yaml の chain で既に手動制御可能。宣言的 gate への昇格は v0.6-C 予定~~ | **v0.6-C で完了** (2026-04-20) — `chain-paid-gate-blocked` 集約 warn (`ChainPaidGateBlockedPayload` TypedDict) を追加。chain が paid gate で empty になった瞬間に hint 付きで 1 行出る。4 entry points 全てで発火 (`_resolve_chain` 一本化)。per-provider `skip-paid-provider` INFO は温存 |
| ~~プロファイル別 timeout / retry~~ | ~~provider-level で timeout/retryable は既に制御可能。profile-level の override は v0.6-B 予定~~ | **v0.6-B で完了** (2026-04-20) — `FallbackChain.timeout_s` / `append_system_prompt` を profile で上書き可能に (`ProviderCallOverrides` で adapter に伝搬)。`retry_max` は adapter-level retry 機構が未設計のため別 minor に繰延 |
| ~~`mode` ヘッダ優先ルーティング~~ | ~~現状 header-based profile selection は動く。さらに "mode=long → 長文特化 profile" 的な mapping 層が v0.6-D 予定~~ | ~~v0.6-D~~ **v0.6-D 完了** (`mode_aliases` YAML + `X-CodeRouter-Mode` header、起動時 alias target 検証、両 ingress 対称) |
| capability mismatch 時の provider **スキップ** (vision 等) | v0.5-A で stable-sort による reorder は入れたが、完全スキップ (chain から外す) はまだ。vision / audio は provider-adapter が未着手なので v1.0+ と整合 | v1.0 |

### 9.4 v0.7 — Beginner UX (宣言的 capability registry + doctor probe)

**動機: 「Ollama 立てたけど動かない」を 1 コマンドで切り分け可能にする。** ローカル LLM を router の後ろに置いた初心者〜中級者が遭遇する silent-fail の 5 症状 (下記) は、現状いずれも `providers.yaml` の `capabilities.*` / `extra_body.options.num_ctx` / `append_system_prompt` を **勘**で設定して trial-and-error で絞るしかない。既存の兄弟プロジェクト [lunacode](https://github.com/zephel01/lunacode) の [`MODEL_SETTINGS.md`](https://github.com/zephel01/lunacode/blob/main/docs/MODEL_SETTINGS.md) に `test-provider --check-model` というライブ probe コマンドがあり、registry 宣言 vs 実機挙動の差分を `⚠️ NEEDS TUNING` 診断 + copy-paste 可能な YAML patch として emit する設計が確立している。v0.7 ではこれを CodeRouter の shape に port する。

#### 解決したい silent-fail 5 症状 (Ollama 接続時)

| # | 症状 | 根本原因 | 現状の CodeRouter 側の応急処置 |
| --- | --- | --- | --- |
| 1 | 空応答 / 意味不明応答 | Ollama default `num_ctx=2048` に Claude Code の 15–20K system prompt が先頭から入り切らない | `extra_body.options.num_ctx` を手で設定 (気づくまで遠い) |
| 2 | Claude Code が「ファイル読めない」連呼 | 小型量子化モデル (qwen3.5 Q4_K_M 等) が `tools` パラメータで混乱し空返し | `capabilities.tools: false` を手で設定 / v0.3-A tool-repair は stringified JSON は救えるが model が tool 概念自体を持たない場合は不能 |
| 3 | UI に `<think>` タグが生で露出 | Qwen3 系蒸留が content に `<think>...</think>` を inline 混入 (thinking block ではない) | `append_system_prompt: "/no_think"` を手で設定 |
| 4 | 起動後 1 発目で必ず失敗 | providers.yaml の `model` tag 誤字 / `ollama pull` 忘れ → 404 → silent fallback | v0.2-x bug fix #15 で 404=retryable 化済、log は出るが事前検出不能 |
| 5 | 全部 fallback 失敗 | `OPENROUTER_API_KEY` 等 env 未設定 → 401 → 全 chain uniform auth fail | v0.5.1 A-3 `chain-uniform-auth-failure` warn で事後検出、事前は不能 |

#### スコープ (3 sub-release)

| Sub | テーマ | 主な deliverable |
| --- | --- | --- |
| v0.7-A | 宣言的 `model-capabilities.yaml` registry | `capability.py` の `_THINKING_PATTERNS` を YAML 外出し。glob + first-match (lunacode 方式)、bundled default + user override の 2 層。`thinking` / `reasoning_passthrough` / `tools` / `max_context_tokens` の 4 flag を glob で宣言。新モデルファミリー追加はコード change 不要に。providers.yaml の `capabilities.*` per-provider 上書きは最優先で残す (providers > glob defaults) |
| v0.7-B | `coderouter doctor --check-model <provider>` | 1 provider を叩いて 3 probe: (1) tool_calls 構造 vs text-JSON (v0.3-A repair 経路判定) / (2) thinking block emit 有無 / (3) `message.reasoning` leak 有無。追加で num_ctx 境界 probe + auth probe (401 early detect)。Registry 宣言と照合、乖離時に `⚠️ NEEDS TUNING` + providers.yaml / model-capabilities.yaml patch を emit。終了コード `0` (match) / `2` (needs_tuning) / `1` (unsupported|auth_fail) で CI 流用。§18 予定の `--network` flag も同時搭載 |
| v0.7-C | README Troubleshooting + HF-on-Ollama reference profile | 上記 5 症状を Troubleshooting に章立て、各症状に `coderouter doctor --check-model` 導線。`examples/providers.yaml` に HF 蒸留 Ollama provider の reference stanza 追加。lunacode `MODEL_SETTINGS.md` とのクロスリンク (併用運用者向け) |

#### 設計 policy

- **per-provider 粒度は維持** (per-model glob にしない)。同じ `qwen3.6:35b` でも Ollama / LMStudio / OpenRouter で tool calling 可用性が違うケースがあり、**provider context で宣言する**現行の粒度が router として正しい。lunacode は editor harness なので per-model でよかったが、CodeRouter は provider 抽象が前提
- **glob defaults は新しい YAML layer として追加**、per-provider explicit は最優先を維持。優先順位: `providers.yaml` の `capabilities.*` (explicit) > `model-capabilities.yaml` (user) > bundled defaults (glob) > Python 最終 fallback (無し)
- **probe は破壊的でないこと**。書き込み tool (Bash / Write) を誘発しない prompt のみ。API key を消費はするが最小限 (1 probe ≤ 100 tokens 入出力)
- **layered lookup は採らない** (`<cwd>` → `<repo>` → `~/` の 3 層マージ)。router の providers.yaml は deployment 時 static config であって per-cwd ではない。代わりに要望があれば `providers.d/*.yaml` merge を v0.7-D or v0.8 に分離検討 (現状 YAGNI)
- **v1.0 と直交**。v1.0 の output cleaning (`<think>` strip) は content-bytes 後処理レイヤ。v0.7 は pre-flight config/probe レイヤ。両者は同じ症状 3 に異なる角度でアプローチする (v0.7 は発生源抑制 = `/no_think` directive を推薦、v1.0 は流出 bytes を剥がす)。v0.7 が先行することで v1.0 design 時に「何を抑制済み/残留しているか」の input になる

#### DoD

- [x] `model-capabilities.yaml` が `capability.py` の heuristic を完全置換 (Python 側は loader + matcher のみ残す、regex 焼き込みは削除) — **v0.7-A 完了** (`_THINKING_CAPABLE_PATTERNS` / `_THINKING_CAPABLE_RE` / `re` import 撤去、bundled YAML が旧 regex を 1:1 encode、振る舞い変更ゼロ)
- [x] `coderouter doctor --check-model <provider>` が 5 症状のうち 3 + 4 + 5 を検出可能 (1 の num_ctx / 2 の tool_calls / 3 の thinking は該当 probe で必ずカバー) — **v0.7-B 完了** (`coderouter/doctor.py` 新規、4 probe auth/tool_calls/thinking/reasoning-leak、symptom 2 tool_calls / symptom 3 thinking / symptom 4 404=UNSUPPORTED / symptom 5 401/403=AUTH_FAIL の 4 つを事前検出。#1 num_ctx probe は follow-on) → **v1.0-B で完全消化** (5 symptom すべてに固有 probe 対応、後述 §10.2)
- [x] Registry 乖離時の suggested patch が `providers.yaml` / `model-capabilities.yaml` どちらにも copy-paste 可能 — **v0.7-B 完了** (`_patch_providers_yaml_capability` / `_patch_model_capabilities_yaml` 両ヘルパ、valid-YAML で parse 可能なことをテストで保証、header comment で貼り先ファイル名明示)
- [x] 終了コード `0` / `1` / `2` が CI で流用可能 (smoke test シナリオ 1 本添付) — **v0.7-B 完了** (`exit_code_for()` が blocker > tuning > ok の precedence で 1/2/0 を返す、報告末尾に `Exit: N` grep 可能な line、`test_exit_code_*` 3 test)
- [x] README Troubleshooting に 5 症状全て記述、各症状に probe コマンド導線 — **v0.7-C 完了** (`### Ollama beginner — 5 silent-fail symptoms (v0.7-C)` subsection、症状 × `coderouter doctor` 実行例 × YAML patch × fix command の 3 点セット、`examples/providers.yaml` `ollama-hf-example` stanza 添付、lunacode `MODEL_SETTINGS.md` cross-link)
- [x] v0.7.0 umbrella tag + [`docs/retrospectives/v0.7.md`](./docs/retrospectives/v0.7.md) 執筆 — **v0.7-C 完了** (retrospective 執筆完了、scope at a glance 表 / 3 sub-release の narrative / 4 design through-lines / what worked / what was sharp / 7 follow-ons / numbers block / how to read this セクションで v0.6.md format に準拠。`git tag v0.7.0` は operator-side 作業)
- [x] テスト合計 306 → ≈340 target (+~35: v0.7-A +15 / v0.7-B +20) — **v0.7-A で 306 → 345 (+39)** + **v0.7-B で 345 → 382 (+37)** + **v0.7-C ±0 (docs-only)**、計 +76 (target +35 の 2 倍超)。`tests/test_doctor.py` 新規 +31 / `tests/test_cli.py` +6

---

## 10. v1.0 — Tool-Call 信頼性 + Code Mode

### 10.1 スコープ

claude-code-local の "実戦で証明された5機能" を取り込む:
- Tool-call フォーマット変換 (Gemma / Llama / Qwen / HF 各形式 ↔ Anthropic)
- 壊れた JSON のリカバリ
- Code Mode (harness slim 化)
- プロンプトキャッシュ再利用
- 出力クリーニング (`<think>` 等剥がし)
- 14 ケース回帰テスト

### 10.2 詳細タスク

- [ ] **Tool-call 変換層**
  - [ ] Anthropic `tool_use` ブロックを共通中間形式に変換
  - [ ] 共通中間形式 → モデル別 tool 呼出フォーマットに変換
    - [ ] OpenAI 形式 (`tool_calls`)
    - [ ] Gemma 形式 (`<|tool_call>call:Name{...}<tool_call|>`)
    - [ ] Llama 3.x 形式 (生 JSON)
    - [ ] HuggingFace `<tool_call>` JSON
  - [ ] 上流応答 → Anthropic `tool_use` への逆変換
- [ ] **`recover_garbled_tool_json()`**
  - [ ] XML in JSON 検出
  - [ ] `<function=X><parameter=Y>` のフォールバック解釈
  - [ ] パラメータキーから tool 名推測
- [ ] **リトライ**
  - [ ] tool_call 意図検出 (heuristic)
  - [ ] パース失敗時に明示プロンプトで最大2回リトライ
- [ ] **Code Mode**
  - [ ] tools 配列に `Bash/Read/Edit/Write/Grep/Glob` のいずれかが含まれる場合に発火
  - [ ] 既定の slim system prompt (~100 トークン) を投入
  - [ ] プロファイル単位で slim/full 切替可能に
- [ ] **プロンプトキャッシュ**
  - [ ] Anthropic adapter: prompt caching API 利用
  - [ ] OpenAI 互換 adapter: prefix ハッシュベースの自前キャッシュ
  - [ ] capability `prompt_cache` で宣言
- [x] **出力クリーニング** — **v1.0-A 完了** (2026-04-20)
  - [x] フィルタチェイン化 (`output_filters: [strip_thinking, strip_stop_markers, ...]`) — `coderouter/output_filters.py` で `OutputFilterChain` + 2 filter (`StripThinkingFilter` / `StripStopMarkersFilter`) + `KNOWN_FILTERS` registry + `validate_output_filters` で fast-fail、`ProviderConfig.output_filters` + `_check_output_filters_known` model-validator で config-load 時に typo 検出
  - [x] `<think>...</think>`, `<|channel>thought`, `<turn|>`, `<|python_tag|>` 等 — `DEFAULT_STOP_MARKERS = ("<|turn|>", "<|end|>", "<|python_tag|>", "<|im_end|>", "<|eot_id|>", "<|channel>thought")` / streaming では `_max_suffix_overlap` で chunk 境界の partial tag を保留、EOF で flush。OpenAI-compat (`generate` + `stream`) + Anthropic native (`generate_anthropic` + `stream_anthropic`) 両 adapter に hook、後者は per-text-block chain で cross-block 状態干渉なし。`log_output_filter_applied` typed payload で 1-stream 1-log dedupe。v0.7-B doctor `_probe_reasoning_leak` 拡張で content-embedded `<think>` / stop markers を検出し、必要な filter を列挙した `providers.yaml` patch を emit (v0.7 retrospective "transformation には probe が伴う" 原則の first application)。+49 tests (382 → 431)
- [x] **Doctor `num_ctx` probe 直接検出** — **v1.0-B 完了** (2026-04-20)
  - [x] v0.7-B 5-symptom coverage の最後の gap (symptom #1 の間接検出) を閉じる — `coderouter/doctor.py` に `_probe_num_ctx` 追加、canary `ZEBRA-MOON-847` を prompt 先頭に置き ~5K token padding で Ollama default 2048 を overflow させ echo-back で truncation を直接観測。`_is_ollama_like(provider)` 2-signal detection (`:11434` port OR `extra_body.options.num_ctx` declared) で非 Ollama provider は SKIP。5-verdict branch (echoed + adequate/echoed only/missing + nothing → add 32768/missing + low → bump/missing + adequate → intrinsic-limit note)、`_patch_providers_yaml_num_ctx` で `extra_body.options.num_ctx: 32768` patch emit、auth short-circuit SKIP tuple に `num_ctx` 追加、probe 順序を `auth → num_ctx → tool_calls → thinking → reasoning-leak` に変更 (truncation verdict が tool_calls 誤検出を支配)。`tests/test_doctor.py` fixture `_oa_provider` default base_url を `:11434` → `:8080` に migration (既存 36 test は non-Ollama-shape と判定されて probe SKIP で通過、mock 追加不要)。+10 tests (431 → 441)
- [x] **Doctor streaming-path probe (output-side truncation 直接検出)** — **v1.0-C 完了** (2026-04-20)
  - [x] v1.0-B の input-side probe の output-side 鏡像。`coderouter/doctor.py` に `_probe_streaming` 追加 (6 番目の probe)、deterministic prompt `"Count from 1 to 30, one number per line. Output only the numbers, nothing else."` を `stream: true` で投げ、`_http_stream_sse` helper が `httpx.AsyncClient().stream("POST", ...)` + `aiter_lines()` で SSE を最後まで消費。5-way verdict: (a) non-Ollama-shape → SKIP / (b) transport err / 4xx / 5xx → SKIP + 診断 note / (c) 2xx + 0 chunk (JSON が来た or 非標準 framing) → NEEDS_TUNING **advisory** (server-side 修正事項、patch 空) / (d) 2xx + `finish_reason="length"` + content < 40 char → NEEDS_TUNING + `_patch_providers_yaml_num_predict(provider, 4096)` patch / (e) 2xx + `finish_reason="stop"` → OK (`[DONE]` 無しは OK + informational note)。Threshold: `_STREAMING_PROBE_MIN_EXPECTED_CHARS = 40` (30 個数字 + 改行で 60+、40 切りは明確打ち切り)、`_STREAMING_PROBE_NUM_PREDICT_DEFAULT = 4096` (Claude Code 応答 200-2000 token を飲み VRAM 圧迫せず)。Ollama-shape gating は v1.0-B と共用 (`_is_ollama_like`)。Probe 順序は `auth → num_ctx → tool_calls → thinking → reasoning-leak → streaming` で最末尾 — output-side は先行 declaration probe と直交軸なので干渉しない位置に。Auth short-circuit SKIP tuple を 5-probe → 6-probe に拡張 (`streaming` 追加)。`tests/test_doctor.py` 既存 5 つの Ollama-shape test に `_add_sse_ok_mock` を 1 行追加 (6-probe chain が end まで走る)、新規 test helper `_sse_stream_count_body` / `_add_sse_ok_mock`、+12 tests (441 → 453、2 patch-emitter + 10 probe behavior)
- [ ] **回帰テスト 14 ケース**
  - [ ] mkdir / ls / read / edit / grep / 連続5本 / multi-step calendar
  - [ ] CI で全 provider について実行できるよう matrix 化
- [ ] **チューニング既定値**
  - [ ] coding profile: temperature 0.2 を既定
  - [ ] tool_call 検出時のリトライ回数を `MLX_TOOL_RETRIES` 相当の env で

---

## 11. v1.6 — auto_router (task-aware routing)

> **状態**: **v1.6.0 で出荷済み (2026-04-22、tag `v1.6.0` commit `a6ac84b`)**。v1.6-A / v1.6-B / v1.6-C の 3 sub-release で schema + classifier / ingress + metrics 配線 / examples + quickstart を投入、527 → 596 tests green。patch-level の v1.6.1 (2026-04-23) で NIM 無料枠対応 + ドキュメント言語優先度スワップ + `__version__` fix を同梱、596 → 601 green。§11.4 に v1.6.1 内容を別記。
> **テーマ**: 「プロファイル概念を知らずに動く」を成立させる。リクエスト本文から用途 (`coding` / `writing` / `multi`) を推論し、対応するフォールバックチェーンに振り分ける。
>
> **旧 §11 内容** (配布 / launcher / doctor / network audit — 旧 v1.1 ラベル) は **v1.7** に繰り下げ、§11.B として別項目化。

### 11.1 なぜ作るか — 3 ティアのユーザー像

v0.6-D まで CodeRouter は「呼び出し側がプロファイルを知っている」前提だった。これは運用者 (router を立てた本人) 目線では自然だが、**エンドユーザー (Claude Code / codex を叩く人)** には不自然。プロファイルを知らない人が Claude Code で画像を貼っても、`coding` プロファイルの 7B コーダーモデルに流れる。

3 ティアを同じ yaml / 同じサーバで支えるのが v1.6 の目標:

| ティア | 触れ方 | 想定 yaml |
|---|---|---|
| **初心者** | ゼロ config、auto が全部やる | `default_profile: auto` のみ (bundled ルール) |
| **中級者** | `auto_router:` block で rule 書き換え | bundled ルールを copy & edit |
| **上級者** | per-request に `X-CodeRouter-Profile` で明示強制 | 既存 v0.6-D precedence に乗る |

**境界は imperative vs declarative**: 中級者は yaml で宣言、上級者はリクエスト単位で命令。auto_router はこの declarative 層を拡張する feature。

### 11.2 スコープ

- `default_profile: auto` を bundled サンプル yaml のデフォルトに昇格
- `AutoRouterConfig` pydantic スキーマ (`rules: [AutoRouteRule]`、`disabled: bool`)
- `AutoRouteRule` matcher: `has_image` / `code_fence_ratio` / `content_contains` / `content_regex` (v1.6 は最小 4 種、後方互換で拡張可)
- **Bundled default ruleset** — ユーザーが `auto_router:` を書かない場合の fallback。3 ルール: (1) image attachment → `multi`、(2) code_fence_ratio ≥ 0.3 → `coding`、(3) default → `writing`
- 既存 precedence の拡張:
  - 旧: `body.profile > X-CodeRouter-Profile > X-CodeRouter-Mode > default_profile`
  - 新: `body.profile > X-CodeRouter-Profile > X-CodeRouter-Mode > auto_router (default_profile == "auto" 時) > default_profile`
- ログイベント `auto-router-resolved` (typed payload: `rule_id` / `resolved_profile` / `signals`)
- v1.5 ダッシュボードへの配線 (新パネル不要 — Recent Events + Usage Mix に自然に乗る)
- 仕様ドキュメント `docs/designs/v1.6-auto-router.md`
- 起動時検証: 全 `rule.profile` が `profiles` に存在すること、bundled ルール使用時は `multi` / `coding` / `writing` の 3 profile が定義済みであること (欠けていたら fast-fail)
- quickstart.ja.md / README の初心者導線を "auto が動く" 前提に書き換え

### 11.3 詳細タスク (3 フェーズ)

**Phase 1 — 仕様ドラフト** (#61)

- [ ] `docs/designs/v1.6-auto-router.md` 新規
  - [ ] 問題提起 (3 ティア)
  - [ ] precedence 挿入位置の図解
  - [ ] `AutoRouterConfig` / `AutoRouteRule` スキーマ定義
  - [ ] bundled ルールセット (3 rule)
  - [ ] matcher DSL (現 4 種 + 拡張点)
  - [ ] user override セマンティクス (絶対上書き、prepend ではない)
  - [ ] 失敗モード (不明 matcher キー / profile 不在 / yaml 壊れ)
  - [ ] ログ event `auto-router-resolved` payload 定義

**Phase 2 — テスト先書き** (#62) **— 完**

- [x] `tests/test_auto_router.py` スケルトン (26 tests, 5 groups)
  - [x] image attachment detection → `multi`
  - [x] code fence ratio threshold (0.29 は `writing`、0.31 は `coding`)
  - [x] default rule → `writing` fallthrough
  - [x] body.profile が auto を上書き
  - [x] `X-CodeRouter-Profile` header が auto を上書き
  - [x] bundled ruleset を使った zero-config 動作
  - [x] user yaml が bundled を完全上書き
  - [x] `auto-router-resolved` ログ event に rule_id / resolved_profile が入る
  - [x] 起動時検証: bundled 利用時に profile 不在なら起動エラー
  - [x] `default_profile: auto` 時に `auto_router.disabled: true` なら旧挙動

**Phase 3-A — schema + classifier** (#63) **— 完**

- [x] `coderouter/routing/auto_router.py`
  - [x] `classify(body, config) -> profile_name`
  - [x] 4 matcher 実装 (`has_image` / `code_fence_ratio` / `content_contains` / `content_regex`)
  - [x] bundled ルールセット定数 (`BUNDLED_RULES`)
  - [x] ログ event emission (`auto-router-resolved` / `auto-router-fallthrough`)
- [x] `coderouter/config/schemas.py`
  - [x] `RuleMatcher` / `AutoRouteRule` / `AutoRouterConfig` 追加
  - [x] `CodeRouterConfig.auto_router: AutoRouterConfig | None` 追加
  - [x] `default_profile == "auto"` かつ bundled 使用時の profile 存在検証 (`@model_validator`)
  - [x] `"auto"` reserved name の fast-fail
  - [x] `content_regex` compile-time 検証

**Phase 3-B — ingress 配線 + counter** (#64) **— 完**

- [x] `coderouter/ingress/{openai,anthropic}_routes.py` — mode header 解決直下に auto 解決を挿入 (`default_profile == "auto"` 時のみ発火)
- [x] `coderouter/metrics/collector.py` — `auto-router-fallthrough` event を `auto_router_fallthrough_total` カウンタへ集約
- [x] `coderouter/metrics/prometheus.py` — `coderouter_auto_router_fallthrough_total` として /metrics へ expose
- [x] `tests/test_auto_router.py` autouse fixture で collector をテスト間で reset
- [x] 検証: `pytest tests/test_auto_router.py` → 26/26、full suite 596/596 green、`mypy --strict` clean

**Phase 3-C — docs + examples + real-machine verify** (#65) **— 実機確認を除き完**

- [x] `examples/providers.auto.yaml` (新設、zero-config 初心者向け — `default_profile: auto` + 3 Ollama provider + `multi`/`coding`/`writing` 3 profile、内蔵ルール即発火)
- [x] `examples/providers.auto-custom.yaml` (新設、中級者向け copy-edit 起点 — 4 matcher variant を網羅した `auto_router:` ブロック付き、load_config + classify smoke test で全ルール動作確認済み)
- [x] `examples/providers.yaml` は `default_profile: default` のまま据え置き (既存テスト / 既存ユーザーの設定互換性を優先、auto は別ファイル併置方式で対応)
- [x] `docs/quickstart.ja.md` に「補足: プロファイル選択を CodeRouter に任せる (v1.6 `auto_router`)」セクションを Pattern A/B の後に追加 (C-1 pull → C-2 `cp auto.yaml` → C-3 カスタマイズ)
- [x] `CHANGELOG.md` v1.6.0 umbrella エントリ (3 sub-release breakdown + non-breaking compat 注記、tests 527 → 596)
- [x] `docs/quickstart.md` (英語版、#58) — v1.6.1 のドキュメント言語優先度スワップで `docs/quickstart.en.md` として整備。quickstart.ja.md の「補足: `auto_router`」セクションは英語側にも伝播済み
- [ ] README / README.ja フロント面への `auto_router` 記載 — v1.6.1 のヒーロー書き換えでは tool-call repair を最前面に据えたため、auto_router 記載は次の doc pass に引き続き送り
- [ ] 実機確認: Claude Code / codex で `default_profile: auto` 挙動を手動で一巡 (ユーザー環境依存のため別枠)

### 11.4 v1.6.1 — NIM 無料枠 + ドキュメント言語優先度スワップ + `__version__` fix (2026-04-23)

v1.6.0 出荷直後の patch-level。5 系統を 1 release に束ねた:

- [x] **NVIDIA NIM 無料枠 (40 req/min) 対応** — `examples/providers.nvidia-nim.yaml` 新設。live 検証 (2026-04-23) 済み:
  - `meta/llama-3.3-70b-instruct` (chat 540ms / tool_calls OK) — 第一選択
  - `qwen/qwen3-coder-480b-a35b-instruct` (chat 634ms / tool_calls OK) — agentic coding 品質 fallback
  - `moonshotai/kimi-k2-instruct` (chat 2.8s / tool_calls OK) — NIM レーン内での別ベンダー diversity
  - `qwen/qwen2.5-coder-32b-instruct` は NIM で tools 無効 (`HTTP 400 "Tool use has not been enabled"`) のため `tools: false` で declare、capability gate で tool-laden traffic を回避
  - 不採用の live 観測結果 (slug 404 / 410 EOL / content-null / timeout) を YAML コメントに記載
- [x] **`claude-code-nim` / `nim-first` / `free-only-nim` / `nim-reasoning` の 4 プロファイル** — local (Ollama 7B/14B) → NIM 3 段 (Meta/Qwen/Moonshot 異ベンダー) → OpenRouter free 2 段 (qwen/gpt-oss) → paid の 8 段チェーン
- [x] **`tests/test_examples_yaml.py` 新設** — `examples/providers*.yaml` 全件ロード + NIM 固有 invariants (`api_key_env=NVIDIA_NIM_API_KEY` / `base_url=https://integrate.api.nvidia.com/v1` / tool-capable 枝の `tools: true` / chat-only 枝の `tools: false` / `nim-kimi-k2-thinking` がプライマリチェーン不在) を CI 時強制、596 → 601 green
- [x] **ドキュメント言語優先度スワップ** — `README.md` / `docs/{usage-guide,security,quickstart,when-do-i-need-coderouter}.md` の 5 ペアを `git mv` で日本語 main (`.md`) / 英語 sub (`.en.md`) に入替。`pyproject.toml readme = "README.md"` も日本語 readme に切替 (PyPI 表示を日本中心のターゲット層に合わせる)
- [x] **クロスリファレンス 20+ 箇所の同時更新** — 両 README の言語スイッチャー、docs 内部リンク、`docs/articles/note-*.md` / `zenn-*.md` の GitHub blob URL 全てを新ファイル名に追随、全 `.md` リンク解決を walker で検証
- [x] **README ヒーロー書き換え** — 汎用の「Local-first coding AI with ZERO cost」タグラインから「Claude Code × ローカル LLM tool calling 破綻 → CodeRouter の tool-call 修復で復元」を最前面に。GIF placeholder (`docs/assets/before-after-toolcall.gif`) を HTML comment で予約
- [x] **`docs/free-tier-guide.md`** 新規 — NIM + OpenRouter 無料枠の使い分け reference (3 層比較表 / `claude-code-nim` プロファイル設計意図 / live 検証済みモデル一覧 / 5 common footguns / `coderouter doctor` 出力例)、JA primary + EN sub、`README.md` + `docs/usage-guide.md` §6 から双方向リンク
- [x] **`coderouter/__init__.py` fix** — `__version__` を hardcode (`"1.5.0"`) から `importlib.metadata.version("coderouter")` 経由に切替 (`009b2b1`)。以降 `pyproject.toml` の `version` 1 行が single source of truth で、`coderouter --version` / `/healthz` 両方が正しく 1.6.x を報告する
- [x] **CI fix** (`d0de1a9`) + `docs/designs/v1.6-auto-router-verification.md` の 2026-04-22 実機 run log 追記 (`19edd97`)

**DoD**:

- [x] pytest 全件 green (601 tests; pre-existing 596 + NIM invariants 5)
- [x] `coderouter doctor --check-model nim-{llama-3.3-70b,qwen3-coder-480b,kimi-k2,qwen-coder-32b-chat}` 全て Exit 0 (live)
- [x] 全 `.md` リンク解決を walker で検証 (唯一残る dangling は pre-existing の `docs/openrouter-roster/CHANGES.md`、cron 生成で意図的)
- [x] README 両言語のバージョンバッジ 1.5.0 → 1.6.1、テスト数 453 → 601 に同期
- [x] `CHANGELOG.md` に `[v1.6.1]` エントリ、release history table 更新

### 11.5 v1.6.2 — Troubleshooting 切り出し + `.env` / NIM YAML hygiene (2026-04-24)

v1.6.1 出荷直後、自分が NIM 構成を実機で立てて二重トラップ (env 変数の `export` 漏れによる 401 → そこを越えても Llama-3.3-70B が "こんにちは" を `Skill(hello)` に化けさせる) を踏んだのを受けて、現場で得た知見を docs / examples 側へ確実に折り込む patch-level。コード変更を伴わないため CHANGELOG / plan.md / docs のみで完結する小さな release。

- [x] **`docs/troubleshooting.md` 新規 (JA primary)** — README §トラブルシューティングの全文を独立化した上で v1.6.2 検証で発覚した 5 トピックを追加 (§1 起動・設定の罠 / §4 Claude Code 連携の罠)。具体的には CLI コマンド訂正 (`serve --mode`、`--profile` ではない)、`.env` の `export` 必須、`env` での export 検証、`Header of type authorization was missing` 401 の切り分け、`~/.zshrc` 反映漏れ、Llama-3.3-70B 系の過剰ツール呼び出し、`UserPromptSubmit hook error` (claude-mem 等プラグインとの構造的ミスマッチ)、auto-compact 遅延、ダッシュボード活用の 9 項目
- [x] **`docs/troubleshooting.en.md` 新規 (EN sub)** — JA 版と章番号 / アンカー 1 対 1 対応
- [x] **README.md / README.en.md §トラブルシューティング短縮** — 30 秒で読める早見表 + 症状別索引 (4 入口) に置換、Ollama 5 症状は 1 行サマリ + リンクのみ。旧アンカー (`ollama-初心者--サイレント失敗-5-症状-v07-c` / `ollama-beginner--5-silent-fail-symptoms-v07-c`) は両 README に残して後方互換確保
- [x] **README.md / README.en.md ドキュメント目次** — 「詰まったとき」「When stuck」行を `troubleshooting.md` / `.en.md` 指向で追加、両 README の言語スイッチャに `troubleshooting` / `トラブルシューティング` を併記
- [x] **`docs/usage-guide.md` / `usage-guide.en.md` §8 quick index** — 既存 README 参照を `docs/troubleshooting.md` 指向に書き換え、`Header of type authorization was missing 401` と「Claude Code 上で挨拶が `Skill(hello)` 等に化ける」の 2 行を追記
- [x] **`examples/.env.example`** — 全キー (`ALLOW_PAID` / `OPENROUTER_API_KEY` / `NVIDIA_NIM_API_KEY` / `ANTHROPIC_API_KEY` / `CODEROUTER_CONFIG`) を `export KEY=value` 形式に統一。冒頭に「ロード方法 (`source .env` で動く / `set -a && source .env && set +a` でも可) / CodeRouter は自動 source しない / 検証コマンド (`env | grep ...`)」のドキュメンテーションを追加
- [x] **`examples/providers.nvidia-nim.yaml` 4 プロファイル並び替え** — `claude-code-nim` / `nim-first` / `free-only-nim` / `nim-reasoning` の全てで NIM レーンの順序を Qwen3-Coder-480B → Kimi-K2 → Llama-3.3-70B に変更 (実機検証で Llama-3.3-70B が Claude Code 単独利用時に過剰ツール呼び出しを起こすことが判明、第一選択から退避線へ)。プロファイル直前のコメントブロックに選定理由 (実機検証の症状ログ + `docs/articles/note-nvidia-nim.md` §6-2 への参照) を追加
- [x] **`examples/providers.nvidia-nim.yaml` セットアップコメント拡張** — 冒頭の "NVIDIA NIM setup" を 5 ステップに拡張、`.env` の `export` 必須 / `coderouter doctor` を起動前に通すこと / `--port 8088` を Claude Code に合わせる必要を明記
- [x] **`docs/articles/note-nvidia-nim.md` 改訂** — v1.6.2 検証ログを §6 (実機罠 3 種) と §7 (ダッシュボード活用) に追記、§4 / §9 / §11 の手順を実機検証済みコマンドに更新

**DoD**:

- [x] `docs/troubleshooting.md` / `.en.md` の章番号 / アンカーが両言語で 1 対 1 対応していること
- [x] README §トラブルシューティングから新ドキュメントへの 4 入口リンクが全て解決すること
- [x] 旧アンカー (`#ollama-初心者...`) を README 内に残置してリンク切れを防いでいること
- [x] `examples/.env.example` で `source .env` 直後に `env | grep` で全キーが visible になること
- [x] `examples/providers.nvidia-nim.yaml` の YAML が pytest `tests/test_examples_yaml.py` でパスすること (profile 順序変更後も既存 invariant が通る)
- [x] `CHANGELOG.md` に `[v1.6.2]` エントリ、release history table 更新

**スコープ外 / 次回送り**:

- [x] `coderouter serve --env-file .env` フラグでの `.env` 自動 source 提供 — **v1.6.3 で解消** (§11.6)
- [ ] capability registry に `claude_code_suitability: degraded` のような hint を追加して、Llama-3.3-70B 系を Claude Code チェーンに置く時に startup で WARN を出す仕組み — 設計の比重が大きいので v1.7 で別途検討

### 11.6 v1.6.3 — `--env-file` + `doctor --check-env` で `.env` ハイジーン (2026-04-24)

v1.6.2 で「`.env` の `export` 漏れ罠」をドキュメント化した直後の patch-level。**手順を覚える代わりにコマンドで解消**できるよう、2 つの新機能を投入:

- [x] **`coderouter serve --env-file PATH`** — `.env` style file を uvicorn 起動前にロード。複数指定可で left-to-right に layering。デフォルトは「shell が勝つ、file は不在キーを埋める」の安全側挙動 (`--env-file-override` で反転)。1Password CLI / sops / direnv / OS Keychain と全部噛み合うゲートウェイを 1 本提供
- [x] **`coderouter doctor --check-env [PATH]`** — `.env` の filesystem / git 状態を 4 項目検査 (existence / POSIX 0600 / `.gitignore` 包含 / git tracking)。`--check-model` と同じ exit code 規約 (0 OK / 2 patchable / 1 blocker)。WARN は `chmod 0600 ...` のような 1 行 fix を、ERROR (= 既に追跡されてる) は `git rm --cached` 入りの remediation 手順を出力
- [x] **stdlib のみの `.env` parser** (`coderouter.config.env_file`) — 1Password / sops / 手書きが実際に出すサブセット (bare / `"double"` quotes with `\n`/`\t`/`\"` escapes / `'single'` quotes literal / `export` prefix / inline `#` comments / blank lines) を網羅。POSIX-invalid key と未終端 quote は `file:lineno` 付きで明示エラー
- [x] **`doctor --check-model` を required から optional へ** — `--check-env` が代替として導入されたため、両方どちらも optional。両方 unset → 友好的 stderr + exit 1。以前から `--check-model` を渡していたスクリプトは挙動変化なし
- [x] **`docs/troubleshooting.md` / `.en.md` §5 新設** — 「`.env` のセキュリティ運用」7 サブセクション (脅威モデル + クイックチェック + 1Password CLI レシピ + direnv+sops レシピ + OS Keychain レシピ + `--env-file` layering + キースコープ最小化)、JA primary + EN sub
- [x] **暗号化を自前実装しない判断** — §5-1 に明文化。脅威モデル 7 種のうち at-rest 暗号化が効くのは 2 種だけ、復号鍵の置き場所問題が残る、既存ツール (1Password / sops) は既に同じ問題を解いている、それを `--env-file` で受けるのが実装 + audit 両面で正解

**DoD**:

- [x] pytest 全件 green (601 → **651**, +50: env_file 26 + env_security 15 + cli 8 + 1 renamed)
- [x] `ruff check coderouter/ tests/` clean (新規追加分について)
- [x] `--env-file` と `--check-env` が単体動作確認済み (`coderouter serve --env-file ...` で env が export される様子を stderr で確認、`coderouter doctor --check-env /tmp/test-env-cli` で perms WARN → 修正後 OK の往復を確認)
- [x] `pyproject.toml version` を 1.6.1 → 1.6.3 に更新 (1.6.2 は docs only patch だったので minor inc は v1.6.3 で吸収)
- [x] `CHANGELOG.md` に `[v1.6.3]` エントリ
- [x] `docs/troubleshooting.md` / `.en.md` §5 新設

**スコープ外 / 次回送り (v1.6.3 → v1.7)**:

- [ ] capability registry の `claude_code_suitability` hint (Llama-3.3-70B 系の startup WARN) — まだ v1.7
- [ ] `coderouter doctor --check-env` の auto-discover が ./.env / ~/.coderouter/.env のいずれも見つからない時、より丁寧な「どこを探したか」表示 — `--check-env` 単体での Quality of Life。実害は無いので後回し

### 11.B (v1.7) — 配布 / launcher / doctor

> 旧 §11 (旧 v1.1 ラベル) の内容をここに退避。v1.6 を auto_router に譲った結果、配布回りは v1.7 に繰り下げ。scope そのものは未変更。
> **v1.7-A (PyPI 公開) は 2026-04-25 出荷済み** (§11.B.3 参照)。
> **次の v1.7-B プラン (4 項目: Trusted Publishing 自動化 / `claude_code_suitability` hint / `doctor --apply` / `setup.sh`)** は §11.B.4 にまとめてある。v1.7-C 候補 / 不実施判断は §11.B.5 / §11.B.6。

#### 11.B.1 スコープ

- `uvx coderouter` または `npm i -g` 一発で動く
- macOS `.command` / Windows `.bat` / Linux `.sh` の launcher 配布
- `coderouter doctor` で構成監査
- `coderouter doctor --network` で外向き接続を検出 (0 outbound を保証)

#### 11.B.2 詳細タスク

- [x] **`uv` 配布パイプライン (PyPI)** — v1.7-A で `coderouter-cli` を PyPI に公開。`uvx coderouter-cli serve --port 8088` 1 行で動く
- [ ] `setup.sh` (RAM 検出 → 推奨ローカルモデルダウンロード → providers.yaml 生成)
- [ ] `Claude Local.command` 互換の launcher 自動生成
- [ ] `coderouter doctor`
  - [ ] 設定ファイル lint
  - [ ] 各 adapter の healthcheck
  - [ ] `ALLOW_PAID` の現状表示
- [ ] `coderouter doctor --network`
  - [ ] `lsof -i -P` 相当を内蔵 or サブプロセス
  - [ ] 接続先一覧をホワイトリストと照合
  - [ ] 「localhost only」のグリーン表示
- [ ] アップデートチェック (任意 / opt-in)
- [ ] PEP 541 reclamation: `coderouter` 名前空間譲渡を Lawrence Chen 経由 / PyPI support に申請 (通れば `coderouter-cli` を alias 化)

#### 11.B.3 v1.7-A — PyPI 公開 (`coderouter-cli`) [2026-04-25 出荷]

v1.7 の幅広いスコープのうち**配布パイプライン部分だけ先行**して shipping。`uvx` 1 行 onboarding は v1.7 の中で最も体感価値が高く、かつ後続の launcher / setup.sh から見ても「PyPI 上に package があるからこそ意味がある」基盤なので、最優先で出した。コード変更は最小 (パッケージ名 rename + version 参照追従) で、テストへの影響もゼロ (651 → 651)。

**やったこと**:

- [x] PyPI 名前空間 `coderouter` の調査 → 既取得 (Lawrence Chen, HTTP routing 系汎用ライブラリ, 2025-06 single 0.1.0) のため `coderouter-cli` で取得
- [x] `pyproject.toml`: `name` rename + `version = "1.7.0"` + `classifiers` enrich + `project.urls` 4 本 + sdist `only-include` allowlist 厳格化
- [x] `coderouter/__init__.py`: `_pkg_version("coderouter")` → `version("coderouter-cli")` に追従
- [x] `LICENSE` ファイル新規 (MIT、wheel に同梱)
- [x] `.github/workflows/release.yml`: tag-driven publish (Trusted Publishing OIDC) + GitHub Release 草稿
- [x] 1Password Vault Item に `PYPI_TOKEN` / `TESTPYPI_TOKEN` を保存、`.env.publish.tpl` + シェル alias で `op run --env-file=... -- uv publish` 経路を整備
- [x] TestPyPI rehearsal → 本番 PyPI publish (両方成功)
- [x] 実機検証: `uv pip install coderouter-cli==1.7.0` → 23 packages 全部 PyPI から正常解決 + `coderouter --version` 表示 OK
- [x] README ja/en、quickstart ja/en、free-tier-guide ja/en の install セクションを `uvx coderouter-cli` 中心に書き換え
- [x] CHANGELOG `[v1.7.0]` エントリ + plan.md §6.2 release table 行追加

**DoD**:

- [x] PyPI 上で `coderouter-cli==1.7.0` が見える ([https://pypi.org/project/coderouter-cli/](https://pypi.org/project/coderouter-cli/))
- [x] `uv pip install coderouter-cli==1.7.0` → fastapi 0.136 / uvicorn 0.46 等 23 packages の依存解決成功
- [x] `coderouter --version` → `coderouter 1.7.0`
- [x] `git tag v1.7.0` push 済み
- [x] pytest 全件 green (651/651)、Runtime deps 据え置き (17 sub-release 連続)

**スコープ外 / 次回 (v1.7-B 以降)**:

- [ ] `uvx coderouter-cli` (短縮形) が初回 publish 直後の uv 内部 cache に「無い」と焼かれて時間経過まで解決しない現象 — uv のキャッシュ TTL が切れれば自動回復、急ぐなら `uv cache clean --all` + `uv tool uninstall` で対処可
- [ ] Trusted Publishing 設定 (PyPI 側で「pending publisher」として GitHub Actions release.yml + environment `pypi` を登録) — 次回 v1.7.1 push から自動 publish が走るようになる
- [ ] PEP 541 reclamation 申請 — 通れば `coderouter` を alias として迎え入れ、`coderouter-cli` を canonical に維持できる構造を作る

#### 11.B.4 v1.7-B プラン — onboarding 摩擦解消の 4 つの追加コマンド

> v1.7-A で PyPI publish は完了。**次に出荷する v1.7-B は「documented gotcha をコマンドで潰す」** という v1.6.3 (`.env` ハイジーン) と同じ筋を踏む 4 項目の umbrella。期間目安 4〜6 日、テスト追加は 30〜50 件、Runtime deps 据え置き継続を想定。

**実施順 (priority order)** — 上から順に消化する想定 (各タスクは独立して出荷可能):

1. [ ] **Trusted Publishing 自動化** — PyPI 側で pending publisher (`release.yml` + environment `pypi`) を登録するだけ。**作業 5 分、以降の v1.7.x patch は tag push で自動 publish**。残り 3 項目の出荷ペースを上げるため最優先で消化する。コード変更ゼロ、テスト追加ゼロ。
2. [ ] **`claude_code_suitability` hint (capability registry に degraded フラグ)** — Llama-3.3-70B 系を Claude Code チェーンに置いたとき startup で WARN。v1.6.2 で docs 化した「`こんにちは` → `Skill(hello)` 過剰ツール呼び出し」罠の構造化対応。実装は (a) `model-capabilities.yaml` schema に `claude_code_suitability: degraded | unknown | ok` フィールドを足す、(b) `mode_aliases` で `claude-code` 系プロファイルに resolve したときだけ chain 内のプロバイダ全件を walk して `degraded` を WARN、(c) `examples/providers.nvidia-nim.yaml` の Llama-3.3-70B エントリと bundled `model-capabilities.yaml` の Llama-3.3 系列に `degraded` を宣言。期間 1 日、テスト +5〜10。**ドキュメントで説明している既知の罠を、コードで自動検出に格上げ**できるのが価値。
3. [ ] **`coderouter doctor --check-model --apply`** — 既存の YAML patch 出力を user に copy-paste させる代わりに、`--apply` フラグで `~/.coderouter/providers.yaml` に **非破壊的に書き戻し**。doctor → fix → verify のループが完全に閉じる。実装の肝はキー順序とコメント保持で、stdlib `yaml` だと両方失われるため `ruamel.yaml` の限定的 adopt を要検討 (dev-only deps、または `pyproject.toml` の `[project.optional-dependencies].apply` に逃がす)。期間 1〜2 日、テスト +15〜20 (round-trip preservation / partial merge / 既存値との conflict 検出 / `--apply --dry-run` による diff プレビュー)。
4. [ ] **`setup.sh` (onboarding ウィザード)** — RAM 検出 (`sysctl hw.memsize` on macOS / `/proc/meminfo` on Linux) → `docs/usage-guide.md §2` 表に対応する推奨ローカルモデル提案 → ユーザー確認 → `ollama pull` 実行 → `~/.coderouter/providers.yaml` を template 生成。**新規依存ゼロ** (bash + 既存 doctor の組み合わせ)、Linux/macOS 両対応 (Windows は WSL2 経路で同じ)。期間 1.5〜2 日、テスト +10〜15 (RAM parse / template render / 既存 `~/.coderouter/providers.yaml` 上書き確認 / `ollama` 不在時の friendly error)。

**v1.7-B umbrella の DoD**:

- [ ] PyPI publish が tag push で自動化 (Trusted Publishing 経由、API トークン不要、v1.7.1+ から有効)
- [ ] Llama-3.3-70B 等を `claude-code` 系プロファイルに置いた状態で `coderouter serve` を起動すると startup で 1 行 `claude-code-suitability-degraded` WARN が出る (実機検証込み、`docs/troubleshooting.md §4-1` で言及している罠が code で自動検出される)
- [ ] `coderouter doctor --check-model <provider> --apply` で `providers.yaml` の `extra_body.options.num_ctx` / `output_filters` 等が key order + コメント保持で書き換わる、`--dry-run` で diff だけ表示
- [ ] `bash setup.sh` で 16 GB Mac で `qwen2.5-coder:7b` が、48 GB Linux で `qwen2.5-coder:14b` + `:7b` が選ばれて `ollama pull` 実行 + `providers.yaml` が生成される
- [ ] 651 → 700+ tests green、Runtime deps 5 → 5 据え置き (18+ sub-release 連続)、`ruamel.yaml` 採用時のみ optional / dev-only deps に追加

#### 11.B.5 v1.7-C 候補 — onboarding 完了後の polish (実需要が出るまで保留)

v1.7-B が出荷できたら次に拾う候補。**実需要が顕在化するまで着手しない** (推測でやらない、というのが v1.6 系 → v1.7-A の hygiene パスで効いた判断ヒューリスティクス)。

- [ ] `coderouter doctor --network` (`lsof -i -P` 相当 + ホワイトリスト照合 + 「localhost only」のグリーン表示) — supply-chain story の最後のピース。エンタープライズ採用の問い合わせや CI 統合の要望が来たら優先度を上げる
- [ ] `coderouter doctor --check-config` / `--check-adapter` / 引数なしで全 probe 順次実行 — quality-of-life (1 日仕事)。doctor surface の完成度に効くが、現行の `--check-model` / `--check-env` で実用上のカバレッジは足りている
- [ ] `recover_garbled_tool_json` の範囲拡大 (末尾カンマ / 引用符違い / 部分マルチライン等) — **実機で失敗を踏んだ後にデータ駆動で拡張**するのが筋。先回り実装はしない (v0.3-A で balanced-brace + fenced JSON まで実装した時の判断と同じ)
- [ ] 起動時アップデートチェック (opt-in) — `uv tool upgrade coderouter-cli` で済むため需要次第。**opt-in を opt-in できる人 = upgrade コマンドを打てる人**、というジレンマを抱える feature

#### 11.B.6 不実施 / v2.0 まで明示保留

以下は plan.md §6.1 / §11.B.1 に並んでいるが、**現行の onboarding 経路 (`uvx` + `setup.sh` 予定) で十分カバーされる** か、**ソロ開発段階で整備しても腐る**ため、v2.0 直前まで意図的に保留する:

- [ ] **launcher (`.command` / `.sh` / `.bat` 自動生成)** — `uvx coderouter-cli serve` + `setup.sh` で onboarding 経路は十分カバー。専用 launcher は要望が顕在化したら検討、推測で先に作らない。Claude Local.command 互換は v1.0 当初の参考設計だったが、`uvx` の出現で前提が変わった
- [ ] **14 ケース回帰スイート / Code Mode (Claude Code harness slim)** — 既存の `scripts/verify_v0_5.sh` / `verify_v1_0.sh` で重要経路は実機検証可能。形式的な claude-code-local 同等スイート移植は **v2.0 計画の手前で着手** (v1.x 系のうちは保留)
- [ ] **`docs/architecture.md` / `providers.md` / `benchmarks.md`** — README + `docs/usage-guide.md` + plan.md でカバーされている内容を別ドキュメントに切り出すのは、**外部 PR や複数 contributor が関わるようになってから** 必要分だけ書く
- [ ] **`CONTRIBUTING.md` / ISSUE / PR テンプレート** — ソロ開発段階で整備すると腐る。**最初の外部 PR が来たタイミング**で 30 分で書く想定
- [ ] **retrospective: v1.5 + v1.6 + v1.7 を 1 本に統合執筆** — v0.x の各 minor 1 本ペースは v1.x 系で「3 minor 1 本」に圧縮する。v1.7-B/C が出荷した後、v2.0 計画開始の手前で書く想定 (現状 `docs/retrospectives/v1.5.md` プレースホルダだけ存在)

---

## 12. v1.5 — 計測ダッシュボード (出荷済み)

> **状態**: **v1.5.0 umbrella として 2026-04-22 出荷済み** — `v1.5-A/B/C/D/E/F` の 6 sub-release (収集 / 配信 / CLI TUI / HTML / timezone / demo)。`v1.0.1 → v1.5.0` で §11 (旧 v1.1 = 配布 / launcher / doctor) をスキップしているため、§11 ヘッダは **v1.6** にリラベル。詳細は CHANGELOG.md `[v1.5.0]` および §6.2 詳細表。

### 12.1 スコープ (当初設計)

claude-code-local の "数字で見せる" を踏襲。

- tok/s 実測
- fallback 発生率
- プロファイル別の成功率
- 直近のリクエスト一覧
- ローカル / 無料 / 有料 の使用比率

### 12.2 詳細タスク

- [x] メトリクス収集レイヤ (in-memory + JSONL) — v1.5-A で in-memory
      (`MetricsCollector` logging.Handler + `/metrics.json`)、v1.5-B で
      JSONL ミラー (`CODEROUTER_EVENTS_PATH` env、`JsonLineFormatter` と
      同一行シェイプ)。
- [x] 簡易 web UI (`http://localhost:4040/dashboard`) — v1.5-D。
      `coderouter/ingress/dashboard_routes.py` に single-page HTML を
      インライン化 (tailwind CDN + vanilla JS)。htmx は 5-dep policy の
      都合でやめて `setInterval` + `fetch("/metrics.json")` 2s ポーリング
      + `data-bind` 属性ベースの DOM 更新器に差し替え。依存は tailwind
      CDN 1 本のみ、サーバ側テンプレ追加ゼロ。4 panel + usage-mix footer。
      23 integration tests で `data-bind` / panel 見出し / ポーリング
      contract を pin。
- [x] CLI `coderouter stats` (TUI) — v1.5-C。stdlib `curses` + `urllib`
      のみ (追加依存ゼロ)。`coderouter/cli_stats.py` に 3 レイヤ
      (fetch / pure render / driver) 分離。`--once` と非 TTY 自動検出で
      script-friendly、1s リフレッシュ、`[q]/[r]/[p]/[f]` キー操作。
      render 層は 46 unit test でカバー、curses 描画のみ `pragma: no cover`。
- [x] export: prometheus 形式 — v1.5-B で `GET /metrics`
      (`text/plain; version=0.0.4`) 提供、`format_prometheus()` は pure
      fn (~140 行) で依存ゼロ、全 counter に `coderouter_` prefix +
      `_total` suffix。

### 12.3 設計案 (2026-04-21)

v1.0.1 hygiene の後続として、実装は次セッション以降 — ここでは wire-shape / 保存先
/ エンドポイント / UI の選択と、v1.5 を A/B/C/D にスライスする増分出荷パスだけ固め
る。**前提: 依存は増やさない (5-dep policy 維持)、ingress と同じ `localhost:4040`
プロセス内で完結、既存 JSON ログを「一次ソース・オブ・トゥルース」として扱う。**

#### 12.3.1 計測の一次ソース — 既存ログを "tap" する

新しいフックを散りばめるより、すでに JsonLineFormatter で構造化されている以下
のイベントを `MetricsCollector` が一箇所で拾う設計にする。adapters / routing /
output_filters 側のコードに触らずに済むので回帰コストが低い。

- `try-provider` / `provider-ok` / `provider-failed` / `provider-failed-midstream`
  (routing/fallback.py) — fallback 成功率・プロバイダ別成功率の素データ
- `skip-paid-provider` / `chain-paid-gate-blocked` (logging.py §paid-gate) —
  ローカル / 無料 / 有料 使用比率の分母
- `capability-degraded` (logging.py §v0.5.1) — 能力降格の発生率
- `output-filter-applied` (logging.py §v1.0-A) — 出力クリーニングの発火率
- `chain-uniform-auth-failure` (logging.py §auth-gate) — 誤設定検出率

実装手段は **`logging.Handler` サブクラスを 1 本追加**し、root logger に
`JsonLineFormatter` と並置する。`MetricsCollector(logging.Handler)` は
`record.__dict__` から `msg` と extra フィールドを受け取り、in-memory カウンタ /
ゲージ / リングバッファに加算する。ログ書き出し自体は stderr 側で継続する
(破壊的変更ゼロ)。

#### 12.3.2 メトリクス wire-shape

3 カテゴリに畳む — counter / gauge / histogram (簡易) / ring buffer。全て
stdlib `collections.Counter` / `collections.deque` で十分。`prometheus_client`
は入れない (exposition format は ~30 行で手書きできる)。

```
# Counters (単調増加、プロセス寿命)
requests_total{ingress="openai"|"anthropic", stream="true"|"false"}
provider_attempts_total{provider=..., outcome="ok"|"failed"|"failed_midstream"}
provider_skipped_total{provider=..., reason="paid"|"unknown"}
capability_degraded_total{provider=..., capability="thinking"|"cache_control"|"reasoning", reason=...}
output_filter_applied_total{provider=..., filter="strip_thinking"|"strip_stop_markers", streaming="true"|"false"}
chain_paid_gate_blocked_total{profile=...}

# Gauges (現在値)
profile_active{profile=...}  # 起動時プロファイル、1 or 0
# ※ token/s は "直近 N リクエストの EWMA" をゲージ化 (histogram はコスト過多)

# Histograms (簡易: 固定 bucket の Counter)
provider_latency_seconds_bucket{provider=..., le="0.5|1|2|5|10|30|+Inf"}
tokens_per_second_bucket{provider=..., le="5|20|50|100|200|+Inf"}

# Ring buffer (最新 N=256 リクエスト)
events: [{ts, ingress, profile, provider, outcome, latency_ms, tok_per_s, ...}]
```

**判断根拠**: dashboard の用途は "いま何が起きているか" と "直近の傾向" であって
長期時系列 DB ではない (それは Prometheus 本物の仕事)。したがって in-memory で
プロセス寿命の counter + 固定長 ring buffer という最小構成で UI 要件はすべて
満たせる。長期保存は §12.3.3 の JSONL が担当する。

#### 12.3.3 保存層 — 2 層

- **in-memory (primary)**: プロセスが死ぬと消える。Counter / gauge / ring buffer
  はすべてここ。起動後すぐ `/metrics` が 200 を返せる最小構成。
- **JSONL append (secondary, opt-in)**: `CODEROUTER_EVENTS_PATH=~/.coderouter/events.jsonl`
  が設定されていたら、`MetricsCollector.emit()` の末尾で 1 行追記する。SQLite は
  入れない — append-only JSONL なら stdlib `open('a')` で済み、ローテーション
  も `logrotate` に委ねられる (運用粒度での"CRUD"要件が出てきた段階で SQLite
  に昇格、v1.6 以降で検討)。

JSONL の行シェイプは `JsonLineFormatter` の出力そのものを再利用する
(= ログファイルと events.jsonl が同一形式。単に "dashboard 向けに別パスに
ミラーしたもの"、と位置付けると概念が増えない)。

#### 12.3.4 エンドポイント設計

`coderouter/ingress/metrics_routes.py` を新設し、`app.include_router(...)` に
追加する (既存 `/v1/chat/completions` / `/v1/messages` と同居)。

- `GET /metrics` — **Prometheus exposition format** (default, 標準)。
  Prometheus / Grafana Agent / OTel collector がそのままスクレイプ可能。
- `GET /metrics.json` — **構造化 JSON** (`{counters, gauges, histograms, recent}`)。
  内製 UI (`/dashboard`) と `coderouter stats` TUI が食べる。内部向けで安定化
  保証は v1.5 時点では "semver patch で壊す可能性あり" と明示。
- `GET /dashboard` — **HTML one-pager** (htmx + tailwind CDN)。`/metrics.json`
  を 2 秒ポーリング。単一 `.html` テンプレート (Jinja2 は依存を増やすので
  stdlib `string.Template` で書く、テンプレート複雑化したら後日差し替え)。

**Prometheus format を既定にした根拠**: "エクスポートできる" が付加価値の上限
ではなく "運用現場の既定プロトコル" であるため。`/metrics.json` を main にして
`/metrics.prom` をサブに置く設計も検討したが、スクレイパ側のデフォルトパスに
合わせた方が接続試験が 1 ステップ減る。

#### 12.3.5 UI

- **CLI `coderouter stats`**: `urllib.request` で `localhost:4040/metrics.json`
  を 1 秒ポーリングし、`rich` で TUI 描画 — と書きたいところだが rich は依存
  追加。初版は **stdlib `curses` + 1 秒 clear 再描画**で十分 (既存の
  `coderouter doctor` と同じ美学: 依存を足すより手書きで済ませる)。
  rich はユーザから "見づらい" のフィードバックが来たら判断。
- **`/dashboard` HTML**: htmx (CDN 1 ファイル) + `tailwindcss` (CDN 1 ファイル)。
  両方ランタイム配信で Python 依存はゼロ。`hx-get="/metrics.json" hx-trigger="every 2s"`
  でほぼ終わる。React は採用しない (ビルドパイプラインが走る時点で設計方針に反する)。

##### 12.3.5.1 TUI ワイヤフレーム (`coderouter stats`)

```
┌─ coderouter stats ────────────────────────────── localhost:4040 ─┐
│ profile: coding     uptime: 1h 23m     requests: 142     tests ✔ │
├─ providers ───────────────────────────────────────────────────────┤
│ provider              att    ok%    p50ms    tok/s    last error │
│ ollama-local          98     98%    420      62.3     -          │
│ groq-free             32     94%    680      145.1    429 rate_… │
│ anthropic-sonnet      12    100%    1200     41.8     -          │
├─ fallback / gates ────────────────────────────────────────────────┤
│ fallback rate:         4.2%  (6/142)                              │
│ paid-gate blocked:     0                                          │
│ capability degraded:   3   (thinking:2  reasoning:1)              │
│ output-filter applied: 12  (strip_thinking:12)                    │
├─ recent ──────────────────────────────────────────────────────────┤
│ 14:32:01  openai    coding   ollama-local     ok       420 ms    │
│ 14:31:58  anthropic coding   ollama-local     ok       390 ms    │
│ 14:31:45  openai    coding   groq-free        FAIL     (429)     │
│ 14:31:45  openai    coding   ollama-local     ok       510 ms    │
│ 14:31:32  openai    general  ollama-local     ok       380 ms    │
└───────────────────────────────────────────────────────────────────┘
  [q]uit    [r]efresh now    [p]ause    [f] toggle failures only
```

**設計意図**:
- 4 パネルの縦割りで、「今どのプロファイルか」→「誰が健全か」→「gate の
  発火度合い」→「直近で何が起きたか」の順に視線が流れる。
- `att` (attempts) と `ok%` を並置 — 低母数の 100% と高母数の 98% を同一に
  見せない。
- `recent` は `tail -f` 相当で、失敗行は赤で強調 (curses の `A_BOLD | COLOR_RED`)。
- キーバインドは `doctor` のフィードバックを踏襲して最小限に。

##### 12.3.5.2 HTML ダッシュボードのレイアウト

ヘッダ 1 行 + 2×2 グリッド (tailwind `grid-cols-1 md:grid-cols-2 gap-4`)、
dark テーマ既定。モックは `docs/designs/v1.5-dashboard-mockup.html`
(静的データでブラウザ表示可、`/metrics.json` のフィードを模擬)。

```
┌──────────────────────────────────────────────────────────────────┐
│ CodeRouter ◆ profile: coding ◆ uptime 1h23m ◆ 142 reqs  ● healthy │
├───────────────────────────────┬──────────────────────────────────┤
│ Providers                     │ Fallback & Gates                 │
│ ─────────────────────────     │ ─────────────────────────        │
│ ● ollama-local  98/98  420ms  │  Fallback rate      4.2%         │
│ ● groq-free     30/32  680ms  │  Paid-gate blocked  0            │
│ ● anthropic-sonnet 12/12 1.2s │  Degraded           3            │
│                               │  Filters applied    12           │
├───────────────────────────────┼──────────────────────────────────┤
│ Throughput (tok/s, last 60s)  │ Recent Requests                  │
│    ╱╲    ╱╲                   │ 14:32:01 openai  ollama     420ms│
│   ╱  ╲__╱  ╲__                │ 14:31:58 anthro  ollama     390ms│
│  ╱           ╲_               │ 14:31:45 openai  groq       FAIL │
│                               │ 14:31:45 openai  ollama     510ms│
│                               │ 14:31:32 openai  ollama     380ms│
└───────────────────────────────┴──────────────────────────────────┘
```

**設計意図**:
- 2×2 で panel あたりの情報量を TUI と揃える。モバイル幅では 1 列縦積み
  (`md:` ブレークポイントでグリッド化)。
- Provider 行頭の `●` は health 状態の色点 (green/yellow/red) で、`ok%`
  の数字を見る前に健康度が視線に入る。
- Throughput スパークラインは SVG 手書き (d3 も chart.js も引かない、
  `<polyline>` 1 本 + polling で値を pushShift するだけ)。
- htmx の `hx-get="/metrics.json" hx-trigger="every 2s" hx-swap="none"` と
  アラート JS で値だけ書き換える — サーバ側テンプレ描画は初期 HTML の
  1 回だけ、以降は JSON ポーリング (SSE は v1.5 では見送り、TTFB より
  "実装量" を優先)。

#### 12.3.6 増分出荷パス — v1.5 A/B/C/D

**v1.5-A — Collector + `/metrics.json`** (最小実行単位、これだけで "見える" に
到達する)

- `coderouter/metrics/collector.py` 追加、`logging.Handler` サブクラス
- `coderouter/ingress/metrics_routes.py` で `GET /metrics.json` 配信
- 既存ログに触らない (回帰ゼロ)、test は既存ログイベントを fire させた後
  `/metrics.json` の出力を assert する統合テストで足りる

**v1.5-B — Prometheus exposition + JSONL persistence**

- 同じ `MetricsCollector` から text/plain で Prometheus 形式を出す
  (`format_prometheus()` ~30 行)
- `CODEROUTER_EVENTS_PATH` ENV でミラー先を有効化
- E2E: `promtool check metrics` で exposition の妥当性を担保 (CI に依存を
  足さずローカル開発者の任意実行で OK)

**v1.5-C — CLI TUI `coderouter stats`**

- `coderouter/cli/stats.py` を `__main__.py` のサブコマンドに追加
- curses ベース、1 秒 refresh、4 パネル構成 (現在プロファイル / プロバイダ別
  成功率 / tok/s / 直近 10 リクエスト)

**v1.5-D — `/dashboard` HTML** (任意、完全版)

- htmx + tailwind CDN、single HTML
- v1.5-A/B/C が先に着地していれば付加価値が明確、先行着地の必要性は低い

**優先順**: A は v1.5 本体、B はほぼ同梱、C と D は feedback driven で順序を
入れ替えてよい。CLI TUI は `doctor` の延長として"開発中のローカル観察用"、
HTML は "共有・デモ用" という役割分担。

#### 12.3.7 決めないこと (non-goals)

- 認証: `/metrics` は 127.0.0.1 バインドで運用、認証は v1.5 スコープ外
  (reverse proxy で挟む前提、README に 1 行明記する)
- 長期保存 DB: SQLite / duckdb はいずれも依存追加 + スキーマメンテコストが
  JSONL と釣り合わない。Prometheus 側の remote_write で引き取ってもらう
- メトリクス名の `coderouter_` prefix: Prometheus 慣習に合わせて v1.5-B で
  一括付与する (上表は記述簡素化のため prefix 省略)

#### 12.3.8 リスクと退路

- **ログ handler で収集する設計のリスク**: logger.info を呼ばずに
  "数字だけ増やす" ホットパスが将来出ると漏れる。退路 = `MetricsCollector`
  に `record(event, **kv)` 公開メソッドを足して直接フィードも許容する。
  v1.5 初期は tap 専念で十分 (既存ログだけで要件達成)。
- **in-memory が再起動で消える**: ダッシュボードとしては OK だが "落ちた後
  の調査" には JSONL が要る。v1.5-B を "任意" ではなく "推奨設定" として
  README に書く。
- **Prometheus 形式の手書きリスク**: exposition format は CNCF 標準で安定、
  `# HELP` / `# TYPE` / `name{labels} value` の 3 行パターンのみ。手書きで
  30 行、テストは `promtool` で担保。依存を足して得るほどの複雑さではない。

---

## 13. v2.0 — プラグイン / MCP / OpenClaw 連携

### 13.1 スコープ

- プラグインで provider 追加可能 (e.g. `pip install coderouter-provider-foo`)
- MCP server としても動く (Anthropic MCP 仕様準拠)
- OpenClaw (将来エコシステム) との連携窓口
- Web UI で設定編集

### 13.2 詳細タスク

- [ ] プラグイン仕様策定 (entry_points or 動的ロード)
- [ ] MCP サーバ実装
- [ ] Web UI で `providers.yaml` / `profiles.yaml` を GUI 編集
- [ ] テスト用ダミー provider プラグインの公開

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
  - [ ] `coderouter doctor --deps` で依存数と outbound を可視化 (v1.1 で本実装)
- [ ] コミュニティ
  - [ ] CONTRIBUTING.md
  - [ ] ISSUE / PR テンプレート
  - [ ] note 記事用ネタ収集 (実測値、ハマりどころ)

---

## 15. やらないこと (Out of Scope, 少なくとも v2.0 まで)

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

v0.1 から v0.5 までの実装項目を時系列で保持するログ。完了済みは参照資料 (retro / CHANGELOG) への orient 用、未着手は v0.6 以降のバックログの primary source。
リリース単位の要約は §6.2、各マイルストーンの DoD は §7 / §8 / §9 を参照。以下は item-level の履歴。

### v0.3 実装状況 (2026-04-20 時点)

| ID | 項目 | 状態 | 概要 |
| --- | --- | --- | --- |
| v0.3-A | Tool-call repair (non-stream) | ✅ | `translation/tool_repair.py` 新設。balanced-brace scanner + fenced JSON 検出 + allowlist。`to_anthropic_response(..., allowed_tool_names=)` で text 埋め込み JSON を tool_use block に昇格。13 ユニットテスト + 3 連携テスト。 |
| v0.3-B | Mid-stream fallback guard | ✅ | `MidStreamError` を新設し `FallbackEngine.stream()` が first-byte 送出後の AdapterError をこれで包む。`_anthropic_sse_iterator` が捕まえて `event: error` / `type: api_error` を emit（「どの provider も開始できない」overloaded_error と区別）。2 + 1 テスト追加。 |
| v0.3-C | Usage 集計 | ✅ | `_StreamState` に emitted_chars 累積 + upstream_input/output_tokens を持たせ、上流 usage が来ればそれを、来なければ `(chars + 3) // 4` 概算を `message_delta.usage.output_tokens` に入れる。OpenAI-compat adapter が `stream_options.include_usage=true` を自動付与（`extra_body` で override 可）。5 + 2 テスト追加。 |
| v0.3-D | Tool-call repair (streaming) | ✅ | `tools` を宣言した streaming リクエストは内部で `stream=false` に downgrade し、v0.3-A の repair を通してから `synthesize_anthropic_stream_from_response` で spec 準拠の SSE イベントに再構築。tool-less streaming は従来通り real streaming。3 synthesizer テスト + 3 ingress テスト。 |
| v0.3-E | 実機 Claude Code 再検証 | ✅ | Ollama + qwen2.5-coder:14b + Claude Code で疎通。(a) tool なし text streaming は real path で `usage` 両パス (upstream authoritative / char estimate fallback) を実機確認、(b) tool 付き streaming は downgrade path で `tool_use` block が Claude Code UI に正しく描画、(c) mid-stream guard は unit test でカバー（実機 pkill timing は困難のため optional）。疎通中に `Message.content=None` クラッシュを発見 → `coderouter/adapters/base.py` で型拡張 + regression test 追加。 |
| v0.3-F | Commit + tag v0.3.0 | ✅ | CHANGELOG 追記 → commit → `git tag v0.3.0`。main に `5261dae` として push 済み。|

テスト合計: 87 passed (v0.2 完了時点 54 → v0.3 で +33。v0.3-E 実機疎通中に発見した `Message.content=None` クラッシュの regression test 1 件を含む)。
ruff: v0.3 で導入した lint issue は 0（残る 11 件はすべて v0.1/v0.2 由来の既知事項）。

### v0.3.x / v0.4 / v0.5 実装ログ (時系列)

4. [x] **Anthropic native adapter (v0.3.x-1)** — `ProviderConfig.kind: "anthropic"` を追加し、`AnthropicAdapter` / `FallbackEngine.generate_anthropic` / `stream_anthropic` 経由で上流が本物の Claude のとき翻訳を通さず passthrough。
   - 翻訳コストを省き、Anthropic 固有の cache_control / thinking ブロックをそのまま活用できるようにする
   - v0.3-D downgrade 実装を ingress から engine に移設し、native provider は downgrade を完全 bypass。混在 chain（native → openai_compat のフォールバック）もサポート
   - tests +23 件 (`test_adapter_anthropic.py` 11 件 / `test_fallback_anthropic.py` 12 件) → 合計 **110 件**
   - 詳細は CHANGELOG.md `[v0.3.x-1]` セクション
4a. [x] **ChatRequest → AnthropicRequest 逆翻訳 (v0.4-A)** — v0.3.x-1 で out of scope としていた方向（OpenAI ingress → `kind: anthropic` provider）を実装。`AnthropicAdapter.generate` / `.stream` が内部で `to_anthropic_request` → `generate_anthropic` / `stream_anthropic` → `to_chat_response` / `stream_anthropic_to_chat_chunks` を呼ぶ逆変換パスを持つ。
   - `/v1/chat/completions` → `kind: anthropic` provider が対称的に到達可能に
   - `role: "system"` → top-level `system` フィールド、連続する `role: "tool"` → 1 つの user turn に複数 `tool_result` block を batch、`tool_calls` ↔ `tool_use` block、`tool_choice` / tools / stop_reason / usage の双方向マップ
   - Anthropic `event: error` → `AdapterError(retryable=False)` で既存 mid-stream guard に接続
   - engine 側はコード変更なし（polymorphic dispatch が効く）
   - tests +37 件 (`test_translation_reverse.py` 31 件新設 / `test_adapter_anthropic.py` +2 net / `test_fallback_anthropic.py` +4) → 合計 **147 件**
   - 詳細は CHANGELOG.md `[v0.4-A]` セクション
5. [x] **Claude Code 向け profile サンプル** を README と `examples/providers.yaml` に追加 (v0.4-A docs pass, 2026-04-20)。
   - `claude-code` profile: 7b → 14b → 2 free → paid (openrouter-claude)。14B timeout は 300s で Claude Code の 15-20K token system prompt 前提
   - `claude-code-direct` profile (NEW): 最終 paid を `anthropic-direct` (kind: anthropic) に差し替えたバリアント。cache_control / thinking ブロックを無傷で扱える
   - README「Use it with Claude Code」に profile の YAML snippet を明示
6. [x] **OpenRouter 無料モデル一覧の棚卸** (v0.4-B, 2026-04-20) — `/api/v1/models` で全 342 モデルを検証。
   - `qwen/qwen3-coder:free` (262K ctx / tools) は健在、primary として継続
   - `deepseek/deepseek-r1:free` は free roster から消失 → `openai/gpt-oss-120b:free` に 1:1 差し替え (ベンダ分散 + tools + 131K ctx + agentic/production 設計)
   - provider 名を `openrouter-deepseek-free` → `openrouter-gpt-oss-free` に改名、4 profile チェーン全て更新
   - ヘッダコメントに「DIFFERENT vendor families を選ぶこと」「2026-04-20 時点の roster 検証結果」を明記
7. [x] **README 更新** — v0.3 / v0.4-A の実装状況を Quickstart / Status セクションに反映 (2026-04-20)。
   - Status を `v0.4-A — Symmetric OpenAI ⇄ Anthropic routing` に
   - 147 tests green / native adapter / 逆翻訳 (v0.4-A) を箇条書きに追加
   - Coming next から v0.3.x 完了項目を除去
8. [x] **v0.4 実機疎通確認** (2026-04-20) — `/tmp/cr-verify.yaml` に検証用 profile を用意し host から spot check。
   - `/v1/chat/completions` → `openrouter-gpt-oss-free`: 200 OK / pong 応答 / `reasoning` フィールドが追加で返る発見あり (将来 reasoning-strip 層で扱う)
   - `/v1/chat/completions` → `anthropic-direct` (tool_choice=auto): `tool_calls[0].id = "toolu_..."` で逆翻訳経路確定。system lift / tools→input_schema / tool_use→tool_calls 全段動作
   - streaming 版: OpenAI shape の delta 列 + trailing usage chunk + [DONE] で完全再構築、Anthropic event 漏れなし
   - `/v1/messages` → `anthropic-direct` (native passthrough): `cache_control: {type: "ephemeral"}` が API まで届き、call 1 で `cache_creation_input_tokens: 1321`、call 2 で `cache_read_input_tokens: 1321` を確認 → cache_control ロスレス運搬を数値で証明
   - server log の `native_anthropic: true` フラグが Anthropic ingress 経由でのみ立つことも確認 → engine の分岐設計どおり
9. [x] **v0.4-D: `anthropic-beta` header passthrough** (2026-04-20) — Claude Code → `anthropic-direct` が 400 Bad Gateway で落ちる件の修正。
   - 原因: Claude Code は `context_management` body field を送る際に `anthropic-beta: context-management-2025-06-27` header を添える。CodeRouter はこの header を転送していなかったため Anthropic が `"context_management: Extra inputs are not permitted"` で拒否
   - 修正: `AnthropicRequest.anthropic_beta: str | None = Field(default=None, exclude=True)` を追加 (body にはリークさせない header-hop 用 stash)。Anthropic ingress が `Header(alias="anthropic-beta")` で抽出して request に積む。native adapter の `_headers(request)` が値を `api.anthropic.com` へ verbatim forward
   - 診断性能強化: `fallback.py` の `provider-failed` / `provider-failed-midstream` ログ 6 箇所に `"error": str(exc)[:500]` を追加 → 400 の body を構造化ログで取れるように。これが `context_management` エラーの特定を可能にした
   - tests +6 件 (`test_adapter_anthropic.py` +4 / `test_ingress_anthropic.py` +2) → 合計 **153 件**
10. [x] **v0.4 retrospective + docs pass** (2026-04-20) — v0.4-A/B/D を一気通貫で振り返り、README の stale 箇所 (tests 件数 / deepseek-r1 / "Coming next") を全て reconcile。新設 2 セクション「Choosing `kind: openai_compat` vs `kind: anthropic`」「Troubleshooting」。詳細なナラティブは [`docs/retrospectives/v0.4.md`](./docs/retrospectives/v0.4.md) — v0.5 計画の primary source。
11. [x] **v0.5-A: thinking capability gate** (2026-04-20) — v0.4 retro §Follow-ons で筆頭に挙げた capability gate の最初のピース。`coderouter/routing/capability.py` 新設 (純粋関数 3 つ: `provider_supports_thinking` / `anthropic_request_requires_thinking` / `strip_thinking`)、`Capabilities.thinking: bool` 追加、`FallbackEngine` の anthropic-shaped path 2 本で `_resolve_anthropic_chain` (capable/degraded stable-sort) + `strip_thinking` + 構造化ログ `capability-degraded`。tests +36 (`test_capability.py` +27 / `test_fallback_thinking.py` +9) → 合計 **189 件**。これで「v0.4-D で model を手動で Sonnet 4.5 → 4.6 に差し替える必要があった」症状が adapter 層で自動解決される。v0.5-B (cache_control normalization) を次に拾う。
12. [x] **v0.5-B: cache_control observability** (2026-04-20) — v0.5-A の gate 基盤に 2 関数 + 1 helper 追加 (`provider_supports_cache_control` / `anthropic_request_has_cache_control` / `_block_has_cache_control`)。thinking と異なり cache_control は silent drop 系 (openai_compat 経由で 400 にならず translation で落ちる) なので、**observability-only**: `FallbackEngine.generate_anthropic` / `stream_anthropic` のループ内で openai_compat provider に cache_control 付きリクエストを渡す際、構造化ログ `capability-degraded` (`reason: "translation-lossy"`, `dropped: ["cache_control"]`) を発火。chain reorder も strip もしない (ユーザーの provider 順序 = latency/cost 意図を尊重、strip は既存 translation が既に実施)。escape hatch: `capabilities.prompt_cache: true` で openai_compat provider を capable に昇格 → ログ抑制。1024-token minimum の footgun は capability.py docstring に明記。tests +21 (`test_capability.py` +13 / `test_fallback_cache_control.py` 新規 +8) → 合計 **210 件**。
13. [x] **v0.5-C: OpenRouter `reasoning` field passive strip** (2026-04-20) — v0.4-B 棚卸で実機検出した非標準フィールド (`openai/gpt-oss-120b:free` が `choice.message.reasoning` / `choice.delta.reasoning` を返す) の adapter-boundary 処理。`Capabilities.reasoning_passthrough: bool = False` 追加。`openai_compat.py` に `_strip_reasoning_field(choices, delta_key)` を実装、`generate()` / `stream()` の出口で適用。strip 発生時に構造化ログ `capability-degraded` (`reason: "non-standard-field"`, `dropped: ["reasoning"]`) を発火。streaming では「最初の 1 回」だけログ (local `reasoning_logged` flag, chunk 毎の連投防止)。`reasoning_passthrough: true` で opt-out。tests +15 (`test_reasoning_strip.py` 新規: helper unit 7 + generate 4 + stream 4) → 合計 **225 件**。
14. [x] **v0.5 retrospective + `v0.5.0` tag proposal** (2026-04-20) — v0.5-A/B/C trio を一気通貫で振り返る narrative。[`docs/retrospectives/v0.5.md`](./docs/retrospectives/v0.5.md) 新設。gate 設計の 3×6 マトリクス (failure mode / detection location / action / escape hatch / log message / log reason) を確立、v0.6+ が踏襲 (または明示的に逸脱) すべき shape として文書化。Follow-ons に v0.5-D (OpenRouter roster cron) のほか、実機再 verify・ヒューリスティック表のメンテ signal・`capability-degraded` payload の schema 化を追加。trio (`ff7ca27` / `e8803da` / `e20fb36`) を `v0.5.0` として tag する提案を併記 → 8444f6b で commit + push 済み。
15. [x] **v0.5 実機再 verify: runner + evidence doc** (2026-04-20) — retro §Follow-ons 筆頭を消化。unit test が httpx-mock だけで回している `capability-degraded` contract を live traffic で叩き直した。成果物: (a) `examples/providers.yaml` に `verify-gpt-oss` プロファイル追加 (openai_compat-only で 3 gate すべてが確実に発火する)、(b) [`scripts/verify_v0_5.sh`](./scripts/verify_v0_5.sh) が 3 シナリオを curl で連射 + server log slice を per-scenario ディレクトリに保存 + pass/fail 判定 + markdown report 生成 (macOS /bin/bash 3.2 compat)、(c) [`docs/retrospectives/v0.5-verify.md`](./docs/retrospectives/v0.5-verify.md) にナラティブ + 実測 evidence。実機 run (14:33 JST) 結果: **3/3 PASS**。副次観察として、A/B の 1 call が request-side gate (fallback.py) と response-side strip (adapter) の `capability-degraded` ログを両方吐いていることを確認 → v0.5-A/B と v0.5-C が独立軸で composable である最初の live 証跡。追加で、最初の試行時に OPENROUTER_API_KEY 未設定で 401 が返ったとき、v0.4-D の log enrichment (`error` field に upstream body) のおかげで診断 1 分未満 → log 投資の二度目のペイオフ。
16. [x] **v1.0 実機再 verify: 3-scenario runner + retro doc** (2026-04-20) — v1.0-A/B/C の 3 sub-release の Follow-ons を 1 つに束ねて消化 (v0.5-verify が 1 doc で 3 gate を束ねた同じ pattern)。成果物: (a) `examples/providers.yaml` に `verify-ollama-bare` / `verify-ollama-tuned` provider pair + `verify-v1-bare` / `verify-v1-tuned` profile pair 追加 — 同一モデル (qwen2.5-coder:7b) / 同一 port / declaration fields (`output_filters`, `extra_body.options.{num_ctx,num_predict}`) のみ差分、という narrow experimental control 設計。(b) [`scripts/verify_v1_0.sh`](./scripts/verify_v1_0.sh) が 3 scenario を fire (A: CodeRouter server に curl、B+C: `coderouter doctor --check-model` CLI を bare + tuned 両方叩く)、各 scenario は bare (症状発生) + tuned (症状消失) の delta で PASS 判定する対称設計。doctor 出力 parsing は `[N/6] <probe> …… [NEEDS TUNING]` badge + patch 本体の `num_ctx: 32768` / `num_predict: 4096` リテラルを grep — patch リテラルが operator の copy-paste target なので、形式変更時に verify が FAIL する lockstep を意図的に仕込んだ。(c) [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md) に narrative (scenario 表 / How to run / Evidence / What to look for / Failure modes / What this run proved / Follow-ons) を v0.5-verify.md と同じ skeleton で執筆。実機 run 23:23 JST で **3/3 PASS** — Ollama 0.20.5 + qwen2.5-coder:7b、tuned 側は B/C 両 probe が `[OK]` に flip、A 側は `output-filter-applied` log + `<think>`-free content を取得。Scenario A bare の advisory 扱い (qwen の stochastic `<think>` emission 依存) に加え、B/C bare も advisory に再定義: Ollama 0.20.5 が request-time `options.num_ctx` / `options.num_predict` を silent override する build であることが実機で判明 — `verify-ollama-bare` に pathological 値 (`num_ctx: 2048` / `num_predict: 16`) を宣言しても上流が無視するため、canary が echo される / `finish_reason=length` が出ない / 応答が 40 文字を超える、という現象になった。この対処として (i) `scripts/verify_v1_0.sh` の B/C に **ADVISORY branch** を追加 (`bare exit + num_ctx verdict present + NEEDS_TUNING 不在` を "symptom could not be induced" として note を出し pass=true を維持)、(ii) `coderouter/doctor.py` の num_ctx probe canary-echoed 分岐を 2-branch → 3-branch に split — 既存の "`declared is None`" case と新設の "`declared < threshold` but echoed" case を分離し、後者では "declared=N (below threshold) — Ollama may have silently ignored; check `ollama ps` / consider `ollama stop`" と診断的に出力。doctor は unit test で probe correctness を保証する層、verify は patch-default 値が実機 threshold を満たすことを証明する層、という責務再確認。実機 evidence は [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md) の `## Evidence` に完全貼付、advisory-bare / hard-tuned asymmetry の rationale は scenario 表 header に明文化済。
18. [x] **v1.0.1 — `CodeRouterError` root 例外 + docstring 91.2% + mypy strict 0 errors** (2026-04-21) — v1.0.0 umbrella 後、残っていた 3 つの足回りを 1 release で消化。
   - (a) `coderouter/errors.py` に root `CodeRouterError(Exception)` class を新設、既存 3 leaf (`AdapterError` / `NoProvidersAvailableError` / `MidStreamError`) の基底を `Exception` → `CodeRouterError` に差し替え、`coderouter/__init__.py` で re-export。downstream integrator が `except CodeRouterError` 一文で router-side failure を catch 可能に。import パスは既存位置維持 (非破壊)。
   - (b) docstring 網羅率 75.6% → 91.2% (`interrogate coderouter` 基準、target 90%) — public API 系ファイル (adapters / routing / ingress / translation model / logging / output_filters) 全て 100%、残 21 は真の internal / closure / stream-state plumbing のみ (`cli._build_parser` / `doctor` private helper / `_StreamState` 内部 helper 等)
   - (c) mypy `--strict` 0 errors を確認 — 10 errors を ingress routes の `response_model=None` + `AsyncIterator[str]` import + fallback.py の `isinstance(adapter, AnthropicAdapter)` narrowing + `StreamChunk.usage` 型宣言で解消
   - tests 453 → **457** (+4、`tests/test_errors.py` 新設で 3 leaf の継承 invariant + `AdapterError` raise→catch smoke test を lock)
   - Runtime deps 5 → 5 (`interrogate` は dev-only)
   - semver 上 patch-level — 実質の public API 追加は `CodeRouterError` 1 つのみで、既存 CI gate / 実機 verify が全 pass
   - retrospective は skip (hygiene pass で narrative 量が薄いため)、次 retrospective の冒頭で 1 行 mention して系譜を保つ
19. [x] **v1.5.0 — Observability pillar (umbrella)** (2026-04-22) — §12 "計測ダッシュボード" を丸ごと 1 minor で出荷。**6 sub-release の横並び**: (A) `MetricsCollector` logging.Handler + `GET /metrics.json` — 既存 structured log stream を in-memory ring (counters / providers / recent 50 events / startup snapshot) に集約、コード変更なしで既存 log に乗っかる hook-only 設計。(B) Prometheus text exposition (`GET /metrics`、`coderouter_*` prefix、gauge + counter 混成、ラベルなし scalar) + `$CODEROUTER_EVENTS_PATH` JSONL mirror (env-gated side-effect、collector snapshot から完全独立、`JsonLineFormatter` と同一行シェイプ)。(C) `coderouter stats` CLI TUI (stdlib `curses` + `urllib` のみ、5 パネル: Providers / Fallback & Gates / Requests/min sparkline / Recent Events / Usage Mix) — pure data+render layer を driver から分離、`--once` mode で TTY 不在環境 (CI / pipe / `demo_traffic.sh` banner) で単発レンダー可能。(D) `/dashboard` HTML 1 ページ (tailwind CDN + fetch polling 2 秒間隔、dashboard_routes.py に single-page HTML を埋め込み、htmx を避けて `setInterval` + `fetch`、5-dep policy 維持)。(E) `display_timezone` config フィールド (`providers.yaml` top-level、IANA zone 名、未設定時 UTC) — 集約された UTC 時刻は保持したまま **表示層だけ**変換 (CLI TUI の `TzFormatter` cache / HTML の `Intl.DateTimeFormat`)、`/metrics.json` の `config.display_timezone` 経由で JS 側に伝搬。(F) `scripts/demo_traffic.sh` — weighted scenario picker (normal 4/10 / stream 3/10 / burst+idle 2/10 / fallback 1/10、paid-gate every 8th tick)、`--duration` / `--serve` / `--dry-run` / banner + expected-count table、macOS /bin/bash 3.2 互換 (heredoc-inside-`$()` 排除 → python3 `-c "$VAR"` / bare `wait` → `wait_pids` PID-tracked、詳細は task #46)。加えて README に live dashboard スクショを追加 (`docs/assets/dashboard-demo.png` / README.md L43 付近 + README.ja.md の鏡像、キャプションは「このダッシュボードを見ると何の問いに即答できるか」= "モデルが動作してる / 利用されてる / 切り替わった" を明示)。計 **457 → 527 tests green (+70、+15.3%)**、Runtime deps 5 → 5 (curses + urllib + `datetime.zoneinfo` は stdlib、tailwind は CDN、Prometheus 形式は自前文字列生成で SDK 依存ゼロ)。semver 上: `v1.0.1 → v1.5.0` で §11 (旧 v1.1 = 配布 / launcher / doctor) をスキップしたため、§11 ヘッダは **v1.6** にリラベル済み (`v1.1` 番号は欠番、SemVer 連続性は保つが内部コードネームの連続性は諦めた)。詳細は CHANGELOG.md `[v1.5.0]`。retrospective (`docs/retrospectives/v1.5.md`) は別途執筆予定。
17. [x] **v1.0.0 umbrella tag + retrospective** (2026-04-20) — v1.0-A / v1.0-B / v1.0-C を 1 つの `v1.0.0` umbrella tag に束ねる提案 + narrative layer 執筆。成果物: (a) [`docs/retrospectives/v1.0.md`](./docs/retrospectives/v1.0.md) — "The observation loop, closed"、v0.7.md の skeleton (scope-at-a-glance / what happened / design through-lines / what worked / what was sharp / follow-ons / numbers / how to read this) を踏襲。3 サブリリース narrative に加えて v1.0-verify companion の位置付け、symptom-orthogonality heuristic for probe ordering の初明文化、transformation + probe same-release principle の track record 化、bare/tuned delta assertion pattern の 2nd instance 確認。(b) CHANGELOG.md に `[v1.0.0]` umbrella entry を v1.0-C の前に prepend — 4 design through-lines (transformation+probe 同梱 / symptom-orthogonality ordering / stateful boundary scrubber / Ollama-shape signals abstraction / bare-tuned delta pattern) を記述、umbrella-level follow-ons 8 項目。(c) plan.md の status line を v1.0-C → v1.0.0 umbrella に pivot、release history 概要表に v1.0.0 行追加、§6.2 詳細表に v1.0.0 行 + umbrella 説明段落追加。Follow-ons のうち最も loud なものは "plan.md §10.1 original scope のうち v1.0.0 で deliver されたのは output-cleaning のみ、残り 5 (tool-call 変換層 / `recover_garbled_tool_json` / Code Mode / prompt cache / 14-case 回帰 / チューニング既定値) は v1.1+ に明示 re-scope 推奨" — v1.0.0 は observation-loop-closed accomplishment の tag であり、claude-code-local feature-completeness の tag ではない、という scope 再確認を retrospective で明示した。

### 低優先 (v0.6 以降で拾う)

- [ ] **当初 v0.5 スコープの未着手分** (§9.3 参照) — `profiles.yaml` schema / ~~`--mode` CLI~~ (v0.6-A 完了) / ~~宣言的 `ALLOW_PAID` gate~~ (v0.6-C 完了) / ~~プロファイル別 timeout~~ (v0.6-B 完了、retry は繰延) / ~~mode ヘッダ優先ルーティング~~ (v0.6-D 完了) / capability mismatch 時の chain 完全スキップ (vision 系は v1.0+)。
- [x] ~~**v0.5-D: OpenRouter roster 週次 cron diff**~~ — **完了 (2026-04-20)**。`scripts/openrouter_roster_diff.py` を standalone cron として追加 (`stdlib + httpx` のみ、`coderouter` パッケージ非依存)。`/api/v1/models` → free-tier 抽出 (pricing 基準、`:free` suffix は hint) → 前回 `docs/openrouter-roster/latest.json` と diff → 差分を `docs/openrouter-roster/CHANGES.md` に newest-first で prepend。first-run baseline は silent (snapshot だけ書いて CHANGES.md は触らない)、2nd run から実トラッキング。`docs/openrouter-roster/README.md` に runbook + triage cheatsheet + future extension 候補。`tests/test_openrouter_roster_diff.py` +24 tests (parse / filter / diff / format / orchestration / main exit code)。週次 cadence は schedule skill 登録 or 手動 (どちらも idempotent) を README で両論併記。
- [ ] **Anthropic ヒューリスティック表のメンテ signal** (retro §Follow-ons) — (a) 週次 `/v1/models` diff、または (b) 未知モデルを検出したら warn ログ。(b) は既存 gate 計算から一関数で取れる。
- [x] ~~**`capability-degraded` payload の schema 化**~~ — **v0.5.1 A-1 で完了 (2026-04-20)**。`coderouter/logging.py` に `CapabilityDegradedReason` (Literal 3 種) / `CapabilityDegradedPayload` (TypedDict) / `log_capability_degraded` chokepoint helper を追加。routing ↔ adapter の import cycle を避けるため実体は `logging.py` (leaf)、`routing/capability.py` は re-export のみ。`tests/test_capability_degraded_payload.py` +9 tests。
- [x] ~~**401 等 non-retryable error の "全 provider 同一失敗" 警告**~~ — **v0.5.1 A-3 で完了 (2026-04-20)**。`coderouter/routing/fallback.py` :: `_warn_if_uniform_auth_failure` を 4 path (generate / stream / generate_anthropic / stream_anthropic) の raise 直前に挿入。auth 限定 (401/403) スコープで、全 attempt が同じ status + 全て non-retryable の時だけ `chain-uniform-auth-failure` warn を hint 付きで吐く。`NoProvidersAvailableError` 例外形は非破壊。`tests/test_fallback_misconfig_warn.py` +9 tests。
- [x] ~~**streaming verify (D- scenario)**~~ — **v0.5.1 A-2 で完了 (2026-04-20)**。`scripts/verify_v0_5.sh` に `run_scenario_streaming` (curl -N + SSE 解析) + D シナリオを追加。HTTP 2xx / `capability-degraded` 1 発ちょうど / 全 chunk で `delta.reasoning` 不在 の 3 assertion を自動化。macOS bash 3.2 互換 (indexed array / 明示配列 index)。`docs/retrospectives/v0.5-verify.md` の scenario 表 + Follow-ons を更新済。
- [ ] 14 ケース回帰テスト / Code Mode は §10 (v1.0) スコープ (出力クリーニングは **v1.0-A で完了** / doctor `num_ctx` probe 直接検出は **v1.0-B で完了** / doctor streaming-path probe (output-side truncation 直接検出) は **v1.0-C で完了**、§10.2 参照)
- [x] ~~計測は §12 (v1.5)~~ — **v1.5.0 で完了 (2026-04-22)** (Observability pillar、§12 / [`CHANGELOG.md` `[v1.5.0]`](./CHANGELOG.md))
- [x] ~~auto_router (task-aware routing) は §11 (v1.6)~~ — **v1.6.0 で完了 (2026-04-22)** (`default_profile: auto` + 4-variant `RuleMatcher` + bundled ruleset、§11.3)
- [x] ~~PyPI 配布~~ — **v1.7-A で完了 (2026-04-25)** (`coderouter-cli` として publish、§11.B.3 / [`CHANGELOG.md` `[v1.7.0]`](./CHANGELOG.md))
- [ ] launcher / setup.sh / `doctor --network` / 起動時アップデートチェック / `claude_code_suitability` hint は §11.B (v1.7-B 以降)
- [ ] プラグイン / MCP / Web UI は §13 (v2.0)

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
| `.env` / `models.yaml` / `install.sh` | §11 |
| README キャッチコピー | §1.3, §17 |
| 「数字で見せる」 | §12 |
| 名前案 ClawRoute / CodeRouter | §17 |

## Appendix B — claude-code-local からの抽出表

| claude-code-local 機能 | plan.md での反映先 | 優先度 |
| --- | --- | --- |
| Anthropic API ネイティブ ingress | §8 (v0.2) | ★★★ |
| tool_call 変換 + 壊れた JSON 修復 | §10 (v1.0) | ★★★ |
| Code Mode (harness slim) | §10 (v1.0) | ★★★ |
| プロンプトキャッシュ再利用 | §10 (v1.0) | ★★ |
| 出力クリーニング | §10 (v1.0-A ✅) | ★★ |
| tool-call チューニング既定値 | §10 (v1.0) | ★★ |
| 14ケース回帰テスト | §10 (v1.0) | ★★ |
| ワンクリック launcher | §11 (v1.1) | ★ |
| ZERO outbound monitor (`doctor`) | §11 (v1.1) | ★ |
| 計測ダッシュボード (tok/s 等) | §12 (v1.5) | ★ |

---

*このplan.mdは生きたドキュメントです。実装中に判明した知見でガンガン書き換えてください。*

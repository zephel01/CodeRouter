# Changelog

All notable changes to CodeRouter are recorded here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/),
versioning follows [SemVer](https://semver.org/).

---

## [v1.10.1] — 2026-05-04 (Patch — tool-aware auto routing + Raspberry Pi starter)

**Theme: 「ローカル小型モデルでは tool calling できないので tool-laden な request だけクラウドに逃がしたい」というユースケース (OpenClaw + Pi 8GB シナリオ) を declarative に解決。** v1.10.0 で feature complete を宣言した auto_router の 6 matcher を 7 matcher に拡張、`has_tools` を追加して「tools[] を宣言したリクエストか否か」で profile を分岐できるように。併せて Raspberry Pi 8GB 向けの starter YAML (`examples/providers.raspberrypi.yaml`) を同梱、SBC 上で OpenClaw / Claude Code 互換 agent を回すユーザーが yaml 1 個 copy するだけで動く状態にした。

含まれる出荷 2 件:

| # | sub-release | テーマ | LOC | tests |
|---|---|---|---|---|
| 1 | **has_tools matcher** | `RuleMatcher.has_tools` 7 番目 matcher 追加、OpenAI/Anthropic `tools[]` + OpenAI legacy `functions[]` を一括認識 (OpenClaw + Pi 由来) | ~80 | +7 |
| 2 | **Raspberry Pi starter** | `examples/providers.raspberrypi.yaml` 新規、Ollama 小型モデル (≤4B) + OpenRouter free 系 + `has_tools` ベースの tool-aware profile 振り分け | YAML のみ | (loader 検証で +0 直接、既存 parametric test に乗る) |

- Tests: 871 → **878** (+7、has_tools matcher の 6 シナリオ + `has_tools: false` の "set 扱いだがマッチしない" 安全網テスト)
- Runtime deps: 5 → 5 (**34 sub-release 連続据え置き**)
- Backward compat: 完全互換、既存 yaml / API / log payload schema 完全に同じ、新フィールド (`has_tools`) を使わない deployment は挙動完全一致
- pyproject version: 1.10.0 → 1.10.1

### Migration

不要。**v1.10.0 からの自然なアップグレード**:

- `coderouter` コマンド名 / Python import 名 / providers.yaml の format / env 変数 / ingress URL すべて完全に同じ
- 既存 `auto_router.rules[]` は何も変わらない、`has_tools` matcher を使い始めるには yaml に 1 行足すだけ
- v1.10.0 で v1.6 系 auto_router を「6 matcher で feature complete」と宣言した直後の追加だが、同じ宣言型 framework の延長線で構造変更なし — 「7 matcher で改めて feature complete」と読み替えて差し支えない

### Out of scope (v1.11 以降)

- **Provider capability gate for tools** — `capabilities.tools=false` を fallback chain の skip ゲートとして機能させる案。本 patch は profile レベルで振り分ける方針 (router で chain を切り替える) で `has_tools` matcher を採用、provider レベルの skip ゲートは別 issue。CodeRouter の chain semantics (順次フォールバック + downgrade) の互換性検討が必要なため、必要性が確認できてから着手。
- **小型ローカルモデルの tool-call repair 強化** — 現状 `tool_repair.py` は `<tool_call>{...}</tool_call>` ラッパ形式の救済を行うが、1-4B モデルが返す自由形式の text からの推測救済は別領域 (`tool_emulation`)。プロンプトテンプレ書き換えで誘導する手段もあり、設計検討は v2.0 後送り。

### Files touched

```
A  examples/providers.raspberrypi.yaml
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/routing/auto_router.py
M  pyproject.toml
M  tests/test_auto_router.py
```

---

### has_tools matcher (OpenClaw + Raspberry Pi 由来)

**Theme: tools[] を宣言したリクエストだけクラウドに振り分け、ローカル小型モデルは tool 不要の素朴な chat に専念させる。** Raspberry Pi 8GB / Jetson Nano クラスの SBC で OpenClaw 等の tool-aware agent を動かしたい時、CPU 推論で実用域に入る Ollama モデル (≤4B) は tool calling が苦手 (`finish_reason: tool_calls` を返さない / 引数 JSON が壊れる / 自由形式 text に bury される) で、結果として agent 側からは「tool 呼び出しが起きてない」状態になる。`auto_router.rules[].if.has_tools` を 7 番目の matcher として追加することで、profile レベルで「tools あり → クラウド (Qwen3-Coder/gpt-oss/Gemini-Flash の OpenRouter free)」「tools 無し → ローカル小型」を declarative に切り替えられる。

ユースケース例 (Raspberry Pi 8GB starter `examples/providers.raspberrypi.yaml` から抜粋):

```yaml
auto_router:
  rules:
    - id: user:has-tools-go-cloud
      profile: with-tools         # OpenRouter free 系のみ
      match:
        has_tools: true
    - id: user:image-go-cloud
      profile: vision              # Gemini Flash 1M ctx
      match:
        has_image: true
    - id: user:longcontext-go-cloud
      profile: longcontext
      match:
        content_token_count_min: 32000
  default_rule_profile: local-chat # qwen3.5:2b/4b / gemma3:1b ローカル
```

OpenClaw (毎ターン Bash/Read/Write 等の tool を declare する agent) を `OPENAI_BASE_URL=http://<pi-ip>:8088/v1` で繋ぐと、tool-laden traffic は自動でクラウドに飛び、軽い chat だけが Pi 上のローカルで処理される。OPENROUTER_API_KEY のみ設定すればよく、有料 API は不要 (`ALLOW_PAID=false` がデフォルト)。

#### なぜ provider レベルの capability gate ではなく profile レベルなのか

`ProviderConfig.capabilities.tools=false` フラグは既存 (v0.x からある) だが、現状は `coderouter doctor` の診断表示と `model-capabilities.yaml` registry の解決に使うだけで、fallback chain における skip ゲートには接続されていない。`thinking` / `cache_control` には `will_degrade` ゲート (capability.py の `provider_supports_*`) があるが、tools には同等の skip 機構がない。これは既存の v0.3-D 「downgrade path」(non-native + tools[] あり → 非ストリーミング + tool_repair) に依存していて、provider が tools を返せなくても adapter エラーは出ず、上流から見ると success (空 tool_calls) で chain が fallthrough せず止まってしまう (= 観測症状: 「tool call されてない」)。

provider レベルの skip ゲートを後付けするのは chain semantics に踏み込む変更で互換性検討が要るため、本 patch では **profile レベルの宣言型 lever** に留める方針を採用。chain semantics を変えず、auto_router rule の追加で同じ効果を得られ、かつ既存の 6 matcher と完全に同じ規約 (exactly one + first match wins + fast-fail at load) で導入できる。

- Tests: 871 → **878** (+7: OpenAI tools[] / Anthropic tools[] / OpenAI legacy functions[] / no-tools fallthrough / 空リスト fallthrough / has_tools rule が code-fence rule より優先 / `has_tools: false` の "set 扱いだがマッチしない" 安全網)
- Runtime deps: 5 → 5 (34 sub-release 連続据え置き)
- Backward compat: 完全互換、既存 `auto_router` rule は何も変わらない、`has_tools` を使わない deployment は挙動完全一致

#### Changes

- `coderouter/config/schemas.py`:
  - `RuleMatcher` に `has_tools: bool | None = None` を追加、`_MATCHER_FIELDS` tuple に追加 (zero/multiple-fields の "exactly one" バリデータが自動適用)。
  - docstring の Variants セクションに 7 番目として `has_tools` を追記、boolean 形状が `has_image` と同じである理由 (`True` のみ意味を持ち、`False` は "set" 扱いだが `_match_rule` の `is True` チェックでマッチしない安全網) と、provider レベルの `capabilities.tools` flag との違い (前者は profile-level routing、後者は doctor の診断補助で chain skip ゲートではない) を明示。

- `coderouter/routing/auto_router.py`:
  - `_has_tools_in_body(body)` ヘルパを新設 — body の top-level `tools[]` (OpenAI Chat Completions / Anthropic Messages API 共通) と `functions[]` (OpenAI legacy、deprecated だが pinned SDK で残存) を一括認識、空リスト / None は False (lazy init 対応)。
  - `_match_rule(rule, message, text, model, estimated_tokens, has_tools)` シグネチャに `has_tools: bool` を追加、`has_tools is True` 分岐を 7 番目として実装。
  - `classify(...)` 内で `_has_tools_in_body(body)` を一度だけ呼んで rule iteration に渡す。`user_msg is None` でも `has_tools` rule は評価する (system-only prompt + tools[] declaration の構成にも対応)。
  - `_emit_resolved` / `_emit_fallthrough` の `signals` payload に `has_tools` を追記、`auto-router-resolved` log で「tools あり判定で routing したか」が dashboard / Prometheus exporter から見える。

- `tests/test_auto_router.py` Group 8 (tool-aware routing) を新設、7 ケース:
  - `test_classify_request_with_openai_tools_routes_to_with_tools` — 基本ケース、OpenAI 形式 `tools[].function` → `with-tools` profile。
  - `test_classify_request_with_anthropic_tools_routes_to_with_tools` — Anthropic 形式 `tools[].input_schema` も同じ top-level `tools` キーなので、単一 matcher で両 ingress カバー。
  - `test_classify_request_with_legacy_functions_routes_to_with_tools` — OpenAI legacy `functions[]` (deprecated だが pinned SDK で残存) も tool-laden 扱い。
  - `test_classify_request_without_tools_falls_through` — 逆ケース、tools 宣言なしの plain chat は `default_rule_profile` (Pi の場合は `local-chat`) に落ちる。
  - `test_classify_empty_tools_list_treated_as_no_tools` — `tools: []` / `functions: []` (lazy init shape) は False 扱い、no-spurious-match property を pin。
  - `test_classify_has_tools_first_match_wins_over_later_content_rule` — has_tools rule が code_fence rule より前に置かれた時、両方マッチする body でも先勝、global "first match wins" を新 matcher にも適用。
  - `test_has_tools_false_rejected_at_load` — `has_tools: False` が `_exactly_one` を通過するが `_match_rule` の `is True` チェックでマッチしない安全網を文書化、誤設定時もデフォルト経路に落ちることを保証。

#### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/routing/auto_router.py
M  pyproject.toml
M  tests/test_auto_router.py
```

---

### Raspberry Pi 8GB starter (`examples/providers.raspberrypi.yaml`)

**Theme: SBC で OpenClaw を動かす最小構成を yaml 1 個に集約。** v1.10.1 で追加した `has_tools` matcher を主役にした starter で、`coderouter serve` 1 発で Pi 上のローカル ollama (qwen3.5:2b/4b、qwen2.5:1.5b、gemma3:1b) と OpenRouter free 系 (qwen3-coder:free / gpt-oss-120b:free / gemini-2.5-flash:free) が tool-aware に振り分けられる。OPENROUTER_API_KEY のみ要設定、有料 API キー不要 (`ALLOW_PAID=false` がデフォルト)。

#### 設計の要点

- **ローカル全部 `tools: false`** — Pi 8GB に乗る ≤4B モデルは tool_calls を安定して返せないため capability で明示的に `false`。これは doctor 診断用の宣言で、実 routing は `has_tools` matcher が profile レベルで振り分けるので二重防御になる。
- **`num_ctx: 8192` + `num_predict: 1024` 制限** — Pi の CPU 推論は context 縮めた方が prefill が現実的、デフォルト ollama の 2048 だと OpenClaw の system prompt で詰む & 2048 から 32K に上げると prefill が分単位になるので 8K が現実的中間点。
- **画像 / 長尺 (32K+) もクラウドへ** — Pi では Gemma 4 E4B (vision capable だが 9.6GB で 8GB Pi に乗らない) の代わりに、`has_image` rule で OpenRouter Gemini Flash (1M ctx + vision native) に逃がす。
- **OpenRouter free 3 モデルで vendor diversity** — qwen-coder / gpt-oss / gemini-flash の 3 ベンダーを並べて、daily cap (~200 req/day per model per account) 当たり時の rate-limit 逃げ場を確保。
- **`output_filters: [strip_thinking, strip_stop_markers]` を Qwen 系で常時適用** — Pi で動かす Qwen 3.5 系は `<think>...</think>` リーク + `<|im_end|>` 漏れの両方を観測、両方 strip。

#### Tests

`tests/test_examples_yaml.py::test_example_yaml_loads` が `examples/providers*.yaml` を parametric にカバーしているため、`providers.raspberrypi.yaml` も自動でこの test に乗る。新たに pin したい invariant (例: ローカル全部 `tools: false`、`has_tools` rule の存在、auto_router default が `local-chat` 等) があれば後続 patch で個別 test 追加可能だが、本 patch では parametric の loader-clean property のみ確保。

#### Files touched

```
A  examples/providers.raspberrypi.yaml
```

---

## [v1.10.0] — 2026-05-01 (Umbrella tag — Cost enforcement + Long-run reliability completion + Auto-router feature complete)

**Theme: 「観測 → 理解 → 行動」を 3 軸で完成、Vision pillar P2/P3 が揃う。** v1.9.1 (patch) で取り切った 2 機能 (v1.9-B2 streaming usage 集約 + per-model auto-routing) は事実上 v1.10 backlog の助走、本 v1.10.0 で残り 3 機能を minor として束ねて出荷。CodeRouter は **「Local LLM で agent を長時間回すための信頼性層」** という Vision の v1.x 担当分が完成 — context overflow (L1) と quality drift (L4) を除く 4 系統障害 (L2/L3/L5/L6) を体系的に対処、auto-router の declarative 6 matcher も揃い、cost 系は観測 (v1.9-D) → enforcement (v1.10) で経路が閉じた。

含まれる出荷 5 件 (`docs/inside/future.md §6.6` の v1.10 着手順序、本 release で全完了):

| # | sub-release | テーマ | LOC | tests | 出荷先 |
|---|---|---|---|---|---|
| 1 | **v1.9-B2** | streaming 経路の usage 集約 (`_StreamUsageAccumulator`、placeholder→観測値) | ~150 | +3 | v1.9.1 |
| 2 | **per-model auto-routing** | `RuleMatcher.model_pattern` (Opus/Sonnet/Haiku 分岐、free-claude-code 由来) | ~120 | +5 | v1.9.1 |
| 3 | **provider 月次予算上限** | `BudgetTracker` + `cost.monthly_budget_usd` (LiteLLM 由来 / v1.9-D 累積版) | ~250 | +8 | **v1.10.0** |
| 4 | **v1.9-E phase 2 (L2/L5)** | Memory pressure detector + Backend health 状態機械 (Vision pillar 完成) | ~370 | +27 | **v1.10.0** |
| 5 | **longContext auto-switch** | `RuleMatcher.content_token_count_min` (claude-code-router 由来) | ~80 | +5 | **v1.10.0** |

- Tests: 838 (v1.9.1) → **871** (+33: 本 minor 単独 +27 from v1.9-E phase 2 + 8 budget + 5 longContext から、v1.10.0 で純増 +33)
- Runtime deps: 5 → 5 (**33 sub-release 連続据え置き**) — 最初から守ってきた `fastapi / uvicorn / httpx / pydantic / pyyaml` のみ
- pyproject version: 1.9.1 → 1.10.0

### Pillar 別の達成

#### P2 Long-run Reliability (v1.9-E 系) — Vision の核心が揃う

6 系統障害体系 (`docs/inside/future.md §1`) のうち v1.x で取りに行くと宣言した分が完成:

| # | 障害 | v1.x 担当 | 状態 |
|---|---|---|---|
| **L1** | Context overflow | (v2.0-F) | ⏳ |
| **L2** | Memory pressure | v1.9-E phase 2 | ✅ v1.10.0 |
| **L3** | Tool loop | v1.9-E phase 1 | ✅ v1.9.0 |
| **L4** | Quality drift | (v2.0-G) | ⏳ |
| **L5** | Backend crash / health | v1.9-E phase 2 | ✅ v1.10.0 |
| **L6** | Mid-stream interrupt | v0.3-A 既存 + (v2.0-H 強化) | ✅ baseline |

L2/L3/L5 の 3 兄弟が `coderouter/guards/` 配下に並び、それぞれ `MemoryPressureGuard` / `_apply_tool_loop_guard` / `BackendHealthMonitor` が pure module として独立。engine 統合は `_observe_provider_failure` / `_observe_provider_success` の 2 chokepoint で済む綺麗な設計に着地。

#### Cost pillar (v1.9-D 系) — 観測 → 制約の経路が閉じる

| 段階 | sub-release | 役割 |
|---|---|---|
| **観測** | v1.9-A | `cache-observed` log + cache hit/miss 4-class outcome |
| **観測のカバレッジ** | v1.9-B2 (v1.9.1) | streaming 経路まで完全カバー、placeholder ゼロ化 |
| **理解** | v1.9-D | per-provider USD cost + cache savings 別計算 (LiteLLM 既存品が落とす精度) |
| **制約** | **v1.10.0** | `monthly_budget_usd` で per-provider 月次 cap、UTC 暦月 in-memory bucketing |

v1.9.0 GA 時点で「観測の 4-class 精度」「LiteLLM 既存品より精度高い cost 計算」が CodeRouter の差別化軸として確立、v1.10.0 でそれを enforcement に活用できる経路が閉じた。

#### Auto-router (v1.6 系) — 6 matcher で feature complete

| # | matcher | 由来 | 出荷 |
|---|---|---|---|
| 1 | `has_image` | v1.6-A bundled | v1.6.0 |
| 2 | `code_fence_ratio_min` | v1.6-A bundled | v1.6.0 |
| 3 | `content_contains` | v1.6-A user-defined | v1.6.0 |
| 4 | `content_regex` | v1.6-A user-defined | v1.6.0 |
| 5 | `model_pattern` | free-claude-code 由来 | v1.9.1 |
| 6 | `content_token_count_min` | claude-code-router 由来 | **v1.10.0** |

declarative routing が「latest message の content / 画像 (per-turn signal) + request 全体の model id / token count (request-shape signal)」で完備、competitive analysis で抽出した v1.10 候補の取り込みは打ち止め、これ以降の追加は要望ドリブンで再開する想定。

### Migration

不要。**v1.9.1 / v1.9.0 / v1.9.0a* からの自然なアップグレード**:

- `coderouter` コマンド名 / Python import 名 / providers.yaml の format / env 変数 / ingress URL すべて完全に同じ
- 新しい schema field (`cost.monthly_budget_usd` / `memory_pressure_*` / `backend_health_*` / `content_token_count_min`) は全部 optional + 安全側 default (`monthly_budget_usd: None` / action は `warn` か `off`)、未指定 deployment は v1.9.x と挙動完全一致
- 新しい log event (`skip-budget-exceeded` / `chain-budget-exceeded` / `memory-pressure-detected` / `skip-memory-pressure` / `chain-memory-pressure-blocked` / `backend-health-changed` / `demote-unhealthy-provider`) は既存 `cache-observed` / `provider-failed` / etc. と同じ JSON 形式、外部 consumer は受信側に dispatch 追加するだけで対応可能

### Out of scope (v2.0 以降)

- **L1 Context overflow** → v2.0-F (semantic compression、context budget per-mode)
- **L4 Quality drift detection** → v2.0-G (response 品質 rolling window 観測)
- **L6 Mid-stream stitching 強化** → v2.0-H (resumable continuation)
- **Continuous probing** → v2.0-I (毎時/毎日 model ヘルスチェック、HF dataset 公開)
- **Persistent budget state** (sqlite / Redis) — 5-deps 不変原則で v1.x 範囲では却下
- **L5 active probing** (60s 間隔の能動 GET /api/version) — v2.0-I の領域、passive で 80% カバーできているため後回し
- **tiktoken / SentencePiece による正確なトークンカウント** — 5-deps 不変原則で却下

詳細は `docs/inside/future.md §7` を参照。

### Files touched

```
A  coderouter/guards/backend_health.py
A  coderouter/guards/memory_pressure.py
A  coderouter/routing/budget.py
A  tests/test_backend_health.py
A  tests/test_budget.py
A  tests/test_memory_pressure.py
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/logging.py
M  coderouter/metrics/collector.py
M  coderouter/metrics/prometheus.py
M  coderouter/routing/auto_router.py
M  coderouter/routing/fallback.py
M  docs/inside/future.md
M  plan.md
M  pyproject.toml
M  tests/test_auto_router.py
```

---

### v1.10 候補 #5: longContext auto-switch (claude-code-router 由来)

**Theme: コンテキスト窓圧迫の自動逃がし。** 長文プロンプト (会話ヒストリーの累積、コードベース貼り付け等) が来た時、context window の小さいモデル (200K Anthropic) ではなく 1M ctx の Gemini Flash 系へ自動切替する仕組み。`auto_router.rules[].if.content_token_count_min` を 6 番目の matcher として追加、既存 5 種と同じ "exactly one" 規約を継承。

ユースケース例:

```yaml
auto_router:
  rules:
    - if: { content_token_count_min: 32000 }
      route_to: longcontext
  default_rule_profile: writing

profiles:
  - name: longcontext
    providers:
      - openrouter-gemini-flash-free   # 1M ctx
      - anthropic-haiku-direct          # 200K ctx
  - name: writing
    providers: [anthropic-sonnet-direct]
```

agent が短いやり取りを 100 ターン続けて context が膨らんだ時、自動で 1M ctx チェーンに切り替わる。`free-claude-code` / `claude-code-router` 由来のニーズを CodeRouter の declarative auto_router framework に取り込んだ形。

#### 設計判断: char/4 ヒューリスティック vs tiktoken

token カウントは `len(text) // 4` の素朴ヒューリスティック (OpenAI 公式の rule of thumb)。**5-deps 不変原則** (`plan.md §5.4`) を守るため tiktoken / SentencePiece は導入しない。トレードオフ:

- **English 散文 / コード**: char/4 はやや緩い (実際は ~3.5/token)、`min` 比較なので大きい threshold で安全側に倒せる
- **CJK (日本語/中国語/韓国語)**: char/4 は **保守的にカウント不足** (実際は ~1.5-2 char/token)、つまり 100k 文字の日本語プロンプトを ~25k tokens と過小評価。これは積極的に context overflow を引き起こす方向ではないので fail-safe な誤差
- **トレードオフ判断**: tiktoken なら正確だが 100MB 級の依存追加、SentencePiece でも 50MB 級。CodeRouter は「個人開発者用の signal-based router」なので、operator が threshold を実機運用フィードバックで調整する前提のヒューリスティックで十分

#### Other matchers との違い

`content_contains` / `content_regex` / `has_image` は **latest user message** に対して評価 (per-turn signal)、`content_token_count_min` は **request 全体 (system + 全 messages)** を walk して合算 (request-shape signal)。context-window pressure はリクエスト全体の性質なので latest-only では誤検出する。

- Tests: 866 → **871** (+5: long-prompt match / 短文 fallthrough / 全 messages walk / 負値 reject / first-match-wins precedence)
- Runtime deps: 5 → 5 (33 sub-release 連続据え置き)
- Backward compat: 完全互換、既存 `auto_router` rule は何も変わらない

#### Changes

- `coderouter/config/schemas.py`:
  - `RuleMatcher` に `content_token_count_min: int | None = None` (`ge=1`) を追加、`_MATCHER_FIELDS` に登録 (既存の "exactly one" バリデータが自動適用、`ge=1` で 0/負値は schema load で reject)。
  - docstring の Variants セクションに 6 番目として明記、char/4 ヒューリスティック + 全 messages 対象 (latest-only の他 matcher と差別化) + 5-deps トレードオフを文書化。

- `coderouter/routing/auto_router.py`:
  - `_estimate_total_tokens(body)` ヘルパを新設 — `body["system"]` (str / list-of-blocks 両対応) と `body["messages"]` の全 message を walk、`_extract_text` で text を抽出、char 合算を `_CHARS_PER_TOKEN_HEURISTIC=4` で除して token 推定。image / non-text blocks は 0 寄与。
  - `_match_rule` に `estimated_tokens: int` パラメータを追加、6 番目の分岐として `content_token_count_min` 比較を実装。
  - `classify(...)` 内で `_estimate_total_tokens(body)` を 1 回計算、ルール評価ループに流す。`_emit_resolved` / `_emit_fallthrough` の signals payload に `estimated_tokens` を追記、dashboard / Prometheus exporter から「何トークン推定でその profile に流れたか」が見える。

- `tests/test_auto_router.py` Group 7 を新設、5 ケース:
  - `test_classify_long_prompt_routes_to_longcontext` — 200,000 chars (~50,000 tokens) → 32,000 threshold を超えて longcontext profile。
  - `test_classify_short_prompt_below_threshold_falls_through` — 1,000 chars (~250 tokens) → default_rule_profile (writing) に落ちる。
  - `test_classify_long_context_walks_all_messages_not_just_latest` — 長い会話 history + 短い最新ユーザー msg、latest-only matcher なら拾えないケースを longContext は拾うことを pin。
  - `test_content_token_count_min_rejects_non_positive_at_load` — `0` / `-5` を `RuleMatcher` 構築時に reject (pydantic `ge=1`)。
  - `test_long_context_first_match_wins_over_later_image_rule` — token-count rule を先に置けば長文+画像 body でも longcontext が勝つ、先勝順序を pin。

#### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/routing/auto_router.py
M  tests/test_auto_router.py
```

#### Why now

`docs/inside/future.md §6.6` の v1.10 着手順序 **#5 (最終)**。実装規模 ~80 LOC + tests ~150 LOC、半日工数 (見積 ~150-200 LOC / 3-5 日より大幅短縮 — auto_router の matcher 追加パターンが per-model auto-routing で確立済み、全 messages walk 用の `_estimate_total_tokens` ヘルパだけ新設で済んだ)。

これで **v1.10 候補 5 件全完了** (#1 v1.9-B2 / #2 per-model auto-routing は v1.9.1、#3 monthly budget / #4 v1.9-E phase 2 / #5 longContext auto-switch は本 [Unreleased] umbrella)。次回 PyPI publish 時に **v1.10.0 minor として umbrella tag 化**できる位置 (Vision pillar 完成 + auto-router 全 6 matcher 揃 + cost enforcement 完成)。

#### Out of scope (v2.0 以降 / 将来の精緻化)

- **tiktoken / SentencePiece による正確なトークンカウント** — 5-deps 不変原則で却下。実機運用で threshold tuning が困難になったら再検討。
- **Provider 別 context-window 自動推測** — `model-capabilities.yaml` に `max_context_tokens` を加えれば自動推測できる方向もあるが、operator の運用シナリオ次第なので明示宣言で十分。
- **動的 threshold (chain の最小 max_context_tokens に応じて)** — 同上、明示宣言で十分。

---

### v1.10 候補 #4: v1.9-E phase 2 (L2 memory pressure + L5 backend health) — Vision 完成

**Theme: 「8 時間 agent ループでも止まらない」を約束する Long-run Reliability pillar (P2) を完成させる。** v1.9.0 で L3 (tool-loop guard) を phase 1 として先行出荷したが、Vision で謳う 6 系統障害体系のうち **L2 (Memory pressure)** と **L5 (Backend crash / health)** が phase 2 として残っていた。本 release で両方を opt-in guard として実装、`coderouter/guards/` 配下に並ぶ 3 兄弟 (tool_loop / memory_pressure / backend_health) で長時間運用の中核 3 障害をカバーする。

#### L2: Memory pressure detection + cooldown

ローカル backend (Ollama / LM Studio / llama.cpp) が VRAM 枯渇で 5xx を返す時、エラー本文に `out of memory` / `CUDA out of memory` / `insufficient memory` / `model requires more system memory` 等の OOM フレーズが入る。L2 はこれを観察して当該 provider をクールダウンに入れ、次の chain resolve から `memory_pressure_cooldown_s` 秒間 skip する:

```yaml
profiles:
  - name: default
    providers: [ollama-large, ollama-small, openrouter-fallback]
    memory_pressure_action: skip       # off / warn / skip (default: warn)
    memory_pressure_cooldown_s: 120    # default 120s, 10〜3600 s
```

`action=skip` の時、ollama-large が OOM → 120 秒間 ollama-large を chain から除外、ollama-small や openrouter-fallback に流れる → クールダウン明けで再度 ollama-large を試す。`action=warn` (default) は log のみ、`off` は完全に無効化 (zero overhead)。

#### L5: Backend health (consecutive failure state machine)

backend が突発 crash した時の defacto demote。`BackendHealthMonitor` が provider ごとに consecutive failure 数を数え、`backend_health_threshold` (default 3) で `HEALTHY → DEGRADED`、`2 x threshold` で `DEGRADED → UNHEALTHY` に遷移。1 回の成功で即 HEALTHY 復帰。`backend_health_action: demote` の時、UNHEALTHY な provider は chain 末尾に降格 (skip ではなく **demote** — 死活確認の 1 リクエストは飛ばす、best-effort principle):

```yaml
profiles:
  - name: default
    providers: [ollama-local, anthropic-fallback]
    backend_health_action: demote       # off / warn / demote (default: warn)
    backend_health_threshold: 3
```

v1.9-C の `adaptive` (rolling-window 連続観測 + debounce) とは直交関係 — adaptive が「徐々に遅くなった」勾配ケース、L5 が「突発 crash」二値ケース。両者重ね掛け可能で、両方 enable した chain では「latency 劣化 → adaptive demote」「crash → L5 demote」の両方の信号で並べ替え。

#### Numbers

- Tests: 839 → **866** (+27 累積、L2 +19 / L5 +8)
- Runtime deps: 5 → 5 (32 sub-release 連続据え置き)
- Backward compat: 完全互換、両 `*_action` のデフォルトは `warn` (= log のみ、行動変化なし)。`off` で完全無効化。既存 v1.9.x deployment は yaml 変更なしで自然継続

#### Changes

- `coderouter/guards/memory_pressure.py` 新設 (~170 LOC):
  - `is_memory_pressure_error(exc)` — 純関数、9 種の OOM フレーズに対する case-insensitive substring match (Ollama / LM Studio / llama.cpp / 汎用 CUDA / Metal の実観測パターン)。
  - `MemoryPressureGuard` — per-provider TTL cooldown tracker、`mark_pressured` / `is_pressured` / `pressured_until` API、`time.monotonic` ベースで wall-clock skew 耐性、tests は `now=` 引数で deterministic 注入。

- `coderouter/guards/backend_health.py` 新設 (~200 LOC):
  - `BackendHealthMonitor` — per-provider 状態機械 (HEALTHY / DEGRADED / UNHEALTHY)、`record_attempt(success, threshold)` で観測、状態遷移時のみ `HealthTransition` を返す (no log spam on stable state)、threshold は per-call で profile 違いに対応。
  - 状態機械の遷移ルール: 失敗 N 回 (= threshold) で DEGRADED、2N 回で UNHEALTHY、1 回成功で即 HEALTHY 復帰。

- `coderouter/config/schemas.py`:
  - `FallbackChain` に `memory_pressure_action` / `memory_pressure_cooldown_s` (L2) と `backend_health_action` / `backend_health_threshold` (L5) を追加。L3 (`tool_loop_*`) と同じ命名 + 同じ "off / warn / 行動" tri-state パターン。
  - 各 field に `Literal` 型 + range 制約 + 詳細 docstring (どの障害を見るか、L2/L5 の使い分け、v1.9-C adaptive との関係)。

- `coderouter/logging.py`:
  - L2: `log_memory_pressure_detected` / `log_skip_memory_pressure` / `log_chain_memory_pressure_blocked` ヘルパ + 3 つの TypedDict payload。paid-gate / budget-gate と完全 symmetric。
  - L5: `log_backend_health_changed` (state transition、payload に old_state/new_state/consecutive_failures) / `log_demote_unhealthy_provider` ヘルパ + 2 つの TypedDict。

- `coderouter/routing/fallback.py`:
  - `FallbackEngine` に `_memory_pressure` / `_backend_health` lazy property を追加 (`_adaptive` / `_budget` と同じパターン、`__new__` 経由 legacy tests 対応)。
  - `_observe_provider_failure(provider, exc, profile)` ヘルパ — L2 OOM 検出 + L5 失敗カウンタを single chokepoint で dispatch、6 つの failure site (4 entry point × non-stream/mid-stream) から呼ぶ。
  - `_observe_provider_success(provider, profile)` 新設 — L5 状態機械の成功遷移を 4 success site (provider-ok 時) から呼ぶ。
  - `_resolve_chain` を 4-pass に拡張: paid → budget → **L2 pressure skip** → L5 demote。L2 は filter (skip)、L5 は reorder (demote)、両者の役割分担を明確化。L5 demote は `unhealthy and healthy` 両方ある時のみ実施 (uniformly UNHEALTHY chain は no-op、log spam 抑制)。

- `coderouter/metrics/collector.py`:
  - `_provider_skipped_memory_pressure: Counter` + `_chain_memory_pressure_blocked_total: int` (L2)。
  - `_provider_demoted_unhealthy: Counter` + `_backend_health_transitions: dict[str, Counter]` (L5、transition を destination state でキー)。
  - `skip-memory-pressure` / `chain-memory-pressure-blocked` / `backend-health-changed` / `demote-unhealthy-provider` event の dispatch + snapshot/reset 配線。

- `coderouter/metrics/prometheus.py`:
  - `coderouter_provider_skipped_total{reason="memory_pressure"}` を既存 `paid` / `unknown` / `budget` と同 counter に同居。
  - `coderouter_provider_demoted_unhealthy_total{provider}` (L5)、`coderouter_backend_health_transitions_total{provider, state}` (L5)、`coderouter_chain_memory_pressure_blocked_total` (L2) を追加。

- `tests/test_memory_pressure.py` 新設 (~360 LOC、+19 tests):
  - **Group 1 (detector)**: 8 種の OOM フレーズを parameterize で網羅、5 種の非 OOM 失敗で false 確認。
  - **Group 2 (guard)**: TTL cooldown / lazy expiry / re-mark 拡張。
  - **Group 3 (engine)**: action=warn は log only / action=skip は cooldown 中 chain skip + fallback / action=off は完全無効 / 全 provider pressured で `chain-memory-pressure-blocked` warn + `NoProvidersAvailableError`。

- `tests/test_backend_health.py` 新設 (~340 LOC、+8 tests):
  - **Group 1 (monitor)**: 初期状態 HEALTHY、threshold/2x threshold での状態遷移、success で UNHEALTHY → HEALTHY 即復帰、stable state で transition 返さない。
  - **Group 2 (engine action)**: warn は log only / demote で chain reorder (try-provider 順序検証) / off で監視ゼロ / UNHEALTHY → HEALTHY recovery transition log。
  - **Group 3 (chain reorder)**: 全 provider UNHEALTHY 時は demote no-op (log spam なし、best-effort 続行)。

#### Files touched

```
A  coderouter/guards/backend_health.py
A  coderouter/guards/memory_pressure.py
A  tests/test_backend_health.py
A  tests/test_memory_pressure.py
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/logging.py
M  coderouter/metrics/collector.py
M  coderouter/metrics/prometheus.py
M  coderouter/routing/fallback.py
```

#### Why now

`docs/inside/future.md §6.6` v1.10 着手順序 #4 — **Vision の核心**。v1.9.0 GA で「v1.10 候補」と整理した backlog で唯一 ~900 LOC スケールの Vision-critical pillar。v1.9.1 の monthly-budget で cost 軸の運用が見えるようになった上に、L2/L5 で **「6 系統障害のうち L2/L3/L5 を体系的に対処」** が完成。`L1 Context overflow` / `L4 Quality drift` / `L6 Mid-stream interrupt 強化` は v2.0-F/G/H の領域、v1.x で cover する long-run reliability の到達点として位置付け。

#### Out of scope (v2.0 以降)

- **L5 active probing** (60s 間隔の能動 GET /api/version) — 受動 observation で十分カバーできる範囲、active probe を加えると httpx の lifecycle / mocking の複雑度が増えるため v2.0-I (`continuous probing` pillar 拡張) で再検討。
- **L2 thresholding (count of OOM events before mark)** — single OOM = mark の素朴実装で十分。複数 OOM 観測でしか mark しないという調整は実機運用 feedback が来てから。
- **HEALTHY/DEGRADED/UNHEALTHY の 4 段階以上化** — 3 段階で十分、運用 feedback が来てから検討。

---

### v1.10 候補 #3: provider 月次予算上限 (LiteLLM 由来 / v1.9-D の累積版)

**Theme: v1.9-D で「いくら使ったか」が見えるようになった所に、「これ以上使うな」を宣言できる gate を足す。** v1.9-D の `cost_total_usd` は process-lifetime cumulative なので billing-cycle 上限としては使えない (再起動で消える + 月境界で reset しない)。本機能は **per-provider monthly USD cap** を `cost.monthly_budget_usd` で宣言できるようにし、UTC 暦月単位の running total が cap に達した provider を chain resolver が skip するようにする。

ユースケース例:

```yaml
providers:
  - name: anthropic-direct
    kind: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-6
    cost:
      input_tokens_per_million: 3.0
      output_tokens_per_million: 15.0
      monthly_budget_usd: 5.0   # ← v1.10 新フィールド
  - name: ollama-local
    base_url: http://localhost:11434/v1
    model: qwen3.6:35b-a3b
    # 無料 / cost 未設定 = 無制限 (skip 対象外)
profiles:
  - name: default
    providers: [anthropic-direct, ollama-local]   # paid → free fallback
```

`anthropic-direct` が今月 5 USD 消費した時点で chain resolver が skip し、`ollama-local` (無料) に fall through する。`skip-budget-exceeded` info + (全 provider が cap に達した時のみ) `chain-budget-exceeded` warn が emit される。

**Persistence の意図的な制限**: in-memory only。プロセス再起動で running total が 0 にリセットされる。**5-deps 不変原則** (`plan.md §5.4`) を守るため sqlite / Redis / disk は導入しない。durable な月次 enforcement が必要なオペレータは v1.9-D の `cost_total_usd` panel を外部監視ツール (Prometheus alertmanager / Grafana threshold) で受ければ十分カバー可能。

- Tests: 831 → **839** (+8: BudgetTracker pure 3 / CostConfig schema 2 / engine integration 3)
- Runtime deps: 5 → 5 (31 sub-release 連続据え置き)
- Backward compat: 完全互換、`monthly_budget_usd` 未設定 deployment は挙動完全一致 (opt-in feature)

#### Changes

- `coderouter/config/schemas.py`:
  - `CostConfig` に `monthly_budget_usd: float | None = None` を追加 (`ge=0.0`、None = 無制限)。
  - docstring で UTC calendar-month + in-memory only persistence を明示、5-deps 不変原則との整合 (no sqlite/Redis) を文書化。

- `coderouter/routing/budget.py` 新設 (~190 LOC):
  - `BudgetTracker` クラス — per-provider current-month USD running total を `dict[str, float]` で保持、`threading.RLock` 配下。月境界判定は `_utc_month_key` ヘルパ (UTC `datetime.now()` 経由、tests は `now=` 引数で deterministic に注入可能)。
  - 公開 API: `record(provider, cost_usd)` / `is_over_budget(provider, budget_usd)` / `current_month()` / `total_for_provider(provider)` / `reset()`。
  - **Lazy month rollover**: 各 public call の入口で `_roll_if_needed` を呼び、cached month と current UTC month が違えば `_totals` を clear してから query に答える。background timer 不要。
  - `is_over_budget` は `>=` 比較 — exact-hit の "5.00 USD" は exhausted と判定 (conservative: 次の call は bill しない)。

- `coderouter/logging.py`:
  - `SkipBudgetExceededPayload` / `ChainBudgetExceededPayload` TypedDict + `log_skip_budget_exceeded` / `log_chain_budget_exceeded` ヘルパを追加。`log_chain_paid_gate_blocked` のパターンを完全 mirror、payload に `month` (YYYY-MM UTC bucket) を含める。

- `coderouter/routing/fallback.py`:
  - `FallbackEngine.__init__` に `_budget_tracker: BudgetTracker = BudgetTracker()` を追加、`_adaptive` と同じ lazy property pattern で `_budget` を露出 (legacy tests の `__new__` 経路でも空 tracker が返る)。
  - `_resolve_chain` を 2-pass に refactor: pass 1 が paid-gate (既存ロジック)、pass 2 が **budget-gate** (新規)。budget-gate は `provider_cfg.cost.monthly_budget_usd` が set されている provider のみ check、`is_over_budget` ならば `skip-budget-exceeded` info を emit して候補から除外。chain が空になった時の aggregate warn は `blocked_by_budget` を優先 (paid-gate より後段で filter したため)、`chain-budget-exceeded` を fire。
  - `_emit_cache_observed` / `_emit_cache_observed_streaming` に `budget: BudgetTracker | None = None` 引数を追加、`compute_cost_for_attempt` の結果が positive な時に `budget.record(provider, cost.total_usd)` を呼ぶ。engine 側 2 callsite (`generate_anthropic` / `stream_anthropic`) で `budget=self._budget` を渡す配線。

- `coderouter/metrics/collector.py`:
  - `_provider_skipped_budget: Counter[str]` + `_chain_budget_exceeded_total: int` を追加、`_provider_skipped_paid` / `_chain_paid_gate_blocked_total` の対称配置。
  - `_dispatch` に `skip-budget-exceeded` / `chain-budget-exceeded` event の handler を追加。`reset()` / `snapshot()` も両 counter を含むように拡張。
  - module docstring の event inventory に v1.10 行 2 件追記。

- `coderouter/metrics/prometheus.py`:
  - `coderouter_provider_skipped_total{provider, reason="budget"}` を既存の `paid` / `unknown` と同じ counter に同居 (dashboards が reason 別 stack できるように)。
  - `coderouter_chain_budget_exceeded_total` scalar counter を新設 (`coderouter_chain_paid_gate_blocked_total` の対称配置)。

- `tests/test_budget.py` 新設 (~340 LOC、+8 tests):
  - **Group 1 (BudgetTracker pure)**: record 蓄積 / is_over_budget の `>=` boundary semantics / 月境界 rollover (`now=` 引数で April→May 跨ぎを deterministic に検証)。
  - **Group 2 (CostConfig schema)**: `monthly_budget_usd: 5.0` 受理、負値 reject (pydantic `ge=0.0`)。
  - **Group 3 (engine integration)**: pre-loaded budget でも primary skip + fallback 経由 (warn なし) / 全 provider cap で `NoProvidersAvailableError` + `chain-budget-exceeded` warn 1 回 / 実 attempt の cost が `BudgetTracker` に蓄積されて 3 回目で skip されることを確認 (real wiring の end-to-end test)。

#### Files touched

```
A  coderouter/routing/budget.py
A  tests/test_budget.py
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/logging.py
M  coderouter/metrics/collector.py
M  coderouter/metrics/prometheus.py
M  coderouter/routing/fallback.py
```

#### Why now

`docs/inside/future.md §6.6` の v1.10 着手順序 #3。v1.9-D で観測の基盤ができた直後に **enforcement** を足す自然な順序、cost-aware ユーザー (paid backend を組み込む operator) にとって最も価値の高い v1.10 候補。LiteLLM が同等機能を `litellm[proxy]` の中で重実装 (Redis 必須) しているのに対し、CodeRouter は in-memory + 5-deps 維持で「個人開発者用の budget guard」として割り切ることで構造的負債を避ける。

#### Out of scope

- **Persistent budget state** (sqlite / Redis / disk-backed) — 5-deps 不変原則により未対応。durable enforcement 必要なケースは v1.9-D dashboard を外部 alerting に繋ぐ運用で代替。
- **Rolling 30-day window** — UTC calendar month で十分 (typical billing cycle と一致、月境界判定の rollover 実装が単純)。rolling window は `_utc_month_key` を date-windowed key に差し替えれば追加できるが、operator request が来てから判断。
- **Per-profile budget** (vs per-provider) — provider 単位で十分。同じ provider を複数 profile が共有する場合 budget は共有されるべき (実コストの帰属先は provider なので) という意味的にも provider 帰属が正しい。

---

## [v1.9.1] — 2026-05-01 (Patch — v1.10 候補から quick win 2 件先行刈取り)

**Theme: v1.9.0 GA で「v1.10 候補」と整理した backlog のうち、構造的負債を伴わない quick win 2 件 (streaming cache 観測の完成形 + agent-driven model 識別子で profile 分岐) を patch として束ねる。** 観測ループの埋め残しと、Claude Code / Cursor 等 agent 側の設定 (Opus / Sonnet / Haiku 使い分け) が CodeRouter の declarative routing に反映できる経路を、完全互換で追加。両機能とも v1.9.0 既存 framework (`cache-observed` log / `auto_router.rules`) の延長線で、新 framework / 依存追加なし。

含まれる出荷 2 件 (`docs/inside/future.md §6.6` の v1.10 着手順序 #1, #2):

| # | sub-release | テーマ | LOC | tests |
|---|---|---|---|---|
| 1 | **v1.9-B2** | streaming 経路の usage 集約 — `_StreamUsageAccumulator` + `_emit_cache_observed_streaming` で `outcome=unknown` placeholder を観測値に置換 | ~150 | +3 |
| 2 | **per-model auto-routing** | `RuleMatcher.model_pattern` 5 番目 matcher 追加、`re.fullmatch` で body model id を評価 (free-claude-code 由来) | ~120 | +5 |

- Tests: 830 → **838** (+8 累積、v1.9-B2 +3 / per-model +5)
- Runtime deps: 5 → 5 (30 sub-release 連続据え置き)
- Backward compat: 完全互換、既存 yaml / API / log payload 全部既存と同じ schema、新フィールド (`model_pattern`) を使わない deployment は挙動完全一致
- pyproject version: 1.9.0 → 1.9.1

### Migration

不要。**v1.9.0 / v1.9.0a* からの自然なアップグレード**:

- `coderouter` コマンド名 / Python import 名 / providers.yaml の format / env 変数 / ingress URL すべて完全に同じ
- streaming で `cache-observed` log を読んでいる外部 consumer (例: dashboard / Prometheus / 自前 JSONL parser) には、v1.9.0a6 までゼロ固定だった `cache_read_input_tokens` / `cache_creation_input_tokens` / `input_tokens` / `output_tokens` / `outcome` / `cost_usd` / `cost_savings_usd` が観測値に置き換わる。consumer 側は **値が増えた** だけで schema は同じ、ロジック変更不要
- `auto_router.rules[].if.model_pattern` を使い始めるには yaml に 1 行足すだけ、既存 rule に影響なし

### Out of scope (v1.10 / v1.9.x 続編)

[v1.9.0] GA ノートと `docs/inside/future.md §6.6` で示した v1.10 候補から残り 3 件:

- **provider 月次予算上限** (LiteLLM 由来、v1.9-D の累積版) — `monthly_budget_usd` で provider 単位の running total + 超過時 skip + log。~400 LOC、3-5 日。
- **v1.9-E phase 2** — L2 Memory pressure (LM Studio / ollama backend OOM 検知) / L5 Backend health (continuous probe + chain reorder)。**Vision の核心 (8 時間 agent ループでも止まらない)** を完成させる pillar。~900 LOC、1-2 週間。
- **longContext auto-switch** — `auto_router` rule type 5 として `content_token_count_min` matcher 追加 (claude-code-router task-based 取込)。~200 LOC、3-5 日。

これら 3 件は構造拡張を伴うため v1.9.1 patch ではなく v1.10.0 minor で個別 sub-release にして出荷する想定。

### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/routing/auto_router.py
M  coderouter/routing/fallback.py
M  docs/inside/future.md
M  plan.md
M  pyproject.toml
M  tests/test_auto_router.py
M  tests/test_fallback_cache_observed.py
```

---

### per-model auto-routing (v1.10 候補 #2、free-claude-code 由来)

**Theme: agent が送ってきた `model` フィールドそのものを auto_router の判定軸に追加。** Claude Code / Cursor 等の agent 側設定 (Opus / Sonnet / Haiku の使い分け) を、CodeRouter 側 profile chain の選択にも反映できるようにする。`auto_router.rules[].if.model_pattern` を 5 番目の matcher として導入、既存 4 種 (`has_image` / `code_fence_ratio_min` / `content_contains` / `content_regex`) と同じ "exactly one" 規約と eager regex compile (typo は startup で fast-fail) を継承。

ユースケース例:

```yaml
auto_router:
  rules:
    - if: { model_pattern: "claude-3-5-haiku.*" }
      route_to: lightweight
    - if: { model_pattern: "claude-3-5-sonnet.*" }
      route_to: coding
  default_rule_profile: writing
```

agent 側で「モデルの使い分けは決まってる」状況に CodeRouter が綺麗に乗れる。`free-claude-code` repo の同様機能を CodeRouter の declarative auto_router framework に取り込んだ形。

- Tests: 833 → **838** (+5: Sonnet→coding / Haiku→lightweight / no-model field → fallthrough / 不正 regex は schema load で fast-fail / model_pattern と content rule の first-match-wins precedence)
- Runtime deps: 5 → 5 (30 sub-release 連続据え置き)
- Backward compat: 完全互換、既存 `auto_router` rule は何も変わらない、`model_pattern` を使わない deployment は挙動完全一致

#### Changes

- `coderouter/config/schemas.py`:
  - `RuleMatcher` に `model_pattern: str | None = None` を追加、`_MATCHER_FIELDS` tuple に追加 (zero/multiple-fields の "exactly one" バリデータが自動適用)。
  - `_compile_regex_eagerly` バリデータを `model_pattern` も覆うよう拡張、不正な regex は schema load で `ValueError("Invalid regex for model_pattern ...")` を発火 (`content_regex` と同じ fast-fail パターン)。
  - docstring の Variants セクションに 5 番目として `model_pattern` を追記、`re.fullmatch` semantics と `content_regex` の `re.search` との違い (model 識別子は "structured tokens" であり全体描写型) を明示。

- `coderouter/routing/auto_router.py`:
  - `_extract_model(body)` ヘルパを新設 — 両 ingress (Anthropic `/v1/messages` / OpenAI `/v1/chat/completions`) で body の top-level `model` field を 1 ヶ所で抽出、空文字列 / 非 str は None 扱い。
  - `_match_rule(rule, message, text, model)` シグネチャに `model: str | None` を追加、`model_pattern` matcher を 5 番目の分岐として実装。`re.fullmatch` で評価 (model id は構造的 token なので部分一致より全体記述型の方が直観に合う)。`model is None` の時は False を返して fallthrough させる (空 body などの test fixtures 対策)。
  - `classify(...)` 内で `_extract_model(body)` を一度だけ呼び、`_match_rule` に流す。`user_msg is None` でも `model_pattern` rule は評価する (空 messages でも model 経路で route 可能)。
  - `_emit_resolved` / `_emit_fallthrough` の `signals` payload に `model` を追記、auto-router-resolved log で何の model id で routing 判断したかが dashboard / Prometheus exporter から見える。

- `tests/test_auto_router.py` Group 6 (per-model auto-routing) を新設、5 ケース:
  - `test_classify_model_pattern_sonnet_routes_to_coding` — 基本ケース、`claude-3-5-sonnet.*` → coding profile。content は writing 寄りでも model rule が勝つ。
  - `test_classify_model_pattern_haiku_routes_to_lightweight` — 4-profile fixture (`_model_pattern_config` で lightweight 追加)、Haiku id → lightweight profile。
  - `test_classify_model_pattern_no_model_field_falls_through` — body に `model` field がない時、`r".+"` でも match せず default_rule_profile に落ちる (fixtures / test harness 用 robustness)。
  - `test_model_pattern_invalid_regex_fast_fails_at_load` — `r"([unclosed"` → `RuleMatcher` 構築時に `ValueError(model_pattern)` (`content_regex` と同じ eager compile path)。
  - `test_model_pattern_first_match_wins_over_later_content_rule` — model_pattern rule を content_contains rule より前に置くと、両方 match する body でも先勝、global "first match wins" を pin。

#### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/routing/auto_router.py
M  tests/test_auto_router.py
```

#### Why now

`docs/inside/future.md §6.6` の v1.10 着手順序で 2 番目に推奨されていた quick win。実装規模 ~120 LOC (見積 ~150-200 LOC を下回って収束)、tests +5、半日工数。既存 auto_router framework の 1 matcher 追加なので構造的負債なし、`free-claude-code` 由来要望を CodeRouter の declarative 思想を崩さずに取り込めた。次の v1.10 候補 (provider 月次予算 / longContext auto-switch / v1.9-E phase 2) の前段階として位置付け。

---

### v1.9-B2: streaming 経路の usage 集約 (v1.10 候補 #1)

**Theme: v1.9.0 で意図的に v1.10 候補へ繰り越した quick win を回収。** v1.9.0a6 で「streaming パスでも `cache-observed` log を emit する」ところまでは揃えたが、token 数は `outcome=unknown` + ゼロ固定の placeholder だった。本 patch は `message_start.message.usage` + 終端 `message_delta.usage` を accumulator で max-merge 集約し、非 streaming (`generate_anthropic`) と同じ outcome 分類 + cost 計算 + ログ payload 形状に揃える。`/dashboard` / Prometheus / MetricsCollector 側は branch 不要で streaming 経路の数字が取れるようになる。

- Tests: 830 → **833** (+3: cache_hit / cache_creation / no_cache の streaming 集約 — `tests/test_fallback_cache_observed.py`)
- Runtime deps: 5 → 5 (29 sub-release 連続据え置き)
- Backward compat: 完全互換、log payload は v1.9-A と同じ schema、`streaming=true` flag のみ意味的に "観測値" になる (ゼロ placeholder ではなくなる)

#### Changes

- `coderouter/routing/fallback.py`:
  - `_StreamUsageAccumulator` を新設 — `message_start.message.usage` と `message_delta.usage` から `input_tokens` / `output_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens` を per-field max-merge で集約。`output_tokens` は終端 `message_delta` で最終値が決まるため max が安全、cache fields は API minor version によって `message_start` / `message_delta` どちらにも現れる可能性があるため両方を観測。`usage_present` は「upstream が空 dict も含めて usage を返したか」を保持し、何も流れてこなかった streaming は引き続き `outcome=unknown` に分類。
  - `_emit_cache_observed_streaming(...)` を追加 — accumulator 値を `classify_cache_outcome` / `compute_cost_for_attempt` に通して `log_cache_observed` を呼ぶ。非 streaming `_emit_cache_observed` と同じ outcome 分類 + cost 計算ロジック。
  - `stream_anthropic(...)` 内のループで `acc = _StreamUsageAccumulator()` を初期化、`first` および後続 `event_iter` の各 event に `acc.observe(...)` を呼ぶ。完了時の `log_cache_observed(..., outcome="unknown", *=0)` を `_emit_cache_observed_streaming(acc, ..., provider_config=adapter.config)` に置換。
  - `_emit_cache_observed` の docstring を更新 — `streaming=True` arg は openai_compat 経路 (downgrade で 1 つの response に collapse される) 用に残す説明に改訂。

- `tests/test_fallback_cache_observed.py`:
  - `_CacheAnthropicAdapter.stream_anthropic` を constructor 引数駆動に変更 (`message_start.message.usage` に input_tokens + cache 系、`message_delta.usage` に input_tokens + output_tokens を流す、ゼロ時は空 dict を出して "usage 一切なし" を再現可能)。
  - 既存 `test_cache_observed_fires_on_streaming_with_unknown_outcome` の docstring を v1.9-B2 文脈に更新 (上流から usage が 1 件も流れない時の `unknown` 床をピン留め)。
  - 新規 3 ケース:
    - `test_streaming_aggregates_cache_hit_usage` — `cache_read_input_tokens=2048` を含む stream → `outcome=cache_hit` + 入出力カウンタ集約。
    - `test_streaming_aggregates_cache_creation_usage` — `cache_creation_input_tokens=1500` の stream → `outcome=cache_creation`。
    - `test_streaming_aggregates_no_cache_outcome` — non-zero usage + cache fields なし → `outcome=no_cache` (本番最頻 case、v1.9.0a6 の placeholder では拾えていなかった)。

#### Why now

v1.9.0 GA ノートで明示した「v1.10 候補」のうち最も短期に取れる quick win。実装サイズ ~150 LOC、半日工数で `outcome=unknown` placeholder を観測値に置き換えられるため、cost dashboard / cache-hit rate panel の streaming 経路カバレッジが完成する。`v1.9-E phase 2` (L2/L5) や per-model auto-routing といった上位 priority 作業の前段で済ませておくと、その後の adaptive routing / Vision pillar 完成度が上がる。

#### Out of scope

- ChatRequest.stream() 経路 (OpenAI-shaped streaming) は対象外 — `stream_anthropic` の sibling であり、Anthropic 経由の cache observation は未対応の領域。Anthropic prompt cache を利用する client は実質 `/v1/messages` 経由なので影響範囲は限定的。
- v1.9.0a6 で論じた "downgrade 後の synthesize_anthropic_stream_from_response" 経路 — 元になる AnthropicResponse から `message_start` event が usage 付きで再構築されるため、accumulator が自動でカバーする (追加実装不要)。

#### Files touched

```
M  CHANGELOG.md
M  coderouter/routing/fallback.py
M  tests/test_fallback_cache_observed.py
```

---

## [v1.9.0] — 2026-04-29 (Umbrella tag — Cache observability + Adaptive routing + Cost-aware + Long-run reliability)

**Theme: 「観測 → 理解 → 行動 → 信頼性」を 1 minor で揃える、observability pillar の成熟。** v1.9.0 は 6 sub-release (v1.9-A〜E) を通じて、CodeRouter を「動いてはいるが何が起きているか分からない」状態から、「**何にいくら使った / どこで遅くなった / 何で詰まった**」が運用ログ 1 行で分かる状態に押し上げる。具体的には:

- **観測 (v1.9-A)** — Anthropic prompt cache の hit/miss を全リクエストで `cache-observed` ログに記録、`/dashboard` から hit_rate / saved tokens が見える
- **透過 (v1.9-B)** — openai_compat 経路でも cache_control / thinking 等の Anthropic 拡張を可能な限り保持、不可能な場合は `capability-degraded` で明示
- **動的最適化 (v1.9-C)** — profile に `adaptive: true` を付けると、normally-fast な provider が一時的に遅くなったとき自動で後ろに送り、user-felt latency を保護
- **コスト把握 (v1.9-D)** — providers.yaml の `cost:` で USD pricing を宣言、cache savings は別計算 (LiteLLM 等の競合品が落としている粒度) で dashboard に出る
- **信頼性ガード (v1.9-E phase 1, L3)** — 同じツールを同じ引数で連続呼び出しする「stuck loop」を検出、profile-level policy (`warn` / `inject` / `break`) で対処

最後の v1.9.0 GA では v1.9.0a6 以降の実機検証で発見された **L3 `break` action の ingress 取りこぼし** (`ToolLoopBreakError` が catch されず 500 が返っていた) を 400 + 構造化 detail に修正、両 ingress 経路 (非 streaming HTTPException / streaming SSE error event) で揃えました。

- Tests: 828 → **830** (+2: break action 非 streaming 400 / streaming SSE error event)
- Runtime deps: 5 → 5 (29 sub-release 連続据え置き)
- Backward compat: 完全互換、profile / providers.yaml / API 全部変化なし
- v1.9.0a1〜a6 をまとめての GA、各 sub-release の詳細は本ファイル下部の alpha entry を参照

### Changes since v1.9.0a6 — E-4 break action の ingress 修正

#### `coderouter/guards/tool_loop.py`

- `ToolLoopBreakError.__init__` に `threshold: int` / `window: int` をキーワード必須で追加。ingress 側で 400 detail を組むときに config を再 lookup せずに済むよう、検出パラメータを exception 自体に carry させる
- docstring に「Anthropic ingress が catch して 400 + 構造化 detail に変換する」を明記 (a3 で約束していたが実装が伴っていなかった)

#### `coderouter/routing/fallback.py`

- `_apply_tool_loop_guard` の `raise ToolLoopBreakError(...)` で `threshold=profile.tool_loop_threshold, window=profile.tool_loop_window` を渡すよう更新

#### `coderouter/ingress/anthropic_routes.py`

- `ToolLoopBreakError` を import
- 非 streaming `messages()` に `except ToolLoopBreakError → HTTPException(status_code=400, detail=_tool_loop_break_detail(exc))` を追加。`detail` は flat dict:

  ```json
  {
    "error": "tool_loop_detected",
    "message": "tool loop detected on profile='test-loop-break': tool 'Read' repeated 3 times consecutively.",
    "profile": "test-loop-break",
    "tool_name": "Read",
    "repeat_count": 3,
    "threshold": 3,
    "window": 5
  }
  ```

  クライアントは `detail.error == "tool_loop_detected"` で branch 可能、`message` は `str(exc)` と同一でログ grep フレンドリー
- streaming `_anthropic_sse_iterator` に `except ToolLoopBreakError` ブランチを追加、Anthropic 標準 envelope (`error.type == "invalid_request_error"`) + `error.tool_loop` ネストで構造化フィールドを露出。HTTP は 200 のまま (StreamingResponse はヘッダ確定後で 4xx に切り替えられない、midstream-error と同じ事情)
- helper 2 つ: `_tool_loop_break_extension(exc)` (両形式で共有する detection payload) / `_tool_loop_break_detail(exc)` (非 streaming flat dict 構築)
- `args_canonical` は両形式から意図的に除外 (tool input にはユーザデータが含まれうるため、400 detail / SSE error event に流出させない)

#### Tests

- **`tests/test_ingress_anthropic.py`** + 2:
  - `_LoopBreakingEngine` クラス + `client_and_loop_breaking_engine` fixture を追加
  - `test_break_action_non_streaming_returns_400_with_structured_detail` — 400 + `detail.error="tool_loop_detected"` + 5 detection field + `args_canonical` 不在を検証
  - `test_break_action_streaming_emits_invalid_request_error_event` — 200 + 単発 SSE error event + Anthropic 標準 envelope + `error.tool_loop` ネスト + `args_canonical` 不在を検証

### v1.9 series summary

| sub | release | feature |
|---|---|---|
| a1 | v1.9-A | Cache Observability — `cache-observed` log + dashboard panel |
| a2 | v1.9-B | Cross-backend cache passthrough + capability gate + doctor cache probe |
| a3 | v1.9-E phase 1 | L3 Tool-loop detection guard (warn / inject / break) |
| a4 | v1.9-C | Adaptive Routing — health-based dynamic chain priority |
| a5 | v1.9-D | Cost-aware Dashboard — Anthropic prompt-cache aware |
| a6 | v1.9-A streaming patch | `_emit_cache_observed` を `stream_anthropic` に追加 (実装漏れ修正) |
| **GA** | **v1.9-E phase 1 patch** | **`break` action の ingress 400 取りこぼし修正** (本 entry) |

### Real-machine verification (2026-04-29, LM Studio + ollama)

```
E-2 (warn):    tool-loop-detected ... action: "warn"   → 200 OK + provider 応答
E-3 (inject):  tool-loop-detected ... action: "inject" → system に hint 追加 + 200 OK
                                                         + cache_read_input_tokens: 453 (prefix キャッシュ命中)
E-4 (break, non-stream): 400 + {"detail":{"error":"tool_loop_detected","profile":"test-loop-break",
                                           "tool_name":"Read","repeat_count":3,...}}
E-4 (break, stream):     200 + event: error
                               data: {"type":"error","error":{"type":"invalid_request_error",
                                      "tool_loop":{"profile":"test-loop-break","repeat_count":3,...}}}

C  (adaptive, 静止):  全 provider 同速 → static order 維持、`adaptive-routing-applied` 出ない
C  (adaptive, 発火):  サイズ差 chain (lmstudio 27B-dense 474ms / ollama qwen-coder-1.5b 134ms / openrouter-free n/a)
                      → global_median 304ms × 1.5 = 456ms、lmstudio 474ms ≥ 456ms → demote +1
                      → effective_order: [ollama-qwen-coder-1_5b, openrouter-free, lmstudio-...]
                      → 試験 4 回目から ollama-qwen-coder-1_5b 行きに切り替わって着地、
                         debounce 30s で oscillation も観察されず
```

E-2/E-3 は a3 で観察済み、E-4 (両形式) と C 発火パスは GA 直前に実機で初観察。verification.md には MoE モデルの罠 (Qwen3.6-35B-A3B は active 3.8B で速い) と rolling-window タイミング制約の注意を後追いで加筆予定 (本リリースには含まず)。

### Migration

不要。**v1.8.x / v1.9.0a* からの自然なアップグレード**:

- `coderouter` コマンド名 / Python import 名 / providers.yaml の format / env 変数 / ingress URL すべて完全に同じ
- `tool_loop_action` を未指定または `warn` / `inject` で運用していた profile は挙動完全変化なし
- `tool_loop_action: break` を既に使っていた profile のみ status code が 5xx → 4xx に変化 (a3〜a6 では実装バグで 500 Internal Server Error が返っていた、1.9.0 で docstring が約束する 400 + 構造化 detail に修正)。実運用で `break` を本番投入していたケースは想定されにくく、検証用途であれば修正後の方が期待挙動

### Out of scope (v1.10 以降)

v1.9 series は意図的に閉じる:

- **v1.9-B2** — `message_delta` event の usage 集約で、streaming 経路でも実 token 数 / cache_read / cache_creation を取得 (現状は `outcome=unknown` 固定)
- **v1.9-E phase 2** — L2 Memory pressure (LM Studio / ollama backend OOM 検知) / L5 Backend health (continuous probe + chain reorder)
- **v1.10-?** — plan.md §13 系 (multi-tenant routing, etc.) — 別 minor

### Files touched

```
M  CHANGELOG.md
M  coderouter/guards/tool_loop.py
M  coderouter/ingress/anthropic_routes.py
M  coderouter/routing/fallback.py
M  pyproject.toml
M  tests/test_ingress_anthropic.py
```

---

## [v1.9.0a6] — 2026-04-28 (v1.9-A streaming パスの cache-observed emit 漏れ patch)

**Theme: 実機検証で発見した v1.9-A の小さな実装ギャップを潰す。** v1.9-A の CHANGELOG / `CacheOutcome` docstring で「streaming レスポンスは `outcome=unknown` で記録される」と約束していたが、`stream_anthropic` 経路に `_emit_cache_observed` の呼び出しが実装漏れしていた (非 streaming `generate_anthropic` のみ実装済み)。実機で `curl -N stream:true` を投げても JSONL に `cache-observed` event が現れない事で発覚。doc で約束していた動作に実装を揃える。

- Tests: 826 → **828** (+2: streaming 成功時 emit / streaming 失敗時 emit せず)
- Runtime deps: 5 → 5 (28 sub-release 連続据え置き)
- Backward compat: 完全互換、profile / API 全部変更なし
- Pre-release: `1.9.0a6`

### Changes

#### `coderouter/routing/fallback.py` `stream_anthropic` に cache-observed emit を追加

- `_apply_tool_loop_guard` 直後に `request_had_cache_control = anthropic_request_has_cache_control(request)` を変数化 (v0.5-B の inline call と新規 emit 用 caller の二重評価を回避)
- successful stream の最後 (`async for ev in event_iter` 完走後、`return` の直前) に `log_cache_observed(...)` を呼ぶ
  - `outcome="unknown"` (v1.9-B が `message_delta` 集約するまで streaming は usage 取得しない約束)
  - `streaming=True`
  - tokens は all 0 (engine は streaming 経路の usage を集約していない、cost も 0)
- 非 streaming `generate_anthropic` の挙動には影響なし

#### Tests

- **`tests/test_fallback_cache_observed.py`** + 2:
  - `test_cache_observed_fires_on_streaming_with_unknown_outcome` — 成功 streaming で `outcome=unknown` / `streaming=True` / `request_had_cache_control=True` が記録される
  - `test_cache_observed_streaming_does_not_fire_on_provider_failure` — provider 失敗時は emit しない (非 streaming と同じ contract)
- 上記のため `_CacheAnthropicAdapter.stream_anthropic` を `NotImplementedError` raise から「3 events (start / delta / stop) を yield する minimal stream」に拡張

### Why

v1.9-A 検証中に「stream:true の curl を投げても `cache-observed` log が JSONL に出ない」を発見 (`docs/inside/verification.md` の A-3 検証パス)。v1.9-A の `CacheOutcome` docstring を読み直すと「streaming responses always pair with `outcome=unknown` until v1.9-B aggregates `message_delta`」と書いてあったが、実装が `generate_anthropic` のみで `stream_anthropic` には emit を入れ忘れていた。

これは **doc-implementation gap**: dashboard / metrics dashboard 利用者から見ると「streaming で動いているはずなのに observation が記録されない」という不整合になる。v1.9.0a6 は約束と実装を揃える小 patch。

副次的効果として A-3 (`hit_rate=null when only `unknown` observations`) の実機検証もこの patch で初めて可能になった。

### Migration

`pyproject.toml version 1.9.0a5 → 1.9.0a6`、`coderouter --version` は 1.9.0a6 を返す。**手元の `~/.coderouter/providers.yaml` は触らない限り完全に変化なし**。Streaming 経路のレスポンス内容も変化なし — log line が 1 件追加されるだけ。

### Files touched

```
M  CHANGELOG.md
M  coderouter/routing/fallback.py
M  pyproject.toml
M  tests/test_fallback_cache_observed.py
```

### Out of scope (v1.9-B 送り)

- `message_delta` event aggregation で streaming 時にも実 token 数 / cache_read / cache_creation を取得する → outcome を unknown 固定でなく実値で出せるようにする

---

## [v1.9.0a5] — 2026-04-28 (v1.9-D: Cost-aware Dashboard — Anthropic prompt-cache aware)

**Theme: 「いくら使ってる」を可視化、cache savings を別枠で。** v1.9-A で観測、v1.9-B で透過保証、v1.9-D で **金額に翻訳**。Anthropic の prompt-cache 価格モデル (cache_read 90% 割引、cache_creation 25% 増し) を最初から正確に実装、LiteLLM 競合品が **cache savings を別計算しない** 弱点を構造的にカバー。

`docs/inside/future.md` §5.5 の v1.9-D 範囲を実装。

- Tests: 811 → **826** (+15: pure compute_cost 8 / collector dispatch 4 / Prometheus exposition 3)
- Runtime deps: 5 → 5 (27 sub-release 連続据え置き)
- Backward compat: 完全互換、`providers.yaml` の `cost:` フィールドは optional (unset = 0 contribution)
- Pre-release: `1.9.0a5`

### Changes

#### `coderouter/cost.py` 新規 (~150 LOC)

- `CostBreakdown` dataclass — per-attempt cost components (input/output/cache_read/cache_creation USD + total + savings)
- `compute_cost_for_attempt(cost_config, *, input_tokens, ..., cache_creation)` 純関数:
  - 4 token bucket をそれぞれの rate で計算
  - cache_read tokens を `input_rate × cache_read_discount` で割引
  - cache_creation tokens を `input_rate × cache_creation_premium` で premium
  - savings = `cache_read tokens × input_rate × (1 - cache_read_discount)` (cache_creation は premium なので savings には入らない)
  - 負の token / None config / partial config に対する defensive 処理

#### Schema: `CostConfig` 新設

- **`coderouter/config/schemas.py`**: `CostConfig` BaseModel に `input_tokens_per_million` / `output_tokens_per_million` / `cache_read_discount=0.10` / `cache_creation_premium=1.25` を declare
- `ProviderConfig.cost: CostConfig | None = None` 追加 — opt-in、unset の provider (local 等) は dashboard に 0 contribution

#### Engine integration

- **`coderouter/routing/fallback.py`**: `_emit_cache_observed` を拡張、`provider_config: ProviderConfig | None = None` パラメータを受けて `compute_cost_for_attempt()` で per-attempt USD cost + savings を計算、log payload に折り込む
- `generate_anthropic` の call site で `adapter.config` を渡す

#### Logging schema 拡張

- **`coderouter/logging.py`** `CacheObservedPayload` に `cost_usd: float` / `cost_savings_usd: float` フィールド追加 (default 0.0、pre-v1.9-D caller は zero contribution で互換)
- `log_cache_observed` helper の signature にも optional kwargs 追加

#### MetricsCollector: per-provider cost aggregation

- **`coderouter/metrics/collector.py`**: `cache-observed` event の dispatch で cost を集計
  - `_cost_total_usd: dict[str, float]` (per-provider)
  - `_cost_savings_usd: dict[str, float]` (per-provider)
  - `_cost_total_usd_aggregate: float` / `_cost_savings_usd_aggregate: float` (process-wide)
- `snapshot()` 拡張:
  - `counters.cost_total_usd` / `cost_savings_usd` (per-provider dict)
  - `counters.cost_total_usd_aggregate` / `cost_savings_usd_aggregate` (process-wide)
  - 各 provider row に `cost: {total_usd, savings_usd}` panel
- `reset()` で v1.9-D state も clear
- 防御的: malformed cost values (str/None) → 0.0 default、handler は raise しない

#### Prometheus exposition

- **`coderouter/metrics/prometheus.py`**: 新 helper `_counter_float()` (float-valued counter、`.10g` formatter で trailing zero trim) + 2 つの新 metric:
  - `coderouter_cost_total_usd_total{provider}` — cumulative USD billed
  - `coderouter_cost_savings_usd_total{provider}` — cumulative cache savings USD

#### Tests (+15)

- **`tests/test_metrics_cost.py`** 新規:
  - `compute_cost_for_attempt`: None config / no cache / cache read discount / cache creation premium / combined / negative tokens defensive / partial config (7)
  - Collector dispatch: per-provider aggregation / zero cost no entry / per-row cost panel / reset / malformed values (5)
  - Prometheus: HELP+TYPE / per-provider labels / `_total` suffix (3)

### Why

`docs/inside/future.md` §5.5 で確立した「LiteLLM ですら未対応の cache savings 計算を最初から正確に実装」の具体実装。Anthropic 価格モデルを 4 token bucket × 4 multiplier で正確に表現、operator が「ローカル LLM 併用でいくら浮いたか」「Anthropic prompt cache でいくら節約できたか」を 1 画面で見える状態を実現。

**競合状況**:
- LiteLLM の cost tracker は `cache_read_input_tokens` を full input rate で billing (= overstate)、savings 別計算なし
- claude-code-router は cost tracking 自体なし
- v1.9-D は **Claude Code 系 OSS で唯一、cache-aware cost dashboard を持つ**

### Migration

`pyproject.toml version 1.9.0a4 → 1.9.0a5`、`coderouter --version` は 1.9.0a5 を返す。**手元の `~/.coderouter/providers.yaml` は触らない限り完全に変化なし**。

明示的に有効化する operator は paid provider に `cost:` ブロックを追加:

```yaml
providers:
  - name: anthropic-direct
    kind: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-8
    api_key_env: ANTHROPIC_API_KEY
    paid: true
    cost:                              # v1.9-D 新フィールド
      input_tokens_per_million: 3.00
      output_tokens_per_million: 15.00
      cache_read_discount: 0.10        # default、省略可
      cache_creation_premium: 1.25     # default、省略可
```

`coderouter serve` 起動後、`/metrics.json` の `counters.cost_total_usd` / `cost_savings_usd` で per-provider cost を取得可能。Prometheus scrape は `coderouter_cost_total_usd_total{provider="anthropic-direct"}` で取れる。

### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/logging.py
M  coderouter/metrics/collector.py
M  coderouter/metrics/prometheus.py
M  coderouter/routing/fallback.py
M  pyproject.toml
A  coderouter/cost.py
A  tests/test_metrics_cost.py
```

### Out of scope (次回以降)

- **`/dashboard` HTML cost panel**: snapshot schema は揃ったが UI 描画は v1.9-D2 で
- **`coderouter stats --cost` TUI**: 5 行サマリ CLI コマンドは v1.9-D2 で
- **期間別累積 (1 day / 1 week / 1 month)**: 現在 process-lifetime のみ。期間集計は SQLite persistence と組み合わせて v1.10 候補
- **OpenAI-shaped engine paths のコスト集計**: Anthropic 非 streaming 経路のみ。OpenAI ingress + streaming 対応は v1.9-C2 と同じ follow-up

---

## [v1.9.0a4] — 2026-04-28 (v1.9-C: Adaptive Routing — health-based dynamic chain priority)

**Theme: 「平常時の最適化」を chain に持ち込む。** 静的に declare した `providers` 順序を、live observed の median latency / error rate に基づいて自動再優先化。L5 (v1.9-E phase 3 予定) は二値 (HEALTHY/UNHEALTHY) で crash 対応するのに対し、C は連続値 gradient で **平常時の遅さ** を吸収する。両方とも同じ observation stream から動くが、適用ロジックが直交。

`docs/inside/future.md` §5.4 の v1.9-C 範囲を MVP 実装。**Anthropic 非 streaming パスのみ** 対応 (v1.9-C2 で OpenAI-shaped + streaming follow-up 予定)。

- Tests: 795 → **811** (+16: stats 4 / no-demote 3 / latency demote 2 / error-rate demote 2 / debounce 2 / engine integration 2 / constants pin 1)
- Runtime deps: 5 → 5 (26 sub-release 連続据え置き)
- Backward compat: 完全互換、既存 profile は default の `adaptive: false` で従来挙動を維持
- Pre-release: `1.9.0a4`、`pip install --pre coderouter-cli` で取得可能

### Changes

#### `coderouter/routing/adaptive.py` 新規 (~360 LOC)

- `AdaptiveAdjuster` クラス — per-process singleton (engine が 1 つ保持)
  - `record_attempt(provider, *, latency_ms, success, now=None)` — observation 記録、append on each engine attempt
  - `stats_for(provider, *, now=None) -> ProviderStats` — rolling-window から median latency + error rate 計算
  - `compute_effective_order(adapters, *, now=None) -> list[BaseAdapter]` — 静的 chain → 動的順序、debounce 適用
- `_ProviderObservation` / `_AdjusterState` / `ProviderStats` データクラス
- `_apply_debounce` 内部メソッド — `last_committed_rank` 比較で debounce window 内の rank 変更を pinning (両方向、demote→promote と promote→demote 両方)
- 定数:
  - `ROLLING_WINDOW_S = 60.0`
  - `LATENCY_DEMOTE_FACTOR = 1.5` (median × 1.5 を超えたら -1 段)
  - `ERROR_RATE_DEMOTE_THRESHOLD = 0.10` (10% 失敗で -2 段)
  - `DEBOUNCE_S = 30.0`
  - `MIN_SAMPLES_FOR_LATENCY = 3` / `MIN_SAMPLES_FOR_ERROR_RATE = 5`

#### Engine integration (`coderouter/routing/fallback.py`)

- `FallbackEngine.__init__` で `_adaptive_adjuster: AdaptiveAdjuster` を eager 構築。`@property` の `_adaptive` で lazy-fallback も用意 (legacy test `__new__` bypass パターンに対する resilience)
- `_resolve_anthropic_chain`: profile が `adaptive: true` のときに `_adaptive.compute_effective_order(base)` で chain を再優先化、その後 thinking-capable bucket logic に渡す
- `_profile_is_adaptive(profile_name)` ヘルパ — chain resolver と recording 側で同じ profile lookup を共有
- `generate_anthropic` の adapter 呼び出しを `time.monotonic()` で wrap、success/failure 両方で `record_attempt(...)` 呼び出し。auth-flavored failures (401/403) は latency_ms=None で記録 (短絡応答なので latency 信号として無意味)

#### Logging

- 新 event `adaptive-routing-applied` (info-level) — 静的 chain と effective chain order が異なるときのみ fire。payload に static_order / effective_order / per-provider stats を含む

#### Config schema

- `FallbackChain.adaptive: bool = False` 追加。既存 yaml はそのまま動く (default false)

#### Tests

- **`tests/test_routing_adaptive.py`** 新規 (+16 tests):
  - **Stats**: unseen / median は success のみ / window roll-off / error rate zero on empty (4)
  - **No demote**: empty chain / no obs / all fast (3)
  - **Latency demote**: 1.5× threshold / min samples gate (2)
  - **Error rate demote**: 10% threshold / min samples gate (2)
  - **Debounce**: pin within window / release after window (2)
  - **Engine integration**: static profile not invoking adjuster / adaptive profile invoking adjuster (2)
  - **Constants pin**: ROLLING_WINDOW_S / LATENCY_DEMOTE_FACTOR / ERROR_RATE_DEMOTE_THRESHOLD / DEBOUNCE_S / MIN_SAMPLES_* (1)

### Why

`docs/inside/future.md` §5.4 で確立した「task-based (auto_router、v1.6-A) + health-based (v1.9-C) の両軸対応」のうち health-based を実装。auto_router は request shape (intent) で profile を選ぶが、profile の chain 内 priority は static のまま。v1.9-C で chain 内 priority が live observed health に追従するようになり、両軸が初めて補完関係を成す。

**競合状況**: claude-code-router は task-based 単独、LiteLLM は session-cost-based、何れも latency-aware adaptive routing を持たない。CodeRouter は v1.9-C で **task-based + health-based 両軸** を持つ唯一の Claude Code 系 OSS という位置づけ。

### Migration

`pyproject.toml version 1.9.0a3 → 1.9.0a4`、`coderouter --version` は 1.9.0a4 を返す。**手元の `~/.coderouter/providers.yaml` は触らない限り完全に変化なし**。新フィールド `adaptive: false` がデフォルトなので、既存 profile はゼロ変更で従来動作を維持。

明示的に有効化する operator は profile に追加:

```yaml
profiles:
  - name: coding
    providers:
      - lmstudio-qwen3-5-9b
      - ollama-gemma4-26b
      - openrouter-free
    adaptive: true   # 平常時の latency / error rate に基づく動的優先度
```

### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/routing/fallback.py
M  pyproject.toml
A  coderouter/routing/adaptive.py
A  tests/test_routing_adaptive.py
```

### Out of scope (次回以降の v1.9-C2)

- **OpenAI-shaped engine paths**: `generate` / `stream` (非 Anthropic ingress) からの `record_attempt` 呼び出し。MVP では Anthropic 非 streaming のみカバー
- **Anthropic streaming**: `stream_anthropic` の latency 計測 (mid-stream success の境界をどこに置くか設計余地あり)
- **Dashboard panel**: `/dashboard` に effective chain order の可視化 (「static order vs current effective order」の差分強調表示)
- **MetricsCollector への adaptive 集計**: 現在は `adaptive-routing-applied` log のみ。将来 dashboard panel 用に reorder 回数 / 直近 reorder timestamp などを集計
- **L5 (v1.9-E phase 3)**: binary HEALTHY/UNHEALTHY backend swap。本実装の continuous gradient と棲み分け、両方とも同じ observation stream を消費する設計

---

## [v1.9.0a3] — 2026-04-28 (v1.9-E phase 1: L3 Tool-loop detection guard)

**Theme: Long-run reliability の最初の guard。** `docs/inside/future.md` §5.3 の v1.9-E は L2/L3/L5 の 3 系統障害を扱う 1-2 週間のまとまった作業。1 commit で全部やると重いので **L3 (Tool loop detection) → L2 (Memory pressure) → L5 (Backend health)** の 3 段階で alpha pre-release を切る。

L3 は最も isolated で HTTP 系の依存なし、~300 LOC、self-contained。「Claude Code を 8 時間連続で local LLM に向けて使っても止まらない」を訴求するための最初の具体実装。

- Tests: 779 → **795** (+16: pure detect 8 / inject mutation 3 / engine helper 5)
- Runtime deps: 5 → 5 (25 sub-release 連続据え置き)
- Backward compat: 完全互換、`providers.yaml` 編集不要 (新フィールドはすべて default 値あり)
- Pre-release: `1.9.0a3`、`pip install --pre coderouter-cli` で取得可能

### Changes

#### `coderouter/guards/` 新パッケージ + L3 detector

- **`coderouter/guards/__init__.py`** 新規 — Long-run guards のパッケージドッジ。L2 / L5 が今後追加される予定地。
- **`coderouter/guards/tool_loop.py`** 新規 (~250 LOC):
  - `detect_tool_loop(request, *, window, threshold) -> ToolLoopDetection | None` 純関数。直近 `window` 件の assistant `tool_use` ブロックの**末尾連続**で同一 `(name, args)` が `threshold` 回以上発生していると検知
  - `ToolUseRecord` / `ToolLoopDetection` データクラス
  - `inject_loop_break_hint(request, *, hint)` — system フィールドに hint を append (str / None / list-of-blocks の 3 形を吸収)
  - `ToolLoopBreakError` (CodeRouterError 派生) — `break` action 用 exception
  - `DEFAULT_LOOP_INJECT_HINT` 定数 — 「You appear to be calling the same tool with the same arguments repeatedly...」
  - **canonical-form JSON 比較** (`json.dumps(args, sort_keys=True)`) で `{"a":1,"b":2}` と `{"b":2,"a":1}` を同一視
  - **trailing-run only** 検出 — 過去に脱出済みの streak は無視 (現在状態のみが actionable)

#### Engine integration

- **`coderouter/routing/fallback.py`**: `_apply_tool_loop_guard(request, config)` ヘルパ追加。`generate_anthropic` / `stream_anthropic` の chain dispatch 直前で呼ばれる。Action 別の挙動:
  - `warn`: log のみ、request はそのまま
  - `inject`: log + `inject_loop_break_hint` で system 注入された新 request を返す
  - `break`: log + `raise ToolLoopBreakError`
- profile lookup 失敗時は silent no-op (chain resolution が別経路で error を出すので二重診断にならない)

#### Config schema

- **`coderouter/config/schemas.py`** `FallbackChain` 拡張:
  - `tool_loop_window: int = 5` (range 2-50)
  - `tool_loop_threshold: int = 3` (range 2-50)
  - `tool_loop_action: Literal["warn", "inject", "break"] = "warn"`
- 既存 profile はすべて default で warn-only として動作 → 既存 deployment はゼロ変更

#### Logging

- **`coderouter/logging.py`**: `tool-loop-detected` warn-level log shape を新設
  - `ToolLoopDetectedPayload` TypedDict (profile / tool_name / repeat_count / threshold / window / action)
  - `log_tool_loop_detected()` helper — 単一の chokepoint
- 3 つの action すべてが同じ log line を fire するので dashboard は detection 全件を捕捉できる (action は label として区別)

### Why

`docs/inside/future.md` §1 で確立した Vision「Local LLM で agent を長時間回すための信頼性層」の P3 (Long-run Reliability) の最初の具体実装。L3 が最も isolated で実装シンプル / テスト容易 / 単独で価値があり、最初の sub-release に最適。

「Claude Code が同じファイルを 5 回 Read し続ける」「Bash で同じコマンドを 3 回叩いて止まらない」というのは長時間 agent loop で頻出する典型症状で、L3 はその検知を request shape だけで完結させる (Claude Code は full conversation history を毎回送るので tail inspection で十分)。

**競合状況** (future.md §3 referenced): L3 を体系的に対処する Claude Code 系 OSS は 2026-04-27 時点で調査リスト中ゼロ。本実装は単独差別化軸として位置づく。

### Migration

`pyproject.toml version 1.9.0a2 → 1.9.0a3`、`coderouter --version` は 1.9.0a3 を返す。**手元の `~/.coderouter/providers.yaml` は触らない限り完全に変化なし**。新 schema フィールドはすべて default 値ありなので、既存 yaml はそのままロード可能で、警告の挙動も warn level (ログ出力のみ) なので既存処理に副作用なし。

明示的に有効化したい operator は profile に以下を追加:

```yaml
profiles:
  - name: long-running-agent
    providers: [...]
    tool_loop_window: 5
    tool_loop_threshold: 3
    tool_loop_action: inject   # または warn / break
```

### Files touched

```
M  CHANGELOG.md
M  coderouter/config/schemas.py
M  coderouter/logging.py
M  coderouter/routing/fallback.py
M  pyproject.toml
A  coderouter/guards/__init__.py
A  coderouter/guards/tool_loop.py
A  tests/test_guards_tool_loop.py
```

### Out of scope (次回以降の v1.9-E phase)

- **L2 (Memory pressure awareness)**: Ollama `/api/ps` / LM Studio `/v1/models` / llama.cpp `/proc/meminfo` 直読みで backend memory probe、95% 超で軽量 model に swap
- **L5 (Backend health continuous monitoring)**: 60s 周期の健康 probe、UNHEALTHY を chain 末尾に降格 / 復帰時に元 priority 戻し、dashboard に effective chain order
- **MetricsCollector への loop event 集計**: 現在は構造化 log のみ、将来 dashboard panel で「直近 24h の loop 検知 N 件」表示
- **inject hint の operator override**: 現在 `DEFAULT_LOOP_INJECT_HINT` のみ、将来 profile-level `tool_loop_inject_hint` で日本語化等可能に

---

## [v1.9.0a2] — 2026-04-28 (v1.9-B: Cross-backend cache passthrough + capability gate + doctor cache probe)

**Theme: v1.9-A の「観測」を「保証」へ。** capability registry に `cache_control` フィールドを新設し、Claude 4 family + LM Studio 経由 Qwen3.5/3.6 を bundled で宣言。doctor に新 probe `_probe_cache` を追加し、cache_control の round-trip (1 回目 creation → 2 回目 read) を実機 verify。

`docs/inside/future.md` §5.2 の v1.9-B 範囲を実装。挙動変更は capability gate 拡張のみで、既存の `provider_supports_cache_control` 呼び出しは下位互換 (registry 未宣言 anthropic-kind は引き続き True)。

- Tests: 759 → **779** (+20: registry resolution 12 / doctor cache probe 8)
- Runtime deps: 5 → 5 (24 sub-release 連続据え置き)
- Backward compat: 完全互換、`providers.yaml` / API 全部変更なし
- Pre-release: `1.9.0a2`、`pip install --pre coderouter-cli` で取得可能

### Changes

#### Capability registry: `cache_control` フィールド新設

- **`coderouter/config/capability_registry.py`**: `RegistryCapabilities` / `ResolvedCapabilities` に `cache_control: bool | None` フィールド追加。lookup walker に同フィールドを追加 (first-match-per-flag 既存 semantics に従う)。
- **`coderouter/data/model-capabilities.yaml`**: bundled で 5 rule 宣言:
  - `claude-opus-4-*` / `claude-sonnet-4-*` / `claude-haiku-4-*` (kind=anthropic): `cache_control: true` — api.anthropic.com で実機検証済 (2026-04-20、1321 tokens 書き / 1321 tokens 読み)
  - `qwen3.5-*` / `qwen3.6-*` (kind=anthropic): `cache_control: true` — LM Studio 0.4.12 `/v1/messages` で v1.8.4 実機検証済 (`cache_read_input_tokens: 280` 観測)
  - openai_compat 系は意図的に未宣言 (= None) → 既存の v0.5-B `capability-degraded reason=translation-lossy` log がそのまま fire

#### Capability gate: registry を consult

- **`coderouter/routing/capability.py`**: `provider_supports_cache_control` に `registry: CapabilityRegistry | None = None` kwarg を追加。解決順序を 3 段に:
  1. `provider.capabilities.prompt_cache: true` → True (explicit per-provider)
  2. registry の `cache_control: true|false` → 即決
  3. fallback: `provider.kind == "anthropic"` → True (pre-v1.9-B 互換)
- registry が `False` を返したら kind=anthropic でも False を返すので、upstream regression 時に operator が一時的に `cache_control: false` を user yaml で declare → `capability-degraded` log が fire するという escape hatch が成立

#### Doctor: `_probe_cache` 新 probe 追加

- **`coderouter/doctor.py`**: `_probe_cache` 関数を新設、orchestrator の最後 (streaming probe の後) に組み込み。auth fail 時の SKIP list にも追加。
  - 動作: 同一 body (~1900 token system prompt + `cache_control: ephemeral`) を 2 回 POST、1 回目で `cache_creation_input_tokens > 0`、2 回目で `cache_read_input_tokens > 0` を期待
  - **Verdict 4 種**:
    - **OK**: 2 回目で read > 0 → cache_control 配管が end-to-end 機能している
    - **NEEDS_TUNING**: 1 回目 creation 観測 / 2 回目 read=0 → TTL 短すぎ or cache key mismatch
    - **NEEDS_TUNING**: 両方とも creation/read 観測なし → upstream が cache_control を silent ignore (Anthropic compat 不完全) or 1024 token 最低未達
    - **SKIP**: not anthropic / 未宣言 / upstream 5xx / auth fail
  - **Gate は意図的に tight**: 2 paid HTTP call を消費するので、registry に `cache_control: true` 明示宣言 OR `providers.yaml capabilities.prompt_cache: true` のときのみ実行。kind=anthropic だけで自動実行はしない (unverified model に対して無駄な call を避ける)

#### Tests

- **`tests/test_capability_registry_cache_control.py`** 新規 (+12): registry resolution 4 / capability gate 5 / bundled YAML 検証 3
  - bundled が `claude-opus-4-8` / `claude-sonnet-4-7` / `claude-haiku-4-1` で `cache_control=true` を返すこと
  - bundled が `qwen3.5-9b` / `qwen3.6-35b-a3b` で `cache_control=true` を返すこと
  - bundled が `openai_compat` の `qwen2.5-coder:7b` で undeclared (None) のまま → translation-lossy gate fire を確実にする
- **`tests/test_doctor_cache_probe.py`** 新規 (+8): probe gate / OK round-trip / NEEDS_TUNING (no hit / no creation) / explicit prompt_cache opt-in / 1st call 5xx → SKIP / auth fail → SKIP

### Why

v1.9-A で「観測」した cache の動作を、v1.9-B で **どの (kind, model) が cache_control を保証するか** という contract に格上げ。doctor cache probe は **どの競合 (LiteLLM / claude-code-router / etc.) にもない機能** で、operator が「LM Studio で本当に cache が効いてるのか」を 1 コマンドで確認できる単独差別化軸。

LM Studio 0.4.12 を bundled YAML に組み込んだのは、v1.8.4 で実機確認した「Anthropic compat `/v1/messages` 経由で `cache_read_input_tokens: 280` が end-to-end 透過する」という事実を CodeRouter として保証宣言する意味がある。Qwen3.5/3.6 を `kind: anthropic` で declare している operator なら、`coderouter doctor --check-model lmstudio-qwen3-5-9b-anthropic` で OK が出れば prompt caching 実利用可能、という保証関係。

### Migration

`pyproject.toml version 1.9.0a1 → 1.9.0a2`、`coderouter --version` は 1.9.0a2 を返す。**手元の `~/.coderouter/providers.yaml` は触らない限り完全に変化なし**。

`provider_supports_cache_control` は kwarg `registry=None` を追加したので signature は backward-compatible (既存 caller は変更なし)。registry を consult した結果 `False` で hard-disable できるのが新機能だが、bundled YAML は positive 宣言のみ ship なので default 挙動は変化なし。

### Files touched

```
M  CHANGELOG.md
M  coderouter/config/capability_registry.py
M  coderouter/data/model-capabilities.yaml
M  coderouter/doctor.py
M  coderouter/routing/capability.py
M  pyproject.toml
A  tests/test_capability_registry_cache_control.py
A  tests/test_doctor_cache_probe.py
```

### Out of scope (次回以降)

- **v1.9-E (前倒し)**: Long-run Guards 三段 (L2 memory pressure / L3 tool loop / L5 backend health continuous) — Vision の核心実装
- **v1.9-C**: Adaptive Routing (rolling latency window + health-based dynamic priority)
- **v1.9-D**: Cost-aware Dashboard
- streaming aggregation: cache 観測の streaming 時 `outcome` 値を `cache_hit/creation/no_cache` に格上げ (v1.9-A の `unknown` から)

---

## [v1.9.0a1] — 2026-04-28 (v1.9-A: Cache Observability — Anthropic prompt caching を観測可能に)

**Theme: v1.9 シリーズ最初の alpha pre-release。Anthropic prompt caching の動作を CodeRouter 側で観測可能にし、`cache_read_input_tokens` / `cache_creation_input_tokens` を 4 分類 (cache_hit / cache_creation / no_cache / unknown) で per-provider 集計。**

`docs/inside/future.md` §5.1 の v1.9-A 範囲を実装。挙動は変えず、観測経路を追加するだけの安全な追加。LiteLLM の `cache_creation_input_tokens` undercounting バグ (future.md §3) を最初から避ける厳密 4 分類集計を導入。次の v1.9-B (cross-backend cache passthrough + capability gate / doctor cache probe) で能動的な cache 制御を追加予定。

- Tests: 737 → **759** (+22: classify_cache_outcome / collector dispatch / snapshot cache panel / Prometheus exposition / engine emission)
- Runtime deps: 5 → 5 (23 sub-release 連続据え置き)
- Backward compat: 完全互換、`providers.yaml` / `~/.coderouter/model-capabilities.yaml` / API 全部変更なし
- Pre-release: `1.9.0a1` の `a1` は PEP 440 alpha pre-release。`pip install --pre coderouter-cli` で取得可能。`v1.9.0` 正式版は v1.9-B/E/C/D も完了次第

### Changes

#### `cache-observed` 構造化ログイベント新設

- **`coderouter/logging.py`**: `CacheOutcome` Literal + `CacheObservedPayload` TypedDict + `log_cache_observed()` helper + `classify_cache_outcome()` 4 分類関数を追加。
  - `cache_hit`: `cache_read_input_tokens > 0` (cache 再利用、〜10% input rate)
  - `cache_creation`: `cache_creation_input_tokens > 0` かつ hit ではない (cache 書き込み、〜125% input rate)
  - `no_cache`: usage 受信したが cache フィールド 0/欠損 (cache_control 無し or upstream が握り潰した)
  - `unknown`: response に usage block 自体無し (streaming / openai_compat 経由 / pre-v1.9-A upstream)
- **理由**: `provider-ok` event に cache フィールドを混ぜると downstream consumers (collector / JSONL mirror / tests) すべてが新 schema 検証必要。専用 event なら streaming 時の `outcome=unknown` も自然に表現できる

#### Engine (`fallback.py`): 成功 response 毎に cache-observed を emit

- **`coderouter/routing/fallback.py`**: `generate_anthropic` の `provider-ok` 直後に `_emit_cache_observed()` 呼び出しを追加。`AnthropicResponse.usage.model_extra` から `cache_read_input_tokens` / `cache_creation_input_tokens` を抽出 (Pydantic `extra="allow"` 経由でラウンドトリップ済み)。
  - native Anthropic + LM Studio `/v1/messages` (`kind: anthropic`) → cache フィールド付き → 4 分類正しく出る
  - openai_compat → anthropic 変換経由 → cache フィールド無し → `outcome=no_cache` or `unknown`
- streaming aggregation は v1.9-B 送り (`message_delta` イベント集約が必要)、v1.9-A では非 streaming パスのみ対応

#### MetricsCollector: per-provider cache 集計

- **`coderouter/metrics/collector.py`**: `cache-observed` event を dispatch table に追加。新カウンタ:
  - `_cache_read_tokens: Counter[str]` (per-provider)
  - `_cache_creation_tokens: Counter[str]` (per-provider)
  - `_cache_outcomes: dict[str, Counter[str]]` (per-provider × 4-class)
  - `_cache_read_tokens_total: int` / `_cache_creation_tokens_total: int` (aggregate、毎 event で incremental 更新、snapshot 時の re-fold コスト回避)
- `snapshot()` 拡張: `counters.cache_*` (per-provider + aggregate) + 各 provider row に `cache: {read_tokens, creation_tokens, outcomes, hit_rate, observations}` panel を追加
  - **`hit_rate`** は `cache_hit / (cache_hit + cache_creation + no_cache)`、`unknown` は分母から除外 (signal 無しを 0% 表示するのを回避)
  - 観測無しなら `hit_rate=None`、dashboard で「—」表示できる
- `reset()` で v1.9-A state も clear

#### Prometheus exposition: 3 つの新 counter

- **`coderouter/metrics/prometheus.py`**:
  - `coderouter_cache_read_tokens_total{provider="..."}` — cache 再利用された input token 累計
  - `coderouter_cache_creation_tokens_total{provider="..."}` — cache 書き込み input token 累計
  - `coderouter_cache_observed_total{provider="...", outcome="cache_hit|cache_creation|no_cache|unknown"}` — 4 分類イベント数
- `hit_rate` を gauge で expose しないのは Prometheus 慣習に従い (`rate()` で derivative を計算する方が時間窓を正しく扱える)

#### Tests (+22)

- **`tests/test_metrics_cache.py`** (+11): `classify_cache_outcome` 4 cases / collector dispatch / snapshot cache panel / hit_rate=None for idle / unknown-only keeps None / reset clears state / 防御的非 int 受け入れ
- **`tests/test_metrics_prometheus_cache.py`** (+5): empty-snapshot HELP/TYPE / per-provider read/creation labels / outcome label pair / `_total` suffix
- **`tests/test_fallback_cache_observed.py`** (+6): cache_hit / cache_creation / no_cache outcome 別 / openai_compat 経路で no_cache or unknown / 失敗時 emit せず / chain fallthrough 時 winning provider のみ emit

### Why

`docs/inside/future.md` §1 で確立した Vision「Local LLM で agent を長時間回すための信頼性層」の 3 pillar 中、**P1 Connection Stability** の核心要素である Anthropic prompt caching を **観測可能に** することが v1.9 シリーズの最初のステップ。LM Studio 0.4.12 の Anthropic 互換 `/v1/messages` 経由で v1.8.4 に observed した `cache_read_input_tokens: 280` を、CodeRouter 側で **per-provider hit 率として集計・可視化** できるようになった。

LiteLLM cluster は `cache_creation_input_tokens` を `no_cache` に丸めて undercount する既知バグ (future.md §3 referenced) があり、CodeRouter は最初から 4 分類厳密集計でこれを回避。Claude Code 特化 OSS の中で **唯一の cache 観測機能** として位置づけ。

### Migration

`pyproject.toml version 1.8.5 → 1.9.0a1`、`coderouter --version` は 1.9.0a1 を返す。**手元の `~/.coderouter/providers.yaml` は触らない限り完全に変化なし**。

`/metrics.json` の counters / providers schema は **追加のみ** (新 key `cache_read_tokens` / `cache_creation_tokens` / `cache_outcomes`、provider rows に `cache` panel)、既存 dashboards は壊れない。Prometheus scraper は新メトリクス自動 discovery。

### Files touched

```
M  CHANGELOG.md
M  coderouter/logging.py
M  coderouter/metrics/collector.py
M  coderouter/metrics/prometheus.py
M  coderouter/routing/fallback.py
M  pyproject.toml
A  tests/test_fallback_cache_observed.py
A  tests/test_metrics_cache.py
A  tests/test_metrics_prometheus_cache.py
```

### Out of scope (次回以降)

- **v1.9-B**: cross-backend cache passthrough + capability gate (`capabilities.cache_control` registry / doctor cache probe / openai_compat strip warn) — 「観測」から「保証」へ
- **v1.9-E (前倒し)**: Long-run Guards 三段 (L2 memory pressure / L3 tool loop / L5 backend health) — Vision の核心実装
- streaming aggregation: `message_delta` event を集約して streaming 時も `outcome=cache_hit/creation/no_cache` を出せるようにする (v1.9-B 範囲)

---

## [v1.8.5] — 2026-04-28 (doctor NEEDS_TUNING メッセージを v1.8.3 thinking-aware budget の事実に揃える + `docs/lmstudio-direct.md` 新規)

**Theme: 文言の整合 patch + ドキュメント補完。**v1.8.3 で `tool_calls` / `num_ctx` / `streaming` の 3 probe に thinking-aware budget (256 / 1024) を入れた。今回はその事実を NEEDS_TUNING 時の detail メッセージに反映し、operator が「probe budget が小さすぎたのでは」と疑う余地をなくす。あわせて v1.8.4 で実機検証した LM Studio 0.4.12 経由経路を `docs/llamacpp-direct.md` と対をなす形で `docs/lmstudio-direct.md` (+ `.en.md`) として正式化。

- Tests: 737 → 737 (既存 assert は phrase-substring を見ていないので追従不要、新規 assertion は不足分を 1 件追加)
- Runtime deps: 5 → 5 (22 sub-release 連続据え置き)
- Backward compat: 完全互換、`providers.yaml` / `~/.coderouter/model-capabilities.yaml` / コード側 API 変更なし

### Changes

#### Doctor NEEDS_TUNING 文言更新 (suggestion を thinking-aware budget 前提に揃える)

- **`coderouter/doctor.py` `_probe_tool_calls`**: 「Common for quantized small models」を残しつつ、thinking モデル時は `Probed with thinking-aware budget (1024 tokens, covers reasoning_content plus the call) — this is a true tools=false case, not budget exhaustion.` を前置。非 thinking 時は `Probed with default budget (256 tokens) — the model produced no tool-shaped output at all.` を前置
- **`coderouter/doctor.py` `_probe_streaming`**: `finish_reason='length'` 偽陽性回避のため、thinking 時は `Probe sent max_tokens=1024 (thinking-aware), so the cap is server-side options.num_predict rather than the probe budget.` を前置。非 thinking 時は `Probe sent max_tokens=512;` 系を前置
- **`coderouter/doctor.py` `_probe_num_ctx`**: 「canary missing」3 ケース (declared=None / declared<threshold / declared>=threshold) すべてに、thinking モデル時は `Probe sent max_tokens=1024 (thinking-aware), so the miss is prompt-side truncation rather than reply truncation.` の budget note を追加。これで operator が「probe の reply budget が足りなかったのでは」という疑問を即座に消せる

#### Documentation 補完: `docs/lmstudio-direct.md` 新規

- **`docs/lmstudio-direct.md` / `.en.md` 新規** — v1.8.4 で実機検証した LM Studio 0.4.12 経由経路を `docs/llamacpp-direct.md` と対をなす形で 7 step + Troubleshooting で。M3 Max 64GB / Q4_K_M / Metal 想定 + GUI 操作前提の canonical recipe
  - Step 1: LM Studio install & Discover タブで Q4_K_M モデルダウンロード (Qwen3.5 9B / Qwen3.6 35B-A3B / Jackrong/Qwopus3.5-9B-v3-GGUF)
  - Step 2: Chat タブで Load Model (Context 32768 / GPU max / Flash Attention ON)
  - Step 3: Local Server タブで Port 1234 / Just-in-time Model Loading: ON / Start Server
  - Step 4: curl 直叩き (OpenAI 互換 + Anthropic 互換 両ルート、native tool_calls / native tool_use 両方確認)
  - Step 5: CodeRouter に provider 登録 (`kind: openai_compat` 経路 + `kind: anthropic` 経路の 2 種)
  - Step 6: doctor 6 probe で動作確認 (両ルートとも全 probe OK)
  - Step 7: CodeRouter 経由 end-to-end (Anthropic prompt caching `cache_read_input_tokens: 280` 観測も含む)

### Why

v1.8.3 で `tool_calls` probe の active-harmful 誤診断 (thinking モデルに対して `tools: false` 提案) を fix したが、メッセージ文面はそのまま v1.8.2 以前の言い回し (「Common for quantized small models」のみ) を残していた。operator が NEEDS_TUNING を見たときに「probe budget が小さすぎたのでは」「v1.8.2 のバグの再発では」と疑う余地が文面上残っていたのを、**実装が既に thinking-aware なので断定できる** という事実に文言を揃える。診断ツールの出力は実装の confidence を反映すべき。

`docs/lmstudio-direct.md` は v1.8.4 で実機検証 + `examples/providers.yaml` に provider 例追加までは済ませていたが、`docs/llamacpp-direct.md` と並ぶレベルの canonical recipe ドキュメントが欠けていた。LM Studio 経由が現時点で最も `qwen35` / `qwen35moe` architecture を安定して動かせる経路 (Anthropic prompt caching まで透過) なので、operator が辿り着けるドキュメントとして正式化。

### Migration

`pyproject.toml version 1.8.3 → 1.8.5`、`coderouter --version` は 1.8.5 を返す。**手元の `~/.coderouter/providers.yaml` は触らない限り完全に変化なし**。doctor 出力の文面が変わるが verdict と suggested_patch の semantic は完全互換。

### Files touched

```
M  CHANGELOG.md
M  coderouter/doctor.py
M  pyproject.toml
A  docs/lmstudio-direct.md
A  docs/lmstudio-direct.en.md
```

---

## [v1.8.3] — 2026-04-26 (tool_calls probe も thinking モデル対応 + adapter で `reasoning_content` strip — llama.cpp 直叩き対応)

**Theme: v1.8.2 と同日リリースの第 2 弾 patch。Qwen3.6:35b-a3b on llama.cpp の実機検証で発見した 2 つの追加課題 — `tool_calls` probe の thinking モデル偽陽性 + llama.cpp が emit する `reasoning_content` フィールドの adapter strip 不足 — を解消。**

v1.8.2 リリース直後、note 記事 v1.8.2「自分が作った診断ツールに自分が騙された話」の続編として **「Ollama 経由で詰んだ Qwen3.6 を Unsloth GGUF + llama.cpp 直叩きで動かしたら native tool_calls が完璧に出た」** を実機検証中、CodeRouter doctor で `tool_calls [NEEDS TUNING]` が依然として出る矛盾に直面。深掘りで `tool_calls` probe の `max_tokens=64` が thinking モデルで `reasoning_content` トークン消費に食い切られる **v1.8.2 で num_ctx / streaming に対して直したのと完全に同じバグ pattern が tool_calls probe にも残っていた** ことが判明。あわせて llama.cpp の `reasoning_content` フィールド (Ollama / OpenRouter は `reasoning`) が openai_compat adapter の strip 対象に入っていなかった事実も発見。両者を v1.8.3 として 1 patch に統合。

**Ollama 経由詰みの真因が完全確定**: Ollama の chat template / tool 仕様未成熟、モデル本体は健全。llama.cpp 直叩きでは Qwen3.6 系の `tool_calls` が native で動作。

- Tests: 733 → **737** (+4: tool_calls probe budget thinking variant / reasoning_content strip 3 件)
- Runtime deps: 5 → 5 (21 sub-release 連続据え置き)
- Backward compat: 完全互換、`providers.yaml` / `~/.coderouter/model-capabilities.yaml` 編集不要

### Changes

#### Doctor `tool_calls` probe: thinking モデル対応バジェット

- **`coderouter/doctor.py`**: `_probe_tool_calls` の `max_tokens` を `64` 固定から **thinking 検出付きの動的選択** (256 default / 1024 thinking) に変更。`_TOOL_CALLS_PROBE_MAX_TOKENS_DEFAULT/_THINKING` 定数を新設、既存の `_is_reasoning_model(provider, resolved)` ヘルパで分岐。
  - 旧 64 では Qwen3.6:35b-a3b on llama.cpp が `reasoning_content` で 64 token 食い切り → `tool_calls` 出力前に length cap → **NEEDS_TUNING + suggested patch 「`tools: false` にしろ」という真逆の推奨** を出していた
  - 新 1024 で thinking + tool_call が両方収まる headroom

#### Adapter: `reasoning_content` フィールド strip 追加

- **`coderouter/adapters/openai_compat.py`**: `_strip_reasoning_field` を `_NON_STANDARD_REASONING_KEYS = ("reasoning", "reasoning_content")` の両方を strip するように拡張。
  - `reasoning` (Ollama / OpenRouter 命名) と `reasoning_content` (llama.cpp `llama-server` 命名) は同じ概念で、ベンダー命名が違うだけ
  - 厳格な OpenAI client はどちらも unknown key として reject するので、両方 strip するのが正しい
  - `capability-degraded` log の `dropped` フィールドも `["reasoning", "reasoning_content"]` に更新 (両方 strip し得ることを表現)

#### Doctor `reasoning-leak` probe: `reasoning_content` 検出

- **`coderouter/doctor.py`**: `_probe_reasoning_leak` の `has_reasoning` 判定を `"reasoning" in msg or "reasoning_content" in msg` に拡張。llama.cpp 経由 provider でも reasoning leak を informational に検出可能に。

#### Tests

- **`tests/test_doctor.py`** + 1: `test_tool_calls_max_tokens_bumped_for_thinking_provider` (thinking provider で tool_calls probe が 1024 を要求、native tool_calls 応答で OK 判定)
- **`tests/test_reasoning_strip.py`** + 3: `test_strip_helper_removes_reasoning_content_field` / `test_strip_helper_removes_both_reasoning_and_reasoning_content` / `test_strip_helper_removes_reasoning_content_from_delta` (各 layer で `reasoning_content` 除去確認)
- 既存 `tests/test_reasoning_strip.py` の `recs[0].dropped == ["reasoning"]` を `["reasoning", "reasoning_content"]` に更新 (log の表現変更に追従)

### Why

v1.8.2 で「diagnostic ツール自身も diagnostic され続ける必要がある」というメタ教訓を書いた直後、まさにそのことを実証する形で残バグが発見された。`tool_calls` probe は num_ctx / streaming probe と同じ「thinking モデルの reasoning トークン消費を考慮していない `max_tokens=64`」問題を抱えていて、しかも doctor の出した suggested patch (`tools: false` に倒せ) は **完全に逆の対処を勧めていた** — false-positive どころか、誠実なユーザーが従うと healthy なモデルを抑制してしまう **active-harmful な誤診断**。

これは v1.8.2 の patch を当てる時点で見つけるべきだった見落としで、note 記事 v1.8.2 のメタ教訓「diagnostic ツール自身も diagnostic され続ける」が現実に試された格好。素早く v1.8.3 で潰す。

`reasoning_content` strip 追加は llama.cpp 直叩き経路を CodeRouter から綺麗に使えるようにする ergonomic 改善で、`v1.8.x` patch 候補で plan.md に記録済みだった項目を実機発見と同時に消化。

### Migration

`pyproject.toml version 1.8.2 → 1.8.3`、`coderouter --version` は 1.8.3 を返す。**手元の `~/.coderouter/providers.yaml` は触らない限り完全に変化なし**。

v1.8.2 で Qwen3.6 / Gemma 4 系 thinking provider に対して `tool_calls [NEEDS TUNING]` が出ていたユーザーは v1.8.3 で再実行すると **OK** 判定 (実機で動いていた provider が doctor 上でも妥当に評価される)。llama.cpp 直叩き provider を使っているユーザーは `reasoning_content` が client に流れることなく綺麗に strip される。

### Files touched

```
M  CHANGELOG.md
M  coderouter/adapters/openai_compat.py
M  coderouter/doctor.py
M  pyproject.toml
M  plan.md
M  docs/troubleshooting.md
M  tests/test_doctor.py
M  tests/test_reasoning_strip.py
```

### Post-release docs followup (同 commit ではなく追加 commit で)

llama.cpp 直叩き経路を canonical な救済路として正式採用したのを受け、関連ドキュメントを v1.8.3 後に整理:

- **`docs/llamacpp-direct.md` / `.en.md` 新規** — `llama.cpp` build → Unsloth GGUF → `llama-server` → CodeRouter 接続を 7 step + Troubleshooting で。M3 Max 64GB / Q4_K_M / Metal 想定の canonical recipe
- **`setup.sh`**: 48 GB+ tier の推奨を旧 `qwen3.6:35b` → `gemma4:26b` に変更 (Ollama 経由詰みのため)。upgrade hint からも Qwen3.6 系を撤去、代わりに `docs/llamacpp-direct.md` への誘導を追加
- **`docs/quickstart.md` / `.en.md`**: 「より良いモデル」セクションの `ollama pull qwen3.6:35b` を撤去、`docs/llamacpp-direct.md` への誘導追加
- **`docs/hf-ollama-models.md`**: `ollama pull qwen3.6:35b` を「⚠️ Qwen3.6 系は Ollama 経由で詰みやすい」警告に置換、llama.cpp 直叩き経路の案内を追加
- **`README.md` / `.en.md`**: ドキュメント目次に「llama.cpp 直叩きガイド」行を追加、英語版言語スイッチャーにも `llama.cpp direct` リンクを追加
- **`examples/providers.yaml`**: `llamacpp-qwen3-6-35b-a3b` provider 例を追加 + `coding` profile chain primary に組み込み (詳細コメント付き)。Qwen3.6 系 Ollama 経路のコメントも v1.8.3 結果反映で更新
- **`tests/test_setup_sh.py`**: 48 GB / 64 GB tier の expected_model assertion を `qwen3.6:35b` → `gemma4:26b` に追従更新

---

## [v1.8.2] — 2026-04-26 (doctor probe を thinking モデル対応に — Gemma 4 偽陽性の解消)

**Theme: v1.8.1 リリース直後の深掘りで `doctor` の `num_ctx` / `streaming` probe が thinking モデルに対して偽陽性 NEEDS_TUNING を出していた事実を発見、probe の `max_tokens` バジェットを reasoning トークン消費分込みで設計し直した patch。**

v1.8.1 で `coding` profile primary に置いた Gemma 4 26B の doctor 結果が `tool_calls [OK]` + `num_ctx [NEEDS TUNING]` + `streaming [NEEDS TUNING]` で「中途半端に動く」と判定されていたが、実機で curl 直叩きすると **Ollama OpenAI-compat 経由でも 5K トークンの canary echo-back に成功** することが判明。原因切り分けの結果、Gemma 4 が emit する非標準 `reasoning` フィールドが doctor probe の `max_tokens=32` (num_ctx) / `max_tokens=128` (streaming) を**思考トークンで食い切って `content=""` で `finish_reason='length'`** を返していた偽陽性と確定。実機検証 (M3 Max 64GB / Ollama 0.21.2) で Anthropic 互換 `/v1/messages` 経由 Gemma 4 26B が "Hello." を 2 秒で返すことも確認、**Gemma 4 26B は実用 OK** と最終判定。

- Tests: 730 → **733** (+3: thinking provider declaration / registry-based / streaming の 3 件)
- Runtime deps: 5 → 5 (20 sub-release 連続据え置き)
- Backward compat: 完全互換、`providers.yaml` / `~/.coderouter/model-capabilities.yaml` 編集不要

### Changes

#### Doctor probe: thinking モデル対応バジェット選択

- **`coderouter/doctor.py`**: `_probe_num_ctx` / `_probe_streaming` の `max_tokens` を thinking 検出付きの動的選択に変更。新 helper `_is_reasoning_model(provider, resolved)` が provider declaration / registry resolved の両方から `thinking` / `reasoning_passthrough` の真偽を見て、reasoning モデルのときだけ大きいバジェットを選ぶ。
  - `_NUM_CTX_PROBE_MAX_TOKENS_DEFAULT = 256` (旧 32)、`_NUM_CTX_PROBE_MAX_TOKENS_THINKING = 1024`
  - `_STREAMING_PROBE_MAX_TOKENS_DEFAULT = 512` (旧 128)、`_STREAMING_PROBE_MAX_TOKENS_THINKING = 1024`
  - 非 thinking モデルは natural stop で早期終了するので無駄消費なし、thinking モデルは reasoning trace + 答えが収まる headroom

#### Registry: 既知 thinking モデルに `thinking: true` 宣言

- **`coderouter/data/model-capabilities.yaml`**: `gemma4:*` / `google/gemma-4*` / `qwen3.6:*` / `qwen/qwen3.6-*` に `thinking: true` を追加。これらは Ollama 経由で `reasoning` フィールドにかなりのトークンを吐く設計と確認済み。registry 経由で渡るので user は `providers.yaml` を触らなくても doctor の thinking バジェットが効く
- **Qwen3.6 セクションのコメント更新**: v1.8.1 時点で「Ollama silent cap」と書いていた part を「**v1.8.2 で num_ctx / streaming は doctor 偽陽性と判明、tool_calls [NEEDS TUNING] が真の課題として残る**」に整理。`claude_code_suitability` 撤回判断は維持 (Qwen3.6 の tool_calls 不全は thinking 起因ではない別の真の課題)

#### Tests

- **`tests/test_doctor.py`**: 3 件追加
  - `test_num_ctx_max_tokens_bumped_for_thinking_provider_declaration`: `provider.capabilities.thinking=True` → 1024
  - `test_num_ctx_max_tokens_bumped_when_registry_says_thinking`: provider 宣言なし + registry 宣言あり → 1024
  - `test_streaming_max_tokens_bumped_for_thinking_provider`: streaming probe も同経路で 1024 になる
- 既存 `test_num_ctx_request_body_merges_extra_body_options` の `max_tokens == 32` assertion を `== 256` に更新 (新 baseline)
- 既存 `test_streaming_request_body_carries_stream_true_and_merges_extra_body` に `max_tokens == 512` assertion を追加 (streaming baseline)

### Why

v1.8.1 article 執筆中に「note の流行モデル → ollama pull → 動かない」のうち Gemma 4 だけ `tool_calls [OK]` の **逆転勝利** だったはずが、`num_ctx [NEEDS TUNING]` も出ていて記事として煮え切らない状態だった。深掘りの結果、`/v1/chat/completions` 経由でも options は効く / `ollama ps` で context length 262144 が出る / **でも doctor は失敗** という矛盾を観測。`.choices[0].message.reasoning` フィールドに思考トークンが流れて `max_tokens=32` を消費していた事実を確認、**doctor 側の probe 設計が thinking モデル時代に追いついていない**ことが判明。

これは「実機 evidence first」原則 (plan.md §5.4) の更に一段下のメタ教訓：**diagnostic ツール自身も diagnostic され続ける必要がある**。

### Migration

`pyproject.toml version 1.8.1 → 1.8.2`、`coderouter --version` は 1.8.2 を返す。**手元の `~/.coderouter/providers.yaml` は触らない限り完全に変化なし**。

v1.8.1 で Gemma 4 26B を `claude_code_suitability` 抑え目に運用していたユーザーは v1.8.2 で doctor 再実行すると `num_ctx [OK]` + `streaming [OK]` まで通るはず。Qwen3.6 系の `tool_calls [NEEDS TUNING]` は本物 (thinking 起因ではない) なので引き続き coding chain primary には推奨しない。

### Files touched

```
M  CHANGELOG.md
M  coderouter/data/model-capabilities.yaml
M  coderouter/doctor.py
M  pyproject.toml
M  plan.md
M  docs/troubleshooting.md
M  docs/articles/v1-saga/note-1-v1-8-1-reality-check.md   (or new file v1-8-2)
M  tests/test_doctor.py
```

---

## [v1.8.1] — 2026-04-26 (実機検証反映 patch — mode_aliases 解決 + Gemma 4 第一候補化 + Ollama 既知問題ドキュメント化)

**Theme: v1.8.0 出荷直後の実機検証 (M3 Max 32GB / Ollama 0.21.2) で踏んだ問題 3 件を patch で解消。**

v1.8.0 の用途別 4 プロファイルが、NIM example yaml ベースで運用しているユーザーで `coderouter serve --mode coding` が **`default_profile 'coding' is not declared in profiles`** エラーで起動失敗する loader bug が判明。あわせて `coding` profile の primary に置いていた Qwen3.6:27b/35b が Ollama 経由で実用厳しい (num_ctx silent cap / tool_calls 0 / streaming 0 chars) ことも実機検証で確認。**「note 記事や HF 評判が高くても Ollama 経由ですぐ動くとは限らない」現実**を troubleshooting.md §4-2 として明文化。

- Tests: 729 → **730** (+1: loader の mode_aliases 解決テスト)
- Runtime deps: 5 → 5 (19 sub-release 連続据え置き)
- Backward compat: 完全互換、`providers.yaml` 編集不要 (loader が alias 経由で解決)

### Changes

#### Bug fixes (実機検証で踏んだもの)

- **`coderouter/config/loader.py`**: `CODEROUTER_MODE` env (= `--mode` CLI) が **`mode_aliases` を解決せず直接 `default_profile` に代入** していた v0.6-A の素朴実装を修正。runtime の `X-CodeRouter-Mode` ヘッダ (v0.6-D) は alias 解決していたので、startup と runtime で semantic 非対称だった。v1.8.1 で env_mode を `mode_aliases` 経由で解決してから `default_profile` 代入する流れに揃え、両者を symmetric に。これで `cr serve --mode coding` が NIM example yaml (profiles=`[claude-code-nim, ...]`、mode_aliases=`{coding: claude-code-nim}`) でも validation エラーにならず起動する
- **`examples/providers.nvidia-nim.yaml`**: v1.8.0 で main `providers.yaml` に追加した `mode_aliases` (default/coding/general/multi/reasoning/fast/cheap/think/vision) を NIM example yaml にも追加。NIM ユーザーも `--mode coding|general|reasoning|multi` を canonical な短縮 alias として使えるように

#### `coding` profile primary を実機検証反映に調整

- **`examples/providers.yaml`**: `coding` profile の providers リスト先頭を Qwen3.6:35b/27b → **`ollama-qwen-coder-14b` / `ollama-gemma4-26b` / `ollama-qwen-coder-7b` / `ollama-qwen3-coder-30b`** の順に変更。Qwen3.6 系は末尾退避線にコメントアウトで降格 (LM/llama.cpp が後日対応強化されたら primary に戻す候補として残置)。順序原則「枯れて確実に動くもの」を上に、note 推奨の新しいものは安定確認後に昇格、を反映
- **`coderouter/data/model-capabilities.yaml`**: `qwen3.6:*` / `qwen/qwen3.6-*` の `claude_code_suitability: ok` を**撤回**。v1.7-B 追加時は note 記事の伝聞ベースで先回り宣言していたが、v1.8.1 実機検証で num_ctx / tool_calls / streaming すべて NEEDS_TUNING 確認、確証ない以上 `tools` 宣言だけ残して suitability は出さない方針に。実機で動いた人は `~/.coderouter/model-capabilities.yaml` で `claude_code_suitability: ok` を user-side override 可能 (registry の first-match-per-flag walk が user → bundled の順序なので)

#### Documentation: 実機 Ollama 運用の Known Issues 追加

- **`docs/troubleshooting.md` §4-2 新設「ローカル Ollama 経由で踏みやすい既知問題」**:
  - **§4-2-A**: Qwen3.6:27b/35b が Ollama 0.21.2 経由で実用厳しい (num_ctx silent cap / tool_calls 0 / streaming 0)、`/no_think` でも改善せず。回避は Gemma 4 / Qwen2.5-Coder を上位に
  - **§4-2-B**: Qwen3.5 系 HF 蒸留モデル (Qwopus3.5 等) は llama.cpp が `qwen35` architecture (hybrid Transformer-SSM) 未対応で `unable to load model` 500 エラー。フレームワーク本体の対応待ち
  - **§4-2-C**: Gemma 4 26B が無加工で tool_calls OK 確認、note 記事「日常の王者」評価が裏付け
  - **§4-2-D**: ベスト実践「枯れたモデル + 観測ツール (doctor)」、HF で見つけた新モデルは `ollama run` → server log で `unknown model architecture` 確認、出たら今は諦め

### Why

v1.8.0 出荷で「用途別 4 プロファイルで `--mode coding` が使える」と謳ったが、NIM example yaml ベースのユーザーが踏むことが分かった loader bug は**最初の実プロンプト到達前に validation で死ぬ**ので最重要修正。あわせて、v1.8.0 example の primary に置いていた Qwen3.6 系列が実機で 3 つの probe NEEDS_TUNING を出すこと、Qwen3.5 ベース HF 蒸留が llama.cpp 未対応であることは、**「先回り実装より実機 evidence」原則** (plan.md §5.4) を再確認させる結果。

### Migration

`pyproject.toml version 1.8.0 → 1.8.1`、`coderouter --version` は 1.8.1 を返す。**手元の `~/.coderouter/providers.yaml` は触らない限り完全に変化なし**。

NIM example ベースで `cr serve --mode coding` が動かなかったユーザーは、

```bash
# 最新 example をコピー (v1.8.1 で mode_aliases 追加済み)
cp examples/providers.nvidia-nim.yaml ~/.coderouter/providers.yaml
# あるいは手で mode_aliases セクションを既存ファイルに追加
```

または、`cr` をローカル開発版から再 install:

```bash
uv tool install --reinstall --force --from /path/to/CodeRouter coderouter-cli --with ruamel.yaml
```

### Real-machine verification

```
$ pytest -q
730 passed, 1 skipped in 1.86s

$ ruff check coderouter/ tests/
All checks passed!

$ cr serve --port 8088 --mode coding   # NIM example yaml でも起動成功
$ cr doctor --check-model ollama-gemma4-26b --apply   # tool_calls OK 確認済み
```

### Out of scope / 次回送り

- Qwen3.5 系 HF 蒸留 (Qwopus / 類似): llama.cpp が `qwen35` architecture を実装したら再評価
- Qwen3.6:27b/35b の Ollama 経由動作: Ollama / llama.cpp 側の改善があれば再評価、`claude_code_suitability` を再付与の検討
- v1.7-C 候補 (network audit / launcher / 起動時 update check) は引き続き需要待ち

---

## [v1.8.0] — 2026-04-26 (用途別 4 プロファイル + GLM/Gemma 4/Qwen3.6 公式化 + apply 自動化)

**Theme: 「Claude Code で意味合いがズレない代替モデル」を operator に渡す minor。** plan.md §11.B (v1.7-B umbrella) を 6 タスクで一気に消化:

1. **PyPI Trusted Publishing** 自動化 — `git tag v* && git push` だけで release.yml が PyPI publish + GitHub Release 草稿を自動作成。API トークン不要 (OIDC)
2. **`claude_code_suitability` hint** — capability registry に `Literal["ok", "degraded"] | None` フィールド新設、Llama-3.3-70B 系を `claude-code-*` profile に置くと startup で `chain-claude-code-suitability-degraded` warn を構造化 emit。v1.6.2 で docs 化した「`こんにちは` → `Skill(hello)` 暴走」罠の自動検出版
3. **`coderouter doctor --check-model --apply` / `--dry-run`** — doctor 提案の YAML パッチを `providers.yaml` / `model-capabilities.yaml` に**非破壊**書き戻し (コメント・key 順序 100% 保持)。`--dry-run` は `git apply` 互換 unified diff、`--apply` は `.bak` バックアップ作成 + 冪等 (二回目は no-op)。`ruamel.yaml` を optional dep (`[doctor]` extras) で lazy import → base 5 deps streak 維持
4. **`setup.sh` onboarding ウィザード** — RAM 自動検出 → 推奨ローカルモデル提案 → `ollama pull` → `~/.coderouter/providers.yaml` 生成。`--ram-gb N` / `--non-interactive` / `--no-pull` / `--dry-run` / `--force` のフラグ整備、bash 3.2 互換、新規依存ゼロ
5. **examples/providers.yaml を 4 プロファイル構成に拡張** — `multi` (default) / `coding` / `general` / `reasoning`、各プロファイルに `append_system_prompt` で Claude 風応答を nudge、`mode_aliases` で `default/fast/vision/think/cheap` ショートカット
6. **Gemma 4 / Qwen3.6 / Z.AI (GLM-4.7/5.1) を providers.yaml に登録** — Ollama 公式 tag 化された `gemma4:e4b/26b/31b` (note 推奨 26B-A4B 含む) と `qwen3.6:27b/35b` (note "local champ" 35b-a3b) を active stanza として登録、note 記事推奨モデルを各 profile primary に格上げ。Z.AI を OpenAI-compat で 2 base_url 提供 (Coding Plan / General API)、unauthorized-tool 警告込みで明文化。`bundled model-capabilities.yaml` に `qwen3.6:*` (claude_code_suitability=ok) / `gemma4:*` / `GLM-5*` / `GLM-4.[5-9]*` family を新規宣言

- Tests: 651 → **710** (+59, +9.1%): `tests/test_claude_code_suitability.py` (6, walker + payload + opt-out), `tests/test_capability_registry.py` (+11 schema/lookup/bundled-yaml), `tests/test_doctor_apply.py` (25, parse/merge/apply/idempotent), `tests/test_setup_sh.py` (17 + 1 shellcheck-skip, RAM 推奨 / 既存ファイル衝突 / dry-run / parent dir 作成), `tests/test_examples_yaml.py` (+5, 4 profiles 存在 / append_system_prompt 必須 / mode_aliases / coding head 検証)
- Runtime deps: 5 → 5 (18 sub-release 連続据え置き; `ruamel.yaml` は `[project.optional-dependencies].doctor` の optional)
- Backward compat: `default_profile: default` → `default_profile: multi` への変更を伴うため、**`examples/providers.yaml` を ~/.coderouter/providers.yaml にコピーし直すと挙動が変わります**。手元の `providers.yaml` は触らない限り変化なし。`mode_aliases.default → multi` で旧 default 呼び出しは multi に解決される後方互換あり

### Theme: 「意味合いがズレない代替モデル」を 3 段の対策で実現

Claude Code を主用途とするユーザーが直面する核心問題は、ローカル/オープンモデルへ fallback したときに **応答の "性格"** が Claude Sonnet/Opus と乖離して「なんでだろ?」と混乱することでした。v1.8.0 はこれに 3 段で対処:

1. **モデル選定を Claude 風に寄せる** — Qwen3.6 35B-A3B (note 記事 "local champ") と Qwen3-Coder family を coding 主軸に。Llama-3.3-70B は引き続き `claude_code_suitability: degraded` で claude-code chain から自動退避。Gemma 4 26B-A4B (note "日常の王者") を multi/general に
2. **`append_system_prompt` で nudge** — 4 プロファイルすべてに「Match Claude Sonnet's coding style」「Match Claude Haiku's style」等の指示を載せ、非 Claude モデルでも応答スタイルが寄るように。プロファイル単位で適用 (v0.6-B 既実装機能)
3. **`output_filters` で表面差をクリーンアップ** — Qwen 系の `<think>` リーク・stop marker は引き続き strip (v1.0-A)。Qwen3.6 / Qwen3-Coder 30B には `[strip_thinking, strip_stop_markers]` を default で付与

### Z.AI (GLM family) — Coding Plan の落とし穴と回避策

Z.AI の GLM-4.7 / 5.1 は note 記事で「intent 理解が Claude Opus 級」と評価される強力な選択肢。OpenAI 互換エンドポイントなので CodeRouter は `kind: openai_compat` でそのまま接続できますが、Coding Plan の規約に注意:

公式 docs (docs.z.ai/devpack/overview) は「**未認可サードパーティツール経由のアクセスは benefit が制限される可能性**」と明記しています。CodeRouter は Anthropic API 互換 ingress を持つので Claude Code から見て認可ツールに見えるはずですが、Z.AI 側の検出ロジック次第で「router 経由」と判定されるリスクは残ります。

`examples/providers.yaml` には 2 種類の base_url stanza を用意:

- `zai-coding-glm-4-7/5-1/4-5-air`: Coding Plan 用 (`api/coding/paas/v4`) — 加入者向け、CodeRouter 経由でも Claude Code 直結に見えるはず
- `zai-paas-glm-4-7` (commented): General API 用 (`api/paas/v4`) — pay-as-you-go、制限対象外。CodeRouter 経由で安心して使える

**推奨運用**: Coding Plan 加入者で確実性を取るなら Claude Code に Z.AI を直結 (CodeRouter 経由しない) または General API stanza を有効化。General API は使用量比例課金。

### Changes

#### v1.8-A: Trusted Publishing 自動化 (ドキュメントのみ、PyPI 側 1 回登録)

- PyPI 側で trusted publisher を登録 (Owner: zephel01, Repo: CodeRouter, Workflow: release.yml, Environment: pypi)
- GitHub 側で `pypi` environment を作成 (protection rules なし、シークレットなし)
- `docs/inside/release-pypi.md` §0-6 に登録手順 + 自動化後フローを追記、§11 のチェックリストを `[x]` 完了に

#### v1.8-B: `claude_code_suitability` hint

- `coderouter/config/capability_registry.py` の `RegistryCapabilities` / `ResolvedCapabilities` に `claude_code_suitability: Literal["ok", "degraded"] | None` フィールド追加。`lookup` メソッドの first-match-per-flag walk に新スロット
- `coderouter/data/model-capabilities.yaml` に Llama-3.3-70B family (`*llama-3.3-70b*` / `*Llama-3.3-70B*`) を `claude_code_suitability: degraded` で宣言
- `coderouter/logging.py` に `ChainClaudeCodeSuitabilityDegradedPayload` TypedDict + `log_chain_claude_code_suitability_degraded` helper 新設、msg は `chain-claude-code-suitability-degraded`
- `coderouter/routing/capability.py` に `CLAUDE_CODE_PROFILE_PREFIX = "claude-code"` 定数 + `check_claude_code_chain_suitability(config, *, logger, registry=None)` 関数。プロファイル名 prefix gate + chain walk + プロファイル単位 aggregate WARN
- `coderouter/ingress/app.py` の lifespan で startup 時に `check_claude_code_chain_suitability` を 1 行で呼び出し

#### v1.8-C: `coderouter doctor --check-model --apply` / `--dry-run`

- `coderouter/doctor_apply.py` 新設 — `parse_patch_yaml` (doctor の YAML literal をコメント strip + safe_load) / `deep_merge_dicts` (再帰マージ、idempotent 検出) / `merge_provider_patch_into_doc` / `merge_capabilities_rule_into_doc` / `apply_doctor_patches` (top-level entry, ApplyResult dataclass 返す) / `render_unified_diff` (stdlib `difflib.unified_diff`) / `DoctorApplyError` + `MissingDependencyError`
- `pyproject.toml` `[project.optional-dependencies].doctor = ["ruamel.yaml>=0.18.6"]` 追加 (base 5 deps streak は維持、`[dev]` にも入れて test 用)
- `coderouter/cli.py` の doctor subparser に `--apply` / `--dry-run` フラグ追加 + `_run_check_model` / `_resolve_config_path` / `_run_apply_or_dry_run` のヘルパ。doctor 提案の YAML パッチを 1 invocation で書き戻し可能に
- 冪等性: 既に同じ値が入っていれば no-op (file mtime 不変、exit 0、"already up to date" メッセージ)
- バックアップ: `--apply` 時に自動で `providers.yaml.bak` を作成 (overwriting 形式、git 派は git-diff で履歴管理)

#### v1.8-D: `setup.sh` onboarding ウィザード

- リポジトリルートに `setup.sh` 新設 (bash 3.2 互換、新規依存ゼロ)
- macOS (`sysctl hw.memsize`) / Linux (`/proc/meminfo`) で RAM 自動検出
- RAM → 推奨モデル: ≥24GB→qwen2.5-coder:14b / ≥12GB→qwen2.5-coder:7b / ≥6GB→qwen2.5-coder:1.5b / <6GB→cloud-only バイル + cloud hint
- `ollama` 不在チェック: 実 pull モード時のみ強制、`--no-pull` / `--dry-run` 時は許容
- 既存 `providers.yaml` 保護: デフォルトは `.new` sidecar に書き、`--force` 時のみ `.bak` 残して overwrite
- 生成 YAML が live `CodeRouterConfig` Pydantic schema で round-trip すること、を回帰テストで pin

#### v1.8-E: examples/providers.yaml の 4 プロファイル構成 + Gemma 4/Qwen3.6/Z.AI 登録

- `default_profile: default` → `default_profile: multi` に変更 (新 default はマルチモーダル対応 chain)
- 4 プロファイル新設:
  - `multi` (default): vision-capable、Gemma 4 26B local primary → Sonnet 4-6 with vision paid 終端
  - `coding`: Qwen3.6 35B-A3B (note "local champ") → Qwen3-Coder 30B → ... → GLM-4.7 → Sonnet 4-6
  - `general`: Gemma 4 E4B (laptop でも動く軽量) → Gemini Flash free → GLM-4.5-Air → Haiku 4-5
  - `reasoning`: Qwen3.6 35B (thinking native) → ... → GLM-5.1 → Opus 4-1 with thinking
- 全プロファイルに `append_system_prompt` で Claude 風応答を nudge
- `mode_aliases`: `default → multi`, `fast → general`, `vision → multi`, `think → reasoning`, `cheap → general`
- 新規プロバイダ 11 種追加: Qwen3.6 (27b/35b), Gemma 4 (e4b/26b/31b), Z.AI (GLM-4.7/5.1/4.5-Air), Gemini Flash free, Claude Haiku/Opus direct
- `coderouter/data/model-capabilities.yaml` に `qwen3.6:*` (tools=true, claude_code_suitability=ok), `gemma4:*` (tools=true), `GLM-5*` / `GLM-4.[5-9]*` (tools=true) family を新規登録
- HF-on-Ollama コメント stanza は Gemma 4 / Qwen3.6 公式 tag 化に伴い縮小、GLM-4.5-Air は Z.AI cloud と HF GGUF の両方を案内
- `docs/hf-ollama-models.md` 新設 (HF GGUF を Ollama に登録する手順、推奨モデル別レシピ、既知の落とし穴)

### Migration

`pyproject.toml version 1.7.0 → 1.8.0`、`coderouter --version` は 1.8.0 を返すように。**手元の `~/.coderouter/providers.yaml` は触らない限り完全に変化なし** (新 example は `examples/providers.yaml` のみで、コピー操作は手動)。

新 example を試したい場合:

```bash
# 既存 config をバックアップしつつ新 example をコピー
cp ~/.coderouter/providers.yaml ~/.coderouter/providers.yaml.bak
cp examples/providers.yaml ~/.coderouter/providers.yaml

# Ollama に推奨モデルを pull (24GB+ VRAM の場合)
ollama pull qwen3.6:35b
ollama pull qwen3-coder:30b-a3b
ollama pull gemma4:26b

# Z.AI を使うなら API key を環境変数に
echo 'export Z_AI_API_KEY="your-key-here"' >> ~/.zshrc
source ~/.zshrc

# 確認
coderouter doctor --check-model local --apply  # 自動 patch も試せる
coderouter serve --port 8088 --mode coding    # 用途別に明示
```

`--mode default` は新 example では `multi` (マルチモーダル chain) に解決されます。旧 example の意味合い (Qwen2.5-Coder + cloud chain) を維持したい場合は `--mode coding` を使うか、`mode_aliases` に独自 alias を追加してください。

### Real-machine verification

```
$ pytest -q
710 passed, 1 skipped in 1.81s

$ ruff check coderouter/ tests/
All checks passed!

$ mypy --strict coderouter/doctor_apply.py coderouter/cli.py
Success: no issues found in 4 source files
```

`coderouter doctor --check-model X --apply` の冪等性 + バックアップ作成を smoke test で確認:

```
$ coderouter doctor --check-model local --apply
[probe report ...]
Apply: 1 target file(s).
  1 patch(es) applied.
[diff 表示]
  Backup: ~/.coderouter/providers.yaml → ~/.coderouter/providers.yaml.bak

$ coderouter doctor --check-model local --apply  # 二回目は no-op
Apply: 1 target file(s).
  All 1 patch(es) already applied — providers.yaml is up to date.
```

### Out of scope / 次回送り (v1.9 候補)

- v1.7-C 候補は引き続き需要待ち: `coderouter doctor --network`, `--check-config`/`--check-adapter` (引数なし全部回す mode), `recover_garbled_tool_json` 拡張, 起動時アップデートチェック
- macOS `.command` / Linux `.sh` / Windows `.bat` launcher は `uvx coderouter-cli` で onboarding 摩擦が十分低くなったため再評価
- PEP 541 reclamation (`coderouter` 名前空間) は引き続き審査待ち、進捗あれば plan.md §11.B に追記
- Z.AI Coding Plan の "router 経由でも認可される" 保証取得 (Z.AI 側へのフィードバック)

---

## [v1.7.0] — 2026-04-25 (PyPI 公開: `uvx coderouter-cli` 一発で動く)

**Theme: 「git clone してから `uv tool install --from git+...`」の onboarding 摩擦をゼロにする minor リリース。** PyPI に **`coderouter-cli`** として公開、以降は `uvx coderouter-cli serve --port 8088` の 1 行で何処からでもインストール + 起動できるようになりました。配布インフラ整備のための小さなコード変更 (パッケージ名、`importlib.metadata` lookup 名追従) と、リリースを反復可能にする GitHub Actions workflow / `pyproject.toml` の sdist allowlist が同梱です。Runtime / API 挙動は v1.6.3 から完全に変化なし。

- Tests: 651 → **651** (±0、コード変更は配布周りのみ)
- Runtime deps: 5 → 5 (17 sub-release 連続据え置き)
- New PyPI package: [`coderouter-cli`](https://pypi.org/project/coderouter-cli/) (Python ≥ 3.12)
- Backward compat: 既存の `git clone + uv tool install --from git+...` 経路も引き続き有効。`coderouter` コマンド名 / Python import 名 (`from coderouter import ...`) も完全に変化なし

### Why `coderouter-cli` (and not `coderouter`)

PyPI 上の `coderouter` 名前空間は別作者 (Lawrence Chen) の HTTP routing 系汎用ライブラリ (2025-06 公開、0.1.0 のみ、ドメイン完全別物) によって既に取得済みでした。新規 publish には別名が必要なため、npm / cargo の慣習 (`*-cli` suffix で CLI ツール) に倣って `coderouter-cli` で取得。**Python import 名と console script 名は両方とも `coderouter` のまま**なので、ユーザー視点では `pip install` 時の名前だけが異なります:

```bash
pip install coderouter-cli       # ← install (名前変わる)
import coderouter                # ← import (変わらない)
coderouter serve --port 8088     # ← run (変わらない)
```

PEP 541 reclamation request で `coderouter` 名を引き取る道は plan.md §11.B に追跡として残します (通っても 1〜数ヶ月かかるので、その間は `coderouter-cli` で運用)。

### Changes

- **PyPI publish 化** — `pyproject.toml` の `name` を `coderouter` → `coderouter-cli` に変更、`version` を 1.7.0 に bump、`classifiers` / `project.urls` / `keywords` を publish に必要なメタデータで enrich (`Topic :: Scientific/Engineering :: Artificial Intelligence` / Homepage / Issues / Changelog / Documentation の 4 URL)
- **`coderouter/__init__.py`** — `importlib.metadata.version("coderouter")` を `version("coderouter-cli")` に追従。Python import 名 (`coderouter`) は変わらないので、`from coderouter import ...` する全ユーザーには影響なし
- **`LICENSE` 新規** — MIT License を明示的にファイル化、wheel の `dist-info/licenses/LICENSE` に同梱されるようになった (PyPI の license 表示と sdist の完全性向上)
- **`tool.hatch.build.targets.sdist`** — `only-include` で sdist を厳格 allowlist 化。ローカル virtualenv (`.venv*`) や `__pycache__` / `dist/` / `.pytest_cache` 等を絶対に取り込まない設計に。これで `uv build` がどのマシンでも同じサイズ (sdist 668 KB / wheel 161 KB) を出す
- **`.github/workflows/release.yml` 新規** — `git tag v*` push 時に Trusted Publishing (OIDC、API トークン不要) で PyPI へ自動 publish + GitHub Release 草稿作成。**初回 publish (v1.7.0) は手動で実施**、Trusted Publisher 登録後の v1.7.x 以降は tag push のみで自動化される
- **doc reorder for new entry path** — README ja/en、quickstart.md ja/en、free-tier-guide ja/en の install セクションを `uvx coderouter-cli` 中心に書き換え。`uv tool install --from git+...` 経路は中級者向けに残置

### Real-machine verification

```
$ uv build
Successfully built dist/coderouter_cli-1.7.0.tar.gz   (668 KB, .venv 汚染ゼロ)
Successfully built dist/coderouter_cli-1.7.0-py3-none-any.whl  (161 KB)

$ coderouter-publish-prod   # = op run + uv publish (1Password から PYPI_TOKEN を inject)
Publishing 2 files https://upload.pypi.org/legacy/
Uploading coderouter_cli-1.7.0-py3-none-any.whl (157.7KiB)
Uploading coderouter_cli-1.7.0.tar.gz (652.7KiB)

$ curl -sI "https://pypi.org/pypi/coderouter-cli/json" | head -1
HTTP/2 200
```

CDN 伝播後に `uvx --from coderouter-cli coderouter --version` で本物の PyPI 経由インストールも確認済み (uv 0.11+ では package 名 ≠ executable 名のとき `--from` 必須、Issue #10 で報告者から fb)。

### Migration

不要。**v1.6.x までで `uv tool install --from git+...` していたユーザーは、自然なアップグレード経路として:**

```bash
# 旧 (引き続き有効)
uv tool install --from git+https://github.com/zephel01/CodeRouter.git coderouter-cli

# 新 (PyPI から、コマンド 1 行 — uv 0.11+ canonical 形式)
uvx --from coderouter-cli coderouter serve --port 8088
# あるいは恒久的に:
uv tool install coderouter-cli
```

`coderouter` 起動コマンド名、`from coderouter import ...` の Python import、`providers.yaml` のフォーマット、env 変数 (`ANTHROPIC_BASE_URL` 等)、ingress の URL 構造、すべて v1.6.3 と完全に同じです。

### Out of scope / 次回送り (v1.7-B 以降)

v1.7.0 (= v1.7-A) は配布パイプラインだけに集中して shipping させました。plan.md §11.B に記載された残りの v1.7 候補機能は v1.7-B 以降で:

- `coderouter doctor --check-config` / `--check-adapter` (引数なしで全部回す mode)
- `coderouter doctor --network` (外向き接続検出、CI で 0 outbound 保証)
- `setup.sh` (RAM 検出 → モデル提案 → providers.yaml 生成)
- macOS `.command` / Linux `.sh` / Windows `.bat` launcher
- 起動時アップデートチェック (opt-in)
- capability registry の `claude_code_suitability` hint (Llama-3.3-70B 系の startup WARN)

---

## [v1.6.3] — 2026-04-24 (`--env-file` + `doctor --check-env` for `.env` hygiene)

**Theme: ergonomic + safe `.env` handling, without rolling our own crypto.** v1.6.2 documented the `.env` `export` gotcha; v1.6.3 makes it disappear by giving operators two new tools that integrate cleanly with the existing secret-management ecosystem (1Password CLI, sops, direnv, OS Keychain) instead of inventing yet another encryption scheme.

- **`coderouter serve --env-file PATH`** — load a `.env`-style file into the worker's env *before* uvicorn boots. Repeatable for layering. Default precedence is "shell wins, file fills in gaps" so it's safe to run as a default; flip with `--env-file-override` when the file is the source of truth (e.g. CI).
- **`coderouter doctor --check-env [PATH]`** — local-fs / git-state probe for a `.env` file: existence + POSIX permissions (0600 expected) + `.gitignore` coverage + git-tracking state. Same exit-code contract as `--check-model` (0 OK / 2 patchable / 1 blocker). `--check-model` and `--check-env` are now mutually optional and can be combined in one invocation.
- **Stdlib-only `.env` parser** (`coderouter.config.env_file`) — supports the subset that 1Password / sops / hand-edited files actually emit (bare values, `"double"` quotes with `\n`/`\t`/`\"` escapes, `'single'` quotes literal, optional `export` prefix, inline `#` comments on bare values, blank lines). No variable expansion, no command substitution, no multi-line. Rejects POSIX-invalid keys and unterminated quotes with `file:lineno`-prefixed errors.
- **`docs/troubleshooting.md` / `.en.md` §5** — new "`.env` security in practice" section with: threat model (what at-rest encryption can and can't defend), 4-point quick checklist, full 1Password CLI recipe (`op run --env-file=.env.tpl --`), direnv + sops recipe (encrypted `.env.enc` in git), OS Keychain recipes (macOS Keychain / Linux libsecret), `--env-file` layering patterns, and the "minimize key scope" hygiene reminder.
- **Why no encryption-in-app**: the design rationale is in §5-1 of the doc — encryption only addresses 2 of 7 realistic threats (cold-disk theft, backups), the decryption key has to live somewhere anyway, and most security-conscious users already run 1Password / sops. `--env-file` makes integration trivial; rolling our own AES would lock those users out of their existing workflow.

- Tests: 601 → **651** (+50, +8.3%): `tests/test_env_file.py` (26 — parsing edge cases, override semantics, multi-file layering), `tests/test_env_security.py` (15 — perms / .gitignore / git-tracking against real subprocess `git`, with `git` skip-marker for non-POSIX), `tests/test_cli.py` (+8 — `--env-file` end-to-end including malformed-file exit, `--check-env` exit codes, multi-`--env-file` precedence; +1 renamed `test_doctor_requires_at_least_one_flag` for the now-optional `--check-model` rule).
- Runtime deps: 5 → 5 (16 sub-release streak preserved). The new modules are pure stdlib (`os`, `stat`, `subprocess`, `shutil`, `pathlib`, `re`).
- Backward compat: `--check-model` is no longer required at the argparse level (now optional), but the CLI emits a friendly "provide --check-model and/or --check-env" + exit 1 when neither is passed. Existing scripts that always passed `--check-model` are unaffected.

### Why

v1.6.2 added 9 docs entries explaining `.env` footguns. The right next step is to give operators commands so they don't have to remember the entire doc — `--env-file` removes the export-in-`.env` confusion entirely (since the file is parsed by us, not sourced by the shell), and `--check-env` collapses the 3-grep manual checklist (`chmod ls -l`, `git check-ignore`, `git ls-files`) into one command with copy-paste fixes. Both ship "additive only" so v1.6.2 setups continue to work verbatim.

### Migration

None required. Existing setups (manual `export` in `.zshrc`, `source .env`, direnv-managed `.envrc`) all keep working unchanged. Adopt `--env-file` and `--check-env` opportunistically when they're the cleaner path for a given workflow.

---

## [v1.6.2] — 2026-04-24 (Troubleshooting split-out + .env / NIM YAML hygiene)

**Theme: v1.6.1 出荷後の実機運用で踏んだ罠を、ドキュメント側に集約する patch-level。** Claude Code から NIM 経由の Llama-3.3-70B を実機で叩いて発見した 3 系統 (`.env` の `export` 漏れによる 401 / Llama-3.3-70B が Claude Code の system prompt に過剰反応して "こんにちは" を `Skill(hello)` に化けさせる挙動 / `claude-mem` 等の第三者プラグインが CodeRouter 経由だと内部呼び出しに失敗する構造) を、独立した `docs/troubleshooting.md` (JA primary) + `.en.md` (EN sub) に切り出して整理。README §トラブルシューティングは 30 秒で読めるサマリ + 症状別索引に短縮。`examples/.env.example` は各キーに `export` 必須の形式に変更し、ロード手順 / 検証手順 / 4 つの API キー (NIM / OpenRouter / Anthropic / CODEROUTER_CONFIG) の説明を冒頭ドキュメンテーションに追加。`examples/providers.nvidia-nim.yaml` の 4 プロファイル (`claude-code-nim` / `nim-first` / `free-only-nim` / `nim-reasoning`) は Llama-3.3-70B を最後尾に下げて Qwen3-Coder-480B を第一選択にする実機検証済みの順序へ並び替え、選択理由を YAML 内コメントで明文化 (Llama 自体の動作は健全、Claude Code 専用 prompt との相性問題)。全て docs / examples のみの変更、Python コード側の public API / ingress 契約は完全に変更なし。

- Tests: 601 → **601** (±0、新規ロジックなし。`tests/test_examples_yaml.py` の NIM YAML invariants が profile 並び替え後も pass することで間接検証)
- Runtime deps: 5 → 5 (15 sub-release 連続据え置き)
- Non-breaking: ドキュメント切り出し + サンプル YAML の export 追加 / プロファイル並び替えのみで、Python コード側の挙動は変更なし

### Changes

- **`docs/troubleshooting.md` 新規 (JA primary)** — README §トラブルシューティングの全文を切り出した上で、v1.6.2 の実機検証で発覚した 5 トピックを §1 (起動・設定の罠) と §4 (Claude Code 連携の罠) として追加。§1 は CLI 訂正 (`serve --mode`)、`.env` の `export` 必須、`env` での export 検証、`Header of type authorization was missing` 401 の切り分け、`~/.zshrc` 反映漏れの 5 つ。§4 は Llama-3.3-70B 系の過剰ツール呼び出し / `UserPromptSubmit hook error` (claude-mem 等プラグインとの構造的ミスマッチ) / auto-compact 遅延 / ダッシュボード活用の 4 つ
- **`docs/troubleshooting.en.md` 新規 (EN sub)** — JA 版と章番号 / アンカー 1 対 1 対応
- **README.md / README.en.md §トラブルシューティング短縮** — 30 秒で読める早見表 + 症状別索引 (4 入口) に置換、Ollama 5 症状は 1 行サマリ + リンクのみ。旧アンカー (`ollama-初心者--サイレント失敗-5-症状-v07-c` / `ollama-beginner--5-silent-fail-symptoms-v07-c`) は両 README に残して後方互換確保
- **README.md / README.en.md ドキュメント目次** — 「詰まったとき」「When stuck」行を `troubleshooting.md` / `.en.md` 指向で追加、両 README の言語スイッチャに `troubleshooting` / `トラブルシューティング` を併記
- **`docs/usage-guide.md` / `usage-guide.en.md` §8 quick index** — 既存 README 参照を `docs/troubleshooting.md` 指向に書き換え、`Header of type authorization was missing 401` と「Claude Code 上で挨拶が `Skill(hello)` 等に化ける」の 2 行を追記
- **`examples/.env.example`** — 全キー (`ALLOW_PAID` / `OPENROUTER_API_KEY` / `NVIDIA_NIM_API_KEY` / `ANTHROPIC_API_KEY` / `CODEROUTER_CONFIG`) を `export KEY=value` 形式に統一。冒頭に「ロード方法 (`source .env` で動く / `set -a && source .env && set +a` でも可) / CodeRouter は自動 source しない / 検証コマンド (`env | grep ...`)」のドキュメンテーションを追加
- **`examples/providers.nvidia-nim.yaml` 4 プロファイル並び替え** — `claude-code-nim` / `nim-first` / `free-only-nim` / `nim-reasoning` の全てで NIM レーンの順序を Qwen3-Coder-480B → Kimi-K2 → Llama-3.3-70B に変更 (実機検証で Llama-3.3-70B が Claude Code 単独利用時に過剰ツール呼び出しを起こすことが判明、第一選択から退避線へ)。プロファイル直前のコメントブロックに選定理由 (実機検証の症状ログ + `docs/articles/note-nvidia-nim.md` §6-2 への参照) を追加
- **`examples/providers.nvidia-nim.yaml` セットアップコメント拡張** — 冒頭の "NVIDIA NIM setup" を 5 ステップに拡張、`.env` の `export` 必須 / `coderouter doctor` を起動前に通すこと / `--port 8088` を Claude Code に合わせる必要を明記
- **`docs/articles/note-nvidia-nim.md` 改訂** — v1.6.2 検証ログを §6 (実機罠 3 種) と §7 (ダッシュボード活用) に追記、§4 / §9 / §11 の手順を実機検証済みコマンドに更新

### Why

v1.6.1 出荷直後にユーザー (=自分) が NIM 構成を実機で立てた際、`source .env` だけでは `coderouter serve` の子プロセスに env 変数が届かず `Header of type authorization was missing` 401 で詰まり、そこを越えても Llama-3.3-70B が "こんにちは" を `Skill(hello)` に化けさせて使い物にならない、という二重トラップを踏んだ。両方とも CodeRouter のコードは健全で、ドキュメント / サンプル設定が「実機で踏むであろう罠」を予防していなかったのが本質的な問題。v1.6.2 はこの「現場で実際に踏んだ」知見を docs / examples に確実に折り込むための小さな patch リリース。コード変更を伴わないため CHANGELOG / plan.md / docs のみで完結。

### Migration

不要。既存 `~/.coderouter/providers.yaml` / 既存 env 変数 / 既存 Python import / 既存 ingress 契約は全て変更なし。`examples/providers.nvidia-nim.yaml` を `~/.coderouter/providers.yaml` にコピーして使っているユーザーは、本リリースの YAML を上書きコピーすると Qwen-first 順序に切り替わる。`.env` を従来形式 (export なし) で運用していて問題なく動いていた人は、実は親シェル経由で別途 export していたケースが大半で、v1.6.2 の `.env.example` をそのまま `cp` しても動作は変わらない (export を二重宣言しても害はない)。

---

## [v1.6.1] — 2026-04-23 (NIM free-tier + doc hygiene)

**Theme: v1.6.0 `auto_router` 出荷直後の patch-level。** NVIDIA NIM 開発者枠 (40 req/min) を 1 級市民として local-first fallback チェーンに組み込み、併せて README / docs の言語優先度を「日本語 main / 英語 sub」にスワップ (ターゲット層の reality に合わせる)、README ヒーローを「Claude Code × ローカル LLM で tool calling が破綻する問題を CodeRouter の修復パスで直す」という最強のピッチに書き換え、`coderouter/__init__.py` の `__version__` hardcode を `importlib.metadata.version("coderouter")` 経由に切替 (`pyproject.toml` の `version` を single source of truth に)。全て non-breaking — 既存 YAML / 既存 API / 既存 ingress 契約は verbatim 維持、新規ファイル追加 + 既存 docs のリネーム + README hero の入替のみ。

- Tests: 596 → **601** (+5, +0.8%)、`tests/test_examples_yaml.py` 新設 (example YAML 全件ロード + NIM 固有 invariants)
- Runtime deps: 5 → 5 (14 sub-release 連続据え置き)
- Non-breaking: 新設 example YAML + 新設 reference doc + ファイルリネーム (`git mv` で blame 保全) + README hero 入替のみで、Python コード側の public API / ingress 契約は完全に変更なし

### Added

- **`examples/providers.nvidia-nim.yaml`** — NVIDIA NIM 開発者枠 (40 req/min 無料、クレカ不要) 向けの完成形サンプル。4 プロファイル (`claude-code-nim` / `nim-first` / `free-only-nim` / `nim-reasoning`) で `local (Ollama 7B/14B) → NIM 3 段 (Meta/Qwen/Moonshot 異ベンダー) → OpenRouter free 2 段 → paid` の 8 段チェーンを既定。live 検証 (2026-04-23、`integrate.api.nvidia.com/v1`) で採用判定:
  - `meta/llama-3.3-70b-instruct` — chat 540ms、tool_calls OK、streaming 260ms / 12 SSE chunks / usage 返却 ✓
  - `qwen/qwen3-coder-480b-a35b-instruct` — chat 634ms、tool_calls OK (480B MoE、agentic coding 特化)
  - `moonshotai/kimi-k2-instruct` — chat 2.8s、tool_calls OK (NIM レーン内でのベンダー diversity)
  - `qwen/qwen2.5-coder-32b-instruct` — chat は 160ms で正常、tool-laden リクエストに対しては NIM が HTTP 400 `"Tool use has not been enabled, because it is unsupported by qwen/qwen2.5-coder-32b-instruct"` を返すため、capability gate で tool-laden traffic を回避する `tools: false` stanza として組み込み
  - `moonshotai/kimi-k2-thinking` — `reasoning_content` に `<think>...</think>` で答えを返す variant、`nim-reasoning` プロファイル専用。`output_filters: [strip_thinking]` を safety net として併記
  - 不採用例 (`nvidia/llama-3.1-nemotron-70b-instruct` → 404、`deepseek-ai/deepseek-r1` → 410 EOL 2026-01-26、`nvidia/llama-3.3-nemotron-super-49b-v1.5` → 200 OK だが content null、`deepseek-ai/deepseek-v3.2` / `z-ai/glm4.7` → timeout) を YAML コメントに記載して再試行を防ぐ
- **`tests/test_examples_yaml.py`** — 新設の +5 tests で `examples/providers*.yaml` 全件ロード検証 + NIM 固有 invariants の CI 時強制:
  - 全 example YAML がロードでき `default_profile` / profile 参照整合性が保たれる (parametrized over 4 ファイル)
  - NIM 3 tool-capable provider (`nim-llama-3.3-70b` / `nim-qwen3-coder-480b` / `nim-kimi-k2`) が存在する
  - 全 `nim-*` stanza が `api_key_env=NVIDIA_NIM_API_KEY` / `base_url=https://integrate.api.nvidia.com/v1` / `paid=False` を満たす (prefix-exact で base_url を pin、`/v2` typo 等を reject)
  - `nim-qwen-coder-32b-chat` が `tools: false` を宣言する (HTTP 400 回避の capability gate 契約)
  - `nim-kimi-k2-thinking` がプライマリ `claude-code-nim` チェーンに含まれない (高 latency + `reasoning_content` 出力形状が Claude Code に不向きなため、`nim-reasoning` プロファイルでのみ引ける)
- **`docs/free-tier-guide.md` / `docs/free-tier-guide.en.md`** — 新規 reference doc。NIM + OpenRouter 無料枠の使い分けだけに絞った 250+ 行の運用ガイド:
  - 3 層比較表 (local / NIM 40 req/min / OpenRouter free 20 req/min + 200 req/day)
  - `claude-code-nim` プロファイルの 8 段チェーン設計意図
  - セットアップ手順 (3 コマンド) + `.env` に置く 2 つの API キー取得先
  - live 検証済みモデル一覧 (採用 / chat-only / 不採用の 3 段)
  - 5 common footguns (NIM の "無料" はクレジット消費型、一部モデルが非標準 `reasoning` フィールドを吐く、Qwen2.5-Coder-32B の tools 無効、OpenRouter の 200 req/day、NIM model ID の case-sensitive drift)
  - `coderouter doctor --check-model` の実出力例 + 読み方
- `README.md` + `README.en.md` の "Usage guide" 案内のすぐ下に free-tier guide への双方向リンクを追加
- `docs/usage-guide.md` / `docs/usage-guide.en.md` の §6 OpenRouter pairing セクションに NIM レイヤ追加と free-tier guide への参照を追加

### Changed

- **ドキュメント言語優先度のスワップ** — `git mv` で 5 ペアを日本語 main / 英語 sub に入替:
  - `README.ja.md` → `README.md` / `README.md` → `README.en.md`
  - `docs/usage-guide.ja.md` → `docs/usage-guide.md` / `docs/usage-guide.md` → `docs/usage-guide.en.md`
  - `docs/security.ja.md` → `docs/security.md` / `docs/security.md` → `docs/security.en.md`
  - `docs/quickstart.ja.md` → `docs/quickstart.md` / `docs/quickstart.md` → `docs/quickstart.en.md`
  - `docs/when-do-i-need-coderouter.ja.md` → `docs/when-do-i-need-coderouter.md` / `docs/when-do-i-need-coderouter.md` → `docs/when-do-i-need-coderouter.en.md`
- `pyproject.toml readme = "README.md"` は維持したため PyPI 側の readme 表示も日本語に切替 (ターゲット層と整合)
- クロスリファレンス 20+ 箇所を同時更新 — 両 README の言語スイッチャー、docs 内部の sibling-language 相互参照、docs 内部の anchor slug 整合 (日本語 README の anchor は日本語スラグ、英語側は英語スラグ)、`docs/articles/note-*.md` / `zenn-*.md` の GitHub blob URL (`blob/main/docs/quickstart.ja.md` → `blob/main/docs/quickstart.md` 等)、`docs/designs/v1.6-auto-router.md` の内部リンク
- **README ヒーロー書き換え** (両言語):
  - 旧: "Local-first coding AI with ZERO cost by default" 型の汎用タグライン
  - 新: "Claude Code でローカル LLM を使うと tool calling が壊れる問題、ルーター側で直します" — `qwen2.5-coder:7B` / `phi-4` / `mistral-nemo` などの量子化モデルが `{"name":..., "arguments":...}` を plain text で吐く症状を CodeRouter の tool-call 修復パスが有効な `tool_use` ブロックへ復元、という最強のピッチを最前面に。"さらに CodeRouter が他にやってくれること" ブロック (doctor / reasoning-leak scrub / local → NIM 40 req/min → OpenRouter free → paid fallback / 5 deps / 601 tests) を言語スイッチャーと既存 "What gets easier" セクションの間に挿入
  - `docs/assets/before-after-toolcall.gif` の HTML comment placeholder を予約 (撮影できたらコメントアウト外すだけ)
  - バージョンバッジを 1.5.0 → 1.6.1 に、テスト数 453 → 601 に同期

### Fixed

- **`coderouter/__init__.py`** (`009b2b1`) — `__version__` の実装を hardcode (`"1.5.0"`) から `importlib.metadata.version("coderouter")` 経由に切替。以降 `pyproject.toml` の `version` 1 行が single source of truth で、`coderouter --version` と `/healthz` の両方が正しく 1.6.x 系を報告する。v1.6.0 の known quirk として `docs/designs/v1.6-auto-router-verification.md` に記録された issue の修復
- CI fix (`d0de1a9`)

### Non-breaking compatibility

- YAML schema に変更なし — 既存の `providers.yaml` / `providers.auto.yaml` / `providers.auto-custom.yaml` は verbatim で動作
- Python public API に変更なし — `coderouter/__init__.py` の `__version__` 取得経路だけが変わった (値は同じフィールドで同じ型)
- Ingress 契約に変更なし — `/v1/messages` / `/v1/chat/completions` / `/metrics` / `/metrics.json` / `/dashboard` 全て verbatim
- ファイルリネームは `git mv` で実施したため blame 履歴保全。pyproject は `readme = "README.md"` のまま (PyPI 側は新しい日本語 readme を自動追随)

---

## [v1.6.0] — 2026-04-22 (Umbrella tag — `auto_router`)

**Theme: plan.md §11「task-aware auto routing」を 1 minor で受ける。** リクエスト本文を宣言的ルールで分類し profile を自動選択する `auto_router` を 3 sub-release で出荷: schema + classifier (v1.6-A) / ingress + metrics 配線 (v1.6-B) / examples + docs (v1.6-C)。初心者は `default_profile: auto` を書くだけで内蔵ルール (画像 → `multi` / コードフェンス比率 ≥ 0.3 → `coding` / それ以外 → `writing`) が効き、中級者は `auto_router:` ブロックで独自ルールに差し替え、上級者は `body.profile` / `X-CodeRouter-Profile` / `X-CodeRouter-Mode` による per-request 上書き (v0.6-D 以来の経路) が引き続き最優先で効く — この 3 tier を 1 ファイルに収める。v0.6-D 互換は完全維持: `default_profile: auto` を書かない限り auto slot は一切発火せず、既存設定は verbatim で動き続ける。

- Tests: 527 → **596** (+69, +13.1%)、v1.6-A 26 new auto_router tests (classifier matchers / regex 事前コンパイル / reserved `auto` 名 / bundled profile 要求 / fall-through / disabled) + v1.6-B ingress+metrics wiring tests + v1.6 validator tests
- Runtime deps: 5 → 5 (据え置き 13 sub-release 連続、分類は純粋正規表現 + dict 走査、外部分類器を呼ばない)
- Non-breaking: 新設 config field (`auto_router:`、任意) + 新設 sentinel (`default_profile: auto`、opt-in) + 新設 Prometheus counter (`auto_router_fallthrough_total`) のみで、既存 ingress / precedence chain / metrics schema は verbatim 維持

### Added

- **v1.6-A — schema + classifier** (coderouter/routing/auto_router.py 新設 +245 LOC / coderouter/config/schemas.py +170 LOC)
  - `RuleMatcher` (Pydantic): `has_image` / `code_fence_ratio_min` / `content_contains` / `content_regex` の 4 matcher variant をフィールドで表現、`_exactly_one` validator で「1 ルールに matcher は 1 つだけ」を load 時強制 (複数書くと `pydantic.ValidationError` で fail)。`content_regex` は `_compile_regex_eagerly` で起動時に `re.compile` され、typo は起動を落とす (毎リクエスト silent fail にしない)
  - `AutoRouteRule` / `AutoRouterConfig`: ルールに `id` (ログの `auto-router-resolved` payload に乗る安定識別子、`builtin:` / `user:` prefix 慣習) / `profile` / `match`、トップに `disabled` (hard off-switch) / `rules` (ordered, first-match-wins) / `default_rule_profile` (fall-through 先) を持たせる
  - `BUNDLED_RULES` (コード側で宣言、YAML に書かなくて済む): `image-attachment → multi`、`code-fence-dense (ratio ≥ 0.3) → coding`、fall-through = `writing`。`BUNDLED_REQUIRED_PROFILES = ("multi", "coding", "writing")` を `CodeRouterConfig._check_bundled_auto_router_requirements` が起動時検証 — `default_profile: auto` + `auto_router` 未定義で multi/coding/writing のいずれかが欠けると load が落ち、エラーメッセージに「(a) 3 profile 全て定義 / (b) 独自 `auto_router:` で上書き / (c) `default_profile` を別 profile 名に」の 3 択を明記
  - `classify(body, config)`: 最新の `role: user` メッセージ 1 件だけを走査 (履歴全体は見ない、トークン消費を削る設計)、OpenAI / Anthropic 両形式の content list から `type: image_url` / `type: image` / `type: input_image` いずれも `has_image` 判定、text は string / multimodal list の両方から抽出。matcher ヒット時 `auto-router-resolved` / 空振り時 `auto-router-fallthrough` の 2 event を発火 (後述 metrics counter の source)
  - `RESERVED_PROFILE_NAME = "auto"`: `CodeRouterConfig._check_auto_is_reserved` が `profiles[].name == "auto"` を起動時 reject。`default_profile: auto` sentinel と衝突するため
  - +26 tests (tests/test_auto_router.py、各 matcher / reserved name / bundled 要求 / regex 事前コンパイル / disabled / fall-through / 3 matcher を併記した rule の reject)
- **v1.6-B — ingress wiring + metrics** (coderouter/ingress/openai_routes.py + coderouter/ingress/anthropic_routes.py + coderouter/metrics/collector.py + coderouter/metrics/prometheus.py)
  - OpenAI / Anthropic 両 ingress の precedence chain に auto router slot を 1 箇所ずつ挿入 (v0.6-D の body.profile > `X-CodeRouter-Profile` > `X-CodeRouter-Mode` > `default_profile` の間、`default_profile` の直上): `if chat_req.profile is None and config.default_profile == RESERVED_PROFILE_NAME: chat_req.profile = classify(payload, config)`。`default_profile != "auto"` では slot は not-taken、engine に渡る profile は pre-v1.6 と bit-identical (engine 側で default profile 埋め込みが従来どおり走る)
  - `MetricsCollector._dispatch` に `auto-router-fallthrough` event を新 counter `_auto_router_fallthrough_total` に配線、snapshot の `counters` dict と `reset()` に同 key を追加。fall-through は「ユーザー定義ルールがどれもヒットしない率」のシグナルなので独立 counter として露出
  - `format_prometheus()` に `coderouter_auto_router_fallthrough_total` を新規 export (HELP テキストに「no user/bundled rule matched, or auto_router.disabled=true」と併記)、`promtool check metrics` は round-trip clean
  - precedence chain のドキュメント (ingress 両ファイルの module docstring) に「4. auto_router (v1.6-A, fires only when `default_profile == 'auto'`)」を追記、v1.6 で新旧どちらの経路がどこで効くか読者が 1 箇所で辿れるように
- **v1.6-C — examples + quickstart 追記** (examples/providers.auto.yaml 新設 / examples/providers.auto-custom.yaml 新設 / docs/quickstart.ja.md +1 section)
  - `examples/providers.auto.yaml`: zero-config 版。`allow_paid: false` / `default_profile: auto` / `display_timezone: Asia/Tokyo` + 3 Ollama provider (qwen2.5-coder:7b / qwen2.5:7b / qwen2.5vl:7b) + 3 profile (coding / writing / multi) のみで内蔵ルールが即発火。冒頭コメントに `ollama pull` 3 コマンドと、画像を送らないなら vl モデルは省略可 (画像リクエストだけ fast-fail) を明記
  - `examples/providers.auto-custom.yaml`: 中級者向け copy-edit 起点。`auto.yaml` を親に `auto_router:` ブロックを挿入、4 matcher variant を 1 つずつ踏んだ 4 ルール (image → multi / 翻訳意図 regex → writing / "Review this PR" 部分文字列 → coding / fence ratio ≥ 0.15 → coding) + `default_rule_profile: writing` を例示。コメントで「rules は内蔵ルールと merge せず完全置換」「matcher は 1 rule に 1 つだけ」「rule 順序が first-match-wins」の 3 点を明示
  - `docs/quickstart.ja.md` に「補足: プロファイル選択を CodeRouter に任せる」セクションを Pattern A/B の後に追加。C-1 pull → C-2 `cp auto.yaml` → C-3 カスタマイズの 3 ステップで、既存の Pattern A/B を書き換えずに合流経路を提示

### Changed

- **precedence chain 公式順序を v1.6 用に更新** — plan.md §11 / ingress docstring / quickstart の 3 箇所で `body.profile > X-CodeRouter-Profile > X-CodeRouter-Mode > auto_router (default_profile == "auto") > default_profile` の 5 段で統一。v0.6-D の 4 段表記から増えたのは 4 番目だけで、既存の 1-3 番と最終 default 解決は verbatim 維持

### Non-breaking compatibility

- `default_profile: "auto"` を書かない限り auto slot は dead code path (ingress で分岐が一切立たない)。v1.5.x までの providers.yaml は v1.6.0 で verbatim 動作
- 新設の `auto_router:` field は Optional で default None、書かないなら `CodeRouterConfig.model_validate` の view から完全不可視
- 新設 Prometheus counter `coderouter_auto_router_fallthrough_total` は既存 counter と並列の scalar で、Prometheus scraper の view は 1 行増えるだけ (削除 / rename なし)

---

## [v1.5.0] — 2026-04-22 (Umbrella tag — Observability pillar)

**Theme: plan.md §12「計測ダッシュボード」を丸ごと 1 minor で受ける。** 収集 (v1.5-A `MetricsCollector` + `/metrics.json`) / 配信 (v1.5-B Prometheus `/metrics` + `$CODEROUTER_EVENTS_PATH` JSONL mirror) / 可視化 CLI (v1.5-C `coderouter stats` curses TUI) / 可視化 HTML (v1.5-D `/dashboard` 1 ページ) / timezone 表示 (v1.5-E `display_timezone` config) / demo 同梱 (v1.5-F `scripts/demo_traffic.sh`) の 6 sub-release を横並びで出荷。READMEに live dashboard のスクショ (`docs/assets/dashboard-demo.png`) と「このダッシュボードを見ると何の問いに即答できるか」を明記するセクションを追加 ("モデルが動作してる / 利用されてる / 切り替わった" が読み取れること) — 数字の羅列ではなく運用上の問いを起点にした書き直し。**SemVer 番号について**: `v1.0.1 → v1.5.0` で旧 v1.1 (= 配布 / launcher / doctor、plan.md §11) を飛び越しているため、plan.md §11 ヘッダは **v1.6** にリラベル、`v1.1.0`-`v1.4.x` は欠番扱い。`v1.5.0` umbrella で plan.md §12 を受け、§11 (v1.6) が次の minor。

- Tests: 457 → **527** (+70, +15.3%)、v1.5-A +41 / v1.5-B +16 / v1.5-C ±0 (data/render layer、D で統合計上) / v1.5-D +12 / v1.5-E +1 / v1.5-F ±0
- Runtime deps: 5 → 5 — `curses` / `urllib` / `datetime.zoneinfo` は全て stdlib、tailwind は CDN 1 ファイル、Prometheus 形式は自前文字列生成で SDK 依存ゼロ (12+ sub-release 連続で依存数据え置き)
- Non-breaking: 新設 endpoint (`/metrics.json` / `/metrics` / `/dashboard`) + 新設 CLI (`coderouter stats`) + 新設 config field (`display_timezone`、任意) のみで既存 endpoint / CLI / config は verbatim 維持

### Added

- **v1.5-A — `MetricsCollector` + `GET /metrics.json`** (coderouter/metrics/collector.py +463 LOC / coderouter/ingress/metrics_routes.py +92 LOC)
  - `MetricsCollector` は `logging.Handler` のサブクラス。既存の structured log stream (v0.3 以降不変の JSON line shape) に handler として `addHandler()` するだけで発火、コード側のログ呼び出しは 1 行も書き換えない。in-memory ring (counters / providers / recent 50 events / startup snapshot) を `_process_record()` で毎秒 refresh
  - `GET /metrics.json` (`FastAPI` JSON response) で snapshot を JSON として返す。`/dashboard` HTML (v1.5-D) と `coderouter stats` CLI (v1.5-C) が同じ endpoint を fetch する single-source-of-truth 設計
  - app.py の lifespan 内で `MetricsCollector` を root logger にアタッチ、startup で `coderouter-startup` event を fire して `startup` snapshot に version / providers / profiles / allow_paid / mode_source を seed
- **v1.5-B — Prometheus text exposition + JSONL mirror** (coderouter/metrics/prometheus.py +211 LOC)
  - `GET /metrics` が Prometheus `text/plain; version=0.0.4` で exposition を返す。`coderouter_*` prefix (慣習)、全て scalar (ラベルなし)、gauge + counter 混成 (e.g. `coderouter_requests_total`, `coderouter_providers_healthy`)
  - `$CODEROUTER_EVENTS_PATH` env が設定されているとき、collector が同じ log record を JSONL としてそのパスに append。snapshot とは完全独立な side-effect (snapshot の in-memory ring はそのまま、JSONL だけが長期保存用に伸びる)。`JsonLineFormatter` と同一行シェイプなので既存の log 解析 pipeline にそのまま乗る
  - +11 tests (test_metrics_prometheus.py)、+5 tests (test_metrics_jsonl.py)
- **v1.5-C — `coderouter stats` CLI TUI** (coderouter/cli_stats.py +752 LOC)
  - stdlib `curses` + `urllib` のみで動く 5 パネル dashboard: Providers (健康状態 + latency_ms + last_event)、Fallback & Gates (fallback chain 進行 / ALLOW_PAID / capability-degraded カウント)、Requests/min sparkline (60 秒 rolling bucket)、Recent Events (直近 10 件、新しい順、tz 変換済み)、Usage Mix (local / free / paid の比率)
  - `--once` mode: TTY 不在 (CI / pipe / `demo_traffic.sh` banner) で単発レンダー、stdout に plain text 版を出す。driver (`_Screen` curses wrapper) と pure data+render layer を分離、unit test は render layer だけを叩く
  - +39 tests (test_cli_stats.py、data layer + render + `--once` snapshot)
- **v1.5-D — `/dashboard` HTML 1 ページ** (coderouter/ingress/dashboard_routes.py +493 LOC)
  - tailwind CDN 1 ファイル + vanilla JS (`setInterval` + `fetch("/metrics.json")` 2 秒間隔) の single-page。htmx を避けたのは 5-dep policy と、fetch polling で十分な TTFB を確認できたため (plan.md §12.3.6 参照)
  - 5 パネルは CLI TUI と同じ意味論 (Providers / Fallback & Gates / Requests/min sparkline / Recent Events / Usage Mix) を HTML で表現。dark theme 既定、`data-bind` attribute で JS 側が部分更新
  - +12 tests (test_dashboard_endpoint.py、HTML 200 / snapshot 埋込 / polling 引数)
- **v1.5-E — `display_timezone` config field** (coderouter/config/schemas.py + cli_stats.py + dashboard_routes.py)
  - `providers.yaml` top-level に `display_timezone: "Asia/Tokyo"` 等を宣言 (任意、IANA zone 名、未設定時 UTC)。集約された UTC 時刻は触らず、**表示層だけ**変換する: CLI TUI は `TzFormatter` (zoneinfo + cache、同じゾーンの繰り返し変換を O(1) に)、HTML は `Intl.DateTimeFormat` (ブラウザ native、zone 引き継ぎ)
  - `/metrics.json` の `config.display_timezone` で JS 側に伝搬、`examples/providers.yaml` に reference stanza 追加
  - +1 test (display_timezone 専用 fixture、tz-aware datetime の format 一致)
- **v1.5-F — `scripts/demo_traffic.sh`** (+861 LOC)
  - weighted scenario picker: normal 4/10 / stream 3/10 / burst+idle 2/10 / fallback 1/10、paid-gate every 8th tick。各 scenario は dashboard で panel がどう動くかを意図して設計 (例: burst+idle → sparkline のスパイク + idle で減衰を観察)
  - flag: `--duration <sec>` (既定 60、`∞` で SIGINT まで連続)、`--serve` (mock HTTP server を `127.0.0.1:4444` で起動、ローカル単体で回せる)、`--dry-run` (scenario picker の確率分布 sampler だけ実行、traffic は送らない)
  - Banner + expected-count table + elapsed/progress readout (`tick N/M, elapsed=XmYs`)、`scenario_*` 関数群、`log_info/ok/warn/err` 統一ログ
  - macOS `/bin/bash` 3.2 互換修正: (i) heredoc-inside-`$()` が bash 3.2 parser で稀に hang するため `PLAN_PY_SRC` / `BODY_PY_SRC` を single-quoted 変数に外出し → `python3 -c "$VAR"`、(ii) 並行 bg job の集約で bare `wait` が SIGCHLD 取りこぼしで hang するため `wait_pids()` helper (`$!` で集めた PID を個別 wait) を新設、`scenario_fallback_burst` / `scenario_burst_then_steady` で適用
- **README dashboard snapshot** (README.md / README.ja.md + docs/assets/dashboard-demo.png)
  - "Live dashboard" セクションを architecture 図の直後に挿入。キャプションは数字の羅列ではなく「このダッシュボードを見ると何の問いに即答できるか」という運用問い起点: どの provider が生きて今応答しているか / fallback が直近で発火したか / 有料ゲートは閉じたままか / 直近数分のリクエスト流量 / 直近 N 件のイベント
  - パネル配置説明 (左上から右下へ: Provider / Fallback & Gates / Requests/min sparkline / Recent Events / Usage Mix) で読者が画像とキャプションを突き合わせられるように

### Changed

- **plan.md §11 ヘッダを "v1.1" → "v1.6" にリラベル** — `v1.0.1 → v1.5.0` で §11 (配布 / launcher / doctor) を飛ばしたため。TOC / §6.1 マイルストーン表 / §6.2 リリース履歴詳細 / 本文中の v1.1 言及 (5 箇所) を全て v1.6 に置換、`v1.1` 番号は欠番扱いを明文化
- **README "Coming next" を v1.5 ✅ 出荷済み表示に** — README.md L149 + L324 付近、README.ja.md 対応箇所。旧: "v1.1 — launcher; v1.5 — metrics dashboard"、新: "v1.5 ✅ — metrics (shipped); v1.6 — launcher (旧 v1.1 ラベル、v1.5 先行出荷により繰り下げ)"
- **docs/usage-guide.{md,ja.md}** — "v1.1" Docker image tracking を "v1.6 (旧 v1.1)" に置換
- **pyproject.toml / coderouter/__init__.py** — `version = "1.0.0"` / `__version__ = "1.0.0"` → `1.5.0`

### Non-Added (explicitly out of scope / deferred)

- **Retrospective `docs/retrospectives/v1.5.md`** — umbrella narrative は別途執筆予定。本 release は CHANGELOG + plan.md status line + README snapshot で compaction、retrospective は 6 sub-release をまたいだ設計 through-line (例: pure data+render layer を CLI と HTML で共有する 2-consumer-1-producer 設計、env-gated JSONL side-effect が snapshot に依存しない isolation 原則、`display_timezone` を表示層だけに限る "aggregate in UTC, render in local" 原則) を書く価値があるので deferral
- **v0.7 / v1.0 follow-ons の着地** — CHANGELOG [v1.0.1] で v1.1+ に push したアイテム (output_filters chain-level override / doctor probe-grouping refactor / num_predict-without-max_tokens / Ollama 0.20.5 silent-override investigation) は v1.5 では未着手。`v1.6` (旧 v1.1) または v1.7 で順次拾う。v1.5 は観測可能性に scope を集中させ "観測 → 矯正" のうち観測側だけを完成させる方針を優先

### Follow-ons

- **v1.5.0 の live-verify scenario** — v0.5-verify / v1.0-verify の pattern を踏襲して `scripts/verify_v1_5.sh` を書く。bare (collector 無効) と tuned (collector 有効 + `$CODEROUTER_EVENTS_PATH` セット) の delta で "JSONL 行が書き込まれる / `/metrics` が 200 を返す / `/dashboard` が HTML を返す" を assertion
- **dashboard retrospective narrative** — 前述
- **`scripts/demo_traffic.sh` の README への runbook セクション** — 現状 `--help` にしか書いていない。scenario 配分 / expected count / `--serve` の意味 / bash 3.2 互換のために仕込んだ `wait_pids` の why が operator doc として欲しい
- **long-running demo の evidence** — 今回スクショだけ貼ったが、"3 分 × 87 リクエストで dashboard が stable" を別 section で時系列 log として残すと後の regression 判定で便利

---

## [v1.0.1] — 2026-04-21 (Hygiene pass — public error hierarchy + docstring + mypy strict)

**Theme: v1.0.0 umbrella のあと、埋まりきっていなかった 3 つの足回りを 1 release で片付ける。** (1) `CodeRouterError` root 例外の新設 — 既存の 3 leaf (`AdapterError` / `NoProvidersAvailableError` / `MidStreamError`) を共通親で束ね、downstream integrator が `except CodeRouterError` 一文で router が raise する全例外を拾えるようにした。`coderouter.errors` モジュールを新設、`coderouter` top-level で re-export、既存 import パスは全て非破壊。(2) docstring 網羅率を **75.6% → 91.2%** へ引き上げ — `interrogate` ベースで measure、public API 系ファイル (adapters / routing / ingress / translation の model / logging) 全て 100%、残りは stream-state 内部 helper / CLI / doctor / translation の private 関数のみ。(3) mypy `--strict` 0 errors を確認 (v0.6 以降累積していた 10 errors を ingress routes の `response_model=None` + `AsyncIterator[str]` + fallback.py の `isinstance(adapter, AnthropicAdapter)` narrowing + `StreamChunk.usage` 型宣言で解消済 — v1.0-verify で未記録だった分を本 release で明文化)。**453 → 457 tests** (+4 は `tests/test_errors.py` 新設、3 leaf 例外の `CodeRouterError` 継承 invariant を lock するガード)。実質 public API の追加は `CodeRouterError` 1 つだけで、既存 CI gate / 実機 verify が全て pass するため semver 上は **patch-level (minor の bump 不要)**。

- Tests: 453 → **457** (+4)
  - `tests/test_errors.py` 新設 +4 (`AdapterError` / `NoProvidersAvailableError` / `MidStreamError` の 3 clase が `CodeRouterError` を継承すること + `AdapterError("boom", provider="p", status_code=500, retryable=False)` を実際に raise して `except CodeRouterError` で catch できることの instance-level smoke test)
- Runtime deps: 5 → 5 (docstring coverage の measure に使う `interrogate` は dev-only、runtime には入らない)
- Non-breaking: 既存 3 例外は基底クラスのみ `Exception` → `CodeRouterError` に変更、`CodeRouterError(Exception)` なので `except Exception` を書いていた caller も従来通り動く。import パスは全て既存位置維持 (`from coderouter.adapters.base import AdapterError` など無変更)。

### Added

- **`coderouter/errors.py` — root `CodeRouterError(Exception)` class** (~30 LOC)
  - 既存 3 leaf 例外の共通親。動作は `Exception` と同じ (`pass`-only 定義)、存在理由は downstream integrator が `except CodeRouterError` で router-side failure を wholesale に catch できるよう API surface を固定すること。leaf を個別 import して enumerate する必要がなくなる。docstring で「leaves are free to grow over time」と明記、将来新例外を追加するときの invariant を文書化
  - 配置理由: `coderouter/adapters/base.py` や `coderouter/routing/fallback.py` に root を置くと import cycle の温床 (`logging.py` 方式と同じ失敗モード)。`errors.py` は dependency-less leaf モジュールとして独立させ、adapters / routing の両方が import する。これで import graph 上は `errors.py` が最深層に落ち着く
- **`coderouter/__init__.py` から `CodeRouterError` を re-export** — `from coderouter import CodeRouterError` を 1 行で可能に。`__all__ = ["CodeRouterError", "__version__"]` として top-level の public API を明示
- **`tests/test_errors.py` — 継承 invariant の regression guard** +4 tests
  - `test_adapter_error_inherits_root` / `test_no_providers_available_inherits_root` / `test_mid_stream_error_inherits_root` — `issubclass(X, CodeRouterError)` で継承関係を静的に assert。将来誰かが leaf の基底を `Exception` に巻き戻したら unit test が FAIL する lockstep
  - `test_adapter_error_instance_is_caught_as_root` — `raise AdapterError(...)` を実際に raise して `except CodeRouterError` で catch できることを instance レベルで確認。`str(exc) == "[p status=500] boom"` で `__str__` フォーマットまで合わせて lock (将来 `AdapterError.__str__` を変えるときに別 test として気づける)

### Changed

- **`AdapterError` / `NoProvidersAvailableError` / `MidStreamError` の基底を `Exception` → `CodeRouterError` に差し替え** — 3 ファイル × 1-2 行の変更
  - `coderouter/adapters/base.py`: `from coderouter.errors import CodeRouterError` を追加、`class AdapterError(Exception)` → `class AdapterError(CodeRouterError)`
  - `coderouter/routing/fallback.py`: 同 import 追加、`class NoProvidersAvailableError(Exception)` → `(CodeRouterError)`、`class MidStreamError(Exception)` → `(CodeRouterError)`
  - 既存 signature / docstring / behavior は verbatim 維持。MRO 上は `Exception` を継承しているので例外を bare `except:` や `except Exception:` で受けていたコードは影響なし
- **Docstring 網羅率 75.6% → 91.2%** (`interrogate coderouter` 基準、目標 90%)
  - 100% 化したファイル: `adapters/base.py` (Message / ChatRequest / AdapterError.__init__+__str__ / BaseAdapter.__init__+name に追加)、`adapters/openai_compat.py` (_headers / _payload / _url / generate / stream)、`adapters/anthropic_native.py` (_url / _headers)、`routing/fallback.py` (NoProvidersAvailableError.__init__ / MidStreamError.__init__ / FallbackEngine class + __init__ + generate)、`ingress/app.py` (create_app / lifespan / healthz / root / __getattr__)、`ingress/openai_routes.py` (chat_completions)、`ingress/anthropic_routes.py` (messages / _format_anthropic_sse)、`output_filters.py` (StripThinkingFilter.__init__+feed / StripStopMarkersFilter.__init__+feed / OutputFilterChain.__init__+is_empty)、`translation/anthropic.py` (AnthropicTextBlock / AnthropicUsage)、`translation/convert.py` (_convert_anthropic_tools)、`logging.py` (JsonLineFormatter.format / get_logger)
  - 残 gap (21 項目、今回 out of scope): `cli.py` の `_build_parser` / `main` (2)、`doctor.py` 内 private helper (5)、`config/capability_registry.py` の internal reader (3)、`config/loader.py` の `_candidate_paths` (1)、`translation/convert.py` の `_StreamState` stream-state helper (8)、`translation/tool_repair.py` 内 closure (1)、`translation/convert.py` helper 2 つ — いずれも真の internal / closure / stream-state plumbing で、public surface から外れた実装詳細。90% floor は public API で達成済
- **mypy `--strict` 0 errors を確認** — v1.0 系の compaction で取りこぼしていた 10 errors を以下で解消 (うち一部は既に v1.0-C 時点で修正済、未記録だった分を本 release で明文化)
  - `coderouter/ingress/openai_routes.py` / `anthropic_routes.py`: `@router.post(..., response_model=None)` + `payload: dict[str, Any]` + `-> StreamingResponse | dict[str, Any]` + `AsyncIterator[str]` を type 注釈に追加 (FastAPI が union return type を Pydantic field として reject する問題 + AsyncIterator の import)
  - `coderouter/routing/fallback.py`: `generate_anthropic` / `stream_anthropic` の Anthropic-shaped method 呼び出し箇所で `if is_native:` boolean guard を `if isinstance(adapter, AnthropicAdapter):` に書き換え — `is_native` boolean は log 用に保持、method 呼び出し分岐では mypy が narrowing できる形へ (`BaseAdapter` 自体が `generate_anthropic` / `stream_anthropic` を宣言していないため、boolean variable では narrow しない)
  - `coderouter/adapters/base.py`: `StreamChunk` に `usage: dict[str, Any] | None = None` field を明示宣言 (Pydantic の `extra="allow"` は runtime では許容するが mypy は見ないため、`convert.py` の reverse translation が `usage=...` kwarg を渡す箇所で Unexpected keyword を指摘していた)

### Non-Added (explicitly out of scope)

- **docstring の CI 強制** (`interrogate` を pre-commit / CI gate に昇格) — 91.2% を floor に設定したい気持ちはあるが、本 release は hygiene pass 1 発で treadmill を避ける、という scope 固定。gate 化は v1.0 系 follow-on が落ち着く v1.1 系で別 ticket に切り出す
- **pytest 間接的に含まれる他 `Exception` 継承 class** (adapters の upstream 4xx 抽象化候補など) — 今回は既存 3 leaf のみを `CodeRouterError` に帰属させた。新 leaf の追加タイミングで同 root に紐付ける規約を `errors.py` docstring + `tests/test_errors.py` の header で文書化済なので、次に増える時は invariant が壊れた瞬間 (= test FAIL) に気づける

### Follow-ons

- **`docs/retrospectives/v1.0.1.md`** — 本 release は hygiene pass なので narrative 書くほど厚みが無い。そのため retrospective skip。ただし v1.1 retrospective の冒頭で "v1.0.1 で足回りを整えてから v1.1 に入った" を 1 行で mention して系譜を保つ
- **docstring 90% CI gate** — `pyproject.toml` に `[tool.interrogate]` section を足し `fail-under = 90`、`pre-commit` or CI step で `interrogate coderouter` を回す。現状は手動 regression が無ければ coverage が少しずつ下がりうる (新コードに docstring を忘れた場合)
- **残 21 箇所の private docstring** — stream-state plumbing が中心なので「書いても読まれない」コスパ懸念あり。ただし `_StreamState._start_event` / `_close_current_block` / `_open_text_block` / `_open_tool_use_block` / `_handle_delta` は Anthropic SSE spec を知らないと読めないので、1-line ずつでも付けて state machine の役割を outline するのは独立した価値あり。v1.1 か v1.2 で別途

---

## [v1.0.0] — 2026-04-20 (Umbrella tag — The observation loop, closed)

**Theme: v1.0-A / v1.0-B / v1.0-C を束ねる umbrella tag。** v0.7 retrospective で "transformation には probe が伴う" 原則を予告した、その具体化を 1 つの minor に束ねた。v1.0-A で宣言的 `output_filters` filter chain (transformation) + doctor reasoning-leak probe 拡張 (probe) を同一 release で同梱、v1.0-B で v0.7-B の symptom #1 (input-side `num_ctx` truncation) を間接検出から直接検出へ置換 — canary `ZEBRA-MOON-847` + ~5K token padding + echo-back → 5-verdict branch + `extra_body.options.num_ctx: 32768` patch、v1.0-C で同じ手法を output-side に鏡像化 — `"Count from 1 to 30"` deterministic prompt を streaming で投げ `finish_reason="length"` + 短 content から output truncation を直接検出 + `options.num_predict: 4096` patch。Ollama の 2 つの truncation knob (入力側 `num_ctx` / 出力側 `num_predict`) 両方が直接観測可能になった。併せて v1.0-verify として 3-scenario 実機 runner (`scripts/verify_v1_0.sh`) + `verify-ollama-bare` / `verify-ollama-tuned` provider pair を整備 — v0.5-verify の bare/tuned delta assertion pattern を 2 度目の instance として再利用、実機 run (Ollama 0.20.5 + qwen2.5-coder:7b、2026-04-20 23:23 JST) で **3/3 PASS**。副次成果として Ollama 0.20.5 が `/v1/chat/completions` の request-time `options.num_ctx` / `options.num_predict` を silent override する build であることが判明 — bare 側で症状を induce できなかったため B+C scenario に **ADVISORY branch** を追加 (bare は advisory、tuned 側の `[OK]` flip と patch-default-value 反映が hard evidence)、`coderouter/doctor.py` の num_ctx probe canary-echoed 分岐を 3-branch に split (`declared is None` と `declared < threshold but still echoed` を診断的に分離)。narrative layer は [`docs/retrospectives/v1.0.md`](./docs/retrospectives/v1.0.md)、per-sub-release の機能詳細は下の `[v1.0-A]` / `[v1.0-B]` / `[v1.0-C]`、live-verify の evidence doc は [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md)。

- Tests: 382 → **453** (+71, +18.6%)、v1.0-A +49 / v1.0-B +10 / v1.0-C +12 / v1.0-verify ±0
- Runtime deps: 5 → 5 (output_filters は pure-Python scanner、num_ctx probe は padding + string match、streaming probe は `httpx.AsyncClient().stream()` + 文字列 SSE parse — 10+ sub-release 連続で SDK 依存ゼロを維持)
- Design through-lines:
  - **Transformation + probe in same release** (v1.0-A) — v0.7-B retrospective の宣言が v1.0 で習慣化。v1.0-A output filter chain と reasoning-leak probe 拡張が同一 release に同梱された
  - **Symptom-orthogonality heuristic for probe ordering** (v1.0-B / v1.0-C) — `num_ctx` は先行 probe の判定に干渉するため **chain 先頭寄り** (auth の直後)、`streaming` は直交軸なので **末尾**。"interferes-goes-first, orthogonal-goes-last" を明文化
  - **Stateful boundary scrubber with partial-suffix hold-back** (v1.0-A) — `_max_suffix_overlap` で chunk 境界の partial tag を保留、streaming / non-streaming で単一 code path を共有。将来の filter 追加時の shape を確立
  - **Ollama-shape signals as abstraction** (v1.0-B / v1.0-C 共用) — `_is_ollama_like(provider)` の 2-signal 判定 (`:11434` port OR `extra_body.options.num_ctx` declared) を v1.0-B で定義 → v1.0-C が verbatim 再利用、3rd Ollama-specific probe が来ても同じ helper に接続できる
  - **Bare/tuned delta assertion as live-verify convention** (v1.0-verify) — v0.5-verify の pattern が generalize することを 2nd instance で確認、v1.1-verify 以降の標準形に

### v1.0 umbrella-level follow-ons

v1.0 各 sub-release の follow-on は該当 section を参照。umbrella level で横串にかかるものは以下:

- **`num_ctx` + `num_predict` joint probe** — 同一 Ollama upstream の 2 knob を 1 verdict + 1 merged patch (`extra_body.options: {num_ctx: 32768, num_predict: 4096}`) で emit、`format_report()` 側で both-present を検出して patch を融合する post-processing 案。v1.1 scope
- **`_has_output_length_knob` / `_has_context_length_knob` generalization** — 2nd non-Ollama upstream with tunable context/output cap (vLLM `--max-model-len` / Together streaming quirks) が現れた時、`_is_ollama_like` を rename + multi-signal に拡張。現状 YAGNI
- **`FallbackChain.output_filters: list[str] | None`** — v0.6-B の shape (`timeout_s` / `append_system_prompt`) に合わせた chain-level override。staging/prod 分岐で filter を切りたい use case。v1.0-D or v1.1-A scope
- **Doctor probe-grouping refactor** — 6-probe chain (`auth / num_ctx / tool_calls / thinking / reasoning-leak / streaming`) を group 化 (`[auth] → [truncation: num_ctx, streaming] → [toolcall: tool_calls, thinking, reasoning-leak]`) + `--only truncation` / `--only toolcall` flag。v1.1 scope
- **Anthropic-native variant of v1.0-verify scenario A** — `/v1/messages` → `kind: anthropic` provider with `output_filters` declared。per-text-block chain isolation を live-verify で初証跡。v1.0-verify-B or v1.1-adjacent
- **Ollama 0.20.5 `options.*` passthrough investigation** — v1.0-verify の実機 run で `/v1/chat/completions` 経由の request-time `options.num_ctx` / `options.num_predict` を silent override する挙動を検出 (bare 側で症状 induce 失敗 → ADVISORY branch で回避)。v1.1 で (a) どの Ollama build から override が入ったか upstream CHANGELOG 確認、(b) `/api/generate` ネイティブエンドポイントでは honor されるか、(c) env var `OLLAMA_CONTEXT_LENGTH` 等の強制経路が使えるか、を調査。結果次第で doctor probe の induce 方式を変更 (request-body → env-var 注入) or probe 先を `/api/generate` に切替の判断。現状 advisory-bare / hard-tuned asymmetry で運用
- **`recover_garbled_tool_json` / tool-call 変換層 / Code Mode / prompt cache / 14-case 回帰 / チューニング既定値** — §10.1 original scope のうち v1.0.0 で deliver されたのは output-cleaning のみ。残り 5 は v1.1+ に明示 re-scope 推奨 (plan.md §10 の DoD 表更新含む)
- **`scripts/release-close.py`** — 4 retro 連続で follow-on に入って未実装。~9 doc touchpoint × 3 sub-release = ~27 手動 edit を自動化
- **Test-count auto-updater** — 3 retro 連続、`pytest --collect-only -q | wc -l` → chart 行自動生成。`release-close.py` と同時実装が最小コスト

---

## [v1.0-C] — 2026-04-20 (Doctor streaming-path probe — direct Ollama output-side truncation detection)

**Theme: v1.0-B の鏡像 — input-side truncation を直接観測できるようになった次は、output-side truncation を同じ粒度で観測する。** v1.0-B は prompt が `num_ctx` 不足で頭から切られて空応答になる症状を canary echo-back で直接検出した。ただし操作者が Claude Code で実際に遭遇するもう一つの silent-fail は **output 側** — 応答が途中で `finish_reason: length` で打ち切られる。典型的には Ollama の `options.num_predict` が default 128 (古い build) や 256 (一部 fork) のまま放置されているケース。v0.7-B の 4-probe + v1.0-B の `num_ctx` probe では、`max_tokens` を明示してない request で上流がどこまで出力するかの宣言層知識が無かったため、この症状は "なんか応答が途中で切れる" という操作者体感でしか拾えなかった。v1.0-C の streaming probe は SSE stream を最後まで consume して `finish_reason` + 実測 content 長さ を見て NEEDS_TUNING verdict と `options.num_predict: 4096` patch を emit する。v0.7 retrospective「silent-fail には直接 probe が必要」の symptom-coverage を 5 → 6 に拡張。v1.0-B (input-side) + v1.0-C (output-side) で Ollama 2-knob truncation の両面が直接検出可能になった。

- Tests: 441 → **453** (+12)
  - `tests/test_doctor.py` +12 (2 patch-emitter tests: `_patch_providers_yaml_num_predict` shape + YAML round-trip / 10 probe behavior tests: non-11434 port SKIP / non-Ollama kind SKIP chain / successful stream → OK / `finish_reason=length` + short content → NEEDS_TUNING + num_predict patch / zero-chunk JSON-instead-of-SSE → NEEDS_TUNING advisory no patch / no `[DONE]` terminator → OK with note / `extra_body.options.num_ctx` signal on non-11434 port fires streaming probe / outbound body carries `stream: true` + merged extra_body / HTTP 500 during streaming → SKIP / auth 401 short-circuits streaming probe)
- Runtime deps: 5 → 5 (`httpx.AsyncClient().stream("POST", ...)` は既に依存、SSE parsing は pure string slicing、依存追加なし)
- Non-breaking: v1.0-B で fixture `_oa_provider` が `localhost:8080` に寄せてあるので、既存 36 test は non-Ollama-shape 判定で streaming probe も SKIP で通過。Ollama-shape opt-in の既存 5 num_ctx test には新たに 5 つ目の SSE mock (`_add_sse_ok_mock`) を 1 行で追加

### Added

- **`coderouter/doctor.py` — `_probe_streaming(provider)` async function** (~130 LOC)
  - Deterministic prompt: `_STREAMING_PROBE_USER_PROMPT = "Count from 1 to 30, one number per line. Output only the numbers, nothing else."` — 正常応答は約 60-90 char (2 digit 数字 + 改行 × 30)、`num_predict=128` 辺りで頭打ちになると content が極端に短くなる observable pattern。canary のような hallucination 耐性は不要 — 出力長さ **そのもの** が被観測量
  - Threshold constants: `_STREAMING_PROBE_MIN_EXPECTED_CHARS = 40` (30 個の数字を改行区切りで並べるだけで 60+ char、40 を切るのは明確に打ち切られたケース)、`_STREAMING_PROBE_NUM_PREDICT_DEFAULT = 4096` (Claude Code の typical 応答 200-2000 token をカバーしつつ VRAM 圧迫を避ける運用値)
  - Probe body 構築: `body = dict(provider.extra_body); body.update({model, messages, max_tokens=128, temperature=0, stream=True})` — `num_ctx` probe と同じく operator が宣言した `options.*` を実際に使って送る唯一の 2 probe (他 4 は adapter 層をバイパスして raw 上流を見る)
  - 5-way verdict branch: (a) non-Ollama-shape (`_is_ollama_like` False) → SKIP; (b) transport error / 4xx / 5xx → SKIP + 診断 note; (c) 2xx + 0 chunks (JSON 応答が来た / 非標準 SSE framing) → NEEDS_TUNING **advisory** (server-side 設定なので patch は emit せず、"upstream silently ignored `stream: true`" を report); (d) 2xx + `finish_reason="length"` + content < 40 char → NEEDS_TUNING + `num_predict: 4096` patch; (e) 2xx + `finish_reason="stop"` + content 十分 → OK (`[DONE]` terminator が無ければ OK + informational note)
- **`_http_stream_sse(url, *, headers, body, timeout) -> tuple[int|None, list[dict], bool, str]`** helper — `httpx.AsyncClient().stream("POST", ...)` で SSE 消費、`resp.aiter_lines()` で `data: <json>` 行を json parse、`data: [DONE]` sentinel を observe。戻り値: (status, chunks, saw_done, error_text)。transport error は (None, [], False, error_msg) に均す (caller の branch logic を簡潔化)
- **`_patch_providers_yaml_num_predict(provider_name, desired_predict=4096) -> str`** — `_patch_providers_yaml_num_ctx` の sibling、`extra_body.options.num_predict: 4096` を emit。header comment で "merge into any existing extra_body.options" を明示 (operator が `num_ctx` を既に書いている既定ケースを想定、collision 回避指示付き)。YAML round-trip test で parse-able を保証
- **`_STREAMING_PROBE_USER_PROMPT` / `_STREAMING_PROBE_MIN_EXPECTED_CHARS` / `_STREAMING_PROBE_NUM_PREDICT_DEFAULT`** constants — `_NUM_CTX_ADEQUATE_THRESHOLD` と同じ section に module-level で宣言、test から直接 import 可能 (behavior invariant を test で lock する v0.5 以降の pattern)
- **`check_model` orchestration update**: 5 probe chain を 6 probe chain に拡張、`auth → num_ctx → tool_calls → thinking → reasoning-leak → streaming` の順に実行。streaming を **最後** に置くのは意図的 — num_ctx (input-side) / tool_calls / thinking / reasoning-leak はいずれも「宣言された capability vs 実測」の宣言層 probe、streaming は output-side 専用の独立観測軸。先行 probe の verdict に干渉しない位置に置くことで、streaming の NEEDS_TUNING が他 probe の dominant signal を塗りつぶさない (v1.0-B で num_ctx を tool_calls の **前** に置いたのとは逆方向の判断、症状カテゴリが直交しているため)
- **Auth short-circuit SKIP tuple 拡張**: `("num_ctx", "tool_calls", "thinking", "reasoning-leak")` → `("num_ctx", "tool_calls", "thinking", "reasoning-leak", "streaming")`。auth が通らない時は後続全 probe を SKIP で埋める invariant を 5-probe → 6-probe に broadcast

### Changed

- **`coderouter/doctor.py` モジュール docstring**
  - Symptom 対応表の symptom #1 行を `"空応答 / 意味不明応答 → num_ctx probe (v1.0-B) + streaming probe (v1.0-C)"` に更新。v1.0-B で input-side を直接拾えるようになり、v1.0-C で output-side も拾えるようになったことを明示 (symptom #1 は実は 2 種類の truncation が合流する beginner-level 症状で、probe side は分離する必要があった旨を section comment で補足)

- **README.md — v1.0-C status section**
  - 見出しを `## Status: v1.0-B — Direct num_ctx probe` → `## Status: v1.0-C — Streaming-path probe (2026-04-20)` に pivot
  - 段落を v1.0-B の output-side sibling として位置づけ直し: `finish_reason=length` + 短い content が典型 fingerprint、Claude Code ユーザから見える症状は "応答が途中で切れる"。`options.num_predict` が 128/256 default のまま放置されている Ollama build が主要な原因。count-1-to-30 の deterministic prompt、`extra_body.options.num_predict: 4096` patch、secondary "2xx with 0 chunks" symptom を advisory で拾う、Ollama-shape gating は v1.0-B 踏襲。test 数を 441 → **453** (+12) に更新、v1.0 系通算 +71 (49 + 10 + 12)

- **`tests/test_doctor.py` — 既存 Ollama-shape test に 5 番目の SSE mock 追加**
  - 5 つの既存 `extra_body={"options": {"num_ctx": ...}}` 系 test (declared-high canary-echoed OK / declared-low canary-missing bump / declared-adequate canary-missing intrinsic-limit / `extra_body.options.num_ctx` signal on non-11434 / `extra_body` merges into outbound body) に `_add_sse_ok_mock(httpx_mock, url)` を 1 行追加。6-probe chain が end まで走り切れるように
  - `_sse_stream_count_body(*, numbers=30, finish_reason="stop", include_done=True) -> bytes` / `_add_sse_ok_mock(httpx_mock, url, **kwargs)` test helper 追加 — 10 個の streaming probe test から共通利用、content-type `text/event-stream` + `data: {...}` × N + closing chunk with `finish_reason` + 任意 `data: [DONE]` を組み立て

### Design notes

- **Why streaming probe runs last, not before tool_calls like num_ctx.** v1.0-B の num_ctx probe を tool_calls の **前** に置いたのは、truncation (input-side) が tool_calls 不在を誤検出させる干渉関係があったため。v1.0-C の streaming probe は output-side で、他 probe の判定空間とは独立 — 先行 probe が OK で走った後に "応答が途中で切れる" が起きても、それぞれが異なる症状に対応する。probe chain の **最後** に置くことで、streaming の NEEDS_TUNING が他の dominant signal を塗りつぶすリスクをゼロにする。原則は「症状カテゴリが直交するなら末尾、干渉するなら先頭」
- **Why gate on `_is_ollama_like`, not all openai_compat.** 非 Ollama の upstream (OpenRouter / Together / Groq / Anthropic) は `options.num_predict` を honor する path が無い — 仮に streaming probe が truncation を検出しても patch の送り先が無い (`extra_body.options.num_predict: 4096` を書いても効かない)。対 Ollama 互換実装のうち、`num_predict` を honor しない fork (まれ) は content が十分長く来るので SKIP せず OK で抜ける、逆に honor する fork には正しい patch が届く。gate は v1.0-B の num_ctx probe と共用 (同じ `_is_ollama_like` helper) — 将来 vLLM の `max_model_len` / `max_tokens` を native に持つ upstream が増えたら、probe 側を `_has_output_length_knob(provider)` に rename + 別 signal 追加で拡張する余地。現状 YAGNI
- **Why "count from 1 to 30", not "echo this canary".** v1.0-B は prompt が truncate されたか (input-side) が問いなので canary echo-back が直接的。v1.0-C は response 自体が truncate されるか (output-side) が問いで、observable は「応答が短すぎる」こと **そのもの**。canary 方式だと "canary は出たが続きで説明文が切れた" を拾えない (canary は短いので num_predict=128 でも間に合う)。Count 1-to-30 は正常時約 60-90 char / `num_predict=128` で頭打ち時 15-30 char という分離が明瞭で、低い threshold (40 char) で確実に判定できる。数字というのは hallucination free なのも bonus — "0, 1, 2..." 改行 + temperature=0 で deterministic
- **Why `num_predict: 4096`, not "find the model's max".** `num_ctx` と同じ哲学 — doctor が model-specific limit database を抱え込むと責務超過。4096 は Claude Code の typical 応答 (200-2000 token) を余裕で飲み、かつ consumer GPU (24GB 以下) で 7B-14B モデルの KV cache 上限を使い切らない値。model の真の max (Llama 3.1 32K completion / Qwen2.5 4K default / etc.) を知りたい operator は patch を受けた後 dial up する
- **Why `num_predict` patch and `num_ctx` patch are separate emitters, not one "num_everything" helper.** `num_ctx` は input-side (prompt 全体を入れる buffer size)、`num_predict` は output-side (response に割り当てる token 数)。OpenAI 互換 API semantics では前者は implicit (prompt を投げた分だけ自動配置)、Ollama では explicit に `options.num_ctx` で宣言しないと 2048 default。`num_predict` は逆に OpenAI `max_tokens` に相当する概念だが、Ollama は request-body `max_tokens` を一部 ignore して `options.num_predict` を優先する build がある (実測)。2 つを別々の emitter に分けるのは、probe の verdict が (a) input 切れ / (b) output 切れ / (c) 両方 を区別して出せるようにするため。operator が両方 NEEDS_TUNING を受けたら 1 回の YAML edit で `extra_body.options: {num_ctx: 32768, num_predict: 4096}` にまとめて merge できる — header comment で merge direction を明示済み
- **Why advisory (no patch) for "2xx with 0 chunks".** 上流が `stream: true` を silent ignore して非 SSE 応答を返すケース (一部 reverse proxy、一部 LM Studio 旧 build 等) は、クライアント側 providers.yaml には修正点が無い — 上流サーバの設定ミス or fork bug。patch を emit しても "貼る先が無い" 状態になり、operator を混乱させる。代わりに "server returned 2xx with 0 streaming chunks — upstream may have ignored `stream: true`" の advisory を吐き、remediation は (a) 上流の streaming 設定確認 / (b) CodeRouter 側 `providers.yaml` で `stream: false` を強制する flag (現状無い、将来検討) を示唆する方向に留める。「patch が emit できないなら verdict は出しても patch 欄は空」という v0.7-B から続く contract を保つ

### Follow-ons

- **Anthropic native streaming variant for v1.0-D** — 現状 probe は `openai_compat` + Ollama-shape のみ発火。Anthropic native (`kind: "anthropic"`) の streaming probe を分離した `_probe_streaming_anthropic` として追加する案。ただし `api.anthropic.com` の `max_tokens` は request 側で明示必須 (server-side default 無し) なので symptom 発生経路が異なる — Claude Code が `max_tokens` をすでに request に含める場合はほぼ symptom が起きない。優先度は低、必要性は v1.0-C の real-machine verify で measure してから判断
- ~~**Real-machine verify for v1.0-C**~~ — **Landed 2026-04-20** via `scripts/verify_v1_0.sh` scenario C (streaming probe). The combined v1.0 verify (A + B + C in one runner) subsumes the originally-scoped per-release verify script. Bare `verify-ollama-bare` triggers the `streaming …… [NEEDS TUNING]` verdict with `num_predict: 4096` patch; tuned `verify-ollama-tuned` flips to `streaming …… [OK]`. Evidence inline in [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md) (v0.5-verify pattern — evidence embedded, not a separate file). Nginx reverse-proxy 0-chunk reproducer was deferred — the unit tests already lock that branch via pytest-httpx, and the symptom is environmentally specific (fork-dependent, not Ollama-default) so live verify would be flakier than it's worth
- **vLLM `max_model_len` detection (output-side)** — vLLM は `--max-model-len` で output-side cap を設定、`extra_body.max_tokens` で request 毎に絞る。Ollama の `num_predict` に意味論的対応。`_is_ollama_like` → `_has_output_length_knob` に rename して vLLM signal を追加する余地、v1.2+
- **Streaming probe timeout tuning** — 現状 `timeout=provider.doctor_probe_timeout_s` (default 5.0s) を使う。count-1-to-30 は CPU inference で 2-4 秒かかることがある (14B モデル + CPU-only CI)、将来 streaming probe 専用の timeout knob (`CodeRouterConfig.doctor.streaming_probe_timeout_s`) を提供する案。現状 default で CI green、defer
- **Canary collision for streaming probe** — v1.0-B と違い streaming probe は canary を使わないので collision リスクは無い。ただし count 1-to-30 を model が "要約して 5 個だけ出力" することは可能 (稀)。その場合 content length ≈ 10 char で誤 NEEDS_TUNING を出し得る。現状実測で issue なし、defer

---

## [v1.0-B] — 2026-04-20 (Doctor `num_ctx` probe — direct Ollama truncation detection)

**Theme: v0.7 retrospective「transformation には probe が伴う」の裏面 — probe そのものが症状の間接検出に頼っていた箇所を直接検出に置換する。** v0.7-B で `coderouter doctor --check-model` を出荷した時、plan.md §9.4 の 5 症状のうち 4 つ (symptom 2-5) は直接 probe を持っていた。残る symptom #1 — Ollama の `num_ctx` default 2048 による silent prompt truncation — だけが "tool_calls probe が `no tool_use emitted` を報告する" という **間接経路** にぶら下がっていた。これは操作者に誤った remediation (`capabilities.tools: false`) を提案するリスクがある。v1.0-B の `num_ctx` probe は canary echo-back スキームで truncation を直接観測し、`extra_body.options.num_ctx: 32768` という **正しい** patch を emit する。v0.7-B の 4-probe suite → v1.0-B の 5-probe suite で、5 症状すべてに固有の probe が対応するようになった。

- Tests: 431 → **441** (+10)
  - `tests/test_doctor.py` +10 (2 patch-emitter tests: `_patch_providers_yaml_num_ctx` shape + YAML round-trip / 8 probe behavior tests: port-heuristic SKIP / 11434 canary-missing NEEDS_TUNING / declared-high canary-echoed OK / declared-low canary-missing bump / declared-adequate canary-missing intrinsic-limit note / `extra_body.options.num_ctx` signal on non-11434 port / `extra_body` merges into outbound body / auth short-circuit includes num_ctx SKIP)
- Runtime deps: 5 → 5 (canary + padding は pure string、依存追加なし)
- Non-breaking: 既存 36 test は fixture `_oa_provider` の default base_url を `localhost:11434` → `localhost:8080` に寄せることで influence なし (port-heuristic によって non-Ollama-shape と判定 → probe SKIP)、test URL の replace-all (31 箇所) を先行して終わらせた

### Added

- **`coderouter/doctor.py` — `_probe_num_ctx(provider)` async function** (~85 LOC)
  - Canary constants: `_NUM_CTX_PROBE_CANARY = "ZEBRA-MOON-847"` (all-caps hyphenated token、自然言語に絶対現れない shape)、`_NUM_CTX_PROBE_PADDING_SENTENCE = "The quick brown fox jumps over the lazy dog near the river bank today. "` (~16 tokens)、`_NUM_CTX_PROBE_PADDING_REPEATS = 300` (~4800 tokens — Ollama default 2048 を確実に突破)、`_NUM_CTX_ADEQUATE_THRESHOLD = 8192` (Claude Code system + tool prompt 15-20k を受けるのに最低必要な headroom 考慮)
  - Prompt 構築: `"CANARY: ZEBRA-MOON-847\n\n" + padding*300 + "\n\nQuestion: What exact canary token appeared at the very beginning of this message?"`。canary を **先頭** に置くのがキモ — Ollama は overflow 時 beginning を truncate する
  - `provider.extra_body` を outbound body に shallow merge する唯一の probe (他の 4 probe は extra_body を無視する)。これによって operator が宣言した `options.num_ctx` を **実際に使って** truncation が起きるかを観測できる。merge order は `body = dict(provider.extra_body); body.update({model, messages, max_tokens, temperature})` — top-level probe fields が extra_body collision で勝つ (adapter の merge order と同じ semantics)
  - 5-way verdict branch: (a) canary echoed & declared ≥ 8192 → OK (operator tuned); (b) canary echoed & nothing declared → OK with informational note (upstream が non-default default を使っている、unusual but benign); (c) canary missing & nothing declared → `NEEDS_TUNING` + patch add 32768; (d) canary missing & declared < 8192 → `NEEDS_TUNING` + patch bump to 32768; (e) canary missing & declared ≥ 8192 → `NEEDS_TUNING` + "model intrinsic limit may be lower than declared" note (まれ、Llama 3 8B の 8192 cap 等で起きる)
- **`_is_ollama_like(provider) -> bool`** — 2 signal detection: (a) base_url が `:11434` を含む (Ollama canonical port); (b) `provider.extra_body.options.num_ctx` が宣言されている (only Ollama honors この path、operator が書いたなら by construction Ollama-shape)。`kind != "openai_compat"` は短絡で False。llama.cpp (:8080) / OpenRouter / Together / Groq / Anthropic native では fire しない — false positive 防止
- **`_declared_num_ctx(provider) -> int | None`** — `extra_body.options.num_ctx` を安全に int として取り出す helper。`options` が dict でないケースや value が int でないケースでは None
- **`_patch_providers_yaml_num_ctx(provider_name, desired_ctx=32768) -> str`** — nested YAML patch emitter: `extra_body: \n  options:\n    num_ctx: <n>`。v0.7-B の `_patch_providers_yaml_capability` / v1.0-A の `_patch_providers_yaml_output_filters` と対称形、comment header が "merge into any existing extra_body" を明示 (operator が他の options を持っている場合を考慮)
- **`check_model` orchestration update**: probe 実行順序を `auth → num_ctx → tool_calls → thinking → reasoning-leak` に変更。**num_ctx を tool_calls の前に置く** のは意図的 — 昔の挙動では truncation が `no tool_use emitted` と誤検出されて `tools: false` patch が提案されていた。num_ctx を先に走らせることで truncation verdict が報告で支配的になり、operator が正しい remediation を適用できる
- **Auth short-circuit SKIP tuple 拡張**: `("tool_calls", "thinking", "reasoning-leak")` → `("num_ctx", "tool_calls", "thinking", "reasoning-leak")`。auth が通らない時は後続全 probe を SKIP で埋める既存 invariant を 4-probe → 5-probe に broadcast

### Changed

- **`coderouter/doctor.py` モジュール docstring**
  - Symptom 対応表の symptom #1 行を `"空応答 / 意味不明応答 → num_ctx probe + basic-chat probe"` に更新 (v0.7-B 時代は `num_ctx probe` と書いてあったが、実在しなかった — v1.0-B で文字通り実在するようになった)。symptom #3 行も `thinking probe + reasoning-leak content-marker detection (v1.0-A)` に更新 (v1.0-A の副作用を反映)

- **`README.md` — Ollama beginner symptom #1**
  - "currently does not probe num_ctx (planned follow-on); symptom shows up indirectly as tool_calls probe..." の notice を削除
  - `coderouter doctor --check-model` 出力例の expected diagnostic を `num_ctx: NEEDS_TUNING — canary missing from reply; upstream truncated (no ``extra_body.options.num_ctx`` declared)` に書き換え、doctor が直接 emit する patch を pre-print
  - "As of v1.0-B the doctor probe detects this directly" 説明を追加、canary + 5K padding スキームと Ollama-shape gating (:11434 / declared options) を 1 段落で明示

- **`tests/test_doctor.py` — fixture migration** (`_oa_provider` default base_url)
  - `localhost:11434/v1` → `localhost:8080/v1` (llama.cpp port) に pivot。全 36 既存 test で fixture を使っていた URL 参照 (31 箇所) を `replace_all` で一括更新。これによって `_is_ollama_like` が False を返し、既存 test は mock 配列を 1 個も増やさずに済む (num_ctx probe が SKIP で通過)
  - fixture signature を `extra_body: dict[str, Any] | None = None` 受け取り形に拡張、Ollama opt-in test が `extra_body={"options": {"num_ctx": 32768}}` を宣言できるように

### Design notes

- **Why the `:11434` + `options.num_ctx` disjunction, not a boolean config flag.** v0.6-A / v0.6-D 以降の pattern で operator 明示指定を要求する手もあったが、(a) Ollama の fresh install は 100% `:11434` を使うので port から推論できる、(b) operator が `options.num_ctx` を書いている時点で Ollama 以外あり得ない (他のどの openai_compat upstream もこの path を honor しない) — なので "implicit signal of intent" で十分、新しい flag を増やすべきではない。false positive は `:11434` を使う自作サーバに限定されるが、その場合も `num_ctx` を honor する Ollama-互換実装なら probe は正しく動く、honor しない実装は canary が echoed されて OK で抜ける (最悪 informational OK になるだけで damage なし)
- **Why 300 repeats, not 500 or 150.** Ollama default `num_ctx = 2048` tokens を超えて truncation を **確実に** 誘発させるのに最小の padding は 2048 tokens 強 = 130 repeats 程度だが、chunking overhead や BPE tokenization のばらつきを考慮して 300 repeats (~4800 tokens) でマージンを取る。500 repeats だと timeout_s=5.0 の default fixture で risky (特に CPU-only CI)、150 repeats だと 2048 boundary に近すぎて一部 tokenizer で通ってしまう。300 は実測で safe margin
- **Why the canary is "ZEBRA-MOON-847", not a hash or UUID.** UUID だと prompt の先頭にあっても LLM が "hallucinate another UUID" と出力する可能性がある (自然言語の prior に近い形)。hash (e.g. `a7f9e2`) は逆に model が "reasonable answer shape" と認識してしまう。全大文字 + 2 ハイフン + アルファベット+数字の mix は natural text に絶対現れない shape — model は prompt で実見しない限り produce できない。長さ 14 char なので tokenizer が BPE で何トークンに分解しようが `in` match で拾える
- **Why emit `32768` as the default patch, not "find the max the model supports".** 32768 は Claude Code prompt (system + tool + user history) を余裕で受ける実用値、かつ consumer hardware (M-series 16-64GB / 24GB VRAM GPU) で大半のモデル (7B-14B) が走る閾値。model の真の max (Llama 3.1 128K / Qwen2.5 32K / etc.) を調査して optimal 値を出そうとすると doctor が model-name-to-context の別 registry を維持することになり責務超過。32768 を一律提案して operator が memory 制約で dial down する余地を残す方が運用コスト低
- **Why num_ctx probe runs before tool_calls, not last.** v0.7-B 時代の報告形では tool_calls probe が truncation 症状を最初に observable として拾って `NEEDS_TUNING: capabilities.tools` を出していた。num_ctx probe を後ろに置くと "tools=false + num_ctx=32768" という冗長な 2-patch 提案になる。前に置くことで num_ctx verdict が自然に支配的になり、次回 run では num_ctx が OK になって tool_calls 本来の verdict が観察できる。"一番 dominant な症状を先に出す" は v0.7 retrospective 「silent-fail には optimal な診断順序がある」の具体化
- **Why `extra_body` shallow-merge in the probe.** 他の 4 probe (auth / tool_calls / thinking / reasoning-leak) は `extra_body` を無視する — probe の目的が "adapter 層を迂回して raw upstream 応答を見る" ことで、`extra_body` をまじなに merge すると "adapter が add する field (`think: false` etc.)" との相互作用まで test してしまう。num_ctx probe は唯一例外 — probe の存在意義自体が "宣言された `options.num_ctx` が実際に効いているか" の観測なので、`extra_body.options` を送らない probe は意味を成さない。merge は top-level shallow (option fields は nested dict のまま保持)、probe 固有の top-level (`model` / `messages` / `max_tokens` / `temperature`) は extra_body を上書きする順序で確定性を保つ

### Follow-ons

- ~~**Real-machine verify for v1.0-B**~~ — **Landed 2026-04-20** via `scripts/verify_v1_0.sh` scenario B (num_ctx probe). Bare `verify-ollama-bare` triggers `num_ctx …… [NEEDS TUNING]` + `num_ctx: 32768` patch; tuned `verify-ollama-tuned` flips to `num_ctx …… [OK]`. Paired with scenario C (streaming) they share a single doctor CLI invocation per side (the 6-probe chain runs all at once). Evidence inline in [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md)
- **Probe model detection accuracy** — 将来的に Ollama 以外で `num_ctx`-ish knob を持つ upstream (例: vLLM の `max_model_len`) が openai_compat を名乗って出てきた時、`_is_ollama_like` を `_has_context_length_knob` に rename + multi-signal (vLLM なら `extra_body.max_model_len`) に拡張する余地。現状 YAGNI
- **Dynamic threshold** — `_NUM_CTX_ADEQUATE_THRESHOLD = 8192` を hard-coded している。Claude Code 側の system prompt が将来 30k まで膨らむと 8192 では足りなくなる (現状でも tool 全開宣言で 18-20k)。`CodeRouterConfig.doctor.min_context: int` を provide して operator が上書きできるようにする案、v1.2 scope (doctor の configuration hierarchy を整えるタイミングで)
- **Canary collision risk** — 極めて低確率だが、training corpus に "ZEBRA-MOON-847" が入っている model は "canary" が truncate されていないのに model が hallucinate できてしまう。probe の反証: canary を毎回 random 生成 (process-local seed、同じ session 内の再現性は保つ) に変える。現状実測で issue なし、defer

---

## [v1.0-A] — 2026-04-20 (Declarative output cleaning chain)

**Theme: v0.7 retrospective で予告した「transformation には probe が伴う」原則の first application。** v0.5-C の reasoning-field passive strip は "model が reasoning field を吐いてくれれば" のみに効く受動層だった。しかし現場の Ollama / HF 蒸留モデルは `<think>...</think>` や `<|turn|>` / `<|channel>thought` 等を **content チャネルに inline で** 流し込んでくる (v0.7-C README symptom #3)。これらを adapter 境界で **宣言的** に剥がす filter chain を追加した。`providers.yaml` に `output_filters: [strip_thinking, strip_stop_markers]` を書くだけで、streaming / non-streaming 両方、OpenAI-compat / Anthropic native 両 adapter で一貫して動く。併せて v0.7-B の reasoning-leak probe を拡張 — content-embedded `<think>` / stop markers を検出し、必要な filter を列挙した `providers.yaml` patch を emit する。**宣言 (v0.7-A YAML) → probe (v0.7-B doctor) → transformation (v1.0-A filter chain)** の triad でやっと "beginner が踏む症状 3 (think-leak)" の観測ループが閉じた。

- Tests: 382 → **431** (+49)
  - `tests/test_output_filters.py` +31 (pure unit: chunk-boundary correctness / chain composition / validate registry)
  - `tests/test_output_filters_adapters.py` +12 (adapter integration: generate / stream / tail flush / per-block chain isolation)
  - `tests/test_config.py` +3 (`output_filters: [...]` schema validation at load time)
  - `tests/test_doctor.py` +3 (reasoning-leak probe: content `<think>` / stop markers detection + patch shape + silence when already configured)
- Runtime deps: 5 → 5 (pure stateful scanner、依存追加なし)
- `examples/providers.yaml`: `ollama-qwen-coder-7b` / `-14b` / `ollama-hf-example` stanza に `output_filters: [strip_thinking]` を enable

### Added

- **`coderouter/output_filters.py`** (新規モジュール、public API ~280 LOC)
  - `DEFAULT_STOP_MARKERS: tuple[str, ...]` — Claude Code で実測された 6 markers: `<|turn|>` / `<|end|>` / `<|python_tag|>` / `<|im_end|>` / `<|eot_id|>` / `<|channel>thought`。閉じカギカッコ省略形 (`<|channel>thought`) を含むのは実機観測ベース。変更には CHANGELOG note を義務付け (regression test `test_default_stop_markers_contents` で lock)
  - `KNOWN_FILTERS: tuple[str, ...] = ("strip_thinking", "strip_stop_markers")` — registry、v1.0-A 時点で 2 filter
  - `validate_output_filters(names: list[str]) -> None` — unknown name は `ValueError` で known names を列挙。typo `strp_thinking` → コピペで直せるエラーメッセージに
  - `OutputFilter` (Protocol) — `feed(text: str, eof: bool = False) -> str` / `modified: bool`。stateful、per-stream 1 instance 原則
  - `StripThinkingFilter` — `<think>...</think>` を inclusive で除去。partial tag (`<thi` / `</thi`) は chunk 境界で hold-back、EOF で unmatched open があれば tail drop (未完了 thinking block の流出を防ぐ)
  - `StripStopMarkersFilter` — `_earliest_match(buffer)` で最初にヒットした marker を iterative に strip、partial marker (`<|pyth`) は hold-back。marker で無い `<|` は EOF で flush
  - `_max_suffix_overlap(buffer, needle)` — longest N where `buffer[-N:] == needle[:N]`、chunk-boundary hold-back の核心ルーチン (両 filter で共通)
  - `OutputFilterChain(filter_names)` — declaration 順で適用。`any_applied` / `applied_filters()` / `names` / `is_empty` / `feed`。unknown name は construction 時に `ValueError` (fast-fail)
  - `apply_output_filters(names, text) -> (scrubbed, applied)` — non-streaming convenience。空 chain は identity、適用された filter 名のみ返す
- **`coderouter/config/schemas.py` — `ProviderConfig.output_filters`** (新 field)
  - `output_filters: list[str] = Field(default_factory=list, ...)` を `append_system_prompt` の直後に配置 (v0.6-B の sibling position)
  - `@model_validator(mode="after") _check_output_filters_known` で `validate_output_filters` を呼ぶ。import は local (config → output_filters の one-way dependency を維持、cycle 回避)
- **`coderouter/adapters/openai_compat.py` — filter hook** (`generate` + `stream`)
  - `generate()`: v0.5-C reasoning strip の直後に `data["choices"]` iteration を挿入、各 `message.content` に `OutputFilterChain.feed(text, eof=True)` を適用、`any_applied` なら `log_output_filter_applied` で 1 message 1 log
  - `stream()`: 入口で `filter_chain: OutputFilterChain | None` を provider 宣言から lazy 構築、`output_filter_logged: bool` と `last_chunk_template: dict | None` を track。per-chunk で `delta["content"]` に `chain.feed(text)` を適用 (eof=False)。`[DONE]` 受信時は従来の `return` を `break` に変更 → loop 後の flush code path に処理を集約。`chain.feed("", eof=True)` で hold-back された tail を flush、非空なら `last_chunk_template` から `id` / `model` / `created` / `system_fingerprint` を借りた synthetic SSE chunk を 1 発 yield (OpenAI SDK 互換性を壊さない)、最後に `[DONE]` を再送して実ストリーム終端
- **`coderouter/adapters/anthropic_native.py` — filter hook** (`generate_anthropic` + `stream_anthropic`)
  - `generate_anthropic()`: response parse 後、`data["content"]` の各 block について `block["type"] == "text"` なら fresh `OutputFilterChain` を作って `block["text"]` に適用。applied filter の union を log (per-response 1 log)
  - `_process_stream_event_for_filters(event, *, chains, logged_flag) -> list[event]` 新 helper
    - `content_block_start` (type=text) → `chains[index]` に fresh chain を格納
    - `content_block_delta` (type=text_delta) → `chains[index].feed(delta["text"])` で in-place mutation
    - `content_block_stop` → `chains[index].feed("", eof=True)` で tail を取得、非空なら同 index 向けの synthetic `content_block_delta` event を `content_block_stop` の **前に** prepend (event 順を自然に保つ)。`logged_flag: list[bool] = [False]` mutable cell で 1 stream 1 log を保証
  - `stream_anthropic()`: 2 か所の `yield AnthropicStreamEvent(...)` 呼び出しを `for out_event in self._process_stream_event_for_filters(...): yield out_event` に置換、入口で `filter_chains: dict[int, OutputFilterChain] = {}` + `logged_flag = [False]` を初期化。**per-text-block chain** なので block 0 の未完了 `<think>` が block 1 に漏れない
- **`coderouter/logging.py` — `log_output_filter_applied` chokepoint helper**
  - `OutputFilterAppliedPayload` TypedDict: `provider: str` / `filters: list[str]` / `streaming: bool`
  - `log_output_filter_applied(logger, *, provider, filters, streaming)` — info level、`log_capability_degraded` と同じ pattern (single chokepoint、payload typed、provider 横断の集計が容易)
- **`coderouter/doctor.py` — reasoning-leak probe extension**
  - prompt を `"In one word: capital of France?"` → `"Think step by step about the capital of France, then answer in one word."` + `max_tokens=128` に変更 (thinking block を誘発して leak 経路を確実に叩く)
  - parse 後: `has_think = "<think>" in content_text` / `leaked_markers = [m for m in DEFAULT_STOP_MARKERS if m in content_text]` を計算、`provider.output_filters` の現状と照合
  - `needs_strip_thinking or needs_strip_markers` → verdict `NEEDS_TUNING`、`_patch_providers_yaml_output_filters(provider_name, filters)` が `providers:\n  - name: <p>\n    output_filters: [<missing>]` 形の copy-paste 可能 patch を emit
  - 未検出時の OK detail を `"no `reasoning` field observed and no content-embedded markers — nothing to strip."` に更新 (既存 test `test_reasoning_leak_not_present_reports_clean` が "nothing to strip" を assert しているので保持)
  - `format_report` の declarations section に `output_filters` 行を追加

### Changed

- **`examples/providers.yaml`**
  - `ollama-qwen-coder-7b`: `output_filters: [strip_thinking]` + 解説コメント (Qwen2.5-Coder は Claude Code の tool-heavy prompting で `<think>` を間欠的に流す / `strip_stop_markers` は Ollama chat template が `<|im_end|>` で clean に終端するので不要)
  - `ollama-qwen-coder-14b`: 同じく `output_filters: [strip_thinking]` + 14b は scrub cost が cheap なので unconditional enable
  - `ollama-hf-example` (commented stanza): 症状 3 (v0.7-C README) の remediation が 2 経路 (source-side `/no_think` / output-side `output_filters`) に整理されていることを stanza 内 comment で明示、`output_filters: [strip_thinking]` を commented-in form で提示 (uncomment → 即 active)。古い `reasoning_passthrough` hint 行は削除 (v1.0-A 経路の方が general のため)

### Design notes

- **Why `output_filters` lives on `ProviderConfig`, not `FallbackChain`.** filter の必要性は model 族依存 (Qwen2.5-Coder は `<think>` を流す / Claude は流さない) で、chain 依存ではない。v0.6-B で `FallbackChain.timeout_s` / `append_system_prompt` を provider 上書き形で入れたのと同じ philosophy: 「default は provider 宣言」「chain は部分 override 可能 (必要になれば v1.0-B で追加)」
- **Why stateful filter, not regex `re.sub`.** streaming で chunk が `<thi` / `nk>` に割れた時、regex は match しない (chunk 1 回目が "hello <thi" のまま leak する)。`_max_suffix_overlap` で partial suffix を hold-back する scanner を書く方が regex 合成より短く、かつ streaming と non-streaming で同じ code path を共有できる。`re.sub` route は non-streaming 専用の二重実装を招くので却下
- **Why per-text-block chain on Anthropic.** Anthropic native は 1 response に複数 `content_block` (text / tool_use / thinking) を直列で吐く。`<think>` が block 0 の末尾で未完了のまま block 1 (text) が始まる場合、per-stream 1 chain だと block 1 が全部 hidden 扱いになって可視部分が消失する。`dict[int, OutputFilterChain]` で block index ごとに isolated な chain を持つことで、block 境界が state も reset する。block 0 で未完了なら block 0 の text が EOF で drop されるだけ (block 1 は正常に流れる)
- **Why `_process_stream_event_for_filters` returns a list of events.** synthetic flush event (hold-back された tail の吐き出し) は `content_block_stop` の **直前** に挿入される必要がある。呼び出し側が単純に `yield event` していた元 code を壊さずに "1 入力 event → 0 ~ 2 出力 events" の可能性を表現するため、list 返却 + `for ... yield` の展開に統一。Python の generator delegation (`yield from`) でも書けるが、list の方が test しやすい
- **Why fast-fail at config load, not at first request.** unknown filter name を request 到達時まで遅延させると、config deploy から symptom 観測までが長くなる。`validate_output_filters` を `ProviderConfig` validator + `OutputFilterChain` constructor の 2 箇所で呼ぶことで、(a) YAML load 時に全 provider 分を一括検証、(b) test 等で chain を直接組む path でも同じエラーが出る、を両立
- **Why `log_output_filter_applied` fires at most once per stream.** filter 適用は SSE の delta ごとに起きうるが、observability の観点で欲しい粒度は "この request で strip_thinking が発動したか" であって "何回 chunk が scrub されたか" ではない。`output_filter_logged: bool` / `logged_flag: list[bool] = [False]` の mutable flag pattern で 1-stream 1-log を保証。非 streaming も同様 (per-message 1 log)
- **Why the doctor probe prompt became "Think step by step about...".** 従来の "capital of France?" だと tuned model は thinking block を吐かずに一発で答え、leak の検出機会を逸する。prompt を "step by step" にし max_tokens を 128 に bump することで、thinking を誘発しつつ検出されるべき `<think>` / stop markers が確実に現れる。v0.7-B retrospective の "probe は観測すべき経路を能動的に活性化すべき" の follow-on
- **Why the probe emits filter patches, not just diagnostics.** v0.7-B の tool_calls probe が `capabilities.tools: false` を copy-paste 形で emit したのと同じ philosophy: 検出した症状に対して **operator が即適用できる remediation** を併走させる。patch の列挙順は検出順 (`strip_thinking` first if `<think>` found / `strip_stop_markers` second if markers found)、chain declaration 順と一致するので YAML に貼り付けるだけで期待通りに動く

### Follow-ons

- ~~**Real-machine verify for v1.0-A**~~ — **Landed 2026-04-20** via `scripts/verify_v1_0.sh` scenario A (filter chain). Routes a `/v1/chat/completions` request through CodeRouter against `verify-v1-bare` then `verify-v1-tuned`, asserts the tuned response's `message.content` is `<think>`-free AND the server stderr log contains an `output-filter-applied` record for `filters=["strip_thinking"]`. Bare side is advisory (qwen is stochastic; if it doesn't emit `<think>` on the sample the script reports "symptom could not be induced" rather than failing). Evidence inline in [`docs/retrospectives/v1.0-verify.md`](./docs/retrospectives/v1.0-verify.md). v0.7 retrospective follow-on #5 (real-machine verify for v0.7) remains scheduled for v0.8 scope — that pass will also sanity-check model-capabilities.yaml matcher against live provider metadata
- **Additional filters** — `strip_tool_call_text_wrapper` (v0.3-A の text→tool_calls lifting と対になる "万一流出した場合の scrubber")、`collapse_whitespace` (model によっては `<think>` strip 後に `"hello  world"` の 2 連 space が残る) を `KNOWN_FILTERS` に追加する候補。現状は YAGNI
- **Filter performance under chunk storms** — 1 SSE chunk が 1-2 文字しか含まない model (一部 Ollama 設定) で `_max_suffix_overlap` が `len(buffer) * len(markers)` で O(N*M) になる。現状 DEFAULT_STOP_MARKERS 6 本 × 平均 marker len 10 なので worst 60 ops/chunk で無視できるが、将来 marker 数が増えたら trie ベースに置換 (v1.5+ scope)
- **Chain-level `output_filters` override** — v0.6-B の `FallbackChain.timeout_s` / `append_system_prompt` と同じく chain-level 上書きが欲しいケース (stage-env ではフィルタ無効、prod では有効) が想定される。現状は provider を分割すれば済むが、v1.0-B or v1.1 で `FallbackChain.output_filters: list[str] | None` として追加検討
- **Doctor probe: streaming path** — 現在の `_probe_reasoning_leak` は non-streaming endpoint を叩く。streaming で `<think>` が chunk 境界に割れた時のみ leak する稀な failure mode は拾えていない。`_probe_reasoning_leak_streaming` を v1.0-C 以降で足す (v0.5.1 A-2 の streaming verify pattern を再利用。v1.0-B は先に num_ctx direct probe を解消した)

---

## [v0.7.0] — 2026-04-20 (Umbrella tag — Beginner UX, made legible)

**Theme: v0.7-A / v0.7-B / v0.7-C を束ねる umbrella tag。** 「Ollama 立てたけど動かない」を 1 コマンドで切り分け可能にする minor。plan.md §9.4 の silent-fail 5 症状 (num_ctx truncation / tools incompetence / `<think>` leak / model-tag 404 / missing API key) を contract として、(A) 宣言を Python literal から YAML に外出し、(B) 宣言と実機を突合する live-probe (`coderouter doctor --check-model <provider>`) を実装、(C) 症状 × probe コマンド × YAML patch × fix command の 3–4 点セットを README Troubleshooting に章立て — の 3 段階で beginner UX の観測ループを閉じた。narrative layer は [`docs/retrospectives/v0.7.md`](./docs/retrospectives/v0.7.md)、per-sub-release の機能詳細は下の `[v0.7-A]` / `[v0.7-B]` / `[v0.7-C]`。

- Tests: 306 → **382** (+76, +25%)、v0.7-A +39 / v0.7-B +37 / v0.7-C ±0
- Runtime deps: 5 → 5 (SDK 依存ゼロ維持、probe は pure httpx + pyyaml + pydantic)
- Design through-lines:
  - **Data-as-configuration** (v0.7-A) — bundled + user 2 層の YAML registry が Python 内 regex literal を置換
  - **Diagnostic surface that bypasses runtime transformations** (v0.7-B) — probe は adapter を介さず直接 httpx、transformation の観測穴を塞ぐ
  - **Dominant-signal short-circuit with SKIP preserved** (v0.7-B) — auth 失敗で残り probe を SKIP、透明性は維持 / token は消費しない
  - **Non-code release as a sub-release boundary** (v0.7-C) — docs + examples を独立 sub-release として versioning

### v0.7 umbrella-level follow-ons

v0.7 各 sub-release の follow-on は該当 section を参照。umbrella level で横串にかかるものは以下:

- **`coderouter doctor` `num_ctx` probe** (symptom #1 direct detection、v0.8 scope)
- **`coderouter doctor --json` output** (CI auto-PR bot 向け、v0.7-D or v0.8)
- **CI smoke workflow**: 週次 `doctor --check-model <each-free-provider>`、v0.5-D cron の対称
- **v1.0 output-cleaning 時の probe 追加** — 「transformation には probe が伴う」原則の適用
- **Real-machine re-verify for v0.7** (`scripts/verify_v0_7.sh` 相当)
- **Test-count auto-updater** (3 retro 連続で名指し、未実装)
- **Doc-edit touchpoint automation** (`scripts/release-close.py` 案、~9 手動編集の自動化)

---

## [v0.7-C] — 2026-04-20 (Ollama beginner Troubleshooting + HF-on-Ollama reference profile)

**Theme: v0.7-A / v0.7-B で構築した宣言レイヤ + probe を「運用者の目線」に落とし込む。** v0.7-A で registry を YAML 化し、v0.7-B で live-probe を導入したが、**どの症状に対してどのコマンド / どの YAML patch を出せばよいか** の導線が README に無ければ beginner は依然として trial-and-error に戻る。v0.7-C は non-code deliverable のみ: plan.md §9.4 の 5 症状を README Troubleshooting に章立てし、各症状に `coderouter doctor --check-model` 実行例 + 具体的な YAML patch + fix の 3 点セットを添付する。併せて `examples/providers.yaml` に HF 蒸留 Ollama provider の reference stanza を追加 (commented-out template、5 knob 全て 1 block で demonstrate)。lunacode [`MODEL_SETTINGS.md`](https://github.com/zephel01/lunacode/blob/main/docs/MODEL_SETTINGS.md) とのクロスリンクで editor-harness layer との対応も明示。これで v0.7 umbrella は deliverable level 完了、`v0.7.0` tag + retrospective 執筆へ。

- Tests: 382 → **382** (コード変更ゼロ、docs + example config のみ)
- plan.md §9.4 DoD: 残り 2 項目中「README Troubleshooting に 5 症状全て記述」を消化。`v0.7.0` umbrella tag + retrospective 執筆の 1 項目が最後

### Added

- **README — `### Ollama beginner — 5 silent-fail symptoms (v0.7-C)`** (Troubleshooting セクション末尾に新規 subsection)
  - 症状 1: num_ctx truncation (`extra_body.options.num_ctx: 32768`) — doctor で **indirect** 検出 (tool_calls probe が "no tool_call emitted" を返す症状として観測)。num_ctx probe 自体は follow-on で v0.7-B の CHANGELOG に明記済み
  - 症状 2: tools=false 未宣言 (`capabilities.tools: false`) — doctor `tool_calls: NEEDS_TUNING` で検出、patch は doctor output 末尾の copy-paste YAML そのまま
  - 症状 3: `<think>` tag leak (`append_system_prompt: "/no_think"` + 将来的に v1.0 output-cleaning) — doctor `reasoning-leak: informational` で検出
  - 症状 4: model tag typo / `ollama pull` 忘れ (404) — doctor `auth+basic-chat: UNSUPPORTED` で検出、`ollama pull <tag>` hint 付き。HF-on-Ollama の `:Q4_K_M` suffix 忘れも同じ分類
  - 症状 5: API key 未設定 (401) — doctor `auth+basic-chat: AUTH_FAIL` で検出、env var 名付きで diagnose。残り 3 probe を SKIP にする auth short-circuit の UX 上の価値をここで回収
  - 末尾に `for p in <providers>; do coderouter doctor --check-model "$p"; done` の loop 例と、exit code 表 (Doctor subsection) への anchor link
  - lunacode [`MODEL_SETTINGS.md`](https://github.com/zephel01/lunacode/blob/main/docs/MODEL_SETTINGS.md) への cross-link — CodeRouter provider-granularity vs lunacode per-model-granularity の棲み分けを一言で説明
- **README — `#### HF-on-Ollama reference profile`** (subsection、上の 5 症状 section 直下)
  - `examples/providers.yaml` の `ollama-hf-example` stanza への導線
  - HF GGUF が 5 症状全てを増幅する理由 (chat template 欠落 / distillation 由来の `<think>` 漏れ / quant suffix 必須) を 1 段落で説明
- **`examples/providers.yaml` — `ollama-hf-example` stanza** (commented-out reference、Ollama Tier 1 の直後に配置)
  - `base_url: http://localhost:11434/v1` + `model: hf.co/unsloth/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M` を default example に
  - コメントで候補 3 種類 (Qwen2.5-Coder / Qwen3-8B / DeepSeek-R1-Distill-Qwen) を列挙
  - `extra_body.options.num_ctx: 32768` — 症状 1 対応、コメントで Claude Code system prompt の token 規模を明記
  - `append_system_prompt: "/no_think"` (commented sub-line) — 症状 3 対応、Qwen3 / R1-distill だけ有効と明記
  - `capabilities.tools: false` default — 症状 2 対応、`coderouter doctor` で OK が出たら flip する運用を記述
  - `reasoning_passthrough: false` (commented) — 症状 3 の流出対応、v1.0 output-cleaning との関係を明記
  - `:<quant>` suffix 必須の warning (症状 4 の HF 特化版) を stanza header comment に
- **README — `#### Doctor` subsection の Troubleshooting からの anchor**
  - 5 症状 section 末尾から Doctor subsection の exit code 表に戻る cross-link

### Changed

- **README の "Coming next" リスト** — v0.7-C 項目を削除、v1.0 を先頭に (次のマイルストーンは 14-case regression suite + Code Mode)
- **README の Troubleshooting 導入行** — 既存「まず `coderouter doctor --check-model <provider>` を走らせろ」の案内はそのまま、5 症状 subsection が新設されたことで「先に読むべき項目」が整理された

### Design notes

- **non-code-only release としての v0.7-C.** v0.7-A が YAML 外出し、v0.7-B が probe という「実装寄り 2 release」に対して v0.7-C は意図的に docs + example config のみ。probe が存在しても operator がそれを「症状 → コマンド → patch」の 3 点セットとして認識できなければ価値が出ないため、non-code だが独立 release として切り出した。plan.md §9.4 の scope 表で最初からこの切り方を宣言していた意図の回収
- **5 症状の配列順は「検出しやすさ」ではなく「初心者が踏みやすい順」に.** 症状 1 (num_ctx) は CodeRouter の doctor では **indirect** にしか検出できないが、Ollama を初めて Claude Code と繋げた時に最初に踏む地雷なので筆頭に配置。症状 5 (API key 未設定) は最も確定的に検出できるが、ある程度セットアップが進んだ段階で踏む症状なので末尾
- **各症状に「検出コマンド出力」の 1 行モック例を添える.** 実際の `coderouter doctor` 出力を 1 行だけ貼る (`# → tool_calls: NEEDS_TUNING — ...` 形式) ことで、operator が実行前に何が見えるかを想像できるようにした。full 出力は Doctor subsection に既に載せてあるため、ここでは該当 probe の verdict line だけ
- **HF-on-Ollama stanza を commented-out で置く理由.** uncomment して初めて active になる設計は、(a) fresh install の default chain を HF provider に汚染させない、(b) operator に「自分で pull して uncomment」の 2 step を踏ませることで `:<quant>` suffix の記入ミスを事前に意識させる、という 2 つの効果を持つ。active な HF provider を example に含めると `coderouter serve` が fresh install 時点で `ollama pull` されていない model 名に対して 404 を吐き続ける failure-by-example になる
- **lunacode MODEL_SETTINGS.md との関係の明示.** 同一作者の兄弟プロジェクトという関係性から、両プロジェクトの知見が重複する部分は多い。ただし CodeRouter は **provider-granularity** (「このプロバイダ経由で使う model の capability」) で宣言し、lunacode は **per-model-granularity** (「この model そのものの設定」) で宣言するため、同じ症状でも declaration の位置が違う。README cross-link は 2 つのプロジェクトを並行運用する場合の "どっちの設定ファイルを触るか" の判断材料として機能する

### Follow-ons

- **`coderouter doctor` num_ctx probe の追加** — 症状 1 を direct 検出するために 5th probe を導入。8K / 16K / 32K の境界で silent truncation するかを確率的にサンプリング (長 prompt + 末尾に marker phrase → response に marker が含まれるか)。v0.8 scope
- **`coderouter doctor --json` output** — CI 向け machine-readable 出力。exit code + 症状の JSON array で auto-patch bot が parse できる shape。v0.7-B CHANGELOG でも言及済み、v0.8 で回収
- **v0.7.0 umbrella tag + `docs/retrospectives/v0.7.md`** — 本 release の直後に commit で消化。plan.md §9.4 DoD 残り 1 項目
- **HF-on-Ollama reference stanza の bundled `model-capabilities.yaml` 対応物** — 現状は per-provider `capabilities.tools: false` で opt-out する設計だが、HF GGUF 特有の glob (`hf.co/unsloth/*` 等) を bundled YAML に足すかは要判断。provider-granularity 原則と矛盾するため v0.7 では見送り

---

## [v0.7-B] — 2026-04-20 (`coderouter doctor --check-model` — per-provider live probe)

**Theme: 「Ollama 立てたけど動かない」を 1 コマンドで切り分け可能にする。** v0.7-A で宣言を YAML に外出しした registry と、providers.yaml の `capabilities.*` explicit opt-in が揃った今、次に足りないのは「宣言と実機挙動の差分を **事前に** 検出する仕組み」だった。v0.7-B では `coderouter doctor --check-model <provider>` を実装し、1 provider に対して 4 probe (auth / tool_calls / thinking / reasoning-leak) を順に走らせ、registry + providers.yaml の宣言と実測を照合して、乖離時には copy-paste 可能な YAML patch を emit する。plan.md §9.4 の 5 症状 (特に #2 tools / #3 thinking / #4 auth / #5 model-not-found) に対する**事前診断**の第一歩。

- Tests: 345 → **382** (+37、`tests/test_doctor.py` +31、`tests/test_cli.py` +6)
- Exit-code contract: `0` = match / `2` = needs_tuning / `1` = auth_fail | model-not-found | transport-error (CI smoke で grep 可能な "Exit: N" 終端行付き)
- 非破壊: probe は read-only、tool-spec は fake `echo` で side-effect なし、auth 失敗時は remaining probe を SKIP にして token 消費を止める

### Added

- **`coderouter/doctor.py`** (新モジュール、~600 行、probe 本体 + reporting)
  - `ProbeVerdict` enum: `OK / SKIP / NEEDS_TUNING / UNSUPPORTED / AUTH_FAIL / TRANSPORT_ERROR`
  - `ProbeResult` / `DoctorReport` dataclass — per-probe verdict + `suggested_patch` + `target_file` (`providers.yaml` / `model-capabilities.yaml`)
  - `exit_code_for(report)` — blocker (auth/unsupported/transport) > needs_tuning > ok の precedence で 0/1/2 を返す
  - **Probe 1 `auth+basic-chat`** — `POST /chat/completions` (openai_compat) or `POST /v1/messages` (anthropic) で minimal prompt を送る。401/403 → AUTH_FAIL、404 → UNSUPPORTED (Ollama `ollama pull` hint 含む)、timeout/5xx → TRANSPORT_ERROR、2xx + parseable → OK。**auth 失敗時は残り 3 probe を SKIP**
  - **Probe 2 `tool_calls`** — fake `echo` tool spec を添えて "Call echo with message=probe" を送る。native `tool_calls` / text-JSON (v0.3-A repair で拾える) / 何も無し の 3 分岐 × 宣言 (providers explicit / registry tools / 両方なし) の組み合わせで OK / NEEDS_TUNING を判定。patch は `providers.yaml capabilities.tools` を `true` / `false` どちらにも flip 可能
  - **Probe 3 `thinking`** — `kind: anthropic` のみ。`thinking: {type: enabled, budget_tokens: 1024}` を送り、response content に `{type: thinking}` block があるかを観測。400 rejection (upstream が field を知らない) も成功シグナルとして検出。openai_compat は SKIP (openai-shape translation で block が失われるため)、ただし `capabilities.thinking=True` の誤設定には SKIP + 警告文で note
  - **Probe 4 `reasoning-leak`** — `kind: openai_compat` のみ。response の `message.reasoning` 非標準 field の有無を観測。存在 + `reasoning_passthrough=false` (default) → 情報提供 OK (v0.5-C strip が働く前提で `capability-degraded` log が出る理由を operator に伝える)。anthropic は SKIP
  - `check_model(config, provider_name, *, registry=None)` async entry / `run_check_model_sync` sync wrapper (CLI から呼ぶ)
  - `format_report(report)` — `[OK]` / `[NEEDS TUNING]` バッジ付き line-oriented 出力、末尾に `Exit: N` 行 (CI grep 用)
  - `_patch_providers_yaml_capability()` / `_patch_model_capabilities_yaml()` — copy-paste YAML 生成ヘルパ。header comment で貼り先ファイル名を明示
- **`coderouter/cli.py`** — `doctor` subcommand 追加 (argparse)
  - `--check-model <provider>` (required) / `--config <path>` (共通)
  - `_run_doctor(args)` — config load + probe 実行 + exit code return。FileNotFoundError / YAML parse error / 不明 provider 名は exit 1 + stderr
- **`tests/test_doctor.py`** (新規 +31)
  - Patch emitters: 3 test (providers.yaml / model-capabilities.yaml それぞれ格納、emitted YAML が valid-yaml で parse 可能)
  - Auth probe: 5 test (401 → AUTH_FAIL + 残り SKIP / 403 同様 / 404 → UNSUPPORTED + model 名 hint / 実 transport error / 2xx+garbage body)
  - Tool-calls probe: 7 test (native + declared / native + silent → patch true / text-JSON + declared false → OK / text-JSON + declared true → NEEDS_TUNING / 何もなし + declared → NEEDS_TUNING false / 何もなし + undeclared / providers.yaml explicit opt-in 優先)
  - Thinking probe: 5 test (openai_compat skip / openai_compat opt-in misconfig warn / anthropic match / anthropic no block but declared / anthropic 400 rejection + declared)
  - Reasoning-leak probe: 3 test (detected → informational OK / absent → OK / anthropic skip)
  - Exit-code: 3 test (all OK = 0 / NEEDS_TUNING alone = 2 / AUTH_FAIL dominates NEEDS_TUNING = 1)
  - Orchestration: 5 test (unknown provider → KeyError with known names / registry kwarg default 経由 / openai Bearer auth / anthropic x-api-key auth / format_report 末尾 "Exit: N")
- **`tests/test_cli.py`** (+6)
  - `doctor` required-arg / load_config への `--check-model` 伝播 / NEEDS_TUNING が exit 2 に伝播 / 不明 provider → exit 1 + stderr に known names / FileNotFoundError → exit 1 / `--config` が load_config に届く

### Design notes

- **なぜ adapter 層を bypass する直接 httpx か.** Reasoning-leak probe は v0.5-C の passive strip が走る前の raw body を見たいし、thinking probe は `kind: anthropic` に Anthropic wire shape を直接送りたい。tool_calls probe も repair pass の前に raw `tool_calls` vs raw text を区別したい。adapter を経由すると観測点が adapter 内部に移動し、test mock が adapter 依存 = brittleになる。probe は「raw POST + raw body 解釈」に閉じた
- **auth short-circuit の理由.** auth 失敗時に残り 3 probe を走らせると、token は消費されないものの操作者にノイズが増える。401 を見た瞬間に「まず env 変数を直せ」と断言でき、tool_calls / thinking の判定は無意味 (そもそも request が通らない)。SKIP 行は残して「何がチェックされてないか」の透明性は保つ
- **exit code の precedence.** blocker (1) > tuning (2) > ok (0)。これは CI 文脈で「1 は人間介入 blocker、2 は自動 PR 可能な mechanical fix、0 は green」という分け。2 を 1 より大きい番号にしたのは従来 Unix 慣例 (lint tools で `--fix` 可能なものが 2、unrecoverable が 1) に合わせたもの
- **probe の読みやすさ vs mock の複雑度.** 各 probe が独立した `POST` を 1 回ずつするシンプル構造にしたため、`httpx_mock.add_response` を probe 順に並べるだけでテストが書ける。alternative としては 1 call で多 probe (batch endpoint) を検討したが、openai_compat と anthropic で endpoint 形状が違う以上 batch 化のメリットが薄く、今の構造が最も直感的
- **patch の target_file 選択.** 単一 provider の問題なら `providers.yaml` を変えるのが最小変更 (glob rule を動かすと同 family の他の provider に波及する)。逆に「model 全 family が registry と異なる」ケースは operator 判断で `model-capabilities.yaml` に patch を書く。doctor は 1 provider しか見ない原則から、suggested_patch は常に `providers.yaml` target にフォールバック。thinking probe の「block emitted but declaration silent」のみ例外 (registry declare が自然な表現なので `model-capabilities.yaml` を suggest)
- **fake `echo` tool の safety.** 名前が `echo` で description に "diagnostic-only" と明記、parameters は `message: string` のみ、副作用性の言及ゼロ。万が一 repair 経由で caller 側に tool_call が届いても、`echo` はホワイトリストされた実ツールには普通マッチしないので silent drop される。probe の非破壊性担保
- **`--network` flag の保留.** plan.md §9.4 でメンション された `--network` flag は static lint mode との分離を想定したものだったが、v0.7-B は `--check-model` 専用で live-probe 前提、`--network` は意味的に自明 (probe = network call)。v0.7-C or v0.8 で static-only lint mode を導入する際に再検討

### Follow-ons

- **v0.7-C で 5 症状を README Troubleshooting に整理** — 各症状に `coderouter doctor --check-model <provider>` 導線を貼る。HF-on-Ollama reference profile の `providers.yaml` stanza + bundled `model-capabilities.yaml` entry も追加
- **num_ctx 境界 probe**: 大 system prompt で silent truncation するかを検出する 5th probe として検討。現状 `max_context_tokens` は registry に declare できるが probe 側では未活用
- **CI smoke script**: GitHub Actions に週次で `coderouter doctor --check-model <each-free-provider>` を回す workflow。exit 2 → auto-PR で providers.yaml patch 適用、exit 1 → issue。v0.5-D OpenRouter roster cron と対称
- **`reasoning` field strip の細粒度化**: 現在 v0.5-C strip は all-or-nothing (`capabilities.reasoning_passthrough` flag)。model ごとに "reasoning tag だけ strip、他の field はそのまま" のような細粒度設計は v1.0+ の reasoning_control 抽象と合流して再検討
- **doctor --json 出力モード**: CI / script 向けに machine-readable 出力。現状は人間向け text のみ。v0.7-C or v0.8 で追加検討

---

## [v0.7-A] — 2026-04-20 (宣言的 `model-capabilities.yaml` registry)

**Theme: 「どの family が thinking を受けるか」を YAML に外出し。** v0.5-A で導入した capability gate の heuristic は Python literal regex (`^claude-sonnet-4-6` など) が `coderouter/routing/capability.py` に焼き込まれていた。Anthropic が新 family を shipping するたびに code change + release cycle が必要で、初心者・中級者にはそもそも存在が見えない隠しレイヤだった。v0.7-A で `model-capabilities.yaml` (bundled default + user override) に宣言を外出しし、新 family 追加 = 1 行 YAML edit にしつつ、将来の `tools` / `reasoning_passthrough` / `max_context_tokens` 宣言のハブに設計。plan.md §9.4 v0.7 scope に対する最初のサブリリース。

- Tests: 306 → **345** (+39、`tests/test_capability_registry.py` 新規、schema validation / glob matching / first-match-per-flag / user override layering / bundled YAML 整合性 / gate function integration)
- 振る舞い変更ゼロ: `provider_supports_thinking` の公開 API・判定結果は v0.5-A と同一 (bundled YAML が旧 regex を 1:1 で encode)
- providers.yaml `capabilities.*` explicit opt-in は最優先のまま (`provider.capabilities.thinking=True` は registry lookup をスキップ)

### Added

- **`coderouter/data/model-capabilities.yaml`** (bundled default、パッケージ同梱)
  - Schema v1: `rules: [{match: glob, kind: "anthropic"|"openai_compat"|"any", capabilities: {thinking, reasoning_passthrough, tools, max_context_tokens}}]`
  - 現行エントリ: `claude-opus-4-*` / `claude-sonnet-4-6*` / `claude-sonnet-4-7*` (forward-compat) / `claude-haiku-4-*` の 4 glob、全て `kind: anthropic` + `thinking: true`
  - comment で「新 family 追加はこの 1 ファイル編集のみ」「user override は `~/.coderouter/model-capabilities.yaml`」と明記
- **`coderouter/data/__init__.py`** — package data を安定に `importlib.resources.files()` 可能にする real-package 化
- **`coderouter/config/capability_registry.py`** (新モジュール)
  - `RegistryCapabilities` / `CapabilityRule` / `CapabilityRegistryFile` Pydantic models (全て `extra="forbid"`、typo で即 ValidationError)
  - `ResolvedCapabilities` frozen dataclass — 4 flag + `None` (= 宣言無し)
  - `CapabilityRegistry.lookup(*, kind, model)` — **first-match-per-flag** semantics: rule を top-down に歩き、flag ごとに「declared 済みの最初の rule」が勝つ (未 declared flag はさらに下の rule にパスする)
  - `CapabilityRegistry.load_default()` / `load_from_paths()` / `from_rule_lists()` loader 3 種 (production / test-isolated / fully-in-memory)
  - user file 不在は `[]` を返して bundled-only で動作 (正常系)、schema error は fail fast
- **`coderouter/routing/capability.py`**
  - `_THINKING_CAPABLE_PATTERNS` / `_THINKING_CAPABLE_RE` / `re` import を削除 (regex 焼き込みの撤去)
  - `get_default_registry()` lazy module-level singleton — 1 process で 1 回だけ disk load
  - `reset_default_registry()` test hook — user YAML を stage したテストが cache を無効化できる
  - `provider_supports_thinking(provider, *, registry=None)` に `registry` kwarg 追加 — DI point。production は default 経由、test はカスタム registry 注入可
  - `__all__` に `CapabilityRegistry` / `ResolvedCapabilities` / `get_default_registry` / `reset_default_registry` を追加 (adapter/engine 層が routing からインポート可能に)
- **`tests/test_capability_registry.py`** (新規 +39)
  - Schema: 7 test (empty YAML OK / top-level typo rejected / rule typo rejected / flag typo rejected / version mismatch rejected / empty match rejected / kind default = "any")
  - Glob matching: 10 param test (`claude-opus-4-*` / `claude-sonnet-4-6*` 境界 / `qwen3-coder:*` / case sensitivity)
  - Lookup semantics: 8 test (no rules → all None / kind filter / first-match-per-flag / flag independence / user > bundled 順序 / unmatched flag = None / `kind: "any"` universal match)
  - Bundled YAML 整合性: 3 test (v0.5-A regex で capable だった model 7 種 × thinking=True / pre-4-6 sonnet → None / openai_compat → None)
  - User override integration: 3 test (load_from_paths 両方読む / missing user OK / malformed user → ValidationError)
  - Gate integration: 8 test (`registry=` kwarg 注入 / providers.yaml explicit > registry / registry 未宣言 → False / `reset_default_registry` で reload / default == fresh load / re-export 確認)

### Design notes

- **なぜ YAML 外出しか.** v0.5-A は「Anthropic release cadence に対する passive drift」を retro で follow-on として挙げていた (docs/retrospectives/v0.5.md §What was sharp)。code change が必要だと release cycle の遅延 = drift が不可視に。YAML ならユーザが bundled を待たず自分で更新可 (`~/.coderouter/model-capabilities.yaml`)、bundled 更新も 1-line PR で済む
- **first-match-per-flag の理由.** 単純な first-match だと「A rule が thinking だけ declare、B rule が同じ glob で tools だけ declare」のケースで B が A を上書きするか無視するかが曖昧になる。per-flag なら「A が thinking=true、B が tools=true、両方適用」が自然に表現できる。YAML 作者は flag ごとに独立した上書き順序を設計できる
- **layered lookup を採らない (plan.md §9.4 policy).** lunacode は `<cwd>/.kairos → <repo>/.kairos → ~/.kairos → bundled` の 4 層だが、CodeRouter の providers.yaml は deployment 時 static config なので per-cwd layer の意味が薄い。bundled + user の 2 層に絞った。将来 `providers.d/*.yaml` merge が要望されたら v0.7-D or v0.8 に分離検討 (現状 YAGNI)
- **per-provider 粒度を維持 (per-model にしない).** 同じ `qwen3-coder:7b` でも Ollama と LMStudio で tool calling の安定度が違うケースがあり、registry lookup の粒度は `(kind, model)` のまま。lunacode は editor harness なので per-model で OK だったが CodeRouter は provider 抽象が前提
- **`kind: "any"` vs `"anthropic"` の使い分け.** 旧 heuristic は `if provider.kind != "anthropic": return False` という hard-check を持っていた。v0.7-A ではこれを「bundled YAML の rule が全部 `kind: anthropic` なので openai_compat query は一致しない」というデータで表現し直した。将来 openai_compat family 向け default (例: `qwen3-coder:*` tools=true) を追加するときは `kind: openai_compat` rule を置けば共存可能
- **`provider.capabilities.thinking=True` の precedence は変わらず最優先.** registry はあくまで「explicit 未宣言時の default」であって、**ユーザが明示的に上書きしたものは上書きしたまま**が unchanged contract。providers.yaml escape hatch は v0.5-A 時点の約束と同じ
- **test-only `reset_default_registry`.** module-level singleton にした代わりに、tests が stage した user YAML を pick up するための hook を置いた。production code は呼ぶ必要なし

### Follow-ons

- **v0.7-B で registry ↔ live probe の diff 機構**: `coderouter doctor --check-model <provider>` が registry 宣言 vs 実機挙動を比較し、乖離を `⚠️ NEEDS TUNING` として emit する。copy-paste YAML patch (`providers.yaml` / `model-capabilities.yaml` どちらにも貼れる形) を出力
- **Registry snapshot の CI**: 週次 `coderouter doctor --check-model` を providers.yaml 全 entry に対して回し、乖離を PR-ready artifact として落とす (v0.5-D の OpenRouter roster cron と対称)
- **v0.7-C で HF-on-Ollama reference profile**: HF distilled model (qwen3.5 / qwen3.6 等) を Ollama 経由で使う用の `model-capabilities.yaml` entry + `providers.yaml` stanza の reference を examples に追加
- **tools / max_context_tokens / reasoning_passthrough の bundled default 追加**: 現在 bundled は thinking のみ。v0.7-B doctor の probe 結果を accumulate して順次 bundled に昇格させる運用を想定 (policy: 「実機検証済の事実のみ bundled に書く」)
- **Capabilities class との合流** (v1.0+): `ProviderConfig.capabilities` は v0.5 retro で「kitchen sink 化」と警告された (10 flag 目前)。v1.0 の `reasoning_control` / `mcp` Literal 抽象と合流する際に registry 側の schema も再整理

---

## [v0.6.0] — 2026-04-20 (umbrella tag for v0.6-A / v0.6-B / v0.6-C / v0.6-D)

**Theme: Chain as a first-class object.** v0.6-A (launch-time profile selection + startup validation), v0.6-B (profile-level parameter override `timeout_s` / `append_system_prompt` + `ProviderCallOverrides`), v0.6-C (宣言的 `ALLOW_PAID` gate + `chain-paid-gate-blocked` 集約 warn), v0.6-D (`mode_aliases` + `X-CodeRouter-Mode` header — intent / implementation 名前空間分離) の 4 サブリリースを一本の tag にまとめる意味合い。**startup fast-fail validator** (4 例) と **typed log payload + chokepoint helper** (v0.6-C = v0.5.1 A-1 パターンの 2 例目) が minor 全体に通底する設計 spine として確立。`_resolve_chain` が 4 engine entry-points を束ねる chokepoint であることが v0.6-C warn 配置で再確認された (v0.4-A の polymorphic chain 化の dividend)。§9.3 の v0.5 未着手分は capability mismatch→chain skip (v1.0+ / vision 同梱) を除いて全消化。

- Commits: v0.6-A → v0.6-B → v0.6-C → v0.6-D (+ 各 sub-release docs commit)
- Tests: 267 → **306** (+39, +15%)
- Narrative & design through-lines: [`docs/retrospectives/v0.6.md`](./docs/retrospectives/v0.6.md)
- Per-sub-release detail: sections `[v0.6-A]` / `[v0.6-B]` / `[v0.6-C]` / `[v0.6-D]` below.
- 5-dep bound 維持 (SDK 非依存、v0.5 で確認した「translation 層は SDK より薄い」賭けが routing / ingress 層にも継続)

---

## [v0.6-D] — 2026-04-20 (`mode_aliases` — `X-CodeRouter-Mode: coding` → profile 名 mapping)

**Theme: 「intent と implementation を名前空間で分ける」。** v0.1 から `profile` (body/header) で chain を選べたが、client 側はいつも「`default` / `fast` / `long-context`」のような**実装寄りの名前**を直接指している状態だった。v0.6-D で `mode_aliases` YAML block と `X-CodeRouter-Mode` header を導入し、client は**意図** (`coding` / `long` / `fast` ...) を送れば済むようにした。profile 名は router 内の実装詳細に格下げされ、裏の chain を付け替えても client には影響しない。§9.3 残 #5 を消化。

- Tests: 291 → **306** (+15、schema 3 / OpenAI ingress 6 / Anthropic ingress 6)
- precedence: body `profile` > `X-CodeRouter-Profile` header > `X-CodeRouter-Mode` header > `default_profile` — Mode は Profile より下 (明示された implementation が最優先)
- 起動時 fast-fail: `mode_aliases` が未知の profile を指していれば `ValidationError` で serve 起動前に落ちる (v0.6-A `default_profile` 検証と同じ philosophy)

### Added

- **`coderouter/config/schemas.py`**
  - `CodeRouterConfig.mode_aliases: dict[str, str]` (`default_factory=dict`) — keys が mode 名、values が profile 名
  - `_check_mode_alias_targets_exist` model validator — 全 alias target が declared profile に存在するか起動時に検証
  - `CodeRouterConfig.resolve_mode(mode) -> str` — alias 引き (見つからなければ `KeyError`、ingress 側で 400 に変換)
- **`coderouter/ingress/openai_routes.py`**
  - 新 header param `x_coderouter_mode: str | None` (`X-CodeRouter-Mode` alias)
  - profile 未決定かつ mode header 有りのとき `config.resolve_mode()` → `mode-alias-resolved` INFO log → `chat_req.profile` に反映。未知 mode は 400 に known modes 列挙付きで返す
  - module docstring を 4-level precedence (body > profile-header > mode-header > default) に更新
- **`coderouter/ingress/anthropic_routes.py`** — 同じパターンを Anthropic route にも適用。`anthropic-version` / `anthropic-beta` 処理の並びの中に自然に組み込み
- **`tests/test_config.py`** (+3) — `resolve_mode` 正常系 + KeyError / 未知 target で `ValidationError` at load / 未宣言なら `mode_aliases == {}` デフォルト
- **`tests/test_ingress_profile.py`** (+6) — mode header → aliased profile / Profile header > Mode header / body profile > Mode header / unknown mode → 400 + known list / `mode_aliases` 空のとき mode header は 400 / 解決結果が engine に届く
- **`tests/test_ingress_anthropic.py`** (+6) — 上記パターンを Anthropic route でも (streaming path も含む)

### Design notes

- **Mode < Profile の理由.** caller が **concrete な profile 名**を送ってきた場合、その caller は router の内部名を知ってて意図的にそれを指定している。そこに Mode を上書きさせると「proxy 経由で mode header が混入したときに profile が無視される」事故が起きる。intent (Mode) は implementation (Profile) で既に specify されていれば負け、という自然な precedence
- **header only — body field は足さない.** profile は body field にもあるが、mode は header だけに留めた。理由は「body は API の契約、header は ops-layer の orchestration」という住み分け。Mode は operator が proxy で注入したい典型例 (例: API gateway が intent を付与する運用) なので header に置くのが筋。body に置くと OpenAI/Anthropic 両方の `*Request` に field を生やす必要があり、scope が肥大化
- **無効 mode → 400 (silent fallback しない).** `mode_aliases` 空 or 未知 mode が来たら fall through で default profile を使う設計もあり得たが、典型的な failure mode は「client/proxy の typo」。silent fallback は「動くけど想定と違う profile に乗ってる」状況を作るので 400 にした。error body に known modes を列挙して self-correctable に
- **起動時検証 (v0.6-A 踏襲).** 実行時に 400 ではなく起動時に `ValidationError` で落ちるのは、`default_profile` 検証と同じ fast-fail 哲学。broken alias が request まで届くと「動くはずの mode が動かない」という間欠的な症状になる
- **`mode-alias-resolved` INFO log の狙い.** mode → profile の解決は client には見えない操作なので、「何が何に解決されたか」を 1 行残す。operator が「coding mode で呼んだ request が fast profile に乗ってる」といった診断を grep でできる

### Follow-ons

- **v0.7+**: `mode_aliases` の階層化 (例: `coding.fast` / `coding.thorough` みたいな dotted name) を考えるかどうか。現時点ではフラット dict で十分 (使う側も 3〜5 種類に収束するはず) なので over-engineering は回避
- **examples/providers.yaml**: 今回は `mode_aliases` block のサンプルは未追加 (実 YAML を壊さずに追加する判別が要る)。v0.6-D docs pass の中で低リスクに足すか、v1.0 近辺の example overhaul でまとめて整理するか要判断

---

## [v0.6-C] — 2026-04-20 (宣言的 `ALLOW_PAID` gate + `chain-paid-gate-blocked` 集約 warn)

**Theme: 「宣言された gate」を chain-granularity の 1 行に昇格。** v0.1 から既に `paid: true` provider は `ALLOW_PAID=false` のとき filter されていたが、per-provider INFO (`skip-paid-provider`) だけで、「chain 全体が paid gate で空になった」ケースは `NoProvidersAvailableError` に埋もれていた。v0.6-C で**集約 warn** (`chain-paid-gate-blocked`) を追加し、gate が chain を empty にした瞬間に hint 付きで 1 行出る。v0.5 capability gate の `capability-degraded` と同じ「typed payload + chokepoint helper + logging.py 居住」パターンを踏襲。

- Tests: 283 → **291** (+8, `tests/test_fallback_paid_gate.py` 新規)
- 振る舞い変更ゼロ: 既存 `NoProvidersAvailableError` の例外 shape は非破壊、`skip-paid-provider` INFO は per-provider レベルで温存
- 4 entry point (generate / stream / generate_anthropic / stream_anthropic) 全てで発火 — `_resolve_chain` に一本化したので共通経路 1 箇所の変更で済んだ

### Added

- **`coderouter/logging.py`**
  - `ChainPaidGateBlockedPayload` TypedDict — `profile` / `blocked_providers: list[str]` / `hint: str` の 3 field
  - `log_chain_paid_gate_blocked(logger, *, profile, blocked_providers, hint=...)` chokepoint helper (warn level)
  - `_DEFAULT_PAID_GATE_HINT` — `"set ALLOW_PAID=true, mark a provider paid=false, or add a free provider to this profile's chain"` のデフォルト文言 (grep-friendly, 必要なら call site で差し替え可)
- **`coderouter/routing/fallback.py`**
  - `_resolve_chain` が paid-blocked provider 名を `blocked_by_paid` リストで収集。chain 解決後に `adapters == [] and blocked_by_paid` なら `log_chain_paid_gate_blocked` を発火
  - `from coderouter.logging import get_logger, log_chain_paid_gate_blocked`
- **`tests/test_fallback_paid_gate.py`** (新規 +8) — 全 paid chain で warn 発火 / multi-paid で blocked_providers が chain 順 / mixed chain では warn しない (free が生き残るから) / ALLOW_PAID=true では skip-paid も warn も無し / unknown-only chain では warn しない / streaming / generate_anthropic / stream_anthropic 各 path で warn 発火を確認

### Design notes

- **集約 warn が必要だった理由.** `skip-paid-provider` は per-provider INFO なので、chain=[paid-A, paid-B, paid-C] のとき 3 行吐かれる。operator が「これが chain 全体を空にした原因か？」を判断するには `skip-paid-provider` → `NoProvidersAvailableError` の時系列を grep で組み直す必要があった。v0.6-C はこれを「1 行で宣言的に」示す
- **warn vs info の選択.** v0.5 の `capability-degraded` は info (gate は常時動いてる normal path)。v0.6-C は warn (chain が empty = 設定ミスの疑いが濃厚、operator の目線を奪う価値がある)。`skip-paid-provider` 側は info のまま (chain は生き延びる余地がある瞬間もある)
- **`logging.py` 居住の継続.** `routing/capability.py` ではなく `logging.py` に helper を置く方針 (v0.5.1 A-1) を踏襲。理由も同じ: `routing/__init__.py` が eager import で `FallbackEngine` を引くので、adapter 側から paid-gate warn を撃ちたい将来の拡張に対して循環を避けておく
- **mixed chain で warn しない理由.** paid-blocked な provider が居ても、free な provider が 1 つでも生きていれば chain は exercise される。その場合の「全 free が失敗した」診断は `provider-failed` のレーンが既に narrate してくれるので、warn が被るだけ。v0.5.1 A-3 (`chain-uniform-auth-failure`) と同じ「aggregate は empty/uniform の時だけ吠える」ルール
- **`retry_max` / startup enumeration は scope 外.** §9.3 #3 には「起動時に paid provider を列挙」という旨も含意されていたが、現 v0.6-A で `coderouter-startup` log に全 provider 情報が既に出ているので別口で足すよりコストが高い。今は chain 時 warn を優先

### Follow-ons

- **v0.6-D**: `mode_aliases` YAML block で `X-CodeRouter-Mode: coding` → profile 名の mapping (§9.3 残 #5)
- **v0.6+**: `chain-paid-gate-blocked` の hint 文言を profile 単位で override できると便利 (例: `claude-code-direct` profile なら "set ANTHROPIC_API_KEY and ALLOW_PAID=true" みたいな文脈追加)。現状は helper の `hint=` で call site 上書きが可能

---

## [v0.6-B] — 2026-04-20 (profile-level `timeout_s` / `append_system_prompt` override)

**Theme: profile を「providers の並び + 制御パラメータ」に昇格。** v0.6-A で profile 選択そのものは CLI/env で差し替えられるようになったが、profile ごとに「こっちは local の低レイテンシー想定だから timeout は短く」「あっちの /no_think 付与は fast-profile だけ」といった**制御パラメータ差分は provider-level にしか存在しなかった**。v0.6-B で `FallbackChain` に optional `timeout_s` / `append_system_prompt` を足し、engine が profile 解決時に一度だけ `ProviderCallOverrides` を組み立てて chain 全体に配る。

- Tests: 275 → **283** (+8、fallback engine 5 / openai_compat adapter 3)
- 優先順位: profile 値 (設定あれば) → provider 値 → 既定値 — **置き換え** (append ではなく replace) セマンティクス。混乱を避けるため `timeout_s` と同じ挙動に揃えた
- `retry_max` は adapter 層に既存の retry 機構が無いため scope 外 (§9.3 #4 の partial)

### Added

- **`coderouter/config/schemas.py`**
  - `FallbackChain.timeout_s: float | None` (`ge=1.0, le=600.0`) — `ProviderConfig.timeout_s` と同じ範囲制約
  - `FallbackChain.append_system_prompt: str | None` — profile 側で "" を明示すれば provider 側の directive を「この profile に限り無効化」できる特別セマンティクス
- **`coderouter/adapters/base.py`**
  - `ProviderCallOverrides` pydantic モデル (`extra="forbid"`, 全 field optional)。engine が profile 単位で 1 回組み立て、同 chain の全 adapter 呼び出しに配る
  - `BaseAdapter.effective_timeout(overrides)` / `effective_append_system_prompt(overrides)` — override > provider default を決定する共通 helper
  - `generate` / `stream` 抽象に `overrides: ProviderCallOverrides | None = None` kwarg を追加 (keyword-only、default None で backward-compat)
- **`coderouter/adapters/openai_compat.py`** — `_prepare_messages` / `_payload` / `generate` / `stream` が `overrides` を受け取り、`httpx.AsyncClient(timeout=...)` と system message 注入の双方に反映
- **`coderouter/adapters/anthropic_native.py`** — `generate_anthropic` / `stream_anthropic` / (reverse) `generate` / `stream` が `overrides` を受け取り、native passthrough 経路の httpx timeout に反映 (`append_system_prompt` は anthropic_native では元々非対応なので timeout のみ)
- **`coderouter/routing/fallback.py`**
  - `_resolve_profile_overrides(profile_name)` helper — profile から `ProviderCallOverrides` を 1 回だけ組み立て
  - `generate` / `stream` / `generate_anthropic` / `stream_anthropic` の 4 entry point すべてで解決 → adapter 呼び出しに `overrides=` を渡す
- **`tests/test_fallback.py`** (+5) — timeout override が adapter に届く / unset 時は provider 値に落ちる / append_system_prompt の置き換え / `""` で clear する / FallbackChain schema sanity
- **`tests/test_openai_compat.py`** (+3) — `ProviderCallOverrides(append_system_prompt="/x")` が outbound に出る / `""` で system 注入がスキップされる / `ProviderCallOverrides()` は `None` と観測的に等価 (回帰ガード)

### Changed

- **`tests/test_fallback.py` / `tests/test_fallback_anthropic.py`** — fake adapter 群の `generate` / `stream` / `generate_anthropic` / `stream_anthropic` 署名に `overrides` kwarg を追加。engine が常に `overrides=` を渡すようになったため、kwarg を受け取れない fake は `TypeError` で落ちる

### Design notes

- **置き換え vs 追加.** `append_system_prompt` は「文字列なので追加が自然」「いや provider と profile で二重に刺さると混乱する」の両論あったが、`timeout_s` がスカラー制約で「置き換え」しかありえない以上、同じ field family に属する `append_system_prompt` も置き換えに揃えた方が意味論がシンプル。profile 側で両方刺したいユースケースが出てきたら v0.6+ で `append_mode: "replace" | "concat"` を別フィールドとして足せる
- **"" で clear する非対称性.** pydantic の field で `None` と `""` は区別できるので、profile が「この profile だけ provider directive を無効化したい」をちゃんと表現できるように、`effective_append_system_prompt` 内で `overrides.append_system_prompt == ""` → `None` を返す特別扱いを入れた。`None` = "override 無し" と意味が被らないよう、helper のコメントで明示
- **override resolution の位置.** engine が「chain ごとに 1 回 override を組み立てて全 adapter 呼び出しに配る」方式を採った。adapter 側で per-call lookup すると (a) config 依存が adapter まで広がる、(b) profile 名を adapter に渡すことになる、の両方を避けられる。profile は immutable per-request なので 1 回解決で十分
- **abstract signature 破壊.** `BaseAdapter.generate` に kwarg を追加したため、既存の fake adapter (tests) は署名を合わせる必要あり。ただし (i) default が `None`、(ii) keyword-only、の 2 条件を満たすので **本体 adapter を実装するサードパーティ (まだ存在しないが) は影響ゼロ**。tests でしか気付かない破壊
- **retry_max の scope 外.** §9.3 #4 は本来 `retry_max` も含んでいたが、adapter 層に現時点で「1 provider 内での retry」概念が無い (retry 相当は fallback chain そのもの)。この mechanism を先に入れると「provider 内 retry → それでも駄目なら fallback」の挙動分岐が発生し、midstream guard との相互作用が非自明になる。v0.6-D 以降で設計込みで再検討

### Follow-ons

- **v0.6-C**: `ALLOW_PAID` 宣言的 gate の強化 — startup log に paid provider 列挙 + `chain-paid-gate-blocked` structured log (§9.3 残 #3)
- **v0.6-D**: `mode_aliases` YAML block で `X-CodeRouter-Mode: coding` → profile 名の mapping (§9.3 残 #5)
- **後日**: `retry_max` (profile + provider の階層) を含む adapter-level retry 機構。midstream guard との整合性が設計のキモ

---

## [v0.6-A] — 2026-04-20 (`--mode` CLI + CODEROUTER_MODE env + startup validation)

**Theme: サーバー起動時点の profile 選択を 1 級市民に昇格。** v0.5 までは「YAML の `default_profile` を書き換える」か「クライアントごとに header を毎回投げる」の二択だった。v0.6-A で `--mode <profile>` CLI オプション + `CODEROUTER_MODE` 環境変数を追加し、サーバー単位 / プロセス単位の軽い override を可能に。併せて `default_profile` が profiles リストに存在しない場合は起動時に fast-fail するように (従来は最初のリクエスト時に 500)。

- Tests: 267 → **275** (+8, CLI 5 + config loader 3)
- 優先順位: request per-call > `--mode` (= `CODEROUTER_MODE`) > YAML `default_profile` > built-in "default"
- §9.3 の 5 項目中 2 項目 (`--mode` CLI / 起動時 fast-fail) を消化

### Added

- **`coderouter/cli.py`**
  - `serve --mode <profile>` 引数。指定値の前後 whitespace を strip してから `CODEROUTER_MODE` env var を export (shell quoting 事故で `" coding "` が渡ってきても loader まで届かない)
  - 既存 `CODEROUTER_MODE` が shell に pre-set されている場合、`--mode` が未指定なら尊重、指定されていれば上書き
- **`coderouter/config/schemas.py`**
  - `CodeRouterConfig` に `@model_validator(mode="after")` で `default_profile` が `profiles` に存在するかチェック。従来は `profile_by_name` lookup 時 (= 最初のリクエスト) まで typo が検出されなかった
- **`coderouter/config/loader.py`**
  - `CODEROUTER_MODE` env var (空白 strip 後に truthy なら) を `raw["default_profile"]` に被せてから pydantic validate。model-validator の存在チェックが「effective mode」に対して走る
- **`coderouter/ingress/app.py`**
  - 起動 log `coderouter-startup` に `default_profile` + `mode_source: "env" | "config"` を追加。operator が「shell が driver してる」か「YAML で決まってる」かを 1 行で把握可能
- **`tests/test_cli.py`** (新規) — `--mode` → env, `--mode` vs pre-set env, whitespace strip, `--mode` 未指定時は env を触らない、既存 `--config` の回帰テスト (+5)
- **`tests/test_config.py`** — `CODEROUTER_MODE` env override, 空文字列は ignore, YAML 側で default_profile が不正な場合の fast-fail (+3)

### Changed

- **`tests/conftest.py`** — `_clear_env` fixture に `CODEROUTER_MODE` を追加。テスト間で env が漏れるのを防ぐ既存パターンに合わせた
- **`README.md`** — Claude Code セクションに `--mode` 例を追加。YAML 側の `default_profile:` を書き換える方法と併記

### Design notes

- **なぜ env 一本化?** `--mode` は単に `CODEROUTER_MODE` を export する薄いラッパにした。`uvicorn --reload` が fork で worker を立ち上げる関係で、引数を直接渡すには factory 関数への引数付け足しが必要で、既存の `--config` と同じ env 経由パターンに揃えた方が自然。worker 側は `os.environ.get("CODEROUTER_MODE")` 1 発で拾える
- **loader で env を raw に被せる順序.** `CodeRouterConfig.model_validate(raw)` の *前* に env override を適用している。これで model-validator の `default_profile exists` チェックが (a) YAML の値に対してではなく (b) 実際に使われる値に対して走る。結果として「YAML は古い profile 名を残してるが env で新しい名前を指してる」というケースが正しく通り、逆に env で typo を打つと起動時に即エラー
- **空文字列の扱い.** `CODEROUTER_MODE=""` または `CODEROUTER_MODE="   "` は「未設定」と同義に扱う (strip 後 empty なら override しない)。shell で `export FOO=` が「clear」のセマンティクスを持つのに合わせた
- **fast-fail の境界.** 未知 profile の検出は起動時 + loader 呼び出し時のみ。runtime (リクエスト中) に profile が "消える" ことはないので、毎リクエスト検証するオーバーヘッドは不要

### Follow-ons

- **v0.6-B**: profile-level `timeout_s` / `append_system_prompt` / `retry_max` override (§9.3 残 #4)
- **v0.6-C**: `ALLOW_PAID` 宣言的 gate の強化 — startup log に paid provider 列挙 + `chain-paid-gate-blocked` structured log (§9.3 残 #3)
- **v0.6-D**: `mode_aliases` YAML block で `X-CodeRouter-Mode: coding` → profile 名の mapping (§9.3 残 #5)

---

## [v0.5-D] — 2026-04-20 (OpenRouter roster weekly cron)

**Theme: proactive free-tier 棚卸の自動化。** v0.4-B で「消えた後でしか気付けなかった `deepseek-r1:free`」が動機。`scripts/openrouter_roster_diff.py` が `httpx + stdlib` だけで `/api/v1/models` を週次でポーリングし、free-tier (`pricing.prompt` と `pricing.completion` がどちらも数値 0) の差分を `docs/openrouter-roster/CHANGES.md` に newest-first で追記する。`coderouter` パッケージへの import は 0 — cron は本体が mid-change でも安全に動く。

- Tests: 243 → **267** (+24)
- Runbook + 設計メモ: [`docs/openrouter-roster/README.md`](./docs/openrouter-roster/README.md)

### Added

- **`scripts/openrouter_roster_diff.py`** — 単一ファイル cron。
  - `parse_models(raw) -> list[RosterEntry]` — OpenRouter レスポンスから id / context_length / pricing を抽出、malformed row は silent skip
  - `is_free(entry)` — `pricing.prompt` と `pricing.completion` の双方が数値 0 に parse できた時のみ True。`:free` suffix は見ない (pricing が authoritative、suffix は hint)
  - `diff_rosters(old, new) -> RosterDiff` — Added / Removed / pricing_changed / context_changed の 4 カテゴリ、id ソート出力
  - `format_markdown(diff, *, fetched_at) -> str` — Removed を先頭に (`⚠️` 付き) 並べた markdown セクション
  - `prepend_changes(path, section)` — 既存 CHANGES.md の先頭に追記 (newest-first)、atomic tmp+replace
  - `run(...)` — 1st invocation は snapshot を書くが CHANGES.md には書かない (baseline noise を避ける)。2nd 以降が実トラッキング
  - `main(argv)` — `--dry-run` / `--url` / `--snapshot` / `--changes`。exit 0 成功 / exit 2 HTTP エラー
- **`tests/test_openrouter_roster_diff.py`** — 24 tests, 3 層構成。
  - Tier 1 (8): `parse_models` / `is_free` / `filter_free` の pure logic
  - Tier 2 (8): `diff_rosters` / `format_markdown` の pure diff
  - Tier 3 (8): `run()` orchestration — `httpx_mock` で `/api/v1/models` を差し替え、first-run baseline / 2nd-run Removal 検出 / dry-run no-write / paid 除外 / 無変更 no-op / newest-first prepend / exit code 0/2 の end-to-end
- **`docs/openrouter-roster/README.md`** — runbook (manual / scheduled の両モード)、triage cheatsheet、"free" の定義 (pricing-based)、future extension 候補 (streaming capability flag / rate-limit band)

### Design notes

- **なぜ `coderouter` パッケージに置かずに `scripts/` + 独立 import?** v0.4-B の教訓で「roster 棚卸は本体の健全性に依存しない方が良い (本体が壊れている時こそ棚卸したい)」。`stdlib + httpx` だけなら、pre-merge の branch でも、production 凍結中でも、どこからでも 1 コマンドで走る
- **pricing が authoritative、`:free` suffix は hint.** OpenRouter は時期によって `:free` suffix 付きで nonzero completion 価格を出すことがある (v0.4-B 棚卸期間中に観測)。`test_is_free_does_not_require_free_suffix` で invariant を pin
- **first-run baseline は silent.** 初回に "Added: 100 models" を書くと log の signal が劣化するので、snapshot だけ書いて CHANGES.md は触らない。トラッキングは 2nd run から
- **prepend (newest-first) を採用.** `git log -p CHANGES.md` 派の他に `head CHANGES.md` 派もいるので、time 順で直感的な newest-first に寄せた。append だと「最新が末尾」になり head では見えない
- **週次 cadence 想定。** OpenRouter の roster が日次で変わるほど激しくないのと、週次なら PR レビュー負荷も年間 52 件で許容範囲。schedule skill で登録する場合は平日朝 JST を推奨 (README runbook 参照)

### Follow-ons (v0.5-D を起点)

- 週次 cron の `schedule` skill 登録 — 手動で README runbook に従うか、skill で週次タスク化するかは運用判断。v0.5-D では script + docs + tests の地盤整備まで
- 初回 `latest.json` baseline のコミット — v0.5-D 本体では作らず (real `OPENROUTER_API_KEY` は roster GET に不要だが、実データを v0.5.x 内に混ぜたくなかった)。次回マニュアル実行時に自然に生える
- Streaming capability flag のトラッキング (README §Future extensions) — v0.5 期間中に観測した `gpt-oss-120b:free` の SSE 挙動変化を説明しうる候補。実装コストは data 追加 1 列

---

## [v0.5.1] — 2026-04-20 (closeout pack)

**Theme: v0.5 retrospective Follow-ons を 3 本束ねて締める。** v0.5-verify の real-machine run から出てきた 3 件の小 Follow-on (payload 型付け / streaming verify / 401 uniform 警告) を closeout pack として一括投入。コア挙動の変更はゼロ (ログ shape の型付け + 観測ツール + 診断ログの追加のみ)、`NoProvidersAvailableError` などの public surface は非破壊。

- Tests: 225 → **243** (+18)
- Per-item narrative below: `[v0.5.1-A1]` / `[v0.5.1-A2]` / `[v0.5.1-A3]`

### Added

- **`coderouter/logging.py`** (A-1)
  - `CapabilityDegradedReason = Literal["provider-does-not-support", "translation-lossy", "non-standard-field"]` — v0.5 gate trio の 3 reason を型として凍結
  - `CapabilityDegradedPayload(TypedDict)` — `provider` / `dropped` / `reason` の構造契約
  - `log_capability_degraded(logger, *, provider, dropped, reason)` — 全 gate が通る single chokepoint。キーワード限定引数で TypedDict contract を static-type で enforce
- **`coderouter/routing/fallback.py`** (A-3)
  - `_AUTH_STATUS_CODES: Final[frozenset[int]] = frozenset({401, 403})`
  - `_warn_if_uniform_auth_failure(errors, *, profile)` — chain の全 attempt が同 auth status + 全て non-retryable の時だけ `chain-uniform-auth-failure` warn を吐く。`profile` / `status` / `count` / `providers` / `hint: "probable-misconfig"` を extra に持つ
- **`scripts/verify_v0_5.sh`** (A-2)
  - `run_scenario_streaming()` — `curl -N` + SSE 解析で streaming scenario を実行、HTTP 2xx / `capability-degraded` 1 発ちょうど / 全 chunk で `delta.<field>` 不在 の 3 assertion を自動化
  - `D-reasoning-stream` シナリオ追加 — v0.5-C の "log once per stream" dedup 契約の real-machine 確認
- **`tests/test_capability_degraded_payload.py`** — Literal 列挙 / TypedDict required_keys / helper emit shape / 3 reason parametrized smoke / logger 名保持 / 払い出し独立性 (+9 tests)
- **`tests/test_fallback_misconfig_warn.py`** — 1-provider 401 発火 / 403 同扱い / 400 非発火 / retryable 非発火 / mixed status 非発火 / 空 chain 非発火 / streaming path 発火 (+9 tests)

### Changed

- **`coderouter/adapters/openai_compat.py`** (A-1) — `log_capability_degraded` を `coderouter.logging` から直接 import。`coderouter.routing.capability` 経由にすると `routing/__init__.py` が `FallbackEngine` → `adapters/registry` → `openai_compat` を再帰的に呼ぶ import cycle が発生するため、leaf の `logging.py` に helper を置いて回避。`generate()` / `stream()` の reasoning strip ログは unified helper 経由に
- **`coderouter/routing/capability.py`** (A-1) — `CapabilityDegradedReason` / `CapabilityDegradedPayload` / `log_capability_degraded` を `coderouter.logging` から re-export。semantic ownership (capability gate のログ) はこのモジュールが持ちつつ、実体の置き場所は cycle 安全な leaf に委譲
- **`coderouter/routing/fallback.py`** (A-3) — 4 raise site (generate / stream / generate_anthropic / stream_anthropic) の直前で `_warn_if_uniform_auth_failure(errors, profile=profile)` を呼ぶ。例外 shape は非破壊

### Design notes

- **logging.py を選んだ理由 (A-1)**. `CapabilityDegraded*` の semantic な置き場は `routing/capability.py` だが、実体をそちらに置くと `adapters/openai_compat.py` が import した瞬間 `routing/__init__.py` が eager に走り `FallbackEngine` → `adapters/registry` → `openai_compat` という cycle を踏む (Python の package init の仕様)。`logging.py` は dependency 無しの leaf なので、そこに型 + helper を置いて capability.py から re-export する形で「ソースは leaf / 概念上の所有は routing」を両立。両モジュールの docstring に why を明示
- **401/403 限定スコープ (A-3)**. 400 "model not found" のような非 retryable error も chain 全滅しうるが、これは env-var 問題ではなく provider-model mismatch。`probable-misconfig` という同じ hint で括ると操作者に誤誘導になるので auth scope に絞った。非 retryable 全般への拡張は future decision
- **`chain-uniform-auth-failure` は warn であって raise ではない (A-3)**. `NoProvidersAvailableError` の例外 shape を維持しないと既存 ingress / tests が壊れるので、追加情報は **ログレーンに並走** させるのみ。1 行の grep で拾える位置 (既存 `provider-failed` トレイルの直後) に出る

### Follow-ons unchanged

- v0.5-D: OpenRouter roster 週次 cron diff (retro §Follow-ons) — v0.5.1 では未着手、次候補
- 当初 v0.5 スコープの本丸 (`profiles.yaml` / `--mode` CLI / 宣言的 ALLOW_PAID / timeout-retry) — v0.6-A に送り継続

---

## [v0.5.0] — 2026-04-20 (umbrella tag for v0.5-A / v0.5-B / v0.5-C)

**Theme: Capability gate trio.** v0.5-A (thinking, request-side strip + chain reorder), v0.5-B (cache_control, observability-only), v0.5-C (OpenRouter `reasoning` field, response-side strip) の 3 サブリリースを一本の tag にまとめる意味合い。gate の共通設計 (unified `capability-degraded` ログ名 / varying `reason` / YAML escape hatch first / SDK 非依存) が 3 ピース通じて確立した。

- Commits: `ff7ca27` (v0.5-A) → `e8803da` (v0.5-B) → `e20fb36` (v0.5-C)
- Tests: 153 → **225** (+72, +47%)
- Narrative & design matrix: [`docs/retrospectives/v0.5.md`](./docs/retrospectives/v0.5.md)
- Per-sub-release detail: sections `[v0.5-A]` / `[v0.5-B]` / `[v0.5-C]` below.

---

## [v0.5-C] — 2026-04-20

### OpenRouter `reasoning` field passive strip

v0.4-B の棚卸で実機検出した非標準フィールドの適正処理。OpenRouter の一部
free-tier モデル (実機確認: `openai/gpt-oss-120b:free` 2026-04-20) が OpenAI
Chat Completions spec 非準拠の `reasoning` フィールドを response choice の
`message` / `delta` に同梱してくる。

Spec 外の key なので strict downstream (openai SDK の一部 typed class, 厳格な
validator) が TypeError を出す可能性があり、v0.4 retro §Follow-ons で「passive
strip + log を将来入れる」と括って保留していた。v0.5-C で adapter 層の出口に
1 枚噛ませて解決する。

#### Added

- **`coderouter/config/schemas.py`**
  - `Capabilities.reasoning_passthrough: bool = False` — opt-out flag。
    `true` なら strip もログも skip (CodeRouter を reasoning-aware な
    downstream に中継する時の escape hatch)
- **`coderouter/adapters/openai_compat.py`**
  - `_strip_reasoning_field(choices, *, delta_key)` — 純粋関数。
    `choices[*].message.reasoning` (non-stream) / `choices[*].delta.reasoning`
    (stream) を in-place で除去。戻り値は「1 件でも除去したか」の bool
    (one-shot ログ判定用)。None / 空 list / 非 dict choice は defensive
    にスキップ

#### Changed

- **`coderouter/adapters/openai_compat.py`**
  - `generate()`: response JSON decode 直後、`ChatResponse` 構築前に
    `_strip_reasoning_field(..., delta_key=False)` を適用。strip が発生
    したら構造化ログ `capability-degraded` (`provider` / `dropped:
    ["reasoning"]` / `reason: "non-standard-field"`)
  - `stream()`: 各 chunk を yield する直前に同じ strip を適用。log は
    stream 中 **1 回だけ** (local `reasoning_logged` flag) で chunk ごとの
    連投を防ぐ。長い reasoning track でログが溢れない
  - v0.5-A (`provider-does-not-support`) / v0.5-B (`translation-lossy`) と
    同じ `capability-degraded` メッセージ名 + `reason` 識別で grep しやすい

#### Tests

- **+15 件** (合計 **225 件 green**, 210 → 225)
  - `test_reasoning_strip.py` (新規):
    - unit: `_strip_reasoning_field` の message / delta 剥離, no-op 挙動
      (field 欠落 / None / empty list / 非 dict choice / wrong delta_key),
      multi-choice
    - non-streaming: strip + `capability-degraded` ログ発火 / reasoning
      欠落時は無発火 / `reasoning_passthrough: true` で保持 + 無発火 /
      content 非破壊
    - streaming: 全 delta から strip + ログは 1 回のみ / 欠落時は無発火 /
      passthrough で保持 + 無発火 / `delta.content` 非破壊

#### Notes

- **既存挙動への影響ゼロ**: `reasoning` を元々出さない provider (llama.cpp /
  Ollama / OpenRouter の従来モデル / Anthropic 経由) は strip 判定が偽で
  終わるため、payload も log も変わらない
- **native anthropic adapter** は対象外。Anthropic wire の response には
  `reasoning` に相当するフィールドが存在しないため、gate は OpenAI-shape の
  adapter だけで完結する
- **実機 verify**: v0.4-B の棚卸で `openai/gpt-oss-120b:free` が返す生
  response を確認済み (retro §3.2 参照)。v0.5-C はその再現を httpx_mock で
  テストに落としているので、今後 OpenRouter 側が同じ挙動を続けても継続的に
  担保される
- **運用上の使い方**: `reason: "non-standard-field"` で grep すると「どの
  provider が非標準キーを送ってきたか」が構造化ログから一括で取れる。新
  モデルが追加されて reasoning 以外の key が出始めたら同じ関数を拡張する
  想定 (今のところは reasoning だけなのでシンプルに)

---

## [v0.5-B] — 2026-04-20

### cache_control observability

v0.5-A (thinking) に続く capability gate の 2 ピース目。thinking が「未対応
model に投げると 400」というハードエラーだったのに対して、cache_control は
もっと性質が違う — Anthropic → OpenAI translation の段階で **silent に落ちる**
(content block 上のマーカーに OpenAI wire 側の等価物がない)。エラーにならず、
上流の Anthropic prompt cache の課金最適化が単に無効化されるだけ。

v0.5-B はこの非対称性を踏まえて **observability-only** (no chain reorder /
no strip) で着地させる:

- cache_control 付きリクエストが openai_compat provider に渡る際、構造化ログ
  `capability-degraded` (`reason: "translation-lossy"`) を出す
- chain 順序は **変えない** — ユーザーの provider 順序は latency / cost の意
  図を反映しており、cache-hit の節約でそれを上書きしない方針
- strip もしない — `to_chat_request` の既存 translation が自動で marker を落
  とすので router 側で追加処理する必要がない

#### Added

- **`coderouter/routing/capability.py`** — 関数 2 つ + helper 1 つ:
  - `provider_supports_cache_control(provider)` — `kind: anthropic` は常に
    True (native passthrough で end-to-end 保持)、`kind: openai_compat` は
    デフォルト False (wire 等価物なし)。`capabilities.prompt_cache: true` を
    YAML で明示すると openai_compat でも True に昇格 (escape hatch: 将来
    OpenAI wire を拡張した upstream が出た場合用)
  - `anthropic_request_has_cache_control(request)` — `system` (list 形式)
    `tools[*]` (Pydantic extras 経由) `messages[*].content` (list 形式) を
    再帰的に walk し、`cache_control` key を持つブロックが 1 つでもあれば
    True
  - `_block_has_cache_control(block)` — 内部 helper (dict 判定 + key 存在判定)

#### Changed

- **`coderouter/routing/fallback.py`** — `generate_anthropic` / `stream_anthropic`
  の両方で:
  - ループ内の provider ごとに `anthropic_request_has_cache_control(request)`
    かつ `not provider_supports_cache_control(adapter.config)` なら
    `capability-degraded` ログ (`provider` / `dropped: ["cache_control"]` /
    `reason: "translation-lossy"`) を発火
  - v0.5-A の thinking gate (`provider-does-not-support`) とは `reason` が
    違うので運用側で絞り込み可能
  - `_resolve_anthropic_chain` は **変更なし** — cache_control では reorder
    しない。同じメソッド内で 2 種類のログが出るようになっただけ

#### Tests

- **+21 件** (合計 **210 件 green**, 189 → 210)
  - `test_capability.py` +13:
    - `provider_supports_cache_control`: anthropic デフォルト True, openai_compat
      デフォルト False, `prompt_cache: true` で openai_compat を昇格, anthropic
      に prompt_cache: true は redundant だが壊れない
    - `anthropic_request_has_cache_control`: plain request / bare-string system /
      system block with marker / system block without marker / tool-level marker /
      message content block marker / string-form content は常に False / 2nd
      message でも検出 / image block 上の marker (type 非依存)
  - `test_fallback_cache_control.py` (新規) +8:
    - openai_compat + cache_control → log 発火 (reason=translation-lossy, dropped=["cache_control"])
    - anthropic kind + cache_control → log 発火しない
    - plain request + openai_compat → log 発火しない
    - **chain 順序が reorder されない** (v0.5-A との重要な差分テスト)
    - `prompt_cache: true` escape hatch でログ抑制
    - fallback chain で複数の openai_compat を踏む場合、1 provider ごとにログ発火
    - streaming path の mirror (openai_compat 発火 / anthropic 発火しない)

#### Notes

- **Anthropic prompt cache の 1024-token 下限**: v0.4 retrospective §What was
  sharp で既出の footgun。system prompt が 1024 token 未満だと、supported
  provider でも Anthropic 側が `cached_tokens: 0` を返す。v0.5-B の gate は
  この Anthropic 側の制約には関知しない (そもそもマーカーを保持することだけを
  扱う層) — なので「小さい prompt でキャッシュヒットが 0 なのは CodeRouter の
  バグ」という誤解を招かないよう docstring にコメント済み
- **実機 verify**: v0.4-D retro で「1321 tokens written on call 1, 1321 read
  on call 2」を実機で確認済み (native anthropic 経由)。v0.5-B は routing 側の
  gate なので、translation layer の既存挙動 + 新規ログのみが差分
- **運用上の使い方**: `reason: "translation-lossy"` で grep すると「ユーザー
  は cache 意図を送ったが本リクエストは openai_compat に流れた」イベントが
  全部拾える。頻度が高ければ YAML 側で anthropic-direct を上に挙げるか、
  openai_compat 側に `prompt_cache: true` を立てる判断材料になる

---

## [v0.5-A] — 2026-04-20

### thinking capability gate

v0.4-D retrospective で follow-on に挙げた「capability gate」の最初のピース。
Anthropic の `thinking: {type: "enabled"}` を対応モデルだけにルーティングし、
未対応モデルには silent strip + 構造化ログで degrade する。

背景: v0.4-D 実機テストで `claude-sonnet-4-5-20250929` が adaptive thinking
リクエストに 400 を返す問題にぶつかり、`claude-sonnet-4-6` に差し替えて回避し
た。ユーザーのモデル選択が「正当性に影響する決定」になっていた状態を、v0.5-A
で「純粋に経済性の決定」に降格させる。

#### Added

- **`coderouter/routing/capability.py`** (新規) — 純粋関数 3 つ:
  - `provider_supports_thinking(provider)` — YAML flag 優先、未指定なら
    model 名 heuristic (`^claude-(opus|sonnet|haiku)-4-(6|7)`, `claude-opus-4-`,
    `claude-haiku-4-` にマッチすれば capable)。`kind: openai_compat` は
    model 名にかかわらず常に incapable (OpenAI wire に thinking field なし)
  - `anthropic_request_requires_thinking(request)` — `model_extra["thinking"]`
    が `{"type": "enabled"}` かどうかを判定。disabled / 欠落 / 非 dict は False
  - `strip_thinking(request)` — extras から `thinking` を除いた複製を返す
    (mutation-free)。`profile` / `anthropic_beta` (exclude=True fields) は保持
- **`coderouter/config/schemas.py`**
  - `Capabilities.thinking: bool = False` 追加。YAML で明示的に `true` を
    立てると heuristic を上書きできる (新モデルファミリーが出た時の escape
    hatch)。`reasoning_control: Literal[...]` (v1.0+ abstract interface) とは
    別物なので併存
- **`coderouter/routing/fallback.py`**
  - `_resolve_anthropic_chain(request)` — `request` が thinking を要求して
    いる場合、chain を `capable` / `degraded` の 2 バケットに stable-sort し
    て返す。要求なしの場合は従来通り declared order を保つ

#### Changed

- **`coderouter/routing/fallback.py`** — `generate_anthropic` / `stream_anthropic`
  の両方で:
  - `_resolve_chain(...)` → `_resolve_anthropic_chain(...)` に差し替え。戻り値が
    `list[tuple[BaseAdapter, bool]]` になり、各 provider について
    `will_degrade` フラグが付く
  - `will_degrade=True` の provider を呼ぶ前に `strip_thinking(request)` + 構造化
    ログ `capability-degraded` (`provider` / `dropped: ["thinking"]` / `reason`)
  - 既存の `try-provider` ログに `"degraded": will_degrade` を追加
- OpenAI ingress (`/v1/chat/completions`) 経路は変更なし。ChatRequest に
  thinking field がそもそもないため、capability logic を通す必要がない

#### Tests

- **+36 件** (合計 **189 件 green**)
  - `test_capability.py` (新規) +27: heuristic の capable/incapable ファミリー
    (パラメトリック), openai_compat 常時 incapable, YAML 明示 true が両 kind で
    wins, `requires_thinking` の enabled/disabled/missing/非 dict 各種, `strip`
    の除去 / 保持 / noop / wire-body clean / 他 extras 非破壊
  - `test_fallback_thinking.py` (新規) +9: capable-pull-to-front, plain-request
    順序保持, degraded fallback + `capability-degraded` ログ発火, strip 後の
    adapter 引数が wire-body レベルで clean, no-degraded-log when capable 成功
    / plain request, openai_compat は Claude-like slug でも incapable 扱い, YAML
    thinking:true で heuristic 外モデルを capable に昇格, streaming path も
    同じ preference

#### Notes

- **v0.5-B で予定**: `cache_control` の normalization。thinking と違って
  「400 vs 200」の二値ではなく「openai_compat 経由だと lossy で pass-through
  する / anthropic で preserve」という非対称性なので、別リリースで扱う
- **heuristic table のメンテナンス**: 新しい Claude family が出たら
  `capability.py` の `_THINKING_CAPABLE_PATTERNS` に regex を追加。allow-list
  なので古いパターンを削る必要はない (deprecated 家族がマッチしても害はない)
- **実機 verify は任意**: 本リリースの挙動は 36 件の unit/engine tests で確認
  済み。実機で chain 再選択を見たい場合は `providers.yaml` に capable/incapable
  の 2 つを置き、thinking 付きリクエストを `/v1/messages` に投げると
  `capability-degraded` ログの有無で確認できる

---

## [v0.4-D] — 2026-04-20

### `anthropic-beta` header passthrough (Claude Code 400 fix)

Claude Code → CodeRouter → `anthropic-direct` を実機で叩くと Anthropic から
`400 Bad Gateway` が返ってくる件の修正。ルートコーズは body field
`context_management` が `anthropic-beta: context-management-2025-06-27` header
なしでは拒否されること。Claude Code は header を送ってきていたが CodeRouter が
それを `api.anthropic.com` まで転送していなかった。

#### Added

- **`coderouter/translation/anthropic.py`**
  - `AnthropicRequest.anthropic_beta: str | None = Field(default=None, exclude=True)`
    — header-hop 用の stash。`exclude=True` なので `model_dump()` には出てこず、
    wire body にリークしない
- **`coderouter/ingress/anthropic_routes.py`**
  - `anthropic_beta: str | None = Header(alias="anthropic-beta")` を `messages()`
    ハンドラ引数に追加
  - 値が来ていれば `anth_req.anthropic_beta = anthropic_beta` で request に積む
- **`coderouter/adapters/anthropic_native.py`**
  - `_headers(request: AnthropicRequest | None = None)` シグネチャ変更。
    `request.anthropic_beta` が set なら `headers["anthropic-beta"]` に verbatim
    forward。`/v1/chat/completions` 逆翻訳パスは request を渡さないので OpenAI
    クライアントの既存挙動は変わらない (OpenAI 側は header を持たない前提)
  - `generate_anthropic` / `stream_anthropic` の `self._headers()` コールを
    `self._headers(request)` に置換。`healthcheck()` は request 文脈なしで呼ぶ
    ので引数なしのまま

#### Changed

- **`coderouter/routing/fallback.py`** — 診断性能の底上げ。
  `provider-failed` / `provider-failed-midstream` ログ 6 箇所に
  `"error": str(exc)[:500]` を追加。今回の 400 の中身 (`context_management`
  rejection の正確な wording) がこれで構造化ログに乗った。将来の同種のバグも
  server log を見るだけで当たりがつく

#### Tests

- **+6 件** (合計 **153 件 green**)
  - `test_adapter_anthropic.py` +4:
    `test_headers_omit_anthropic_beta_when_not_set` /
    `test_headers_forward_anthropic_beta_when_set` /
    `test_generate_anthropic_forwards_anthropic_beta_header` /
    `test_stream_anthropic_forwards_anthropic_beta_header`
  - `test_ingress_anthropic.py` +2:
    `test_anthropic_beta_header_threads_through_to_request` /
    `test_missing_anthropic_beta_header_leaves_field_none`
- カバー範囲: (a) field が body に leak しないこと (`Field(exclude=True)` の
  実挙動を outbound JSON で検証) / (b) header が outbound request に乗ること
  (streaming / non-streaming 両パス) / (c) ingress が header を抽出して
  request に積むこと / (d) 負のケース (header 未指定 → None のまま)

#### Notes

- 将来、他の beta feature も同じ経路で通せる。`anthropic-beta` はカンマ区切りで
  複数 feature flag を取る仕様なので、値は触らず verbatim forward が正しい
- v0.2 §8.4.1 の `?beta=true` クエリ文字列問題とは別件。あちらは Anthropic 側が
  黙殺するだけだが、今回は body field 不許可で 400 を返す heavier failure mode

---

## [v0.4-A] — 2026-04-20

### ChatRequest → AnthropicRequest 逆翻訳 (OpenAI ingress → kind:anthropic provider)

v0.3.x-1 の設計決定 F で意図的に out of scope としていた「OpenAI クライアントから
Anthropic-native provider を叩く」経路を埋める。`AnthropicAdapter.generate` /
`.stream` が retryable=False で reject していたのをやめ、
`ChatRequest → AnthropicRequest` および `AnthropicResponse → ChatResponse` /
`AnthropicStreamEvent* → StreamChunk*` の逆方向翻訳で上流 Anthropic Messages API
を呼ぶようにする。これにより `/v1/chat/completions` ingress と `kind: anthropic`
provider の組み合わせが対称的に動作するようになる。

#### Added

- **`coderouter/translation/convert.py`** — 逆方向の翻訳ヘルパを追加 (~300 lines)
  - `to_anthropic_request(ChatRequest) → AnthropicRequest`
    - `role: "system"` メッセージを top-level `system` フィールドに集約（複数 system
      メッセージは `\n` で結合）
    - 連続する `role: "tool"` メッセージを 1 つの user turn にまとめ、複数の
      `tool_result` block として格納（Anthropic canonical shape）
    - assistant の `tool_calls` を `tool_use` content block に変換
    - `image_url` content part を `data:` URI 判定で base64 / url source に振り分け
    - OpenAI `tools` → Anthropic `tools`（`parameters` → `input_schema`）
    - `tool_choice` 双方向マップ: `"auto"↔{type:auto}` / `"required"↔{type:any}` /
      `"none"↔{type:none}` / `{type:function}↔{type:tool}`
    - `max_tokens` 省略時は 4096 をデフォルト（Anthropic は必須、OpenAI は optional）
    - malformed JSON な `tool_calls.arguments` は `{"_raw": <string>}` に保持
  - `to_chat_response(AnthropicResponse) → ChatResponse`
    - 複数 text block は連結、`tool_use` block は top-level `tool_calls` に昇格
    - stop_reason 逆マップ: `end_turn→stop` / `max_tokens→length` /
      `tool_use→tool_calls` / `stop_sequence→stop`
    - `usage.input_tokens`/`output_tokens` → OpenAI `prompt_tokens` /
      `completion_tokens` / `total_tokens`
  - `stream_anthropic_to_chat_chunks(AnthropicStreamEvent*) → StreamChunk*`
    - stateful 翻訳: Anthropic の per-block index → OpenAI `tool_calls[].index` を
      `_ReverseStreamState.block_idx_to_tool_idx` で対応付け
    - 初期 `message_start` で `delta.role = "assistant"` を emit（OpenAI 慣例）
    - `text_delta` → `delta.content`
    - `tool_use` block_start → `delta.tool_calls[].function.name`（args 空）
    - `input_json_delta` → `delta.tool_calls[].function.arguments` 断片
    - 終端で finish_reason 付きチャンク + `choices: []` な usage チャンクを emit
      （OpenAI `stream_options.include_usage=true` と同形式）
    - Anthropic `event: error` は `AdapterError(retryable=False)` を raise。engine の
      v0.3-B mid-stream guard が `MidStreamError` に変換する経路はそのまま
- **`coderouter/adapters/anthropic_native.py`** — `generate` / `stream` を実装に差替
  - `generate(ChatRequest) → ChatResponse`:
    `to_anthropic_request` → `self.generate_anthropic` → `to_chat_response`
  - `stream(ChatRequest) → AsyncIterator[StreamChunk]`:
    `to_anthropic_request` → `self.stream_anthropic` → `stream_anthropic_to_chat_chunks`
  - retryable semantics は内部で呼ぶ `generate_anthropic` / `stream_anthropic` の
    ステータスコード分類をそのまま引き継ぐ（429 は retryable、400 は not）
  - `coderouter_provider` タグは両方向で保持
- **`coderouter/translation/__init__.py`** — 新規 export
  - `to_anthropic_request` / `to_chat_response` / `stream_anthropic_to_chat_chunks`

#### Changed

- **`FallbackEngine.generate` / `.stream`** — コード変更なし。`AnthropicAdapter` の
  OpenAI-shape メソッドが正しく動くようになったため、engine の polymorphic ループが
  自然に kind:anthropic provider を含む profile を扱えるようになる（混在 chain も含む）
- **`coderouter/ingress/openai_routes.py`** — 変更なし。`/v1/chat/completions` が
  `kind: anthropic` provider に到達できる経路が開通（従来は即 500）

#### Tests

v0.3.x-1 完了時点 110 件 → **147 件 (+37 件)**:

- `tests/test_adapter_anthropic.py` — OpenAI-shape エントリポイントの 2 件を
  「retryable=False で reject」テストから「reverse 翻訳で正常動作」テストに差替 (+2 net)
  - `test_openai_shaped_generate_reverse_translates`: system / user / assistant+tool_calls /
    tool / user の 5 メッセージ → 送信 body（system 昇格 / tool_result batching /
    tools shape / tool_choice map / max_tokens default）を検証、text+tool_use の
    レスポンスが `ChatResponse` に戻ることを確認
  - `test_openai_shaped_generate_429_is_retryable`: 429 → retryable=True が reverse
    経路でも保持される
  - `test_openai_shaped_stream_reverse_translates`: SSE を `adapter.stream` で消費し、
    role 初期チャンク / content delta / finish / trailing usage の順を検証
  - `test_openai_shaped_stream_anthropic_error_event_is_non_retryable`: upstream
    `event: error` が `AdapterError(retryable=False)` として surface する
- `tests/test_translation_reverse.py` **31 件（新設）**
  - `to_anthropic_request`: simple text / system 昇格 / 複数 system join / system list /
    assistant tool_calls / 連続 tool batching / tool-then-user flush / image data URI /
    image URL / tools 変換 / tool_choice 4 ケース / max_tokens passthrough /
    malformed JSON args / 空 user 省略 / 空 assistant placeholder / stream+profile+stop
  - `to_chat_response`: text only / tool_use only / mixed / 複数 text 連結 /
    stop_reason 4 ケース
  - `stream_anthropic_to_chat_chunks`: text stream / tool_use stream (args 断片結合) /
    parallel tool_use blocks の index 分離 / `event: error` → retryable=False
- `tests/test_fallback_anthropic.py` **+4 件**
  - `test_openai_generate_routes_to_kind_anthropic_via_reverse_translation`
  - `test_openai_stream_routes_to_kind_anthropic_via_reverse_translation`
  - `test_openai_generate_mixed_chain_falls_over_openai_to_anthropic`
  - `test_openai_stream_midstream_kind_anthropic_raises_midstream_error`

テスト合計: **147 passed**。lint: v0.4-A で導入した issue は 0。

#### Design Decisions

- **A**: adapter 層で透過的に変換する（engine を変えない）。`FallbackEngine.generate` /
  `.stream` は provider kind を気にせずループするため、reverse 翻訳は
  `AnthropicAdapter.generate` / `.stream` の内部実装で閉じる
- **B**: client 送信の `model` は placeholder 扱いとし、provider config の `model` が
  常に優先（v0.3.x-1 の openai_compat / anthropic-native ルールと同じ）
- **C**: OpenAI の `role: "tool"` を複数連続して受けた場合、Anthropic の canonical shape
  （1 つの user turn に複数の `tool_result` block）にまとめる
- **D**: Anthropic `event: error` → `AdapterError(retryable=False)`。初期の
  `message_start` で既に role チャンクを emit 済みなので engine の mid-stream guard
  が `MidStreamError` に変換する動作も検証済み

#### Known Limitations

- 「OpenAI ingress → kind:anthropic provider」経路で送る場合、`max_tokens` を
  client が省略すると 4096 にデフォルトされる。精密に制御したいユーザは
  `/v1/chat/completions` body に `max_tokens` を明示する必要あり
- Anthropic 独自の `cache_control` / `thinking` ブロックは OpenAI 側に等価表現が
  ないため、OpenAI ingress からは設定不可。cache_control を活かしたい場合は
  v0.3.x-1 で追加した `/v1/messages` ingress を使う

---

## [v0.3.x-1] — 2026-04-20

### Anthropic Native Adapter (passthrough)

Claude 本家 / OpenRouter の Anthropic 互換エンドポイントに対し、翻訳コスト
ゼロで素通しする native adapter。`ProviderConfig.kind: "anthropic"` で有効化し、
`/v1/messages` → `AnthropicAdapter` → upstream Anthropic Messages API の経路で
cache_control / thinking / structured tool_use などの Anthropic 固有フィールドを
そのまま活用できるようにする。openai_compat provider と混在した fallback chain
もサポート（native 先頭 → openai_compat 後続、あるいはその逆）。

#### Added

- **`coderouter/adapters/anthropic_native.py`** — `AnthropicAdapter(BaseAdapter)`
  - 認証: `x-api-key` ヘッダ（Authorization: Bearer ではない）、`api_key_env` から取得
  - `anthropic-version: 2023-06-01` をデフォルト付与。`extra_body.anthropic_version` で上書き可
  - `base_url` は `/v1` 終端有無の両方を正規化して `{base}/v1/messages` を叩く
  - `generate_anthropic(AnthropicRequest) → AnthropicResponse` — httpx 直叩きの passthrough
  - `stream_anthropic(AnthropicRequest) → AsyncIterator[AnthropicStreamEvent]`
    - SSE を `event:` / `data:` ペアで buffer、空行境界で block 確定
    - heartbeat コメント行と malformed block は silently skip
  - OpenAI-shape の `generate` / `stream` は `retryable=False` の `AdapterError` を raise
    （逆翻訳 `ChatRequest → AnthropicRequest` は設計決定 F で out of scope）
  - retryable status code: `{404, 408, 425, 429, 500, 502, 503, 504}`
  - クライアント送信の `model` は strip、provider config の `model` が常に優先
- **`coderouter/routing/fallback.py`** — Anthropic 用 dispatch 追加 (~110 lines)
  - `generate_anthropic(AnthropicRequest) → AnthropicResponse`:
    adapter ごとに `isinstance(adapter, AnthropicAdapter)` で native / openai_compat を切替。
    native は passthrough、openai_compat は `to_chat_request` → `adapter.generate`
    → `to_anthropic_response(allowed_tool_names=...)` の経路（v0.3-A repair が発火）
  - `stream_anthropic(AnthropicRequest) → AsyncIterator[AnthropicStreamEvent]`:
    native は `adapter.stream_anthropic` を直 passthrough、openai_compat + tools は
    v0.3-D downgrade（内部非 stream → repair → `synthesize_anthropic_stream_from_response`）、
    openai_compat no-tools は `stream_chat_to_anthropic_events` 経由の真 streaming
  - mid-stream ガードは既存 `stream()` と同一セマンティクスを維持
    （first event 送出後の `AdapterError` → `MidStreamError`、fallback 禁止）

#### Changed

- **`coderouter/config/schemas.py`** — `ProviderConfig.kind` の `Literal` に `"anthropic"` を追加
  （`openai_compat` と並列）。既存設定は default のまま `openai_compat` が継続
- **`coderouter/adapters/registry.py`** — `build_adapter` が `kind="anthropic"` で
  `AnthropicAdapter` をインスタンス化するよう分岐
- **`coderouter/ingress/anthropic_routes.py`** — v0.3-D downgrade ロジックを engine に移設した
  副作用で大幅に簡素化。`messages()` ハンドラは `engine.generate_anthropic` /
  `engine.stream_anthropic` を呼ぶだけになり、ingress は HTTP 境界 + SSE wire format の
  責務のみ保持。`_anthropic_sse_iterator` は engine から流れる event を wrap しつつ
  `NoProvidersAvailableError → overloaded_error` / `MidStreamError → api_error` に変換
- **`examples/providers.yaml`** — `anthropic-direct` サンプル provider を追記
  (`kind: anthropic`, `paid: true`, `ANTHROPIC_API_KEY` 参照)

#### Tests

v0.3 完了時点 87 件 → **110 件 (+23 件)**:

- `tests/test_adapter_anthropic.py` **11 件（新設）**
  - URL 正規化 (`/v1` 終端有無両対応)
  - `x-api-key` / `anthropic-version` ヘッダ（default / override 両方）
  - OpenAI-shape `generate` / `stream` は retryable=False で reject
  - `generate_anthropic`: payload shape（client の model は無視、provider config が勝つ）、
    429 / 400 / 500 の status マッピング
  - `stream_anthropic`: SSE パースで `event:`/`data:` ペアを AnthropicStreamEvent に、
    `stream: true` が body に入る、初期 4xx は AdapterError、heartbeat / malformed block skip
- `tests/test_fallback_anthropic.py` **12 件（新設）**
  - native passthrough / openai_compat 経由の round-trip
  - tool-call repair が openai_compat 経由の generate_anthropic でも発火
  - 混在 chain（native → openai_compat / openai_compat → native）の fallback 双方向
  - 全 provider 失敗 → `NoProvidersAvailableError`、non-retryable は即中断
  - stream: native 真 streaming、openai_compat no-tools 真 streaming、
    openai_compat + tools は downgrade（`generate_calls` のみ埋まり `stream_calls == []`）、
    **native + tools は downgrade せず** native の structured tool_use を passthrough
  - mid-stream 失敗 → `MidStreamError`、初期失敗は従来どおり fallback
- `tests/test_ingress_anthropic.py` — engine への責務移譲に合わせ stub engines を
  `AnthropicRequest` / `AnthropicResponse` / `AnthropicStreamEvent` を直接やり取りする
  形にリライト。downgrade 関連の ingress 側テストは engine 側 (`test_fallback_anthropic.py`)
  に移譲

テスト合計: **110 passed**。lint: v0.3.x-1 で導入した issue は 0
（新規 `anthropic_native.py` の SIM117 は既存 `openai_compat.py` と同じパターンで意図的に踏襲）。

#### Design Decisions

- **A-1**: Anthropic-shape entry points を engine に追加（adapter 側だけで完結させず、
  既存の fallback / mid-stream guard / profile resolution をそのまま再利用する）
- **B**: 混在 chain（native + openai_compat が 1 profile に共存）を第一級サポート
- **C**: SSE は parse ベース（line-based → block-based）で受ける。mid-stream guard を
  event 単位で効かせるため
- **D**: 認証は `api_key_env` + `x-api-key` 固定、`anthropic-version` ヘッダ追加
- **E**: 5 依存原則を維持（`anthropic` SDK は使わず httpx 直叩き）
- **F**: 逆翻訳（`ChatRequest → AnthropicRequest`）は out of scope。OpenAI クライアントから
  Anthropic-native provider を叩く経路は今後のスコープ（`generate` / `stream` は
  retryable=False で即 reject）

#### Known Limitations

- client が `/v1/messages` に送る `model` は無視され、provider config の `model` が勝つ
  （OpenAI-compat adapter と同じ挙動）。profile 経由でしかモデルを切替できないが、
  これは CodeRouter のルーティング設計として意図的
- `ChatRequest` → `AnthropicRequest` の逆方向翻訳は未実装（設計決定 F）。
  OpenAI クライアントから Anthropic-native provider を叩きたい場合は v0.4+ の課題

---

## [v0.3.0] — 2026-04-20

### v0.3: 実運用向け品質改善

Claude Code + ローカル LLM (qwen2.5-coder:14b など) で実運用したときに浮いた
3つの課題を潰すフェーズ。いずれも v0.2 で「仕様通りには動いているが実モデル
が歪んだ出力を返したときに壊れる」領域。

#### Added

- **Tool-call repair (non-streaming / v0.3-A)** — `coderouter/translation/tool_repair.py`
  - 上流モデル（特に qwen2.5-coder:14b）が `tool_calls` フィールドを使わず
    `{"name": ..., "arguments": ...}` を平文 text で返す失敗パターンに対応
  - balanced-brace scanner（文字列/エスケープ認識）で text body から JSON を抽出
  - fenced ``` ```json ``` ブロックも検出
  - リクエストが宣言した tool 名 allowlist に照合し、未知の tool 名は text のまま残す
  - 抽出された JSON は OpenAI `tool_calls` 形式に正規化され、後段で通常どおり
    Anthropic `tool_use` content block に変換される
  - `to_anthropic_response(..., allowed_tool_names=[...])` で呼び出し
- **Mid-stream fallback guard (v0.3-B)** — `coderouter/routing/fallback.py`
  - 新しい例外 `MidStreamError(provider, original)` を追加
  - `FallbackEngine.stream()` は first byte 送出後に AdapterError が出たら
    次 provider に fall through せず `MidStreamError` を raise
  - `_anthropic_sse_iterator` が `MidStreamError` を捕まえ `event: error` /
    `type: api_error` を emit して SSE を閉じる（「最初の 1 byte も出せない」
    `overloaded_error` とは区別）
  - 目的: Claude Code の画面に部分応答 + 重複コンテンツが届く事故を防ぐ
- **Usage aggregation (v0.3-C)** — `coderouter/translation/convert.py`
  - stream 終端の `message_delta.usage.output_tokens` を正しく埋める
  - 優先順位: upstream の `completion_tokens`（authoritative） > `(emitted_chars + 3) // 4` 概算
  - `input_tokens` は upstream が `prompt_tokens` を送ってきた場合のみ反映
  - OpenAI-compat adapter は streaming 時に自動で `stream_options: {"include_usage": true}` を付与。
    provider 側が `extra_body` で上書きしていればそちらが優先
  - Ollama のように flag を無視する upstream でも、char 概算のおかげで 0 にはならない
- **Tool-call repair (streaming / v0.3-D)** — strategy 2: downgrade to non-stream
  - `tools` を宣言した streaming リクエストは内部で `stream=false` に切り替え、
    v0.3-A の repair を通してから `synthesize_anthropic_stream_from_response` で
    Anthropic SSE イベント列を合成して返す
  - クライアントから見た wire はあくまで streaming（`message_start → … → message_stop`）
  - tool を含まない streaming は従来通り真の streaming パス
  - トレードオフ: tool ターンは first-byte latency が full response 時間まで伸びる
    （tool ターンは実質的に「完成してから次の手」が前提なので許容）

#### Changed

- `coderouter/adapters/openai_compat.py` — streaming 時 `stream_options.include_usage` を既定で true
- `coderouter/translation/__init__.py` — `synthesize_anthropic_stream_from_response` を export
- `coderouter/routing/__init__.py` — `MidStreamError` を export
- `_handle_delta` が `emitted_chars` を累積（text_delta + tool name + input_json_delta）

#### Fixed

- **`Message.content = None` が pydantic ValidationError で 500 を返す** — Claude Code が
  multi-turn 履歴に「tool_use だけ / text なし」の assistant ターンを含めてくると、
  `_convert_anthropic_message` が `content: None` を吐き、`Message` モデルが reject していた。
  OpenAI spec は `tool_calls` を持つ assistant message に `content: null` を許可しているので、
  `coderouter/adapters/base.py` の `Message.content` 型を `str | list[dict[str, Any]] | None = None`
  に拡張し、`exclude_none=True` のシリアライズで upstream には content キーを送らない挙動に統一。
  regression test を `tests/test_translation_anthropic.py::test_assistant_message_with_only_tool_use_has_null_content` に追加。

#### Tests

v0.2 完了時点 54 件 → v0.3 完了後 **86 件（+32 件）**:

- `tests/test_tool_repair.py` **13 件**（新設） — text 埋込 JSON 抽出の全パターン
- `tests/test_translation_anthropic.py` **+8 件**
  - repair 連携 3 件
  - usage 集計 5 件（upstream 優先 / 概算 fallback / tool args 込み / 空応答 0 / upstream が estimate を override）
  - synthesizer 3 件（text-only / tool_use / mixed）
- `tests/test_ingress_anthropic.py` **+4 件**
  - mid-stream 時の `event: error` / `type: api_error`
  - tool 付き streaming の downgrade + repair
  - tool なし streaming は real streaming のまま
  - downgrade パスでも 502 は error event で surface
- `tests/test_fallback.py` **+2 件** — mid-stream で MidStreamError, 初期エラーは従来どおり fallback
- `tests/test_openai_compat.py` **+2 件** — stream_options.include_usage 自動付与 / extra_body 上書き尊重

lint (ruff): v0.3 で導入した issue は 0。残 11 件はすべて v0.1/v0.2 由来の既知事項。

#### Verified (2026-04-20 実機)

Ollama + qwen2.5-coder:14b + Claude Code (`ANTHROPIC_BASE_URL=http://localhost:8088`) で疎通確認。

- **(a) tool なし text streaming (curl 直撃 `/v1/messages`)** — real streaming path
  (`engine.stream()` → `stream_chat_to_anthropic_events`) で
  `message_start → content_block_start → content_block_delta × N → content_block_stop →
  message_delta → message_stop` まで spec 準拠。
  - 1 回目: `usage: {output_tokens: 122, input_tokens: 46}` ← Ollama が
    `stream_options.include_usage: true` を honor → v0.3-C の **upstream authoritative パス**
    が発火。
  - 2 回目: `usage: {output_tokens: 97}` (input_tokens 欠落) ← Ollama が terminal usage chunk
    を省略 → v0.3-C の **char-based estimate fallback** が発火。2 経路とも実機で踏めた。
- **(b) Claude Code + tool 付き streaming** — `_anthropic_downgraded_tool_iterator` が
  動作 (サーバログに `try-provider ... stream: false` → `provider-ok ... stream: false`)。
  `tool_use` content block が Claude Code UI に tool invocation として描画された
  (`⏺ Glob()` など)。ただしモデルの tool 選択の妥当性は別レイヤの問題
  (qwen2.5-coder:14b は `pwd` 要求に対して Bash でなく Glob を選ぶなどした)。
- **プロファイル経路**: `skip-paid-provider` (openrouter-claude, `ALLOW_PAID=false`)
  → `ollama-qwen-coder-14b` の fallback を実機で確認。
- **Bug fix (実機疎通中に発見)**: `Message.content = None` を pydantic が reject して 500
  を返していた問題を修正。Claude Code は multi-turn 履歴に「tool_use のみ / text なし」の
  assistant ターンを含めるため、2 ターン目以降で必ず踏む構造のバグだった。
  - `coderouter/adapters/base.py` の `Message.content` を
    `str | list[dict[str, Any]] | None = None` に拡張
  - `_prepare_messages` は `exclude_none=True` で dump するので upstream には
    content キーごと送らない → OpenAI spec どおりの shape を維持
  - regression test: `test_translation_anthropic.py::
    test_assistant_message_with_only_tool_use_has_null_content`
- **(c) mid-stream guard**: unit test
  (`test_ingress_anthropic.py::test_streaming_midstream_failure_emits_api_error_event`
  ほか) でカバー。実機 pkill の timing を qwen の生成速度に合わせるのは困難で、
  かつ Ollama の runner/serve 2 プロセス構成で graceful close を返されるケースがあり、
  実機 smoke は optional とした。ロジック自体は代数的にテスト済み。
- **Claude Code の tool 宣言挙動**: Claude Code は毎ターン全 tool (Bash/Glob/Read/Write/...)
  を `tools: [...]` で送ってくるため、**Claude Code 経由では常に v0.3-D downgrade path
  に入る**。real streaming path は tool を宣言しない OpenAI-shape 互換クライアントや
  Anthropic direct curl でのみ使われる。この構造は CHANGELOG の Known Limitations
  「tool を含む streaming は実質的に非 streaming と同じ遅延プロファイル」と一致。

総テスト件数: **87 passed** (86 + 実機疎通中の bug fix regression 1)。lint clean。

#### Known Limitations

- qwen2.5-coder:14b のような tool-call を text で返すモデルでも現在は repair で
  wire 準拠に戻せるが、実際に tool が呼び出せるかは「モデルが引数を正しく組み立てるか」
  という別レイヤの問題。repair は信号経路の話であり、モデル能力の補完ではない
- tool を含む streaming は実質的に非 streaming と同じ遅延プロファイル。「tool 判断を
  ユーザに見せながら stream」は v0.4+ の課題（strategy 1: 投機的 emit + rollback）
- `input_tokens` は upstream が `prompt_tokens` を送った場合のみ。ローカル
  tiktoken 同梱での事前計測は依存 5 パッケージ制約（plan.md §5.4）があるため v1.0+ で検討
- OpenRouter / Claude 本家 API 経由では未検証（v0.3-E で補完予定）

---

## [v0.2.0] — 2026-04-20

### Anthropic Ingress

Claude Code などの Anthropic クライアントから `ANTHROPIC_BASE_URL=http://localhost:8088`
で直接 CodeRouter を叩けるようになりました。

#### Added

- **`POST /v1/messages`** — Anthropic Messages API 互換 ingress
  - 非 streaming / streaming (SSE) 両対応
  - `message_start → content_block_start → content_block_delta(×N) → content_block_stop → message_delta → message_stop` の spec 準拠順で event 発火
  - `tool_use` / `tool_result` / `image` / `text` の content block 4 種を双方向変換
  - `system` は string / block list の両形を受け、内部では 1 本の system message に flatten
  - `stop_sequences` / `temperature` / `top_p` / `top_k` を passthrough
  - `anthropic-version` ヘッダを受理（enforce はしない、debug ログに残すのみ）
  - profile 選択は既存 OpenAI route と同じく body > `X-CodeRouter-Profile` ヘッダ > default の順
  - 未知 profile は 400、プロバイダ全滅は 502（非 stream）/ `event: error`（stream）
- **`coderouter/translation/`** 新モジュール
  - `anthropic.py` — Anthropic wire-format の pydantic models（request / response / stream event + content block 4 種）
  - `convert.py` — Anthropic ⇄ 共通 `ChatRequest`/`ChatResponse` の双方向変換
    - `to_chat_request`, `to_anthropic_response`
    - `stream_chat_to_anthropic_events` は stateful に block index を管理（text→tool_use 切替時は text block を先に閉じる、multi tool_call に個別 index）
  - malformed tool_call JSON は `_raw` 退避で素通しし、後段で修復可能に
- **`/` と `HEAD /`** に tiny handler — Claude Code 起動時の preflight で 404 を返さないように
- **テスト +28 件 / 総数 54 件**
  - `tests/test_translation_anthropic.py` 17 件 — request / response / stream 変換ユニット
  - `tests/test_ingress_anthropic.py` 11 件 — HTTP 境界、profile 経路、SSE event 順序、エラーマッピング

### Changed

- `providers.yaml` — `ollama-qwen-coder-14b` の `timeout_s` を 120 → 300
  （Claude Code は 15-20K token の巨大 system prompt を毎ターン送るので、14B クラスでは 120s を平気で超えるため）
- `plan.md` §8 を完了形に更新、§8.4 に実装知見 7 項目、§8.5 に v0.3 以降へ送った項目を明記

### Verified

- `ANTHROPIC_BASE_URL=http://localhost:8088 claude` でフルパス疎通
  - text 応答・streaming SSE 順序・tool 定義引き渡しまで一周
- 全 54 テスト green
- 本家 Ollama / qwen2.5-coder:14b 実機で動作

### Known Limitations (→ v0.3 以降)

- **tool-call 構造化出力の不安定性**: qwen2.5-coder:14b に Claude Code の 10+ tool 定義を渡すと、
  `tool_calls` フィールドではなく text 本文に JSON ブロックで返してくることがある。これは翻訳バグではなくモデル能力限界で、
  v1.0 の「tool-call 信頼性」スコープで text → tool_calls 引き剥がしヒューリスティックを入れる。
- **mid-stream fallback**: 初バイト送出後に provider が落ちた場合の fallback を現状は禁止していない。
  v0.3 で `first_byte_sent` ガード + `event: error` emit に変更予定。
- **`message_delta.usage.output_tokens`** が 0 固定（stream 終端で usage を集計していない）。v0.3 で改修。
- **Anthropic native adapter** (`kind: "anthropic"`, 翻訳を通さない pass-through) は未実装。v0.3 以降。

---

## [v0.1.0] — 2026-04-20

### Walking Skeleton

"OpenAI 互換 ingress + ローカル 1 個 + フォールバック 1 個が動く" の最小骨組み。

#### Added

- **`POST /v1/chat/completions`** — OpenAI Chat Completions 互換 ingress（非 streaming / streaming SSE）
- **adapter 層** — `BaseAdapter` + `OpenAICompatAdapter`（llama.cpp / Ollama / OpenRouter / LM Studio / Together / Groq を 1 枚でカバー）
- **`FallbackEngine`** — 順次 fallback、`retryable=False` で中断、`paid=true` は `ALLOW_PAID=false` 環境で skip
- **`providers.yaml` / `profiles`** — provider 定義 + fallback chain 名前付け
- **profile 選択** — body `profile` フィールド > `X-CodeRouter-Profile` ヘッダ > `default_profile` の順
- **`ProviderConfig.extra_body` / `append_system_prompt`** — モデル固有オプション
- **JSON 構造化ログ** — `coderouter.routing.fallback` から `try-provider` / `provider-ok` / `provider-failed` / `skip-paid-provider`
- **`/healthz`** エンドポイント
- **テスト 26 件**（config / fallback / openai 互換 / profile 選択）

### Verified

- curl で fizzbuzz 生成成功
- fallback: 1 つ目の provider を外すと 2 つ目に自動遷移
- fast profile 実機確認 (qwen2.5:1.5b → gemma3:1b の 2 ホップ成功)
- 全 26 テスト green

### Notable Decisions / Implementation Learnings

- **qwen3.x の thinking モードは抑制不能**
  - Ollama は `think: false` を落とす / qwen3.5:4b は RL で `/no_think` を拒否
  - fast profile からは qwen3.x を外し、dedicated `think` profile に移管
- **Lazy module-level `app`** via `__getattr__`
  - `uvicorn coderouter.ingress.app:app` は機能させつつ、テスト import で providers.yaml を eager load しない
- **Bug fix**: `request.model` が provider の model を上書きしていた問題を修正（provider 固有 model を送る仕様に）
- **Bug fix**: 404 を retryable に変更（ルート違いの fallback を許容）

---

## Unreleased

v0.3 以降の候補は [`plan.md` §8.5](./plan.md) と [`plan.md` §18](./plan.md) を参照。

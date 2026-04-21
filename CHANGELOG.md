# Changelog

All notable changes to CodeRouter are recorded here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/),
versioning follows [SemVer](https://semver.org/).

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

# Changelog

All notable changes to CodeRouter are recorded here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/),
versioning follows [SemVer](https://semver.org/).

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

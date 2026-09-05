# 外部コーディングエージェント CLI (agent_cli)

> English: [`external-agents.en.md`](./external-agents.en.md)

`kind: "agent_cli"` は、Claude Code CLI のような外部コーディングエージェントを CodeRouter の 1 プロバイダとして登録するアダプタである。v2.7.7 で新規追加され(Phase 1a: claude)、v2.7.8 で grok が追加され(Phase 1d)、v2.7.9 で codex が追加され(Phase 1b)、v2.7.10 で antigravity が追加された(Phase 1c)。これにより **Phase 1(4バックエンド)は完了**した。詳細設計は [`docs/designs/external-agents-adapter.md`](../designs/external-agents-adapter.md) を参照。

> **重要: v2.9.0 で破壊的変更(agent_cli plugin 切り出し Phase 2c)** — in-core の `agent_cli` アダプタは v2.9.0 で削除された。`kind: "agent_cli"` を使うには外部プラグイン **`coderouter-plugin-agents`** のインストールと、`providers.yaml` への `plugins.enabled: [agents]` の指定が **必須**になった。v2.8.x までは in-core 実装が優先して動く猶予期間(Phase 2b)があったが、v2.9.0 でその猶予は終了している。手順は下記の[クイックスタート](#クイックスタート)を参照。プラグイン未導入のまま `kind: agent_cli` を書くと `coderouter-t serve` は起動時にエラーで停止し、移行手順を示すメッセージが表示される。`coderouter doctor` も同じ設定ミスを検出して警告する。`agent_cli:` サブ設定(`AgentCliConfig`)のスキーマ自体および各プロバイダのエントリは無変更であり、書き換える必要はない。

---

## 概要

コーディングエージェント CLI は本来、ファイルを書き換えながら何ターンも自律的に動くステートフルな制御ループであり、CodeRouter の「1リクエスト = 1変換」という思想とは相性が悪い。`agent_cli` はこれを **ワンショット `exec`**(プロンプト in → 最終回答テキスト out)に押し込めることで両立させている。オーケストレーション(マルチターン制御・ツール実行)はエージェント CLI 内部で完結し、CodeRouter 側からは「1回の対話で答えを返すだけの1つのプロバイダ」として見える。

- **対象 CLI**: `agent` フィールドで `claude` / `codex` / `antigravity` / `grok` の4種を宣言できる。`gemini` もスキーマの `Literal` には残っているが、常に拒否される(下記参照)。
- **実装状況(v2.7.10 時点)**: **Phase 1 が完了し、`claude`(Claude Code CLI・Phase 1a)・`codex`(OpenAI Codex CLI・Phase 1b)・`antigravity`(Google Antigravity CLI・Phase 1c)・`grok`(Grok CLI・Phase 1d)の4バックエンドすべてが実装済み**。`gemini` は Google が2026年6月に個人アカウント向け Gemini CLI の提供を終了したため実装対象から外れ、アダプタ構築時に必ず次のような専用メッセージ付きの AdapterError で拒否される:
- **v2.9.0 以降の前提条件**: 上記4バックエンドのアダプタ本体は Phase 2b(v2.8.1)で `coderouter-plugin-agents` へ移設され、Phase 2c(v2.9.0)で in-core コピーが削除された。したがって v2.9.0 以降、`kind: agent_cli` を使うには `coderouter-plugin-agents` のインストール + `plugins.enabled: [agents]` が必須である。`agent_cli:` サブ設定(`AgentCliConfig`)・各 CLI の argv/挙動・provider エントリの書き方はいずれも無変更(下記の各セクション参照)。

  ```
  AdapterError: Google discontinued Gemini CLI for individual accounts (June 2026).
  Use agent='antigravity' instead.
  ```

  この拒否は `retryable=False` — フォールバックチェーンに他プロバイダがあっても、設定ミスとして即座に停止する。経緯の詳細は [gemini の廃止と antigravity への移行](#gemini-の廃止と-antigravity-への移行) を参照。

- 本ドキュメントの共通部分(認証設計・設定リファレンス・制限事項)は claude ターゲットを軸に記述し、codex / antigravity / grok 固有の挙動はそれぞれ [codex(OpenAI Codex CLI)](#codexopenai-codex-cli) / [antigravity(Google Antigravity CLI)](#antigravitygoogle-antigravity-cli) / [grok(Grok CLI)](#grokgrok-cli) セクションにまとめる。

### gemini の廃止と antigravity への移行

旧 Gemini CLI(`@google/gemini-cli` 0.50.0 系)は、個人アカウントの OAuth が **2026-06-18 をもって廃止済み**である(公式ブログ)。実機で以下のエラーが確認されている。

```
IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals.
To continue using Gemini, please migrate to the Antigravity suite of products: https://antigravity.google
```

(`reasonCode: UNSUPPORTED_CLIENT`, `tierId: free-tier`)

さらに旧 gemini CLI は trusted-directory ゲートで **exit 55**(`--skip-trust` または `GEMINI_CLI_TRUST_WORKSPACE=true` が必要)にも本アダプタの実機検証中に遭遇している — こちらは個人アカウント終了とは別に以前から存在する制約である。

Google が後継として案内しているのは **Antigravity CLI**(コマンド名 `agy`)である。gemini-cli のフォークではなく Go 製の別実装であり、個人の Google アカウント OAuth(無料枠含む)はこちらで存続する。CodeRouter はこの移行を受けて、当初計画していた Phase 1c(`agent: "gemini"`)を **`agent: "antigravity"`** として実装した(v2.7.10)。

`agent: "gemini"` はスキーマの `Literal` には残すが、アダプタ構築時に上記の専用メッセージで拒否される。`gemini` を使っていた設定は `agent: antigravity` に切り替え、[antigravity(Google Antigravity CLI)](#antigravitygoogle-antigravity-cli) セクションの設定例を参照すること。

---

## クイックスタート

1. **`coderouter-plugin-agents` をインストールし、有効化する**(v2.9.0 以降必須。未実施だと `kind: agent_cli` の provider がある状態で `coderouter-t serve` が起動時エラーになる):

   ```bash
   # uv の場合
   uv pip install "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"

   # pip の場合
   pip install "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"

   # CodeRouter 本体を uv tool install で入れている場合は同じツール環境に同居させる
   uv tool install coderouter-t \
     --with "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"
   ```

   `providers.yaml` に `plugins.enabled: [agents]` を追記する(二段ゲート — インストールしただけでは有効にならない):

   ```yaml
   plugins:
     enabled: [agents]
   ```

2. **Claude Code CLI をインストールする**(`claude --version` で確認できること)。
3. **ログインを済ませる** — 対話起動して `claude` を実行し `/login` を通すか、ヘッドレス環境なら [プラットフォーム別の認証](#プラットフォーム別の認証) の手順で `claude setup-token` を使う。
4. **サンプル設定で起動する**(`examples/providers-agent-cli.yaml` には既に `plugins.enabled: [agents]` が入っている):

   ```bash
   uv run coderouter-t serve --config examples/providers-agent-cli.yaml --port 8088
   ```

5. **動作確認**:

   ```bash
   curl http://localhost:8088/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -H 'X-CodeRouter-Profile: claude-agent' \
     -d '{"model":"opus","messages":[{"role":"user","content":"1行でこんにちはと言って"}]}'
   ```

### 初回呼び出しは遅い

`agent_cli` は毎回 CLI プロセスを新規起動する(常駐なし)。初回はプロセス起動 + Claude 本体との1往復が乗るため、通常の HTTP バックエンドより明らかに遅い。

さらに `usage.prompt_tokens` が **2万トークン台**になることがある。これは Claude Code 自身のシステムプロンプト(hooks/CLAUDE.md 探索・ツール定義一式)が毎回プロンプトに乗るためで、CodeRouter 側から渡した実際のメッセージ量とは無関係である。サブスクリプション OAuth で動かしている場合、この分の**課金は発生しない**(5時間窓/週次クォータの消費にはなる — [制限事項](#制限事項) 参照)。

2回目以降は Anthropic 側のプロンプトキャッシュが効き、レスポンスの `usage.prompt_tokens_details.cached_tokens` が増える。実測では、同一 `workdir` への初回呼び出しで `coderouter_cost_usd`(API従量換算のドル相当額。課金額そのものではない — [設定リファレンス](#設定リファレンス) 参照)が **約 $0.22 相当**だったのに対し、キャッシュが効いた2回目以降は **約 $0.05 相当**まで下がった。

---

## プラットフォーム別の認証

`AgentCliAdapter` は子プロセスに**親プロセスの環境をそのまま継承させない**。固定の安全な `PATH` / `NO_COLOR=1` / `TERM=dumb`、そして `HOME` / `USER` / `LOGNAME`(値が設定されている場合のみ)、`passthrough_env` に列挙した変数だけを明示的に注入する。この設計により `ANTHROPIC_API_KEY` は既定では子プロセスに渡らず、サブスクリプション認証(OAuth)が優先される。

| プラットフォーム | 資格情報の保管場所 | 必要な環境変数の継承 | v2.7.7 での対応状況 |
|---|---|---|---|
| **macOS** | Keychain | `USER`(Keychain エントリ解決に必須) | v2.7.7 で `USER` / `LOGNAME` を継承するようになり対応済み。`claude /login` 済みならそのまま動作(実機確認済み) |
| **Linux** | `~/.claude/.credentials.json`(パーミッション `0600`) | `HOME` | `HOME` 継承のみで動作。`claude /login` 済みならそのまま動作(実機確認済み) |
| **ヘッドレスサーバー / コンテナ**(ブラウザ無し) | 上記いずれか、または長期トークン | `CLAUDE_CODE_OAUTH_TOKEN`(`passthrough_env` で明示) | 下記手順を参照 |
| **Windows** | ネイティブ未対応 | — | WSL2 上で CodeRouter ごと動かす(Linux と同じ扱いになる) |

### macOS

Claude Code CLI は Keychain からトークンを解決する際に `USER` 環境変数を参照する。v2.7.7 以前の env allowlist は `HOME` しか継承しておらず、`USER` が欠けていたため macOS のヘッドレス/サーバー実行で Keychain 解決に失敗し `Not logged in` になっていた。v2.7.7 で `_build_child_env()` が `USER` / `LOGNAME` も継承するよう修正され、この問題は解消している。事前に `claude` を対話起動して `/login` を完了させておけば、追加設定なしでそのまま動作することを実機で確認済み。

### Linux

資格情報は `~/.claude/.credentials.json`(パーミッション `0600`)に保存される。子プロセスは `HOME` を継承するだけでこのファイルを読める。macOS と同様、事前に `claude /login` を済ませておけばそのまま動作することを実機で確認済み。

### ヘッドレスサーバー / コンテナ(ブラウザ無し)

ブラウザ付きの対話ログインができない環境向けに、長期トークンを発行して環境変数経由で渡す経路がある。

1. **ブラウザのあるマシン**で `claude setup-token` を実行し、1年間有効な OAuth トークンを発行する。
2. 発行されたトークンを対象サーバーの `.env` に `CLAUDE_CODE_OAUTH_TOKEN=...` として置く。パーミッションは `0600`、`.gitignore` での除外を必ず確認する(`coderouter doctor --check-env` の `env_security` チェックがこの2点を検査する)。
3. `providers.yaml` の該当プロバイダで `agent_cli.passthrough_env: [CLAUDE_CODE_OAUTH_TOKEN]` を指定し、子プロセスへ明示的に転送する。

### Windows

`AgentCliAdapter` は POSIX 前提で実装されている(`os.killpg` によるプロセスグループ kill、`/usr/local/bin` 形式の固定 `PATH` など)ため、Windows ネイティブでは動作しない。WSL2 内に CodeRouter 一式を立て、WSL2 上の `claude` を呼ぶ構成にすれば、実質的に Linux と同じ扱いになる。

### 重要な注意 — API キーは自動では渡らない

子プロセスは親環境を継承しないため、シェルで `ANTHROPIC_API_KEY` をエクスポートしていても **claude CLI には渡らない**。これはサブスクリプション認証を優先し、環境に残った API キーがうっかりサブスク認証を上書きする事故を防ぐための意図的な設計である。API キー従量課金で動かしたい場合のみ、`passthrough_env: [ANTHROPIC_API_KEY]` のように明示的に列挙すること。

---

## 設定リファレンス

> `agent_cli:` サブ設定(`AgentCliConfig`)のスキーマは v2.9.0 でも変更されていない。変更点は「`coderouter-plugin-agents` のインストール + `providers.yaml` への `plugins.enabled: [agents]` の追加」のみであり、既存の provider エントリ自体を書き換える必要はない([クイックスタート](#クイックスタート)参照)。

`providers.yaml` の `agent_cli:` サブ設定(`AgentCliConfig`)の全フィールド。`extra: forbid` なので未知のキーは設定読み込み時に即座にエラーになる。

| フィールド | 型 | 既定値 | 説明 |
|---|---|---|---|
| `agent` | `"claude" \| "codex" \| "antigravity" \| "gemini" \| "grok"` | (必須) | 呼び出す CLI。**v2.7.10 で `claude`・`codex`・`antigravity`・`grok` の4つすべてが実装済み(Phase 1 完了)。`gemini` はスキーマに残るがアダプタ構築時に拒否される** |
| `command` | `str \| null` | `null`(未設定時は `agent` と同名) | CLI 実行ファイル名 or 絶対パス。`PATH` から解決。**`agent: antigravity` のみ既定値は `agy`**(バイナリ名が製品名と異なるため) |
| `workdir` | `str \| null` | `null`(未設定時は `~/.coderouter-t/agents/<プロバイダ名>`) | ワンショット exec の作業ディレクトリ。`~` / 環境変数展開あり。`..` を含むパスは拒否される |
| `exec_timeout_s` | `float` | `600.0`(範囲 `1.0`–`1800.0`) | exec 全体の強制タイムアウト(秒)。`ProviderConfig.timeout_s` とは**別系統**(後者は agent_cli では使われない)。antigravity ではこの値から `--print-timeout` も生成される(下記参照) |
| `allow_file_writes` | `bool` | `false` | ファイル書き込みを許可するか。`false` のときは `sandbox_mode` の値に関わらず read-only にクランプされる |
| `sandbox_mode` | `"read_only" \| "edit" \| "full_auto"` | `"read_only"` | 各 CLI のサンドボックス/承認フラグへマッピングされる(claude は[下表](#sandbox_mode--permission-mode-マッピングclaude)、codex は [codex セクション](#sandbox_mode--codex-フラグのマッピング)、antigravity は [antigravity セクション](#sandbox_mode--antigravity-フラグのマッピング)、grok は [grok セクション](#sandbox_mode--grok-フラグのマッピング)参照) |
| `model` | `str \| null` | `null`(未設定時は `ProviderConfig.model` を使用) | CLI の `--model` / `-m` に渡すモデル名(claude: `opus` / `sonnet` / `haiku` / `fable` 等、codex: `gpt-5.5` 等、antigravity: `"Gemini 3.5 Flash (Low)"` 等の**表示名文字列**、grok: `grok-4.5` 等) |
| `max_turns` | `int \| null` | `8`(範囲 `1`–`50`) | CLI 内部のターン上限。`--max-turns` として渡る。**codex・antigravity には対応する CLI フラグが無いため常に無視される**(この2つでは `exec_timeout_s` + プロセスグループ kill のみが時間上限になる。antigravity はこれに加えて CLI 自身の `--print-timeout` も持つ) |
| `passthrough_env` | `list[str]` | `[]` | 親環境から子プロセスへ転送する環境変数名のallowlist。`ANTHROPIC_API_KEY` はここに書かない限り渡らない |
| `agent_depth_limit` | `int` | `2`(範囲 `1`–`4`) | 再帰ネストの上限。`CODEROUTER_AGENT_DEPTH` が上限以上なら `AdapterError(retryable=False)` で即停止 |

`command` が未設定の場合は `agent` と同名がデフォルトになる。また、`allow_file_writes: true` と `sandbox_mode: read_only` を同時に指定すると、矛盾した設定として**設定読み込み時に `ValueError`** で弾かれる(書き込みを許可したいなら `sandbox_mode` を `edit` か `full_auto` にすること)。

### `sandbox_mode` → `--permission-mode` マッピング(claude)

| `sandbox_mode` | claude `--permission-mode` | 備考 |
|---|---|---|
| `read_only`(既定) | `plan` | ファイル変更なし。`allow_file_writes=false` のとき常にこのモードにクランプされる |
| `edit` | `acceptEdits` | ファイル編集を自動承認 |
| `full_auto` | `acceptEdits` | claude では `edit` と同じマッピング(claude 側に full_auto 相当の別モードは未使用)。grok は `--always-approve` で区別される([grok セクション](#sandbox_mode--grok-フラグのマッピング)参照) |

### `paid: false` の理由

サンプル設定 `examples/providers-agent-cli.yaml` の `agent-claude` プロバイダは `paid: false` になっている。これはサブスクリプション OAuth で運用する限り**従量課金が一切発生しない**(消費するのは後述の5時間窓/週次クォータのみ)ためである。API キー従量課金で運用したい場合は `paid: true` に変更し、CodeRouter 起動時に `ALLOW_PAID=true` を環境変数として渡す必要がある。`ALLOW_PAID` 環境変数は `providers.yaml` に書いた値(`allow_paid`)を**起動時に上書きする**ため、`paid: true` のプロバイダは `ALLOW_PAID` 未設定時にはルーティングから除外される点に注意する。

---

## codex(OpenAI Codex CLI)

v2.7.9(Phase 1b)で `agent: codex` が実装された。claude と同じくプロンプトを **stdin 経由**で渡す方式である(grok のようなファイル経由ではない)。JSONL 出力・`--ephemeral`・git 外ディレクトリ対応など codex 固有の挙動がある。以下は codex CLI **0.144.1**(作者の Mac、2026-07-11 実機検証)を基準とする。

### 設定例

```yaml
providers:
  - name: agent-codex
    kind: agent_cli
    model: gpt-5.5                # 現行フロンティアモデルの例。既定モデルは環境/プラン依存なので明示推奨
    paid: false                   # ChatGPT プランサブスクリプション OAuth 運用 = 従量課金ゼロ
    capabilities:
      streaming: false
      tools: false
    agent_cli:
      agent: codex
      command: codex
      workdir: ~/.coderouter-t/agents/codex
      exec_timeout_s: 600
      allow_file_writes: false
      sandbox_mode: read_only
      max_turns: 8                 # codex には対応フラグが無く無視される(下記参照)
      passthrough_env: []          # OAuth は ~/.codex/auth.json を HOME 継承で読むため空でよい。
                                    # CI で API キーを使う場合のみ CODEX_API_KEY(exec専用)
                                    # または OPENAI_API_KEY を列挙する
```

### アダプタが構築する argv

`sandbox_mode: read_only`(既定)の場合、アダプタは次の argv を構築する。

```
codex exec --json --skip-git-repo-check --ephemeral -m <model> -C <workdir> -s read-only -
```

### プロンプトは stdin 経由(claude と同じ、grok とは異なる)

codex の `exec` サブコマンドは PROMPT 引数を省略する(または明示的に `-` を指定する)と stdin からプロンプトを読む。アダプタは argv 末尾に明示の `-` を置いてこの経路を強制する。grok のように隔離 workdir 内の一時ファイル(`--prompt-file`)を経由する必要はなく、claude と同じ stdin 方式である。

### `--skip-git-repo-check` を常時付与する理由

CodeRouter の隔離 workdir は git リポジトリではない。codex はデフォルトで「信頼済みディレクトリ」チェックを行い、git リポジトリ外かつ未確認のディレクトリでは exit 1 + stderr `Not inside a trusted directory and --skip-git-repo-check was not specified.` で即座に失敗する(実機確認済み)。アダプタはこのフラグを常時付与するため、実運用でこのエラーメッセージが表示されることはない。

### `--ephemeral` を常時付与する理由

`--ephemeral` はセッションをディスクに保存しない。grok の `--no-memory` と同じ理由 — CodeRouter の「1リクエスト = 1ステートレス変換」思想との整合 — で、アダプタは常にこのフラグを付与する(設定で外すことはできない)。

### `sandbox_mode` → codex フラグのマッピング

claude/grok と同様、`allow_file_writes=false` のときは `sandbox_mode` の値に関わらず `read_only` にクランプされる。

| `sandbox_mode` | codex フラグ | 備考 |
|---|---|---|
| `read_only`(既定) | `-s read-only` | ファイル変更なし。`allow_file_writes=false` のとき常にこのモードにクランプされる |
| `edit` | `-s workspace-write` | workspace-write サンドボックス内でのファイル編集を許可 |
| `full_auto` | `-s workspace-write` | **`codex exec` に承認フラグ(`-a` / `--ask-for-approval`)は存在しない**(0.144.1 の `exec --help` に無し。非対話実行のため承認プロンプト自体が無い)。そのため `edit` と同一マッピングになる。`--dangerously-bypass-approvals-and-sandbox` は使わない |

### `--max-turns` / `--timeout` は存在しない

codex exec には `--max-turns` も `--timeout` も存在しない。したがって `AgentCliConfig.max_turns` は **codex では無視される**。時間上限は既存の `exec_timeout_s` + プロセスグループ SIGKILL のみが担う。

### JSONL 出力と usage 正規化

`--json` の出力は JSONL(1行1イベント)であり、進捗は stderr、イベント列(最終応答を含む)は stdout に出る(公式ドキュメント・実機とも確認済み)。実機での one-shot 実行例:

```
$ codex exec --json --skip-git-repo-check "1+1は?数字だけ答えて"
{"type":"thread.started","thread_id":"019f4e74-08fd-77b2-9cc6-9afa744df130"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"2"}}
{"type":"turn.completed","usage":{"input_tokens":13810,"cached_input_tokens":9984,"output_tokens":5,"reasoning_output_tokens":0}}
```

- 最終回答: **最後の** `item.completed` かつ `item.type=="agent_message"` の `item.text`。
- usage 正規化: `turn.completed.usage` の `input_tokens` → `prompt_tokens`、`output_tokens` → `completion_tokens`、両者の和を `total_tokens` とする。`cached_input_tokens` は `input_tokens` の**部分集合**であり(実測: input 13810 ⊃ cached 9984)、加算せず `prompt_tokens_details.cached_tokens` として保持する。`reasoning_output_tokens` が 0 より大きい場合は `completion_tokens_details.reasoning_tokens` として保持する(防御的)。複数の `turn.completed` が出た場合は合算する。
- `thread_id`(`thread.started`)はレスポンスメタデータ `coderouter_session_id` に写す。
- `error` イベントまたは `turn.failed` を検出した場合、あるいは agent_message が1つも得られない/stdout が空/全行が非JSON の場合は retryable `AdapterError` としてフォールバックチェーンを次のプロバイダへ進める。JSONL の個々の行が非JSON でも他の行の処理は続行する(stderr 混入等への防御)。

### 認証(ChatGPT プランサブスクリプション OAuth / API キー)

codex CLI は ChatGPT プランの OAuth ログインに対応している。資格情報は `~/.codex/auth.json`(または OS キーチェーン)に保存され、アダプタの `HOME` 継承によって `passthrough_env: []` のままで動作する。

1. `codex login` を実行してログインを済ませる。`codex login status` が exit 0 を返せばログイン済みと確認できる。
2. OAuth トークンは**約8日で stale** になる。使用時に自動リフレッシュされるが、長期間 codex を呼び出さない構成では stale のまま失敗することがあるため、定期的に codex を実行するか再ログインしておくことを推奨する。

CI などで API キー従量課金を使う場合は、`CODEX_API_KEY`(**exec 専用**)または `OPENAI_API_KEY`(一般)を `passthrough_env` に列挙する。`CODEX_HOME` で config/資格情報ディレクトリ自体を上書きすることも可能。

### エラー報告

codex CLI は成功時に終了コード 0、失敗時は非ゼロ終了コード + stderr にエラーテキストを出力する(例: git 外チェック失敗時のメッセージ)。JSONL 内に `error` イベントや `turn.failed` が含まれる場合もあり、これらも retryable な `AdapterError` としてフォールバックチェーンを次のプロバイダへ進める。

### pre-1.0 であることの注意

codex CLI は pre-1.0 でほぼ毎日リリースされている。`--json` の別名は依然として `--experimental-json` のままであり、JSONL スキーマは未凍結である。**バージョンの pin を推奨する**(`command` に固定版バイナリのフルパスを指定できる)。スキーマ変化が起きても防御的パースにより retryable な `AdapterError` となり、フォールバックチェーンの次のプロバイダへ降格する。

---

## antigravity(Google Antigravity CLI)

v2.7.10(Phase 1c)で `agent: antigravity` が実装され、これで **Phase 1(claude・codex・antigravity・grok の4バックエンド)は完了**した。当初計画されていた `gemini` は Google が個人アカウント向け提供を終了したため、後継の Antigravity CLI(コマンド `agy`)を対象に実装している。詳しい経緯は [gemini の廃止と antigravity への移行](#gemini-の廃止と-antigravity-への移行) を参照。プロンプトを **argv 値として**渡す点(claude/codex の stdin、grok の `--prompt-file` とはいずれも異なる)、プレーンテキスト出力(usage は常にゼロ)、そして CLI 自身がタイムアウトを持つ唯一のエージェントである点が固有の挙動である。以下は agy **1.1.1**(作者の Mac、2026-07-11 実機検証)を基準とする。

### 設定例

```yaml
providers:
  - name: agent-antigravity
    kind: agent_cli
    model: "Gemini 3.5 Flash (Low)"   # 表示名文字列(API IDではない。下記参照)
    paid: false                       # Google アカウント OAuth 運用(無料枠含む) = 従量課金ゼロ
    capabilities:
      streaming: false
      tools: false
    agent_cli:
      agent: antigravity
      # command は省略可(既定 "agy" — バイナリ名が製品名と異なる点に注意)
      workdir: ~/.coderouter-t/agents/antigravity
      exec_timeout_s: 600
      allow_file_writes: false
      sandbox_mode: read_only
      passthrough_env: []             # OAuth は OS キーリング + ~/.gemini/antigravity-cli/ を
                                       # HOME(macOS では USER も)継承で読むため空でよい。
                                       # API キー env は UNCONFIRMED(下記参照)
```

### アダプタが構築する argv

`sandbox_mode: read_only`(既定)の場合、アダプタは次の argv を構築する。

```
agy -p <prompt> --model "Gemini 3.5 Flash (Low)" --mode plan --print-timeout 600s
```

### プロンプトは argv 値経由(claude/codex の stdin・grok の `--prompt-file` のいずれとも異なる)

4エージェントのプロンプト配送方式は3パターンに分かれる。

| agent | 配送方式 | 備考 |
|---|---|---|
| claude | stdin | `-p` は引数省略時に stdin を読む |
| codex | stdin(argv 末尾に明示 `-`) | claude と同じ経路 |
| grok | `--prompt-file`(隔離 workdir 内 0600 一時ファイル) | argv 値・stdin いずれも不可なため |
| antigravity | argv 値(`-p <prompt>`) | stdin 不可・`--prompt-file` 相当のフラグも無い |

agy の `-p` / `--print` / `--prompt` は値必須のフラグであり、stdin からプロンプトを読む経路も、grok のような `--prompt-file` 相当のフラグも存在しない。したがってプロンプトは argv の値として渡すほかなく、Linux の `MAX_ARG_STRLEN`(約128KiB)というサイズ上限と、`ps` からプロンプト全文が見えてしまう既知の制限が残る(他に選択肢が無いためドキュメント化するのみ)。argv はリスト形式で渡され(`shell=True` は使わない)、シェル解釈は経由しない。

### stdin にパイプするとハングする — agy を直接叩く場合の注意

アダプタ自身は stdin に何も書き込まず即座にクローズする(`communicate(input=None)`、実機で `</dev/null` により正常動作することを確認済み)ため CodeRouter 経由では問題にならない。しかし **agy に stdin から何かをパイプすると応答が返らずハングする**ことが実機で確認されている(`printf '...' | agy -p "..."` は応答待ちのまま `Error: timeout waiting for response` になる)。agy は stdin をコンテキストとして読む機能を持たない。CLI を直接スクリプトで叩く場合は、必ず stdin を空にする(`</dev/null` 等)こと。

### `sandbox_mode` → antigravity フラグのマッピング

claude/codex/grok と同様、`allow_file_writes=false` のときは `sandbox_mode` の値に関わらず `read_only` にクランプされる。

| `sandbox_mode` | agy フラグ | 備考 |
|---|---|---|
| `read_only`(既定) | `--mode plan` | ファイル変更なし。`allow_file_writes=false` のとき常にこのモードにクランプされる |
| `edit` | `--mode accept-edits` | ファイル編集を自動承認 |
| `full_auto` | `--mode accept-edits --dangerously-skip-permissions` | 全ツール実行を自動承認(agy の `--help` に列挙されているモード値は `accept-edits`/`plan` の2値のみ。full_auto は `--dangerously-skip-permissions` の追加で表現する) |

### プレーンテキスト出力と usage 常時ゼロ

agy には `--output-format` 系のフラグが**存在しない**。出力はプレーンテキストのみである。アダプタは stdout を UTF-8 デコードした上で ANSI エスケープを防御的に除去(regex)し、前後の空白を strip して最終回答とする。空文字列になった場合は retryable な `AdapterError` を送出する。JSON 出力が無いためトークン usage・セッション ID・構造化エラーはいずれも取得不能であり、grok と同様に usage は**常にゼロ**で報告される(`coderouter_cost_usd` も `ProviderConfig.cost` に単価を設定しない限り 0 のまま)。レスポンスメタデータにセッション ID は入らない。

### `--print-timeout` — CLI 側タイムアウトを持つ初めてのエージェント

agy は `--print-timeout <Go duration>`(既定 5m0s)という print モード自身の待ち時間上限フラグを持つ。claude/codex/grok にはこの種の CLI 側タイムアウトが無い。アダプタは `AgentCliConfig.exec_timeout_s` から `--print-timeout` を生成し(例: `exec_timeout_s=600` → `--print-timeout 600s`)、CLI 自身に自己終了させる一次防壁とする。これに加えて、従来どおり外側の `asyncio.wait_for` + プロセスグループ SIGKILL による二次防壁も維持しており、antigravity は**二重のタイムアウト**を持つ唯一のエージェントになる。

### `max_turns` は無視される

agy には `--max-turns` に相当するフラグが無い。`AgentCliConfig.max_turns` は antigravity では常に無視され(codex と同様)、時間上限は `--print-timeout` と `exec_timeout_s` の二重防壁のみが担う。

### 認証(Google アカウント OAuth・無料枠可)

agy は Google アカウント OAuth に対応しており、無料枠でも動作する。資格情報は OS キーリングを優先し、`~/.gemini/antigravity-cli/`(`credentials.enc` / `settings.json`)にも保管される。アダプタの `HOME` 継承(macOS では `USER` も)によって `passthrough_env: []` のまま headless 動作する(既存の `_build_child_env()` で対応済み)。セットアップ手順:

1. `agy` を初回実行するとブラウザでの Google アカウントログインが始まる。ログインを済ませておく。
2. `agy models` を実行し、モデル一覧が返ることをスモーク確認する。

API キー経由の認証(環境変数名)は情報が錯綜しており(`ANTIGRAVITY_API_KEY` 説あり・`GEMINI_API_KEY` は無視されるという説もあり)**未確認(UNCONFIRMED)**。本ドキュメントでは断定しない。

### モデル名は表示名文字列 — Claude モデルまで届く

`--model` に渡す値は API ID ではなく、`agy models` が返す**表示名文字列**である。実機(agy 1.1.1)で確認された一覧の例:

```
Gemini 3.5 Flash (Medium/High/Low)
Gemini 3.1 Pro (Low/High)
Claude Sonnet 4.6 (Thinking)
Claude Opus 4.6 (Thinking)
GPT-OSS 120B (Medium)
```

面白いことに、agy 経由では Google 以外のモデル、たとえば Claude Opus 4.6 まで呼び出せる。`providers.yaml` の `model` フィールドには、このいずれかの表示名文字列をそのまま(空白・括弧を含めて)書くこと。

### early-days プロダクトであることの注意

Antigravity CLI は登場したばかりのプロダクトである。フラグ体系・モデル一覧は今後変わる可能性が高いため、**バージョンの pin を推奨する**(`command` に固定版バイナリのフルパスを指定できる)。プレーンテキスト出力の性質上 JSON スキーマ変化のようなパース事故は起きにくいが、想定外の空応答・非零終了コードは防御的に扱われ、retryable な `AdapterError` としてフォールバックチェーンの次のプロバイダへ降格する。

---

## grok(Grok CLI)

v2.7.8(Phase 1d)で `agent: grok` が実装された。claude と同じワンショット exec 方式だが、プロンプトの渡し方・クロスセッションメモリの無効化・usage 報告の各点で grok 固有の挙動がある。以下は grok CLI **v0.2.93**([stable] チャネル、2026-07-10 実機検証)を基準とする。

### 設定例

```yaml
providers:
  - name: agent-grok
    kind: agent_cli
    model: grok-4.5              # 現行インストールの既定モデル。`grok models` で一覧確認
    paid: false                  # サブスクリプション OAuth 運用 = 従量課金ゼロ
    capabilities:
      streaming: false
      tools: false
    agent_cli:
      agent: grok
      command: grok
      workdir: ~/.coderouter-t/agents/grok
      exec_timeout_s: 600
      allow_file_writes: false
      sandbox_mode: read_only
      max_turns: 8
      passthrough_env: []        # OAuth は ~/.grok/auth.json を HOME 継承で読むため空でよい。
                                 # CI で API キーを使う場合のみ GROK_CODE_XAI_API_KEY を列挙
```

### アダプタが構築する argv

`sandbox_mode: read_only`(既定)の場合、アダプタは次の argv を構築する。

```
grok --prompt-file <workdir>/.coderouter-prompt-<uuid>.txt \
     --output-format json -m <model> --cwd <workdir> \
     --max-turns <N> --no-memory \
     --sandbox read-only --permission-mode plan
```

### プロンプトはファイル経由で渡す(`--prompt-file`)

grok の `-p` / `--single` はプロンプトを **argv の値としてしか受け取らない**(stdin をプロンプトとして受け付けないことを実 CLI で確認済み)。argv に巨大なプロンプトを載せると Linux の `MAX_ARG_STRLEN`(約 128KiB)の上限に当たるうえ、`ps` からプロンプト全文が見えてしまう。そのためアダプタは隔離 workdir 内にパーミッション `0600` の一時ファイル(`.coderouter-prompt-<uuid>.txt`)としてプロンプトを書き出し、`--prompt-file` で渡す。この一時ファイルは exec 終了後に**必ず削除される**(タイムアウト・エラー経路を含む)。

### `sandbox_mode` → grok フラグのマッピング

claude と同様、`allow_file_writes=false` のときは `sandbox_mode` の値に関わらず `read_only` にクランプされる。

| `sandbox_mode` | grok フラグ | 備考 |
|---|---|---|
| `read_only`(既定) | `--sandbox read-only --permission-mode plan` | ファイル変更なし。`allow_file_writes=false` のとき常にこのモードにクランプされる |
| `edit` | `--sandbox workspace --permission-mode acceptEdits` | workspace サンドボックス内でのファイル編集を自動承認 |
| `full_auto` | `--sandbox workspace --always-approve` | claude と異なり grok では `edit` と区別されたマッピングになる |

### `--no-memory` を常に付与する

grok CLI はセッションをまたぐメモリ機能を持つ。前回呼び出しの記憶が次の応答へ漏れることは CodeRouter の「1リクエスト = 1ステートレス変換」思想と衝突するため、アダプタは**常に** `--no-memory` を付与してこれを無効化する(設定で外すことはできない)。

### JSON 出力と usage / cost

`--output-format json` の出力は単一 JSON オブジェクト `{"text", "stopReason", "sessionId", "requestId", "thought"?}` である(grok v0.2.93 で確認)。`text` が最終回答として、`sessionId` がレスポンスメタデータ `coderouter_session_id` として返る。**トークン usage・コストのフィールドは存在しない**ため、usage はすべてゼロで報告され、`coderouter_cost_usd` も運用者が `ProviderConfig.cost` に単価を設定しない限り 0 のままである(claude が `total_cost_usd` を直接出力するのとは対照的)。JSON は防御的にパースされ、想定外の形は `AdapterError(retryable=True)` としてフォールバックチェーンを次のプロバイダへ進める。

### 認証(サブスクリプション OAuth / API キー)

grok CLI は OAuth によるサブスクリプションログインに対応している(SuperGrok / X Premium+)。資格情報は `~/.grok/auth.json` に保存され(7日で失効・自動リフレッシュあり。`GROK_HOME` で保管場所を上書き可能)、アダプタの `HOME` 継承によって `passthrough_env: []` のままで OAuth が機能する。セットアップ手順:

1. `grok login` を実行してサブスクリプションログインを済ませる。
2. `grok models` でモデル一覧が返ることをスモーク確認する。現行インストールでは `grok-4.5`(既定)と `grok-composer-2.5-fast` が返る。

CI などで API キー従量課金を使う場合のみ、`passthrough_env: [GROK_CODE_XAI_API_KEY]` を列挙する。環境変数名は **`GROK_CODE_XAI_API_KEY` であり `XAI_API_KEY` ではない**点に注意。API キーが渡っている場合は OAuth より優先される。

### エラー報告

grok CLI は成功時に終了コード 0、認証・ネットワーク・実行時エラーでは終了コード 1 でエラーテキストを **stderr** に出力する。アダプタは stderr の末尾を `AdapterError` メッセージに含めるため、表示されたメッセージをそのまま手がかりにできる。

### early beta であることの注意

grok CLI は early beta である(v0.2.93 [stable] チャネル、2026-07-10 時点)。JSON スキーマが今後変わる可能性があるため、**バージョンの pin を推奨する**(`command` に固定版バイナリのフルパスを指定できる)。スキーマ変化が起きた場合も防御的パースにより retryable な `AdapterError` となり、フォールバックチェーンの次のプロバイダへ降格する。

---

## 制限事項

| 制限 | 内容 |
|---|---|
| **one-shot のみ** | セッション継続(resume)は非対応。呼び出しごとに新しい CLI プロセスが起動し、前回のやり取りは引き継がれない(設計方針として意図的に非スコープ) |
| **擬似ストリーミング** | CLI 側にトークン単位の安定したストリーム出力面が無いため、`generate()` の最終テキストを固定サイズのチャンクに分割して順に yield するだけの擬似ストリームになる。サンプル設定でも `capabilities.streaming: false` を明示している(既定 `true` の上書きが必須) |
| **plan モードの色が付くことがある** | 既定の `sandbox_mode: read_only` は `--permission-mode plan` にマップされる。plan モードは本来インタラクティブな人間によるレビュー UI 向けの応答形式であり、one-shot 実行では実際の変更を伴わない「計画の説明」寄りの文面が返ってくることがある |
| **サブスクのクォータを消費する** | Claude Code サブスクリプションの5時間窓/週次クォータを消費する。API 課金がゼロでも無制限に呼べるわけではない |
| **再帰上限あり** | `agent_depth_limit`(既定2、最大4)を超えるネスト呼び出しは拒否される。エージェント CLI が内部で CodeRouter を呼び返すような構成を組む場合は特に注意 |

---

## トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| `Not logged in · Please run /login` で失敗する | claude CLI がその実行ユーザー/環境でログイン状態にない | まず `claude` を対話起動して `/login` の状態を確認する。macOS でヘッドレス実行している場合、v2.7.7 以前は `USER` 環境変数が子プロセスに渡らずこのエラーになっていたが、v2.7.7 で修正済み(`USER` / `LOGNAME` を継承するようになった) |
| リクエストが `paid gate blocked` 相当で弾かれる/ルーティングされない | `agent_cli` プロバイダが `paid: true` なのに `ALLOW_PAID` が立っていない | サブスク運用なら `paid: false` にする。API キー従量課金で使うなら CodeRouter 起動時に `ALLOW_PAID=true` を設定する |
| `claude exited 1: ...` のエラーメッセージに具体的な理由が出る | claude CLI は認証エラー等を **stdout** に `is_error: true` の JSON として出力し(stderr は空のまま)終了コード1で終わることがある | v2.7.7 の `_error_detail()` は stderr が空でも stdout の `is_error` JSON から `result` フィールド(実際のエラー文言、例: `Not logged in · Please run /login`)を優先的に拾ってエラーメッセージに含めるようになっている。表示されたメッセージをそのまま手がかりにできる |
| `grok exited 1: ...` で失敗する | grok CLI は認証・ネットワーク・実行時エラーを終了コード1で終わり、エラーテキストを stderr に出す | アダプタが stderr の末尾を `AdapterError` に含めるので、そのメッセージを手がかりにする。認証エラーなら `grok login` を再実行し、`grok models` が通ることをスモーク確認する |
| `codex exited 1: ...` で失敗する(OAuth の stale を疑う場合) | codex の OAuth トークンは約8日で stale になる。使用時自動リフレッシュされるが、長期間呼び出しが無いと stale のまま失敗することがある | `codex login status` でログイン状態を確認し、必要なら `codex login` を再実行する。定期的に codex を実行しておくと stale 化を避けやすい |
| `Not inside a trusted directory and --skip-git-repo-check was not specified.` が表示される | 通常は発生しない — アダプタは `--skip-git-repo-check` を常時付与するため | もし表示された場合は CodeRouter 側の argv 構築にバグがある可能性が高い。バージョンを確認し、再現するなら issue を報告する |
| `agent: gemini` を指定すると `AdapterError` になる/`IneligibleTierError` が出る | Google が2026年6月に個人アカウント向け Gemini CLI 提供を終了したため(実機で `IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals...` を確認) | `agent: antigravity` に切り替える。設定例は [antigravity(Google Antigravity CLI)](#antigravitygoogle-antigravity-cli) セクションを参照 |
| `agy` が応答を返さずハングする | agy に stdin をパイプしている(例: `printf '...' \| agy -p "..."`) | stdin をパイプしない。何も書かず `</dev/null` にリダイレクトする。CodeRouter 経由の呼び出しではアダプタが既にこの対策(stdin 即クローズ)を取っている |
| CLI 起動に失敗する(`failed to launch ...`) | `command`(既定は `agent` と同名。ただし antigravity のみ既定 `agy`)が `PATH` 上に無い | `claude --version` / `codex --version` / `agy --version` / `grok --version` が通ることを確認する。フルパスを `command` に指定してもよい |
| `kind: agent_cli` の provider があるのに `coderouter-t serve` が起動時にエラーで停止する | v2.9.0(Phase 2c)で in-core `agent_cli` アダプタが削除され、`coderouter-plugin-agents` の導入 + `plugins.enabled: [agents]` が必須になった | `uv pip install "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"`(または pip、あるいは `uv tool install coderouter-t --with "coderouter-plugin-agents @ git+https://github.com/zephel01/coderouter-plugin-agents"`)を実行し、`providers.yaml` に `plugins.enabled: [agents]` を追記する。起動時のエラーメッセージ自体にも同じ移行手順(インストール + `plugins.enabled`)が案内される |
| `coderouter doctor` が `agent_cli` 関連の設定警告を出す | `kind: agent_cli` の provider があるのに `plugins.enabled` に `agents` が含まれていない(または plugin 未インストール) | 上記と同じインストールコマンド + `plugins.enabled: [agents]` の追記で解消する。`doctor` の出力にも修正用の yaml スニペットが表示される |

---

## 関連ドキュメント

- [外部エージェントアダプタ 設計ドキュメント](../designs/external-agents-adapter.md) — 認証設計・argv構築・セキュリティ要件の詳細
- [`examples/providers-agent-cli.yaml`](../../examples/providers-agent-cli.yaml) — 実際に動く設定例
- [シークレット管理とセキュリティ方針](../guides/security.md)

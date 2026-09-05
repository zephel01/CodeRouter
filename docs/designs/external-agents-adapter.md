# 外部コーディングエージェント統合アダプタ設計書（`kind="agent_cli"`）

> 対象バージョン: CodeRouter v2.x 系 / Phase 1（in-core adapter）
> 関連: `docs/inside/future.md` §1.2・§5（ローカル専用 / 非公開）、[`docs/backends/launcher.md`](../backends/launcher.md)
> ステータス: 設計確定 / Phase 1(1a claude・1b codex・1c antigravity・1d grok)実装済み・1c は antigravity として実装
>
> ⚠️ **本書が記述する in-core 実装は現存しません（2026-08-10 追記）。** Phase 2 として
> v2.8.0（Adapter Protocol 配線）→ v2.8.1（`coderouter-plugin-agents` へ移設）→
> **v2.9.0 で in-core `AgentCliAdapter` は削除**されました（BREAKING）。現在 `kind: agent_cli` を
> 使うには外部プラグイン `coderouter-plugin-agents` の導入が必須です。
> 移設の設計は [`agent-cli-plugin-extraction.md`](./agent-cli-plugin-extraction.md)、
> 利用者向けの説明は [`docs/backends/external-agents.md`](../backends/external-agents.md) を参照。
> 本書は **CLI の認証・argv・出力仕様の一次記録**、および plugin 側の設計典拠として残しています。

本設計書は、OpenAI Codex CLI・Google Gemini CLI・xAI Grok・Anthropic Claude Code CLI の4つの外部コーディングエージェントを、CodeRouter の新しいアダプタ種別 `kind="agent_cli"` として本体に組み込む方式を定義するものである。CLI のフラグ・認証仕様はすべて調査レポート（`RESEARCH-cli.md`、2026-07 時点）を、コード上の拡張点はすべてコード解析報告書（`ANALYSIS-code.md`）を典拠とする。ユーザー確定事項（`DECISIONS.md`）は拘束条件であり、本書はそれを厳密に実装する。

---

## 1. 概要と目的

### 1.1 何を作るか

CodeRouter 本体に、外部コーディングエージェント CLI をワンショット（one-shot exec）で呼び出す in-core アダプタ `AgentCliAdapter` を新設する。以下を満たす。

- アダプタ種別は `kind="agent_cli"` の**1アダプタ・複数ターゲット方式**である。`openai_compat` アダプタが多数のバックエンドを1クラスで捌く前例（`ANALYSIS-code.md` §4-1）に倣い、`agent` フィールド（`codex` / `gemini` / `grok` / `claude`）で分岐する。
- 呼び出しは**ワンショット exec のみ**である（`codex exec` / `gemini -p` / `claude -p` / `grok -p` もしくは Grok は公式 API 直）。セッション継続（resume）・対話モードは扱わない（`DECISIONS.md` #2）。
- 認証は**サブスクリプション優先**である。子プロセス環境は親環境を継承せず、明示 allowlist で最小注入する（`DECISIONS.md` #4）。

### 1.2 なぜ作るか

Claude Code などのオーケストレータから見たとき、CodeRouter は OpenAI 互換 / Anthropic 互換のエンドポイントを1つ提供する透過プロキシである。本機能により、オーケストレータは他社エージェント（Codex・Gemini・Grok）を**「1つのモデル」**として、`model` 名または `X-CodeRouter-Profile` ヘッダを指定するだけで透過的に利用できる。ユーザーは各社 CLI の起動コマンド・フラグ・認証差を意識せず、`providers.yaml` に1エントリ足すだけで済む。

### 1.3 future.md 思想との整合

`future.md` の核心価値は「透過性」と「1リクエスト=1変換のステートレス性」である（`ANALYSIS-code.md` §2、future.md L647）。コーディングエージェントは本来ステートフル・マルチターン・FS 編集を伴う制御ループであり、この思想と衝突する。しかし**ワンショット exec に限定すれば「プロンプト in → テキスト out」の1変換に落とし込め**、思想と整合する（`ANALYSIS-code.md` §2）。オーケストレーション本体は CodeRouter の「クライアント」＝別プロセス側に置くという future.md §5.2 の結論も維持される。CodeRouter はあくまで1変換を担うだけであり、エージェント間協調は行わない。

---

## 2. スコープ / 非スコープ

### 2.1 スコープ（Phase 1）

| 項目 | 内容 |
|---|---|
| 実装形態 | in-core adapter（`kind="agent_cli"`）を CodeRouter 本体へ実装 |
| 対象エージェント | codex / gemini / grok / claude の4種 |
| 呼び出し | ワンショット exec（`generate()`）と擬似ストリーム（`stream()`） |
| 認証 | サブスク優先 + 子プロセス env allowlist |
| 安全制御 | allowlist argv・既定 read-only・workdir 境界・timeout 強制 kill・再帰上限 |

### 2.2 非スコープ

以下は本フェーズでは扱わない。将来拡張として言及するにとどめる。

| 非スコープ項目 | 理由 / 扱い |
|---|---|
| セッション継続（resume） | `DECISIONS.md` #2。ステートフル化は思想と衝突。将来拡張として §10 に記載のみ |
| 対話モード（TTY） | 非対話面のみを使う。TTY スクレイピングは禁止（`RESEARCH-cli.md` §7） |
| CodeRouter の MCP サーバー化 | 依存最小主義（Core 5 deps）違反（`ANALYSIS-code.md` §3-D） |
| マルチターン協調・エージェント間オーケストレーション | future.md §5.2 によりクライアント側の責務 |
| Plugin SDK 経由の Adapter フック配線 | Adapter Protocol は未配線（`ANALYSIS-code.md` §1）。**Phase 2** として §9 に別掲 |

---

## 3. ユーザーストーリー

運用者は `providers.yaml` に agent_cli プロバイダを記述し、専用プロファイルに割り当てる。Claude Code などのクライアントは `X-CodeRouter-Profile: codex` ヘッダ、または `model_pattern` により `model` 名で当該プロファイルへ到達する。

### 3.1 providers.yaml 記述例（4エージェント分）

```yaml
allow_paid: true                 # grok API 直や有料サブスク利用時に必要

providers:
  # ---- Claude Code CLI（Phase 1a・最優先）----
  - name: agent-claude
    kind: agent_cli
    model: opus                  # CLI に渡す --model 値（opus|sonnet|haiku|fable）
    paid: true
    capabilities:
      streaming: false            # 既定 true のため明示上書き必須（§7）
    agent_cli:
      agent: claude
      command: claude            # PATH 解決。絶対パスも可
      workdir: ~/.coderouter-t/agents/claude
      exec_timeout_s: 600
      allow_file_writes: false   # 既定 read-only
      max_turns: 8
      sandbox_mode: read_only
      passthrough_env: [CLAUDE_CODE_OAUTH_TOKEN]   # setup-token（任意）

  # ---- OpenAI Codex CLI（Phase 1b）----
  - name: agent-codex
    kind: agent_cli
    model: gpt-5.5
    paid: true
    capabilities:
      streaming: false
    agent_cli:
      agent: codex
      command: codex
      workdir: ~/.coderouter-t/agents/codex
      exec_timeout_s: 600
      allow_file_writes: false
      sandbox_mode: read_only
      passthrough_env: []        # サブスク OAuth は ~/.codex から読む

  # ---- Google Gemini CLI（Phase 1c）----
  - name: agent-gemini
    kind: agent_cli
    model: gemini-2.5-pro
    paid: true
    capabilities:
      streaming: false
    agent_cli:
      agent: gemini
      command: gemini
      workdir: ~/.coderouter-t/agents/gemini
      exec_timeout_s: 600
      allow_file_writes: false
      max_turns: 8               # settings.json の maxSessionTurns 相当
      sandbox_mode: read_only
      passthrough_env: [GEMINI_API_KEY]   # OAuth キャッシュが無い場合の保険

  # ---- xAI Grok（Phase 1d・CLI 経路）----
  - name: agent-grok
    kind: agent_cli
    model: grok-code-fast-1
    paid: true
    capabilities:
      streaming: false
    agent_cli:
      agent: grok
      command: grok
      workdir: ~/.coderouter-t/agents/grok
      exec_timeout_s: 600
      allow_file_writes: false
      max_turns: 8
      sandbox_mode: read_only
      passthrough_env: [XAI_API_KEY]      # grok は API 課金必須（サブスク非対応）

profiles:
  - name: codex
    providers: [agent-codex]
  - name: gemini
    providers: [agent-gemini]
  - name: grok
    providers: [agent-grok]
  - name: claude-agent
    providers: [agent-claude]

# model 名でも到達可能にする（任意）
auto_router:
  rules:
    - id: user:route-codex
      profile: codex
      match: { model_pattern: "codex.*" }
```

> 補足: Grok は公式 API が OpenAI 互換（`https://api.x.ai/v1`）であるため、CLI を使わず既存 `kind="openai_compat"` プロバイダとして `grok-code-fast-1` を直接叩く方が安定である（`RESEARCH-cli.md` §3、`DECISIONS.md` #5）。上記 agent_cli 版は CLI 統一運用を望む場合の代替経路である（§9 Phase 1d 参照）。

### 3.2 呼び出しの流れ

Claude Code 側の接続例。

```bash
# プロファイルヘッダで codex エージェントに到達
ANTHROPIC_BASE_URL=http://localhost:8088 \
ANTHROPIC_AUTH_TOKEN=dummy \
  claude
# → リクエストヘッダ X-CodeRouter-Profile: codex を付与すると
#   CodeRouter が codex exec を1回起動し、最終回答テキストを1変換で返す
```

クライアントから見えるのは通常の OpenAI/Anthropic 応答であり、背後で外部 CLI が動いていることは透過である。

---

## 4. アーキテクチャ

### 4.1 構成図

```
 ┌────────────────┐   OpenAI/Anthropic 互換    ┌──────────────────────────────┐
 │ Claude Code /  │ ────── HTTP ────────────►  │ CodeRouter ingress            │
 │ Cursor 等      │   X-CodeRouter-Profile     │  openai_routes / anthropic    │
 └────────────────┘                            └──────────────┬───────────────┘
                                                              │ ChatRequest
                                                              ▼
                                              ┌──────────────────────────────┐
                                              │ routing/fallback.py           │
                                              │  register_provider / _adapters│
                                              │  guards（tool_loop /          │
                                              │  context_budget / memory）    │
                                              └──────────────┬───────────────┘
                                                              │ build_adapter(provider)
                                                              ▼
                                              ┌──────────────────────────────┐
                                              │ AgentCliAdapter(BaseAdapter)  │
                                              │  agent=codex|gemini|grok|claude│
                                              │  generate() / stream() /      │
                                              │  healthcheck()                │
                                              └──────────────┬───────────────┘
                                                              │ create_subprocess_exec
                                                              │ （allowlist argv・env allowlist・PGID）
                                                              ▼
                        ┌──────────┬──────────┬──────────┬──────────────┐
                        │ codex    │ gemini   │ grok     │ claude        │
                        │ exec     │ -p       │ -p / API │ -p            │
                        │ --json   │ --output │ --output │ --output      │
                        │          │ -format  │ -format  │ -format json  │
                        └──────────┴──────────┴──────────┴──────────────┘
                              ▲ 認証は各 CLI の資格情報ディレクトリ
                              │ ~/.codex ~/.gemini ~/.claude（OAuth）
```

### 4.2 AgentCliAdapter の位置づけ

`AgentCliAdapter` は `coderouter/adapters/base.py` の `BaseAdapter`（L162）を継承する。既存の fallback / profile / guards / cost / `register_provider` の資産を無改造で継承できる（`ANALYSIS-code.md` §3-A・§4-4）。`build_adapter`（`adapters/registry.py` L11）に1分岐を足すだけで配線される。

### 4.3 リクエストフロー

1. ingress が `ChatRequest`（`base.py` L43）を構築。
2. `routing/fallback.py` がプロファイルの chain を解決し、既存 guards を wire 層で適用（`ANALYSIS-code.md` §5）。
3. `register_provider`（fallback.py L1176）が `_adapters` キャッシュ（L1106-1108）から `AgentCliAdapter` を取得。
4. `generate()` が `ChatRequest.messages` からプロンプト文字列を組み立て、CLI を1回 exec、最終回答を `ChatResponse`（L70）へ整形して返す。
5. 失敗は `AdapterError`（L104、`retryable` フラグ付き）で送出し、fallback エンジンが次プロバイダへ降格する。

---

## 5. 詳細設計

### 5.1 `coderouter/adapters/agent_cli.py`（新規）

#### 5.1.1 クラス設計

```python
class AgentCliAdapter(BaseAdapter):
    """外部コーディングエージェント CLI を one-shot exec で呼ぶアダプタ。

    agent フィールドで codex / gemini / grok / claude を分岐する
    1アダプタ・複数ターゲット方式（openai_compat.py の前例に倣う）。
    """

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self.acfg: AgentCliConfig = config.agent_cli  # 必須（検証済み）
        # agent → argv ビルダ / パーサのディスパッチ表
        self._builders = {
            "codex": self._build_codex_argv,
            "gemini": self._build_gemini_argv,
            "grok": self._build_grok_argv,
            "claude": self._build_claude_argv,
        }
        self._parsers = {
            "codex": self._parse_codex,
            "gemini": self._parse_gemini,
            "grok": self._parse_grok,
            "claude": self._parse_claude,
        }
```

#### 5.1.2 `healthcheck()` 擬似コード

CLI バイナリの存在確認のみを行う（`ANALYSIS-code.md` §4-1、`base.py` L239）。実 API を叩かない軽量チェックである。

```python
async def healthcheck(self) -> bool:
    import shutil
    return shutil.which(self.acfg.command) is not None
```

#### 5.1.3 `generate()` 擬似コード

```python
async def generate(self, request, *, overrides=None) -> ChatResponse:
    # 注意: 継承元の effective_timeout()（base.py L222）はオーバーライド未設定時に
    # self.config.timeout_s（ProviderConfig.timeout_s）へフォールバックするため、
    # そのまま呼ぶと exec_timeout_s が無視される。agent_cli は独自に解決する。
    timeout = (
        overrides.timeout_s
        if overrides is not None and overrides.timeout_s is not None
        else self.acfg.exec_timeout_s
    )
    prompt = self._render_prompt(request, overrides)     # messages → 1本の文字列
    argv = self._builders[self.acfg.agent](prompt)       # allowlist argv
    env = self._build_child_env()                         # env allowlist 注入（§5.3）
    cwd = self._resolve_workdir()                         # workdir 境界検証（§6）

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=PIPE, stdout=PIPE, stderr=PIPE,
        cwd=cwd, env=env,
        start_new_session=True,     # 新プロセスグループ（PGID kill 用）
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode() if self._uses_stdin else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        self._kill_process_group(proc)   # os.killpg(os.getpgid(pid), SIGKILL)
        raise AdapterError(f"{self.acfg.agent} exec timed out after {timeout}s",
                           provider=self.name, retryable=True)

    if proc.returncode != 0:
        raise AdapterError(f"{self.acfg.agent} exit={proc.returncode}: {stderr[-2000:]!r}",
                           provider=self.name,
                           status_code=None,
                           retryable=self._is_retryable(proc.returncode))

    final_text, usage, cost = self._parsers[self.acfg.agent](stdout, stderr)
    return self._to_chat_response(request, final_text, usage, cost)
```

#### 5.1.4 `stream()` 擬似コード（擬似ストリーム）

CLI によっては安定した `stream-json` 面が無い（Codex は JSONL だが最終メッセージ抽出前提、Gemini の stream-json は pre-1.0）。本フェーズは**擬似ストリーム**方針を採る。`generate()` の最終テキストを1つ以上の `StreamChunk`（`base.py` L86）に分割して yield する。`capabilities.streaming=false` を既定とし（§7）、クライアントが SSE を要求した場合のみ擬似的に応答する。

```python
async def stream(self, request, *, overrides=None) -> AsyncIterator[StreamChunk]:
    resp = await self.generate(request, overrides=overrides)
    text = resp.choices[0]["message"]["content"]
    for piece in _chunk_text(text):      # 適当な粒度で分割
        yield StreamChunk(id=resp.id, created=resp.created, model=resp.model,
                          choices=[{"index": 0, "delta": {"content": piece},
                                    "finish_reason": None}])
    yield StreamChunk(id=resp.id, created=resp.created, model=resp.model,
                      choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
                      usage=resp.usage)
```

#### 5.1.5 各エージェントの argv 構築表

すべてのフラグは `RESEARCH-cli.md` を典拠とする。`sandbox_mode=read_only`（既定）の場合を示す。`shell=True` は禁止し、必ずリスト形式の argv を `create_subprocess_exec` へ渡す。

| agent | 基本 argv | JSON 出力 | モデル指定 | 作業dir | サンドボックス/承認 | ターン上限 | プロンプト投入 |
|---|---|---|---|---|---|---|---|
| **codex** | `codex exec` | `--json` | `-m <model>` | `--cd <workdir>` | `-s read-only` | （フラグ無し→外部 timeout で囲む） | 引数末尾 or `-`（stdin） |
| **gemini** | `gemini` | `--output-format json` | `-m <model>` | `--include-directories <workdir>` | `--approval-mode default` | settings.json `maxSessionTurns`（CLI フラグ無し） | `-p "<prompt>"` |
| **grok**(CLI) | `grok` | `--output-format json` | `-m <model>` | `--cwd <workdir>` | `--sandbox`（書込拒否） | `--max-turns <N>` | `-p "<prompt>"` |
| **claude** | `claude -p` | `--output-format json` | `--model <model>` | `--add-dir <workdir>` | `--permission-mode plan` | `--max-turns <N>` | 引数 or stdin（10MB上限） |

補足事項（`RESEARCH-cli.md` 由来）:

- **codex**: `--timeout` / `--max-turns` は**存在しない**。時間上限は本アダプタの `asyncio.wait_for` + PGID kill で強制する。git 外ディレクトリで動かす場合は `--skip-git-repo-check` を付与。`--ephemeral` でセッション不保存。`--full-auto` は非推奨のため使わない。
- **gemini**: 全体 `--timeout` フラグは無い。`maxSessionTurns` 超過は終了コード **53**。非 TTY で自動ヘッドレス化するが、確実性のため `-p` を明示する。
- **grok(CLI)**: `--always-approve`（別名 `--yolo`）は full_auto 時のみ。read_only では付与しない。
- **claude**: `--bare` は付与**しない**（理由は §5.3.4）。`--max-turns` は print mode（`-p`）でのみ有効。

#### 5.1.6 JSON 出力のパース仕様

| agent | 出力形式 | 最終回答の抽出 | usage 正規化 | コスト |
|---|---|---|---|---|
| **codex** | JSONL（1行1イベント） | 最後の `item.completed` かつ `item.type=="agent_message"` の `item.text` | `turn.completed.usage` の `input_tokens` / `cached_input_tokens` / `output_tokens` | ドル額出力なし → `cost` から算出（§5.2） |
| **gemini** | 単一 JSON オブジェクト | `.response` | `.stats.models.<name>.tokens` | ドル額なし → 算出 |
| **grok**(CLI) | 単一 JSON（`--output-format json`） | 応答本文フィールド | トークンフィールド | ドル額なし → 算出 |
| **claude** | 単一 JSON オブジェクト | `.result`（`type=="result"`, `subtype=="success"`, `is_error==false`） | `num_turns` / `duration_ms` 併記 | **`total_cost_usd` を直接出力**（唯一） |

- usage は OpenAI 形式 `{"prompt_tokens", "completion_tokens", "total_tokens"}` に正規化して `ChatResponse.usage`（`base.py` L80）へ格納する。codex の `cached_input_tokens` は `prompt_tokens_details.cached_tokens` として保持する。
- **total_cost_usd**: claude は JSON の `total_cost_usd` をそのまま `coderouter_provider` 相当のメタとして cost dashboard に流す。他エージェントは `ProviderConfig.cost`（`schemas.py` L246、`CostConfig`）に単価を設定した場合のみトークン数から算出する。未設定ならコスト0（トークン数のみ集計）。
- JSON は**防御的にパース**する（`RESEARCH-cli.md` §7）。想定フィールド欠落時は `AdapterError(retryable=True)` を送出する。

#### 5.1.7 stdout/stderr の drain

Codex は**進捗を stderr・最終メッセージを stdout** に分離出力する（`RESEARCH-cli.md` §1）。`communicate()` は両ストリームを並行に読み切るため、パイプバッファ満杯によるデッドロックを避けられる。`launcher_routes.py` の `_drain`（L555-577、`_LOG_STREAM_LIMIT`・`LimitOverrunError` 対策）が下敷きになる（`ANALYSIS-code.md` §4-5）。ただしワンショットのため常駐レジストリ（`ManagedProcess` L109）は不要で、`create_subprocess_exec` + `communicate()` の1ショットで足りる。

#### 5.1.8 タイムアウトとプロセスグループ kill

- `exec_timeout_s`（プロファイル override（`ProviderCallOverrides.timeout_s`）があればそちらが優先）を `asyncio.wait_for` で強制する。継承元 `effective_timeout()`（`base.py` L222）は `ProviderConfig.timeout_s` にフォールバックする実装のためそのままは使わず、§5.1.3 の通り `AgentCliConfig.exec_timeout_s` を基準に解決する。Codex/Gemini は CLI 側 timeout を持たないため Python 側の強制 kill が必須である（`RESEARCH-cli.md` §7）。
- 子プロセスは `start_new_session=True` で新プロセスグループとして起動し、タイムアウト時は `os.killpg(os.getpgid(pid), SIGKILL)` でグループごと確実に停止する（CLI が子プロセス＝実 LLM 呼び出しをぶら下げるため、親のみ kill では孤児が残る）。

#### 5.1.9 ANSI / TTY 対策

必ず非対話面（`exec` / `-p`）と JSON 出力を使う（`RESEARCH-cli.md` §7）。加えて子プロセス env に `NO_COLOR=1` / `TERM=dumb` を注入し、ANSI エスケープの混入を防ぐ。stdin/stdout はパイプ接続であり TTY を割り当てない。

### 5.2 config schema

#### 5.2.1 `AgentCliConfig`（新規サブスキーマ）

`schemas.py` に以下を追加する。`extra="forbid"` を踏襲する（`schemas.py` L175 の方針）。

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `agent` | `Literal["codex","gemini","grok","claude"]` | 必須 | 呼び出す外部エージェント種別 |
| `command` | `str` | `agent` 名と同じ | CLI 実行ファイル名または絶対パス（PATH 解決） |
| `workdir` | `str \| None` | `None` | 作業ディレクトリ。`~` / 環境変数展開。未設定時は専用隔離 dir を自動生成 |
| `exec_timeout_s` | `float`（`ge=1.0, le=1800.0`） | `600.0` | ワンショット exec の強制タイムアウト |
| `allow_file_writes` | `bool` | `False` | FS 書き込み許可。既定は read-only（§6） |
| `sandbox_mode` | `Literal["read_only","edit","full_auto"]` | `"read_only"` | 各 CLI のサンドボックス/承認へのマッピング元（§5.4） |
| `model` | `str \| None` | `None` | CLI へ渡すモデル名の上書き。未設定なら `ProviderConfig.model` を使用 |
| `max_turns` | `int \| None`（`ge=1, le=50`） | `8` | ターン上限。codex は無視（CLI 未対応）、他は該当フラグへ |
| `passthrough_env` | `list[str]` | `[]` | 子プロセスへ転送する env 変数名の allowlist（§5.3） |
| `agent_depth_limit` | `int`（`ge=1, le=4`） | `2` | 再帰ネスト上限（§5.5） |

検証ルール（`model_validator(mode="after")`、`schemas.py` の既存 fast-fail パターンに倣う）:

1. `agent=="grok"` かつ `passthrough_env` に `XAI_API_KEY` が無い場合は警告レベルで注記（Grok はサブスク非対応で API キー必須。`RESEARCH-cli.md` §3）。
2. `allow_file_writes=True` かつ `sandbox_mode=="read_only"` は矛盾のため `ValueError`。
3. `exec_timeout_s` は `ProviderConfig.timeout_s`（`schemas.py` L198）とは独立。exec 用の別枠として扱う。

#### 5.2.2 `ProviderConfig` への追加

`schemas.py` L178 の `kind` の `Literal` に `"agent_cli"` を追加し、`agent_cli` フィールドを任意で足す（`restart_command` L274 のように opt-in・既定 None のパターンを踏襲）。

```python
kind: Literal["openai_compat", "anthropic", "agent_cli"] = "openai_compat"
agent_cli: AgentCliConfig | None = Field(default=None,
    description="kind='agent_cli' 時に必須の外部エージェント設定。")
```

追加検証（`ProviderConfig` の `model_validator`）:

- `kind=="agent_cli"` のとき `agent_cli` は必須。欠落は `ValueError`。
- `kind=="agent_cli"` のとき `base_url` は不要。**設計判断**: 現行 `base_url: HttpUrl`（L186）は必須のため、`base_url: HttpUrl | None = None` に緩和し、「`kind` が `openai_compat`/`anthropic` のときのみ必須」を `model_validator` で強制する。これにより agent_cli は URL を持たずに済む。
- `model` は必須のまま流用する。capability registry・ログ・cost 集計のキーとして機能し、`AgentCliConfig.model` が未設定なら CLI の `--model` 値にもなる。

#### 5.2.3 `build_adapter` への配線

`adapters/registry.py` L11-17 に1分岐を追加する（`ANALYSIS-code.md` §4-2）。

```python
if provider.kind == "agent_cli":
    from coderouter.adapters.agent_cli import AgentCliAdapter
    return AgentCliAdapter(provider)
```

`routing/fallback.py` は改修不要（`register_provider` L1176・`_adapters` キャッシュ L1106-1108 が新 kind を自動で扱う。`ANALYSIS-code.md` §4-4）。

### 5.3 認証設計（`DECISIONS.md` #4 準拠）

#### 5.3.1 基本方針: サブスク優先 + 子プロセス env allowlist

子プロセスは**親環境を継承しない**。`env=` を明示構築し、allowlist に列挙された変数のみを注入する。これは残存 `ANTHROPIC_API_KEY` がサブスク OAuth を上書きする事故（`RESEARCH-cli.md` §4 の落とし穴）を根本から断つためである。

`_build_child_env()` は以下だけを含む最小環境を返す。

| キー | 出所 | 目的 |
|---|---|---|
| `HOME` | 親の `HOME`（後述ポリシー） | 資格情報ディレクトリ探索 |
| `PATH` | 固定の安全な PATH | CLI バイナリ解決 |
| `NO_COLOR` / `TERM=dumb` | 固定注入 | ANSI 抑止 |
| `CODEROUTER_AGENT_DEPTH` | 現在値+1 | 再帰防止（§5.5） |
| `passthrough_env` 列挙分 | 親環境から該当名のみ | エージェント別の必須キー（下表） |

`ANTHROPIC_API_KEY` は allowlist に**既定で含めない**。含めたい場合は運用者が明示的に `passthrough_env` へ書く必要がある（`ANALYSIS-code.md` §5、`config/loader.py` の `resolve_api_key` L95 とは別系統の転送）。

#### 5.3.2 HOME / 資格情報ディレクトリ継承ポリシー

サブスク OAuth トークンはファイルで保管される（`~/.codex/auth.json`・`~/.gemini/`・`~/.claude/.credentials.json`）。子プロセスがこれらを読めるよう `HOME` を継承させる。ただし **`HOME` 継承はするが env allowlist は最小のまま**という分離を守る（`ANALYSIS-code.md` §5 が「(a) env allowlist と (b) HOME/設定ディレクトリ継承ポリシーを分けて定義せよ」と要求）。運用者が資格情報を隔離したい場合は、CLI 別の `CODEX_HOME` 等を `passthrough_env` で指す運用も可能とする。

#### 5.3.3 エージェント別の認証要件

| agent | 優先経路（サブスク） | 代替 | passthrough_env の推奨 |
|---|---|---|---|
| **claude** | Pro/Max OAuth（`~/.claude/.credentials.json`）。`claude setup-token` の1年トークン `CLAUDE_CODE_OAUTH_TOKEN` も可 | `ANTHROPIC_API_KEY`（**非推奨**・既定注入せず） | `[CLAUDE_CODE_OAUTH_TOKEN]`（任意） |
| **codex** | ChatGPT プラン OAuth（`~/.codex/auth.json`）。**約8日で失効**に注意 | `CODEX_API_KEY` | `[]`（サブスク時は空） |
| **gemini** | Google OAuth キャッシュ（`~/.gemini/`）。**キャッシュ済みなら**動作 | `GEMINI_API_KEY` | `[GEMINI_API_KEY]`（保険） |
| **grok** | **サブスク非対応**（SuperGrok / X Premium+ は API をカバーしない） | `XAI_API_KEY` **必須** | `[XAI_API_KEY]`（必須） |

#### 5.3.4 `--bare` を使わない理由（claude）

`claude --bare` は hooks/skills/plugins/MCP/CLAUDE.md の自動探索をスキップし CI 向きだが、**OAuth / keychain を読まない**（`RESEARCH-cli.md` §4）。本機能はサブスク OAuth を第一経路とするため、`--bare` を付けるとサブスク認証が効かなくなる。したがって `--bare` は**付与しない**。将来 `--bare` が `-p` の既定になる予定がある点は §10 のリスクに記載する。

### 5.4 mode → 各 CLI サンドボックス/承認フラグのマッピング

`sandbox_mode` を各 CLI の実フラグへ写像する（`RESEARCH-cli.md` §synthesis 由来）。既定は `read_only`。

| sandbox_mode | codex | gemini | grok(CLI) | claude |
|---|---|---|---|---|
| **read_only**（既定） | `-s read-only` | `--approval-mode default`（書込なし） | `--sandbox`（書込拒否） | `--permission-mode plan` |
| **edit** | `-s workspace-write -a on-request` | `--approval-mode auto_edit` | `--allow`（値未確定・要検証） | `--permission-mode acceptEdits` |
| **full_auto** | `-s workspace-write -a never` | `--yolo`（+ Docker サンドボックス推奨） | `--always-approve` | `--permission-mode acceptEdits`（CI ロックダウンは `dontAsk`） |

- `allow_file_writes=False`（既定）は `read_only` 相当にクランプし、`edit`/`full_auto` を要求されても書き込みフラグを外す（多重防御）。
- codex の `workspace-write` は既定でネットワーク無効・`.git` 読取専用（`RESEARCH-cli.md` §1-4）であり、そのまま利用する。
- grok(CLI) の `--allow` は `RESEARCH-cli.md` にフラグの存在のみ記載されており、値（サブコマンド/引数形式）は未確認である。Phase 1d 着手時に実 CLI で検証すること（`--deny` も同様）。

### 5.5 再帰防止

外部エージェントがさらに CodeRouter を呼び返すネスト再帰を防ぐ（`RESEARCH-cli.md` §7）。

- `CODEROUTER_AGENT_DEPTH` 環境変数を子プロセスへ伝播する。`_build_child_env()` は現在値（未設定なら 0）に +1 した値を注入する。
- `generate()` 冒頭で現在の depth を読み、`AgentCliConfig.agent_depth_limit`（既定 2）以上なら `AdapterError(retryable=False)` で即時停止する。
- 併せて `max_turns`（既定 8）で各エージェントの内部ループを有界化する（codex はフラグ非対応のため timeout で代替）。

---

## 6. セキュリティ要件

subprocess は任意コード実行の攻撃面である（`ANALYSIS-code.md` §5）。以下を要件化する。

| 要件 | 実装 |
|---|---|
| **allowlist argv のみ** | `create_subprocess_exec` にリスト argv を渡す。`shell=True` は**禁止**。プロンプトは引数値または stdin で渡し、シェル解釈を経由させない |
| **既定 read-only** | `allow_file_writes=False` / `sandbox_mode="read_only"` が既定。書き込みは明示 opt-in（`restart_command` の「opt-in・既定オフ」先例に倣う） |
| **workdir 境界** | `workdir` を絶対パスへ正規化し、専用隔離ディレクトリ配下に限定。`..` によるエスケープを拒否。未設定時は `~/.coderouter-t/agents/<name>` を自動生成 |
| **timeout 強制 kill** | `exec_timeout_s` を `asyncio.wait_for` で強制、超過時は PGID ごと SIGKILL（§5.1.8） |
| **env allowlist** | 親環境を継承せず最小注入。`ANTHROPIC_API_KEY` は既定で渡さない（§5.3） |
| **ネスト上限** | `CODEROUTER_AGENT_DEPTH` + `agent_depth_limit`（§5.5） |

加えて、既存 guards（`tool_loop.py` L290 / `context_budget.py` L109,L153 / `memory_budget.py` L150）は**wire 層検査**であり、新アダプタにも**自動適用**される（`ANALYSIS-code.md` §5）。すなわちツールループ検出・コンテキスト予算・メモリ予算の各保護は agent_cli プロバイダでも追加実装なしで効く。

---

## 7. capability gate との整合

agent_cli プロバイダは「プロンプト in → テキスト out」の不透明なテキストバックエンドである。native なツール呼び出しの往復をクライアントへ中継しない（内部で完結する）。したがって capability の分類方針を以下に定める。

- **`capabilities.tools=false`**（`schemas.py` L29 既定）。エージェントはツールを内部使用するが、CodeRouter のワイヤ上は往復しないため、gate 上は「ツール非対応」とする。
- **`capabilities.streaming=false`**。§5.1.4 の擬似ストリームであり native SSE ではないため false を既定とする。
- **`capabilities.reasoning_control="none"` / `mcp="none"`**。CLI が内部で完結するため CodeRouter は関与しない。

配置方針: agent_cli プロバイダは**専用プロファイル**（例: `codex` / `gemini`）に単独で置くか、**fallback chain の終端**に置く。理由は、ツール往復を期待するクライアント要求（`has_tools` 経路）を中間に挟むと、ツール応答をワイヤに返せず不整合になるためである。`auto_router` の `model_pattern` で専用プロファイルへ振り分けるのが推奨形（§3.1）。capability_registry への新規エントリ追加は必須ではなく、`ProviderConfig.capabilities` の明示宣言で足りる。

---

## 8. テスト計画

`ANALYSIS-code.md` §6 を具体化する。雛形は `tests/test_launcher_mtp.py`（L340・L368）である。

| # | テスト | 手法 |
|---|---|---|
| T1 | argv 構築の正しさ | 4エージェント × 3 sandbox_mode の argv を snapshot 検証（フラグが `RESEARCH-cli.md` と一致すること） |
| T2 | JSON パース | 各 CLI の実サンプル JSON/JSONL を固定文字列で与え、最終回答・usage・cost の抽出を検証。欠落フィールドで `AdapterError(retryable=True)` |
| T3 | スタブ subprocess | `monkeypatch` で `asyncio.create_subprocess_exec` を `_FakeProc` に差し替え（`test_launcher_mtp.py` L368-374 の手法）。stdout/stderr/returncode を注入 |
| T4 | TestClient E2E | `TestClient(create_app())`（L340）で agent_cli provider + profile を config に書き、`POST /v1/chat/completions` / `/v1/messages` を検証。`X-CodeRouter-Profile` ヘッダ（`openai_routes` L180・L202） |
| T5 | fallback 降格 | `AdapterError(retryable=True)` を注入し、次プロバイダへ降格することを検証 |
| T6 | タイムアウト kill | `_FakeProc` を無限ハングさせ、`exec_timeout_s` 超過で PGID kill + `AdapterError` を確認 |
| T7 | 認証 env 分離 | `_build_child_env()` を単体検証。`ANTHROPIC_API_KEY` が親環境にあっても子 env に**含まれない**こと、`passthrough_env` 列挙分のみ通ること、`CODEROUTER_AGENT_DEPTH` が +1 されること |
| T8 | 再帰上限 | `CODEROUTER_AGENT_DEPTH` を上限値に設定した状態で `generate()` が `AdapterError(retryable=False)` を出すこと |
| T9 | 実 CLI smoke | opt-in の手動テスト（CI 非搭載）。各 CLI が実在する環境でのみ実行 |

---

## 9. 実装フェーズ

| Phase | 対象 | 主な変更ファイル | 概算規模 |
|---|---|---|---|
| **1a** | claude のみ（最安定・`total_cost_usd` 出力あり・安全制御最細） | `adapters/agent_cli.py`（新規）/ `adapters/registry.py` / `config/schemas.py` / `tests/test_agent_cli.py`（新規） | 新規 ~400 行 + 既存 ~15 行 |
| **1b** | codex 追加（JSONL パーサ・stderr/stdout 分離） | `adapters/agent_cli.py`（builder/parser 追加） | +~120 行 |
| **1c** | gemini 追加（単一 JSON・exit 53 ハンドリング） | `adapters/agent_cli.py` | +~90 行 |
| **1d** | grok 追加（CLI 経路） | `adapters/agent_cli.py` | +~90 行 |
| **2** | Adapter Protocol の engine 配線 + `coderouter-plugin-agents` への切り出し | `plugins/base.py`（Adapter Protocol 実体化）/ `plugins/loader.py` / 新規パッケージ | 別リポ・core 改修 |

### 9.1 Phase 1a（claude 最優先）

`DECISIONS.md` #5・`RESEARCH-cli.md` §synthesis の推奨に従い claude を最初に実装する。`total_cost_usd` を直接出力する唯一の CLI であり、`--output-format json` スキーマが最も安定（後方互換・追加のみ）であるため、パーサ・cost 経路の基準実装になる。

### 9.2 Phase 1d（grok）の判断基準

Grok の公式 API は OpenAI 互換であり、**既存 `kind="openai_compat"` プロバイダで既に代替可能**である（`base_url: https://api.x.ai/v1`・`api_key_env: XAI_API_KEY`・`model: grok-code-fast-1`）。したがって agent_cli の grok CLI 対応は次を満たす場合のみ実装する。

- 公式 CLI「Grok Build」が beta を脱し JSON スキーマが安定したこと。
- CLI 固有機能（ローカル FS 編集・サンドボックス）を CodeRouter 経由で使う運用需要が実在すること。

それ以外は API 直（openai_compat）を推奨経路とする。

### 9.3 Phase 2（plugin 化）の移行パス

現状 Adapter Protocol は実体が空（`name: str` のみ、`plugins/base.py` L168-169、`ANALYSIS-code.md` §1）でデッドエンドである。将来 core が Adapter hook を配線したら、in-core の `AgentCliAdapter` を**ほぼそのまま** `coderouter-plugin-agents` へ移設できる（前方互換、`ANALYSIS-code.md` §3）。ロードは entry-point 方式（`group="coderouter.adapters"` 等）+ `plugins.enabled` の二段ゲート（`PluginsConfig`、`schemas.py` L1247）を経る。

> **ステータス（2026-07-11 追記）**: 作者の方向性指示（「作業ごとにモデルを割当・サブエージェントを利用する」、`_article/direction-brief-2026-07-11.md`）を受けた方向性確定により、Phase 2 の優先度が上昇した（`docs/inside/future.md` §2.5 参照）。あわせて、§11.3〜§11.5 に記録した実 CLI 検証（grok の `--sandbox`/認証仕様変更、codex の承認フラグ obsolete 化、gemini 個人向け終了→antigravity 移行）が示す **CLI churn（バージョン間の破壊的変更）に Core を追従させ続けるコストを Core のリリース周期から切り離す**動機も加わった。移行トリガは変わらず: (i) agent_cli 利用比重の恒常化 (ii) CLI churn 追従コストが Core リリースを圧迫 (iii) コミュニティ要望。着手そのものは未着手（発火条件待ち）。
>
> 訂正（2026-07-11）: entry-point group 名は loader 実装（`plugins/loader.py` L94）に合わせ `coderouter.adapter`（単数形）が正。詳細は [`agent-cli-plugin-extraction.md`](./agent-cli-plugin-extraction.md) を参照。

---

## 10. リスクと未解決事項

| リスク / 事項 | 内容 | 対応方針 |
|---|---|---|
| **CLI バージョン churn** | codex/gemini はほぼ毎日リリース、pre-1.0 の破壊的変更あり（`RESEARCH-cli.md` §1-6・§2-6） | バージョン **pin** を運用手順に明記。JSON は防御的パース（欠落で retryable エラー）。`command` に固定版バイナリを指定可能に |
| **サブスク規約** | Consumer Terms は常時稼働サービスへの転用を禁止。claude/codex のサブスク OAuth はマルチテナント常時サービスに使うべきでない（`RESEARCH-cli.md` §synthesis の「避けること」） | 個人 CI/スクリプト用途に限定と明記。常時 backend は API キー（Commercial Terms）を運用者判断で選択 |
| **OAuth 失効** | codex OAuth は**約8日**で失効（`RESEARCH-cli.md` §1-2） | healthcheck では検知不可（バイナリ存在のみ）。exec 失敗を `AdapterError` で fallback。運用手順に再ログイン注記 |
| **レート制限（5時間窓）** | claude/codex は5時間ローリング窓 + 週次上限（`RESEARCH-cli.md` §1-5・§4-5） | 上限到達時の exec 失敗を retryable エラーで扱い、chain の次プロバイダへ降格 |
| **ストリーミング制約** | 安定した stream-json 面が CLI により無い | 擬似ストリーム方針（§5.1.4）。`capabilities.streaming=false` を既定に |
| **gemini OAuth キャッシュ依存** | クリーン CI では OAuth 未サインインで動かない（`RESEARCH-cli.md` §2-2） | `GEMINI_API_KEY` を `passthrough_env` の保険に |
| **claude `--bare` 既定化予定** | 将来 `--bare` が `-p` の既定になる可能性（`RESEARCH-cli.md` §4-6）。その場合 OAuth を読まなくなる | バージョン pin + 移行時に明示的に `--bare` を無効化するフラグ調査 |
| **gemini maxSessionTurns 未遵守バグ** | 既知の変動（`RESEARCH-cli.md` §2-6） | timeout を最終防波堤とする |

---

## 11. 付録

### 11.1 調査サマリ比較表（`RESEARCH-cli.md` §5 要約）

| 項目 | Codex CLI | Gemini CLI | Grok（API / CLI） | Claude Code CLI |
|---|---|---|---|---|
| 認証 | ChatGPT OAuth or API key | Google OAuth / API key | API key（推奨）/ CLI は OIDC | Pro/Max OAuth or API key |
| ヘッドレス | `codex exec "..."` | `gemini -p "..."` | API 直 / `grok -p "..."` | `claude -p "..."` |
| JSON 出力 | `--json`（JSONL） | `--output-format json` | API ネイティブ | `--output-format json` |
| サンドボックス | Seatbelt/Landlock, net 既定無効 | Docker/Podman/Seatbelt | API=なし / CLI=`--sandbox` | Seatbelt/bubblewrap |
| サブスクで使えるか | ○（8日失効・CI は key 推奨） | △（要キャッシュ） | **×**（API 別課金） | ○（setup-token 1年・個人 CI 公認） |
| コスト出力 | トークンのみ | トークンのみ | トークンのみ | **`total_cost_usd` あり** |
| max-turns/timeout | なし（外部で囲む） | maxSessionTurns（bug あり） | `--max-turns`（CLI） | `--max-turns`（print） |
| 成熟度 | 高（要 pin） | 中（pre-1.0） | API=高 / CLI=beta | **最高** |

### 11.2 シンボル早見表（`ANALYSIS-code.md` 付録より関連分）

| 対象 | ファイル:行 |
|---|---|
| `BaseAdapter` generate/stream/healthcheck | `adapters/base.py` L162, L243, L256, L239 |
| `ChatRequest` / `ChatResponse` / `StreamChunk` | `adapters/base.py` L43 / L70 / L86 |
| `AdapterError`（retryable フラグ） | `adapters/base.py` L104 |
| `effective_timeout` | `adapters/base.py` L222 |
| `build_adapter` | `adapters/registry.py` L11 |
| `register_provider` / `_adapters` キャッシュ | `routing/fallback.py` L1176 / L1106 |
| `ProviderConfig.kind` / `restart_command` / `api_key_env` | `config/schemas.py` L178 / L274 / L188 |
| `CostConfig` | `config/schemas.py` L64 |
| `PluginsConfig` / Adapter Protocol（空） | `config/schemas.py` L1247 / `plugins/base.py` L156, L168 |
| `ManagedProcess` / `_drain`（drain 手法） | `ingress/launcher_routes.py` L109 / L555-577 |
| guards（自動適用） | `tool_loop.py` L290 / `context_budget.py` L109 / `memory_budget.py` L150 |
| 資格情報解決 | `config/loader.py` L95 |
| テスト雛形 | `tests/test_launcher_mtp.py` L340, L368 |
| 設計思想（透過性・ステートレス） | `docs/future.md` §1.2 L164, §5 L636-815（特に L647, L677） |

### 11.3 Phase 1d 実CLI検証結果（grok v0.2.93, 2026-07-10）

Phase 1d（grok）実装時に、設計時点の調査（`RESEARCH-cli.md`、2026-07 時点）と実 CLI（grok v0.2.93、[stable] チャネル）との差分を検証した。本設計書の本文（§3.1・§5.1.5・§5.2.1・§5.3.3・§5.4・§11.1）のうち grok に関する記述は以下の検証結果で読み替えること（歴史的記録として本文は改変しない）。

| 項目 | 設計時の想定 | 実 CLI での検証結果（v0.2.93） |
|---|---|---|
| `--sandbox` | 値なしフラグ（書込拒否）と想定（§5.4） | **プロファイル値を取る**: `--sandbox off\|workspace\|read-only\|strict`。read_only は `--sandbox read-only` |
| 承認モード | `--allow`（値未確定）/ `--always-approve` のみ把握 | **`--permission-mode` が存在**し、Claude Code 互換の値を取る。read_only=`--permission-mode plan`、edit=`--sandbox workspace --permission-mode acceptEdits`、full_auto=`--sandbox workspace --always-approve` |
| プロンプト投入 | `-p "<prompt>"`（§5.1.5） | `-p` / `--single` は **argv 値必須で stdin をプロンプトとして受け付けない**。argv の肥大化（Linux MAX_ARG_STRLEN 約128KiB）と `ps` 露出を避けるため、**`--prompt-file`**（workdir 内 0600 一時ファイル、終了後必ず削除）を採用した |
| 認証 | 「grok はサブスク非対応・`XAI_API_KEY` 必須」（§5.3.3・§11.1） | **廃止された前提**。OAuth サブスクリプションログインに対応（`grok login`、SuperGrok / X Premium+。資格情報は `~/.grok/auth.json`、7日失効・自動リフレッシュ、`GROK_HOME` で上書き可）。`HOME` 継承により `passthrough_env: []` で動作する |
| API キー環境変数 | `XAI_API_KEY` | **`GROK_CODE_XAI_API_KEY`**（`XAI_API_KEY` ではない）。転送時は OAuth より優先される |
| JSON 出力 | 「応答本文フィールド」「トークンフィールド」と仮置き（§5.1.6） | 単一 JSON `{"text", "stopReason", "sessionId", "requestId", "thought"?}`。**usage / cost フィールドは存在しない** → usage はゼロ報告、cost は `ProviderConfig.cost` 単価設定時のみ算出 |
| モデル | `grok-code-fast-1`（§3.1） | **`grok-code-fast-1` は 2026-05-15 に廃止**。現行は `grok-4.5`（既定）/ `grok-composer-2.5-fast`（`grok models` で一覧） |
| `--max-turns` | 存在（§11.1） | **確認済み**（そのまま採用） |
| `--allow` / `--deny` | 値の形式未確認（§5.4 注記） | ToolPrefix（glob）形式のルールを取ることを確認。ただし **Phase 1d のアダプタでは使用しない** |
| メモリ | （記載なし） | grok はクロスセッションメモリを持つ。ステートレス性維持のためアダプタは **`--no-memory` を常時付与** |

あわせて、§5.2.1 の検証ルール #1（`agent=="grok"` かつ `passthrough_env` に `XAI_API_KEY` が無い場合の警告）は前提の消滅により**廃止**した（実装には含まれない）。§9.2 の「API 直（openai_compat）推奨」は引き続き有効な代替経路である。

### 11.4 Phase 1b 実CLI検証結果（codex-cli 0.144.1, 2026-07-11）

Phase 1b（codex）実装時に、設計時点の調査（`RESEARCH-cli.md`、2026-07 時点）と実 CLI（codex-cli 0.144.1、作者の Mac、2026-07-11）との差分を検証した。本設計書の本文（§3.1・§5.1.5・§5.1.6・§5.2.1・§5.3.3・§5.4・§9・§11.1）のうち codex に関する記述は以下の検証結果で読み替えること（歴史的記録として本文は改変しない）。

| 項目 | 設計時の想定 | 実 CLI での検証結果（0.144.1） |
|---|---|---|
| 承認フラグ（edit/full_auto） | `-a/--ask-for-approval` が存在し、`edit` → `-s workspace-write -a on-request`、`full_auto` → `-s workspace-write -a never` とマッピング（§5.4） | **`-a`/`--ask-for-approval` は `exec` サブコマンドに存在しない**（0.144.1 の `exec --help` に無し。非対話実行なので承認プロンプト自体が無い）。設計の `edit`/`full_auto` マッピングは obsolete — 両方とも `-s workspace-write` のみで、`edit` と `full_auto` は区別されない |
| `--full-auto` | 非推奨のため使わないと注記（§5.1.5 補足） | **確認済み・不使用のまま**。`--dangerously-bypass-approvals-and-sandbox` も同様に使わない |
| `--skip-git-repo-check` | git 外ディレクトリで動かす場合に付与、と注記（§5.1.5 補足） | **実機で必須と確認**。CodeRouter の隔離 workdir は git リポジトリではないため、無いと exit 1 + stderr `Not inside a trusted directory and --skip-git-repo-check was not specified.` で即座に失敗する（実測）。アダプタは常時付与する |
| `--ephemeral` | セッション不保存の位置付けで言及（§5.1.5 補足） | **採用を確定**。grok の `--no-memory` と同じ理由（ステートレス思想）で常時付与する |
| プロンプト投入 | 「引数末尾 or `-`（stdin）」（§5.1.5 表） | **stdin 方式を確定採用**。PROMPT 省略時（または明示 `-`）は stdin から読む。argv 末尾に明示の `-` を置く。claude と同じ経路であり、grok のような `--prompt-file` は不要 |
| JSON 出力スキーマ | JSONL・最終回答は最後の `item.completed`（`item.type=="agent_message"`）の `item.text`、usage は `turn.completed.usage` の `input_tokens`/`cached_input_tokens`/`output_tokens`（§5.1.6） | **実機で確認・スキーマに追加あり**: `turn.completed.usage` に**新フィールド `reasoning_output_tokens`**が含まれる（設計時点では未記載）。0 より大きい場合は `completion_tokens_details.reasoning_tokens` として保持する（防御的）。`cached_input_tokens` は `input_tokens` の部分集合であり加算しない（実測: input 13810 ⊃ cached 9984） |
| `--max-turns` / `--timeout` | 「（フラグ無し→外部 timeout で囲む）」と設計時点から想定済み（§5.1.5 表、§10 リスク表） | **設計どおり確認**。`--max-turns` も `--timeout` も存在せず、`AgentCliConfig.max_turns` は codex では無視される。`exec_timeout_s` + PGID kill のみが時間上限 |
| 認証 | ChatGPT プラン OAuth or API key、約8日失効に既に言及（§5.3.3・§11.1） | **概ね設計どおりで確認・詳細確定**。`~/.codex/auth.json`（または OS キーチェーン）、約8日で stale・使用時自動リフレッシュ。CI 用に**`CODEX_API_KEY`（exec 専用）**および `OPENAI_API_KEY`（一般）の両方を確認。`CODEX_HOME` で config/資格情報ディレクトリを上書き可能（設計時点で未記載の追加事項） |
| モデル | `gpt-5.5`（§3.1 の providers.yaml 例） | **`gpt-5.5` は現行フロンティアモデルとして引き続き有効**。既定モデルは環境/プラン依存のため `providers.yaml` での明示を推奨する方針も変更なし |
| 成熟度 / バージョン churn | pre-1.0・要 pin（§10 リスク表、§11.1） | **確認済み**。CLI は pre-1.0 でほぼ毎日リリース。`--json` の別名は依然 `--experimental-json` のままでスキーマ未凍結 → バージョン pin 推奨 + 防御的パース必須の方針を維持 |

あわせて、§5.1.5 表の codex 行の「サンドボックス/承認」列は `-s read-only` のみが確定であり、`edit`/`full_auto` は上表のとおり `-s workspace-write` に統一されたことを付記する（本文は歴史的記録として改変しない）。

### 11.5 Phase 1c 実CLI検証結果（gemini 廃止と Antigravity CLI `agy` 1.1.1, 2026-07-11）

Phase 1c 着手時点で、本設計書の本文（§1.1・§3.1・§5.1.5・§5.2.1・§5.3.3・§5.4・§9・§11.1）が前提としていた対象 CLI そのもの（Google Gemini CLI、コマンド `gemini`）が実装対象から外れた。Google が個人アカウント向け Gemini CLI の提供を終了したためであり、以下は検証結果である（歴史的記録として本文は改変しない。本文中の `gemini` に関する記述は下表で読み替えること）。

| 項目 | 設計時の想定（gemini） | 実機での検証結果（agy 1.1.1、作者の Mac、2026-07-11） |
|---|---|---|
| 対象 CLI そのもの | Google Gemini CLI（コマンド `gemini`）を想定（§1.1・§3.1・§4.1 構成図など） | 個人アカウント向け Gemini CLI（`@google/gemini-cli` 0.50.0 系）の OAuth は **2026-06-18 で提供終了**（公式ブログ）。実機で次の verbatim エラーを確認: `IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals. To continue using Gemini, please migrate to the Antigravity suite of products: https://antigravity.google`（`reasonCode: UNSUPPORTED_CLIENT`, `tierId: free-tier`） |
| trusted-directory ゲート | （設計時点で gemini については未記載） | 旧 gemini CLI は trusted-directory ゲートで **exit 55**（`--skip-trust` または `GEMINI_CLI_TRUST_WORKSPACE=true` が必要）にも遭遇。個人アカウント終了とは別に以前から存在する制約 |
| 後継 CLI との関係 | （未検討） | 後継の **Antigravity CLI**（コマンド `agy`）は gemini-cli の**フォークではなく Go 製の別実装**。個人の Google アカウント OAuth（無料枠含む）はこちらで存続する |
| 出力形式 | `--output-format json`（§5.1.5 表・§5.1.6 表） | agy には `--output-format` 系のフラグが**存在しない**。出力はプレーンテキストのみで、トークン usage・セッション ID・構造化エラーはいずれも取得不能 |
| 承認/モード制御 | `--approval-mode default/auto_edit/--yolo`（§5.4 表） | agy は `--mode plan\|accept-edits` を持つ（ヘルプに列挙されるのはこの2値のみ）。`--dangerously-skip-permissions` で全ツール実行を自動承認できる。read_only→`--mode plan`、edit→`--mode accept-edits`、full_auto→`--mode accept-edits --dangerously-skip-permissions` |
| CLI 側タイムアウト | 「全体 `--timeout` フラグは無い」と記載（§5.1.5 補足） | agy は `--print-timeout <Go duration>`（既定 5m0s）という print モード自身のタイムアウトを持つ。**claude/codex/grok にはこの種のフラグが無く、agy が唯一**。`exec_timeout_s` から生成し、外側の `asyncio.wait_for` + PGID kill と合わせて二重防壁になる |
| ターン上限 | `maxSessionTurns`（settings.json、CLI フラグ無し。exit 53 でハンドリング想定、§5.1.5・§9） | agy には `--max-turns` 相当のフラグが存在しない。`AgentCliConfig.max_turns` は antigravity では常に無視される（codex と同じ扱い） |
| プロンプト投入 | `-p "<prompt>"`（§5.1.5 表） | `-p` / `--print` / `--prompt` は argv 値必須。**stdin 経由のプロンプト受け取りは無く、grok のような `--prompt-file` 相当のフラグも無い**。プロンプトは argv 値として渡すほかなく、`MAX_ARG_STRLEN`（約128KiB）と `ps` 可視性が既知の制限として残る |
| stdin パイプ時の挙動 | （未検討） | **stdin に何かをパイプすると agy がハングする**ことを実機で確認（`printf '...' \| agy -p "..."` は応答なしのまま `Error: timeout waiting for response`）。アダプタは stdin に何も書かず即クローズする（`communicate(input=None)`）ため CodeRouter 経由では問題にならない（`</dev/null` での正常動作を実機確認済み） |
| 非 TTY 出力バグ | （未検討） | v1.x 初期に「非 TTY だと出力空 + exit 0」の既知バグ報告があったが、**agy 1.1.1 では再現せず**（TTY・非 TTY いずれも正常） |
| モデル指定 | `gemini-2.5-pro` のような API ID 相当を想定（§3.1 の providers.yaml 例） | `--model` は **表示名文字列**（`Gemini 3.5 Flash (Low)` 等）。`agy models` で一覧確認できる。Google 以外のモデル（例: `Claude Opus 4.6 (Thinking)`）まで agy 経由で呼べる |
| 認証 | Google OAuth キャッシュ（`~/.gemini/`）+ `GEMINI_API_KEY` を保険として `passthrough_env`（§5.3.3） | 資格情報は **OS キーリングを優先**し、`~/.gemini/antigravity-cli/`（`credentials.enc` / `settings.json`）にも保管。`HOME`（macOS では `USER` も）継承で headless 動作し `passthrough_env: []` でよい。API キー経由の環境変数名は情報が錯綜しており **UNCONFIRMED**（断定しない） |
| エラー体系 | （未検討） | exit code の公開表は無い。非零 exit は既存どおり retryable 扱いとする |
| スキーマ上の帰結 | （n/a） | `AgentCliConfig.agent` の `Literal` に **5番目の値 `"antigravity"`** を追加。`command` の既定値は agent 名と同じだが **antigravity のみ既定 `"agy"`**（バイナリ名が製品名と異なるため）。`gemini` は `Literal` に残すが、アダプタ `__init__` で「Google discontinued Gemini CLI for individual accounts (June 2026) → use agent='antigravity'」という専用メッセージ付きで拒否する（`retryable=False`） |

以上により Phase 1（1a claude・1b codex・1c antigravity・1d grok）が完了した。§9 の実装フェーズ表・§11.1 の比較表にある「gemini」列は、上表の読み替えを前提とした歴史的記録として残す（本文は改変しない）。

# Context Budget Management (v2.0.0)

English: [`context-budget.en.md`](./context-budget.en.md)

長時間 agent session が context window を使い切って死ぬ問題を、CodeRouter が未然に防ぎます。

## なぜ必要か

Claude Code / Cline / OpenClaw 等で 8 時間超えのコーディング session を回すと、messages が backend の context window (32K–200K tokens) に漸近します。限界を超えた瞬間 backend が 400 エラーを返し、agent session は即死します。

従来の対策:

- 手動で「新しい session にする」 → 作業中断 + コンテキスト喪失
- 外部ツールで token 数を監視 → 手間 + 設定複雑

CodeRouter v2.0.0 の Context Budget Management は **自動** で解決します:

1. **警告 (warn)** — 使用率が 80% を超えたら response header で通知
2. **自動トリム (trim)** — 使用率が 90% を超えたら古い messages を自動削除し、session を継続

## メリット

- **Session 死亡ゼロ**: どれだけ長い session でも context overflow で落ちない
- **ツールペア保全**: `tool_use` / `tool_result` のペアを atomic に保全。trim 後も agent loop が壊れない
- **設定不要で安全**: デフォルト `off`。opt-in 1 行で有効化、既存環境に影響なし
- **外部依存ゼロ**: char/4 heuristic で token 推定。tiktoken 等の追加 dep 不要 (5-deps 不変)
- **可観測性完備**: response header / structured log / Prometheus metrics / stats TUI の 4 経路で状態確認可能

## 設定方法

`providers.yaml` の profile に以下を追加:

```yaml
profiles:
  - name: default
    providers:
      - ollama-qwen3
    # Context Budget Guard (v2.0.0)
    context_budget_action: warn          # off | warn | trim
    context_budget_warn_threshold: 0.80  # 使用率 80% で warning
    # ↓ 以下 3 つは action: trim のときだけ効く (warn では inert)
    context_budget_trim_threshold: 0.90  # 使用率 90% で自動 trim
    context_budget_trim_target: 0.75     # trim 後の目標使用率
    context_budget_preserve_last_n: 4    # 直近 N messages は常に保持
```

> **まず `warn` から始めてください。** `trim` は【会話履歴を実際に削ります】(元に戻せません)。
> v2.12 (H-5) で token 推定が tool_result / tool_use / thinking を数えるようになり、
> tool 主体の session では推定値が従来の 5〜29 倍になったため、この guard は
> 「今までまったく発火しなかった環境で、突然発火する」状態にあります。
> `warn` で `context-budget-warning` ログと `X-CodeRouter-Context-Budget`
> ヘッダを一定期間観察し、閾値が自分の使い方に合っていることを確認してから
> `trim` に切り替えるのを推奨します。同じ理由で `examples/providers.yaml` /
> `examples/providers-multiagent.yaml` の配布既定も `warn` に下げてあります。

### パラメータ

| パラメータ | デフォルト | 説明 |
|---|---|---|
| `context_budget_action` | `off` | `off`: 無効 / `warn`: 警告のみ / `trim`: 警告 + 自動トリム |
| `context_budget_warn_threshold` | `0.80` | 警告を発する context 使用率 |
| `context_budget_trim_threshold` | `0.90` | 自動 trim を発火する context 使用率 |
| `context_budget_trim_target` | `0.75` | trim 後の目標使用率 (ここまで messages を削除) |
| `context_budget_preserve_last_n` | `4` | 直近 N messages は trim しても必ず保持 |

### アクションの選び方

| ユースケース | 推奨 action |
|---|---|
| まず様子を見たい | `warn` — ログと header で通知、messages は触らない |
| 長時間 session を安定運用したい | `trim` — 自動で overflow を防ぐ |
| 自前で token 管理している | `off` — guard 無効 |

## 動作の仕組み

```
リクエスト着信
    │
    ▼
estimate_context_usage()
    │  char/4 で token 数推定
    │  usage_ratio = estimated / max_context_tokens
    │
    ├─ ratio < warn_threshold → そのまま通過
    │
    ├─ warn_threshold ≤ ratio < trim_threshold
    │   → WARNING ログ出力
    │   → X-CodeRouter-Context-Budget: warning ヘッダ付与
    │   → (action=warn ならここで終了)
    │
    └─ ratio ≥ trim_threshold (action=trim のとき)
        → trim_to_budget() 実行
        → 古い messages を先頭から削除
        → tool_use/tool_result ペアは atomic 保全
        → X-CodeRouter-Context-Budget: trimmed ヘッダ付与
        → trim 後のリクエストを backend に送信
```

## Token 推定の仕組み

外部依存なしの char/4 heuristic:

```
estimated_tokens ≈ (数える対象の総文字数) // 4
```

「数える対象」は content block の `type` ごとに決まります (v2.12 / H-5):

| block type | 寄与 |
|---|---|
| `text` | `text` をそのまま |
| `tool_result` | `content` が str ならそのまま、block list なら中身に同じ規則を再帰適用 |
| `tool_use` | `name` + `json.dumps(input)` の長さ |
| `thinking` | `thinking` 文字列 (次ターンでモデルに送り返されるので実際に context を食う) |
| `image` | **0** |
| `redacted_thinking` | 0 (モデルが読めない暗号文) |
| 上記以外 (未知の type) | 0 |

- **`image` が 0 なのは仕様です。** base64 画像を `json.dumps` でそのまま数えると、400KB の PNG 1 枚で推定値が 35 倍に膨らみ、スクリーンショットを 1 回貼っただけの session の履歴を trim が破壊します。画像は別の課金軸なので 0 のまま据え置きます
- v2.11.x までは `text` block しか数えていませんでした。Claude Code のような tool 主体の client は文脈のほとんどを `tool_result` / `tool_use` に置くため、20 ターンで 5.1 倍、200 ターンで 28.6 倍の過小評価になっていました。v2.12 でこれを修正しています (= guard がようやく本来の対象で発火する)
- 英語テキストでは実測値の ±10% 以内
- CJK (日本語 / 中国語) では過小評価する傾向があるため、閾値を 5–10% 低めに設定すると安全
- tool の**定義** (リクエストの `tools[]`) は推定に含みません。`POST /v1/messages/count_tokens` だけは別途 `tools` の JSON 長を足しているため、両者の数字は tool schema が大きいリクエストで一致しません (既知の経路間差、v2.12 時点で未解消)

### v2.11.x の推定に戻す (互換シム)

修正後の推定値でチューニング済みの環境が乱れる場合、トップレベル設定で旧挙動に戻せます:

```yaml
token_estimation_include_tool_content: false   # v2.11.x と完全に同一の推定値
```

`false` にすると `tool_result` / `tool_use` / `thinking` が再び 0 文字換算になり、context budget guard の発火頻度・auto_router の `content_token_count_min` マッチ・`/v1/messages/count_tokens` の返り値がすべて v2.11.x と一致します。プロファイル単位ではなくトップレベルに置いてあるのは、推定器の利用者 4 つのうち 3 つ (auto_router / count_tokens / language tax) がプロファイル確定前・プロファイル非依存で動くためで、経路ごとに違う数字が出る事態を防ぐためです。**これは移行用のエスケープハッチであり、将来のリリースで削除予定です。**

`max_context_tokens` は `model-capabilities.yaml` に主要モデルが bundled 済み:

| モデル | max_context_tokens |
|---|---|
| Claude (Sonnet/Opus/Haiku) | 200,000 |
| Qwen3 | 32,768 |
| Qwen3-Coder / Qwen3.5 / Qwen3.6 | 131,072 |
| Gemma 4 | 131,072 |
| DeepSeek V3 / R1 | 131,072 |
| GPT-OSS | 131,072 |

model-capabilities.yaml に載っていないモデルは、provider config で明示指定:

```yaml
providers:
  - name: my-custom-model
    kind: openai_compat
    base_url: http://localhost:11434/v1
    model: custom-model
    max_context_tokens: 65536   # 明示指定
```

## Trim アルゴリズム

1. System prompt は削除対象外
2. 直近 `preserve_last_n` messages は常に保持
3. 古い messages から先頭順に削除
4. **Tool pair 保全**: `tool_use_id` で対応する `tool_use` / `tool_result` を特定し、片方だけ残る状態を防止 (fixpoint algorithm)
5. 削除後に再推定 → まだ `trim_target` 超えなら `preserve_last_n` を 1 減らして再試行 (最小 floor: 2)
6. **先頭の正規化**: Anthropic は先頭を `user` メッセージにすることを要求し、かつ先頭 user が宙に浮いた `tool_result` で始まってはいけない。trim 後に先頭がこの条件を満たすまで前から落とす
7. **空リストを返さない**: tool 主体の会話では保全した直近 N 件が全部 `assistant(tool_use)` / `user(tool_result)` になり得るため、6 を素直にやると messages が空になる (pydantic は通り、上流が 400)。その場合は削除済みの中から **予算内に収まる直近の clean user メッセージ** を先頭に復活させる。予算内に収まる候補が無ければ `[earlier conversation trimmed to fit the context budget]` という短い合成 user メッセージを差し込む。復元候補を「予算を見ずに直近優先」で選ぶと、直前の巨大ペーストをそのまま戻して trim が実質無効化されるため、必ず予算チェックを通す

## 可観測性

### Response Header

```
X-CodeRouter-Context-Budget: warning   # 警告状態
X-CodeRouter-Context-Budget: trimmed   # trim 実行済み
```

Streaming response でも SSE 開始前にヘッダが付与されます。

### Prometheus Metrics

```
coderouter_context_budget_warnings_total{profile="default"}  # 警告回数
coderouter_context_budget_trims_total{profile="default"}     # trim 回数
coderouter_context_budget_usage_ratio{profile="default"}     # 最新の使用率 (gauge)
```

```bash
curl http://localhost:8088/metrics | grep context_budget
```

### Stats TUI

```bash
coderouter stats --port 8088
```

Gates セクションに `ctx_budget_warnings` / `ctx_budget_trims` / `latest_ratio` が表示されます。

### Structured Log

```json
{"msg": "context-budget-warning", "profile": "default", "usage_ratio": 0.83, ...}
{"msg": "context-budget-trimmed", "profile": "default", "messages_removed": 5, ...}
```

## 検証用設定

検証用の providers.yaml が同梱されています:

```bash
cp examples/providers.v2-context-budget.yaml ~/.coderouter-t/providers.yaml
coderouter-t serve --port 8088 --log-level debug
```

閾値を下げて少ないデータ量で warn / trim をトリガーできるようになっています。詳細は `docs/inside/verification-v2.0-F.md` 参照。

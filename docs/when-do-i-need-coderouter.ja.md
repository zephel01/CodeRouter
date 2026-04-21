# CodeRouter は自分に必要か？

短く答えると、CodeRouter は **wire 翻訳 + 絆創膏の層** です。エージェントとモデルが同じ wire を喋り、モデルが素直に動いてくれるなら、CodeRouter はいりません。どちらかが崩れた瞬間に仕事が発生します。このページでは「エージェント × モデル」の 2 つのマトリクスで、1 分で判断できるようにします。

---

## 1. エージェント対応マトリクス

すべてのエージェントが `/v1` を任意のローカルエンドポイントに差し替えられるわけではありません。判断軸は「base-URL / endpoint の差し替え口があるか」と「どの wire を喋るか」の 2 つ。

| エージェント | wire 形式 | Ollama に直接向けられる？ | CodeRouter 必要？ |
|---|---|---|---|
| **Claude Code** | Anthropic `/v1/messages` | ✕ — Ollama は OpenAI しか喋らない | **必須** — 翻訳こそが本題 |
| **Codex CLI** (`@openai/codex`) | OpenAI | ◯ — `OPENAI_BASE_URL` で | オプション — フィルタ / フォールバックが欲しいときだけ |
| **素の OpenAI SDK / `curl`** | OpenAI | ◯ — `base_url` で | オプション — 同上 |
| **gemini-cli** | Gemini | ✕ — 別 wire | 必要（Gemini アダプタ追加時） |
| **GitHub Copilot CLI** (`gh copilot`) | GitHub 独自 | **✕ — バックエンド固定** | **無力** — Copilot 側が差し替え不可 |

要点: **Claude Code ユーザはブリッジ**（CodeRouter か相当品）**がないとローカルに到達できない**。**OpenAI 互換 CLI は Ollama に直接当てられる**ので、判断はモデル側に寄ります。

---

## 2. モデルの振る舞いマトリクス

CodeRouter の出力フィルタと修復ロジックは、**特定のモデル個性に対する絆創膏**です。お行儀の良いモデルでは、これらは静かに座っているだけです。

| モデルファミリ | 典型的な問題 | 効く対策 |
|---|---|---|
| `llama3.1` / `llama3.2` instruct (Q5+) | 通常なし | — |
| `mistral-nemo` / `mistral-small` | 通常なし | — |
| `phi-3` / `phi-4` | 通常なし | — |
| `qwen2.5`（`-coder` でない方） | 通常なし | — |
| **`qwen2.5-coder`**（全サイズ） | 出力に `<think>…</think>` が混ざる | `strip_thinking` |
| **`gpt-oss-120b` / `gpt-oss-20b`** | `<think>…</think>` を吐く | `strip_thinking` |
| **`deepseek-r1` / `qwq`**（reasoning 系） | 推論過程がそのまま応答に漏れる | `strip_thinking` |
| **小さい量子化**（Q2 / Q3） | tool_call JSON が壊れる | `repair_tool_call` |
| **テンプレート不整合の fine-tune / Modelfile** | `<\|eot_id\|>` / `<\|im_end\|>` 等の stop marker が漏れる | `strip_stop_markers` |

要点: 「お行儀の良いモデル + OpenAI 互換エージェント」が、CodeRouter の仕事が **一番少ない**組み合わせ。reasoning 系 (`r1` / `qwq` / `gpt-oss` / `qwen-coder`) と、小さい / マイナーな量子化に触り始めた瞬間から効いてきます。

---

## 3. `num_ctx` の落とし穴（全員の問題）

モデルに関係なく、ローカル環境を必ず噛むのがこれ: **Ollama のデフォルト `num_ctx` は 2048 トークン**。実コーディング用途には全く足りないサイズで、かつ Ollama は **無言で切り詰め**、エラーにしません。

- 直続き: リクエストごとに `num_ctx` を指定し、エージェントのシステムプロンプトが増えたら覚えて更新する
- CodeRouter 経由: `providers.yaml` に 1 箇所書いて終わり

これだけで CodeRouter を入れる理由には弱いですが、存在することは知っておいて損はない落とし穴です。

---

## 4. 直続きで行ける条件（チェックリスト）

**全部** チェックが付くなら、直続きで十分です:

- [ ] エージェントが OpenAI 互換（Codex CLI / 素の SDK / curl）
- [ ] モデルが「問題なし」リスト（または自分で確認済み）
- [ ] 単一プロバイダで運用（ローカル→クラウドのフォールバック不要）
- [ ] Anthropic `/v1/messages` ingress が不要
- [ ] `num_ctx` / `keep_alive` は自分で渡す運用で問題ない

1 つでも外れたら、CodeRouter は実際に仕事をします。

---

## 5. 自分で確かめる手順

何かを導入する前に、まず直続きが本当に動くか試すのが早いです:

```bash
# Codex CLI（あるいは任意の OpenAI 互換ツール）を Ollama に向ける
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=ollama  # ダミー。Ollama は無視
codex "write a function that reverses a string in rust"
```

以下 4 症状を観察:

1. **応答に `<think>` タグが見える** → reasoning 漏れ。`strip_thinking` が要る
2. **応答末尾に `<|eot_id|>` / `<|im_end|>` / `<|turn|>` などが残る** → テンプレート不整合。`strip_stop_markers` が要る
3. **「tool_call が返ってこない」とエージェントが言うのに、モデルは tool の JSON を素のテキストで吐いている** → `repair_tool_call` が要る
4. **長いプロンプトで応答が無言で切れる** → `num_ctx` が小さい

どれも起きずに 1 日実運用できたなら、CodeRouter は不要です。繰り返し出る症状があれば、対応するフィルタが解決策。

---

## 6. CodeRouter が必須になる場面

- **Claude Code を Anthropic 以外のモデルで動かす。** `/v1/messages` は Ollama には存在しないため、直続きの経路自体がありません
- **ローカル → 無料クラウド → 有料 の自動フォールバックを、mid-stream ガード付きで** 回したい。自前で書くと地味に難しい（[`docs/articles/zenn-02-coderouter-architecture.md`](./articles/zenn-02-coderouter-architecture.md) 参照）
- **`coderouter doctor` で不調を診断したい** — 6 プローブが上記の失敗モードを網羅しています

## 7. CodeRouter では解けない場面

- **GitHub Copilot CLI.** バックエンドが GitHub 固定で、どんなツールでも差し替え不可能
- **単一エージェント + 単一モデルの本番で「動く部品を最小に保ちたい」** ケース。直 `base_url` 差し替えの方が単純
- **CodeRouter にない機能が必要**（キャッシュ、埋め込み、細粒度コスト計測、会話ストアなど）。そこは LiteLLM か自前ラッパの領分

---

## まとめ

CodeRouter を入れる価値があるのは、**以下の少なくとも 1 つ**が当てはまるとき:

1. エージェントが Anthropic wire を喋る（Claude Code）
2. モデルが `<think>` / stop marker / 壊れた tool JSON を吐く
3. mid-stream ガード付きのティアフォールバックが欲しい
4. `doctor` でセットアップ不調を診断したい

それ以外は、`OPENAI_BASE_URL=http://localhost:11434/v1` というシンプルな一行が正解で、CodeRouter を使わない選択も十分妥当です。
